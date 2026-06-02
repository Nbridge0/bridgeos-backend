from openai import OpenAI

from app.config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)


FALLBACK_NO_DATA_ANSWER = (
    "Sorry, I don't have this data yet. Please ask your admin to upload it."
)


def classify_query_route(query: str) -> str:
    """
    Returns:
    - "database" when the user is asking about uploaded yacht/company documents,
      files, logs, records, images, manuals, safety docs, maintenance data, crew data,
      or anything that should be answered only from Supabase context.
    - "general" when the user is asking a normal/general AI question.

    This does not answer the user. It only routes the question.
    """

    clean_query = (query or "").strip()

    if not clean_query:
        return "general"

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {
                    "role": "system",
                    "content": """
You are a strict router for a yacht document chatbot.

Return exactly one word:
database
or
general

Return database if the user asks about:
- uploaded files or documents
- yacht manuals, yacht procedures, yacht specs, yacht records
- safety protocols, maintenance logs, audit reports, inspection reports
- crew documents, operations, onboard information
- images/files uploaded into the system
- anything phrased like "my yacht", "our yacht", "this vessel", "the document", "the file", "the manual", "the report", "what do we have"

Return general if the user asks:
- normal knowledge questions
- explanations
- writing help
- translation
- coding help
- math
- greetings or casual chat
- anything that does not require uploaded database content

Do not explain. Return only database or general.
"""
                },
                {
                    "role": "user",
                    "content": clean_query
                }
            ]
        )

        route = (response.output_text or "").strip().lower()

        if route == "database":
            return "database"

        if route == "general":
            return "general"

        return "general"

    except Exception as e:
        print("QUERY ROUTER ERROR:", type(e).__name__, str(e))

        # Safe default: general questions should not crash.
        return "general"


def ask_general_llm(query: str) -> str:
    """
    Answers normal non-database questions.
    This should never return document sources.
    """

    clean_query = (query or "").strip()

    if not clean_query:
        return "How can I help?"

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {
                    "role": "system",
                    "content": """
You are BridgeOS, a helpful assistant.

For normal/general questions, answer naturally and helpfully.

Do not claim you checked yacht documents.
Do not mention uploaded files.
Do not cite sources.
Do not invent private yacht data.
If the user asks specifically about uploaded documents, yacht files, logs, manuals, records, or operations data, say:
"I can check that in the yacht documents if it has been uploaded."
"""
                },
                {
                    "role": "user",
                    "content": clean_query
                }
            ]
        )

        return response.output_text or "I could not generate an answer."

    except Exception as e:
        print("GENERAL LLM ERROR:", type(e).__name__, str(e))
        return "I could not generate an answer right now. Please try again."


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

    try:
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

    except Exception as e:
        print("DATABASE LLM ERROR:", type(e).__name__, str(e))
        return FALLBACK_NO_DATA_ANSWER