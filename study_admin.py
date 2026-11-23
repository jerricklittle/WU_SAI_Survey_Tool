"""Admin controls for treatment arms, parts, class sections, and IR link files."""

import re

from nicegui import ui

from study_links import (
    ensure_default_arms,
    ordered_arms,
    ordered_parts,
    study_links_csv,
)
from survey_models import CourseSection, ParticipantLink, Survey, SurveyArm, SurveyPart, SurveyPartArm, SurveyQuestion


def _session():
    from admin_panel import Session
    return Session()


@ui.refreshable
def study_design_panel(survey_id: int) -> None:
    survey_id = int(survey_id)
    session = _session()
    try:
        survey = session.get(Survey, survey_id)
        if survey is None:
            return
        arms = ordered_arms(session, survey_id)
        parts = ordered_parts(session, survey_id)
        sections = (
            session.query(CourseSection)
            .filter_by(survey_id=survey_id)
            .order_by(CourseSection.course_code, CourseSection.section_code, CourseSection.id)
            .all()
        )
        memberships = (
            session.query(SurveyPartArm).filter(SurveyPartArm.part_id.in_([part.id for part in parts])).all()
            if parts
            else []
        )
        arm_ids_by_part: dict[int, set[int]] = {}
        for row in memberships:
            arm_ids_by_part.setdefault(row.part_id, set()).add(row.arm_id)
        issued = {}
        if sections:
            rows = (
                session.query(ParticipantLink.section_id)
                .filter(ParticipantLink.section_id.in_([section.id for section in sections]))
                .all()
            )
            for (section_id,) in rows:
                issued[section_id] = issued.get(section_id, 0) + 1
        snapshot = {
            'arms': [
                {
                    'id': arm.id,
                    'key': arm.key,
                    'label': arm.label,
                    'llm': bool(arm.llm_followups),
                    'note': arm.external_note_html or '',
                }
                for arm in arms
            ],
            'parts': [
                {
                    'id': part.id,
                    'title': part.title,
                    'submit': part.submit_label or 'Submit',
                    'arm_ids': arm_ids_by_part.get(part.id, set()),
                }
                for part in parts
            ],
            'sections': [
                {
                    'id': section.id,
                    'instructor': section.instructor_name,
                    'course': section.course_code,
                    'section': section.section_code,
                    'enrollment': section.enrollment,
                    'issued': issued.get(section.id, 0),
                }
                for section in sections
            ],
        }
    finally:
        session.close()

    with ui.card().classes('w-full p-6 mt-4'):
        ui.label('Study design').classes('text-2xl font-bold mb-1')
        ui.label(
            'One survey covers every class. Arms decide which parts a student sees. '
            'Links hide the instructor, course, section, semester, and arm.'
        ).classes('text-sm text-gray-600 mb-4')

        ui.label('Treatment arms').classes('text-lg font-semibold mb-2')
        if not snapshot['arms']:
            ui.label('No arms yet. Add the three study arms, then assign parts to them.').classes('text-sm text-gray-500 mb-2')
            ui.button('Add negative, positive, and experimental arms', on_click=lambda: _add_default_arms(survey_id)).classes(
                'bg-blue-600 text-white mb-4'
            )
        else:
            for arm in snapshot['arms']:
                with ui.card().classes('w-full p-3 mb-2 bg-slate-50'):
                    ui.label(arm['key']).classes('text-xs text-gray-500')
                    label_input = ui.input('Label', value=arm['label']).classes('w-full')
                    llm = ui.checkbox('LLM follow-up questions', value=arm['llm'])
                    note = ui.textarea('Note shown before the survey (optional)', value=arm['note']).classes('w-full')
                    ui.button(
                        'Save arm',
                        on_click=lambda arm_id=arm['id'], label_input=label_input, llm=llm, note=note: _save_arm(
                            arm_id, label_input.value, llm.value, note.value
                        ),
                    ).props('flat dense no-caps').classes('text-blue-700')

        ui.separator().classes('my-4')
        ui.label('Parts').classes('text-lg font-semibold mb-1')
        ui.label(
            'Each part ends with its own submit button. Check the arms that should see that part. '
            'Once this survey has parts, a question that is not assigned to a part is left out of the student survey.'
        ).classes('text-sm text-gray-600 mb-2')
        with ui.row().classes('gap-2 items-end flex-wrap mb-3'):
            part_title = ui.input('New part title', placeholder='Classification questions').classes('w-64')
            ui.button('Add part', on_click=lambda: _add_part(survey_id, part_title.value)).classes('bg-green-600 text-white')

        arm_options = {str(arm['id']): arm['label'] for arm in snapshot['arms']}
        for index, part in enumerate(snapshot['parts']):
            with ui.card().classes('w-full p-3 mb-2'):
                title_input = ui.input('Title', value=part['title']).classes('w-full')
                submit_input = ui.input('Submit button', value=part['submit']).classes('w-full max-w-xs')
                selected = {str(arm_id) for arm_id in part['arm_ids']}
                checks = []
                if arm_options:
                    ui.label('Shown to').classes('text-sm text-gray-600 mt-2')
                    for arm_id, label in arm_options.items():
                        checks.append((arm_id, ui.checkbox(label, value=arm_id in selected)))
                else:
                    ui.label('Add arms before choosing who sees this part.').classes('text-sm text-gray-500')
                with ui.row().classes('gap-2 mt-2 flex-wrap'):
                    ui.button(
                        'Save part',
                        on_click=lambda part_id=part['id'], title_input=title_input, submit_input=submit_input, checks=checks: _save_part(
                            part_id, title_input.value, submit_input.value, checks
                        ),
                    ).props('flat dense no-caps').classes('text-blue-700')
                    if index > 0:
                        ui.button('Up', on_click=lambda part_id=part['id']: _move_part(part_id, -1)).props('flat dense')
                    if index < len(snapshot['parts']) - 1:
                        ui.button('Down', on_click=lambda part_id=part['id']: _move_part(part_id, 1)).props('flat dense')
                    ui.button('Delete', on_click=lambda part_id=part['id']: _delete_part(part_id)).props('flat dense color=red')

        ui.separator().classes('my-4')
        ui.label('Class sections').classes('text-lg font-semibold mb-1')
        ui.label(
            'Enter the instructor, course, section, and how many student links to issue. '
            'Links are divided across the arms in blocks of three. Downloading again reuses links already created. '
            'Lowering enrollment does not revoke a link that was already issued.'
        ).classes('text-sm text-gray-600 mb-2')
        with ui.row().classes('gap-2 items-end flex-wrap mb-3'):
            instructor = ui.input('Instructor').classes('w-48')
            course = ui.input('Course').classes('w-32')
            section_code = ui.input('Section').classes('w-24')
            enrollment = ui.number('Students', value=0, min=0, precision=0).classes('w-28')
            ui.button(
                'Add section',
                on_click=lambda: _add_section(
                    survey_id, instructor.value, course.value, section_code.value, enrollment.value
                ),
            ).classes('bg-blue-600 text-white')

        if snapshot['sections']:
            for section in snapshot['sections']:
                with ui.row().classes('w-full items-center justify-between gap-2 py-1'):
                    ui.label(
                        f'{section["course"]} {section["section"]} · {section["instructor"]} · '
                        f'{section["enrollment"]} students · {section["issued"]} links issued'
                    ).classes('text-sm')
                    ui.button(
                        'Remove',
                        on_click=lambda section_id=section['id']: _delete_section(section_id),
                    ).props('flat dense color=red')
        else:
            ui.label('No sections yet.').classes('text-sm text-gray-500')

        ui.button('Download IR links (CSV)', on_click=lambda: _download_links(survey_id)).classes(
            'bg-slate-800 text-white mt-4'
        )


