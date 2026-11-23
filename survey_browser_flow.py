"""
Per-client survey session state and UI (avoids shared globals for concurrent respondents).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from nicegui import context, ui
from sqlalchemy.exc import IntegrityError

from Question_Timer import Question_Timer
from chatbot import analyze_all_responses_for_survey
from responses import Response
from study_links import upsert_part_response
from time_per_question import Time_Per_Question

_survey_sessions: dict[str, dict[str, Any]] = {}


def _cid() -> str:
    return str(context.client.id)


def _session() -> dict[str, Any]:
    cid = _cid()
    if cid not in _survey_sessions:
        _survey_sessions[cid] = {}
    return _survey_sessions[cid]


def reset_survey_session(
    survey: dict[str, Any],
    survey_db_id: int,
    sid: str,
    resume_answers: dict[str, Any] | None = None,
    completed_part_ids: list[int] | None = None,
) -> None:
    static_count = len(survey.get("questions", []))
    state: dict[str, Any] = {
        "timer": Question_Timer(),
        "survey": survey,
        "survey_db_id": survey_db_id,
        "sid": sid,
        "current_index": 0,
        "answers": dict(resume_answers or {}),
        "survey_state": {"mode": "static", "static_count": static_count},
        "dynamic_questions": [],
        "archived_dynamic": _archived_from_prior(survey),
        "part_mode": bool(survey.get("partMode")),
        "parts": list(survey.get("parts") or []),
        "study": dict(survey.get("study") or {}),
        "part_index": 0,
    }
    _survey_sessions[_cid()] = state
    if state["part_mode"] and state["parts"]:
        done = {int(part_id) for part_id in (completed_part_ids or [])}
        start = 0
        while start < len(state["parts"]) and int(state["parts"][start]["id"]) in done:
            start += 1
        if start >= len(state["parts"]):
            start = 0
        _activate_part(state, start)


def clear_survey_session() -> None:
    _survey_sessions.pop(_cid(), None)


def _archived_from_prior(survey: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep follow-ups from earlier parts when a student resumes after a partial save."""
    archived: list[dict[str, Any]] = []
    for item in survey.pop("priorDynamicQuestions", None) or []:
        if not isinstance(item, dict) or not item.get("questionId"):
            continue
        archived.append(
            {
                "id": str(item["questionId"]),
                "type": "text",
                "prompt": item.get("questionPrompt") or "",
            }
        )
    return archived


def _dynamic_question_list(s: dict[str, Any]) -> list[dict[str, Any]]:
    return list(s.get("archived_dynamic") or []) + list(s.get("dynamic_questions") or [])


def _get_all_questions(s: dict[str, Any]) -> list[dict[str, Any]]:
    return s["survey"]["questions"] + s["dynamic_questions"]


def _save_answer(s: dict[str, Any], question: dict[str, Any], value: Any) -> None:
    """Store answer with prompt text so stored responses (esp. AI follow-ups) stay interpretable."""
    item: dict[str, Any] = {
        "questionId": question["id"],
        "questionType": question["type"],
        "value": value,
    }
    prompt = question.get("prompt")
    if prompt is not None and str(prompt).strip():
        item["questionPrompt"] = str(prompt).strip()
    triggered = question.get("triggered_by")
    if triggered:
        item["triggeredByQuestionId"] = triggered
    s["answers"][question["id"]] = item


def _handle_text_answer(s: dict[str, Any], question: dict[str, Any], value: str) -> None:
    _save_answer(s, question, value)


