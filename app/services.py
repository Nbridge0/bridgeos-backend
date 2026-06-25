from fastapi import HTTPException, Request
import requests
import json
import random
import hashlib
import smtplib
from email.message import EmailMessage
from app.database import supabase
from app.embeddings import embed
from app.config import BUCKET_NAME, RUNPOD_BASE_URL, BRIDGEOS_API_KEY, API_SYNC_TIMEOUT_SECONDS
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
    extract_ocr_from_image,
    extract_ocr_from_pdf_pages
)

import time
import uuid
import jwt as pyjwt
import io
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

from app.config import (
    SUPABASE_JWT_SECRET,
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    SMTP_FROM_EMAIL,
    SMTP_FROM_NAME,
    BREVO_API_KEY,
    BREVO_FROM_EMAIL,
    BREVO_FROM_NAME,
    BREVO_API_URL
)
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

def get_request_ip(request: Request | None) -> str | None:
    if not request:
        return None

    cloudflare_ip = request.headers.get("cf-connecting-ip")
    if cloudflare_ip:
        return cloudflare_ip.strip()

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    if request.client and request.client.host:
        return request.client.host

    return None


def parse_user_agent(user_agent: str | None) -> dict:
    value = (user_agent or "").lower()

    device_type = "desktop"
    browser = "unknown"
    operating_system = "unknown"

    if "mobile" in value or "iphone" in value or "android" in value:
        device_type = "mobile"

    if "ipad" in value or "tablet" in value:
        device_type = "tablet"

    if "edg/" in value:
        browser = "edge"
    elif "chrome/" in value and "safari/" in value:
        browser = "chrome"
    elif "firefox/" in value:
        browser = "firefox"
    elif "safari/" in value and "chrome/" not in value:
        browser = "safari"

    if "windows" in value:
        operating_system = "windows"
    elif "mac os" in value or "macintosh" in value:
        operating_system = "macos"
    elif "iphone" in value or "ipad" in value or "ios" in value:
        operating_system = "ios"
    elif "android" in value:
        operating_system = "android"
    elif "linux" in value:
        operating_system = "linux"

    return {
        "device_type": device_type,
        "browser": browser,
        "operating_system": operating_system
    }


def lookup_ip_geo(ip_address: str | None) -> dict:
    if not ip_address:
        return {}

    if ip_address.startswith("127.") or ip_address in ["localhost", "::1"]:
        return {}

    if ip_address.startswith("10.") or ip_address.startswith("192.168."):
        return {}

    if ip_address.startswith("172."):
        try:
            second = int(ip_address.split(".")[1])
            if 16 <= second <= 31:
                return {}
        except Exception:
            pass

    try:
        response = requests.get(
            f"https://ipapi.co/{ip_address}/json/",
            timeout=4
        )

        if response.status_code >= 400:
            print("IP GEO LOOKUP FAILED:", response.status_code, response.text[:200])
            return {}

        data = response.json()

        return {
            "geo_country": data.get("country_name"),
            "geo_region": data.get("region"),
            "geo_city": data.get("city"),
            "geo_latitude": data.get("latitude"),
            "geo_longitude": data.get("longitude"),
            "geo_source": "ip"
        }

    except Exception as e:
        print("IP GEO LOOKUP ERROR:", type(e).__name__, str(e))
        return {}

def reverse_geocode_browser_location(latitude: float | None, longitude: float | None) -> dict:
    """
    Converts browser latitude/longitude into country, region, and city.
    """

    if latitude is None or longitude is None:
        return {}

    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "format": "jsonv2",
                "lat": latitude,
                "lon": longitude
            },
            headers={
                "User-Agent": "BridgeOS/1.0"
            },
            timeout=6
        )

        if response.status_code >= 400:
            print("REVERSE GEO LOOKUP FAILED:", response.status_code, response.text[:300])
            return {}

        data = response.json()
        address = data.get("address") or {}

        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )

        return {
            "geo_country": address.get("country"),
            "geo_region": address.get("state") or address.get("region"),
            "geo_city": city,
            "geo_latitude": latitude,
            "geo_longitude": longitude,
            "geo_source": "browser"
        }

    except Exception as e:
        print("REVERSE GEO LOOKUP ERROR:", type(e).__name__, str(e))
        return {}


def register_login_log(
    user_id: str | None,
    email: str | None,
    login_type: str = "password",
    success: bool = True,
    request: Request | None = None,
    client_geo: dict | None = None
):
    if not user_id:
        print("LOGIN LOG SKIPPED: missing user_id")
        return None

    clean_email = (email or "").strip().lower()
    login_time = datetime.now(timezone.utc).isoformat()

    ip_address = get_request_ip(request)
    user_agent = request.headers.get("user-agent") if request else None
    parsed_agent = parse_user_agent(user_agent)

    geo_payload = lookup_ip_geo(ip_address)

    if client_geo:
        browser_latitude = client_geo.get("latitude")
        browser_longitude = client_geo.get("longitude")

        reverse_geo = reverse_geocode_browser_location(
            latitude=browser_latitude,
            longitude=browser_longitude
        )

        geo_payload = {
            "geo_country": reverse_geo.get("geo_country") or client_geo.get("country"),
            "geo_region": reverse_geo.get("geo_region") or client_geo.get("region"),
            "geo_city": reverse_geo.get("geo_city") or client_geo.get("city"),
            "geo_latitude": browser_latitude,
            "geo_longitude": browser_longitude,
            "geo_source": "browser"
        }

    crew = None

    try:
        crew_res = auth_admin.table("crew") \
            .select("id, yacht_id, email") \
            .eq("id", user_id) \
            .limit(1) \
            .execute()

        if crew_res.data:
            crew = crew_res.data[0]

    except Exception as e:
        print("LOGIN LOG CREW LOOKUP ERROR:", type(e).__name__, str(e))

    payload = {
        "user_id": user_id,
        "crew_id": crew.get("id") if crew else user_id,
        "yacht_id": crew.get("yacht_id") if crew else None,
        "email": clean_email or (crew.get("email") if crew else None),
        "login_type": login_type,
        "success": success,
        "login_at": login_time,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "device_type": parsed_agent.get("device_type"),
        "browser": parsed_agent.get("browser"),
        "operating_system": parsed_agent.get("operating_system"),
        "geo_country": geo_payload.get("geo_country"),
        "geo_region": geo_payload.get("geo_region"),
        "geo_city": geo_payload.get("geo_city"),
        "geo_latitude": geo_payload.get("geo_latitude"),
        "geo_longitude": geo_payload.get("geo_longitude"),
        "geo_source": geo_payload.get("geo_source")
    }

    try:
        log_res = auth_admin.table("login_logs").insert(payload).execute()

        print("LOGIN LOG INSERT PAYLOAD:", payload)
        print("LOGIN LOG INSERT RESPONSE:", log_res)

        if not log_res.data:
            raise Exception("Supabase returned no inserted login_logs row")

        return log_res.data[0]

    except Exception as e:
        print("LOGIN LOG INSERT FAILED:", type(e).__name__, str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Login succeeded, but login_logs insert failed: {type(e).__name__}: {str(e)}"
        )

def login(
    email: str,
    password: str,
    request: Request | None = None,
    client_geo: dict | None = None
):
    """
    Logs in a user using Supabase Auth and returns a clean token response.
    Also writes one row to login_logs.
    """

    clean_email = (email or "").strip().lower()

    try:
        auth_res = supabase.auth.sign_in_with_password({
            "email": clean_email,
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

    user_id = auth_res.user.id if auth_res.user else None
    user_email = auth_res.user.email if auth_res.user else clean_email

    login_log = register_login_log(
        user_id=user_id,
        email=user_email,
        login_type="password",
        success=True,
        request=request,
        client_geo=client_geo
    )

    return {
        "access_token": auth_res.session.access_token,
        "refresh_token": auth_res.session.refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user_id,
            "email": user_email
        },
        "login_log": login_log
    }

def dev_login(
    email: str,
    full_name: str = "Test Admin",
    yacht_name: str = "Test Yacht",
    request: Request | None = None
):
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

    try:
        supabase.table("login_logs").insert({
            "user_id": user_id,
            "crew_id": crew.get("id") if crew else user_id,
            "yacht_id": crew.get("yacht_id") if crew else None,
            "email": email,
            "login_at": datetime.now(timezone.utc).isoformat(),
            "login_type": "dev-login",
            "success": True
        }).execute()

    except Exception as e:
        print("DEV LOGIN LOG INSERT ERROR:", type(e).__name__, str(e))

    login_log = register_login_log(
        user_id=user_id,
        email=email,
        login_type="dev-login",
        success=True,
        request=request
    )
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
        "crew": crew,
        "login_log": login_log
    }

