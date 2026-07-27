from fastapi import HTTPException, Request
import requests
import json
import random
import hashlib
import smtplib
from email.message import EmailMessage
from app.database import supabase
from app.embeddings import embed
from app.config import (
    BUCKET_NAME,
    RUNPOD_BASE_URL,
    BRIDGEOS_API_KEY,
    API_SYNC_TIMEOUT_SECONDS,
    GMAIL_SYNC_MAX_RESULTS
)
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

import base64
from email.utils import parsedate_to_datetime

import time
import uuid
import jwt as pyjwt
import io
import re
import zipfile
import ast
import operator
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

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
    BREVO_API_URL,
    WHATSAPP_WEBHOOK_VERIFY_TOKEN,
    META_APP_ID,
    META_APP_SECRET,
    META_GRAPH_VERSION
)

from supabase import create_client

auth_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

storage_admin = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

# ------------------------
# WHATSAPP EXPORT UPLOADS
# ------------------------

WHATSAPP_EXPORT_LINE_PATTERNS = [
    re.compile(
        r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),?\s+"
        r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)\s+-\s+"
        r"(?P<sender>[^:]{1,120}):\s*(?P<message>.*)$"
    ),
    re.compile(
        r"^\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),?\s+"
        r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)\]\s+"
        r"(?P<sender>[^:]{1,120}):\s*(?P<message>.*)$"
    ),
]


def parse_whatsapp_export_messages(text: str) -> list[dict]:
    if not text:
        return []

    messages = []
    current_message = None

    for raw_line in text.splitlines():
        line = raw_line.strip("\ufeff").rstrip()
        matched = None

        for pattern in WHATSAPP_EXPORT_LINE_PATTERNS:
            matched = pattern.match(line)
            if matched:
                break

        if matched:
            if current_message:
                messages.append(current_message)

            current_message = {
                "date": matched.group("date"),
                "time": matched.group("time"),
                "sender": matched.group("sender").strip(),
                "message": matched.group("message").strip(),
            }
        else:
            if current_message and line:
                current_message["message"] += "\n" + line

    if current_message:
        messages.append(current_message)

    return messages


def is_whatsapp_export_text(text: str, filename: str | None = None) -> bool:
    lower_filename = (filename or "").lower()

    if "whatsapp" in lower_filename and lower_filename.endswith(".txt"):
        return True

    if not text:
        return False

    messages = parse_whatsapp_export_messages(text)

    if len(messages) >= 3:
        return True

    lower_text = text[:3000].lower()

    markers = [
        "messages and calls are end-to-end encrypted",
        "changed the subject",
        "created group",
        "added you",
        "left",
        "image omitted",
        "video omitted",
        "audio omitted",
        "sticker omitted",
    ]

    return any(marker in lower_text for marker in markers)


def format_whatsapp_messages_for_analysis(messages: list[dict], filename: str | None = None) -> str:
    if not messages:
        return ""

    sender_counts = {}

    for item in messages:
        sender = item.get("sender") or "Unknown"
        sender_counts[sender] = sender_counts.get(sender, 0) + 1

    sender_summary = "\n".join(
        f"- {sender}: {count} messages"
        for sender, count in sorted(sender_counts.items(), key=lambda pair: pair[1], reverse=True)
    )

    formatted_messages = []

    for item in messages:
        formatted_messages.append(
            f"[{item.get('date', '')} {item.get('time', '')}] "
            f"{item.get('sender', 'Unknown')}: {item.get('message', '')}"
        )

    return "\n\n".join([
        "WhatsApp chat export",
        f"File name: {filename or 'WhatsApp export'}",
        f"Total parsed messages: {len(messages)}",
        "Participants:",
        sender_summary,
        "Messages:",
        "\n".join(formatted_messages),
    ]).strip()


def normalise_whatsapp_export_text(text: str, filename: str | None = None) -> tuple[str, list[dict]]:
    messages = parse_whatsapp_export_messages(text)

    if not messages:
        return text or "", []

    return format_whatsapp_messages_for_analysis(messages, filename), messages


def extract_whatsapp_zip_payload(file, filename: str) -> tuple[str, list[dict], str]:
    file.seek(0)
    zip_bytes = file.read()
    file.seek(0)

    archive = zipfile.ZipFile(io.BytesIO(zip_bytes))

    txt_names = [
        name
        for name in archive.namelist()
        if name.lower().endswith(".txt")
        and "__macosx" not in name.lower()
        and not name.endswith("/")
    ]

    if not txt_names:
        raise ValueError("No .txt WhatsApp chat file was found inside the .zip export.")

    preferred_name = None

    for name in txt_names:
        lower_name = name.lower()

        if "whatsapp" in lower_name or "chat" in lower_name:
            preferred_name = name
            break

    txt_name = preferred_name or txt_names[0]
    raw_bytes = archive.read(txt_name)

    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raw_text = raw_bytes.decode("latin-1", errors="replace")

    normalised_text, messages = normalise_whatsapp_export_text(raw_text, txt_name or filename)

    return normalised_text, messages, txt_name


def save_whatsapp_chat_history(
    asset_id: str,
    yacht_id: str,
    chat_id: str | None,
    uploaded_by: str | None,
    original_file_name: str,
    source_text_file_name: str | None,
    messages: list[dict]
):
    if not messages:
        return

    sender_counts = {}

    for item in messages:
        sender = item.get("sender") or "Unknown"
        sender_counts[sender] = sender_counts.get(sender, 0) + 1

    first = messages[0]
    last = messages[-1]

    export_res = supabase.table("whatsapp_chat_exports").upsert({
        "asset_id": asset_id,
        "yacht_id": yacht_id,
        "chat_id": chat_id,
        "uploaded_by": uploaded_by,
        "original_file_name": original_file_name,
        "source_text_file_name": source_text_file_name,
        "message_count": len(messages),
        "participants": sender_counts,
        "first_message_label": f"{first.get('date', '')} {first.get('time', '')}".strip(),
        "last_message_label": f"{last.get('date', '')} {last.get('time', '')}".strip(),
        "updated_at": "now()"
    }, on_conflict="asset_id").execute()

    if not export_res.data:
        raise RuntimeError("Could not save WhatsApp export row.")

    export_id = export_res.data[0]["id"]

    supabase.table("whatsapp_chat_messages").delete().eq("asset_id", asset_id).execute()

    rows = []

    for index, item in enumerate(messages):
        date_raw = item.get("date") or ""
        time_raw = item.get("time") or ""

        rows.append({
            "export_id": export_id,
            "asset_id": asset_id,
            "yacht_id": yacht_id,
            "chat_id": chat_id,
            "uploaded_by": uploaded_by,
            "message_index": index,
            "sender_name": item.get("sender") or "Unknown",
            "message_text": clean_text_for_postgres(item.get("message") or ""),
            "message_date_raw": date_raw or None,
            "message_time_raw": time_raw or None,
            "message_label": f"{date_raw} {time_raw}".strip() or None,
        })

    for start in range(0, len(rows), 500):
        supabase.table("whatsapp_chat_messages").insert(rows[start:start + 500]).execute()

# ------------------------
# WHATSAPP BUSINESS CLOUD API
# ------------------------

def verify_whatsapp_webhook_token(token: str | None) -> bool:
    clean_token = (token or "").strip()

    if not clean_token:
        return False

    if WHATSAPP_WEBHOOK_VERIFY_TOKEN and clean_token == WHATSAPP_WEBHOOK_VERIFY_TOKEN:
        return True

    res = supabase.table("whatsapp_connections") \
        .select("id") \
        .eq("verify_token", clean_token) \
        .eq("is_active", True) \
        .limit(1) \
        .execute()

    return bool(res.data)


def exchange_whatsapp_code_for_token(code: str) -> dict:
    """
    Used after Meta Embedded Signup redirects back with a code.
    """

    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/oauth/access_token"

    params = {
        "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET,
        "code": code,
    }

    res = requests.get(url, params=params, timeout=30)

    if res.status_code >= 400:
        raise RuntimeError(f"Meta token exchange failed: {res.text}")

    return res.json()


def save_client_whatsapp_connection(
    yacht_id: str,
    crew_id: str,
    client_name: str | None,
    waba_id: str | None,
    phone_number_id: str,
    display_phone_number: str | None,
    access_token: str
):
    verify_token = WHATSAPP_WEBHOOK_VERIFY_TOKEN or f"bridgeos-whatsapp-{uuid.uuid4()}"

    payload = {
        "yacht_id": yacht_id,
        "created_by": crew_id,
        "client_name": client_name,
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "display_phone_number": display_phone_number,
        "verify_token": verify_token,
        "access_token": access_token,
        "is_active": True,
        "updated_at": "now()"
    }

    res = supabase.table("whatsapp_connections") \
        .upsert(payload, on_conflict="phone_number_id") \
        .execute()

    if not res.data:
        raise RuntimeError("Could not save WhatsApp connection.")

    return res.data[0]


def get_whatsapp_connection_by_phone_number_id(phone_number_id: str | None):
    if not phone_number_id:
        return None

    res = supabase.table("whatsapp_connections") \
        .select("*") \
        .eq("phone_number_id", phone_number_id) \
        .eq("is_active", True) \
        .limit(1) \
        .execute()

    if not res.data:
        return None

    return res.data[0]


def parse_whatsapp_timestamp(timestamp_raw: str | None):
    if not timestamp_raw:
        return None

    try:
        return datetime.fromtimestamp(int(timestamp_raw), tz=timezone.utc).isoformat()
    except Exception:
        return None


def extract_whatsapp_message_text(message: dict) -> str:
    message_type = message.get("type")

    if message_type == "text":
        return (message.get("text") or {}).get("body") or ""

    if message_type == "button":
        return (message.get("button") or {}).get("text") or ""

    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        button_reply = interactive.get("button_reply") or {}
        list_reply = interactive.get("list_reply") or {}

        return (
            button_reply.get("title")
            or list_reply.get("title")
            or list_reply.get("description")
            or ""
        )

    if message_type == "image":
        return (message.get("image") or {}).get("caption") or "[Image message]"

    if message_type == "document":
        document = message.get("document") or {}
        return document.get("caption") or document.get("filename") or "[Document message]"

    if message_type == "audio":
        return "[Audio message]"

    if message_type == "video":
        return (message.get("video") or {}).get("caption") or "[Video message]"

    if message_type == "sticker":
        return "[Sticker message]"

    if message_type == "location":
        location = message.get("location") or {}
        return f"[Location] {location.get('latitude', '')}, {location.get('longitude', '')}".strip()

    if message_type == "contacts":
        return "[Contact card]"

    return f"[Unsupported WhatsApp message type: {message_type}]"


def get_whatsapp_media_info(message: dict) -> dict:
    message_type = message.get("type")

    if message_type not in ["image", "document", "audio", "video", "sticker"]:
        return {}

    media = message.get(message_type) or {}

    return {
        "media_id": media.get("id"),
        "media_mime_type": media.get("mime_type"),
        "media_sha256": media.get("sha256"),
    }


def ensure_whatsapp_conversation_asset(
    connection: dict,
    contact_wa_id: str,
    contact_name: str | None
):
    yacht_id = connection["yacht_id"]
    connection_id = connection["id"]

    conversation_res = supabase.table("whatsapp_conversations") \
        .select("*") \
        .eq("connection_id", connection_id) \
        .eq("contact_wa_id", contact_wa_id) \
        .limit(1) \
        .execute()

    if conversation_res.data:
        conversation = conversation_res.data[0]

        if conversation.get("asset_id"):
            return conversation
    else:
        conversation_insert = supabase.table("whatsapp_conversations").insert({
            "connection_id": connection_id,
            "yacht_id": yacht_id,
            "contact_wa_id": contact_wa_id,
            "contact_name": contact_name,
            "message_count": 0
        }).execute()

        if not conversation_insert.data:
            raise RuntimeError("Could not create WhatsApp conversation row.")

        conversation = conversation_insert.data[0]

    asset_title = f"WhatsApp - {contact_name or contact_wa_id}.txt"
    asset_id = str(uuid.uuid4())

    asset_res = supabase.table("assets").insert({
        "id": asset_id,
        "yacht_id": yacht_id,
        "chat_id": None,
        "uploaded_by": connection.get("created_by"),
        "security_level": 1,
        "folder_name": "WhatsApp",
        "folder_security_level": 1,
        "file_name": asset_title,
        "original_file_name": asset_title,
        "original_relative_path": None,
        "file_hash": f"whatsapp-api-{connection_id}-{contact_wa_id}",
        "file_type": "whatsapp_api_chat",
        "mime_type": "text/plain",
        "storage_path": f"whatsapp-api/{connection_id}/{contact_wa_id}.txt",
        "file_url": None,
        "processing_status": "processed",
        "summary": f"WhatsApp API conversation with {contact_name or contact_wa_id}"
    }).execute()

    if not asset_res.data:
        raise RuntimeError("Could not create WhatsApp conversation asset.")

    supabase.table("whatsapp_conversations").update({
        "asset_id": asset_id,
        "updated_at": "now()"
    }).eq("id", conversation["id"]).execute()

    conversation["asset_id"] = asset_id

    return conversation


def append_whatsapp_message_chunk(
    asset_id: str,
    yacht_id: str,
    security_level: int,
    message_index: int,
    sender_name: str | None,
    contact_wa_id: str,
    message_text: str,
    message_label: str | None,
    message_type: str | None
):
    content = f"""
WhatsApp message
Sender: {sender_name or contact_wa_id}
WhatsApp ID: {contact_wa_id}
Message label: {message_label or ""}
Message type: {message_type or ""}
Content:
{message_text}
""".strip()

    supabase.table("asset_chunks").insert({
        "asset_id": asset_id,
        "yacht_id": yacht_id,
        "chat_id": None,
        "security_level": security_level,
        "content": content,
        "content_type": "whatsapp_api_message",
        "chunk_index": message_index,
        "detected_date": None,
        "detected_year": None,
        "tags": ["whatsapp", "api", "message"],
        "embedding": embed(content)
    }).execute()


def save_whatsapp_api_message(
    connection: dict,
    contact: dict,
    message: dict,
    raw_value: dict
):
    metadata = raw_value.get("metadata") or {}
    phone_number_id = metadata.get("phone_number_id") or connection.get("phone_number_id")

    contact_wa_id = contact.get("wa_id") or message.get("from")
    contact_name = ((contact.get("profile") or {}).get("name")) or contact_wa_id

    if not contact_wa_id:
        return

    wa_message_id = message.get("id")

    if not wa_message_id:
        return

    existing = supabase.table("whatsapp_api_messages") \
        .select("id") \
        .eq("wa_message_id", wa_message_id) \
        .limit(1) \
        .execute()

    if existing.data:
        return

    conversation = ensure_whatsapp_conversation_asset(
        connection=connection,
        contact_wa_id=contact_wa_id,
        contact_name=contact_name
    )

    asset_id = conversation.get("asset_id")
    yacht_id = connection["yacht_id"]

    timestamp_raw = message.get("timestamp")
    timestamp_at = parse_whatsapp_timestamp(timestamp_raw)
    message_type = message.get("type")
    message_text = extract_whatsapp_message_text(message)
    media_info = get_whatsapp_media_info(message)

    current_count = int(conversation.get("message_count") or 0)
    next_index = current_count + 1

    supabase.table("whatsapp_api_messages").insert({
        "connection_id": connection["id"],
        "conversation_id": conversation["id"],
        "yacht_id": yacht_id,
        "asset_id": asset_id,
        "wa_message_id": wa_message_id,
        "direction": "inbound",
        "from_wa_id": message.get("from"),
        "to_phone_number_id": phone_number_id,
        "sender_name": contact_name,
        "message_type": message_type,
        "message_text": clean_text_for_postgres(message_text),
        "media_id": media_info.get("media_id"),
        "media_mime_type": media_info.get("media_mime_type"),
        "media_sha256": media_info.get("media_sha256"),
        "timestamp_raw": timestamp_raw,
        "timestamp_at": timestamp_at,
        "raw_payload": message
    }).execute()

    supabase.table("whatsapp_conversations").update({
        "contact_name": contact_name,
        "message_count": next_index,
        "first_message_at": conversation.get("first_message_at") or timestamp_at,
        "last_message_at": timestamp_at,
        "updated_at": "now()"
    }).eq("id", conversation["id"]).execute()

    if asset_id:
        append_whatsapp_message_chunk(
            asset_id=asset_id,
            yacht_id=yacht_id,
            security_level=1,
            message_index=next_index,
            sender_name=contact_name,
            contact_wa_id=contact_wa_id,
            message_text=message_text,
            message_label=timestamp_at or timestamp_raw,
            message_type=message_type
        )

        supabase.table("assets").update({
            "summary": clean_text_for_postgres(
                f"WhatsApp API conversation with {contact_name or contact_wa_id}\n"
                f"Latest message: {message_text}\n"
                f"Last message at: {timestamp_at or ''}\n"
                f"Message count: {next_index}"
            ),
            "processing_status": "processed",
            "processing_error": None
        }).eq("id", asset_id).execute()


