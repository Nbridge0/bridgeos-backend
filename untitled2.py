import requests

from app.config import VISION_API_URL, VISION_API_KEY, OCR_API_URL, OCR_API_KEY


def describe_image(file, filename: str) -> str:
    """
    Sends image to a vision model and returns a searchable visual description.

    Until VISION_API_URL and VISION_API_KEY are configured, returns empty string.
    """

    if not VISION_API_URL or not VISION_API_KEY:
        return ""

    file.seek(0)

    files = {
        "file": (filename, file)
    }

    data = {
        "prompt": """
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
    }

    response = requests.post(
        VISION_API_URL,
        headers={
            "Authorization": f"Bearer {VISION_API_KEY}"
        },
        files=files,
        data=data,
        timeout=60
    )

    response.raise_for_status()
    result = response.json()

    return result.get("description", "")


def extract_ocr_from_image(file, filename: str) -> str:
    """
    Extracts visible text from an image.

    Until OCR_API_URL and OCR_API_KEY are configured, returns empty string.
    """

    if not OCR_API_URL or not OCR_API_KEY:
        return ""

    file.seek(0)

    files = {
        "file": (filename, file)
    }

    response = requests.post(
        OCR_API_URL,
        headers={
            "Authorization": f"Bearer {OCR_API_KEY}"
        },
        files=files,
        timeout=60
    )

    response.raise_for_status()
    result = response.json()

    return result.get("text", "")