def chat_with_runpod_bridgeos(
    query: str,
    crew: dict,
    chat_id: str,
    uploaded_asset_id: str | None = None
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
    query_scope = classify_bridgeos_query_scope(query)

    if query_scope == "factual" and not uploaded_asset_id:
        answer = FALLBACK_NO_DATA_ANSWER

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": crew["yacht_id"],
            "crew_id": crew["id"],
            "role": "user",
            "content": query,
            "sources": []
        }).execute()

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": crew["yacht_id"],
            "crew_id": crew["id"],
            "role": "assistant",
            "content": answer,
            "sources": []
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

    uploaded_asset = None

    if uploaded_asset_id:
        asset_res = (
            supabase.table("assets")
            .select("id, file_name, original_file_name, mime_type, yacht_id, chat_id")
            .eq("id", uploaded_asset_id)
            .eq("yacht_id", crew["yacht_id"])
            .execute()
        )

        if asset_res.data:
            uploaded_asset = asset_res.data[0]

    supabase.table("messages").insert({
        "chat_id": chat_id,
        "yacht_id": crew["yacht_id"],
        "crew_id": crew["id"],
        "role": "user",
        "content": query,
        "uploaded_asset_id": uploaded_asset.get("id") if uploaded_asset else None,
        "file_name": uploaded_asset.get("file_name") if uploaded_asset else None,
        "original_file_name": uploaded_asset.get("original_file_name") if uploaded_asset else None,
        "mime_type": uploaded_asset.get("mime_type") if uploaded_asset else None
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

def repair_admin_login(
    email: str,
    password: str,
    full_name: str,
    yacht_name: str,
    request: Request | None = None
):
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

        login_log = register_login_log(
            user_id=user_id,
            email=auth_res.user.email or email,
            login_type="repair-admin-login",
            success=True,
            request=request
        )

        return {
            "message": "Login successful. Crew profile already exists.",
            "access_token": auth_res.session.access_token,
            "refresh_token": auth_res.session.refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user_id,
                "email": auth_res.user.email or email
            },
            "crew": crew,
            "login_log": login_log
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

    login_log = register_login_log(
        user_id=user_id,
        email=auth_res.user.email or email,
        login_type="repair-admin-login",
        success=True,
        request=request
    )

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
        "yacht": yacht,
        "login_log": login_log
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
    1 = can access Tier 1, 2, and 3 documents
    2 = can access Tier 2 and 3 documents
    3 = can access Tier 3 documents only
    4 = custom access only, must be manually granted files in asset_access
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 main accounts can create sub accounts"
        )

    security_level = int(security_level)

    if security_level not in [1, 2, 3, 4]:
        raise HTTPException(
            status_code=400,
            detail="security_level must be 1, 2, 3, or 4"
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

        if security_level not in [1, 2, 3, 4]:
            raise HTTPException(
                status_code=400,
                detail="security_level must be 1, 2, 3, or 4"
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
    Synced document permission model.

    Document security_level:
    1 = Tier 1 only
    2 = Tier 1 and Tier 2
    3 = Tier 1, Tier 2, and Tier 3
    4 = Custom only

    Crew security_level:
    1 = admin/full access
    2 = sees doc levels 2 and 3, plus manual grants
    3 = sees doc level 3, plus manual grants
    4 = custom only, manual grants only

    Tier 4 documents:
    - Tier 1 admins can always see/manage them.
    - Tier 2, 3, and 4 users only see them if manually granted in asset_access.
    """

    security_level = int(security_level)

    if security_level not in [1, 2, 3, 4]:
        return []

    allowed_ids = set()

    if security_level == 1:
        assets = supabase.table("assets") \
            .select("id") \
            .eq("yacht_id", yacht_id) \
            .execute()

        allowed_ids = {
            asset["id"]
            for asset in (assets.data or [])
            if asset.get("id")
        }

    elif security_level in [2, 3]:
        base_assets = supabase.table("assets") \
            .select("id") \
            .eq("yacht_id", yacht_id) \
            .gte("security_level", security_level) \
            .lte("security_level", 3) \
            .execute()

        allowed_ids = {
            asset["id"]
            for asset in (base_assets.data or [])
            if asset.get("id")
        }

    manual_access = supabase.table("asset_access") \
        .select("asset_id, assets!inner(yacht_id)") \
        .eq("crew_id", crew_id) \
        .eq("assets.yacht_id", yacht_id) \
        .execute()

    for row in manual_access.data or []:
        if row.get("asset_id"):
            allowed_ids.add(row["asset_id"])

    return list(allowed_ids)
    
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

# ------------------------
# API CONNECTIONS
# ------------------------

def _require_tier_1_admin(crew: dict):
    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    if int(crew.get("security_level") or 4) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can manage API connections"
        )


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None

    value = str(value)

    if len(value) <= 6:
        return "***"

    return f"{value[:3]}***{value[-4:]}"


def _clean_api_connection(row: dict) -> dict:
    clean = dict(row)
    clean["api_key"] = _mask_secret(clean.get("api_key"))
    return clean


def create_api_connection(
    admin_crew: dict,
    name: str,
    base_url: str,
    auth_type: str = "none",
    api_key: str | None = None,
    extra_headers: dict | None = None,
    security_level: int = 1
):
    """
    Creates a reusable external API connection for this yacht.

    auth_type:
    - none
    - bearer
    - x-api-key
    """

    _require_tier_1_admin(admin_crew)

    clean_name = (name or "").strip()
    clean_base_url = (base_url or "").strip()
    clean_auth_type = (auth_type or "none").strip().lower()

    if not clean_name:
        raise HTTPException(status_code=400, detail="Connection name is required")

    if not clean_base_url:
        raise HTTPException(status_code=400, detail="base_url is required")

    if not clean_base_url.startswith("http://") and not clean_base_url.startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="base_url must start with http:// or https://"
        )

    if clean_auth_type not in ["none", "bearer", "x-api-key"]:
        raise HTTPException(
            status_code=400,
            detail="auth_type must be one of: none, bearer, x-api-key"
        )

    if clean_auth_type != "none" and not api_key:
        raise HTTPException(
            status_code=400,
            detail="api_key is required when auth_type is bearer or x-api-key"
        )

    safe_headers = extra_headers or {}

    if not isinstance(safe_headers, dict):
        raise HTTPException(status_code=400, detail="extra_headers must be an object")

    try:
        res = supabase.table("api_connections").insert({
            "yacht_id": admin_crew["yacht_id"],
            "created_by": admin_crew["id"],
            "name": clean_name,
            "base_url": clean_base_url,
            "auth_type": clean_auth_type,
            "api_key": api_key,
            "extra_headers": safe_headers,
            "security_level": security_level
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create API connection: {str(e)}"
        )

    if not res.data:
        raise HTTPException(status_code=400, detail="Could not create API connection")

    return {
        "message": "API connection created successfully",
        "connection": _clean_api_connection(res.data[0])
    }


def list_api_connections(admin_crew: dict):
    _require_tier_1_admin(admin_crew)

    try:
        res = supabase.table("api_connections") \
            .select("*") \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .order("created_at", desc=True) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not list API connections: {str(e)}"
        )

    return {
        "data": [_clean_api_connection(row) for row in (res.data or [])]
    }


def get_api_connection_for_admin(connection_id: str, admin_crew: dict) -> dict:
    _require_tier_1_admin(admin_crew)

    res = supabase.table("api_connections") \
        .select("*") \
        .eq("id", connection_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="API connection not found")

    return res.data[0]


def delete_api_connection(connection_id: str, admin_crew: dict):
    _require_tier_1_admin(admin_crew)

    try:
        res = supabase.table("api_connections") \
            .delete() \
            .eq("id", connection_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not delete API connection: {str(e)}"
        )

    if not res.data:
        raise HTTPException(status_code=404, detail="API connection not found")

    return {
        "message": "API connection deleted successfully",
        "deleted_connection_id": connection_id
    }

def sync_one_google_drive_file(
    drive_file: dict,
    connection: dict,
    admin_crew: dict,
    security_level: int
) -> dict:
    file_id = drive_file.get("id")
    file_name = drive_file.get("name") or "google-drive-file"
    mime_type = drive_file.get("mimeType") or ""

    if not file_id:
        return {
            "file_name": file_name,
            "status": "skipped",
            "reason": "Missing Google Drive file id"
        }

    if mime_type == "application/vnd.google-apps.folder":
        return {
            "file_name": file_name,
            "file_id": file_id,
            "status": "skipped",
            "reason": "Folder skipped"
        }

    if mime_type == "application/vnd.google-apps.document":
        download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/plain"
        final_file_name = file_name if file_name.lower().endswith(".txt") else f"{file_name}.txt"

    elif mime_type == "application/vnd.google-apps.spreadsheet":
        download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/csv"
        final_file_name = file_name if file_name.lower().endswith(".csv") else f"{file_name}.csv"

    elif mime_type == "application/vnd.google-apps.presentation":
        download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/plain"
        final_file_name = file_name if file_name.lower().endswith(".txt") else f"{file_name}.txt"

    else:
        download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        final_file_name = file_name

    headers = {
        "Accept": "*/*"
    }

    extra_headers = connection.get("extra_headers") or {}

    if isinstance(extra_headers, dict):
        headers.update(extra_headers)

    auth_type = (connection.get("auth_type") or "none").lower()
    api_key = connection.get("api_key")

    if auth_type == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if auth_type == "x-api-key" and api_key:
        headers["x-api-key"] = api_key

    try:
        response = requests.get(
            download_url,
            headers=headers,
            timeout=API_SYNC_TIMEOUT_SECONDS
        )
    except Exception as e:
        return {
            "file_name": file_name,
            "file_id": file_id,
            "status": "failed",
            "reason": f"{type(e).__name__}: {str(e)}"
        }

    if response.status_code >= 400:
        return {
            "file_name": file_name,
            "file_id": file_id,
            "status": "failed",
            "reason": f"Google Drive returned {response.status_code}: {response.text[:500]}"
        }

    extracted_content = ""

    try:
        downloaded_bytes = response.content or b""
        file_type = detect_file_type(final_file_name, response.headers.get("content-type"))

        if file_type in ["pdf", "docx", "text"]:
            extracted_content = extract_text_by_file_type(
                file=io.BytesIO(downloaded_bytes),
                filename=final_file_name,
                file_type=file_type
            )
        else:
            extracted_content = downloaded_bytes.decode("utf-8", errors="ignore")

    except Exception as e:
        return {
            "file_name": file_name,
            "file_id": file_id,
            "status": "failed",
            "reason": f"Could not extract text: {type(e).__name__}: {str(e)}"
        }

    extracted_content = (extracted_content or "").strip()

    if not extracted_content:
        return {
            "file_name": file_name,
            "file_id": file_id,
            "status": "skipped",
            "reason": "No readable text extracted"
        }

    source_header = f"""
Google Drive source metadata:
File name: {file_name}
Google Drive file id: {file_id}
Google Drive MIME type: {mime_type}
Modified time: {drive_file.get("modifiedTime") or ""}
Web link: {drive_file.get("webViewLink") or ""}
""".strip()

    final_content = f"{source_header}\n\n---\n\n{extracted_content}"

    try:
        seeded = seed_text_asset(
            file_name=final_file_name,
            content=final_content,
            yacht_id=admin_crew["yacht_id"],
            uploaded_by=admin_crew["id"],
            security_level=security_level
        )

        return {
            "file_name": final_file_name,
            "file_id": file_id,
            "status": "synced",
            "asset_id": seeded.get("asset", {}).get("id"),
            "duplicate": seeded.get("duplicate", False)
        }

    except Exception as e:
        return {
            "file_name": file_name,
            "file_id": file_id,
            "status": "failed",
            "reason": f"Could not save asset: {type(e).__name__}: {str(e)}"
        }


def sync_api_connection(
    connection_id: str,
    admin_crew: dict,
    endpoint_path: str | None = None,
    method: str = "GET",
    payload: dict | None = None,
    file_name: str | None = None,
    security_level: int = 1
):
    """
    Calls the external API, converts the response into text, and saves it
    as a searchable asset using the existing asset/chunk pipeline.
    """

    _require_tier_1_admin(admin_crew)

    connection = get_api_connection_for_admin(connection_id=connection_id, admin_crew=admin_crew)

    if security_level is None:
        security_level = connection.get("security_level") or 1

    security_level = int(security_level)

    if security_level not in [1, 2, 3]:
        raise HTTPException(status_code=400, detail="security_level must be 1, 2, or 3")

    base_url = connection["base_url"].rstrip("/")
    clean_path = (endpoint_path or "").strip()

    if clean_path:
        url = f"{base_url}/{clean_path.lstrip('/')}"
    else:
        url = base_url

    headers = {
        "Accept": "application/json"
    }

    extra_headers = connection.get("extra_headers") or {}

    if isinstance(extra_headers, dict):
        headers.update(extra_headers)

    auth_type = (connection.get("auth_type") or "none").lower()
    api_key = connection.get("api_key")

    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"

    if auth_type == "x-api-key":
        headers["x-api-key"] = api_key

    clean_method = (method or "GET").upper()

    if clean_method not in ["GET", "POST"]:
        raise HTTPException(status_code=400, detail="method must be GET or POST")

    try:
        if clean_method == "POST":
            response = requests.post(
                url,
                json=payload or {},
                headers=headers,
                timeout=API_SYNC_TIMEOUT_SECONDS
            )
        else:
            response = requests.get(
                url,
                headers=headers,
                timeout=API_SYNC_TIMEOUT_SECONDS
            )

    except Exception as e:
        error_text = f"{type(e).__name__}: {str(e)}"

        supabase.table("api_connections").update({
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
            "last_sync_status": "failed",
            "last_sync_error": error_text
        }).eq("id", connection_id).eq("yacht_id", admin_crew["yacht_id"]).execute()

        raise HTTPException(
            status_code=502,
            detail=f"External API request failed: {error_text}"
        )

    if response.status_code >= 400:
        error_text = response.text[:1000]

        supabase.table("api_connections").update({
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
            "last_sync_status": "failed",
            "last_sync_error": error_text
        }).eq("id", connection_id).eq("yacht_id", admin_crew["yacht_id"]).execute()

        raise HTTPException(
            status_code=502,
            detail=f"External API returned {response.status_code}: {error_text}"
        )

    content_type = response.headers.get("content-type", "")
    # ------------------------
    # GOOGLE DRIVE FOLDER / FILE CONTENT SYNC
    # ------------------------
    is_google_drive_url = "www.googleapis.com/drive/v3/files" in url

    if is_google_drive_url and "application/json" in content_type:
        try:
            data = response.json()
        except Exception:
            data = None

        if isinstance(data, dict) and isinstance(data.get("files"), list):
            results = []

            for drive_file in data.get("files") or []:
                result = sync_one_google_drive_file(
                    drive_file=drive_file,
                    connection=connection,
                    admin_crew=admin_crew,
                    security_level=security_level
                )
                results.append(result)

            synced_count = len([r for r in results if r.get("status") == "synced"])
            skipped_count = len([r for r in results if r.get("status") == "skipped"])
            failed_count = len([r for r in results if r.get("status") == "failed"])

            supabase.table("api_connections").update({
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
                "last_sync_status": "success" if synced_count > 0 else "failed",
                "last_sync_error": None if synced_count > 0 else "No Google Drive files were synced"
            }).eq("id", connection_id).eq("yacht_id", admin_crew["yacht_id"]).execute()

            return {
                "message": "Google Drive content sync completed",
                "connection_id": connection_id,
                "url": url,
                "provider": "google_drive",
                "synced_count": synced_count,
                "skipped_count": skipped_count,
                "failed_count": failed_count,
                "results": results
            }

    try:
        if "application/json" in content_type:
            extracted_content = json.dumps(response.json(), indent=2, ensure_ascii=False)
        else:
            extracted_content = response.text
    except Exception:
        extracted_content = response.text

    extracted_content = clean_text_for_postgres(extracted_content)

    if not extracted_content.strip():
        raise HTTPException(
            status_code=400,
            detail="External API returned empty content"
        )

    final_file_name = (
        file_name
        or f"api-{connection.get('name', 'connection')}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    )

    asset_result = seed_text_asset(
        file_name=final_file_name,
        content=extracted_content,
        yacht_id=admin_crew["yacht_id"],
        uploaded_by=admin_crew["id"],
        security_level=security_level
    )

    supabase.table("api_connections").update({
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "last_sync_status": "success",
        "last_sync_error": None
    }).eq("id", connection_id).eq("yacht_id", admin_crew["yacht_id"]).execute()

    return {
        "message": "API connection synced successfully",
        "connection_id": connection_id,
        "url": url,
        "asset_result": asset_result
    }


def ingest_api_data_directly(
    admin_crew: dict,
    source_name: str,
    content: dict | list | str,
    file_name: str | None = None,
    security_level: int = 1
):
    """
    Allows a client system to push data directly into BridgeOS by API.
    This is useful for Zapier, Make, webhooks, CRMs, PMS systems, etc.
    """

    _require_tier_1_admin(admin_crew)

    security_level = int(security_level)

    if security_level not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="security_level must be 1, 2, or 3"
        )

    clean_source_name = (source_name or "api-data").strip()

    if isinstance(content, str):
        extracted_content = content
    else:
        extracted_content = json.dumps(content, indent=2, ensure_ascii=False)

    extracted_content = clean_text_for_postgres(extracted_content)

    if not extracted_content.strip():
        raise HTTPException(status_code=400, detail="content is required")

    final_file_name = (
        file_name
        or f"{clean_source_name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    )

    return seed_text_asset(
        file_name=final_file_name,
        content=extracted_content,
        yacht_id=admin_crew["yacht_id"],
        uploaded_by=admin_crew["id"],
        security_level=security_level
    )


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

def create_asset_preview(asset_id: str, crew: dict):
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

    asset = res.data[0]

    title = (
        asset.get("original_file_name")
        or asset.get("file_name")
        or "Document preview"
    )

    visual_description = asset.get("visual_description") or ""
    ocr_text = asset.get("ocr_text") or ""
    normal_text = asset.get("extracted_text") or ""

    extracted_text = "\n\n".join([
        "Image visual description:\n" + visual_description if visual_description else "",
        "OCR text:\n" + ocr_text if ocr_text else "",
        "Extracted document text:\n" + normal_text if normal_text else ""
    ]).strip()

    storage_path = asset.get("storage_path")
    mime_type = asset.get("mime_type") or "application/octet-stream"

    if storage_path:
        try:
            signed = storage_admin.storage.from_(BUCKET_NAME).create_signed_url(
                storage_path,
                60 * 10
            )

            signed_url = signed.get("signedURL") or signed.get("signed_url")

            if signed_url:
                return {
                    "asset_id": asset_id,
                    "title": title,
                    "preview_type": "url",
                    "url": signed_url,
                    "mime_type": mime_type
                }

        except Exception as e:
            print("PREVIEW SIGNED URL ERROR:", type(e).__name__, str(e))

    if extracted_text.strip():
        return {
            "asset_id": asset_id,
            "title": title,
            "preview_type": "text",
            "text": extracted_text,
            "mime_type": "text/plain"
        }

    raise HTTPException(
        status_code=404,
        detail="No preview available for this asset"
    )

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

def move_asset_to_folder(
    asset_id: str,
    folder_name: str | None,
    admin_crew: dict
):
    """
    Moves one asset into another virtual folder.

    Folders are virtual:
    - There is no folders table.
    - A folder exists because assets have folder_name.
    - Moving a file means updating assets.folder_name.
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can move assets"
        )

    clean_folder_name = (folder_name or "").strip()

    asset_res = supabase.table("assets") \
        .select("*") \
        .eq("id", asset_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not asset_res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    folder_security_level = None

    if clean_folder_name:
        existing_folder_res = supabase.table("assets") \
            .select("folder_security_level, security_level") \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .eq("folder_name", clean_folder_name) \
            .limit(1) \
            .execute()

        if existing_folder_res.data:
            existing_folder = existing_folder_res.data[0]
            folder_security_level = (
                existing_folder.get("folder_security_level")
                or existing_folder.get("security_level")
            )

    try:
        moved_res = supabase.table("assets") \
            .update({
                "folder_name": clean_folder_name or None,
                "folder_security_level": folder_security_level
            }) \
            .eq("id", asset_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not move asset: {str(e)}"
        )

    if not moved_res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    return {
        "message": "Asset moved successfully",
        "asset": moved_res.data[0]
    }

def rename_asset(
    asset_id: str,
    new_name: str,
    admin_crew: dict
):
    """
    Renames one asset for display in Yacht Documentation.

    Database sync:
    - assets.original_file_name
    - assets.file_name
    - assets.previous_file_name
    - assets.renamed_at
    - assets.renamed_by

    It does not rename the physical storage path.
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can rename assets"
        )

    clean_name = (new_name or "").strip()

    if not clean_name:
        raise HTTPException(status_code=400, detail="File name is required")

    if len(clean_name) > 180:
        clean_name = clean_name[:180]

    asset_res = supabase.table("assets") \
        .select("*") \
        .eq("id", asset_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not asset_res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset = asset_res.data[0]

    previous_name = (
        asset.get("original_file_name")
        or asset.get("file_name")
        or ""
    )

    now = datetime.now(timezone.utc).isoformat()

    renamed_res = supabase.table("assets") \
        .update({
            "original_file_name": clean_name,
            "file_name": clean_name,
            "previous_file_name": previous_name,
            "renamed_at": now,
            "renamed_by": admin_crew["id"]
        }) \
        .eq("id", asset_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not renamed_res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    return {
        "message": "Asset renamed successfully",
        "previous_file_name": previous_name,
        "new_file_name": clean_name,
        "renamed_at": now,
        "renamed_by": admin_crew["id"],
        "asset": renamed_res.data[0]
    }
    
def delete_asset(asset_id: str, admin_crew: dict):
    """
    Deletes one asset from:
    1. Supabase Storage
    2. asset_chunks
    3. asset_access
    4. assets table

    Only Tier 1 admins can delete yacht documentation.
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can delete assets"
        )

    asset_res = supabase.table("assets") \
        .select("*") \
        .eq("id", asset_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not asset_res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset = asset_res.data[0]
    storage_path = asset.get("storage_path")

    try:
        supabase.table("asset_chunks") \
            .delete() \
            .eq("asset_id", asset_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not delete asset chunks: {str(e)}"
        )

    try:
        supabase.table("asset_access") \
            .delete() \
            .eq("asset_id", asset_id) \
            .execute()
    except Exception:
        pass

    if storage_path:
        try:
            storage_admin.storage.from_(BUCKET_NAME).remove([storage_path])
        except Exception as e:
            print("STORAGE DELETE WARNING:", type(e).__name__, str(e))

    try:
        delete_res = supabase.table("assets") \
            .delete() \
            .eq("id", asset_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not delete asset row: {str(e)}"
        )

    if not delete_res.data:
        raise HTTPException(status_code=404, detail="Asset not found")

    return {
        "message": "Asset deleted successfully",
        "deleted_asset_id": asset_id,
        "deleted_file_name": asset.get("original_file_name") or asset.get("file_name")
    }



def create_asset_folder(
    folder_name: str,
    security_level: int,
    admin_crew: dict
):
    """
    Creates an empty folder in asset_folders.

    Uses service-role Supabase client because backend already checked:
    - logged-in user
    - crew profile
    - Tier 1 admin
    - yacht_id ownership
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can create folders"
        )

    clean_name = (folder_name or "").strip()

    if not clean_name:
        raise HTTPException(status_code=400, detail="Folder name is required")

    if len(clean_name) > 180:
        clean_name = clean_name[:180]

    security_level = int(security_level)

    if security_level not in [1, 2, 3, 4]:
        raise HTTPException(
            status_code=400,
            detail="security_level must be 1, 2, 3, or 4"
        )

    try:
        existing = storage_admin.table("asset_folders") \
            .select("*") \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .ilike("name", clean_name) \
            .limit(1) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not check existing folder: {str(e)}"
        )

    if existing.data:
        raise HTTPException(
            status_code=409,
            detail="A folder with this name already exists"
        )

    try:
        res = storage_admin.table("asset_folders").insert({
            "yacht_id": admin_crew["yacht_id"],
            "name": clean_name,
            "security_level": security_level,
            "created_by": admin_crew["id"]
        }).execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not create folder: {str(e)}"
        )

    if not res.data:
        raise HTTPException(status_code=400, detail="Could not create folder")

    return {
        "message": "Folder created successfully",
        "folder": res.data[0]
    }


def list_my_asset_folders(crew: dict):
    """
    Lists folders visible to the current user.
    Uses service role because backend already knows the user's yacht and tier.
    """

    security_level = int(crew["security_level"])

    query = storage_admin.table("asset_folders") \
        .select("*") \
        .eq("yacht_id", crew["yacht_id"])

    if security_level == 1:
        return query.order("created_at", desc=True).execute()

    if security_level in [2, 3]:
        return query \
            .gte("security_level", security_level) \
            .lte("security_level", 3) \
            .order("created_at", desc=True) \
            .execute()

    return {"data": []}


def rename_asset_folder(
    old_folder_name: str,
    new_folder_name: str,
    admin_crew: dict
):
    """
    Renames a folder in both:
    - assets.folder_name
    - asset_folders.name

    Works even if folder is empty.
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can rename folders"
        )

    clean_old_name = (old_folder_name or "").strip()
    clean_new_name = (new_folder_name or "").strip()

    if not clean_old_name:
        raise HTTPException(status_code=400, detail="Current folder name is required")

    if not clean_new_name:
        raise HTTPException(status_code=400, detail="New folder name is required")

    if len(clean_new_name) > 180:
        clean_new_name = clean_new_name[:180]

    if clean_old_name == clean_new_name:
        return {
            "message": "Folder name unchanged",
            "old_folder_name": clean_old_name,
            "new_folder_name": clean_new_name,
            "updated_count": 0
        }

    existing_old_folder = storage_admin.table("asset_folders") \
        .select("*") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .eq("name", clean_old_name) \
        .limit(1) \
        .execute()

    existing_old_assets = storage_admin.table("assets") \
        .select("id") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .eq("folder_name", clean_old_name) \
        .limit(1) \
        .execute()

    if not existing_old_folder.data and not existing_old_assets.data:
        raise HTTPException(status_code=404, detail="Folder not found")

    existing_new = storage_admin.table("asset_folders") \
        .select("id") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .ilike("name", clean_new_name) \
        .limit(1) \
        .execute()

    if existing_new.data:
        raise HTTPException(
            status_code=409,
            detail="A folder with this name already exists"
        )

    now = datetime.now(timezone.utc).isoformat()

    renamed_assets_res = storage_admin.table("assets") \
        .update({
            "folder_name": clean_new_name,
            "renamed_at": now,
            "renamed_by": admin_crew["id"]
        }) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .eq("folder_name", clean_old_name) \
        .execute()

    if existing_old_folder.data:
        storage_admin.table("asset_folders") \
            .update({
                "name": clean_new_name,
                "renamed_at": now,
                "renamed_by": admin_crew["id"]
            }) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .eq("name", clean_old_name) \
            .execute()
    else:
        storage_admin.table("asset_folders") \
            .insert({
                "yacht_id": admin_crew["yacht_id"],
                "name": clean_new_name,
                "security_level": 1,
                "created_by": admin_crew["id"],
                "renamed_at": now,
                "renamed_by": admin_crew["id"]
            }) \
            .execute()

    return {
        "message": "Folder renamed successfully",
        "old_folder_name": clean_old_name,
        "new_folder_name": clean_new_name,
        "updated_count": len(renamed_assets_res.data or []),
        "assets": renamed_assets_res.data or []
    }


def delete_folder_assets(folder_name: str, admin_crew: dict):
    """
    Deletes a folder.

    If folder has files:
    - deletes all assets inside it
    - deletes asset_folders row

    If folder is empty:
    - deletes only asset_folders row
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can delete folders"
        )

    clean_folder_name = (folder_name or "").strip()

    if not clean_folder_name:
        raise HTTPException(status_code=400, detail="Folder name is required")

    folder_row_res = storage_admin.table("asset_folders") \
        .select("*") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .eq("name", clean_folder_name) \
        .limit(1) \
        .execute()

    folder_assets_res = storage_admin.table("assets") \
        .select("id, file_name, original_file_name, folder_name") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .eq("folder_name", clean_folder_name) \
        .execute()

    folder_assets = folder_assets_res.data or []

    if not folder_row_res.data and not folder_assets:
        raise HTTPException(status_code=404, detail="Folder not found")

    deleted = []

    for asset in folder_assets:
        result = delete_asset(
            asset_id=asset["id"],
            admin_crew=admin_crew
        )

        deleted.append(result)

    try:
        storage_admin.table("asset_folders") \
            .delete() \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .eq("name", clean_folder_name) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Folder assets were deleted, but folder row could not be removed: {str(e)}"
        )

    return {
        "message": "Folder deleted successfully",
        "folder_name": clean_folder_name,
        "deleted_count": len(deleted),
        "deleted_assets": deleted
    }
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

def create_pending_document_preview(
    pending_document_id: str,
    admin_crew: dict
):
    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can preview pending documents"
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

    storage_path = pending_doc.get("storage_path")

    if not storage_path:
        raise HTTPException(
            status_code=404,
            detail="This pending document has no stored file to preview"
        )

    signed = storage_admin.storage.from_(BUCKET_NAME).create_signed_url(
        storage_path,
        60 * 10
    )

    signed_url = signed.get("signedURL") or signed.get("signed_url")

    if not signed_url:
        raise HTTPException(
            status_code=500,
            detail="Could not create pending document preview URL"
        )

    title = (
        pending_doc.get("original_file_name")
        or pending_doc.get("file_name")
        or "Pending document preview"
    )

    return {
        "pending_document_id": pending_document_id,
        "title": title,
        "preview_type": "url",
        "url": signed_url,
        "mime_type": pending_doc.get("mime_type") or "application/octet-stream"
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
    security_level: int = 1,
    folder_name: str | None = None,
    folder_security_level: int | None = None
):
    
    """
    Uploads any file, stores it in Supabase Storage, creates an asset row,
    extracts/processes it, creates chunks and embeddings.
    """

    clean_filename = safe_filename(filename)
    file_type = detect_file_type(clean_filename, mime_type)
    security_level = int(security_level)

    if security_level not in [1, 2, 3, 4]:
        raise HTTPException(
            status_code=400,
            detail="security_level must be 1, 2, 3, or 4"
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
            "folder_name": folder_name,
            "folder_security_level": None,
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

def clean_text_for_postgres(value: str | None) -> str:
    """
    Removes characters Postgres/Supabase cannot store in text fields.
    Fixes errors like:
    unsupported Unicode escape sequence
    \\u0000 cannot be converted to text
    """

    if value is None:
        return ""

    text = str(value)

    # Remove NULL bytes and escaped NULL sequences
    text = text.replace("\x00", "")
    text = text.replace("\\u0000", "")
    text = text.replace("\u0000", "")

    # Remove other unsafe control characters but keep normal whitespace
    cleaned_chars = []

    for char in text:
        code = ord(char)

        if char in ["\n", "\r", "\t"]:
            cleaned_chars.append(char)
        elif code >= 32:
            cleaned_chars.append(char)

    return "".join(cleaned_chars).strip()

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

            extracted_text = clean_text_for_postgres(extracted_text)

        if file_type == "pdf":
            should_run_pdf_ocr = False

            extracted_clean = (extracted_text or "").strip()
            digit_count = sum(char.isdigit() for char in extracted_clean)

            # Scanned PDFs often return little or no text.
            if len(extracted_clean) < 150:
                should_run_pdf_ocr = True

            # Invoices/receipts usually contain several numbers.
            # If almost no numbers were extracted, OCR is needed.
            if digit_count < 8:
                should_run_pdf_ocr = True

            # Also run OCR for likely financial files by filename.
            # This is generic, not vendor-specific.
            lower_filename = (filename or "").lower()
            financial_file_words = [
                "invoice",
                "receipt",
                "quote",
                "statement",
                "purchase",
                "order",
                "bill",
                "payment",
                "tax",
                "vat",
            ]

            if any(word in lower_filename for word in financial_file_words):
                should_run_pdf_ocr = True

            if should_run_pdf_ocr:
                file.seek(0)

                pdf_ocr_text = extract_ocr_from_pdf_pages(
                    file=file,
                    filename=filename,
                    max_pages=12
                )

                pdf_ocr_text = clean_text_for_postgres(pdf_ocr_text)

                if pdf_ocr_text:
                    if extracted_text:
                        extracted_text = clean_text_for_postgres(
                            f"{extracted_text}\n\nPDF OCR fallback text:\n{pdf_ocr_text}"
                        )
                    else:
                        extracted_text = pdf_ocr_text

                    ocr_text = pdf_ocr_text

        if file_type == "image":
            file.seek(0)
            visual_description = describe_image(file, filename)

            file.seek(0)
            ocr_text = extract_ocr_from_image(file, filename)

            file.seek(0)
            invoice_text = extract_invoice_text_from_image(file, filename)

            if ocr_text == "NO_READABLE_TEXT":
                ocr_text = ""

            if invoice_text == "NO_READABLE_TEXT":
                invoice_text = ""

            if invoice_text:
                if ocr_text:
                    ocr_text = f"{ocr_text}\n\nFinancial document extraction:\n{invoice_text}"
                else:
                    ocr_text = f"Financial document extraction:\n{invoice_text}"

            visual_description = clean_text_for_postgres(visual_description)
            ocr_text = clean_text_for_postgres(ocr_text)

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

        summary = combined_text[:1500]

        supabase.table("assets").update({
            "extracted_text": extracted_text or None,
            "visual_description": visual_description or None,
            "ocr_text": ocr_text or None,
            "detected_date": detected_date.isoformat() if detected_date else None,
            "detected_year": detected_year,
            "detected_month": detected_date.month if detected_date else None,
            "detected_day": detected_date.day if detected_date else None,
            "date_source": date_source,
            "detected_event": detected_event,
            "tags": tags,
            "summary": summary,
            "processing_status": "processed",
            "processing_error": None
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

def get_uploaded_chat_asset_rows(
    uploaded_asset_id: str,
    crew_id: str,
    yacht_id: str,
    security_level: int,
    chat_id: str
):
    """
    Gets context for one specific file/photo/doc uploaded inside the current chat.

    Used only when frontend sends uploaded_asset_id.
    Does not search unrelated Yacht Documentation.
    """

    if not uploaded_asset_id:
        return []

    asset_owner_check = supabase.table("assets") \
        .select("id, uploaded_by, chat_id, yacht_id") \
        .eq("id", uploaded_asset_id) \
        .eq("yacht_id", yacht_id) \
        .eq("chat_id", chat_id) \
        .eq("uploaded_by", crew_id) \
        .execute()

    if not asset_owner_check.data:
        raise HTTPException(status_code=403, detail="No access to this uploaded file")
    asset_res = supabase.table("assets") \
        .select("""
            id,
            yacht_id,
            chat_id,
            file_name,
            original_file_name,
            file_type,
            mime_type,
            processing_status,
            processing_error,
            extracted_text,
            visual_description,
            ocr_text,
            summary
        """) \
        .eq("id", uploaded_asset_id) \
        .eq("yacht_id", yacht_id) \
        .eq("chat_id", chat_id) \
        .execute()

    if not asset_res.data:
        raise HTTPException(
            status_code=404,
            detail="Uploaded chat file was not found for this chat"
        )

    asset = asset_res.data[0]

    rows = []

    file_name = (
        asset.get("original_file_name")
        or asset.get("file_name")
        or "Uploaded file"
    )

    file_type = asset.get("file_type") or "file"

    direct_parts = [
        f"File name: {file_name}",
        f"File type: {file_type}",
        f"Processing status: {asset.get('processing_status') or ''}",
    ]

    if asset.get("processing_error"):
        direct_parts.append(
            "Processing error:\n" + str(asset.get("processing_error"))
        )

    if asset.get("visual_description"):
        direct_parts.append(
            "Image visual description:\n" + str(asset.get("visual_description"))
        )

    if asset.get("ocr_text"):
        direct_parts.append(
            "OCR text:\n" + str(asset.get("ocr_text"))
        )

    if asset.get("extracted_text"):
        direct_parts.append(
            "Extracted document text:\n" + str(asset.get("extracted_text"))
        )

    if asset.get("summary"):
        direct_parts.append(
            "Summary:\n" + str(asset.get("summary"))
        )

    direct_context = "\n\n".join(
        part for part in direct_parts if str(part or "").strip()
    ).strip()

    if direct_context:
        rows.append({
            "asset_id": asset.get("id"),
            "yacht_id": asset.get("yacht_id"),
            "chat_id": asset.get("chat_id"),
            "security_level": security_level,
            "content": direct_context,
            "content_type": "uploaded_chat_asset",
            "chunk_index": 0,
            "detected_date": None,
            "detected_year": None,
            "tags": [],
            "file_name": asset.get("file_name"),
            "original_file_name": asset.get("original_file_name"),
            "file_type": asset.get("file_type")
        })

    try:
        chunks_res = supabase.table("asset_chunks") \
            .select("""
                asset_id,
                yacht_id,
                chat_id,
                security_level,
                content,
                content_type,
                chunk_index,
                detected_date,
                detected_year,
                tags,
                assets!inner (
                    id,
                    file_name,
                    original_file_name,
                    file_type
                )
            """) \
            .eq("asset_id", uploaded_asset_id) \
            .eq("yacht_id", yacht_id) \
            .eq("chat_id", chat_id) \
            .order("chunk_index") \
            .limit(40) \
            .execute()

        for row in chunks_res.data or []:
            asset_data = row.get("assets") or {}

            rows.append({
                "asset_id": row.get("asset_id"),
                "yacht_id": row.get("yacht_id"),
                "chat_id": row.get("chat_id"),
                "security_level": row.get("security_level"),
                "content": row.get("content"),
                "content_type": row.get("content_type"),
                "chunk_index": row.get("chunk_index"),
                "detected_date": row.get("detected_date"),
                "detected_year": row.get("detected_year"),
                "tags": row.get("tags"),
                "file_name": asset_data.get("file_name"),
                "original_file_name": asset_data.get("original_file_name"),
                "file_type": asset_data.get("file_type")
            })

    except Exception as e:
        print("UPLOADED CHAT CHUNKS LOOKUP ERROR:", type(e).__name__, str(e))

    return rows
    
def build_sources_from_asset_results(results: list[dict]) -> list[dict]:
    seen = set()
    sources = []

    for row in results:
        asset_id = row.get("asset_id")

        if not asset_id or asset_id in seen:
            continue

        seen.add(asset_id)

        file_name = (
            row.get("original_file_name")
            or row.get("file_name")
            or "Untitled document"
        )

        sources.append({
            "asset_id": asset_id,
            "title": file_name,
            "file_name": file_name
        })

    return sources

def is_bad_uploaded_file_answer(answer: str, query: str) -> bool:
    """
    Generic quality guard for uploaded-file answers.
    Prevents lazy, one-word, vague, or non-answer replies.
    No content hard-coding.
    """

    clean_answer = (answer or "").strip()
    clean_query = (query or "").strip().lower()

    if not clean_answer:
        return True

    lower_answer = clean_answer.lower().strip(" .,!?\n\t")

    bad_short_answers = {
        "good",
        "bad",
        "yes",
        "no",
        "maybe",
        "ok",
        "okay",
        "fine",
        "unclear",
        "not sure",
    }

    if lower_answer in bad_short_answers:
        return True

    if len(clean_answer.split()) < 8:
        return True

    vague_phrases = [
        "the uploaded file appears",
        "the uploaded file is",
        "the image appears",
        "the image is",
    ]

    asks_specific_followup = any(
        phrase in clean_query
        for phrase in [
            "what type",
            "what kind",
            "why",
            "is this good",
            "good or no",
            "tell me more",
            "explain",
            "calculate",
            "how much",
            "total",
        ]
    )

    if asks_specific_followup and any(clean_answer.lower().startswith(p) for p in vague_phrases):
        return True

    return False

def is_weak_uploaded_answer(answer: str, query: str) -> bool:
    """
    Blocks lazy/caption-like answers for uploaded images/files.
    """

    clean_answer = (answer or "").strip()
    clean_query = (query or "").strip().lower()

    if not clean_answer:
        return True

    lower_answer = clean_answer.lower().strip()

    if len(clean_answer.split()) < 12:
        return True

    bad_exact_answers = {
        "good.",
        "good",
        "bad.",
        "bad",
        "yes.",
        "yes",
        "no.",
        "no",
        "maybe.",
        "maybe",
        "it depends.",
        "it depends",
    }

    if lower_answer in bad_exact_answers:
        return True

    weak_starts = [
        "the uploaded file is an image",
        "the uploaded file appears",
        "the image shows",
        "the image appears",
        "this image shows",
        "based on the uploaded image, i can provide",
        "based on the visual description",
    ]

    specific_question_markers = [
        "is it good",
        "is this good",
        "good or no",
        "recommend",
        "buy",
        "buying",
        "what type",
        "what kind",
        "tell me more",
        "why",
        "calculate",
        "total",
        "how much",
    ]

    is_specific_question = any(marker in clean_query for marker in specific_question_markers)

    if is_specific_question and any(lower_answer.startswith(start) for start in weak_starts):
        return True

    vague_phrases = [
        "it is difficult to determine",
        "cannot be determined",
        "without more information",
    ]

    # This is only weak if it says limitation but gives no useful next step/evidence.
    if is_specific_question:
        has_vague = any(phrase in lower_answer for phrase in vague_phrases)
        has_useful_next_step = any(
            phrase in lower_answer
            for phrase in [
                "check",
                "inspect",
                "survey",
                "engine",
                "maintenance",
                "price",
                "condition",
                "visible",
                "evidence",
                "before buying",
            ]
        )

        if has_vague and not has_useful_next_step:
            return True

    return False

def answer_from_uploaded_chat_asset(
    query: str,
    context: str,
    matched_rows: list[dict]
):
    """
    Answers questions about a file/photo/document uploaded inside the current chat.

    This function must behave like an assistant, not like an image-caption tool.
    """

    clean_context = (context or "").strip()
    clean_query = (query or "").strip()

    if not clean_context:
        return {
            "answer": (
                "I received the uploaded file, but I could not read or analyse its contents yet. "
                "Please try uploading it again, or check the backend processing_error for this asset."
            ),
            "sources": []
        }

    try:
        answer = ask_llm(
            query=clean_query,
            context=f"""
You are BridgeOS.

The user is asking about a file/image/document they uploaded in this chat.

Your job:
Answer the user's latest question directly, like a practical assistant.

Hard rules:
- Do NOT behave like an image captioning model.
- Do NOT start with "Based on the uploaded image" unless absolutely necessary.
- Do NOT repeat the same visual description again and again.
- Do NOT simply restate the uploaded context.
- Do NOT give one-word answers.
- Do NOT say only "good", "bad", "yes", or "no".
- Do NOT invent facts.
- Use only the uploaded file context.
- Use British English.
- Plain text only.

For image questions:
- Answer the actual question.
- If the question asks what type/kind it is, give the broad visible category and evidence.
- If the question asks whether it is good or recommended, explain that this cannot be confirmed from the image alone.
- You may comment on visible design/use-case only.
- You must NOT judge true condition, value, safety, seaworthiness, mechanical state, maintenance, survey status, or whether to buy unless those facts are visible/readable in the context.
- If a buyer asks whether to buy it, say what the image suggests visually, then list what must be checked before buying.

For invoice/document questions:
- Extract visible fields from the uploaded context.
- If it is an invoice, receipt, quote, purchase order, statement, or bill, look for supplier, invoice number, date, line items, quantities, unit prices, subtotal, VAT/tax, total, and currency.
- If the user asks for a calculation, calculate only from visible numbers.
- Show the arithmetic briefly.
- If numbers are missing, say exactly which numbers are missing.
- Do not invent missing values.

Style:
- Be direct.
- Be useful.
- Prefer 2 to 5 short paragraphs or bullets.
- Do not over-explain.
- Do not include source names inside the answer.

User question:
{clean_query}

Uploaded file context:
{clean_context}

Now answer the user's question directly.
""".strip()
        )

        answer = str(answer or "").strip()

    except Exception as e:
        print("UPLOADED CHAT ASSET LLM ERROR:", type(e).__name__, str(e))
        answer = ""

    if is_weak_uploaded_answer(answer, clean_query):
        try:
            answer = ask_llm(
                query=clean_query,
                context=f"""
Rewrite the answer below because it is weak, repetitive, or caption-like.

User wants a direct practical answer, not a generic image description.

Rules:
- Do not start with "Based on the uploaded image".
- Do not repeat the whole image description.
- Answer the user's question directly.
- If asked whether the boat is good/recommended, explain visible positives and what cannot be judged from the image.
- If asked whether to buy, say you cannot recommend buying from an image alone and list checks needed.
- Use only the uploaded context.
- Use British English.
- Plain text only.

User question:
{clean_query}

Weak answer:
{answer}

Uploaded context:
{clean_context}

Better answer:
""".strip()
            )

            answer = str(answer or "").strip()

        except Exception as e:
            print("UPLOADED CHAT ASSET REWRITE ERROR:", type(e).__name__, str(e))

    if not answer:
        answer = (
            "I can see the uploaded file context, but I could not generate a reliable answer from it. "
            "Please try again or check whether the file was processed successfully."
        )

    sources = []

    if matched_rows:
        sources = build_sources_from_asset_results([matched_rows[0]])

    return {
        "answer": answer,
        "sources": sources
    }
    
def get_asset_permissions(
    asset_id: str,
    admin_crew: dict
):
    """
    Returns current document permissions.

    Synced permission sources:
    - assets.security_level controls automatic Tier 1 / 2 / 3 access.
    - asset_access controls manual custom grants, including Tier 4 users.
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can view asset permissions"
        )

    asset_res = supabase.table("assets") \
        .select("*") \
        .eq("id", asset_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not asset_res.data:
        raise HTTPException(status_code=404, detail="Asset not found for this yacht")

    asset = asset_res.data[0]
    asset_security_level = int(asset.get("security_level") or 1)

    crew_res = supabase.table("crew") \
        .select("id, email, full_name, role, position, security_level") \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .order("security_level") \
        .order("full_name") \
        .execute()

    access_res = supabase.table("asset_access") \
        .select("asset_id, crew_id, granted_by, created_at") \
        .eq("asset_id", asset_id) \
        .execute()

    manual_access_by_crew_id = {
        row["crew_id"]: row
        for row in (access_res.data or [])
        if row.get("crew_id")
    }

    people = []

    for crew in crew_res.data or []:
        crew_security_level = int(crew.get("security_level") or 4)

        if crew_security_level in [1, 2, 3]:
            has_tier_access = asset_security_level >= crew_security_level
        else:
            has_tier_access = False

        has_manual_access = crew["id"] in manual_access_by_crew_id

        people.append({
            "crew_id": crew["id"],
            "email": crew.get("email"),
            "full_name": crew.get("full_name"),
            "role": crew.get("role"),
            "position": crew.get("position"),
            "security_level": crew_security_level,
            "has_tier_access": has_tier_access,
            "has_manual_access": has_manual_access,
            "can_view": has_tier_access or has_manual_access,
            "manual_access_row": manual_access_by_crew_id.get(crew["id"])
        })

    return {
        "asset": {
            "id": asset["id"],
            "file_name": asset.get("file_name"),
            "original_file_name": asset.get("original_file_name"),
            "security_level": asset_security_level
        },
        "tier_options": [
            {
                "security_level": 1,
                "label": "Tier 1 only",
                "description": "Tier 1 users can view automatically. Others need manual access."
            },
            {
                "security_level": 2,
                "label": "Tier 1 and Tier 2",
                "description": "Tier 1 and Tier 2 users can view automatically. Tier 3 and 4 need manual access."
            },
            {
                "security_level": 3,
                "label": "Tier 1, Tier 2, and Tier 3",
                "description": "Tier 1, Tier 2, and Tier 3 users can view automatically. Tier 4 still needs manual access."
            }
        ],
        "people": people
    }


def update_asset_permissions(
    asset_id: str,
    security_level: int,
    crew_ids: list[str],
    admin_crew: dict
):
    """
    Updates document permissions and keeps database in sync.

    Database sync:
    - assets.security_level controls automatic Tier 1 / 2 / 3 access.
    - asset_chunks.security_level stays synced for retrieval.
    - asset_access controls manual grants for Tier 4 and optional extra grants for Tier 2/3.
    """

    if int(admin_crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can update asset permissions"
        )

    security_level = int(security_level)

    if security_level not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="Document security_level must be 1, 2, or 3. Tier 4 is custom user access only."
        )

    asset_res = supabase.table("assets") \
        .select("*") \
        .eq("id", asset_id) \
        .eq("yacht_id", admin_crew["yacht_id"]) \
        .execute()

    if not asset_res.data:
        raise HTTPException(status_code=404, detail="Asset not found for this yacht")

    clean_crew_ids = []

    for crew_id in crew_ids or []:
        clean_crew_id = str(crew_id).strip()

        if clean_crew_id and clean_crew_id not in clean_crew_ids:
            clean_crew_ids.append(clean_crew_id)

    if clean_crew_ids:
        crew_res = supabase.table("crew") \
            .select("id") \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .in_("id", clean_crew_ids) \
            .execute()

        valid_crew_ids = {
            row["id"]
            for row in (crew_res.data or [])
        }

        missing_ids = [
            crew_id
            for crew_id in clean_crew_ids
            if crew_id not in valid_crew_ids
        ]

        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail="One or more crew members were not found for this yacht"
            )

    now = datetime.now(timezone.utc).isoformat()

    try:
        supabase.table("assets") \
            .update({
                "security_level": security_level,
                "permissions_updated_at": now,
                "permissions_updated_by": admin_crew["id"]
            }) \
            .eq("id", asset_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()

        supabase.table("asset_chunks") \
            .update({
                "security_level": security_level
            }) \
            .eq("asset_id", asset_id) \
            .eq("yacht_id", admin_crew["yacht_id"]) \
            .execute()

        supabase.table("asset_access") \
            .delete() \
            .eq("asset_id", asset_id) \
            .execute()

        rows = [
            {
                "asset_id": asset_id,
                "crew_id": crew_id,
                "granted_by": admin_crew["id"]
            }
            for crew_id in clean_crew_ids
        ]

        if rows:
            supabase.table("asset_access").insert(rows).execute()

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not update asset permissions: {str(e)}"
        )

    return get_asset_permissions(
        asset_id=asset_id,
        admin_crew=admin_crew
    )
    
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

