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

def _bytes_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def _ask_vision_with_base64(image_base64: str, filename: str, prompt: str) -> str:
    if not RUNPOD_BASE_URL or not BRIDGEOS_API_KEY:
        print("VISION ERROR: RunPod config missing")
        return ""

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    try:
        response = requests.post(
            url,
            json={
                "user_input": prompt,
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

        print("RUNPOD PDF OCR DEBUG: status:", response.status_code)
        print("RUNPOD PDF OCR DEBUG: response:", response.text[:500])

        if response.status_code >= 400:
            return ""

        data = response.json()

        return (
            data.get("response")
            or data.get("answer")
            or ""
        ).strip()

    except Exception as e:
        print("PDF OCR RUNPOD ERROR:", type(e).__name__, str(e))
        return ""


def extract_ocr_from_pdf_pages(file, filename: str, max_pages: int = 5) -> str:
    """
    OCR fallback for scanned PDFs.

    pypdf can read real text PDFs, but scanned invoices are usually page images.
    This renders PDF pages to images and sends each page to the existing RunPod
    vision/chat endpoint.
    """

    try:
        import fitz  # PyMuPDF
    except Exception as e:
        print("PDF OCR ERROR: pymupdf missing:", type(e).__name__, str(e))
        return ""

    try:
        file.seek(0)
        pdf_bytes = file.read()
        file.seek(0)

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        texts = []

        page_count = min(len(doc), max_pages)

        for page_index in range(page_count):
            page = doc.load_page(page_index)

            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            png_bytes = pix.tobytes("png")
            image_base64 = _bytes_to_base64(png_bytes)

            page_text = _ask_vision_with_base64(
                image_base64=image_base64,
                filename=f"{filename} page {page_index + 1}",
                prompt=(
                    "Extract all readable text from this invoice/document page. "
                    "Preserve invoice numbers, dates, vendor names, item descriptions, "
                    "quantities, unit prices, tax, subtotal, total, currency, and payment terms. "
                    "Return only text actually visible on the page. Do not invent values."
                )
            )

            page_text = str(page_text or "").strip()

            if page_text:
                texts.append(f"PDF page {page_index + 1} OCR text:\n{page_text}")

        return "\n\n".join(texts).strip()

    except Exception as e:
        print("PDF OCR ERROR:", type(e).__name__, str(e))
        return ""