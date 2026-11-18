#!/usr/bin/env python3
"""Validate IntelliDocs v1 contract schemas, examples, configuration, and API spec.

Contract YAML files use the JSON subset of YAML so this check needs only the
Python standard library; no runtime or service dependencies are required.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "contracts" / "schemas"
EXAMPLES = ROOT / "contracts" / "examples"
OPENAPI = ROOT / "contracts" / "openapi" / "intelligence.v1.yaml"
ORG_CONFIG = ROOT / "config" / "orgs" / "example.yaml"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


class ContractError(ValueError):
    """Raised when a contract artifact is invalid."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"{path.relative_to(ROOT)}: invalid JSON/YAML: {error}") from error


def resolve_ref(ref: str, base: Path, schemas: dict[Path, dict[str, Any]]) -> dict[str, Any]:
    if ref.startswith("#"):
        raise ContractError(f"{base.relative_to(ROOT)}: JSON Pointer refs are not supported by this validator")
    target = (base.parent / ref).resolve()
    if target not in schemas:
        raise ContractError(f"{base.relative_to(ROOT)}: unresolved schema ref {ref}")
    return schemas[target]


def validate_schema(schema: Any, path: Path, schemas: dict[Path, dict[str, Any]]) -> None:
    if not isinstance(schema, dict):
        raise ContractError(f"{path.relative_to(ROOT)}: schema root must be an object")
    if schema.get("$schema") != DRAFT_2020_12:
        raise ContractError(f"{path.relative_to(ROOT)}: must declare JSON Schema draft 2020-12")
    if schema.get("$id") != path.name:
        raise ContractError(f"{path.relative_to(ROOT)}: $id must equal the schema filename")

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str):
                raise ContractError(f"{path.relative_to(ROOT)}: $ref must be a string")
            resolve_ref(ref, path, schemas)
        node_type = node.get("type")
        if node_type is not None and node_type not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
            raise ContractError(f"{path.relative_to(ROOT)}: unsupported type {node_type!r}")
        for key in ("properties", "items", "additionalProperties", "if", "then"):
            value = node.get(key)
            if isinstance(value, dict):
                if key == "properties":
                    for child in value.values():
                        walk(child)
                else:
                    walk(value)
        if "required" in node and (not isinstance(node["required"], list) or not all(isinstance(x, str) for x in node["required"])):
            raise ContractError(f"{path.relative_to(ROOT)}: required must be an array of strings")

    walk(schema)


def instance_type_matches(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }
    return checks[expected]()