def parse_llm_json_response(raw_text: str):
    """
    Safely parses a JSON object from the LLM response.
    No hardcoded questions. No keyword matching.
    """

    if not raw_text:
        return None

    raw_text = str(raw_text).strip()

    try:
        return json.loads(raw_text)
    except Exception:
        pass

    try:
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1

        if start >= 0 and end > start:
            return json.loads(raw_text[start:end])
    except Exception:
        pass

    return None

def build_retrieval_queries(query: str) -> list[str]:
    """
    Generic multi-part retrieval query builder.
    No hardcoded document names or topics.
    """

    clean_query = (query or "").strip()

    if not clean_query:
        return []

    queries = [clean_query]

    separators = [
        "?",
        ";",
        "\n",
        " and ",
        " also ",
        " plus ",
        " as well as ",
        " together with ",
        " along with ",
        " then ",
        " compare ",
        " versus ",
        " vs "
    ]

    candidate_parts = [clean_query]

    for separator in separators:
        next_parts = []

        for part in candidate_parts:
            split_parts = part.split(separator)
            next_parts.extend(split_parts)

        candidate_parts = next_parts

    for part in candidate_parts:
        part = part.strip(" .,-;:\n\t")

        if len(part) >= 5:
            queries.append(part)

    tokens = (
        clean_query
        .replace(",", " ")
        .replace("?", " ")
        .replace(";", " ")
        .replace(":", " ")
        .replace("(", " ")
        .replace(")", " ")
        .replace('"', " ")
        .replace("'", " ")
        .split()
    )

    for token in tokens:
        token = token.strip(" .,-;:\n\t")

        has_digit = any(char.isdigit() for char in token)
        has_separator = "-" in token or "/" in token or "_" in token

        # Generic reference-like tokens:
        # SOP-SAF-008, SMM/c/ac/003, SAF_001, 2023, etc.
        if len(token) >= 4 and (has_separator or has_digit):
            queries.append(token)

    unique_queries = []
    seen = set()

    for item in queries:
        item = item.strip()
        key = item.lower()

        if item and key not in seen:
            seen.add(key)
            unique_queries.append(item)

    return unique_queries[:10]

