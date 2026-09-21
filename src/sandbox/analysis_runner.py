"""Trusted analysis entrypoint. Result JSON never shares the stdout log channel."""

import ast
import builtins
import copy
import errno
import json
import linecache
import os
import runpy
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from urllib.parse import urlsplit


class DataAccessError(Exception):
    def __init__(self, category, code, message, retryable=False):
        super().__init__(message)
        self.detail = dict(category=category, code=code, message=message, retryable=retryable)


class AnalysisResourceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.detail = dict(category="resource", code=code, message=message, retryable=False)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    script, request_path, output_path = map(Path, sys.argv[1:4])
    policy = runpy.run_path(str(Path(__file__).with_name("runner.py")))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request_path.unlink()
    source = script.read_text(encoding="utf-8")
    # Keep trusted source lines for diagnostics without reopening sandbox files after audit setup.
    linecache.cache["analysis.py"] = (
        len(source),
        None,
        source.splitlines(keepends=True),
        "analysis.py",
    )
    max_bytes = int(os.environ["SANDBOX_ANALYSIS_MAX_RESULT_BYTES"])
    result_stream = output_path.open("xb")
    proxy_url = request.get("data_proxy_url")
    query_timeout = min(request["timeout"], 30)
    inputs = request["inputs"]
    proxy_address = ("127.0.0.1", urlsplit(proxy_url).port) if proxy_url else None
    # Load the transport before restricting file access; no proxy/environment credentials.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    library_roots = [Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]

    def audit(event, args):
        if event == "socket.connect" and (proxy_address is None or args[1] != proxy_address):
            raise PermissionError("Network access is limited to the task data proxy")
        if event == "socket.getaddrinfo" and (
            proxy_address is None or (args[0], args[1]) != proxy_address
        ):
            raise PermissionError("Name resolution is limited to the task data proxy")
        if event in {"socket.bind", "socket.sendto", "socket.sendmsg"}:
            raise PermissionError("Analysis cannot open listening or datagram sockets")
        if event == "open":
            path, mode, flags = args
            if isinstance(path, int):
                raise PermissionError("File descriptor access is not allowed")
            candidate = Path(path).resolve()
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                raise PermissionError("Analysis file writes are not allowed")
            if not any(candidate.is_relative_to(root) for root in library_roots):
                raise PermissionError("Analysis may only read task inputs via context.data.read")
        if event in {
            "os.remove",
            "os.rename",
            "os.mkdir",
            "os.rmdir",
            "os.link",
            "os.symlink",
            "os.chdir",
            "os.chmod",
            "os.truncate",
            "os.listdir",
            "os.scandir",
        }:
            # Imports may inspect library directories; they cannot read task/server files.
            if event in {"os.listdir", "os.scandir"} and args and isinstance(args[0], (str, bytes)):
                candidate = Path(os.fsdecode(args[0])).resolve()
                if any(candidate.is_relative_to(root) for root in library_roots):
                    return
            raise PermissionError("Filesystem mutation or inspection is not allowed")

    def read(alias):
        if alias not in inputs:
            raise DataAccessError("data", "dataset_not_found", "Dataset alias is unavailable")
        return copy.deepcopy(inputs[alias])

    def query(endpoint, params=None):
        if not proxy_url:
            raise DataAccessError(
                "permission", "data_access_disabled", "Data access is not enabled"
            )
        if not isinstance(endpoint, str) or not isinstance(params or {}, dict):
            raise ValueError("query requires endpoint string and params object")
        payload = json.dumps(
            {"endpoint": endpoint, "params": params or {}}, allow_nan=False
        ).encode()
        http_request = urllib.request.Request(
            proxy_url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
            },
        )
        try:
            try:
                response = opener.open(http_request, timeout=query_timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise DataAccessError(
                        "data", "data_response_too_large", "Data response exceeds limit"
                    )
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise DataAccessError(
                        "infrastructure",
                        "data_gateway_invalid_json",
                        "Data gateway returned invalid JSON",
                        True,
                    ) from None
                if response.status >= 300 or isinstance(payload, dict) and "error" in payload:
                    error = payload.get("error", {}) if isinstance(payload, dict) else {}
                    category = error.get("category")
                    if category not in {"data", "permission", "infrastructure"}:
                        category = (
                            "permission" if response.status in {401, 403} else "infrastructure"
                        )
                    message = str(error.get("message", "Data gateway request failed"))[:1000]
                    error_code = str(error.get("code", "data_gateway_error"))[:100]
                    raise DataAccessError(
                        category,
                        error_code,
                        message,
                        bool(error.get("retryable", response.status >= 500)),
                    )
                if not isinstance(payload, dict) or "data" not in payload:
                    raise DataAccessError(
                        "infrastructure",
                        "data_gateway_invalid_response",
                        "Data gateway response has no data",
                        True,
                    )
                return payload["data"]
        except DataAccessError:
            raise
        except Exception:
            raise DataAccessError(
                "infrastructure",
                "data_gateway_unavailable",
                "Data gateway could not be reached",
                True,
            ) from None

    try:
        policy["validate_script_ast"](script)
        tree = ast.parse(source, filename="analysis.py")
        blocked_names = {
            "eval",
            "exec",
            "compile",
            "open",
            "getattr",
            "setattr",
            "delattr",
            "globals",
            "locals",
            "vars",
            "dir",
            "breakpoint",
            "__import__",
        }
        blocked_imports = {
            "os",
            "sys",
            "pathlib",
            "urllib",
            "httpx",
            "requests",
            "yfinance",
            "ast",
            "types",
            "traceback",
            "io",
            "builtins",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and (
                node.attr.startswith("_")
                or node.attr in blocked_names
                or node.attr
                in {"os", "sys", "builtins", "modules", "format", "format_map", "environ"}
            ):
                raise PermissionError("Private attribute access is not allowed")
            if isinstance(node, ast.Name) and (
                node.id in blocked_names or node.id.startswith("__")
            ):
                raise PermissionError("Unsafe builtin access is not allowed")
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            if any(name.split(".")[0] in blocked_imports for name in imports):
                raise PermissionError("Use context.data for analysis input access")
        compiled = compile(tree, "analysis.py", "exec")
        policy["install_audit_hook"]()
        policy["apply_resource_limits"]()
        sys.addaudithook(audit)
        namespace = {"__builtins__": dict(vars(builtins)), "__name__": "analysis"}
        exec(compiled, namespace)
        analyze = namespace.get("analyze")
        if not callable(analyze):
            raise ValueError("Module must define analyze(context)")
        context = SimpleNamespace(
            inputs=MappingProxyType(copy.deepcopy(request["inputs"])),
            parameters=MappingProxyType(request["parameters"]),
            data=SimpleNamespace(read=read, query=query),
        )
        result = analyze(context)
        encoded = json.dumps(
            {"result": result}, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > max_bytes:
            raise AnalysisResourceError(
                "analysis_result_too_large", "Analysis result exceeds configured size limit"
            )
    except BaseException as exc:
        if isinstance(exc, (DataAccessError, AnalysisResourceError)):
            error = exc.detail
        elif isinstance(exc, MemoryError):
            error = dict(
                category="resource",
                code="analysis_memory_limit",
                message="Analysis memory allocation failed",
                retryable=False,
            )
        elif isinstance(exc, OSError) and exc.errno in {errno.EFBIG, errno.ENOSPC, errno.ENOMEM}:
            error = dict(
                category="resource",
                code="analysis_resource_limit",
                message="Analysis exceeded available process or file resources",
                retryable=False,
            )
        else:
            message = str(exc)[:2000]
            error = dict(
                category="code", code="analysis_code_error", message=message, retryable=False
            )
        encoded = json.dumps({"error": error}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        result_stream.write(encoded)
        result_stream.close()
        try:
            traceback.TracebackException.from_exception(exc, capture_locals=False).print(
                file=sys.stderr
            )
        except Exception:
            # Diagnostic formatting must not destroy the separate structured error channel.
            print(f"{type(exc).__name__}: {error['message']}", file=sys.stderr)
        return 1
    result_stream.write(encoded)
    result_stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
