import os
from dotenv import load_dotenv

load_dotenv(dotenv_path='".env"')

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

BUCKET_NAME = os.environ.get("BUCKET_NAME", "yacht-files")


RUNPOD_BASE_URL = os.environ.get("RUNPOD_BASE_URL")
BRIDGEOS_API_KEY = os.environ.get("BRIDGEOS_API_KEY")
FRONTEND_ORIGINS = os.environ.get("FRONTEND_ORIGINS", "*")
API_SYNC_TIMEOUT_SECONDS = int(os.environ.get("API_SYNC_TIMEOUT_SECONDS", "60"))
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "BridgeOS")

WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "")
META_APP_ID = os.getenv("META_APP_ID", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0")


GMAIL_SYNC_MAX_RESULTS = int(os.environ.get("GMAIL_SYNC_MAX_RESULTS", "25"))
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
BREVO_FROM_EMAIL = os.environ.get("BREVO_FROM_EMAIL")
BREVO_FROM_NAME = os.environ.get("BREVO_FROM_NAME", "BridgeOS")
BREVO_API_URL = os.environ.get(
    "BREVO_API_URL",
    "https://api.brevo.com/v3/smtp/email"
)

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing.")

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY is missing.")

if not SUPABASE_JWT_SECRET:
    raise RuntimeError("SUPABASE_JWT_SECRET is missing.")

if not RUNPOD_BASE_URL:
    raise RuntimeError("RUNPOD_BASE_URL is missing.")

if not BRIDGEOS_API_KEY:
    raise RuntimeError("BRIDGEOS_API_KEY is missing.")
    
if not SUPABASE_ANON_KEY:
    raise RuntimeError("SUPABASE_ANON_KEY is missing.")