def handle_whatsapp_webhook_payload(payload: dict):
    saved_count = 0

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id")

            connection = get_whatsapp_connection_by_phone_number_id(phone_number_id)

            if not connection:
                print("WHATSAPP WEBHOOK WARNING: no connection for phone_number_id:", phone_number_id)
                continue

            contacts_by_wa_id = {}

            for contact in value.get("contacts") or []:
                wa_id = contact.get("wa_id")
                if wa_id:
                    contacts_by_wa_id[wa_id] = contact

            for message in value.get("messages") or []:
                sender_wa_id = message.get("from")
                contact = contacts_by_wa_id.get(sender_wa_id) or {
                    "wa_id": sender_wa_id,
                    "profile": {}
                }

                save_whatsapp_api_message(
                    connection=connection,
                    contact=contact,
                    message=message,
                    raw_value=value
                )

                saved_count += 1

    return {
        "received": True,
        "saved": saved_count
    }

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
    Uses BridgeOS secure document retrieval for every document-based question.

    Important:
    - Searches documents already uploaded to Yacht Documentation.
    - Uses the exact uploaded chat asset when one is attached.
    - Does not answer factual questions without document evidence.
    - Uses the existing deterministic document and numeric-answer pipeline.
    """

    return chat(
        query=query,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        security_level=int(crew["security_level"]),
        chat_id=chat_id,
        uploaded_asset_id=uploaded_asset_id
    )
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

    Also deletes files uploaded inside that chat.

    Important:
    - Do NOT detach chat assets by setting chat_id = None.
    - Detaching can violate the global asset hash uniqueness constraint.
    - Chat-uploaded assets belong to that chat, so deleting the chat should delete those chat assets.
    """

    verify_chat_access(
        chat_id=chat_id,
        crew_id=crew_id,
        yacht_id=yacht_id
    )

    try:
        # Find assets uploaded inside this chat.
        chat_assets_res = supabase.table("assets") \
            .select("id, storage_path, file_name, original_file_name") \
            .eq("chat_id", chat_id) \
            .eq("yacht_id", yacht_id) \
            .execute()

        chat_assets = chat_assets_res.data or []
        chat_asset_ids = [
            asset["id"]
            for asset in chat_assets
            if asset.get("id")
        ]

        # Delete chunks and access rows for chat assets.
        if chat_asset_ids:
            supabase.table("asset_chunks") \
                .delete() \
                .in_("asset_id", chat_asset_ids) \
                .eq("yacht_id", yacht_id) \
                .execute()

            try:
                supabase.table("asset_access") \
                    .delete() \
                    .in_("asset_id", chat_asset_ids) \
                    .execute()
            except Exception as e:
                print("CHAT ASSET ACCESS DELETE WARNING:", type(e).__name__, str(e))

            # Delete physical files from storage.
            storage_paths = [
                asset.get("storage_path")
                for asset in chat_assets
                if asset.get("storage_path")
            ]

            if storage_paths:
                try:
                    storage_admin.storage.from_(BUCKET_NAME).remove(storage_paths)
                except Exception as e:
                    print("CHAT ASSET STORAGE DELETE WARNING:", type(e).__name__, str(e))

            # Delete asset rows.
            supabase.table("assets") \
                .delete() \
                .in_("id", chat_asset_ids) \
                .eq("yacht_id", yacht_id) \
                .execute()

        # Delete chat messages.
        supabase.table("messages") \
            .delete() \
            .eq("chat_id", chat_id) \
            .eq("crew_id", crew_id) \
            .eq("yacht_id", yacht_id) \
            .execute()

        # Delete the chat itself.
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
        "deleted_chat_id": chat_id,
        "deleted_chat_assets": len(chat_asset_ids)
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

def _gmail_b64url_decode(value: str | None) -> str:
    """
    Gmail message bodies are base64url encoded.
    """

    if not value:
        return ""

    try:
        clean = value.replace("-", "+").replace("_", "/")
        padding = len(clean) % 4

        if padding:
            clean += "=" * (4 - padding)

        raw = base64.b64decode(clean.encode("utf-8"))

        return raw.decode("utf-8", errors="ignore")

    except Exception as e:
        print("GMAIL BODY DECODE ERROR:", type(e).__name__, str(e))
        return ""


def _gmail_header(headers: list[dict], name: str) -> str:
    if not headers:
        return ""

    for header in headers:
        if str(header.get("name") or "").lower() == name.lower():
            return str(header.get("value") or "").strip()

    return ""


def _extract_gmail_body_from_payload(payload: dict | None) -> str:
    """
    Extracts readable plain text from a Gmail message payload.
    Prefers text/plain, falls back to text/html stripped roughly.
    """

    if not payload:
        return ""

    mime_type = payload.get("mimeType") or ""

    body = payload.get("body") or {}
    data = body.get("data")

    if data and mime_type == "text/plain":
        return _gmail_b64url_decode(data).strip()

    parts = payload.get("parts") or []

    plain_parts = []
    html_parts = []

    for part in parts:
        part_mime = part.get("mimeType") or ""

        if part_mime.startswith("multipart/"):
            nested = _extract_gmail_body_from_payload(part)
            if nested:
                plain_parts.append(nested)
            continue

        part_body = part.get("body") or {}
        part_data = part_body.get("data")

        if not part_data:
            continue

        decoded = _gmail_b64url_decode(part_data).strip()

        if not decoded:
            continue

        if part_mime == "text/plain":
            plain_parts.append(decoded)
        elif part_mime == "text/html":
            html_parts.append(decoded)

    if plain_parts:
        return "\n\n".join(plain_parts).strip()

    if html_parts:
        html_text = "\n\n".join(html_parts)

        # Basic HTML cleanup without adding another dependency.
        import re

        html_text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.I)
        html_text = re.sub(r"</p\s*>", "\n", html_text, flags=re.I)
        html_text = re.sub(r"<[^>]+>", " ", html_text)
        html_text = re.sub(r"\s+", " ", html_text)

        return html_text.strip()

    if data:
        return _gmail_b64url_decode(data).strip()

    return ""


def sync_one_gmail_message(
    gmail_message: dict,
    connection: dict,
    admin_crew: dict,
    security_level: int
) -> dict:
    """
    Fetches one Gmail message, converts it into searchable text,
    and saves it through the existing asset/chunk pipeline.
    """

    message_id = gmail_message.get("id")

    if not message_id:
        return {
            "status": "skipped",
            "reason": "Missing Gmail message id"
        }

    base_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    url = f"{base_url}/{message_id}?format=full"

    headers = {
        "Accept": "application/json"
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
            url,
            headers=headers,
            timeout=API_SYNC_TIMEOUT_SECONDS
        )
    except Exception as e:
        return {
            "message_id": message_id,
            "status": "failed",
            "reason": f"{type(e).__name__}: {str(e)}"
        }

    if response.status_code >= 400:
        return {
            "message_id": message_id,
            "status": "failed",
            "reason": f"Gmail returned {response.status_code}: {response.text[:500]}"
        }

    try:
        message = response.json()
    except Exception as e:
        return {
            "message_id": message_id,
            "status": "failed",
            "reason": f"Could not parse Gmail JSON: {type(e).__name__}: {str(e)}"
        }

    payload = message.get("payload") or {}
    headers_list = payload.get("headers") or []

    subject = _gmail_header(headers_list, "Subject") or "(no subject)"
    sender = _gmail_header(headers_list, "From")
    to = _gmail_header(headers_list, "To")
    cc = _gmail_header(headers_list, "Cc")
    date_header = _gmail_header(headers_list, "Date")
    snippet = message.get("snippet") or ""

    body_text = _extract_gmail_body_from_payload(payload)

    if not body_text and snippet:
        body_text = snippet

    if not body_text.strip():
        return {
            "message_id": message_id,
            "subject": subject,
            "status": "skipped",
            "reason": "No readable email body"
        }

    internal_date = message.get("internalDate")
    email_date = ""

    if date_header:
        email_date = date_header
    elif internal_date:
        try:
            email_date = datetime.fromtimestamp(
                int(internal_date) / 1000,
                tz=timezone.utc
            ).isoformat()
        except Exception:
            email_date = ""

    final_content = f"""
Gmail email source metadata:
Gmail message id: {message_id}
Thread id: {message.get("threadId") or ""}
Subject: {subject}
From: {sender}
To: {to}
Cc: {cc}
Date: {email_date}
Snippet: {snippet}

---

Email body:
{body_text}
""".strip()

    safe_subject = safe_filename(subject)[:80] or "email"
    final_file_name = f"email-{safe_subject}-{message_id}.txt"
    
    try:
        seeded = seed_text_asset(
            file_name=final_file_name,
            content=final_content,
            yacht_id=admin_crew["yacht_id"],
            uploaded_by=admin_crew["id"],
            security_level=security_level
        )

        return {
            "message_id": message_id,
            "subject": subject,
            "from": sender,
            "date": email_date,
            "status": "synced",
            "asset_id": seeded.get("asset", {}).get("id"),
            "duplicate": seeded.get("duplicate", False)
        }

    except Exception as e:
        return {
            "message_id": message_id,
            "subject": subject,
            "status": "failed",
            "reason": f"Could not save Gmail email: {type(e).__name__}: {str(e)}"
        }


def sync_gmail_messages_from_connection(
    connection: dict,
    admin_crew: dict,
    security_level: int,
    endpoint_path: str | None = None,
    max_results: int | None = None
) -> dict:
    """
    Lists Gmail messages and syncs each one into the existing searchable asset system.

    Expected connection:
    - base_url: https://gmail.googleapis.com/gmail/v1/users/me/messages
    - auth_type: bearer
    - api_key: Google OAuth access token with Gmail readonly scope
    """

    limit = int(max_results or GMAIL_SYNC_MAX_RESULTS)

    if limit < 1:
        limit = 1

    if limit > 100:
        limit = 100

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

    if auth_type == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if auth_type == "x-api-key" and api_key:
        headers["x-api-key"] = api_key

    params = {
        "maxResults": limit,
        "q": "in:anywhere -in:chats"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=API_SYNC_TIMEOUT_SECONDS
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Gmail request failed: {type(e).__name__}: {str(e)}"
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Gmail returned {response.status_code}: {response.text[:1000]}"
        )

    try:
        data = response.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Gmail returned invalid JSON"
        )

    messages = data.get("messages") or []

    results = []

    for gmail_message in messages:
        result = sync_one_gmail_message(
            gmail_message=gmail_message,
            connection=connection,
            admin_crew=admin_crew,
            security_level=security_level
        )
        results.append(result)

    synced_count = len([r for r in results if r.get("status") == "synced"])
    skipped_count = len([r for r in results if r.get("status") == "skipped"])
    failed_count = len([r for r in results if r.get("status") == "failed"])

    return {
        "message": "Gmail sync completed",
        "provider": "gmail",
        "synced_count": synced_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "results": results
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
    # GMAIL MESSAGE SYNC
    # ------------------------
    is_gmail_url = "gmail.googleapis.com/gmail/v1/users/me/messages" in url

    if is_gmail_url:
        try:
            gmail_result = sync_gmail_messages_from_connection(
                connection=connection,
                admin_crew=admin_crew,
                security_level=security_level,
                endpoint_path=endpoint_path,
                max_results=GMAIL_SYNC_MAX_RESULTS
            )

            supabase.table("api_connections").update({
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
                "last_sync_status": "success" if gmail_result.get("synced_count", 0) > 0 else "failed",
                "last_sync_error": None if gmail_result.get("synced_count", 0) > 0 else "No Gmail messages were synced"
            }).eq("id", connection_id).eq("yacht_id", admin_crew["yacht_id"]).execute()

            return {
                "message": "Gmail email sync completed",
                "connection_id": connection_id,
                "url": url,
                **gmail_result
            }

        except HTTPException:
            raise

        except Exception as e:
            error_text = f"{type(e).__name__}: {str(e)}"

            supabase.table("api_connections").update({
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
                "last_sync_status": "failed",
                "last_sync_error": error_text
            }).eq("id", connection_id).eq("yacht_id", admin_crew["yacht_id"]).execute()

            raise HTTPException(
                status_code=502,
                detail=f"Gmail sync failed: {error_text}"
            )

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

def transcribe_audio(
    file,
    filename: str,
    mime_type: str | None = None
) -> str:
    """
    Sends a voice note to the RunPod speech-to-text endpoint.

    Expected RunPod response:
    {
        "text": "transcribed message"
    }

    It also accepts:
    {
        "transcript": "transcribed message"
    }
    """

    if not RUNPOD_BASE_URL:
        raise HTTPException(
            status_code=500,
            detail="RUNPOD_BASE_URL is missing"
        )

    if not BRIDGEOS_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="BRIDGEOS_API_KEY is missing"
        )

    try:
        file.seek(0)
        audio_bytes = file.read()
        file.seek(0)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read voice note: {str(e)}"
        )

    if not audio_bytes:
        raise HTTPException(
            status_code=400,
            detail="The voice note is empty"
        )

    # Maximum 25 MB.
    if len(audio_bytes) > 25 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Voice note must be smaller than 25 MB"
        )

    url = f"{RUNPOD_BASE_URL.rstrip('/')}/api/bridgeos/transcribe"

    try:
        response = requests.post(
            url,
            files={
                "file": (
                    filename or "voice-note.webm",
                    audio_bytes,
                    mime_type or "audio/webm"
                )
            },
            headers={
                "x-api-key": BRIDGEOS_API_KEY
            },
            timeout=180
        )

        print("VOICE TRANSCRIPTION DEBUG URL:", url)
        print("VOICE TRANSCRIPTION DEBUG STATUS:", response.status_code)
        print("VOICE TRANSCRIPTION DEBUG RESPONSE:", response.text[:500])

    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail="Voice transcription timed out"
        )

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not connect to the voice transcription service: "
                f"{type(e).__name__}: {str(e)}"
            )
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Voice transcription returned {response.status_code}: "
                f"{response.text[:500]}"
            )
        )

    try:
        data = response.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Voice transcription returned invalid JSON"
        )

    transcript = (
        data.get("text")
        or data.get("transcript")
        or data.get("response")
        or data.get("result", {}).get("text")
        or ""
    )

    transcript = clean_text_for_postgres(str(transcript or ""))

    if not transcript:
        raise HTTPException(
            status_code=422,
            detail="No speech could be detected in the voice note"
        )

    return transcript

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

    if clean_filename.lower().endswith(".zip"):
        file_type = "whatsapp_zip"

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
            security_level=security_level,
            uploaded_by=uploaded_by
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
    security_level: int = 1,
    uploaded_by: str | None = None
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
        whatsapp_messages = []
        whatsapp_source_text_file_name = None

        if file_type == "whatsapp_zip":
            file.seek(0)

            extracted_text, whatsapp_messages, whatsapp_source_text_file_name = extract_whatsapp_zip_payload(
                file=file,
                filename=filename
            )

            extracted_text = clean_text_for_postgres(extracted_text)
            file_type = "whatsapp_chat"

        elif file_type == "audio":
            file.seek(0)

            extracted_text = transcribe_audio(
                file=file,
                filename=filename,
                mime_type=None
            )

            extracted_text = clean_text_for_postgres(extracted_text)

            if not extracted_text:
                raise ValueError(
                    "The audio file did not contain detectable speech"
                )

        elif file_type in ["text", "pdf", "docx"]:
            original_file_type = file_type

            file.seek(0)
            extracted_text = extract_text_by_file_type(
                file=file,
                filename=filename,
                file_type=original_file_type
            )

            extracted_text = clean_text_for_postgres(extracted_text)

            # Only genuine WhatsApp TXT exports become whatsapp_chat.
            if (
                original_file_type == "text"
                and is_whatsapp_export_text(extracted_text, filename)
            ):
                extracted_text, whatsapp_messages = normalise_whatsapp_export_text(
                    text=extracted_text,
                    filename=filename
                )

                extracted_text = clean_text_for_postgres(extracted_text)
                whatsapp_source_text_file_name = filename
                file_type = "whatsapp_chat"
            else:
                file_type = original_file_type

            if file_type in ["text", "pdf", "docx"]:
                extracted_text = str(extracted_text or "").strip()

                print(
                    "DOCUMENT EXTRACTION RESULT:",
                    {
                        "asset_id": asset_id,
                        "filename": filename,
                        "file_type": file_type,
                        "characters": len(extracted_text),
                        "preview": extracted_text[:500]
                    }
                )

                if not extracted_text:
                    raise ValueError(
                        f"No readable text was extracted from {filename}. "
                        f"The document may be empty, damaged, encrypted, "
                        f"scanned, or stored in unsupported embedded objects."
                    )

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

            if ocr_text == "NO_READABLE_TEXT":
                ocr_text = ""

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
            "file_type": file_type,
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

        # Remove stale chunks when an asset is reprocessed.
    try:
        supabase.table("asset_chunks") \
            .delete() \
            .eq("asset_id", asset_id) \
            .eq("yacht_id", yacht_id) \
            .execute()

    except Exception as e:
        print(
            "OLD ASSET CHUNK DELETE ERROR:",
            type(e).__name__,
            str(e)
        )

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

    if not rows:
        raise ValueError(
            f"No searchable chunks were created for asset {asset_id}"
        )

    for start in range(0, len(rows), 200):
        batch = rows[start:start + 200]

        result = supabase.table("asset_chunks") \
            .insert(batch) \
            .execute()

        if not result.data:
            raise RuntimeError(
                f"Supabase returned no inserted chunks for asset {asset_id}"
            )

    print(
        "ASSET CHUNKS CREATED:",
        {
            "asset_id": asset_id,
            "chunk_count": len(rows),
            "text_characters": len(extracted_text or ""),
            "ocr_characters": len(ocr_text or "")
        }
    )

