import asyncio
import hashlib
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sandbox.config import Settings
from sandbox.main import create_app

HEADERS = {"X-API-Key": "test-key"}


def client_for(tmp_path, **overrides):
    values = dict(
        upload_dir=tmp_path / "uploads",
        data_dir=tmp_path / "data",
        data_upload_dir=tmp_path / "data-files",
        api_key="test-key",
        max_timeout_seconds=5,
        analysis_result_dir=tmp_path / "results",
    )
    values.update(overrides)
    return TestClient(create_app(Settings(**values)))


def body(
    code="def analyze(context):\n    print('log only')\n    return context.data.read('prices')",
    **kw,
):
    return dict(execution_id=str(uuid4()), code=code, inputs={"prices": [1, 2]}, **kw)


def test_actual_result_survives_cleanup_and_same_id_reuses(tmp_path):
    request = body()
    with client_for(tmp_path) as client:
        response = client.post("/analysis/execute", json=request, headers=HEADERS)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "success"
        assert result["result"] == [1, 2]
        assert result["stdout"].strip() == "log only"
        assert (
            client.get(f"/analysis/executions/{request['execution_id']}", headers=HEADERS).json()
            == result
        )
        assert client.post("/analysis/execute", json=request, headers=HEADERS).json() == result
        request["inputs"] = {}
        assert client.post("/analysis/execute", json=request, headers=HEADERS).status_code == 409
    with client_for(tmp_path) as client:
        assert (
            client.get(f"/analysis/executions/{request['execution_id']}", headers=HEADERS).json()
            == result
        )


def test_large_result_download_auth_release(tmp_path):
    request = body("def analyze(context):\n    return {'data': 'x' * 2100000}")
    with client_for(tmp_path) as client:
        result = client.post("/analysis/execute", json=request, headers=HEADERS).json()
        assert result["status"] == "success"
        assert "result" not in result
        artifact = result["artifact"]
        url = f"/analysis/results/{artifact['artifact_id']}"
        assert client.get(url).status_code == 401
        download = client.get(url, headers=HEADERS)
        assert download.status_code == 200
        assert len(download.content) == artifact["size_bytes"]
        assert hashlib.sha256(download.content).hexdigest() == artifact["sha256"]
        assert len(download.json()["data"]) == 2100000
        assert client.delete(url, headers=HEADERS).status_code == 204
        assert client.get(url, headers=HEADERS).status_code == 404


@pytest.mark.parametrize(
    "code,category,status",
    [
        ("def analyze(context):\n    return 1 / 0", "code", "error"),
        ("def analyze(context):\n    return float('nan')", "code", "error"),
        ("def analyze(context):\n    while True: pass", "infrastructure", "timeout"),
        ("def analyze(context):\n    return context.data.read('missing')", "data", "error"),
    ],
)
def test_execution_failures(tmp_path, code, category, status):
    with client_for(tmp_path) as client:
        result = client.post(
            "/analysis/execute", json=body(code, timeout=0.5), headers=HEADERS
        ).json()
        assert result["status"] == status
        assert result["error"]["category"] == category


def test_untrusted_callback_rejected_without_token_echo(tmp_path):
    with client_for(tmp_path) as client:
        result = client.post(
            "/analysis/execute",
            json=body(
                data_access={
                    "base_url": "http://169.254.169.254/private",
                    "token": "sensitive-token",
                }
            ),
            headers=HEADERS,
        )
        assert result.status_code == 400
        assert "sensitive-token" not in result.text


def test_paths_and_symlinks_are_rejected(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/analysis/results/not-a-uuid", headers=HEADERS).status_code == 422
        execution_id = str(uuid4())
        root = tmp_path / "results"
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"secret": True}))
        try:
            (root / f"{execution_id}.result.json").symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable")
        assert client.get(f"/analysis/results/{execution_id}", headers=HEADERS).status_code == 404


