import base64
import io
import mimetypes
import re

from openai import OpenAI

from app.config import (
    OPENAI_API_KEY,
    OPENAI_VISION_MODEL
)


VISION_TIMEOUT_SECONDS = 180

_openai_vision_client = None


def get_openai_vision_client() -> OpenAI:
    """
    Returns one reusable OpenAI client for image analysis and OCR.
    """

    global _openai_vision_client

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is missing from the backend environment."
        )

    if _openai_vision_client is None:
        _openai_vision_client = OpenAI(
            api_key=OPENAI_API_KEY,
            timeout=VISION_TIMEOUT_SECONDS,
            max_retries=2
        )

    return _openai_vision_client


def _read_file_bytes(file) -> bytes:
    file.seek(0)
    data = file.read()
    file.seek(0)

    if isinstance(data, str):
        data = data.encode("utf-8")

    return data or b""


def _guess_mime_type(
    filename: str | None,
    default: str = "application/octet-stream"
) -> str:
    mime_type, _ = mimetypes.guess_type(
        filename or ""
    )

    return mime_type or default


def _file_to_data_url(
    file,
    filename: str,
    mime_type: str | None = None
) -> str:
    file_bytes = _read_file_bytes(file)

    if not file_bytes:
        return ""

    final_mime_type = (
        mime_type
        or _guess_mime_type(
            filename,
            "image/png"
        )
    )

    encoded = base64.b64encode(
        file_bytes
    ).decode("utf-8")

    return (
        f"data:{final_mime_type};"
        f"base64,{encoded}"
    )


def _call_openai_vision(
    *,
    user_input: str,
    file,
    filename: str,
    task: str,
    mime_type: str | None = None
) -> str:
    """
    Sends one image to the OpenAI Responses API.

    There is no RunPod fallback.
    """

    data_url = _file_to_data_url(
        file=file,
        filename=filename,
        mime_type=mime_type
    )

    if not data_url:
        return ""

    client = get_openai_vision_client()

    system_instructions = """
You are BridgeOS visual document analysis.

You must analyse only the actual uploaded image.

Never invent:
- names;
- brands;
- logos;
- vessel names;
- locations;
- dates;
- prices;
- quantities;
- invoice values;
- currencies;
- unreadable text;
- hidden details.

When text is blurry, cropped, hidden or unreadable, explicitly treat it
as unreadable rather than guessing.

Return only the requested result.
""".strip()

    print(
        "OPENAI VISION REQUEST:",
        {
            "provider": "openai",
            "model": OPENAI_VISION_MODEL,
            "filename": filename,
            "task": task
        }
    )

    try:
        response = client.responses.create(
            model=OPENAI_VISION_MODEL,
            instructions=system_instructions,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": user_input
                        },
                        {
                            "type": "input_image",
                            "image_url": data_url,
                            "detail": "high"
                        }
                    ]
                }
            ]
        )

        result = str(
            response.output_text or ""
        ).strip()

        print(
            "OPENAI VISION SUCCESS:",
            {
                "provider": "openai",
                "model": OPENAI_VISION_MODEL,
                "response_id": getattr(
                    response,
                    "id",
                    None
                ),
                "characters": len(result)
            }
        )

        return result

    except Exception as error:
        print(
            "OPENAI VISION ERROR:",
            {
                "provider": "openai",
                "model": OPENAI_VISION_MODEL,
                "error_type": type(error).__name__,
                "error": str(error)
            }
        )

        raise


def _clean_visual_description(
    text: str
) -> str:
    clean = str(text or "").strip()

    if not clean:
        return ""

    remove_prefixes = [
        "image visual description:",
        "visual description:",
        "analysis:",
        "answer:"
    ]

    lowered = clean.lower()

    for prefix in remove_prefixes:
        if lowered.startswith(prefix):
            clean = clean[len(prefix):].strip()
            lowered = clean.lower()

    kept_lines = []

    for line in clean.splitlines():
        stripped = line.strip()
        lowered_line = stripped.lower()

        if lowered_line.startswith("ocr text"):
            continue

        if "financial document extraction" in lowered_line:
            continue

        kept_lines.append(line)

    return "\n".join(
        kept_lines
    ).strip()


