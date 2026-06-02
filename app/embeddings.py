import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


EMBEDDING_DIMENSIONS = 1536


def _zero_embedding():
    """
    Safe fallback so the backend does not crash.
    Must match Supabase vector(1536).
    """
    return [0.0] * EMBEDDING_DIMENSIONS


def embed(text: str):
    """
    Creates an embedding using your own RunPod / BridgeOS embedding service.

    No OpenAI.
    No OpenAI API key.
    No text-embedding-3-small.

    IMPORTANT:
    The returned vector must be 1536 dimensions because your Supabase
    asset_chunks.embedding column is vector(1536).
    """

    if not text:
        text = ""

    if not RUNPOD_BASE_URL:
        print("EMBEDDING ERROR: RUNPOD_BASE_URL is missing")
        return _zero_embedding()

    if not BRIDGEOS_API_KEY:
        print("EMBEDDING ERROR: BRIDGEOS_API_KEY is missing")
        return _zero_embedding()

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/embed"

    try:
        response = requests.post(
            url,
            json={
                "input": text
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=120
        )

        if response.status_code >= 400:
            print("EMBEDDING ERROR STATUS:", response.status_code)
            print("EMBEDDING ERROR RESPONSE:", response.text[:1000])
            return _zero_embedding()

        data = response.json()

        embedding = (
            data.get("embedding")
            or data.get("data", {}).get("embedding")
            or data.get("vector")
            or []
        )

        if not isinstance(embedding, list):
            print("EMBEDDING ERROR: embedding is not a list")
            return _zero_embedding()

        if len(embedding) != EMBEDDING_DIMENSIONS:
            print(
                "EMBEDDING ERROR: wrong dimensions:",
                len(embedding),
                "expected:",
                EMBEDDING_DIMENSIONS
            )
            return _zero_embedding()

        return embedding

    except requests.exceptions.Timeout:
        print("EMBEDDING ERROR: RunPod embedding timeout")
        return _zero_embedding()

    except Exception as e:
        print("EMBEDDING REQUEST ERROR:", type(e).__name__, str(e))
        return _zero_embedding()