def keyword_match_asset_chunks(
    retrieval_query: str,
    allowed_asset_ids: list[str],
    yacht_id: str,
    limit: int = 8
) -> list[dict]:
    """
    Generic keyword/file-name fallback.

    No hardcoded document names, years, reports, SOPs, or topics.

    It scores chunks by:
    - useful query tokens found in file name
    - useful query tokens found in original file name
    - useful query tokens found in chunk content
    - earlier chunks get a small boost because they often contain title/purpose
    """

    if not retrieval_query or not allowed_asset_ids:
        return []

    clean_query = retrieval_query.strip()

    if not clean_query:
        return []

    stop_words = {
        "what", "when", "where", "who", "why", "how",
        "must", "should", "does", "do", "did", "the",
        "and", "or", "of", "at", "in", "on", "to",
        "a", "an", "is", "are", "be", "for", "with",
        "give", "tell", "me", "about", "summary",
        "summarize", "please", "can", "you", "from",
        "report", "document", "file", "form","previous", 
        "current", "user", "request", "chat","conversation", 
        "message", "question"
    }

    raw_tokens = (
        clean_query
        .replace(",", " ")
        .replace("?", " ")
        .replace(";", " ")
        .replace(":", " ")
        .replace("(", " ")
        .replace(")", " ")
        .replace('"', " ")
        .replace("'", " ")
        .split()
    )

    tokens = []

    for token in raw_tokens:
        clean_token = token.strip(" .,-;:\n\t()[]{}").lower()

        if len(clean_token) < 3:
            continue

        if clean_token in stop_words:
            continue

        tokens.append(clean_token)

    if not tokens:
        return []

    scored_rows = {}

    def normalize_row(row):
        asset_data = row.get("assets") or {}

        return {
            "asset_id": row.get("asset_id"),
            "yacht_id": row.get("yacht_id"),
            "chat_id": row.get("chat_id"),
            "security_level": row.get("security_level"),
            "content": row.get("content"),
            "content_type": row.get("content_type"),
            "chunk_index": row.get("chunk_index"),
            "detected_date": row.get("detected_date"),
            "detected_year": row.get("detected_year"),
            "tags": row.get("tags"),
            "file_name": asset_data.get("file_name"),
            "original_file_name": asset_data.get("original_file_name"),
            "file_type": asset_data.get("file_type")
        }

    def score_row(row):
        file_name = str(row.get("file_name") or "").lower()
        original_file_name = str(row.get("original_file_name") or "").lower()
        content = str(row.get("content") or "").lower()

        file_text = f"{file_name} {original_file_name}"

        file_hits = sum(1 for token in tokens if token in file_text)
        content_hits = sum(1 for token in tokens if token in content)

        score = 0

        score += file_hits * 30
        score += content_hits * 6

        if len(tokens) >= 2 and file_hits >= 2:
            score += 80

        if len(tokens) >= 2 and content_hits >= 2:
            score += 25

        try:
            chunk_index = int(row.get("chunk_index") or 0)
            if chunk_index <= 2:
                score += 8
        except Exception:
            pass

        return score

    def add_row(row):
        normalized = normalize_row(row)

        key = (
            normalized.get("asset_id"),
            normalized.get("chunk_index"),
            normalized.get("content_type")
        )

        score = score_row(normalized)

        if score <= 0:
            return

        existing = scored_rows.get(key)

        if not existing or score > existing["score"]:
            scored_rows[key] = {
                "score": score,
                "row": normalized
            }

    try:
        # Search matching file names first.
        matching_asset_ids = set()

        for token in tokens[:8]:
            asset_res = supabase.table("assets") \
                .select("id, file_name, original_file_name") \
                .eq("yacht_id", yacht_id) \
                .in_("id", allowed_asset_ids) \
                .or_(
                    f"file_name.ilike.%{token}%,original_file_name.ilike.%{token}%"
                ) \
                .limit(30) \
                .execute()

            for asset in asset_res.data or []:
                asset_id = asset.get("id")

                if asset_id:
                    matching_asset_ids.add(asset_id)

        if matching_asset_ids:
            chunks_res = supabase.table("asset_chunks") \
                .select("""
                    asset_id,
                    yacht_id,
                    chat_id,
                    security_level,
                    content,
                    content_type,
                    chunk_index,
                    detected_date,
                    detected_year,
                    tags,
                    assets!inner (
                        id,
                        file_name,
                        original_file_name,
                        file_type
                    )
                """) \
                .eq("yacht_id", yacht_id) \
                .in_("asset_id", list(matching_asset_ids)) \
                .order("chunk_index") \
                .limit(limit * 4) \
                .execute()

            for row in chunks_res.data or []:
                add_row(row)

    except Exception as e:
        print("KEYWORD FILE SEARCH ERROR:", type(e).__name__, str(e))

    try:
        # Search chunk content with useful query tokens.
        for token in tokens[:8]:
            chunk_res = supabase.table("asset_chunks") \
                .select("""
                    asset_id,
                    yacht_id,
                    chat_id,
                    security_level,
                    content,
                    content_type,
                    chunk_index,
                    detected_date,
                    detected_year,
                    tags,
                    assets!inner (
                        id,
                        file_name,
                        original_file_name,
                        file_type
                    )
                """) \
                .eq("yacht_id", yacht_id) \
                .in_("asset_id", allowed_asset_ids) \
                .ilike("content", f"%{token}%") \
                .limit(limit * 4) \
                .execute()

            for row in chunk_res.data or []:
                add_row(row)

    except Exception as e:
        print("KEYWORD CHUNK SEARCH ERROR:", type(e).__name__, str(e))

    ranked = sorted(
        scored_rows.values(),
        key=lambda item: item["score"],
        reverse=True
    )

    return [item["row"] for item in ranked[:limit]]

