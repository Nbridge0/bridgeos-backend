from fastapi import Request, HTTPException

# TEMP TEST TOKEN.
# This is only for development/testing.
# Remove before production.
DEV_ACCESS_TOKEN = "bridgeos-dev-token"

# Deterministic demo user id.
# Must be a valid UUID string because your database uses UUID ids.
DEV_USER_ID = "11111111-1111-1111-1111-111111111111"
DEV_EMAIL = "demo@bridgeos.com"


def get_user(request: Request):
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing authorization token")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = auth_header.replace("Bearer ", "").strip()

    if not token:
        raise HTTPException(status_code=401, detail="Empty authorization token")

    # TEMP DEV MODE.
    # Accept our test token so frontend upload/chat can work immediately.
    if token == DEV_ACCESS_TOKEN:
        return {
            "sub": DEV_USER_ID,
            "email": DEV_EMAIL,
            "role": "authenticated"
        }

    raise HTTPException(status_code=401, detail="Invalid test token")
