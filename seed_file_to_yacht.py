import json
import sys
import urllib.request
import urllib.error
import mimetypes
import uuid
from pathlib import Path


API_BASE = "https://bridgeos-backend.onrender.com"

# Always upload seeded/demo documents as Tier 1 only.
SECURITY_LEVEL = 1


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
        with urllib.request.urlopen(req, timeout=120) as res:
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


def post_multipart_file(path, file_path: Path, token: str):
    """
    Uploads the real file to /assets as multipart/form-data.

    Important:
    - Uploads the actual PDF/file, not just extracted text.
    - Security level is always 1.
    - The backend /assets route will extract text, chunk it, embed it,
      store the original file, and make it searchable/previewable.
    """

    url = API_BASE + path
    boundary = "----BridgeOSBoundary" + uuid.uuid4().hex

    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    file_bytes = file_path.read_bytes()

    body = bytearray()

    # security_level field
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(b'Content-Disposition: form-data; name="security_level"\r\n')
    body.extend(b"Content-Type: text/plain\r\n\r\n")
    body.extend(str(SECURITY_LEVEL).encode("utf-8"))
    body.extend(b"\r\n")

    # file field
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{file_path.name}"\r\n'
        ).encode("utf-8")
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body))
    }

    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            response_body = res.read().decode("utf-8")

            if not response_body.strip():
                return {}

            return json.loads(response_body)

    except urllib.error.HTTPError as e:
        response_body = e.read().decode("utf-8", errors="ignore")

        print("")
        print("UPLOAD FAILED")
        print("URL:", url)
        print("STATUS:", e.code)
        print("BODY:", response_body)
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
        print('python3 seed_file_to_yacht.py arman@askthebridge.com "Bridge123!" "Watchkeeping At Anchor DEMO.docx.pdf"')
        print("")
        print(f"Security level: {SECURITY_LEVEL} = Tier 1 only")
        raise SystemExit(1)

    email = sys.argv[1]
    password = sys.argv[2]
    file_path = Path(sys.argv[3])

    if not file_path.exists():
        print(f"File not found: {file_path}")
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
    print("Uploading real file to BridgeOS assets...")
    print(f"File: {file_path.name}")
    print(f"Security level: {SECURITY_LEVEL} = Tier 1 only")
    print("")

    result = post_multipart_file(
        path="/assets",
        file_path=file_path,
        token=token
    )

    print("Upload result:")
    print(json.dumps(result, indent=2))
    print("")
    print("Done. The file is uploaded, processed, searchable, previewable, and Tier 1 only.")


if __name__ == "__main__":
    main()