def keyword_search_asset_chunks(
    query: str,
    yacht_id: str,
    allowed_asset_ids: list[str],
    year_filter: int | None = None,
    limit: int = 8
) -> list[dict]:
    """
    Compatibility wrapper.
    chat() calls keyword_search_asset_chunks, while the real implementation is keyword_match_asset_chunks.
    """

    return keyword_match_asset_chunks(
        retrieval_query=query,
        allowed_asset_ids=allowed_asset_ids,
        yacht_id=yacht_id,
        limit=limit
    )

def get_recent_chat_context(chat_id: str, limit: int = 6) -> str:
    """
    Gets recent chat messages.

    This is used only to resolve follow-up references like:
    - it
    - that
    - this
    - they
    - the above
    - the previous answer

    It is not used as factual evidence.
    """

    try:
        res = supabase.table("messages") \
            .select("role, content, created_at") \
            .eq("chat_id", chat_id) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()

        rows = list(reversed(res.data or []))

        parts = []

        for row in rows:
            role = row.get("role") or "message"
            content = (row.get("content") or "").strip()

            if content:
                parts.append(f"{role}: {content}")

        return "\n".join(parts)

    except Exception as e:
        print("RECENT CHAT CONTEXT ERROR:", type(e).__name__, str(e))
        return ""


