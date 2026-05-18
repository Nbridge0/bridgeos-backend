import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_KEY = SUPABASE_SERVICE_KEY
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

BUCKET_NAME = os.environ.get("BUCKET_NAME", "yacht-files")

LLM_API_URL = os.environ.get("LLM_API_URL")
LLM_API_KEY = os.environ.get("LLM_API_KEY")

EMBEDDING_API_URL = os.environ.get("EMBEDDING_API_URL")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY")

VISION_API_URL = os.environ.get("VISION_API_URL")
VISION_API_KEY = os.environ.get("VISION_API_KEY")

OCR_API_URL = os.environ.get("OCR_API_URL")
OCR_API_KEY = os.environ.get("OCR_API_KEY")

FRONTEND_ORIGINS = os.environ.get("FRONTEND_ORIGINS", "*")


if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing. Check your .env file.")

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY is missing. Check your .env file.")

if not SUPABASE_JWT_SECRET:
    raise RuntimeError("SUPABASE_JWT_SECRET is missing. Check your .env file.")