def _build_submission_payload(s: dict[str, Any], survey: dict[str, Any], uuid_str: str) -> dict[str, Any]:
    """Static answers stay in ``answers``; AI follow-ups go in ``dynamic_questions`` (variable length blob)."""
    dynamic_ids = {str(q["id"]) for q in _dynamic_question_list(s) if q.get("id")}
    answers_by_id: dict[str, Any] = dict(s.get("answers") or {})

    static_answers: list[dict[str, Any]] = []
    seen_static: set[str] = set()
    for q in survey.get("questions", []) or []:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip()
        if not qid or qid in dynamic_ids:
            continue
        item = answers_by_id.get(qid)
        if isinstance(item, dict):
            static_answers.append(item)
            seen_static.add(qid)
    for qid, item in answers_by_id.items():
        if qid in dynamic_ids or qid in seen_static:
            continue
        if isinstance(item, dict):
            static_answers.append(item)

    blob: list[dict[str, Any]] = []
    for dq in _dynamic_question_list(s):
        if not isinstance(dq, dict):
            continue
        qid = str(dq.get("id") or "").strip()
        if not qid:
            continue
        item = answers_by_id.get(qid)
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("questionPrompt") or dq.get("prompt") or "").strip()
        blob.append(
            {
                "questionId": qid,
                "questionPrompt": prompt,
                "response": item.get("value"),
            }
        )

    listed = {b["questionId"] for b in blob}
    for qid in sorted(dynamic_ids):
        if qid in listed:
            continue
        item = answers_by_id.get(qid)
        if not isinstance(item, dict):
            continue
        blob.append(
            {
                "questionId": qid,
                "questionPrompt": str(item.get("questionPrompt") or "").strip(),
                "response": item.get("value"),
            }
        )

    return {
        "id": uuid_str,
        "surveyId": survey["id"],
        "surveyVersion": survey["surveyVersion"],
        "submittedAt": datetime.utcnow().isoformat(),
        "answers": static_answers,
        "dynamic_questions": blob,
        "sid": s["sid"],
    }


def submit_survey(dialog, session_factory, s: dict[str, Any]) -> None:
    session = session_factory()
    try:
        existing = (
            session.query(Response.id)
            .filter_by(survey_id=s["survey_db_id"], sid=s["sid"])
            .first()
        )
        if existing is not None:
            s["timer"].stop_all()
            dialog.close()
            clear_survey_session()
            ui.notify(
                "This survey link was already used to submit a response.",
                type="warning",
                position="top",
            )
            ui.navigate.to("/")
            return

        s["timer"].stop_all()
        uuid_str = str(uuid.uuid4())
        survey = s["survey"]
        submission = _build_submission_payload(s, survey, uuid_str)

        response = Response(
            response=submission,
            uuid=uuid_str,
            sid=s["sid"],
            survey_id=s["survey_db_id"],
        )
        session.add(response)
        session.flush()
        response_id = response.id

        timing_data = s["timer"].get_all_times()
        for question_id, time_seconds in timing_data.items():
            session.add(
                Time_Per_Question(
                    response_id=response_id,
                    question_id=question_id,
                    time_spent=time_seconds,
                )
            )

        session.commit()
    except IntegrityError:
        session.rollback()
        s["timer"].stop_all()
        dialog.close()
        clear_survey_session()
        ui.notify(
            "This survey link was already used to submit a response.",
            type="warning",
            position="top",
        )
        ui.navigate.to("/")
        return
    finally:
        session.close()

    dialog.close()
    clear_survey_session()
    ui.navigate.to("/survey/thanks")
    try:
        ui.navigate.history.replace("/survey/thanks")
    except AttributeError:
        pass


def _queue_submit(s: dict[str, Any]) -> None:
    if s.get("part_mode"):
        _queue_part_submit(s)
        return
    dialog = s.get("_dialog")
    session_factory = s.get("_session_factory")
    if dialog is None or session_factory is None:
        return

    client = s.get("_nicegui_client") or context.client

    def _run() -> None:
        submit_survey(dialog, session_factory, s)

    # ui.timer must be created inside a client slot; async tasks have no slot after await.
    with client:
        ui.timer(0.05, _run, once=True)


