import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sandbox.config import Settings
from sandbox.main import create_app

RUNTIME_PARAMETERS = {
    "initial_capital": "1000000",
    "fee_rate": "0.0001",
    "slippage_rate": "0.0005",
    "max_drawdown_limit_rate": "0.20",
}
FUTURES_RUNTIME_PARAMETERS = {
    **RUNTIME_PARAMETERS,
    "contract_multiplier": "10000",
    "margin_rate": "0.02",
}


def make_client(tmp_path: Path, **overrides: object) -> TestClient:
    data_dir = tmp_path / "data"
    data_upload_dir = tmp_path / "data-files"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "input.csv").write_text("date,close\n2026-07-21,101.5\n", encoding="utf-8")
    (data_dir / "test_data.csv").write_text("date,close\n2026-07-20,100.0\n", encoding="utf-8")
    values: dict[str, object] = {
        "upload_dir": tmp_path,
        "data_dir": data_dir,
        "data_upload_dir": data_upload_dir,
        "api_key": "test-key",
        "max_upload_bytes": 1024,
        "max_timeout_seconds": 2,
        "cleanup_interval_seconds": 60,
    }
    values.update(overrides)
    return TestClient(create_app(Settings(**values)))


def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


def upload(client: TestClient, filename: str, source: str):
    return client.post(
        "/upload",
        headers=auth_headers(),
        files={"file": (filename, source.encode("utf-8"), "text/x-python")},
    )


def upload_data_file(client: TestClient, filename: str, content: bytes, **headers: str):
    return client.post(
        "/data-files",
        headers={**auth_headers(), **headers},
        files={"file": (filename, content, "text/csv")},
    )


def execution_body(identity: dict[str, str], **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        **identity,
        "data_file": "input.csv",
        "parameters": dict(RUNTIME_PARAMETERS),
    }
    body.update(overrides)
    return body


def assert_error(response, code: str) -> None:
    payload = response.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message"}
    assert payload["error"]["code"] == code
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]


def test_health_does_not_require_authentication(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_business_endpoints_require_valid_api_key(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        missing = client.post("/upload", files={"file": ("a.py", b"pass")})
        wrong = client.post(
            "/upload",
            headers={"X-API-Key": "wrong"},
            files={"file": ("a.py", b"pass")},
        )
        missing_data_file = client.post(
            "/data-files",
            files={"file": ("quotes.csv", b"a,b\n1,2\n", "text/csv")},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing_data_file.status_code == 401
    assert_error(missing, "invalid_api_key")
    assert_error(wrong, "invalid_api_key")
    assert_error(missing_data_file, "invalid_api_key")


def test_data_file_upload_succeeds_and_can_be_used_by_execute(tmp_path: Path) -> None:
    content = b"date,close\n2026-07-27,123.45\n"
    request_id = "market-upload-request"
    with make_client(tmp_path) as client:
        uploaded = upload_data_file(
            client,
            "bond-bars.csv",
            content,
            **{"X-Request-ID": request_id},
        )
        identity = upload(
            client,
            "read_uploaded_csv.py",
            "import sys\nfrom pathlib import Path\nprint(Path(sys.argv[1]).read_text(), end='')",
        ).json()
        executed = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, data_file=uploaded.json()["data_file"]),
        )

    assert uploaded.status_code == 201
    payload = uploaded.json()
    assert set(payload) == {"data_file", "size_bytes", "request_id"}
    assert payload["data_file"].startswith("uploaded/")
    assert payload["data_file"].endswith(".csv")
    assert "bond-bars" not in payload["data_file"]
    assert payload["size_bytes"] == len(content)
    assert payload["request_id"] == request_id
    assert uploaded.headers["X-Request-ID"] == request_id
    assert executed.status_code == 200
    assert executed.json()["stdout"] == content.decode("utf-8")


def test_execute_copies_signal_files_and_passes_read_only_manifest(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        market = upload_data_file(client, "market.csv", b"date,close\n2026-07-31,100\n")
        signal = upload_data_file(
            client,
            "omo.csv",
            b"operationDate,operationAmount\n2026-07-31,500\n",
        )
        identity = upload(
            client,
            "read_signals.py",
            (
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                "payload = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))\n"
                "item = payload['series'][0]\n"
                "print(item['alias'])\n"
                "print(Path(item['csv_path']).read_text(encoding='utf-8'), end='')\n"
            ),
        ).json()
        executed = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                identity,
                data_file=market.json()["data_file"],
                signal_files=[
                    {
                        "alias": "omo",
                        "dataset": "money_market_omo_info",
                        "data_file": signal.json()["data_file"],
                        "date_field": "operationDate",
                        "time_field": None,
                        "availability_lag_days": 1,
                    }
                ],
            ),
        )

    assert executed.status_code == 200
    assert executed.json()["stdout"] == ("omo\noperationDate,operationAmount\n2026-07-31,500\n")


