import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


state = {"count": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        print(f"GET {self.path}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        print(f"POST {self.path}", flush=True)
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        try:
            json.loads(body or b"{}")
        except Exception:
            pass
        state["count"] += 1
        if state["count"] == 1:
            arguments = json.dumps(
                {"report": "probe report from OpenCode"},
                ensure_ascii=False,
            )
            chunks = [
                {
                    "id": "probe",
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [{
                                "index": 0,
                                "id": "call_report",
                                "type": "function",
                                "function": {
                                    "name": "report_task",
                                    "arguments": arguments,
                                },
                            }],
                        },
                        "finish_reason": None,
                    }],
                },
                {
                    "id": "probe",
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }],
                },
            ]
        else:
            chunks = [
                {
                    "id": "probe",
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "probe complete"},
                        "finish_reason": None,
                    }],
                },
                {
                    "id": "probe",
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }],
                },
            ]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


HTTPServer(("127.0.0.1", int(os.environ.get("MUTSUMI_PROBE_PORT", "18080"))), Handler).serve_forever()
