"""RAG subsystem runtime settings resolver.

This side-effect-free resolver is shared by Dashboard, CLI, server processes,
and tests. It does not create index directories or read secret values merely by
being imported.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
for candidate in (ROOT, SRC_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import config

try:
    from data_foundation.memory_corpus import canonical_lessons_path
    from data_foundation.paths import RuntimePaths, load_paths
except ImportError:  # pragma: no cover - direct script fallback
    RuntimePaths = Any  # type: ignore
    load_paths = None  # type: ignore
    canonical_lessons_path = None  # type: ignore


DEFAULT_ZH_1024_MODEL = "BAAI/bge-large-zh-v1.5"
DEFAULT_ZH_MODEL = "intfloat/multilingual-e5-small"
DEFAULT_EN_1024_MODEL = "BAAI/bge-large-en-v1.5"
DEFAULT_EN_MODEL = "all-MiniLM-L6-v2"
MODEL_DIMENSIONS = {
    DEFAULT_ZH_1024_MODEL: 1024,
    DEFAULT_ZH_MODEL: 384,
    DEFAULT_EN_1024_MODEL: 1024,
    DEFAULT_EN_MODEL: 384,
}
EMBEDDING_MODEL_OPTIONS = (
    {"language": "zh", "model": DEFAULT_ZH_MODEL, "dimension": 384, "label": "中文/多语 384 - multilingual E5 small"},
    {"language": "zh", "model": DEFAULT_ZH_1024_MODEL, "dimension": 1024, "label": "中文 1024 - BGE large zh"},
    {"language": "en", "model": DEFAULT_EN_1024_MODEL, "dimension": 1024, "label": "English 1024 - BGE large en"},
    {"language": "en", "model": DEFAULT_EN_MODEL, "dimension": 384, "label": "English 384 - MiniLM L6"},
)
VALID_NOVA_RAG_MODES = {"legacy", "v2-shadow", "v2", "disabled"}
VALID_LANGUAGE_PROFILES = {"zh", "en", "mixed"}
VALID_EMBEDDING_PROVIDERS = {"local", "cloud"}
VALID_RERANKER_PROVIDERS = {"none", "local-score"}
VALID_EXTERNAL_SOURCE_MODES = {"supplement", "replace"}
VALID_EXTERNAL_SOURCE_SYMLINK_POLICIES = {"reject", "within-root"}
DEFAULT_EXTERNAL_SOURCE_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_EXTERNAL_SOURCE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_EXTERNAL_SOURCE_MAX_FILES = 10_000
DEFAULT_RAG_SERVER_HOST = "127.0.0.1"
DEFAULT_RAG_SERVER_PORT = 3037
DEFAULT_RAG_SERVER_HEALTH_PATH = "/health"
DEFAULT_INDEXING_SOURCE_SETS = (
    "filtered-dialogue-daily",
    "lessons",
    "foundation-usage-rollups",
    "foundation-dashboard-snapshots",
    "diary-markdown-sections",
    "diary-markdown-embedded-json",
    "nova-task-work-graph-events",
    "task-board-snapshot",
    "foundation-period-projections",
)
RETIRED_INDEXING_SOURCE_SETS = {"legacy-diary-daily"}
DEFAULT_RETRIEVAL_LATENCY_BUDGET_SECONDS = 60.0
MAX_RETRIEVAL_LATENCY_BUDGET_SECONDS = 120.0


@dataclass(frozen=True)
class ExternalSourceSettings:
    enabled: bool
    mode: str
    paths: tuple[Path, ...]
    recursive: bool
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    max_file_bytes: int
    max_total_bytes: int
    max_files: int
    symlink_policy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "paths": [str(path) for path in self.paths],
            "recursive": self.recursive,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_files": self.max_files,
            "symlink_policy": self.symlink_policy,
        }


@dataclass(frozen=True)
class RagSettings:
    enabled: bool
    mode: str
    legacy_index_path: Path
    diary_source_root: Path
    foundation_db_path: Path
    task_board_path: Path
    lessons_path: Path
    v2_store_path: Path
    language_profile: str
    embedding_provider: str
    embedding_provider_id: str
    embedding_model: str
    embedding_dimension: int
    embedding_endpoint: str
    embedding_api_key_env: str
    embedding_secret_ref: dict[str, Any] | None
    embedding_secret_migration_required: bool
    embedding_batch_size: int
    embedding_device: str
    server_enabled: bool
    server_host: str
    server_port: int
    server_health_path: str
    indexing_enabled: bool
    indexing_source_sets: tuple[str, ...]
    indexing_default_full_rebuild: bool
    external_sources: ExternalSourceSettings
    retrieval_top_k: int
    recency_half_life_days: int
    retrieval_latency_budget_seconds: float
    retrieval_max_concurrent_searches: int
    reranker_enabled: bool
    reranker_provider: str
    reranker_model: str | None
    runtime_home: Path

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in (
            "legacy_index_path",
            "diary_source_root",
            "foundation_db_path",
            "task_board_path",
            "lessons_path",
            "v2_store_path",
        ):
            result[key] = str(result[key])
        result["indexing_source_sets"] = list(result["indexing_source_sets"])
        result["external_sources"] = self.external_sources.to_dict()
        result.pop("runtime_home", None)
        return result


def resolve_rag_settings(paths: RuntimePaths | None = None, settings: dict | None = None) -> RagSettings:
    selected = paths or _load_selected_paths()
    runtime_settings = settings if settings is not None else _read_runtime_settings(selected)
    future = _as_dict(runtime_settings.get("rag"))
    features = _as_dict(runtime_settings.get("features"))
    setting_paths = _as_dict(runtime_settings.get("paths"))
    path_settings = _as_dict(setting_paths.get("rag"))
    diary_paths = _as_dict(setting_paths.get("diary"))

    language_profile = _normalize_choice(
        _setting_or_default(future.get("languageProfile"), "zh"),
        VALID_LANGUAGE_PROFILES,
        "NOVA_RAG_LANGUAGE_PROFILE",
    )
    default_model = DEFAULT_EN_MODEL if language_profile == "en" else DEFAULT_ZH_MODEL
    embedding = _as_dict(future.get("embedding"))
    server = _as_dict(future.get("server"))
    retrieval = _as_dict(future.get("retrieval"))
    reranker = _as_dict(retrieval.get("reranker"))
    legacy = _as_dict(future.get("legacy"))
    v2 = _as_dict(future.get("v2"))
    embedding_provider, embedding_provider_id = _embedding_provider_settings(embedding)

    model = str(_setting_or_default(embedding.get("model"), default_model))
    dimension = _positive_int(
        _setting_or_default(embedding.get("dimension"), MODEL_DIMENSIONS.get(model, 1024)),
        "NOVA_RAG_EMBEDDING_DIMENSION",
    )
    legacy_index = _legacy_index_path(
        _setting_or_default(
            legacy.get("indexPath") or path_settings.get("legacyRagIndex"),
            _default_legacy_rag_root(selected),
        )
    )
    source = _as_dict(future.get("source"))
    indexing = _as_dict(future.get("indexing"))
    external_sources = _external_source_settings(_as_dict(indexing.get("externalSources")))
    diary_source_root = _absolute_path(
        _setting_or_default(
            source.get("root")
            or indexing.get("sourceRoot")
            or v2.get("sourceRoot")
            or diary_paths.get("generatedDiary"),
            _default_diary_source_root(selected),
        )
    )
    foundation_db_path = _absolute_path(
        _setting_or_default(
            source.get("foundationDbPath")
            or indexing.get("foundationDbPath")
            or v2.get("foundationDbPath"),
            getattr(selected, "db_path", selected.home / "data" / "actanara_data.sqlite3"),
        )
    )
    task_board_path = _absolute_path(
        _setting_or_default(
            source.get("taskBoardPath")
            or indexing.get("taskBoardPath")
            or v2.get("taskBoardPath"),
            getattr(selected, "task_board_path", diary_source_root / "TASK_BOARD.md"),
        )
    )
    lessons_path = _absolute_path(
        _setting_or_default(
            source.get("lessonsPath")
            or indexing.get("lessonsPath")
            or v2.get("lessonsPath"),
            _default_lessons_path(selected, diary_source_root),
        )
    )
    v2_store = _absolute_path(
        _setting_or_default(
            v2.get("storePath") or future.get("home"),
            selected.home / "reserved" / "rag" / "v2",
        )
    )

    mode = _normalize_choice(_setting_or_default(future.get("mode"), "v2"), VALID_NOVA_RAG_MODES, "NOVA_RAG_MODE")
    enabled_default = bool(features.get("rag", True))
    enabled = _to_bool(_setting_or_default(future.get("enabled"), enabled_default))
    product_enabled = bool(enabled and mode != "disabled")
    server_enabled_default = bool(features.get("embeddingServer", True))
    server_enabled = product_enabled and _to_bool(_setting_or_default(server.get("enabled"), server_enabled_default))
    indexing_source_sets = _normalize_indexing_source_sets(
        _as_list(indexing.get("sourceSets"), DEFAULT_INDEXING_SOURCE_SETS)
    )
    memory_search = (
        runtime_settings.get("memorySearch")
        if isinstance(runtime_settings.get("memorySearch"), dict)
        else {}
    )
    native_memory = (
        memory_search.get("nativeMemory")
        if isinstance(memory_search.get("nativeMemory"), dict)
        else {}
    )
    native_tools = (
        native_memory.get("tools")
        if isinstance(native_memory.get("tools"), dict)
        else {}
    )
    if (
        _to_bool(native_memory.get("enabled", True))
        and _to_bool(native_memory.get("allowInRag", True))
        and any(
            _to_bool(native_tools.get(tool, True))
            for tool in ("codex", "claudeCode")
        )
    ):
        native_source_sets = ["agent-native-memory"]
        if _to_bool(native_memory.get("includeInstructions", True)):
            native_source_sets.append("agent-native-instructions")
        indexing_source_sets = tuple(
            dict.fromkeys((*indexing_source_sets, *native_source_sets))
        )

    return RagSettings(
        enabled=enabled,
        mode=mode,
        legacy_index_path=legacy_index,
        diary_source_root=diary_source_root,
        foundation_db_path=foundation_db_path,
        task_board_path=task_board_path,
        lessons_path=lessons_path,
        v2_store_path=v2_store,
        language_profile=language_profile,
        embedding_provider=_normalize_choice(
            embedding_provider,
            VALID_EMBEDDING_PROVIDERS,
            "NOVA_RAG_EMBEDDING_PROVIDER",
        ),
        embedding_provider_id=embedding_provider_id,
        embedding_model=model,
        embedding_dimension=dimension,
        embedding_endpoint=str(_setting_or_default(embedding.get("endpoint"), "")),
        embedding_api_key_env=str(embedding.get("apiKeyEnv") or "NOVA_RAG_CLOUD_API_KEY"),
        embedding_secret_ref=embedding.get("secretRef") if isinstance(embedding.get("secretRef"), dict) else None,
        embedding_secret_migration_required=_secret_ref_requires_reentry(
            runtime_settings,
            embedding.get("secretRef") if isinstance(embedding.get("secretRef"), dict) else None,
        ),
        embedding_batch_size=_positive_int(embedding.get("batchSize", 200), "rag.embedding.batchSize"),
        embedding_device=str(_setting_or_default(embedding.get("device"), "auto")),
        server_enabled=server_enabled,
        server_host=str(_setting_or_default(server.get("host"), DEFAULT_RAG_SERVER_HOST)),
        server_port=_positive_int(_setting_or_default(server.get("port"), DEFAULT_RAG_SERVER_PORT), "NOVA_RAG_SERVER_PORT"),
        server_health_path=_normalize_health_path(
            str(_setting_or_default(server.get("healthPath"), DEFAULT_RAG_SERVER_HEALTH_PATH))
        ),
        indexing_enabled=_to_bool(indexing.get("enabled", True)),
        indexing_source_sets=indexing_source_sets,
        indexing_default_full_rebuild=_to_bool(indexing.get("defaultFullRebuild", False)),
        external_sources=external_sources,
        retrieval_top_k=_positive_int(retrieval.get("topK", 8), "rag.retrieval.topK"),
        recency_half_life_days=_positive_int(
            retrieval.get("recencyHalfLifeDays", 7),
            "rag.retrieval.recencyHalfLifeDays",
        ),
        retrieval_latency_budget_seconds=_bounded_positive_float(
            retrieval.get("latencyBudgetSeconds", DEFAULT_RETRIEVAL_LATENCY_BUDGET_SECONDS),
            "rag.retrieval.latencyBudgetSeconds",
            maximum=MAX_RETRIEVAL_LATENCY_BUDGET_SECONDS,
        ),
        retrieval_max_concurrent_searches=_positive_int(
            retrieval.get("maxConcurrentSearches", 2),
            "rag.retrieval.maxConcurrentSearches",
        ),
        reranker_enabled=_to_bool(reranker.get("enabled", False)),
        reranker_provider=_normalize_choice(
            _setting_or_default(reranker.get("provider"), "none"),
            VALID_RERANKER_PROVIDERS,
            "NOVA_RAG_RERANKER_PROVIDER",
        ),
        reranker_model=str(reranker.get("model")).strip() if reranker.get("model") else None,
        runtime_home=selected.home,
    )


def is_rag_product_enabled(settings: RagSettings | None = None, paths: RuntimePaths | None = None) -> bool:
    """Return the effective product-level RAG enabled state."""
    resolved = settings or resolve_rag_settings(paths)
    return bool(resolved.enabled and resolved.mode != "disabled")


def rag_product_disabled_reason(settings: RagSettings | None = None, paths: RuntimePaths | None = None) -> str | None:
    resolved = settings or resolve_rag_settings(paths)
    if not resolved.enabled:
        return "nova-RAG subsystem is disabled by settings."
    if resolved.mode == "disabled":
        return "nova-RAG mode is disabled by settings."
    return None


def _load_selected_paths() -> RuntimePaths:
    if load_paths is None:
        raise RuntimeError("data_foundation.paths is required to resolve RAG settings")
    return load_paths()


def _read_runtime_settings(paths: RuntimePaths) -> dict:
    path = paths.config_dir / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _default_legacy_rag_root(paths: RuntimePaths) -> Path:
    legacy_root = getattr(paths, "legacy_rag_root", None)
    if legacy_root is None:
        return Path(getattr(paths, "home")) / "reserved" / "retired" / "legacy-rag"
    return Path(legacy_root)


def _default_diary_source_root(paths: RuntimePaths) -> Path:
    return Path(getattr(paths, "diary_dir", None) or getattr(paths, "legacy_diary_root"))


def _default_lessons_path(paths: RuntimePaths, diary_source_root: Path) -> Path:
    if canonical_lessons_path is not None:
        return canonical_lessons_path(paths)
    return Path(getattr(paths, "home", diary_source_root)) / "artifacts" / "learning" / "lessons.jsonl"


def _normalize_indexing_source_sets(values: list[Any]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in values:
        source_set = str(item).strip()
        if not source_set or source_set in RETIRED_INDEXING_SOURCE_SETS:
            continue
        normalized.append(source_set)
    return tuple(normalized)


def effective_indexing_source_sets(settings: RagSettings) -> tuple[str, ...]:
    """Return the source-set snapshot implied by external source mode."""
    external = settings.external_sources
    if not external.enabled:
        return settings.indexing_source_sets
    if external.mode == "replace":
        return ("external-content",)
    return tuple(dict.fromkeys((*settings.indexing_source_sets, "external-content")))


def _external_source_settings(value: dict[str, Any]) -> ExternalSourceSettings:
    raw_paths = value.get("paths", [])
    if not isinstance(raw_paths, list):
        raise ValueError("rag.indexing.externalSources.paths must be a list")
    paths: list[Path] = []
    for item in raw_paths:
        raw = str(item or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("rag.indexing.externalSources.paths entries must be absolute paths")
        paths.append(path.absolute())
    include = _safe_glob_patterns(value.get("include", ["*", "**/*"]), "include")
    exclude = _safe_glob_patterns(value.get("exclude", []), "exclude")
    return ExternalSourceSettings(
        enabled=_to_bool(value.get("enabled", False)),
        mode=_normalize_choice(
            _setting_or_default(value.get("mode"), "supplement"),
            VALID_EXTERNAL_SOURCE_MODES,
            "rag.indexing.externalSources.mode",
        ),
        paths=tuple(dict.fromkeys(paths)),
        recursive=_to_bool(value.get("recursive", True)),
        include=include,
        exclude=exclude,
        max_file_bytes=_positive_int(
            value.get("maxFileBytes", DEFAULT_EXTERNAL_SOURCE_MAX_FILE_BYTES),
            "rag.indexing.externalSources.maxFileBytes",
        ),
        max_total_bytes=_positive_int(
            value.get("maxTotalBytes", DEFAULT_EXTERNAL_SOURCE_MAX_TOTAL_BYTES),
            "rag.indexing.externalSources.maxTotalBytes",
        ),
        max_files=_positive_int(
            value.get("maxFiles", DEFAULT_EXTERNAL_SOURCE_MAX_FILES),
            "rag.indexing.externalSources.maxFiles",
        ),
        symlink_policy=_normalize_choice(
            _setting_or_default(value.get("symlinkPolicy"), "reject"),
            VALID_EXTERNAL_SOURCE_SYMLINK_POLICIES,
            "rag.indexing.externalSources.symlinkPolicy",
        ),
    )


def _safe_glob_patterns(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"rag.indexing.externalSources.{field} must be a list")
    patterns: list[str] = []
    for item in value:
        pattern = str(item or "").strip().replace("\\", "/")
        parts = pattern.split("/")
        if not pattern or pattern.startswith("/") or ".." in parts:
            raise ValueError(
                f"rag.indexing.externalSources.{field} contains an unsafe traversal pattern: {item!r}"
            )
        patterns.append(pattern)
    return tuple(dict.fromkeys(patterns))


def _legacy_index_path(value: Any) -> Path:
    path = _absolute_path(value)
    if path.suffix == ".jsonl":
        return path
    return path / "index.jsonl"


def _absolute_path(value: Any) -> Path:
    return Path(str(value)).expanduser().absolute()


def _setting_or_default(setting_value: Any, default: Any) -> Any:
    if setting_value is not None:
        return setting_value
    return default


def _normalize_choice(value: Any, allowed: set[str], name: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")
    return normalized


def _embedding_provider_settings(embedding: dict[str, Any]) -> tuple[str, str]:
    """Normalize old provider-only RAG settings into mode + provider id."""
    raw_mode = embedding.get("mode")
    raw_provider = embedding.get("provider")
    raw_provider_id = embedding.get("providerId")
    if raw_mode is not None:
        mode = _normalize_choice(raw_mode, VALID_EMBEDDING_PROVIDERS, "rag.embedding.mode")
    elif str(raw_provider or "").strip() in VALID_EMBEDDING_PROVIDERS:
        mode = str(raw_provider).strip()
    elif raw_provider:
        mode = "cloud"
    else:
        mode = "local"

    if raw_provider_id:
        provider_id = str(raw_provider_id).strip()
    elif raw_provider and str(raw_provider).strip() not in VALID_EMBEDDING_PROVIDERS:
        provider_id = str(raw_provider).strip()
    else:
        provider_id = mode
    return mode, provider_id or mode


def _normalize_health_path(value: str) -> str:
    stripped = value.strip() or "/health"
    return stripped if stripped.startswith("/") else f"/{stripped}"


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return parsed


def _bounded_positive_float(value: Any, name: str, *, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    if parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum:g} seconds")
    return parsed


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _secret_ref_requires_reentry(settings: dict, secret_ref: dict | None) -> bool:
    if not isinstance(secret_ref, dict) or secret_ref.get("backend") != "macos-keychain":
        return False
    # A legacy Keychain ref is not safe for unattended server or scheduler
    # processes, even before its one-time migration has been attempted.
    return True


def _as_list(value: Any, default: list) -> list:
    return value if isinstance(value, list) else default
