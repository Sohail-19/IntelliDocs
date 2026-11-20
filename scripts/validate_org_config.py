#!/usr/bin/env python3
"""Validate organization YAML against the shared v1 schema and its named settings."""

import argparse
import json
import re
from pathlib import Path

try:
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
except ImportError:
    raise SystemExit(
        "Install the Intelligence development dependencies (PyYAML and jsonschema)."
    ) from None

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


def references(config):
    """Check references between fields after the schema has checked their shapes."""
    departments = set(config["departments"])
    roles = config.get("roles", {})
    languages = {code.casefold() for code in config["languages"]}
    doc_types = set(config["document_types"])

    def unknown(path, names, choices):
        for name in sorted(set(names) - set(choices)):
            yield f"{path}: {name!r} is not declared in this organization."

    for name in ("languages", "departments", "document_types"):
        values = config[name]
        if len(values) != len(set(values)):
            yield f"{name}: remove duplicate entries."
    if config["ocr_engine"] not in {"docling", "tesseract"}:
        yield "ocr_engine: choose docling or tesseract."
    for index, pattern in enumerate(
        config.get("document_structure", {}).get("page_number_patterns", []), 1
    ):
        try:
            re.compile(pattern)
        except re.error as error:
            path = f"document_structure.page_number_patterns entry {index}"
            yield f"{path}: invalid regular expression ({error})."
    yield from unknown("department_aliases", config.get("department_aliases", {}), departments)
    for role, settings in roles.items():
        if "department" in settings:
            yield from unknown(f"roles.{role}.department", [settings["department"]], departments)
    defaults = config.get("upload_defaults", {})
    for field, allowed in (("department", departments), ("doc_type", doc_types)):
        if field in defaults:
            yield from unknown(f"upload_defaults.{field}", [defaults[field]], allowed)
    if access := config.get("access"):
        grants = access["role_permissions"]
        yield from unknown("access.role_permissions", grants, roles)
        bootstrap, registration = access["bootstrap_role"], access["registration_role"]
        yield from unknown("access.bootstrap_role", [bootstrap], roles)
        yield from unknown("access.registration_role", [registration], roles)
        if "users:register" not in grants.get(bootstrap, []):
            yield "access.bootstrap_role: grant users:register to the bootstrap role."
        if "users:register" in grants.get(registration, []):
            yield "access.registration_role: choose a role without users:register."
    if summary := config.get("summary"):
        for section, allowed in (("department_profiles", departments), ("role_profiles", roles)):
            profiles = summary.get(section, {})
            yield from unknown(f"summary.{section}", profiles, allowed)
            for audience, profile in profiles.items():
                yield from unknown(
                    f"summary.{section}.{audience}.glossary",
                    (code.casefold() for code in profile["glossary"]),
                    languages,
                )
        yield from unknown(
            "summary.not_relevant_reasons",
            (code.casefold() for code in summary.get("not_relevant_reasons", {})),
            languages,
        )
    detection = config.get("obligation_detection", {})
    yield from unknown("obligation_detection", (code.casefold() for code in detection), languages)
    for code, rules in detection.items():
        for field, groups in (
            ("date_patterns", None),
            ("relative_deadline_patterns", {"days", "weeks", "months", "years", "unsupported"}),
            ("absolute_deadline_wrappers", {"date"}),
        ):
            for index, pattern in enumerate(rules.get(field, []), 1):
                path = f"obligation_detection.{code}.{field} entry {index}"
                try:
                    compiled = re.compile(pattern, re.IGNORECASE)
                except re.error as error:
                    yield f"{path}: invalid regular expression ({error})."
                    continue
                if groups is None and compiled.search(""):
                    yield f"{path}: a date pattern must match some text."
                if groups is not None and (
                    len(compiled.groupindex) != 1 or not compiled.groupindex.keys() <= groups
                ):
                    yield f"{path}: use exactly one named capture from {', '.join(sorted(groups))}."
    relations = config.get("relation_detection", {})
    for field in ("reference_cues", "change_cues", "nonoperative_cues"):
        yield from unknown(
            f"relation_detection.{field}",
            (code.casefold() for code in relations.get(field, {})),
            languages,
        )
    related = relations.get("related_document_types", {})
    yield from unknown("relation_detection.related_document_types", related, doc_types)
    for doc_type, targets in related.items():
        yield from unknown(
            f"relation_detection.related_document_types.{doc_type}", targets, doc_types
        )

    rules = config["routing_rules"]
    policy = config.get("routing_policy")
    structured = [rule for rule in rules if "id" in rule]
    if structured and not policy:
        yield "routing_policy: required with structured routing rules."
    if not policy:
        return
    yield from unknown("routing_policy.review_role", [policy["review_role"]], roles)
    if len(structured) != len(rules):
        yield "routing_rules: use only structured rules with a routing policy."
    defaults = [rule for rule in structured if not rule["conditions"]]
    if len(defaults) != 1 or any(
        rule["conditions"] and rule["priority"] <= defaults[0]["priority"] for rule in structured
    ):
        yield "routing_rules: use exactly one unconditional rule with the lowest priority."
    names = set()
    for rule in structured:
        path = f"routing_rules.{rule['id']}"
        if rule["id"] in names:
            yield f"{path}: duplicate rule ID."
        names.add(rule["id"])
        conditions = rule["conditions"]
        for field, allowed in (("owner_department", departments), ("document_type", doc_types)):
            if field in conditions:
                yield from unknown(f"{path}.conditions.{field}", [conditions[field]], allowed)
        target = rule["target"]
        if target["kind"] in {"role_queue", "department_queue"}:
            allowed = roles if target["kind"] == "role_queue" else departments
            yield from unknown(f"{path}.target.value", [target["value"]], allowed)
        elif len(target["value"]) > 320 or not re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+", target["value"]
        ):
            yield f"{path}.target.value: specify the assignee's email address."


def validate(config):
    schemas = [
        json.loads(path.read_text(encoding="utf-8")) for path in SCHEMAS.glob("*.v1.schema.json")
    ]
    registry = Registry().with_resources(
        (item["$id"], Resource.from_contents(item)) for item in schemas
    )
    schema = next(item for item in schemas if item["$id"] == "org-config.v1.schema.json")
    validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(config), key=lambda error: error.json_path)
    if errors:
        return [f"{error.json_path}: {error.message}" for error in errors]
    return list(references(config))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args()
    failed = False
    for path in args.paths:
        try:
            problems = validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as error:
            problems = [str(error)]
        if problems:
            failed = True
            print(f"{path} has configuration problems")
            for problem in problems:
                print(f"- {problem}")
        else:
            print(f"{path} is valid")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
