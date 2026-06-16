import os
from dotenv import load_dotenv

load_dotenv(dotenv_path='".env"')

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

BUCKET_NAME = os.environ.get("BUCKET_NAME", "yacht-files")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
RUNPOD_BASE_URL = os.environ.get("RUNPOD_BASE_URL")
BRIDGEOS_API_KEY = os.environ.get("BRIDGEOS_API_KEY")
FRONTEND_ORIGINS = os.environ.get("FRONTEND_ORIGINS", "*")
API_SYNC_TIMEOUT_SECONDS = int(os.environ.get("API_SYNC_TIMEOUT_SECONDS", "60"))

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing.")

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY is missing.")

if not SUPABASE_JWT_SECRET:
    raise RuntimeError("SUPABASE_JWT_SECRET is missing.")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing.")

if not RUNPOD_BASE_URL:
    raise RuntimeError("RUNPOD_BASE_URL is missing.")

if not BRIDGEOS_API_KEY:
    raise RuntimeError("BRIDGEOS_API_KEY is missing.")
    
if not SUPABASE_ANON_KEY:
    raise RuntimeError("SUPABASE_ANON_KEY is missing.")