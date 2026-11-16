# Chunk retrieval

Pass the existing `QdrantStore` to `create_retriever(store)`. The retriever uses
the store's collection, vector names, and configured query embedder. That keeps
query prompts, sparse tokenization, and the stored embedding configuration
aligned.

```python
from app.retrieval import create_retriever

retriever = create_retriever(store)
candidates = retriever.retrieve(
    "monthly report deadline",
    filters={"language": "en", "doc_date_from": "2026-01-01"},
    top_k=5,
)
```

Each candidate contains a validated v1 `Chunk`, a score, and the source
`dense`, `sparse`, or `fused`. Dense and sparse modes return their Qdrant
similarity scores. Hybrid scores reflect reciprocal rank fusion (RRF). Scores
from different modes use different scales.

| Environment variable | Default | Behavior |
| --- | --- | --- |
| `RETRIEVAL_MODE` | `hybrid` | Select `hybrid`, `dense`, or `sparse` through the factory. |
| `RETRIEVAL_CANDIDATE_POOL_SIZE` | `50` | Maximum hits fetched per branch, before merging. |
| `RETRIEVAL_RRF_K` | `2` | Positive RRF constant for zero-based ranks. |
| `RETRIEVAL_DEFAULT_TOP_K` | `10` | Final result limit when omitted, from 1 to 100. |

Evaluation can pass a `RetrievalConfig` to the factory or to any retriever
directly. The final limit applies to the union in hybrid mode, so the number of
results can be lower than `top_k`. The retrievers order the returned candidate
pool by descending score, using Qdrant point ID as the tie break.

Combine exact matches for `department`, `doc_type`, `language`, and
`doc_id` using the v1 string filter map. `document_type` is an alias for
`doc_type`, as used in the v1 search example. Use `doc_date_from` and
`doc_date_to` as inclusive UTC calendar dates in `YYYY-MM-DD` format.
Either bound can be used alone, and `doc_date` matches a single day. A date
condition excludes chunks with no date.

With `k=2`, hybrid mode uses one Qdrant Query API call with filtered prefetches
and server-side RRF. The pinned server supports the standard constant. Custom
`k` values use two filtered Query API calls and fuse the ranked lists locally
with `sum(1 / (k + rank))`. [Qdrant RRF reference](https://qdrant.tech/documentation/search/hybrid-queries/#setting-rrf-constant-k)

If the query has no sparse terms, hybrid mode fuses the dense list by itself
and sparse-only mode returns an empty list. Missing or empty collections return
an empty list.
