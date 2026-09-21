"""Independent analysis execution, durable receipts, and authenticated result delivery."""

import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from starlette.responses import FileResponse, JSONResponse, Response

from .admission import SandboxBusyError
from .analysis_proxy import AnalysisDataProxy
from .analysis_runtime import AnalysisRuntime
from .executor import SandboxExecutor

_CONTRACT = json.loads(
    (Path(__file__).parent / "contracts/analysis-execution.v1.json").read_text(encoding="utf-8")
)
INLINE_BYTES = _CONTRACT["defaults"]["inline_bytes"]
RETENTION_SECONDS = _CONTRACT["defaults"]["retention_hours"] * 3600


class DataAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: StrictStr
    token: StrictStr = Field(min_length=1, repr=False)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    execution_id: UUID
    code: StrictStr = Field(min_length=1)
    inputs: dict = Field(default_factory=dict)
    parameters: dict = Field(default_factory=dict)
    data_access: DataAccess | None = None
    timeout: float | None = Field(default=None, gt=0)
    expected_runtime_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


def failure(category, code, message, retryable=False):
    return dict(category=category, code=code, message=message, retryable=retryable)


class AnalysisRuntimeChanged(Exception):
    pass


class AnalysisStore:
    def __init__(self, settings):
        self.root = settings.analysis_result_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_storage = settings.analysis_max_storage_bytes

    def path(self, identity, suffix):
        path = self.root / f"{UUID(str(identity))}.{suffix}.json"
        if path.is_symlink() or path.resolve().parent != self.root:
            raise FileNotFoundError("Result unavailable")
        return path

    def load(self, identity):
        path = self.path(identity, "receipt")
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            response = receipt["response"]
            fingerprint = receipt["fingerprint"]
            if (
                not isinstance(response, dict)
                or response["execution_id"] != str(identity)
                or response["status"] not in _CONTRACT["response"]["status"]
                or not isinstance(response["stdout"], str)
                or not isinstance(response["stderr"], str)
                or not isinstance(response["exit_code"], int)
                or not isinstance(receipt["expires"], (float, int))
                or not math.isfinite(receipt["expires"])
                or not (
                    isinstance(fingerprint, str)
                    and len(fingerprint) == 64
                    or fingerprint is None
                    and response["status"] == "interrupted"
                )
            ):
                raise ValueError("Invalid execution receipt")
        except (ValueError, UnicodeError, TypeError, KeyError):
            receipt = self._recover_corrupt(identity, path)
        if receipt["expires"] <= time.time():
            self.release(identity)
            path.unlink(missing_ok=True)
            raise FileNotFoundError("Execution expired")
        return receipt

    def _recover_corrupt(self, identity, path):
        receipt = dict(
            fingerprint=None,
            expires=path.stat().st_mtime + RETENTION_SECONDS,
            response=dict(
                execution_id=str(identity),
                status="interrupted",
                stdout="",
                stderr="",
                exit_code=-1,
                error=failure(
                    "infrastructure",
                    "analysis_receipt_corrupt",
                    "Execution receipt was damaged; execution was not repeated",
                    True,
                ),
            ),
        )
        try:
            self.save(identity, receipt)
        except OSError:
            # Even a still-full disk must yield an explicit recovery state, not repeated 500s.
            pass
        return receipt

    def save(self, identity, receipt, *, create=False):
        path = self.path(identity, "receipt")
        raw = json.dumps(receipt, ensure_ascii=False, allow_nan=False)
        files = [item for item in self.root.glob("*.json") if not item.is_symlink()]
        used = sum(item.stat().st_size for item in files)
        previous = path.stat().st_size if path.exists() else 0
        # Reserve a small terminal receipt even when the result/log quota fills up.
        reserve = 4096 * max(
            0, sum(item.name.endswith(".receipt.json") for item in files) + (1 if create else -1)
        )
        if used - previous + len(raw.encode("utf-8")) + reserve > self.max_storage:
            raise OSError("Analysis result storage is full")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{identity}.",
                suffix=".receipt.tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if create:
                # Atomic no-clobber publication: concurrent creators cannot replace a receipt.
                os.link(temporary, path)
            else:
                temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def persist_result(self, identity, result, expires):
        raw = json.dumps(
            result, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        used = sum(
            path.stat().st_size for path in self.root.glob("*.json") if not path.is_symlink()
        )
        reserve = 4096 * sum(
            1 for path in self.root.glob("*.receipt.json") if not path.is_symlink()
        )
        if used + len(raw) + reserve > self.max_storage:
            raise OSError("Analysis result storage is full")
        path = self.path(identity, "result")
        with path.open("xb") as stream:
            stream.write(raw)
        return {
            "artifact_id": str(identity),
            "filename": f"analysis-{identity}.json",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
        }

    def release(self, identity):
        self.path(identity, "result").unlink(missing_ok=True)

    def cleanup_expired(self):
        removed = 0
        for path in self.root.glob("*.receipt.json"):
            if path.is_symlink():
                continue
            try:
                self.load(path.name.split(".")[0])
            except FileNotFoundError:
                removed += 1
            except (OSError, ValueError, KeyError):
                continue
        for path in self.root.glob(".*.receipt.tmp"):
            if not path.is_symlink() and path.stat().st_mtime + RETENTION_SECONDS <= time.time():
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def recover_interrupted(self):
        # This store is owned by one service worker (matching the deployment).
        for path in self.root.glob("*.receipt.json"):
            if path.is_symlink():
                continue
            try:
                identity = path.name.split(".")[0]
                receipt = self.load(identity)
                if receipt["response"]["status"] == "running":
                    receipt["response"].update(
                        status="interrupted",
                        exit_code=-1,
                        error=failure(
                            "infrastructure",
                            "analysis_interrupted",
                            "Previous service process stopped during execution",
                            True,
                        ),
                    )
                    self.save(identity, receipt)
            except (OSError, ValueError, KeyError):
                continue


class AnalysisExecutor(SandboxExecutor):
    def __init__(self, settings, runtime=None):
        super().__init__(settings)
        self.runtime = runtime or AnalysisRuntime(settings)

    async def run_analysis(self, request):
        cancelled = threading.Event()
        job = asyncio.create_task(asyncio.to_thread(self._run_sync, request, cancelled))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            cancelled.set()
            while not job.done():
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if job.done() and not job.cancelled():
                job.exception()
            raise

    def _run_sync(self, request, cancelled):
        with (
            AnalysisDataProxy(
                request.get("data_access"),
                request["timeout"],
                self._settings.analysis_max_result_bytes,
            ) as proxy,
            tempfile.TemporaryDirectory(prefix="analysis-sandbox-") as directory,
        ):
            workspace = Path(directory)
            script = workspace / "analysis.py"
            input_path = workspace / "request.json"
            result_path = workspace / "result.json"
            runner_path = workspace / "analysis_runner.py"
            runner_path.write_text(self.runtime.runner_source, encoding="utf-8")
            (workspace / "runner.py").write_text(self.runtime.policy_source, encoding="utf-8")
            script.write_text(request["code"], encoding="utf-8")
            child_request = {
                "inputs": request["inputs"],
                "parameters": request["parameters"],
                "timeout": request["timeout"],
                "data_proxy_url": proxy.url,
                "import_policy": self.runtime.import_policy,
            }
            input_path.write_text(json.dumps(child_request, allow_nan=False), encoding="utf-8")
            environment = self._child_environment()
            environment["SANDBOX_ANALYSIS_MAX_RESULT_BYTES"] = str(
                min(self._settings.analysis_max_result_bytes, self._settings.process_file_bytes)
            )
            command = [
                sys.executable,
                "-X",
                "utf8",
                "-I",
                str(runner_path),
                str(script),
                str(input_path),
                str(result_path),
            ]
            options = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}
            )
            process = subprocess.Popen(
                command,
                cwd=directory,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **options,
            )
            logs = [bytearray(), bytearray()]

            def drain(stream, output):
                truncated = False
                with stream:
                    while chunk := stream.read(65536):
                        remaining = max(0, self._settings.max_output_bytes - len(output))
                        output.extend(chunk[:remaining])
                        truncated = truncated or len(chunk) > remaining
                if truncated:
                    output.extend(b"\n[output truncated]\n")

            readers = [
                threading.Thread(target=drain, args=(stream, output))
                for stream, output in zip((process.stdout, process.stderr), logs, strict=True)
            ]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + request["timeout"]
            status = None
            try:
                while process.poll() is None:
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        status = "interrupted" if cancelled.is_set() else "timeout"
                        self._terminate_process_tree_sync(process)
                        break
                    cancelled.wait(0.02)
            finally:
                if process.poll() is None:
                    self._terminate_process_tree_sync(process)
                for reader in readers:
                    reader.join()
            status = status or ("success" if process.returncode == 0 else "error")
            response = dict(
                execution_id=request["execution_id"],
                status=status,
                stdout=self._decode(bytes(logs[0])),
                stderr=self._decode(bytes(logs[1])),
                exit_code=process.returncode if status not in {"timeout", "interrupted"} else -1,
            )
            token = (request.get("data_access") or {}).get("token")
            for key in ("stdout", "stderr"):
                response[key] = response[key].replace(directory, ".")
                runner_path = Path(__file__).with_name("analysis_runner.py")
                response[key] = response[key].replace(str(runner_path), "analysis_runner.py")
                response[key] = response[key].replace(runner_path.as_posix(), "analysis_runner.py")
                if token:
                    response[key] = response[key].replace(token, "[redacted]")
            if status in {"timeout", "interrupted"}:
                response["error"] = failure(
                    "infrastructure", f"analysis_{status}", f"Analysis execution {status}", True
                )
                return response
            if process.returncode < 0:
                signal_number = -process.returncode
                if signal_number in {24, 25}:  # Linux SIGXCPU / SIGXFSZ exit representations.
                    code = "analysis_cpu_limit" if signal_number == 24 else "analysis_file_limit"
                    response["error"] = failure(
                        "resource", code, "Analysis process was terminated by a resource limit"
                    )
                else:
                    response["error"] = failure(
                        "infrastructure",
                        "analysis_process_terminated",
                        f"Analysis process terminated by signal {signal_number}",
                        True,
                    )
                return response
            limit = min(self._settings.analysis_max_result_bytes, self._settings.process_file_bytes)
            if (
                not result_path.is_file()
                or result_path.is_symlink()
                or result_path.stat().st_size > limit
            ):
                response.update(
                    status="error",
                    error=failure(
                        "infrastructure",
                        "analysis_result_missing",
                        "Runner did not produce a valid result",
                    ),
                )
                return response
            try:
                result = json.loads(
                    result_path.read_bytes(),
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")),
                )
                if "error" in result:
                    response.update(status="error", error=result["error"])
                elif "result" in result and status == "success":
                    response["result"] = result["result"]
                else:
                    raise ValueError("Missing result")
            except (ValueError, UnicodeError):
                response.update(
                    status="error",
                    error=failure("code", "analysis_result_invalid", "Invalid result JSON"),
                )
            return response


