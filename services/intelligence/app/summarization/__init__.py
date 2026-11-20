"""Grounded summaries scoped to a configured audience and one document."""

from .service import SummaryService, summarizer_from_env

__all__ = ["SummaryService", "summarizer_from_env"]
