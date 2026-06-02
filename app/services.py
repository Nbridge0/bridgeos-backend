from fastapi import HTTPException
import requests

from app.database import supabase
from app.embeddings import embed
from app.config import BUCKET_NAME, RUNPOD_BASE_URL, BRIDGEOS_API_KEY
from app.llm import ask_llm, FALLBACK_NO_DATA_ANSWER
from app.file_utils import detect_file_type, calculate_file_hash, safe_filename
from app.metadata_utils import (
    extract_date_from_filename,
    extract_year_from_text,
    detect_event,
    generate_basic_tags,
    extract_query_filters
)
from app.extractors import (
    chunk_text,
    extract_text_by_file_type
)
from app.image_ai import (
    describe_image,
    extract_ocr_from_image
)


import time
import uuid
import jwt as pyjwt
import io
from urllib.parse import quote
from datetime import datetime, timezone

from app.config import SUPABASE_JWT_SECRET, SUPABASE_URL, SUPABASE_SERVICE_KEY
from supabase import create_client

auth_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

storage_admin = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

# ------------------------
# YACHT
# ------------------------

def create_yacht(user_id: str, name: str):
    return supabase.table("yachts").insert({
        "name": name,
        "owner_id": user_id
    }).execute()


# ------------------------
# AUTH / ACCOUNTS
# ------------------------
def signup_admin(email: str, password: str, full_name: str, yacht_name: str):
    """
    Creates the MAIN admin account using normal Supabase signup:
    1. Supabase Auth user
    2. Yacht row
    3. Crew profile with security_level = 1

    This avoids supabase.auth.admin.create_user(), which can fail with:
    'User not allowed'
    """

    try:
        auth_res = auth_admin.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True
        })
    except Exception as e:
        error_text = str(e)

        if (
            "already been registered" in error_text
            or "already exists" in error_text
            or "User already registered" in error_text
        ):
            raise HTTPException(
                status_code=409,
                detail="This email already exists. Try logging in or use another email."
            )

        raise HTTPException(
            status_code=400,
            detail=f"Could not create Supabase Auth user: {error_text}"
        )

    if not auth_res.user:
        raise HTTPException(
            status_code=400,
            detail="Supabase did not return a user."
        )

    user_id = auth_res.user.id

    try:
        existing_crew = supabase.table("crew") \
            .select("*") \
            .eq("id", user_id) \
            .execute()

        if existing_crew.data:
            return {
                "message": "Admin account already exists",
                "account_type": "main_admin",
                "email": email,
                "user_id": user_id,
                "crew": existing_crew.data[0]
            }
    except Exception:
        pass

    try:
        yacht_res = supabase.table("yachts").insert({
            "name": yacht_name,
            "owner_id": user_id
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create yacht row: {str(e)}"
        )

    if not yacht_res.data:
        raise HTTPException(
            status_code=400,
            detail="Could not create yacht row. Supabase returned no data."
        )

    yacht = yacht_res.data[0]

    try:
        crew_res = supabase.table("crew").insert({
            "id": user_id,
            "email": email,
            "full_name": full_name,
            "yacht_id": yacht["id"],
            "security_level": 1,
            "created_by": user_id
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create admin crew profile: {str(e)}"
        )

    if not crew_res.data:
        raise HTTPException(
            status_code=400,
            detail="Could not create admin crew profile. Supabase returned no data."
        )

    return {
        "message": "Main admin account created successfully",
        "account_type": "main_admin",
        "email": email,
        "user_id": user_id,
        "yacht": yacht,
        "crew": crew_res.data[0]
    }
    
def dev_create_admin(email: str, password: str, full_name: str, yacht_name: str):
    """
    DEV ONLY.

    Creates a Supabase Auth user, yacht, and crew profile.
    Use a new email each time while testing.

    If the email already exists, it returns a clear error.
    """

    try:
        auth_res = supabase.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True
        })
    except Exception as e:
        error_text = str(e)

        if "already been registered" in error_text or "already exists" in error_text:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This email already exists in Supabase Auth. "
                    "Use a new email or delete this user from Supabase Auth first."
                )
            )

        raise HTTPException(
            status_code=500,
            detail=f"Could not create Supabase Auth user: {error_text}"
        )

    if not auth_res.user:
        raise HTTPException(status_code=500, detail="Supabase did not return a user")

    user_id = auth_res.user.id

    yacht_res = supabase.table("yachts").insert({
        "name": yacht_name,
        "owner_id": user_id
    }).execute()

    if not yacht_res.data:
        raise HTTPException(status_code=500, detail="Could not create yacht row")

    yacht = yacht_res.data[0]

    crew_res = supabase.table("crew").insert({
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "yacht_id": yacht["id"],
        "security_level": 1,
        "created_by": user_id
    }).execute()

    if not crew_res.data:
        raise HTTPException(status_code=500, detail="Could not create crew row")

    return {
        "message": "Dev admin created successfully",
        "email": email,
        "password": password,
        "user_id": user_id,
        "yacht": yacht,
        "crew": crew_res.data[0]
    }


def login(email: str, password: str):
    """
    Logs in a user using Supabase Auth and returns a clean token response.
    """

    try:
        auth_res = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Supabase login failed: {str(e)}"
        )

    if not auth_res:
        raise HTTPException(status_code=401, detail="No login response from Supabase")

    if not getattr(auth_res, "session", None):
        raise HTTPException(status_code=401, detail="No session returned from Supabase")

    if not auth_res.session.access_token:
        raise HTTPException(status_code=401, detail="No access token returned from Supabase")

    return {
        "access_token": auth_res.session.access_token,
        "refresh_token": auth_res.session.refresh_token,
        "token_type": "bearer",
        "user": {
            "id": auth_res.user.id if auth_res.user else None,
            "email": auth_res.user.email if auth_res.user else email
        }
    }

def dev_login(email: str, full_name: str = "Test Admin", yacht_name: str = "Test Yacht"):
    """
    DEV LOGIN ONLY.

    This bypasses Supabase Auth.
    It creates/repairs:
    - yacht
    - crew profile
    - JWT token your own auth.py can read

    Remove this before production.
    """

    user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, email.lower().strip()))

    crew_res = supabase.table("crew") \
        .select("*") \
        .eq("id", user_id) \
        .execute()

    if crew_res.data:
        crew = crew_res.data[0]
    else:
        yacht_res = supabase.table("yachts") \
            .select("*") \
            .eq("name", yacht_name) \
            .limit(1) \
            .execute()

        if yacht_res.data:
            yacht = yacht_res.data[0]
        else:
            yacht_insert = supabase.table("yachts").insert({
                "name": yacht_name,
                "owner_id": user_id
            }).execute()

            if not yacht_insert.data:
                raise HTTPException(status_code=500, detail="Could not create yacht")

            yacht = yacht_insert.data[0]

        crew_insert = supabase.table("crew").upsert({
            "id": user_id,
            "email": email,
            "full_name": full_name,
            "yacht_id": yacht["id"],
            "security_level": 1,
            "created_by": user_id
        }).execute()

        if not crew_insert.data:
            raise HTTPException(status_code=500, detail="Could not create crew profile")

        crew = crew_insert.data[0]

    now = int(time.time())

    token = pyjwt.encode(
        {
            "sub": user_id,
            "email": email,
            "aud": "authenticated",
            "role": "authenticated",
            "iat": now,
            "exp": now + 60 * 60 * 24 * 30
        },
        SUPABASE_JWT_SECRET,
        algorithm="HS256"
    )

    return {
        "access_token": token,
        "refresh_token": None,
        "token_type": "bearer",
        "user": {
            "id": user_id,
            "email": email
        },
        "crew": crew
    }

