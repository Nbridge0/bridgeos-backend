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
    """
    Extracts text from:
    - normal DOCX paragraphs
    - tables
    - headers
    - footers
    - text boxes and other XML text nodes
    """

    file.seek(0)
    document = Document(file)

    parts = []
    seen = set()

    def add_text(value: str):
        clean = " ".join(str(value or "").split()).strip()

        if not clean:
            return

        if clean in seen:
            return

        seen.add(clean)
        parts.append(clean)

    # Normal document paragraphs
    for paragraph in document.paragraphs:
        add_text(paragraph.text)

    # Tables, including nested tables
    def extract_table(table):
        for row in table.rows:
            row_values = []

            for cell in row.cells:
                cell_parts = []

                for paragraph in cell.paragraphs:
                    text = " ".join((paragraph.text or "").split()).strip()

                    if text:
                        cell_parts.append(text)

                for nested_table in cell.tables:
                    extract_table(nested_table)

                cell_text = " | ".join(cell_parts).strip()

                if cell_text:
                    row_values.append(cell_text)

            if row_values:
                add_text(" | ".join(row_values))

    for table in document.tables:
        extract_table(table)

    # Headers and footers
    for section in document.sections:
        for paragraph in section.header.paragraphs:
            add_text(paragraph.text)

        for table in section.header.tables:
            extract_table(table)

        for paragraph in section.footer.paragraphs:
            add_text(paragraph.text)

        for table in section.footer.tables:
            extract_table(table)

    # Text boxes, shapes and XML text nodes that python-docx does not
    # expose through document.paragraphs.
    try:
        namespace = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        }

        xml_text_nodes = document.element.body.xpath(
            ".//w:t",
            namespaces=namespace
        )

        for node in xml_text_nodes:
            add_text(node.text)

    except TypeError:
        # Some lxml/python-docx versions already know the namespaces.
        try:
            xml_text_nodes = document.element.body.xpath(".//w:t")

            for node in xml_text_nodes:
                add_text(node.text)

        except Exception as e:
            print(
                "DOCX XML TEXT EXTRACTION ERROR:",
                type(e).__name__,
                str(e)
            )

    except Exception as e:
        print(
            "DOCX XML TEXT EXTRACTION ERROR:",
            type(e).__name__,
            str(e)
        )

    try:
        file.seek(0)
    except Exception:
        pass

    return "\n".join(parts).strip()
    
def extract_text_by_file_type(file, filename: str, file_type: str) -> str:
    file_type = str(file_type or "").strip().lower()

    try:
        file.seek(0)

        if file_type == "text":
            extracted = extract_plain_text(file, filename)

        elif file_type == "pdf":
            extracted = extract_pdf_text(file, filename)

        elif file_type == "docx":
            extracted = extract_docx_text(file)

        else:
            raise ValueError(
                f"Unsupported extractable file type: {file_type}"
            )

        extracted = str(extracted or "").strip()

        print(
            "TEXT EXTRACTION DEBUG:",
            {
                "filename": filename,
                "file_type": file_type,
                "characters": len(extracted),
                "preview": extracted[:300]
            }
        )

        return extracted

    finally:
        try:
            file.seek(0)
        except Exception:
            pass