import os
import secrets
import sqlite3
import json
import csv
import io
import time
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional, List
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchText, MatchValue
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Fuse AI Knowledge Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY")
)

COLLECTION_NAME = "fuse-knowledge"
TOP_K = 15
ANSWER_MODEL = "gpt-4o-mini"

# Where the SQLite conversation log lives. Resolution order:
#   1. DB_PATH env var, if set (use this if your Render disk is mounted somewhere
#      other than /data, e.g. DB_PATH=/var/data/conversations.db).
#   2. /data/conversations.db — automatically used when a Render persistent disk
#      is mounted at /data, so the data survives every redeploy/restart.
#   3. ./conversations.db next to this file — local-dev fallback (no /data present).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if os.getenv("DB_PATH"):
    DB_PATH = os.getenv("DB_PATH")
elif os.path.isdir("/data"):
    DB_PATH = "/data/conversations.db"
else:
    DB_PATH = os.path.join(BASE_DIR, "conversations.db")

# ── Users ─────────────────────────────────────────────────
USERS = {
    "rob": os.getenv("UI_PASSWORD", "fuse2026"),
    "bryan": os.getenv("UI_PASSWORD", "fuse2026"),
    "shimei": os.getenv("UI_PASSWORD", "fuse2026"),
    "haowen": os.getenv("UI_PASSWORD", "fuse2026"),
    "sarvin": os.getenv("UI_PASSWORD", "fuse2026"),
}

ADMIN_USER = "sarvin"

security = HTTPBasic()

def verify_password(credentials: HTTPBasicCredentials = Depends(security)):
    username = credentials.username.lower()
    if username not in USERS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        USERS[username].encode("utf8")
    )
    if not correct_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return username

def require_admin(username: str = Depends(verify_password)):
    if username != ADMIN_USER:
        raise HTTPException(status_code=403, detail="Access denied")
    return username

# ── SQLite setup ──────────────────────────────────────────
# Evaluation-ready record. Columns added after the original schema are created
# on fresh DBs here and back-filled on existing DBs by the migration below.
NEW_COLUMNS = {
    "rewritten_query": "TEXT",
    "retrieved_chunks": "TEXT",
    "source_files": "TEXT",
    "similarity_scores": "TEXT",
    "model": "TEXT",
    "latency_ms": "INTEGER",
    "user_feedback": "TEXT",
    "feedback_comment": "TEXT",
    "expected_answer": "TEXT",
    "evaluation_status": "TEXT DEFAULT 'unreviewed'",
}

# Field order used for /logs/export (both JSONL and CSV).
EXPORT_FIELDS = [
    "id", "username", "question", "rewritten_query", "answer",
    "folder_path", "selected_file", "retrieved_chunks", "source_files",
    "similarity_scores", "model", "timestamp", "latency_ms",
    "user_feedback", "feedback_comment", "expected_answer", "evaluation_status",
]

# Fields stored as JSON strings that should be expanded back to objects in JSONL.
JSON_FIELDS = {"retrieved_chunks", "source_files", "similarity_scores"}

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            question TEXT NOT NULL,
            rewritten_query TEXT,
            answer TEXT NOT NULL,
            folder_path TEXT,
            selected_file TEXT,
            sources TEXT,
            retrieved_chunks TEXT,
            source_files TEXT,
            similarity_scores TEXT,
            model TEXT,
            latency_ms INTEGER,
            user_feedback TEXT,
            feedback_comment TEXT,
            expected_answer TEXT,
            evaluation_status TEXT DEFAULT 'unreviewed',
            timestamp TEXT NOT NULL
        )
    """)

    # Migrate an existing conversations table without deleting old logs.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    for column, column_type in NEW_COLUMNS.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE conversations ADD COLUMN {column} {column_type}")

    conn.commit()
    conn.close()

# Captures the most recent logging failure so /debug/db can surface it.
LAST_LOG_ERROR = None

def log_conversation(
    username, question, answer, folder_path, selected_file, sources,
    rewritten_query=None, retrieved_chunks=None, model=None, latency_ms=None
):
    """Insert one evaluation-ready record and return its row id (or None on failure)."""
    global LAST_LOG_ERROR
    try:
        retrieved_chunks = retrieved_chunks or []
        similarity_scores = [chunk.get("score") for chunk in retrieved_chunks]
        # Unique source filenames, order preserved.
        source_files = list(dict.fromkeys(
            chunk.get("filename") for chunk in retrieved_chunks if chunk.get("filename")
        ))

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("""
            INSERT INTO conversations (
                username, question, rewritten_query, answer, folder_path,
                selected_file, sources, retrieved_chunks, source_files,
                similarity_scores, model, latency_ms, evaluation_status, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            username,
            question,
            rewritten_query or "",
            answer,
            folder_path or "",
            selected_file or "",
            json.dumps(sources or [], ensure_ascii=False),
            json.dumps(retrieved_chunks, ensure_ascii=False),
            json.dumps(source_files, ensure_ascii=False),
            json.dumps(similarity_scores),
            model or "",
            latency_ms,
            "unreviewed",
            datetime.utcnow().isoformat()
        ))
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id
    except Exception as e:
        LAST_LOG_ERROR = repr(e)
        print(f"Logging error: {e}")
        return None

