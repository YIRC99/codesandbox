import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sandbox.config import Settings
from sandbox.main import create_app

HEADERS = {"X-API-Key": "test-runtime-key"}


def client_for(tmp_path, **overrides):
    values = dict(
        upload_dir=tmp_path / "uploads",
        data_dir=tmp_path / "data",
        data_upload_dir=tmp_path / "data-files",
        analysis_result_dir=tmp_path / "results",
        api_key="test-runtime-key",
        max_timeout_seconds=5,
    )
    values.update(overrides)
    return TestClient(create_app(Settings(**values)))


def request_body(**overrides):
    request = {
        "execution_id": str(uuid4()),
        "code": "def analyze(context):\n    return {'value': 42}",
    }
    request.update(overrides)
    return request


def runtime_from(client):
    response = client.get("/analysis/runtime", headers=HEADERS)
    assert response.status_code == 200
    return response.json()


def test_real_runtime_descriptor_source_versions_hash_and_auth(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/analysis/runtime").status_code == 401
        assert client.get("/analysis/runtime", headers={"X-API-Key": "wrong"}).status_code == 401
        descriptor = runtime_from(client)
        assert descriptor == runtime_from(client)
        assert descriptor["schema_version"] == "analysis-runtime.v1"
        assert descriptor["sdk_version"] == "1.0.0"
        assert descriptor["python_version"] == platform.python_version()
        assert descriptor["dependencies"]["pandas"] == importlib.metadata.version("pandas")
        assert "httpx" not in descriptor["dependencies"]
        expected_source = Path(__file__).parents[1] / "src/sandbox/analysis_runner.py"
        assert descriptor["runner_source"] == expected_source.read_text(encoding="utf-8")
        payload = {key: value for key, value in descriptor.items() if key != "runtime_sha256"}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(canonical.encode()).hexdigest() == descriptor["runtime_sha256"]
        assert descriptor["limits"]["max_timeout_seconds"] == 5
        assert "test-runtime-key" not in canonical
        assert str(tmp_path) not in canonical


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "caller-key"}])
def test_runtime_allows_requests_when_key_is_not_configured(tmp_path, headers):
    with client_for(tmp_path, api_key="") as client:
        response = client.get("/analysis/runtime", headers=headers)
        assert response.status_code == 200
        assert response.json()["schema_version"] == "analysis-runtime.v1"


