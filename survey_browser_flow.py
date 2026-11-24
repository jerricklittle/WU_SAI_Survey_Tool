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
from chatbot import analyze_all_responses_for_survey, analyze_part_followups
from study_links import normalize_llm_question_count, upsert_part_response
from responses import Response
from time_per_question import Time_Per_Question

_survey_sessions: dict[str, dict[str, Any]] = {}


def _group_heading(study: dict[str, Any] | None) -> str:
    """Respondent-facing label: Group (Category - Code)."""
    study = study or {}
    group = str(study.get("group") or study.get("instructor") or "").strip()
    category = str(study.get("category") or study.get("course") or "").strip()
    code = str(study.get("code") or study.get("section") or "").strip()
    detail = " - ".join(piece for piece in (category, code) if piece)
    if group and detail:
        return f"{group} ({detail})"
    return group or detail


def _show_group_heading(study: dict[str, Any] | None) -> None:
    heading = _group_heading(study)
    if heading:
        ui.label(heading).classes("text-sm font-semibold text-slate-600")


def _thanks_path(study: dict[str, Any] | None) -> str:
    heading = _group_heading(study)
    if not heading:
        return "/survey/thanks"
    from urllib.parse import quote
    return f"/survey/thanks?context={quote(heading)}"


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


def _is_answered(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    value = item.get("value")
    if value is None or value == "":
        return False
    if isinstance(value, list) and not value:
        return False
    return True


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
    thanks = _thanks_path(s.get("study"))
    ui.navigate.to(thanks)
    try:
        ui.navigate.history.replace(thanks)
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


def _activate_part(s: dict[str, Any], index: int, *, introduce: bool = False) -> None:
    parts = s.get("parts") or []
    if not parts:
        return
    index = max(0, min(index, len(parts) - 1))
    part = parts[index]
    # The first part replaces survey["questions"] with its own subset. Later parts must
    # still resolve their questions from the full list captured before that replacement.
    if "arm_questions" not in s:
        s["arm_questions"] = list(s["survey"].get("questions") or [])
    by_id = {str(q.get("id")): q for q in s.get("arm_questions") or []}
    chosen = [by_id[qid] for qid in part.get("questionIds") or [] if qid in by_id]
    s["survey"]["questions"] = chosen
    s["current_part_question_ids"] = [str(q.get("id")) for q in chosen]
    s["dynamic_questions"] = []
    s["current_index"] = 0
    s["part_index"] = index
    s["llm_part"] = bool(part.get("llmFollowups"))
    s["link_url"] = "" if s["llm_part"] else str(part.get("linkUrl") or "").strip()
    s["link_part"] = bool(s["link_url"])
    s["one_per_page"] = bool(part.get("onePerPage", True))
    s["llm_started"] = False
    s["llm_question_count"] = normalize_llm_question_count(part.get("llmQuestionCount"))
    s["llm_instructions"] = str(part.get("llmInstructions") or "")
    s["part_context"] = str(part.get("contextText") or "").strip()
    previous = parts[index - 1] if index else None
    s["llm_source_ids"] = [str(qid) for qid in (previous.get("questionIds") or [])] if previous else []
    # Context, and the screen after an LLM part, wait for Start before the questions begin.
    mode = "part_intro" if s["part_context"] or introduce else "static"
    s["survey_state"] = {"mode": mode, "static_count": len(chosen)}


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
    leaving_llm = bool(s.get("llm_part"))
    if is_final:
        dialog.close()
        clear_survey_session()
        thanks = _thanks_path(s.get("study"))
        ui.navigate.to(thanks)
        try:
            ui.navigate.history.replace(thanks)
        except AttributeError:
            pass
        return

    _activate_part(s, index + 1, introduce=leaving_llm)
    survey_page.refresh()


async def _generate_dynamic_questions(s: dict[str, Any], refresh_fn) -> None:
    client = s.get("_nicegui_client") or context.client
    text_responses = []
    survey = s["survey"]
    lookup = list(s.get("arm_questions") or []) + list(survey.get("questions") or [])
    if s.get("llm_part"):
        source_ids = {str(qid) for qid in (s.get("llm_source_ids") or [])}
        for q_id, answer in s["answers"].items():
            if str(q_id) not in source_ids or answer.get("questionType") != "text" or not answer.get("value"):
                continue
            text_value = str(answer["value"]).strip()
            if not text_value:
                continue
            original_q = next((q for q in lookup if q.get("id") == q_id), None)
            text_responses.append(
                {
                    "question_id": q_id,
                    "text": text_value,
                    "prompt": (original_q or {}).get("prompt") or answer.get("questionPrompt") or "",
                }
            )
        if not text_responses:
            _queue_submit(s)
            return
        try:
            result = await analyze_part_followups(
                text_responses,
                s.get("llm_question_count") or 3,
                s.get("llm_instructions") or "",
            )
            data = json.loads(result)
            limit = normalize_llm_question_count(s.get("llm_question_count"))
            if data.get("needs_followup"):
                for idx, fq in enumerate(data.get("followup_questions") or [], 1):
                    prompt = str((fq or {}).get("prompt") or "").strip()
                    if not prompt:
                        continue
                    s["dynamic_questions"].append(
                        {
                            "id": f"followup_{idx}",
                            "type": "text",
                            "prompt": prompt,
                            "triggered_by": "previous_part",
                        }
                    )
                    if len(s["dynamic_questions"]) >= limit:
                        break
            s["survey_state"]["mode"] = "transition" if s["dynamic_questions"] else "complete"
        except Exception:
            s["survey_state"]["mode"] = "complete"
        if s["survey_state"]["mode"] == "complete":
            _queue_submit(s)
        else:
            with client:
                refresh_fn()
        return
    else:
        allowed = {str(qid) for qid in (s.get("current_part_question_ids") or [])}
        for q_id, answer in s["answers"].items():
            if allowed and str(q_id) not in allowed:
                continue
            if answer["questionType"] == "text" and answer.get("value"):
                original_q = None
                for q in lookup:
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


def _render_question_inputs(s: dict[str, Any], q: dict[str, Any], refresh_fn) -> None:
    """Draw one question. Choice answers update the progress bar."""

    def store(value, question=q) -> None:
        if value is None:
            return
        previous = s["answers"].get(question["id"], {}).get("value")
        if previous == value:
            return
        _save_answer(s, question, value)
        refresh_fn()

    if q["type"] == "likert":
        labels = q["scale"]["labels"]
        options = [label for _, label in sorted(labels.items(), key=lambda x: int(x[0]))]
        ui.radio(
            options,
            value=s["answers"].get(q["id"], {}).get("value"),
            on_change=lambda e: store(e.value),
        ).classes("gap-2")
    elif q["type"] == "boolean":
        ui.radio(
            [q["options"]["trueLabel"], q["options"]["falseLabel"]],
            value=s["answers"].get(q["id"], {}).get("value"),
            on_change=lambda e: store(e.value == q["options"]["trueLabel"]),
        ).classes("gap-2")
    elif q["type"] == "text":
        textarea = ui.textarea(
            placeholder=q.get("text", {}).get("placeholder", ""),
            value=s["answers"].get(q["id"], {}).get("value", ""),
        ).classes("w-full")
        def remember_text(field=textarea, question=q) -> None:
            previous = s["answers"].get(question["id"], {}).get("value", "")
            current = field.value or ""
            if current == (previous or ""):
                return
            _save_answer(s, question, current)
            refresh_fn()

        textarea.on("blur", lambda e: remember_text())
    elif q["type"] == "multi":
        opts = q.get("options") or []
        current_vals = list(s["answers"].get(q["id"], {}).get("value") or [])
        ui.select(
            opts,
            multiple=True,
            label="Select all that apply",
            value=current_vals,
            on_change=lambda e: store(list(e.value or [])),
        ).classes("w-full")


def _render_all_questions(s: dict[str, Any], survey: dict[str, Any], refresh_fn) -> None:
    questions = list(survey.get("questions") or [])
    total = len(questions)
    answered = sum(1 for q in questions if _is_answered(s["answers"].get(q["id"])))
    if questions:
        s["timer"].start_question(questions[0]["id"])
    part = (s.get("parts") or [{}])[int(s.get("part_index") or 0)]
    part_label = part.get("submitLabel") or "Submit"
    with ui.column().classes(
        "w-full min-h-0 max-h-full flex-1 overflow-y-auto bg-gray-100 flex flex-col items-center justify-start py-4 gap-3"
    ):
        with ui.card().classes(
            "w-full max-w-xl bg-white shadow-sm border border-gray-200 rounded-lg px-6 py-5"
        ):
            _show_group_heading(s.get("study"))
            ui.label(survey.get("title", "Survey")).classes("text-2xl font-bold text-gray-800")
            if s.get("parts"):
                ui.label(
                    f'{part.get("title") or "Part"} · Part {int(s.get("part_index") or 0) + 1} of {len(s["parts"])}'
                ).classes("text-sm text-gray-500 mt-1")
            ui.label(f"{answered} of {total} answered").classes("text-xs text-gray-400 mt-2")
            ui.linear_progress(
                value=(answered / total) if total else 0,
                show_value=False,
                size="10px",
                color="blue-600",
            ).classes("mt-2")
        for index, q in enumerate(questions, 1):
            with ui.card().classes(
                "w-full max-w-xl bg-white shadow-sm border border-gray-200 rounded-lg px-6 py-6"
            ):
                ui.label(f"{index}. {q['prompt']}").classes("text-base font-semibold text-gray-800 mb-4")
                _render_question_inputs(s, q, refresh_fn)
        ui.button(
            part_label,
            on_click=lambda: _queue_part_submit(s),
        ).classes("bg-green-600 text-white hover:bg-green-700 mb-6")


def _begin_part(s: dict[str, Any], refresh_fn) -> None:
    s["survey_state"]["mode"] = "static"
    refresh_fn()


def _advance_to_dynamic(s: dict[str, Any], refresh_fn) -> None:
    s["survey_state"]["mode"] = "dynamic"
    # An LLM part has no questions of its own, so the first follow-up is index 0.
    # A legacy survey starts follow-ups immediately after the last authored question.
    s["current_index"] = int(s["survey_state"].get("static_count") or 0)
    refresh_fn()


@ui.refreshable
def survey_page(dialog, session_factory):
    s = _session()
    if not s.get("survey"):
        return

    survey = s["survey"]
    mode = s["survey_state"]["mode"]
    refresh = survey_page.refresh
    if (
        s.get("part_mode")
        and s.get("llm_part")
        and mode == "static"
        and not (survey.get("questions") or [])
        and not s.get("dynamic_questions")
        and not s.get("llm_started")
    ):
        s["llm_started"] = True
        ui.timer(0.05, lambda: _finalize_static_block(s, refresh), once=True)
        mode = "loading"

    if mode == "loading":
        with ui.column().classes(
            "w-full min-h-0 max-h-full flex-1 bg-gray-100 flex flex-col items-center justify-center gap-4 py-6"
        ):
            with ui.card().classes(
                "w-full max-w-xl bg-white shadow-lg border border-gray-200 rounded-lg px-8 py-12 text-center"
            ):
                _show_group_heading(s.get("study"))
                ui.label("Analyzing your responses...").classes(
                    "text-2xl font-bold text-gray-800 mb-4"
                )
                ui.spinner(size="lg", color="blue-600")
        return

    if mode == "part_intro":
        parts = s.get("parts") or []
        part_index = int(s.get("part_index") or 0)
        part = parts[part_index] if 0 <= part_index < len(parts) else {}
        title = str(part.get("title") or "Next section").strip() or "Next section"
        context = str(s.get("part_context") or "").strip()
        with ui.column().classes(
            "w-full min-h-0 max-h-full flex-1 overflow-y-auto bg-gray-100 flex flex-col items-center justify-center gap-4 py-6"
        ):
            with ui.card().classes(
                "w-full max-w-xl bg-white shadow-lg border border-gray-200 rounded-lg px-8 py-12 text-center"
            ):
                _show_group_heading(s.get("study"))
                ui.label(title).classes("text-3xl font-bold text-gray-800 mb-2")
                if parts:
                    ui.label(f"Part {part_index + 1} of {len(parts)}").classes(
                        "text-sm text-gray-500 mb-4"
                    )
                if context:
                    ui.label(context).classes(
                        "whitespace-pre-wrap text-left text-gray-700 mb-6"
                    )
                else:
                    ui.label("The next section is ready.").classes(
                        "text-lg text-gray-600 mb-6"
                    )
                ui.button(
                    "Start",
                    on_click=lambda: _begin_part(s, refresh),
                ).classes("bg-blue-600 text-white text-lg px-8 py-3 rounded-lg hover:bg-blue-700")
        return

    if mode == "transition":
        with ui.column().classes(
            "w-full min-h-0 max-h-full flex-1 bg-gray-100 flex flex-col items-center justify-center gap-4 py-6"
        ):
            with ui.card().classes(
                "w-full max-w-xl bg-white shadow-lg border border-gray-200 rounded-lg px-8 py-12 text-center"
            ):
                _show_group_heading(s.get("study"))
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

    if s.get("part_mode") and s.get("link_part") and s.get("link_url"):
        part = (s.get("parts") or [{}])[int(s.get("part_index") or 0)]
        continue_label = part.get("submitLabel") or "Continue"
        with ui.column().classes(
            "w-full min-h-0 max-h-full flex-1 bg-gray-100 flex flex-col items-center justify-center gap-4 py-6"
        ):
            with ui.card().classes(
                "w-full max-w-xl bg-white shadow-lg border border-gray-200 rounded-lg px-8 py-10"
            ):
                _show_group_heading(s.get("study"))
                ui.label(survey.get("title", "Survey")).classes("text-2xl font-bold text-gray-800")
                if s.get("parts"):
                    ui.label(
                        f'{part.get("title") or "Part"} · Part {int(s.get("part_index") or 0) + 1} of {len(s["parts"])}'
                    ).classes("text-sm text-gray-500 mt-1 mb-4")
                ui.label(
                    "Open the page, finish what you need to do there, then come back and continue."
                ).classes("text-gray-700 mb-4")
                ui.link(
                    "Open page",
                    s["link_url"],
                    new_tab=True,
                ).classes(
                    "inline-block bg-blue-600 text-white text-lg px-8 py-3 rounded-lg hover:bg-blue-700 no-underline"
                )
                ui.button(
                    continue_label,
                    on_click=lambda: _queue_part_submit(s),
                ).classes("bg-green-600 text-white mt-6")
        return

    questions = _get_all_questions(s)
    show_all = (
        s.get("part_mode")
        and not s.get("one_per_page", True)
        and not s.get("llm_part")
        and not s.get("link_part")
        and s["survey_state"]["mode"] == "static"
    )
    if show_all and questions:
        _render_all_questions(s, survey, refresh)
        return
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
            _show_group_heading(s.get("study"))
            ui.label(survey.get("title", "Survey")).classes(
                "text-2xl font-bold text-gray-800"
            )
            if s.get("part_mode") and s.get("parts"):
                part = s["parts"][s.get("part_index") or 0]
                ui.label(
                    f'{part.get("title") or "Part"} · Part {int(s.get("part_index") or 0) + 1} of {len(s["parts"])}'
                ).classes("text-sm text-gray-500 mt-1")
            ui.label(f"Question {current + 1} of {total}").classes("text-xs text-gray-400 mt-2")
            ui.linear_progress(
                value=(current + 1) / total,
                show_value=False,
                size="10px",
                color="blue-600",
            ).classes("mt-2")

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
                    part_needs_llm = bool(s.get("llm_part"))

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
                    _show_group_heading(survey.get("study"))
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
                    _show_group_heading(survey.get("study"))
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