def test_execute_copies_additional_execution_files_into_portfolio_manifest(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        primary = upload_data_file(
            client,
            "bond.csv",
            b"issue_time,open_net_price\n2026-07-31T09:31:00+08:00,100\n",
        )
        future = upload_data_file(
            client,
            "future.csv",
            b"issue_time,open_price\n2026-07-31T09:31:00+08:00,105\n",
        )
        identity = upload(
            client,
            "read_portfolio.py",
            (
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                "payload = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))\n"
                "leg = payload['execution_legs'][0]\n"
                "print(payload['primary_alias'])\n"
                "print(leg['alias'], leg['dataset'], "
                "leg['contract_multiplier'], leg['margin_rate'])\n"
                "print(Path(leg['csv_path']).read_text(encoding='utf-8'), end='')\n"
            ),
        ).json()
        executed = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                identity,
                data_file=primary.json()["data_file"],
                primary_alias="bond",
                execution_files=[
                    {
                        "alias": "future",
                        "dataset": "futures_bars",
                        "data_file": future.json()["data_file"],
                        "contract_multiplier": "10000",
                        "margin_rate": "0.02",
                    }
                ],
            ),
        )

    assert executed.status_code == 200
    assert executed.json()["stdout"] == (
        "bond\n"
        "future futures_bars 10000 0.02\n"
        "issue_time,open_price\n2026-07-31T09:31:00+08:00,105\n"
    )


def test_data_file_upload_rejects_non_csv_and_oversized_file(tmp_path: Path) -> None:
    with make_client(tmp_path, max_data_file_bytes=4) as client:
        invalid = upload_data_file(client, "quotes.txt", b"1234")
        too_large = upload_data_file(client, "quotes.csv", b"12345")
        upload_root = client.app.state.settings.data_upload_dir

    assert invalid.status_code == 400
    assert too_large.status_code == 413
    assert_error(invalid, "data_file_upload_invalid")
    assert_error(too_large, "data_file_upload_too_large")
    assert not list(upload_root.rglob("*.part"))
    assert not list(upload_root.rglob("*.csv"))


