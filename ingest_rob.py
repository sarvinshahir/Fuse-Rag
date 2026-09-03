import os
import fitz
import docx
import msal
import requests
import time
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, PayloadSchemaType
from openai import OpenAI
from dotenv import load_dotenv
import uuid
import openpyxl
import io

load_dotenv()

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY")
)

COLLECTION_NAME = "fuse-knowledge"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
BATCH_SIZE = 50
UPSERT_BATCH = 100
MAX_FILE_SIZE = 10_000_000
PROGRESS_FILE = "ingested_files.txt"

ROB_EMAIL = "rsafrata@fuse-power.com"
SCOPES = ["Files.Read.All"]

# ── Progress tracking ─────────────────────────────────────
def load_ingested():
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def mark_ingested(file_id):
    with open(PROGRESS_FILE, "a") as f:
        f.write(file_id + "\n")

# ── Step 1: Microsoft login with refresh support ──────────
def create_msal_app():
    return msal.PublicClientApplication(
        client_id=os.getenv("AZURE_CLIENT_ID"),
        authority=f"https://login.microsoftonline.com/{os.getenv('AZURE_TENANT_ID')}"
    )

def get_access_token(msal_app):
    accounts = msal_app.get_accounts()
    if accounts:
        result = msal_app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            print("Token refreshed silently.")
            return result["access_token"]
    result = msal_app.acquire_token_interactive(scopes=SCOPES)
    return result["access_token"]

def maybe_refresh_token(msal_app, token, token_time):
    if time.time() - token_time > 2700:
        print("\nRefreshing token...")
        new_token = get_access_token(msal_app)
        return new_token, time.time()
    return token, token_time

# ── Step 2: Find folder ID by name ───────────────────────
def get_folder_id(token, folder_name, user_id, parent_id="root"):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/drive/items/{parent_id}/children"
    response = requests.get(url, headers=headers, timeout=30).json()
    for item in response.get("value", []):
        if item["name"].lower() == folder_name.lower() and "folder" in item:
            return item["id"]
    return None

# ── Step 3: Recursively list files ───────────────────────
def list_files_recursive(token, user_id, folder_id, current_path=""):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/drive/items/{folder_id}/children"
    files = []
    skipped = []

    while url:
        response = requests.get(url, headers=headers, timeout=30).json()
        for item in response.get("value", []):
            item_path = f"{current_path}/{item['name']}"
            if "folder" in item:
                sub_files, sub_skipped = list_files_recursive(token, user_id, item["id"], item_path)
                files += sub_files
                skipped += sub_skipped
            elif "file" in item:
                ext = item["name"].split(".")[-1].lower()
                size = item.get("size", 0)
                if ext in ["pdf", "docx", "xlsx", "xls"]:
                    if size > MAX_FILE_SIZE:
                        skipped.append({
                            "name": item["name"],
                            "path": item_path,
                            "size_mb": round(size / 1_000_000, 1)
                        })
                    else:
                        files.append({
                            "id": item["id"],
                            "name": item["name"],
                            "folder_path": current_path,
                            "full_path": item_path
                        })
        url = response.get("@odata.nextLink")

    return files, skipped

# ── Step 4: Download and extract text ────────────────────
def extract_text(token, user_id, file_info):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/drive/items/{file_info['id']}/content"

    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Connection error, retrying ({attempt+1}/3)...")
                time.sleep(3)
            else:
                print(f"  Skipping after 3 failed attempts: {file_info['name']}")
                return ""

    ext = file_info["name"].split(".")[-1].lower()

    try:
        if ext == "pdf":
            doc = fitz.open(stream=response.content, filetype="pdf")
            return "\n".join(page.get_text() for page in doc)
        elif ext == "docx":
            doc = docx.Document(io.BytesIO(response.content))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        elif ext in ["xlsx", "xls"]:
            wb = openpyxl.load_workbook(io.BytesIO(response.content), data_only=True)
            text = ""
            for sheet in wb.worksheets:
                text += f"\nSheet: {sheet.title}\n"
                for row in sheet.iter_rows(values_only=True):
                    row_text = " | ".join(str(cell) for cell in row if cell is not None)
                    if row_text.strip():
                        text += row_text + "\n"
            return text
    except Exception as e:
        print(f"  Error extracting {file_info['name']}: {e}")
        return ""
    return ""