def validate_instance(instance: Any, schema: dict[str, Any], source: Path, schema_path: Path, schemas: dict[Path, dict[str, Any]], location: str = "$") -> None:
    if "$ref" in schema:
        target = (schema_path.parent / schema["$ref"]).resolve()
        validate_instance(instance, resolve_ref(schema["$ref"], schema_path, schemas), source, target, schemas, location)
        # Draft 2020-12 also applies keywords alongside $ref.
    if "const" in schema and instance != schema["const"]:
        raise ContractError(f"{source.relative_to(ROOT)}: {location} does not match const")
    if "if" in schema:
        try:
            validate_instance(instance, schema["if"], source, schema_path, schemas, location)
        except ContractError:
            pass
        else:
            validate_instance(instance, schema.get("then", {}), source, schema_path, schemas, location)
    expected = schema.get("type")
    if expected and not instance_type_matches(instance, expected):
        raise ContractError(f"{source.relative_to(ROOT)}: {location} must be a {expected}")
    if "enum" in schema and instance not in schema["enum"]:
        raise ContractError(f"{source.relative_to(ROOT)}: {location} is not an allowed value")
    if isinstance(instance, str):
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise ContractError(f"{source.relative_to(ROOT)}: {location} does not match pattern")
        if len(instance) < schema.get("minLength", 0):
            raise ContractError(f"{source.relative_to(ROOT)}: {location} is shorter than minLength")
        if schema.get("format") == "date":
            try:
                parsed_date = date.fromisoformat(instance)
                if parsed_date.isoformat() != instance:
                    raise ValueError("expected YYYY-MM-DD")
            except ValueError as error:
                raise ContractError(f"{source.relative_to(ROOT)}: {location} is not an ISO date") from error
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(instance.replace("Z", "+00:00"))
            except ValueError as error:
                raise ContractError(f"{source.relative_to(ROOT)}: {location} is not an ISO 8601 date-time") from error
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ContractError(f"{source.relative_to(ROOT)}: {location} is below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise ContractError(f"{source.relative_to(ROOT)}: {location} is above maximum")
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            raise ContractError(f"{source.relative_to(ROOT)}: {location} has too few items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise ContractError(f"{source.relative_to(ROOT)}: {location} has too many items")
        if "items" in schema:
            for index, item in enumerate(instance):
                validate_instance(item, schema["items"], source, schema_path, schemas, f"{location}[{index}]")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                raise ContractError(f"{source.relative_to(ROOT)}: {location}.{key} is required")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                validate_instance(value, properties[key], source, schema_path, schemas, f"{location}.{key}")
            elif additional is False:
                raise ContractError(f"{source.relative_to(ROOT)}: unexpected property {location}.{key}")
            elif isinstance(additional, dict):
                validate_instance(value, additional, source, schema_path, schemas, f"{location}.{key}")


def validate_openapi(schemas: dict[Path, dict[str, Any]]) -> None:
    spec = load_json(OPENAPI)
    if spec.get("openapi") != "3.1.0" or spec.get("info", {}).get("version") != "v1":
        raise ContractError("contracts/openapi/intelligence.v1.yaml: must be OpenAPI 3.1.0, version v1")
    expected = {"/health": "get", "/v1/ingest": "post", "/v1/index": "post", "/v1/search": "post", "/v1/answer": "post", "/v1/extract-obligations": "post"}
    if set(spec.get("paths", {})) != set(expected):
        raise ContractError("contracts/openapi/intelligence.v1.yaml: paths do not match the v1 contract")
    for endpoint, method in expected.items():
        operation = spec["paths"][endpoint].get(method)
        if not isinstance(operation, dict) or "responses" not in operation:
            raise ContractError(f"contracts/openapi/intelligence.v1.yaml: {method.upper()} {endpoint} is incomplete")
        if endpoint == "/health":
            continue
        media_type = "multipart/form-data" if endpoint == "/v1/ingest" else "application/json"
        request_schema = operation.get("requestBody", {}).get("content", {}).get(media_type, {}).get("schema", {})
        response_schemas = [
            body.get("content", {}).get("application/json", {}).get("schema", {})
            for body in operation["responses"].values()
        ]
        for schema_ref in [request_schema, *response_schemas]:
            ref = schema_ref.get("$ref") if isinstance(schema_ref, dict) else None
            if not ref:
                raise ContractError(f"contracts/openapi/intelligence.v1.yaml: {method.upper()} {endpoint} bodies must $ref schemas")
            resolve_ref(ref, OPENAPI, schemas)


def main() -> int:
    schema_paths = sorted(SCHEMAS.glob("*.v1.schema.json"))
    if not schema_paths:
        raise ContractError("no versioned schemas found")
    schemas = {path.resolve(): load_json(path) for path in schema_paths}
    for path in schema_paths:
        validate_schema(schemas[path.resolve()], path, schemas)
        example = EXAMPLES / path.name.replace(".schema.json", ".json")
        if not example.is_file():
            raise ContractError(f"missing example for {path.name}")
        validate_instance(load_json(example), schemas[path.resolve()], example, path.resolve(), schemas)
    validate_instance(load_json(ORG_CONFIG), schemas[(SCHEMAS / "org-config.v1.schema.json").resolve()], ORG_CONFIG, (SCHEMAS / "org-config.v1.schema.json").resolve(), schemas)
    validate_openapi(schemas)
    print(f"Validated {len(schema_paths)} schemas, examples, organization config, and OpenAPI v1.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"Contract validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
