import requests

from app.config import EMBEDDING_API_URL, EMBEDDING_API_KEY


def embed(text: str):
    """
    Creates a real embedding for semantic search.

    Production rule:
    Do NOT use fake embeddings. Fake embeddings make search useless.
    """

    if not text:
        text = ""

    if not EMBEDDING_API_URL or not EMBEDDING_API_KEY:
        raise RuntimeError(
            "Embedding API is not configured. Real embeddings are required for chatbot search."
        )

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