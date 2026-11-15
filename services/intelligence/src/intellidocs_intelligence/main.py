"""HTTP entry point for the intelligence service."""

from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile

from app.ingestion import IngestionService, load_org, storage_from_env
from app.storage import Storage

from .models import (
    ExtractObligationsRequest,
    ExtractObligationsResponse,
    IngestResponse,
    SearchRequest,
    SearchResponse,
)

app = FastAPI(title="IntelliDocs Intelligence API", version="v1")


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
    org_id: Annotated[str | None, Form(min_length=1)] = None,
) -> IngestResponse:
    """Persist a validated upload, parsed content, and attachment results."""
    try:
        result = IngestionService(storage).ingest(file, load_org(org_id))
    except HTTPException:
        raise
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    except Exception as error:
        raise HTTPException(422, "Unable to parse the uploaded document") from error
    return IngestResponse(document_id=result.document.id, parse_result=result)


@app.post("/v1/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    """Return an empty, contract-valid result set until retrieval is added."""
    del request
    return SearchResponse(results=[])


@app.post("/v1/extract-obligations", response_model=ExtractObligationsResponse)
def extract_obligations(request: ExtractObligationsRequest) -> ExtractObligationsResponse:
    """Return an empty, contract-valid obligation set until extraction is added."""
    del request
    return ExtractObligationsResponse(obligations=[])