class AnalysisService:
    def __init__(self, settings, gate):
        self.settings = settings
        self.gate = gate
        self.store = AnalysisStore(settings)
        self.store.recover_interrupted()
        self.runtime = AnalysisRuntime(settings)
        self.executor = AnalysisExecutor(settings, self.runtime)
        self.tasks = {}
        self.cancellations = {}

    def request_fingerprint(self, body, *, legacy=False):
        request = body.model_dump(mode="json")
        request.pop("expected_runtime_sha256", None)
        if legacy:
            request["timeout"] = body.timeout or self.settings.max_timeout_seconds
        try:
            raw = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError):
            raise HTTPException(400, "Analysis inputs must be finite JSON") from None
        return hashlib.sha256(raw).hexdigest()

    def validate(self, body):
        request = body.model_dump(mode="json")
        request["timeout"] = body.timeout or self.settings.max_timeout_seconds
        if request["timeout"] > self.settings.max_timeout_seconds:
            raise HTTPException(400, "Analysis timeout exceeds configured limit")
        if len(body.code.encode("utf-8")) > self.settings.max_upload_bytes:
            raise HTTPException(413, "Analysis code exceeds configured limit")
        try:
            raw = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError):
            raise HTTPException(400, "Analysis inputs must be finite JSON") from None
        if len(raw) > self.settings.max_data_file_bytes:
            raise HTTPException(413, "Analysis inputs exceed configured limit")
        if body.data_access:
            url = urlsplit(body.data_access.base_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or body.data_access.base_url not in self.settings.analysis_callback_urls
            ):
                raise HTTPException(400, "Analysis callback URL is not configured")
        return request, self.request_fingerprint(body)

    def response(self, receipt):
        response = dict(receipt["response"])
        if response.get("artifact", {}).get("size_bytes", INLINE_BYTES + 1) <= INLINE_BYTES:
            try:
                response["result"] = json.loads(
                    self.store.path(response["execution_id"], "result").read_bytes()
                )
            except FileNotFoundError:
                pass
        return response

    async def execute(self, body):
        identity = str(body.execution_id)
        fingerprint = self.request_fingerprint(body)
        try:
            receipt = self.store.load(identity)
        except FileNotFoundError:
            expected = body.expected_runtime_sha256
            if expected and expected.lower() != self.runtime.receipt()["runtime_sha256"]:
                raise AnalysisRuntimeChanged from None
            request, fingerprint = self.validate(body)
            receipt = dict(
                fingerprint=fingerprint,
                fingerprint_version=2,
                expires=time.time() + RETENTION_SECONDS,
                response=dict(
                    execution_id=identity,
                    status="running",
                    stdout="",
                    stderr="",
                    exit_code=0,
                    runtime=self.runtime.receipt(),
                ),
            )
            try:
                self.store.save(identity, receipt, create=True)
            except FileExistsError:
                receipt = self.store.load(identity)
            except OSError:
                raise HTTPException(503, "Analysis result storage is unavailable") from None
            else:
                task = asyncio.create_task(self._execute(request, receipt))
                self.tasks[identity] = task
                task.add_done_callback(lambda _: self.tasks.pop(identity, None))
        if receipt["fingerprint"] is None:
            return self.response(receipt)
        matches_legacy = "fingerprint_version" not in receipt and receipt[
            "fingerprint"
        ] == self.request_fingerprint(body, legacy=True)
        if receipt["fingerprint"] != fingerprint and not matches_legacy:
            raise HTTPException(409, "Execution ID belongs to a different request")
        if identity in self.tasks:
            await asyncio.shield(self.tasks[identity])
            receipt = self.store.load(identity)
        return self.response(receipt)

    async def _execute(self, request, receipt):
        identity = request["execution_id"]
        try:
            async with self.gate.slot():
                response = await self.executor.run_analysis(request)
            if response["status"] == "success":
                result = response.pop("result")
                response["artifact"] = self.store.persist_result(
                    identity, result, receipt["expires"]
                )
        except asyncio.CancelledError:
            response = dict(
                execution_id=identity,
                status="interrupted",
                stdout="",
                stderr="",
                exit_code=-1,
                error=failure(
                    "infrastructure",
                    "analysis_interrupted",
                    "Service stopped during execution",
                    True,
                ),
            )
        except SandboxBusyError:
            response = dict(
                execution_id=identity,
                status="error",
                stdout="",
                stderr="",
                exit_code=-1,
                error=failure(
                    "infrastructure", "sandbox_busy", "Analysis admission queue is full", True
                ),
            )
        except Exception:
            response = dict(
                execution_id=identity,
                status="error",
                stdout="",
                stderr="",
                exit_code=-1,
                error=failure(
                    "infrastructure",
                    "analysis_execution_failed",
                    "Analysis execution infrastructure failed",
                    True,
                ),
            )
        response["runtime"] = receipt["response"].get("runtime", self.runtime.receipt())
        receipt["response"] = response
        try:
            self.store.save(identity, receipt)
        except OSError:
            self.store.release(identity)
            receipt["response"] = dict(
                execution_id=identity,
                status="error",
                stdout="",
                stderr="",
                exit_code=-1,
                error=failure(
                    "infrastructure",
                    "analysis_storage_full",
                    "Analysis result could not be retained within the storage limit",
                    True,
                ),
                runtime=response["runtime"],
            )
            self.store.save(identity, receipt)

    async def close(self):
        await asyncio.gather(*(self.cancel(identity) for identity in list(self.tasks)))

    async def cancel(self, identity):
        identity = str(identity)
        operation = self.cancellations.get(identity)
        if operation is None:
            operation = asyncio.create_task(self._cancel_once(identity))
            self.cancellations[identity] = operation
            operation.add_done_callback(lambda _: self.cancellations.pop(identity, None))
        return await asyncio.shield(operation)

    async def _cancel_once(self, identity):
        receipt = self.store.load(identity)
        task = self.tasks.get(identity)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            receipt = self.store.load(identity)
        if receipt["response"]["status"] == "running":
            receipt["response"].update(
                status="interrupted",
                exit_code=-1,
                error=failure(
                    "infrastructure", "analysis_interrupted", "Analysis was cancelled", True
                ),
            )
            self.store.save(identity, receipt)
        return self.response(receipt)