# ── Export helpers (kept separate so they're testable without HTTP) ────────
def fetch_all_rows(limit=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM conversations ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def rows_to_jsonl(rows):
    lines = []
    for r in rows:
        record = {}
        for field in EXPORT_FIELDS:
            value = r.get(field)
            if field in JSON_FIELDS and isinstance(value, str) and value:
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    pass
            record[field] = value
        lines.append(json.dumps(record, ensure_ascii=False))
    return "\n".join(lines)

def rows_to_csv(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({field: r.get(field) for field in EXPORT_FIELDS})
    return buffer.getvalue()

# ── Request models ────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: str

class QueryRequest(BaseModel):
    question: str
    folder_path: Optional[str] = None
    selected_file: Optional[str] = None
    history: Optional[List[Message]] = []

class FeedbackRequest(BaseModel):
    conversation_id: int
    feedback: Optional[str] = None   # "up" or "down"
    comment: Optional[str] = None

class EvaluationRequest(BaseModel):
    conversation_id: int
    expected_answer: Optional[str] = None
    evaluation_status: Optional[str] = None

# ── Embed query ───────────────────────────────────────────
def embed_query(text):
    response = openai_client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )
    return response.data[0].embedding

# ── Search Qdrant ─────────────────────────────────────────
def search(query_text, folder_path=None, selected_file=None):
    query_vector = embed_query(query_text)

    must_conditions = []

    if folder_path:
        must_conditions.append(
            FieldCondition(key="folder_path", match=MatchText(text=folder_path))
        )

    if selected_file:
        must_conditions.append(
            FieldCondition(key="filename", match=MatchValue(value=selected_file))
        )

    search_filter = Filter(must=must_conditions) if must_conditions else None

    results = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=TOP_K,
        query_filter=search_filter,
        with_payload=True
    ).points
    return results

# ── Detect overview question ──────────────────────────────
def is_overview_question(question):
    overview_phrases = [
        "what's in this file", "what is in this file", "whats in this file",
        "summarize this file", "summarize this", "what does this file contain",
        "overview of this file", "what are the sheets", "explain this file",
        "what is this file about", "give me an overview", "what info is here",
        "what information is here", "contents of this file", "what is in here",
        "what's in here", "whats in here", "tell me about this file",
        "describe this file", "what does this contain"
    ]
    q = question.lower().strip()
    return any(phrase in q for phrase in overview_phrases)

# ── Fetch all chunks from a file ──────────────────────────
def fetch_all_file_chunks(filename, folder_path=None):
    must_conditions = [
        FieldCondition(key="filename", match=MatchValue(value=filename))
    ]
    if folder_path:
        must_conditions.append(
            FieldCondition(key="folder_path", match=MatchText(text=folder_path))
        )

    results, _ = qdrant.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(must=must_conditions),
        limit=200,
        with_payload=True
    )
    return results

# ── Detect intent ─────────────────────────────────────────
def is_search_query(message):
    response = openai_client.chat.completions.create(
        model=ANSWER_MODEL,
        messages=[{
            "role": "user",
            "content": f"Is this message related to finding, listing, or retrieving information from documents? This includes follow-up requests like 'more', 'list them', 'give me names', 'from the folder', 'what else', 'who else'. Reply with only 'search' or 'conversation'. Only reply 'conversation' for greetings, thanks, and clearly social messages.\n\nMessage: {message}"
        }],
        temperature=0
    )
    return response.choices[0].message.content.strip().lower() == "search"

# ── Rewrite query with conversation context ───────────────
def rewrite_query(question, history):
    if not history:
        return question
    recent = history[-4:]
    history_text = "\n".join([f"{m.role}: {m.content}" for m in recent])
    response = openai_client.chat.completions.create(
        model=ANSWER_MODEL,
        messages=[{
            "role": "user",
            "content": f"Given this conversation history, rewrite the latest question as a complete standalone search query that includes all necessary context.\n\nConversation:\n{history_text}\nLatest question: {question}\n\nRewritten query (just the query, nothing else):"
        }],
        temperature=0
    )
    return response.choices[0].message.content.strip()

