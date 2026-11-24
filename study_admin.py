"""Admin controls for comparison groups, parts, respondent groups, and link files."""

from nicegui import ui

from study_links import (
    ensure_default_arms,
    normalize_placeholder_tag,
    normalize_placeholder_tags,
    ordered_arms,
    ordered_parts,
    study_links_package,
)
from survey_models import (
    CourseSection,
    ParticipantLink,
    QuestionBank,
    Survey,
    SurveyArm,
    SurveyPart,
    SurveyPartArm,
    SurveyQuestion,
)


def _session():
    from admin_panel import Session
    return Session()


async def _live_text(element) -> str:
    """Read the field's current text, including characters not yet sent to the server."""
    try:
        value = await ui.run_javascript(f'getElement({int(element.id)}).inputValue')
    except Exception:
        value = None
    if value is None:
        value = getattr(element, 'value', '')
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


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
        parts_by_id = {part.id: part for part in parts}
        sequence_by_arm: dict[int, list[dict]] = {arm.id: [] for arm in arms}
        for row in sorted(memberships, key=lambda item: (item.sort_order or 0, item.part_id)):
            part = parts_by_id.get(row.part_id)
            if part is None:
                continue
            sequence_by_arm.setdefault(row.arm_id, []).append({
                'id': part.id,
                'title': part.title,
                'llm': bool(part.llm_followups),
                'link': bool(part.is_link) and not part.llm_followups,
                'one_per_page': bool(row.one_per_page),
            })
        issued = {}
        if sections:
            rows = (
                session.query(ParticipantLink.section_id)
                .filter(ParticipantLink.section_id.in_([section.id for section in sections]))
                .all()
            )
            for (section_id,) in rows:
                issued[section_id] = issued.get(section_id, 0) + 1
        part_questions: dict[int, list[dict]] = {part.id: [] for part in parts}
        if parts:
            question_rows = (
                session.query(SurveyQuestion, QuestionBank)
                .join(QuestionBank, SurveyQuestion.question_id == QuestionBank.id)
                .filter(
                    SurveyQuestion.survey_id == survey_id,
                    SurveyQuestion.part_id.in_([part.id for part in parts]),
                )
                .order_by(SurveyQuestion.order, SurveyQuestion.id)
                .all()
            )
            for sq, question in question_rows:
                part_questions.setdefault(sq.part_id, []).append({
                    'id': sq.id,
                    'bank_id': question.id,
                    'name': question.name,
                    'text': question.question_text or '',
                })
        snapshot = {
            'arms': [
                {
                    'id': arm.id,
                    'key': arm.key,
                    'label': arm.label,
                    'note': arm.external_note_html or '',
                    'sequence': sequence_by_arm.get(arm.id, []),
                }
                for arm in arms
            ],
            'parts': [
                {
                    'id': part.id,
                    'title': part.title,
                    'submit': part.submit_label or 'Submit',
                    'llm': bool(part.llm_followups),
                    'link': bool(part.is_link) and not part.llm_followups,
                    'link_url': part.link_url or '',
                    'llm_count': int(part.llm_question_count or 3),
                    'llm_instructions': part.llm_instructions or '',
                    'context': part.context_text or '',
                    'tags': normalize_placeholder_tags(part.placeholder_tags),
                    'arm_ids': arm_ids_by_part.get(part.id, set()),
                    'questions': part_questions.get(part.id, []),
                }
                for part in parts
            ],
            'sections': [
                {
                    'id': section.id,
                    'group': section.instructor_name,
                    'category': section.course_code,
                    'code': section.section_code,
                    'enrollment': section.enrollment,
                    'issued': issued.get(section.id, 0),
                    'placeholders': section.placeholders if isinstance(section.placeholders, dict) else {},
                }
                for section in sections
            ],
        }
    finally:
        session.close()

    with ui.card().classes('w-full p-6 mt-4'):
        ui.label('Survey Design').classes('text-2xl font-bold mb-1')
        ui.label(
            'Build the survey from parts, then place those parts into each control. '
            'A respondent submits one part and continues to the next part in that control. The last part finishes the survey.'
        ).classes('text-sm text-gray-600 mb-4')

        ui.label('Parts').classes('text-lg font-semibold mb-1')
        ui.label(
            'Create the parts first. A part is its own questions, an LLM follow-up, or a link to another page. '
            'An LLM part writes questions from the open-ended answers in the part placed just before it. '
            'A link part sends the respondent to that page. When they come back, they continue with the parts that remain. '
            'Context is shown once, before that part starts.'
        ).classes('text-sm text-gray-600 mb-2')
        with ui.row().classes('gap-2 items-end flex-wrap mb-3'):
            part_title = ui.input('New part title', placeholder='Standard survey').classes('w-64')
            part_kind = ui.select(
                {'questions': 'Survey questions', 'llm': 'LLM follow-up', 'link': 'Link'},
                value='questions',
                label='Kind',
            ).classes('w-48')
            ui.button(
                'Add part',
                on_click=lambda: _add_part(survey_id, part_title.value, part_kind.value),
            ).classes('bg-green-600 text-white')

        for part in snapshot['parts']:
            with ui.card().classes('w-full p-3 mb-2'):
                kind_name = 'LLM follow-up' if part['llm'] else 'Link' if part['link'] else 'Survey questions'
                ui.label(kind_name).classes('text-xs font-semibold text-slate-500')
                title_input = ui.input('Title', value=part['title']).classes('w-full')
                submit_input = ui.input('Submit button', value=part['submit']).classes('w-full max-w-xs')
                context_input = ui.textarea(
                    'Context shown before this part',
                    value=part['context'],
                    placeholder='Shown once, before this part starts. Leave blank to begin with the questions.',
                ).classes('w-full').props('autogrow')
                ui.label('Placeholder tags').classes('text-sm font-semibold text-gray-700 mt-3')
                ui.label(
                    'When the text says [Instructor], add a tag for it. '
                    'Tag name is the label on each group. Tag is the word inside the brackets.'
                ).classes('text-sm text-gray-600')
                tag_fields: list = []
                for tag in part['tags']:
                    with ui.row().classes('w-full gap-2 items-end flex-wrap'):
                        name_field = ui.input('Tag name', value=tag['name']).classes('w-56')
                        token_field = ui.input('Tag', value=tag['tag']).classes('w-40')
                        tag_fields.append((name_field, token_field))
                        ui.button(
                            'Remove',
                            on_click=lambda part_id=part['id'], token=tag['tag']: _remove_part_tag(part_id, token),
                        ).props('flat dense color=red')
                with ui.row().classes('w-full gap-2 items-end flex-wrap'):
                    new_tag_name = ui.input('Tag name', placeholder="Instructor's name").classes('w-56')
                    new_tag = ui.input('Tag', placeholder='Instructor').classes('w-40')

                    async def add_tag(
                        part_id=part['id'],
                        fields=tag_fields,
                        name_field=new_tag_name,
                        token_field=new_tag,
                    ):
                        pairs = [
                            (await _live_text(name), await _live_text(token))
                            for name, token in fields
                        ]
                        pairs.append((await _live_text(name_field), await _live_text(token_field)))
                        _replace_part_tags(part_id, pairs)

                    ui.button('Add tag', on_click=add_tag).classes('bg-slate-700 text-white')
                kind_input = ui.select(
                    {'questions': 'Survey questions', 'llm': 'LLM follow-up', 'link': 'Link'},
                    value='llm' if part['llm'] else 'link' if part['link'] else 'questions',
                    label='Kind',
                ).classes('w-full max-w-xs')
                llm_fields: dict = {}
                llm_settings = ui.column().classes('w-full')

                def show_llm_settings(
                    kind=kind_input,
                    holder=llm_settings,
                    fields=llm_fields,
                    stored_count=part['llm_count'],
                    stored_instructions=part['llm_instructions'],
                    stored_url=part['link_url'],
                ):
                    holder.clear()
                    fields.clear()
                    if kind.value == 'link':
                        with holder:
                            ui.label(
                                'The respondent opens this page, then comes back to continue with any parts that remain.'
                            ).classes('text-sm text-gray-600 mt-3')
                            fields['url'] = ui.input(
                                'Page address',
                                value=stored_url,
                                placeholder='https://example.com/page',
                            ).classes('w-full')
                        return
                    if kind.value != 'llm':
                        return
                    with holder:
                        ui.label(
                            'Respondents answer the open-ended questions in the previous part. '
                            'This part then asks the number of follow-up questions below, using your instructions.'
                        ).classes('text-sm text-gray-600 mt-3')
                        fields['count'] = ui.number(
                            'Number of questions',
                            value=stored_count,
                            min=1,
                            max=10,
                            precision=0,
                        ).classes('w-40 mt-2')
                        fields['instructions'] = ui.textarea(
                            'Instructions for these questions',
                            value=stored_instructions,
                            placeholder='What the follow-up questions should explore, and what they should leave alone.',
                        ).classes('w-full').props('autogrow')

                kind_input.on_value_change(lambda _event, refresh=show_llm_settings: refresh())
                show_llm_settings()
                if not part['llm'] and not part['link']:
                    ui.label('Questions in this part').classes('text-sm font-semibold text-gray-700 mt-3')
                    questions = part['questions']
                    if questions:
                        for q_index, question in enumerate(questions):
                            snippet = question['text'][:80] + ('...' if len(question['text']) > 80 else '')
                            with ui.row().classes('w-full items-center justify-between gap-2'):
                                ui.label(f'{q_index + 1}. {question["name"]}').classes('text-sm')
                                with ui.row().classes('gap-1'):
                                    ui.button(
                                        'Edit',
                                        on_click=lambda bank_id=question['bank_id'], sid=survey_id: ui.navigate.to(
                                            f'/admin/questions/edit/{bank_id}?return_survey_id={sid}'
                                        ),
                                    ).props('flat dense')
                                    if q_index > 0:
                                        ui.button(
                                            'Up',
                                            on_click=lambda sq_id=question['id']: _move_part_question(sq_id, -1),
                                        ).props('flat dense')
                                    if q_index < len(questions) - 1:
                                        ui.button(
                                            'Down',
                                            on_click=lambda sq_id=question['id']: _move_part_question(sq_id, 1),
                                        ).props('flat dense')
                                    ui.button(
                                        'Remove',
                                        on_click=lambda sq_id=question['id']: _remove_question_from_part(sq_id),
                                    ).props('flat dense color=red')
                            if snippet:
                                ui.label(snippet).classes('text-xs text-gray-500 -mt-1 mb-1')
                    else:
                        ui.label('No questions in this part yet.').classes('text-sm text-gray-500')
                    with ui.row().classes('gap-2 mt-2 flex-wrap'):
                        ui.button(
                            'New question',
                            on_click=lambda part_id=part['id'], sid=survey_id: ui.navigate.to(
                                f'/admin/questions/new?return_survey_id={sid}&return_part_id={part_id}'
                            ),
                        ).classes('bg-green-600 text-white')
                        ui.button(
                            'Add question bank',
                            on_click=lambda part_id=part['id']: _add_bank_to_part(survey_id, part_id),
                        ).classes('bg-indigo-600 text-white')
                        ui.button(
                            'Add questions',
                            on_click=lambda part_id=part['id']: _add_questions_to_part(survey_id, part_id),
                        ).classes('bg-slate-700 text-white')
                with ui.row().classes('gap-2 mt-2 flex-wrap'):
                    async def save_this_part(
                        part_id=part['id'],
                        title_input=title_input,
                        submit_input=submit_input,
                        kind_input=kind_input,
                        fields=llm_fields,
                        context_input=context_input,
                        tag_fields=tag_fields,
                    ):
                        kind = kind_input.value or 'questions'
                        count = None if fields.get('count') is None else await _live_text(fields['count'])
                        instructions = '' if fields.get('instructions') is None else await _live_text(fields['instructions'])
                        link_url = await _live_text(fields['url']) if kind == 'link' and fields.get('url') is not None else None
                        tag_pairs = [
                            (await _live_text(name), await _live_text(token))
                            for name, token in tag_fields
                        ]
                        _save_part(
                            part_id,
                            await _live_text(title_input),
                            await _live_text(submit_input),
                            kind == 'llm',
                            count,
                            instructions,
                            link_url,
                            await _live_text(context_input),
                            tag_pairs,
                        )

                    ui.button('Save part', on_click=save_this_part).props('flat dense no-caps').classes('text-blue-700')
                    ui.button('Delete', on_click=lambda part_id=part['id']: _delete_part(part_id)).props('flat dense color=red')

        ui.separator().classes('my-4')
        ui.label('Controls').classes('text-lg font-semibold mb-1')
        ui.label(
            'Place parts into each control in the order respondents in that control will see them. '
            'A question part can be one question per page in one control and all on one page in another. '
            'Positive control can include the standard survey and the UI/UX follow-up, and leave out the LLM part. '
            'Experimental includes all three. Negative control includes only the parts you add here.'
        ).classes('text-sm text-gray-600 mb-2')
        if not snapshot['arms']:
            ui.label('Add the three controls, then place parts into each one.').classes('text-sm text-gray-500 mb-2')
            ui.button(
                'Add negative, positive, and experimental controls',
                on_click=lambda: _add_default_arms(survey_id),
            ).classes('bg-blue-600 text-white mb-4')
        else:
            part_choices = {str(part['id']): part['title'] for part in snapshot['parts']}
            for arm in snapshot['arms']:
                with ui.card().classes('w-full p-3 mb-2 bg-slate-50'):
                    ui.label(arm['key']).classes('text-xs text-gray-500')
                    label_input = ui.input('Label', value=arm['label']).classes('w-full')
                    note = ui.textarea('Note shown before the survey (optional)', value=arm['note']).classes('w-full')
                    ui.button(
                        'Save control',
                        on_click=lambda arm_id=arm['id'], label_input=label_input, note=note: _save_arm(
                            arm_id, label_input.value, note.value
                        ),
                    ).props('flat dense no-caps').classes('text-blue-700')
                    ui.label('Respondent order').classes('text-sm font-semibold mt-3')
                    sequence = arm['sequence']
                    if sequence:
                        for step, item in enumerate(sequence):
                            kind_name = 'LLM follow-up' if item['llm'] else 'Link' if item.get('link') else 'Survey questions'
                            with ui.row().classes('w-full items-center justify-between gap-2'):
                                ui.label(f'{step + 1}. {item["title"]} · {kind_name}').classes('text-sm')
                                if not item['llm'] and not item.get('link'):
                                    ui.select(
                                        {'page': 'One question per page', 'all': 'All on one page'},
                                        value='page' if item.get('one_per_page', True) else 'all',
                                        on_change=lambda e, arm_id=arm['id'], part_id=item['id']: _set_control_paging(
                                            arm_id, part_id, e.value
                                        ),
                                    ).classes('w-56')
                                with ui.row().classes('gap-1'):
                                    if step > 0:
                                        ui.button(
                                            'Up',
                                            on_click=lambda arm_id=arm['id'], part_id=item['id']: _move_control_part(arm_id, part_id, -1),
                                        ).props('flat dense')
                                    if step < len(sequence) - 1:
                                        ui.button(
                                            'Down',
                                            on_click=lambda arm_id=arm['id'], part_id=item['id']: _move_control_part(arm_id, part_id, 1),
                                        ).props('flat dense')
                                    ui.button(
                                        'Remove',
                                        on_click=lambda arm_id=arm['id'], part_id=item['id']: _remove_control_part(arm_id, part_id),
                                    ).props('flat dense color=red')
                    else:
                        ui.label('No parts in this control yet.').classes('text-sm text-gray-500')
                    remaining = {
                        part_id: title
                        for part_id, title in part_choices.items()
                        if int(part_id) not in {item['id'] for item in sequence}
                    }
                    if remaining:
                        with ui.row().classes('gap-2 items-end mt-2 flex-wrap'):
                            picker = ui.select(remaining, value=next(iter(remaining)), label='Add part').classes('w-64')
                            ui.button(
                                'Add to control',
                                on_click=lambda arm_id=arm['id'], picker=picker: _add_control_part(arm_id, picker.value),
                            ).classes('bg-slate-700 text-white')
                    elif not part_choices:
                        ui.label('Create a part above before adding it to this control.').classes('text-sm text-gray-500 mt-2')

        ui.separator().classes('my-4')
        ui.label('Groups').classes('text-lg font-semibold mb-1')
        ui.label(
            'A group is one set of people who should be split across the controls together. '
            'Enter a group, a category, a code, and how many links to issue. '
            'Links are divided across the controls in blocks of three. Downloading again reuses links already created. '
            'Lowering the count does not revoke a link that was already issued. '
            'The download is a zip file with one spreadsheet per group, and each spreadsheet has a tab for every control.'
        ).classes('text-sm text-gray-600 mb-2')
        survey_tags = _survey_placeholder_tags(snapshot['parts'])
        with ui.column().classes('w-full gap-2 mb-3'):
            with ui.row().classes('gap-2 items-end flex-wrap'):
                instructor = ui.input('Group').classes('w-48')
                course = ui.input('Category').classes('w-32')
                section_code = ui.input('Code').classes('w-24')
                enrollment = ui.number('Participants', value=0, min=0, precision=0).classes('w-28')
            new_values: list = []
            if survey_tags:
                ui.label('Placeholder values').classes('text-sm font-semibold text-gray-700')
                for tag in survey_tags:
                    value_field = ui.input(tag['name']).classes('w-full max-w-md')
                    ui.label(f'Tag: {tag["tag"]}').classes('text-xs text-gray-500 -mt-2')
                    new_values.append((tag['tag'], value_field))

            async def add_group(
                group_field=instructor,
                category_field=course,
                code_field=section_code,
                count_field=enrollment,
                value_fields=new_values,
            ) -> None:
                values = {
                    tag: await _live_text(field)
                    for tag, field in value_fields
                }
                _add_section(
                    survey_id,
                    await _live_text(group_field),
                    await _live_text(category_field),
                    await _live_text(code_field),
                    await _live_text(count_field),
                    values,
                )

            ui.button('Add group', on_click=add_group).classes('bg-blue-600 text-white w-fit')

        if snapshot['sections']:
            for section in snapshot['sections']:
                with ui.column().classes('w-full gap-1 py-2'):
                    with ui.row().classes('w-full items-center justify-between gap-2'):
                        ui.label(
                            f'{section["group"]} · {section["category"]} · {section["code"]} · '
                            f'{section["enrollment"]} participants · {section["issued"]} links issued'
                        ).classes('text-sm')
                        ui.button(
                            'Remove',
                            on_click=lambda section_id=section['id']: _delete_section(section_id),
                        ).props('flat dense color=red')
                    if survey_tags:
                        stored = section['placeholders']
                        value_fields = []
                        for tag in survey_tags:
                            value_field = ui.input(
                                tag['name'],
                                value=str(stored.get(tag['tag']) or stored.get(tag['tag'].lower()) or ''),
                            ).classes('w-full max-w-md')
                            ui.label(f'Tag: {tag["tag"]}').classes('text-xs text-gray-500 -mt-2')
                            value_fields.append((tag['tag'], value_field))

                        async def save_values(section_id=section['id'], fields=value_fields) -> None:
                            _save_section_placeholders(
                                section_id,
                                {tag: await _live_text(field) for tag, field in fields},
                            )

                        ui.button('Save placeholder values', on_click=save_values).props('flat dense').classes('w-fit')
        else:
            ui.label('No groups yet.').classes('text-sm text-gray-500')

        ui.button('Download IR links', on_click=lambda: _download_links(survey_id)).classes(
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


def _save_arm(arm_id: int, label: str, note: str) -> None:
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
        arm.external_note_html = (note or '').strip() or None
        session.commit()
    finally:
        session.close()
    ui.notify('Arm saved', type='positive')
    _refresh()


def _add_part(survey_id: int, title: str, kind: str = 'questions') -> None:
    cleaned = (title or '').strip()
    if not cleaned:
        ui.notify('Part title is required', type='negative')
        return
    is_llm = kind == 'llm'
    session = _session()
    try:
        count = session.query(SurveyPart).filter_by(survey_id=survey_id).count()
        part = SurveyPart(
            survey_id=survey_id,
            title=cleaned,
            submit_label='Continue' if kind == 'link' else 'Submit',
            llm_followups=is_llm,
            is_link=kind == 'link',
            sort_order=count + 1,
        )
        session.add(part)
        session.commit()
    finally:
        session.close()
    ui.notify('Part added', type='positive')
    _refresh()


def _assign_questions_to_part(survey_id: int, part_id: int, question_ids: list[int]) -> tuple[int, int]:
    """Put questions on a part. Questions already on the survey move into this part."""
    from admin_panel import _reindex_survey_questions

    session = _session()
    try:
        part = session.get(SurveyPart, int(part_id))
        if part is None or part.survey_id != int(survey_id):
            ui.notify('Part not found', type='warning')
            return 0, 0
        rows = _reindex_survey_questions(session, int(survey_id))
        by_qid = {row.question_id: row for row in rows}
        next_order = len(rows)
        placed = already = 0
        for raw_id in question_ids:
            qid = int(raw_id)
            row = by_qid.get(qid)
            if row is None:
                if session.get(QuestionBank, qid) is None:
                    continue
                next_order += 1
                row = SurveyQuestion(
                    survey_id=int(survey_id),
                    question_id=qid,
                    order=next_order,
                    part_id=part.id,
                )
                session.add(row)
                by_qid[qid] = row
                placed += 1
                continue
            if row.part_id == part.id:
                already += 1
                continue
            next_order += 1
            row.order = next_order
            row.part_id = part.id
            placed += 1
        session.flush()
        _reindex_survey_questions(session, int(survey_id))
        session.commit()
        return placed, already
    finally:
        session.close()


def _add_bank_to_part(survey_id: int, part_id: int) -> None:
    from question_banks import open_bank_picker

    def on_confirm(question_ids: list[int]) -> None:
        placed, already = _assign_questions_to_part(survey_id, part_id, question_ids)
        if placed:
            ui.notify(f'Added {placed} question{"s" if placed != 1 else ""} to this part', type='positive')
        elif already:
            ui.notify('Those questions are already in this part', type='warning')
        else:
            ui.notify('No questions added', type='warning')
        _refresh()

    open_bank_picker(on_confirm=on_confirm, title='Add a question bank to this part')


def _add_questions_to_part(survey_id: int, part_id: int) -> None:
    from authentication import get_current_user_id

    session = _session()
    try:
        user_id = get_current_user_id()
        questions = (
            session.query(QuestionBank)
            .filter((QuestionBank.created_by == user_id) | (QuestionBank.created_by.is_(None)))
            .order_by(QuestionBank.name, QuestionBank.id)
            .all()
        )
        in_part = {
            row.question_id
            for row in session.query(SurveyQuestion).filter_by(survey_id=int(survey_id), part_id=int(part_id)).all()
        }
        choices = [
            {'id': question.id, 'name': question.name, 'text': question.question_text or ''}
            for question in questions
            if question.id not in in_part
        ]
    finally:
        session.close()

    if not choices:
        ui.notify('No other questions to add. Create questions or a question bank first.', type='warning')
        return

    checks: list[tuple[int, object]] = []
    with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl p-4'):
        ui.label('Add questions to this part').classes('text-xl font-bold')
        ui.label('Tick the questions that belong in this sub-survey.').classes('text-sm text-gray-600 mb-2')
        with ui.column().classes('w-full max-h-80 overflow-y-auto gap-1 border border-gray-200 rounded p-2'):
            for question in choices:
                snippet = question['text'][:70] + ('...' if len(question['text']) > 70 else '')
                checkbox = ui.checkbox(question['name']).classes('w-full')
                if snippet:
                    ui.label(snippet).classes('text-xs text-gray-500 ml-8 -mt-1')
                checks.append((question['id'], checkbox))

        def confirm() -> None:
            ids = [question_id for question_id, checkbox in checks if checkbox.value]
            if not ids:
                ui.notify('Select at least one question', type='warning')
                return
            dialog.close()
            placed, _already = _assign_questions_to_part(survey_id, part_id, ids)
            if placed:
                ui.notify(f'Added {placed} question{"s" if placed != 1 else ""} to this part', type='positive')
            _refresh()

        with ui.row().classes('justify-end w-full mt-4 gap-2'):
            ui.button('Cancel', on_click=dialog.close).props('flat')
            ui.button('Add selected', on_click=confirm).classes('bg-slate-700 text-white')
    dialog.open()


def _remove_question_from_part(sq_id: int) -> None:
    session = _session()
    try:
        row = session.get(SurveyQuestion, int(sq_id))
        if row is None:
            return
        session.delete(row)
        session.commit()
    finally:
        session.close()
    ui.notify('Question removed from this part', type='positive')
    _refresh()


def _move_part_question(sq_id: int, delta: int) -> None:
    session = _session()
    survey_id = None
    try:
        row = session.get(SurveyQuestion, int(sq_id))
        if row is None or row.part_id is None:
            return
        siblings = (
            session.query(SurveyQuestion)
            .filter_by(survey_id=row.survey_id, part_id=row.part_id)
            .order_by(SurveyQuestion.order, SurveyQuestion.id)
            .all()
        )
        ids = [item.id for item in siblings]
        current = ids.index(row.id)
        target = current + delta
        if target < 0 or target >= len(ids):
            return
        other = siblings[target]
        row.order, other.order = other.order, row.order
        session.commit()
    finally:
        session.close()
    _refresh()


def _control_part_ids(session, arm_id: int) -> list[int]:
    rows = (
        session.query(SurveyPartArm)
        .filter_by(arm_id=int(arm_id))
        .order_by(SurveyPartArm.sort_order, SurveyPartArm.part_id)
        .all()
    )
    return [row.part_id for row in rows]


def _write_control_order(session, arm_id: int, part_ids: list[int]) -> None:
    existing = {
        row.part_id: bool(row.one_per_page)
        for row in session.query(SurveyPartArm).filter_by(arm_id=int(arm_id)).all()
    }
    session.query(SurveyPartArm).filter_by(arm_id=int(arm_id)).delete(synchronize_session=False)
    for index, part_id in enumerate(part_ids, 1):
        session.add(SurveyPartArm(
            part_id=int(part_id),
            arm_id=int(arm_id),
            sort_order=index,
            one_per_page=existing.get(int(part_id), True),
        ))


def _set_control_paging(arm_id: int, part_id: int, choice) -> None:
    if choice not in ('page', 'all'):
        return
    one_per_page = choice != 'all'
    session = _session()
    try:
        row = session.get(SurveyPartArm, (int(part_id), int(arm_id)))
        if row is None or bool(row.one_per_page) == one_per_page:
            return
        row.one_per_page = one_per_page
        session.commit()
    finally:
        session.close()
    _refresh()


def _add_control_part(arm_id: int, part_id) -> None:
    if not part_id:
        ui.notify('Choose a part', type='warning')
        return
    session = _session()
    try:
        ids = _control_part_ids(session, arm_id)
        part_id = int(part_id)
        if part_id not in ids:
            ids.append(part_id)
        _write_control_order(session, arm_id, ids)
        session.commit()
    finally:
        session.close()
    ui.notify('Part added to this control', type='positive')
    _refresh()


def _remove_control_part(arm_id: int, part_id: int) -> None:
    session = _session()
    try:
        ids = [item for item in _control_part_ids(session, arm_id) if item != int(part_id)]
        _write_control_order(session, arm_id, ids)
        session.commit()
    finally:
        session.close()
    _refresh()


def _move_control_part(arm_id: int, part_id: int, delta: int) -> None:
    session = _session()
    try:
        ids = _control_part_ids(session, arm_id)
        current = ids.index(int(part_id))
        target = current + delta
        if target < 0 or target >= len(ids):
            return
        ids[current], ids[target] = ids[target], ids[current]
        _write_control_order(session, arm_id, ids)
        session.commit()
    finally:
        session.close()
    _refresh()


def _survey_placeholder_tags(parts: list[dict]) -> list[dict[str, str]]:
    """One entry per tag, in the order the parts define them."""
    tags: list[dict[str, str]] = []
    seen: set[str] = set()
    for part in parts:
        for tag in part.get('tags') or []:
            key = str(tag.get('tag') or '').lower()
            if not key or key in seen:
                continue
            seen.add(key)
            tags.append(tag)
    return tags


def _tags_from_pairs(pairs: list[tuple[str, str]]) -> list[dict[str, str]] | None:
    tags: list[dict[str, str]] = []
    for name, token in pairs:
        if not str(name or '').strip() and not str(token or '').strip():
            continue
        tag = normalize_placeholder_tag(name, token)
        if tag is None:
            return None
        tags.append(tag)
    return normalize_placeholder_tags(tags)


def _replace_part_tags(part_id: int, pairs: list[tuple[str, str]]) -> None:
    tags = _tags_from_pairs(pairs)
    if tags is None:
        ui.notify('Enter a tag name and a tag such as Instructor.', type='negative')
        return
    session = _session()
    try:
        part = session.get(SurveyPart, int(part_id))
        if part is None:
            ui.notify('Part not found', type='warning')
            return
        part.placeholder_tags = tags or None
        session.commit()
    finally:
        session.close()
    ui.notify('Placeholder tags saved', type='positive')
    _refresh()


def _remove_part_tag(part_id: int, token: str) -> None:
    session = _session()
    try:
        part = session.get(SurveyPart, int(part_id))
        if part is None:
            return
        key = str(token or '').strip().lower()
        part.placeholder_tags = [
            tag for tag in normalize_placeholder_tags(part.placeholder_tags)
            if tag['tag'].lower() != key
        ] or None
        session.commit()
    finally:
        session.close()
    _refresh()


def _save_part(
    part_id: int,
    title: str,
    submit_label: str,
    llm_followups: bool = False,
    llm_question_count=3,
    llm_instructions: str = '',
    link_url: str | None = None,
    context_text: str = '',
    tag_pairs: list[tuple[str, str]] | None = None,
) -> None:
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
        part.context_text = (context_text or '').strip() or None
        if tag_pairs is not None:
            tags = _tags_from_pairs(tag_pairs)
            if tags is None:
                ui.notify('Enter a tag name and a tag such as Instructor.', type='negative')
                return
            part.placeholder_tags = tags or None
        from study_links import normalize_link_url
        page = None
        if not llm_followups and link_url is not None:
            page = normalize_link_url(link_url)
            if not page:
                ui.notify('Enter the page address for this link', type='negative')
                return
        part.llm_followups = bool(llm_followups)
        part.is_link = page is not None
        part.link_url = page
        if llm_question_count is not None:
            from study_links import normalize_llm_question_count
            part.llm_question_count = normalize_llm_question_count(llm_question_count)
            part.llm_instructions = (llm_instructions or '').strip() or None
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
        session.query(SurveyQuestion).filter_by(part_id=part.id).delete(synchronize_session=False)
        session.delete(part)
        session.commit()
    finally:
        session.close()
    ui.notify('Part removed', type='positive')
    _refresh()


def _add_section(
    survey_id: int,
    instructor: str,
    course: str,
    section_code: str,
    enrollment,
    placeholders: dict | None = None,
) -> None:
    instructor = (instructor or '').strip()
    course = (course or '').strip()
    section_code = (section_code or '').strip()
    count = int(enrollment or 0)
    if not instructor or not course or not section_code:
        ui.notify('Group, category, and code are required', type='negative')
        return
    if count < 0:
        ui.notify('Participant count cannot be negative', type='negative')
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
                placeholders=_placeholder_value_map(placeholders),
            )
        )
        session.commit()
    finally:
        session.close()
    ui.notify('Group added', type='positive')
    _refresh()


