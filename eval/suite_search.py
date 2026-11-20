"""Run retrieval and paired answer comparisons using the production pipeline."""

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

from app.answering import AnswerService
from app.generation import ClaimGenerator, GenerationResult
from app.reranking import NoOpReranker, Reranker, RerankerConfig, create_reranker
from app.retrieval import RetrievalConfig, Retriever, create_retriever
from app.search import SearchService
from app.storage import LocalStorage
from app.verification import CitationVerifier, verifier_from_env
from intellidocs_intelligence.embeddings import configured_embedder
from intellidocs_intelligence.llm import FakeProvider, LLMProvider, provider_from_env
from intellidocs_intelligence.models import (
    AnswerRequest,
    Chunk,
    ParseResult,
    SearchRequest,
    SearchResponse,
)

from .answers.answer_metrics import GoldAnswer, ScoredClaim, baseline_claims, score, summarize
from .retrieval.evaluate_retrieval import load_gold, open_store, read_jsonl
from .retrieval.metrics import ranking_metrics
from .suite_config import SuiteConfig, fingerprint, path
from .suite_report import Report


@dataclass(frozen=True)
class Variant:
    retrieval: str = "hybrid"
    rerank: bool = True
    verify: bool = True


VARIANTS = {
    "full": Variant(),
    "no_reranker": Variant(rerank=False),
    "dense_only": Variant(retrieval="dense"),
    "sparse_only": Variant(retrieval="sparse"),
    "verifier_off": Variant(verify=False),
    "plain_rag": Variant(retrieval="dense", rerank=False, verify=False),
}


def evaluate_retrieval(config: SuiteConfig, report: Report) -> None:
    inputs = config.inputs.retrieval
    gold, digest = load_gold(path(inputs.gold))
    settings = RetrievalConfig.from_env()
    reranking = RerankerConfig.from_env()
    reranker = create_reranker(reranking)
    embedder = configured_embedder()
    with open_store(embedder, path(inputs.chunks)) as (store, corpus):
        report.stages["retrieval"] = {
            "mode": "live retrieval over a local chunk snapshot",
            "gold": {"path": inputs.gold, "sha256": digest},
            "corpus": corpus,
            "embedding": embedder.cache_key(),
            "retrieval": asdict(settings),
            "reranking": asdict(reranking),
        }
        for name in ("full", *config.ablations):
            if name == "verifier_off":  # This intervention begins after retrieval.
                continue
            variant = VARIANTS[name]
            retriever = create_retriever(store, replace(settings, mode=variant.retrieval))
            groups: dict[str, list[dict[str, float]]] = defaultdict(list)
            for row in gold:
                with report.time("retrieval", name, "retrieve", "queries"):
                    pool = retriever.retrieve_pool(row.query, row.filters)
                if variant.rerank:
                    with report.time("retrieval", name, "rerank", "queries"):
                        pool = reranker.rerank(row.query, pool, len(pool))
                metrics = ranking_metrics(
                    [item.chunk for item in pool],
                    row.targets,
                    config.retrieval.recall_at,
                    config.retrieval.ndcg_at,
                )
                for group in (
                    "overall",
                    f"language={row.language}",
                    f"query_type={row.query_type}",
                    f"language={row.language}, query_type={row.query_type}",
                ):
                    groups[group].append(metrics)
            groups.setdefault("query_type=same_language", [])
            groups.setdefault("query_type=cross_lingual", [])
            for group, rows in groups.items():
                for metric in groups["overall"][0]:
                    report.add(
                        "retrieval",
                        name,
                        group,
                        metric,
                        mean(row[metric] for row in rows) if rows else None,
                        len(rows),
                        "queries",
                    )


class MeasuredSearch(SearchService):
    def __init__(self, retriever: Retriever, reranker: Reranker, report: Report, run: str) -> None:
        super().__init__(retriever, reranker)
        self.report, self.run = report, run

    def search(self, request: SearchRequest) -> SearchResponse:
        response = super().search(request.model_copy(update={"debug": True}))
        assert response.debug is not None
        stats = response.debug.timings_ms
        for operation, elapsed in (
            ("retrieve", stats.retrieve),
            ("rerank", stats.rerank),
            ("render_search_citations", stats.citations),
        ):
            if operation != "rerank" or not isinstance(self.reranker, NoOpReranker):
                self.report.latencies["answer", self.run, operation, "queries"].append(elapsed)
        return response


class MeasuredGenerator(ClaimGenerator):
    def __init__(self, provider: LLMProvider, report: Report, run: str) -> None:
        super().__init__(provider)
        self.report, self.run = report, run

    def generate(
        self, query: str, chunks: Sequence[Chunk], *, language: str | None = None
    ) -> GenerationResult:
        with self.report.time("answer", self.run, "generate", "queries"):
            return super().generate(query, chunks, language=language)


