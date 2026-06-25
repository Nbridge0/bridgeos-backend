import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I don't have this data yet. Please ask your admin to upload it."
)


def ask_llm(query: str, context: str) -> str:
    if not RUNPOD_BASE_URL:
        print("RUNPOD LLM ERROR: RUNPOD_BASE_URL missing")
        return "RunPod config error: RUNPOD_BASE_URL missing"

    if not BRIDGEOS_API_KEY:
        print("RUNPOD LLM ERROR: BRIDGEOS_API_KEY missing")
        return "RunPod config error: BRIDGEOS_API_KEY missing"

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
                "prompt": prompt,
                "max_tokens": 2500,
                "max_new_tokens": 2500,
                "max_output_tokens": 2500,
                "temperature": 0.1
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        print("RUNPOD GENERATE DEBUG: url:", url)
        print("RUNPOD GENERATE DEBUG: status:", response.status_code)
        print("RUNPOD GENERATE DEBUG: response:", response.text[:1000])

        if response.status_code >= 400:
            return f"RunPod generate error {response.status_code}: {response.text[:500]}"

        data = response.json()

        answer = (
            data.get("response")
            or data.get("answer")
            or data.get("message")
            or ""
        )

        answer = str(answer or "").strip()

        if not answer:
            return "RunPod returned an empty response."

        return answer

    except Exception as e:
        print("RUNPOD GENERATE REQUEST ERROR:", type(e).__name__, str(e))
        return f"RunPod request error: {type(e).__name__}: {str(e)}"