def chat_with_runpod_bridgeos(
    query: str,
    crew: dict,
    chat_id: str
):
    """
    Sends the BridgeOS chat request to the RunPod AI API.

    Important:
    - The frontend calls the BridgeOS backend.
    - The BridgeOS backend calls RunPod.
    - Users should never see raw Internal Server Error messages.
    - If RunPod fails, return the normal fallback answer.
    """

    verify_chat_access(
        chat_id=chat_id,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"]
    )

    supabase.table("messages").insert({
        "chat_id": chat_id,
        "yacht_id": crew["yacht_id"],
        "crew_id": crew["id"],
        "role": "user",
        "content": query
    }).execute()

    if not RUNPOD_BASE_URL:
        answer = FALLBACK_NO_DATA_ANSWER

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": crew["yacht_id"],
            "crew_id": crew["id"],
            "role": "assistant",
            "content": answer
        }).execute()

        return {
            "answer": answer,
            "sources": [],
            "provider": "runpod_bridgeos",
            "error": True
        }

    if not BRIDGEOS_API_KEY:
        answer = FALLBACK_NO_DATA_ANSWER

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": crew["yacht_id"],
            "crew_id": crew["id"],
            "role": "assistant",
            "content": answer
        }).execute()

        return {
            "answer": answer,
            "sources": [],
            "provider": "runpod_bridgeos",
            "error": True
        }

    history_res = supabase.table("messages") \
        .select("role, content") \
        .eq("chat_id", chat_id) \
        .eq("crew_id", crew["id"]) \
        .eq("yacht_id", crew["yacht_id"]) \
        .order("created_at", desc=False) \
        .limit(20) \
        .execute()

    history = []

    for message in history_res.data or []:
        role = message.get("role")
        content = message.get("content")

        if role in ["user", "assistant"] and content:
            history.append({
                "role": role,
                "content": content
            })

    backend_context = {
        "crew": {
            "id": crew.get("id"),
            "email": crew.get("email"),
            "full_name": crew.get("full_name"),
            "role": crew.get("role"),
            "position": crew.get("position"),
            "phone_number": crew.get("phone_number"),
            "security_level": crew.get("security_level"),
            "yacht_id": crew.get("yacht_id"),
            "yacht_name": crew.get("yacht_name")
        }
    }

    try:
        my_assets = list_my_assets(crew)

        safe_assets = []

        asset_rows = (
            my_assets.get("data", [])
            if isinstance(my_assets, dict)
            else getattr(my_assets, "data", []) or []
        )

        for asset in asset_rows:
            safe_assets.append({
                "id": asset.get("id"),
                "file_name": asset.get("file_name"),
                "original_file_name": asset.get("original_file_name"),
                "file_type": asset.get("file_type"),
                "mime_type": asset.get("mime_type"),
                "security_level": asset.get("security_level"),
                "processing_status": asset.get("processing_status"),
                "detected_year": asset.get("detected_year"),
                "detected_event": asset.get("detected_event"),
                "tags": asset.get("tags"),
                "summary": asset.get("summary")
            })

        backend_context["assets"] = safe_assets[:50]

    except Exception as e:
        print("ASSET CONTEXT ERROR:", type(e).__name__, str(e))
        backend_context["assets"] = []

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/chat"

    try:
        print("RUNPOD DEBUG: url:", url)
        print("RUNPOD DEBUG: key present:", bool(BRIDGEOS_API_KEY))
        print("RUNPOD DEBUG: key length:", len(BRIDGEOS_API_KEY or ""))
        print("RUNPOD DEBUG: key last4:", (BRIDGEOS_API_KEY or "")[-4:])

        response = requests.post(
            url,
            json={
                "user_input": query,
                "history": history,
                "backend_context": backend_context
            },
            headers={
                "Content-Type": "application/json",
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        print("RUNPOD DEBUG: status:", response.status_code)
        print("RUNPOD DEBUG: response:", response.text[:500])

    except requests.exceptions.Timeout:
        print("RUNPOD TIMEOUT ERROR")

        answer = FALLBACK_NO_DATA_ANSWER

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": crew["yacht_id"],
            "crew_id": crew["id"],
            "role": "assistant",
            "content": answer
        }).execute()

        supabase.table("chats").update({
            "updated_at": "now()"
        }).eq("id", chat_id).eq("crew_id", crew["id"]).eq("yacht_id", crew["yacht_id"]).execute()

        return {
            "answer": answer,
            "sources": [],
            "provider": "runpod_bridgeos",
            "error": True
        }

    except Exception as e:
        print("RUNPOD REQUEST ERROR:", type(e).__name__, str(e))

        answer = FALLBACK_NO_DATA_ANSWER

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": crew["yacht_id"],
            "crew_id": crew["id"],
            "role": "assistant",
            "content": answer
        }).execute()

        supabase.table("chats").update({
            "updated_at": "now()"
        }).eq("id", chat_id).eq("crew_id", crew["id"]).eq("yacht_id", crew["yacht_id"]).execute()

        return {
            "answer": answer,
            "sources": [],
            "provider": "runpod_bridgeos",
            "error": True
        }

    if response.status_code >= 400:
        print("RUNPOD ERROR STATUS:", response.status_code)
        print("RUNPOD ERROR RESPONSE:", response.text[:1000])

        answer = FALLBACK_NO_DATA_ANSWER

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": crew["yacht_id"],
            "crew_id": crew["id"],
            "role": "assistant",
            "content": answer
        }).execute()

        supabase.table("chats").update({
            "updated_at": "now()"
        }).eq("id", chat_id).eq("crew_id", crew["id"]).eq("yacht_id", crew["yacht_id"]).execute()

        return {
            "answer": answer,
            "sources": [],
            "provider": "runpod_bridgeos",
            "error": True
        }

    try:
        data = response.json()
    except Exception:
        print("RUNPOD JSON PARSE ERROR:", response.text[:1000])

        answer = FALLBACK_NO_DATA_ANSWER

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": crew["yacht_id"],
            "crew_id": crew["id"],
            "role": "assistant",
            "content": answer
        }).execute()

        supabase.table("chats").update({
            "updated_at": "now()"
        }).eq("id", chat_id).eq("crew_id", crew["id"]).eq("yacht_id", crew["yacht_id"]).execute()

        return {
            "answer": answer,
            "sources": [],
            "provider": "runpod_bridgeos",
            "error": True
        }

    answer = data.get("response") or FALLBACK_NO_DATA_ANSWER

    supabase.table("messages").insert({
        "chat_id": chat_id,
        "yacht_id": crew["yacht_id"],
        "crew_id": crew["id"],
        "role": "assistant",
        "content": answer
    }).execute()

    supabase.table("chats").update({
        "updated_at": "now()"
    }).eq("id", chat_id).eq("crew_id", crew["id"]).eq("yacht_id", crew["yacht_id"]).execute()

    return {
        "answer": answer,
        "sources": [],
        "provider": "runpod_bridgeos",
        "error": False
    }
# ------------------------
# CREW
# ------------------------
def get_crew(user_id: str):
    crew_res = supabase.table("crew") \
        .select("*") \
        .eq("id", user_id) \
        .execute()

    if not crew_res.data:
        return None

    crew = crew_res.data[0]

    yacht_res = supabase.table("yachts") \
        .select("name") \
        .eq("id", crew["yacht_id"]) \
        .execute()

    crew["yacht_name"] = None

    if yacht_res.data:
        crew["yacht_name"] = yacht_res.data[0].get("name")

    return crew

def create_chat(crew_id: str, yacht_id: str, title: str = "New Chat"):
    """
    Creates a private chat owned by this exact crew member.
    Even if two users have the same yacht_id, they get separate chats.
    """

    res = supabase.table("chats").insert({
        "crew_id": crew_id,
        "yacht_id": yacht_id,
        "title": title or "New Chat"
    }).execute()

    if not res.data:
        raise HTTPException(status_code=500, detail="Could not create chat")

    chat = res.data[0]

    return {
        "chat_id": chat["id"],
        "chat": chat
    }


