import os
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchText
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY")
)

COLLECTION_NAME = "fuse-knowledge"
TOP_K = 15

def embed_query(text):
    response = openai_client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )
    return response.data[0].embedding

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

def answer(query_text, folder_path=None):
    results = search(query_text, folder_path)

    if not results:
        return "No relevant documents found."

    context = ""
    sources = []
    seen_texts = set()
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
        sources.append(f"{i}. {filename} ({folder})")

    prompt = f"""You are an AI assistant for Fuse Power Management.
Answer the question based on the context provided below.
Extract and summarize any relevant information you find, even if it is partial.
If there is truly nothing relevant, say "I don't have enough information in the selected documents."
Always cite which source number(s) you used.

Context:
{context}

Question: {query_text}

Answer:"""

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )

    answer_text = response.choices[0].message.content
    sources_text = "\n\nSources:\n" + "\n".join(sources)
    return answer_text + sources_text

if __name__ == "__main__":
    print("Fuse AI Knowledge Assistant")
    print("Type 'exit' to quit\n")

    folder = input("Filter by folder path (press Enter to search all): ").strip()
    if not folder:
        folder = None

    while True:
        query = input("\nYour question: ").strip()
        if query.lower() == "exit":
            break
        if not query:
            continue

        print("\nSearching...\n")
        result = answer(query, folder_path=folder)
        print(result)
        print("\n" + "─"*60)