def test_policy_only_upgrade_changes_runtime_and_exposes_exact_policy(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        before = runtime_from(client)
    read_text = Path.read_text
    policy_path = Path(__file__).parents[1] / "src/sandbox/runner.py"
    original = policy_path.read_text(encoding="utf-8")

    def changed_policy(path, *args, **kwargs):
        if path == policy_path:
            return original + "\n# changed trusted execution policy\n"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", changed_policy)
    with client_for(tmp_path) as client:
        after = runtime_from(client)
        assert after["runtime_sha256"] != before["runtime_sha256"]
        assert after["policy_source"] == original + "\n# changed trusted execution policy\n"


def test_wrong_expected_runtime_rejects_without_creating_execution(tmp_path):
    with client_for(tmp_path) as client:
        request = request_body(expected_runtime_sha256="0" * 64)
        response = client.post("/analysis/execute", json=request, headers=HEADERS)
        assert response.status_code == 409
        assert response.json()["error"]["category"] == "infrastructure"
        assert response.json()["error"]["code"] == "analysis_runtime_changed"
        assert client.app.state.gate.active == 0
        assert not client.app.state.analysis.tasks
        assert (
            client.get(
                f"/analysis/executions/{request['execution_id']}", headers=HEADERS
            ).status_code
            == 404
        )


def test_changed_runtime_precedes_new_stricter_timeout_validation(tmp_path):
    with client_for(tmp_path, max_timeout_seconds=1) as client:
        response = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=request_body(timeout=2, expected_runtime_sha256="0" * 64),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "analysis_runtime_changed"


def test_descriptor_distinguishes_windows_and_inherited_posix_limits(tmp_path):
    from sandbox.analysis_runtime import resource_enforcement

    settings = Settings(process_cpu_seconds=50)
    windows = resource_enforcement(settings, platform_name="Windows")
    assert windows["platform"] == "Windows"
    assert windows["limits"]["process_cpu_seconds"]["planned"] is None
    assert windows["limits"]["process_cpu_seconds"]["supported"] is False
    fake = SimpleNamespace(RLIMIT_CPU=0, RLIM_INFINITY=-1, getrlimit=lambda _: (10, 20))
    posix = resource_enforcement(settings, platform_name="Linux", resource_module=fake)
    assert posix["limits"]["process_cpu_seconds"] == {
        "supported": True,
        "inherited_hard": 20,
        "planned": 20,
    }
    assert posix["limits"]["process_memory_bytes"]["supported"] is False


@pytest.mark.parametrize(
    "code,status",
    [
        ("def analyze(context):\n    return 42", "success"),
        ("def analyze(context):\n    return missing_name", "error"),
    ],
)
def test_each_execution_receipt_has_runtime_evidence(tmp_path, code, status):
    with client_for(tmp_path) as client:
        descriptor = runtime_from(client)
        request = request_body(code=code, expected_runtime_sha256=descriptor["runtime_sha256"])
        result = client.post("/analysis/execute", json=request, headers=HEADERS).json()
        assert result["status"] == status
        assert result["runtime"] == {
            key: descriptor[key] for key in ("runtime_sha256", "sdk_version", "python_version")
        }
        assert (
            client.get(f"/analysis/executions/{request['execution_id']}", headers=HEADERS).json()
            == result
        )


def test_upgrade_preserves_existing_result_and_checks_new_execution(tmp_path):
    with client_for(tmp_path) as client:
        original_runtime = runtime_from(client)
        request = request_body(expected_runtime_sha256=original_runtime["runtime_sha256"])
        result = client.post("/analysis/execute", json=request, headers=HEADERS).json()
    with client_for(tmp_path, max_timeout_seconds=6) as upgraded:
        assert runtime_from(upgraded)["runtime_sha256"] != original_runtime["runtime_sha256"]
        assert upgraded.post("/analysis/execute", json=request, headers=HEADERS).json() == result
        assert (
            upgraded.get(f"/analysis/executions/{request['execution_id']}", headers=HEADERS).json()
            == result
        )
        request["execution_id"] = str(uuid4())
        assert upgraded.post("/analysis/execute", json=request, headers=HEADERS).status_code == 409


def test_runtime_hash_tracks_effective_limits(tmp_path):
    with client_for(tmp_path, analysis_max_result_bytes=900, process_file_bytes=600) as client:
        descriptor = runtime_from(client)
        assert descriptor["limits"]["max_result_bytes"] == 600
        assert descriptor["limits"]["max_callback_response_bytes"] == 600
    with client_for(tmp_path, analysis_max_result_bytes=900, process_file_bytes=700) as client:
        updated = runtime_from(client)
        assert updated["limits"]["max_result_bytes"] == 700
        assert descriptor["runtime_sha256"] != updated["runtime_sha256"]


def test_execution_uses_the_advertised_runner_snapshot(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        descriptor = runtime_from(client)
        read_text = Path.read_text

        def changed_runner(path, *args, **kwargs):
            if path.name == "analysis_runner.py":
                return "raise RuntimeError('unadvertised replacement')\n"
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", changed_runner)
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=request_body(expected_runtime_sha256=descriptor["runtime_sha256"]),
        ).json()
        assert result["status"] == "success", result
        assert result["result"] == {"value": 42}
        assert result["runtime"]["runtime_sha256"] == descriptor["runtime_sha256"]


def test_runtime_precondition_format_is_validated(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=request_body(expected_runtime_sha256="not-a-sha256"),
        )
        assert response.status_code == 422


def test_pre_descriptor_receipt_remains_recoverable(tmp_path):
    request = request_body()
    normalized = {**request, "inputs": {}, "parameters": {}, "data_access": None, "timeout": 5}
    fingerprint = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    receipt = {
        "fingerprint": fingerprint,
        "expires": 2000000000,
        "response": {
            "execution_id": request["execution_id"],
            "status": "error",
            "stdout": "",
            "stderr": "old diagnostic",
            "exit_code": 1,
        },
    }
    with client_for(tmp_path) as client:
        client.app.state.analysis.store.save(request["execution_id"], receipt, create=True)
        assert (
            client.post("/analysis/execute", json=request, headers=HEADERS).json()
            == receipt["response"]
        )


def test_descriptor_has_no_task_values_after_execution(tmp_path):
    with client_for(tmp_path) as client:
        before = runtime_from(client)
        request = request_body(inputs={"private_task_value": "private-data-content"})
        assert client.post("/analysis/execute", json=request, headers=HEADERS).status_code == 200
        after = runtime_from(client)
        assert before == after
        serialized = json.dumps(after)
        assert "private_task_value" not in serialized
        assert "private-data-content" not in serialized
        assert request["execution_id"] not in serialized


def test_runtime_ignores_workspace_package_shadow(tmp_path, monkeypatch):
    shadow = tmp_path / "pandas"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise RuntimeError('workspace shadow')")
    monkeypatch.syspath_prepend(str(tmp_path))
    with client_for(tmp_path, max_timeout_seconds=15) as client:
        descriptor = runtime_from(client)
        assert "pandas" in descriptor["dependencies"]
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=request_body(
                code="import pandas as pd\ndef analyze(context):\n"
                "    return int(pd.Series([2, 3]).sum())"
            ),
        ).json()
        assert result["status"] == "success", result
        assert result["result"] == 5


