"""HTTP entry point for the intelligence service."""

from fastapi import FastAPI

from .models import (
    ExtractObligationsRequest,
    ExtractObligationsResponse,
    IngestRequest,
    IngestResponse,
    SearchRequest,
    SearchResponse,
)

app = FastAPI(title="IntelliDocs Intelligence API", version="v1")


@app.get("/health")
def health() -> dict[str, str]:
    """Return the service liveness state."""
    return {"status": "ok"}


@app.post("/v1/ingest", response_model=IngestResponse, status_code=202)
def ingest(request: IngestRequest) -> IngestResponse:
    """Accept a file reference; processing will be implemented later."""
    del request
    return IngestResponse(document_id="pending-document")


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
