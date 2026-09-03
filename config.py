# ── Ingestion target ──────────────────────────────────────
ONEDRIVE_USER = "rsafrata@fuse-power.com"
ONEDRIVE_FOLDER = "Fuse Power Management"
ONEDRIVE_PARENT = "Documents"  # parent folder containing ONEDRIVE_FOLDER

# ── Chunking ──────────────────────────────────────────────
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
BATCH_SIZE = 50
UPSERT_BATCH = 100
MAX_FILE_SIZE = 10_000_000  # 10MB
MAX_CHUNK_CHARS = 6000      # OpenAI token limit safety cap

# ── Qdrant ────────────────────────────────────────────────
COLLECTION_NAME = "fuse-knowledge"

# ── Progress tracking ─────────────────────────────────────
PROGRESS_FILE = "ingested_files.txt"

# ── Supported file types ──────────────────────────────────
SUPPORTED_EXTENSIONS = ["pdf", "docx", "xlsx", "xls"]

# ── Auth ──────────────────────────────────────────────────
SCOPES = ["Files.Read.All"]
TOKEN_REFRESH_INTERVAL = 2700  # 45 minutes in seconds