ANSWER_RATES = {
    "citation_faithfulness": ("citations", "attempted citations"),
    "citation_precision": ("citations", "attempted citations"),
    "unsupported_claim_rate": ("claims", "claims"),
    "abstention_rate_unanswerable": ("unanswerable_questions", "unanswerable queries"),
    "answer_coverage": ("expected_points", "expected points"),
}


def _answer_run(
    name: str,
    gold: Sequence[GoldAnswer],
    service: AnswerService,
    provider: LLMProvider,
    responses: dict[str, GenerationResult] | None,
    top_k: int,
    report: Report,
) -> dict[str, list[dict[str, int]]]:
    variant = VARIANTS[name]
    totals: dict[str, list[dict[str, int]]] = defaultdict(list)
    for row in gold:
        if responses is not None:
            assert isinstance(provider, FakeProvider)
            provider.response = responses[row.id].model_dump(mode="json")
        if row.language is None:
            raise ValueError(f"Answer gold query {row.id} must specify a language")
        with report.time("answer", name, "prepare", "queries"):
            draft = service.prepare(
                AnswerRequest(
                    query=row.query,
                    language=row.language,
                    top_k=top_k,
                    filters=row.filters,
                )
            )
        if name == "full" or not variant.verify:
            mode = "before_verification" if name == "full" else name
            totals[mode].append(score(row, baseline_claims(draft), draft))
        if variant.verify:
            with report.time("answer", name, "verify", "queries"):
                answer = service.finalize(draft)
            claims = [ScoredClaim(claim.text, claim.citations) for claim in answer.claims]
            mode = "after_verification" if name == "full" else name
            totals[mode].append(score(row, claims, draft))
        if draft.generation_failed:
            report.stages["answer"].setdefault("generation_failures", {})
            failures = report.stages["answer"]["generation_failures"]
            assert isinstance(failures, dict)
            failures.setdefault(name, []).append(row.id)
    return totals


def evaluate_answers(config: SuiteConfig, report: Report) -> None:
    inputs = config.inputs.answer
    gold, digest = read_jsonl(path(inputs.gold), GoldAnswer)
    ids = {row.id for row in gold}
    if len(ids) != len(gold):
        raise ValueError("Duplicate answer gold query IDs")
    responses = None
    if inputs.responses:
        response_path = path(inputs.responses)
        raw = json.loads(response_path.read_bytes())
        if set(raw) != ids:
            raise ValueError("Answer response fixture IDs must match the gold set")
        responses = {key: GenerationResult.model_validate(value) for key, value in raw.items()}
    provider = provider_from_env()
    verifier: CitationVerifier = verifier_from_env()
    config_retrieval = RetrievalConfig.from_env()
    reranker = create_reranker(RerankerConfig.from_env())
    with (
        TemporaryDirectory() as storage_dir,
        open_store(configured_embedder(), path(inputs.chunks)) as (store, corpus),
    ):
        storage = None
        if inputs.sources:
            storage = LocalStorage(Path(storage_dir))
            parsed, _ = read_jsonl(path(inputs.sources), ParseResult)
            if len({row.document.id for row in parsed}) != len(parsed):
                raise ValueError("Duplicate answer source document IDs")
            for source in parsed:
                storage.save_result(source.document.id, source)
        report.stages["answer"] = {
            "mode": "fixture replay through retrieval and verification"
            if responses is not None
            else "live generation and verification over a local chunk snapshot",
            "gold": {"path": inputs.gold, "sha256": digest},
            "corpus": corpus,
            "provider": provider.provider_id,
            "model": provider.model,
            "responses": fingerprint(path(inputs.responses)) if inputs.responses else None,
            "sources": fingerprint(path(inputs.sources)) if inputs.sources else None,
        }
        for name in ("full", *config.ablations):
            if name == "verifier_off":  # The paired pre-verification draft is that ablation.
                continue
            variant = VARIANTS[name]
            retriever = create_retriever(store, replace(config_retrieval, mode=variant.retrieval))
            service = AnswerService(
                MeasuredSearch(
                    retriever, reranker if variant.rerank else NoOpReranker(), report, name
                ),
                MeasuredGenerator(provider, report, name),
                verifier,
                storage,
            )
            totals = _answer_run(
                name, gold, service, provider, responses, config.retrieval.answer_top_k, report
            )
            for mode, counts in totals.items():
                scores = summarize(counts)
                report.rates("answer", mode, "overall", scores, ANSWER_RATES)
                if mode == "before_verification" and "verifier_off" in config.ablations:
                    report.rates("answer", "verifier_off", "overall", scores, ANSWER_RATES)
