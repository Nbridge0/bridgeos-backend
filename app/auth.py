from fastapi import Request, HTTPException
from supabase import create_client

from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY


def get_user(request: Request):
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing authorization token")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = auth_header.replace("Bearer ", "").strip()

    if not token:
        raise HTTPException(status_code=401, detail="Empty authorization token")

    try:
        # Separate client used ONLY for checking the incoming access token.
        # Do not import or use app.database.supabase here.
        auth_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

        auth_res = auth_client.auth.get_user(token)

        if not auth_res or not auth_res.user:
            raise HTTPException(status_code=401, detail="Invalid Supabase user token")

        user = auth_res.user

        return {
            "id": user.id,
            "sub": user.id,
            "email": user.email,
            "role": "authenticated",
            "aud": "authenticated"
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Could not verify Supabase token: {str(e)}"
        )