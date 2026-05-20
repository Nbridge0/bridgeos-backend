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
        # IMPORTANT:
        # Supabase JWTs can fail strict PyJWT audience checks depending on project/settings.
        # We verify the signature, but do NOT force audience here.
        payload = pyjwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={
                "verify_aud": False
            }
        )

        user_id = payload.get("sub")
        email = payload.get("email")

        if not user_id:
            raise HTTPException(status_code=401, detail="Token missing user id")

        return {
            "id": user_id,
            "sub": user_id,
            "email": email,
            "role": payload.get("role", "authenticated"),
            "aud": payload.get("aud")
        }

    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")

    except pyjwt.InvalidSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Invalid token signature. Check SUPABASE_JWT_SECRET in Render."
        )

    except pyjwt.DecodeError:
        raise HTTPException(status_code=401, detail="Invalid token decode")

    except pyjwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")