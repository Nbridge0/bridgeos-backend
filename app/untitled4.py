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


def extract_pdf_text(file) -> str:
    """
    First placeholder.

    Later install pypdf and implement real extraction.
    """

    return ""


def extract_docx_text(file) -> str:
    """
    First placeholder.

    Later install python-docx and implement real extraction.
    """

    return ""


def extract_text_by_file_type(file, filename: str, file_type: str) -> str:
    if file_type == "text":
        return extract_plain_text(file, filename)

    if file_type == "pdf":
        return extract_pdf_text(file)

    if file_type == "docx":
        return extract_docx_text(file)

    return ""