def test_data_file_upload_rejects_missing_file_with_stable_error(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/data-files", headers=auth_headers())

    assert response.status_code == 400
    assert_error(response, "data_file_upload_invalid")


def test_data_file_upload_failure_does_not_leak_server_path(tmp_path: Path) -> None:
    class BrokenDataFileStorage:
        async def save(self, _file):
            raise OSError(f"cannot write {tmp_path.resolve()}")

    with make_client(tmp_path) as client:
        client.app.state.data_files = BrokenDataFileStorage()
        response = upload_data_file(client, "quotes.csv", b"a,b\n1,2\n")

    assert response.status_code == 500
    assert_error(response, "data_file_upload_failed")
    assert str(tmp_path.resolve()) not in response.text
    assert "Traceback" not in response.text


def test_data_file_upload_generates_unique_server_names(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = upload_data_file(client, "same.csv", b"first")
        second = upload_data_file(client, "same.csv", b"second")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data_file"] != second.json()["data_file"]


def test_data_file_upload_openapi_contract(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        openapi = client.app.openapi()

    operation = openapi["paths"]["/data-files"]["post"]
    response_ref = operation["responses"]["201"]["content"]["application/json"]["schema"]["$ref"]
    response_schema = openapi["components"]["schemas"][response_ref.rsplit("/", 1)[-1]]
    request_schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]

    assert set(response_schema["required"]) == {"data_file", "size_bytes", "request_id"}
    assert request_schema["$ref"].rsplit("/", 1)[-1] in openapi["components"]["schemas"]


def test_upload_and_execute_preserve_date_filename_contract(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        uploaded = upload(
            client,
            "random_123.py",
            "import sys\nfrom pathlib import Path\nprint(Path(sys.argv[1]).read_text(), end='')",
        )
        identity = uploaded.json()
        body = execution_body(identity)
        executed = client.post("/execute", headers=auth_headers(), json=body)
        repeated = client.post("/execute", headers=auth_headers(), json=body)

    assert uploaded.status_code == 201
    assert set(identity) == {"date", "filename"}
    assert identity["filename"] == "random_123.py"
    assert executed.status_code == 200
    assert executed.json() == {
        "stdout": "date,close\n2026-07-21,101.5\n",
        "stderr": "",
        "exit_code": 0,
        "status": "success",
    }
    assert repeated.status_code == 200
    assert repeated.json() == executed.json()
    assert (tmp_path / identity["date"] / identity["filename"]).exists()


def test_execute_passes_isolated_parameters_file_as_second_script_argument(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        identity = upload(
            client,
            "read_parameters.py",
            (
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                "parameter_file = Path(sys.argv[2])\n"
                "print(json.dumps({"
                "'parameters': json.loads(parameter_file.read_text(encoding='utf-8')),"
                "'path': str(parameter_file)"
                "}))"
            ),
        ).json()
        response = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity),
        )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "success"
    payload = json.loads(result["stdout"])
    assert payload["parameters"] == RUNTIME_PARAMETERS
    parameter_path = Path(payload["path"])
    assert parameter_path.name == "parameters.json"
    assert not parameter_path.exists()
    assert not (tmp_path / "parameters.json").exists()
    assert not (tmp_path / "data" / "parameters.json").exists()


def test_execute_rejects_missing_or_unknown_runtime_parameter_fields(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        identity = upload(client, "parameter_shape.py", "print('must not run')").json()
        missing_parameters = client.post(
            "/execute",
            headers=auth_headers(),
            json={**identity, "data_file": "input.csv"},
        )
        missing_field = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                identity,
                parameters={
                    key: value for key, value in RUNTIME_PARAMETERS.items() if key != "fee_rate"
                },
            ),
        )
        unknown_parameter = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                identity,
                parameters={**RUNTIME_PARAMETERS, "benchmark": "000300.SH"},
            ),
        )
        unknown_request_field = client.post(
            "/execute",
            headers=auth_headers(),
            json={**execution_body(identity), "token": "not-supported"},
        )

    for response in (
        missing_parameters,
        missing_field,
        unknown_parameter,
        unknown_request_field,
    ):
        assert response.status_code == 400
        assert_error(response, "runtime_parameters_invalid")
        assert str(tmp_path) not in response.text
        assert "Traceback" not in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_capital", 1000000),
        ("initial_capital", ""),
        ("initial_capital", "NaN"),
        ("initial_capital", "Infinity"),
        ("initial_capital", "not-decimal"),
        ("initial_capital", "0"),
        ("fee_rate", "-0.0001"),
        ("fee_rate", "1"),
        ("slippage_rate", "-1"),
        ("slippage_rate", "-1.01"),
        ("slippage_rate", "NaN"),
        ("slippage_rate", "-Infinity"),
        ("slippage_rate", -0.001),
        ("slippage_rate", "1"),
        ("max_drawdown_limit_rate", "0"),
        ("max_drawdown_limit_rate", "1.0001"),
    ],
)
def test_execute_rejects_invalid_runtime_parameter_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    with make_client(tmp_path) as client:
        identity = upload(client, f"invalid_{field}.py", "print('must not run')").json()
        response = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                identity,
                parameters={**RUNTIME_PARAMETERS, field: value},
            ),
        )

    assert response.status_code == 400
    assert_error(response, "runtime_parameters_invalid")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_capital", "0.0001"),
        ("fee_rate", "0"),
        ("fee_rate", "0.999999"),
        ("slippage_rate", "0"),
        ("slippage_rate", "-0.001"),
        ("slippage_rate", "-0.999999"),
        ("slippage_rate", "0.999999"),
        ("max_drawdown_limit_rate", "0.0001"),
        ("max_drawdown_limit_rate", "1"),
    ],
)
def test_execute_accepts_runtime_parameter_boundary_values(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    with make_client(tmp_path) as client:
        identity = upload(
            client,
            f"valid_{field}.py",
            "import json, sys\n"
            "with open(sys.argv[2], encoding='utf-8') as f:\n"
            f"    print(json.load(f)[{field!r}])\n",
        ).json()
        response = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                identity,
                parameters={**RUNTIME_PARAMETERS, field: value},
            ),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["exit_code"] == 0
    assert response.json()["stdout"].strip() == value