@pytest.mark.parametrize(
    "status,payload,category",
    [
        (200, {"data": {"rows": [1, 2, 3]}}, None),
        (
            403,
            {"error": {"category": "permission", "code": "denied", "message": "denied"}},
            "permission",
        ),
        (404, {"error": {"category": "data", "code": "missing", "message": "missing"}}, "data"),
        (
            503,
            {
                "error": {
                    "category": "infrastructure",
                    "code": "busy",
                    "message": "busy",
                    "retryable": True,
                }
            },
            "infrastructure",
        ),
    ],
)
def test_real_data_callback(tmp_path, status, payload, category):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (
                    self.headers.get("Authorization"),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1/internal/analysis-data/query"
        try:
            with client_for(tmp_path, analysis_callback_urls=(url,)) as client:
                result = client.post(
                    "/analysis/execute",
                    headers=HEADERS,
                    json=body(
                        "def analyze(context):\n"
                        "    return context.data.query('news', {'limit': 3})",
                        data_access={"base_url": url, "token": "secret-token"},
                    ),
                ).json()
            if category:
                assert result["error"]["category"] == category
            else:
                assert result["result"] == payload["data"]
            assert requests == [
                ("Bearer secret-token", {"endpoint": "news", "params": {"limit": 3}})
            ]
            assert "secret-token" not in json.dumps(result)
        finally:
            server.shutdown()
            thread.join()


def test_invalid_body_does_not_echo_token(tmp_path):
    with client_for(tmp_path) as client:
        request = body(
            data_access={
                "base_url": "http://localhost/callback",
                "token": "secret-token",
                "extra": 1,
            }
        )
        response = client.post("/analysis/execute", json=request, headers=HEADERS)
        assert response.status_code == 422
        assert "secret-token" not in response.text
        request["timeout"] = "wrong secret-token"
        response = client.post("/analysis/execute", json=request, headers=HEADERS)
        assert response.status_code == 422
        assert "secret-token" not in response.text


def test_expiry_removes_inline_artifact_and_receipt(tmp_path):
    with client_for(tmp_path) as client:
        result = client.post("/analysis/execute", json=body(), headers=HEADERS).json()
        store = client.app.state.analysis.store
        receipt = store.load(result["execution_id"])
        receipt["expires"] = time.time() - 1
        store.save(result["execution_id"], receipt)
        assert store.cleanup_expired() == 1
        assert not list((tmp_path / "results").glob("*.json"))


def test_science_libraries_and_bounded_output(tmp_path):
    with client_for(tmp_path, max_timeout_seconds=15, max_output_bytes=100) as client:
        result = client.post(
            "/analysis/execute",
            json=body(
                "import pandas as pd\ndef analyze(context):\n    print('x' * 1000)\n"
                "    return {'total': int(pd.Series(context.data.read('prices')).sum())}"
            ),
            headers=HEADERS,
        ).json()
        assert result["status"] == "success", result
        assert result["result"] == {"total": 3}
        assert "[output truncated]" in result["stdout"]


@pytest.mark.parametrize(
    "code",
    [
        "def analyze(context):\n    return open('request.json').read()",
        "import os\ndef analyze(context):\n    return os.environ",
        "def analyze(context):\n    return context.data.query.__globals__",
    ],
)
def test_user_code_cannot_read_runner_secrets(tmp_path, code):
    with client_for(tmp_path) as client:
        result = client.post("/analysis/execute", json=body(code), headers=HEADERS).json()
        assert result["status"] == "error"
        assert result["error"]["category"] == "code"


async def test_executor_cancellation_waits_for_child_cleanup(tmp_path):
    from sandbox.analysis import AnalysisExecutor

    executor = AnalysisExecutor(Settings(analysis_result_dir=tmp_path))
    request = body("def analyze(context):\n    while True: pass", timeout=10, parameters={})
    job = asyncio.create_task(executor.run_analysis(request))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert time.monotonic() - started < 3


def test_restart_marks_unfinished_execution_interrupted(tmp_path):
    request = body()
    with client_for(tmp_path) as client:
        service = client.app.state.analysis
        normalized, fingerprint = service.validate(
            __import__("sandbox.analysis", fromlist=["AnalysisRequest"]).AnalysisRequest(**request)
        )
        service.store.save(
            request["execution_id"],
            {
                "fingerprint": fingerprint,
                "expires": time.time() + 100,
                "response": {
                    "execution_id": request["execution_id"],
                    "status": "running",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                },
            },
            create=True,
        )
    with client_for(tmp_path) as client:
        result = client.post("/analysis/execute", json=request, headers=HEADERS).json()
        assert result["status"] == "interrupted"
        assert result["error"]["category"] == "infrastructure"


def test_result_limit_and_storage_limit(tmp_path):
    with client_for(tmp_path, analysis_max_result_bytes=500) as client:
        result = client.post(
            "/analysis/execute",
            json=body("def analyze(context):\n    return 'x' * 1000"),
            headers=HEADERS,
        ).json()
        assert result["status"] == "error"
        assert result["error"]["category"] == "resource"
        assert result["error"]["code"] == "analysis_result_too_large"
        assert result["error"]["retryable"] is False


def test_name_error_diagnostic_has_traceback_source_and_line(tmp_path):
    with client_for(tmp_path) as client:
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=body("def analyze(context):\n    value = 1\n    return missing_name + value"),
        ).json()
        assert result["error"]["category"] == "code"
        assert "Traceback (most recent call last):" in result["stderr"]
        assert 'File "analysis.py", line 3, in analyze' in result["stderr"]
        assert "return missing_name + value" in result["stderr"]
        assert "NameError: name 'missing_name' is not defined" in result["stderr"]


