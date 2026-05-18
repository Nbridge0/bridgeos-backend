import requests

from app.config import LLM_API_URL, LLM_API_KEY


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I don't have this data yet. Please ask your admin to upload it."
)


def ask_llm(query: str, context: str) -> str:
    """
    Calls your LLM API.

    Always returns a string.
    """

    if not context or not context.strip():
        return FALLBACK_NO_DATA_ANSWER

    system_prompt = """
You are a secure yacht memory assistant.

You answer only using the provided context.

The context may include:
- extracted text from documents
- OCR text from images or scanned files
- visual descriptions of uploaded images
- metadata such as file names, detected years, tags, and source information

Rules:
1. Answer only using the provided context.
2. Do not use outside knowledge.
3. Do not guess.
4. If the answer is not clearly supported by the context, say exactly:
"Sorry, I don't have this data yet. Please ask your admin to upload it."
5. Do not mention other users, other yachts, hidden documents, or unavailable files.
6. If the answer is based on images, say it is based on uploaded image descriptions.
7. If the context is uncertain, explain the uncertainty briefly.
8. Be specific and practical.
"""

    if not LLM_API_URL or not LLM_API_KEY:
        return (
            "LLM API is not configured yet. "
            "The retrieval system found context, but no LLM is connected to generate the final answer."
        )

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