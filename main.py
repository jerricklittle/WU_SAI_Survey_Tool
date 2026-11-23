import os

from dotenv import load_dotenv
from nicegui import app, ui
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from Base import Base

import db_models  # noqa: F401 — register users + surveys + responses before create_all / backfill

load_dotenv()

try:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
except ImportError:
    pass

database_url = os.getenv("DATABASE_URL")
engine = create_engine(database_url)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)


def _ensure_survey_extra_columns() -> None:
    """Add columns missing on older DBs (create_all does not alter existing tables)."""
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import OperationalError

    insp = inspect(engine)
    if not insp.has_table("surveys"):
        return
    cols = {c["name"] for c in insp.get_columns("surveys")}
    specs = [
        ("participant_landing_html", "TEXT"),
        ("opens_at", "TIMESTAMP"),
        ("closes_at", "TIMESTAMP"),
        ("public_id", "VARCHAR(64)"),
        ("semester", "VARCHAR(64)"),
    ]
    for name, sql_type in specs:
        if name in cols:
            continue
        if engine.dialect.name == "postgresql":
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE surveys ADD COLUMN IF NOT EXISTS {name} {sql_type}"))
        else:
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE surveys ADD COLUMN {name} {sql_type}"))
            except OperationalError:
                pass


_ensure_survey_extra_columns()


def _ensure_study_columns() -> None:
    """Add the part column on databases created before survey parts existed."""
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import OperationalError, ProgrammingError

    insp = inspect(engine)
    if not insp.has_table("survey_questions") or not insp.has_table("survey_parts"):
        return
    cols = {c["name"] for c in insp.get_columns("survey_questions")}
    if "part_id" not in cols:
        if engine.dialect.name == "postgresql":
            stmt = (
                "ALTER TABLE survey_questions ADD COLUMN IF NOT EXISTS part_id INTEGER "
                "REFERENCES survey_parts(id) ON DELETE SET NULL"
            )
        else:
            stmt = (
                "ALTER TABLE survey_questions ADD COLUMN part_id INTEGER "
                "REFERENCES survey_parts(id) ON DELETE SET NULL"
            )
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except (OperationalError, ProgrammingError):
            pass
    part_cols = {c["name"] for c in insp.get_columns("survey_parts")}
    if "llm_followups" not in part_cols:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE survey_parts ADD COLUMN llm_followups BOOLEAN NOT NULL DEFAULT FALSE"
                ))
        except (OperationalError, ProgrammingError):
            pass
    if "llm_question_count" not in part_cols:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE survey_parts ADD COLUMN llm_question_count INTEGER NOT NULL DEFAULT 3"
                ))
        except (OperationalError, ProgrammingError):
            pass
    if "llm_instructions" not in part_cols:
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE survey_parts ADD COLUMN llm_instructions TEXT"))
        except (OperationalError, ProgrammingError):
            pass
    if "link_url" not in part_cols:
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE survey_parts ADD COLUMN link_url TEXT"))
        except (OperationalError, ProgrammingError):
            pass
    if "is_link" not in part_cols:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE survey_parts ADD COLUMN is_link BOOLEAN NOT NULL DEFAULT FALSE"
                ))
        except (OperationalError, ProgrammingError):
            pass
    if "one_per_page" not in part_cols:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE survey_parts ADD COLUMN one_per_page BOOLEAN NOT NULL DEFAULT TRUE"
                ))
        except (OperationalError, ProgrammingError):
            pass
    link_cols = {c["name"] for c in insp.get_columns("survey_part_arms")} if insp.has_table("survey_part_arms") else set()
    if "one_per_page" not in link_cols and insp.has_table("survey_part_arms"):
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE survey_part_arms ADD COLUMN one_per_page BOOLEAN NOT NULL DEFAULT TRUE"
                ))
                conn.execute(text(
                    "UPDATE survey_part_arms AS membership "
                    "SET one_per_page = part.one_per_page "
                    "FROM survey_parts AS part "
                    "WHERE membership.part_id = part.id"
                ))
        except (OperationalError, ProgrammingError):
            pass
    if "sort_order" not in link_cols and insp.has_table("survey_part_arms"):
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE survey_part_arms ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
                ))
                conn.execute(text(
                    "UPDATE survey_part_arms AS membership "
                    "SET sort_order = part.sort_order "
                    "FROM survey_parts AS part "
                    "WHERE membership.part_id = part.id"
                ))
        except (OperationalError, ProgrammingError):
            pass


