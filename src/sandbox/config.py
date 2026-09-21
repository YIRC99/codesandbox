import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_upload_dir() -> Path:
    return Path("uploads").resolve()


def _default_data_dir() -> Path:
    return Path("data").resolve()


def _default_data_upload_dir() -> Path:
    return Path("data-files").resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    upload_dir: Path = field(default_factory=_default_upload_dir)
    data_dir: Path = field(default_factory=_default_data_dir)
    data_upload_dir: Path = field(default_factory=_default_data_upload_dir)
    api_key: str = ""
    max_upload_bytes: int = 10 * 1024 * 1024
    max_data_file_bytes: int = 100 * 1024 * 1024
    max_output_bytes: int = 10 * 1024 * 1024
    max_timeout_seconds: int = 240
    max_concurrent: int = 4
    max_waiting: int = 20
    queue_wait_seconds: float = 5.0
    retention_days: int = 30
    cleanup_interval_seconds: float = 3600.0
    process_cpu_seconds: int = 60
    process_memory_bytes: int = 1536 * 1024 * 1024
    process_count_limit: int = 32
    process_file_bytes: int = 20 * 1024 * 1024
    process_open_files: int = 64
    terminate_grace_seconds: float = 1.0
    analysis_result_dir: Path = field(default_factory=lambda: Path("analysis-results").resolve())
    analysis_callback_urls: tuple[str, ...] = ()
    analysis_max_result_bytes: int = 20 * 1024 * 1024
    analysis_max_storage_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "upload_dir", Path(self.upload_dir).resolve())
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        object.__setattr__(self, "data_upload_dir", Path(self.data_upload_dir).resolve())
        object.__setattr__(self, "analysis_result_dir", Path(self.analysis_result_dir).resolve())
        positive_fields = (
            "max_upload_bytes",
            "max_data_file_bytes",
            "max_output_bytes",
            "max_timeout_seconds",
            "max_concurrent",
            "queue_wait_seconds",
            "retention_days",
            "cleanup_interval_seconds",
            "process_cpu_seconds",
            "process_memory_bytes",
            "process_count_limit",
            "process_file_bytes",
            "process_open_files",
            "terminate_grace_seconds",
            "analysis_max_result_bytes",
            "analysis_max_storage_bytes",
        )
        for name in positive_fields:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.max_waiting < 0:
            raise ValueError("max_waiting must be zero or greater")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upload_dir=Path(os.getenv("SANDBOX_UPLOAD_DIR", "uploads")),
            data_dir=Path(os.getenv("SANDBOX_DATA_DIR", "data")),
            data_upload_dir=Path(os.getenv("SANDBOX_DATA_UPLOAD_DIR", "data-files")),
            api_key=os.getenv("SANDBOX_API_KEY", ""),
            max_upload_bytes=_env_int("SANDBOX_MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
            max_data_file_bytes=_env_int("SANDBOX_MAX_DATA_FILE_BYTES", 100 * 1024 * 1024),
            max_output_bytes=_env_int("SANDBOX_MAX_OUTPUT_BYTES", 10 * 1024 * 1024),
            max_timeout_seconds=_env_int("SANDBOX_MAX_TIMEOUT_SECONDS", 240),
            max_concurrent=_env_int("SANDBOX_MAX_CONCURRENT", 4),
            max_waiting=_env_int("SANDBOX_MAX_WAITING", 20),
            queue_wait_seconds=_env_float("SANDBOX_QUEUE_WAIT_SECONDS", 5.0),
            retention_days=_env_int("SANDBOX_RETENTION_DAYS", 30),
            cleanup_interval_seconds=_env_float("SANDBOX_CLEANUP_INTERVAL_SECONDS", 3600.0),
            process_cpu_seconds=_env_int("SANDBOX_PROCESS_CPU_SECONDS", 60),
            process_memory_bytes=_env_int("SANDBOX_PROCESS_MEMORY_BYTES", 1536 * 1024 * 1024),
            process_count_limit=_env_int("SANDBOX_PROCESS_COUNT_LIMIT", 32),
            process_file_bytes=_env_int("SANDBOX_PROCESS_FILE_BYTES", 20 * 1024 * 1024),
            process_open_files=_env_int("SANDBOX_PROCESS_OPEN_FILES", 64),
            terminate_grace_seconds=_env_float("SANDBOX_TERMINATE_GRACE_SECONDS", 1.0),
            analysis_result_dir=Path(os.getenv("SANDBOX_ANALYSIS_RESULT_DIR", "analysis-results")),
            analysis_callback_urls=tuple(
                filter(
                    None,
                    (
                        value.strip()
                        for value in os.getenv("SANDBOX_ANALYSIS_CALLBACK_URLS", "").split(",")
                    ),
                )
            ),
            analysis_max_result_bytes=_env_int(
                "SANDBOX_ANALYSIS_MAX_RESULT_BYTES", 20 * 1024 * 1024
            ),
            analysis_max_storage_bytes=_env_int("SANDBOX_ANALYSIS_MAX_STORAGE_BYTES", 1024**3),
        )


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))
