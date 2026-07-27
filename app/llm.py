from openai import OpenAI

from app.config import (
    OPENAI_API_KEY,
    OPENAI_CHAT_MODEL
)


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I could not find enough information "
    "in the available documents to answer that."
)


_openai_client = None


def get_openai_client() -> OpenAI:
    """
    Creates and reuses one OpenAI client.

    The API key is read from the backend environment through app.config.
    """

    global _openai_client

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is missing from the backend environment."
        )

    if _openai_client is None:
        _openai_client = OpenAI(
            api_key=OPENAI_API_KEY,
            timeout=120.0,
            max_retries=2
        )

    return _openai_client


def extract_openai_response_text(response) -> str:
    """
    Extracts the final text from an OpenAI Responses API response.
    """

    direct_text = getattr(
        response,
        "output_text",
        None
    )

    if direct_text:
        return str(direct_text).strip()

    text_parts = []

    output_items = getattr(
        response,
        "output",
        None
    ) or []

    for output_item in output_items:
        content_items = getattr(
            output_item,
            "content",
            None
        ) or []

        for content_item in content_items:
            content_text = getattr(
                content_item,
                "text",
                None
            )

            if content_text:
                text_parts.append(
                    str(content_text)
                )

    return "\n".join(
        text_parts
    ).strip()


def ask_llm(
    query: str,
    context: str = ""
) -> str:
    """
    Main BridgeOS language-model function.

    Existing code can continue calling:

        ask_llm(
            query=query,
            context=context
        )

    No services.py call sites need to change.
    """

    clean_query = str(
        query or ""
    ).strip()

    clean_context = str(
        context or ""
    ).strip()

    if not clean_query:
        return ""

    client = get_openai_client()

    system_instructions = """
You are BridgeOS, a private document-based assistant.

Follow the instructions contained in the supplied context.

Rules:
- Use British English unless the requested format requires otherwise.
- When document context is supplied, use only that document context
  for factual answers.
- Do not invent facts, names, numbers, dates, sources, values or events.
- When asked to return JSON, return valid JSON only.
- Do not wrap JSON in markdown code fences.
- Preserve monetary values, decimal separators, quantities and currencies.
- Do not perform approximate arithmetic when exact values are available.
- Follow the requested output structure exactly.
- Return only the requested answer or structured result.
""".strip()

    if clean_context:
        user_input = f"""
Instructions and document context:
{clean_context}

User request:
{clean_query}
""".strip()

    else:
        user_input = clean_query

    try:
        response = client.responses.create(
            model=OPENAI_CHAT_MODEL,
            instructions=system_instructions,
            input=user_input
        )

        answer = extract_openai_response_text(
            response
        )

        print(
            "OPENAI LLM RESPONSE:",
            {
                "model": OPENAI_CHAT_MODEL,
                "query_characters": len(
                    clean_query
                ),
                "context_characters": len(
                    clean_context
                ),
                "answer_characters": len(
                    answer
                )
            }
        )

        return answer

    except Exception as error:
        print(
            "OPENAI LLM ERROR:",
            {
                "model": OPENAI_CHAT_MODEL,
                "error_type": type(
                    error
                ).__name__,
                "error": str(error)
            }
        )

        raise RuntimeError(
            "OpenAI generation failed: "
            f"{type(error).__name__}: {str(error)}"
        ) from error