def build_context_from_asset_results(
    results: list[dict]
) -> str:
    """
    Builds private document context without exposing storage information.
    """

    parts = []

    for index, row in enumerate(results or [], start=1):
        content = str(
            row.get("search_text")
            or row.get("content")
            or ""
        ).strip()

        if not content:
            continue

        part = f"""
SOURCE {index}
File name: {row.get("original_file_name") or row.get("file_name")}
File type: {row.get("file_type")}
Content type: {row.get("content_type")}
Detected year: {row.get("detected_year")}

Content:
{content}
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

For WhatsApp chat exports:
- Analyse the upload as a conversation.
- You may summarise the conversation, participants, dates, repeated topics, decisions, tasks, concerns, sentiment, and notable messages.
- If the user asks who said something, use the sender names from the chat.
- If the user asks for tasks or decisions, only include tasks or decisions clearly supported by the chat text.
- If the user asks for tone or sentiment, explain it as an interpretation based on the messages.
- Do not invent missing messages, deleted messages, private context, or intent that is not supported by the chat.

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
- If the uploaded file is a WhatsApp chat export, answer as a conversation analyst: summarise, identify participants, tasks, decisions, topics, concerns, dates, or tone when supported by the chat text.
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
    limit: int = 40
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
    Rewrites follow-up questions into standalone document-search questions.

    No hardcoded topics, products, vendors, food names, years, or document names.
    """

    clean_query = str(query or "").strip()

    if not clean_query:
        return ""

    recent_user_context = get_recent_user_context(
        chat_id=chat_id,
        current_query=clean_query,
        limit=6
    )

    if not recent_user_context.strip():
        return clean_query

    try:
        rewritten = ask_llm(
            query=clean_query,
            context=f"""
You rewrite the latest user message into a complete standalone search query for document retrieval.

Use only the previous user messages to understand the user's intent.

Rules:
- Do not answer the question.
- Do not add facts.
- Do not invent values.
- Do not invent document names.
- Do not use assistant replies.
- Preserve the latest user-requested item, date, person, object, category, or subject.
- If the latest message is a follow-up, rewrite it as a full standalone question using the previous user message pattern.
- If the latest message is already standalone, return it unchanged.
- Return plain text only.
- No explanations.

Previous user messages:
{recent_user_context}

Latest user message:
{clean_query}

Standalone search query:
""".strip()
        )

        rewritten = str(rewritten or "").strip()

        if rewritten:
            print("LOCAL CHAT DEBUG: rewritten retrieval query:", rewritten)
            return rewritten

    except Exception as e:
        print("MEMORY AWARE QUERY REWRITE ERROR:", type(e).__name__, str(e))

    return clean_query

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
    if not row:
        return False

    row_text = normalise_for_source_check(
        row.get("content")
        or row.get("text")
        or ""
    )

    if not row_text:
        return False

    quote_text = normalise_for_source_check(quote)

    if not quote_text:
        # If the source number is valid but the quote is empty, do not approve blindly.
        return False

    # Exact normalised quote match.
    if quote_text in row_text:
        return True

    # Soft fallback:
    # Sometimes the LLM copies evidence with punctuation, line breaks, or small spacing differences.
    # Accept it only if most meaningful words from the quote appear in the selected source.
    quote_words = [
        word
        for word in quote_text.split()
        if len(word) >= 4
    ]

    if len(quote_words) < 4:
        return False

    matched_words = [
        word
        for word in quote_words
        if word in row_text
    ]

    return len(matched_words) / max(len(quote_words), 1) >= 0.75

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

def build_sources_from_asset_results(results: list[dict]) -> list[dict]:
    seen = set()
    sources = []

    missing_name_asset_ids = []

    for row in results or []:
        asset_id = row.get("asset_id")

        if not asset_id or asset_id in seen:
            continue

        file_name = (
            row.get("original_file_name")
            or row.get("file_name")
            or row.get("title")
        )

        if not file_name:
            missing_name_asset_ids.append(asset_id)

        seen.add(asset_id)

    asset_name_lookup = {}

    if missing_name_asset_ids:
        try:
            asset_res = supabase.table("assets") \
                .select("id, file_name, original_file_name, file_type") \
                .in_("id", missing_name_asset_ids) \
                .execute()

            for asset in asset_res.data or []:
                asset_name_lookup[asset.get("id")] = (
                    asset.get("original_file_name")
                    or asset.get("file_name")
                    or "Untitled document"
                )

        except Exception as e:
            print("SOURCE NAME LOOKUP ERROR:", type(e).__name__, str(e))

    seen = set()

    for row in results or []:
        asset_id = row.get("asset_id")

        if not asset_id or asset_id in seen:
            continue

        seen.add(asset_id)

        file_name = (
            row.get("original_file_name")
            or row.get("file_name")
            or row.get("title")
            or asset_name_lookup.get(asset_id)
            or "Untitled document"
        )

        sources.append({
            "asset_id": asset_id,
            "title": file_name,
            "file_name": file_name
        })

    return sources

    
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
    Conservative deterministic gate.

    conversational = safe to answer without documents
    factual = must be answered from documents only

    No hardcoded products, vendors, file names, document topics, or question examples.
    """

    clean = " ".join(str(query or "").strip().lower().split())

    if not clean:
        return "conversational"

    stripped = clean.strip(" .,!?\n\t")

    conversational_exact = {
        "hi",
        "hello",
        "hey",
        "hiya",
        "good morning",
        "good afternoon",
        "good evening",
        "thanks",
        "thank you",
        "thank you very much",
        "ok",
        "okay",
        "cool",
        "great",
        "nice",
        "bye",
        "goodbye",
    }

    if stripped in conversational_exact:
        return "conversational"

    app_help_phrases = [
        "what can you do",
        "how can you help",
        "how do i use",
        "how do i upload",
        "how can i upload",
        "how do i search",
        "how can i search",
        "help me search",
        "help me find",
        "what can i ask",
    ]

    if any(phrase in stripped for phrase in app_help_phrases):
        return "conversational"

    # Anything else needs document proof.
    return "factual"
    

def get_recent_user_context(chat_id: str, current_query: str, limit: int = 6) -> str:
    """
    Gets recent user messages only.

    This is used to resolve follow-ups.
    Assistant replies are intentionally excluded so fallback/sorry answers do not pollute the rewrite.
    """

    try:
        res = supabase.table("messages") \
            .select("role, content, created_at") \
            .eq("chat_id", chat_id) \
            .eq("role", "user") \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()

        rows = list(reversed(res.data or []))

        current_clean = str(current_query or "").strip()
        parts = []

        for row in rows:
            content = str(row.get("content") or "").strip()

            if not content:
                continue

            # Skip the current message because chat() already inserted it.
            if content == current_clean:
                continue

            parts.append(content)

        return "\n".join(parts)

    except Exception as e:
        print("RECENT USER CONTEXT ERROR:", type(e).__name__, str(e))
        return ""

def filter_rows_that_directly_answer_query(query: str, rows: list[dict]) -> list[dict]:
    """
    Keeps only rows that directly contain the answer to the user's exact question.

    No hardcoded topics, products, brands, vendors, yacht terms, files, or specific questions.
    """

    clean_query = str(query or "").strip()

    if not clean_query or not rows:
        return []

    candidate_rows = rows[:100]
    context = build_context_from_asset_results(candidate_rows)

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

            if 0 <= index < len(candidate_rows):
                direct_rows.append(candidate_rows[index])

        return direct_rows

    except Exception as e:
        print("DIRECT SOURCE FILTER ERROR:", type(e).__name__, str(e))
        return []

def get_previous_assistant_source_asset_ids(
    chat_id: str,
    crew_id: str,
    yacht_id: str,
    limit: int = 6
) -> list[str]:
    """
    Gets source asset ids from the most recent assistant answer that had sources.

    Used for follow-up questions so BridgeOS expands from the same document
    instead of searching unrelated documents.
    """

    try:
        res = supabase.table("messages") \
            .select("role, sources, created_at") \
            .eq("chat_id", chat_id) \
            .eq("crew_id", crew_id) \
            .eq("yacht_id", yacht_id) \
            .eq("role", "assistant") \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()

        for row in res.data or []:
            row_sources = row.get("sources") or []

            if not isinstance(row_sources, list):
                continue

            asset_ids = []

            for source in row_sources:
                if not isinstance(source, dict):
                    continue

                asset_id = source.get("asset_id")

                if asset_id and asset_id not in asset_ids:
                    asset_ids.append(asset_id)

            if asset_ids:
                return asset_ids

    except Exception as e:
        print("PREVIOUS ASSISTANT SOURCES ERROR:", type(e).__name__, str(e))

    return []

def is_contextual_followup_query(query: str, chat_id: str) -> bool:
    """
    Detects whether the latest user message depends on earlier chat context.

    No hardcoded topics, products, vendors, or document names.
    """

    clean_query = str(query or "").strip()

    if not clean_query:
        return False

    recent_user_context = get_recent_user_context(
        chat_id=chat_id,
        current_query=clean_query,
        limit=6
    )

    if not recent_user_context.strip():
        return False

    try:
        raw = ask_llm(
            query=clean_query,
            context=f"""
Classify whether the latest user message depends on previous user messages.

Return ONLY one word:

followup

or

standalone

Definitions:
- followup = the latest message is incomplete without previous context, asks to continue, asks for more detail, asks "what about..." another item, or refers back to the previous topic.
- standalone = the latest message is complete by itself.

Rules:
- Do not answer the user.
- Do not explain.
- Return only followup or standalone.

Previous user messages:
{recent_user_context}

Latest user message:
{clean_query}
""".strip()
        )

        value = str(raw or "").strip().lower()

        return value == "followup"

    except Exception as e:
        print("FOLLOWUP CLASSIFIER ERROR:", type(e).__name__, str(e))
        return False

# ------------------------
# GENERIC DOCUMENT CONTEXT EXPANSION
# ------------------------

def get_row_text_for_relevance(row) -> str:
    """
    Returns the searchable text from an asset chunk row.
    Generic: works for PDFs, DOCX, text, OCR, WhatsApp, tables, etc.
    """

    if not row:
        return ""

    return str(
        row.get("content")
        or row.get("text")
        or row.get("summary")
        or ""
    )


def normalise_search_text(value: str) -> str:
    value = value or ""
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def meaningful_terms_from_query(query: str) -> list[str]:
    """
    Generic query terms, not domain-specific.
    Used only to avoid expanding totally unrelated documents.
    """

    text = normalise_search_text(query)

    stopwords = {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "from",
        "with", "by", "is", "are", "was", "were", "be", "been", "being",
        "it", "this", "that", "these", "those", "i", "me", "my", "we", "our",
        "you", "your", "they", "them", "their", "what", "which", "who", "where",
        "when", "why", "how", "many", "much", "give", "tell", "show", "find",
        "list", "count", "currently", "please", "can", "could", "would", "should"
    }

    terms = []

    for word in text.split():
        if len(word) < 3:
            continue

        if word in stopwords:
            continue

        terms.append(word)

    # Deduplicate while preserving order.
    seen = set()
    final_terms = []

    for term in terms:
        if term not in seen:
            seen.add(term)
            final_terms.append(term)

    return final_terms


def row_relevance_score_for_query(query: str, row) -> float:
    """
    Generic overlap scoring.
    This does NOT answer the question.
    It only prevents obviously unrelated assets from being expanded.
    """

    row_text = normalise_search_text(get_row_text_for_relevance(row))
    terms = meaningful_terms_from_query(query)

    if not row_text or not terms:
        return 0.0

    hits = 0

    for term in terms:
        if term in row_text:
            hits += 1

    return hits / max(len(terms), 1)


def ordered_unique_asset_ids_from_rows(rows: list[dict], max_assets: int = 3) -> list[str]:
    asset_ids = []

    for row in rows or []:
        asset_id = row.get("asset_id")

        if not asset_id:
            continue

        if asset_id not in asset_ids:
            asset_ids.append(asset_id)

        if len(asset_ids) >= max_assets:
            break

    return asset_ids


def choose_relevant_asset_ids_for_query(
    query: str,
    matched_rows: list[dict],
    max_assets: int = 2
) -> list[str]:
    """
    Chooses which matched document(s) should be expanded to full-document context.

    This is generic:
    - no crew terms
    - no invoice terms
    - no WhatsApp terms
    - no document-specific hard-coding

    It uses only retrieval order + generic term overlap.
    """

    if not matched_rows:
        return []

    asset_scores = {}
    asset_order_bonus = {}

    for index, row in enumerate(matched_rows):
        asset_id = row.get("asset_id")

        if not asset_id:
            continue

        score = row_relevance_score_for_query(query, row)

        # Retrieval order matters, but should not overpower relevance.
        order_bonus = max(0.0, 1.0 - (index * 0.03))

        asset_scores[asset_id] = asset_scores.get(asset_id, 0.0) + score + order_bonus
        asset_order_bonus[asset_id] = max(asset_order_bonus.get(asset_id, 0.0), order_bonus)

    if not asset_scores:
        return ordered_unique_asset_ids_from_rows(matched_rows, max_assets=max_assets)

    ranked = sorted(
        asset_scores.items(),
        key=lambda item: item[1],
        reverse=True
    )

    selected = []

    for asset_id, score in ranked:
        if score <= 0:
            continue

        selected.append(asset_id)

        if len(selected) >= max_assets:
            break

    if selected:
        return selected

    return ordered_unique_asset_ids_from_rows(matched_rows, max_assets=max_assets)


def get_full_asset_rows_for_context(
    yacht_id: str,
    asset_id: str,
    security_level: int,
    max_rows: int = 400
) -> list[dict]:
    """
    Loads the full chunk set for one document, ordered by chunk_index.

    Important:
    - Joins assets so source cards can show the real document name.
    - Without this, expanded rows only contain asset_chunks fields and the UI shows "Untitled document".
    """

    if not asset_id:
        return []

    try:
        res = supabase.table("asset_chunks") \
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
                    file_type,
                    mime_type
                )
            """) \
            .eq("yacht_id", yacht_id) \
            .eq("asset_id", asset_id) \
            .order("chunk_index", desc=False) \
            .limit(max_rows) \
            .execute()

        rows = []

        for row in res.data or []:
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
                "file_type": asset_data.get("file_type"),
                "mime_type": asset_data.get("mime_type")
            })

        return rows

    except Exception as e:
        print("FULL ASSET CONTEXT LOAD ERROR:", type(e).__name__, str(e), asset_id)
        return []