def test_memory_exhaustion_is_resource_error(tmp_path):
    with client_for(tmp_path) as client:
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=body("def analyze(context):\n    raise MemoryError()"),
        ).json()
        assert result["error"]["category"] == "resource"
        assert result["error"]["code"] == "analysis_memory_limit"


def test_explicit_cancel_stops_child_and_keeps_receipt(tmp_path):
    request = body("def analyze(context):\n    while True: pass")
    with client_for(tmp_path) as client, ThreadPoolExecutor() as pool:
        pending = pool.submit(client.post, "/analysis/execute", json=request, headers=HEADERS)
        url = f"/analysis/executions/{request['execution_id']}"
        for _ in range(100):
            if client.get(url, headers=HEADERS).status_code == 200:
                break
            time.sleep(0.01)
        cancelled = client.delete(url, headers=HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "interrupted"
        assert pending.result(timeout=3).json()["status"] == "interrupted"
        assert client.delete(url, headers=HEADERS).json() == cancelled.json()
        assert client.get(url, headers=HEADERS).json() == cancelled.json()


def test_analysis_requires_configured_auth(tmp_path):
    with client_for(tmp_path, api_key="") as client:
        assert client.post("/analysis/execute", json=body()).status_code == 503


def test_full_storage_rejects_admission(tmp_path):
    with client_for(tmp_path, analysis_max_storage_bytes=20) as client:
        assert client.post("/analysis/execute", json=body(), headers=HEADERS).status_code == 503


def test_logs_cannot_overfill_result_store(tmp_path):
    with client_for(tmp_path, analysis_max_storage_bytes=6000) as client:
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=body("def analyze(context):\n    print('x' * 10000)\n    return 1"),
        )
        assert result.status_code == 200
        assert result.json()["status"] == "error"
        assert result.json()["error"]["category"] == "infrastructure"
        assert sum(p.stat().st_size for p in (tmp_path / "results").glob("*")) <= 6000


def test_packaged_contract_matches_routes_and_runtime(tmp_path):
    from sandbox.analysis import INLINE_BYTES, RETENTION_SECONDS, AnalysisRequest

    root = Path(__file__).resolve().parents[1]
    contract = json.loads(
        (root / "src/sandbox/contracts/analysis-execution.v1.json").read_text(encoding="utf-8")
    )
    with client_for(tmp_path) as client:
        paths = client.get("/openapi.json").json()["paths"]
    for operation, method in {
        "execute": "post",
        "status": "get",
        "cancel": "delete",
        "download": "get",
        "release": "delete",
    }.items():
        assert method in paths[contract["routes"][operation]]
    assert set(AnalysisRequest.model_fields) == set(contract["request"]["properties"])
    assert INLINE_BYTES == contract["defaults"]["inline_bytes"]
    assert RETENTION_SECONDS == contract["defaults"]["retention_hours"] * 3600


