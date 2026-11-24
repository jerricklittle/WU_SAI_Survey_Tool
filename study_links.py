"""Comparison groups, survey parts, respondent groups, and opaque participant links."""

from __future__ import annotations

import csv
import io
import random
import re
import secrets
import uuid
import zipfile
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

def normalize_link_url(value) -> str:
    """Accept a page address with or without a scheme."""
    url = str(value or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = "https://" + url
    return url


def normalize_llm_question_count(value, default: int = 3) -> int:
    """Keep an LLM part's question count between 1 and 10."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(1, min(count, 10))


DEFAULT_ARMS: list[tuple[str, str, bool, str]] = [
    (
        "negative_control",
        "Negative control",
        False,
        "",
    ),
    (
        "positive_control",
        "Positive control",
        False,
        "",
    ),
    (
        "experimental",
        "Experimental",
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
        "period",
        "group",
        "category",
        "code",
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


_LINK_SHEET_HEADERS = ["period", "group", "category", "code", "url"]


def _section_workbook_label(section: CourseSection) -> str:
    """File name stem: Group (Category - Code)."""
    group = (section.instructor_name or "").strip()
    category = (section.course_code or "").strip()
    code = (section.section_code or "").strip()
    detail = " - ".join(part for part in (category, code) if part)
    if group and detail:
        return f"{group} ({detail})"
    return group or detail or f"Group {section.id}"


def _unique_clipped(label: str, used: set[str], limit: int, empty: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(label or "")).strip() or empty
    number = 1
    while True:
        suffix = "" if number == 1 else f" {number}"
        room = max(limit - len(suffix), 1)
        candidate = f"{cleaned[:room].rstrip()}{suffix}"[:limit] or f"{empty}{suffix}"[:limit]
        if candidate.lower() not in used:
            used.add(candidate.lower())
            return candidate
        number += 1


def _excel_sheet_name(label: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\\/*?:\[\]]+", " ", str(label or ""))
    return _unique_clipped(cleaned, used, 31, "Control")


def _excel_filename(label: str, used: set[str]) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", str(label or ""))
    stem = _unique_clipped(cleaned, used, 80, "Group")
    return f"{stem}.xlsx"


def _workbook_bytes(sheets: list[tuple[str, list[list[str]]]]) -> bytes:
    """One workbook. Each item is a sheet title plus data rows (no header)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    used_names: set[str] = set()
    for index, (label, rows) in enumerate(sheets):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        sheet.title = _excel_sheet_name(label, used_names)
        sheet.append(list(_LINK_SHEET_HEADERS))
        for row in rows:
            sheet.append(list(row))
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:E{max(sheet.max_row, 1)}"
        for column, width in enumerate((18, 28, 18, 14, 72), 1):
            sheet.column_dimensions[get_column_letter(column)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def study_link_workbooks(session, survey_id: int, base_url: str) -> list[tuple[str, bytes]]:
    """One spreadsheet per group. Each control is its own sheet."""
    survey = session.get(Survey, survey_id)
    if survey is None:
        raise ValueError("Survey not found")
    semester = (survey.semester or "").strip()
    sections = (
        session.query(CourseSection)
        .filter_by(survey_id=survey_id)
        .order_by(CourseSection.instructor_name, CourseSection.course_code, CourseSection.section_code, CourseSection.id)
        .all()
    )
    arms = ordered_arms(session, survey_id)
    if not arms:
        raise ValueError("Add treatment arms before generating links")
    base = (base_url or "").rstrip("/")
    files: list[tuple[str, bytes]] = []
    used_names: set[str] = set()
    for section in sections:
        links = ensure_section_links(session, section)
        by_arm: dict[int, list[list[str]]] = {arm.id: [] for arm in arms}
        unassigned: list[list[str]] = []
        for link in links:
            url = f"{base}/s/{link.token}" if base else f"/s/{link.token}"
            row = [
                semester,
                section.instructor_name,
                section.course_code,
                section.section_code,
                url,
            ]
            bucket = by_arm.get(link.arm_id)
            if bucket is None:
                unassigned.append(row)
            else:
                bucket.append(row)
        sheets = [(arm.label or arm.key or "Control", by_arm[arm.id]) for arm in arms]
        if unassigned:
            sheets.append(("Unassigned", unassigned))
        filename = _excel_filename(_section_workbook_label(section), used_names)
        files.append((filename, _workbook_bytes(sheets)))
    return files


def study_links_package(session, survey_id: int, base_url: str) -> tuple[bytes, str, str]:
    """A zip of one spreadsheet per group. Each spreadsheet has a sheet per control."""
    survey = session.get(Survey, survey_id)
    if survey is None:
        raise ValueError("Survey not found")
    files = study_link_workbooks(session, survey_id, base_url)
    if not files:
        raise ValueError("Add at least one group")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for filename, body in files:
            bundle.writestr(filename, body)
    survey_name = re.sub(r"[^\w\-.]+", "_", (survey.name or "survey").strip()).strip("._") or "survey"
    return archive.getvalue(), f"{survey_name}_ir_links.zip", "application/zip"


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
            llm_followups=bool(part.llm_followups),
            llm_question_count=normalize_llm_question_count(part.llm_question_count),
            llm_instructions=part.llm_instructions,
            context_text=part.context_text,
            link_url=part.link_url,
            is_link=bool(part.is_link),
            one_per_page=bool(part.one_per_page),
            sort_order=part.sort_order,
            placeholder_tags=normalize_placeholder_tags(part.placeholder_tags) or None,
        )
        session.add(copied)
        session.flush()
        part_map[part.id] = copied.id
        for old_arm_id in links_by_part.get(part.id, []):
            new_arm_id = arm_map.get(old_arm_id)
            if new_arm_id is not None:
                session.add(SurveyPartArm(
                    part_id=copied.id,
                    arm_id=new_arm_id,
                    sort_order=next(
                        (link.sort_order for link in links if link.part_id == part.id and link.arm_id == old_arm_id),
                        1,
                    ),
                    one_per_page=next(
                        (
                            bool(link.one_per_page)
                            for link in links
                            if link.part_id == part.id and link.arm_id == old_arm_id
                        ),
                        True,
                    ),
                ))
    return part_map


_PLACEHOLDER = re.compile(r"\[([A-Za-z][A-Za-z0-9_]*)\]")


def normalize_placeholder_tag(name: str, tag: str) -> dict[str, str] | None:
    """A tag is a label plus the token used in survey text, such as [Instructor]."""
    label = str(name or "").strip()
    token = str(tag or "").strip().strip("[]")
    if not label or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", token):
        return None
    return {"name": label, "tag": token}


def normalize_placeholder_tags(raw) -> list[dict[str, str]]:
    items = raw if isinstance(raw, list) else []
    tags: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        tag = normalize_placeholder_tag(item.get("name"), item.get("tag"))
        if tag is None or tag["tag"].lower() in seen:
            continue
        seen.add(tag["tag"].lower())
        tags.append(tag)
    return tags


def placeholder_values(section: CourseSection) -> dict[str, str]:
    """Group values for tags defined on the survey parts."""
    stored = section.placeholders if isinstance(section.placeholders, dict) else {}
    values: dict[str, str] = {}
    for name, value in stored.items():
        token = str(name or "").strip().strip("[]")
        text = str(value or "").strip()
        if token and text:
            values[token.lower()] = text
    return values


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    if not text or not values or "[" not in text:
        return text

    def replace(match: re.Match) -> str:
        value = values.get(match.group(1).lower())
        if not value:
            return match.group(0)
        return value

    return _PLACEHOLDER.sub(replace, text)


def apply_survey_placeholders(data: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Fill [Name] tokens in the text a respondent reads."""
    if not values:
        return data
    for key in ("title", "description", "participant_landing_html", "armExternalNoteHtml"):
        if isinstance(data.get(key), str):
            data[key] = fill_placeholders(data[key], values)
    for question in data.get("questions") or []:
        if not isinstance(question, dict):
            continue
        if isinstance(question.get("prompt"), str):
            question["prompt"] = fill_placeholders(question["prompt"], values)
        scale = question.get("scale")
        if isinstance(scale, dict) and isinstance(scale.get("labels"), dict):
            scale["labels"] = {
                label_key: fill_placeholders(label, values) if isinstance(label, str) else label
                for label_key, label in scale["labels"].items()
            }
        options = question.get("options")
        if isinstance(options, list):
            question["options"] = [
                fill_placeholders(option, values) if isinstance(option, str) else option
                for option in options
            ]
        elif isinstance(options, dict):
            for option_key, option in list(options.items()):
                if isinstance(option, str):
                    options[option_key] = fill_placeholders(option, values)
        text_cfg = question.get("text")
        if isinstance(text_cfg, dict) and isinstance(text_cfg.get("placeholder"), str):
            text_cfg["placeholder"] = fill_placeholders(text_cfg["placeholder"], values)
    for part in data.get("parts") or []:
        if not isinstance(part, dict):
            continue
        for key in ("title", "contextText", "linkUrl"):
            if isinstance(part.get(key), str):
                part[key] = fill_placeholders(part[key], values)
    return data


def personalize_survey(payload: dict[str, Any], arm: SurveyArm, section: CourseSection, semester: str) -> dict[str, Any]:
    """Restrict a survey payload to one comparison group. The respondent URL does not carry these fields."""
    import copy

    data = copy.deepcopy(payload)
    questions = list(data.get("questions") or [])
    by_id = {str(q.get("id")): q for q in questions if isinstance(q, dict)}
    parts_out: list[dict[str, Any]] = []
    ordered_questions: list[dict[str, Any]] = []
    included = [
        part for part in (data.get("parts") or [])
        if arm.key in (part.get("armKeys") or [])
    ]
    included.sort(key=lambda part: ((part.get("armOrder") or {}).get(arm.key, 0), part.get("id") or 0))
    for part in included:
        ids = [qid for qid in part.get("questionIds") or [] if qid in by_id]
        is_llm = bool(part.get("llmFollowups"))
        link_url = normalize_link_url(part.get("linkUrl"))
        is_link = bool(part.get("isLink")) and not is_llm and bool(link_url)
        if not ids and not is_llm and not is_link:
            continue
        parts_out.append({
            "id": part["id"],
            "title": part.get("title") or "Part",
            "submitLabel": part.get("submitLabel") or "Submit",
            "llmFollowups": is_llm,
            "llmQuestionCount": normalize_llm_question_count(part.get("llmQuestionCount")),
            "llmInstructions": part.get("llmInstructions") or "",
            "contextText": part.get("contextText") or "",
            "linkUrl": link_url if is_link else "",
            "onePerPage": bool((part.get("armPaging") or {}).get(arm.key, part.get("onePerPage", True))),
            "questionIds": [] if is_llm or is_link else ids,
        })
        if not is_llm and not is_link:
            ordered_questions.extend(by_id[qid] for qid in ids)
    if data.get("parts"):
        data["questions"] = ordered_questions
        data["parts"] = parts_out
        data["partMode"] = True
    else:
        data["partMode"] = False
        data["parts"] = []
    data.pop("arms", None)
    if not any(part.get("llmFollowups") for part in parts_out):
        for question in data.get("questions") or []:
            question["adaptive"] = False
            question.pop("prompt_text", None)
    data["armExternalNoteHtml"] = (arm.external_note_html or "").strip()
    data["study"] = {
        "armKey": arm.key,
        "armLabel": arm.label,
        "llmFollowups": any(part.get("llmFollowups") for part in parts_out),
        "sectionId": section.id,
        "period": (semester or "").strip(),
        "group": section.instructor_name,
        "category": section.course_code,
        "code": section.section_code,
        "fill": placeholder_values(section),
    }
    apply_survey_placeholders(data, data["study"]["fill"])
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
        "linkUrl": part.get("linkUrl") or "",
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
        "period": study.get("period") or study.get("semester") or "",
        "group": study.get("group") or study.get("instructor") or "",
        "category": study.get("category") or study.get("course") or "",
        "code": study.get("code") or study.get("section") or "",
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


def save_response_draft(
    session,
    *,
    survey_db_id: int,
    survey_public_id: str,
    survey_version: int,
    token: str,
    study: dict[str, Any],
    static_answers: list[dict[str, Any]],
    dynamic_blob: list[dict[str, Any]],
) -> None:
    """Store answers before a part is finished. Does not mark the part or the survey complete."""
    row = load_response_row(session, survey_db_id, token)
    if row is not None and response_is_complete(row.response):
        return
    now = datetime.utcnow().isoformat()
    meta = {
        "surveyId": survey_public_id,
        "surveyVersion": survey_version,
        "submittedAt": now,
        "sid": token,
        "answers": list(static_answers),
        "dynamic_questions": list(dynamic_blob),
        "completed": False,
        "arm": study.get("armKey") or "",
        "armLabel": study.get("armLabel") or "",
        "sectionId": study.get("sectionId"),
        "period": study.get("period") or study.get("semester") or "",
        "group": study.get("group") or study.get("instructor") or "",
        "category": study.get("category") or study.get("course") or "",
        "code": study.get("code") or study.get("section") or "",
    }
    if row is None:
        payload = {
            "id": str(uuid.uuid4()),
            "completedPartIds": [],
            "parts": {},
            **meta,
        }
        row = Response(
            response=payload,
            uuid=payload["id"],
            sid=token,
            survey_id=survey_db_id,
        )
        session.add(row)
    else:
        payload = dict(row.response or {})
        payload.update(meta)
        row.response = payload
        flag_modified(row, "response")
    session.commit()


def question_part_ids(session, survey_id: int) -> dict[int, int | None]:
    rows = session.query(SurveyQuestion).filter_by(survey_id=survey_id).all()
    return {row.id: row.part_id for row in rows}
