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

You answer only using the provided context when the user is asking about yacht data, documents, files, records, logs, audits, procedures, maintenance, safety, crew, or operations.

The context may include:
- extracted text from documents
- OCR text from images or scanned files
- visual descriptions of uploaded images
- metadata such as file names, detected years, tags, and source information

Rules:
1. If the user's question is conversational, a greeting, thanks, or not asking about the provided context, answer naturally and do not use the context.
2. For yacht/document/operations questions, answer only using the provided context.
3. Do not use outside knowledge for yacht/document/operations questions.
4. Do not guess.
5. If a yacht/document/operations answer is not clearly supported by the context, say exactly:
"Sorry, I don't have this data yet. Please ask your admin to upload it."
6. Do not mention other users, other yachts, hidden documents, or unavailable files.
7. If the answer is based on images, say it is based on uploaded image descriptions.
8. If the context is uncertain, explain the uncertainty briefly.
9. Be specific and practical.
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
