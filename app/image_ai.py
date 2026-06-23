import base64
import mimetypes
import os
from typing import Any, Dict, Optional

import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


VISION_TIMEOUT_SECONDS = int(os.getenv("VISION_TIMEOUT_SECONDS", "180"))
PDF_OCR_MAX_PAGES = int(os.getenv("PDF_OCR_MAX_PAGES", "12"))
PDF_RENDER_ZOOM = float(os.getenv("PDF_RENDER_ZOOM", "2.0"))


def _read_file_bytes(file) -> bytes:
    """
    Read bytes from an UploadFile/file-like object without permanently breaking the pointer.
    """
    if file is None:
        return b""

    try:
        current_position = file.tell()
    except Exception:
        current_position = None

    try:
        file.seek(0)
    except Exception:
        pass

    data = file.read()

    if isinstance(data, str):
        data = data.encode("utf-8", errors="ignore")

    if current_position is not None:
        try:
            file.seek(current_position)
        except Exception:
            pass
    else:
        try:
            file.seek(0)
        except Exception:
            pass

    return data or b""


def _bytes_to_base64(data: bytes) -> str:
    return base64.b64encode(data or b"").decode("utf-8")


def _guess_mime_type(filename: str, fallback: str = "application/octet-stream") -> str:
    mime_type, _ = mimetypes.guess_type(filename or "")
    return mime_type or fallback


def _extract_response_text(data: Any) -> str:
    """
    Extract text from different possible RunPod response shapes.
    """
    if data is None:
        return ""

    if isinstance(data, str):
        return data.strip()

    if isinstance(data, dict):
        for key in [
            "response",
            "answer",
            "message",
            "text",
            "output",
            "result",
            "content",
        ]:
            value = data.get(key)

            if isinstance(value, str) and value.strip():
                return value.strip()

            if isinstance(value, dict):
                nested = _extract_response_text(value)
                if nested:
                    return nested

            if isinstance(value, list):
                nested = _extract_response_text(value)
                if nested:
                    return nested

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0]

            if isinstance(first_choice, dict):
                message = first_choice.get("message")

                if isinstance(message, dict):
                    content = message.get("content")

                    if isinstance(content, str) and content.strip():
                        return content.strip()

                text = first_choice.get("text")

                if isinstance(text, str) and text.strip():
                    return text.strip()

        nested_data = data.get("data")

        if nested_data is not None:
            nested_text = _extract_response_text(nested_data)

            if nested_text:
                return nested_text

    if isinstance(data, list):
        for item in data:
            text = _extract_response_text(item)

            if text:
                return text

    return ""


def _call_vision_model(
    *,
    image_bytes: bytes,
    filename: str,
    prompt: str,
    mime_type: Optional[str] = None,
    timeout: int = VISION_TIMEOUT_SECONDS,
) -> str:
    """
    Calls the existing RunPod BridgeOS chat endpoint with image data.

    Multiple image fields are sent for compatibility with different worker schemas.
    This is not content hard-coding.
    """
    if not RUNPOD_BASE_URL or not BRIDGEOS_API_KEY:
        print("VISION ERROR: RUNPOD_BASE_URL or BRIDGEOS_API_KEY is missing")
        return ""

    if not image_bytes:
        print("VISION ERROR: empty image bytes")
        return ""

    clean_filename = filename or "uploaded_image"
    clean_mime_type = mime_type or _guess_mime_type(clean_filename, "image/png")
    image_base64 = _bytes_to_base64(image_bytes)

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    payload: Dict[str, Any] = {
        "user_input": prompt,
        "history": [],
        "temperature": 0,
        "max_tokens": 1800,

        # Original field used by your backend
        "image": image_base64,

        # Compatibility fields
        "image_base64": image_base64,
        "mime_type": clean_mime_type,
        "image_mime_type": clean_mime_type,
        "images": [
            {
                "filename": clean_filename,
                "mime_type": clean_mime_type,
                "data": image_base64,
            }
        ],

        "backend_context": {
            "filename": clean_filename,
            "mime_type": clean_mime_type,
            "task": "careful_image_analysis",
        },
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY,
            },
            timeout=timeout,
        )

        print("VISION DEBUG status:", response.status_code)
        print("VISION DEBUG body:", response.text[:1000])

        if response.status_code >= 400:
            return ""

        try:
            data = response.json()
        except Exception:
            return response.text.strip()

        return _extract_response_text(data)

    except Exception as e:
        print("VISION ERROR:", type(e).__name__, str(e))
        return ""


