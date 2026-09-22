import os
import secrets
import sqlite3
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional, List
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchText
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
DB_PATH = "/data/conversations.db"

# ── Users ─────────────────────────────────────────────────
USERS = {
    "rob": os.getenv("UI_PASSWORD", "fuse2026"),
    "bryan": os.getenv("UI_PASSWORD", "fuse2026"),
    "shimei": os.getenv("UI_PASSWORD", "fuse2026"),
    "haowen": os.getenv("UI_PASSWORD", "fuse2026"),
    "sarvin": os.getenv("UI_PASSWORD", "fuse2026"),
}

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

# ── SQLite setup ──────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            folder_path TEXT,
            selected_file TEXT,
            sources TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def log_conversation(username, question, answer, folder_path, selected_file, sources):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO conversations (username, question, answer, folder_path, selected_file, sources, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            username,
            question,
            answer,
            folder_path or "",
            selected_file or "",
            str([s["filename"] for s in sources]),
            datetime.utcnow().isoformat()
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Logging error: {e}")

# ── Request model ─────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: str

class QueryRequest(BaseModel):
    question: str
    folder_path: Optional[str] = None
    selected_file: Optional[str] = None
    history: Optional[List[Message]] = []

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
            FieldCondition(key="filename", match=MatchText(text=selected_file))
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

# ── Detect intent ─────────────────────────────────────────
def is_search_query(message):
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
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
        model="gpt-4o-mini",
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

    if not is_search_query(query_text):
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": f"You are a helpful AI assistant for Fuse Power Management. Respond naturally and conversationally to this message: {query_text}"
            }],
            temperature=0.7
        )
        return {
            "answer": response.choices[0].message.content,
            "sources": []
        }

    search_query = rewrite_query(query_text, history) if history else query_text
    results = search(search_query, folder_path, selected_file)

    if not results:
        return {
            "answer": "I couldn't find information about that in the selected documents. You may want to try a different folder or rephrase your question.",
            "sources": []
        }

    context = ""
    sources = []
    seen_files = set()
    i = 0

    for r in results:
        chunk_text = r.payload.get("text", "")
        filename = r.payload.get("filename", "unknown")
        folder = r.payload.get("folder_path", "")
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

    messages = []
    for m in history[-6:]:
        messages.append({"role": m.role, "content": m.content})

    messages.append({
        "role": "user",
        "content": f"""You are an AI assistant for Fuse Power Management.
Answer the question based on the context provided below.
Extract and summarize any relevant information you find, even if it is partial.
If the answer is not in the context, say you couldn't find it in the selected documents and suggest trying a different folder.
Always cite which source number(s) you used.
Respond in a natural, helpful tone.

Context:
{context}

Question: {query_text}

Answer:"""
    })

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0
    )

    return {
        "answer": response.choices[0].message.content,
        "sources": sources
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
    return {"username": username}

@app.get("/ui")
def serve_ui(username: str = Depends(verify_password)):
    return FileResponse(os.path.join(os.path.dirname(__file__), "app.html"))

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
    result = generate_answer(
        request.question,
        request.folder_path,
        request.selected_file,
        request.history
    )
    log_conversation(
        username=username,
        question=request.question,
        answer=result["answer"],
        folder_path=request.folder_path,
        selected_file=request.selected_file,
        sources=result["sources"]
    )
    return result

@app.get("/logs")
def get_logs(username: str = Depends(verify_password)):
    if username != "sarvin":
        raise HTTPException(status_code=403, detail="Access denied")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM conversations ORDER BY timestamp DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return {"conversations": [dict(r) for r in rows]}