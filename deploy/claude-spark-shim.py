#!/usr/bin/env python3
"""Anthropic-API shim: hoist in-array `system` messages to the top-level field.

Claude Code sends a message with role="system" INSIDE the messages array. vLLM's
/v1/messages validator follows the Anthropic spec, where system is a top-level
field and messages may only contain user/assistant:

    messages[1].role='system' -> "Input should be 'user' or 'assistant'"  (400)

This is the same class of failure as the GGUF Jinja "System message must be at
the beginning" bug in ../claude-local, but it happens at request validation, so
no chat template can fix it. This shim rewrites the request instead: every
system-role message is lifted out of `messages`, converted to text blocks, and
appended to the top-level `system` array, preserving order.

Everything else — tools, streaming SSE, other routes — is proxied untouched.

    ./claude-spark-shim.py [--listen 127.0.0.1:8016] [--upstream http://gx10:8006]
"""
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

UPSTREAM = "http://gx10:8006"
DEBUG = os.environ.get("SHIM_DEBUG") == "1"
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer",
       "proxy-authorization", "proxy-authenticate", "upgrade", "content-length",
       "content-encoding", "host"}


def to_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return []


def hoist_system(payload):
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return payload
    hoisted, kept = [], []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            hoisted.extend(to_blocks(m.get("content")))
        else:
            kept.append(m)
    if not hoisted:
        return payload
    existing = payload.get("system")
    base = to_blocks(existing) if existing is not None else []
    payload["system"] = base + hoisted
    # The API requires at least one message; if the caller sent only system
    # turns, leave a minimal user turn behind rather than 400 on an empty array.
    payload["messages"] = kept or [{"role": "user", "content": "."}]
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        if DEBUG:
            sys.stderr.write("  path=%s\n" % self.path)
            sys.stderr.flush()

    def _relay(self, method):
        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)

        # Claude Code appends a query string (e.g. /v1/messages?beta=true), so
        # match on the path component only.
        route = self.path.split("?", 1)[0].rstrip("/")
        if route.endswith("/v1/messages") and body:
            try:
                payload = hoist_system(json.loads(body))
                body = json.dumps(payload).encode()
                if DEBUG:
                    sys.stderr.write("  hoisted system -> %d blocks\n"
                                     % len(payload.get("system") or []))
                    sys.stderr.flush()
            except (ValueError, TypeError):
                pass  # not JSON we understand; pass through untouched

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        try:
            up = requests.request(method, UPSTREAM + self.path, data=body,
                                  headers=headers, stream=True, timeout=3600)
        except requests.RequestException as e:
            msg = json.dumps({"type": "error",
                              "error": {"type": "api_error", "message": str(e)}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(up.status_code)
        for k, v in up.headers.items():
            if k.lower() not in HOP:
                self.send_header(k, v)
        # Close-delimited framing keeps SSE streaming simple and correct.
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for chunk in up.iter_content(chunk_size=8192):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_POST(self):
        self._relay("POST")

    def do_GET(self):
        self._relay("GET")


def main():
    global UPSTREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:8016")
    ap.add_argument("--upstream", default=UPSTREAM)
    a = ap.parse_args()
    UPSTREAM = a.upstream.rstrip("/")
    host, _, port = a.listen.rpartition(":")
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    print(f"shim listening on {a.listen} -> {UPSTREAM}", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