def list_my_chats(crew_id: str, yacht_id: str):
    """
    Lists only chats owned by this logged-in crew member.
    Same yacht users cannot see each other's chats.
    """

    return supabase.table("chats") \
        .select("*") \
        .eq("crew_id", crew_id) \
        .eq("yacht_id", yacht_id) \
        .order("updated_at", desc=True) \
        .execute()


def verify_chat_access(chat_id: str, crew_id: str, yacht_id: str):
    """
    Blocks access unless the chat belongs to this exact crew member.
    This is the main privacy check.
    """

    res = supabase.table("chats") \
        .select("*") \
        .eq("id", chat_id) \
        .eq("crew_id", crew_id) \
        .eq("yacht_id", yacht_id) \
        .execute()

    if not res.data:
        raise HTTPException(status_code=403, detail="Chat not found or not yours")

    return res.data[0]

def update_chat_title(chat_id: str, crew_id: str, yacht_id: str, title: str):
    """
    Renames a chat only if it belongs to this exact crew member.
    """

    clean_title = (title or "").strip()

    if not clean_title:
        raise HTTPException(status_code=400, detail="Chat title cannot be empty")

    if len(clean_title) > 120:
        clean_title = clean_title[:120]

    verify_chat_access(
        chat_id=chat_id,
        crew_id=crew_id,
        yacht_id=yacht_id
    )

    try:
        res = supabase.table("chats") \
            .update({
                "title": clean_title,
                "updated_at": "now()"
            }) \
            .eq("id", chat_id) \
            .eq("crew_id", crew_id) \
            .eq("yacht_id", yacht_id) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not rename chat: {str(e)}"
        )

    if not res.data:
        raise HTTPException(status_code=404, detail="Chat not found")

    return {
        "message": "Chat renamed successfully",
        "chat": res.data[0]
    }


def delete_chat(chat_id: str, crew_id: str, yacht_id: str):
    """
    Deletes a chat only if it belongs to this exact crew member.
    Also removes its messages first.

    Chat-linked assets are kept, but detached from the deleted chat.
    """

    verify_chat_access(
        chat_id=chat_id,
        crew_id=crew_id,
        yacht_id=yacht_id
    )

    try:
        supabase.table("messages") \
            .delete() \
            .eq("chat_id", chat_id) \
            .eq("crew_id", crew_id) \
            .eq("yacht_id", yacht_id) \
            .execute()

        supabase.table("assets") \
            .update({
                "chat_id": None
            }) \
            .eq("chat_id", chat_id) \
            .eq("yacht_id", yacht_id) \
            .execute()

        res = supabase.table("chats") \
            .delete() \
            .eq("id", chat_id) \
            .eq("crew_id", crew_id) \
            .eq("yacht_id", yacht_id) \
            .execute()

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not delete chat: {str(e)}"
        )

    if not res.data:
        raise HTTPException(status_code=404, detail="Chat not found")

    return {
        "message": "Chat deleted successfully",
        "deleted_chat_id": chat_id
    }


def get_chat_messages(chat_id: str, crew_id: str, yacht_id: str):
    """
    Loads saved messages only if this chat belongs to this crew member.
    """

    verify_chat_access(
        chat_id=chat_id,
        crew_id=crew_id,
        yacht_id=yacht_id
    )

    return supabase.table("messages") \
        .select("*") \
        .eq("chat_id", chat_id) \
        .eq("crew_id", crew_id) \
        .eq("yacht_id", yacht_id) \
        .order("created_at") \
        .execute()

def repair_admin_login(email: str, password: str, full_name: str, yacht_name: str):
    """
    TEMP SETUP / REPAIR LOGIN.

    Use when Supabase Auth user exists, but crew/yacht rows are missing.

    It:
    1. Logs in with Supabase Auth
    2. Gets the real Supabase user id
    3. Creates or reuses yacht
    4. Creates or repairs crew profile with security_level = 1
    5. Returns the normal Supabase access token

    Remove or protect this route after setup.
    """

    try:
        auth_res = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Supabase login failed: {str(e)}"
        )

    if not auth_res or not getattr(auth_res, "session", None):
        raise HTTPException(status_code=401, detail="No Supabase session returned")

    if not auth_res.user:
        raise HTTPException(status_code=401, detail="No Supabase user returned")

    user_id = auth_res.user.id

    crew_res = supabase.table("crew") \
        .select("*") \
        .eq("id", user_id) \
        .execute()

    if crew_res.data:
        crew = crew_res.data[0]

        return {
            "message": "Login successful. Crew profile already exists.",
            "access_token": auth_res.session.access_token,
            "refresh_token": auth_res.session.refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user_id,
                "email": auth_res.user.email or email
            },
            "crew": crew
        }

    yacht_res = supabase.table("yachts") \
        .select("*") \
        .eq("owner_id", user_id) \
        .limit(1) \
        .execute()

    if yacht_res.data:
        yacht = yacht_res.data[0]
    else:
        yacht_insert = supabase.table("yachts").insert({
            "name": yacht_name,
            "owner_id": user_id
        }).execute()

        if not yacht_insert.data:
            raise HTTPException(status_code=500, detail="Could not create yacht row")

        yacht = yacht_insert.data[0]

    crew_insert = supabase.table("crew").insert({
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "yacht_id": yacht["id"],
        "security_level": 1,
        "created_by": user_id
    }).execute()

    if not crew_insert.data:
        raise HTTPException(status_code=500, detail="Could not create crew row")

    return {
        "message": "Login successful. Admin crew profile repaired.",
        "access_token": auth_res.session.access_token,
        "refresh_token": auth_res.session.refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user_id,
            "email": auth_res.user.email or email
        },
        "crew": crew_insert.data[0],
        "yacht": yacht
    }


def create_crew(data: dict):
    """
    Basic direct crew creation.
    You may not need this after using create_crew_user().
    """

    return supabase.table("crew").insert(data).execute()


