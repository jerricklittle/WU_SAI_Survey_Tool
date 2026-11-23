from openai import AsyncOpenAI
import os

# Default decision rules (used in the question editor when adaptive follow-ups are on, and as the model fallback when prompt_text is empty).
DEFAULT_ADAPTIVE_PROMPT_TEXT = """
You are collecting clearer, more specific answers to an open-ended survey question.

Given the respondent's answer, decide whether a follow-up question would add
useful detail beyond what was already said.

Decision rules:
- Request follow-up questions if ANY of the following are true:
    1. The answer is vague, generic, or lacks specifics
       (e.g. "it was fine", "I didn't like it", "could be better").
    2. The respondent mentions a problem, frustration, or negative experience
       but does not explain why or give an example.
    3. The respondent names something specific but does not explain why it mattered.
    4. The answer suggests a tradeoff or tension worth exploring.
- Do NOT generate follow-ups for answers that are already detailed,
  concrete, and self-contained.

Generation rules:
- Generate at most 2 follow-up questions per answer.
- If the answer was vague or negative without detail, the first follow-up
  should ask for a specific example.
- Follow-up questions must be open-ended.
- Do NOT repeat, restate, or closely paraphrase the original question or
  the respondent's answer.
- Each follow-up should explore a new angle or clarify an implication.
""".strip()

# Fixed JSON contract (always appended to the system message).
ADAPTIVE_FOLLOWUP_OUTPUT_SPEC = """
Output rules:
- If follow-up questions are needed, set "needs_followup" to true.
- If no follow-up questions are needed, set "needs_followup" to false and
  return an empty list for "followup_questions".
- Respond ONLY with valid JSON. Do not include explanations, commentary,
  or formatting outside the JSON.

Return JSON in exactly the following format:

{
  "needs_followup": true,
  "followup_questions": [
    {
      "id": "followup_1",
      "prompt": "Can you describe a specific example of when this happened?",
      "source_question_id": "q1"
    },
    {
      "id": "followup_2",
      "prompt": "How did this impact your learning experience?",
      "source_question_id": "q1"
    }
  ]
}
""".strip()

# Full legacy system prompt (default guidance + output spec).
adaptive_prompt = f"{DEFAULT_ADAPTIVE_PROMPT_TEXT}\n\n{ADAPTIVE_FOLLOWUP_OUTPUT_SPEC}"

FOLLOWUP_SYSTEM_PREAMBLE = """You help analyze open-ended survey responses and propose optional follow-up questions.
Each numbered response below begins with a "Guidance" section. Apply that guidance only when deciding whether follow-ups are needed for that specific response, and when phrasing follow-up questions for that response.
""".strip()


def _effective_prompt_text(response: dict) -> str:
    raw = response.get("prompt_text")
    if raw is None:
        return DEFAULT_ADAPTIVE_PROMPT_TEXT
    text = str(raw).strip()
    return text if text else DEFAULT_ADAPTIVE_PROMPT_TEXT


async def analyze_all_responses_for_survey(text_responses):
    """
    Analyzes multiple open-ended responses at once and generates
    follow-up questions for all of them in a single GPT call.

    Args:
        text_responses: List of dicts with keys: question_id, text, prompt,
            and optional prompt_text (extra guidance for the model;
            empty or missing uses DEFAULT_ADAPTIVE_PROMPT_TEXT).

    Returns:
        JSON string with needs_followup and followup_questions array
    """
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    system_content = f"{FOLLOWUP_SYSTEM_PREAMBLE}\n\n{ADAPTIVE_FOLLOWUP_OUTPUT_SPEC}"

    combined_text = "The respondent provided the following answers:\n\n"

    for i, response in enumerate(text_responses, 1):
        guidance = _effective_prompt_text(response)
        combined_text += f"Response {i}:\n"
        combined_text += f"Guidance (for this response only):\n{guidance}\n\n"
        combined_text += f"Original Question: {response['prompt']}\n"
        combined_text += f"Question ID: {response['question_id']}\n"
        combined_text += f"Answer: {response['text']}\n\n"

    combined_text += (
        "\nBased on ALL of the above responses, generate follow-up questions where appropriate. "
        "Respect each response's Guidance only for that response. "
        "Include the source_question_id field in each follow-up to indicate which response it's addressing."
    )

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": combined_text},
        ],
        temperature=0.2,
    )

    raw_response = response.choices[0].message.content
    cleaned_response = raw_response.strip()
    if cleaned_response.startswith("```json"):
        cleaned_response = cleaned_response[7:]
    if cleaned_response.startswith("```"):
        cleaned_response = cleaned_response[3:]
    if cleaned_response.endswith("```"):
        cleaned_response = cleaned_response[:-3]

    return cleaned_response.strip()


def _clean_model_json(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


PART_FOLLOWUP_OUTPUT_SPEC = """
Output rules:
- Write exactly the requested number of follow-up questions. The requested number wins over any different count in the instructions.
- Each question is its own item. Do not combine them into one question.
- If the answers are empty, set "needs_followup" to false and return an empty list.
- Respond ONLY with valid JSON. Do not include explanations outside the JSON.

Return JSON in exactly the following format:

{
  "needs_followup": true,
  "followup_questions": [
    {
      "id": "followup_1",
      "prompt": "Can you say more about what stood out?"
    }
  ]
}
""".strip()


async def analyze_part_followups(text_responses, question_count: int = 3, instructions: str = "") -> str:
    """Write one set of follow-up questions from the open-ended answers in a previous part."""
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    try:
        count = int(question_count)
    except (TypeError, ValueError):
        count = 3
    count = max(1, min(count, 10))
    guidance = (instructions or "").strip() or (
        "Ask for clearer, more specific detail. Do not repeat the earlier questions."
    )
    system_content = (
        f"You write exactly {count} follow-up survey questions from a respondent's "
        "open-ended answers in the previous part of a survey. "
        f"Return {count} separate questions.\n\n"
        f"{PART_FOLLOWUP_OUTPUT_SPEC}"
    )
    combined_text = (
        f"Write exactly {count} follow-up questions.\n\n"
        f"Instructions:\n{guidance}\n\n"
        "Open-ended answers from the previous part:\n\n"
    )
    for index, response in enumerate(text_responses, 1):
        combined_text += f"Answer {index}\n"
        combined_text += f"Question: {response.get('prompt') or ''}\n"
        combined_text += f"Answer: {response.get('text') or ''}\n\n"

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": combined_text},
        ],
        temperature=0.2,
    )
    return _clean_model_json(response.choices[0].message.content or "")