def test_execute_accepts_complete_futures_runtime_parameters(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        identity = upload(client, "futures_parameters.py", "print('ok')").json()
        response = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, parameters=FUTURES_RUNTIME_PARAMETERS),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "success"


@pytest.mark.parametrize(
    "parameters",
    [
        {**RUNTIME_PARAMETERS, "contract_multiplier": "10000"},
        {**RUNTIME_PARAMETERS, "margin_rate": "0.02"},
        {**FUTURES_RUNTIME_PARAMETERS, "contract_multiplier": "0"},
        {**FUTURES_RUNTIME_PARAMETERS, "margin_rate": "0"},
        {**FUTURES_RUNTIME_PARAMETERS, "margin_rate": "1.01"},
    ],
)
def test_execute_rejects_incomplete_or_invalid_futures_runtime_parameters(
    tmp_path: Path,
    parameters: dict[str, str],
) -> None:
    with make_client(tmp_path) as client:
        identity = upload(client, "invalid_futures_parameters.py", "print('must not run')").json()
        response = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, parameters=parameters),
        )

    assert response.status_code == 400
    assert_error(response, "runtime_parameters_invalid")


def test_execute_openapi_requires_strict_runtime_parameters_object(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        openapi = client.app.openapi()

    execute_schema = openapi["components"]["schemas"]["ExecuteRequest"]
    parameter_ref = execute_schema["properties"]["parameters"]["$ref"]
    parameter_schema = openapi["components"]["schemas"][parameter_ref.rsplit("/", 1)[-1]]

    assert execute_schema["additionalProperties"] is False
    assert "parameters" in execute_schema["required"]
    assert parameter_schema["additionalProperties"] is False
    assert set(parameter_schema["required"]) == set(RUNTIME_PARAMETERS)
    assert {
        name: field_schema["type"]
        for name, field_schema in parameter_schema["properties"].items()
        if name in RUNTIME_PARAMETERS
    } == {name: "string" for name in RUNTIME_PARAMETERS}
    assert set(parameter_schema["properties"]) == {
        *RUNTIME_PARAMETERS,
        "contract_multiplier",
        "margin_rate",
    }


def test_upload_rejects_invalid_duplicate_and_large_files(tmp_path: Path) -> None:
    with make_client(tmp_path, max_upload_bytes=4) as client:
        invalid = upload(client, "../unsafe.py", "pass")
        first = upload(client, "same.py", "1234")
        duplicate = upload(client, "same.py", "pass")
        large = upload(client, "large.py", "12345")

    assert invalid.status_code == 400
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert large.status_code == 413
    assert_error(invalid, "upload_filename_invalid")
    assert_error(duplicate, "upload_duplicate")
    assert_error(large, "upload_too_large")


def test_execute_rejects_invalid_missing_and_excessive_timeout(tmp_path: Path) -> None:
    with make_client(tmp_path, max_timeout_seconds=2) as client:
        invalid = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                {"date": "../2026-07-17", "filename": "x.py"},
            ),
        )
        missing = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                {"date": "2026-07-17", "filename": "missing.py"},
            ),
        )
        uploaded = upload(client, "slow.py", "pass").json()
        excessive = client.post(
            "/execute", headers=auth_headers(), json=execution_body(uploaded, timeout=3)
        )

    assert invalid.status_code == 400
    assert missing.status_code == 404
    assert excessive.status_code == 400
    assert_error(invalid, "uploaded_code_path_invalid")
    assert_error(missing, "uploaded_code_not_found")
    assert_error(excessive, "execution_timeout_invalid")
    assert excessive.json()["error"]["message"] == (
        "请求执行超时为 3 秒，超过沙箱允许的最大值 2 秒；代码尚未开始运行"
    )


