import base64
import io
import json
import mimetypes
import re
from typing import Any

import requests

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY


VISION_TIMEOUT_SECONDS = 180


def _read_file_bytes(file) -> bytes:
    file.seek(0)
    data = file.read()
    file.seek(0)

    if isinstance(data, str):
        data = data.encode("utf-8")

    return data or b""


def _guess_mime_type(filename: str | None, default: str = "application/octet-stream") -> str:
    mime_type, _ = mimetypes.guess_type(filename or "")
    return mime_type or default


def _file_to_base64(file) -> str:
    data = _read_file_bytes(file)
    return base64.b64encode(data).decode("utf-8")


def _extract_response_text(data: Any) -> str:
    """
    Supports different possible RunPod response shapes without hard-coding one.
    """

    if data is None:
        return ""

    if isinstance(data, str):
        return data.strip()

    if isinstance(data, dict):
        for key in [
            "response",
            "answer",
            "text",
            "content",
            "message",
            "output",
            "result",
        ]:
            value = data.get(key)

            if isinstance(value, str) and value.strip():
                return value.strip()

            if isinstance(value, dict):
                nested = _extract_response_text(value)
                if nested:
                    return nested

            if isinstance(value, list):
                parts = []

                for item in value:
                    nested = _extract_response_text(item)
                    if nested:
                        parts.append(nested)

                if parts:
                    return "\n".join(parts).strip()

        choices = data.get("choices")

        if isinstance(choices, list):
            parts = []

            for choice in choices:
                nested = _extract_response_text(choice)
                if nested:
                    parts.append(nested)

            if parts:
                return "\n".join(parts).strip()

    if isinstance(data, list):
        parts = []

        for item in data:
            nested = _extract_response_text(item)
            if nested:
                parts.append(nested)

        return "\n".join(parts).strip()

    return ""