def install_analysis_routes(app, settings, authenticated):
    service = AnalysisService(settings, app.state.gate)
    app.state.analysis = service

    router = APIRouter(prefix="/analysis", dependencies=[authenticated])

    @router.post("/execute")
    async def execute(body: AnalysisRequest):
        try:
            return await service.execute(body)
        except AnalysisRuntimeChanged:
            return JSONResponse(
                status_code=409,
                content={
                    "error": failure(
                        "infrastructure",
                        "analysis_runtime_changed",
                        "Analysis runtime changed; fetch the current runtime before execution",
                    ),
                    "runtime": service.runtime.receipt(),
                },
            )

    @router.get("/runtime")
    async def runtime():
        return service.runtime.descriptor()

    @router.get("/executions/{execution_id}")
    async def status(execution_id: UUID):
        try:
            return service.response(service.store.load(execution_id))
        except FileNotFoundError:
            raise HTTPException(404, "Analysis execution unavailable") from None

    @router.delete("/executions/{execution_id}")
    async def cancel(execution_id: UUID):
        try:
            return await service.cancel(execution_id)
        except FileNotFoundError:
            raise HTTPException(404, "Analysis execution unavailable") from None

    @router.get("/results/{artifact_id}")
    async def download(artifact_id: UUID):
        try:
            receipt = service.store.load(artifact_id)
            artifact = receipt["response"].get("artifact")
            path = service.store.path(artifact_id, "result")
            if not artifact or not path.is_file():
                raise FileNotFoundError
            return FileResponse(path, media_type="application/json", filename=artifact["filename"])
        except FileNotFoundError:
            raise HTTPException(404, "Analysis result unavailable") from None

    @router.delete("/results/{artifact_id}", status_code=204)
    async def release(artifact_id: UUID):
        try:
            service.store.load(artifact_id)
            service.store.release(artifact_id)
        except FileNotFoundError:
            raise HTTPException(404, "Analysis result unavailable") from None
        return Response(status_code=204)

    app.include_router(router)
