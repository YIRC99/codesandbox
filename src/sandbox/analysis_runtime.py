"""Immutable runtime manifest and the exact trusted sources used by an analysis worker."""

import ast
import copy
import hashlib
import importlib.metadata
import json
import platform
from importlib.machinery import PathFinder
from pathlib import Path

from .analysis_runner import trusted_import_paths

_ANALYSIS_DISTRIBUTIONS = {
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "matplotlib": "matplotlib",
    "seaborn": "seaborn",
    "plotly": "plotly",
    "sklearn": "scikit-learn",
    "backtesting": "backtesting",
    "pandas_ta": "pandas-ta",
    "loguru": "loguru",
}


def resource_enforcement(settings, *, platform_name=None, resource_module=None):
    system = platform_name or platform.system()
    if system != "Windows" and resource_module is None:
        import resource as resource_module
    resources = {
        "process_cpu_seconds": "RLIMIT_CPU",
        "process_memory_bytes": "RLIMIT_AS",
        "process_count_limit": "RLIMIT_NPROC",
        "process_file_bytes": "RLIMIT_FSIZE",
        "process_open_files": "RLIMIT_NOFILE",
    }
    values = {}
    for setting, name in resources.items():
        kind = getattr(resource_module, name, None)
        supported = system != "Windows" and kind is not None
        inherited = planned = None
        if supported:
            hard = resource_module.getrlimit(kind)[1]
            inherited = None if hard == resource_module.RLIM_INFINITY else hard
            configured = getattr(settings, setting)
            planned = configured if inherited is None else min(configured, inherited)
        values[setting] = {"supported": supported, "inherited_hard": inherited, "planned": planned}
    return {"platform": system, "limits": values}


def _declared_literal(source, name):
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                expression = node.value
                if (
                    isinstance(expression, ast.Call)
                    and isinstance(expression.func, ast.Name)
                    and expression.func.id == "frozenset"
                ):
                    expression = expression.args[0]
                return ast.literal_eval(expression)
    raise ValueError(f"Trusted runtime has no literal {name}")


class AnalysisRuntime:
    def __init__(self, settings):
        root = Path(__file__).parent
        contract = json.loads(
            (root / "contracts/analysis-execution.v1.json").read_text(encoding="utf-8")
        )
        self.runner_source = (root / "analysis_runner.py").read_text(encoding="utf-8")
        self.policy_source = (root / "runner.py").read_text(encoding="utf-8")
        sdk_version = _declared_literal(self.runner_source, "ANALYSIS_SDK_VERSION")
        blocked_imports = _declared_literal(self.runner_source, "BLOCKED_ANALYSIS_IMPORTS")
        allowed_imports = _declared_literal(self.policy_source, "ALLOWED_IMPORTS")
        self.import_policy = trusted_import_paths()
        import_paths, _, _ = self.import_policy
        dependencies = {}
        for name, distribution in _ANALYSIS_DISTRIBUTIONS.items():
            if name not in allowed_imports or name in blocked_imports:
                continue
            try:
                installed = next(
                    iter(importlib.metadata.distributions(name=distribution, path=import_paths)),
                    None,
                )
                if installed is not None and PathFinder.find_spec(name, import_paths) is not None:
                    dependencies[name] = installed.version
            except (ImportError, ValueError):
                continue
        descriptor = {
            "schema_version": contract["runtime_descriptor"]["schema_version"],
            "runner_source": self.runner_source,
            "policy_source": self.policy_source,
            "sdk_version": sdk_version,
            "python_version": platform.python_version(),
            "dependencies": dependencies,
            "resource_enforcement": resource_enforcement(settings),
            "limits": {
                "max_timeout_seconds": settings.max_timeout_seconds,
                "max_result_bytes": min(
                    settings.analysis_max_result_bytes, settings.process_file_bytes
                ),
                "max_input_bytes": settings.max_data_file_bytes,
                "max_code_bytes": settings.max_upload_bytes,
                "max_output_bytes": settings.max_output_bytes,
                "process_cpu_seconds": settings.process_cpu_seconds,
                "process_memory_bytes": settings.process_memory_bytes,
                "process_file_bytes": settings.process_file_bytes,
                "process_count_limit": settings.process_count_limit,
                "process_open_files": settings.process_open_files,
                "max_concurrent": settings.max_concurrent,
                "max_waiting": settings.max_waiting,
                "queue_wait_seconds": settings.queue_wait_seconds,
                "max_storage_bytes": settings.analysis_max_storage_bytes,
                "max_callback_response_bytes": min(
                    settings.analysis_max_result_bytes, settings.process_file_bytes
                ),
                "terminate_grace_seconds": settings.terminate_grace_seconds,
            },
        }
        canonical = json.dumps(
            descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        descriptor["runtime_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self._descriptor = descriptor

    def descriptor(self):
        return copy.deepcopy(self._descriptor)

    def receipt(self):
        return {
            key: self._descriptor[key]
            for key in ("runtime_sha256", "sdk_version", "python_version")
        }