def create_crew_user(
    admin_crew: dict,
    email: str,
    password: str,
    full_name: str,
    security_level: int,
    position: str | None = None,
    phone_number: str | None = None
):
    """
    MAIN account creates SUB accounts under the SAME yacht.

    MAIN decides security_level:
    1 = can access all yacht assets/docs
    2 = limited, only explicitly granted assets/docs
    3 = limited, only explicitly granted assets/docs
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 main accounts can create sub accounts"
        )

    security_level = int(security_level)

    if security_level not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="security_level must be 1, 2, or 3"
        )

    try:
        auth_res = auth_admin.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True
        })
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create Supabase Auth user: {str(e)}"
        )

    if not auth_res.user:
        raise HTTPException(status_code=400, detail="Could not create Supabase Auth user")

    user_id = auth_res.user.id
    try:
        crew_res = supabase.table("crew").insert({
            "id": user_id,
            "email": email,
            "full_name": full_name,
            "yacht_id": admin_crew["yacht_id"],
            "security_level": security_level,
            "position": position,
            "phone_number": phone_number,
            "created_by": admin_crew["id"]
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create crew profile: {str(e)}"
        )

    if not crew_res.data:
        raise HTTPException(status_code=400, detail="Could not create crew profile")

    return {
        "message": "Sub account created successfully",
        "account_type": "sub_account",
        "main_account_id": admin_crew["id"],
        "yacht_id": admin_crew["yacht_id"],
        "sub_user_id": user_id,
        "sub_security_level": security_level,
        "crew": crew_res.data[0]
    }
def list_crew_for_yacht(admin_crew: dict):
    """
    Admin can see all crew for their yacht.
    """

    if admin_crew["security_level"] != 1:
        raise HTTPException(status_code=403, detail="Only security level 1 can list crew")

    return supabase.table("crew") \
        .select("*") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

def _require_admin_level_1(admin_crew: dict):
    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can manage users"
        )


def _get_target_crew_for_admin(admin_crew: dict, target_crew_id: str):
    target_res = supabase.table("crew") \
        .select("*") \
        .eq("id", target_crew_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not target_res.data:
        raise HTTPException(
            status_code=404,
            detail="Crew member not found for this yacht"
        )

    return target_res.data[0]


def update_crew_user(
    admin_crew: dict,
    target_crew_id: str,
    email: str | None = None,
    full_name: str | None = None,
    security_level: int | None = None,
    position: str | None = None,
    phone_number: str | None = None
):
    """
    Tier 1 admin updates a crew user's profile.
    Also updates Supabase Auth email if email is changed.
    """

    _require_admin_level_1(admin_crew)

    target_crew = _get_target_crew_for_admin(
        admin_crew=admin_crew,
        target_crew_id=target_crew_id
    )

    updates = {}

    if email is not None:
        updates["email"] = email

    if full_name is not None:
        updates["full_name"] = full_name

    if security_level is not None:
        security_level = int(security_level)

        if security_level not in [1, 2, 3]:
            raise HTTPException(
                status_code=400,
                detail="security_level must be 1, 2, or 3"
            )

        updates["security_level"] = security_level

    if position is not None:
        updates["position"] = position

    if phone_number is not None:
        updates["phone_number"] = phone_number

    if not updates:
        return {
            "message": "No changes provided",
            "crew": target_crew
        }

    if email is not None:
        try:
            supabase.auth.admin.update_user_by_id(
                target_crew_id,
                {
                    "email": email,
                    "email_confirm": True
                }
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Could not update Supabase Auth email: {str(e)}"
            )

    try:
        crew_res = supabase.table("crew") \
            .update(updates) \
            .eq("id", target_crew_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not update crew profile: {str(e)}"
        )

    if not crew_res.data:
        raise HTTPException(
            status_code=400,
            detail="Could not update crew profile"
        )

    return {
        "message": "User updated successfully",
        "crew": crew_res.data[0]
    }

def reset_my_password(
    crew: dict,
    new_password: str
):
    """
    Logged-in user resets their own password.

    Safe behavior:
    - Updates password in Supabase Auth only.
    - Does NOT store the real password in your database.
    - Stores audit metadata in crew table.
    """

    if not new_password or len(new_password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters"
        )

    crew_id = crew["id"]
    now = datetime.now(timezone.utc).isoformat()

    try:
        auth_admin.auth.admin.update_user_by_id(
            crew_id,
            {
                "password": new_password
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not update password in Supabase Auth: {str(e)}"
        )

    try:
        crew_res = supabase.table("crew") \
            .update({
                "email": crew.get("email"),
                "password_updated_at": now,
                "password_updated_by": crew_id,
                "password_reset_by_role": "self",
                "must_change_password": False
            }) \
            .eq("id", crew_id) \
            .eq("yacht_id", crew["yacht_id"]) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=(
                "Password was updated in Supabase Auth, but database sync failed: "
                f"{str(e)}"
            )
        )

    if not crew_res.data:
        raise HTTPException(
            status_code=400,
            detail="Password was updated, but crew database sync returned no data"
        )

    return {
        "message": "Password reset successfully",
        "password_updated_at": now,
        "crew": crew_res.data[0]
    }

def reset_crew_password(
    admin_crew: dict,
    target_crew_id: str,
    new_password: str
):
    """
    Tier 1 admin resets a crew user's Supabase Auth password.

    Safe behavior:
    - Updates password in Supabase Auth only.
    - Does NOT store the real password.
    - Stores audit metadata in crew table.
    """

    _require_admin_level_1(admin_crew)

    target_crew = _get_target_crew_for_admin(
        admin_crew=admin_crew,
        target_crew_id=target_crew_id
    )

    if not new_password or len(new_password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters"
        )

    now = datetime.now(timezone.utc).isoformat()

    try:
        auth_admin.auth.admin.update_user_by_id(
            target_crew_id,
            {
                "password": new_password
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not reset password in Supabase Auth: {str(e)}"
        )

    try:
        crew_res = supabase.table("crew") \
            .update({
                "password_updated_at": now,
                "password_updated_by": admin_crew["id"],
                "password_reset_by_role": "admin",
                "must_change_password": True
            }) \
            .eq("id", target_crew_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=(
                "Password was reset in Supabase Auth, but crew database sync failed: "
                f"{str(e)}"
            )
        )

    if not crew_res.data:
        raise HTTPException(
            status_code=400,
            detail="Password was reset, but crew database sync returned no data"
        )

    return {
        "message": "Password reset successfully",
        "password_updated_at": now,
        "target_crew": crew_res.data[0]
    }
def delete_crew_user(
    admin_crew: dict,
    target_crew_id: str
):
    """
    Tier 1 admin deletes a crew user from:
    1. crew table
    2. Supabase Auth

    Admin cannot delete themselves.
    """

    _require_admin_level_1(admin_crew)

    if target_crew_id == admin_crew["id"]:
        raise HTTPException(
            status_code=400,
            detail="You cannot delete your own admin account"
        )

    target_crew = _get_target_crew_for_admin(
        admin_crew=admin_crew,
        target_crew_id=target_crew_id
    )

    try:
        supabase.table("crew") \
            .delete() \
            .eq("id", target_crew_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not delete crew profile: {str(e)}"
        )

    try:
        supabase.auth.admin.delete_user(target_crew_id)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=(
                "Crew profile was deleted, but Supabase Auth user could not be deleted: "
                f"{str(e)}"
            )
        )

    return {
        "message": "User deleted successfully",
        "deleted_user": {
            "id": target_crew_id,
            "email": target_crew.get("email")
        }
    }

def get_accessible_asset_ids(crew_id: str, yacht_id: str, security_level: int):
    """
    Level 1:
        Can access all assets for the yacht.

    Level 2 and 3:
        Can only access assets explicitly granted in asset_access.
    """

    security_level = int(security_level)

    if security_level == 1:
        assets = supabase.table("assets") \
            .select("id") \
            .eq("yacht_id", yacht_id) \
            .execute()

        return [asset["id"] for asset in assets.data]

    access = supabase.table("asset_access") \
        .select("asset_id, assets!inner(yacht_id)") \
        .eq("crew_id", crew_id) \
        .eq("assets.yacht_id", yacht_id) \
        .execute()

    return [row["asset_id"] for row in access.data]


def authorize_asset_access(
    asset_id: str,
    target_crew_id: str,
    granted_by: str,
    yacht_id: str
):
    asset_res = supabase.table("assets") \
        .select("*") \
        .eq("id", asset_id) \
        .eq("yacht_id", yacht_id) \
        .execute()

    if not asset_res.data:
        raise HTTPException(status_code=404, detail="Asset not found for this yacht")

    crew_res = supabase.table("crew") \
        .select("*") \
        .eq("id", target_crew_id) \
        .eq("yacht_id", yacht_id) \
        .execute()

    if not crew_res.data:
        raise HTTPException(status_code=404, detail="Crew member not found for this yacht")

    return supabase.table("asset_access").upsert({
        "asset_id": asset_id,
        "crew_id": target_crew_id,
        "granted_by": granted_by
    }).execute()



def list_assets_for_admin(admin_crew: dict):
    if admin_crew["security_level"] != 1:
        raise HTTPException(status_code=403, detail="Only security level 1 can list assets")

    return supabase.table("assets") \
        .select("*") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .order("created_at", desc=True) \
        .execute()


def list_my_assets(crew: dict):
    asset_ids = get_accessible_asset_ids(
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        security_level=crew["security_level"]
    )

    if not asset_ids:
        return {"data": []}

    return supabase.table("assets") \
        .select("*") \
        .in_("id", asset_ids) \
        .eq("yacht_id", crew["yacht_id"]) \
        .order("created_at", desc=True) \
        .execute()


def get_asset_status(asset_id: str, yacht_id: str):
    res = supabase.table("assets") \
        .select("id, file_name, processing_status, processing_error") \
        .eq("id", asset_id) \
        .eq("yacht_id", yacht_id) \
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    return res.data[0]

def create_asset_signed_url(asset_id: str, crew: dict):
    """
    Creates a temporary signed URL for a private asset.

    Security:
    - Checks the asset is accessible to this crew member.
    - Checks the asset belongs to the same yacht.
    - Does not expose permanent public URLs.
    """

    accessible_asset_ids = get_accessible_asset_ids(
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        security_level=crew["security_level"]
    )

    if asset_id not in accessible_asset_ids:
        raise HTTPException(status_code=403, detail="No access to this asset")

    asset_res = supabase.table("assets") \
        .select("id, yacht_id, storage_path") \
        .eq("id", asset_id) \
        .eq("yacht_id", crew["yacht_id"]) \
        .execute()

    if not asset_res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset = asset_res.data[0]

    signed = storage_admin.storage.from_(BUCKET_NAME).create_signed_url(
        asset["storage_path"],
        60 * 5
    )

    signed_url = signed.get("signedURL") or signed.get("signed_url")

    if not signed_url:
        raise HTTPException(
            status_code=500,
            detail="Could not create signed URL"
        )

    return {
        "asset_id": asset_id,
        "signed_url": signed_url
    }

def get_asset_for_download(asset_id: str, crew: dict):
    """
    Gets an asset only if this crew member has access to it.
    Used by the asset download endpoint.
    """

    accessible_asset_ids = get_accessible_asset_ids(
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        security_level=crew["security_level"]
    )

    if asset_id not in accessible_asset_ids:
        raise HTTPException(status_code=403, detail="No access to this asset")

    res = supabase.table("assets") \
        .select("*") \
        .eq("id", asset_id) \
        .eq("yacht_id", crew["yacht_id"]) \
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    return res.data[0]

def seed_text_asset(
    file_name: str,
    content: str,
    yacht_id: str,
    uploaded_by: str,
    security_level: int = 1
):
    """
    TEMP DEV FUNCTION.

    Creates a searchable text asset directly in the database,
    without using file upload or Supabase Storage.

    Remove before production.
    """

    import hashlib

    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="Content is required")

    security_level = int(security_level)

    if security_level not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="security_level must be 1, 2, or 3"
        )

    clean_filename = safe_filename(file_name or "seeded_asset.txt")
    unique_id = str(uuid.uuid4())

    file_hash = hashlib.sha256(
        f"{yacht_id}:{clean_filename}:{content}".encode("utf-8")
    ).hexdigest()

    detected_year = extract_year_from_text(content)
    detected_event = detect_event(content)
    tags = generate_basic_tags(content)

    storage_path = f"{yacht_id}/seeded/{unique_id}-{clean_filename}"

    try:
        existing = supabase.table("assets") \
            .select("*") \
            .eq("yacht_id", yacht_id) \
            .eq("file_hash", file_hash) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not check duplicate seeded asset: {str(e)}"
        )

    if existing.data:
        return {
            "message": "Seeded asset already exists",
            "asset": existing.data[0],
            "duplicate": True
        }

    try:
        asset_res = supabase.table("assets").insert({
            "yacht_id": yacht_id,
            "chat_id": None,
            "uploaded_by": uploaded_by,
            "security_level": security_level,
            "file_name": clean_filename,
            "original_file_name": clean_filename,
            "original_relative_path": None,
            "file_hash": file_hash,
            "file_type": "text",
            "mime_type": "text/plain",
            "storage_path": storage_path,
            "file_url": None,
            "extracted_text": content,
            "visual_description": None,
            "ocr_text": None,
            "detected_date": None,
            "detected_year": detected_year,
            "detected_month": None,
            "detected_day": None,
            "date_source": None,
            "detected_event": detected_event,
            "tags": tags,
            "summary": content[:1500],
            "processing_status": "processed",
            "processing_error": None
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not insert seeded asset row: {str(e)}"
        )

    if not asset_res.data:
        raise HTTPException(status_code=500, detail="Could not save seeded asset")

    asset = asset_res.data[0]

    rows = []

    metadata_content = f"""
