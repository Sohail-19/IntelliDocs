"""Config-driven candidate detection and structured obligation drafts."""

from .detector import CandidateChunk, CandidateDetector
from .extractor import (
    DraftObligation,
    ExtractionConfig,
    ExtractionError,
    ObligationExtractor,
    extractor_from_env,
)
from .processor import ObligationProcessor

__all__ = [
    "CandidateChunk",
    "CandidateDetector",
    "DraftObligation",
    "ExtractionConfig",
    "ExtractionError",
    "ObligationExtractor",
    "ObligationProcessor",
    "extractor_from_env",
]