_ensure_study_columns()


def _backfill_survey_public_ids() -> None:
    from survey_models import Survey, generate_survey_public_id

    sess = Session()
    try:
        pending = sess.query(Survey).filter(Survey.public_id.is_(None)).all()
        for s in pending:
            assigned = False
            for _ in range(40):
                pid = generate_survey_public_id()
                if sess.query(Survey).filter(Survey.public_id == pid).first() is None:
                    s.public_id = pid
                    assigned = True
                    break
            if not assigned:
                raise RuntimeError(
                    "Could not assign a unique survey public_id after many attempts; retry startup."
                )
        if pending:
            sess.commit()
    finally:
        sess.close()


_backfill_survey_public_ids()


def _ensure_response_survey_sid_unique_index() -> None:
    """One submitted response per (survey_id, sid) when both are set."""
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import OperationalError, ProgrammingError

    insp = inspect(engine)
    if not insp.has_table("responses"):
        return
    idx_name = "uq_responses_survey_id_sid"
    existing = {ix["name"] for ix in insp.get_indexes("responses")}
    if idx_name in existing:
        return
    stmt = f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON responses (survey_id, sid)"
    try:
        with engine.begin() as conn:
            conn.execute(text(stmt))
    except (OperationalError, ProgrammingError):
        pass


_ensure_response_survey_sid_unique_index()

import googleSSO  # noqa: E402, F401
import authentication  # noqa: E402, F401
import admin_panel  # noqa: E402, F401

from app_config import get_informed_consent_url  # noqa: E402
from survey_browser_flow import render_survey_entry_with_landing  # noqa: E402
from survey_from_db import sid_already_submitted, student_survey_access  # noqa: E402

TEAM = [
    ("Research team", "Principal investigators and research assistants"),
    ("Assessment & analytics", "Data handling under IR and FERPA guidance"),
]


@ui.page("/")
def landing_page():
    external_consent = get_informed_consent_url()
    with ui.column().classes("w-full min-h-screen bg-slate-50"):
        with ui.column().classes("max-w-3xl mx-auto px-6 py-12 gap-6"):
            ui.label("Survey DIVA").classes(
                "text-3xl md:text-4xl font-bold text-slate-900"
            )
            ui.label("Surveys and questionnaires").classes("text-xl text-slate-600 -mt-2")

            ui.markdown(
                """
Design a survey from parts, assign those parts to comparison groups, and send each
person a personal link. The link does not show which group they are in.

Participation is **by invitation**. Whoever sends the links keeps the list that
matches a link to a person. This application stores the response, not a name or
email address.
"""
            ).classes("text-slate-700 leading-relaxed")

            with ui.row().classes("flex-wrap gap-4 items-center"):
                ui.button(
                    "Informed consent (summary)",
                    on_click=lambda: ui.navigate.to("/consent"),
                ).classes("bg-blue-700 text-white px-4 py-2 rounded-lg")
                if external_consent:
                    ui.link("Official consent document (opens in new tab)", external_consent, new_tab=True).classes(
                        "text-blue-800 underline"
                    )

            ui.separator().classes("my-2")

            ui.label("Meet the team").classes("text-2xl font-bold text-slate-900")
            with ui.column().classes("gap-3 w-full"):
                for name, role in TEAM:
                    with ui.card().classes("w-full p-4 bg-white border border-slate-200 shadow-sm"):
                        ui.label(name).classes("font-semibold text-slate-900")
                        ui.label(role).classes("text-slate-600 text-sm")

            ui.separator().classes("my-2")

            ui.label("Staff access").classes("text-lg font-semibold text-slate-800")
            ui.label(
                "Survey editors and link managers sign in here."
            ).classes("text-sm text-slate-600")
            ui.button("Staff sign-in", on_click=lambda: ui.navigate.to("/login")).classes(
                "bg-slate-800 text-white px-5 py-2 rounded-lg w-fit"
            )


