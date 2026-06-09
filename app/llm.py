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
You are BridgeOS, a helpful yacht assistant.

Always respond in British English.

When context is provided:
- Use it only if it is relevant to the user's question.
- If it is not relevant, answer normally.

When no context is provided:
- Answer normally and helpfully.

Never claim a document was used unless the answer is actually based on the document context.
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
