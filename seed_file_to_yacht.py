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
    if len(sys.argv) != 5:
        print("Usage:")
        print("python3 seed_file_to_yacht.py EMAIL PASSWORD FILE_PATH SECURITY_LEVEL")
        print("")
        print("SECURITY_LEVEL:")
        print("1 = Tier 1 only")
        print("2 = Tier 1 and Tier 2")
        print("3 = Tier 1, Tier 2, and Tier 3")
        raise SystemExit(1)

    email = sys.argv[1]
    password = sys.argv[2]
    file_path = Path(sys.argv[3])
    security_level = int(sys.argv[4])

    if security_level not in [1, 2, 3]:
        print("SECURITY_LEVEL must be 1, 2, or 3")
        raise SystemExit(1)

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

    print(f"Seeding file with security_level={security_level}...")
    result = post_json("/dev/seed-asset", {
        "file_name": file_path.name,
        "content": content,
        "security_level": security_level
    }, token=token)

    print(json.dumps(result, indent=2))
    print("")
    print("Done. This file is now searchable by allowed tiers only.")


if __name__ == "__main__":
    main()