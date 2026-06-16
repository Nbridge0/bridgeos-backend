from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends 
from fastapi.responses import StreamingResponse
import io
from urllib.parse import quote
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from app.config import FRONTEND_ORIGINS, BUCKET_NAME

from app.auth import get_user
from app import services
from app.services import get_asset_for_download

app = FastAPI(title="Yacht Secure Chatbot API")

allowed_origins = (
    ["*"]
    if FRONTEND_ORIGINS == "*"
    else [origin.strip() for origin in FRONTEND_ORIGINS.split(",")]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
security = HTTPBearer()
# ------------------------
# REQUEST MODELS
# ------------------------

class SignupAdminRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    yacht_name: str

class AuthorizeAssetRequest(BaseModel):
    crew_id: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class CreateYachtRequest(BaseModel):
    name: str


class CreateCrewUserRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    security_level: int
    position: Optional[str] = None
    phone_number: Optional[str] = None

class CreateCrewRequest(BaseModel):
    id: Optional[str] = None
    yacht_id: Optional[str] = None
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    role: Optional[str] = "crew"
    security_level: Optional[int] = 1

class RepairAdminLoginRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    yacht_name: str


class AuthorizeAssetRequest(BaseModel):
    crew_id: str


class ChatRequest(BaseModel):
    chat_id: Optional[str] = None
    query: Optional[str] = None
    message: Optional[str] = None
    uploaded_asset_id: Optional[str] = None

class CreateChatRequest(BaseModel):
    title: Optional[str] = "New Chat"

class UpdateChatRequest(BaseModel):
    title: str

class RenameAssetRequest(BaseModel):
    name: str

class RenameFolderRequest(BaseModel):
    name: str


class UpdateAssetPermissionsRequest(BaseModel):
    security_level: int
    crew_ids: list[str] = []

class AuthorizeDocumentRequest(BaseModel):
    crew_id: str

class DevCreateAdminRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str = "Test Admin"
    yacht_name: str = "Test Yacht"

class DevLoginRequest(BaseModel):
    email: EmailStr
    full_name: Optional[str] = "Test Admin"
    yacht_name: Optional[str] = "Test Yacht"

class UpdateCrewUserRequest(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    security_level: Optional[int] = None
    position: Optional[str] = None
    phone_number: Optional[str] = None


class ResetCrewPasswordRequest(BaseModel):
    password: str

class ResetMyPasswordRequest(BaseModel):
    password: str

class SeedAssetRequest(BaseModel):
    file_name: str
    content: str
    security_level: int = 1

class MoveAssetRequest(BaseModel):
    folder_name: Optional[str] = None

class CreateAssetFolderRequest(BaseModel):
    name: str
    security_level: int = 1

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class CreateApiConnectionRequest(BaseModel):
    name: str
    base_url: str
    auth_type: str = "none"
    api_key: Optional[str] = None
    extra_headers: dict = {}
    security_level: int = 1

class SyncApiConnectionRequest(BaseModel):
    endpoint_path: Optional[str] = None
    method: str = "GET"
    payload: Optional[dict] = None
    file_name: Optional[str] = None
    security_level: Optional[int] = None

class DirectApiIngestRequest(BaseModel):
    source_name: str
    content: object
    file_name: Optional[str] = None
    security_level: int = 1

@app.post("/auth/dev-login")
async def dev_login(body: DevLoginRequest):
    return services.dev_login(
        email=body.email,
        full_name=body.full_name or "Test Admin",
        yacht_name=body.yacht_name or "Test Yacht"
    )


@app.get("/health")
async def health():
    return {"status": "ok"}

# ------------------------
# HEALTH CHECK
# ------------------------

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Yacht Secure Chatbot API is running"
    }


# ------------------------
# AUTH: SIGNUP ADMIN
# ------------------------

@app.post("/auth/signup-admin")
async def signup_admin(body: SignupAdminRequest):
    return services.signup_admin(
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        yacht_name=body.yacht_name
    )


@app.post("/auth/dev-create-admin")
async def dev_create_admin(body: DevCreateAdminRequest):
    return services.dev_create_admin(
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        yacht_name=body.yacht_name
    )


# ------------------------
# AUTH: LOGIN
# ------------------------

@app.post("/auth/login")
async def login(body: LoginRequest):
    return services.login(
        email=body.email,
        password=body.password
    )


# ------------------------
# CURRENT USER
# ------------------------

from app.config import RUNPOD_BASE_URL, BRIDGEOS_API_KEY

@app.get("/debug-ai-config")
async def debug_ai_config():
    return {
        "runpod_base_url": RUNPOD_BASE_URL,
        "bridgeos_key_present": bool(BRIDGEOS_API_KEY),
        "bridgeos_key_length": len(BRIDGEOS_API_KEY or ""),
        "bridgeos_key_last4": (BRIDGEOS_API_KEY or "")[-4:]
    }

@app.get("/me")
async def me(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No crew profile found")

    return crew
    
@app.post("/me/reset-password")
async def reset_my_password_api(
    body: ResetMyPasswordRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No crew profile found")

    return services.reset_my_password(
        crew=crew,
        new_password=body.password
    )


# ------------------------
# CREATE YACHT
# Optional route. Admin signup already creates a yacht.
# ------------------------

@app.post("/yachts")
async def create_yacht(body: CreateYachtRequest, request: Request):
    user = get_user(request)

    return services.create_yacht(
        user_id=user["sub"],
        name=body.name
    )


# ------------------------
# ADMIN CREATES CREW USER
# ------------------------

@app.post("/crew/create-user")
async def create_crew_user(
    body: CreateCrewUserRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    if admin_crew["security_level"] != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 can create users"
        )

    return services.create_crew_user(
        admin_crew=admin_crew,
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        security_level=body.security_level,
        position=body.position,
        phone_number=body.phone_number
    )


# ------------------------
# LIST CREW FOR ADMIN
# ------------------------

@app.get("/crew")
async def list_crew(request: Request):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_crew_for_yacht(admin_crew)

@app.post("/auth/repair-admin-login")
async def repair_admin_login(body: RepairAdminLoginRequest):
    return services.repair_admin_login(
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        yacht_name=body.yacht_name
    )

@app.patch("/crew/{crew_id}")
async def update_crew_user_api(
    crew_id: str,
    body: UpdateCrewUserRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.update_crew_user(
        admin_crew=admin_crew,
        target_crew_id=crew_id,
        email=body.email,
        full_name=body.full_name,
        security_level=body.security_level,
        position=body.position,
        phone_number=body.phone_number
    )


@app.post("/crew/{crew_id}/reset-password")
async def reset_crew_password_api(
    crew_id: str,
    body: ResetCrewPasswordRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.reset_crew_password(
        admin_crew=admin_crew,
        target_crew_id=crew_id,
        new_password=body.password
    )


@app.delete("/crew/{crew_id}")
async def delete_crew_user_api(
    crew_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.delete_crew_user(
        admin_crew=admin_crew,
        target_crew_id=crew_id
    )

# ------------------------
# AUTHORIZE DOCUMENT ACCESS
# Only security level 1 can grant access.
# ------------------------

@app.post("/documents/{document_id}/authorize")
async def authorize_document(
    document_id: str,
    body: AuthorizeDocumentRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    if admin_crew["security_level"] != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 can authorize documents"
        )

    return services.authorize_document_access(
        document_id=document_id,
        target_crew_id=body.crew_id,
        granted_by=admin_crew["id"],
        yacht_id=admin_crew["yacht_id"]
    )


# ------------------------
# LIST DOCUMENTS FOR ADMIN
# ------------------------

@app.get("/documents/admin")
async def list_documents_admin(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_documents_for_admin(admin_crew)

@app.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest):
    return services.forgot_password(email=body.email)


# ------------------------
# LIST MY ACCESSIBLE DOCUMENTS
# ------------------------

@app.get("/documents/my")
async def list_my_documents(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_my_documents(crew)

@app.post("/pending-documents")
async def upload_pending_document_api(
    request: Request,
    file: UploadFile = File(...),
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    if int(crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only Tier 1 admins can upload pending documents"
        )

    return services.upload_pending_document(
        file=file.file,
        filename=file.filename,
        mime_type=file.content_type,
        yacht_id=crew["yacht_id"],
        uploaded_by=crew["id"]
    )


@app.get("/pending-documents/admin")
async def list_pending_documents_api(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_pending_documents(admin_crew)


@app.get("/pending-documents/{pending_document_id}/signed-url")
async def pending_document_signed_url_api(
    pending_document_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.create_pending_document_signed_url(
        pending_document_id=pending_document_id,
        admin_crew=admin_crew
    )

@app.get("/pending-documents/{pending_document_id}/preview")
async def pending_document_preview_api(
    pending_document_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.create_pending_document_preview(
        pending_document_id=pending_document_id,
        admin_crew=admin_crew
    )

@app.get("/pending-documents/{pending_document_id}/download")
async def download_pending_document_api(
    pending_document_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    pending_doc = services.get_pending_document_for_download(
        pending_document_id=pending_document_id,
        admin_crew=admin_crew
    )

    try:
        file_bytes = services.storage_admin.storage.from_(services.BUCKET_NAME).download(
            pending_doc["storage_path"]
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not download pending document from storage: {str(e)}"
        )

    filename = pending_doc.get("original_file_name") or pending_doc.get("file_name") or "pending-document"
    mime_type = pending_doc.get("mime_type") or "application/octet-stream"

    safe_download_name = quote(filename)

    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe_download_name}"
        }
    )


# ------------------------
# UPLOAD DOCUMENT
# Only security level 1 can upload.
# First-step version supports text extraction from .txt files.
# ------------------------

@app.post("/documents")
async def upload_doc(
    request: Request,
    file: UploadFile = File(...),
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    if crew["security_level"] != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 can upload documents"
        )

    return services.upload_document(
        file=file.file,
        filename=file.filename,
        yacht_id=crew["yacht_id"],
        uploaded_by=crew["id"]
    )


# ------------------------
# UPLOAD IMAGE
# Only security level 1 can upload.
# ------------------------

@app.post("/images")
async def upload_img(
    request: Request,
    file: UploadFile = File(...),
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    if crew["security_level"] != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 can upload images"
        )

    return services.upload_image(
        file=file.file,
        filename=file.filename,
        yacht_id=crew["yacht_id"],
        uploaded_by=crew["id"]
    )

@app.post("/assets")
async def upload_asset_api(
    request: Request,
    file: UploadFile = File(...),
    chat_id: Optional[str] = Form(None),
    security_level: int = Form(1),
    folder_name: Optional[str] = Form(None),
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    if int(crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 can upload assets"
        )

    try:
        final_security_level = int(security_level)

        if final_security_level not in [1, 2, 3, 4]:
            raise HTTPException(
                status_code=400,
                detail="security_level must be 1, 2, 3, or 4"
            )

        return services.upload_asset(
            file=file.file,
            filename=file.filename,
            mime_type=file.content_type,
            yacht_id=crew["yacht_id"],
            uploaded_by=crew["id"],
            chat_id=chat_id,
            security_level=final_security_level,
            folder_name=folder_name,
            folder_security_level=None
        )

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Asset upload failed: {type(e).__name__}: {str(e)}"
        )
        
@app.post("/assets/batch")
async def upload_assets_batch_api(
    request: Request,
    files: list[UploadFile] = File(...),
    chat_id: Optional[str] = Form(None),
    security_level: int = Form(1),
    folder_name: Optional[str] = Form(None),
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    if int(crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 can upload assets"
        )

    final_security_level = int(security_level)

    if final_security_level not in [1, 2, 3, 4]:
        raise HTTPException(
            status_code=400,
            detail="security_level must be 1, 2, 3, or 4"
        )

    results = []

    for file in files:
        result = services.upload_asset(
            file=file.file,
            filename=file.filename,
            mime_type=file.content_type,
            yacht_id=crew["yacht_id"],
            uploaded_by=crew["id"],
            chat_id=chat_id,
            security_level=final_security_level,
            folder_name=folder_name,
            folder_security_level=None
        )
        results.append(result)

    return {
        "message": "Batch upload completed",
        "count": len(results),
        "folder_name": folder_name,
        "security_level": final_security_level,
        "results": results
    }

@app.get("/assets/admin")
async def list_assets_admin(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_assets_for_admin(admin_crew)

@app.get("/assets/my")
async def list_my_assets(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_my_assets(crew)


@app.get("/assets/{asset_id}/status")
async def asset_status(
    asset_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.get_asset_status(
        asset_id=asset_id,
        yacht_id=crew["yacht_id"]
    )

@app.get("/assets/{asset_id}/signed-url")
async def get_asset_signed_url_api(
    asset_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.create_asset_signed_url(
        asset_id=asset_id,
        crew=crew
    )

@app.get("/assets/{asset_id}/preview")
async def get_asset_preview_api(
    asset_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.create_asset_preview(
        asset_id=asset_id,
        crew=crew
    )

@app.get("/assets/{asset_id}/download")
async def download_asset(
    asset_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=404, detail="Crew profile not found")

    asset = services.get_asset_for_download(
        asset_id=asset_id,
        crew=crew
    )

    storage_path = asset.get("storage_path")

    if not storage_path:
        raise HTTPException(
            status_code=404,
            detail="This asset has no stored file to download"
        )

    try:
        file_bytes = services.storage_admin.storage.from_(services.BUCKET_NAME).download(
            storage_path
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not download file from storage: {str(e)}"
        )

    if not file_bytes:
        raise HTTPException(
            status_code=404,
            detail="File not found in storage"
        )

    filename = (
        asset.get("original_file_name")
        or asset.get("file_name")
        or "download"
    )

    filename = str(filename).replace("\r", "").replace("\n", "").strip()

    mime_type = asset.get("mime_type") or "application/octet-stream"

    ascii_filename = filename.replace('"', "").replace("\\", "")

    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=mime_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        }
    )

@app.patch("/assets/{asset_id}/rename")
async def rename_asset_api(
    asset_id: str,
    body: RenameAssetRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.rename_asset(
        asset_id=asset_id,
        new_name=body.name,
        admin_crew=admin_crew
    )

@app.delete("/assets/{asset_id}")
async def delete_asset_api(
    asset_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.delete_asset(
        asset_id=asset_id,
        admin_crew=admin_crew
    )

@app.patch("/assets/{asset_id}/move")
async def move_asset_api(
    asset_id: str,
    body: MoveAssetRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.move_asset_to_folder(
        asset_id=asset_id,
        folder_name=body.folder_name,
        admin_crew=admin_crew
    )

@app.post("/assets/folders")
async def create_asset_folder_api(
    body: CreateAssetFolderRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.create_asset_folder(
        folder_name=body.name,
        security_level=body.security_level,
        admin_crew=admin_crew
    )


@app.get("/assets/folders/my")
async def list_my_asset_folders_api(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_my_asset_folders(crew)

@app.patch("/assets/folders/{folder_name}/rename")
async def rename_folder_assets_api(
    folder_name: str,
    body: RenameFolderRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.rename_asset_folder(
        old_folder_name=folder_name,
        new_folder_name=body.name,
        admin_crew=admin_crew
    )

@app.delete("/assets/folders/{folder_name}")
async def delete_folder_assets_api(
    folder_name: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.delete_folder_assets(
        folder_name=folder_name,
        admin_crew=admin_crew
    )


@app.post("/assets/{asset_id}/authorize")
async def authorize_asset(
    asset_id: str,
    body: AuthorizeAssetRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    if admin_crew["security_level"] != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 can authorize assets"
        )

    return services.authorize_asset_access(
        asset_id=asset_id,
        target_crew_id=body.crew_id,
        granted_by=admin_crew["id"],
        yacht_id=admin_crew["yacht_id"]
    )

@app.post("/dev/seed-asset")
async def seed_asset_api(
    body: SeedAssetRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    if int(crew["security_level"]) != 1:
        raise HTTPException(
            status_code=403,
            detail="Only security level 1 can seed assets"
        )

    return services.seed_text_asset(
        file_name=body.file_name,
        content=body.content,
        yacht_id=crew["yacht_id"],
        uploaded_by=crew["id"],
        security_level=body.security_level
    )

@app.get("/assets/{asset_id}/permissions")
async def get_asset_permissions_api(
    asset_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.get_asset_permissions(
        asset_id=asset_id,
        admin_crew=admin_crew
    )


@app.put("/assets/{asset_id}/permissions")
async def update_asset_permissions_api(
    asset_id: str,
    body: UpdateAssetPermissionsRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.update_asset_permissions(
        asset_id=asset_id,
        security_level=body.security_level,
        crew_ids=body.crew_ids,
        admin_crew=admin_crew
    )

# ------------------------
# API CONNECTIONS
# ------------------------
@app.post("/api-connections")
async def create_api_connection_api(
    body: CreateApiConnectionRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.create_api_connection(
        admin_crew=admin_crew,
        name=body.name,
        base_url=body.base_url,
        auth_type=body.auth_type,
        api_key=body.api_key,
        extra_headers=body.extra_headers,
        security_level=body.security_level
    )

@app.get("/api-connections")
async def list_api_connections_api(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_api_connections(admin_crew)


@app.delete("/api-connections/{connection_id}")
async def delete_api_connection_api(
    connection_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.delete_api_connection(
        connection_id=connection_id,
        admin_crew=admin_crew
    )


@app.post("/api-connections/{connection_id}/sync")
async def sync_api_connection_api(
    connection_id: str,
    body: SyncApiConnectionRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.sync_api_connection(
        connection_id=connection_id,
        admin_crew=admin_crew,
        endpoint_path=body.endpoint_path,
        method=body.method,
        payload=body.payload,
        file_name=body.file_name,
        security_level=body.security_level
    )


@app.post("/api-ingest")
async def direct_api_ingest_api(
    body: DirectApiIngestRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    admin_crew = services.get_crew(user["sub"])

    if not admin_crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.ingest_api_data_directly(
        admin_crew=admin_crew,
        source_name=body.source_name,
        content=body.content,
        file_name=body.file_name,
        security_level=body.security_level
    )
    
# ------------------------
# CHAT
# ------------------------
@app.post("/chats/new")
async def create_chat_api(
    body: CreateChatRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.create_chat(
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        title=body.title or "New Chat"
    )


@app.get("/chats/my")
async def list_my_chats_api(
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.list_my_chats(
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"]
    )

@app.patch("/chats/{chat_id}")
async def update_chat_api(
    chat_id: str,
    body: UpdateChatRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.update_chat_title(
        chat_id=chat_id,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        title=body.title
    )


@app.delete("/chats/{chat_id}")
async def delete_chat_api(
    chat_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.delete_chat(
        chat_id=chat_id,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"]
    )


@app.get("/chats/{chat_id}/messages")
async def get_chat_messages_api(
    chat_id: str,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    return services.get_chat_messages(
        chat_id=chat_id,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"]
    )

@app.post("/chat")
async def chat_api(
    body: ChatRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    print("CHAT DEBUG: /chat endpoint hit")
    print("CHAT DEBUG: authorization header present:", bool(request.headers.get("Authorization")))

    try:
        user = get_user(request)
        print("CHAT DEBUG: user verified:", user.get("sub"))
    except Exception as e:
        print("CHAT DEBUG: get_user failed:", type(e).__name__, str(e))
        raise

    crew = services.get_crew(user["sub"])

    if not crew:
        print("CHAT DEBUG: no crew profile found for:", user["sub"])
        raise HTTPException(status_code=403, detail="No access")

    query = body.query or body.message

    if not query:
        raise HTTPException(status_code=422, detail="Missing query")

    if not body.chat_id:
        raise HTTPException(status_code=422, detail="Missing chat_id")

    print("CHAT DEBUG: using local Supabase asset search")
    print("CHAT DEBUG: chat_id:", body.chat_id)
    print("CHAT DEBUG: crew_id:", crew["id"])
    print("CHAT DEBUG: yacht_id:", crew["yacht_id"])
    print("CHAT DEBUG: security_level:", crew["security_level"])
    print("CHAT DEBUG: query:", query)

    return services.chat(
        query=query,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        security_level=crew["security_level"],
        chat_id=body.chat_id,
        uploaded_asset_id=body.uploaded_asset_id
    )