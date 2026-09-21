import os
import runpy
import shutil
from pathlib import Path

import pytest
import uvicorn

from sandbox.config import Settings


@pytest.mark.parametrize(
    "existing_callback", [None, "http://configured/v1/internal/analysis-data/query"]
)
def test_local_entrypoint_loads_project_env_and_preserves_process_environment(
    tmp_path, monkeypatch, existing_callback
):
    entrypoint = tmp_path / "main.py"
    shutil.copyfile(Path(__file__).parents[1] / "main.py", entrypoint)
    callback = "http://127.0.0.1:32001/v1/internal/analysis-data/query"
    (tmp_path / ".env").write_text(
        f"SANDBOX_API_KEY=\nSANDBOX_ANALYSIS_CALLBACK_URLS={callback}\n", encoding="utf-8"
    )
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("SANDBOX_ANALYSIS_CALLBACK_URLS", raising=False)
    if existing_callback:
        monkeypatch.setenv("SANDBOX_ANALYSIS_CALLBACK_URLS", existing_callback)
    calls = []

    def check_startup(app, **kwargs):
        assert Path(kwargs["env_file"]) == tmp_path / ".env"
        uvicorn.Config(app, **kwargs)
        settings = Settings.from_env()
        assert settings.api_key == ""
        assert settings.analysis_callback_urls == (existing_callback or callback,)
        calls.append(app)

    monkeypatch.setattr(uvicorn, "run", check_startup)
    runpy.run_path(str(entrypoint), run_name="__main__")
    assert calls == ["src.sandbox.main:app"]


def test_local_entrypoint_without_env_file_keeps_existing_startup(tmp_path, monkeypatch):
    entrypoint = tmp_path / "main.py"
    shutil.copyfile(Path(__file__).parents[1] / "main.py", entrypoint)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    runpy.run_path(str(entrypoint), run_name="__main__")
    assert calls[0]["env_file"] is None
