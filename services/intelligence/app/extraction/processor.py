"""Convert extraction drafts to cited, resolved, deduplicated obligations."""

import json
from collections import Counter
from collections.abc import Sequence
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from app.generation import GeneratedClaim
from app.verification import CitationVerifier, VerificationConfig
from intellidocs_intelligence.models import (
    Chunk,
    ExtractionResult,
    ExtractionStats,
    Obligation,
    OrgConfig,
    ParsedDocument,
)

from .detector import contains_phrase
from .extractor import DraftObligation
from .resolution import DeadlineNormalizer, OwnerResolver, normalize_text, owner_key

EXTRACTOR_VERSION = "obligation-extraction.v1"


def _sourced(raw: str, quote: str) -> bool:
    text = normalize_text(raw)
    return bool(text) and contains_phrase(normalize_text(quote), text)


class ObligationProcessor:
    def __init__(self, org: OrgConfig) -> None:
        self.owner_resolver = OwnerResolver(org)
        self.deadline_normalizer = DeadlineNormalizer(org)
        self.verifier = CitationVerifier(
            VerificationConfig(fuzzy_threshold=1.0, support_check=False)
        )

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

        unique: dict[str, Obligation] = {}
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
            status: Literal["verified", "needs_review", "rejected"] = "verified"
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
            if (existing := unique.get(identifier)) is not None:
                if existing.citations is not None:
                    existing.citations.extend(
                        citation for citation in citations if citation not in existing.citations
                    )
                continue
            unique[identifier] = Obligation(
                id=identifier,
                doc_id=document.id,
                action=draft.action,
                owner=owner,
                deadline=deadline,
                condition=draft.condition,
                obligation_type=draft.obligation_type,
                citations=citations,
                confidence=0.0,
                status=status,
                extractor_version=EXTRACTOR_VERSION,
            )
        counts = Counter(obligation.status for obligation in unique.values())
        return ExtractionResult(
            doc_id=document.id,
            obligations=list(unique.values()),
            warnings=[],
            stats=ExtractionStats(
                total=len(unique),
                extracted=0,
                verified=counts["verified"],
                needs_review=counts["needs_review"],
                rejected=counts["rejected"],
            ),
        )
