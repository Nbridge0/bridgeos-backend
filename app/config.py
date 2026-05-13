import os
from dotenv import load_dotenv

# Your env file is literally named: ".env"
# including the quotation marks.
# Keep this for local development.
load_dotenv(dotenv_path='".env"')

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_KEY = SUPABASE_SERVICE_KEY
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

BUCKET_NAME = os.environ.get("BUCKET_NAME", "yacht-files")

LLM_API_URL = os.environ.get("LLM_API_URL")
LLM_API_KEY = os.environ.get("LLM_API_KEY")

EMBEDDING_API_URL = os.environ.get("EMBEDDING_API_URL")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY")

# Used for CORS. Keep * for testing, replace with your real frontend domain later.
FRONTEND_ORIGINS = os.environ.get("FRONTEND_ORIGINS", "*")


print("DEBUG SUPABASE_URL:", SUPABASE_URL)
print("DEBUG SERVICE KEY EXISTS:", bool(SUPABASE_SERVICE_KEY))
print("DEBUG JWT SECRET EXISTS:", bool(SUPABASE_JWT_SECRET))
print("DEBUG BUCKET_NAME:", BUCKET_NAME)


if not SUPABASE_URL:
    raise RuntimeError('SUPABASE_URL is missing. Check your env file named ".env".')

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError('SUPABASE_SERVICE_KEY is missing. Check your env file named ".env".')

if not SUPABASE_JWT_SECRET:
    raise RuntimeError('SUPABASE_JWT_SECRET is missing. Check your env file named ".env".')
