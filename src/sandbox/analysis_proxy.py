"""Per-execution loopback capability; upstream credentials stay in the parent process."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx


class AnalysisDataProxy:
    def __init__(self, access, timeout, max_bytes):
        self._access = access
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._stopped = threading.Event()
        self._server = None
        self._thread = None
        self.url = None

    def __enter__(self):
        if not self._access:
            return self
        capability_path = "/" + uuid4().hex
        proxy = self

        class BoundedServer(ThreadingHTTPServer):
            request_queue_size = 4
            daemon_threads = False

            def __init__(self, *args):
                self.slots = threading.BoundedSemaphore(4)
                super().__init__(*args)

            def process_request(self, request, address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, address)
                except BaseException:
                    self.slots.release()
                    raise

            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    self.slots.release()

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(min(proxy._timeout, 2))

            def do_POST(self):
                if self.path != capability_path or proxy._stopped.is_set():
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= proxy._max_bytes:
                        self.send_error(413)
                        return
                    body = self.rfile.read(size)
                    payload = json.loads(body)
                    if (
                        not isinstance(payload, dict)
                        or set(payload) != {"endpoint", "params"}
                        or not isinstance(payload["endpoint"], str)
                        or not isinstance(payload["params"], dict)
                    ):
                        self.send_error(400)
                        return
                    status, response = asyncio.run(proxy._forward(body))
                except Exception:
                    status, response = 503, proxy._unavailable()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self._server = BoundedServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}{capability_path}"
        return self

    @staticmethod
    def _unavailable():
        return json.dumps(
            {
                "error": {
                    "category": "infrastructure",
                    "code": "data_gateway_unavailable",
                    "message": "Data gateway could not be reached",
                    "retryable": True,
                }
            }
        ).encode()

    async def _forward(self, body):
        task = asyncio.create_task(self._request(body))
        try:
            while not self._stopped.is_set():
                done, _ = await asyncio.wait({task}, timeout=0.05)
                if done:
                    return task.result()
            return 503, self._unavailable()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _request(self, body):
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(min(self._timeout, 30), connect=min(self._timeout, 2)),
        ) as client:
            async with client.stream(
                "POST",
                self._access["base_url"],
                content=body,
                headers={
                    "Authorization": "Bearer " + self._access["token"],
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    if len(raw) + len(chunk) > self._max_bytes:
                        return 413, json.dumps(
                            {
                                "error": {
                                    "category": "data",
                                    "code": "data_response_too_large",
                                    "message": "Data response exceeds limit",
                                    "retryable": False,
                                }
                            }
                        ).encode()
                    raw.extend(chunk)
            # Never forward reflected authorization values, even if an upstream error echoes them.
            parsed = json.loads(raw)

            def redact(value):
                if isinstance(value, str):
                    return value.replace(self._access["token"], "[redacted]")
                if isinstance(value, list):
                    return [redact(item) for item in value]
                if isinstance(value, dict):
                    return {redact(key): redact(item) for key, item in value.items()}
                return value

            return response.status_code, json.dumps(redact(parsed), allow_nan=False).encode()

    def __exit__(self, *args):
        self._stopped.set()
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