def _call_vision_chat(
    *,
    user_input: str,
    file,
    filename: str,
    task: str,
    mime_type: str | None = None
) -> str:
    """
    Sends image/PDF-page image to the RunPod vision endpoint.

    Important:
    - Sends multiple common image fields for compatibility.
    - Does not hard-code yacht/image answers.
    - The anti-hallucination rules are generic.
    """

    if not RUNPOD_BASE_URL:
        return ""

    if not BRIDGEOS_API_KEY:
        return ""

    clean_filename = filename or "uploaded-image"
    clean_mime_type = mime_type or _guess_mime_type(clean_filename, "image/png")
    image_base64 = _file_to_base64(file)

    if not image_base64:
        return ""

    data_url = f"data:{clean_mime_type};base64,{image_base64}"

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    payload = {
        "user_input": user_input,
        "history": [],
        "image": image_base64,
        "image_base64": image_base64,
        "image_data_url": data_url,
        "mime_type": clean_mime_type,
        "images": [
            {
                "filename": clean_filename,
                "mime_type": clean_mime_type,
                "data": image_base64,
                "data_url": data_url,
            }
        ],
        "backend_context": {
            "filename": clean_filename,
            "mime_type": clean_mime_type,
            "task": task,
            "important_instruction": (
                "Analyse only the actual uploaded visual content. "
                "Do not invent names, brands, logos, vessel names, locations, text, numbers, prices, "
                "invoice values, dates, or financial amounts. "
                "If something is not visible or not readable, say it is not visible or not readable. "
                "For boats, classify only the broad visible vessel type and explain the visible evidence. "
                "Never pretend to know condition, build quality, price, maintenance history, engine status, "
                "survey status, suitability for purchase, or exact make/model from an image alone."
            ),
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
            timeout=VISION_TIMEOUT_SECONDS,
        )

        print("VISION DEBUG status:", response.status_code)
        print("VISION DEBUG response:", response.text[:800])

        if response.status_code >= 400:
            return ""

        try:
            data = response.json()
        except Exception:
            return response.text.strip()

        return _extract_response_text(data)

    except Exception as e:
        print("VISION REQUEST ERROR:", type(e).__name__, str(e))
        return ""


def _clean_visual_description(text: str) -> str:
    clean = str(text or "").strip()

    if not clean:
        return ""

    remove_prefixes = [
        "image visual description:",
        "visual description:",
        "analysis:",
        "answer:",
    ]

    lowered = clean.lower()

    for prefix in remove_prefixes:
        if lowered.startswith(prefix):
            clean = clean[len(prefix):].strip()
            lowered = clean.lower()

    # Remove fake OCR/financial boilerplate if the model added it to image descriptions.
    unwanted_lines = []

    for line in clean.splitlines():
        stripped = line.strip()
        low = stripped.lower()

        if not stripped:
            unwanted_lines.append(line)
            continue

        if low.startswith("ocr text"):
            continue

        if "financial document extraction" in low:
            continue

        if "no relevant numbers or financial values" in low:
            continue

        unwanted_lines.append(line)

    clean = "\n".join(unwanted_lines).strip()

    return clean


def _looks_like_scene_description_not_ocr(text: str) -> bool:
    """
    OCR must be visible written text only.
    If the model describes the scene, it is not OCR.
    """

    clean = str(text or "").strip().lower()

    if not clean:
        return False

    scene_phrases = [
        "the image shows",
        "the image depicts",
        "the image appears",
        "this image shows",
        "this image depicts",
        "there is a",
        "there are",
        "it shows",
        "it appears",
        "a white yacht",
        "a sailboat",
        "a boat",
        "a marina",
        "calm water",
        "clear sky",
    ]

    if any(phrase in clean for phrase in scene_phrases):
        return True

    # OCR should usually contain short text fragments, not long paragraphs.
    if len(clean.split()) > 35 and not re.search(r"[A-Z0-9]{2,}", text or ""):
        return True

    return False


def _clean_ocr_text(text: str) -> str:
    clean = str(text or "").strip()

    if not clean:
        return ""

    lowered = clean.lower().strip()

    no_text_markers = [
        "no readable text",
        "no clearly readable text",
        "no text visible",
        "no visible text",
        "not applicable",
        "none",
        "n/a",
    ]

    if any(marker in lowered for marker in no_text_markers):
        return ""

    if _looks_like_scene_description_not_ocr(clean):
        return ""

    remove_prefixes = [
        "ocr text:",
        "extracted text:",
        "visible text:",
        "text:",
    ]

    for prefix in remove_prefixes:
        if lowered.startswith(prefix):
            clean = clean[len(prefix):].strip()
            lowered = clean.lower().strip()

    return clean.strip()


def describe_image(file, filename: str) -> str:
    """
    Creates a grounded visual description.

    The output should be useful for later chat answers, not a salesy caption.
    """

    prompt = """
You are analysing one uploaded image.

Return a concise factual description of what is visibly present.

Rules:
- Describe only visible objects and layout.
- Do not invent brands, logos, names, locations, dates, numbers, or text.
- Do not say text is visible unless it is clearly readable.
- Do not guess exact make/model.
- Do not judge whether something is good, bad, safe, seaworthy, damaged, expensive, or worth buying.
- If the image contains a boat, identify only the broad visible type, such as motor yacht, sailing yacht, catamaran, tender, or unknown.
- Explain the visible evidence for the broad type.
- Mention visible limitations, such as exact model or condition not being visible.
- Do not include OCR output.
- Do not include financial-document comments unless the image is actually a financial document.

Write in plain text, 3 to 6 sentences maximum.
""".strip()

    raw = _call_vision_chat(
        user_input=prompt,
        file=file,
        filename=filename,
        task="image_visual_description",
    )

    return _clean_visual_description(raw)


def extract_ocr_from_image(file, filename: str) -> str:
    """
    Extracts only actual visible written text.

    If there is no readable text, returns NO_READABLE_TEXT.
    """

    prompt = """
You are doing OCR on one uploaded image.

Return ONLY the written text that is visibly readable in the image.

Rules:
- Do not describe the scene.
- Do not summarise the image.
- Do not invent text.
- Do not infer hidden or blurry text.
- Preserve line breaks when useful.
- For invoices/receipts, preserve supplier, invoice number, dates, line items, quantities, unit prices, subtotal, VAT/tax, total, and currency exactly as visible.
- If no text is clearly readable, return exactly:
NO_READABLE_TEXT

Return OCR text only.
""".strip()

    raw = _call_vision_chat(
        user_input=prompt,
        file=file,
        filename=filename,
        task="image_ocr_only",
    )

    clean = _clean_ocr_text(raw)

    if not clean:
        return "NO_READABLE_TEXT"

    return clean


def extract_ocr_from_pdf_pages(file, filename: str, max_pages: int = 12) -> str:
    """
    OCR fallback for scanned PDFs.

    Requires pymupdf:
        pip install pymupdf
    """

    try:
        import fitz
    except Exception as e:
        print("PDF OCR ERROR: pymupdf not installed:", type(e).__name__, str(e))
        return ""

    try:
        file.seek(0)
        pdf_bytes = file.read()
        file.seek(0)

        if not pdf_bytes:
            return ""

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = min(len(doc), max_pages)

        all_text = []

        for page_index in range(page_count):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image_bytes = pix.tobytes("png")

            image_file = io.BytesIO(image_bytes)

            page_text = extract_ocr_from_image(
                image_file,
                f"{filename or 'document'}-page-{page_index + 1}.png"
            )

            page_text = _clean_ocr_text(page_text)

            if page_text:
                all_text.append(
                    f"Page {page_index + 1} OCR:\n{page_text}"
                )

        doc.close()

        return "\n\n".join(all_text).strip()

    except Exception as e:
        print("PDF OCR ERROR:", type(e).__name__, str(e))
        return ""