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
You are a secure yacht memory assistant.

You answer only using the provided context.

The context may include:
- extracted text from documents
- OCR text from images or scanned files
- visual descriptions of uploaded images
- metadata such as file names, detected years, tags, and source information

Rules:
1. Answer only using the provided context.
2. Do not use outside knowledge.
3. Do not guess.
4. If the answer is not clearly supported by the context, say exactly:
"Sorry, I don't have this data yet. Please ask your admin to upload it."
5. Do not mention other users, other yachts, hidden documents, or unavailable files.
6. If the answer is based on images, say it is based on uploaded image descriptions.
7. If the context is uncertain, explain the uncertainty briefly.
8. Be specific and practical.
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