def test_execute_rejects_unsafe_or_missing_data_paths_without_leaking_root(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        identity = upload(client, "data_paths.py", "pass").json()
        absolute = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(
                identity,
                data_file=str((tmp_path / "data" / "input.csv").resolve()),
            ),
        )
        traversal = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, data_file="../input.csv"),
        )
        uploaded_traversal = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, data_file="uploaded/../input.csv"),
        )
        non_csv = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, data_file="input.txt"),
        )
        missing = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, data_file="missing.csv"),
        )

    assert absolute.status_code == 400
    assert traversal.status_code == 400
    assert non_csv.status_code == 400
    assert missing.status_code == 404
    for response in (absolute, traversal, uploaded_traversal, non_csv):
        assert_error(response, "data_file_path_invalid")
        assert str(tmp_path) not in response.text
    assert_error(missing, "data_file_not_found")
    assert str(tmp_path) not in missing.text


def test_execute_still_supports_built_in_test_data_csv(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        identity = upload(
            client,
            "read_test_data.py",
            "import sys\nfrom pathlib import Path\nprint(Path(sys.argv[1]).read_text(), end='')",
        ).json()
        response = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, data_file="test_data.csv"),
        )

    assert response.status_code == 200
    assert response.json()["stdout"] == "date,close\n2026-07-20,100.0\n"


def test_execute_rejects_windows_unsafe_data_path_segments(tmp_path: Path) -> None:
    unsafe_paths = (
        "market/data:secret.csv",
        "NUL.csv",
        "nested/COM1.csv",
        "nested/AUX.report.csv",
        "nested/NUL .csv",
        "nested/COM1..csv",
        "nested/conin$.csv",
        "CONOUT$.report.csv",
        "nested/COM¹.csv",
        "market/com².report.csv",
        "nested/COM³.csv",
        "market/LPT¹.csv",
        "nested/lpt².report.csv",
        "nested/LPT³ .csv",
        "market/bad?.csv",
        "market/control\x1f.csv",
        "market/delete\x7f.csv",
        "market/nul\x00.csv",
    )
    with make_client(tmp_path) as client:
        identity = upload(client, "unsafe_windows_paths.py", "pass").json()
        responses = [
            client.post(
                "/execute",
                headers=auth_headers(),
                json=execution_body(identity, data_file=data_file),
            )
            for data_file in unsafe_paths
        ]

    for data_file, response in zip(unsafe_paths, responses, strict=True):
        assert response.status_code == 400, data_file
        assert_error(response, "data_file_path_invalid")
        assert str(tmp_path) not in response.text


def test_execute_accepts_safe_unicode_data_subdirectory(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        data_file = tmp_path / "data" / "行情" / "日线.csv"
        data_file.parent.mkdir()
        data_file.write_text("日期,收盘\n2026-07-21,101.5\n", encoding="utf-8")
        identity = upload(
            client,
            "unicode_data.py",
            "import sys\nfrom pathlib import Path\nprint(Path(sys.argv[1]).read_text(), end='')",
        ).json()
        response = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity, data_file="行情/日线.csv"),
        )

    assert response.status_code == 200
    assert response.json()["stdout"] == "日期,收盘\n2026-07-21,101.5\n"


def test_execute_rejects_oversized_data_file_without_leaking_root(tmp_path: Path) -> None:
    with make_client(tmp_path, max_data_file_bytes=8) as client:
        identity = upload(client, "oversized_data.py", "pass").json()
        response = client.post(
            "/execute",
            headers=auth_headers(),
            json=execution_body(identity),
        )

    assert response.status_code == 413
    assert_error(response, "data_file_too_large")
    assert str(tmp_path) not in response.text


