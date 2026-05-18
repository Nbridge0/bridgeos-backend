from fastapi import HTTPException

from app.database import supabase
from app.embeddings import embed
from app.config import BUCKET_NAME
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
    Creates:
    1. Supabase Auth user
    2. Yacht
    3. Crew profile with security_level = 1
    """

    auth_res = supabase.auth.admin.create_user({
        "email": email,
        "password": password,
        "email_confirm": True
    })

    if not auth_res.user:
        raise HTTPException(status_code=400, detail="Could not create admin user")

    user_id = auth_res.user.id

    yacht_res = supabase.table("yachts").insert({
        "name": yacht_name,
        "owner_id": user_id
    }).execute()

    if not yacht_res.data:
        raise HTTPException(status_code=400, detail="Could not create yacht")

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
        raise HTTPException(status_code=400, detail="Could not create admin crew profile")

    return {
        "message": "Admin account created successfully",
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



# ------------------------
# CREW
# ------------------------

def get_crew(user_id: str):
    res = supabase.table("crew").select("*").eq("id", user_id).execute()

    if not res.data:
        return None

    return res.data[0]


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
    security_level: int
):
    """
    Admin creates a crew user for the same yacht.
    Only security level 1 can create users.
    """

    if admin_crew["security_level"] != 1:
        raise HTTPException(status_code=403, detail="Only security level 1 can create users")

    security_level = int(security_level)

    if security_level not in [2, 3]:
        raise HTTPException(status_code=400, detail="Crew security level must be 2 or 3")

    auth_res = supabase.auth.admin.create_user({
        "email": email,
        "password": password,
        "email_confirm": True
    })

    if not auth_res.user:
        raise HTTPException(status_code=400, detail="Could not create user")

    user_id = auth_res.user.id

    crew_res = supabase.table("crew").insert({
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "yacht_id": admin_crew["yacht_id"],
        "security_level": security_level,
        "created_by": admin_crew["id"]
    }).execute()

    if not crew_res.data:
        raise HTTPException(status_code=400, detail="Could not create crew profile")

    return {
        "message": "Crew user created successfully",
        "user_id": user_id,
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

def upload_asset(
    file,
    filename: str,
    yacht_id: str,
    uploaded_by: str,
    mime_type: str | None = None,
    original_relative_path: str | None = None
):
    """
    Uploads any messy file:
    - image
    - txt
    - pdf
    - docx
    - unknown

    Then processes it into searchable chunks.
    """

    clean_filename = safe_filename(filename)
    file_type = detect_file_type(clean_filename, mime_type)
    file_hash = calculate_file_hash(file)

    existing = supabase.table("assets") \
        .select("*") \
        .eq("yacht_id", yacht_id) \
        .eq("file_hash", file_hash) \
        .execute()

    if existing.data:
        return {
            "message": "Asset already exists",
            "asset": existing.data[0],
            "duplicate": True
        }

    path = f"{yacht_id}/assets/{clean_filename}"

    file.seek(0)

    supabase.storage.from_(BUCKET_NAME).upload(
        path,
        file,
        file_options={"upsert": "true"}
    )

    url = supabase.storage.from_(BUCKET_NAME).get_public_url(path)

    asset_res = supabase.table("assets").insert({
        "yacht_id": yacht_id,
        "uploaded_by": uploaded_by,
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

    if not asset_res.data:
        raise HTTPException(status_code=400, detail="Could not save asset")

    asset = asset_res.data[0]

    file.seek(0)

    process_uploaded_asset(
        asset_id=asset["id"],
        file=file,
        filename=clean_filename,
        file_type=file_type,
        yacht_id=yacht_id
    )

    updated = supabase.table("assets") \
        .select("*") \
        .eq("id", asset["id"]) \
        .single() \
        .execute()

    return {
        "message": "Asset uploaded successfully",
        "asset": updated.data,
        "duplicate": False
    }

def process_uploaded_asset(
    asset_id: str,
    file,
    filename: str,
    file_type: str,
    yacht_id: str
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
            filename=filename,
            file_type=file_type,
            extracted_text=extracted_text,
            visual_description=visual_description,
            ocr_text=ocr_text,
            detected_date=detected_date,
            detected_year=detected_year,
            tags=tags
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
    filename: str,
    file_type: str,
    extracted_text: str = "",
    visual_description: str = "",
    ocr_text: str = "",
    detected_date=None,
    detected_year: int | None = None,
    tags: list[str] | None = None
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
    parts = []

    for index, row in enumerate(results, start=1):
        part = f"""
SOURCE {index}
File name: {row.get("file_name")}
File type: {row.get("file_type")}
Content type: {row.get("content_type")}
Detected year: {row.get("detected_year")}
File URL: {row.get("file_url")}

Content:
{row.get("content")}
""".strip()

        parts.append(part)

    return "\n\n---\n\n".join(parts)


def build_sources_from_asset_results(results: list[dict]) -> list[dict]:
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
            "file_url": row.get("file_url"),
            "storage_path": row.get("storage_path"),
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


def get_accessible_document_ids(crew_id: str, yacht_id: str, security_level: int):
    """
    Level 1:
        Can access all documents for the yacht.

    Level 2 and 3:
        Can only access documents explicitly granted in document_access.
    """

    security_level = int(security_level)

    if security_level == 1:
        docs = supabase.table("documents") \
            .select("id") \
            .eq("yacht_id", yacht_id) \
            .execute()

        return [doc["id"] for doc in docs.data]

    access = supabase.table("document_access") \
        .select("document_id, documents!inner(yacht_id)") \
        .eq("crew_id", crew_id) \
        .eq("documents.yacht_id", yacht_id) \
        .execute()

    return [row["document_id"] for row in access.data]


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

def chat(query: str, crew_id: str, yacht_id: str, security_level: int):
    """
    Secure chatbot flow:

    1. Find assets the user can access.
    2. Detect query filters, like year = 2015.
    3. Search only authorized asset chunks.
    4. Send only authorized context to the LLM.
    5. Return answer plus sources.
    """

    accessible_asset_ids = get_accessible_asset_ids(
        crew_id=crew_id,
        yacht_id=yacht_id,
        security_level=security_level
    )

    if not accessible_asset_ids:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    filters = extract_query_filters(query)
    year_filter = filters.get("year")

    results = supabase.rpc("match_asset_chunks_secure", {
        "query_embedding": embed(query),
        "match_count": 12,
        "allowed_asset_ids": accessible_asset_ids,
        "year_filter": year_filter
    }).execute()

    if not results.data:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    context = build_context_from_asset_results(results.data)

    if not context.strip():
        return {
            "answer": FALLBACK_NO_DATA_ANSWER,
            "sources": []
        }

    answer = ask_llm(
        query=query,
        context=context
    )

    sources = build_sources_from_asset_results(results.data)

    return {
        "answer": answer,
        "sources": sources
    }
# ------------------------
# TEMP DEMO LOGIN FOR TESTING ONLY
# Remove before production.
# ------------------------

import time
import uuid
import jwt as pyjwt

from app.config import SUPABASE_JWT_SECRET


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