def _activate_part(s: dict[str, Any], index: int) -> None:
    parts = s.get("parts") or []
    if not parts:
        return
    index = max(0, min(index, len(parts) - 1))
    part = parts[index]
    by_id = {str(q.get("id")): q for q in s["survey"].get("questions") or []}
    # The full arm question list is kept on the survey until the first activation replaces it.
    if "arm_questions" not in s:
        s["arm_questions"] = list(s["survey"].get("questions") or [])
        by_id = {str(q.get("id")): q for q in s["arm_questions"]}
    chosen = [by_id[qid] for qid in part.get("questionIds") or [] if qid in by_id]
    s["survey"]["questions"] = chosen
    s["current_part_question_ids"] = [str(q.get("id")) for q in chosen]
    s["dynamic_questions"] = []
    s["current_index"] = 0
    s["part_index"] = index
    s["survey_state"] = {"mode": "static", "static_count": len(chosen)}


def _queue_part_submit(s: dict[str, Any]) -> None:
    client = s.get("_nicegui_client") or context.client

    def _run() -> None:
        _commit_current_part(s)

    with client:
        ui.timer(0.05, _run, once=True)


def _commit_current_part(s: dict[str, Any]) -> None:
    dialog = s.get("_dialog")
    session_factory = s.get("_session_factory")
    parts = s.get("parts") or []
    index = int(s.get("part_index") or 0)
    if dialog is None or session_factory is None or not parts:
        return
    part = parts[index]
    survey = s["survey"]
    is_final = index >= len(parts) - 1
    submission = _build_submission_payload(s, survey, s.get("study", {}).get("armKey") or "part")
    session = session_factory()
    try:
        s["timer"].stop_all()
        upsert_part_response(
            session,
            survey_db_id=s["survey_db_id"],
            survey_public_id=str(survey.get("id") or ""),
            survey_version=int(survey.get("surveyVersion") or 1),
            token=s["sid"],
            study=s.get("study") or {},
            part=part,
            static_answers=submission["answers"],
            dynamic_blob=submission["dynamic_questions"],
            is_final=is_final,
            timing=s["timer"].get_all_times(),
        )
    except Exception:
        session.rollback()
        ui.notify("Could not save this part. Please try again.", type="negative", position="top")
        return
    finally:
        session.close()

    s.setdefault("archived_dynamic", []).extend(list(s.get("dynamic_questions") or []))
    s["dynamic_questions"] = []
    if is_final:
        dialog.close()
        clear_survey_session()
        ui.navigate.to("/survey/thanks")
        try:
            ui.navigate.history.replace("/survey/thanks")
        except AttributeError:
            pass
        return

    _activate_part(s, index + 1)
    s["survey_state"]["mode"] = "static"
    survey_page.refresh()


async def _generate_dynamic_questions(s: dict[str, Any], refresh_fn) -> None:
    client = s.get("_nicegui_client") or context.client
    text_responses = []
    survey = s["survey"]
    allowed = {str(qid) for qid in (s.get("current_part_question_ids") or [])}
    for q_id, answer in s["answers"].items():
        if allowed and str(q_id) not in allowed:
            continue
        if answer["questionType"] == "text" and answer.get("value"):
            original_q = None
            for q in survey["questions"]:
                if q["id"] == q_id:
                    original_q = q
                    break
            if original_q and original_q.get("adaptive", False):
                text_value = str(answer["value"]).strip()
                if len(text_value) >= 15:
                    text_responses.append(
                        {
                            "question_id": q_id,
                            "text": text_value,
                            "prompt": original_q["prompt"],
                            "prompt_text": original_q.get("prompt_text"),
                        }
                    )

    if not text_responses:
        _queue_submit(s)
        return

    try:
        result = await analyze_all_responses_for_survey(text_responses)
        data = json.loads(result)
        if data.get("needs_followup"):
            for idx, fq in enumerate(data.get("followup_questions", []), 1):
                source_q = fq.get("source_question_id", "unknown")
                s["dynamic_questions"].append(
                    {
                        "id": f"followup_{source_q}_{idx}",
                        "type": "text",
                        "prompt": fq["prompt"],
                        "triggered_by": source_q,
                    }
                )
        s["survey_state"]["mode"] = "transition" if s["dynamic_questions"] else "complete"
    except Exception:
        s["survey_state"]["mode"] = "complete"

    if s["survey_state"]["mode"] == "complete":
        _queue_submit(s)
    else:
        with client:
            refresh_fn()