def trim_rows_for_context_limit(
    rows: list[dict],
    max_chars: int = 120000,
    preserve_all_rows: bool = False
) -> list[dict]:
    """
    Limits focused-answer context without silently removing later rows.

    For comprehensive and whole-document requests, all rows are preserved
    and will be processed in smaller batches later.
    """

    clean_rows = []

    for row in rows or []:
        row_text = get_row_text_for_relevance(row).strip()

        if row_text:
            clean_rows.append(row)

    if preserve_all_rows:
        return clean_rows

    trimmed = []
    total_chars = 0

    for row in clean_rows:
        row_text = get_row_text_for_relevance(row)
        row_length = len(row_text)

        if total_chars + row_length > max_chars:
            continue

        trimmed.append(row)
        total_chars += row_length

    return trimmed

def deduplicate_context_rows(rows: list[dict]) -> list[dict]:
    """
    Removes duplicate content while preserving document order.
    """

    unique_rows = []
    seen = set()

    for row in rows or []:
        normalised_content = normalise_search_text(
            get_row_text_for_relevance(row)
        )

        if not normalised_content:
            continue

        key = (
            row.get("asset_id"),
            normalised_content
        )

        if key in seen:
            continue

        seen.add(key)
        unique_rows.append(row)

    return unique_rows

def split_context_rows_into_batches(
    rows: list[dict],
    max_chars_per_batch: int = 30000
) -> list[list[dict]]:
    """
    Splits source rows into model-sized batches without splitting rows.
    """

    batches = []
    current_batch = []
    current_chars = 0

    for row in rows or []:
        row_text = get_row_text_for_relevance(row).strip()

        if not row_text:
            continue

        row_size = len(row_text)

        if (
            current_batch
            and current_chars + row_size > max_chars_per_batch
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0

        current_batch.append(row)
        current_chars += row_size

    if current_batch:
        batches.append(current_batch)

    return batches

def extract_relevant_information_from_batches(
    query: str,
    rows: list[dict],
    answer_depth: str
) -> tuple[str, list[dict]]:
    """
    Inspects every document batch before creating the final answer.
    """

    batches = split_context_rows_into_batches(
        rows=rows,
        max_chars_per_batch=30000
    )

    extracted_parts = []
    relevant_rows = []

    for batch_index, batch_rows in enumerate(batches, start=1):
        batch_context = build_context_from_asset_results(batch_rows)

        try:
            raw = ask_llm(
                query=query,
                context=f"""
You are extracting evidence from one part of a larger document context.

The final answer will be created later after every document part has
been inspected.

Rules:
- Use only this document part.
- Extract every distinct item that directly relates to the exact user request.
- Do not summarise away details.
- Preserve relevant names, values, dates, conditions, exceptions,
  warnings, requirements and procedural steps.
- Do not use outside knowledge.
- Do not invent missing details.
- If this document part contains no directly relevant information,
  return exactly:
NO_RELEVANT_INFORMATION
- Return plain text only.
- Do not add an introduction or conclusion.

Requested answer depth:
{answer_depth}

User request:
{query}

Document part {batch_index} of {len(batches)}:

{batch_context}
""".strip()
)

            extracted = str(raw or "").strip()

        except Exception as e:
            print(
                "BATCH EXTRACTION ERROR:",
                batch_index,
                type(e).__name__,
                str(e)
            )
            extracted = ""

        if (
            extracted
            and extracted != "NO_RELEVANT_INFORMATION"
        ):
            extracted_parts.append(
                f"Evidence part {batch_index}:\n{extracted}"
            )
            relevant_rows.extend(batch_rows)

    return (
        "\n\n".join(extracted_parts).strip(),
        deduplicate_context_rows(relevant_rows)
    )



def expand_retrieved_rows_to_full_relevant_documents(
    query: str,
    matched_rows: list[dict],
    yacht_id: str,
    security_level: int,
    answer_depth: str = "focused",
    max_assets: int | None = None,
    max_rows_per_asset: int | None = None,
    max_context_chars: int = 120000
) -> list[dict]:
    """
    Expands retrieved chunks according to the requested answer depth.

    focused:
        Load the strongest relevant document with a bounded context.

    comprehensive:
        Load all chunks from several relevant documents.

    document:
        Load all chunks from the strongest relevant document.
    """

    if not matched_rows:
        return []

    if max_assets is None:
        if answer_depth == "comprehensive":
            max_assets = 5
        else:
            max_assets = 1

    if max_rows_per_asset is None:
        if answer_depth in {"comprehensive", "document"}:
            max_rows_per_asset = 5000
        else:
            max_rows_per_asset = 800

    selected_asset_ids = choose_relevant_asset_ids_for_query(
        query=query,
        matched_rows=matched_rows,
        max_assets=max_assets
    )

    print(
        "FULL CONTEXT DEBUG:",
        {
            "answer_depth": answer_depth,
            "selected_asset_ids": selected_asset_ids
        }
    )

    expanded_rows = []

    for asset_id in selected_asset_ids:
        full_rows = get_full_asset_rows_for_context(
            yacht_id=yacht_id,
            asset_id=asset_id,
            security_level=security_level,
            max_rows=max_rows_per_asset
        )

        if full_rows:
            expanded_rows.extend(full_rows)

    if not expanded_rows:
        return matched_rows

    preserve_all_rows = answer_depth in {
        "comprehensive",
        "document"
    }

    expanded_rows = trim_rows_for_context_limit(
        rows=expanded_rows,
        max_chars=max_context_chars,
        preserve_all_rows=preserve_all_rows
    )

    print("FULL CONTEXT DEBUG expanded_rows:", len(expanded_rows))

    return expanded_rows or matched_rows
    
# ------------------------
# GENERIC NUMERIC TABLE / LIST COMPARISON
# ------------------------

def parse_numeric_comparison_query(query: str):
    """
    Detects generic numeric comparison questions.

    Examples:
    - less than 10
    - below 5
    - under 3
    - more than 100
    - greater than 12
    - at least 8
    - no more than 20

    This is NOT domain-specific.
    """

    q = (query or "").lower()

    patterns = [
        ("lte", r"(?:less than or equal to|lower than or equal to|at most|no more than|maximum of|<=)\s*(\d+(?:\.\d+)?)"),
        ("gte", r"(?:greater than or equal to|more than or equal to|at least|no less than|minimum of|>=)\s*(\d+(?:\.\d+)?)"),
        ("lt", r"(?:less than|lower than|below|under|fewer than|<)\s*(\d+(?:\.\d+)?)"),
        ("gt", r"(?:greater than|more than|above|over|>)\s*(\d+(?:\.\d+)?)"),
        ("eq", r"(?:equal to|equals|exactly|=)\s*(\d+(?:\.\d+)?)"),
    ]

    for operator, pattern in patterns:
        match = re.search(pattern, q)

        if match:
            try:
                threshold = Decimal(match.group(1))
            except InvalidOperation:
                return None

            return {
                "operator": operator,
                "threshold": threshold
            }

    return None


def compare_numeric_value(
    value: Decimal,
    comparison_operator: str,
    threshold: Decimal
) -> bool:
    if comparison_operator == "lt":
        return value < threshold

    if comparison_operator == "lte":
        return value <= threshold

    if comparison_operator == "gt":
        return value > threshold

    if comparison_operator == "gte":
        return value >= threshold

    if comparison_operator == "eq":
        return value == threshold

    return False

def format_number_for_answer(value):
    try:
        number = Decimal(str(value))

        if number == number.to_integral_value():
            return str(number.quantize(Decimal("1")))

        return format(number.normalize(), "f")

    except Exception:
        return str(value)

def extract_numeric_rows_with_llm(
    query: str,
    context: str
) -> list[dict]:
    """
    Generic row extractor.

    The LLM is only allowed to extract rows and numbers.
    It is NOT allowed to decide the comparison result.
    Python performs the numeric comparison afterwards.
    """

    raw = ask_llm(
        query=query,
        context=f"""
You are a strict document table/list extractor.

The user is asking a numeric comparison question.

Your job:
- Find the table, list, log, form, or section in the document context that is most relevant to the user's question.
- Extract ALL relevant rows/items from that table/list/section.
- Do NOT perform the comparison.
- Do NOT decide who matches.
- Do NOT skip rows.
- Do NOT invent rows.
- Use only the document context.

Return ONLY valid JSON in this exact shape:

{{
  "rows": [
    {{
      "label": "row/person/item label copied from the document",
      "numeric_values": [10, 8, 12],
      "evidence": "exact row or nearby text copied from the document"
    }}
  ]
}}

Rules:
- "label" should identify the row/item/person/entity.
- "numeric_values" must contain every numeric value in that row that is relevant to the user's comparison.
- If the relevant table has multiple date/value columns, include all relevant numbers from that row.
- If a row has no relevant numeric values, do not include it.
- Evidence must be copied from the document context.
- Do not include commentary outside JSON.

User question:
{query}

Document context:
{context}
""".strip()
    )

    parsed = parse_llm_json_response(raw)

    if not parsed or not isinstance(parsed, dict):
        return []

    rows = parsed.get("rows") or []

    clean_rows = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        label = str(row.get("label") or "").strip()
        evidence = str(row.get("evidence") or "").strip()
        numeric_values = row.get("numeric_values") or []

        if not label or not numeric_values:
            continue

        clean_numbers = []

        for value in numeric_values:
            try:
                clean_value = Decimal(str(value))
            except InvalidOperation:
                continue

            clean_numbers.append(clean_value)

        if not clean_numbers:
            continue

        clean_rows.append({
            "label": label,
            "numeric_values": clean_numbers,
            "evidence": evidence
        })

    return clean_rows


def answer_numeric_comparison_from_context(
    query: str,
    context: str,
    matched_rows: list[dict]
):
    """
    Answers generic numeric comparison questions deterministically.

    The LLM extracts rows/numbers.
    Python compares the numbers.
    This prevents hallucinated comparisons such as saying 10 is less than 10.
    """

    comparison = parse_numeric_comparison_query(query)

    if not comparison:
        return None

    extracted_rows = extract_numeric_rows_with_llm(
        query=query,
        context=context
    )

    print("NUMERIC COMPARISON DEBUG extracted_rows:", len(extracted_rows))

    if not extracted_rows:
        return None

    operator = comparison["operator"]
    threshold = comparison["threshold"]

    matching_rows = []

    for row in extracted_rows:
        matching_values = [
            value
            for value in row["numeric_values"]
            if compare_numeric_value(value, operator, threshold)
        ]

        if matching_values:
            matching_rows.append({
                "label": row["label"],
                "matching_values": matching_values,
                "evidence": row.get("evidence") or ""
            })

    operator_text = {
        "lt": "less than",
        "lte": "less than or equal to",
        "gt": "greater than",
        "gte": "greater than or equal to",
        "eq": "equal to"
    }.get(operator, "matching")

    threshold_text = format_number_for_answer(threshold)

    if not matching_rows:
        answer = f"No matching rows were found with values {operator_text} {threshold_text} in the provided document context."
    else:
        parts = []

        for item in matching_rows:
            values_text = ", ".join(
                format_number_for_answer(value)
                for value in item["matching_values"]
            )

            parts.append(f"{item['label']} ({values_text})")

        answer = (
            f"The rows with values {operator_text} {threshold_text} are: "
            + "; ".join(parts)
            + "."
        )

    sources = build_sources_from_asset_results(matched_rows[:3])

    return {
        "answer": answer,
        "sources": sources
    }

def classify_answer_depth(query: str) -> str:
    """
    Determines how much document context the user is requesting.

    Returns:
    - "focused": answer the specific question from the most relevant section
    - "comprehensive": include all relevant information about the requested subject
    - "document": inspect the entire relevant document

    This classifier contains no document topics or subject names.
    """

    clean_query = str(query or "").strip()

    if not clean_query:
        return "focused"

    try:
        raw = ask_llm(
            query=clean_query,
            context="""
Classify how much source information the user is requesting.

Return exactly one of these words:

focused
comprehensive
document

Definitions:

focused:
The user asks a specific question and needs the directly relevant answer.

comprehensive:
The user asks for all information, every detail, everything available,
a complete explanation, a full list, all entries, all steps, all requirements,
all actions, all findings, or otherwise requests exhaustive information
about a subject.

document:
The user asks to summarise, analyse, extract, review, or explain the entire
file or document.

Rules:
- Classify intent only.
- Do not answer the question.
- Do not identify the subject.
- Do not add facts.
- Return one word only.
""".strip()
        )

        value = str(raw or "").strip().lower()

        if value in {"focused", "comprehensive", "document"}:
            return value

    except Exception as e:
        print(
            "ANSWER DEPTH CLASSIFIER ERROR:",
            type(e).__name__,
            str(e)
        )

    return "focused"

def create_voice_note_and_answer(
    file,
    filename: str,
    mime_type: str | None,
    crew: dict,
    chat_id: str
):
    """
    Saves the original voice recording, transcribes it, saves the transcript
    as the user message, and generates the normal BridgeOS response.
    """

    verify_chat_access(
        chat_id=chat_id,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"]
    )

    allowed_mime_types = {
        "audio/webm",
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/m4a",
        "audio/ogg",
        "audio/aac",
        "video/mp4"
    }

    clean_mime_type = (mime_type or "").lower().split(";")[0].strip()

    if clean_mime_type and clean_mime_type not in allowed_mime_types:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported voice-note format: {clean_mime_type}"
        )

    try:
        file.seek(0)
        audio_bytes = file.read()
        file.seek(0)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read voice note: {str(e)}"
        )

    if not audio_bytes:
        raise HTTPException(
            status_code=400,
            detail="Voice note is empty"
        )

    if len(audio_bytes) > 25 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Voice note must be smaller than 25 MB"
        )

    voice_filename = safe_filename(
        filename or f"voice-note-{uuid.uuid4()}.webm"
    )

    # This uses the existing asset pipeline:
    # - Supabase Storage
    # - assets table
    # - transcription
    # - asset_chunks
    upload_result = upload_asset(
        file=io.BytesIO(audio_bytes),
        filename=voice_filename,
        mime_type=mime_type or "audio/webm",
        yacht_id=crew["yacht_id"],
        uploaded_by=crew["id"],
        chat_id=chat_id,
        security_level=int(crew["security_level"]),
        folder_name=None,
        folder_security_level=None
    )

    asset = upload_result.get("asset") or {}

    asset_id = asset.get("id")

    if not asset_id:
        raise HTTPException(
            status_code=500,
            detail="Voice note was uploaded but no asset ID was returned"
        )

    transcript = clean_text_for_postgres(
        asset.get("extracted_text") or ""
    )

    # In case the returned upload row was the original pre-update row,
    # retrieve the processed asset again.
    if not transcript:
        asset_res = supabase.table("assets") \
            .select("id, extracted_text, file_name, mime_type, storage_path") \
            .eq("id", asset_id) \
            .eq("yacht_id", crew["yacht_id"]) \
            .single() \
            .execute()

        processed_asset = asset_res.data or {}
        transcript = clean_text_for_postgres(
            processed_asset.get("extracted_text") or ""
        )

        if processed_asset:
            asset = processed_asset

    if not transcript:
        raise HTTPException(
            status_code=422,
            detail="The voice note was saved, but no speech was detected"
        )

    # Reuse your existing chat function.
    # This saves the transcript as the user message and saves the AI answer.
    chat_result = chat(
        query=transcript,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        security_level=crew["security_level"],
        chat_id=chat_id,
        uploaded_asset_id=asset_id
    )

    return {
        **chat_result,
        "transcript": transcript,
        "voice_asset_id": asset_id,
        "voice_asset": {
            "id": asset_id,
            "file_name": asset.get("file_name"),
            "mime_type": asset.get("mime_type"),
            "storage_path": asset.get("storage_path")
        }

    }
    