def _placeholder_value_map(values: dict | None) -> dict[str, str] | None:
    cleaned: dict[str, str] = {}
    for tag, value in (values or {}).items():
        token = str(tag or '').strip().strip('[]')
        text = str(value or '').strip()
        if token and text:
            cleaned[token] = text
    return cleaned or None


def _save_section_placeholders(section_id: int, values: dict) -> None:
    session = _session()
    try:
        section = session.get(CourseSection, int(section_id))
        if section is None:
            ui.notify('Group not found', type='warning')
            return
        section.placeholders = _placeholder_value_map(values)
        session.commit()
    finally:
        session.close()
    ui.notify('Placeholder values saved', type='positive')
    _refresh()


def _delete_section(section_id: int) -> None:
    session = _session()
    try:
        section = session.get(CourseSection, int(section_id))
        if section is None:
            return
        issued = session.query(ParticipantLink).filter_by(section_id=section.id).count()
        if issued:
            ui.notify('This group already has links. Leave it so those URLs keep working.', type='warning')
            return
        session.delete(section)
        session.commit()
    finally:
        session.close()
    ui.notify('Group removed', type='positive')
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
            ui.notify('Add at least one group', type='warning')
            return
        body, filename, media_type = study_links_package(session, survey.id, base)
        session.commit()
    except ValueError as exc:
        session.rollback()
        ui.notify(str(exc), type='negative')
        return
    finally:
        session.close()
    ui.download(body, filename, media_type)
    _refresh()