def _finalize_static_block(s: dict[str, Any], refresh_fn) -> None:
    s["_nicegui_client"] = context.client
    s["survey_state"]["mode"] = "loading"
    refresh_fn()
    task = asyncio.create_task(_generate_dynamic_questions(s, refresh_fn))

    def _consume_task_exc(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            import logging

            logging.getLogger(__name__).error("Survey follow-up step failed", exc_info=exc)

    task.add_done_callback(_consume_task_exc)


def _next_page(s: dict[str, Any], refresh_fn) -> None:
    current = s["current_index"]

    all_questions = _get_all_questions(s)
    if current == len(all_questions) - 1 and s["survey_state"]["mode"] == "dynamic":
        _queue_submit(s)
        return

    if current < len(_get_all_questions(s)) - 1:
        s["current_index"] += 1
        refresh_fn()


def _prev_page(s: dict[str, Any], refresh_fn) -> None:
    current = s["current_index"]
    static_count = s["survey_state"]["static_count"]

    if s["survey_state"]["mode"] == "dynamic" and current <= static_count:
        ui.notify("Cannot return to previous questions", type="warning", position="top")
        return

    if s["current_index"] > 0:
        s["current_index"] -= 1
        refresh_fn()


def _advance_to_dynamic(s: dict[str, Any], refresh_fn) -> None:
    s["survey_state"]["mode"] = "dynamic"
    s["current_index"] += 1
    refresh_fn()


@ui.refreshable
def survey_page(dialog, session_factory):
    s = _session()
    if not s.get("survey"):
        return

    survey = s["survey"]
    mode = s["survey_state"]["mode"]
    refresh = survey_page.refresh

    if mode == "loading":
        with ui.column().classes(
            "w-full min-h-0 max-h-full flex-1 bg-gray-100 flex flex-col items-center justify-center gap-4 py-6"
        ):
            with ui.card().classes(
                "w-full max-w-xl bg-white shadow-lg border border-gray-200 rounded-lg px-8 py-12 text-center"
            ):
                ui.label("Analyzing your responses...").classes(
                    "text-2xl font-bold text-gray-800 mb-4"
                )
                ui.spinner(size="lg", color="blue-600")
        return

    if mode == "transition":
        with ui.column().classes(
            "w-full min-h-0 max-h-full flex-1 bg-gray-100 flex flex-col items-center justify-center gap-4 py-6"
        ):
            with ui.card().classes(
                "w-full max-w-xl bg-white shadow-lg border border-gray-200 rounded-lg px-8 py-12 text-center"
            ):
                ui.label("Thank you for your feedback!").classes(
                    "text-3xl font-bold text-gray-800 mb-3"
                )
                n = len(s["dynamic_questions"])
                ui.label(
                    f"We have {n} follow-up question{'s' if n != 1 else ''}"
                ).classes("text-lg text-gray-600 mb-6")
                ui.button(
                    "Continue",
                    on_click=lambda: _advance_to_dynamic(s, refresh),
                ).classes("bg-blue-600 text-white text-lg px-8 py-3 rounded-lg hover:bg-blue-700")
        return

    questions = _get_all_questions(s)
    if not questions:
        with ui.column().classes("w-full items-center justify-center py-12"):
            ui.label("This link has no questions to show.").classes("text-lg text-gray-700")
        return
    q = questions[s["current_index"]]
    total = len(questions)
    current = s["current_index"]
    s["timer"].start_question(q["id"])

    with ui.column().classes(
        "w-full min-h-0 max-h-full flex-1 overflow-y-auto bg-gray-100 flex flex-col items-center justify-start py-4 gap-3"
    ):
        with ui.card().classes(
            "w-full max-w-xl bg-white shadow-sm border border-gray-200 rounded-lg px-6 py-5"
        ):
            ui.label(survey.get("title", "SAI survey")).classes(
                "text-2xl font-bold text-gray-800"
            )
            if s.get("part_mode") and s.get("parts"):
                part = s["parts"][s.get("part_index") or 0]
                ui.label(
                    f'{part.get("title") or "Part"} · Part {int(s.get("part_index") or 0) + 1} of {len(s["parts"])}'
                ).classes("text-sm text-gray-500 mt-1")
            ui.label(f"Question {current + 1} of {total}").classes("text-xs text-gray-400 mt-2")
            ui.linear_progress(value=(current + 1) / total, color="blue-600").classes("mt-1 h-1")

        with ui.card().classes(
            "w-full max-w-xl bg-white shadow-sm border border-gray-200 rounded-lg px-6 py-6"
        ):
            ui.label(q["prompt"]).classes("text-base font-semibold text-gray-800 mb-4")

            if q["type"] == "likert":
                labels = q["scale"]["labels"]
                options = [label for _, label in sorted(labels.items(), key=lambda x: int(x[0]))]
                ui.radio(
                    options,
                    value=s["answers"].get(q["id"], {}).get("value"),
                    on_change=lambda e: _save_answer(s, q, e.value),
                ).classes("gap-2")

            elif q["type"] == "boolean":
                ui.radio(
                    [q["options"]["trueLabel"], q["options"]["falseLabel"]],
                    value=s["answers"].get(q["id"], {}).get("value"),
                    on_change=lambda e: _save_answer(
                        s, q, e.value == q["options"]["trueLabel"]
                    ),
                ).classes("gap-2")

            elif q["type"] == "text":
                textarea = ui.textarea(
                    placeholder=q.get("text", {}).get("placeholder", ""),
                    value=s["answers"].get(q["id"], {}).get("value", ""),
                ).classes("w-full")
                textarea.on(
                    "blur",
                    lambda e: _handle_text_answer(s, q, textarea.value),
                )

            elif q["type"] == "multi":
                opts = q.get("options") or []
                current_vals = list(s["answers"].get(q["id"], {}).get("value") or [])
                ui.select(
                    opts,
                    multiple=True,
                    label="Select all that apply",
                    value=current_vals,
                    on_change=lambda e: _save_answer(s, q, list(e.value or [])),
                ).classes("w-full")

            with ui.row().classes("w-full justify-between mt-6"):
                show_back = survey["settings"].get("allowBack", True) and current > 0
                if s["survey_state"]["mode"] == "dynamic" and current == s["survey_state"]["static_count"]:
                    show_back = False

                if show_back:
                    ui.button("Back", on_click=lambda: _prev_page(s, refresh)).classes(
                        "bg-gray-200 text-gray-700 hover:bg-gray-300"
                    )
                else:
                    ui.label("")

                is_last_static = (
                    current == s["survey_state"]["static_count"] - 1
                    and s["survey_state"]["mode"] == "static"
                )
                is_truly_last = current >= len(questions) - 1
                part_label = "Submit"
                part_needs_llm = False
                if s.get("part_mode") and s.get("parts"):
                    part_label = s["parts"][s.get("part_index") or 0].get("submitLabel") or "Submit"
                    part_ids = set(s.get("current_part_question_ids") or [])
                    part_needs_llm = any(
                        bool(question.get("adaptive")) and str(question.get("id")) in part_ids
                        for question in (s["survey"].get("questions") or [])
                    )

                if is_last_static and s.get("part_mode") and not part_needs_llm:
                    ui.button(
                        part_label,
                        on_click=lambda: _queue_part_submit(s),
                    ).classes("bg-green-600 text-white hover:bg-green-700")
                elif is_last_static:
                    ui.button(
                        part_label if s.get("part_mode") else "Submit",
                        on_click=lambda: _finalize_static_block(s, refresh),
                    ).classes("bg-green-600 text-white hover:bg-green-700")
                elif is_truly_last:
                    ui.button(
                        part_label if s.get("part_mode") else "Submit",
                        on_click=lambda: _queue_part_submit(s) if s.get("part_mode") else submit_survey(dialog, session_factory, s),
                    ).classes("bg-green-600 text-white hover:bg-green-700")
                else:
                    ui.button(
                        "Next",
                        on_click=lambda: _next_page(s, refresh),
                    ).classes("bg-blue-600 text-white hover:bg-blue-700")


def render_survey_flow(
    session_factory,
    survey: dict[str, Any],
    survey_db_id: int,
    sid: str,
    resume_answers: dict[str, Any] | None = None,
    completed_part_ids: list[int] | None = None,
) -> None:
    with ui.dialog().props("maximized") as dialog:
        reset_survey_session(
            survey,
            survey_db_id,
            sid,
            resume_answers=resume_answers,
            completed_part_ids=completed_part_ids,
        )
        st = _session()
        st["_dialog"] = dialog
        st["_session_factory"] = session_factory
        st["_nicegui_client"] = context.client
        survey_page(dialog, session_factory)

    dialog.open()


def render_survey_entry_with_landing(
    session_factory,
    survey: dict[str, Any],
    survey_db_id: int,
    sid: str,
    *,
    resume_answers: dict[str, Any] | None = None,
    completed_part_ids: list[int] | None = None,
    skip_intro: bool = False,
) -> None:
    """
    Optional HTML landing (instructions, consent) before opening the maximized survey dialog.
    Content is authored by staff in the admin survey editor (trusted HTML).

    Do not ``root.clear()`` before opening the dialog: the dialog can be a descendant of
    that column, and clearing it removes the dialog → blank page (especially on Railway).
    """
    def proceed() -> None:
        landing_block.visible = False
        if note and not skip_intro:
            note_block.visible = True
            return
        render_survey_flow(
            session_factory,
            survey,
            survey_db_id,
            sid,
            resume_answers=resume_answers,
            completed_part_ids=completed_part_ids,
        )

    def start_after_note() -> None:
        note_block.visible = False
        render_survey_flow(
            session_factory,
            survey,
            survey_db_id,
            sid,
            resume_answers=resume_answers,
            completed_part_ids=completed_part_ids,
        )

    landing = "" if skip_intro else (survey.get("participant_landing_html") or "").strip()
    note = "" if skip_intro else (survey.get("armExternalNoteHtml") or "").strip()
    root = ui.column().classes("w-full bg-slate-50 items-stretch")
    landing_block = ui.column().classes("w-full")
    note_block = ui.column().classes("w-full")

    with root:
        with landing_block:
            if landing:
                with ui.column().classes("max-w-3xl mx-auto w-full px-4 py-8 gap-6"):
                    ui.label(survey.get("title", "Survey")).classes("text-2xl font-bold text-slate-900")
                    ui.html(landing, sanitize=False).classes(
                        "survey-landing text-slate-800 text-base leading-relaxed [&_a]:text-blue-700 [&_a]:underline"
                    )
                    ui.button(
                        "Continue to the survey",
                        on_click=proceed,
                    ).classes("bg-blue-700 text-white px-6 py-2 rounded-lg w-fit")
            else:
                landing_block.visible = False
        with note_block:
            note_block.visible = False
            if note:
                with ui.column().classes("max-w-3xl mx-auto w-full px-4 py-8 gap-6"):
                    ui.label(survey.get("title", "Survey")).classes("text-2xl font-bold text-slate-900")
                    ui.html(note, sanitize=False).classes(
                        "survey-landing text-slate-800 text-base leading-relaxed [&_a]:text-blue-700 [&_a]:underline"
                    )
                    ui.button(
                        "Continue",
                        on_click=start_after_note,
                    ).classes("bg-blue-700 text-white px-6 py-2 rounded-lg w-fit")
        if not landing and not note:
            ui.timer(
                0.05,
                lambda: render_survey_flow(
                    session_factory,
                    survey,
                    survey_db_id,
                    sid,
                    resume_answers=resume_answers,
                    completed_part_ids=completed_part_ids,
                ),
                once=True,
            )
        elif not landing and note and not skip_intro:
            note_block.visible = True
