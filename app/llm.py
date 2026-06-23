import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I don't have this data yet. Please ask your admin to upload it."
)


def ask_llm(query: str, context: str) -> str:
    if not RUNPOD_BASE_URL:
        print("RUNPOD LLM ERROR: RUNPOD_BASE_URL missing")
        return FALLBACK_NO_DATA_ANSWER

    if not BRIDGEOS_API_KEY:
        print("RUNPOD LLM ERROR: BRIDGEOS_API_KEY missing")
        return FALLBACK_NO_DATA_ANSWER

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    user_input = f"""
Question:
{query}

Context:
{context or ""}
""".strip()

    try:
        response = requests.post(
            url,
            json={
                "user_input": user_input,
                "history": [],
                "backend_context": {}
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        print("RUNPOD LLM DEBUG: url:", url)
        print("RUNPOD LLM DEBUG: status:", response.status_code)
        print("RUNPOD LLM DEBUG: response:", response.text[:500])

        if response.status_code >= 400:
            return FALLBACK_NO_DATA_ANSWER

        data = response.json()

        return (
            data.get("response")
            or data.get("answer")
            or FALLBACK_NO_DATA_ANSWER
        )

    except Exception as e:
        print("RUNPOD LLM REQUEST ERROR:", type(e).__name__, str(e))
        return FALLBACK_NO_DATA_ANSWER