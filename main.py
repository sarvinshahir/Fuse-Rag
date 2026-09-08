import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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

# ── Request model ─────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: str

class QueryRequest(BaseModel):
    question: str
    folder_path: Optional[str] = None
    history: Optional[List[Message]] = []

# ── Embed query ───────────────────────────────────────────
def embed_query(text):
    response = openai_client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )
    return response.data[0].embedding

# ── Search Qdrant ─────────────────────────────────────────
def search(query_text, folder_path=None):
    query_vector = embed_query(query_text)

    search_filter = None
    if folder_path:
        search_filter = Filter(
            must=[
                FieldCondition(
                    key="folder_path",
                    match=MatchText(text=folder_path)
                )
            ]
        )

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
def generate_answer(query_text, folder_path=None, history=None):
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
    results = search(search_query, folder_path)

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

# ── Endpoints ─────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Fuse AI Knowledge Assistant is running"}

import os

@app.get("/ui")
def serve_ui():
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

@app.post("/query")
def query(request: QueryRequest):
    result = generate_answer(
        request.question,
        request.folder_path,
        request.history
    )
    return result