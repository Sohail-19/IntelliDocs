"""Stable UUID identities shared by chunk producers and vector storage."""

import json
from uuid import NAMESPACE_URL, uuid5


def chunk_id(doc_id: str, char_start: int, char_end: int) -> str:
    if not doc_id or char_start < 0 or char_end <= char_start:
        raise ValueError("Chunk identity requires a document ID and a nonempty character range")
    identity = json.dumps([doc_id, char_start, char_end], ensure_ascii=False, separators=(",", ":"))
    return str(uuid5(NAMESPACE_URL, identity))
