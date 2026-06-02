import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

from pypdf import PdfReader


API_BASE = "https://bridgeos-backend.onrender.com"

YACHT_ID = "1c7a2b09-2bca-465b-a77b-c6b5c7995fff"
SECURITY_LEVEL = 1


def extract_text(file_path: Path) -> str:
    """
    Extract readable text from a supported file.
    Supports PDF and plain text-style files.
    """

    if file_path.suffix.lower() == ".pdf":
        reader = PdfReader(str(file_path))
        pages = []

        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append(f"\n\n--- Page {index} ---\n{text}")

        return "\n".join(pages)

    return file_path.read_text(encoding="utf-8", errors="ignore")


def post_json(path, payload, token=None):
    url = API_BASE + path
    data = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json"
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as res:
            body = res.read().decode("utf-8")

            if not body.strip():
                return {}

            return json.loads(body)

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")

        print("")
        print("REQUEST FAILED")
        print("URL:", url)
        print("STATUS:", e.code)
        print("BODY:", body)
        print("")

        raise SystemExit(1)

    except urllib.error.URLError as e:
        print("")
        print("NETWORK ERROR")
        print("URL:", url)
        print("ERROR:", e)
        print("")

        raise SystemExit(1)


def main():
    if len(sys.argv) != 4:
        print("Usage:")
        print("python3 seed_file_to_yacht.py EMAIL PASSWORD FILE_PATH")
        print("")
        print("Example:")
        print('python3 seed_file_to_yacht.py admin@example.com password123 "Audit Report 3rd Feb 2023.docx.pdf"')
        print("")
        print(f"Yacht ID: {YACHT_ID}")
        print(f"Security level: {SECURITY_LEVEL} = Tier 1 only")
        raise SystemExit(1)

    email = sys.argv[1]
    password = sys.argv[2]
    file_path = Path(sys.argv[3])

    if not file_path.exists():
        print(f"File not found: {file_path}")
        raise SystemExit(1)

    print("Reading document...")
    content = extract_text(file_path)

    if not content.strip():
        print("Could not extract readable text from the file.")
        raise SystemExit(1)

    print("Logging in...")
    login = post_json("/auth/login", {
        "email": email,
        "password": password
    })

    token = login.get("access_token")

    if not token:
        print("Login response did not include access_token.")
        print(json.dumps(login, indent=2))
        raise SystemExit(1)

    print("")
    print("Uploading document to chatbot knowledge base...")
    print(f"File: {file_path.name}")
    print(f"Yacht ID: {YACHT_ID}")
    print(f"Security level: {SECURITY_LEVEL} = Tier 1 only")
    print("")

    result = post_json("/dev/seed-asset", {
        "yacht_id": YACHT_ID,
        "file_name": file_path.name,
        "content": content,
        "security_level": SECURITY_LEVEL
    }, token=token)

    print("Upload result:")
    print(json.dumps(result, indent=2))
    print("")
    print("Done. This document should now be searchable by Tier 1 users only.")


if __name__ == "__main__":
    main()