"""Treatment arms, survey parts, class sections, and opaque participant links."""

from __future__ import annotations

import csv
import io
import random
import secrets
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from responses import Response
from survey_models import (
    CourseSection,
    ParticipantLink,
    Survey,
    SurveyArm,
    SurveyPart,
    SurveyPartArm,
    SurveyQuestion,
)
from time_per_question import Time_Per_Question

DEFAULT_ARMS: list[tuple[str, str, bool, str]] = [
    (
        "negative_control",
        "Negative control (Canvas SAI)",
        False,
        "<p>Your Student Assessment of Instruction for this course is in Canvas. "
        "After you finish it there, continue to the short survey about the experience.</p>",
    ),
    (
        "positive_control",
        "Positive control (no LLM follow-ups)",
        False,
        "",
    ),
    (
        "experimental",
        "Experimental (LLM follow-ups)",
        True,
        "",
    ),
]


def arm_for_seat(section_id: int, seat_index: int, arm_ids: list[int]) -> int:
    """Blocked assignment: each block of len(arms) seats contains every arm once, shuffled."""
    count = len(arm_ids)
    if count == 0:
        raise ValueError("No treatment arms")
    block = seat_index // count
    rng = random.Random(f"{section_id}:{block}")
    order = list(arm_ids)
    rng.shuffle(order)
    return order[seat_index % count]


def response_is_complete(payload: Any) -> bool:
    """Legacy rows have no completed flag and were stored only at final submit."""
    if not isinstance(payload, dict):
        return True
    if "completed" not in payload:
        return True
    return bool(payload.get("completed"))


def load_response_row(session, survey_id: int, sid: str) -> Response | None:
    key = (sid or "").strip()
    if not key:
        return None
    return (
        session.query(Response)
        .filter(Response.survey_id == survey_id, Response.sid == key)
        .first()
    )


def ensure_default_arms(session, survey_id: int) -> list[SurveyArm]:
    existing = (
        session.query(SurveyArm)
        .filter_by(survey_id=survey_id)
        .order_by(SurveyArm.sort_order, SurveyArm.id)
        .all()
    )
    if existing:
        return existing
    created: list[SurveyArm] = []
    for index, (key, label, llm, note) in enumerate(DEFAULT_ARMS, 1):
        arm = SurveyArm(
            survey_id=survey_id,
            key=key,
            label=label,
            llm_followups=llm,
            external_note_html=note or None,
            sort_order=index,
        )
        session.add(arm)
        created.append(arm)
    session.flush()
    return created


def ordered_arms(session, survey_id: int) -> list[SurveyArm]:
    return (
        session.query(SurveyArm)
        .filter_by(survey_id=survey_id)
        .order_by(SurveyArm.sort_order, SurveyArm.id)
        .all()
    )


def ordered_parts(session, survey_id: int) -> list[SurveyPart]:
    return (
        session.query(SurveyPart)
        .filter_by(survey_id=survey_id)
        .order_by(SurveyPart.sort_order, SurveyPart.id)
        .all()
    )


def _new_token(session) -> str:
    for _ in range(40):
        token = secrets.token_urlsafe(18)
        if session.query(ParticipantLink).filter_by(token=token).first() is None:
            return token
    raise RuntimeError("Could not allocate a unique participant token")


def ensure_section_links(session, section: CourseSection) -> list[ParticipantLink]:
    """Create missing seats up to enrollment. Already issued tokens are kept."""
    arms = ordered_arms(session, section.survey_id)
    if not arms:
        raise ValueError("Add treatment arms before generating links")
    arm_ids = [arm.id for arm in arms]
    existing = {
        link.seat_index: link
        for link in session.query(ParticipantLink).filter_by(section_id=section.id).all()
    }
    enrollment = max(int(section.enrollment or 0), 0)
    for seat in range(enrollment):
        if seat in existing:
            continue
        link = ParticipantLink(
            token=_new_token(session),
            survey_id=section.survey_id,
            section_id=section.id,
            arm_id=arm_for_seat(section.id, seat, arm_ids),
            seat_index=seat,
        )
        session.add(link)
        existing[seat] = link
    session.flush()
    return [existing[seat] for seat in sorted(existing)]


