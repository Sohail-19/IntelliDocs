# Vector persistence

`VectorStore` exposes `upsert_chunks` and `delete_document`. `search` is a reserved
method that raises `NotImplementedError`; retrieval/ranking is not implemented.

```python
from qdrant_client import QdrantClient
from app.vector_store import QdrantStore
from intellidocs_intelligence.embeddings import FakeEmbedder

client = QdrantClient(":memory:")
try:
    store = QdrantStore(client, "chunks_v1", FakeEmbedder())
    acknowledged = store.upsert_chunks(chunks)
    store.delete_document(document_id)
finally:
    client.close()
```

For Compose use `QdrantStore.from_env(embedder)` with `QDRANT_URL` (for example,
`http://qdrant:6333`) and `QDRANT_COLLECTION`. When running on the host, use its
published endpoint such as `http://localhost:6333`. The caller owns/closes
`store.client`. The SDK is pinned to the 1.12 series to match the Compose server.

The collection is initialized on the first nonempty upsert. Dense size comes
from the embedder's actual vectors, never a hard-coded model dimension. Named
vectors default to `dense` (cosine) and `sparse` and can be named explicitly in
the constructor. All embeddings are validated before collection/point writes.
An existing collection must have compatible vector names, dense size, and cosine
distance. Conflicting payload index types cause an error, not index deletion.
Use a separate collection per embedding model/version, even if dimensions happen
to match; dimensional compatibility alone cannot prove model compatibility.

All supplied Chunk fields are stored as JSON payload, including aliases, source
offsets, block IDs, clause number, heading path, and metadata. Optional `None`
values are omitted. `doc_date` retains the contract's `YYYY-MM-DD` string; Qdrant
1.12 accepts this as midnight UTC for datetime indexing/filtering. Keyword indexes
are requested for `department`, `doc_type`, `language`, and `doc_id`; `doc_date`
uses a datetime index. Indexes are created before the first point upsert.

The backend point ID is a UUIDv5 of the unambiguous JSON tuple
`[doc_id, char_start, char_end]`. The chunker uses the same helper, so new chunk
`id`, `chunk_id`, and point ID agree. The store recomputes point identity even
when a legacy caller supplies arbitrary aliases; the original aliases remain in
the full payload for compatibility. Re-upserting the same spans replaces vectors
and payload rather than creating duplicate points, including when text/metadata
changes. Duplicate identities in a single batch are rejected as ambiguous.

`upsert_chunks` is a point upsert, not an implicit deletion of other spans. If a
document's chunk boundaries change, write the complete new chunk set, then call
`delete_document(doc_id, keep_chunk_ids=current_ids)` to prune obsolete spans.
Omitting `keep_chunk_ids` deletes the entire document. These operations are not
an atomic document replacement. Deletion uses an exact
`doc_id` payload filter, waits for completion, and never initializes an embedder.
All upserts also wait for acknowledgement; the returned count is points submitted,
not the number of newly created points. Pass `embeddings=...` to `upsert_chunks`
to persist precomputed vectors without invoking the store's embedder again.

Tests exclusively use `QdrantClient(":memory:")` with socket connections blocked.
They cover named vectors/dimensions, full payload round trips, repeat upserts,
deletion isolation, and department plus inclusive UTC date ranges using `scroll`
filters (not search). Local Qdrant intentionally does not build payload indexes;
a wrapping spy verifies all five index requests/types. No performance claim or
running-server dependency is implied by those in-memory tests.

`app/indexing.py` wires this component into `/v1/index`. The store itself does
not implement ranking or choose org metadata.
