# ⚡ Fuse AI Knowledge Assistant

An internal AI Knowledge Assistant for Fuse Power Management that reads company documents from OneDrive and answers questions using natural language, scoped to specific folder paths. Built on a RAG (Retrieval-Augmented Generation) architecture.

## What It Does

- Answers natural language questions across 200+ internal Fuse documents
- Scopes searches to specific project folders using cascading navigation
- Cites exact source documents and folder paths for every answer
- Supports PDF, Word (.docx), and Excel (.xlsx) files
- Distinguishes between search queries and conversational messages

## Architecture

Streamlit UI (app.py)
↓
FastAPI REST API (main.py)
↓
RAG Query Engine — OpenAI text-embedding-3-small + GPT-4o-mini
↓
Qdrant Cloud Vector Database — AWS Oregon — 1,239 vectors
↓
Microsoft OneDrive — Microsoft Graph API OAuth 2.0


## Tech Stack

| Component | Technology |
|---|---|
| Vector Database | Qdrant Cloud (AWS Oregon) |
| Embeddings | OpenAI text-embedding-3-small (1536 dimensions) |
| LLM | GPT-4o-mini |
| Backend API | FastAPI |
| Frontend | Streamlit |
| Document Source | Microsoft OneDrive via Microsoft Graph API |
| Authentication | OAuth 2.0 delegated flow via MSAL + Azure Entra ID |

## Project Structure
```
fuse-rag/
├── ingest.py # Document ingestion pipeline (OneDrive → Qdrant)
├── query.py # RAG query pipeline (terminal interface)
├── main.py # FastAPI REST API (/folders and /query endpoints)
├── app.py # Streamlit chat UI with cascading folder navigation
├── .env # API credentials (never committed — see .gitignore)
└── .gitignore # Excludes .env, venv/, pycache/
```

## Setup

### Prerequisites

- Python 3.9
- Qdrant Cloud account
- OpenAI API account
- Microsoft Azure app registration with Files.Read.All permission

### Installation

```bash
git clone https://github.com/sarvinshahir/Fuse-Rag.git
cd Fuse-Rag
python3 -m venv venv
source venv/bin/activate
pip install qdrant-client openai python-dotenv msal requests python-docx PyMuPDF openpyxl fastapi uvicorn streamlit
```

### Environment Variables

Create a `.env` file in the project root:

OPENAI_API_KEY=your_openai_api_key
QDRANT_URL=your_qdrant_cluster_url
QDRANT_API_KEY=your_qdrant_api_key
AZURE_CLIENT_ID=your_azure_client_id
AZURE_TENANT_ID=your_azure_tenant_id
AZURE_CLIENT_SECRET=your_azure_client_secret


### Running

**Step 1 — Ingest documents:**
```bash
python ingest.py
```
Authenticates with Microsoft, crawls OneDrive folders, extracts text, embeds chunks, and uploads to Qdrant.

**Step 2 — Start FastAPI backend:**
```bash
uvicorn main:app --port 8000
```

**Step 3 — Start Streamlit UI:**
```bash
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Health check |
| `/folders` | GET | Returns all indexed folder paths |
| `/query` | POST | Accepts question + folder_path, returns cited answer |

## Key Design Decisions

- **Deterministic UUID5 chunk IDs** — re-running ingestion updates existing vectors without duplication
- **Batch embedding** — 50 chunks per OpenAI API call for efficiency
- **Batch upsert** — 100 points per Qdrant request to avoid payload limits
- **Folder path payload index** — enables prefix-filtered vector similarity search
- **Post-retrieval deduplication** — removes duplicate chunks before LLM context assembly
- **Intent detection** — classifies messages as search queries or conversational before running RAG pipeline
- **10MB file size limit** — large files logged and skipped to avoid rate limits

## Phase Roadmap

### Phase 1 — Complete ✅
- OneDrive document ingestion (PDF, DOCX, XLSX)
- Qdrant Cloud vector storage with folder metadata
- RAG retrieval with folder-scoped search
- FastAPI REST API
- Streamlit UI with cascading folder navigation

### Phase 1.5 — Planned
- Production OneDrive connection (Rob's account via Netdigix admin consent)
- Cloud deployment (Azure App Service) for 24/7 access
- Nightly automated delta sync via Microsoft Graph API
- Hybrid search (dense + BM25 sparse vectors)
- Cross-encoder reranking (Cohere Rerank or FlashRank)
- OCR for scanned PDFs (Azure Document Intelligence)
- RAGAS evaluation framework

### Phase 2 — Planned
- AI content generation using institutional knowledge
- LangGraph multi-step agent orchestration
- Microsoft SSO authentication
- React/Next.js production UI

## Cost

Total infrastructure cost for Phase 1 development and testing: **$0.66**

| Item | Cost |
|---|---|
| OpenAI embeddings + queries | $0.66 |
| Qdrant Cloud | $0.00 (free tier) |
| Azure app registration | $0.00 |

---

*Built by Sarvin Shahir — AI & Data Analyst, Fuse Power Management Ltd.*
*July 2026*