File name: {clean_filename}
File type: text
Detected year: {detected_year or ""}
Tags: {", ".join(tags)}
""".strip()

    rows.append({
        "asset_id": asset["id"],
        "yacht_id": yacht_id,
        "chat_id": None,
        "security_level": security_level,
        "content": metadata_content,
        "content_type": "metadata",
        "chunk_index": 0,
        "detected_date": None,
        "detected_year": detected_year,
        "tags": tags,
        "embedding": embed(metadata_content)
    })

    for index, chunk in enumerate(chunk_text(content)):
        rows.append({
            "asset_id": asset["id"],
            "yacht_id": yacht_id,
            "chat_id": None,
            "security_level": security_level,
            "content": chunk,
            "content_type": "text",
            "chunk_index": index,
            "detected_date": None,
            "detected_year": detected_year,
            "tags": tags,
            "embedding": embed(chunk)
        })

    try:
        supabase.table("asset_chunks").insert(rows).execute()
    except Exception as e:
        supabase.table("assets").update({
            "processing_status": "failed",
            "processing_error": f"Could not insert seeded chunks: {str(e)}"
        }).eq("id", asset["id"]).execute()

        raise HTTPException(
            status_code=500,
            detail=f"Could not insert seeded asset chunks: {str(e)}"
        )

    return {
        "message": "Seeded asset created successfully",
        "asset": asset,
        "chunks_created": len(rows),
        "duplicate": False
    }

def upload_pending_document(
    file,
    filename: str,
    mime_type: str | None,
    yacht_id: str,
    uploaded_by: str
):
    """
    Uploads a Yacht Documentation file into the gray zone only.

    This does NOT:
    - create an assets row
    - create asset_chunks
    - create embeddings
    - make the file searchable by the chatbot
    """

    clean_filename = safe_filename(filename or "pending-document")
    unique_id = str(uuid.uuid4())

    try:
        file.seek(0)
        file_bytes = file.read()
        file.seek(0)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read uploaded file: {str(e)}"
        )

    storage_path = f"{yacht_id}/pending-documents/{unique_id}-{clean_filename}"

    try:
        storage_admin.storage.from_(BUCKET_NAME).upload(
            storage_path,
            file_bytes,
            file_options={
                "content-type": mime_type or "application/octet-stream",
                "upsert": "false"
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not upload pending document to storage: {str(e)}"
        )

    try:
        res = supabase.table("pending_documents").insert({
            "yacht_id": yacht_id,
            "uploaded_by": uploaded_by,
            "file_name": clean_filename,
            "original_file_name": filename,
            "mime_type": mime_type,
            "storage_path": storage_path,
            "file_size": len(file_bytes),
            "status": "pending"
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not save pending document row: {str(e)}"
        )

    if not res.data:
        raise HTTPException(
            status_code=500,
            detail="Could not save pending document. Supabase returned no data."
        )

    return {
        "message": "Document uploaded to gray zone for review",
        "pending_document": res.data[0]
    }


def list_pending_documents(admin_crew: dict):
    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can list pending documents"
        )

    return supabase.table("pending_documents") \
        .select("""
            *,
            yachts:yacht_id (
                id,
                name
            ),
            crew:uploaded_by (
                id,
                email,
                full_name,
                security_level
            )
        """) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .order("created_at", desc=True) \
        .execute()
    
def create_pending_document_signed_url(
    pending_document_id: str,
    admin_crew: dict
):
    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can download pending documents"
        )

    res = supabase.table("pending_documents") \
        .select("*") \
        .eq("id", pending_document_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not res.data:
        raise HTTPException(
            status_code=404,
            detail="Pending document not found"
        )

    pending_doc = res.data[0]

    signed = storage_admin.storage.from_(BUCKET_NAME).create_signed_url(
        pending_doc["storage_path"],
        60 * 5
    )

    signed_url = signed.get("signedURL") or signed.get("signed_url")

    if not signed_url:
        raise HTTPException(
            status_code=500,
            detail="Could not create signed URL"
        )

    return {
        "pending_document_id": pending_document_id,
        "signed_url": signed_url
    }

def get_pending_document_for_download(
    pending_document_id: str,
    admin_crew: dict
):
    """
    Gets a pending document only if it belongs to the admin's yacht.
    Used by the download endpoint.
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can download pending documents"
        )

    res = supabase.table("pending_documents") \
        .select("""
            *,
            yachts:yacht_id (
                id,
                name
            ),
            crew:uploaded_by (
                id,
                email,
                full_name
            )
        """) \
        .eq("id", pending_document_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not res.data:
        raise HTTPException(
            status_code=404,
            detail="Pending document not found"
        )

    return res.data[0]

def upload_asset(
    file,
    filename: str,
    yacht_id: str,
    uploaded_by: str,
    mime_type: str | None = None,
    original_relative_path: str | None = None,
    chat_id: str | None = None,
    security_level: int = 1
):
    """
    Uploads any file, stores it in Supabase Storage, creates an asset row,
    extracts/processes it, creates chunks and embeddings.
    """

    clean_filename = safe_filename(filename)
    file_type = detect_file_type(clean_filename, mime_type)
    security_level = int(security_level)

    if security_level not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="security_level must be 1, 2, or 3"
        )
        
    if chat_id:
        verify_chat_access(
            chat_id=chat_id,
            crew_id=uploaded_by,
            yacht_id=yacht_id
        )

    try:
        file_hash = calculate_file_hash(file)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not calculate file hash: {str(e)}"
        )

    try:
        existing_query = supabase.table("assets") \
            .select("*") \
            .eq("yacht_id", yacht_id) \
            .eq("file_hash", file_hash)

        if chat_id:
            existing_query = existing_query.eq("chat_id", chat_id)
        else:
            existing_query = existing_query.is_("chat_id", "null")

        existing = existing_query.execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not check duplicate asset: {str(e)}"
        )

    if existing.data:
        return {
            "message": "Asset already exists",
            "asset": existing.data[0],
            "duplicate": True
        }

    unique_id = str(uuid.uuid4())

    if chat_id:
        path = f"{yacht_id}/chats/{chat_id}/assets/{unique_id}-{clean_filename}"
    else:
        path = f"{yacht_id}/assets/{unique_id}-{clean_filename}"

    try:
        file.seek(0)
        file_bytes = file.read()
        file.seek(0)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read uploaded file: {str(e)}"
        )

    try:
        storage_admin.storage.from_(BUCKET_NAME).upload(
            path,
            file_bytes,
            file_options={
                "content-type": mime_type or "application/octet-stream",
                "upsert": "true"
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Asset upload failed at Supabase Storage. "
                f"Bucket: {BUCKET_NAME}. "
                f"Path: {path}. "
                f"Error: {str(e)}"
            )
        )

    url = None

    try:
        asset_res = supabase.table("assets").insert({
            "yacht_id": yacht_id,
            "chat_id": chat_id,
            "uploaded_by": uploaded_by,
            "security_level": security_level,
            "file_name": clean_filename,
            "original_file_name": filename,
            "original_relative_path": original_relative_path,
            "file_hash": file_hash,
            "file_type": file_type,
            "mime_type": mime_type,
            "storage_path": path,
            "file_url": url,
            "processing_status": "pending"
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not insert asset row. Check assets table columns. Error: {str(e)}"
        )

    if not asset_res.data:
        raise HTTPException(status_code=500, detail="Could not save asset. Supabase returned no data.")

    asset = asset_res.data[0]

    try:
        import io
        processing_file = io.BytesIO(file_bytes)

        process_uploaded_asset(
            asset_id=asset["id"],
            file=processing_file,
            filename=clean_filename,
            file_type=file_type,
            yacht_id=yacht_id,
            chat_id=chat_id,
            security_level=security_level
        )
    except Exception as e:
        supabase.table("assets").update({
            "processing_status": "failed",
            "processing_error": f"{type(e).__name__}: {str(e)}"
        }).eq("id", asset["id"]).execute()

        raise HTTPException(
            status_code=500,
            detail=f"Asset was uploaded but processing failed: {type(e).__name__}: {str(e)}"
        )

    try:
        updated = supabase.table("assets") \
            .select("*") \
            .eq("id", asset["id"]) \
            .single() \
            .execute()

        return {
            "message": "Asset uploaded and processed successfully",
            "asset": updated.data,
            "duplicate": False
        }

    except Exception:
        return {
            "message": "Asset uploaded and processed successfully",
            "asset": asset,
            "duplicate": False
        }



def process_uploaded_asset(
    asset_id: str,
    file,
    filename: str,
    file_type: str,
    yacht_id: str,
    chat_id: str | None = None,
    security_level: int = 1
):
    """
    Converts a raw uploaded file into searchable memory.
    """

    try:
        supabase.table("assets").update({
            "processing_status": "processing",
            "processing_error": None
        }).eq("id", asset_id).execute()

        extracted_text = ""
        visual_description = ""
        ocr_text = ""

        if file_type in ["text", "pdf", "docx"]:
            file.seek(0)
            extracted_text = extract_text_by_file_type(
                file=file,
                filename=filename,
                file_type=file_type
            )

        if file_type == "image":
            file.seek(0)
            visual_description = describe_image(file, filename)

            file.seek(0)
            ocr_text = extract_ocr_from_image(file, filename)

        combined_text = "\n\n".join([
            f"File name: {filename}",
            f"File type: {file_type}",
            f"Extracted text:\n{extracted_text}" if extracted_text else "",
            f"Image visual description:\n{visual_description}" if visual_description else "",
            f"OCR text:\n{ocr_text}" if ocr_text else ""
        ]).strip()

        detected_date, date_source = extract_date_from_filename(filename)

        detected_year = None
        detected_month = None
        detected_day = None

        if detected_date:
            detected_year = detected_date.year
            detected_month = detected_date.month
            detected_day = detected_date.day
        else:
            detected_year = extract_year_from_text(combined_text)

        detected_event = detect_event(combined_text)
        tags = generate_basic_tags(combined_text)

        summary = combined_text[:1500] if combined_text else None

        supabase.table("assets").update({
            "extracted_text": extracted_text or None,
            "visual_description": visual_description or None,
            "ocr_text": ocr_text or None,
            "detected_date": detected_date.isoformat() if detected_date else None,
            "detected_year": detected_year,
            "detected_month": detected_month,
            "detected_day": detected_day,
            "date_source": date_source,
            "detected_event": detected_event,
            "tags": tags,
            "summary": summary,
            "processing_status": "processed"
        }).eq("id", asset_id).execute()

        create_asset_chunks(
            asset_id=asset_id,
            yacht_id=yacht_id,
            chat_id=chat_id,
            filename=filename,
            file_type=file_type,
            extracted_text=extracted_text,
            visual_description=visual_description,
            ocr_text=ocr_text,
            detected_date=detected_date,
            detected_year=detected_year,
            tags=tags,
            security_level=security_level
        )

    except Exception as e:
        supabase.table("assets").update({
            "processing_status": "failed",
            "processing_error": str(e)
        }).eq("id", asset_id).execute()

        raise

def create_asset_chunks(
    asset_id: str,
    yacht_id: str,
    chat_id: str | None,
    filename: str,
    file_type: str,
    extracted_text: str = "",
    visual_description: str = "",
    ocr_text: str = "",
    detected_date=None,
    detected_year: int | None = None,
    tags: list[str] | None = None,
    security_level: int = 1
):
    """
    Creates searchable chunks for asset metadata, text, OCR, and image captions.
    """

    tags = tags or []
    rows = []

    metadata_content = f"""
File name: {filename}
File type: {file_type}
Detected year: {detected_year or ""}
Tags: {", ".join(tags)}
""".strip()

    rows.append({
        "asset_id": asset_id,
        "yacht_id": yacht_id,
        "chat_id": chat_id,
        "security_level": security_level,
        "content": metadata_content,
        "content_type": "metadata",
        "chunk_index": 0,
        "detected_date": detected_date.isoformat() if detected_date else None,
        "detected_year": detected_year,
        "tags": tags,
        "embedding": embed(metadata_content)
    })

    if visual_description:
        content = f"""
Image visual description:
{visual_description}

File name: {filename}
Detected year: {detected_year or ""}
Tags: {", ".join(tags)}
""".strip()

        rows.append({
            "asset_id": asset_id,
            "yacht_id": yacht_id,
            "chat_id": chat_id,
            "security_level": security_level,
            "content": content,
            "content_type": "image_caption",
            "chunk_index": 0,
            "detected_date": detected_date.isoformat() if detected_date else None,
            "detected_year": detected_year,
            "tags": tags,
            "embedding": embed(content)
        })

    if ocr_text:
        for index, chunk in enumerate(chunk_text(ocr_text)):
            rows.append({
                "asset_id": asset_id,
                "yacht_id": yacht_id,
                "chat_id": chat_id,
                "security_level": security_level,
                "content": chunk,
                "content_type": "ocr",
                "chunk_index": index,
                "detected_date": detected_date.isoformat() if detected_date else None,
                "detected_year": detected_year,
                "tags": tags,
                "embedding": embed(chunk)
            })

    if extracted_text:
        for index, chunk in enumerate(chunk_text(extracted_text)):
            rows.append({
                "asset_id": asset_id,
                "yacht_id": yacht_id,
                "chat_id": chat_id,
                "security_level": security_level,
                "content": chunk,
                "content_type": "text",
                "chunk_index": index,
                "detected_date": detected_date.isoformat() if detected_date else None,
                "detected_year": detected_year,
                "tags": tags,
                "embedding": embed(chunk)
            })

    if rows:
        supabase.table("asset_chunks").insert(rows).execute()

def build_context_from_asset_results(results: list[dict]) -> str:
    """
    Builds the private context sent to the LLM.

    Important:
    - Do not include file_url.
    - Do not include storage_path.
    - Only include text/chunks that already passed yacht/user access checks.
    """

    parts = []

    for index, row in enumerate(results, start=1):
        part = f"""
SOURCE {index}
File name: {row.get("file_name")}
File type: {row.get("file_type")}
Content type: {row.get("content_type")}
Detected year: {row.get("detected_year")}

Content:
{row.get("content")}
""".strip()

        parts.append(part)

    return "\n\n---\n\n".join(parts)

def build_sources_from_asset_results(results: list[dict]) -> list[dict]:
    """
    Builds the sources returned to the frontend.

    Important:
    - Do not return file_url.
    - Do not return storage_path.
    - The frontend should only receive safe metadata.
    - If the frontend needs to open the file, it must call the signed-url endpoint.
    """

    seen = set()
    sources = []

    for row in results:
        asset_id = row.get("asset_id")

        if asset_id in seen:
            continue

        seen.add(asset_id)

        sources.append({
            "asset_id": asset_id,
            "file_name": row.get("file_name"),
            "file_type": row.get("file_type"),
            "content_type": row.get("content_type"),
            "detected_year": row.get("detected_year"),
            "matched_content": row.get("content")
        })

    return sources

# ------------------------
# DOCUMENT ACCESS
# ------------------------

def authorize_document_access(
    document_id: str,
    target_crew_id: str,
    granted_by: str,
    yacht_id: str
):
    """
    Checks:
    1. The document belongs to the same yacht.
    2. The target crew member belongs to the same yacht.
    3. Then grants document access.
    """

    doc_res = supabase.table("documents") \
        .select("*") \
        .eq("id", document_id) \
        .eq("yacht_id", yacht_id) \
        .execute()

    if not doc_res.data:
        raise HTTPException(status_code=404, detail="Document not found for this yacht")

    crew_res = supabase.table("crew") \
        .select("*") \
        .eq("id", target_crew_id) \
        .eq("yacht_id", yacht_id) \
        .execute()

    if not crew_res.data:
        raise HTTPException(status_code=404, detail="Crew member not found for this yacht")

    return supabase.table("document_access").upsert({
        "document_id": document_id,
        "crew_id": target_crew_id,
        "granted_by": granted_by
    }).execute()


def get_accessible_asset_ids(crew_id: str, yacht_id: str, security_level: int):
    """
    File security model:

    User Tier 1:
        Can access files with security_level 1, 2, or 3.

    User Tier 2:
        Can access files with security_level 2 or 3.

    User Tier 3:
        Can access files with security_level 3 only.

    Extra explicit access:
        Level 2/3 users may also access assets granted in asset_access.
    """

    security_level = int(security_level)

    if security_level not in [1, 2, 3]:
        return []

    # Tier rule:
    # lower number = more powerful user
    # lower number file = more sensitive file
    base_assets = supabase.table("assets") \
        .select("id") \
        .eq("yacht_id", yacht_id) \
        .gte("security_level", security_level) \
        .execute()

    allowed_ids = {asset["id"] for asset in (base_assets.data or [])}

    # Optional explicit grants.
    # Useful if a Tier 3 user needs one specific Tier 1 or Tier 2 file.
    access = supabase.table("asset_access") \
        .select("asset_id, assets!inner(yacht_id)") \
        .eq("crew_id", crew_id) \
        .eq("assets.yacht_id", yacht_id) \
        .execute()

    for row in access.data or []:
        allowed_ids.add(row["asset_id"])

    return list(allowed_ids)

def list_documents_for_admin(admin_crew: dict):
    """
    Admin can list all documents for their yacht.
    """

    if admin_crew["security_level"] != 1:
        raise HTTPException(status_code=403, detail="Only security level 1 can list documents")

    return supabase.table("documents") \
        .select("*") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .order("created_at", desc=True) \
        .execute()


def list_my_documents(crew: dict):
    """
    Level 1 gets all yacht documents.
    Level 2 and 3 get only authorized documents.
    """

    document_ids = get_accessible_document_ids(
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        security_level=crew["security_level"]
    )

    if not document_ids:
        return {"data": []}

    return supabase.table("documents") \
        .select("*") \
        .in_("id", document_ids) \
        .eq("yacht_id", crew["yacht_id"]) \
        .order("created_at", desc=True) \
        .execute()


# ------------------------
# DOCUMENT TEXT CHUNKING
# ------------------------

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150):
    """
    Splits document text into overlapping chunks.
    """

    if not text:
        return []

    chunks = []
    start = 0
    text_length = len(text)

    while start < text_length:
        end = start + chunk_size
        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        start += chunk_size - overlap

    return chunks


