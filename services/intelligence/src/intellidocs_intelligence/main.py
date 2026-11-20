"""HTTP entry point for the intelligence service."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse

from app.answering import AnswerFactory, answerer_from_env
from app.extraction.service import ExtractionFactory, IndexNotReady, extraction_from_env
from app.indexing import IndexingFactory, document_tree, indexer_from_env
from app.ingestion import IngestionService, load_org, select_org_id, storage_from_env
from app.relations.service import RelationFactory, relations_from_env
from app.retrieval.filters import InvalidFilter
from app.search import SearchFactory, search_from_env
from app.storage import Storage
from app.summarization import SummaryService, summarizer_from_env
from app.vector_store import QdrantStore

from .embeddings import configured_embedder
from .http_runtime import InternalHTTP, lifespan, readiness
from .models import (
    Answer,
    AnswerRequest,
    DetectRelationsRequest,
    ExtractionResult,
    ExtractObligationsRequest,
    IndexRequest,
    IndexResult,
    IngestResponse,
    RelationResult,
    SearchRequest,
    SearchResponse,
    Summary,
    SummaryRequest,
    UploadMetadata,
)
from .runtime import FeatureUnavailable, Runtime, require_llm

app = FastAPI(title="IntelliDocs Intelligence API", version="v1", lifespan=lifespan)
app.add_middleware(InternalHTTP)


@app.exception_handler(FeatureUnavailable)
async def feature_unavailable(_request: Request, error: FeatureUnavailable) -> JSONResponse:
    return JSONResponse(
        {"detail": {"code": error.code, "message": str(error)}},
        status_code=error.status,
        headers={"Cache-Control": "no-store"},
    )


SummaryFactory = Callable[[], AbstractContextManager[SummaryService]]


def get_indexing_factory(storage: Annotated[Storage, Depends(storage_from_env)]) -> IndexingFactory:
    # Defer configuration/model/client construction unless indexing is requested.
    return lambda: indexer_from_env(storage)


def get_search_factory(storage: Annotated[Storage, Depends(storage_from_env)]) -> SearchFactory:
    return lambda: search_from_env(storage)


def get_answer_factory(storage: Annotated[Storage, Depends(storage_from_env)]) -> AnswerFactory:
    return lambda: answerer_from_env(storage)


def get_extraction_factory(
    storage: Annotated[Storage, Depends(storage_from_env)],
) -> ExtractionFactory:
    return lambda: extraction_from_env(storage)


def get_relation_factory(storage: Annotated[Storage, Depends(storage_from_env)]) -> RelationFactory:
    return lambda: relations_from_env(storage)


def get_summary_factory(storage: Annotated[Storage, Depends(storage_from_env)]) -> SummaryFactory:
    return lambda: summarizer_from_env(storage)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the service liveness state."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    status, payload = readiness()
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


@app.post(
    "/v1/ingest", response_model=IngestResponse, status_code=202, response_model_exclude_none=True
)
def ingest(
    file: Annotated[UploadFile, File()],
    storage: Annotated[Storage, Depends(storage_from_env)],
    indexing_factory: Annotated[IndexingFactory, Depends(get_indexing_factory)],
    org_id: Annotated[str | None, Form(min_length=1)] = None,
    index: Annotated[bool, Form()] = False,
    department: Annotated[str | None, Form(min_length=1)] = None,
    doc_type: Annotated[str | None, Form(min_length=1)] = None,
    doc_date: Annotated[str | None, Form(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
) -> IngestResponse | JSONResponse:
    """Persist a validated upload, parsed content, and attachment results."""
    try:
        selected_org = select_org_id(org_id)
        result = IngestionService(storage).ingest(
            file,
            load_org(selected_org),
            UploadMetadata.model_validate(
                {"department": department, "doc_type": doc_type, "doc_date": doc_date}
            ),
            org_id=selected_org,
        )
    except HTTPException:
        raise
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    except Exception as error:
        raise HTTPException(422, "Unable to parse the uploaded document") from error
    response = IngestResponse(document_id=result.document.id, parse_result=result)
    if index:
        response.index_results = []
        try:
            with indexing_factory() as indexer:
                for item in document_tree(result):
                    response.index_results.append(
                        indexer.index(item.document.id, org_id=selected_org)
                    )
        except Exception:
            response.index_error = (
                "Optional indexing failed; parsed documents remain stored. Retry /v1/index."
            )
            return JSONResponse(
                status_code=503, content=response.model_dump(mode="json", exclude_none=True)
            )
    return response


@app.post("/v1/index", response_model=IndexResult)
def index_document(
    request: IndexRequest,
    indexing_factory: Annotated[IndexingFactory, Depends(get_indexing_factory)],
) -> IndexResult:
    """Index a stored parse result with batched, cache-aware embeddings."""
    try:
        with indexing_factory() as indexer:
            return indexer.index(request.doc_id, org_id=request.org_id)
    except FileNotFoundError as error:
        raise HTTPException(404, "Stored document not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(503, "Indexing failed; retry the stored document") from error


@app.get(
    "/v1/documents/{doc_id}/source",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
def document_source(
    doc_id: UUID,
    org_id: Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]+$")],
    storage: Annotated[Storage, Depends(storage_from_env)],
) -> FileResponse:
    """Serve an original PDF to core after validating its stored organization."""
    identifier = str(doc_id)
    try:
        parsed = storage.load_result(identifier)
    except (OSError, ValueError) as error:
        raise HTTPException(404, "Source not found") from error
    if parsed.org_id != org_id:
        raise HTTPException(404, "Source not found")
    if parsed.document.mime_type != "application/pdf":
        raise HTTPException(415, "This source does not have an original PDF")
    path = storage.source_path(identifier, parsed.document.filename)
    if not path.is_file():
        raise HTTPException(404, "Source not found")
    return FileResponse(
        path, media_type="application/pdf", headers={"Cache-Control": "private, no-store"}
    )


@app.delete("/v1/documents/{doc_id}", status_code=204)
def delete_demo_document(
    doc_id: UUID,
    org_id: Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]+$")],
    storage: Annotated[Storage, Depends(storage_from_env)],
) -> Response:
    """Purge an expired document selected by Core's demo retention job."""
    if not Runtime.from_env().demo:
        raise HTTPException(404, "Demo cleanup is disabled")
    identifier = str(doc_id)
    store = QdrantStore.from_env(configured_embedder())
    try:
        try:
            with storage.index_lock(identifier):
                parsed = storage.load_result(identifier)
                if parsed.org_id != org_id:
                    raise HTTPException(404, "Stored document not found in the organization")
                store.delete_document(identifier, org_id=org_id)
                storage.delete(identifier)
        except FileNotFoundError:
            # A retry after partial cleanup may only have vector data left.
            store.delete_document(identifier, org_id=org_id)
    finally:
        store.client.close()
    return Response(status_code=204)


