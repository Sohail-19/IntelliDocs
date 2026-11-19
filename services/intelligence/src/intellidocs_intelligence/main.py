"""HTTP entry point for the intelligence service."""

from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.answering import AnswerFactory, answerer_from_env
from app.extraction.service import ExtractionFactory, IndexNotReady, extraction_from_env
from app.indexing import IndexingFactory, document_tree, indexer_from_env
from app.ingestion import IngestionService, load_org, select_org_id, storage_from_env
from app.retrieval.filters import InvalidFilter
from app.search import SearchFactory, search_from_env
from app.storage import Storage

from .models import (
    Answer,
    AnswerRequest,
    ExtractionResult,
    ExtractObligationsRequest,
    IndexRequest,
    IndexResult,
    IngestResponse,
    SearchRequest,
    SearchResponse,
    UploadMetadata,
)

app = FastAPI(title="IntelliDocs Intelligence API", version="v1")


def get_indexing_factory(storage: Annotated[Storage, Depends(storage_from_env)]) -> IndexingFactory:
    # Defer configuration/model/client construction unless indexing is requested.
    return lambda: indexer_from_env(storage)


def get_search_factory() -> SearchFactory:
    return search_from_env


def get_answer_factory(storage: Annotated[Storage, Depends(storage_from_env)]) -> AnswerFactory:
    return lambda: answerer_from_env(storage)


def get_extraction_factory(
    storage: Annotated[Storage, Depends(storage_from_env)],
) -> ExtractionFactory:
    return lambda: extraction_from_env(storage)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the service liveness state."""
    return {"status": "ok"}


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
                    response.index_results.append(indexer.index(item.document.id))
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
            return indexer.index(request.doc_id)
    except FileNotFoundError as error:
        raise HTTPException(404, "Stored document not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(503, "Indexing failed; retry the stored document") from error


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
    try:
        with answer_factory() as service:
            return service.answer(request)
    except InvalidFilter as error:
        raise HTTPException(422, str(error)) from error
    except Exception as error:
        raise HTTPException(503, "Answer service unavailable") from error


@app.post("/v1/extract-obligations", response_model=ExtractionResult)
def extract_obligations(
    request: ExtractObligationsRequest,
    factory: Annotated[ExtractionFactory, Depends(get_extraction_factory)],
) -> ExtractionResult:
    """Extract and verify obligations from the document's indexed chunks."""
    try:
        with factory() as service:
            return service.extract(request.doc_id)
    except FileNotFoundError as error:
        raise HTTPException(404, "Stored document not found") from error
    except IndexNotReady as error:
        raise HTTPException(409, str(error)) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "Obligation extraction failed") from error