def save_document_chunks(document_id: str, yacht_id: str, text: str):
    """
    Saves chunks and embeddings into document_chunks.
    """

    chunks = chunk_text(text)

    if not chunks:
        return None

    rows = []

    for chunk in chunks:
        rows.append({
            "document_id": document_id,
            "yacht_id": yacht_id,
            "content": chunk,
            "embedding": embed(chunk)
        })

    return supabase.table("document_chunks").insert(rows).execute()


def extract_text_from_uploaded_file(file, filename: str):
    """
    First-step version:
    Supports .txt files.

    Later we can add:
    - PDF extraction
    - DOCX extraction
    - image OCR
    """

    file.seek(0)
    raw = file.read()

    if filename.lower().endswith(".txt"):
        return raw.decode("utf-8", errors="ignore")

    return ""


# ------------------------
# DOCUMENTS
# ------------------------

def upload_document(file, filename: str, yacht_id: str, uploaded_by: str):
    return upload_asset(
        file=file,
        filename=filename,
        yacht_id=yacht_id,
        uploaded_by=uploaded_by,
        mime_type=None
    )

# ------------------------
# IMAGES
# ------------------------

def upload_image(file, filename: str, yacht_id: str, uploaded_by: str):
    return upload_asset(
        file=file,
        filename=filename,
        yacht_id=yacht_id,
        uploaded_by=uploaded_by,
        mime_type=None
    )

