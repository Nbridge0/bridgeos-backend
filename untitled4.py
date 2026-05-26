import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

API_BASE = "https://bridgeos-backend.onrender.com"


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
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print("REQUEST FAILED")
        print("URL:", url)
        print("STATUS:", e.code)
        print("BODY:", body)
        raise SystemExit(1)


def main():
    if len(sys.argv) != 4:
        print("Usage:")
        print("python3 seed_file_to_yacht.py EMAIL PASSWORD FILE_PATH")
        raise SystemExit(1)

    email = sys.argv[1]
    password = sys.argv[2]
    file_path = Path(sys.argv[3])

    if not file_path.exists():
        print(f"File not found: {file_path}")
        raise SystemExit(1)

    content = file_path.read_text(encoding="utf-8", errors="ignore")

    if not content.strip():
        print("File is empty.")
        raise SystemExit(1)

    print("Logging in...")
    login = post_json("/auth/login", {
        "email": email,
        "password": password
    })

    token = login["access_token"]

    print("Seeding file into this user's yacht database...")
    result = post_json("/dev/seed-asset", {
        "file_name": file_path.name,
        "content": content
    }, token=token)

    print(json.dumps(result, indent=2))
    print("")
    print("Done. This file is now searchable by that yacht account.")


if __name__ == "__main__":
    main()