@app.post("/v1/search", response_model=SearchResponse, response_model_exclude_none=True)
def search(
    request: SearchRequest,
    search_factory: Annotated[SearchFactory, Depends(get_search_factory)],
) -> SearchResponse:
    """Retrieve and rerank chunks with source citations."""
    try:
        with search_factory() as service:
            return service.search(request)
    except InvalidFilter as error:
        raise HTTPException(422, str(error)) from error
    except Exception as error:
        raise HTTPException(503, "Search service unavailable") from error


@app.post("/v1/answer", response_model=Answer, response_model_exclude_none=True)
def answer(
    request: AnswerRequest,
    answer_factory: Annotated[AnswerFactory, Depends(get_answer_factory)],
) -> Answer:
    """Return only source-verified claims, or an explicit abstention."""
    require_llm(Runtime.from_env())
    try:
        with answer_factory() as service:
            return service.answer(request)
    except InvalidFilter as error:
        raise HTTPException(422, str(error)) from error
    except FeatureUnavailable:
        raise
    except Exception as error:
        raise HTTPException(503, "Answer service unavailable") from error


@app.post("/v1/summarize", response_model=Summary, response_model_exclude_none=True)
def summarize(
    request: SummaryRequest,
    factory: Annotated[SummaryFactory, Depends(get_summary_factory)],
) -> Summary:
    """Summarize a stored document using the assignment snapshot supplied by Core."""
    require_llm(Runtime.from_env())
    try:
        if str(UUID(request.doc_id)) != request.doc_id:
            raise FileNotFoundError(request.doc_id)
        with factory() as service:
            return service.summarize(request, assigned_obligations=request.assigned_obligations)
    except FileNotFoundError as error:
        raise HTTPException(404, "Stored document not found") from error
    except (HTTPException, FeatureUnavailable):
        raise
    except ValueError as error:
        raise HTTPException(422, "Summary request could not be applied") from error
    except Exception as error:
        raise HTTPException(503, "Summary service unavailable") from error


@app.post("/v1/extract-obligations", response_model=ExtractionResult)
def extract_obligations(
    request: ExtractObligationsRequest,
    factory: Annotated[ExtractionFactory, Depends(get_extraction_factory)],
) -> ExtractionResult:
    """Extract and verify obligations from the document's indexed chunks."""
    require_llm(Runtime.from_env())
    try:
        with factory() as service:
            return service.extract(request.doc_id, org_id=request.org_id)
    except FileNotFoundError as error:
        raise HTTPException(404, "Stored document not found") from error
    except IndexNotReady as error:
        raise HTTPException(409, str(error)) from error
    except (HTTPException, FeatureUnavailable):
        raise
    except Exception as error:
        raise HTTPException(503, "Obligation extraction failed") from error


@app.post("/v1/detect-relations", response_model=RelationResult, response_model_exclude_none=True)
def detect_relations(
    request: DetectRelationsRequest,
    factory: Annotated[RelationFactory, Depends(get_relation_factory)],
) -> RelationResult:
    """Return evidence-checked relation proposals for an indexed document."""
    require_llm(Runtime.from_env())
    try:
        with factory() as service:
            return service.detect(request.doc_id, org_id=request.org_id)
    except FileNotFoundError as error:
        raise HTTPException(404, "Stored document not found") from error
    except IndexNotReady as error:
        raise HTTPException(409, str(error)) from error
    except (HTTPException, FeatureUnavailable):
        raise
    except Exception as error:
        raise HTTPException(503, "Relation detection failed") from error