def test_trusted_import_paths_exclude_arbitrary_sys_path(tmp_path, monkeypatch):
    from sandbox.analysis_runner import trusted_import_paths

    shadow = tmp_path / "site-packages"
    shadow.mkdir()
    (shadow / "pandas.py").write_text("raise RuntimeError('fake package')")
    monkeypatch.syspath_prepend(str(shadow))
    paths, directories, files = trusted_import_paths()
    assert not any(Path(path).is_relative_to(tmp_path) for path in paths + directories + files)


def test_overlay_allows_distribution_namespace_and_native_libraries(tmp_path, monkeypatch):
    from sandbox import analysis_runner

    interpreter_site = tmp_path / "interpreter/Lib/site-packages"
    extra_site = tmp_path / "project/.venv/Lib/site-packages"
    interpreter_site.mkdir(parents=True)
    extra_site.mkdir(parents=True)
    (interpreter_site / "_uv_ephemeral_overlay.pth").write_text(
        f"import site; site.addsitedir({str(extra_site)!r}); site.addsitedir({str(tmp_path)!r})",
        encoding="utf-8",
    )
    namespace = extra_site / "example_namespace"
    native = extra_site / "example.libs"
    namespace.mkdir()
    native.mkdir()
    (namespace / "module.py").write_text("value = 5")
    (native / "library.dll").write_bytes(b"native dependency fixture")
    metadata = extra_site / "example-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: example\nVersion: 1.0\n")
    (metadata / "RECORD").write_text(
        "example_namespace/module.py,,\nexample.libs/library.dll,,\n"
        "example-1.0.dist-info/METADATA,,\n../../credential.txt,,\n"
    )
    original = analysis_runner.sysconfig.get_path
    monkeypatch.setattr(
        analysis_runner.sysconfig,
        "get_path",
        lambda name: str(interpreter_site) if name in {"purelib", "platlib"} else original(name),
    )
    paths, directories, files = analysis_runner.trusted_import_paths()
    assert str(extra_site) in paths
    assert str(namespace) in directories
    assert str(native) in directories
    assert str(tmp_path) not in paths + directories
    assert not any(Path(path).name == "credential.txt" for path in files)


@pytest.mark.parametrize("location", ["workspace", "interpreter_config"])
def test_import_permission_does_not_allow_pandas_arbitrary_file_reads(tmp_path, location):
    secret = tmp_path / "credentials.csv"
    secret.write_text("token\nprivate-token-must-not-be-read\n")
    target = secret if location == "workspace" else Path(sys.prefix) / "pyvenv.cfg"
    assert target.is_file()
    with client_for(tmp_path, max_timeout_seconds=15) as client:
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=request_body(
                code="import pandas as pd\ndef analyze(context):\n"
                "    return pd.read_csv(context.parameters['path']).to_dict()",
                parameters={"path": str(target)},
            ),
        ).json()
        assert result["status"] == "error", result
        assert result["error"]["message"] == (
            "Analysis may only read task inputs via context.data.read"
        )
        assert "private-token-must-not-be-read" not in json.dumps(result)


def test_public_request_cannot_override_trusted_import_policy(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=request_body(import_policy=[[str(tmp_path)], [str(tmp_path)], []]),
        )
        assert response.status_code == 422


@pytest.mark.parametrize(
    "code,identifier,line,column",
    [
        ("def analyze(context):\n    return type(ValueError()).__name__", "__name__", 2, 12),
        ("def analyze(context):\n    return open('input.csv')", "open", 2, 12),
        ("import os\ndef analyze(context):\n    return 1", "os", 1, 1),
        ("from pathlib import Path\ndef analyze(context):\n    return 1", "pathlib", 1, 1),
        ("import builtins\ndef analyze(context):\n    return 1", "builtins", 1, 1),
    ],
)
def test_analysis_policy_rejection_identifies_source_location(
    tmp_path, code, identifier, line, column
):
    with client_for(tmp_path) as client:
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=request_body(code=code),
        ).json()
        assert result["status"] == "error", result
        assert result["error"]["category"] == "code"
        message = result["error"]["message"]
        assert repr(identifier) in message
        assert f"analysis.py line {line}, column {column}" in message


@pytest.mark.parametrize("module", ["matplotlib", "seaborn", "backtesting", "pandas_ta"])
def test_libraries_requiring_external_cache_are_not_advertised_or_executable(tmp_path, module):
    with client_for(tmp_path, max_timeout_seconds=15) as client:
        descriptor = runtime_from(client)
        result = client.post(
            "/analysis/execute",
            headers=HEADERS,
            json=request_body(code=f"import {module}\ndef analyze(context):\n    return 1"),
        ).json()
        assert result["status"] == "error", result
        assert repr(module) in result["error"]["message"], result
        assert "analysis.py line 1, column 1" in result["error"]["message"]
        assert module not in descriptor["dependencies"]
