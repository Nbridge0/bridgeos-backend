from pypdf import PdfReader
from docx import Document


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150):
    """
    Splits text into overlapping chunks.
    """

    if not text:
        return []

    chunks = []
    start = 0
    text_length = len(text)

    while start < text_length:
        end = start + chunk_size
        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        start += chunk_size - overlap

    return chunks


def extract_plain_text(file, filename: str) -> str:
    file.seek(0)
    raw = file.read()

    return raw.decode("utf-8", errors="ignore")


def extract_pdf_form_fields(reader) -> str:
    """
    Extracts fillable PDF form fields generically.

    Some invoices/forms store values as AcroForm fields instead of normal page text,
    so page.extract_text() can miss line items, prices, quantities, and totals.
    """

    try:
        fields = reader.get_fields() or {}
    except Exception:
        return ""

    if not fields:
        return ""

    lines = ["PDF form fields:"]

    for field_name, field_data in fields.items():
        value = ""

        if isinstance(field_data, dict):
            value = (
                field_data.get("/V")
                or field_data.get("V")
                or field_data.get("/DV")
                or field_data.get("DV")
                or ""
            )
        else:
            value = field_data

        value = str(value or "").strip()

        if not value:
            continue

        lines.append(f"{field_name}: {value}")

    return "\n".join(lines).strip()


def extract_pdf_text(file) -> str:
    file.seek(0)

    reader = PdfReader(file)

    text_parts = []

    for page in reader.pages:
        text = page.extract_text() or ""

        if text.strip():
            text_parts.append(text.strip())

    form_text = extract_pdf_form_fields(reader)

    if form_text:
        text_parts.append(form_text)

    return "\n\n".join(text_parts).strip()


def extract_docx_text(file) -> str:
    file.seek(0)

    document = Document(file)

    parts = []

    for paragraph in document.paragraphs:
        text = paragraph.text or ""

        if text.strip():
            parts.append(text.strip())

    return "\n\n".join(parts).strip()


def extract_text_by_file_type(file, filename: str, file_type: str) -> str:
    if file_type == "text":
        return extract_plain_text(file, filename)

    if file_type == "pdf":
        return extract_pdf_text(file)

    if file_type == "docx":
        return extract_docx_text(file)

    return ""