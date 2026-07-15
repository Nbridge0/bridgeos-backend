import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


EMBEDDING_DIMENSIONS = 1536


class EmbeddingError(RuntimeError):
    pass


def embed(text: str) -> list[float]:
    clean_text = str(text or "").strip()

    if not clean_text:
        raise EmbeddingError("Cannot embed empty text")

    if not RUNPOD_BASE_URL:
        raise EmbeddingError("RUNPOD_BASE_URL is missing")

    if not BRIDGEOS_API_KEY:
        raise EmbeddingError("BRIDGEOS_API_KEY is missing")

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/embed"

    try:
        response = requests.post(
            url,
            json={"input": clean_text},
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=120
        )
    except requests.RequestException as exc:
        raise EmbeddingError(
            f"Embedding request failed: {type(exc).__name__}: {exc}"
        ) from exc

    if response.status_code >= 400:
        raise EmbeddingError(
            f"Embedding endpoint returned {response.status_code}: "
            f"{response.text[:500]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise EmbeddingError(
            "Embedding endpoint returned invalid JSON"
        ) from exc

    embedding = (
        data.get("embedding")
        or data.get("vector")
        or (data.get("data") or {}).get("embedding")
    )

    if not isinstance(embedding, list):
        raise EmbeddingError(
            "Embedding response does not contain a vector list"
        )

    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise EmbeddingError(
            f"Expected {EMBEDDING_DIMENSIONS} dimensions, "
            f"got {len(embedding)}"
        )

    try:
        embedding = [float(value) for value in embedding]
    except (TypeError, ValueError) as exc:
        raise EmbeddingError(
            "Embedding contains non-numeric values"
        ) from exc

    if not any(abs(value) > 1e-12 for value in embedding):
        raise EmbeddingError(
            "Embedding endpoint returned a zero vector"
        )

    return embedding