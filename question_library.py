"""Named question banks: ordered membership, JSON question creation, survey attach."""

from __future__ import annotations

from datetime import datetime

from survey_models import QuestionBank, QuestionCollection, QuestionCollectionItem, SurveyQuestion


def question_items_from_payload(data) -> list[dict]:
    """Accept a survey/bank object or a bare list of question objects."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        raw = data.get('questions')
        items = raw if isinstance(raw, list) else []
    else:
        raise ValueError('JSON must be an object with questions, or a list of questions')
    q_items = [q for q in items if isinstance(q, dict)]
    if not q_items:
        raise ValueError('No questions found')
    q_items.sort(key=lambda x: (int(x.get('order') or 0), str(x.get('id') or '')))
    return q_items


def bank_name_from_payload(data) -> str:
    if isinstance(data, dict):
        name = (data.get('name') or data.get('title') or '').strip()
        if name:
            return name
    return 'Imported question bank'


def bank_description_from_payload(data) -> str | None:
    if isinstance(data, dict):
        text = (data.get('description') or '').strip()
        return text or None
    return None


def item_is_adaptive(q: dict) -> bool:
    return bool(q.get('adaptive') or q.get('is_adaptive'))


def create_question_from_item(session, q: dict, seq: int, created_by: int | None) -> QuestionBank:
    """Insert one question row from a survey-style JSON object. Caller commits."""
    qtype = str(q.get('type') or q.get('question_type') or 'text').strip() or 'text'
    prompt = str(q.get('prompt') or q.get('question_text') or '').strip()
    qid = str(q.get('id') or q.get('name') or f'q_{seq}').strip() or f'q_{seq}'
    cfg: dict = {}
    if isinstance(q.get('tags'), list):
        cfg['tags'] = list(q.get('tags'))
    if qtype == 'likert':
        cfg['scale'] = q.get('scale') or {}
        cfg['allowComment'] = bool(q.get('allowComment'))
    elif qtype == 'boolean':
        cfg['options'] = q.get('options') or {}
    elif qtype == 'text':
        cfg['text'] = q.get('text') or {}
        if item_is_adaptive(q):
            cfg['adaptive'] = True
            cfg['prompt_text'] = str(q.get('prompt_text') or '').strip()
    elif qtype == 'multi':
        cfg['options'] = q.get('options') or []

    question = QuestionBank(
        name=qid,
        question_text=prompt or qid,
        question_type=qtype,
        version=int(q.get('version') or 1),
        created_by=created_by,
        config=cfg,
    )
    session.add(question)
    session.flush()
    return question


def create_questions_from_items(session, items: list[dict], created_by: int | None) -> list[int]:
    ids: list[int] = []
    for seq, item in enumerate(items, 1):
        question = create_question_from_item(session, item, seq, created_by)
        ids.append(question.id)
    return ids


def ordered_collection_items(session, collection_id: int) -> list[QuestionCollectionItem]:
    return (
        session.query(QuestionCollectionItem)
        .filter_by(collection_id=collection_id)
        .order_by(QuestionCollectionItem.sort_order.asc(), QuestionCollectionItem.id.asc())
        .all()
    )


def assign_sort_orders(session, rows: list[QuestionCollectionItem]) -> None:
    for i, row in enumerate(rows):
        row.sort_order = -(i + 1)
    session.flush()
    for i, row in enumerate(rows, 1):
        row.sort_order = i


def reindex_collection(session, collection_id: int) -> list[QuestionCollectionItem]:
    rows = ordered_collection_items(session, collection_id)
    expected = list(range(1, len(rows) + 1))
    if [row.sort_order for row in rows] != expected:
        assign_sort_orders(session, rows)
    return rows


def _touch_collection(session, collection_id: int) -> None:
    collection = session.get(QuestionCollection, collection_id)
    if collection is not None:
        collection.updated_at = datetime.utcnow()


def append_collection_questions(session, collection_id: int, question_ids: list[int]) -> tuple[int, int]:
    """Add existing questions to a bank, skipping ones already in it. Returns (added, skipped)."""
    rows = reindex_collection(session, collection_id)
    existing = {row.question_id for row in rows}
    next_order = len(rows)
    added = skipped = 0
    for raw_id in question_ids:
        qid = int(raw_id)
        if qid in existing:
            skipped += 1
            continue
        if session.get(QuestionBank, qid) is None:
            continue
        next_order += 1
        session.add(
            QuestionCollectionItem(
                collection_id=collection_id,
                question_id=qid,
                sort_order=next_order,
            )
        )
        existing.add(qid)
        added += 1
    if added:
        _touch_collection(session, collection_id)
    return added, skipped


def move_collection_item(session, item_id: int, destination) -> None:
    from admin_panel import _placement_index

    item = session.get(QuestionCollectionItem, item_id)
    if item is None:
        return
    rows = ordered_collection_items(session, item.collection_id)
    ids = [row.id for row in rows]
    if item_id not in ids:
        return
    current = ids.index(item_id)
    remaining_ids = [row_id for row_id in ids if row_id != item_id]
    index = _placement_index(current, remaining_ids, destination)
    remaining_ids.insert(index, item_id)
    by_id = {row.id: row for row in rows}
    assign_sort_orders(session, [by_id[row_id] for row_id in remaining_ids])
    _touch_collection(session, item.collection_id)


def remove_collection_item(session, item_id: int) -> None:
    item = session.get(QuestionCollectionItem, item_id)
    if item is None:
        return
    collection_id = item.collection_id
    session.delete(item)
    session.flush()
    assign_sort_orders(session, ordered_collection_items(session, collection_id))
    _touch_collection(session, collection_id)


def append_survey_questions(session, survey_id: int, question_ids: list[int]) -> tuple[int, int]:
    """Append questions to a survey in the given order. Skips questions already on it."""
    from admin_panel import _reindex_survey_questions

    existing_rows = _reindex_survey_questions(session, survey_id)
    existing_ids = {row.question_id for row in existing_rows}
    next_order = len(existing_rows)
    added = skipped = 0
    for raw_id in question_ids:
        qid = int(raw_id)
        if qid in existing_ids:
            skipped += 1
            continue
        if session.get(QuestionBank, qid) is None:
            continue
        next_order += 1
        session.add(
            SurveyQuestion(
                survey_id=survey_id,
                question_id=qid,
                order=next_order,
            )
        )
        existing_ids.add(qid)
        added += 1
    return added, skipped


def collection_question_ids(session, collection_id: int) -> list[int]:
    return [row.question_id for row in reindex_collection(session, collection_id)]


def survey_question_ids(session, survey_id: int) -> list[int]:
    rows = (
        session.query(SurveyQuestion)
        .filter_by(survey_id=survey_id)
        .order_by(SurveyQuestion.order.asc(), SurveyQuestion.id.asc())
        .all()
    )
    return [row.question_id for row in rows]
