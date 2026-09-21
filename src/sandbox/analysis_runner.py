"""Trusted analysis entrypoint. Result JSON never shares the stdout log channel."""

import ast
import builtins
import copy
import errno
import importlib.metadata
import json
import linecache
import os
import runpy
import sys
import sysconfig
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from urllib.parse import urlsplit

ANALYSIS_SDK_VERSION = "1.0.0"
BLOCKED_ANALYSIS_IMPORTS = frozenset(
    {
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
        "matplotlib",
        "seaborn",
        "backtesting",
        "pandas_ta",
    }
)


def trusted_import_paths():
    """Resolve installed dependencies without trusting cwd, PYTHONPATH or sys.path.

    uv records overlay dependencies in the interpreter-owned .pth file. Parse only
    literal site.addsitedir calls there; never execute .pth text or trust arbitrary
    search paths. Installed distribution records also cover namespace/native libs.
    """
    sites = []
    for name in ("purelib", "platlib"):
        path = Path(sysconfig.get_path(name)).resolve()
        if path not in sites:
            sites.append(path)
    for site_root in tuple(sites):
        overlay = site_root / "_uv_ephemeral_overlay.pth"
        if not overlay.is_file():
            continue
        tree = ast.parse(overlay.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "site"
                and node.func.attr == "addsitedir"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                path = Path(node.args[0].value).resolve()
                if path.name == "site-packages" and path.is_dir() and path not in sites:
                    sites.append(path)

    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    standard = [stdlib, Path(sys.base_prefix, "DLLs").resolve()]
    dynamic = sysconfig.get_config_var("DESTSHARED")
    if dynamic:
        standard.append(Path(dynamic).resolve())
    # Windows and POSIX place the optional stdlib zip beside the stdlib directory.
    archive = stdlib.parent / f"python{sys.version_info.major}{sys.version_info.minor}.zip"
    standard.append(archive)
    directories = {path for path in standard if path.is_dir()}
    files = {path for path in standard if path.is_file()}
    for site_root in sites:
        for distribution in importlib.metadata.distributions(path=[str(site_root)]):
            top_levels = {
                record.parts[0]
                for record in distribution.files or ()
                if record.parts and record.parts[0] not in {".", ".."}
            }
            for top_level in top_levels:
                path = (site_root / top_level).resolve()
                if not path.is_relative_to(site_root):
                    continue
                if path.is_dir():
                    directories.add(path)
                elif path.is_file():
                    files.add(path)
    return (
        [str(path) for path in dict.fromkeys(standard + sites)],
        [str(path) for path in sorted(directories)],
        [str(path) for path in sorted(files)],
    )


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
    # Captured once by the trusted parent alongside the runtime source snapshot.
    # The public request schema never accepts this internal import policy.
    import_paths, library_directories, library_files = request["import_policy"]
    sys.path[:] = import_paths
    library_roots = [Path(path) for path in library_directories]
    library_files = {Path(path) for path in library_files}
    import_roots = {Path(path) for path in import_paths}

    def library_readable(candidate):
        if candidate in library_files:
            return True
        for root in library_roots:
            if candidate.is_relative_to(root):
                # A stdlib root can contain site-packages; only distribution-owned
                # paths within it are readable, not every file in that directory.
                relative = candidate.relative_to(root)
                if "site-packages" not in relative.parts:
                    return True
        return False

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
            if not library_readable(candidate):
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
                if candidate in import_roots or library_readable(candidate):
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

    def reject_source(node, identifier, message):
        # AST columns are UTF-8 byte offsets; show a one-based character column.
        prefix = source.splitlines()[node.lineno - 1].encode("utf-8")[: node.col_offset]
        column = len(prefix.decode("utf-8")) + 1
        raise PermissionError(
            f"{message}: {identifier!r} at analysis.py line {node.lineno}, column {column}"
        )

    try:
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
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and (
                node.attr.startswith("_")
                or node.attr in blocked_names
                or node.attr
                in {"os", "sys", "builtins", "modules", "format", "format_map", "environ"}
            ):
                reject_source(node, node.attr, "Private attribute access is not allowed")
            if isinstance(node, ast.Name) and (
                node.id in blocked_names or node.id.startswith("__")
            ):
                reject_source(node, node.id, "Unsafe builtin access is not allowed")
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            for name in imports:
                if name.split(".")[0] in BLOCKED_ANALYSIS_IMPORTS:
                    reject_source(node, name, "Import is not allowed by analysis policy")
        policy["validate_script_ast"](script)
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
