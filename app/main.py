from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

from app.auth import get_user
from app import services

app = FastAPI(title="Yacht Secure Chatbot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
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


class CreateCrewRequest(BaseModel):
    id: Optional[str] = None
    yacht_id: Optional[str] = None
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    role: Optional[str] = "crew"
    security_level: Optional[int] = 1


class AuthorizeDocumentRequest(BaseModel):
    crew_id: str


class ChatRequest(BaseModel):
    query: Optional[str] = None
    message: Optional[str] = None

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
        security_level=body.security_level
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


# ------------------------
# CHAT
# ------------------------

@app.post("/chat")
async def chat_api(
    body: ChatRequest,
    request: Request,
    token: HTTPAuthorizationCredentials = Depends(security)
):
    user = get_user(request)

    crew = services.get_crew(user["sub"])

    if not crew:
        raise HTTPException(status_code=403, detail="No access")

    query = body.query or body.message

    if not query:
        raise HTTPException(status_code=422, detail="Missing query")

    return services.chat(
        query=query,
        crew_id=crew["id"],
        yacht_id=crew["yacht_id"],
        security_level=crew["security_level"]
    )