def build_standalone_retrieval_query(query: str, chat_id: str) -> str:
    """
    Turns a follow-up question into a standalone retrieval query using recent chat.

    No hardcoded document names.
    No hardcoded topics.
    No hardcoded example questions.

    The LLM uses recent chat only to resolve references.
    """

    recent_chat_context = get_recent_chat_context(
        chat_id=chat_id,
        limit=6
    )

    if not recent_chat_context.strip():
        return query

    try:
        rewritten = ask_llm(
            query=query,
            context=f"""
You rewrite user questions for document retrieval.

Your task:
- Use the recent conversation only to resolve references in the current question.
- If the current question depends on previous context, rewrite it as a complete standalone search query.
- If it is already standalone, return it unchanged.
- Do not answer the question.
- Do not add facts.
- Do not invent document names.
- Do not invent topics.
- Do not hardcode anything.
- Return only the rewritten search query as plain text.

Recent conversation:
{recent_chat_context}

Current question:
{query}
""".strip()
        )

        rewritten = str(rewritten or "").strip()

        if rewritten:
            return rewritten

    except Exception as e:
        print("STANDALONE QUERY REWRITE ERROR:", type(e).__name__, str(e))

    return query

def get_previous_user_query(chat_id: str, current_query: str) -> str:
    """
    Gets the previous user message in this same chat.

    This is generic memory for follow-up questions.
    It does not hardcode document names, topics, SOPs, reports, years, or examples.
    """

    try:
        res = supabase.table("messages") \
            .select("role, content, created_at") \
            .eq("chat_id", chat_id) \
            .eq("role", "user") \
            .order("created_at", desc=True) \
            .limit(6) \
            .execute()

        rows = res.data or []
        current_clean = (current_query or "").strip()

        for index, row in enumerate(rows):
            content = (row.get("content") or "").strip()

            if not content:
                continue

            # The newest row is usually the current message because chat()
            # inserts it before retrieval. Skip it.
            if index == 0 and content == current_clean:
                continue

            return content

    except Exception as e:
        print("PREVIOUS USER QUERY ERROR:", type(e).__name__, str(e))

    return ""


def build_memory_aware_retrieval_input(query: str, chat_id: str) -> str:
    """
    Builds a retrieval query using the previous user message as chat memory.

    The previous message is used only to resolve context.
    The answer must still come from uploaded document chunks.
    """

    previous_query = get_previous_user_query(
        chat_id=chat_id,
        current_query=query
    )

    if not previous_query:
        return query

    return f"""
Previous user request in this chat:
{previous_query}

Current user request:
{query}
""".strip()

def get_latest_chat_asset_id(
    chat_id: str,
    crew_id: str,
    yacht_id: str,
    security_level: int
) -> str | None:
    """
    Gets the latest uploaded asset attached to this exact chat.

    This is generic chat memory:
    - no hardcoded phrases
    - no hardcoded document names
    - no hardcoded examples
    """

    try:
        accessible_asset_ids = get_accessible_asset_ids(
            crew_id=crew_id,
            yacht_id=yacht_id,
            security_level=security_level
        )

        if not accessible_asset_ids:
            return None

        res = supabase.table("assets") \
            .select("id, chat_id, created_at") \
            .eq("chat_id", chat_id) \
            .eq("yacht_id", yacht_id) \
            .in_("id", accessible_asset_ids) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if not res.data:
            return None

        return res.data[0].get("id")

    except Exception as e:
        print("LATEST CHAT ASSET ERROR:", type(e).__name__, str(e))
        return None
# ------------------------
# CHAT SECURE
# ------------------------
def is_file_listing_query(query: str) -> bool:
    """
    Detects generic questions asking what uploaded files/documents exist.
    """

    clean = (query or "").lower().strip()

    listing_phrases = [
        "what invoices",
        "which invoices",
        "list invoices",
        "show invoices",
        "uploaded invoices",

        "what documents",
        "which documents",
        "list documents",
        "show documents",
        "uploaded documents",

        "what files",
        "which files",
        "list files",
        "show files",
        "uploaded files",

        "what docs",
        "which docs",
        "list docs",
        "show docs",

        "what do we have uploaded",
        "what have we uploaded",
        "what is uploaded",
        "what's uploaded"
    ]

    return any(phrase in clean for phrase in listing_phrases)

def answer_file_listing_directly(
    query: str,
    rows: list[dict]
) -> dict:
    """
    Answers file/document/invoice listing questions directly from asset metadata.

    This avoids the LLM returning the fallback even when files exist.
    """

    clean_query = (query or "").lower()

    if not rows:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    filtered_rows = []

    for row in rows:
        file_name = (
            row.get("original_file_name")
            or row.get("file_name")
            or "Untitled document"
        )

        content = str(row.get("content") or "").lower()
        file_name_lower = file_name.lower()

        if "invoice" in clean_query:
            if "invoice" not in file_name_lower and "invoice" not in content:
                continue

        filtered_rows.append(row)

    if not filtered_rows:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    lines = []

    if "invoice" in clean_query:
        lines.append("The uploaded invoice files I can see are:")
    else:
        lines.append("The uploaded documents/files I can see are:")

    for row in filtered_rows:
        file_name = (
            row.get("original_file_name")
            or row.get("file_name")
            or "Untitled document"
        )

        file_type = row.get("file_type") or "file"
        status = row.get("processing_status") or ""

        extra = []

        if file_type:
            extra.append(file_type)

        if status:
            extra.append(status)

        if extra:
            lines.append(f"- {file_name} ({', '.join(extra)})")
        else:
            lines.append(f"- {file_name}")

    return {
        "answer": "\n".join(lines),
        "sources": build_sources_from_asset_results(filtered_rows)
    }

def get_asset_metadata_rows_for_listing(
    query: str,
    yacht_id: str,
    allowed_asset_ids: list[str],
    limit: int = 50
) -> list[dict]:
    """
    Returns asset metadata rows when the user asks what files/documents exist.

    This prevents valid file-listing questions from falling back just because
    the answer is in asset metadata rather than inside a document paragraph.
    """

    if not allowed_asset_ids:
        return []

    clean_query = (query or "").lower()

    try:
        res = supabase.table("assets") \
            .select("""
                id,
                yacht_id,
                chat_id,
                security_level,
                file_name,
                original_file_name,
                file_type,
                mime_type,
                processing_status,
                processing_error,
                summary,
                extracted_text,
                visual_description,
                ocr_text,
                detected_year,
                tags,
                created_at
            """) \
            .eq("yacht_id", yacht_id) \
            .in_("id", allowed_asset_ids) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()

    except Exception as e:
        print("ASSET METADATA LISTING ERROR:", type(e).__name__, str(e))
        return []

    rows = []

    for asset in res.data or []:
        file_name = (
            asset.get("original_file_name")
            or asset.get("file_name")
            or "Untitled document"
        )

        file_name_lower = file_name.lower()
        summary = asset.get("summary") or ""
        extracted_text = asset.get("extracted_text") or ""
        visual_description = asset.get("visual_description") or ""
        ocr_text = asset.get("ocr_text") or ""

        # For invoice questions, prefer invoice-like files.
        # This is generic by file name/content, not hardcoded to any vendor.
        if "invoice" in clean_query:
            searchable = " ".join([
                file_name_lower,
                summary.lower(),
                extracted_text.lower(),
                visual_description.lower(),
                ocr_text.lower()
            ])

            if "invoice" not in searchable:
                continue

        content = f"""
File name: {file_name}
File type: {asset.get("file_type") or ""}
MIME type: {asset.get("mime_type") or ""}
Processing status: {asset.get("processing_status") or ""}
Processing error: {asset.get("processing_error") or ""}
Detected year: {asset.get("detected_year") or ""}
Tags: {", ".join(asset.get("tags") or [])}

Summary:
{summary}

Extracted text preview:
{extracted_text[:2000]}

Image visual description:
{visual_description[:1000]}

OCR text:
{ocr_text[:1000]}
""".strip()

        rows.append({
            "asset_id": asset.get("id"),
            "yacht_id": asset.get("yacht_id"),
            "chat_id": asset.get("chat_id"),
            "security_level": asset.get("security_level"),
            "content": content,
            "content_type": "asset_metadata_listing",
            "chunk_index": 0,
            "detected_date": None,
            "detected_year": asset.get("detected_year"),
            "tags": asset.get("tags") or [],
            "file_name": asset.get("file_name"),
            "original_file_name": asset.get("original_file_name"),
            "file_type": asset.get("file_type")
        })

    return rows
 
