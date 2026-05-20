from fastapi import Request, HTTPException
import jwt as pyjwt

from app.config import SUPABASE_JWT_SECRET


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
        payload = pyjwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated"
        )

        user_id = payload.get("sub")
        email = payload.get("email")

        if not user_id:
            raise HTTPException(status_code=401, detail="Token missing user id")

        return {
            "sub": user_id,
            "email": email,
            "role": payload.get("role", "authenticated")
        }

    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")

    except pyjwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Invalid token audience")

    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")