def test_deserialization_cannot_recover_private_credentials(tmp_path):
    code = (
        "from pandas.compat.pickle_compat import pickle as p\n"
        "def analyze(context):\n"
        "    f = p.loads(b'cbuiltins\\ngetattr\\n.')\n"
        "    return str(f(context.data.query, '__closure__'))"
    )
    with client_for(tmp_path, max_timeout_seconds=15) as client:
        result = client.post("/analysis/execute", json=body(code), headers=HEADERS).json()
        assert result["status"] == "error"
        assert "cell at" not in json.dumps(result)


@pytest.mark.parametrize(
    "reflection",
    [
        "operator.attrgetter('__closure__')(context.data.query)",
        "operator.methodcaller('__getattribute__', '__closure__')(context.data.query)",
        "pd.Series([context.data.query]).apply(operator.attrgetter('__closure__')).iloc[0]",
        "np.vectorize(operator.attrgetter('__closure__'), otypes=[object])"
        "([context.data.query])[0]",
    ],
)
def test_reflection_cannot_disclose_upstream_credentials_or_request(tmp_path, reflection):
    code = (
        "import operator\nimport pandas as pd\nimport numpy as np\n"
        "def analyze(context):\n"
        f"    cells = {reflection}\n"
        "    return [str(operator.attrgetter('cell_contents')(cell)) for cell in cells]"
    )
    url = "http://127.0.0.1:9/v1/internal/analysis-data/query"
    with client_for(tmp_path, max_timeout_seconds=15, analysis_callback_urls=(url,)) as client:
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=body(
                code, data_access={"base_url": url, "token": "credential-must-stay-in-parent"}
            ),
        ).json()
        assert result["status"] == "success", result
        serialized = json.dumps(result)
        assert "credential-must-stay-in-parent" not in serialized
        assert "data_access" not in serialized
        assert "execution_id':" not in serialized


def test_cancel_closes_inflight_proxy_and_child(tmp_path):
    entered = threading.Event()
    finish = threading.Event()
    baseline_handlers = sum("process_request_thread" in item.name for item in threading.enumerate())

    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            entered.set()
            finish.wait(15)
            try:
                self.wfile.write(b'{"data": 1}')
            except OSError:
                pass

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1/internal/analysis-data/query"
        try:
            with (
                client_for(
                    tmp_path, max_timeout_seconds=10, analysis_callback_urls=(url,)
                ) as client,
                ThreadPoolExecutor() as pool,
            ):
                request = body(
                    "def analyze(context):\n    return context.data.query('news', {})",
                    data_access={"base_url": url, "token": "test-secret"},
                )
                pending = pool.submit(
                    client.post, "/analysis/execute", json=request, headers=HEADERS
                )
                assert entered.wait(4)
                started = time.monotonic()
                cancelled = client.delete(
                    f"/analysis/executions/{request['execution_id']}", headers=HEADERS
                )
                assert cancelled.json()["status"] == "interrupted"
                assert time.monotonic() - started < 3
                assert pending.result(timeout=3).json()["status"] == "interrupted"
                active_handlers = sum(
                    "process_request_thread" in item.name for item in threading.enumerate()
                )
                assert (
                    active_handlers <= baseline_handlers + 1
                )  # Only the upstream test server remains.
        finally:
            finish.set()
            server.shutdown()
            thread.join()


