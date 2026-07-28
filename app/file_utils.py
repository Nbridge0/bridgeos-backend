import hashlib
import mimetypes
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".html", ".xml"}
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx"}


def detect_file_type(filename: str, mime_type: str | None = None) -> str:
    ext = Path(filename.lower()).suffix

    if ext in IMAGE_EXTENSIONS:
        return "image"

    if ext in PDF_EXTENSIONS:
        return "pdf"

    if ext in DOCX_EXTENSIONS:
        return "docx"

    if ext in TEXT_EXTENSIONS:
        return "text"

    if mime_type:
        if mime_type.startswith("image/"):
            return "image"

        if mime_type == "application/pdf":
            return "pdf"

        if "wordprocessingml" in mime_type:
            return "docx"

        if mime_type.startswith("text/"):
            return "text"

    guessed_type, _ = mimetypes.guess_type(filename)

    if guessed_type:
        if guessed_type.startswith("image/"):
            return "image"

        if guessed_type == "application/pdf":
            return "pdf"

        if guessed_type.startswith("text/"):
            return "text"

    return "unknown"


def calculate_file_hash(file) -> str:
    file.seek(0)

    hasher = hashlib.sha256()

    while True:
        chunk = file.read(8192)
        if not chunk:
            break
        hasher.update(chunk)

    file.seek(0)

    return hasher.hexdigest()


def safe_filename(filename: str) -> str:
    """
    Basic filename cleanup.
    Keeps it simple for now.
    """

    return filename.replace("/", "_").replace("\\", "_").strip()