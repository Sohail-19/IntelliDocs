"""Conservative citation verification settings."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationConfig:
    fuzzy_threshold: float = 1.0
    support_check: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.fuzzy_threshold) or not 0 < self.fuzzy_threshold <= 1:
            raise ValueError("VERIFICATION_FUZZY_THRESHOLD must be in (0, 1]")
        if type(self.support_check) is not bool:
            raise ValueError("VERIFICATION_SUPPORT_CHECK must be a boolean")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "VerificationConfig":
        values = os.environ if env is None else env
        enabled = values.get("VERIFICATION_SUPPORT_CHECK", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("VERIFICATION_SUPPORT_CHECK must be true or false")
        return cls(
            fuzzy_threshold=float(values.get("VERIFICATION_FUZZY_THRESHOLD", "1.0")),
            support_check=enabled == "true",
        )
