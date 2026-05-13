from fastapi import HTTPException

from app.database import supabase
from app.embeddings import embed
from app.config import BUCKET_NAME
from app.llm import ask_llm, FALLBACK_NO_DATA_ANSWER


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
    Logs in a user using Supabase Auth.
    Frontend should use the returned access_token as:
    Authorization: Bearer ACCESS_TOKEN
    """

    return supabase.auth.sign_in_with_password({
        "email": email,
        "password": password
    })


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
    """
    Uploads the document to Supabase Storage,
    creates a documents row,
    extracts text,
    chunks text,
    saves embeddings.
    """

    path = f"{yacht_id}/documents/{filename}"

    file.seek(0)

    supabase.storage.from_(BUCKET_NAME).upload(
        path,
        file,
        file_options={"upsert": "true"}
    )

    url = supabase.storage.from_(BUCKET_NAME).get_public_url(path)

    doc_res = supabase.table("documents").insert({
        "yacht_id": yacht_id,
        "name": filename,
        "file_url": url,
        "storage_path": path,
        "security_level": 1,
        "uploaded_by": uploaded_by
    }).execute()

    if not doc_res.data:
        raise HTTPException(status_code=400, detail="Could not save document")

    document = doc_res.data[0]

    extracted_text = extract_text_from_uploaded_file(file, filename)

    if extracted_text:
        save_document_chunks(
            document_id=document["id"],
            yacht_id=yacht_id,
            text=extracted_text
        )

    return {
        "message": "Document uploaded successfully",
        "document": document,
        "text_extracted": bool(extracted_text)
    }


# ------------------------
# IMAGES
# ------------------------

def upload_image(file, filename: str, yacht_id: str, uploaded_by: str):
    path = f"{yacht_id}/images/{filename}"

    file.seek(0)

    supabase.storage.from_(BUCKET_NAME).upload(
        path,
        file,
        file_options={"upsert": "true"}
    )

    url = supabase.storage.from_(BUCKET_NAME).get_public_url(path)

    return supabase.table("images").insert({
        "yacht_id": yacht_id,
        "file_url": url,
        "storage_path": path,
        "description": "",
        "security_level": 1,
        "uploaded_by": uploaded_by
    }).execute()


# ------------------------
# CHAT SECURE
# ------------------------

def chat(query: str, crew_id: str, yacht_id: str, security_level: int):
    """
    Secure chatbot flow:

    1. Find documents the user can access.
    2. Search only those documents.
    3. Send only authorized context to the LLM.
    4. If no data exists, return fallback answer.
    """

    accessible_document_ids = get_accessible_document_ids(
        crew_id=crew_id,
        yacht_id=yacht_id,
        security_level=security_level
    )

    if not accessible_document_ids:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER
        }

    results = supabase.rpc("match_documents_secure", {
        "query_embedding": embed(query),
        "match_count": 5,
        "allowed_document_ids": accessible_document_ids
    }).execute()

    if not results.data:
        return {
            "answer": FALLBACK_NO_DATA_ANSWER
        }

    context = "\n\n".join([
        row["content"]
        for row in results.data
        if row.get("content")
    ])

    if not context.strip():
        return {
            "answer": FALLBACK_NO_DATA_ANSWER
        }

    answer = ask_llm(
        query=query,
        context=context
    )

    return {
        "answer": answer
    }