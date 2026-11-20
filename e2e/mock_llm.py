"""OpenAI-compatible fixture server for the Compose browser test."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE = json.loads(Path(__file__).with_name("fixture.json").read_text(encoding="utf-8"))
ONBOARDING = json.loads(Path(__file__).with_name("onboarding-fixtures.json").read_text("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.respond(200, {"status": "ok"}) if self.path == "/health" else self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = request["messages"][0]["content"]
            if "\nPAIR_JSON\n" in prompt:
                result = relation(json.loads(prompt.split("\nPAIR_JSON\n", 1)[1]))
            elif "\nREQUEST_JSON\n" in prompt:
                context = json.loads(prompt.split("\nREQUEST_JSON\n", 1)[1])
                result = answer(context) if "query" in context else {"points": []}
            else:
                chunks = json.loads(prompt.split("\nCHUNKS_JSON\n", 1)[1])
                result = {"obligations": obligations(chunks)}
        except (KeyError, IndexError, ValueError, TypeError):
            self.respond(422, {"error": "Unrecognized fixture prompt"})
            return
        self.respond(200, {"choices": [{"message": {"content": json.dumps(result)}}]})


def obligations(chunks: list[dict]) -> list[dict]:
    return [
        {
            "chunk_id": chunk["chunk_id"],
            "action": fixture["action"],
            "owner_text": fixture.get("owner_text", fixture.get("owner")),
            "deadline_text": fixture.get("deadline_text", fixture.get("deadline")),
            "condition": None,
            "obligation_type": fixture.get("obligation_type", "reporting"),
            "quote": fixture["quote"],
        }
        for chunk in chunks
        for fixture in (FIXTURE, FIXTURE["original"], ONBOARDING["example2"])
        if fixture["quote"] in chunk["text"]
    ]


def answer(context: dict) -> dict:
    """Serve an extractive demo answer without reading timing questions or gold answers."""

    def terms(text: str) -> set[str]:
        return {token for token in re.findall(r"[^\W_]+", text.casefold()) if len(token) >= 4}

    query = terms(context["query"])
    best_hits = 0
    claim = None
    for chunk in context["chunks"]:
        for line in chunk["text"].splitlines():
            quote = line.strip()
            hits = len(query & terms(quote))
            if hits > best_hits:
                best_hits = hits
                claim = {"chunk_id": chunk["chunk_id"], "text": quote, "quote": quote}
    return {"claims": [claim] if claim else []}


def relation(pair: dict) -> dict:
    new, old = FIXTURE["amendment"]["quote"], FIXTURE["original"]["quote"]
    if new in pair["NEW"]["text"] and old in pair["OLD"]["text"]:
        return {
            "relation_type": "amends",
            "scope": "clauses",
            "source_quote": new,
            "target_quote": old,
        }
    return {
        "relation_type": None,
        "scope": None,
        "source_quote": "",
        "target_quote": "",
    }


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