@ui.page("/consent")
def consent_page():
    external = get_informed_consent_url()
    with ui.column().classes("w-full min-h-screen bg-slate-50 px-6 py-10"):
        with ui.card().classes("max-w-3xl mx-auto w-full p-8 bg-white border border-slate-200"):
            ui.label("Informed consent — summary").classes("text-2xl font-bold text-slate-900 mb-4")
            ui.markdown(
                """
By submitting this survey, you acknowledge that you are providing your responses voluntarily and consent to the collection of the data you provide. Your participation in this survey is completely voluntary, and you may choose to exit the survey at any time without any negative consequences.
Please note that the responses you provide will only be used for research purposes and will remain confidential. This survey does not collect any identifying information. 
The link provided is unique to you. Please do not share it. The sender keeps the list that matches links to people. This survey does not store your name or email address.
"""
            ).classes("text-slate-700 leading-relaxed")
            if external:
                ui.link("Open the official informed consent document", external, new_tab=True).classes(
                    "text-blue-800 underline mt-4 block"
                )
            ui.button("← Back to home", on_click=lambda: ui.navigate.to("/")).classes(
                "mt-6 bg-slate-200 text-slate-800"
            )


@ui.page("/survey/thanks")
def survey_thanks_page():
    with ui.column().classes(
        "w-full min-h-[60vh] flex flex-col items-center justify-center px-6 py-12 bg-slate-50"
    ):
        with ui.card().classes("max-w-lg w-full p-8 text-center shadow-md"):
            ui.label("Thank you").classes("text-2xl font-bold text-slate-900 mb-3")
            ui.label(
                "Your responses were submitted successfully. You can close this window."
            ).classes("text-slate-600 mb-6")
            ui.button("Project home", on_click=lambda: ui.navigate.to("/")).classes(
                "bg-blue-700 text-white px-6 py-2 rounded-lg"
            )


@ui.page("/survey/{survey_public_id}")
def student_survey_entry(survey_public_id: str, request: Request):
    sid = (request.query_params.get("sid") or "").strip()
    if not sid:
        with ui.column().classes(
            "w-full min-h-screen bg-slate-100 flex flex-col items-center justify-center px-6"
        ):
            with ui.card().classes("max-w-lg w-full p-8 text-center"):
                ui.label("Personalized link required").classes("text-xl font-bold text-slate-900 mb-2")
                ui.label(
                    "Open the survey using the personal link you were sent. "
                    "It includes a secure participant code."
                ).classes("text-slate-600")
                ui.button("Project home", on_click=lambda: ui.navigate.to("/")).classes("mt-6")
        return

    survey = None
    survey_db_id = None
    access_error = None
    already_submitted = False
    db_session = Session()
    try:
        survey, access_error, survey_db_id = student_survey_access(db_session, survey_public_id)
        if survey is not None and survey_db_id is not None and not access_error:
            already_submitted = sid_already_submitted(db_session, survey_db_id, sid)
    finally:
        db_session.close()

    if access_error or survey is None or survey_db_id is None:
        with ui.column().classes(
            "w-full min-h-screen bg-slate-100 flex flex-col items-center justify-center px-6"
        ):
            with ui.card().classes("max-w-lg w-full p-8 text-center"):
                ui.label("Survey unavailable").classes("text-xl font-bold text-slate-900 mb-2")
                ui.label(access_error or "This survey is not available.").classes("text-slate-600")
                ui.button("Project home", on_click=lambda: ui.navigate.to("/")).classes("mt-6")
        return

    if already_submitted:
        with ui.column().classes(
            "w-full min-h-screen bg-slate-100 flex flex-col items-center justify-center px-6"
        ):
            with ui.card().classes("max-w-lg w-full p-8 text-center"):
                ui.label("Survey already completed").classes("text-xl font-bold text-slate-900 mb-2")
                ui.label(
                    "This link has already been used to submit a response for this survey. "
                    "If you believe this is a mistake, contact the person who sent your link."
                ).classes("text-slate-600")
                ui.button("Project home", on_click=lambda: ui.navigate.to("/")).classes("mt-6")
        return

    render_survey_entry_with_landing(Session, survey, survey_db_id, sid)


