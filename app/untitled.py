import requests

from app.config import LLM_API_URL, LLM_API_KEY


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I don't have this data yet. Please ask your admin to upload it."
)


def ask_llm(query: str, context: str):
    """
    Calls your own LLM API.

    Expected API response options:
    {
      "answer": "..."
    }

    If no LLM API is configured yet, this returns a development response.
    """

    if not context.strip():
        return FALLBACK_NO_DATA_ANSWER

    system_prompt = """
You are a secure yacht assistant.

You must follow these rules strictly:

1. Answer only using the provided context.
2. Do not use outside knowledge.
3. Do not guess.
4. Do not mention other users, other yachts, hidden documents, or unavailable files.
5. If the answer is not clearly present in the provided context, say exactly:
"Sorry, I don't have this data yet. Please ask your admin to upload it."
"""

    if not LLM_API_URL or not LLM_API_KEY:
        return {
            "development_mode": True,
            "message": "LLM API is not configured yet. This is the context that would be sent to your LLM.",
            "query": query,
            "context": context
        }

    payload = {
        "system": system_prompt,
        "question": query,
        "context": context
    }

    response = requests.post(
        LLM_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json"
        },
        timeout=60
    )

    response.raise_for_status()
    data = response.json()

    answer = data.get("answer")

    if not answer:
        return FALLBACK_NO_DATA_ANSWER

    return answer