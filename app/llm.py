import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I could not generate a response right now. Please try again."
)


def ask_llm(query: str, context: str = "") -> str:
    """
    Calls your own BridgeOS / RunPod LLM.

    No OpenAI.
    Uses the same RunPod payload style that was already working in services.py.
    """

    if not RUNPOD_BASE_URL:
        print("LLM ERROR: RUNPOD_BASE_URL is missing")
        return FALLBACK_NO_DATA_ANSWER

    if not BRIDGEOS_API_KEY:
        print("LLM ERROR: BRIDGEOS_API_KEY is missing")
        return FALLBACK_NO_DATA_ANSWER

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    if context and context.strip():
        user_input = f"""
{query}

Context:
{context}
""".strip()
    else:
        user_input = query

    try:
        print("RUNPOD LLM DEBUG: url:", url)
        print("RUNPOD LLM DEBUG: key present:", bool(BRIDGEOS_API_KEY))
        print("RUNPOD LLM DEBUG: key length:", len(BRIDGEOS_API_KEY or ""))
        print("RUNPOD LLM DEBUG: key last4:", (BRIDGEOS_API_KEY or "")[-4:])

        response = requests.post(
            url,
            json={
                "user_input": user_input,
                "history": [],
                "backend_context": {
                    "source": "bridgeos_backend",
                    "has_document_context": bool(context and context.strip())
                }
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        print("RUNPOD LLM DEBUG: status:", response.status_code)
        print("RUNPOD LLM DEBUG: response:", response.text[:1000])

        if response.status_code >= 400:
            return FALLBACK_NO_DATA_ANSWER

        data = response.json()

        answer = (
            data.get("response")
            or data.get("answer")
            or data.get("message")
            or data.get("output")
            or ""
        )

        answer = str(answer or "").strip()

        if not answer:
            print("RUNPOD LLM ERROR: empty answer from response json:", data)
            return FALLBACK_NO_DATA_ANSWER

        return answer

    except requests.exceptions.Timeout:
        print("RUNPOD LLM TIMEOUT")
        return FALLBACK_NO_DATA_ANSWER

    except Exception as e:
        print("RUNPOD LLM REQUEST ERROR:", type(e).__name__, str(e))
        return FALLBACK_NO_DATA_ANSWER