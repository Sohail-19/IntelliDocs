"""Convert extraction drafts to cited, resolved, deduplicated obligations."""

import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from app.generation import GeneratedClaim
from app.verification import CitationVerifier, VerificationConfig
from app.verification.matching import normalize
from intellidocs_intelligence.models import (
    Chunk,
    Citation,
    ExtractionResult,
    ExtractionStats,
    Obligation,
    ObligationDeadline,
    ObligationOwner,
    OrgConfig,
    ParsedDocument,
)

from .confidence import ConfidenceWeights
from .detector import CandidateDetector, contains_phrase
from .extractor import DraftObligation
from .resolution import DeadlineNormalizer, OwnerResolver, normalize_text, owner_key

EXTRACTOR_VERSION = "obligation-extraction.v1"
ExtractionStatus = Literal["verified", "needs_review", "rejected"]


@dataclass
class _ResolvedDraft:
    draft: DraftObligation
    owner: ObligationOwner
    deadline: ObligationDeadline
    status: ExtractionStatus
    citations: list[Citation] = field(default_factory=list)
    sources: set[tuple[str, int, int]] = field(default_factory=set)
    quote_exact: bool = False
    recognized_cue: bool = False

    def record_source(self, draft: DraftObligation, citation: Citation, has_cue: bool) -> None:
        if citation not in self.citations:
            self.citations.append(citation)
        if draft.obligation_type != self.draft.obligation_type:
            return
        self.sources.add((draft.chunk_id, citation.char_start, citation.char_end))
        self.quote_exact |= normalize(draft.quote).text == normalize(citation.quote).text
        self.recognized_cue |= has_cue

    def finish(self, identifier: str, doc_id: str, weights: ConfidenceWeights) -> Obligation:
        agreement = any(
            left_id != right_id and (left_end <= right_start or right_end <= left_start)
            for (left_id, left_start, left_end), (right_id, right_start, right_end) in combinations(
                self.sources, 2
            )
        )
        breakdown = weights.contributions(
            quote_exact=self.quote_exact,
            quote_fuzzy=bool(self.sources) and not self.quote_exact,
            owner_resolved=self.owner.resolved,
            deadline_resolved=self.deadline.resolved,
            recognized_cue=self.recognized_cue,
            cross_chunk_agreement=agreement,
        )
        return Obligation(
            id=identifier,
            doc_id=doc_id,
            action=self.draft.action,
            owner=self.owner,
            deadline=self.deadline,
            condition=self.draft.condition,
            obligation_type=self.draft.obligation_type,
            citations=self.citations,
            confidence=min(1.0, breakdown.total),
            confidence_breakdown=breakdown,
            status=self.status,
            extractor_version=EXTRACTOR_VERSION,
        )


def _sourced(raw: str, quote: str) -> bool:
    text = normalize_text(raw)
    return bool(text) and contains_phrase(normalize_text(quote), text)


class ObligationProcessor:
    def __init__(self, org: OrgConfig) -> None:
        self.owner_resolver = OwnerResolver(org)
        self.deadline_normalizer = DeadlineNormalizer(org)
        self.detector = CandidateDetector(org)
        self.weights = ConfidenceWeights.from_env()
        try:
            threshold = float(os.environ.get("EXTRACTION_FUZZY_THRESHOLD", "1.0"))
            config = VerificationConfig(fuzzy_threshold=threshold)
        except ValueError as error:
            raise ValueError("EXTRACTION_FUZZY_THRESHOLD must be in (0, 1]") from error
        self.verifier = CitationVerifier(config)

    def process(
        self,
        document: ParsedDocument,
        chunks: Sequence[Chunk],
        drafts: Sequence[DraftObligation],
    ) -> ExtractionResult:
        if any(chunk.doc_id != document.id for chunk in chunks):
            raise ValueError("Obligation chunks must belong to the supplied document")
        claims = [
            GeneratedClaim(text=draft.action, chunk_id=draft.chunk_id, quote=draft.quote)
            for draft in drafts
        ]
        checked = self.verifier.verify(claims, chunks, documents={document.id: document})

        languages = {chunk.chunk_id or chunk.id: chunk.language for chunk in chunks}
        unique: dict[str, _ResolvedDraft] = {}
        for draft, claim in zip(drafts, checked, strict=True):
            citations = claim.citations if claim.status == "verified" else []
            quote = citations[0].quote if citations else ""
            owner = self.owner_resolver.resolve(
                draft.owner_text, sourced=_sourced(draft.owner_text, quote)
            )
            deadline = self.deadline_normalizer.resolve(
                draft.deadline_text,
                document.doc_date,
                sourced=_sourced(draft.deadline_text, quote),
            )
            status: ExtractionStatus = "verified"
            if not citations:
                status = "rejected"
            elif not owner.resolved or (draft.deadline_text.strip() and not deadline.resolved):
                status = "needs_review"

            # Keep distinct triggers separate, even when the required action is identical.
            identity = [
                document.id,
                normalize_text(draft.action),
                (owner.resolved_department, owner.resolved_role)
                if owner.resolved
                else owner_key(owner.raw_text),
                deadline.normalized_date.isoformat()
                if deadline.normalized_date
                else normalize_text(deadline.raw_text),
                normalize_text(draft.condition or ""),
                status,
                (draft.chunk_id, draft.quote) if status == "rejected" else None,
            ]
            identifier = str(uuid5(NAMESPACE_URL, json.dumps(identity, ensure_ascii=False)))
            resolved = unique.get(identifier)
            if resolved is None:
                resolved = unique[identifier] = _ResolvedDraft(draft, owner, deadline, status)
            for citation in citations:
                has_cue = self.detector.has_cue(citation.quote, languages[draft.chunk_id])
                resolved.record_source(draft, citation, has_cue)
        obligations = [
            resolved.finish(identifier, document.id, self.weights)
            for identifier, resolved in unique.items()
        ]
        counts = Counter(obligation.status for obligation in obligations)
        return ExtractionResult(
            doc_id=document.id,
            obligations=obligations,
            warnings=[],
            stats=ExtractionStats(
                total=len(obligations),
                extracted=0,
                verified=counts["verified"],
                needs_review=counts["needs_review"],
                rejected=counts["rejected"],
            ),
        )