async def test_duplicate_cancel_and_shutdown_wait_for_one_cleanup(tmp_path, monkeypatch):
    from sandbox.admission import ExecutionGate
    from sandbox.analysis import AnalysisRequest, AnalysisService

    gate = ExecutionGate(1, 1, 1)
    service = AnalysisService(Settings(analysis_result_dir=tmp_path), gate)
    cleanup_started = threading.Event()
    cleanup_finish = threading.Event()
    started = threading.Event()

    def controlled_process(request, cancellation):
        started.set()
        cancellation.wait(3)
        cleanup_started.set()
        cleanup_finish.wait(3)
        return {
            "execution_id": request["execution_id"],
            "status": "interrupted",
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
        }

    monkeypatch.setattr(service.executor, "_run_sync", controlled_process)
    request = AnalysisRequest(**body())
    execution = asyncio.create_task(service.execute(request))
    await asyncio.to_thread(started.wait, 2)
    first = asyncio.create_task(service.cancel(request.execution_id))
    await asyncio.to_thread(cleanup_started.wait, 2)
    second = asyncio.create_task(service.cancel(request.execution_id))
    shutdown = asyncio.create_task(service.close())
    try:
        await asyncio.sleep(0.05)
        assert gate.active == 1
        assert not first.done() and not second.done() and not shutdown.done()
        assert service.store.load(request.execution_id)["response"]["status"] == "running"
    finally:
        cleanup_finish.set()
        await asyncio.gather(execution, first, second, shutdown, return_exceptions=True)
    assert gate.active == 0
    assert service.store.load(request.execution_id)["response"]["status"] == "interrupted"


@pytest.mark.parametrize(
    "exit_code,category,code",
    [
        (-24, "resource", "analysis_cpu_limit"),
        (-25, "resource", "analysis_file_limit"),
        (-9, "infrastructure", "analysis_process_terminated"),
    ],
)
async def test_simulated_linux_signal_precedes_empty_result_parse(
    tmp_path, monkeypatch, exit_code, category, code
):
    from sandbox.analysis import AnalysisExecutor

    class SignalledProcess:
        def __init__(self, command, **kwargs):
            Path(command[-1]).write_bytes(b"")
            self.returncode = exit_code
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def poll(self):
            return self.returncode

    monkeypatch.setattr("sandbox.analysis.subprocess.Popen", SignalledProcess)
    result = await AnalysisExecutor(Settings(analysis_result_dir=tmp_path)).run_analysis(
        body(timeout=10, parameters={})
    )
    assert result["exit_code"] == exit_code
    assert result["error"]["category"] == category
    assert result["error"]["code"] == code


def test_initial_receipt_partial_write_is_never_published(tmp_path, monkeypatch):
    from sandbox.analysis import AnalysisStore

    store = AnalysisStore(Settings(analysis_result_dir=tmp_path))
    identity = str(uuid4())
    original_open = Path.open

    class InterruptedWrite:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, text):
            raise OSError("simulated disk full during write")

    def interrupted_open(path, mode="r", *args, **kwargs):
        if mode == "x":
            path.touch()
            return InterruptedWrite()
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", interrupted_open)

    def interrupted_flush(_descriptor):
        raise OSError("simulated disk full during flush")

    monkeypatch.setattr("sandbox.analysis.os.fsync", interrupted_flush)
    with pytest.raises(OSError):
        store.save(identity, {"expires": time.time() + 100}, create=True)
    assert not store.path(identity, "receipt").exists()


@pytest.mark.parametrize("corrupt", ["", "{", '{"expires": 100}', "[]"])
def test_corrupt_receipt_returns_interrupted_without_rerun(tmp_path, corrupt):
    request = body()
    root = tmp_path / "results"
    root.mkdir()
    (root / f"{request['execution_id']}.receipt.json").write_text(corrupt)
    with client_for(tmp_path) as client:
        status = client.get(f"/analysis/executions/{request['execution_id']}", headers=HEADERS)
        assert status.status_code == 200
        assert status.json()["status"] == "interrupted"
        assert status.json()["error"]["code"] == "analysis_receipt_corrupt"
        assert (
            client.post("/analysis/execute", json=request, headers=HEADERS).json() == status.json()
        )
        assert not list(root.glob("*.result.json"))


def test_atomic_create_does_not_overwrite_existing_receipt(tmp_path):
    from sandbox.analysis import AnalysisStore

    store = AnalysisStore(Settings(analysis_result_dir=tmp_path))
    identity = str(uuid4())
    first = {"marker": "original"}
    store.save(identity, first, create=True)
    with pytest.raises(FileExistsError):
        store.save(identity, {"marker": "replacement"}, create=True)
    assert json.loads(store.path(identity, "receipt").read_text()) == first
    assert not list(tmp_path.glob(".*.receipt.tmp"))
