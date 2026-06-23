import base64
import mimetypes
import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


def _file_to_base64(file) -> str:
    file.seek(0)
    data = file.read()
    file.seek(0)

    return base64.b64encode(data).decode("utf-8")


def describe_image(file, filename: str) -> str:
    if not RUNPOD_BASE_URL or not BRIDGEOS_API_KEY:
        print("IMAGE DESCRIPTION ERROR: RunPod config missing")
        return ""

    image_base64 = _file_to_base64(file)

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    try:
        response = requests.post(
            url,
            json={
                "user_input": (
                    "Describe this uploaded image clearly. Mention visible objects, "
                    "people if any, location or setting, condition, labels, signs, "
                    "documents, equipment, dates, and anything useful for later search. "
                    "Do not invent anything not visible."
                ),
                "image": image_base64,
                "history": [],
                "backend_context": {
                    "filename": filename
                }
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        print("RUNPOD IMAGE DESCRIPTION DEBUG: status:", response.status_code)
        print("RUNPOD IMAGE DESCRIPTION DEBUG: response:", response.text[:500])

        if response.status_code >= 400:
            return ""

        data = response.json()

        return (
            data.get("response")
            or data.get("answer")
            or ""
        ).strip()

    except Exception as e:
        print("IMAGE DESCRIPTION RUNPOD ERROR:", type(e).__name__, str(e))
        return ""


def extract_ocr_from_image(file, filename: str) -> str:
    if not RUNPOD_BASE_URL or not BRIDGEOS_API_KEY:
        print("IMAGE OCR ERROR: RunPod config missing")
        return ""

    image_base64 = _file_to_base64(file)

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    try:
        response = requests.post(
            url,
            json={
                "user_input": (
                    "Extract all readable text from this image. "
                    "Return only the text you can actually read. "
                    "If there is no readable text, return an empty string."
                ),
                "image": image_base64,
                "history": [],
                "backend_context": {
                    "filename": filename
                }
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        print("RUNPOD IMAGE OCR DEBUG: status:", response.status_code)
        print("RUNPOD IMAGE OCR DEBUG: response:", response.text[:500])

        if response.status_code >= 400:
            return ""

        data = response.json()

        text = (
            data.get("response")
            or data.get("answer")
            or ""
        ).strip()

        if text.lower() in [
            "empty string",
            "no readable text",
            "there is no readable text",
            "none",
            "n/a"
        ]:
            return ""

        return text

    except Exception as e:
        print("IMAGE OCR RUNPOD ERROR:", type(e).__name__, str(e))
        return ""