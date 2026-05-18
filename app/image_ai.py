import base64
from openai import OpenAI

from app.config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)


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
    data_url = _file_to_data_url(file, filename)

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": """
Describe this image for a private yacht memory/search system.

Include:
- visible objects
- setting/location on the yacht if inferable
- decorations
- event clues
- visible dates or text
- season/holiday clues
- colors and arrangement
- anything useful for future search

Do not invent exact dates unless visible or provided.
"""
                    },
                    {
                        "type": "input_image",
                        "image_url": data_url
                    }
                ]
            }
        ]
    )

    return response.output_text or ""


def extract_ocr_from_image(file, filename: str) -> str:
    data_url = _file_to_data_url(file, filename)

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": """
Extract any visible text from this image.

Return only the visible text.
If there is no visible text, return an empty string.
"""
                    },
                    {
                        "type": "input_image",
                        "image_url": data_url
                    }
                ]
            }
        ]
    )

    return response.output_text or ""
