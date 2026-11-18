"""Fail-closed quote attribution and optional entailment checks for draft claims."""

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from string import Template
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.chunking.canonical import CanonicalDocument, canonical_document
from app.generation import GeneratedClaim
from intellidocs_intelligence.cache import CachedLLMProvider, DiskCache
from intellidocs_intelligence.llm import LLMProvider, provider_from_env
from intellidocs_intelligence.models import Answer, Chunk, Citation, Claim, ParsedDocument

from .config import VerificationConfig
from .matching import match_quote

VERIFIER_VERSION = "citation-verifier.v1"
PROMPTS = Path(__file__).with_name("prompts")


class SupportVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["supported", "not supported"]


def _citation(
    claim: GeneratedClaim,
    chunk: Chunk,
    threshold: float,
    document: CanonicalDocument | None,
) -> Citation | None:
    if chunk.char_end - chunk.char_start != len(chunk.text):
        return None
    first = chunk.page_start or chunk.page
    last = chunk.page_end or first
    if first != chunk.page or first > last:
        return None
    match = match_quote(chunk.text, claim.quote, threshold)
    if match is None:
        return None
    begin, end = match
    start, finish = chunk.char_start + begin, chunk.char_start + end
    if document is None:
        if first != last:
            return None  # Chunk bounds alone cannot locate a quote's exact page.
    else:
        if document.text[chunk.char_start : chunk.char_end] != chunk.text:
            return None
        sources = [span for span in document.blocks if span.start < finish and span.end > start]
        if not sources or any(span.block.type == "header_footer" for span in sources):
            return None
        if not first <= sources[0].page <= sources[-1].page <= last:
            return None
        first, last = sources[0].page, sources[-1].page
    return Citation(
        doc_id=chunk.doc_id,
        page=first,
        page_start=first,
        page_end=last,
        char_start=start,
        char_end=finish,
        quote=chunk.text[begin:end],
        clause_number=chunk.clause_number,
        heading_path=chunk.heading_path or [],
    )


class CitationVerifier:
    def __init__(
        self,
        config: VerificationConfig | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.config = config if config is not None else VerificationConfig.from_env()
        self.provider = provider

    def _supports(self, text: str, quote: str) -> bool:
        if not self.config.support_check:
            return True
        if self.provider is None:
            return False
        schema = SupportVerdict.model_json_schema()
        try:
            prompt = Template((PROMPTS / "support.txt").read_text(encoding="utf-8")).substitute(
                schema_json=json.dumps(schema),
                request_json=json.dumps({"claim": text, "quote": quote}, ensure_ascii=False),
            )
            response = self.provider.generate(prompt, schema)
            return SupportVerdict.model_validate(response).verdict == "supported"
        except Exception:
            # Provider, cache, JSON, and schema failures must never approve a claim.
            return False

    def verify(
        self,
        claims: Sequence[GeneratedClaim],
        chunks: Sequence[Chunk],
        *,
        documents: Mapping[str, ParsedDocument] | None = None,
    ) -> list[Claim]:
        by_id: dict[str, Chunk] = {}
        ambiguous: set[str] = set()
        for retrieved in chunks:
            identifier = retrieved.chunk_id or retrieved.id
            if identifier in by_id:
                ambiguous.add(identifier)
            by_id[identifier] = retrieved
        canonical: dict[str, CanonicalDocument | None] = {}
        output: list[Claim] = []
        for claim in claims:
            chunk = by_id.get(claim.chunk_id)
            citation = None
            if chunk is not None and claim.chunk_id not in ambiguous:
                if documents is not None and chunk.doc_id not in canonical:
                    document = documents.get(chunk.doc_id)
                    try:
                        canonical[chunk.doc_id] = (
                            canonical_document(document)
                            if document is not None and document.id == chunk.doc_id
                            else None
                        )
                    except ValueError:
                        canonical[chunk.doc_id] = None
                # If explicit documents were supplied, missing/invalid sources fail closed.
                if documents is None or canonical.get(chunk.doc_id) is not None:
                    citation = _citation(
                        claim, chunk, self.config.fuzzy_threshold, canonical.get(chunk.doc_id)
                    )
            supported = citation is not None and self._supports(claim.text, citation.quote)
            output.append(
                Claim(
                    text=claim.text,
                    citations=[citation] if citation is not None else [],
                    status="verified" if supported else "unverified",
                )
            )
        return output

    def build_answer(
        self,
        claims: Sequence[GeneratedClaim],
        chunks: Sequence[Chunk],
        *,
        model: str,
        documents: Mapping[str, ParsedDocument] | None = None,
    ) -> Answer:
        verified = [
            claim
            for claim in self.verify(claims, chunks, documents=documents)
            if claim.status == "verified"
        ]
        return Answer(
            answer_text="\n".join(claim.text for claim in verified),
            claims=verified,
            abstained=not verified,
            abstain_reason="No claims passed citation and support verification."
            if not verified
            else "",
            retrieved_chunk_ids=list(dict.fromkeys(chunk.chunk_id or chunk.id for chunk in chunks)),
            model=model,
            verifier_version=VERIFIER_VERSION,
        )


def verifier_from_env() -> CitationVerifier:
    config = VerificationConfig.from_env()
    provider = None
    if config.support_check:
        storage = Path(os.environ.get("STORAGE_DIR", "data/uploads"))
        cache = DiskCache(Path(os.environ.get("LLM_CACHE_DIR") or storage / "llm-cache"))
        provider = CachedLLMProvider(provider_from_env(), cache)
    return CitationVerifier(config, provider)
