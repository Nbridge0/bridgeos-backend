import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I don't have this data yet. Please ask your admin to upload it."
)


def ask_llm(query: str, context: str) -> str:
    """
    BridgeOS LLM adapter.

    IMPORTANT:
    This keeps all existing BridgeOS logic in services.py.
    services.py still handles:
    - document retrieval
    - uploaded-file logic
    - sources
    - metadata/file listing
    - invoice rules
    - memory-aware retrieval
    - fallback rules

    This function only replaces the old OpenAI call with RunPod.
    """

    if not RUNPOD_BASE_URL:
        print("RUNPOD LLM ERROR: RUNPOD_BASE_URL missing")
        return FALLBACK_NO_DATA_ANSWER

    if not BRIDGEOS_API_KEY:
        print("RUNPOD LLM ERROR: BRIDGEOS_API_KEY missing")
        return FALLBACK_NO_DATA_ANSWER

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    system_rules = """
You are BridgeOS, a secure yacht documentation assistant.

Always respond in British English.

Follow the user's provided instructions exactly.
If the user asks you to return JSON only, return JSON only.
If document context is provided, use only that context.
Do not invent facts, files, procedures, amounts, dates, names, or sources.
""".strip()

    user_input = f"""
{context or ""}

User request:
{query}
""".strip()

    try:
        response = requests.post(
            url,
            json={
                "user_input": user_input,
                "history": [
                    {
                        "role": "system",
                        "content": system_rules
                    }
                ],
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
        print("RUNPOD LLM REQUEST ERROR:", type(e).__name__, str(e))
        return FALLBACK_NO_DATA_ANSWER