def parse_llm_json_response(raw_text: str) -> dict | None:
    if not raw_text:
        return None

    text = str(raw_text).strip()

    if text.startswith("```"):
        text = text.strip("`").strip()

        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        return None

    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None

def build_numbered_context_from_asset_results(rows):
    parts = []

    for index, row in enumerate(rows or [], start=1):
        file_name = (
            row.get("original_file_name")
            or row.get("file_name")
            or row.get("title")
            or "Unknown source"
        )

        content = row.get("content") or row.get("text") or ""

        if not str(content).strip():
            continue

        parts.append(
            f"""
[SOURCE {index}]
File: {file_name}
Content:
{str(content).strip()}
""".strip()
        )

    return "\n\n---\n\n".join(parts)


def normalise_for_source_check(value):
    return " ".join(str(value or "").lower().split())


def source_quote_exists_in_row(row, quote):
    if not row or not quote:
        return False

    quote_text = normalise_for_source_check(quote)

    if not quote_text:
        return False

    row_text = normalise_for_source_check(
        row.get("content")
        or row.get("text")
        or ""
    )

    if not row_text:
        return False

    return quote_text in row_text


def verified_source_rows_from_llm_result(parsed, matched_rows):
    if not parsed or not isinstance(parsed, dict):
        return []

    if not bool(parsed.get("document_used")):
        return []

    used_sources = parsed.get("used_sources") or []

    if not isinstance(used_sources, list):
        return []

    verified_rows = []

    for used_source in used_sources:
        if not isinstance(used_source, dict):
            continue

        try:
            source_number = int(used_source.get("source_number"))
        except Exception:
            continue

        quote = str(used_source.get("evidence_quote") or "").strip()

        if source_number <= 0:
            continue

        index = source_number - 1

        if index < 0 or index >= len(matched_rows):
            continue

        row = matched_rows[index]

        if source_quote_exists_in_row(row, quote):
            verified_rows.append(row)
        else:
            print(
                "SOURCE VERIFICATION FAILED:",
                {
                    "source_number": source_number,
                    "quote": quote[:200]
                }
            )

    return verified_rows

def build_numbered_context_from_asset_results(rows):
    parts = []

    for index, row in enumerate(rows or [], start=1):
        file_name = (
            row.get("original_file_name")
            or row.get("file_name")
            or row.get("title")
            or "Unknown source"
        )

        content = row.get("content") or row.get("text") or ""

        if not str(content).strip():
            continue

        parts.append(
            f"""
[SOURCE {index}]
File: {file_name}
Content:
{str(content).strip()}
""".strip()
        )

    return "\n\n---\n\n".join(parts)


    
def validate_answer_supported_by_source(query: str, answer: str, source_row: dict) -> bool:
    """
    Checks whether one selected source directly supports the answer
    to the user's exact question.

    This prevents:
    - hallucinated answers
    - loosely related documents being used as sources
    - random retrieved chunks being shown as source cards
    """

    clean_query = str(query or "").strip()
    clean_answer = str(answer or "").strip()

    if not clean_query or not clean_answer:
        return False

    if clean_answer == FALLBACK_NO_DATA_ANSWER:
        return False

    source_content = str(source_row.get("content") or "").strip()

    if not source_content:
        return False

    try:
        raw = ask_llm(
            query=clean_query,
            context=f"""
You are validating whether a document source directly supports an answer.

Return ONLY valid JSON:

{{
  "supported": true
}}

or:

{{
  "supported": false
}}

Rules:
- supported=true ONLY if the source text directly answers the user's exact question.
- supported=true ONLY if the answer is clearly grounded in this source text.
- supported=false if the source is only loosely related.
- supported=false if the source is about a similar topic but does not answer the exact question.
- supported=false if the answer uses outside knowledge.
- supported=false if the answer adds facts not present in the source.
- supported=false if the source would not be enough for a human to verify the answer.
- Do not explain.
- Return JSON only.

User question:
{clean_query}

Answer:
{clean_answer}

Source text:
{source_content}
""".strip()
        )

        parsed = parse_llm_json_response(raw)

        if parsed and isinstance(parsed, dict):
            return bool(parsed.get("supported"))

    except Exception as e:
        print("SOURCE SUPPORT VALIDATION ERROR:", type(e).__name__, str(e))

    return False
def classify_bridgeos_query_scope(query: str) -> str:
    """
    Generic classifier.

    conversational = can be answered without documents
    factual = must be answered from uploaded/retrieved documents only

    No hardcoded topics, products, brands, vendors, yacht terms, or specific questions.
    """

    clean_query = str(query or "").strip()

    if not clean_query:
        return "conversational"

    try:
        raw = ask_llm(
            query=clean_query,
            context=f"""
You are classifying a user message for a private document-based assistant.

Return ONLY one word:

conversational

or

factual

Definitions:

conversational:
- The user is greeting, thanking, saying goodbye, acknowledging, or making brief small talk.
- The user is asking how to use the assistant or how to search/upload/find documents.
- The user is not asking for factual information about the world, operations, products, people, procedures, values, dates, recommendations, explanations, or stored records.

factual:
- The user asks for any information, explanation, recommendation, instruction, fact, value, date, person, responsibility, procedure, comparison, calculation, product detail, operational detail, or document-based answer.
- The user asks "what", "which", "who", "when", "where", "why", "how", "how much", "should", "can", "does", "is", or asks for a definition/explanation that is not just app usage.
- The answer would require knowledge or data, whether from documents or general knowledge.

Rules:
- If the message could require factual knowledge, return factual.
- If unsure, return factual.
- Do not answer the user.
- Do not explain.
- Return only conversational or factual.

User message:
{clean_query}
""".strip()
        )

        value = str(raw or "").strip().lower()

        if value == "conversational":
            return "conversational"

        return "factual"

    except Exception as e:
        print("QUERY SCOPE CLASSIFIER ERROR:", type(e).__name__, str(e))
        return "factual"


def filter_rows_that_directly_answer_query(query: str, rows: list[dict]) -> list[dict]:
    """
    Keeps only rows that directly contain the answer to the user's exact question.

    No hardcoded topics, products, brands, vendors, yacht terms, files, or specific questions.
    """

    clean_query = str(query or "").strip()

    if not clean_query or not rows:
        return []

    context = build_context_from_asset_results(rows[:20])

    if not context.strip():
        return []

    try:
        raw = ask_llm(
            query=clean_query,
            context=f"""
You are checking retrieved document chunks for a document-based assistant.

Return ONLY valid JSON:

{{
  "direct_source_numbers": [1]
}}

or:

{{
  "direct_source_numbers": []
}}

Rules:
- Select a source ONLY if it directly contains the information needed to answer the user's exact question.
- Do not select a source because it is generally related.
- Do not select a source because it mentions a similar topic.
- Do not select a source if the answer would require outside knowledge.
- Do not select a source if it does not contain the actual answer.
- If none of the sources directly answer the exact question, return an empty list.
- Do not explain.
- Return JSON only.

User question:
{clean_query}

Retrieved sources:
{context}
""".strip()
        )

        parsed = parse_llm_json_response(raw)

        if not parsed or not isinstance(parsed, dict):
            return []

        numbers = parsed.get("direct_source_numbers") or []

        if not isinstance(numbers, list):
            return []

        direct_rows = []

        for number in numbers:
            try:
                index = int(number) - 1
            except Exception:
                continue

            if 0 <= index < len(rows[:20]):
                direct_rows.append(rows[index])

        return direct_rows

    except Exception as e:
        print("DIRECT SOURCE FILTER ERROR:", type(e).__name__, str(e))
        return []


def chat(
    query: str,
    crew_id: str,
    yacht_id: str,
    security_level: int,
    chat_id: str,
    uploaded_asset_id: str | None = None
):
    """
    Secure BridgeOS chat.

    Behaviour:
    - Conversational/app-use messages can be answered without documents.
    - Any factual question must be answered only from document context.
    - If no document context directly answers the question, return FALLBACK_NO_DATA_ANSWER.
    - Sources show only when the answer is taken from selected document rows.
    - No hardcoded topics, brands, vendors, product names, or question examples.
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
        "content": query,
        "sources": []
    }).execute()

    if chat_row.get("title") == "New Chat":
        supabase.table("chats").update({
            "title": query[:60],
            "updated_at": "now()"
        }).eq("id", chat_id).eq("crew_id", crew_id).eq("yacht_id", yacht_id).execute()

    answer = ""
    sources = []
    matched_rows = []
    context = ""
    retrieval_query_input = query

    query_scope = classify_bridgeos_query_scope(query)

    # Only use an uploaded chat asset if the frontend explicitly sends it
    # for this exact message. Do not reuse the previous uploaded file.
    resolved_uploaded_asset_id = uploaded_asset_id

    # Conversational/app-help messages are allowed without documents.
    if query_scope == "conversational" and not resolved_uploaded_asset_id:
        try:
            raw_answer = ask_llm(
                query=query,
                context="""
You are BridgeOS, a helpful private document assistant.

The user message is conversational or about using the assistant.

