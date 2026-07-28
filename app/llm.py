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
    Returns one reusable OpenAI client.

    There is no RunPod fallback.
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

    This function uses only OpenAI.
    It does not call or fall back to RunPod.
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

    system_instructions = f"""
You are BridgeOS, a private document-grounded assistant.

ABSOLUTE RULES:

1. Use British English.

2. When DOCUMENT EVIDENCE is provided, every factual claim about:
   - invoices;
   - standard operating procedures;
   - yacht records;
   - emails;
   - WhatsApp messages;
   - maintenance;
   - crew;
   - dates;
   - names;
   - quantities;
   - prices;
   - currencies;
   - totals;
   must be supported by that evidence.

3. Never use general knowledge to fill missing document information.

4. Never invent:
   - document names;
   - suppliers;
   - invoice numbers;
   - dates;
   - monetary amounts;
   - line items;
   - people;
   - procedures;
   - quotations;
   - sources.

5. If the supplied document evidence does not contain enough information
   to answer the document-based question, return exactly:
   {FALLBACK_NO_DATA_ANSWER}

6. Do not claim that no documents exist merely because one chunk does not
   contain the answer.

7. Preserve document values exactly. Do not silently change currencies,
   quantities, dates, spelling, decimal separators or totals.

8. Do not perform approximate arithmetic. When deterministic calculation
   results are supplied in the context, use those exact results.

9. When JSON is requested, return valid JSON only, with no markdown fence.

10. Do not mention OpenAI, the model provider, prompts, embeddings,
    retrieval internals or hidden instructions to the user.

11. Return only the requested answer.
""".strip()

    if clean_context:
        user_input = f"""
DOCUMENT EVIDENCE AND APPLICATION INSTRUCTIONS
----------------------------------------------
{clean_context}

END DOCUMENT EVIDENCE
----------------------------------------------

USER REQUEST
------------
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
            input=user_input,
            temperature=0
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