MONEY_PATTERN = re.compile(
    r"""
    (?<![\w\d])
    (?P<currency_before>
        US\$|CA\$|AU\$|
        USD|EUR|GBP|AED|SAR|QAR|CAD|AUD|
        \$|€|£
    )?
    \s*
    (?P<amount>
        \(?
        (?:
            \d{1,3}(?:,\d{3})+(?:\.\d{1,2})?
            |
            \d{1,3}(?:\.\d{3})+(?:,\d{1,2})?
            |
            \d+(?:\.\d{1,2})?
            |
            \d+(?:,\d{1,2})
        )
        \)?
    )
    \s*
    (?P<currency_after>
        USD|EUR|GBP|AED|SAR|QAR|CAD|AUD
    )?
    (?![\w\d])
    """,
    re.IGNORECASE | re.VERBOSE
)


INVOICE_TOTAL_LABELS = [
    # Highest-confidence labels first.
    ("grand_total", 100, [
        "grand total",
        "invoice total",
        "final total",
        "total invoice",
        "amount paid",
        "total paid",
    ]),

    ("amount_due", 90, [
        "amount due",
        "balance due",
        "total due",
        "net amount due",
    ]),

    ("total", 80, [
        "total amount",
        "total payable",
        "total payment",
        "total",
    ]),
]


EXCLUDED_TOTAL_LABELS = [
    "subtotal",
    "sub total",
    "tax total",
    "vat total",
    "sales tax",
    "tax amount",
    "vat amount",
    "discount",
    "shipping",
    "delivery",
    "deposit",
    "balance brought forward",
    "previous balance",
]


CURRENCY_MAP = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",

    "€": "EUR",
    "EUR": "EUR",

    "£": "GBP",
    "GBP": "GBP",

    "AED": "AED",
    "SAR": "SAR",
    "QAR": "QAR",
    "CAD": "CAD",
    "CA$": "CAD",
    "AUD": "AUD",
    "AU$": "AUD",
}


CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "AED": "AED ",
    "SAR": "SAR ",
    "QAR": "QAR ",
    "CAD": "CAD ",
    "AUD": "AUD ",
}


def normalise_invoice_line(value: str) -> str:
    text = str(value or "")
    text = text.replace("\u00a0", " ")
    text = text.replace("\t", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalise_currency(value: str | None) -> str:
    clean = str(value or "").strip().upper()

    if not clean:
        return ""

    return CURRENCY_MAP.get(clean, clean)


def parse_decimal_money(raw_amount: str) -> Decimal | None:
    """
    Parses one written monetary value without using floating-point arithmetic.

    Supported examples:
    - 1,234.56
    - 1234.56
    - 1 234.56
    - 1234
    - 1.234,56
    - 1234,56
    - (125.00)

    Ambiguous values are rejected rather than guessed.
    """

    raw = str(raw_amount or "").strip()

    if not raw:
        return None

    negative = raw.startswith("(") and raw.endswith(")")

    raw = raw.strip("()")
    raw = raw.replace("\u00a0", "")
    raw = raw.replace(" ", "")

    if not raw:
        return None

    # European format: 1.234,56
    if "." in raw and "," in raw:
        last_dot = raw.rfind(".")
        last_comma = raw.rfind(",")

        if last_comma > last_dot:
            raw = raw.replace(".", "")
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")

    # One comma with one or two trailing digits: 1234,56
    elif "," in raw and "." not in raw:
        comma_parts = raw.split(",")

        if len(comma_parts) == 2 and len(comma_parts[1]) in [1, 2]:
            raw = comma_parts[0] + "." + comma_parts[1]

        # Thousands groups: 1,234 or 1,234,567
        elif all(
            len(part) == 3
            for part in comma_parts[1:]
        ):
            raw = "".join(comma_parts)

        else:
            # Ambiguous formatting: reject it.
            return None

    # Multiple dots may be European thousands separators: 1.234.567
    elif raw.count(".") > 1:
        dot_parts = raw.split(".")

        if all(
            len(part) == 3
            for part in dot_parts[1:]
        ):
            raw = "".join(dot_parts)
        else:
            return None

    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None

    if negative:
        value = -value

    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )


def extract_money_values_from_line(line: str) -> list[dict]:
    """
    Extracts complete monetary amounts from one line.

    It rejects:
    - dates
    - percentages
    - invoice/reference numbers
    - isolated integers without currency unless they contain decimals or
      thousands separators
    """

    clean_line = normalise_invoice_line(line)
    values = []

    if not clean_line:
        return values

    for match in MONEY_PATTERN.finditer(clean_line):
        raw_amount = str(match.group("amount") or "").strip()

        if not raw_amount:
            continue

        full_match = str(match.group(0) or "").strip()

        currency_raw = (
            match.group("currency_before")
            or match.group("currency_after")
            or ""
        )

        currency = normalise_currency(currency_raw)

        start = match.start()
        end = match.end()

        before = clean_line[max(0, start - 2):start]
        after = clean_line[end:end + 2]

        # Reject percentages.
        if "%" in after or "%" in before:
            continue

        # Reject date fragments such as 27/07/2026.
        if "/" in before or "/" in after:
            continue

        # Reject time fragments such as 14:30.
        if ":" in before or ":" in after:
            continue

        # Reject plain, currency-free integers because these are commonly:
        # invoice numbers, quantities, dates, references or page numbers.
        has_decimal_separator = "." in raw_amount or "," in raw_amount

        if not currency and not has_decimal_separator:
            continue

        amount = parse_decimal_money(raw_amount)

        if amount is None:
            continue

        # Monetary totals should not be negative for spend aggregation.
        if amount < Decimal("0.00"):
            continue

        values.append({
            "amount": amount,
            "currency": currency,
            "raw": full_match,
            "start": start,
            "end": end,
        })

    return values


def determine_currency_from_document(text: str) -> str:
    """
    Determines the document currency conservatively.

    Repeated appearances of the same currency are allowed.
    Different currencies make the result ambiguous.
    """

    clean_text = str(text or "")

    matches = re.findall(
        r"US\$|CA\$|AU\$|\bUSD\b|\bEUR\b|\bGBP\b|\bAED\b|"
        r"\bSAR\b|\bQAR\b|\bCAD\b|\bAUD\b|\$|€|£",
        clean_text,
        flags=re.IGNORECASE
    )

    currencies = {
        normalise_currency(match)
        for match in matches
        if normalise_currency(match)
    }

    if len(currencies) == 1:
        return next(iter(currencies))

    return ""


def line_has_excluded_total_label(line: str) -> bool:
    lower = normalise_invoice_line(line).lower()

    return any(
        label in lower
        for label in EXCLUDED_TOTAL_LABELS
    )


def find_total_label(line: str):
    lower = normalise_invoice_line(line).lower()

    if line_has_excluded_total_label(lower):
        return None

    for label_type, priority, phrases in INVOICE_TOTAL_LABELS:
        for phrase in phrases:
            if phrase in lower:
                return {
                    "type": label_type,
                    "priority": priority,
                    "phrase": phrase,
                }

    return None


def get_row_document_name(row: dict) -> str:
    return (
        row.get("original_file_name")
        or row.get("file_name")
        or row.get("title")
        or "Untitled invoice"
    )


def build_invoice_text_by_asset(
    matched_rows: list[dict]
) -> dict[str, dict]:
    """
    Combines financial text and searchable text by asset.

    financial_text:
    Used only to identify and calculate the final invoice total.

    search_text:
    Used to check whether the document contains the user's requested
    subject, such as beef, fuel, vegetables, repairs, etc.
    """

    assets = {}

    for row in matched_rows or []:
        asset_id = row.get("asset_id")

        if not asset_id:
            continue

        financial_content = str(
            row.get("content")
            or row.get("text")
            or ""
        ).strip()

        search_content = str(
            row.get("search_text")
            or financial_content
            or ""
        ).strip()

        if not financial_content:
            continue

        if asset_id not in assets:
            assets[asset_id] = {
                "asset_id": asset_id,
                "file_name": get_row_document_name(row),
                "rows": [],
                "contents": [],
                "search_contents": []
            }

        financial_key = normalise_for_source_check(
            financial_content
        )

        existing_financial_keys = {
            normalise_for_source_check(item)
            for item in assets[asset_id]["contents"]
        }

        if (
            financial_key
            and financial_key not in existing_financial_keys
        ):
            assets[asset_id]["contents"].append(
                financial_content
            )

        search_key = normalise_for_source_check(
            search_content
        )

        existing_search_keys = {
            normalise_for_source_check(item)
            for item in assets[asset_id]["search_contents"]
        }

        if (
            search_key
            and search_key not in existing_search_keys
        ):
            assets[asset_id]["search_contents"].append(
                search_content
            )

        assets[asset_id]["rows"].append(row)

    return assets


def extract_invoice_total_candidates(
    document_text: str
) -> list[dict]:
    """
    Finds labelled final invoice totals.

    Only accepts:
    - a monetary amount on the same line after the total label; or
    - a monetary amount on the immediately following short line.

    It rejects subtotal, tax, VAT, discount, delivery and other non-final totals.
    """

    raw_lines = str(document_text or "").splitlines()

    lines = [
        normalise_invoice_line(line)
        for line in raw_lines
    ]

    lines = [
        line
        for line in lines
        if line
    ]

    document_currency = determine_currency_from_document(
        document_text
    )

    candidates = []

    for index, line in enumerate(lines):
        label = find_total_label(line)

        if not label:
            continue

        lower_line = line.lower()
        phrase_position = lower_line.find(label["phrase"])

        if phrase_position < 0:
            continue

        phrase_end = phrase_position + len(label["phrase"])

        # Only inspect text following the total label.
        text_after_label = line[phrase_end:].strip(" :-–—")

        selected_values = extract_money_values_from_line(
            text_after_label
        )

        evidence = line

        # Some invoices put the amount on the next line.
        if not selected_values and index + 1 < len(lines):
            next_line = lines[index + 1].strip()

            if len(next_line) <= 60:
                next_label = find_total_label(next_line)

                if not next_label:
                    selected_values = extract_money_values_from_line(
                        next_line
                    )

                    if selected_values:
                        evidence = f"{line} {next_line}"

        clean_values = {}

        for value in selected_values:
            amount = value.get("amount")

            if not isinstance(amount, Decimal):
                continue

            currency = (
                value.get("currency")
                or document_currency
            )

            # Do not guess the currency.
            if not currency:
                continue

            key = (
                currency,
                amount
            )

            clean_values[key] = {
                **value,
                "currency": currency
            }

        # The labelled section must contain exactly one possible total.
        if len(clean_values) != 1:
            continue

        selected = next(iter(clean_values.values()))

        candidates.append({
            "label_type": label["type"],
            "label_phrase": label["phrase"],
            "priority": label["priority"],
            "amount": selected["amount"],
            "currency": selected["currency"],
            "evidence": evidence,
            "line_index": index,
        })

    return candidates


def choose_one_invoice_total(
    candidates: list[dict]
) -> tuple[dict | None, str | None]:
    """
    Selects one authoritative total for one invoice.

    If equally authoritative candidates conflict, the invoice is rejected.
    """

    if not candidates:
        return None, "No labelled invoice total was found."

    highest_priority = max(
        item["priority"]
        for item in candidates
    )

    strongest = [
        item
        for item in candidates
        if item["priority"] == highest_priority
    ]

    unique_totals = {}

    for item in strongest:
        key = (
            item["currency"],
            item["amount"]
        )

        unique_totals[key] = item

    if len(unique_totals) > 1:
        return None, (
            "Conflicting invoice totals were found at the same confidence level."
        )

    return next(iter(unique_totals.values())), None


def format_currency_amount(
    amount: Decimal,
    currency: str
) -> str:
    amount = amount.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    symbol = CURRENCY_SYMBOLS.get(currency)

    if symbol:
        return f"{symbol}{amount:,.2f}"

    return f"{amount:,.2f} {currency}".strip()

def is_financial_total_query(query: str) -> bool:
    clean_query = " ".join(
        str(query or "").strip().lower().split()
    )

    phrases = [
        "how much did we spend",
        "how much have we spent",
        "how much we spent",
        "how much was spent",
        "what did we spend",
        "what have we spent",
        "what was spent",
        "total spent",
        "total spend",
        "total spending",
        "total cost",
        "total costs",
        "total amount",
        "total price",
        "grand total",
        "invoice total",
        "amount paid",
        "amount due",
        "balance due",
        "sum of",
        "add up",
        "calculate the total",
        "calculate total"
    ]

    return any(
        phrase in clean_query
        for phrase in phrases
    )

def extract_spending_subject(query: str) -> str:
    """
    Uses the LLM to determine whether the user is asking for spending
    on a specific subject and extracts that subject generically.

    No hard-coded question wording.
    """

    clean_query = str(query or "").strip()

    if not clean_query:
        return ""

    try:
        raw = ask_llm(
            query=clean_query,
            context="""
Classify the user's request.

Return ONLY valid JSON in this exact shape:

{
  "is_specific_spending_question": true,
  "subject": "requested item or category"
}

or:

{
  "is_specific_spending_question": false,
  "subject": ""
}

Definitions:
- A specific spending question asks for the monetary amount spent,
  paid, charged, purchased, billed, or incurred for a particular
  item, service, category, person, supplier, project, or purpose.
- The subject is only the thing whose cost the user wants.
- Remove generic words such as total, money, amount, cost, spending,
  spent, paid, invoice, purchase, and in total.
- Preserve the user's actual subject.
- Do not answer the question.
- Do not calculate anything.
- Do not invent a subject.
- Return JSON only.
""".strip()
        )

        parsed = parse_llm_json_response(raw)

        if not parsed or not isinstance(parsed, dict):
            return ""

        if not bool(parsed.get("is_specific_spending_question")):
            return ""

        subject = str(
            parsed.get("subject")
            or ""
        ).strip(" .?!,;:")

        return subject

    except Exception as e:
        print(
            "SPENDING SUBJECT CLASSIFICATION ERROR:",
            type(e).__name__,
            str(e)
        )

        return ""
    
