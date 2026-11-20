"""Compose audience evidence, stored obligation points, and a verified summary."""

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import portalocker

from app.generation import GenerationError
from app.ingestion import load_org
from app.retrieval import Retriever, create_retriever
from app.storage import Storage
from app.vector_store import QdrantStore
from app.verification import (
    VERIFIER_VERSION,
    CitationVerifier,
    VerificationConfig,
    verifier_from_env,
)
from intellidocs_intelligence.cache import DiskCache
from intellidocs_intelligence.embeddings import configured_embedder
from intellidocs_intelligence.llm import provider_from_env
from intellidocs_intelligence.models import (
    Obligation,
    OrgConfig,
    ParseResult,
    Summary,
    SummaryAudience,
    SummaryConfig,
    SummaryPoint,
    SummaryProfile,
    SummaryRequest,
)

from .evidence import obligation_points, select_chunks
from .generator import SummaryGenerator


def _profiles(
    request: SummaryRequest, org: OrgConfig, config: SummaryConfig
) -> list[SummaryProfile]:
    selectors = (
        (request.department, org.departments, config.department_profiles, "department"),
        (request.role, org.roles, config.role_profiles, "role"),
    )
    profiles = []
    for name, allowed, configured, label in selectors:
        if name is None:
            continue
        if name not in allowed:
            raise ValueError(f"Unknown summary {label}: {name}")
        if name in configured:
            profiles.append(configured[name])
    if request.department and request.role:
        role_department = org.roles[request.role].department
        if role_department and role_department != request.department:
            raise ValueError("Summary role does not belong to the requested department")
    return profiles


class SummaryService:
    def __init__(
        self,
        storage: Storage,
        retriever: Retriever,
        generator: SummaryGenerator,
        verifier: CitationVerifier,
        cache: DiskCache,
        org_loader: Callable[[str | None], OrgConfig] = load_org,
    ) -> None:
        self.storage = storage
        self.retriever = retriever
        self.generator = generator
        # Summary drafts must contain exact quotes, even if answer verification allows fuzziness.
        self.verifier = CitationVerifier(
            VerificationConfig(support_check=verifier.config.support_check), verifier.provider
        )
        self.cache = cache
        self.org_loader = org_loader

    def summarize(
        self, request: SummaryRequest, *, assigned_obligations: Sequence[Obligation]
    ) -> Summary:
        """The caller supplies the current, authorized assignments as Obligation v1 records."""
        parsed = self.storage.load_result(request.doc_id)
        if request.org_id is not None and parsed.org_id != request.org_id:
            raise FileNotFoundError(request.doc_id)
        if parsed.document.status != "ready":
            raise ValueError("Document must be parsed successfully before summarization")
        org = self.org_loader(parsed.org_id)
        config = org.summary
        if config is None:
            raise ValueError("Summary profiles are not configured for this organization")
        language = request.language.casefold()
        if language not in {code.casefold() for code in org.languages}:
            raise ValueError(f"Unknown summary language: {request.language}")
        profiles = _profiles(request, org, config)
        key = self._cache_key(request, parsed, config, assigned_obligations)

        self.cache.directory.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.cache.directory / f"{key}.lock"), timeout=60):
            try:
                cached = self.cache.get(key)
                if cached is not None:
                    return Summary.model_validate(cached)
            except ValueError:
                # A partial or invalid on-disk entry can be rebuilt under the same lock.
                pass

            points = obligation_points(parsed.document, assigned_obligations)
            try:
                points.extend(self._generated_points(request, parsed, profiles, points, config))
            except (GenerationError, OSError):
                # A failed provider request must not become a permanent cache entry.
                return self._result(request, points, config)

            summary = self._result(request, points, config)
            self.cache.set(key, summary.model_dump(mode="json", exclude_none=True))
            return summary

    def _generated_points(
        self,
        request: SummaryRequest,
        parsed: ParseResult,
        profiles: Sequence[SummaryProfile],
        stored: Sequence[SummaryPoint],
        config: SummaryConfig,
    ) -> list[SummaryPoint]:
        budget = config.settings.max_length - sum(len(point.text) for point in stored)
        if budget <= 0:
            return []
        chunks = select_chunks(self.retriever, request.doc_id, parsed.org_id, profiles, stored)
        covered = [citation for point in stored for citation in point.citations]
        chunks = [
            chunk
            for chunk in chunks
            if not any(
                citation.char_start <= chunk.char_start and chunk.char_end <= citation.char_end
                for citation in covered
            )
        ]
        if not chunks:
            return []
        draft = self.generator.generate(request, profiles, chunks, stored, config.settings, budget)
        verified = self.verifier.verify(
            draft.points, chunks, documents={request.doc_id: parsed.document}
        )
        seen = {point.text for point in stored}
        points = []
        for claim in verified:
            if claim.status != "verified" or not claim.citations:
                continue
            text = claim.text.strip()
            if text in seen or len(text) > budget:
                continue
            if any(
                citation.char_start < previous.char_end and previous.char_start < citation.char_end
                for citation in claim.citations
                for previous in covered
            ):
                continue
            seen.add(text)
            points.append(SummaryPoint(text=text, citations=claim.citations))
            budget -= len(text)
        return points

    def _result(
        self, request: SummaryRequest, points: list[SummaryPoint], config: SummaryConfig
    ) -> Summary:
        reason = ""
        if not points:
            language = request.language.casefold()
            reasons = {code.casefold(): text for code, text in config.not_relevant_reasons.items()}
            reason = (
                reasons.get(language)
                or reasons.get(language.split("-", 1)[0])
                or "no_verified_audience_points"
            )
        return Summary(
            doc_id=request.doc_id,
            audience=SummaryAudience(department=request.department, role=request.role),
            language=request.language,
            points=points,
            not_relevant=not points,
            relevance_reason=reason,
            generated_by=self.generator.provider.model,
            verifier_version=VERIFIER_VERSION,
        )

    def _cache_key(
        self,
        request: SummaryRequest,
        parsed: ParseResult,
        config: SummaryConfig,
        assigned: Sequence[Obligation],
    ) -> str:
        document = parsed.document
        content_hash = document.content_hash
        if content_hash is None:
            with self.storage.source_path(document.id, document.filename).open("rb") as source:
                content_hash = hashlib.file_digest(source, "sha256").hexdigest()
        identity = {
            "request": request.model_dump(exclude={"assigned_obligations"}),
            "summary_payload": 2,
            "org": parsed.org_id,
            "content_hash": content_hash,
            "parser": [parsed.parser_name, parsed.parser_version],
            "prompt": self.generator.prompt_version,
            "provider": [self.generator.provider.provider_id, self.generator.provider.model],
            "verifier": [VERIFIER_VERSION, self.verifier.config.support_check],
            "config": config.model_dump(),
            "assigned": [item.model_dump(mode="json") for item in assigned],
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


@contextmanager
def summarizer_from_env(storage: Storage) -> Iterator[SummaryService]:
    storage_dir = Path(os.environ.get("STORAGE_DIR", "data/uploads"))
    cache_dir = Path(os.environ.get("LLM_CACHE_DIR") or storage_dir / "llm-cache")
    store = QdrantStore.from_env(configured_embedder())
    try:
        yield SummaryService(
            storage,
            create_retriever(store),
            SummaryGenerator(provider_from_env()),
            verifier_from_env(),
            DiskCache(cache_dir / "summaries"),
        )
    finally:
        store.client.close()
