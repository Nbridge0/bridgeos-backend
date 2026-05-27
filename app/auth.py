from fastapi import Request, HTTPException
from supabase import create_client
import jwt as pyjwt

from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET


auth_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_user(request: Request):
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing authorization token")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = auth_header.replace("Bearer ", "", 1).strip()

    if not token:
        raise HTTPException(status_code=401, detail="Empty authorization token")

    # First try real Supabase session token
    try:
        auth_res = auth_client.auth.get_user(token)

        if auth_res and auth_res.user:
            user = auth_res.user

            return {
                "id": user.id,
                "sub": user.id,
                "email": user.email,
                "role": "authenticated",
                "aud": "authenticated"
            }

    except Exception:
        pass

    # Then try your custom dev-login JWT
    try:
        payload = pyjwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated"
        )

        user_id = payload.get("sub")

        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        return {
            "id": user_id,
            "sub": user_id,
            "email": payload.get("email"),
            "role": payload.get("role", "authenticated"),
            "aud": payload.get("aud", "authenticated")
        }

    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Could not verify Supabase token"
        )