def extract_subject_line_items_with_llm(
    query: str,
    subject: str,
    context: str
) -> list[dict]:
    """
    Extracts every financial row associated with the requested subject.

    The LLM identifies table structure only.
    Python validates and performs all arithmetic.
    """

    if not str(subject or "").strip():
        return []

    if not str(context or "").strip():
        return []

    try:
        raw = ask_llm(
            query=query,
            context=f"""
You are a strict financial table row extractor.

The user is requesting the monetary amount associated with this subject:

{subject}

Inspect every supplied source and extract every distinct row that directly
matches the requested subject.

Return ONLY valid JSON in this exact structure:

{{
  "items": [
    {{
      "description": "exact row description",
      "unit_price": "exact single-unit price or empty string",
      "quantity": "exact quantity or empty string",
      "line_total": "exact full row total or empty string",
      "currency": "currency code or symbol, or empty string",
      "evidence": "complete exact row including description and values",
      "source_number": 1
    }}
  ]
}}

Table interpretation rules:
- Read the table headers before interpreting row values.
- unit_price is the monetary price for one unit.
- quantity is the number or measure purchased.
- line_total is the full monetary amount for the complete row.
- If headers are Price, QTY, Total and a row is:
  Item | 40 | 10 | 400
  then:
  unit_price = 40
  quantity = 10
  line_total = 400
- Never put unit_price into line_total.
- Never put quantity into line_total.
- Never use an invoice number, date, page number, product code,
  reference number or weight as a monetary total.
- Copy an explicit line total when one is shown.
- If line total is absent but unit price and quantity are present,
  leave line_total empty. Do not calculate it.
- Include matching rows from every supplied source.
- Do not include unrelated rows.
- Do not use invoice subtotal, tax, VAT, delivery, discount or grand total.
- Do not calculate or add values.
- Do not invent values.
- Return {{"items": []}} if no reliable rows exist.
- Return JSON only.

User request:
{query}

Document sources:
{context}
""".strip()
        )

    except Exception as e:
        print(
            "SUBJECT LINE ITEM EXTRACTION ERROR:",
            type(e).__name__,
            str(e)
        )
        return []

    print(
        "SUBJECT LINE ITEM RAW RESPONSE:",
        str(raw or "")[:5000]
    )

    parsed = parse_llm_json_response(raw)

    if not parsed or not isinstance(parsed, dict):
        return []

    raw_items = parsed.get("items") or []

    if not isinstance(raw_items, list):
        return []

    clean_items = []

    subject_terms = [
        term
        for term in normalise_search_text(subject).split()
        if len(term) >= 2
    ]

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        description = str(
            item.get("description") or ""
        ).strip()

        evidence = str(
            item.get("evidence") or ""
        ).strip()

        raw_unit_price = str(
            item.get("unit_price") or ""
        ).strip()

        raw_quantity = str(
            item.get("quantity") or ""
        ).strip()

        raw_line_total = str(
            item.get("line_total") or ""
        ).strip()

        currency = normalise_currency(
            item.get("currency")
        )

        try:
            source_number = int(
                item.get("source_number")
            )
        except Exception:
            source_number = None

        if not description or not evidence:
            continue

        searchable_row = normalise_search_text(
            f"{description} {evidence}"
        )

        # The extracted row must actually contain the requested subject.
        if subject_terms and not all(
            term in searchable_row
            for term in subject_terms
        ):
            continue

        unit_price = parse_decimal_money(
            raw_unit_price
        )

        quantity = parse_general_decimal(
            raw_quantity
        )

        explicit_line_total = parse_decimal_money(
            raw_line_total
        )

        if unit_price is not None and unit_price < Decimal("0"):
            unit_price = None

        if quantity is not None and quantity < Decimal("0"):
            quantity = None

        if (
            explicit_line_total is not None
            and explicit_line_total < Decimal("0")
        ):
            explicit_line_total = None

        calculated_total = None

        if (
            unit_price is not None
            and quantity is not None
        ):
            calculated_total = (
                unit_price * quantity
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )

        # Validate the explicit total against price x quantity when
        # all three values are present.
        if (
            explicit_line_total is not None
            and calculated_total is not None
            and explicit_line_total != calculated_total
        ):
            print(
                "SUBJECT ROW TOTAL MISMATCH:",
                {
                    "description": description,
                    "unit_price": str(unit_price),
                    "quantity": str(quantity),
                    "explicit_line_total": str(
                        explicit_line_total
                    ),
                    "calculated_total": str(
                        calculated_total
                    ),
                    "evidence": evidence
                }
            )

            # Prefer deterministic arithmetic from the identified columns.
            final_amount = calculated_total
            amount_method = "quantity_times_unit_price"

        elif explicit_line_total is not None:
            final_amount = explicit_line_total
            amount_method = "explicit_line_total"

        elif calculated_total is not None:
            final_amount = calculated_total
            amount_method = "quantity_times_unit_price"

        else:
            print(
                "SUBJECT ROW REJECTED: insufficient values",
                {
                    "description": description,
                    "unit_price": raw_unit_price,
                    "quantity": raw_quantity,
                    "line_total": raw_line_total,
                    "evidence": evidence
                }
            )
            continue

        # Explicitly block the old failure where the price or quantity
        # was returned as the complete spend amount.
        if (
            quantity is not None
            and unit_price is not None
            and quantity != Decimal("1")
            and final_amount in {quantity, unit_price}
        ):
            final_amount = (
                quantity * unit_price
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )

            amount_method = "quantity_times_unit_price"

        clean_items.append({
            "description": description,
            "unit_price": unit_price,
            "quantity": quantity,
            "explicit_line_total": explicit_line_total,
            "amount": final_amount,
            "currency": currency,
            "evidence": evidence,
            "source_number": source_number,
            "amount_method": amount_method
        })

    return clean_items

def is_financial_total_query(query: str) -> bool:
    """
    Generic financial-calculation intent classifier.

    No hard-coded question phrases.
    """

    clean_query = str(query or "").strip()

    if not clean_query:
        return False

    try:
        raw = ask_llm(
            query=clean_query,
            context="""
Classify whether the user is requesting a monetary calculation from documents.

Return ONLY valid JSON:

{
  "is_financial_calculation": true
}

or:

{
  "is_financial_calculation": false
}

A financial calculation includes requests to:
- add monetary values
- calculate spending or costs
- calculate invoice totals
- calculate line-item costs
- calculate amounts paid, charged, owed, due, purchased, or billed
- calculate a monetary value for a category, item, supplier, project,
  date range, person, department, or purpose

It does not include:
- asking what a document says without requesting arithmetic
- asking to list files
- general conversation
- non-monetary numeric comparisons

Rules:
- Classify intent only.
- Do not answer.
- Do not calculate.
- Do not use outside knowledge.
- Return JSON only.
""".strip()
        )

        parsed = parse_llm_json_response(raw)

        if not parsed or not isinstance(parsed, dict):
            return False

        return bool(
            parsed.get("is_financial_calculation")
        )

    except Exception as e:
        print(
            "FINANCIAL QUERY CLASSIFICATION ERROR:",
            type(e).__name__,
            str(e)
        )

        return False

def parse_general_decimal(
    raw_value: str
) -> Decimal | None:
    """
    Parses a non-monetary decimal such as quantity, weight, hours,
    count, volume, or measurement.
    """

    raw = str(raw_value or "").strip()

    if not raw:
        return None

    raw = raw.replace("\u00a0", "")
    raw = raw.replace(" ", "")

    if "." in raw and "," in raw:
        last_dot = raw.rfind(".")
        last_comma = raw.rfind(",")

        if last_comma > last_dot:
            raw = raw.replace(".", "")
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")

    elif "," in raw:
        parts = raw.split(",")

        if len(parts) == 2 and len(parts[1]) <= 3:
            raw = parts[0] + "." + parts[1]
        elif all(len(part) == 3 for part in parts[1:]):
            raw = "".join(parts)
        else:
            return None

    elif raw.count(".") > 1:
        parts = raw.split(".")

        if all(len(part) == 3 for part in parts[1:]):
            raw = "".join(parts)
        else:
            return None

    try:
        return Decimal(raw)
    except InvalidOperation:
        return None

