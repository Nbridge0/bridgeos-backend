import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I don't have this data yet. Please ask your admin to upload it."
)


def ask_llm(query: str, context: str) -> str:
    """
    BridgeOS LLM adapter.

    This keeps the existing BridgeOS logic in services.py.
    services.chat() still handles retrieval, permissions, uploaded files,
    sources, metadata logic, invoice logic, and JSON parsing.

    This function only replaces the old OpenAI call with RunPod generation.
    """

    if not RUNPOD_BASE_URL:
        print("RUNPOD GENERATE ERROR: RUNPOD_BASE_URL missing")
        return FALLBACK_NO_DATA_ANSWER

    if not BRIDGEOS_API_KEY:
        print("RUNPOD GENERATE ERROR: BRIDGEOS_API_KEY missing")
        return FALLBACK_NO_DATA_ANSWER

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/generate"

    prompt = f"""
{context or ""}

Current user request:
{query}
""".strip()

    try:
        response = requests.post(
            url,
            json={
                "prompt": prompt
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        print("RUNPOD GENERATE DEBUG: url:", url)
        print("RUNPOD GENERATE DEBUG: status:", response.status_code)
        print("RUNPOD GENERATE DEBUG: response:", response.text[:500])

        if response.status_code >= 400:
            return FALLBACK_NO_DATA_ANSWER

        data = response.json()

        answer = (
            data.get("response")
            or data.get("answer")
            or data.get("message")
            or ""
        )

        answer = str(answer or "").strip()

        if not answer:
            return FALLBACK_NO_DATA_ANSWER

        return answer

    except Exception as e:
        print("RUNPOD GENERATE REQUEST ERROR:", type(e).__name__, str(e))
        return FALLBACK_NO_DATA_ANSWER