"""Resolve sourced owner and deadline wording using organization settings."""

import re
import unicodedata
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import dateparser  # type: ignore[import-untyped]

from intellidocs_intelligence.models import ObligationDeadline, ObligationOwner, OrgConfig

OwnerTarget = tuple[str | None, str | None]
RELATIVE_UNITS = {"days", "weeks", "months", "years", "unsupported"}


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text.casefold()).split())


def owner_key(text: str) -> str:
    return " ".join(
        "".join(
            " " if unicodedata.category(char).startswith("P") else char
            for char in normalize_text(text)
        ).split()
    )


class OwnerResolver:
    def __init__(self, org: OrgConfig) -> None:
        self.targets: dict[str, set[OwnerTarget]] = defaultdict(set)
        if not org.department_aliases.keys() <= set(org.departments):
            raise ValueError("department_aliases must reference configured departments")
        for department in org.departments:
            for name in (department, *org.department_aliases.get(department, [])):
                self.targets[owner_key(name)].add((department, None))
        for role, settings in org.roles.items():
            if settings.department is not None and settings.department not in org.departments:
                raise ValueError(f"Role {role!r} references an unknown department")
            for name in (role, *settings.aliases):
                self.targets[owner_key(name)].add((settings.department, role))

    def resolve(self, raw_text: str, *, sourced: bool) -> ObligationOwner:
        key = owner_key(raw_text)
        matches = self.targets.get(key, set()) if sourced and key else set()
        department, role = next(iter(matches)) if len(matches) == 1 else (None, None)
        return ObligationOwner(
            raw_text=raw_text,
            resolved_department=department,
            resolved_role=role,
            resolved=len(matches) == 1,
        )


def _advance(anchor: date, unit: str, amount: int) -> date | None:
    if unit == "unsupported":
        return None
    if unit in {"days", "weeks"}:
        return anchor + timedelta(days=amount * (7 if unit == "weeks" else 1))
    months = amount * (12 if unit == "years" else 1)
    year, month = divmod(anchor.year * 12 + anchor.month - 1 + months, 12)
    month += 1
    return date(year, month, min(anchor.day, monthrange(year, month)[1]))


def _compile(pattern: str, groups: set[str]) -> re.Pattern[str]:
    try:
        compiled = re.compile(unicodedata.normalize("NFKC", pattern), re.IGNORECASE)
    except re.error as error:
        raise ValueError(f"Invalid deadline pattern: {pattern!r}") from error
    if len(compiled.groupindex) != 1 or not compiled.groupindex.keys() <= groups:
        raise ValueError(
            f"Deadline pattern must capture exactly one named group from {sorted(groups)}"
        )
    return compiled


class DeadlineNormalizer:
    def __init__(self, org: OrgConfig) -> None:
        configured = {language.casefold() for language in org.languages}
        self.languages = sorted({language.split("-", 1)[0] for language in configured})
        self.relative_patterns = [
            _compile(pattern, RELATIVE_UNITS)
            for language, rules in org.obligation_detection.items()
            if language.casefold() in configured
            for pattern in rules.relative_deadline_patterns
        ]
        self.wrappers = [
            _compile(pattern, {"date"})
            for language, rules in org.obligation_detection.items()
            if language.casefold() in configured
            for pattern in rules.absolute_deadline_wrappers
        ]

    def _absolute(self, text: str) -> date | None:
        forms: set[str] = set()
        for pattern in self.wrappers:
            if (match := pattern.fullmatch(text)) and (inner := match.group("date")):
                forms.add(inner)
        dates: set[date] = set()
        for form in forms or {text}:
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", form):
                # Do not let a locale parser reinterpret an invalid ISO date.
                try:
                    dates.add(date.fromisoformat(form))
                except ValueError:
                    pass
                continue
            if not re.search(r"(?<!\d)[0-9]{4}(?!\d)", form):
                continue
            for language in self.languages:
                for order in ("DMY", "MDY"):
                    settings: dict[str, Any] = {
                        "STRICT_PARSING": True,
                        "PARSERS": ["absolute-time"],
                        "PREFER_LOCALE_DATE_ORDER": False,
                        "DATE_ORDER": order,
                    }
                    try:
                        parsed = dateparser.parse(form, languages=[language], settings=settings)
                    except ValueError:
                        # An org may include languages absent from dateparser's locale data.
                        continue
                    if parsed is not None:
                        dates.add(parsed.date())
        return next(iter(dates)) if len(dates) == 1 else None

    def resolve(self, raw_text: str, anchor: date | None, *, sourced: bool) -> ObligationDeadline:
        text = normalize_text(raw_text).rstrip(".।")
        # dateparser and ISO parsing should see the same digits in every script.
        text = "".join(
            str(unicodedata.decimal(char)) if char.isdecimal() else char for char in text
        )
        relative = {
            (unit, value)
            for pattern in self.relative_patterns
            if (match := pattern.fullmatch(text))
            for unit, value in match.groupdict().items()
            if value is not None
        }
        normalized = None
        if sourced and relative and anchor is not None and len(relative) == 1:
            unit, amount = next(iter(relative))
            try:
                normalized = _advance(anchor, unit, int(amount))
            except (ValueError, OverflowError):
                pass
        elif sourced and not relative:
            normalized = self._absolute(text)
        return ObligationDeadline(
            raw_text=raw_text,
            normalized_date=normalized,
            is_relative=bool(relative),
            anchor_date=anchor if relative and sourced else None,
            resolved=normalized is not None,
        )