# ------------------------
# CHAT SECURE
# ------------------------
def chat(
    query: str,
    crew_id: str,
    yacht_id: str,
    security_level: int,
    chat_id: str
):
    """
    Hybrid chat flow.

    Behavior:
    - Always answers normally.
    - Searches the yacht database first.
    - If the database has a relevant answer, database content has priority.
    - If no good database match exists, answer normally from the model.
    - If a document was used, return sources and append a document reference.
    """

    chat_row = verify_chat_access(
        chat_id=chat_id,
        crew_id=crew_id,
        yacht_id=yacht_id
    )

    supabase.table("messages").insert({
        "chat_id": chat_id,
        "yacht_id": yacht_id,
        "crew_id": crew_id,
        "role": "user",
        "content": query
    }).execute()

    if chat_row.get("title") == "New Chat":
        supabase.table("chats").update({
            "title": query[:60],
            "updated_at": "now()"
        }).eq("id", chat_id).eq("crew_id", crew_id).eq("yacht_id", yacht_id).execute()

    accessible_asset_ids = get_accessible_asset_ids(
        crew_id=crew_id,
        yacht_id=yacht_id,
        security_level=security_level
    )

    results = None
    sources = []
    context = ""

    if accessible_asset_ids:
        assets_res = supabase.table("assets") \
            .select("id") \
            .eq("yacht_id", yacht_id) \
            .in_("id", accessible_asset_ids) \
            .execute()

        allowed_asset_ids = [asset["id"] for asset in (assets_res.data or [])]

        if allowed_asset_ids:
            filters = extract_query_filters(query)
            year_filter = filters.get("year")

            results = supabase.rpc("match_asset_chunks_secure", {
                "query_embedding": embed(query),
                "match_count": 6,
                "allowed_asset_ids": allowed_asset_ids,
                "yacht_filter": yacht_id,
                "year_filter": year_filter
            }).execute()

            matched_rows = results.data or []

            # IMPORTANT:
            # Only use database context when the match is actually relevant.
            # Adjust threshold if your Supabase function returns similarity/distance differently.
            relevant_rows = []
            for row in matched_rows:
                similarity = row.get("similarity")
                distance = row.get("distance")

                if similarity is not None:
                    if float(similarity) >= 0.72:
                        relevant_rows.append(row)
                elif distance is not None:
                    if float(distance) <= 0.35:
                        relevant_rows.append(row)
                else:
                    # If your RPC does not return similarity/distance,
                    # keep the row, but limit the number of chunks.
                    relevant_rows.append(row)

            if relevant_rows:
                context = build_context_from_asset_results(relevant_rows[:4])
                sources = build_sources_from_asset_results(relevant_rows[:4])

    if context.strip():
        answer = ask_llm(
            query=query,
            context=f"""
You are BridgeOS.

Answer the user's question naturally.

Use the DATABASE CONTEXT below only if it directly answers the question.
The database has priority over general knowledge when it is relevant.
Do not force database context into unrelated questions.
If you use database context, end the answer with:

Document reference: <file name>

DATABASE CONTEXT:
{context}
""".strip()
        )

        if sources:
            first_source = sources[0]
            file_name = first_source.get("file_name") or "Uploaded document"

            if "Document reference:" not in answer:
                answer = f"{answer}\n\nDocument reference: {file_name}"

    else:
        # No useful database match: answer normally.
        answer = ask_llm(
            query=query,
            context="""
You are BridgeOS.

Answer the user's question normally and helpfully.
There is no relevant yacht database/document context for this question.
Do not mention documents or sources.
""".strip()
        )

    supabase.table("messages").insert({
        "chat_id": chat_id,
        "yacht_id": yacht_id,
        "crew_id": crew_id,
        "role": "assistant",
        "content": answer
    }).execute()

    supabase.table("chats").update({
        "updated_at": "now()"
    }).eq("id", chat_id).eq("crew_id", crew_id).eq("yacht_id", yacht_id).execute()

    return {
        "answer": answer,
        "sources": sources
    }
