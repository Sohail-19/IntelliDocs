"""OpenAI-compatible fixture server for the Compose browser test."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE = json.loads(Path(__file__).with_name("fixture.json").read_text(encoding="utf-8"))


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
            chunks = json.loads(prompt.split("\nCHUNKS_JSON\n", 1)[1])
        except (KeyError, IndexError, ValueError, TypeError):
            self.respond(422, {"error": "Expected the fixture extraction prompt"})
            return
        obligations = [
            {
                "chunk_id": chunk["chunk_id"],
                "action": FIXTURE["action"],
                "owner_text": FIXTURE["owner_text"],
                "deadline_text": FIXTURE["deadline_text"],
                "condition": None,
                "obligation_type": FIXTURE["obligation_type"],
                "quote": FIXTURE["quote"],
            }
            for chunk in chunks
            if FIXTURE["quote"] in chunk["text"]
        ]
        content = json.dumps({"obligations": obligations})
        self.respond(200, {"choices": [{"message": {"content": content}}]})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
