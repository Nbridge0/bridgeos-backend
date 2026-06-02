from openai import OpenAI

from app.config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I don't have this data yet. Please ask your admin to upload it."
)


def ask_llm(query: str, context: str) -> str:
    if not context or not context.strip():
        return FALLBACK_NO_DATA_ANSWER

    system_prompt = """
You are BridgeOS, a helpful assistant.

You can answer normal questions using general knowledge.

When DATABASE CONTEXT is provided and it directly answers the user's question:
- Prefer the database context.
- Use it as the source of truth.
- End with a document reference.

When DATABASE CONTEXT is empty or irrelevant:
- Answer normally.
- Do not pretend the answer came from a document.
- Do not say you cannot answer just because no document was found.
"""
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": f"""
Question:
{query}

Context:
{context}
"""
            }
        ]
    )

    return response.output_text or FALLBACK_NO_DATA_ANSWER
