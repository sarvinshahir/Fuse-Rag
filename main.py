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
    except Exception:
        pass

    try:
        qdrant.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="filename",
            field_schema=PayloadSchemaType.KEYWORD
        )
    except Exception:
        pass