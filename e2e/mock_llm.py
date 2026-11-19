"""OpenAI-compatible fixture server for the Compose browser test."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE = json.loads(
    Path(__file__).with_name("fixture.json").read_text(encoding="utf-8")
)


class Handler(BaseHTTPRequestHandler):
    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.respond(
            200, {"status": "ok"}
        ) if self.path == "/health" else self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = request["messages"][0]["content"]
            if "\nPAIR_JSON\n" in prompt:
                result = relation(json.loads(prompt.split("\nPAIR_JSON\n", 1)[1]))
            else:
                chunks = json.loads(prompt.split("\nCHUNKS_JSON\n", 1)[1])
                result = {"obligations": obligations(chunks)}
        except (KeyError, IndexError, ValueError, TypeError):
            self.respond(
                422, {"error": "Expected a fixture extraction or relation prompt"}
            )
            return
        self.respond(200, {"choices": [{"message": {"content": json.dumps(result)}}]})


def obligations(chunks: list[dict]) -> list[dict]:
    return [
        {
            "chunk_id": chunk["chunk_id"],
            "action": fixture["action"],
            "owner_text": fixture["owner_text"],
            "deadline_text": fixture["deadline_text"],
            "condition": None,
            "obligation_type": fixture["obligation_type"],
            "quote": fixture["quote"],
        }
        for chunk in chunks
        for fixture in (FIXTURE, FIXTURE["original"])
        if fixture["quote"] in chunk["text"]
    ]


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
