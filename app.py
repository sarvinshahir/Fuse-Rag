import streamlit as st
import requests

API_URL = "http://localhost:8000"

st.set_page_config(
    page_title="Fuse AI Knowledge Assistant",
    page_icon="⚡",
    layout="wide"
)

st.title("⚡ Fuse AI Knowledge Assistant")
st.caption("Search and query internal Fuse Power documents using natural language.")

# ── Load folders ──────────────────────────────────────────
def get_folders():
    try:
        response = requests.get(f"{API_URL}/folders")
        folders = response.json().get("folders", [])
        return folders
    except:
        return []

def build_folder_tree(folders):
    tree = {}
    for folder in folders:
        parts = folder.split("/")
        node = tree
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]
    return tree

def get_full_path(parts):
    return "/".join(parts)

# ── Session state ─────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []

all_folders = get_folders()
tree = build_folder_tree(all_folders)

# Start directly inside Fuse Power Management
fpm_tree = tree.get("Fuse Power Management", {})

# ── Sidebar — cascading folder selector ───────────────────
with st.sidebar:
    st.header("📁 Document Scope")
    st.write("Select a folder to scope your search.")

    selected_path_parts = []
    selected_folder = None

    level1_options = ["All Fuse Power Management"] + sorted(fpm_tree.keys())
    level1 = st.selectbox("Folder", options=level1_options, index=0)

    if level1 != "All Fuse Power Management":
        selected_path_parts.append("Fuse Power Management")
        selected_path_parts.append(level1)
        level2_tree = fpm_tree[level1]

        if level2_tree:
            level2_options = ["— All in this folder —"] + sorted(level2_tree.keys())
            level2 = st.selectbox("Subfolder", options=level2_options, index=0)

            if level2 != "— All in this folder —":
                selected_path_parts.append(level2)
                level3_tree = level2_tree[level2]

                if level3_tree:
                    level3_options = ["— All in this folder —"] + sorted(level3_tree.keys())
                    level3 = st.selectbox("Sub-subfolder", options=level3_options, index=0)

                    if level3 != "— All in this folder —":
                        selected_path_parts.append(level3)

        selected_folder = get_full_path(selected_path_parts)
        st.info(f"🔒 Scoped to:\n`{'/'.join(selected_path_parts[1:])}`")
    else:
        st.info("🌐 Searching all Fuse Power Management documents")

    if st.button("🔄 New Conversation"):
        st.session_state.messages = []

# ── Chat history ──────────────────────────────────────────
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if "sources" in msg and msg["sources"]:
            with st.expander("📄 Sources"):
                for source in msg["sources"]:
                    st.write(f"**{source['filename']}**")
                    st.caption(source["folder_path"].replace("Fuse Power Management/", ""))

# ── Chat input ────────────────────────────────────────────
if prompt := st.chat_input("Ask a question about Fuse documents..."):

    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("assistant"):
        with st.spinner("Searching documents..."):
            try:
                payload = {
                    "question": prompt,
                    "folder_path": selected_folder,
                    "history": [
                        {"role": m["role"], "content": m["content"]}
                        for m in st.session_state.messages[-6:]
                    ]
                }
                response = requests.post(f"{API_URL}/query", json=payload)
                data = response.json()
                answer = data.get("answer", "No answer returned.")
                sources = data.get("sources", [])
            except Exception as e:
                answer = f"Error connecting to API: {str(e)}"
                sources = []

        st.markdown(answer)

        if sources:
            with st.expander("📄 Sources"):
                for source in sources:
                    st.write(f"**{source['filename']}**")
                    st.caption(source["folder_path"].replace("Fuse Power Management/", ""))

    st.session_state.messages.append({
        "role": "assistant",
        "content": answer,
        "sources": sources
    })