# ── Generate answer ───────────────────────────────────────
def generate_answer(query_text, folder_path=None, selected_file=None, history=None):
    if history is None:
        history = []

    search_query = query_text

    if not is_search_query(query_text):
        response = openai_client.chat.completions.create(
            model=ANSWER_MODEL,
            messages=[{
                "role": "user",
                "content": f"You are a helpful AI assistant for Fuse Power Management. Respond naturally and conversationally to this message: {query_text}"
            }],
            temperature=0.7
        )
        return {
            "answer": response.choices[0].message.content,
            "sources": [],
            "rewritten_query": search_query,
            "retrieved_chunks": [],
            "model": ANSWER_MODEL
        }

    # Use full-file retrieval for overview questions.
    overview_mode = bool(selected_file and is_overview_question(query_text))
    if overview_mode:
        results = fetch_all_file_chunks(selected_file, folder_path)
        print(f"Overview mode: fetched {len(results)} chunks from {selected_file}")
    else:
        search_query = rewrite_query(query_text, history) if history else query_text
        results = search(search_query, folder_path, selected_file)

    if not results:
        return {
            "answer": "I couldn't find information about that in the selected documents. You may want to try a different folder or rephrase your question.",
            "sources": [],
            "rewritten_query": search_query,
            "retrieved_chunks": [],
            "model": ANSWER_MODEL
        }

    context = ""
    sources = []
    retrieved_chunks = []
    seen_files = set()
    i = 0

    for r in results:
        chunk_text = r.payload.get("text", "")
        filename = r.payload.get("filename", "unknown")
        folder = r.payload.get("folder_path", "")
        chunk_index = r.payload.get("chunk_index")
        file_id = r.payload.get("file_id")
        file_key = f"{filename}_{chunk_text[:50]}"

        if file_key in seen_files:
            continue
        seen_files.add(file_key)

        i += 1
        context += f"\n[{i}] {chunk_text}\n"
        sources.append({
            "index": i,
            "filename": filename,
            "folder_path": folder
        })
        retrieved_chunks.append({
            "rank": i,
            "filename": filename,
            "folder_path": folder,
            "file_id": file_id,
            "chunk_index": chunk_index,
            # Scroll results used for overview mode do not have similarity scores.
            "score": getattr(r, "score", None),
            "text": chunk_text
        })

    messages = []
    for m in history[-6:]:
        messages.append({"role": m.role, "content": m.content})

    if overview_mode:
        prompt_instruction = f"""You are an AI assistant for Fuse Power Management.
The user has selected the file "{selected_file}" and wants an overview of its contents.
Based on ALL the context provided below (which contains chunks from the entire file), provide a comprehensive summary including:
- What the file is about
- Key sections or sheets
- Important data, numbers, or findings
- How the information is structured

Do not say you could not find information — summarize everything you can see in the context.
Do not cite source numbers for overview questions — just give a clear structured summary."""
    else:
        prompt_instruction = """You are an AI assistant for Fuse Power Management.
Answer the question based on the context provided below.
Extract and summarize any relevant information you find, even if it is partial.
If the answer is not in the context, say you couldn't find it in the selected documents and suggest trying a different folder.
Always cite which source number(s) you used.
Respond in a natural, helpful tone."""

    messages.append({
        "role": "user",
        "content": f"""{prompt_instruction}

Context:
{context}

Question: {query_text}

Answer:"""
    })

    response = openai_client.chat.completions.create(
        model=ANSWER_MODEL,
        messages=messages,
        temperature=0
    )

    return {
        "answer": response.choices[0].message.content,
        "sources": sources,
        "rewritten_query": search_query,
        "retrieved_chunks": retrieved_chunks,
        "model": ANSWER_MODEL
    }

# ── Startup ───────────────────────────────────────────────
@app.on_event("startup")
def startup():
    init_db()

# ── Endpoints ─────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Fuse AI Knowledge Assistant is running"}

@app.get("/logout")
def logout():
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Logged out",
        headers={"WWW-Authenticate": "Basic"},
    )

@app.get("/me")
def get_me(username: str = Depends(verify_password)):
    return {"username": username, "is_admin": username == ADMIN_USER}

@app.get("/ui")
def serve_ui(username: str = Depends(verify_password)):
    return FileResponse(os.path.join(BASE_DIR, "app.html"))

@app.get("/admin")
def serve_admin(username: str = Depends(require_admin)):
    return FileResponse(os.path.join(BASE_DIR, "logs.html"))