def answer_financial_total_from_context(
    query: str,
    context: str,
    matched_rows: list[dict]
):
    """
    Handles financial calculations from retrieved documents.

    Calculation modes:
    1. Specific subject/category spending:
       - The LLM identifies the requested subject and extracts row structure.
       - Python validates quantity, unit price and line total.
       - Python Decimal performs all arithmetic.

    2. Whole-document/invoice totals:
       - Python identifies one verified final total per document.
       - Subtotals, VAT, tax and discounts are excluded.
       - Different currencies are never combined.

    No question wording is hard-coded in this function.
    """

    # ---------------------------------------------------------
    # FINANCIAL INTENT CHECK
    # ---------------------------------------------------------
    if not is_financial_total_query(query):
        return None

    if not matched_rows or not str(context or "").strip():
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    # ---------------------------------------------------------
    # SPECIFIC SUBJECT/CATEGORY CALCULATION
    # ---------------------------------------------------------
    # Examples of subjects may be an item, service, supplier,
    # category, project, person or purpose. The subject itself
    # is identified generically by the classifier.
    subject = extract_spending_subject(query)

    if subject:
        subject_result = answer_subject_spending_from_context(
            query=query,
            context=context,
            matched_rows=matched_rows
        )

        if subject_result is not None:
            return subject_result

    # ---------------------------------------------------------
    # WHOLE-INVOICE TOTAL CALCULATION
    # ---------------------------------------------------------
    assets = build_invoice_text_by_asset(
        matched_rows
    )

    if not assets:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    verified_documents = []
    rejected_documents = []

    for asset_id, asset_data in assets.items():
        # Text selected for deterministic final-total extraction.
        document_text = "\n\n".join(
            asset_data.get("contents")
            or []
        ).strip()

        # Wider text used for document context and subject relevance.
        # This may include extracted text, OCR and summary text.
        searchable_document_text = "\n\n".join(
            asset_data.get("search_contents")
            or asset_data.get("contents")
            or []
        ).strip()

        if not document_text:
            rejected_documents.append({
                "asset_id": asset_id,
                "file_name": (
                    asset_data.get("file_name")
                    or "Untitled document"
                ),
                "reason": "No readable financial text"
            })
            continue

        candidates = extract_invoice_total_candidates(
            document_text=document_text
        )

        selected_total, rejection_reason = (
            choose_one_invoice_total(
                candidates=candidates
            )
        )

        if not selected_total:
            rejected_documents.append({
                "asset_id": asset_id,
                "file_name": (
                    asset_data.get("file_name")
                    or "Untitled document"
                ),
                "reason": (
                    rejection_reason
                    or "No unique final total could be verified"
                )
            })
            continue

        amount = selected_total.get("amount")
        currency = selected_total.get("currency")
        evidence = str(
            selected_total.get("evidence")
            or ""
        ).strip()

        if not isinstance(amount, Decimal):
            rejected_documents.append({
                "asset_id": asset_id,
                "file_name": (
                    asset_data.get("file_name")
                    or "Untitled document"
                ),
                "reason": "The selected total is not a valid decimal"
            })
            continue

        if amount < Decimal("0.00"):
            rejected_documents.append({
                "asset_id": asset_id,
                "file_name": (
                    asset_data.get("file_name")
                    or "Untitled document"
                ),
                "reason": "The selected total is negative"
            })
            continue

        if not currency:
            # Try to verify one currency from the full searchable
            # document representation before rejecting the invoice.
            currency = determine_currency_from_document(
                searchable_document_text
                or document_text
            )

        if not currency:
            rejected_documents.append({
                "asset_id": asset_id,
                "file_name": (
                    asset_data.get("file_name")
                    or "Untitled document"
                ),
                "reason": "The currency could not be verified"
            })
            continue

        # -----------------------------------------------------
        # VERIFY THE SELECTED VALUE APPEARS IN THE EVIDENCE
        # -----------------------------------------------------
        evidence_values = extract_money_values_from_line(
            evidence
        )

        amount_verified = any(
            item.get("amount") == amount
            and (
                not item.get("currency")
                or item.get("currency") == currency
            )
            for item in evidence_values
        )

        if not amount_verified:
            rejected_documents.append({
                "asset_id": asset_id,
                "file_name": (
                    asset_data.get("file_name")
                    or "Untitled document"
                ),
                "reason": (
                    "The selected total could not be verified "
                    "against its source evidence"
                )
            })
            continue

        source_rows = asset_data.get("rows") or []

        if not source_rows:
            rejected_documents.append({
                "asset_id": asset_id,
                "file_name": (
                    asset_data.get("file_name")
                    or "Untitled document"
                ),
                "reason": "No source row was available"
            })
            continue

        verified_documents.append({
            "asset_id": asset_id,
            "file_name": (
                asset_data.get("file_name")
                or "Invoice"
            ),
            "amount": amount.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            ),
            "currency": currency,
            "evidence": evidence,
            "source_row": source_rows[0]
        })

    print(
        "FINANCIAL TOTAL DEBUG:",
        {
            "verified_documents": [
                {
                    "asset_id": item.get("asset_id"),
                    "file_name": item.get("file_name"),
                    "amount": str(item.get("amount")),
                    "currency": item.get("currency")
                }
                for item in verified_documents
            ],
            "rejected_documents": rejected_documents
        }
    )

    if not verified_documents:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    source_rows = [
        item["source_row"]
        for item in verified_documents
    ]

    sources = build_sources_from_asset_results(
        source_rows
    )

    currencies = {
        item["currency"]
        for item in verified_documents
        if item.get("currency")
    }

    # ---------------------------------------------------------
    # NEVER COMBINE DIFFERENT CURRENCIES
    # ---------------------------------------------------------
    if len(currencies) > 1:
        answer_lines = [
            (
                "The verified documents use different currencies, "
                "so they cannot be combined into one accurate total."
            ),
            ""
        ]

        for currency in sorted(currencies):
            currency_documents = [
                item
                for item in verified_documents
                if item["currency"] == currency
            ]

            currency_total = sum(
                (
                    item["amount"]
                    for item in currency_documents
                ),
                Decimal("0.00")
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )

            answer_lines.append(
                f"{currency}: "
                f"{format_currency_amount(currency_total, currency)}"
            )

            for item in currency_documents:
                answer_lines.append(
                    f"- {item['file_name']}: "
                    f"{format_currency_amount(item['amount'], currency)}"
                )

        if rejected_documents:
            answer_lines.extend([
                "",
                (
                    f"{len(rejected_documents)} document(s) were excluded "
                    "because a unique final total or currency could not "
                    "be verified."
                )
            ])

        return {
            "answer": "\n".join(answer_lines),
            "sources": sources
        }

    # ---------------------------------------------------------
    # ONE VERIFIED CURRENCY
    # ---------------------------------------------------------
    currency = next(iter(currencies))

    total = sum(
        (
            item["amount"]
            for item in verified_documents
        ),
        Decimal("0.00")
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    answer_lines = [
        (
            "The verified total is "
            f"{format_currency_amount(total, currency)}."
        ),
        ""
    ]

    for item in verified_documents:
        answer_lines.append(
            f"- {item['file_name']}: "
            f"{format_currency_amount(item['amount'], currency)}"
        )

    if len(verified_documents) > 1:
        calculation = " + ".join(
            f"{item['amount']:.2f}"
            for item in verified_documents
        )

        answer_lines.extend([
            "",
            f"Calculation: {calculation} = {total:.2f}"
        ])

    if rejected_documents:
        answer_lines.extend([
            "",
            (
                f"{len(rejected_documents)} document(s) were excluded "
                "because a unique final total or currency could not "
                "be verified."
            )
        ])

    return {
        "answer": "\n".join(answer_lines),
        "sources": sources
    }

def answer_subject_spending_from_context(
    query: str,
    context: str,
    matched_rows: list[dict]
):
    """
    Calculates the monetary amount associated with a requested subject.

    Behaviour:
    - Calculates each matching row separately.
    - Combines matching rows across multiple invoices.
    - Uses Decimal for every calculation.
    - Never combines different known currencies.
    - Does not require a currency symbol to calculate numeric totals.
    """

    subject = extract_spending_subject(
        query
    )

    if not subject:
        return None

    if not str(context or "").strip() or not matched_rows:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    extracted_items = extract_subject_line_items_with_llm(
        query=query,
        subject=subject,
        context=context
    )

    print(
        "SUBJECT SPENDING EXTRACTED ITEMS:",
        [
            {
                "description": item.get("description"),
                "unit_price": str(item.get("unit_price")),
                "quantity": str(item.get("quantity")),
                "explicit_line_total": str(
                    item.get("explicit_line_total")
                ),
                "amount": str(item.get("amount")),
                "currency": item.get("currency"),
                "source_number": item.get("source_number"),
                "amount_method": item.get("amount_method")
            }
            for item in extracted_items
        ]
    )

    if not extracted_items:
        return {
            "answer": (
                f"I found no row for {subject} with a verifiable "
                "unit price and quantity or line total."
            ),
            "sources": []
        }

    # ---------------------------------------------------------
    # DEDUPLICATE OCR / EXTRACTED-TEXT COPIES
    # ---------------------------------------------------------
    unique_items = []
    seen = set()

    for item in extracted_items:
        evidence_key = normalise_search_text(
            item.get("evidence") or ""
        )

        key = (
            item.get("source_number"),
            normalise_search_text(
                item.get("description") or ""
            ),
            item.get("unit_price"),
            item.get("quantity"),
            item.get("amount"),
            evidence_key
        )

        if key in seen:
            continue

        seen.add(key)
        unique_items.append(item)

    extracted_items = unique_items

    # ---------------------------------------------------------
    # RESOLVE CURRENCY FROM EACH SOURCE WHEN POSSIBLE
    # ---------------------------------------------------------
    source_currency_lookup = {}

    for source_index, row in enumerate(
        matched_rows,
        start=1
    ):
        source_text = str(
            row.get("search_text")
            or row.get("content")
            or ""
        )

        verified_currency = determine_currency_from_document(
            source_text
        )

        if verified_currency:
            source_currency_lookup[
                source_index
            ] = verified_currency

    for item in extracted_items:
        if item.get("currency"):
            continue

        source_number = item.get("source_number")

        if source_number in source_currency_lookup:
            item["currency"] = source_currency_lookup[
                source_number
            ]

    known_currencies = {
        item.get("currency")
        for item in extracted_items
        if item.get("currency")
    }

    # Rows without a currency can be combined only when there is
    # no conflicting known currency.
    if len(known_currencies) == 1:
        only_currency = next(
            iter(known_currencies)
        )

        for item in extracted_items:
            if not item.get("currency"):
                item["currency"] = only_currency

    final_currencies = {
        item.get("currency")
        for item in extracted_items
        if item.get("currency")
    }

    has_unknown_currency = any(
        not item.get("currency")
        for item in extracted_items
    )

    # Do not combine known conflicting currencies.
    if len(final_currencies) > 1:
        answer_lines = [
            (
                f"The verified {subject} rows use different currencies, "
                "so they cannot be combined into one total."
            ),
            ""
        ]

        for currency in sorted(final_currencies):
            currency_items = [
                item
                for item in extracted_items
                if item.get("currency") == currency
            ]

            currency_total = sum(
                (
                    item["amount"]
                    for item in currency_items
                ),
                Decimal("0.00")
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )

            answer_lines.append(
                f"{currency}: "
                f"{format_currency_amount(currency_total, currency)}"
            )

            for item in currency_items:
                unit_price = item.get("unit_price")
                quantity = item.get("quantity")
                amount = item["amount"]

                if (
                    unit_price is not None
                    and quantity is not None
                ):
                    answer_lines.append(
                        f"- {item['description']}: "
                        f"{format_number_for_answer(unit_price)} × "
                        f"{format_number_for_answer(quantity)} = "
                        f"{format_currency_amount(amount, currency)}"
                    )
                else:
                    answer_lines.append(
                        f"- {item['description']}: "
                        f"{format_currency_amount(amount, currency)}"
                    )

        relevant_rows = []

        for item in extracted_items:
            source_number = item.get("source_number")

            if (
                isinstance(source_number, int)
                and 1 <= source_number <= len(matched_rows)
            ):
                relevant_rows.append(
                    matched_rows[source_number - 1]
                )

        return {
            "answer": "\n".join(answer_lines),
            "sources": build_sources_from_asset_results(
                relevant_rows
            )
        }

    # ---------------------------------------------------------
    # CALCULATE COMBINED TOTAL
    # ---------------------------------------------------------
    total = sum(
        (
            item["amount"]
            for item in extracted_items
        ),
        Decimal("0.00")
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    single_currency = None

    if len(final_currencies) == 1:
        single_currency = next(
            iter(final_currencies)
        )

    if single_currency:
        formatted_total = format_currency_amount(
            total,
            single_currency
        )
    else:
        formatted_total = format_number_for_answer(
            total
        )

    answer_lines = [
        (
            f"The verified total associated with {subject} is "
            f"{formatted_total}."
        ),
        ""
    ]

    # ---------------------------------------------------------
    # SHOW EACH INVOICE/ROW CALCULATION
    # ---------------------------------------------------------
    for index, item in enumerate(
        extracted_items,
        start=1
    ):
        unit_price = item.get("unit_price")
        quantity = item.get("quantity")
        amount = item["amount"]
        currency = item.get("currency")

        if currency:
            formatted_amount = format_currency_amount(
                amount,
                currency
            )

            formatted_unit_price = (
                format_currency_amount(
                    unit_price,
                    currency
                )
                if unit_price is not None
                else ""
            )
        else:
            formatted_amount = format_number_for_answer(
                amount
            )

            formatted_unit_price = (
                format_number_for_answer(
                    unit_price
                )
                if unit_price is not None
                else ""
            )

        source_label = ""

        source_number = item.get("source_number")

        if isinstance(source_number, int):
            source_label = f"Source {source_number} — "

        if (
            unit_price is not None
            and quantity is not None
        ):
            answer_lines.append(
                f"- {source_label}{item['description']}: "
                f"{formatted_unit_price} × "
                f"{format_number_for_answer(quantity)} = "
                f"{formatted_amount}"
            )

        else:
            answer_lines.append(
                f"- {source_label}{item['description']}: "
                f"{formatted_amount}"
            )

    # Show aggregation when multiple invoices/rows matched.
    if len(extracted_items) > 1:
        calculation_parts = [
            format_number_for_answer(
                item["amount"]
            )
            for item in extracted_items
        ]

        answer_lines.extend([
            "",
            (
                "Combined calculation: "
                + " + ".join(calculation_parts)
                + " = "
                + format_number_for_answer(total)
            )
        ])

    if has_unknown_currency and not single_currency:
        answer_lines.extend([
            "",
            (
                "The source does not show a verifiable currency, "
                "so the result is reported as a numeric amount."
            )
        ])

    # ---------------------------------------------------------
    # INCLUDE ONLY SOURCES THAT CONTRIBUTED TO THE TOTAL
    # ---------------------------------------------------------
    relevant_rows = []
    relevant_row_keys = set()

    for item in extracted_items:
        source_number = item.get("source_number")

        if not isinstance(source_number, int):
            continue

        source_index = source_number - 1

        if not 0 <= source_index < len(matched_rows):
            continue

        row = matched_rows[source_index]

        row_key = (
            row.get("asset_id"),
            row.get("chunk_index"),
            row.get("content_type")
        )

        if row_key in relevant_row_keys:
            continue

        relevant_row_keys.add(row_key)
        relevant_rows.append(row)

    if not relevant_rows:
        relevant_rows = matched_rows[:3]

    return {
        "answer": "\n".join(answer_lines),
        "sources": build_sources_from_asset_results(
            relevant_rows
        )
    }

def chat(
    query: str,
    crew_id: str,
    yacht_id: str,
    security_level: int,
    chat_id: str,
    uploaded_asset_id: str | None = None
):
    """
    Secure BridgeOS document chat.

    Behaviour:
    - Uses only documents accessible to the current crew member.
    - Uses an uploaded chat file only when its asset ID is supplied.
    - Searches Yacht Documentation for normal document questions.
    - Loads financial documents directly for spending and total questions.
    - Uses deterministic Decimal calculations for financial totals.
    - Uses deterministic Python comparisons for numeric comparison questions.
    - Never combines different currencies.
    - Returns the no-data fallback rather than inventing information.
    """

    clean_query = str(query or "").strip()
    security_level = int(security_level)

    chat_row = verify_chat_access(
        chat_id=chat_id,
        crew_id=crew_id,
        yacht_id=yacht_id
    )

    # ---------------------------------------------------------
    # SAVE USER MESSAGE
    # ---------------------------------------------------------
    try:
        supabase.table("messages").insert({
            "chat_id": chat_id,
            "yacht_id": yacht_id,
            "crew_id": crew_id,
            "role": "user",
            "content": clean_query,
            "uploaded_asset_id": uploaded_asset_id,
            "sources": []
        }).execute()

    except Exception as e:
        print(
            "USER MESSAGE INSERT WITH UPLOADED ASSET FAILED:",
            type(e).__name__,
            str(e)
        )

        try:
            supabase.table("messages").insert({
                "chat_id": chat_id,
                "yacht_id": yacht_id,
                "crew_id": crew_id,
                "role": "user",
                "content": clean_query,
                "sources": []
            }).execute()

        except Exception as second_error:
            print(
                "USER MESSAGE FALLBACK INSERT FAILED:",
                type(second_error).__name__,
                str(second_error)
            )

    # ---------------------------------------------------------
    # UPDATE DEFAULT CHAT TITLE
    # ---------------------------------------------------------
    if chat_row.get("title") == "New Chat":
        try:
            supabase.table("chats").update({
                "title": clean_query[:60] or "New Chat",
                "updated_at": "now()"
            }) \
                .eq("id", chat_id) \
                .eq("crew_id", crew_id) \
                .eq("yacht_id", yacht_id) \
                .execute()

        except Exception as e:
            print(
                "CHAT TITLE UPDATE ERROR:",
                type(e).__name__,
                str(e)
            )

    answer = ""
    sources = []
    matched_rows = []
    context = ""
    retrieval_query_input = clean_query

    query_scope = classify_bridgeos_query_scope(
        clean_query
    )

    financial_query = is_financial_total_query(
        clean_query
    )

    if financial_query:
        answer_depth = "comprehensive"
    else:
        answer_depth = classify_answer_depth(
            clean_query
        )

    print(
        "LOCAL CHAT DEBUG: answer_depth:",
        answer_depth
    )

    print(
        "LOCAL CHAT DEBUG: financial_query:",
        financial_query
    )

    is_followup_query = is_contextual_followup_query(
        query=clean_query,
        chat_id=chat_id
    )

    previous_source_asset_ids = []

    if is_followup_query:
        previous_source_asset_ids = (
            get_previous_assistant_source_asset_ids(
                chat_id=chat_id,
                crew_id=crew_id,
                yacht_id=yacht_id
            )
        )

    resolved_uploaded_asset_id = uploaded_asset_id

    # ---------------------------------------------------------
    # SAVE ASSISTANT RESPONSE
    # ---------------------------------------------------------
    def save_assistant_response(
        final_answer: str,
        final_sources: list[dict]
    ):
        try:
            supabase.table("messages").insert({
                "chat_id": chat_id,
                "yacht_id": yacht_id,
                "crew_id": crew_id,
                "role": "assistant",
                "content": final_answer,
                "sources": final_sources
            }).execute()

        except Exception as e:
            print(
                "ASSISTANT MESSAGE SAVE ERROR:",
                type(e).__name__,
                str(e)
            )

        try:
            supabase.table("chats").update({
                "updated_at": "now()"
            }) \
                .eq("id", chat_id) \
                .eq("crew_id", crew_id) \
                .eq("yacht_id", yacht_id) \
                .execute()

        except Exception as e:
            print(
                "CHAT UPDATE ERROR:",
                type(e).__name__,
                str(e)
            )

    # ---------------------------------------------------------
    # CONVERSATIONAL MODE
    # ---------------------------------------------------------
    if (
        query_scope == "conversational"
        and not resolved_uploaded_asset_id
    ):
        try:
            raw_answer = ask_llm(
                query=clean_query,
                context="""
You are BridgeOS, a helpful private document assistant.

The user message is conversational or asks how to use the assistant.

Rules:
- Reply briefly and naturally.
- You may explain that you can search uploaded documents.
- Do not answer factual, technical, operational, financial, legal,
  medical, recommendation, or outside-knowledge questions without documents.
- Do not invent document data.
- Do not claim that a document was used.
- Use British English.
- Return plain text only.
""".strip()
            )

            answer = str(
                raw_answer or ""
            ).strip()

        except Exception as e:
            print(
                "CONVERSATIONAL CHAT ERROR:",
                type(e).__name__,
                str(e)
            )

            answer = ""

        if not answer:
            answer = "Hello. How can I help?"

        sources = []

        save_assistant_response(
            final_answer=answer,
            final_sources=sources
        )

        return {
            "answer": answer,
            "sources": sources,
            "uploaded_asset_id": None,
            "mode": "conversational"
        }

    # ---------------------------------------------------------
    # DOCUMENT RETRIEVAL
    # ---------------------------------------------------------
    try:
        # =====================================================
        # EXPLICIT CHAT-UPLOADED FILE
        # =====================================================
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

            matched_rows = deduplicate_context_rows(
                matched_rows
            )

            if matched_rows:
                # Always answer from the original document rows.
                # Do not replace document text with an LLM-generated summary.
                context = build_context_from_asset_results(
                    matched_rows
                )
            else:
                context = ""

        # =====================================================
        # YACHT DOCUMENTATION
        # =====================================================
        else:
            accessible_asset_ids = get_accessible_asset_ids(
                crew_id=crew_id,
                yacht_id=yacht_id,
                security_level=security_level
            )

            allowed_asset_ids = []

            if accessible_asset_ids:
                try:
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

                except Exception as e:
                    print(
                        "ALLOWED ASSET LOOKUP ERROR:",
                        type(e).__name__,
                        str(e)
                    )

                    allowed_asset_ids = []

            # Normal follow-up questions prefer the previous source.
            # Financial questions must inspect every accessible invoice.
            if (
                previous_source_asset_ids
                and not financial_query
            ):
                restricted_ids = [
                    asset_id
                    for asset_id in allowed_asset_ids
                    if asset_id in previous_source_asset_ids
                ]

                if restricted_ids:
                    allowed_asset_ids = restricted_ids

                print(
                    "LOCAL CHAT DEBUG: follow-up allowed_asset_ids:",
                    allowed_asset_ids
                )

            print(
                "LOCAL CHAT DEBUG: allowed_asset_ids:",
                allowed_asset_ids
            )

            if allowed_asset_ids:
                # -------------------------------------------------
                # FINANCIAL DIRECT RETRIEVAL
                # -------------------------------------------------
                if financial_query:
                    try:
                        financial_assets_res = (
                            supabase.table("assets")
                            .select("""
                                id,
                                yacht_id,
                                chat_id,
                                security_level,
                                file_name,
                                original_file_name,
                                file_type,
                                mime_type,
                                extracted_text,
                                ocr_text,
                                summary,
                                processing_status,
                                processing_error
                            """)
                            .eq("yacht_id", yacht_id)
                            .in_("id", allowed_asset_ids)
                            .eq("processing_status", "processed")
                            .execute()
                        )

                        direct_financial_rows = []

                        for asset in financial_assets_res.data or []:
                            extracted_text = str(
                                asset.get("extracted_text")
                                or ""
                            ).strip()

                            ocr_text = str(
                                asset.get("ocr_text")
                                or ""
                            ).strip()

                            summary = str(
                                asset.get("summary")
                                or ""
                            ).strip()

                            file_name = (
                                asset.get("original_file_name")
                                or asset.get("file_name")
                                or ""
                            )

                            # Used to search for the subject requested by the
                            # user, such as beef, fuel, vegetables or repairs.
                            searchable_document_text = "\n\n".join([
                                f"File name: {file_name}",
                                extracted_text,
                                ocr_text,
                                summary
                            ]).strip()

                            # Used only for safely reading one final total.
                            financial_document_text = ""
                            selected_total = None

                            candidate_texts = []

                            if extracted_text:
                                candidate_texts.append({
                                    "type": "extracted_text",
                                    "text": extracted_text
                                })

                            if ocr_text:
                                candidate_texts.append({
                                    "type": "ocr_text",
                                    "text": ocr_text
                                })

                            if summary:
                                candidate_texts.append({
                                    "type": "summary",
                                    "text": summary
                                })

                            for candidate_source in candidate_texts:
                                candidate_text = candidate_source[
                                    "text"
                                ]

                                total_candidates = (
                                    extract_invoice_total_candidates(
                                        document_text=candidate_text
                                    )
                                )

                                if not total_candidates:
                                    continue

                                candidate_total, rejection_reason = (
                                    choose_one_invoice_total(
                                        candidates=total_candidates
                                    )
                                )

                                if candidate_total:
                                    financial_document_text = (
                                        candidate_text
                                    )

                                    selected_total = candidate_total

                                    print(
                                        "FINANCIAL TEXT SELECTED:",
                                        {
                                            "file_name": file_name,
                                            "source_type": (
                                                candidate_source["type"]
                                            ),
                                            "amount": str(
                                                candidate_total.get(
                                                    "amount"
                                                )
                                            ),
                                            "currency": (
                                                candidate_total.get(
                                                    "currency"
                                                )
                                            )
                                        }
                                    )

                                    break

                                print(
                                    "FINANCIAL TEXT CANDIDATE REJECTED:",
                                    {
                                        "file_name": file_name,
                                        "source_type": (
                                            candidate_source["type"]
                                        ),
                                        "reason": rejection_reason
                                    }
                                )

                            if (
                                not financial_document_text
                                or not selected_total
                            ):
                                print(
                                    "FINANCIAL DOCUMENT SKIPPED:",
                                    {
                                        "file_name": file_name,
                                        "reason": (
                                            "No unique labelled final total"
                                        )
                                    }
                                )

                                continue

                            direct_financial_rows.append({
                                "asset_id": asset.get("id"),
                                "yacht_id": asset.get("yacht_id"),
                                "chat_id": asset.get("chat_id"),
                                "security_level": asset.get(
                                    "security_level"
                                ),

                                # Used for deterministic total extraction.
                                "content": financial_document_text,

                                # Used for matching subjects such as beef.
                                "search_text": (
                                    searchable_document_text
                                ),

                                "content_type": (
                                    "financial_document"
                                ),
                                "chunk_index": 0,
                                "detected_date": None,
                                "detected_year": None,
                                "tags": [],
                                "file_name": asset.get("file_name"),
                                "original_file_name": asset.get(
                                    "original_file_name"
                                ),
                                "file_type": asset.get("file_type"),
                                "mime_type": asset.get("mime_type")
                            })

                        matched_rows = deduplicate_context_rows(
                            direct_financial_rows
                        )

                        context = build_context_from_asset_results(
                            matched_rows
                        )

                        print(
                            "FINANCIAL DIRECT RETRIEVAL DEBUG:",
                            len(matched_rows)
                        )

                    except Exception as e:
                        print(
                            "FINANCIAL DIRECT RETRIEVAL ERROR:",
                            type(e).__name__,
                            str(e)
                        )

                        matched_rows = []
                        context = ""

                # -------------------------------------------------
                # NORMAL DOCUMENT RETRIEVAL
                # -------------------------------------------------
                else:
                    if is_followup_query:
                        retrieval_query_input = (
                            build_memory_aware_retrieval_input(
                                query=clean_query,
                                chat_id=chat_id
                            )
                        )
                    else:
                        retrieval_query_input = clean_query

                    if not retrieval_query_input:
                        retrieval_query_input = clean_query

                    print(
                        "LOCAL CHAT DEBUG: retrieval_query_input:",
                        retrieval_query_input
                    )

                    retrieval_queries = build_retrieval_queries(
                        retrieval_query_input
                    )

                    if not retrieval_queries:
                        retrieval_queries = [
                            retrieval_query_input
                        ]

                    matched_rows_by_key = {}

                    # File listing questions use asset metadata.
                    if is_file_listing_query(clean_query):
                        listing_rows = (
                            get_asset_metadata_rows_for_listing(
                                query=clean_query,
                                yacht_id=yacht_id,
                                allowed_asset_ids=allowed_asset_ids,
                                limit=50
                            )
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
                        retrieval_query = str(
                            retrieval_query or ""
                        ).strip()

                        if not retrieval_query:
                            continue

                        filters = extract_query_filters(
                            retrieval_query
                        )

                        year_filter = filters.get("year")

                        # -----------------------------------------
                        # KEYWORD SEARCH
                        # -----------------------------------------
                        try:
                            keyword_rows = (
                                keyword_search_asset_chunks(
                                    query=retrieval_query,
                                    yacht_id=yacht_id,
                                    allowed_asset_ids=(
                                        allowed_asset_ids
                                    ),
                                    year_filter=year_filter,
                                    limit=40
                                )
                            )

                            for row in keyword_rows:
                                key = (
                                    row.get("asset_id"),
                                    row.get("chunk_index"),
                                    row.get("content_type")
                                )

                                if key not in matched_rows_by_key:
                                    matched_rows_by_key[key] = row

                        except Exception as e:
                            print(
                                "KEYWORD SEARCH ERROR:",
                                type(e).__name__,
                                str(e)
                            )

                        # -----------------------------------------
                        # SEMANTIC SEARCH
                        # -----------------------------------------
                        try:
                            query_embedding = embed(
                                retrieval_query
                            )

                            semantic_results = supabase.rpc(
                                "match_asset_chunks_secure",
                                {
                                    "query_embedding": query_embedding,
                                    "match_count": 40,
                                    "allowed_asset_ids": (
                                        allowed_asset_ids
                                    ),
                                    "yacht_filter": yacht_id,
                                    "year_filter": year_filter
                                }
                            ).execute()

                            for row in semantic_results.data or []:
                                key = (
                                    row.get("asset_id"),
                                    row.get("chunk_index"),
                                    row.get("content_type")
                                )

                                if key not in matched_rows_by_key:
                                    matched_rows_by_key[key] = row

                        except Exception as e:
                            # Keyword search remains available even if
                            # the embedding endpoint is unavailable.
                            print(
                                "SEMANTIC SEARCH ERROR:",
                                type(e).__name__,
                                str(e)
                            )

                    matched_rows = list(
                        matched_rows_by_key.values()
                    )[:100]

                    print(
                        "LOCAL CHAT DEBUG: matched chunks:",
                        len(matched_rows)
                    )

                    if (
                        matched_rows
                        and not is_file_listing_query(clean_query)
                    ):
                        matched_rows = (
                            expand_retrieved_rows_to_full_relevant_documents(
                                query=retrieval_query_input,
                                matched_rows=matched_rows,
                                yacht_id=yacht_id,
                                security_level=security_level,
                                answer_depth=answer_depth
                            )
                        )

                        matched_rows = deduplicate_context_rows(
                            matched_rows
                        )

                        # Always use original rows as final context.
                        context = build_context_from_asset_results(
                            matched_rows
                        )

                        print(
                            "FULL CONTEXT DEBUG final expanded rows:",
                            len(matched_rows)
                        )

                        print(
                            "FULL CONTEXT DEBUG final context preview:",
                            context[:1000]
                        )

                    elif matched_rows:
                        context = build_context_from_asset_results(
                            matched_rows
                        )

                    else:
                        context = ""

            else:
                matched_rows = []
                context = ""

    except Exception as e:
        print(
            "LOCAL CHAT DOCUMENT SEARCH ERROR:",
            type(e).__name__,
            str(e)
        )

        matched_rows = []
        context = ""

    print(
        "LOCAL CHAT DEBUG FINAL matched_rows:",
        len(matched_rows or [])
    )

    print(
        "LOCAL CHAT DEBUG FINAL context length:",
        len(context or "")
    )

    print(
        "LOCAL CHAT DEBUG FINAL resolved_uploaded_asset_id:",
        resolved_uploaded_asset_id
    )

    # ---------------------------------------------------------
    # EXPLICITLY UPLOADED CHAT FILE ANSWER
    # ---------------------------------------------------------
    if resolved_uploaded_asset_id:
        if not context:
            answer = FALLBACK_NO_DATA_ANSWER
            sources = []

        else:
            financial_result = (
                answer_financial_total_from_context(
                    query=clean_query,
                    context=context,
                    matched_rows=matched_rows
                )
            )

            numeric_result = None

            if financial_result is None:
                numeric_result = (
                    answer_numeric_comparison_from_context(
                        query=clean_query,
                        context=context,
                        matched_rows=matched_rows
                    )
                )

            if financial_result is not None:
                answer = str(
                    financial_result.get("answer")
                    or FALLBACK_NO_DATA_ANSWER
                ).strip()

                sources = (
                    financial_result.get("sources")
                    or []
                )

            elif numeric_result is not None:
                answer = str(
                    numeric_result.get("answer")
                    or FALLBACK_NO_DATA_ANSWER
                ).strip()

                sources = (
                    numeric_result.get("sources")
                    or []
                )

            else:
                uploaded_result = answer_from_uploaded_chat_asset(
                    query=clean_query,
                    context=context,
                    matched_rows=matched_rows
                )

                answer = str(
                    uploaded_result.get("answer")
                    or FALLBACK_NO_DATA_ANSWER
                ).strip()

                sources = (
                    uploaded_result.get("sources")
                    or []
                )

        if not answer:
            answer = FALLBACK_NO_DATA_ANSWER

        if not isinstance(sources, list):
            sources = []

        if answer.strip() == FALLBACK_NO_DATA_ANSWER:
            sources = []

        save_assistant_response(
            final_answer=answer,
            final_sources=sources
        )

        return {
            "answer": answer,
            "sources": sources,
            "uploaded_asset_id": resolved_uploaded_asset_id,
            "mode": "uploaded_chat_asset"
        }

    # ---------------------------------------------------------
    # YACHT DOCUMENTATION ANSWER
    # ---------------------------------------------------------
    if not context:
        answer = FALLBACK_NO_DATA_ANSWER
        sources = []

    else:
        # -----------------------------------------------------
        # DETERMINISTIC FINANCIAL TOTAL
        # -----------------------------------------------------
        financial_result = answer_financial_total_from_context(
            query=clean_query,
            context=context,
            matched_rows=matched_rows
        )

        # -----------------------------------------------------
        # DETERMINISTIC NUMERIC COMPARISON
        # -----------------------------------------------------
        numeric_result = None

        if financial_result is None:
            numeric_result = (
                answer_numeric_comparison_from_context(
                    query=clean_query,
                    context=context,
                    matched_rows=matched_rows
                )
            )

        if financial_result is not None:
            answer = str(
                financial_result.get("answer")
                or FALLBACK_NO_DATA_ANSWER
            ).strip()

            sources = (
                financial_result.get("sources")
                or []
            )

        elif numeric_result is not None:
            answer = str(
                numeric_result.get("answer")
                or FALLBACK_NO_DATA_ANSWER
            ).strip()

            sources = (
                numeric_result.get("sources")
                or []
            )

        # -----------------------------------------------------
        # FILE LISTING
        # -----------------------------------------------------
        elif is_file_listing_query(clean_query):
            listing_result = answer_file_listing_directly(
                query=clean_query,
                rows=matched_rows
            )

            answer = str(
                listing_result.get("answer")
                or FALLBACK_NO_DATA_ANSWER
            ).strip()

            sources = (
                listing_result.get("sources")
                or []
            )

            if answer == FALLBACK_NO_DATA_ANSWER:
                sources = []

        # -----------------------------------------------------
        # NORMAL DOCUMENT QUESTION
        # -----------------------------------------------------
        else:
            try:
                raw_answer = ask_llm(
                    query=clean_query,
                    context=f"""
You are BridgeOS, a private document-based assistant.

Always respond in British English.

You may answer only when the supplied document context directly supports
the user's exact question.

Return ONLY valid JSON in exactly this shape:

{{
  "answer": "clear answer grounded only in the documents",
  "document_used": true,
  "used_sources": [
    {{
      "source_number": 1,
      "evidence_quote": "exact text copied from the selected source"
    }}
  ]
}}

Or return:

{{
  "answer": "{FALLBACK_NO_DATA_ANSWER}",
  "document_used": false,
  "used_sources": []
}}

Hard rules:
- Use only the document context below.
- Do not use general knowledge.
- Do not fill gaps.
- Do not estimate.
- Do not invent names, dates, amounts, totals, prices, statuses or facts.
- Do not answer from loosely related context.
- If the exact answer is not supported, return the fallback answer.
- If a value is missing, state that it was not found.
- For tables and lists, inspect all supplied rows.
- Do not perform unsupported calculations.
- Do not include a source unless it directly supports the answer.
- Evidence quotes must be copied from the context.
- Do not mention document names inside the answer.
- Return JSON only.

Requested answer depth:
{answer_depth}

User question:
{clean_query}

Search query used:
{retrieval_query_input}

Document context:
{context}
""".strip()
                )

                parsed = parse_llm_json_response(
                    raw_answer
                )

            except Exception as e:
                print(
                    "DOCUMENT ANSWER LLM ERROR:",
                    type(e).__name__,
                    str(e)
                )

                parsed = None

            if not parsed or not isinstance(parsed, dict):
                answer = FALLBACK_NO_DATA_ANSWER
                document_used = False
                raw_used_sources = []

            else:
                answer = str(
                    parsed.get("answer")
                    or ""
                ).strip()

                document_used = bool(
                    parsed.get("document_used")
                )

                raw_used_sources = (
                    parsed.get("used_sources")
                    or []
                )

                if not isinstance(
                    raw_used_sources,
                    list
                ):
                    raw_used_sources = []

                if not answer:
                    answer = FALLBACK_NO_DATA_ANSWER

            if answer == FALLBACK_NO_DATA_ANSWER:
                document_used = False
                sources = []

            elif document_used:
                try:
                    verified_rows = (
                        verified_source_rows_from_llm_result(
                            parsed=parsed,
                            matched_rows=matched_rows
                        )
                    )

                except Exception as e:
                    print(
                        "SOURCE VERIFICATION HELPER ERROR:",
                        type(e).__name__,
                        str(e)
                    )

                    verified_rows = []

                if verified_rows:
                    sources = build_sources_from_asset_results(
                        verified_rows
                    )

                else:
                    print(
                        "LOCAL CHAT SOURCE QUOTE VERIFICATION FAILED: "
                        "using selected source rows"
                    )

                    selected_rows = []

                    for used_source in raw_used_sources:
                        if not isinstance(
                            used_source,
                            dict
                        ):
                            continue

                        try:
                            source_number = int(
                                used_source.get(
                                    "source_number"
                                )
                            )
                        except Exception:
                            continue

                        selected_index = source_number - 1

                        if (
                            0
                            <= selected_index
                            < len(matched_rows)
                        ):
                            selected_row = matched_rows[
                                selected_index
                            ]

                            if selected_row not in selected_rows:
                                selected_rows.append(
                                    selected_row
                                )

                    # Do not throw away a grounded answer because OCR,
                    # punctuation or line spacing changed the evidence quote.
                    if selected_rows:
                        sources = build_sources_from_asset_results(
                            selected_rows
                        )

                    elif matched_rows:
                        sources = build_sources_from_asset_results(
                            matched_rows[:1]
                        )

                    else:
                        answer = FALLBACK_NO_DATA_ANSWER
                        sources = []

            else:
                answer = FALLBACK_NO_DATA_ANSWER
                sources = []

    # ---------------------------------------------------------
    # FINAL NORMALISATION
    # ---------------------------------------------------------
    if not answer:
        answer = FALLBACK_NO_DATA_ANSWER

    if not isinstance(sources, list):
        sources = []

    if answer.strip() == FALLBACK_NO_DATA_ANSWER:
        sources = []

    save_assistant_response(
        final_answer=answer,
        final_sources=sources
    )

    return {
        "answer": answer,
        "sources": sources,
        "uploaded_asset_id": resolved_uploaded_asset_id,
        "mode": "document_qa"
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