def _looks_like_scene_description_not_ocr(
    text: str
) -> bool:
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
        "clear sky"
    ]

    if any(
        phrase in clean
        for phrase in scene_phrases
    ):
        return True

    if (
        len(clean.split()) > 35
        and not re.search(
            r"[A-Z0-9]{2,}",
            text or ""
        )
    ):
        return True

    return False


def _clean_ocr_text(
    text: str
) -> str:
    clean = str(text or "").strip()

    if not clean:
        return ""

    lowered = clean.lower().strip()

    exact_no_text_markers = {
        "no_readable_text",
        "no readable text",
        "no clearly readable text",
        "no text visible",
        "no visible text",
        "none",
        "n/a"
    }

    if lowered in exact_no_text_markers:
        return ""

    if _looks_like_scene_description_not_ocr(
        clean
    ):
        return ""

    remove_prefixes = [
        "ocr text:",
        "extracted text:",
        "visible text:",
        "text:"
    ]

    for prefix in remove_prefixes:
        if lowered.startswith(prefix):
            clean = clean[len(prefix):].strip()
            lowered = clean.lower().strip()

    return clean.strip()


def describe_image(
    file,
    filename: str
) -> str:
    """
    Produces a grounded visual description.
    """

    prompt = """
Analyse this uploaded image.

Return a concise factual description of what is visibly present.

Rules:
- Describe only visible objects and layout.
- Do not invent brands, names, locations, dates, numbers or text.
- Do not claim text is readable unless it is clearly readable.
- Do not guess an exact make or model.
- Do not judge safety, seaworthiness, price, damage, maintenance history
  or purchase suitability.
- For a vessel, identify only the broad visible type.
- Mention important visual limitations.
- Do not include OCR text.
- Write 3 to 6 sentences maximum.
""".strip()

    raw = _call_openai_vision(
        user_input=prompt,
        file=file,
        filename=filename,
        task="image_visual_description"
    )

    return _clean_visual_description(
        raw
    )


def extract_ocr_from_image(
    file,
    filename: str
) -> str:
    """
    Extracts only visible written text.

    Returns NO_READABLE_TEXT when no reliable text is visible.
    """

    prompt = """
Perform OCR on this uploaded image.

Return only text that is clearly visible and readable.

Rules:
- Do not describe the scene.
- Do not summarise.
- Do not guess missing, hidden, cropped or blurry text.
- Preserve useful line breaks.
- Preserve names, dates, invoice numbers, quantities, prices, taxes,
  totals and currencies exactly as visible.
- Never calculate or correct values during OCR.
- If no text is clearly readable, return exactly:
NO_READABLE_TEXT

Return OCR text only.
""".strip()

    raw = _call_openai_vision(
        user_input=prompt,
        file=file,
        filename=filename,
        task="image_ocr_only"
    )

    clean = _clean_ocr_text(
        raw
    )

    if not clean:
        return "NO_READABLE_TEXT"

    return clean


def extract_ocr_from_pdf_pages(
    file,
    filename: str,
    max_pages: int = 12
) -> str:
    """
    Renders scanned PDF pages locally and sends each rendered page
    to OpenAI for OCR.

    Requires:
        pip install pymupdf
    """

    try:
        import fitz
    except Exception as error:
        print(
            "PDF OCR ERROR: pymupdf is not installed:",
            type(error).__name__,
            str(error)
        )

        return ""

    try:
        file.seek(0)
        pdf_bytes = file.read()
        file.seek(0)

        if not pdf_bytes:
            return ""

        document = fitz.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        page_count = min(
            len(document),
            max_pages
        )

        all_text = []

        for page_index in range(page_count):
            page = document.load_page(
                page_index
            )

            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(2, 2),
                alpha=False
            )

            image_bytes = pixmap.tobytes(
                "png"
            )

            image_file = io.BytesIO(
                image_bytes
            )

            page_text = extract_ocr_from_image(
                file=image_file,
                filename=(
                    f"{filename or 'document'}"
                    f"-page-{page_index + 1}.png"
                )
            )

            page_text = _clean_ocr_text(
                page_text
            )

            if page_text:
                all_text.append(
                    f"Page {page_index + 1} OCR:\n"
                    f"{page_text}"
                )

        document.close()

        return "\n\n".join(
            all_text
        ).strip()

    except Exception as error:
        print(
            "PDF OCR ERROR:",
            type(error).__name__,
            str(error)
        )

        return ""