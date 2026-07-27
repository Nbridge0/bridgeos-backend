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
    Return one reusable OpenAI client.

    This function does not use RunPod and has no RunPod fallback.
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


def ask_llm(
    query: str,
    context: str = ""
) -> str:
    """
    Main BridgeOS language-model function.

    Every existing call to ask_llm() in services.py will use OpenAI.
    There is deliberately no RunPod fallback in this function.
    """

    clean_query = str(query or "").strip()
    clean_context = str(context or "").strip()

    if not clean_query:
        return ""

    client = get_openai_client()

    system_instructions = """
You are BridgeOS, a private document-based assistant.

Rules:
- Use British English.
- Follow the instructions supplied in the context.
- When document context is supplied, use only that context.
- Do not invent facts, names, dates, numbers, currencies or sources.
- When JSON is requested, return valid JSON only.
- Do not wrap JSON in markdown code fences.
- Preserve values from documents exactly.
- Return only the requested result.
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

    print(
        "OPENAI REQUEST START:",
        {
            "provider": "openai",
            "model": OPENAI_CHAT_MODEL,
            "query_characters": len(clean_query),
            "context_characters": len(clean_context)
        }
    )

    try:
        response = client.responses.create(
            model=OPENAI_CHAT_MODEL,
            instructions=system_instructions,
            input=user_input
        )

        answer = str(
            response.output_text or ""
        ).strip()

        print(
            "OPENAI REQUEST SUCCESS:",
            {
                "provider": "openai",
                "model": OPENAI_CHAT_MODEL,
                "response_id": getattr(
                    response,
                    "id",
                    None
                ),
                "answer_characters": len(answer)
            }
        )

        if not answer:
            raise RuntimeError(
                "OpenAI returned an empty response."
            )

        return answer

    except Exception as error:
        print(
            "OPENAI REQUEST FAILED:",
            {
                "provider": "openai",
                "model": OPENAI_CHAT_MODEL,
                "error_type": type(error).__name__,
                "error": str(error)
            }
        )

        raise