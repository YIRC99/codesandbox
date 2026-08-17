from pathlib import Path

import pytest

from sandbox.config import Settings


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SANDBOX_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(tmp_path / "market-data"))
    monkeypatch.setenv("SANDBOX_DATA_UPLOAD_DIR", str(tmp_path / "uploaded-market-data"))
    monkeypatch.setenv("SANDBOX_API_KEY", "secret")
    monkeypatch.setenv("SANDBOX_MAX_UPLOAD_BYTES", "2048")
    monkeypatch.setenv("SANDBOX_MAX_DATA_FILE_BYTES", "4096")
    monkeypatch.setenv("SANDBOX_MAX_CONCURRENT", "3")

    settings = Settings.from_env()

    assert settings.upload_dir == tmp_path.resolve()
    assert settings.data_dir == (tmp_path / "market-data").resolve()
    assert settings.data_upload_dir == (tmp_path / "uploaded-market-data").resolve()
    assert settings.api_key == "secret"
    assert settings.max_upload_bytes == 2048
    assert settings.max_data_file_bytes == 4096
    assert settings.max_concurrent == 3


def test_default_retention_is_30_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_RETENTION_DAYS", raising=False)

    assert Settings().retention_days == 30
    assert Settings.from_env().retention_days == 30


def test_default_max_timeout_is_240_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_MAX_TIMEOUT_SECONDS", raising=False)

    assert Settings().max_timeout_seconds == 240
    assert Settings.from_env().max_timeout_seconds == 240


def test_default_data_directory_is_absolute_project_data_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_DATA_DIR", raising=False)

    assert Settings().data_dir == Path("data").resolve()
    assert Settings.from_env().data_dir == Path("data").resolve()


def test_default_data_upload_directory_is_absolute_data_files_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_DATA_UPLOAD_DIR", raising=False)

    assert Settings().data_upload_dir == Path("data-files").resolve()
    assert Settings.from_env().data_upload_dir == Path("data-files").resolve()


def test_default_output_limit_is_10_mib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_MAX_OUTPUT_BYTES", raising=False)

    assert Settings().max_output_bytes == 10_485_760
    assert Settings.from_env().max_output_bytes == 10_485_760


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_upload_bytes", 0),
        ("max_data_file_bytes", 0),
        ("max_output_bytes", 0),
        ("max_timeout_seconds", 0),
        ("max_concurrent", 0),
        ("max_waiting", -1),
        ("queue_wait_seconds", 0),
        ("retention_days", 0),
    ],
)
def test_settings_reject_invalid_limits(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=field):
        Settings(**{field: value})
