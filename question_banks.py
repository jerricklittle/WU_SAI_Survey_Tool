"""Admin UI for named question banks."""

import json

from nicegui import ui
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from authentication import is_admin
from question_library import (
    append_collection_questions,
    append_survey_questions,
    bank_description_from_payload,
    bank_name_from_payload,
    collection_question_ids,
    create_questions_from_items,
    move_collection_item,
    question_items_from_payload,
    reindex_collection,
    remove_collection_item,
    survey_question_ids,
)
from survey_models import QuestionCollection, QuestionCollectionItem, Survey, SurveyQuestion


def _session():
    from admin_panel import Session
    return Session()


def _user_id():
    from admin_panel import get_current_user_id
    return get_current_user_id()


def _visible_collections(session) -> list[QuestionCollection]:
    user_id = _user_id()
    return (
        session.query(QuestionCollection)
        .filter((QuestionCollection.created_by == user_id) | (QuestionCollection.created_by == None))
        .order_by(desc(QuestionCollection.updated_at))
        .all()
    )


def _visible_surveys(session) -> list[Survey]:
    user_id = _user_id()
    return (
        session.query(Survey)
        .filter((Survey.created_by == user_id) | (Survey.created_by == None))
        .order_by(desc(Survey.updated_at))
        .all()
    )


def _notify_added(added: int, skipped: int, *, noun: str = 'question') -> None:
    if added and skipped:
        ui.notify(f'Added {added} {noun}{"s" if added != 1 else ""}. Skipped {skipped} already present.', type='positive')
    elif added:
        ui.notify(f'Added {added} {noun}{"s" if added != 1 else ""}.', type='positive')
    elif skipped:
        ui.notify('Those questions are already included.', type='warning')
    else:
        ui.notify('No questions to add.', type='warning')


def _require_admin() -> bool:
    if is_admin():
        return True
    ui.navigate.to('/login?error=admin_only')
    return False


