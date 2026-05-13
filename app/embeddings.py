import requests

from app.config import EMBEDDING_API_URL, EMBEDDING_API_KEY


def embed(text: str):
    """
    Temporary behavior:
    - If EMBEDDING_API_URL and EMBEDDING_API_KEY exist, call your embedding API.
    - Otherwise return a fake 1536-dimension vector.

    IMPORTANT:
    The fake vector is only for development.
    Real semantic search will NOT work properly until you connect a real embedding API.
    """

    if not text:
        text = ""

    if EMBEDDING_API_URL and EMBEDDING_API_KEY:
        response = requests.post(
            EMBEDDING_API_URL,
            json={"input": text},
            headers={
                "Authorization": f"Bearer {EMBEDDING_API_KEY}",
                "Content-Type": "application/json"
            },
            timeout=30
        )

        response.raise_for_status()
        data = response.json()

        if "embedding" in data:
            return data["embedding"]

        if "data" in data and len(data["data"]) > 0 and "embedding" in data["data"][0]:
            return data["data"][0]["embedding"]

        raise ValueError("Embedding API response does not contain an embedding")

    return [0.0] * 1536