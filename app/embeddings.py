import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


# IMPORTANT:
# Set this to the exact dimension used by the vector column in Supabase.
# Examples:
# vector(384)  -> 384
# vector(768)  -> 768
# vector(1024) -> 1024
# vector(1536) -> 1536
EMBEDDING_DIMENSIONS = 1024


def _zero_embedding() -> list[float]:
    return [0.0] * EMBEDDING_DIMENSIONS


def is_zero_embedding(vector: list[float] | None) -> bool:
    if not isinstance(vector, list):
        return True

    if len(vector) != EMBEDDING_DIMENSIONS:
        return True

    return not any(abs(float(value or 0)) > 0.0000001 for value in vector)


def embed(text: str, required: bool = False) -> list[float] | None:
    clean_text = str(text or "").strip()

    if not clean_text:
        if required:
            raise RuntimeError("Cannot create an embedding for empty text.")
        return None

    if not RUNPOD_BASE_URL:
        if required:
            raise RuntimeError("RUNPOD_BASE_URL is missing.")
        return None

    if not BRIDGEOS_API_KEY:
        if required:
            raise RuntimeError("BRIDGEOS_API_KEY is missing.")
        return None

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/embed"

    try:
        response = requests.post(
            url,
            json={
                "input": clean_text,
                "text": clean_text
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=120
        )

        print("EMBEDDING DEBUG:", {
            "url": url,
            "status": response.status_code,
            "response_preview": response.text[:500]
        })

        if response.status_code >= 400:
            message = (
                f"Embedding endpoint returned {response.status_code}: "
                f"{response.text[:500]}"
            )

            if required:
                raise RuntimeError(message)

            print("EMBEDDING WARNING:", message)
            return None

        data = response.json()

        embedding = (
            data.get("embedding")
            or data.get("vector")
            or (data.get("data") or {}).get("embedding")
            or (data.get("result") or {}).get("embedding")
        )

        if not isinstance(embedding, list):
            if required:
                raise RuntimeError(
                    f"Embedding response did not contain a list: {data}"
                )

            return None

        try:
            embedding = [float(value) for value in embedding]
        except Exception as error:
            if required:
                raise RuntimeError(
                    f"Embedding contains invalid values: {error}"
                )

            return None

        if len(embedding) != EMBEDDING_DIMENSIONS:
            message = (
                f"Wrong embedding dimension. Expected "
                f"{EMBEDDING_DIMENSIONS}, received {len(embedding)}."
            )

            if required:
                raise RuntimeError(message)

            print("EMBEDDING WARNING:", message)
            return None

        if is_zero_embedding(embedding):
            if required:
                raise RuntimeError("Embedding endpoint returned an all-zero vector.")

            return None

        return embedding

    except RuntimeError:
        raise

    except Exception as error:
        message = (
            f"Embedding request failed: "
            f"{type(error).__name__}: {error}"
        )

        if required:
            raise RuntimeError(message)

        print("EMBEDDING REQUEST ERROR:", message)
        return None