Rules:
- Reply briefly and naturally.
- You may explain that you can help search uploaded documents.
- Do not answer factual, technical, operational, product, financial, legal, medical, recommendation, or external-knowledge questions.
- Do not invent document data.
- Do not claim you used a document.
- Use British English.
- Return plain text only.
""".strip()
            )

            answer = str(raw_answer or "").strip() or "Hello. How can I help?"

        except Exception as e:
            print("CONVERSATIONAL CHAT ERROR:", type(e).__name__, str(e))
            answer = "Hello. How can I help?"

        sources = []

        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": yacht_id,
            "crew_id": crew_id,
            "role": "assistant",
            "content": answer,
            "sources": sources
        }).execute()

        supabase.table("chats").update({
            "updated_at": "now()"
        }).eq("id", chat_id).eq("crew_id", crew_id).eq("yacht_id", yacht_id).execute()

        return {
            "answer": answer,
            "sources": sources,
            "uploaded_asset_id": None
        }

    try:
        if resolved_uploaded_asset_id:
            print(
                "LOCAL CHAT DEBUG: using explicit uploaded asset:",
                resolved_uploaded_asset_id
            )

            matched_rows = get_uploaded_chat_asset_rows(
                uploaded_asset_id=resolved_uploaded_asset_id,
                crew_id=crew_id,
                yacht_id=yacht_id,
                security_level=security_level,
                chat_id=chat_id
            )

            if matched_rows:
                context = build_context_from_asset_results(matched_rows)
            else:
                context = ""

        else:
            accessible_asset_ids = get_accessible_asset_ids(
                crew_id=crew_id,
                yacht_id=yacht_id,
                security_level=security_level
            )

            if accessible_asset_ids:
                assets_res = supabase.table("assets") \
                    .select("id") \
                    .eq("yacht_id", yacht_id) \
                    .in_("id", accessible_asset_ids) \
                    .eq("processing_status", "processed") \
                    .execute()

                allowed_asset_ids = [
                    asset["id"]
                    for asset in (assets_res.data or [])
                    if asset.get("id")
                ]

                print("LOCAL CHAT DEBUG: allowed_asset_ids:", allowed_asset_ids)

                if allowed_asset_ids:
                    retrieval_query_input = build_memory_aware_retrieval_input(
                        query=query,
                        chat_id=chat_id
                    )

                    print("LOCAL CHAT DEBUG: retrieval_query_input:", retrieval_query_input)

                    retrieval_queries = build_retrieval_queries(retrieval_query_input)
                    matched_rows_by_key = {}

                    if is_file_listing_query(query):
                        listing_rows = get_asset_metadata_rows_for_listing(
                            query=query,
                            yacht_id=yacht_id,
                            allowed_asset_ids=allowed_asset_ids,
                            limit=50
                        )

                        for row in listing_rows:
                            key = (
                                row.get("asset_id"),
                                row.get("chunk_index"),
                                row.get("content_type")
                            )

                            if key not in matched_rows_by_key:
                                matched_rows_by_key[key] = row

                    for retrieval_query in retrieval_queries:
                        filters = extract_query_filters(retrieval_query)
                        year_filter = filters.get("year")

                        keyword_rows = keyword_search_asset_chunks(
                            query=retrieval_query,
                            yacht_id=yacht_id,
                            allowed_asset_ids=allowed_asset_ids,
                            year_filter=year_filter,
                            limit=10
                        )

                        for row in keyword_rows:
                            key = (
                                row.get("asset_id"),
                                row.get("chunk_index"),
                                row.get("content_type")
                            )

                            if key not in matched_rows_by_key:
                                matched_rows_by_key[key] = row

                        try:
                            semantic_results = supabase.rpc("match_asset_chunks_secure", {
                                "query_embedding": embed(retrieval_query),
                                "match_count": 6,
                                "allowed_asset_ids": allowed_asset_ids,
                                "yacht_filter": yacht_id,
                                "year_filter": year_filter
                            }).execute()

                            for row in semantic_results.data or []:
                                key = (
                                    row.get("asset_id"),
                                    row.get("chunk_index"),
                                    row.get("content_type")
                                )

                                if key not in matched_rows_by_key:
                                    matched_rows_by_key[key] = row

                        except Exception as e:
                            print("SEMANTIC SEARCH ERROR:", type(e).__name__, str(e))

                    matched_rows = list(matched_rows_by_key.values())[:30]

                    print("LOCAL CHAT DEBUG: matched chunks:", len(matched_rows))

                    if matched_rows:
                        context = build_context_from_asset_results(matched_rows)
                    else:
                        context = ""

    except Exception as e:
        print("LOCAL CHAT DOCUMENT SEARCH ERROR:", type(e).__name__, str(e))
        matched_rows = []
        context = ""

    # Factual question with no retrieved document context = sorry answer.
    if not context:
        answer = FALLBACK_NO_DATA_ANSWER
        sources = []

    elif is_file_listing_query(query):
        listing_result = answer_file_listing_directly(
            query=query,
            rows=matched_rows
        )

        answer = listing_result.get("answer") or FALLBACK_NO_DATA_ANSWER
        sources = listing_result.get("sources") or []

        if answer.strip() == FALLBACK_NO_DATA_ANSWER:
            sources = []

    else:
        raw_answer = ask_llm(
            query=query,
            context=f"""
You are BridgeOS, a private document-based assistant.

Always respond in British English.

You may answer ONLY if the document context directly answers the user's exact question.

You MUST return ONLY valid JSON in this exact shape:

{{
  "answer": "clear user-facing answer",
  "document_used": true,
  "used_sources": [
    {{
      "source_number": 1,
      "evidence_quote": "short exact quote copied from the selected source"
    }}
  ]
}}

or:

{{
  "answer": "{FALLBACK_NO_DATA_ANSWER}",
  "document_used": false,
  "used_sources": []
}}

Rules:
- Answer only from the document context below.
- Do not use general knowledge.
- Do not fill gaps.
- Do not infer facts that are not in the context.
- Do not answer from loosely related context.
- If the exact answer is not directly present in the context, answer exactly:
{FALLBACK_NO_DATA_ANSWER}
- Set "document_used": true only if the final answer is directly taken from the context.
- If "document_used" is true, include at least one item in "used_sources".
- Each used source must include a valid "source_number".
- Each "evidence_quote" should be copied from the selected source.
- Do not include a source just because it was retrieved.
- If no source directly supports the answer, use the fallback answer.
- Do not include document names inside the answer.
- Return JSON only.

User question:
{query}

Document context:
{context}
""".strip()
        )

        parsed = parse_llm_json_response(raw_answer)

        answer = ""
        sources = []

        if parsed and isinstance(parsed, dict):
            answer = str(parsed.get("answer") or "").strip()
            document_used = bool(parsed.get("document_used"))

            used_source_numbers = []

            # Preferred shape: used_sources = [{"source_number": 1, ...}]
            raw_used_sources = parsed.get("used_sources") or []

            if isinstance(raw_used_sources, list):
                for item in raw_used_sources:
                    if isinstance(item, dict):
                        try:
                            source_number = int(item.get("source_number"))

                            if source_number > 0:
                                used_source_numbers.append(source_number)
                        except Exception:
                            pass

            # Backwards compatibility if model returns used_source_numbers.
            if not used_source_numbers:
                raw_used_source_numbers = parsed.get("used_source_numbers") or []

                if isinstance(raw_used_source_numbers, list):
                    for number in raw_used_source_numbers:
                        try:
                            number = int(number)

                            if number > 0:
                                used_source_numbers.append(number)
                        except Exception:
                            pass

            if not answer:
                answer = FALLBACK_NO_DATA_ANSWER
                document_used = False
                used_source_numbers = []

            if answer.strip() == FALLBACK_NO_DATA_ANSWER:
                document_used = False
                used_source_numbers = []

            if document_used:
                source_rows = []

                for source_number in used_source_numbers:
                    index = source_number - 1

                    if 0 <= index < len(matched_rows):
                        source_rows.append(matched_rows[index])

                if source_rows:
                    sources = build_sources_from_asset_results(source_rows)
                else:
                    # Never keep a document answer without a valid source.
                    answer = FALLBACK_NO_DATA_ANSWER
                    sources = []
            else:
                sources = []

        else:
            print("LOCAL CHAT JSON SOURCE PARSE FAILED:", str(raw_answer)[:500])
            answer = FALLBACK_NO_DATA_ANSWER
            sources = []

        if not answer:
            answer = FALLBACK_NO_DATA_ANSWER
            sources = []

        if answer.strip() == FALLBACK_NO_DATA_ANSWER:
            sources = []

    supabase.table("messages").insert({
        "chat_id": chat_id,
        "yacht_id": yacht_id,
        "crew_id": crew_id,
        "role": "assistant",
        "content": answer,
        "sources": sources
    }).execute()

    supabase.table("chats").update({
        "updated_at": "now()"
    }).eq("id", chat_id).eq("crew_id", crew_id).eq("yacht_id", yacht_id).execute()

    return {
        "answer": answer,
        "sources": sources,
        "uploaded_asset_id": resolved_uploaded_asset_id
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

def _clean_reset_email(email: str) -> str:
    return (email or "").strip().lower()


def _make_reset_code() -> str:
    return str(random.randint(100000, 999999))


def _hash_reset_code(email: str, code: str) -> str:
    raw = f"{_clean_reset_email(email)}:{code}:{SUPABASE_JWT_SECRET}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _send_password_reset_code_email(email: str, code: str):
    """
    Sends password reset code using Brevo Transactional Email API.
    """

    if not BREVO_API_KEY or not BREVO_FROM_EMAIL:
        raise HTTPException(
            status_code=500,
            detail="Password reset email is not configured. Missing Brevo settings."
        )

    subject = "Your BridgeOS password reset code"

    text_content = f"""
Hello,

Your BridgeOS password reset code is:

{code}

This code expires in 15 minutes.

If you did not request this password reset, you can ignore this email.

BridgeOS
""".strip()

    html_content = f"""
<!doctype html>
<html>
  <body style="font-family: Arial, sans-serif; color: #111827; line-height: 1.5;">
    <p>Hello,</p>

    <p>Your BridgeOS password reset code is:</p>

    <p style="font-size: 28px; font-weight: bold; letter-spacing: 4px;">
      {code}
    </p>

    <p>This code expires in 15 minutes.</p>

    <p>If you did not request this password reset, you can ignore this email.</p>

    <p>If you don’t see this email in your inbox, please check your spam or junk folder.</p>


    <p>BridgeOS</p>
  </body>
</html>
""".strip()

    payload = {
        "sender": {
            "name": BREVO_FROM_NAME,
            "email": BREVO_FROM_EMAIL
        },
        "to": [
            {
                "email": email
            }
        ],
        "subject": subject,
        "htmlContent": html_content,
        "textContent": text_content,
        "tags": ["password-reset"]
    }

    try:
        response = requests.post(
            BREVO_API_URL,
            json=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "api-key": BREVO_API_KEY
            },
            timeout=20
        )

        print("BREVO PASSWORD RESET DEBUG: status:", response.status_code)
        print("BREVO PASSWORD RESET DEBUG: response:", response.text[:500])

        if response.status_code >= 400:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Brevo failed to send password reset email: "
                    f"{response.status_code}: {response.text[:500]}"
                )
            )

    except HTTPException:
        raise

    except Exception as e:
        print("BREVO PASSWORD RESET ERROR:", type(e).__name__, str(e))

        raise HTTPException(
            status_code=500,
            detail=(
                "Could not send password reset email through Brevo: "
                f"{type(e).__name__}: {str(e)}"
            )
        )
def forgot_password(email: str):
    """
    Sends a 6-digit password reset verification code by email.

    Security behaviour:
    - Always returns a generic message.
    - Does not reveal whether the email exists.
    - Stores only a hash of the code.
    - Code expires after 15 minutes.
    """

    clean_email = _clean_reset_email(email)

    if not clean_email:
        raise HTTPException(status_code=400, detail="Email is required")

    crew_res = supabase.table("crew") \
        .select("id, email") \
        .ilike("email", clean_email) \
        .limit(1) \
        .execute()

    generic_response = {
        "message": "If this email exists, a verification code has been sent."
    }

    if not crew_res.data:
        return generic_response

    code = _make_reset_code()
    code_hash = _hash_reset_code(clean_email, code)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    try:
        auth_admin.table("password_reset_codes").insert({
            "email": clean_email,
            "code_hash": code_hash,
            "code_preview": code,
            "expires_at": expires_at,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "sent_provider": "brevo"
        }).execute()

        auth_admin.table("password_reset_codes").insert({
            "email": clean_email,
            "code_hash": code_hash,
            "expires_at": expires_at
        }).execute()

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not create password reset code: {str(e)}"
        )

    print("PASSWORD RESET DEBUG: sending email to", clean_email)

    _send_password_reset_code_email(clean_email, code)

    print("PASSWORD RESET DEBUG: email sent successfully")

    return generic_response


def confirm_forgot_password(email: str, code: str, new_password: str):
    """
    Verifies the emailed code and updates the user's Supabase Auth password.
    """

    clean_email = _clean_reset_email(email)
    clean_code = (code or "").strip()

    if not clean_email:
        raise HTTPException(status_code=400, detail="Email is required")

    if not clean_code:
        raise HTTPException(status_code=400, detail="Verification code is required")

    if not new_password or len(new_password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters"
        )

    code_hash = _hash_reset_code(clean_email, clean_code)
    now = datetime.now(timezone.utc)

    try:
        code_res = auth_admin.table("password_reset_codes") \
            .select("*") \
            .ilike("email", clean_email) \
            .eq("code_hash", code_hash) \
            .is_("used_at", "null") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not verify reset code: {str(e)}"
        )

    if not code_res.data:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    reset_row = code_res.data[0]

    attempts = int(reset_row.get("attempts") or 0)

    if attempts >= 5:
        raise HTTPException(
            status_code=400,
            detail="Too many attempts. Please request a new code."
        )

    expires_at_raw = reset_row.get("expires_at")

    try:
        expires_at = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    if expires_at < now:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    crew_res = supabase.table("crew") \
        .select("id, email, yacht_id") \
        .ilike("email", clean_email) \
        .limit(1) \
        .execute()

    if not crew_res.data:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    crew = crew_res.data[0]
    crew_id = crew["id"]

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
            detail=f"Could not reset password in Supabase Auth: {str(e)}"
        )

    try:
        auth_admin.table("password_reset_codes") \
            .update({
                "used_at": now.isoformat(),
                "attempts": attempts + 1
            }) \
            .eq("id", reset_row["id"]) \
            .execute()

        supabase.table("crew") \
            .update({
                "password_updated_at": now.isoformat(),
                "password_updated_by": crew_id,
                "password_reset_by_role": "forgot_password",
                "must_change_password": False
            }) \
            .eq("id", crew_id) \
            .execute()

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Password was reset, but database sync failed: {str(e)}"
        )

    return {
        "message": "Password reset successfully. Please log in with your new password."
    }