# Relation candidates

Import `CandidateFinder` from `app.relations.candidates` and construct it
with the Qdrant store and the loaded
organisation config. Call `find(doc_id, org_id=…)` on an indexed document.
Each result gives the target document ID, the matching source and target
chunks, and the reasons for each pair. Evidence for an explicit reference includes
the matching key, such as `number:circ-12/2024`. Similarity pairs also
include the retrieval score.

Candidate finding combines three bounded lookups.

- Reference cues under `relation_detection.reference_cues` enable exact
  matches for nearby numbers, dates, and titles. Indexed titles come from the
  filename stem, the main heading, or the opening lines.
- Matching clause numbers or headings supply separate structural evidence.
- Similarity uses the configured retriever and limits the search to the same
  document type and any additional target types under that source type in
  `relation_detection.related_document_types`.

All lookups stay within the supplied organisation. Known target dates must
precede the source date. If either date is missing, the pair carries
`needs_date_review` so downstream code can distinguish it from a confirmed
chronological match. Any dates mentioned in the text help match references.
Chronological checks use the stored metadata dates.

Explicit references rank ahead of structural matches and similarity-only
matches. `RELATION_CANDIDATE_LIMIT` caps the document results. The finder
queries up to `RELATION_QUERY_CHUNKS` source chunks, prioritising chunks
with references and structural metadata. `RELATION_CHUNK_HITS` caps the
similarity hits per queried chunk, and `RELATION_MIN_SIMILARITY` is the
exclusive score floor on the configured retriever's scale.

Normal indexing adds the exact lookup keys. Previously indexed documents can
still appear through similarity. Re-index them to enable direct reference
and structure lookups. Unchanged documents reuse their saved embeddings.

## Classifying proposals

Send `{"doc_id":"<indexed-document-id>"}` to `POST /v1/detect-relations`.
The service selects chunk pairs from the document's own organisation and
sends each pair to the configured LLM. It retries invalid structured output
up to `RELATION_MAX_RETRIES` and caches schema-valid drafts.
`RELATION_MAX_PAIRS` and `RELATION_PAIRS_PER_CANDIDATE` cap the number of
pairs considered by each request.

Both quotes must match their own canonical document after Unicode and
whitespace normalization. Only verified spans become citations. The response
returns proposals and flags missing evidence, uncertain dates, absent
target references, and ambiguous change language with `needs_review`.
Language-specific `relation_detection.change_cues` and
`relation_detection.nonoperative_cues` help distinguish stated changes
from negated or conditional wording.

Conflicts describe incompatible instructions on the same subject that
remain in force. Conflict proposals require human review of applicability.
Evidence of an operative change prevents a conflicting-instructions
classification for the same pair. Confidence combines explicit reference
(0.20), two verified quotes (0.45), established date order (0.20), and
recognized wording (0.15). Each contribution is included in the result.
