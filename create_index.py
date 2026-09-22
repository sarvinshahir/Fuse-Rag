from qdrant_client import QdrantClient
from qdrant_client.models import PayloadSchemaType
import os
from dotenv import load_dotenv
load_dotenv()

qdrant = QdrantClient(url=os.getenv('QDRANT_URL'), api_key=os.getenv('QDRANT_API_KEY'))
qdrant.create_payload_index(
    collection_name='fuse-knowledge',
    field_name='filename',
    field_schema=PayloadSchemaType.TEXT
)
print('filename index created')