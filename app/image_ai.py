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
    elif ext == "gif":
        mime_type = "image/gif"

    encoded = base64.b64encode(data).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def describe_image(file, filename: str) -> str:
    """
    Creates a visual description of an uploaded image/photo.
    This is stored in assets.visual_description and asset_chunks.
    """

    try:
        image_url = _file_to_data_url(file, filename)

        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Describe this yacht-related uploaded photo clearly. "
                                "Mention visible objects, people if any, location/setting, "
                                "condition, labels, signs, documents, equipment, dates, "
                                "and anything useful for later search. "
                                "Do not invent anything not visible."
                            )
                        },
                        {
                            "type": "input_image",
                            "image_url": image_url
                        }
                    ]
                }
            ]
        )

        return (response.output_text or "").strip()

    except Exception as e:
        print("IMAGE DESCRIPTION ERROR:", type(e).__name__, str(e))
        return ""


def extract_ocr_from_image(file, filename: str) -> str:
    """
    Extracts readable text from an uploaded image/photo.
    This is stored in assets.ocr_text and asset_chunks.
    """

    try:
        image_url = _file_to_data_url(file, filename)

        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Extract all readable text from this image. "
                                "Return only the text you can actually read. "
                                "If there is no readable text, return an empty string."
                            )
                        },
                        {
                            "type": "input_image",
                            "image_url": image_url
                        }
                    ]
                }
            ]
        )

        text = (response.output_text or "").strip()

        if text.lower() in [
            "empty string",
            "no readable text",
            "there is no readable text",
            "none"
        ]:
            return ""

        return text

    except Exception as e:
        print("IMAGE OCR ERROR:", type(e).__name__, str(e))
        return ""