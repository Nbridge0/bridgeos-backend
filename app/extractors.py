from pypdf import PdfReader
from docx import Document
from app.image_ai import extract_ocr_from_pdf_pages

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


def extract_pdf_text(file, filename: str = "document.pdf") -> str:
    """
    Extract text from digital PDFs, PDF form fields, and scanned PDFs.

    Digital invoices usually work with pypdf.
    Scanned invoices need OCR fallback.
    """
    parts = []

    try:
        file.seek(0)
        reader = PdfReader(file)

        for page_index, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text() or ""
                page_text = page_text.strip()

                if page_text:
                    parts.append(f"PDF page {page_index + 1} text:\n{page_text}")

            except Exception as e:
                print("PDF PAGE TEXT ERROR:", page_index + 1, type(e).__name__, str(e))

        try:
            form_fields = extract_pdf_form_fields(reader)

            if form_fields:
                parts.append("PDF form fields:\n" + form_fields)

        except Exception as e:
            print("PDF FORM FIELD ERROR:", type(e).__name__, str(e))

    except Exception as e:
        print("PDF TEXT ERROR:", type(e).__name__, str(e))

    extracted_text = "\n\n".join(parts).strip()

    digit_count = sum(char.isdigit() for char in extracted_text)
    text_is_weak = len(extracted_text) < 120
    numbers_are_weak = digit_count < 5

    should_run_ocr = text_is_weak or numbers_are_weak

    if should_run_ocr:
        try:
            file.seek(0)
            ocr_text = extract_ocr_from_pdf_pages(
                file=file,
                filename=filename,
            )

            if ocr_text:
                if extracted_text:
                    extracted_text += "\n\nScanned PDF OCR text:\n" + ocr_text
                else:
                    extracted_text = "Scanned PDF OCR text:\n" + ocr_text

        except Exception as e:
            print("PDF OCR FALLBACK ERROR:", type(e).__name__, str(e))

    try:
        file.seek(0)
    except Exception:
        pass

    return extracted_text.strip()

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
        return extract_pdf_text(file, filename)

    if file_type == "docx":
        return extract_docx_text(file)

    return ""