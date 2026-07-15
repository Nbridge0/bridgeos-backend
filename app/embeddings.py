import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


EMBEDDING_DIMENSIONS = 1536


def _zero_embedding():
    return [0.0] * EMBEDDING_DIMENSIONS


def embed(text: str):
    """
    Uses your own RunPod embedding endpoint.

    IMPORTANT:
    Your RunPod currently returns 404 for /api/bridgeos/embed.
    Until that endpoint exists, document semantic search cannot work correctly.
    """

    if not text:
        text = ""

    if not RUNPOD_BASE_URL:
        print("EMBEDDING ERROR: RUNPOD_BASE_URL missing")
        return _zero_embedding()

    if not BRIDGEOS_API_KEY:
        print("EMBEDDING ERROR: BRIDGEOS_API_KEY missing")
        return _zero_embedding()

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/embed"

    try:
        response = requests.post(
            url,
            json={"input": text},
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=120
        )

        print("EMBEDDING DEBUG: url:", url)
        print("EMBEDDING DEBUG: status:", response.status_code)
        print("EMBEDDING DEBUG: response:", response.text[:500])

        if response.status_code >= 400:
            return _zero_embedding()

        data = response.json()

        embedding = (
            data.get("embedding")
            or data.get("vector")
            or data.get("data", {}).get("embedding")
            or []
        )

        if not isinstance(embedding, list):
            print("EMBEDDING ERROR: embedding is not a list")
            return _zero_embedding()

        if len(embedding) != EMBEDDING_DIMENSIONS:
            print("EMBEDDING ERROR: wrong dimension:", len(embedding))
            return _zero_embedding()

        return embedding

    except Exception as e:
        print("EMBEDDING REQUEST ERROR:", type(e).__name__, str(e))
        return _zero_embedding()