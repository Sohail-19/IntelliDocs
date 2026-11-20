"""Set up the disposable local pilot and seed it through Core's public API."""

import argparse
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from eval.timing.core import Core
from eval.timing.run import read_object, write

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "eval/timing/pilot_documents.json"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def init(directory: Path, mode: str) -> None:
    ports = {
        name: int(os.environ.get(f"DEMO_{name}", default))
        for name, default in {
            "CORE_PORT": "53000",
            "WEB_PORT": "55173",
            "INTELLIGENCE_PORT": "58000",
            "QDRANT_PORT": "56333",
            "POSTGRES_PORT": "55432",
        }.items()
    }
    if len(set(ports.values())) != len(ports) or any(
        not 1024 <= port <= 65535 for port in ports.values()
    ):
        raise ValueError("Use distinct pilot ports between 1024 and 65535")
    core = f"http://127.0.0.1:{ports['CORE_PORT']}"
    web = f"http://127.0.0.1:{ports['WEB_PORT']}"
    provider = os.environ.get("LLM_PROVIDER", "") if mode == "live" else "fake"
    model = os.environ.get("LLM_MODEL", "") if mode == "live" else "fixture"
    if mode == "live" and (
        provider not in {"openai_compatible", "gemini"}
        or not re.fullmatch(r"[\w./:@-]+", model)
        or not os.environ.get("LLM_API_KEY")
        or (provider == "openai_compatible" and not os.environ.get("LLM_BASE_URL"))
    ):
        raise ValueError(
            "Live mode requires a provider, model, API key, and compatible base URL if applicable"
        )
    settings = {
        **ports,
        "POSTGRES_USER": "pilot",
        "POSTGRES_DB": "pilot",
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "AUTH_JWT_SECRET": secrets.token_hex(32),
        "AUTH_BOOTSTRAP_TOKEN": secrets.token_hex(32),
        "AUTH_ACCESS_TTL_SECONDS": 3600,
        "VITE_API_URL": core,
        "CORE_CORS_ORIGINS": web,
        "TESSERACT_LANGUAGES": "eng,hin,spa,fra",
        "PARSING_EXTRAS": "tesseract,formats",
        "STORAGE_DIR": "/data/uploads",
        "ORG_CONFIG_DIR": "/config/orgs",
        "QDRANT_URL": "http://qdrant:6333",
        "LLM_PROVIDER": provider,
        "LLM_MODEL": model,
    }
    if mode == "fixture":
        settings.update(
            SPARSE_EMBEDDING_BACKEND="native",
            VERIFICATION_SUPPORT_CHECK="false",
            EXTRACTION_FUZZY_THRESHOLD="1.0",
            EXTRACTION_CONFIDENCE_WEIGHTS="",
        )
    with (directory / "compose.env").open("x", encoding="utf-8") as file:
        file.writelines(f"{name}={value}\n" for name, value in settings.items())
    organizations = {
        org: {
            role: {
                "email": f"pilot-{role}@{org}.example.test",
                "password": secrets.token_urlsafe(24),
            }
            for role in ("operator", "reader")
        }
        for org in read_object(PILOT)
    }
    write(
        directory / "access.json",
        {
            "core_url": core,
            "web_url": web,
            "organizations": organizations,
            "stack": {
                "mode": mode,
                "provider": "openai_compatible local fixture" if mode == "fixture" else provider,
                "model": "demo-mock" if mode == "fixture" else model,
                "embedder": "fake"
                if mode == "fixture"
                else os.environ.get("EMBEDDING_BACKEND", "fake"),
                "reranker": "none"
                if mode == "fixture"
                else os.environ.get("RERANKER_BACKEND", "none"),
            },
        },
    )


def docx(path: Path, paragraphs: list[str]) -> None:
    """Write a minimal Word document with one paragraph per source passage."""
    body = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in paragraphs)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" '
                'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                f'<Override PartName="/word/document.xml" ContentType="{DOCX}.main+xml"/></Types>'
            ),
        )
        archive.writestr(
            "_rels/.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Target="word/document.xml" Type="'
                'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
                "</Relationships>"
            ),
        )
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{body}</w:body></w:document>"
            ),
        )


def upload(core: Core, source: Path, attributes: dict[str, Any], token: str) -> str:
    boundary = secrets.token_hex(16)
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n'
            f"\r\n{attributes[key]}\r\n"
        ).encode()
        for key in ("department", "doc_type", "doc_date")
    ]
    parts += [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{source.name}"\r\n'
            f"Content-Type: {DOCX}\r\n\r\n"
        ).encode(),
        source.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    result = core.request(
        "/v1/upload",
        data=b"".join(parts),
        token=token,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    if result.status != 202:
        raise ValueError(f"Pilot upload failed for {source.name} ({result.status or result.error})")
    raw = result.json()
    if not raw.get("index_results") or not raw.get("document_id"):
        raise ValueError(f"Pilot document was not indexed: {source.name}")
    return str(raw["document_id"])


def seed(directory: Path) -> None:
    access = read_object(directory / "access.json")
    core = Core(access["core_url"], 240)
    bootstrap = os.environ["AUTH_BOOTSTRAP_TOKEN"]
    document_ids: dict[str, dict[str, str]] = {}
    for org, documents in read_object(PILOT).items():
        users = access["organizations"][org]
        operator, reader = users["operator"], users["reader"]
        result = core.post(
            "/api/auth/register",
            {
                "org_id": org,
                "name": "Pilot Operator",
                "department": documents[0]["department"],
                "bootstrap_token": bootstrap,
                **operator,
            },
        )
        if result.status != 201:
            raise ValueError(
                f"Pilot operator registration failed for {org} ({result.status or result.error})"
            )
        token = core.login(org, operator["email"], operator["password"])
        result = core.post(
            "/api/auth/register", {"org_id": org, "name": "Pilot Reader", **reader}, token
        )
        if result.status != 201:
            raise ValueError(
                f"Pilot reader registration failed for {org} ({result.status or result.error})"
            )
        output = directory / "documents" / org
        output.mkdir(parents=True, exist_ok=True)
        document_ids[org] = {}
        for document in documents:
            source = output / document["filename"]
            docx(source, document["paragraphs"])
            document_ids[org][source.name] = upload(core, source, document, token)
    write(directory / "documents.json", document_ids)
    print(f"Web login {access['web_url']}/login\nCore API {access['core_url']}")
    print(f"Source documents {directory / 'documents'}")
    print(f"Timing credentials {directory / 'access.json'}")
    print(f"Stack mode {access['stack']['mode']}")
    for org, users in access["organizations"].items():
        for role, user in users.items():
            print(f"Organization {org} | {role} | {user['email']} | password {user['password']}")


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("phase", choices=("init", "seed"))
    cli.add_argument("run", type=Path)
    cli.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    args = cli.parse_args()
    os.umask(0o077)
    try:
        if args.phase == "init":
            init(args.run, args.mode)
        else:
            seed(args.run)
    except (OSError, TypeError, KeyError, ValueError) as error:
        print(f"Pilot setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