@ui.refreshable
def banks_index_page() -> None:
    """List named question banks."""
    session = _session()
    try:
        banks = _visible_collections(session)
        counts = {}
        if banks:
            ids = [bank.id for bank in banks]
            rows = (
                session.query(QuestionCollectionItem.collection_id)
                .filter(QuestionCollectionItem.collection_id.in_(ids))
                .all()
            )
            for (collection_id,) in rows:
                counts[collection_id] = counts.get(collection_id, 0) + 1
        snapshot = [
            {
                'id': bank.id,
                'name': bank.name,
                'description': (bank.description or '').strip(),
                'count': counts.get(bank.id, 0),
                'updated': bank.updated_at.strftime('%Y-%m-%d %H:%M') if bank.updated_at else '',
            }
            for bank in banks
        ]
    finally:
        session.close()

    with ui.column().classes('w-full p-8'):
        with ui.row().classes('w-full justify-between items-center mb-6 gap-2 flex-wrap'):
            ui.label('Question banks').classes('text-3xl font-bold')
            with ui.row().classes('gap-2 flex-wrap'):
                ui.button('← Back', on_click=lambda: ui.navigate.to('/admin')).classes('bg-gray-500 text-white')
                ui.button('All questions', on_click=lambda: ui.navigate.to('/admin/questions/all')).classes('bg-gray-600 text-white')
                ui.button('Import JSON', on_click=open_json_as_new_bank).classes('bg-slate-700 text-white')
                ui.button('➕ New bank', on_click=open_new_bank_dialog).classes('bg-blue-600 text-white')

        ui.label(
            'A bank is a named, ordered set of questions. Build one by creating questions, '
            'pulling them from another bank or a survey, or importing JSON.'
        ).classes('text-sm text-gray-600 mb-4')

        if not snapshot:
            ui.label('No question banks yet. Create one, or import a JSON file.').classes('text-gray-500 text-center py-8')
            return

        with ui.card().classes('w-full'):
            columns = [
                {'name': 'name', 'label': 'Name', 'field': 'name', 'align': 'left'},
                {'name': 'description', 'label': 'Description', 'field': 'description', 'align': 'left'},
                {'name': 'count', 'label': 'Questions', 'field': 'count', 'align': 'center'},
                {'name': 'updated', 'label': 'Updated', 'field': 'updated', 'align': 'left'},
                {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'},
            ]
            rows = []
            for bank in snapshot:
                description = bank['description']
                if len(description) > 80:
                    description = description[:80] + '...'
                rows.append({
                    'id': bank['id'],
                    'name': bank['name'],
                    'description': description,
                    'count': bank['count'],
                    'updated': bank['updated'],
                    'actions': bank['id'],
                })
            table = ui.table(columns=columns, rows=rows, row_key='id').classes('w-full')
            table.add_slot('body-cell-actions', '''
                <q-td :props="props">
                    <q-btn flat round dense icon="edit" size="sm" color="blue"
                           @click="$parent.$emit('edit', props.row.id)" />
                    <q-btn flat round dense icon="delete" size="sm" color="red"
                           @click="$parent.$emit('delete', props.row.id)" />
                </q-td>
            ''')
            table.on('edit', lambda e: ui.navigate.to(f'/admin/questions/banks/{e.args}'))
            table.on('delete', lambda e: delete_bank(e.args))


def open_new_bank_dialog() -> None:
    with ui.dialog() as dialog, ui.card().classes('w-full max-w-lg p-4'):
        ui.label('New question bank').classes('text-xl font-bold mb-2')
        name_input = ui.input('Bank name').classes('w-full')
        description_input = ui.textarea('Description').classes('w-full')

        def create() -> None:
            name = (name_input.value or '').strip()
            if not name:
                ui.notify('Bank name is required', type='negative')
                return
            description = (description_input.value or '').strip() or None
            session = _session()
            try:
                bank = QuestionCollection(name=name, description=description, created_by=_user_id())
                session.add(bank)
                session.commit()
                bank_id = bank.id
            finally:
                session.close()
            dialog.close()
            ui.notify(f'Question bank "{name}" created', type='positive')
            ui.navigate.to(f'/admin/questions/banks/{bank_id}')

        with ui.row().classes('justify-end w-full mt-4 gap-2'):
            ui.button('Cancel', on_click=dialog.close).props('flat')
            ui.button('Create', on_click=create).classes('bg-blue-600 text-white')
    dialog.open()


def delete_bank(collection_id: int) -> None:
    session = _session()
    try:
        bank = session.get(QuestionCollection, int(collection_id))
        if bank is None:
            ui.notify('Question bank not found', type='warning')
            return
        name = bank.name
        session.delete(bank)
        session.commit()
    finally:
        session.close()
    ui.notify(f'Question bank "{name}" deleted. Its questions are still in the library.', type='positive')
    banks_index_page.refresh()


def open_json_as_new_bank() -> None:
    with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl p-6'):
        ui.label('Import question bank from JSON').classes('text-xl font-bold mb-2')
        ui.label(
            'Use a survey JSON file, an object with a questions list, or a bare list of questions. '
            'A new bank is created and filled with new copies of those questions.'
        ).classes('text-sm text-gray-600 mb-4')

        async def on_upload(e) -> None:
            try:
                data = json.loads((await e.file.read()).decode('utf-8-sig'))
                items = question_items_from_payload(data)
            except Exception as ex:
                ui.notify(f'Could not read questions ({ex})', type='negative')
                return
            session = _session()
            try:
                bank = QuestionCollection(
                    name=bank_name_from_payload(data),
                    description=bank_description_from_payload(data),
                    created_by=_user_id(),
                )
                session.add(bank)
                session.flush()
                ids = create_questions_from_items(session, items, _user_id())
                append_collection_questions(session, bank.id, ids)
                session.commit()
                bank_id = bank.id
                bank_name = bank.name
            except Exception as ex:
                session.rollback()
                ui.notify(f'Import failed: {ex}', type='negative')
                return
            finally:
                session.close()
            dialog.close()
            ui.notify(f'Imported {len(items)} questions into "{bank_name}"', type='positive')
            ui.navigate.to(f'/admin/questions/banks/{bank_id}')

        ui.upload(label='Select JSON file', auto_upload=True, on_upload=on_upload).props('accept=.json').classes('w-full')
        with ui.row().classes('justify-end w-full mt-4'):
            ui.button('Close', on_click=dialog.close).classes('bg-gray-200 text-gray-700')
    dialog.open()


@ui.page('/admin/questions/banks/{collection_id}')
def bank_edit_page(collection_id: int) -> None:
    if not _require_admin():
        return
    collection_id = int(collection_id)
    session = _session()
    try:
        bank = session.get(QuestionCollection, collection_id)
        if bank is None:
            ui.notify('Question bank not found', type='negative')
            ui.navigate.to('/admin/questions')
            return
        bank_name = bank.name
        bank_description = bank.description or ''
    finally:
        session.close()

    @ui.refreshable
    def bank_questions_display() -> None:
        from admin_panel import _move_icon

        session = _session()
        try:
            reindex_collection(session, collection_id)
            rows = (
                session.query(QuestionCollectionItem)
                .options(joinedload(QuestionCollectionItem.question))
                .filter_by(collection_id=collection_id)
                .order_by(QuestionCollectionItem.sort_order.asc(), QuestionCollectionItem.id.asc())
                .all()
            )
            questions = [
                {
                    'item_id': item.id,
                    'question_id': item.question_id,
                    'order': item.sort_order,
                    'name': item.question.name if item.question else 'Question',
                    'text': item.question.question_text if item.question else '',
                }
                for item in rows
            ]
            session.commit()
        finally:
            session.close()

        if not questions:
            ui.label('No questions in this bank yet.').classes('text-gray-500')
            return

        last_index = len(questions) - 1

        def relocate(item_id: int, destination) -> None:
            session = _session()
            try:
                move_collection_item(session, item_id, destination)
                session.commit()
            finally:
                session.close()
            bank_questions_display.refresh()

        for index, question in enumerate(questions):
            with ui.card().classes('w-full p-4 mb-2'):
                with ui.row().classes('w-full justify-between items-center gap-3'):
                    with ui.column().classes('flex-1 min-w-0'):
                        ui.label(f'{question["order"]}. {question["name"]}').classes('font-semibold')
                        preview = question['text']
                        if len(preview) > 80:
                            preview = preview[:80] + '...'
                        ui.label(preview).classes('text-sm text-gray-600')
                    with ui.row().classes('gap-1 items-center flex-wrap justify-end'):
                        ui.button(
                            'Edit',
                            on_click=lambda qid=question['question_id'], bid=collection_id: ui.navigate.to(
                                f'/admin/questions/edit/{qid}?return_bank_id={bid}'
                            ),
                        ).props('flat dense no-caps').classes('text-blue-700')
                        _move_icon(
                            'vertical_align_top',
                            'Move to beginning',
                            index > 0,
                            lambda item_id=question['item_id']: relocate(item_id, 'start'),
                        )
                        _move_icon(
                            'arrow_upward',
                            'Move up',
                            index > 0,
                            lambda item_id=question['item_id']: relocate(item_id, 'up'),
                        )
                        _move_icon(
                            'arrow_downward',
                            'Move down',
                            index < last_index,
                            lambda item_id=question['item_id']: relocate(item_id, 'down'),
                        )
                        _move_icon(
                            'vertical_align_bottom',
                            'Move to end',
                            index < last_index,
                            lambda item_id=question['item_id']: relocate(item_id, 'end'),
                        )
                        ui.button(
                            'Place…',
                            on_click=lambda item_id=question['item_id']: open_place_dialog(item_id, relocate),
                        ).props('flat dense no-caps').tooltip('Place after another question')
                        ui.button(
                            icon='close',
                            on_click=lambda item_id=question['item_id']: drop_from_bank(item_id),
                        ).props('flat round dense color=red').tooltip('Remove from this bank')

        def drop_from_bank(item_id: int) -> None:
            session = _session()
            try:
                remove_collection_item(session, item_id)
                session.commit()
            finally:
                session.close()
            ui.notify('Removed from this bank. The question is still in the library.', type='positive')
            bank_questions_display.refresh()

    def save_details(name: str, description: str) -> None:
        cleaned = (name or '').strip()
        if not cleaned:
            ui.notify('Bank name is required', type='negative')
            return
        session = _session()
        try:
            bank = session.get(QuestionCollection, collection_id)
            if bank is None:
                ui.notify('Question bank not found', type='negative')
                return
            bank.name = cleaned
            bank.description = (description or '').strip() or None
            session.commit()
        finally:
            session.close()
        ui.notify('Question bank updated', type='positive')

    def add_question_ids(question_ids: list[int]) -> None:
        session = _session()
        try:
            added, skipped = append_collection_questions(session, collection_id, question_ids)
            session.commit()
        finally:
            session.close()
        _notify_added(added, skipped)
        bank_questions_display.refresh()

    def import_json_into_this_bank() -> None:
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl p-6'):
            ui.label('Import questions from JSON').classes('text-xl font-bold mb-2')
            ui.label(
                'Creates new questions from the file and adds them at the end of this bank.'
            ).classes('text-sm text-gray-600 mb-4')

            async def on_upload(e) -> None:
                try:
                    data = json.loads((await e.file.read()).decode('utf-8-sig'))
                    items = question_items_from_payload(data)
                except Exception as ex:
                    ui.notify(f'Could not read questions ({ex})', type='negative')
                    return
                session = _session()
                try:
                    ids = create_questions_from_items(session, items, _user_id())
                    added, skipped = append_collection_questions(session, collection_id, ids)
                    session.commit()
                except Exception as ex:
                    session.rollback()
                    ui.notify(f'Import failed: {ex}', type='negative')
                    return
                finally:
                    session.close()
                dialog.close()
                _notify_added(added, skipped)
                bank_questions_display.refresh()

            ui.upload(label='Select JSON file', auto_upload=True, on_upload=on_upload).props('accept=.json').classes('w-full')
            with ui.row().classes('justify-end w-full mt-4'):
                ui.button('Close', on_click=dialog.close).classes('bg-gray-200 text-gray-700')
        dialog.open()

    with ui.column().classes('w-full max-w-4xl mx-auto p-8'):
        with ui.row().classes('w-full justify-between items-center mb-6 gap-2'):
            ui.label('Edit question bank').classes('text-3xl font-bold')
            ui.button('← Question banks', on_click=lambda: ui.navigate.to('/admin/questions')).classes('bg-gray-500 text-white')

        with ui.card().classes('w-full p-6 mb-4'):
            name_input = ui.input('Bank name', value=bank_name).classes('w-full')
            description_input = ui.textarea('Description', value=bank_description).classes('w-full')
            ui.button(
                'Update details',
                on_click=lambda: save_details(name_input.value, description_input.value),
            ).classes('bg-blue-600 text-white mt-2')

        with ui.card().classes('w-full p-6'):
            ui.label('Questions').classes('text-2xl font-bold mb-2')
            ui.label(
                'Order here is the order used when this bank is imported into a survey. '
                'Removing a question only takes it out of this bank.'
            ).classes('text-sm text-gray-600 mb-4')
            with ui.row().classes('gap-2 mb-4 flex-wrap'):
                ui.button(
                    '➕ New question',
                    on_click=lambda: ui.navigate.to(f'/admin/questions/new?return_bank_id={collection_id}'),
                ).classes('bg-green-600 text-white')
                ui.button(
                    'From another bank',
                    on_click=lambda: open_bank_picker(
                        on_confirm=add_question_ids,
                        exclude_collection_id=collection_id,
                        title='Import from another question bank',
                        empty_message='No other question banks yet.',
                    ),
                ).classes('bg-blue-600 text-white')
                ui.button(
                    'From a survey',
                    on_click=lambda: open_survey_picker(on_confirm=add_question_ids),
                ).classes('bg-indigo-600 text-white')
                ui.button('Import JSON', on_click=import_json_into_this_bank).classes('bg-slate-700 text-white')
            bank_questions_display()


def open_place_dialog(item_id: int, relocate) -> None:
    session = _session()
    try:
        item = (
            session.query(QuestionCollectionItem)
            .options(joinedload(QuestionCollectionItem.question))
            .filter_by(id=item_id)
            .first()
        )
        if item is None:
            ui.notify('Question not found', type='warning')
            return
        rows = (
            session.query(QuestionCollectionItem)
            .options(joinedload(QuestionCollectionItem.question))
            .filter_by(collection_id=item.collection_id)
            .order_by(QuestionCollectionItem.sort_order.asc(), QuestionCollectionItem.id.asc())
            .all()
        )
        current_name = item.question.name if item.question else 'Question'
        current_order = item.sort_order
        choices = []
        previous_id = None
        passed = False
        for row in rows:
            name = row.question.name if row.question else 'Question'
            if row.id == item_id:
                passed = True
                continue
            choices.append((row.id, f'After {row.sort_order}. {name}'))
            if not passed:
                previous_id = row.id
    finally:
        session.close()

    options = {'start': 'At the beginning'}
    for row_id, label in choices:
        options[f'after:{row_id}'] = label
    options['end'] = 'At the end'
    default = 'start' if previous_id is None else f'after:{previous_id}'

    with ui.dialog() as dialog, ui.card().classes('w-full max-w-lg p-4'):
        ui.label('Move question').classes('text-xl font-bold')
        ui.label(f'{current_order}. {current_name}').classes('text-gray-600 mb-2')
        place = ui.select(options, value=default, label='Place').classes('w-full')

        def apply() -> None:
            choice = place.value or default
            dialog.close()
            if choice == 'start':
                relocate(item_id, 'start')
            elif choice == 'end':
                relocate(item_id, 'end')
            elif str(choice).startswith('after:'):
                relocate(item_id, ('after', int(str(choice).split(':', 1)[1])))
            ui.notify('Question moved', type='positive')

        with ui.row().classes('justify-end w-full mt-4 gap-2'):
            ui.button('Cancel', on_click=dialog.close).props('flat')
            ui.button('Move', on_click=apply).classes('bg-blue-600 text-white')
    dialog.open()


def open_bank_picker(
    *,
    on_confirm,
    exclude_collection_id: int | None = None,
    title: str = 'Import question bank',
    empty_message: str = 'Create a question bank first.',
) -> None:
    """Pick a whole bank or some of its questions. ``on_confirm`` receives question ids in bank order."""
    session = _session()
    try:
        banks = [
            bank for bank in _visible_collections(session)
            if exclude_collection_id is None or bank.id != int(exclude_collection_id)
        ]
        options = {str(bank.id): bank.name for bank in banks}
    finally:
        session.close()

    if not options:
        ui.notify(empty_message, type='warning')
        return

    selected_id = next(iter(options))
    checks: list[tuple[int, object]] = []

    with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl p-4'):
        ui.label(title).classes('text-xl font-bold')
        ui.label(
            'Add the entire bank, or tick the questions you want. Questions already present are skipped.'
        ).classes('text-sm text-gray-600 mb-2')

        @ui.refreshable
        def question_checks() -> None:
            checks.clear()
            session = _session()
            try:
                rows = (
                    session.query(QuestionCollectionItem)
                    .options(joinedload(QuestionCollectionItem.question))
                    .filter_by(collection_id=int(selected_id))
                    .order_by(QuestionCollectionItem.sort_order.asc(), QuestionCollectionItem.id.asc())
                    .all()
                )
                loaded = [
                    (row.question_id, f'{row.sort_order}. {row.question.name if row.question else "Question"}')
                    for row in rows
                ]
            finally:
                session.close()
            if not loaded:
                ui.label('This bank has no questions.').classes('text-gray-500')
                return
            with ui.column().classes('w-full max-h-80 overflow-y-auto gap-1 border border-gray-200 rounded p-2'):
                for question_id, label in loaded:
                    checkbox = ui.checkbox(label).classes('w-full')
                    checks.append((question_id, checkbox))

        def on_bank_change(e) -> None:
            nonlocal selected_id
            selected_id = str(e.value)
            question_checks.refresh()

        ui.select(options, value=selected_id, label='Question bank', on_change=on_bank_change).classes('w-full')
        question_checks()

        def confirm_all() -> None:
            session = _session()
            try:
                ids = collection_question_ids(session, int(selected_id))
                session.commit()
            finally:
                session.close()
            if not ids:
                ui.notify('This bank has no questions.', type='warning')
                return
            dialog.close()
            on_confirm(ids)

        def confirm_selected() -> None:
            ids = [question_id for question_id, checkbox in checks if checkbox.value]
            if not ids:
                ui.notify('Select at least one question, or add the entire bank.', type='warning')
                return
            dialog.close()
            on_confirm(ids)

        with ui.row().classes('justify-end w-full mt-4 gap-2 flex-wrap'):
            ui.button('Cancel', on_click=dialog.close).props('flat')
            ui.button('Add selected', on_click=confirm_selected).classes('bg-slate-700 text-white')
            ui.button('Add entire bank', on_click=confirm_all).classes('bg-blue-600 text-white')
    dialog.open()


def open_survey_picker(*, on_confirm) -> None:
    """Pick questions that already sit on a survey. ``on_confirm`` receives those question ids in survey order."""
    session = _session()
    try:
        surveys = _visible_surveys(session)
        options = {str(survey.id): survey.name for survey in surveys}
    finally:
        session.close()

    if not options:
        ui.notify('No surveys to import from.', type='warning')
        return

    selected_id = next(iter(options))
    checks: list[tuple[int, object]] = []

    with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl p-4'):
        ui.label('Import questions from a survey').classes('text-xl font-bold')
        ui.label(
            'Uses the questions already on that survey, in survey order. Questions already in this bank are skipped.'
        ).classes('text-sm text-gray-600 mb-2')

        @ui.refreshable
        def question_checks() -> None:
            checks.clear()
            session = _session()
            try:
                rows = (
                    session.query(SurveyQuestion)
                    .options(joinedload(SurveyQuestion.question))
                    .filter_by(survey_id=int(selected_id))
                    .order_by(SurveyQuestion.order.asc(), SurveyQuestion.id.asc())
                    .all()
                )
                loaded = [
                    (row.question_id, f'{row.order}. {row.question.name if row.question else "Question"}')
                    for row in rows
                ]
            finally:
                session.close()
            if not loaded:
                ui.label('This survey has no questions.').classes('text-gray-500')
                return
            with ui.column().classes('w-full max-h-80 overflow-y-auto gap-1 border border-gray-200 rounded p-2'):
                for question_id, label in loaded:
                    checkbox = ui.checkbox(label).classes('w-full')
                    checks.append((question_id, checkbox))

        def on_survey_change(e) -> None:
            nonlocal selected_id
            selected_id = str(e.value)
            question_checks.refresh()

        ui.select(options, value=selected_id, label='Survey', on_change=on_survey_change).classes('w-full')
        question_checks()

        def confirm_all() -> None:
            session = _session()
            try:
                ids = survey_question_ids(session, int(selected_id))
            finally:
                session.close()
            if not ids:
                ui.notify('This survey has no questions.', type='warning')
                return
            dialog.close()
            on_confirm(ids)

        def confirm_selected() -> None:
            ids = [question_id for question_id, checkbox in checks if checkbox.value]
            if not ids:
                ui.notify('Select at least one question, or add all questions from the survey.', type='warning')
                return
            dialog.close()
            on_confirm(ids)

        with ui.row().classes('justify-end w-full mt-4 gap-2 flex-wrap'):
            ui.button('Cancel', on_click=dialog.close).props('flat')
            ui.button('Add selected', on_click=confirm_selected).classes('bg-slate-700 text-white')
            ui.button('Add all from survey', on_click=confirm_all).classes('bg-blue-600 text-white')
    dialog.open()


def import_bank_into_survey(survey_id: int) -> None:
    from admin_panel import questions_list_display

    def on_confirm(question_ids: list[int]) -> None:
        session = _session()
        try:
            added, skipped = append_survey_questions(session, int(survey_id), question_ids)
            session.commit()
        finally:
            session.close()
        _notify_added(added, skipped)
        questions_list_display.refresh()

    open_bank_picker(on_confirm=on_confirm, title='Import into this survey')
