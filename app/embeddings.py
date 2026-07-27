from openai import OpenAI

from app.config import (
    OPENAI_API_KEY,
    OPENAI_EMBEDDING_MODEL,
    OPENAI_EMBEDDING_DIMENSIONS
)


EMBEDDING_DIMENSIONS = OPENAI_EMBEDDING_DIMENSIONS

_openai_embedding_client = None


def get_openai_embedding_client() -> OpenAI:
    """
    Returns one reusable OpenAI client for embeddings.
    """

    global _openai_embedding_client

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is missing from the backend environment."
        )

    if _openai_embedding_client is None:
        _openai_embedding_client = OpenAI(
            api_key=OPENAI_API_KEY,
            timeout=120.0,
            max_retries=2
        )

    return _openai_embedding_client


def is_zero_embedding(
    vector: list[float] | None
) -> bool:
    if not isinstance(vector, list):
        return True

    if len(vector) != EMBEDDING_DIMENSIONS:
        return True

    return not any(
        abs(float(value or 0)) > 0.0000001
        for value in vector
    )


def embed(
    text: str,
    required: bool = False
) -> list[float] | None:
    """
    Creates an OpenAI embedding.

    The returned vector dimension is forced to match the existing
    Supabase vector column dimension.

    There is no RunPod fallback.
    """

    clean_text = str(text or "").strip()

    if not clean_text:
        if required:
            raise RuntimeError(
                "Cannot create an embedding for empty text."
            )

        return None

    try:
        client = get_openai_embedding_client()

        response = client.embeddings.create(
            model=OPENAI_EMBEDDING_MODEL,
            input=clean_text,
            dimensions=EMBEDDING_DIMENSIONS,
            encoding_format="float"
        )

        if not response.data:
            raise RuntimeError(
                "OpenAI returned no embedding data."
            )

        vector = response.data[0].embedding

        if not isinstance(vector, list):
            vector = list(vector)

        vector = [
            float(value)
            for value in vector
        ]

        if len(vector) != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                "OpenAI returned the wrong embedding dimension. "
                f"Expected {EMBEDDING_DIMENSIONS}, "
                f"received {len(vector)}."
            )

        if is_zero_embedding(vector):
            raise RuntimeError(
                "OpenAI returned an all-zero embedding."
            )

        print(
            "OPENAI EMBEDDING SUCCESS:",
            {
                "provider": "openai",
                "model": OPENAI_EMBEDDING_MODEL,
                "dimensions": len(vector),
                "input_characters": len(clean_text)
            }
        )

        return vector

    except Exception as error:
        message = (
            "OpenAI embedding request failed: "
            f"{type(error).__name__}: {error}"
        )

        print(
            "OPENAI EMBEDDING ERROR:",
            message
        )

        if required:
            raise RuntimeError(message) from error

        return None