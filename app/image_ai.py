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

def _file_to_data_url(file, filename: str) -> str:
    file.seek(0)
    data = file.read()
    file.seek(0)

    ext = filename.lower().split(".")[-1]

    mime_type = "image/jpeg"

    if ext == "png":
        mime_type = "image/png"
    elif ext == "webp":
        mime_type = "image/webp"
    elif ext in ["jpg", "jpeg"]:
        mime_type = "image/jpeg"

    encoded = base64.b64encode(data).decode("utf-8")

    return f"data:{mime_type};base64,{encoded}"


def describe_image(file, filename: str) -> str:
    """
    No-OpenAI image description placeholder.

    This keeps uploads working without OpenAI.
    If you later add image understanding to your own LLM/RunPod,
    replace this function with a RunPod vision call.
    """
    return ""


def extract_ocr_from_image(file, filename: str) -> str:
    """
    No-OpenAI OCR placeholder.

    This keeps image uploads working without OpenAI.
    If you later add OCR to your own LLM/RunPod,
    replace this function with a RunPod OCR call.
    """
    return ""