def _refresh() -> None:
    study_design_panel.refresh()


def _add_default_arms(survey_id: int) -> None:
    session = _session()
    try:
        ensure_default_arms(session, survey_id)
        session.commit()
    finally:
        session.close()
    ui.notify('Study arms added', type='positive')
    _refresh()


def _save_arm(arm_id: int, label: str, llm: bool, note: str) -> None:
    cleaned = (label or '').strip()
    if not cleaned:
        ui.notify('Arm label is required', type='negative')
        return
    session = _session()
    try:
        arm = session.get(SurveyArm, int(arm_id))
        if arm is None:
            ui.notify('Arm not found', type='warning')
            return
        arm.label = cleaned
        arm.llm_followups = bool(llm)
        arm.external_note_html = (note or '').strip() or None
        session.commit()
    finally:
        session.close()
    ui.notify('Arm saved', type='positive')
    _refresh()


def _add_part(survey_id: int, title: str) -> None:
    cleaned = (title or '').strip()
    if not cleaned:
        ui.notify('Part title is required', type='negative')
        return
    session = _session()
    try:
        count = session.query(SurveyPart).filter_by(survey_id=survey_id).count()
        part = SurveyPart(survey_id=survey_id, title=cleaned, submit_label='Submit', sort_order=count + 1)
        session.add(part)
        session.flush()
        for arm in ordered_arms(session, survey_id):
            if arm.key != 'negative_control':
                session.add(SurveyPartArm(part_id=part.id, arm_id=arm.id))
        session.commit()
    finally:
        session.close()
    ui.notify('Part added', type='positive')
    _refresh()


