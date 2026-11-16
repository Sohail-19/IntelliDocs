"""Translate the search contract's string filters to Qdrant payload conditions."""

from collections.abc import Mapping
from datetime import UTC, date, datetime, time

from qdrant_client import models

KEYWORD_FIELDS = {"department", "doc_type", "language", "doc_id"}
DATE_FIELDS = {"doc_date", "doc_date_from", "doc_date_to"}


class InvalidFilter(ValueError):
    """The request's filters cannot be applied."""


def _parse_date(value: str, field: str) -> date:
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return day
    except ValueError:
        pass
    raise InvalidFilter(f"{field} must be a calendar date in YYYY-MM-DD format")


def payload_filter(filters: Mapping[str, str] | None) -> models.Filter | None:
    if not filters:
        return None
    values = dict(filters)
    if "document_type" in values:
        # The v1 search example uses document_type. The Chunk uses doc_type.
        alias = values.pop("document_type")
        if values.setdefault("doc_type", alias) != alias:
            raise InvalidFilter("doc_type and document_type filters disagree")
    conditions: list[models.Condition] = []
    for field, value in sorted(values.items()):
        if field not in KEYWORD_FIELDS | DATE_FIELDS:
            raise InvalidFilter(f"Unsupported retrieval filter: {field}")
        if not isinstance(value, str) or not value.strip():
            raise InvalidFilter(f"{field} must be a nonempty string")
        if field in KEYWORD_FIELDS:
            conditions.append(
                models.FieldCondition(key=field, match=models.MatchValue(value=value))
            )

    dates = {key: _parse_date(values[key], key) for key in DATE_FIELDS if key in values}
    lower, upper = dates.get("doc_date_from"), dates.get("doc_date_to")
    if lower is not None and upper is not None and lower > upper:
        raise InvalidFilter("doc_date_from must be on or before doc_date_to")
    if day := dates.get("doc_date"):
        lower = max(lower, day) if lower is not None else day
        upper = min(upper, day) if upper is not None else day
        if lower > upper:
            raise InvalidFilter("doc_date falls outside the requested date range")
    if lower is not None or upper is not None:
        conditions.append(
            models.FieldCondition(
                key="doc_date",
                range=models.DatetimeRange(
                    gte=datetime.combine(lower, time.min, UTC) if lower else None,
                    lte=datetime.combine(upper, time.max, UTC) if upper else None,
                ),
            )
        )
    return models.Filter(must=conditions)
