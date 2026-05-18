from fastapi import Request, HTTPException
import jwt

from app.config import SUPABASE_JWT_SECRET
from app.database import supabase


def get_user(request: Request):
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing authorization token")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = auth_header.replace("Bearer ", "").strip()

    if not token:
        raise HTTPException(status_code=401, detail="Empty authorization token")

    # First try local JWT decoding.
    # This is fast, but it depends on SUPABASE_JWT_SECRET being exactly correct.
    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={
                "verify_exp": True,
                "verify_aud": False
            }
        )

        if not payload.get("sub"):
            raise HTTPException(status_code=401, detail="Token missing user id")

        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")

    except Exception:
        # Fallback: ask Supabase Auth to validate the token.
        # This protects you if the Render SUPABASE_JWT_SECRET is wrong or audience decoding is different.
        try:
            user_res = supabase.auth.get_user(token)

            if not user_res or not user_res.user:
                raise HTTPException(status_code=401, detail="Invalid Supabase token")

            return {
                "sub": user_res.user.id,
                "email": user_res.user.email
            }

        except HTTPException:
            raise

        except Exception as e:
            raise HTTPException(
                status_code=401,
                detail=f"Invalid token: {str(e)}"
            )