# ── Step 5: Chunk text ───────────────────────────────────
def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk[:6000])  # hard cap
        start += chunk_size - overlap
    return chunks

# ── Step 6: Batch embed ──────────────────────────────────
def embed_batch(texts):
    # Truncate chunks to max 8000 tokens (roughly 6000 words)
    texts = [t[:6000] for t in texts]  # 12000 chars ≈ 3000 words ≈ 8000 tokens
    for attempt in range(3):
        try:
            response = openai_client.embeddings.create(
                input=texts,
                model="text-embedding-3-small"
            )
            return [r.embedding for r in response.data]
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                wait = 2 ** attempt
                print(f"  Rate limit hit, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise Exception("Failed after 3 retries")

# ── Step 7: Create Qdrant collection ─────────────────────
def setup_collection():
    existing = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION_NAME not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=1536, distance=Distance.COSINE)
        )
        print(f"Created collection: {COLLECTION_NAME}")
    else:
        print(f"Collection already exists: {COLLECTION_NAME}")

    try:
        qdrant.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="folder_path",
            field_schema=PayloadSchemaType.TEXT
        )
        print("folder_path index ensured")
    except Exception:
        pass

# ── Main pipeline ─────────────────────────────────────────
def ingest():
    setup_collection()

    # Load already ingested file IDs
    ingested = load_ingested()
    print(f"Already ingested: {len(ingested)} files — will skip these.")

    msal_app = create_msal_app()
    token = get_access_token(msal_app)
    token_time = time.time()

    user_id = ROB_EMAIL
    print(f"Targeting OneDrive for: {user_id}")

    print("Locating Documents folder...")
    docs_id = get_folder_id(token, "Documents", user_id)

    if docs_id:
        print("Locating Fuse Power Management inside Documents...")
        rob_id = get_folder_id(token, "Fuse Power Management", user_id, parent_id=docs_id)
    else:
        print("Documents not found, trying root...")
        rob_id = get_folder_id(token, "Fuse Power Management", user_id)

    if not rob_id:
        print("ERROR: Could not find 'Fuse Power Management' folder")
        return

    print("Scanning Fuse Power Management...")
    files, skipped = list_files_recursive(token, user_id, rob_id, current_path="Fuse Power Management")
    print(f"Found {len(files)} files total")

    if skipped:
        print(f"\nSkipped {len(skipped)} large files (>{MAX_FILE_SIZE/1_000_000:.0f}MB):")
        for s in skipped:
            print(f"  - {s['name']} ({s['size_mb']}MB) — {s['path']}")
        print()

    to_process = [f for f in files if f["id"] not in ingested]
    print(f"To process: {len(to_process)} new files ({len(ingested)} already done)\n")

    for idx, file_info in enumerate(to_process):
        # Refresh token every 45 minutes
        token, token_time = maybe_refresh_token(msal_app, token, token_time)

        print(f"[{idx+1}/{len(to_process)}] Processing: {file_info['full_path']}")
        text = extract_text(token, user_id, file_info)
        if not text.strip():
            print(f"  Skipping (no text extracted)")
            mark_ingested(file_info["id"])  # mark as done so we don't retry
            continue

        chunks = chunk_text(text)
        points = []

        for batch_start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[batch_start:batch_start + BATCH_SIZE]
            vectors = embed_batch(batch)
            for j, (chunk, vector) in enumerate(zip(batch, vectors)):
                i = batch_start + j
                points.append(PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{file_info['id']}_{i}")),
                    vector=vector,
                    payload={
                        "text": chunk,
                        "filename": file_info["name"],
                        "folder_path": file_info["folder_path"],
                        "full_path": file_info["full_path"],
                        "chunk_index": i
                    }
                ))

        for i in range(0, len(points), UPSERT_BATCH):
            qdrant.upsert(collection_name=COLLECTION_NAME, points=points[i:i+UPSERT_BATCH])

        mark_ingested(file_info["id"])  # save progress after each file
        print(f"  Uploaded {len(points)} chunks")

    print("\nIngestion complete.")

if __name__ == "__main__":
    ingest()