def iter_study_link_rows(session, survey_id: int, base_url: str) -> list[list[str]]:
    """Ensure links for every section, then return CSV rows including the header."""
    survey = session.get(Survey, survey_id)
    if survey is None:
        raise ValueError("Survey not found")
    semester = (survey.semester or "").strip()
    sections = (
        session.query(CourseSection)
        .filter_by(survey_id=survey_id)
        .order_by(CourseSection.course_code, CourseSection.section_code, CourseSection.id)
        .all()
    )
    arms = {arm.id: arm for arm in ordered_arms(session, survey_id)}
    base = (base_url or "").rstrip("/")
    rows: list[list[str]] = [[
        "semester",
        "instructor",
        "course",
        "section",
        "arm",
        "url",
    ]]
    for section in sections:
        links = ensure_section_links(session, section)
        for link in links:
            arm = arms.get(link.arm_id)
            token = link.token
            url = f"{base}/s/{token}" if base else f"/s/{token}"
            rows.append([
                semester,
                section.instructor_name,
                section.course_code,
                section.section_code,
                arm.label if arm else "",
                url,
            ])
    return rows


def study_links_csv(session, survey_id: int, base_url: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(iter_study_link_rows(session, survey_id, base_url))
    return buf.getvalue()


def clone_study_structure(session, src_survey_id: int, dest_survey_id: int) -> dict[int, int]:
    """Copy arms and parts onto a new survey. Returns old part id -> new part id."""
    arm_map: dict[int, int] = {}
    for arm in ordered_arms(session, src_survey_id):
        copied = SurveyArm(
            survey_id=dest_survey_id,
            key=arm.key,
            label=arm.label,
            llm_followups=bool(arm.llm_followups),
            external_note_html=arm.external_note_html,
            sort_order=arm.sort_order,
        )
        session.add(copied)
        session.flush()
        arm_map[arm.id] = copied.id

    part_map: dict[int, int] = {}
    source_parts = ordered_parts(session, src_survey_id)
    source_part_ids = [part.id for part in source_parts]
    links = (
        session.query(SurveyPartArm).filter(SurveyPartArm.part_id.in_(source_part_ids)).all()
        if source_part_ids
        else []
    )
    links_by_part: dict[int, list[int]] = {}
    for link in links:
        links_by_part.setdefault(link.part_id, []).append(link.arm_id)
    for part in source_parts:
        copied = SurveyPart(
            survey_id=dest_survey_id,
            title=part.title,
            submit_label=part.submit_label or "Submit",
            sort_order=part.sort_order,
        )
        session.add(copied)
        session.flush()
        part_map[part.id] = copied.id
        for old_arm_id in links_by_part.get(part.id, []):
            new_arm_id = arm_map.get(old_arm_id)
            if new_arm_id is not None:
                session.add(SurveyPartArm(part_id=copied.id, arm_id=new_arm_id))
    return part_map


def personalize_survey(payload: dict[str, Any], arm: SurveyArm, section: CourseSection, semester: str) -> dict[str, Any]:
    """Restrict a survey payload to one arm. The student URL does not carry these fields."""
    import copy

    data = copy.deepcopy(payload)
    questions = list(data.get("questions") or [])
    by_id = {str(q.get("id")): q for q in questions if isinstance(q, dict)}
    parts_out: list[dict[str, Any]] = []
    ordered_questions: list[dict[str, Any]] = []
    for part in data.get("parts") or []:
        if arm.key not in (part.get("armKeys") or []):
            continue
        ids = [qid for qid in part.get("questionIds") or [] if qid in by_id]
        if not ids:
            continue
        parts_out.append({
            "id": part["id"],
            "title": part.get("title") or "Part",
            "submitLabel": part.get("submitLabel") or "Submit",
            "questionIds": ids,
        })
        ordered_questions.extend(by_id[qid] for qid in ids)
    if data.get("parts"):
        data["questions"] = ordered_questions
        data["parts"] = parts_out
        data["partMode"] = True
    else:
        data["partMode"] = False
        data["parts"] = []
    data.pop("arms", None)
    if not arm.llm_followups:
        for question in data.get("questions") or []:
            question["adaptive"] = False
            question.pop("prompt_text", None)
    data["armExternalNoteHtml"] = (arm.external_note_html or "").strip()
    data["study"] = {
        "armKey": arm.key,
        "armLabel": arm.label,
        "llmFollowups": bool(arm.llm_followups),
        "sectionId": section.id,
        "semester": (semester or "").strip(),
        "instructor": section.instructor_name,
        "course": section.course_code,
        "section": section.section_code,
    }
    return data


def answers_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    stored: dict[str, Any] = {}
    for item in payload.get("answers") or []:
        if isinstance(item, dict) and item.get("questionId"):
            stored[str(item["questionId"])] = item
    for item in payload.get("dynamic_questions") or []:
        if not isinstance(item, dict) or not item.get("questionId"):
            continue
        stored[str(item["questionId"])] = {
            "questionId": item.get("questionId"),
            "questionType": "text",
            "value": item.get("response"),
            "questionPrompt": item.get("questionPrompt") or "",
        }
    return stored


def upsert_part_response(
    session,
    *,
    survey_db_id: int,
    survey_public_id: str,
    survey_version: int,
    token: str,
    study: dict[str, Any],
    part: dict[str, Any],
    static_answers: list[dict[str, Any]],
    dynamic_blob: list[dict[str, Any]],
    is_final: bool,
    timing: dict[str, float],
) -> Response:
    row = load_response_row(session, survey_db_id, token)
    now = datetime.utcnow().isoformat()
    if row is None:
        payload: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "surveyId": survey_public_id,
            "surveyVersion": survey_version,
            "sid": token,
            "answers": [],
            "dynamic_questions": [],
            "completedPartIds": [],
            "parts": {},
            "completed": False,
        }
        row = Response(
            response=payload,
            uuid=payload["id"],
            sid=token,
            survey_id=survey_db_id,
        )
        session.add(row)
        session.flush()
    else:
        payload = dict(row.response or {})

    completed_ids = [int(part_id) for part_id in (payload.get("completedPartIds") or [])]
    part_id = int(part["id"])
    if part_id not in completed_ids:
        completed_ids.append(part_id)
    parts_meta = dict(payload.get("parts") or {})
    parts_meta[str(part_id)] = {
        "title": part.get("title") or "",
        "submittedAt": now,
    }
    payload.update({
        "surveyId": survey_public_id,
        "surveyVersion": survey_version,
        "submittedAt": now,
        "sid": token,
        "answers": static_answers,
        "dynamic_questions": dynamic_blob,
        "completedPartIds": completed_ids,
        "parts": parts_meta,
        "completed": bool(is_final),
        "arm": study.get("armKey") or "",
        "armLabel": study.get("armLabel") or "",
        "sectionId": study.get("sectionId"),
        "semester": study.get("semester") or "",
        "instructor": study.get("instructor") or "",
        "course": study.get("course") or "",
        "section": study.get("section") or "",
    })
    row.response = payload
    flag_modified(row, "response")

    kept = {
        item.question_id: float(item.time_spent or 0)
        for item in session.query(Time_Per_Question).filter_by(response_id=row.id).all()
    }
    for question_id, seconds in timing.items():
        kept[str(question_id)] = float(seconds)
    session.query(Time_Per_Question).filter_by(response_id=row.id).delete(synchronize_session=False)
    for question_id, seconds in kept.items():
        session.add(
            Time_Per_Question(
                response_id=row.id,
                question_id=str(question_id),
                time_spent=float(seconds),
            )
        )
    session.commit()
    return row


def question_part_ids(session, survey_id: int) -> dict[int, int | None]:
    rows = session.query(SurveyQuestion).filter_by(survey_id=survey_id).all()
    return {row.id: row.part_id for row in rows}
