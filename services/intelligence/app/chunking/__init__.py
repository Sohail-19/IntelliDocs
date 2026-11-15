"""Deterministic structural chunking and canonical citation offsets."""

from .canonical import CanonicalDocument, canonical_document
from .chunker import Chunker
from .config import ChunkingConfig

__all__ = ["CanonicalDocument", "Chunker", "ChunkingConfig", "canonical_document"]