# ------------------------
# TEMP DEMO LOGIN FOR TESTING ONLY
# Remove before production.
# ------------------------

def dev_demo_login(email: str = "demo@bridgeos.com"):
    """
    TEMP TEST LOGIN.

    Creates/fetches a demo yacht and demo crew row directly in the database,
    then returns a JWT that your existing get_user() can read.

    This bypasses Supabase Auth only so you can test upload/chat now.
    Remove before production.
    """

    demo_user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, email))
    demo_yacht_name = "Demo Yacht"

    crew_res = supabase.table("crew") \
        .select("*") \
        .eq("id", demo_user_id) \
        .execute()

    if crew_res.data:
        crew = crew_res.data[0]
    else:
        yacht_res = supabase.table("yachts").insert({
            "name": demo_yacht_name,
            "owner_id": demo_user_id
        }).execute()

        if not yacht_res.data:
            raise HTTPException(status_code=400, detail="Could not create demo yacht")

        yacht = yacht_res.data[0]

        crew_insert = supabase.table("crew").insert({
            "id": demo_user_id,
            "email": email,
            "full_name": "Demo Admin",
            "yacht_id": yacht["id"],
            "security_level": 1,
            "created_by": demo_user_id
        }).execute()

        if not crew_insert.data:
            raise HTTPException(status_code=400, detail="Could not create demo crew")

        crew = crew_insert.data[0]

    now = int(time.time())

    token = pyjwt.encode(
        {
            "sub": demo_user_id,
            "email": email,
            "aud": "authenticated",
            "role": "authenticated",
            "iat": now,
            "exp": now + 60 * 60 * 24 * 7
        },
        SUPABASE_JWT_SECRET,
        algorithm="HS256"
    )

    return {
        "access_token": token,
        "refresh_token": None,
        "token_type": "bearer",
        "user": {
            "id": demo_user_id,
            "email": email
        },
        "crew": crew
    }

# ------------------------
# TEMP WORKING DEMO AUTH
# This bypasses Supabase Auth so you can test upload/chat now.
# Remove before production.
# ------------------------

DEV_ACCESS_TOKEN = "bridgeos-dev-token"
DEV_USER_ID = "11111111-1111-1111-1111-111111111111"
DEV_EMAIL = "demo@bridgeos.com"
DEV_YACHT_NAME = "Demo Yacht"


def ensure_demo_account():
    """
    Creates or reuses:
    - demo yacht
    - demo crew profile with security_level = 1

    This does NOT use Supabase Auth.
    It only creates database rows needed by the app.
    """

    try:
        crew_res = supabase.table("crew") \
            .select("*") \
            .eq("id", DEV_USER_ID) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not query demo crew. Check crew table. Error: {str(e)}"
        )

    if crew_res.data:
        return crew_res.data[0]

    try:
        yacht_res = supabase.table("yachts").insert({
            "name": DEV_YACHT_NAME,
            "owner_id": DEV_USER_ID
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not create demo yacht. Check yachts table columns. Error: {str(e)}"
        )

    if not yacht_res.data:
        raise HTTPException(
            status_code=500,
            detail="Could not create demo yacht. Supabase returned no data."
        )

    yacht = yacht_res.data[0]

    try:
        crew_insert = supabase.table("crew").insert({
            "id": DEV_USER_ID,
            "email": DEV_EMAIL,
            "full_name": "Demo Admin",
            "yacht_id": yacht["id"],
            "security_level": 1,
            "created_by": DEV_USER_ID
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not create demo crew. Check crew table columns. Error: {str(e)}"
        )

    if not crew_insert.data:
        raise HTTPException(
            status_code=500,
            detail="Could not create demo crew. Supabase returned no data."
        )

    return crew_insert.data[0]


def test_login_response():
    crew = ensure_demo_account()

    return {
        "access_token": DEV_ACCESS_TOKEN,
        "refresh_token": None,
        "token_type": "bearer",
        "user": {
            "id": DEV_USER_ID,
            "email": DEV_EMAIL
        },
        "crew": crew
    }
 