def _normalize_no_readable_text(text: str) -> str:
    clean = str(text or "").strip()

    if not clean:
        return "NO_READABLE_TEXT"

    upper_clean = clean.upper().strip()

    if upper_clean in {
        "NO_READABLE_TEXT",
        "NO READABLE TEXT",
        "NO_TEXT",
        "NONE",
        "N/A",
        "NULL",
    }:
        return "NO_READABLE_TEXT"

    return clean


def _looks_like_visual_description_not_ocr(text: str) -> bool:
    """
    OCR should be written text only.
    If OCR returns a scene description, reject it.
    """
    clean = str(text or "").strip()
    lower = clean.lower()

    if not clean:
        return True

    descriptive_starts = [
        "the image shows",
        "this image shows",
        "the photo shows",
        "this photo shows",
        "the picture shows",
        "this picture shows",
        "i can see",
        "there is",
        "there are",
        "it appears",
        "appears to be",
        "the uploaded file appears",
    ]

    if any(lower.startswith(phrase) for phrase in descriptive_starts):
        return True

    descriptive_phrases = [
        "in the background",
        "in the foreground",
        "the scene",
        "the setting",
        "sunny day",
        "clear sky",
        "calm water",
        "people are",
        "people appear",
        "floating on",
        "docked at",
    ]

    sentence_count = clean.count(".") + clean.count("!") + clean.count("?")

    if sentence_count >= 2 and any(phrase in lower for phrase in descriptive_phrases):
        return True

    return False


def describe_image(file, filename: str) -> str:
    """
    Visual analysis for normal images.

    This stores enough structured visual context so follow-up questions like
    "what kind of boat is it?" can be answered without re-reading the image.
    """
    image_bytes = _read_file_bytes(file)
    mime_type = _guess_mime_type(filename, "image/png")

    prompt = """
You are a careful visual analyst.

Analyze the uploaded image using only visible evidence.

Your job is to create structured visual context for a later chatbot answer.

Rules:
- Do not invent brands, logos, vessel names, locations, people identities, text, numbers, prices, invoice values, or hidden details.
- Do not guess unreadable text.
- Do not say a marina, city, country, brand, system, or company name unless it is clearly visible.
- You may classify visible object types when the visual evidence supports it.
- For uncertain classifications, use cautious wording like "likely", "appears to be", or "cannot determine exactly".
- If the image shows a boat, identify the broad vessel category from visible structure when possible, such as monohull sailboat, catamaran, motor yacht, tender, dinghy, RIB, centre-console, or other visible category.
- If the exact make/model is not visible, say the exact make/model is not visible.
- If the image shows a financial document, extract visible fields and numbers carefully.
- If the image is not a financial document, do not say calculations are possible.
- Do not mention OCR unless there is clearly readable written text.

Return exactly this structure:

Primary subject:
<main visible subject>

Likely category/type:
<best visual classification, with confidence wording>

Visible evidence for category/type:
<short evidence from visible features only>

Visual description:
<concise description of visible scene>

Clearly readable text:
<only text clearly readable in the image, or "No clearly readable text">

Numbers / financial values:
<only visible numbers or financial values, or "No relevant numbers visible">

What cannot be determined:
<make/model/location/unclear items that cannot be determined from the image>
""".strip()

    result = _call_vision_model(
        image_bytes=image_bytes,
        filename=filename,
        mime_type=mime_type,
        prompt=prompt,
    )

    return str(result or "").strip()


def extract_ocr_from_image(file, filename: str) -> str:
    """
    OCR-only extraction.
    """
    image_bytes = _read_file_bytes(file)

    return extract_ocr_from_image_bytes(
        image_bytes=image_bytes,
        filename=filename,
        mime_type=_guess_mime_type(filename, "image/png"),
    )