def _save_part(part_id: int, title: str, submit_label: str, checks: list) -> None:
    cleaned = (title or '').strip()
    if not cleaned:
        ui.notify('Part title is required', type='negative')
        return
    session = _session()
    try:
        part = session.get(SurveyPart, int(part_id))
        if part is None:
            ui.notify('Part not found', type='warning')
            return
        part.title = cleaned
        part.submit_label = (submit_label or '').strip() or 'Submit'
        session.query(SurveyPartArm).filter_by(part_id=part.id).delete(synchronize_session=False)
        for arm_id, checkbox in checks:
            if checkbox.value:
                session.add(SurveyPartArm(part_id=part.id, arm_id=int(arm_id)))
        session.commit()
    finally:
        session.close()
    ui.notify('Part saved', type='positive')
    _refresh()


def _move_part(part_id: int, delta: int) -> None:
    session = _session()
    try:
        part = session.get(SurveyPart, int(part_id))
        if part is None:
            return
        parts = ordered_parts(session, part.survey_id)
        ids = [row.id for row in parts]
        current = ids.index(part.id)
        target = current + delta
        if target < 0 or target >= len(ids):
            return
        ids[current], ids[target] = ids[target], ids[current]
        by_id = {row.id: row for row in parts}
        for index, row_id in enumerate(ids, 1):
            by_id[row_id].sort_order = index
        session.commit()
    finally:
        session.close()
    _refresh()


def _delete_part(part_id: int) -> None:
    session = _session()
    try:
        part = session.get(SurveyPart, int(part_id))
        if part is None:
            return
        session.query(SurveyQuestion).filter_by(part_id=part.id).update({SurveyQuestion.part_id: None})
        session.delete(part)
        session.commit()
    finally:
        session.close()
    ui.notify('Part removed. Its questions are no longer in a part.', type='positive')
    _refresh()
    from admin_panel import questions_list_display
    questions_list_display.refresh()


def _add_section(survey_id: int, instructor: str, course: str, section_code: str, enrollment) -> None:
    instructor = (instructor or '').strip()
    course = (course or '').strip()
    section_code = (section_code or '').strip()
    count = int(enrollment or 0)
    if not instructor or not course or not section_code:
        ui.notify('Instructor, course, and section are required', type='negative')
        return
    if count < 0:
        ui.notify('Student count cannot be negative', type='negative')
        return
    session = _session()
    try:
        session.add(
            CourseSection(
                survey_id=survey_id,
                instructor_name=instructor,
                course_code=course,
                section_code=section_code,
                enrollment=count,
            )
        )
        session.commit()
    finally:
        session.close()
    ui.notify('Section added', type='positive')
    _refresh()


def _delete_section(section_id: int) -> None:
    session = _session()
    try:
        section = session.get(CourseSection, int(section_id))
        if section is None:
            return
        issued = session.query(ParticipantLink).filter_by(section_id=section.id).count()
        if issued:
            ui.notify('This section already has links. Leave it so those URLs keep working.', type='warning')
            return
        session.delete(section)
        session.commit()
    finally:
        session.close()
    ui.notify('Section removed', type='positive')
    _refresh()


def _download_links(survey_id: int, base_url: str | None = None) -> None:
    from app_config import get_public_base_url

    base = (base_url or '').strip() or get_public_base_url(None)
    if not base:
        ui.notify('Set PUBLIC_BASE_URL before downloading links.', type='warning')
        return
    session = _session()
    try:
        survey = session.get(Survey, int(survey_id))
        if survey is None:
            ui.notify('Survey not found', type='negative')
            return
        if not ordered_arms(session, survey.id):
            ui.notify('Add treatment arms first', type='warning')
            return
        if session.query(CourseSection).filter_by(survey_id=survey.id).count() == 0:
            ui.notify('Add at least one class section', type='warning')
            return
        body = study_links_csv(session, survey.id, base)
        session.commit()
        name = re.sub(r'[^\w\-.]+', '_', (survey.name or 'survey').strip()) or 'survey'
    except ValueError as exc:
        session.rollback()
        ui.notify(str(exc), type='negative')
        return
    finally:
        session.close()
    ui.download(body.encode('utf-8'), f'{name}_ir_links.csv')
    _refresh()