def test_execute_reports_nonzero_exit_as_error(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        identity = upload(client, "error.py", "raise RuntimeError('bad')").json()
        response = client.post("/execute", headers=auth_headers(), json=execution_body(identity))

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["exit_code"] != 0
    assert "RuntimeError: bad" in response.json()["stderr"]


def test_upload_can_be_executed_twice_concurrently(tmp_path: Path) -> None:
    with make_client(tmp_path, max_concurrent=2) as client:
        identity = upload(client, "repeatable.py", "import time\ntime.sleep(0.3)").json()

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                client.post,
                "/execute",
                headers=auth_headers(),
                json=execution_body(identity),
            )
            for _ in range(100):
                if client.app.state.gate.active == 1:
                    break
                time.sleep(0.005)
            repeated = client.post(
                "/execute", headers=auth_headers(), json=execution_body(identity)
            )
            assert first.result().status_code == 200

    assert repeated.status_code == 200
    assert (tmp_path / identity["date"] / identity["filename"]).exists()


def test_concurrent_executions_use_independent_parameter_files(tmp_path: Path) -> None:
    with make_client(tmp_path, max_concurrent=2) as client:
        identity = upload(
            client,
            "concurrent_parameters.py",
            (
                "import json\n"
                "import sys\n"
                "import time\n"
                "from pathlib import Path\n"
                "time.sleep(0.2)\n"
                "print(Path(sys.argv[2]).read_text(encoding='utf-8'))"
            ),
        ).json()
        first_parameters = {**RUNTIME_PARAMETERS, "initial_capital": "1000000.00"}
        second_parameters = {**RUNTIME_PARAMETERS, "initial_capital": "2000000.00"}

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                client.post,
                "/execute",
                headers=auth_headers(),
                json=execution_body(identity, parameters=first_parameters),
            )
            second = pool.submit(
                client.post,
                "/execute",
                headers=auth_headers(),
                json=execution_body(identity, parameters=second_parameters),
            )
            responses = (first.result(), second.result())

    assert [response.status_code for response in responses] == [200, 200]
    assert [json.loads(response.json()["stdout"]) for response in responses] == [
        first_parameters,
        second_parameters,
    ]
    assert list(tmp_path.rglob("parameters.json")) == []


def test_execute_returns_429_when_execution_queue_is_full(tmp_path: Path) -> None:
    with make_client(
        tmp_path,
        max_concurrent=1,
        max_waiting=0,
        queue_wait_seconds=0.1,
    ) as client:
        slow_identity = upload(client, "slow.py", "import time\ntime.sleep(0.5)").json()
        queued_identity = upload(client, "queued.py", "pass").json()

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                client.post,
                "/execute",
                headers=auth_headers(),
                json=execution_body(slow_identity, timeout=1),
            )
            for _ in range(100):
                if client.app.state.gate.active == 1:
                    break
                time.sleep(0.005)
            second = client.post(
                "/execute",
                headers=auth_headers(),
                json=execution_body(queued_identity, timeout=1),
            )
            assert first.result().status_code == 200

    assert second.status_code == 429
    assert_error(second, "sandbox_busy")


def test_unexpected_execute_error_does_not_leak_traceback(tmp_path: Path) -> None:
    class BrokenExecutor:
        async def execute(self, script: Path, data_file: Path, parameters, timeout: float):
            raise RuntimeError(f"secret path: {script}")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "input.csv").write_text("value\n1\n", encoding="utf-8")
    app = create_app(
        Settings(
            upload_dir=tmp_path,
            data_dir=data_dir,
            api_key="test-key",
            cleanup_interval_seconds=60,
        )
    )
    app.state.executor = BrokenExecutor()
    with TestClient(app) as client:
        identity = upload(client, "broken.py", "pass").json()
        response = client.post("/execute", headers=auth_headers(), json=execution_body(identity))

    assert response.status_code == 500
    assert_error(response, "sandbox_execution_failed")
    assert str(tmp_path) not in response.text
    assert "Traceback" not in response.text
    assert (tmp_path / identity["date"] / identity["filename"]).exists()