@app.get("/debug/db")
def debug_db(username: str = Depends(require_admin)):
    """Admin-only diagnostics: confirms which DB file is in use, its real schema,
    row count, and the last logging error (if any)."""
    db_dir = os.path.dirname(DB_PATH) or "."
    info = {
        "db_path": DB_PATH,
        "db_dir_exists": os.path.isdir(db_dir),
        "db_file_exists": os.path.isfile(DB_PATH),
        "data_disk_mounted": os.path.isdir("/data"),
        "last_log_error": LAST_LOG_ERROR,
    }
    try:
        conn = sqlite3.connect(DB_PATH)
        info["columns"] = [
            {"name": r[1], "type": r[2], "notnull": r[3], "default": r[4]}
            for r in conn.execute("PRAGMA table_info(conversations)")
        ]
        info["row_count"] = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
    except Exception as e:
        info["read_error"] = repr(e)
    return info

@app.get("/folders")
def get_folders():
    results = qdrant.scroll(
        collection_name=COLLECTION_NAME,
        limit=1000,
        with_payload=True
    )
    folders = sorted(set(
        point.payload.get("folder_path", "")
        for point in results[0]
        if point.payload.get("folder_path")
    ))
    return {"folders": folders}

@app.get("/files")
def get_files(folder_path: str = None):
    results = qdrant.scroll(
        collection_name=COLLECTION_NAME,
        limit=10000,
        with_payload=True
    )

    files = {}
    for point in results[0]:
        fp = point.payload.get("folder_path", "")
        fn = point.payload.get("filename", "")

        if folder_path and not fp.startswith(folder_path):
            continue

        if fn and fp:
            key = f"{fp}/{fn}"
            if key not in files:
                files[key] = {
                    "filename": fn,
                    "folder_path": fp
                }

    return {"files": sorted(files.values(), key=lambda x: x["filename"])}

@app.post("/query")
def query(request: QueryRequest, username: str = Depends(verify_password)):
    start_time = time.perf_counter()

    result = generate_answer(
        request.question,
        request.folder_path,
        request.selected_file,
        request.history
    )

    latency_ms = int((time.perf_counter() - start_time) * 1000)

    row_id = log_conversation(
        username=username,
        question=request.question,
        answer=result["answer"],
        folder_path=request.folder_path,
        selected_file=request.selected_file,
        sources=result.get("sources", []),
        rewritten_query=result.get("rewritten_query"),
        retrieved_chunks=result.get("retrieved_chunks", []),
        model=result.get("model"),
        latency_ms=latency_ms
    )

    # Keep the UI payload lean; the full retrieved_chunks stay in the DB for eval.
    return {
        "id": row_id,
        "answer": result["answer"],
        "sources": result.get("sources", []),
    }

@app.post("/feedback")
def submit_feedback(request: FeedbackRequest, username: str = Depends(verify_password)):
    """Any logged-in user can rate an answer (👍/👎) with an optional comment."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            """UPDATE conversations
               SET user_feedback = ?,
                   feedback_comment = COALESCE(?, feedback_comment)
               WHERE id = ?""",
            (request.feedback, request.comment, request.conversation_id)
        )
        conn.commit()
        updated = cursor.rowcount
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save feedback: {e}")
    if not updated:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True, "id": request.conversation_id}

@app.post("/evaluate")
def evaluate(request: EvaluationRequest, username: str = Depends(require_admin)):
    """Admin fills in the expected answer and/or review status for a row."""
    updates = []
    values = []
    if request.expected_answer is not None:
        updates.append("expected_answer = ?")
        values.append(request.expected_answer)
    if request.evaluation_status is not None:
        updates.append("evaluation_status = ?")
        values.append(request.evaluation_status)
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    values.append(request.conversation_id)
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?",
            values
        )
        conn.commit()
        updated = cursor.rowcount
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save evaluation: {e}")
    if not updated:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True, "id": request.conversation_id}

@app.get("/logs")
def get_logs(limit: Optional[int] = None, username: str = Depends(require_admin)):
    """Full conversation history (no 100-row cap). Pass ?limit=N to page."""
    rows = fetch_all_rows(limit=limit)
    return {"count": len(rows), "conversations": rows}

@app.get("/logs/export")
def export_logs(format: str = "jsonl", username: str = Depends(require_admin)):
    rows = fetch_all_rows()
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    if format == "csv":
        content = rows_to_csv(rows)
        media_type = "text/csv"
        filename = f"fuse_eval_dataset_{stamp}.csv"
    else:
        content = rows_to_jsonl(rows)
        media_type = "application/x-ndjson"
        filename = f"fuse_eval_dataset_{stamp}.jsonl"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
