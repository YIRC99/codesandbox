import asyncio
import hmac
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)
from starlette.responses import JSONResponse

from .admission import ExecutionGate, SandboxBusyError
from .analysis import install_analysis_routes
from .config import Settings
from .executor import ExecutionFileInput, SandboxExecutor, SignalFileInput
from .storage import (
    DataFileStorage,
    DataFileTooLargeError,
    DuplicateUploadError,
    InvalidPathError,
    UploadStorage,
    UploadTooLargeError,
)


class InterceptHandler(logging.Handler):
    """将 Python 标准 logging 模块的日志重定向到 Loguru 处理器"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_loguru_logging(logs_dir: Path | None = None) -> None:
    """初始化 Loguru 日志系统，自动创建 logs 文件夹并配置多级别按天日志文件"""
    if logs_dir is None:
        env_log_dir = os.getenv("SANDBOX_LOG_DIR")
        if env_log_dir:
            logs_dir = Path(env_log_dir)
        else:
            default_dir = Path("logs")
            try:
                default_dir.mkdir(parents=True, exist_ok=True)
                logs_dir = default_dir
            except OSError:
                # 兼容容器环境只读文件系统 (Read-only rootfs)，自动降级回退到可写的 /tmp/logs
                logs_dir = Path("/tmp/logs")

    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_logging_enabled = True
    except OSError:
        file_logging_enabled = False

    logger.remove()

    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # 1. 控制台标准输出
    logger.add(sys.stderr, format=log_format, level="INFO")

    if file_logging_enabled:
        # 2. INFO 级别按天日志文件 (如 logs/info-2026-7-21.log)
        logger.add(
            sink=str(logs_dir / "info-{time:YYYY-M-D}.log"),
            format=log_format,
            filter=lambda r: r["level"].name == "INFO",
            rotation="00:00",
            retention="30 days",
            encoding="utf-8",
            enqueue=True,
        )

        # 3. WARNING 级别按天日志文件 (如 logs/warning-2026-7-21.log)
        logger.add(
            sink=str(logs_dir / "warning-{time:YYYY-M-D}.log"),
            format=log_format,
            filter=lambda r: r["level"].name == "WARNING",
            rotation="00:00",
            retention="30 days",
            encoding="utf-8",
            enqueue=True,
        )

        # 4. ERROR 及以上级别按天日志文件 (如 logs/error-2026-7-21.log)
        logger.add(
            sink=str(logs_dir / "error-{time:YYYY-M-D}.log"),
            format=log_format,
            filter=lambda r: r["level"].no >= 40,
            rotation="00:00",
            retention="30 days",
            encoding="utf-8",
            enqueue=True,
        )

    # 拦截标准库 logging 模块（包含 FastAPI 和 Uvicorn）
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        mod_logger = logging.getLogger(logger_name)
        mod_logger.handlers = [InterceptHandler()]
        mod_logger.propagate = False


# 执行全局 Loguru 配置
setup_loguru_logging()


class BacktestRuntimeParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_capital: StrictStr
    fee_rate: StrictStr
    slippage_rate: StrictStr = Field(
        description="Signed slippage rate: -1 < value < 1; negative values improve fill prices.",
        examples=["0.0005", "-0.001"],
    )
    max_drawdown_limit_rate: StrictStr
    contract_multiplier: StrictStr | None = None
    margin_rate: StrictStr | None = None

    @field_validator(
        "initial_capital",
        "fee_rate",
        "slippage_rate",
        "max_drawdown_limit_rate",
        "contract_multiplier",
        "margin_rate",
    )
    @classmethod
    def validate_decimal_string(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("runtime parameter must not be blank")
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("runtime parameter must be a valid decimal string") from exc
        if not decimal_value.is_finite():
            raise ValueError("runtime parameter must be finite")
        if info.field_name == "initial_capital" and decimal_value <= Decimal("0"):
            raise ValueError("initial_capital must be greater than 0")
        if info.field_name == "fee_rate" and not (Decimal("0") <= decimal_value < Decimal("1")):
            raise ValueError(f"{info.field_name} must satisfy 0 <= value < 1")
        if info.field_name == "slippage_rate" and not (
            Decimal("-1") < decimal_value < Decimal("1")
        ):
            raise ValueError("slippage_rate must satisfy -1 < value < 1")
        if info.field_name == "max_drawdown_limit_rate" and not (
            Decimal("0") < decimal_value <= Decimal("1")
        ):
            raise ValueError("max_drawdown_limit_rate must satisfy 0 < value <= 1")
        if info.field_name == "contract_multiplier" and decimal_value <= Decimal("0"):
            raise ValueError("contract_multiplier must be greater than 0")
        if info.field_name == "margin_rate" and not (Decimal("0") < decimal_value <= Decimal("1")):
            raise ValueError("margin_rate must satisfy 0 < value <= 1")
        return value

    @model_validator(mode="after")
    def validate_futures_parameter_pair(self) -> "BacktestRuntimeParameters":
        if (self.contract_multiplier is None) != (self.margin_rate is None):
            raise ValueError("contract_multiplier and margin_rate must be provided together")
        return self


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str = Field(description="Upload date in YYYY-MM-DD format")
    filename: str = Field(description="Uploaded Python filename")
    data_file: str = Field(description="CSV path relative to the configured data directory")
    signal_files: list["SignalFileRequest"] = Field(default_factory=list, max_length=8)
    primary_alias: str = Field(
        default="primary",
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$",
    )
    execution_files: list["ExecutionFileRequest"] = Field(default_factory=list, max_length=7)
    parameters: BacktestRuntimeParameters = Field(
        description="Strict string-valued backtest runtime parameters"
    )
    timeout: float | None = Field(default=None, gt=0, description="Timeout in seconds")

    @model_validator(mode="after")
    def validate_signal_aliases(self) -> "ExecuteRequest":
        aliases = [item.alias for item in self.signal_files]
        if len(set(aliases)) != len(aliases):
            raise ValueError("signal file aliases must be unique")
        execution_aliases = [self.primary_alias] + [item.alias for item in self.execution_files]
        if len(set(execution_aliases)) != len(execution_aliases):
            raise ValueError("execution file aliases must be unique")
        return self


class SignalFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    dataset: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    data_file: str
    date_field: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    time_field: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$",
    )
    availability_lag_days: int = Field(ge=0, le=30)


class ExecutionFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    dataset: str = Field(pattern=r"^(bond_bars|futures_bars)$")
    data_file: str
    contract_multiplier: StrictStr | None = None
    margin_rate: StrictStr | None = None

    @model_validator(mode="after")
    def validate_market_parameters(self) -> "ExecutionFileRequest":
        if self.dataset == "futures_bars":
            if self.contract_multiplier is None or self.margin_rate is None:
                raise ValueError("futures execution file requires multiplier and margin")
        elif self.contract_multiplier is not None or self.margin_rate is not None:
            raise ValueError("bond execution file does not accept futures parameters")
        return self


ExecuteRequest.model_rebuild()


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    status: str


class DataFileUploadResponse(BaseModel):
    data_file: str
    size_bytes: int
    request_id: str


class SandboxErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable sandbox error code")
    message: str = Field(description="Safe human-readable error message")


class SandboxErrorResponse(BaseModel):
    error: SandboxErrorDetail


class SandboxHTTPError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


async def _cleanup_loop(
    storage: UploadStorage | DataFileStorage,
    interval: float,
) -> None:
    logger.info("启动后台文件清理循环任务 (间隔时间: %.1f秒)", interval)
    while True:
        try:
            removed = await asyncio.to_thread(storage.cleanup_expired)
            if removed:
                logger.info("已清理 %s 个过期的沙箱上传文件/目录", removed)
        except Exception:
            logger.exception("后台文件清理循环任务遇到未预期异常")
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    logger.info(
        "初始化沙箱服务配置：最大并发=%d 最大排队=%d 最大超时时间=%d秒",
        resolved_settings.max_concurrent,
        resolved_settings.max_waiting,
        resolved_settings.max_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logger.info("正在启动沙箱服务生命周期...")
        cleanup_tasks = [
            asyncio.create_task(_cleanup_loop(storage, resolved_settings.cleanup_interval_seconds))
            for storage in (
                application.state.storage,
                application.state.data_files,
                application.state.analysis.store,
            )
        ]
        try:
            yield
        finally:
            logger.info("正在关闭沙箱服务生命周期...")
            await application.state.analysis.close()
            for cleanup_task in cleanup_tasks:
                cleanup_task.cancel()
            for cleanup_task in cleanup_tasks:
                with suppress(asyncio.CancelledError):
                    await cleanup_task

    application = FastAPI(
        title="Python Code Sandbox",
        description="Resource-bounded service for running AI-generated Python code.",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.storage = UploadStorage(resolved_settings)
    application.state.data_files = DataFileStorage(resolved_settings)
    application.state.executor = SandboxExecutor(resolved_settings)
    application.state.gate = ExecutionGate(
        max_active=resolved_settings.max_concurrent,
        max_waiting=resolved_settings.max_waiting,
        wait_timeout=resolved_settings.queue_wait_seconds,
    )

    @application.exception_handler(SandboxHTTPError)
    async def sandbox_error_response(
        _request: Request,
        exc: SandboxHTTPError,
    ) -> JSONResponse:
        payload = SandboxErrorResponse(error=SandboxErrorDetail(code=exc.code, message=exc.message))
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(),
            headers=exc.headers,
        )

    @application.exception_handler(RequestValidationError)
    async def request_validation_error_response(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if request.url.path.startswith("/analysis/"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "analysis_request_invalid",
                        "message": "Analysis request is invalid",
                    }
                },
            )
        if request.url.path == "/data-files":
            payload = SandboxErrorResponse(
                error=SandboxErrorDetail(
                    code="data_file_upload_invalid",
                    message="a CSV file is required",
                )
            )
            return JSONResponse(status_code=400, content=payload.model_dump())
        if request.url.path == "/execute":
            logger.warning(
                "代码执行请求参数校验失败 (400)：请求ID=%s 错误数=%d",
                getattr(request.state, "request_id", "unknown"),
                len(exc.errors()),
            )
            payload = SandboxErrorResponse(
                error=SandboxErrorDetail(
                    code="runtime_parameters_invalid",
                    message="backtest runtime parameters are invalid",
                )
            )
            return JSONResponse(status_code=400, content=payload.model_dump())
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors()},
        )

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        start_time = time.monotonic()
        client_ip = request.client.host if request.client else "unknown"
        logger.info(
            f"HTTP 请求开始：方法={request.method} 路径={request.url.path} "
            f"客户端IP={client_ip} 请求ID={request_id}"
        )
        try:
            response = await call_next(request)
            elapsed_ms = (time.monotonic() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            logger.info(
                f"HTTP 请求完成：方法={request.method} 路径={request.url.path} "
                f"状态码={response.status_code} 耗时={elapsed_ms:.2f}ms 请求ID={request_id}"
            )
            return response
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            if request.url.path.startswith("/analysis/"):
                # Analysis frames can contain scoped callback credentials; never log locals.
                logger.error("分析请求发生内部异常：请求ID={}", request_id)
            else:
                logger.exception(
                    f"HTTP 请求异常：方法={request.method} 路径={request.url.path} "
                    f"耗时={elapsed_ms:.2f}ms 请求ID={request_id} 错误={exc}"
                )

            raise

    async def require_api_key(
        request: Request,
        provided_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        expected_key = resolved_settings.api_key
        if expected_key and (
            provided_key is None or not hmac.compare_digest(provided_key, expected_key)
        ):
            client_ip = request.client.host if request.client else "unknown"
            logger.warning(
                f"API Key 鉴权失败：请求ID={request.state.request_id} 客户端IP={client_ip}"
            )
            raise SandboxHTTPError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="invalid_api_key",
                message="invalid API key",
            )

    authenticated = Depends(require_api_key)
    install_analysis_routes(application, resolved_settings, authenticated)

    @application.get("/health", status_code=status.HTTP_200_OK)
    async def health_check() -> dict[str, int | str]:
        active = application.state.gate.active
        waiting = application.state.gate.waiting
        logger.debug(f"健康检查被调用：运行中={active} 排队中={waiting}")
        return {
            "status": "healthy",
            "active_executions": active,
            "waiting_executions": waiting,
        }

    @application.post(
        "/upload",
        status_code=status.HTTP_201_CREATED,
        responses={
            400: {"model": SandboxErrorResponse},
            401: {"model": SandboxErrorResponse},
            409: {"model": SandboxErrorResponse},
            413: {"model": SandboxErrorResponse},
            500: {"model": SandboxErrorResponse},
        },
    )
    async def upload_file(
        file: Annotated[UploadFile, File()],
        request: Request,
        _: Annotated[None, authenticated],
    ) -> dict[str, str]:
        req_id = request.state.request_id
        logger.info(f"收到文件上传：原始文件名={file.filename} 请求ID={req_id}")
        try:
            saved = await application.state.storage.save(file)
            logger.info(
                f"文件上传成功：日期={saved.date} 保存文件名={saved.filename} 请求ID={req_id}"
            )
        except InvalidPathError as exc:
            logger.warning(f"上传被拒绝 (路径格式无效 400)：{exc} 请求ID={req_id}")
            raise SandboxHTTPError(
                status_code=400,
                code="upload_filename_invalid",
                message=str(exc),
            ) from exc
        except DuplicateUploadError as exc:
            logger.warning(f"上传被拒绝 (文件重复 409)：{exc} 请求ID={req_id}")
            raise SandboxHTTPError(
                status_code=409,
                code="upload_duplicate",
                message=str(exc),
            ) from exc
        except UploadTooLargeError as exc:
            logger.warning(f"上传被拒绝 (文件过大 413)：{exc} 请求ID={req_id}")
            raise SandboxHTTPError(
                status_code=413,
                code="upload_too_large",
                message=str(exc),
            ) from exc
        except Exception as exc:
            logger.exception(f"文件上传发生未知异常 (500) 请求ID={req_id}")
            raise SandboxHTTPError(
                status_code=500,
                code="upload_failed",
                message="upload failed",
            ) from exc
        finally:
            await file.close()
        return {"date": saved.date, "filename": saved.filename}

    @application.post(
        "/data-files",
        status_code=status.HTTP_201_CREATED,
        response_model=DataFileUploadResponse,
        responses={
            400: {"model": SandboxErrorResponse},
            401: {"model": SandboxErrorResponse},
            413: {"model": SandboxErrorResponse},
            500: {"model": SandboxErrorResponse},
        },
    )
    async def upload_data_file(
        file: Annotated[UploadFile, File()],
        request: Request,
        _: Annotated[None, authenticated],
    ) -> DataFileUploadResponse:
        request_id = request.state.request_id
        try:
            saved = await application.state.data_files.save(file)
        except InvalidPathError as exc:
            logger.warning(f"行情数据文件上传被拒绝：请求ID={request_id} 原因=文件类型无效")
            raise SandboxHTTPError(
                status_code=400,
                code="data_file_upload_invalid",
                message=str(exc),
            ) from exc
        except DataFileTooLargeError as exc:
            logger.warning(f"行情数据文件上传被拒绝：请求ID={request_id} 原因=文件过大")
            raise SandboxHTTPError(
                status_code=413,
                code="data_file_upload_too_large",
                message=str(exc),
            ) from exc
        except Exception as exc:
            logger.error(f"行情数据文件上传失败：请求ID={request_id}")
            raise SandboxHTTPError(
                status_code=500,
                code="data_file_upload_failed",
                message="data file upload failed",
            ) from exc
        finally:
            await file.close()
        return DataFileUploadResponse(
            data_file=saved.data_file,
            size_bytes=saved.size_bytes,
            request_id=request_id,
        )

    @application.post(
        "/execute",
        response_model=ExecuteResponse,
        responses={
            400: {"model": SandboxErrorResponse},
            401: {"model": SandboxErrorResponse},
            404: {"model": SandboxErrorResponse},
            413: {"model": SandboxErrorResponse},
            429: {"model": SandboxErrorResponse},
            500: {"model": SandboxErrorResponse},
        },
    )
    async def execute_code(
        body: ExecuteRequest,
        request: Request,
        _: Annotated[None, authenticated],
    ) -> ExecuteResponse:
        req_id = request.state.request_id
        logger.info(
            f"收到代码执行请求：日期={body.date} 文件名={body.filename} "
            f"数据文件={body.data_file} 请求超时={body.timeout} 请求ID={req_id}"
        )

        timeout = (
            body.timeout if body.timeout is not None else resolved_settings.max_timeout_seconds
        )

        if timeout > resolved_settings.max_timeout_seconds:
            logger.warning(
                f"代码执行被拒绝 (超时超出限制 400)：请求超时 {timeout:.2f}秒 > "
                f"上限 {resolved_settings.max_timeout_seconds:.2f}秒 请求ID={req_id}"
            )
            raise SandboxHTTPError(
                status_code=400,
                code="execution_timeout_invalid",
                message=(
                    f"请求执行超时为 {timeout:g} 秒，超过沙箱允许的最大值 "
                    f"{resolved_settings.max_timeout_seconds:g} 秒；代码尚未开始运行"
                ),
            )
        try:
            script = application.state.storage.resolve(body.date, body.filename)
        except InvalidPathError as exc:
            logger.warning(f"代码执行被拒绝 (路径格式无效 400)：{exc} 请求ID={req_id}")
            raise SandboxHTTPError(
                status_code=400,
                code="uploaded_code_path_invalid",
                message=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            logger.warning(
                f"代码执行被拒绝 (未找到脚本 404)：日期={body.date} "
                f"文件名={body.filename} 请求ID={req_id}"
            )
            raise SandboxHTTPError(
                status_code=404,
                code="uploaded_code_not_found",
                message="uploaded file not found",
            ) from exc

        try:
            data_file = application.state.data_files.resolve(body.data_file)
        except DataFileTooLargeError as exc:
            logger.warning(f"代码执行被拒绝 (数据文件过大 413)：{exc} 请求ID={req_id}")
            raise SandboxHTTPError(
                status_code=413,
                code="data_file_too_large",
                message=str(exc),
            ) from exc
        except InvalidPathError as exc:
            logger.warning(f"代码执行被拒绝 (数据文件路径无效 400)：{exc} 请求ID={req_id}")
            raise SandboxHTTPError(
                status_code=400,
                code="data_file_path_invalid",
                message=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            logger.warning(
                f"代码执行被拒绝 (未找到数据文件 404)：数据文件={body.data_file} 请求ID={req_id}"
            )
            raise SandboxHTTPError(
                status_code=404,
                code="data_file_not_found",
                message="data file not found",
            ) from exc

        signal_files: list[SignalFileInput] = []
        for signal in body.signal_files:
            try:
                signal_path = application.state.data_files.resolve(signal.data_file)
            except DataFileTooLargeError as exc:
                raise SandboxHTTPError(
                    status_code=413,
                    code="signal_data_file_too_large",
                    message=str(exc),
                ) from exc
            except InvalidPathError as exc:
                raise SandboxHTTPError(
                    status_code=400,
                    code="signal_data_file_path_invalid",
                    message=str(exc),
                ) from exc
            except FileNotFoundError as exc:
                raise SandboxHTTPError(
                    status_code=404,
                    code="signal_data_file_not_found",
                    message="signal data file not found",
                ) from exc
            signal_files.append(
                SignalFileInput(
                    alias=signal.alias,
                    dataset=signal.dataset,
                    path=signal_path,
                    date_field=signal.date_field,
                    time_field=signal.time_field,
                    availability_lag_days=signal.availability_lag_days,
                )
            )

        execution_files: list[ExecutionFileInput] = []
        for execution_file in body.execution_files:
            try:
                execution_path = application.state.data_files.resolve(execution_file.data_file)
            except DataFileTooLargeError as exc:
                raise SandboxHTTPError(
                    status_code=413,
                    code="execution_data_file_too_large",
                    message=str(exc),
                ) from exc
            except InvalidPathError as exc:
                raise SandboxHTTPError(
                    status_code=400,
                    code="execution_data_file_path_invalid",
                    message=str(exc),
                ) from exc
            except FileNotFoundError as exc:
                raise SandboxHTTPError(
                    status_code=404,
                    code="execution_data_file_not_found",
                    message="execution data file not found",
                ) from exc
            execution_files.append(
                ExecutionFileInput(
                    alias=execution_file.alias,
                    dataset=execution_file.dataset,
                    path=execution_path,
                    contract_multiplier=execution_file.contract_multiplier,
                    margin_rate=execution_file.margin_rate,
                )
            )

        logger.info(
            f"正在申请执行门控槽位：脚本={script.name} 数据文件={data_file.name} "
            f"超时限制={timeout:.2f}秒 请求ID={req_id}"
        )
        try:
            async with application.state.gate.slot():
                if signal_files or execution_files:
                    result = await application.state.executor.execute(
                        script,
                        data_file,
                        body.parameters.model_dump(exclude_none=True),
                        timeout,
                        signal_files=tuple(signal_files),
                        primary_alias=body.primary_alias,
                        execution_files=tuple(execution_files),
                    )
                else:
                    result = await application.state.executor.execute(
                        script,
                        data_file,
                        body.parameters.model_dump(exclude_none=True),
                        timeout,
                    )
        except DataFileTooLargeError as exc:
            logger.warning(f"代码执行被拒绝 (数据文件过大 413)：{exc} 请求ID={req_id}")
            raise SandboxHTTPError(
                status_code=413,
                code="data_file_too_large",
                message=str(exc),
            ) from exc
        except SandboxBusyError as exc:
            logger.warning(
                f"代码执行被拒绝 (沙箱繁忙 429)：运行中={application.state.gate.active} "
                f"排队中={application.state.gate.waiting} 请求ID={req_id}"
            )
            raise SandboxHTTPError(
                status_code=429,
                code="sandbox_busy",
                message="sandbox is busy; retry later",
                headers={"Retry-After": "1"},
            ) from exc
        except Exception as exc:
            logger.exception(
                f"代码执行遭遇未预期错误 (500)：文件名={body.filename} 请求ID={req_id}"
            )
            raise SandboxHTTPError(
                status_code=500,
                code="sandbox_execution_failed",
                message="sandbox execution failed",
            ) from exc

        logger.info(
            f"代码执行完成：请求ID={req_id} 文件名={body.filename} "
            f"状态={result.status} 退出码={result.exit_code}"
        )
        return ExecuteResponse(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            status=result.status,
        )

    return application


app = create_app()
