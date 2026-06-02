import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I could not generate a response right now. Please try again."
)


def ask_llm(query: str, context: str = "") -> str:
    """
    Calls your own BridgeOS / RunPod LLM.

    No OpenAI.
    No OpenAI client.
    No OpenAI API key.

    The caller can pass:
    - query only for normal chat
    - query + context for document-aware chat
    """

    if not RUNPOD_BASE_URL:
        return FALLBACK_NO_DATA_ANSWER

    if not BRIDGEOS_API_KEY:
        return FALLBACK_NO_DATA_ANSWER

    system_prompt = """
You are BridgeOS, a helpful yacht assistant.

When uploaded document context is provided:
- Use it only if it is relevant to the user's question.
- If it directly answers the question, use it as the priority source.
- If it is not relevant, answer normally.

When no context is provided:
- Answer normally and helpfully.

Never claim a document was used unless the answer is actually based on the provided document context.
""".strip()

    user_prompt = f"""
Question:
{query}

Context:
{context or ""}
""".strip()

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    try:
        response = requests.post(
            url,
            json={
                "user_input": user_prompt,
                "history": [
                    {
                        "role": "system",
                        "content": system_prompt
                    }
                ],
                "backend_context": {
                    "source": "bridgeos_backend",
                    "has_context": bool(context and context.strip())
                }
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        if response.status_code >= 400:
            print("RUNPOD LLM ERROR STATUS:", response.status_code)
            print("RUNPOD LLM ERROR RESPONSE:", response.text[:1000])
            return FALLBACK_NO_DATA_ANSWER

        data = response.json()

        answer = (
            data.get("response")
            or data.get("answer")
            or data.get("message")
            or data.get("output")
            or ""
        )

        answer = str(answer).strip()

        if not answer:
            return FALLBACK_NO_DATA_ANSWER

        return answer

    except requests.exceptions.Timeout:
        print("RUNPOD LLM TIMEOUT")
        return FALLBACK_NO_DATA_ANSWER

    except Exception as e:
        print("RUNPOD LLM REQUEST ERROR:", type(e).__name__, str(e))
        return FALLBACK_NO_DATA_ANSWER
