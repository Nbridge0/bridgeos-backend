from openai import OpenAI

from app.config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)


def embed(text: str):
    """
    Creates a real embedding for semantic search.
    Uses text-embedding-3-small, which returns 1536 dimensions.
    This matches vector(1536) in Supabase.
    """

    if not text:
        text = ""

    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )

    return response.data[0].embedding