@ui.page("/s/{token}")
def participant_study_link(token: str):
    """Opaque survey link. Group and comparison arm are stored with the token, not in the URL."""
    from study_links import answers_from_payload, load_response_row, personalize_survey, response_is_complete
    from survey_from_db import load_survey_from_db
    from survey_models import CourseSection, ParticipantLink, Survey, SurveyArm

    token = (token or "").strip()
    db_session = Session()
    survey = None
    survey_db_id = None
    access_error = None
    already_submitted = False
    resume_answers = None
    completed_part_ids = None
    skip_intro = False
    try:
        link = db_session.query(ParticipantLink).filter_by(token=token).first()
        if link is None:
            access_error = "This survey link is not valid."
        else:
            arm = db_session.get(SurveyArm, link.arm_id)
            section = db_session.get(CourseSection, link.section_id)
            survey_row = db_session.get(Survey, link.survey_id)
            if arm is None or section is None or survey_row is None:
                access_error = "This survey link is not valid."
            else:
                base, access_error, survey_db_id = student_survey_access(db_session, survey_row.public_id or "")
                if base is not None and not access_error:
                    survey = personalize_survey(base, arm, section, survey_row.semester or "")
                    existing = load_response_row(db_session, survey_db_id, token)
                    if existing is not None and isinstance(existing.response, dict):
                        survey["priorDynamicQuestions"] = list(
                            existing.response.get("dynamic_questions") or []
                        )
                    if existing is not None and response_is_complete(existing.response):
                        already_submitted = True
                    elif existing is not None and isinstance(existing.response, dict):
                        resume_answers = answers_from_payload(existing.response)
                        completed_part_ids = [
                            int(part_id) for part_id in (existing.response.get("completedPartIds") or [])
                        ]
                        skip_intro = True
    finally:
        db_session.close()

    if access_error or survey is None or survey_db_id is None:
        with ui.column().classes(
            "w-full min-h-screen bg-slate-100 flex flex-col items-center justify-center px-6"
        ):
            with ui.card().classes("max-w-lg w-full p-8 text-center"):
                ui.label("Survey unavailable").classes("text-xl font-bold text-slate-900 mb-2")
                ui.label(access_error or "This survey is not available.").classes("text-slate-600")
                ui.button("Project home", on_click=lambda: ui.navigate.to("/")).classes("mt-6")
        return

    if already_submitted:
        with ui.column().classes(
            "w-full min-h-screen bg-slate-100 flex flex-col items-center justify-center px-6"
        ):
            with ui.card().classes("max-w-lg w-full p-8 text-center"):
                ui.label("Survey already completed").classes("text-xl font-bold text-slate-900 mb-2")
                ui.label(
                    "This link has already been used to submit a response for this survey. "
                    "If you believe this is a mistake, contact the person who sent your link."
                ).classes("text-slate-600")
                ui.button("Project home", on_click=lambda: ui.navigate.to("/")).classes("mt-6")
        return

    render_survey_entry_with_landing(
        Session,
        survey,
        survey_db_id,
        token,
        resume_answers=resume_answers,
        completed_part_ids=completed_part_ids,
        skip_intro=skip_intro,
    )


storage_secret = os.getenv("NICEGUI_STORAGE_SECRET", "your-secret-key-change-this-in-production-12345")
ui.run(storage_secret=storage_secret)
