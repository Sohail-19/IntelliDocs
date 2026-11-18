"""Quote attribution, optional support checks, and verified answer assembly."""

from .config import VerificationConfig
from .verifier import VERIFIER_VERSION, CitationVerifier, verifier_from_env

__all__ = ["VERIFIER_VERSION", "CitationVerifier", "VerificationConfig", "verifier_from_env"]