def extract_ocr_from_image_bytes(
    *,
    image_bytes: bytes,
    filename: str,
    mime_type: Optional[str] = None,
) -> str:
    prompt = """
You are an OCR engine.

Task:
Extract only text visibly written in the image.

Rules:
- Return only visible written text.
- Do not describe the image.
- Do not analyse the scene.
- Do not guess words, labels, logos, brands, vessel names, or numbers.
- Do not infer missing text from context.
- If text is blurry, cut off, hidden, or uncertain, write [unclear].
- Preserve line breaks where possible.
- Preserve numbers exactly as written.
- Preserve currency symbols, decimals, VAT, tax, subtotal, total, dates, invoice numbers, quantities, and unit prices exactly as written.
- If no readable written text is visible, return exactly: NO_READABLE_TEXT

Return only the OCR text.
""".strip()

    raw_text = _call_vision_model(
        image_bytes=image_bytes,
        filename=filename,
        mime_type=mime_type or _guess_mime_type(filename, "image/png"),
        prompt=prompt,
    )

    clean_text = _normalize_no_readable_text(raw_text)

    if _looks_like_visual_description_not_ocr(clean_text):
        return "NO_READABLE_TEXT"

    return clean_text


def extract_invoice_text_from_image(file, filename: str) -> str:
    """
    Special extraction for invoice/receipt/financial document images.
    """
    image_bytes = _read_file_bytes(file)
    mime_type = _guess_mime_type(filename, "image/png")

    return extract_invoice_text_from_image_bytes(
        image_bytes=image_bytes,
        filename=filename,
        mime_type=mime_type,
    )


def extract_invoice_text_from_image_bytes(
    *,
    image_bytes: bytes,
    filename: str,
    mime_type: Optional[str] = None,
) -> str:
    prompt = """
You are extracting data from a financial document image.

The image may be an invoice, receipt, quote, purchase order, statement, payment document, or table of charges.

Rules:
- Extract only text and numbers that are visibly present.
- Do not invent missing values.
- Do not calculate values that are not supported by visible numbers.
- Preserve invoice number, dates, supplier/vendor, customer, line items, descriptions, quantities, unit prices, tax/VAT, subtotal, total, currency, payment terms, and bank/payment details if visible.
- Preserve line breaks and table-like structure as much as possible.
- If a field is not visible, do not create it.
- If a value is unclear, write [unclear].
- If no financial document text is readable, return exactly: NO_READABLE_TEXT.

Return only the extracted document text.
""".strip()

    raw_text = _call_vision_model(
        image_bytes=image_bytes,
        filename=filename,
        mime_type=mime_type or _guess_mime_type(filename, "image/png"),
        prompt=prompt,
    )

    clean_text = _normalize_no_readable_text(raw_text)

    if _looks_like_visual_description_not_ocr(clean_text):
        return "NO_READABLE_TEXT"

    return clean_text


def extract_ocr_from_pdf_pages(file, filename: str, max_pages: Optional[int] = None) -> str:
    """
    OCR fallback for scanned PDFs.

    Normal digital PDFs are handled by pypdf in extractors.py.
    Scanned invoice PDFs need pages rendered to images first.
    """
    try:
        import fitz
    except Exception as e:
        print("PDF OCR ERROR: PyMuPDF is not installed:", type(e).__name__, str(e))
        return ""

    pdf_bytes = _read_file_bytes(file)

    if not pdf_bytes:
        return ""

    page_limit = max_pages or PDF_OCR_MAX_PAGES

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        print("PDF OCR ERROR: could not open PDF:", type(e).__name__, str(e))
        return ""

    extracted_pages = []

    try:
        page_count = min(len(doc), page_limit)

        for page_index in range(page_count):
            try:
                page = doc.load_page(page_index)
                matrix = fitz.Matrix(PDF_RENDER_ZOOM, PDF_RENDER_ZOOM)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                png_bytes = pix.tobytes("png")

                page_ocr_text = extract_invoice_text_from_image_bytes(
                    image_bytes=png_bytes,
                    filename=f"{filename} - page {page_index + 1}.png",
                    mime_type="image/png",
                )

                page_ocr_text = _normalize_no_readable_text(page_ocr_text)

                if page_ocr_text and page_ocr_text != "NO_READABLE_TEXT":
                    extracted_pages.append(
                        f"PDF page {page_index + 1} OCR text:\n{page_ocr_text}"
                    )

            except Exception as page_error:
                print(
                    "PDF OCR PAGE ERROR:",
                    page_index + 1,
                    type(page_error).__name__,
                    str(page_error),
                )

    finally:
        try:
            doc.close()
        except Exception:
            pass

    return "\n\n".join(extracted_pages).strip()