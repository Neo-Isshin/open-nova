"""Runtime settings stored under the selected ACTANARA_HOME."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config

from .llm_provider_catalog import (
    CUSTOM_PROVIDER_ID,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_PIPELINE_CONCURRENCY,
    DEFAULT_PIPELINE_GATE_TOKENS,
    PIPELINE_GATE_MODE_AUTO,
    PIPELINE_GATE_MODE_MANUAL,
    SUPPORTED_APIS,
    auto_pipeline_gate_tokens,
    default_llm_provider_settings,
    llm_provider_catalog,
    normalize_llm_provider_chain_update,
    normalize_llm_provider_update,
)
from .pipeline_language import (
    DEFAULT_PIPELINE_LANGUAGE_PROFILE,
    resolve_pipeline_language_profile,
    valid_pipeline_language_profiles,
)
from .network import require_loopback_host
from .platform_support import default_timer_provider
from .external_tool_definitions import default_external_tool_settings_from_catalog
from .paths import (
    RUNTIME_SCHEMA_VERSION,
    RuntimePaths,
    initialize_home,
    load_paths,
    runtime_paths_for_home,
    update_runtime_manifest_paths,
)
from .secret_store import (
    llm_api_key_ref,
    rag_embedding_api_key_ref,
    read_secret,
    settings_transaction_secret_ref,
    store_secret,
)
from .settings_transaction import (
    SettingsTransactionPlan,
    execute_settings_transaction,
    settings_mutation_barrier,
)
from .time import detect_system_timezone, detect_system_timezone_authority

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"
SCHEDULER_STATE_FILENAME = "settings-scheduler-state.json"
MASKED_SECRET = "********"
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 3036
DEFAULT_DASHBOARD_HEALTH_PATH = "/health"
VALID_RUNTIME_SOURCES = {"legacy", "foundation"}
VALID_PIPELINE_LANGUAGE_PROFILES = valid_pipeline_language_profiles()
INSTALL_ONLY_PIPELINE_FIELDS = {
    "languageProfile",
    "englishEnabled",
    "diarySchemaVersion",
    "promptPayloadProfile",
}
TIME_OF_DAY_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FEATURE_DEFAULTS = {
    "pipeline": True,
    "dashboard": True,
    "foundationSnapshots": True,
    "rag": True,
    "embeddingServer": False,
    "novaTask": True,
    "taskAuditSink": False,
    "llmGeneration": True,
}
RUNTIME_SOURCE_FIELDS = {
    "DASHBOARD_READ_SOURCE": "dashboardReadSource",
    "REPORT_READ_SOURCE": "reportReadSource",
    "DIARY_METRICS_SOURCE": "diaryMetricsSource",
    "DIARY_MEMORY_SOURCE": "diaryMemorySource",
    "DIARY_TASKS_SOURCE": "diaryTasksSource",
    "TASK_AUDIT_SINK": "taskAuditSink",
}
RUNTIME_SOURCE_DEFAULTS = {env_name: "foundation" for env_name in RUNTIME_SOURCE_FIELDS}
OPERATOR_SETTINGS_WRITE_ALLOWED_TOP_LEVEL = {
    "general",
    "dashboard",
    "schedule",
    "features",
    "memorySearch",
    "externalTools",
    "paths",
    "pipeline",
    "runtimeSources",
    "weather",
    "todos",
}
OPERATOR_SETTINGS_WRITE_PROTECTED_TOP_LEVEL = {
    "backup",
    "schemaVersion",
    "updatedAt",
    "llmProvider",
    "llmProviderChain",
    "rag",
}
SETTINGS_AUTHORITY_GROUPS = (
    {
        "group": "general",
        "authority": "settings-json",
        "writableVia": "operator settings API; future Dashboard/CLI general controls",
        "manualDefaultPolicy": "checked-in defaults seed product identity and locale; operator edits persist in settings",
        "fields": (
            {"path": "general.appName", "defaultSource": "default_settings"},
            {"path": "general.environment", "env": "ACTANARA_ENVIRONMENT", "defaultSource": "default_settings"},
            {"path": "general.timezone", "env": "TARGET_TIMEZONE", "defaultSource": "system timezone detection; config.TARGET_TIMEZONE fallback"},
            {"path": "general.locale", "env": "ACTANARA_LOCALE", "defaultSource": "default_settings"},
            {"path": "general.workspaceRoot", "env": "WORKSPACE_DIR", "defaultSource": "config.WORKSPACE_DIR"},
            {"path": "general.tmpWorkspace", "env": "TMP_WORKSPACE", "defaultSource": "config.TMP_WORKSPACE"},
        ),
    },
    {
        "group": "runtimePaths",
        "authority": "path-service",
        "writableVia": "runtime path validate/select/import-legacy controls",
        "manualDefaultPolicy": "selected runtime root is explicit operator state; derived paths follow the selected root",
        "fields": (
            {"path": "paths.runtime.actanaraHome", "env": "ACTANARA_HOME", "defaultSource": "config.ACTANARA_HOME"},
            {"path": "paths.runtime.database", "env": "ACTANARA_DATA_DB_PATH", "defaultSource": "$ACTANARA_HOME/data/actanara_data.sqlite3"},
            {"path": "paths.runtime.snapshots", "env": "ACTANARA_DATA_EXPORT_DIR", "defaultSource": "$ACTANARA_HOME/snapshots"},
            {"path": "paths.diary.legacyDiaryRoot", "env": "DIARY_OUTPUT_DIR", "defaultSource": "config.DIARY_OUTPUT_DIR"},
            {"path": "paths.install.workspace", "env": "WORKSPACE_DIR", "defaultSource": "config.WORKSPACE_DIR"},
            {"path": "paths.logsCacheTmp.tmp", "env": "TMP_WORKSPACE", "defaultSource": "config.TMP_WORKSPACE"},
        ),
    },
    {
        "group": "runtimeSources",
        "authority": "settings-resolver",
        "writableVia": "settings runtimeSources block and guarded source-switch workflows",
        "manualDefaultPolicy": "operator-selected source in settings; env remains explicit archive/diagnostic override",
        "fields": tuple(
            {
                "path": f"runtimeSources.{settings_field}",
                "env": env_name,
                "defaultSource": f"config.{env_name}",
                "allowedValues": sorted(VALID_RUNTIME_SOURCES),
            }
            for env_name, settings_field in RUNTIME_SOURCE_FIELDS.items()
        ),
    },
    {
        "group": "weather",
        "authority": "settings-json + weather resolver",
        "writableVia": "operator settings API; future Dashboard weather controls",
        "manualDefaultPolicy": "auto-ip is the default location mode; explicit manual coordinates override network location detection",
        "fields": (
            {"path": "weather.enabled", "defaultSource": "default_settings"},
            {"path": "weather.locationMode", "defaultSource": "default_settings"},
            {"path": "weather.latitude", "defaultSource": "unset until manual configuration or auto-ip cache"},
            {"path": "weather.longitude", "defaultSource": "unset until manual configuration or auto-ip cache"},
            {"path": "weather.label", "defaultSource": "unset until manual configuration or auto-ip cache"},
            {"path": "weather.timezone", "defaultSource": "auto uses detected location timezone or runtime timezone"},
            {"path": "weather.cacheTtlHours", "defaultSource": "default_settings"},
        ),
    },
    {
        "group": "llmProvider",
        "authority": "settings-resolver",
        "writableVia": "Dashboard LLM Provider modal and future CLI provider controls",
        "manualDefaultPolicy": "preset metadata supplies defaults; explicit operator edits become manual where applicable",
        "fields": (
            {"path": "llmProvider.provider", "env": "LLM_PROVIDER", "defaultSource": "provider catalog"},
            {"path": "llmProvider.endpoint", "env": "LLM_HOST", "defaultSource": "operator-selected provider"},
            {"path": "llmProvider.model", "env": "LLM_MODEL_NAME", "defaultSource": "operator-selected model"},
            {"path": "llmProvider.api", "env": "LLM_API", "defaultSource": "provider catalog"},
            {"path": "llmProvider.apiKey", "env": "LLM_API_KEY", "defaultSource": "operator secret"},
            {"path": "llmProvider.pipelineConcurrency", "env": "LLM_PIPELINE_CONCURRENCY", "defaultSource": "provider default"},
            {"path": "llmProvider.timeoutSeconds", "env": None, "defaultSource": "nova-setting"},
            {
                "path": "llmProviderChain",
                "env": None,
                "defaultSource": "ordered provider entries; absent/empty projects legacy llmProvider as primary",
            },
            {
                "path": "llmProvider.pipelineGateTokens",
                "env": "LLM_PIPELINE_GATE_TOKENS",
                "defaultSource": "autoPipelineGateTokens unless pipelineGateMode=manual",
                "modePath": "llmProvider.pipelineGateMode",
                "autoValuePath": "llmProvider.autoPipelineGateTokens",
            },
        ),
    },
    {
        "group": "pipeline",
        "authority": "settings-json",
        "writableVia": "protected pipeline policy only; stable command remains backward compatible",
        "manualDefaultPolicy": "pipeline defaults document command/runtime contract; behavior changes require dedicated pipeline approval",
        "fields": (
            {"path": "pipeline.stableCommand", "defaultSource": "advanced/pipeline/run_daily_pipeline.py"},
            {"path": "pipeline.languageProfile", "defaultSource": "install-time language profile; zh remains the production default", "writableVia": "installer/internal bootstrap only"},
            {"path": "pipeline.englishEnabled", "defaultSource": "false; explicit gate before English diary pass execution", "writableVia": "installer/internal bootstrap only"},
            {"path": "pipeline.diarySchemaVersion", "defaultSource": "language-profile specific diary projection schema", "writableVia": "installer/internal bootstrap only"},
            {"path": "pipeline.promptPayloadProfile", "defaultSource": "language-profile specific prompt fixture namespace", "writableVia": "installer/internal bootstrap only"},
            {"path": "pipeline.pythonExecutable", "env": "ACTANARA_PIPELINE_PYTHON", "defaultSource": "current interpreter or python3"},
            {"path": "pipeline.workingDirectory", "env": "WORKSPACE_DIR", "defaultSource": "config.WORKSPACE_DIR"},
            {"path": "pipeline.dailyDateArgument", "defaultSource": "YYYY-MM-DD optional positional argument"},
            {"path": "pipeline.skipFinalRagEnv", "env": "ACTANARA_PIPELINE_SKIP_FINAL_RAG", "defaultSource": "unset"},
            {"path": "pipeline.thinkingMode", "env": "LLM_THINKING_MODE", "defaultSource": "off"},
            {"path": "pipeline.stepTimeoutSeconds", "env": "ACTANARA_PIPELINE_STEP_TIMEOUT_SECONDS", "defaultSource": "default_settings"},
            {"path": "pipeline.stepTimeouts", "defaultSource": "default_settings"},
            {"path": "pipeline.totalWatchdogSeconds", "env": "ACTANARA_PIPELINE_TOTAL_WATCHDOG_SECONDS", "defaultSource": "default_settings"},
        ),
    },
    {
        "group": "memorySearch",
        "authority": "settings-json + memory-router",
        "writableVia": "operator settings API and Dashboard memory controls",
        "manualDefaultPolicy": "auto prefers configured nova-RAG and retains a local lexical fallback; allowlisted native Agent memory is enabled unless explicitly disabled",
        "fields": (
            {"path": "memorySearch.enabled", "defaultSource": "true"},
            {"path": "memorySearch.backendPolicy", "defaultSource": "auto"},
            {"path": "memorySearch.local.enabled", "defaultSource": "true"},
            {"path": "memorySearch.local.syncAfterPipeline", "defaultSource": "true"},
            {"path": "memorySearch.local.maxScanFiles", "defaultSource": "2000"},
            {"path": "memorySearch.local.maxScanBytes", "defaultSource": "67108864"},
            {"path": "memorySearch.nativeMemory.enabled", "defaultSource": "true; explicit false is preserved"},
            {"path": "memorySearch.nativeMemory.allowInRag", "defaultSource": "true; effective only when nova-RAG is enabled"},
            {"path": "memorySearch.nativeMemory.includeInstructions", "defaultSource": "true; allowlisted instruction Markdown only"},
            {"path": "memorySearch.nativeMemory.tools", "defaultSource": "Codex and Claude Code enabled"},
        ),
    },
    {
        "group": "dashboard",
        "authority": "settings-json",
        "writableVia": "operator settings API; launch scripts may use env as process-local overrides",
        "manualDefaultPolicy": "common local Dashboard defaults seed new homes; operator edits persist in settings",
        "fields": (
            {"path": "dashboard.projectRoot", "env": "ACTANARA_DASHBOARD_PROJECT_ROOT", "defaultSource": "config.WORKSPACE_DIR"},
            {"path": "dashboard.pythonExecutable", "env": "ACTANARA_DASHBOARD_PYTHON", "defaultSource": "default_settings"},
            {"path": "dashboard.appDir", "defaultSource": "$WORKSPACE_DIR/src/dashboard"},
            {"path": "dashboard.host", "env": "ACTANARA_DASHBOARD_HOST", "defaultSource": "127.0.0.1"},
            {"path": "dashboard.port", "env": "ACTANARA_DASHBOARD_PORT", "defaultSource": "3036"},
            {"path": "dashboard.publicBaseUrl", "defaultSource": "http://127.0.0.1:${dashboard.port}"},
            {"path": "dashboard.allowedOrigins", "defaultSource": "[]; add explicit remote/nginx browser origins"},
            {"path": "dashboard.healthPath", "defaultSource": "/health"},
            {"path": "dashboard.logsDir", "defaultSource": "~/Library/Logs/Actanara"},
            {"path": "dashboard.serviceLabel", "defaultSource": "com.actanara.dashboard"},
            {"path": "dashboard.watchdogLabel", "defaultSource": "com.actanara.dashboard.watchdog"},
        ),
    },
    {
        "group": "schedule",
        "authority": "settings-json",
        "writableVia": "Dashboard scheduler/settings controls",
        "manualDefaultPolicy": "checked-in defaults seed new runtime homes; operator edits persist in settings",
        "fields": (
            {"path": "schedule.enabled", "defaultSource": "default_settings"},
            {"path": "schedule.mode", "defaultSource": "default_settings"},
            {"path": "schedule.timezone", "env": "TARGET_TIMEZONE", "defaultSource": "system timezone detection; config.TARGET_TIMEZONE fallback"},
            {"path": "schedule.dailyPipelineTime", "defaultSource": "default_settings"},
            {"path": "schedule.dashboardAggregationTime", "defaultSource": "default_settings"},
            {"path": "schedule.refreshTargets", "defaultSource": "default_settings"},
            {"path": "schedule.systemTimer", "defaultSource": "default_settings"},
        ),
    },
    {
        "group": "backup",
        "authority": "settings-json",
        "writableVia": "Dashboard AI Assets backup controls",
        "manualDefaultPolicy": "disabled until an external target directory is explicitly selected",
        "fields": (
            {"path": "backup.targetDirectory", "defaultSource": "empty; operator selection required"},
            {"path": "backup.include", "defaultSource": "default_settings"},
            {"path": "backup.retention", "defaultSource": "default_settings"},
            {"path": "backup.schedule", "defaultSource": "default_settings"},
        ),
    },
    {
        "group": "rag",
        "authority": "rag-settings-resolver",
        "writableVia": "Dashboard RAG control plane and guarded RAG actions",
        "manualDefaultPolicy": "settings.json owns normal nova-RAG mode/config; NOVA_RAG_* values are child-process/diagnostic exports, not persisted authority overrides",
        "fields": (
            {"path": "rag.enabled", "diagnosticEnv": "NOVA_RAG_ENABLED", "defaultSource": "features.rag"},
            {"path": "rag.mode", "diagnosticEnv": "NOVA_RAG_MODE", "defaultSource": "default_settings"},
            {"path": "rag.languageProfile", "diagnosticEnv": "NOVA_RAG_LANGUAGE_PROFILE", "defaultSource": "default_settings"},
            {"path": "rag.legacy.indexPath", "diagnosticEnv": "NOVA_RAG_LEGACY_INDEX", "defaultSource": "paths.rag.legacyRagIndex"},
            {"path": "rag.v2.storePath", "diagnosticEnv": "NOVA_RAG_V2_STORE", "defaultSource": "$ACTANARA_HOME/reserved/rag/v2"},
            {"path": "rag.embedding.mode", "diagnosticEnv": "NOVA_RAG_EMBEDDING_PROVIDER", "defaultSource": "default_settings"},
            {"path": "rag.embedding.providerId", "diagnosticEnv": "NOVA_RAG_EMBEDDING_PROVIDER_ID", "defaultSource": "default_settings"},
            {"path": "rag.embedding.model", "diagnosticEnv": "NOVA_RAG_EMBEDDING_MODEL", "defaultSource": "default_settings"},
            {"path": "rag.embedding.dimension", "diagnosticEnv": "NOVA_RAG_EMBEDDING_DIMENSION", "defaultSource": "model metadata"},
            {"path": "rag.embedding.device", "diagnosticEnv": "NOVA_RAG_EMBEDDING_DEVICE", "defaultSource": "default_settings"},
            {"path": "rag.server.enabled", "diagnosticEnv": "NOVA_RAG_SERVER_ENABLED", "defaultSource": "features.embeddingServer"},
            {"path": "rag.server.host", "diagnosticEnv": "NOVA_RAG_SERVER_HOST", "defaultSource": "default_settings"},
            {"path": "rag.server.port", "diagnosticEnv": "NOVA_RAG_SERVER_PORT", "defaultSource": "default_settings"},
            {"path": "rag.server.healthPath", "diagnosticEnv": "NOVA_RAG_SERVER_HEALTH_PATH", "defaultSource": "default_settings"},
            {"path": "rag.retrieval.reranker.provider", "diagnosticEnv": "NOVA_RAG_RERANKER_PROVIDER", "defaultSource": "default_settings"},
        ),
    },
    {
        "group": "features",
        "authority": "settings-json",
        "writableVia": "settings feature block; feature-specific controls only",
        "manualDefaultPolicy": "feature defaults seed new runtime homes; operator edits persist in settings",
        "fields": (
            {"path": "features.pipeline", "defaultSource": "default_settings"},
            {"path": "features.dashboard", "defaultSource": "default_settings"},
            {"path": "features.foundationSnapshots", "defaultSource": "default_settings"},
            {"path": "features.rag", "defaultSource": "default_settings"},
            {"path": "features.embeddingServer", "defaultSource": "default_settings"},
            {"path": "features.novaTask", "defaultSource": "default_settings"},
            {"path": "features.taskAuditSink", "defaultSource": "legacy_compat; runtimeSources.taskAuditSink is production authority"},
            {"path": "features.llmGeneration", "defaultSource": "default_settings"},
        ),
    },
    {
        "group": "externalTools",
        "authority": "settings-json",
        "writableVia": "future Dashboard/CLI external tool path controls",
        "manualDefaultPolicy": "common home-directory defaults seed new runtime homes; operator path edits persist in settings",
        "fields": (
            {"path": "externalTools.openclaw.home", "defaultSource": "~/.openclaw"},
            {"path": "externalTools.openclaw.agentsRoot", "defaultSource": "~/.openclaw/agents"},
            {"path": "externalTools.openclaw.configPath", "defaultSource": "~/.openclaw/config.json"},
            {"path": "externalTools.openclaw.credentialsPath", "defaultSource": "~/.openclaw/credentials.json"},
            {"path": "externalTools.openclaw.workspaceRoot", "defaultSource": "~/.openclaw/workspace"},
            {"path": "externalTools.openclaw.workspaceCoderRoot", "defaultSource": "~/.openclaw/workspace-coder"},
            {"path": "externalTools.openclaw.projectsRoot", "defaultSource": "~/.openclaw/workspace/PROJECTS"},
            {"path": "externalTools.openclaw.skillsRoot", "defaultSource": "~/.openclaw/workspace/skills"},
            {"path": "externalTools.openclaw.systemSkillsRoot", "defaultSource": "~/.openclaw/skills"},
            {"path": "externalTools.openclaw.memoryRoot", "defaultSource": "~/.openclaw/memory"},
            {"path": "externalTools.openclaw.cronJobsPath", "defaultSource": "~/.openclaw/cron/jobs.json"},
            {"path": "externalTools.openclaw.cronJobsMigratedPath", "defaultSource": "~/.openclaw/cron/jobs.json.migrated"},
            {"path": "externalTools.openclaw.cronRunsRoot", "defaultSource": "~/.openclaw/cron/runs"},
            {"path": "externalTools.openclaw.toolConfigSnapshotPath", "defaultSource": "~/.openclaw/workspace/.dashboard-tool-configs.json"},
            {"path": "externalTools.claudeCode.home", "defaultSource": "~/.claude"},
            {"path": "externalTools.claudeCode.projectsRoot", "defaultSource": "~/.claude/projects"},
            {"path": "externalTools.claudeCode.skillsRoot", "defaultSource": "~/.claude/skills"},
            {"path": "externalTools.claudeCode.commandsRoot", "defaultSource": "~/.claude/commands"},
            {"path": "externalTools.claudeCode.pluginsRoot", "defaultSource": "~/.claude/plugins"},
            {"path": "externalTools.claudeCode.configPath", "defaultSource": "~/.claude/settings.json"},
            {"path": "externalTools.claudeCode.binaryCandidates", "defaultSource": "common macOS install paths"},
            {"path": "externalTools.codex.home", "defaultSource": "~/.codex"},
            {"path": "externalTools.codex.sessionsRoot", "defaultSource": "~/.codex/sessions"},
            {"path": "externalTools.codex.skillsRoot", "defaultSource": "~/.codex/skills"},
            {"path": "externalTools.codex.configPath", "defaultSource": "~/.codex/config.toml"},
            {"path": "externalTools.geminiCli.home", "defaultSource": "~/.gemini"},
            {"path": "externalTools.geminiCli.chatsRoot", "defaultSource": "~/.gemini/tmp/ssd/chats"},
            {"path": "externalTools.geminiCli.projectsPath", "defaultSource": "~/.gemini/projects.json"},
            {"path": "externalTools.geminiCli.skillsRoot", "defaultSource": "~/.gemini/skills"},
            {"path": "externalTools.geminiCli.configPath", "defaultSource": "~/.gemini/settings.json"},
            {"path": "externalTools.hermes.home", "defaultSource": "~/.hermes"},
            {"path": "externalTools.hermes.stateDbPath", "defaultSource": "~/.hermes/state.db"},
            {"path": "externalTools.hermes.sessionsRoot", "defaultSource": "~/.hermes/sessions"},
            {"path": "externalTools.hermes.skillsRoot", "defaultSource": "~/.hermes/hermes-agent/skills"},
            {"path": "externalTools.hermes.optionalSkillsRoot", "defaultSource": "~/.hermes/hermes-agent/optional-skills"},
            {"path": "externalTools.hermes.pluginsRoot", "defaultSource": "~/.hermes/hermes-agent/plugins"},
            {"path": "externalTools.hermes.profilesRoot", "defaultSource": "~/.hermes/profiles"},
            {"path": "externalTools.hermes.configPath", "defaultSource": "~/.hermes/config.yaml"},
            {"path": "externalTools.hermes.binaryCandidates", "defaultSource": "~/.local/bin/hermes"},
            {"path": "externalTools.opencode.home", "env": "OPENCODE_HOME", "defaultSource": "$XDG_DATA_HOME/opencode"},
            {"path": "externalTools.opencode.databasePath", "defaultSource": "$XDG_DATA_HOME/opencode/opencode.db"},
            {"path": "externalTools.opencode.storageRoot", "defaultSource": "$XDG_DATA_HOME/opencode/storage"},
            {"path": "externalTools.opencode.configHome", "defaultSource": "$XDG_CONFIG_HOME/opencode"},
            {"path": "externalTools.opencode.configPath", "defaultSource": "$XDG_CONFIG_HOME/opencode/opencode.jsonc"},
            {"path": "externalTools.opencode.binaryCandidates", "defaultSource": "common user install paths"},
            {"path": "externalTools.antigravity.home", "defaultSource": "~/.gemini"},
            {"path": "externalTools.antigravity.cliHome", "defaultSource": "~/.gemini/antigravity-cli"},
            {"path": "externalTools.antigravity.ideHome", "defaultSource": "~/.gemini/antigravity-ide"},
            {"path": "externalTools.antigravity.appHome", "defaultSource": "~/.gemini/antigravity"},
            {"path": "externalTools.antigravity.cliConversationsRoot", "defaultSource": "~/.gemini/antigravity-cli/conversations"},
            {"path": "externalTools.antigravity.ideConversationsRoot", "defaultSource": "~/.gemini/antigravity-ide/conversations"},
            {"path": "externalTools.antigravity.appConversationsRoot", "defaultSource": "~/.gemini/antigravity/conversations"},
            {"path": "externalTools.antigravity.cliHistoryPath", "defaultSource": "~/.gemini/antigravity-cli/history.jsonl"},
            {"path": "externalTools.antigravity.cliBrainRoot", "defaultSource": "~/.gemini/antigravity-cli/brain"},
            {"path": "externalTools.antigravity.ideBrainRoot", "defaultSource": "~/.gemini/antigravity-ide/brain"},
            {"path": "externalTools.antigravity.appBrainRoot", "defaultSource": "~/.gemini/antigravity/brain"},
            {"path": "externalTools.antigravity.binaryCandidates", "defaultSource": "common user install paths"},
            {"path": "externalTools.cursor.home", "env": "CURSOR_AGENT_HOME", "defaultSource": "~/.cursor"},
            {"path": "externalTools.cursor.chatsRoot", "defaultSource": "~/.cursor/chats"},
            {"path": "externalTools.cursor.projectsRoot", "defaultSource": "~/.cursor/projects"},
            {"path": "externalTools.cursor.acpSessionsRoot", "defaultSource": "~/.cursor/acp-sessions"},
            {"path": "externalTools.cursor.configPath", "defaultSource": "~/.cursor/cli-config.json"},
            {"path": "externalTools.cursor.ideStateDbCandidates", "defaultSource": "Cursor IDE user-data paths"},
            {"path": "externalTools.cursor.workspaceStorageRoots", "defaultSource": "Cursor IDE user-data paths"},
            {"path": "externalTools.cursor.binaryCandidates", "defaultSource": "common user install paths"},
        ),
    },
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _settings_path(paths: RuntimePaths) -> Path:
    return paths.config_dir / SETTINGS_FILENAME


def _scheduler_state_path(paths: RuntimePaths) -> Path:
    return paths.state_dir / "scheduler" / SCHEDULER_STATE_FILENAME


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def default_settings(paths: RuntimePaths | None = None) -> dict:
    paths = paths or load_paths()
    workspace = Path(config.WORKSPACE_DIR)
    legacy_rag = paths.legacy_rag_root or (paths.home / "reserved" / "retired" / "legacy-rag")
    default_timezone = detect_system_timezone(config.TARGET_TIMEZONE)
    schedule_timezone = (
        detect_system_timezone_authority() or default_timezone
        if platform.system() == "Darwin"
        else default_timezone
    )
    default_pipeline_language = resolve_pipeline_language_profile(DEFAULT_PIPELINE_LANGUAGE_PROFILE)
    return {
        "schemaVersion": SETTINGS_SCHEMA_VERSION,
        "updatedAt": _now_iso(),
        "general": {
            "appName": "Actanara",
            "environment": "local",
            "timezone": default_timezone,
            "locale": "zh-CN",
            "workspaceRoot": str(workspace),
            "tmpWorkspace": str(config.TMP_WORKSPACE),
        },
        "schedule": {
            "enabled": False,
            "mode": "system",
            "timezone": schedule_timezone,
            "dailyPipelineTime": "04:00",
            "dashboardAggregationTime": "04:30",
            "refreshTargets": {
                "currentDay": True,
                "currentWeek": True,
                "currentMonth": True,
            },
            "systemTimer": {
                "provider": default_timer_provider(),
                "label": "actanara.daily",
                "registered": False,
                "registrationManagedBy": "manual",
            },
        },
        "backup": {
            "targetDirectory": "",
            "include": {
                "database": True,
                "diaryMarkdown": True,
                "periodReports": True,
                "ragV2": True,
                "novaTaskExports": True,
                "settings": True,
                "workspaceAttribution": True,
                "runtimeManifests": True,
            },
            "retention": {
                "maxBackups": 7,
                "maxAgeDays": 30,
            },
            "schedule": {
                "enabled": False,
                "frequency": "weekly",
                "timeOfDay": "05:00",
            },
        },
        "paths": {
            "install": {
                "workspace": str(workspace),
                "dashboardApp": str(workspace / "src" / "dashboard"),
            },
            "runtime": {
                "actanaraHome": str(paths.home),
                "database": str(paths.db_path),
                "snapshots": str(paths.snapshots_dir),
                "state": str(paths.state_dir),
            },
            "diary": {
                "generatedDiary": str(paths.diary_dir),
                "legacyDiaryRoot": str(paths.legacy_diary_root) if paths.legacy_diary_root else "",
                "reports": str(paths.reports_dir),
            },
            "intermediate": {
                "archives": str(paths.archives_dir),
                "taskIntelligence": str(paths.task_intelligence_dir),
            },
            "tasks": {
                "taskBoard": str(paths.task_board_path),
                "legacyTaskDatabase": str(config.TASK_DB_PATH),
            },
            "rag": {
                "legacyRagIndex": str(legacy_rag),
                "reservedRuntimeNamespace": str(paths.home / "reserved" / "rag"),
            },
            "logsCacheTmp": {
                "logs": str(paths.state_dir / "logs"),
                "cache": str(paths.state_dir / "cache"),
                "tmp": str(paths.state_dir / "tmp"),
                "backups": str(paths.state_dir / "backups"),
            },
        },
        "features": copy.deepcopy(FEATURE_DEFAULTS),
        "memorySearch": {
            "enabled": True,
            "backendPolicy": "auto",
            "local": {
                "enabled": True,
                "syncAfterPipeline": True,
                "maxScanFiles": 2000,
                "maxScanBytes": 67108864,
            },
            "nativeMemory": {
                "enabled": True,
                "allowInRag": True,
                "includeInstructions": True,
                "tools": {
                    "codex": True,
                    "claudeCode": True,
                },
            },
        },
        "runtimeSources": {
            field_name: RUNTIME_SOURCE_DEFAULTS[env_name]
            for env_name, field_name in RUNTIME_SOURCE_FIELDS.items()
        },
        "weather": {
            "enabled": True,
            "locationMode": "auto-ip",
            "latitude": None,
            "longitude": None,
            "label": "",
            "timezone": "auto",
            "cacheTtlHours": 24,
        },
        "rag": {
            "enabled": True,
            "mode": "v2",
            "languageProfile": "zh",
            "legacy": {
                "indexPath": str(legacy_rag / "index.jsonl"),
            },
            "v2": {
                "storePath": str(paths.home / "reserved" / "rag" / "v2"),
            },
            "embedding": {
                "mode": "local",
                "provider": "local",
                "providerId": "local",
                "model": "intfloat/multilingual-e5-small",
                "dimension": 384,
                "batchSize": 200,
                "device": "auto",
            },
            "server": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 3037,
                "healthPath": "/health",
            },
            "indexing": {
                "enabled": True,
                "defaultFullRebuild": False,
                "externalSources": {
                    "enabled": False,
                    "mode": "supplement",
                    "paths": [],
                    "recursive": True,
                    "include": ["*", "**/*"],
                    "exclude": [],
                    "maxFileBytes": 10485760,
                    "maxTotalBytes": 268435456,
                    "maxFiles": 10000,
                    "symlinkPolicy": "reject",
                },
                "sourceSets": [
                    "filtered-dialogue-daily",
                    "lessons",
                    "foundation-usage-rollups",
                    "foundation-dashboard-snapshots",
                    "diary-markdown-sections",
                    "diary-markdown-embedded-json",
                    "nova-task-work-graph-events",
                    "task-board-snapshot",
                    "foundation-period-projections",
                ],
            },
            "retrieval": {
                "topK": 8,
                "recencyHalfLifeDays": 7,
                "reranker": {"enabled": False, "provider": "none", "model": None},
                "tags": ["daily", "coding", "general", "task", "lesson", "incident", "decision"],
            },
        },
        "pipeline": {
            "stableCommand": "python advanced/pipeline/run_daily_pipeline.py [YYYY-MM-DD]",
            "languageProfile": default_pipeline_language.profile_id,
            "englishEnabled": False,
            "diarySchemaVersion": default_pipeline_language.diary_schema_version,
            "promptPayloadProfile": default_pipeline_language.prompt_payload_profile,
            "pythonExecutable": "python3",
            "workingDirectory": str(workspace),
            "dailyDateArgument": "YYYY-MM-DD",
            "skipFinalRagEnv": "ACTANARA_PIPELINE_SKIP_FINAL_RAG",
            "thinkingMode": "off",
            "stepTimeoutSeconds": 1800,
            "stepTimeouts": {
                "narrative_pass.py": 1800,
                "technical_pass.py": 1800,
                "learning_pass.py": 900,
                "rag_v2_sync.py": 1800,
            },
            "totalWatchdogSeconds": 7200,
        },
        "dashboard": {
            "projectRoot": str(workspace),
            "pythonExecutable": "python3",
            "appDir": str(workspace / "src" / "dashboard"),
            "host": "127.0.0.1",
            "port": 3036,
            "publicBaseUrl": "",
            "allowedOrigins": [],
            "healthPath": "/health",
            "logsDir": str(Path.home() / "Library" / "Logs" / "Actanara"),
            "serviceLabel": "com.actanara.dashboard",
            "watchdogLabel": "com.actanara.dashboard.watchdog",
        },
        "externalTools": default_external_tool_settings(),
        "llmProvider": default_llm_provider_settings(),
        "llmProviderChain": [],
        "llmProviderSecrets": {},
        "todos": {
            "githubUrl": "",
            "i18n": "todo-protected-prompt-review-required",
            "cliCommands": [
                "actanara settings show",
                "actanara settings scheduler status",
                "actanara llm-provider show",
            ],
        },
    }


def default_external_tool_settings(home: Path | None = None) -> dict:
    return default_external_tool_settings_from_catalog(home)


def _ensure_settings_unlocked(paths: RuntimePaths) -> dict:
    settings_path = _settings_path(paths)
    current = _read_json(settings_path)
    if current.get("schemaVersion") != SETTINGS_SCHEMA_VERSION:
        current = default_settings(paths)
        _sanitize_persisted_secrets(paths, current, migrate_persisted_refs=False)
        _write_json_atomic(settings_path, current)
    else:
        merged = _deep_merge(default_settings(paths), current)
        _preserve_legacy_manual_pipeline_gate(current, merged)
        _sanitize_persisted_secrets(paths, merged, migrate_persisted_refs=False)
        if merged != current:
            current = merged
            _write_json_atomic(settings_path, current)
    return current


def _read_settings_defaults_view(paths: RuntimePaths) -> dict:
    current = _read_json(_settings_path(paths))
    if current.get("schemaVersion") != SETTINGS_SCHEMA_VERSION:
        return default_settings(paths)
    merged = _deep_merge(default_settings(paths), current)
    _preserve_legacy_manual_pipeline_gate(current, merged)
    return merged


def _recover_linux_systemd_settings_barrier(paths: RuntimePaths) -> None:
    from .systemd_user import SystemdUserError, recover_user_unit_transactions

    recovery = recover_user_unit_transactions(
        paths,
        owner_id=None,
        _runtime_guard_held=True,
    )
    if any(item.get("status") in {"active", "conflict"} for item in recovery):
        raise SystemdUserError(
            "Settings persistence is blocked by an active or conflicting "
            "systemd transaction"
        )


def ensure_settings(paths: RuntimePaths | None = None) -> dict:
    paths = paths or load_paths()
    if platform.system() != "Linux":
        return _ensure_settings_unlocked(paths)

    from .runtime_mutation import (
        RuntimeMutationBusy,
        RuntimeMutationUnsafe,
        require_runtime_mutation_owner,
        runtime_mutation_guard,
    )

    try:
        with runtime_mutation_guard(paths.home, blocking=False):
            require_runtime_mutation_owner(paths.home, owner_id=None)
            with settings_mutation_barrier(
                paths,
                pretransaction_side_effects=lambda: (
                    _recover_linux_systemd_settings_barrier(paths)
                ),
            ):
                return _ensure_settings_unlocked(paths)
    except RuntimeMutationBusy:
        # A read that discovers additive defaults must not publish them across
        # an install/update or service Settings transaction.
        return _read_settings_defaults_view(paths)
    except RuntimeMutationUnsafe as exc:
        raise RuntimeError(str(exc)) from exc


def read_settings(paths: RuntimePaths | None = None, *, redact_secrets: bool = True, persist_defaults: bool = True) -> dict:
    paths = paths or load_paths()
    if platform.system() == "Linux" and not paths.config_dir.is_dir():
        # A read-only command aimed at a missing Runtime must not initialize
        # it.  Once initialize_home has established the managed config
        # boundary, preserve the existing additive-default persistence
        # contract through ensure_settings() and its mutation guard.
        persist_defaults = False
    if persist_defaults:
        settings = ensure_settings(paths)
    else:
        settings_path = _settings_path(paths)
        current = _read_json(settings_path)
        settings = _deep_merge(default_settings(paths), current) if current.get("schemaVersion") == SETTINGS_SCHEMA_VERSION else default_settings(paths)
    result = copy.deepcopy(settings)
    if redact_secrets:
        provider = result.setdefault("llmProvider", {})
        _redact_llm_provider_block(provider, settings=settings, paths=paths)
        chain = result.get("llmProviderChain")
        if isinstance(chain, list):
            for entry in chain:
                if isinstance(entry, dict):
                    _redact_llm_provider_block(entry, settings=settings, paths=paths)
    result["settingsPath"] = str(_settings_path(paths))
    return result


def _redact_llm_provider_block(
    provider: dict[str, Any],
    *,
    settings: dict[str, Any],
    paths: RuntimePaths,
) -> None:
    api_key = str(provider.get("apiKey") or "")
    secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else None
    migration_required = _secret_ref_requires_reentry(settings, secret_ref)
    secret_readable = _secret_ref_has_pipeline_readable_key(
        secret_ref,
        paths=paths,
        settings=settings,
    )
    provider["hasApiKey"] = bool(api_key or secret_readable)
    provider["hasSecretRef"] = bool(secret_ref)
    provider["secretReadable"] = secret_readable
    provider["secretMigrationRequired"] = migration_required
    provider["apiKey"] = MASKED_SECRET if api_key or secret_readable else ""


def write_settings(update: dict[str, Any], paths: RuntimePaths | None = None) -> dict:
    paths = paths or load_paths()
    if platform.system() == "Linux":
        # Legacy/internal callers still use this unrestricted writer. Route
        # them through the Runtime guard, Settings lock, crash journal, and
        # pending systemd handoff recovery used by the operator APIs.
        migrate_persisted_secret_refs(paths)
        return _write_operator_settings_transaction(
            paths,
            lambda _current: copy.deepcopy(update),
        )
    current = migrate_persisted_secret_refs(paths)
    merged, secret_writes, _ = _prepare_settings_update(
        current,
        update,
        paths,
        secret_ref_factory=lambda provider_id: _llm_provider_secret_ref(paths, provider_id),
        rag_secret_ref_factory=lambda provider_id: _rag_embedding_secret_ref(paths, provider_id),
    )
    for secret_ref, value in secret_writes:
        _store_secret_for_paths(secret_ref, value, paths)
    _write_json_atomic(_settings_path(paths), merged)
    return read_settings(paths)


def migrate_persisted_secret_refs(paths: RuntimePaths | None = None) -> dict:
    """Migrate legacy secret references only at an explicit mutation/use boundary.

    Generic settings reads, doctor checks, health probes, and service startup
    must remain read-only.  This boundary is called by settings writes and by
    non-redacted runtime LLM resolution, where a pipeline-readable credential
    is actually required.
    """
    paths = paths or load_paths()
    if platform.system() == "Linux":
        from .runtime_mutation import (
            RuntimeMutationBusy,
            RuntimeMutationUnsafe,
            require_runtime_mutation_owner,
            runtime_mutation_guard,
        )

        try:
            with runtime_mutation_guard(paths.home, blocking=False):
                require_runtime_mutation_owner(paths.home, owner_id=None)

                with settings_mutation_barrier(
                    paths,
                    pretransaction_side_effects=lambda: (
                        _recover_linux_systemd_settings_barrier(paths)
                    ),
                ):
                    current = _ensure_settings_unlocked(paths)
                    migrated = copy.deepcopy(current)
                    _sanitize_persisted_secrets(
                        paths,
                        migrated,
                        migrate_persisted_refs=True,
                    )
                    if migrated != current:
                        _write_json_atomic(_settings_path(paths), migrated)
                    return migrated
        except RuntimeMutationBusy:
            # Service startup and health resolution must remain read-only while
            # a fresh/install/update or another Settings handoff owns the
            # Runtime. Legacy refs remain directly readable for this attempt.
            return read_settings(
                paths,
                redact_secrets=False,
                persist_defaults=False,
            )
        except RuntimeMutationUnsafe as exc:
            raise RuntimeError(str(exc)) from exc
    current = ensure_settings(paths)
    migrated = copy.deepcopy(current)
    _sanitize_persisted_secrets(paths, migrated, migrate_persisted_refs=True)
    if migrated != current:
        _write_json_atomic(_settings_path(paths), migrated)
    return migrated


def _prepare_settings_update(
    current: dict[str, Any],
    update: dict[str, Any],
    paths: RuntimePaths,
    *,
    secret_ref_factory: Callable[[str], object],
    rag_secret_ref_factory: Callable[[str], object],
) -> tuple[dict[str, Any], tuple[tuple[dict, str], ...], tuple[dict, ...]]:
    merged = _deep_merge(current, update)
    _reconcile_pipeline_language_profile(merged, update)
    secret_writes: list[tuple[dict, str]] = []
    chain_update_present = "llmProviderChain" in update
    legacy_provider_update_present = "llmProvider" in update
    current_chain = _configured_llm_provider_chain(current)
    if chain_update_present:
        merged_chain = normalize_llm_provider_chain_update(
            update.get("llmProviderChain"),
            current_chain,
        )
        merged["llmProviderChain"] = _prepare_llm_provider_chain_secrets(
            merged_chain,
            current_chain,
            current.get("llmProvider") if isinstance(current.get("llmProvider"), dict) else {},
            secret_ref_factory=secret_ref_factory,
            secret_writes=secret_writes,
        )
        merged["llmProvider"] = _legacy_provider_from_chain_entry(merged["llmProviderChain"][0])

    provider = merged.setdefault("llmProvider", {})
    current_provider = current.get("llmProvider", {}) if isinstance(current.get("llmProvider"), dict) else {}
    provider_id = _llm_provider_secret_provider_id(provider)
    current_provider_id = _llm_provider_secret_provider_id(current_provider)
    provider_secrets = _llm_provider_secret_refs(merged)
    api_key = str(provider.get("apiKey") or "")
    if api_key and api_key != MASKED_SECRET:
        planned_ref = secret_ref_factory(provider_id)
        stored_ref = planned_ref.as_dict() if hasattr(planned_ref, "as_dict") else dict(planned_ref)
        secret_writes.append((stored_ref, api_key))
        provider_secrets[provider_id] = stored_ref
        provider["secretRef"] = stored_ref
        provider["apiKey"] = ""
    elif not api_key and current_provider.get("apiKey"):
        planned_ref = secret_ref_factory(provider_id)
        stored_ref = planned_ref.as_dict() if hasattr(planned_ref, "as_dict") else dict(planned_ref)
        secret_writes.append((stored_ref, str(current_provider["apiKey"])))
        provider_secrets[provider_id] = stored_ref
        provider["secretRef"] = stored_ref
        provider["apiKey"] = ""
    if not provider.get("secretRef") and provider_secrets.get(provider_id):
        provider["secretRef"] = provider_secrets[provider_id]
    elif not provider.get("secretRef") and provider_id == current_provider_id and current_provider.get("secretRef"):
        provider["secretRef"] = current_provider["secretRef"]
        provider_secrets[provider_id] = current_provider["secretRef"]
    elif provider_id != current_provider_id and provider.get("secretRef") == current_provider.get("secretRef"):
        provider.pop("secretRef", None)
    provider["apiKeyEnv"] = _normalized_persisted_api_key_env(str(provider.get("apiKeyEnv") or "LLM_API_KEY"))
    if isinstance(provider.get("secretRef"), dict):
        provider_secrets[provider_id] = copy.deepcopy(provider["secretRef"])
    merged["llmProviderSecrets"] = provider_secrets

    if chain_update_present:
        primary_entry = merged["llmProviderChain"][0]
        merged["llmProviderChain"][0] = {
            **_legacy_provider_from_chain_entry(provider),
            "entryId": primary_entry["entryId"],
        }
    elif legacy_provider_update_present and current_chain:
        merged["llmProviderChain"] = [
            {
                **_legacy_provider_from_chain_entry(provider),
                "entryId": current_chain[0]["entryId"],
            },
            *copy.deepcopy(current_chain[1:]),
        ]

    rag = merged.get("rag") if isinstance(merged.get("rag"), dict) else {}
    embedding = rag.get("embedding") if isinstance(rag.get("embedding"), dict) else {}
    current_rag = current.get("rag") if isinstance(current.get("rag"), dict) else {}
    current_embedding = (
        current_rag.get("embedding")
        if isinstance(current_rag.get("embedding"), dict)
        else {}
    )
    embedding_provider_id = _rag_embedding_secret_provider_id(embedding)
    current_embedding_provider_id = _rag_embedding_secret_provider_id(current_embedding)
    raw_embedding_api_key = str(embedding.get("apiKey") or "")
    if raw_embedding_api_key and raw_embedding_api_key != MASKED_SECRET:
        planned_ref = rag_secret_ref_factory(embedding_provider_id)
        stored_ref = planned_ref.as_dict() if hasattr(planned_ref, "as_dict") else dict(planned_ref)
        secret_writes.append((stored_ref, raw_embedding_api_key))
        embedding["secretRef"] = stored_ref
    elif raw_embedding_api_key == MASKED_SECRET:
        if not isinstance(embedding.get("secretRef"), dict) and isinstance(current_embedding.get("secretRef"), dict):
            embedding["secretRef"] = current_embedding["secretRef"]
    elif (
        embedding_provider_id != current_embedding_provider_id
        and embedding.get("secretRef") == current_embedding.get("secretRef")
    ):
        embedding.pop("secretRef", None)
    embedding.pop("apiKey", None)

    merged["schemaVersion"] = SETTINGS_SCHEMA_VERSION
    merged["updatedAt"] = _now_iso()
    previous_refs = _settings_secret_refs(current)
    next_refs = _settings_secret_refs(merged)
    garbage_collection_candidates = tuple(
        ref
        for resource_id, ref in previous_refs.items()
        if resource_id not in next_refs
    )
    return merged, tuple(secret_writes), garbage_collection_candidates


def _prepare_llm_provider_chain_secrets(
    chain: list[dict[str, Any]],
    current_chain: list[dict[str, Any]],
    current_provider: dict[str, Any],
    *,
    secret_ref_factory: Callable[[str], object],
    secret_writes: list[tuple[dict, str]],
) -> list[dict[str, Any]]:
    current_by_id = {
        str(entry.get("entryId")): entry
        for entry in current_chain
        if isinstance(entry, dict) and entry.get("entryId")
    }
    prepared: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(chain):
        entry = copy.deepcopy(raw_entry)
        entry_id = str(entry["entryId"])
        provider_id = _llm_provider_secret_provider_id(entry)
        current_entry = current_by_id.get(entry_id, {})
        same_provider = (
            bool(current_entry)
            and provider_id == _llm_provider_secret_provider_id(current_entry)
        )
        raw_api_key = str(entry.get("apiKey") or "")
        if raw_api_key and raw_api_key != MASKED_SECRET:
            planned_ref = secret_ref_factory(_llm_provider_chain_secret_slot(entry))
            stored_ref = planned_ref.as_dict() if hasattr(planned_ref, "as_dict") else dict(planned_ref)
            secret_writes.append((stored_ref, raw_api_key))
            entry["secretRef"] = stored_ref
        elif same_provider and isinstance(current_entry.get("secretRef"), dict):
            entry["secretRef"] = copy.deepcopy(current_entry["secretRef"])
        elif (
            index == 0
            and provider_id == _llm_provider_secret_provider_id(current_provider)
            and isinstance(current_provider.get("secretRef"), dict)
        ):
            entry["secretRef"] = copy.deepcopy(current_provider["secretRef"])
        else:
            entry.pop("secretRef", None)
        entry["apiKey"] = ""
        entry["apiKeyEnv"] = _normalized_persisted_api_key_env(
            str(entry.get("apiKeyEnv") or "LLM_API_KEY")
        )
        prepared.append(entry)
    return prepared


def _configured_llm_provider_chain(settings: dict[str, Any]) -> list[dict[str, Any]]:
    chain = settings.get("llmProviderChain")
    if not isinstance(chain, list):
        return []
    return [copy.deepcopy(entry) for entry in chain if isinstance(entry, dict)]


def _llm_provider_chain_secret_slot(entry: dict[str, Any]) -> str:
    return (
        f"chain:{str(entry.get('entryId') or 'provider')}:"
        f"{_llm_provider_secret_provider_id(entry)}"
    )


def _legacy_provider_from_chain_entry(entry: dict[str, Any]) -> dict[str, Any]:
    legacy = copy.deepcopy(entry)
    for key in (
        "entryId",
        "order",
        "role",
        "readiness",
        "hasApiKey",
        "hasSecretRef",
        "secretReadable",
        "secretMigrationRequired",
    ):
        legacy.pop(key, None)
    return legacy


def _settings_secret_refs(settings: dict[str, Any]) -> dict[tuple[str, str, str], dict]:
    refs: list[dict] = list(_llm_provider_secret_refs(settings).values())
    provider = settings.get("llmProvider") if isinstance(settings.get("llmProvider"), dict) else {}
    if isinstance(provider.get("secretRef"), dict):
        refs.append(provider["secretRef"])
    for entry in _configured_llm_provider_chain(settings):
        if isinstance(entry.get("secretRef"), dict):
            refs.append(entry["secretRef"])
    rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
    embedding = rag.get("embedding") if isinstance(rag.get("embedding"), dict) else {}
    if isinstance(embedding.get("secretRef"), dict):
        refs.append(embedding["secretRef"])
    result: dict[tuple[str, str, str], dict] = {}
    for ref in refs:
        normalized = {
            "backend": str(ref.get("backend") or ""),
            "service": str(ref.get("service") or "actanara"),
            "account": str(ref.get("account") or ""),
        }
        resource_id = (normalized["backend"], normalized["service"], normalized["account"])
        if normalized["backend"] and normalized["account"]:
            result[resource_id] = normalized
    return result


def _reconcile_pipeline_language_profile(merged: dict[str, Any], update: dict[str, Any]) -> None:
    requested_pipeline = update.get("pipeline") if isinstance(update, dict) else None
    if not isinstance(requested_pipeline, dict) or "languageProfile" not in requested_pipeline:
        return
    pipeline = merged.setdefault("pipeline", {})
    if not isinstance(pipeline, dict):
        merged["pipeline"] = pipeline = {}
    profile = resolve_pipeline_language_profile(pipeline.get("languageProfile"))
    pipeline["languageProfile"] = profile.profile_id
    if "diarySchemaVersion" not in requested_pipeline:
        pipeline["diarySchemaVersion"] = profile.diary_schema_version
    if "promptPayloadProfile" not in requested_pipeline:
        pipeline["promptPayloadProfile"] = profile.prompt_payload_profile


def validate_operator_settings_update(update: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(update, dict):
        raise ValueError("settings update must be an object")
    requested = set(update)
    protected = sorted(requested & OPERATOR_SETTINGS_WRITE_PROTECTED_TOP_LEVEL)
    unsupported = sorted(requested - OPERATOR_SETTINGS_WRITE_ALLOWED_TOP_LEVEL - OPERATOR_SETTINGS_WRITE_PROTECTED_TOP_LEVEL)
    if protected:
        raise ValueError(
            "protected settings groups require a dedicated API: "
            + ", ".join(protected)
        )
    if unsupported:
        raise ValueError("unsupported settings groups: " + ", ".join(unsupported))
    allowed = {key: update[key] for key in sorted(requested) if key in OPERATOR_SETTINGS_WRITE_ALLOWED_TOP_LEVEL}
    _validate_general_update(allowed.get("general"))
    _validate_dashboard_update(allowed.get("dashboard"))
    _validate_schedule_update(allowed.get("schedule"))
    _validate_memory_search_update(allowed.get("memorySearch"))
    _validate_external_tools_update(allowed.get("externalTools"))
    _validate_paths_update(allowed.get("paths"))
    _validate_pipeline_update(allowed.get("pipeline"))
    _validate_runtime_sources_update(allowed.get("runtimeSources"))
    _validate_weather_update(allowed.get("weather"))
    return allowed


def write_operator_settings(update: dict[str, Any], paths: RuntimePaths | None = None) -> dict:
    paths = paths or load_paths()
    allowed = validate_operator_settings_update(update)
    allowed = _normalize_diary_path_update(allowed, paths)
    return _write_operator_settings_transaction(
        paths,
        lambda current: _mark_system_timer_stale_if_needed(current, allowed),
    )


def write_operator_settings_bundle(
    payload: dict[str, Any],
    paths: RuntimePaths | None = None,
    *,
    readiness_verifier: Callable[[], None] | None = None,
) -> dict:
    """Atomically persist operator settings plus dedicated RAG/LLM groups."""
    if not isinstance(payload, dict):
        raise ValueError("settings bundle must be an object")
    paths = paths or load_paths()
    settings_update = payload.get("settings") if "settings" in payload else {
        key: value for key, value in payload.items() if key in OPERATOR_SETTINGS_WRITE_ALLOWED_TOP_LEVEL
    }
    allowed_settings: dict[str, Any] = {}
    if settings_update:
        allowed_settings = validate_operator_settings_update(settings_update)
        allowed_settings = _normalize_diary_path_update(allowed_settings, paths)
    normalized_rag: dict[str, Any] | None = None
    if "rag" in payload:
        rag_update = payload.get("rag")
        if isinstance(rag_update, dict) and isinstance(rag_update.get("rag"), dict):
            rag_update = rag_update["rag"]
        _validate_operator_rag_update(rag_update)
        normalized_rag = normalize_rag_settings_update(rag_update)
    raw_provider_update = (
        payload.get("llmProvider")
        if "llmProvider" in payload and isinstance(payload.get("llmProvider"), dict)
        else None
    )
    raw_provider_chain_update = payload.get("llmProviderChain") if "llmProviderChain" in payload else None
    if (
        not allowed_settings
        and normalized_rag is None
        and raw_provider_update is None
        and raw_provider_chain_update is None
    ):
        raise ValueError("settings bundle has no supported changes")

    def build_update(current: dict[str, Any]) -> dict[str, Any]:
        combined: dict[str, Any] = {}
        if allowed_settings:
            combined = _deep_merge(
                combined,
                _mark_system_timer_stale_if_needed(current, allowed_settings),
            )
        if normalized_rag is not None:
            combined["rag"] = normalized_rag
        if raw_provider_update is not None:
            current_provider = current.get("llmProvider", {})
            normalized_provider = normalize_llm_provider_update(
                raw_provider_update,
                current_provider if isinstance(current_provider, dict) else {},
            )
            _validate_llm_provider_complete(normalized_provider)
            combined["llmProvider"] = normalized_provider
        if raw_provider_chain_update is not None:
            current_chain = _configured_llm_provider_chain(current)
            normalized_chain = normalize_llm_provider_chain_update(
                raw_provider_chain_update,
                current_chain,
            )
            for provider_index, normalized_provider in enumerate(normalized_chain):
                try:
                    _validate_llm_provider_complete(normalized_provider)
                except ValueError as exc:
                    raise ValueError(
                        f"llmProviderChain[{provider_index}] is invalid: {exc}"
                    ) from None
            combined["llmProviderChain"] = normalized_chain
        return combined

    return _write_operator_settings_transaction(
        paths,
        build_update,
        readiness_verifier=readiness_verifier,
    )


def _write_operator_settings_transaction(
    paths: RuntimePaths,
    update_builder: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    readiness_verifier: Callable[[], None] | None = None,
    precommit_side_effects: Callable[[dict], Callable[[], None] | None] | None = None,
    postcommit_side_effects: Callable[[dict], None] | None = None,
    runtime_mutation_owner_id: str | None = None,
) -> dict:
    def prepare(
        transaction_id: str,
        settings_before: bytes | None,
        manifest_before: bytes | None,
    ) -> SettingsTransactionPlan:
        persisted = _decode_json_document(settings_before)
        if persisted.get("schemaVersion") == SETTINGS_SCHEMA_VERSION:
            current = _deep_merge(default_settings(paths), persisted)
            _preserve_legacy_manual_pipeline_gate(persisted, current)
        else:
            current = default_settings(paths)
        update = update_builder(copy.deepcopy(current))
        merged, secret_writes, garbage_collection_candidates = _prepare_settings_update(
            current,
            update,
            paths,
            secret_ref_factory=lambda provider_id: settings_transaction_secret_ref(
                str(paths.home),
                transaction_id,
                provider_id=provider_id,
            ),
            rag_secret_ref_factory=lambda provider_id: settings_transaction_secret_ref(
                str(paths.home),
                transaction_id,
                provider_id=f"rag-embedding:{provider_id}",
            ),
        )
        manifest = _runtime_manifest_payload_from_settings(
            merged,
            paths,
            _decode_json_document(manifest_before),
        )
        return SettingsTransactionPlan(
            settings_bytes=_encode_json_document(merged),
            manifest_bytes=_encode_json_document(manifest),
            secret_writes=secret_writes,
            garbage_collection_candidates=garbage_collection_candidates,
        )

    def execute() -> dict:
        pretransaction = None
        if platform.system() == "Linux":
            def recover_systemd_handoffs() -> None:
                from .systemd_user import (
                    SystemdUserError,
                    recover_user_unit_transactions,
                )

                recovery = recover_user_unit_transactions(
                    paths,
                    owner_id=runtime_mutation_owner_id,
                    _runtime_guard_held=True,
                )
                if any(
                    item.get("status") in {"active", "conflict"}
                    for item in recovery
                ):
                    raise SystemdUserError(
                        "Settings mutation is blocked by an active or conflicting systemd transaction"
                    )

            pretransaction = recover_systemd_handoffs
        return execute_settings_transaction(
            paths,
            prepare,
            verify=readiness_verifier,
            pretransaction_side_effects=pretransaction,
            precommit_side_effects=precommit_side_effects,
            postcommit_side_effects=postcommit_side_effects,
            apply_side_effects=lambda: _ensure_runtime_manifest_directories(paths),
        )

    if platform.system() == "Linux":
        from .runtime_mutation import (
            RuntimeMutationBusy,
            RuntimeMutationUnsafe,
            require_runtime_mutation_owner,
            runtime_mutation_guard,
        )

        try:
            # Peer Settings writers have no durable install/update owner and
            # must serialize through this outer namespace guard before the
            # Settings lock can merge their updates. Long-lived Runtime
            # transactions are still rejected by the owner check below.
            with runtime_mutation_guard(paths.home, blocking=True):
                require_runtime_mutation_owner(
                    paths.home,
                    owner_id=runtime_mutation_owner_id,
                )
                summary = execute()
        except (RuntimeMutationBusy, RuntimeMutationUnsafe) as exc:
            raise RuntimeError(str(exc)) from exc
    else:
        summary = execute()
    saved = read_settings(paths)
    saved["settingsTransaction"] = summary
    return saved


def write_scheduler_handoff_settings(
    schedule_update: dict[str, Any],
    paths: RuntimePaths,
    *,
    precommit_side_effects: Callable[[dict], Callable[[], None] | None],
    postcommit_side_effects: Callable[[dict], None] | None = None,
    current_validator: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Commit scheduler desired state around one explicit external handoff."""
    allowed = validate_operator_settings_update({"schedule": schedule_update})
    def build_update(current: dict[str, Any]) -> dict[str, Any]:
        if current_validator is not None:
            current_validator(current)
        return allowed

    return _write_operator_settings_transaction(
        paths,
        build_update,
        precommit_side_effects=precommit_side_effects,
        postcommit_side_effects=postcommit_side_effects,
    )


def write_service_manager_settings(
    registration_update: dict[str, Any],
    paths: RuntimePaths,
    *,
    precommit_side_effects: Callable[[dict], Callable[[], None] | None],
    postcommit_side_effects: Callable[[dict], None] | None = None,
    current_validator: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Commit internal service-registration metadata around an external handoff."""

    if not isinstance(registration_update, dict) or not registration_update:
        raise ValueError("service registration update must be a non-empty object")
    if set(registration_update) - {"dashboard", "rag"}:
        raise ValueError("service registration update contains an unsupported group")
    def build_update(current: dict[str, Any]) -> dict[str, Any]:
        if current_validator is not None:
            current_validator(current)
        return registration_update

    return _write_operator_settings_transaction(
        paths,
        build_update,
        precommit_side_effects=precommit_side_effects,
        postcommit_side_effects=postcommit_side_effects,
    )


def write_linux_installer_handoff_settings(
    registration_update: dict[str, Any],
    paths: RuntimePaths,
    *,
    precommit_side_effects: Callable[[dict], Callable[[], None] | None],
    postcommit_side_effects: Callable[[dict], None] | None = None,
    runtime_mutation_owner_id: str | None = None,
) -> dict:
    """Commit Linux installer registration metadata around its unit handoff."""

    if not isinstance(registration_update, dict) or not registration_update:
        raise ValueError("Linux installer registration update must be a non-empty object")
    if set(registration_update) - {"dashboard", "rag", "schedule"}:
        raise ValueError("Linux installer registration update contains an unsupported group")
    return _write_operator_settings_transaction(
        paths,
        lambda _current: registration_update,
        precommit_side_effects=precommit_side_effects,
        postcommit_side_effects=postcommit_side_effects,
        runtime_mutation_owner_id=runtime_mutation_owner_id,
    )


def write_linux_fresh_install_settings(
    settings_update: dict[str, Any],
    paths: RuntimePaths,
    *,
    precommit_side_effects: Callable[[dict], Callable[[], None] | None],
    runtime_mutation_owner_id: str,
) -> dict:
    """Commit the complete fresh-install Settings bundle with an outer intent hook."""

    if not isinstance(settings_update, dict):
        raise ValueError("fresh-install settings update must be an object")
    pipeline_update = (
        settings_update.get("pipeline")
        if isinstance(settings_update.get("pipeline"), dict)
        else None
    )
    base_update = {
        key: value
        for key, value in settings_update.items()
        if key not in {"rag", "pipeline"}
    }
    if pipeline_update is not None:
        base_update["pipeline"] = {
            key: value
            for key, value in pipeline_update.items()
            if key not in INSTALL_ONLY_PIPELINE_FIELDS
        }
    allowed = validate_operator_settings_update(base_update)
    if pipeline_update is not None:
        allowed["pipeline"] = copy.deepcopy(pipeline_update)
    if "rag" in settings_update:
        # languageProfile is immutable through normal operator writes but is
        # selected exactly once at fresh-install bootstrap.
        allowed["rag"] = normalize_rag_settings_update(settings_update["rag"])
    return _write_operator_settings_transaction(
        paths,
        lambda _current: allowed,
        precommit_side_effects=precommit_side_effects,
        runtime_mutation_owner_id=runtime_mutation_owner_id,
    )


def write_backup_settings(
    backup_update: dict[str, Any],
    paths: RuntimePaths | None = None,
    *,
    readiness_verifier: Callable[[], None] | None = None,
) -> dict:
    """Persist the dedicated data-backup policy through the settings transaction."""
    paths = paths or load_paths()
    normalized = normalize_backup_settings_update(backup_update)

    def build_update(current: dict[str, Any]) -> dict[str, Any]:
        current_backup = current.get("backup") if isinstance(current.get("backup"), dict) else {}
        complete = _deep_merge(current_backup, normalized)
        _validate_complete_backup_settings(complete)
        return {"backup": normalized}

    return _write_operator_settings_transaction(
        paths,
        build_update,
        readiness_verifier=readiness_verifier,
    )


def _decode_json_document(content: bytes | None) -> dict[str, Any]:
    if content is None:
        return {}
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _encode_json_document(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _runtime_manifest_payload_from_settings(
    settings: dict[str, Any],
    paths: RuntimePaths,
    current_manifest: dict[str, Any],
) -> dict[str, Any]:
    manifest = copy.deepcopy(current_manifest)
    if not manifest:
        manifest = {
            "instanceId": "local-primary",
            "schemaVersion": RUNTIME_SCHEMA_VERSION,
            "createdAt": _now_iso(),
            "ragMode": "legacy-external",
        }
    manifest["schemaVersion"] = RUNTIME_SCHEMA_VERSION
    path_settings = settings.get("paths") if isinstance(settings.get("paths"), dict) else {}
    diary = path_settings.get("diary") if isinstance(path_settings.get("diary"), dict) else {}
    runtime = path_settings.get("runtime") if isinstance(path_settings.get("runtime"), dict) else {}
    intermediate = (
        path_settings.get("intermediate")
        if isinstance(path_settings.get("intermediate"), dict)
        else {}
    )
    tasks = path_settings.get("tasks") if isinstance(path_settings.get("tasks"), dict) else {}
    updates = {
        "generatedDiaryRoot": diary.get("generatedDiary"),
        "legacyDiaryRoot": diary.get("legacyDiaryRoot"),
        "databasePath": runtime.get("database"),
        "snapshotsRoot": runtime.get("snapshots"),
        "reportsRoot": diary.get("reports"),
        "archivesRoot": intermediate.get("archives"),
        "taskBoardPath": tasks.get("taskBoard"),
        "taskIntelligenceRoot": intermediate.get("taskIntelligence"),
    }
    for key, raw_value in updates.items():
        value = str(raw_value or "").strip()
        if value:
            manifest[key] = str(Path(value).expanduser().absolute())
    return manifest


def _ensure_runtime_manifest_directories(paths: RuntimePaths) -> Callable[[], None]:
    resolved = runtime_paths_for_home(paths.home)
    directories = [
        resolved.db_path.parent,
        resolved.archives_dir,
        resolved.diary_dir,
        resolved.reports_dir / "weekly",
        resolved.reports_dir / "monthly",
        resolved.task_board_path.parent,
        resolved.task_intelligence_dir,
        resolved.snapshots_dir / "dashboard",
        resolved.snapshots_dir / "reports",
    ]
    if resolved.legacy_diary_root:
        directories.append(resolved.legacy_diary_root)
    absent = [directory for directory in directories if not directory.exists()]

    def cleanup() -> None:
        for directory in reversed(absent):
            try:
                directory.rmdir()
            except (FileNotFoundError, OSError):
                continue

    try:
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
    except Exception:
        cleanup()
        raise

    return cleanup


def _mark_system_timer_stale_if_needed(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    schedule = current.get("schedule") if isinstance(current.get("schedule"), dict) else {}
    timer = schedule.get("systemTimer") if isinstance(schedule.get("systemTimer"), dict) else {}
    schedule_update = update.get("schedule") if isinstance(update.get("schedule"), dict) else {}
    mode_changed = "mode" in schedule_update and schedule_update.get("mode") != schedule.get("mode", "system")
    enabled_changed = "enabled" in schedule_update and bool(schedule_update.get("enabled")) != bool(schedule.get("enabled"))
    if mode_changed or enabled_changed:
        proposed_enabled = bool(schedule_update.get("enabled", schedule.get("enabled")))
        proposed_mode = str(schedule_update.get("mode", schedule.get("mode", "system")))
        proposed_registered = proposed_enabled and proposed_mode == "system"
        if proposed_registered != bool(timer.get("registered")):
            raise ValueError(
                "scheduler-handoff-required: change system/agent scheduler ownership through the explicit handoff API"
            )
    if timer.get("provider", default_timer_provider()) not in {"launchd", "systemd"} or not timer.get("registered"):
        return update
    if not _update_affects_system_timer(update):
        return update
    marked = copy.deepcopy(update)
    schedule_update = marked.setdefault("schedule", {})
    if not isinstance(schedule_update, dict):
        return update
    timer_update = schedule_update.setdefault("systemTimer", {})
    if not isinstance(timer_update, dict):
        return update
    timer_update.update(
        {
            "stale": True,
            "reinstallRequired": True,
            "staleReason": "operator-settings-changed",
            "staleAt": _now_iso(),
            "lastActionStatus": "registered-stale",
        }
    )
    return marked


def _update_affects_system_timer(update: dict[str, Any]) -> bool:
    schedule = update.get("schedule") if isinstance(update.get("schedule"), dict) else {}
    if set(schedule) & {"timezone", "dailyPipelineTime", "dashboardAggregationTime"}:
        return True
    system_timer = schedule.get("systemTimer") if isinstance(schedule.get("systemTimer"), dict) else {}
    if set(system_timer) & {"provider", "label"}:
        return True
    pipeline = update.get("pipeline") if isinstance(update.get("pipeline"), dict) else {}
    if set(pipeline) & {"pythonExecutable", "workingDirectory"}:
        return True
    return False


def _validate_operator_rag_update(update: Any) -> None:
    if not isinstance(update, dict):
        return
    if "languageProfile" in update:
        raise ValueError("rag.languageProfile is immutable after install; choose the language profile during installer/runtime bootstrap.")


def normalize_rag_settings_update(update: Any) -> dict:
    if not isinstance(update, dict):
        raise ValueError("rag settings must be an object")
    normalized = json.loads(json.dumps(update))
    server = normalized.get("server")
    if isinstance(server, dict) and "host" in server:
        server["host"] = require_loopback_host(server.get("host"))
    indexing = normalized.get("indexing")
    if indexing is not None and not isinstance(indexing, dict):
        raise ValueError("rag.indexing must be an object")
    if isinstance(indexing, dict) and "externalSources" in indexing:
        indexing["externalSources"] = _normalize_external_sources_update(indexing.get("externalSources"))
    embedding = normalized.get("embedding")
    if not isinstance(embedding, dict):
        return normalized
    raw_api_key = str(embedding.get("apiKey") or "")
    if raw_api_key:
        # This field is write-only.  _prepare_settings_update moves it into the
        # secret backend before any Settings bytes are encoded or persisted.
        embedding["apiKey"] = raw_api_key
    secret_ref = embedding.get("secretRef")
    if secret_ref in (None, "", {}):
        embedding.pop("secretRef", None)
    elif isinstance(secret_ref, dict):
        allowed = {
            "backend": str(secret_ref.get("backend") or "").strip(),
            "service": str(secret_ref.get("service") or "actanara").strip(),
            "account": str(secret_ref.get("account") or "").strip(),
        }
        if not allowed["backend"] or not allowed["account"]:
            raise ValueError("rag.embedding.secretRef requires backend and account")
        embedding["secretRef"] = allowed
    else:
        raise ValueError("rag.embedding.secretRef must be an object")
    return normalized


def _normalize_external_sources_update(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("rag.indexing.externalSources must be an object")
    normalized = copy.deepcopy(value)
    for field in ("enabled", "recursive"):
        if field in normalized and type(normalized[field]) is not bool:
            raise ValueError(f"rag.indexing.externalSources.{field} must be a boolean")
    if "mode" in normalized:
        mode = str(normalized.get("mode") or "").strip()
        if mode not in {"supplement", "replace"}:
            raise ValueError("rag.indexing.externalSources.mode must be supplement or replace")
        normalized["mode"] = mode
    if "symlinkPolicy" in normalized:
        policy = str(normalized.get("symlinkPolicy") or "").strip()
        if policy not in {"reject", "within-root"}:
            raise ValueError("rag.indexing.externalSources.symlinkPolicy must be reject or within-root")
        normalized["symlinkPolicy"] = policy
    if "paths" in normalized:
        paths = normalized.get("paths")
        if not isinstance(paths, list):
            raise ValueError("rag.indexing.externalSources.paths must be a list")
        normalized_paths: list[str] = []
        for item in paths:
            raw = str(item or "").strip()
            if not raw:
                raise ValueError("rag.indexing.externalSources.paths entries must be non-empty strings")
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ValueError("rag.indexing.externalSources.paths entries must be absolute paths")
            normalized_paths.append(str(path.absolute()))
        normalized["paths"] = list(dict.fromkeys(normalized_paths))
    for field in ("include", "exclude"):
        if field not in normalized:
            continue
        patterns = normalized.get(field)
        if not isinstance(patterns, list):
            raise ValueError(f"rag.indexing.externalSources.{field} must be a list")
        normalized_patterns: list[str] = []
        for item in patterns:
            pattern = str(item or "").strip().replace("\\", "/")
            if not pattern or pattern.startswith("/") or ".." in pattern.split("/"):
                raise ValueError(
                    f"rag.indexing.externalSources.{field} contains an unsafe traversal pattern: {item!r}"
                )
            normalized_patterns.append(pattern)
        normalized[field] = list(dict.fromkeys(normalized_patterns))
    for field in ("maxFileBytes", "maxTotalBytes", "maxFiles"):
        if field not in normalized:
            continue
        raw = normalized.get(field)
        if type(raw) is bool:
            raise ValueError(f"rag.indexing.externalSources.{field} must be a positive integer")
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"rag.indexing.externalSources.{field} must be a positive integer") from exc
        if parsed <= 0:
            raise ValueError(f"rag.indexing.externalSources.{field} must be a positive integer")
        normalized[field] = parsed
    return normalized


def _normalize_diary_path_update(update: dict[str, Any], paths: RuntimePaths) -> dict[str, Any]:
    path_update = update.get("paths")
    if not isinstance(path_update, dict):
        return update
    diary_update = path_update.get("diary")
    if not isinstance(diary_update, dict) or "generatedDiary" not in diary_update:
        return update
    generated = str(diary_update.get("generatedDiary") or "").strip()
    if not generated:
        return update
    return update


def _sync_runtime_manifest_from_settings(settings: dict[str, Any], paths: RuntimePaths) -> None:
    path_settings = settings.get("paths") if isinstance(settings.get("paths"), dict) else {}
    if not path_settings:
        return
    diary = path_settings.get("diary", {}) if isinstance(path_settings.get("diary"), dict) else {}
    runtime = path_settings.get("runtime", {}) if isinstance(path_settings.get("runtime"), dict) else {}
    intermediate = path_settings.get("intermediate", {}) if isinstance(path_settings.get("intermediate"), dict) else {}
    tasks = path_settings.get("tasks", {}) if isinstance(path_settings.get("tasks"), dict) else {}
    generated = str(diary.get("generatedDiary") or "").strip()
    legacy = str(diary.get("legacyDiaryRoot") or "").strip()

    def optional_path(value: Any) -> Path | None:
        text = str(value or "").strip()
        return Path(text) if text else None

    update_runtime_manifest_paths(
        paths.home,
        generated_diary_root=optional_path(generated),
        legacy_diary_root=optional_path(legacy),
        database_path=optional_path(runtime.get("database")),
        snapshots_root=optional_path(runtime.get("snapshots")),
        reports_root=optional_path(diary.get("reports")),
        archives_root=optional_path(intermediate.get("archives")),
        task_board_path=optional_path(tasks.get("taskBoard")),
        task_intelligence_root=optional_path(intermediate.get("taskIntelligence")),
    )


def _validate_general_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("general settings must be an object")
    for key in ("appName", "environment", "timezone", "locale"):
        if key in update and not str(update.get(key) or "").strip():
            raise ValueError(f"general.{key} must be a non-empty string")
    if "timezone" in update:
        _validate_timezone_name("general.timezone", update.get("timezone"))
    for key in ("workspaceRoot", "tmpWorkspace"):
        if key in update:
            _validate_path_string(f"general.{key}", update.get(key))


def _validate_dashboard_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("dashboard settings must be an object")
    for key in ("projectRoot", "pythonExecutable", "appDir", "logsDir"):
        if key in update:
            _validate_path_string(f"dashboard.{key}", update.get(key))
    for key in ("host", "healthPath", "serviceLabel", "watchdogLabel"):
        if key in update and not str(update.get(key) or "").strip():
            raise ValueError(f"dashboard.{key} must be a non-empty string")
    if "publicBaseUrl" in update:
        public_base_url = str(update.get("publicBaseUrl") or "").strip()
        if public_base_url:
            _validate_http_url("dashboard.publicBaseUrl", public_base_url)
    if "allowedOrigins" in update:
        origins = update.get("allowedOrigins")
        if not isinstance(origins, list):
            raise ValueError("dashboard.allowedOrigins must be a list")
        for idx, origin in enumerate(origins):
            _validate_http_origin(f"dashboard.allowedOrigins[{idx}]", origin)
    if "port" in update:
        try:
            port = int(update.get("port"))
        except (TypeError, ValueError):
            raise ValueError("dashboard.port must be between 1 and 65535") from None
        if port < 1 or port > 65535:
            raise ValueError("dashboard.port must be between 1 and 65535")


def _validate_http_url(field: str, value: Any) -> None:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field} must be an http(s) URL")


def _validate_http_origin(field: str, value: Any) -> None:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"}:
        raise ValueError(f"{field} must be an http(s) origin")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{field} must be an origin without path, query, or fragment")


def _validate_timezone_name(field: str, value: Any) -> None:
    raw_name = str(value or "")
    name = raw_name.strip()
    if name != raw_name:
        raise ValueError(f"{field} must be a valid IANA timezone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{field} must be a valid IANA timezone") from None


def _validate_schedule_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("schedule settings must be an object")
    if "mode" in update and update.get("mode") not in {"system", "agent"}:
        raise ValueError("schedule.mode must be one of: agent, system")
    if "timezone" in update:
        _validate_timezone_name("schedule.timezone", update.get("timezone"))
        if platform.system() == "Darwin":
            system_timezone = detect_system_timezone_authority()
            if not system_timezone:
                raise ValueError("schedule.timezone cannot be saved because the macOS system timezone is unavailable")
            if str(update.get("timezone")) != system_timezone:
                raise ValueError(
                    "schedule.timezone must match the macOS system timezone; "
                    f"configured={update.get('timezone')} system={system_timezone}"
                )
    for key in ("dailyPipelineTime", "dashboardAggregationTime"):
        if key in update and not TIME_OF_DAY_RE.match(str(update.get(key) or "")):
            raise ValueError(f"schedule.{key} must use HH:MM 24-hour time")
    refresh_targets = update.get("refreshTargets")
    if refresh_targets is not None and not isinstance(refresh_targets, dict):
        raise ValueError("schedule.refreshTargets must be an object")
    system_timer = update.get("systemTimer")
    if system_timer is not None:
        if not isinstance(system_timer, dict):
            raise ValueError("schedule.systemTimer must be an object")
        provider = system_timer.get("provider")
        if provider is not None and provider not in {"launchd", "systemd"}:
            raise ValueError("schedule.systemTimer.provider must be one of: launchd, systemd")
        label = system_timer.get("label")
        if label is not None and not str(label or "").strip():
            raise ValueError("schedule.systemTimer.label must be a non-empty string")


BACKUP_INCLUDE_FIELDS = {
    "database",
    "diaryMarkdown",
    "periodReports",
    "ragV2",
    "novaTaskExports",
    "settings",
    "workspaceAttribution",
    "runtimeManifests",
}


def normalize_backup_settings_update(update: Any) -> dict[str, Any]:
    """Validate and normalize the additive backup settings group."""
    if not isinstance(update, dict):
        raise ValueError("backup settings must be an object")
    unexpected = sorted(set(update) - {"targetDirectory", "include", "retention", "schedule"})
    if unexpected:
        raise ValueError("unsupported backup settings fields: " + ", ".join(unexpected))
    normalized = copy.deepcopy(update)
    if "targetDirectory" in normalized:
        raw_target = str(normalized.get("targetDirectory") or "").strip()
        if "\x00" in raw_target:
            raise ValueError("backup.targetDirectory contains a NUL byte")
        if raw_target:
            raw_path = Path(raw_target).expanduser()
            if not raw_path.is_absolute():
                raise ValueError("backup.targetDirectory must be an absolute path")
            if ".." in Path(raw_target).parts:
                raise ValueError("backup.targetDirectory must not contain directory traversal")
            normalized["targetDirectory"] = str(raw_path.absolute())
        else:
            normalized["targetDirectory"] = ""
    include = normalized.get("include")
    if include is not None:
        if not isinstance(include, dict):
            raise ValueError("backup.include must be an object")
        unknown = sorted(set(include) - BACKUP_INCLUDE_FIELDS)
        if unknown:
            raise ValueError("unsupported backup.include fields: " + ", ".join(unknown))
        for key, value in include.items():
            if type(value) is not bool:
                raise ValueError(f"backup.include.{key} must be a boolean")
    retention = normalized.get("retention")
    if retention is not None:
        if not isinstance(retention, dict):
            raise ValueError("backup.retention must be an object")
        unknown = sorted(set(retention) - {"maxBackups", "maxAgeDays"})
        if unknown:
            raise ValueError("unsupported backup.retention fields: " + ", ".join(unknown))
        for key, upper in (("maxBackups", 1000), ("maxAgeDays", 36500)):
            if key not in retention:
                continue
            value = retention[key]
            if type(value) is bool or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"backup.retention.{key} must be an integer in 1..{upper}")
    schedule = normalized.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            raise ValueError("backup.schedule must be an object")
        unknown = sorted(set(schedule) - {"enabled", "frequency", "timeOfDay"})
        if unknown:
            raise ValueError("unsupported backup.schedule fields: " + ", ".join(unknown))
        if "enabled" in schedule and type(schedule["enabled"]) is not bool:
            raise ValueError("backup.schedule.enabled must be a boolean")
        if "frequency" in schedule and schedule.get("frequency") not in {"daily", "weekly", "monthly"}:
            raise ValueError("backup.schedule.frequency must be one of: daily, weekly, monthly")
        if "timeOfDay" in schedule and not TIME_OF_DAY_RE.match(str(schedule.get("timeOfDay") or "")):
            raise ValueError("backup.schedule.timeOfDay must use HH:MM 24-hour time")
    return normalized


def _validate_complete_backup_settings(settings: dict[str, Any]) -> None:
    normalized = normalize_backup_settings_update(settings)
    include = normalized.get("include") if isinstance(normalized.get("include"), dict) else {}
    if not any(include.get(key) is True for key in BACKUP_INCLUDE_FIELDS):
        raise ValueError("backup.include must select at least one backup item")
    schedule = normalized.get("schedule") if isinstance(normalized.get("schedule"), dict) else {}
    if schedule.get("enabled") and not str(normalized.get("targetDirectory") or "").strip():
        raise ValueError("backup.targetDirectory is required when scheduled backups are enabled")


def _validate_external_tools_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("externalTools settings must be an object")
    for tool, values in update.items():
        if not isinstance(values, dict):
            raise ValueError(f"externalTools.{tool} must be an object")
        for key, value in values.items():
            field = f"externalTools.{tool}.{key}"
            if key in {"binaryCandidates", "ideStateDbCandidates", "workspaceStorageRoots"}:
                if not isinstance(value, list) or not value:
                    raise ValueError(f"{field} must be a non-empty list")
                for item in value:
                    _validate_path_string(field, item)
            elif key.lower().endswith(("root", "path", "home")) or key in {"stateDbPath", "projectsPath", "configPath", "credentialsPath"}:
                _validate_path_string(field, value)


def _validate_memory_search_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("memorySearch settings must be an object")
    unsupported = sorted(set(update) - {"enabled", "backendPolicy", "local", "nativeMemory"})
    if unsupported:
        raise ValueError("unsupported memorySearch settings: " + ", ".join(unsupported))
    if "enabled" in update and type(update["enabled"]) is not bool:
        raise ValueError("memorySearch.enabled must be a boolean")
    if "backendPolicy" in update and update.get("backendPolicy") not in {"auto", "rag", "local"}:
        raise ValueError("memorySearch.backendPolicy must be one of: auto, local, rag")
    local = update.get("local")
    if local is not None:
        if not isinstance(local, dict):
            raise ValueError("memorySearch.local must be an object")
        unknown = sorted(set(local) - {"enabled", "syncAfterPipeline", "maxScanFiles", "maxScanBytes"})
        if unknown:
            raise ValueError("unsupported memorySearch.local settings: " + ", ".join(unknown))
        for key in ("enabled", "syncAfterPipeline"):
            if key in local and type(local[key]) is not bool:
                raise ValueError(f"memorySearch.local.{key} must be a boolean")
        for key in ("maxScanFiles", "maxScanBytes"):
            if key in local:
                _validate_positive_int(f"memorySearch.local.{key}", local[key])
    native = update.get("nativeMemory")
    if native is None:
        return
    if not isinstance(native, dict):
        raise ValueError("memorySearch.nativeMemory must be an object")
    unknown = sorted(set(native) - {"enabled", "allowInRag", "includeInstructions", "tools"})
    if unknown:
        raise ValueError("unsupported memorySearch.nativeMemory settings: " + ", ".join(unknown))
    for key in ("enabled", "allowInRag", "includeInstructions"):
        if key in native and type(native[key]) is not bool:
            raise ValueError(f"memorySearch.nativeMemory.{key} must be a boolean")
    tools = native.get("tools")
    if tools is not None:
        if not isinstance(tools, dict):
            raise ValueError("memorySearch.nativeMemory.tools must be an object")
        unknown_tools = sorted(set(tools) - {"codex", "claudeCode"})
        if unknown_tools:
            raise ValueError(
                "unsupported memorySearch.nativeMemory tools: " + ", ".join(unknown_tools)
            )
        for tool, enabled in tools.items():
            if type(enabled) is not bool:
                raise ValueError(f"memorySearch.nativeMemory.tools.{tool} must be a boolean")


def _validate_paths_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("paths settings must be an object")
    for group, values in update.items():
        if not isinstance(values, dict):
            raise ValueError(f"paths.{group} must be an object")
        for key, value in values.items():
            if group == "runtime" and key == "actanaraHome":
                raise ValueError("paths.runtime.actanaraHome is managed by the dedicated runtime path API")
            _validate_path_string(f"paths.{group}.{key}", value)


def _validate_pipeline_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("pipeline settings must be an object")
    install_only = sorted(set(update) & INSTALL_ONLY_PIPELINE_FIELDS)
    if install_only:
        raise ValueError(
            "pipeline install-time language fields are immutable via operator settings API: "
            + ", ".join(f"pipeline.{key}" for key in install_only)
        )
    for key in (
        "stableCommand",
        "pythonExecutable",
        "workingDirectory",
        "dailyDateArgument",
        "skipFinalRagEnv",
        "thinkingMode",
    ):
        if key in update and not str(update.get(key) or "").strip():
            raise ValueError(f"pipeline.{key} must be a non-empty string")
    for key in ("pythonExecutable", "workingDirectory"):
        if key in update:
            _validate_path_string(f"pipeline.{key}", update.get(key))
    for key in ("stepTimeoutSeconds", "totalWatchdogSeconds"):
        if key in update:
            _validate_positive_int(f"pipeline.{key}", update.get(key))
    step_timeouts = update.get("stepTimeouts")
    if step_timeouts is not None:
        if not isinstance(step_timeouts, dict):
            raise ValueError("pipeline.stepTimeouts must be an object")
        for name, value in step_timeouts.items():
            if not str(name or "").strip():
                raise ValueError("pipeline.stepTimeouts keys must be non-empty strings")
            _validate_positive_int(f"pipeline.stepTimeouts.{name}", value)


def _validate_runtime_sources_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("runtimeSources settings must be an object")
    allowed_fields = set(RUNTIME_SOURCE_FIELDS.values())
    for key, value in update.items():
        if key not in allowed_fields:
            raise ValueError(f"runtimeSources.{key} is not supported")
        if value not in VALID_RUNTIME_SOURCES:
            raise ValueError(f"runtimeSources.{key} must be one of: {', '.join(sorted(VALID_RUNTIME_SOURCES))}")


def _validate_weather_update(update: Any) -> None:
    if update is None:
        return
    if not isinstance(update, dict):
        raise ValueError("weather settings must be an object")
    allowed = {"enabled", "locationMode", "latitude", "longitude", "label", "timezone", "cacheTtlHours"}
    unsupported = sorted(set(update) - allowed)
    if unsupported:
        raise ValueError("unsupported weather settings: " + ", ".join(unsupported))
    if "locationMode" in update and update.get("locationMode") not in {"auto-ip", "manual", "disabled"}:
        raise ValueError("weather.locationMode must be one of: auto-ip, disabled, manual")
    if "latitude" in update and update.get("latitude") not in (None, ""):
        latitude = _float_setting("weather.latitude", update.get("latitude"))
        if latitude < -90 or latitude > 90:
            raise ValueError("weather.latitude must be between -90 and 90")
    if "longitude" in update and update.get("longitude") not in (None, ""):
        longitude = _float_setting("weather.longitude", update.get("longitude"))
        if longitude < -180 or longitude > 180:
            raise ValueError("weather.longitude must be between -180 and 180")
    for key in ("label", "timezone"):
        if key in update and update.get(key) is not None and "\x00" in str(update.get(key)):
            raise ValueError(f"weather.{key} must not contain NUL bytes")
    if "cacheTtlHours" in update:
        _validate_positive_int("weather.cacheTtlHours", update.get("cacheTtlHours"))


def _float_setting(field: str, value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number") from None


def _validate_positive_int(field: str, value: Any) -> None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _validate_path_string(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path string")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL bytes")


def _store_secret_for_paths(ref: object, value: str, paths: RuntimePaths) -> dict[str, str]:
    payload = ref.as_dict() if hasattr(ref, "as_dict") else dict(ref)  # type: ignore[arg-type]
    if str(payload.get("backend") or "") == "runtime-file":
        return store_secret(ref, value, runtime_home=paths.home)  # type: ignore[arg-type]
    return store_secret(ref, value)  # type: ignore[arg-type]


def _read_secret_for_paths(ref: object, paths: RuntimePaths | None) -> str:
    payload = ref.as_dict() if hasattr(ref, "as_dict") else dict(ref)  # type: ignore[arg-type]
    if str(payload.get("backend") or "") == "runtime-file":
        runtime_paths = paths or load_paths()
        return read_secret(ref, runtime_home=runtime_paths.home)  # type: ignore[arg-type]
    return read_secret(ref)  # type: ignore[arg-type]


def read_llm_provider(paths: RuntimePaths | None = None, *, redact_secrets: bool = True, persist_defaults: bool = True) -> dict:
    paths = paths or load_paths()
    settings = read_settings(paths, redact_secrets=redact_secrets, persist_defaults=persist_defaults)
    provider = settings.get("llmProvider", {})
    if isinstance(provider, dict):
        provider["catalog"] = llm_provider_catalog()
        provider["savedProviderKeys"] = _llm_provider_saved_key_status(settings, paths=paths)
    return provider


def write_llm_provider(update: dict[str, Any], paths: RuntimePaths | None = None) -> dict:
    paths = paths or load_paths()
    settings = migrate_persisted_secret_refs(paths)
    current = settings.get("llmProvider", {})
    normalized = normalize_llm_provider_update(update if isinstance(update, dict) else {}, current if isinstance(current, dict) else {})
    _validate_llm_provider_complete(normalized)
    raw_api_key = str((update or {}).get("apiKey") or "")
    provider_id = _llm_provider_secret_provider_id(normalized)
    provider_secrets = _llm_provider_secret_refs(settings)
    if raw_api_key and raw_api_key != MASKED_SECRET:
        stored_ref = _store_secret_for_paths(_llm_provider_secret_ref(paths, provider_id), raw_api_key, paths)
        provider_secrets[provider_id] = stored_ref
        normalized["secretRef"] = stored_ref
        normalized["apiKey"] = ""
    else:
        provider_ref = provider_secrets.get(provider_id)
        current_provider_id = _llm_provider_secret_provider_id(current) if isinstance(current, dict) else ""
        if not provider_ref and provider_id == current_provider_id and isinstance(current, dict) and isinstance(current.get("secretRef"), dict):
            provider_ref = current["secretRef"]
            provider_secrets[provider_id] = provider_ref
        if provider_ref:
            normalized["secretRef"] = provider_ref
        else:
            normalized.pop("secretRef", None)
        normalized["apiKey"] = ""
    return write_settings({"llmProvider": normalized, "llmProviderSecrets": provider_secrets}, paths).get("llmProvider", {})


def write_llm_provider_chain(
    update: list[dict[str, Any]] | dict[str, Any],
    paths: RuntimePaths | None = None,
) -> list[dict[str, Any]]:
    """Atomically persist an ordered provider chain and its independent secrets."""
    saved = write_operator_settings_bundle(
        {"llmProviderChain": update},
        paths,
    )
    chain = saved.get("llmProviderChain")
    return chain if isinstance(chain, list) else []


def _validate_llm_provider_complete(provider: dict[str, Any]) -> None:
    if str(provider.get("provider") or "") == CUSTOM_PROVIDER_ID:
        if not str(provider.get("endpoint") or "").strip():
            raise ValueError("llmProvider.endpoint is required for custom provider")
        if not str(provider.get("model") or "").strip():
            raise ValueError("llmProvider.model is required for custom provider")


def write_llm_api_key_secret(value: str, paths: RuntimePaths | None = None) -> dict:
    paths = paths or load_paths()
    settings = migrate_persisted_secret_refs(paths)
    current = settings.get("llmProvider", {})
    provider_id = _llm_provider_secret_provider_id(current)
    provider_secrets = _llm_provider_secret_refs(settings)
    stored_ref = _store_secret_for_paths(_llm_provider_secret_ref(paths, provider_id), value, paths)
    provider_secrets[provider_id] = stored_ref
    provider_update = {**current, "apiKey": "", "secretRef": stored_ref}
    return write_settings({"llmProvider": provider_update, "llmProviderSecrets": provider_secrets}, paths).get("llmProvider", {})


def _sanitize_persisted_secrets(
    paths: RuntimePaths,
    settings: dict[str, Any],
    *,
    migrate_persisted_refs: bool,
) -> None:
    migration_ledger = (
        copy.deepcopy(settings.get("secretMigration"))
        if isinstance(settings.get("secretMigration"), dict)
        else {"schemaVersion": 1, "targetBackend": "runtime-file", "attempts": {}}
    )
    attempts_before = copy.deepcopy(migration_ledger.get("attempts"))
    provider = settings.get("llmProvider") if isinstance(settings.get("llmProvider"), dict) else None
    if provider is not None:
        provider_id = _llm_provider_secret_provider_id(provider)
        provider_secrets = _llm_provider_secret_refs(settings)
        migrated_provider_secrets = {
            saved_provider_id: (
                _migrate_provider_secret_ref(
                    paths,
                    saved_provider_id,
                    saved_ref,
                    migration_ledger=migration_ledger,
                )
                if migrate_persisted_refs
                else copy.deepcopy(saved_ref)
            )
            for saved_provider_id, saved_ref in provider_secrets.items()
        }
        provider_secrets = migrated_provider_secrets
        api_key = str(provider.get("apiKey") or "")
        secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else None
        if secret_ref and migrate_persisted_refs:
            secret_ref = _migrate_provider_secret_ref(
                paths,
                provider_id,
                secret_ref,
                migration_ledger=migration_ledger,
            )
            provider["secretRef"] = secret_ref
            provider_secrets[provider_id] = secret_ref
            provider["apiKey"] = ""
        elif secret_ref:
            provider["secretRef"] = copy.deepcopy(secret_ref)
            provider_secrets[provider_id] = copy.deepcopy(secret_ref)
            provider["apiKey"] = ""
        elif api_key and api_key != MASKED_SECRET:
            try:
                provider["secretRef"] = _store_secret_for_paths(
                    _llm_provider_secret_ref(paths, provider_id),
                    api_key,
                    paths,
                )
                provider_secrets[provider_id] = provider["secretRef"]
            except Exception:
                provider.pop("secretRef", None)
            finally:
                provider["apiKey"] = ""
        elif api_key == MASKED_SECRET:
            provider["apiKey"] = ""
        settings["llmProviderSecrets"] = provider_secrets

    chain = _configured_llm_provider_chain(settings)
    if chain:
        chain = normalize_llm_provider_chain_update(chain, chain)
        sanitized_chain: list[dict[str, Any]] = []
        for index, entry in enumerate(chain):
            provider_id = _llm_provider_secret_provider_id(entry)
            raw_api_key = str(entry.get("apiKey") or "")
            secret_ref = entry.get("secretRef") if isinstance(entry.get("secretRef"), dict) else None
            if (
                secret_ref is None
                and index == 0
                and isinstance(provider, dict)
                and provider_id == _llm_provider_secret_provider_id(provider)
                and isinstance(provider.get("secretRef"), dict)
            ):
                secret_ref = copy.deepcopy(provider["secretRef"])
            if secret_ref and migrate_persisted_refs:
                entry["secretRef"] = _migrate_provider_secret_ref(
                    paths,
                    _llm_provider_chain_secret_slot(entry),
                    secret_ref,
                    migration_ledger=migration_ledger,
                )
            elif secret_ref:
                entry["secretRef"] = copy.deepcopy(secret_ref)
            elif raw_api_key and raw_api_key != MASKED_SECRET:
                try:
                    entry["secretRef"] = _store_secret_for_paths(
                        _llm_provider_secret_ref(
                            paths,
                            _llm_provider_chain_secret_slot(entry),
                        ),
                        raw_api_key,
                        paths,
                    )
                except Exception:
                    entry.pop("secretRef", None)
            entry["apiKey"] = ""
            entry["apiKeyEnv"] = _normalized_persisted_api_key_env(
                str(entry.get("apiKeyEnv") or "LLM_API_KEY")
            )
            sanitized_chain.append(entry)
        settings["llmProviderChain"] = sanitized_chain
        settings["llmProvider"] = _legacy_provider_from_chain_entry(sanitized_chain[0])
        primary_ref = sanitized_chain[0].get("secretRef")
        if isinstance(primary_ref, dict):
            provider_secrets = _llm_provider_secret_refs(settings)
            provider_secrets[_llm_provider_secret_provider_id(sanitized_chain[0])] = copy.deepcopy(primary_ref)
            settings["llmProviderSecrets"] = provider_secrets
    _sanitize_rag_embedding_secret(
        paths,
        settings,
        migration_ledger=migration_ledger,
        migrate_persisted_refs=migrate_persisted_refs,
    )
    if migration_ledger.get("attempts") or attempts_before:
        settings["secretMigration"] = migration_ledger


def _llm_provider_secret_provider_id(provider: dict[str, Any] | None) -> str:
    if not isinstance(provider, dict):
        return CUSTOM_PROVIDER_ID
    provider_id = str(provider.get("provider") or provider.get("presetProvider") or "").strip()
    return provider_id or CUSTOM_PROVIDER_ID


def _llm_provider_secret_name(provider_id: str) -> str:
    raw_provider = str(provider_id or CUSTOM_PROVIDER_ID)
    safe_provider = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_provider).strip("-") or CUSTOM_PROVIDER_ID
    if safe_provider != raw_provider or len(safe_provider) > 120:
        safe_provider = f"{safe_provider[:100]}-{hashlib.sha256(raw_provider.encode('utf-8')).hexdigest()[:16]}"
    return f"llm-provider-api-key-{safe_provider}"


def _llm_provider_secret_ref(paths: RuntimePaths, provider_id: str):
    return llm_api_key_ref(str(paths.home), name=_llm_provider_secret_name(provider_id))


def _rag_embedding_secret_provider_id(embedding: dict[str, Any] | None) -> str:
    if not isinstance(embedding, dict):
        return "cloud"
    provider_id = str(embedding.get("providerId") or embedding.get("provider") or embedding.get("mode") or "cloud")
    return provider_id.strip() or "cloud"


def _rag_embedding_secret_ref(paths: RuntimePaths, provider_id: str):
    return rag_embedding_api_key_ref(str(paths.home), provider_id=provider_id)


def _migrate_provider_secret_ref(
    paths: RuntimePaths,
    provider_id: str,
    secret_ref: dict[str, Any],
    *,
    migration_ledger: dict[str, Any],
) -> dict[str, Any]:
    account = str(secret_ref.get("account") or "")
    default_target_ref = _llm_provider_secret_ref(paths, provider_id).as_dict()
    target_ref = (
        {
            "backend": default_target_ref["backend"],
            "service": str(secret_ref.get("service") or "actanara"),
            "account": account,
        }
        if account.startswith("settings-tx-")
        else default_target_ref
    )
    return _migrate_secret_ref_once(
        paths,
        secret_ref,
        target_ref,
        migration_ledger=migration_ledger,
    )


def _sanitize_rag_embedding_secret(
    paths: RuntimePaths,
    settings: dict[str, Any],
    *,
    migration_ledger: dict[str, Any],
    migrate_persisted_refs: bool,
) -> None:
    rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else None
    embedding = rag.get("embedding") if isinstance(rag, dict) and isinstance(rag.get("embedding"), dict) else None
    if embedding is None:
        return
    provider_id = _rag_embedding_secret_provider_id(embedding)
    raw_api_key = str(embedding.get("apiKey") or "")
    secret_ref = embedding.get("secretRef") if isinstance(embedding.get("secretRef"), dict) else None
    if secret_ref:
        default_target_ref = _rag_embedding_secret_ref(paths, provider_id).as_dict()
        target_ref = (
            {
                "backend": default_target_ref["backend"],
                "service": str(secret_ref.get("service") or "actanara"),
                "account": str(secret_ref.get("account") or ""),
            }
            if str(secret_ref.get("account") or "").startswith("settings-tx-")
            else default_target_ref
        )
        if migrate_persisted_refs and secret_ref != target_ref:
            secret_ref = _migrate_secret_ref_once(
                paths,
                secret_ref,
                target_ref,
                migration_ledger=migration_ledger,
            )
        embedding["secretRef"] = secret_ref
    elif raw_api_key and raw_api_key != MASKED_SECRET:
        try:
            embedding["secretRef"] = _store_secret_for_paths(
                _rag_embedding_secret_ref(paths, provider_id),
                raw_api_key,
                paths,
            )
        except Exception:
            embedding.pop("secretRef", None)
    embedding.pop("apiKey", None)


def _migrate_secret_ref_once(
    paths: RuntimePaths,
    source_ref: dict[str, Any],
    target_ref: dict[str, Any],
    *,
    migration_ledger: dict[str, Any],
) -> dict[str, Any]:
    if all(
        str(source_ref.get(key) or "") == str(target_ref.get(key) or "")
        for key in ("backend", "service", "account")
    ):
        return source_ref

    source_backend = str(source_ref.get("backend") or "")
    if source_backend != "macos-keychain":
        try:
            value = _read_secret_for_paths(source_ref, paths)
            if value:
                return _store_secret_for_paths(target_ref, value, paths)
        except Exception:
            pass
        return source_ref

    attempts = migration_ledger.setdefault("attempts", {})
    if not isinstance(attempts, dict):
        migration_ledger["attempts"] = attempts = {}
    source_id = _secret_ref_migration_id(source_ref)
    previous = attempts.get(source_id) if isinstance(attempts.get(source_id), dict) else {}
    if previous.get("status") == "migrated" and isinstance(previous.get("targetRef"), dict):
        return copy.deepcopy(previous["targetRef"])
    if previous.get("status") == "reentry-required":
        return source_ref

    try:
        value = _read_secret_for_paths(source_ref, paths)
        if value:
            migrated = _store_secret_for_paths(target_ref, value, paths)
            attempts[source_id] = {
                "status": "migrated",
                "sourceBackend": "macos-keychain",
                "targetRef": migrated,
                "attemptedAt": _now_iso(),
            }
            return migrated
    except Exception:
        pass
    attempts[source_id] = {
        "status": "reentry-required",
        "sourceBackend": "macos-keychain",
        "attemptedAt": _now_iso(),
    }
    return source_ref


def _llm_provider_secret_refs(settings: dict[str, Any]) -> dict[str, dict]:
    refs = settings.get("llmProviderSecrets")
    if not isinstance(refs, dict):
        return {}
    return {str(provider_id): copy.deepcopy(ref) for provider_id, ref in refs.items() if isinstance(ref, dict)}


def _llm_provider_saved_key_status(
    settings: dict[str, Any],
    *,
    paths: RuntimePaths | None = None,
) -> dict[str, bool]:
    saved = {
        provider_id: True
        for provider_id, ref in _llm_provider_secret_refs(settings).items()
        if _secret_ref_has_pipeline_readable_key(ref, paths=paths, settings=settings)
    }
    provider = settings.get("llmProvider") if isinstance(settings.get("llmProvider"), dict) else {}
    if (
        isinstance(provider, dict)
        and isinstance(provider.get("secretRef"), dict)
        and _secret_ref_has_pipeline_readable_key(provider["secretRef"], paths=paths, settings=settings)
    ):
        saved[_llm_provider_secret_provider_id(provider)] = True
    return saved


def _secret_ref_has_pipeline_readable_key(
    secret_ref: dict[str, Any] | None,
    *,
    paths: RuntimePaths | None = None,
    settings: dict[str, Any] | None = None,
) -> bool:
    if not isinstance(secret_ref, dict):
        return False
    if str(secret_ref.get("backend") or "").strip() in {"", "memory", "process-env"}:
        return False
    if str(secret_ref.get("backend") or "").strip() == "macos-keychain":
        # A legacy Keychain ref is not a launchd-safe runtime credential.  Do
        # not query it from read-only settings/status paths; the explicit
        # migration boundary will attempt it once when the key is really used.
        return False
    if settings is not None and _secret_ref_requires_reentry(settings, secret_ref):
        return False
    try:
        return bool(_read_secret_for_paths(secret_ref, paths))
    except Exception:
        return False


def _secret_ref_requires_reentry(
    settings: dict[str, Any],
    secret_ref: dict[str, Any] | None,
) -> bool:
    if not isinstance(secret_ref, dict) or secret_ref.get("backend") != "macos-keychain":
        return False
    # All legacy Keychain refs require migration before they are safe for a
    # background pipeline.  The migration ledger still distinguishes a pending
    # first attempt from a terminal re-entry-required result internally.
    return True


def _secret_ref_migration_id(secret_ref: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "backend": str(secret_ref.get("backend") or ""),
                "service": str(secret_ref.get("service") or "actanara"),
                "account": str(secret_ref.get("account") or ""),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def resolve_general_settings(paths: RuntimePaths | None = None) -> dict[str, Any]:
    settings = _read_settings_for_resolution(paths)
    general = settings.get("general") if isinstance(settings.get("general"), dict) else {}
    return {
        "appName": str(general.get("appName") or "Actanara"),
        "environment": str(general.get("environment") or "local"),
        "timezone": str(general.get("timezone") or config.TARGET_TIMEZONE),
        "locale": str(general.get("locale") or "zh-CN"),
        "workspaceRoot": str(Path(general.get("workspaceRoot") or config.WORKSPACE_DIR).expanduser().absolute()),
        "tmpWorkspace": str(Path(general.get("tmpWorkspace") or config.TMP_WORKSPACE).expanduser().absolute()),
    }


def resolve_pipeline_settings(paths: RuntimePaths | None = None) -> dict[str, Any]:
    settings = _read_settings_for_resolution(paths)
    pipeline = settings.get("pipeline") if isinstance(settings.get("pipeline"), dict) else {}
    language_profile = resolve_pipeline_language_profile(pipeline.get("languageProfile"))
    default_step_timeout = _positive_int(pipeline.get("stepTimeoutSeconds"), 1800)
    step_timeouts = _resolve_pipeline_step_timeouts(pipeline.get("stepTimeouts"), default_step_timeout)
    return {
        "stableCommand": str(pipeline.get("stableCommand") or "python advanced/pipeline/run_daily_pipeline.py [YYYY-MM-DD]"),
        "languageProfile": language_profile.profile_id,
        "languageStatus": language_profile.status,
        "englishEnabled": _bool_setting(pipeline.get("englishEnabled"), False),
        "displayLocale": language_profile.locale,
        "diarySchemaVersion": str(pipeline.get("diarySchemaVersion") or language_profile.diary_schema_version),
        "promptPayloadProfile": str(pipeline.get("promptPayloadProfile") or language_profile.prompt_payload_profile),
        "ragLanguageProfile": language_profile.rag_language_profile,
        "pythonExecutable": str(pipeline.get("pythonExecutable") or "python3"),
        "workingDirectory": str(Path(pipeline.get("workingDirectory") or config.WORKSPACE_DIR).expanduser().absolute()),
        "dailyDateArgument": str(pipeline.get("dailyDateArgument") or "YYYY-MM-DD"),
        "skipFinalRagEnv": str(pipeline.get("skipFinalRagEnv") or "ACTANARA_PIPELINE_SKIP_FINAL_RAG"),
        "skipFinalRag": os.getenv(str(pipeline.get("skipFinalRagEnv") or "ACTANARA_PIPELINE_SKIP_FINAL_RAG")),
        "thinkingMode": str(pipeline.get("thinkingMode") or "off"),
        "stepTimeoutSeconds": default_step_timeout,
        "stepTimeouts": step_timeouts,
        "dailyTimeoutSeconds": _positive_int(
            pipeline.get("dailyTimeoutSeconds"),
            900,
        ),
        "totalWatchdogSeconds": _positive_int(
            pipeline.get("totalWatchdogSeconds"),
            7200,
        ),
    }


def _resolve_pipeline_step_timeouts(raw: Any, default_step_timeout: int) -> dict[str, int]:
    defaults = {
        "narrative_pass.py": 1800,
        "technical_pass.py": 1800,
        "learning_pass.py": 900,
        "rag_v2_sync.py": 1800,
    }
    result = {key: _positive_int(value, default_step_timeout) for key, value in defaults.items()}
    if isinstance(raw, dict):
        for key, value in raw.items():
            normalized = str(key or "").strip()
            if normalized:
                result[normalized] = _positive_int(value, default_step_timeout)
    return result


def resolve_dashboard_settings(paths: RuntimePaths | None = None) -> dict[str, Any]:
    settings = _read_settings_for_resolution(paths)
    dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
    project_root = Path(dashboard.get("projectRoot") or config.WORKSPACE_DIR).expanduser().absolute()
    host = str(dashboard.get("host") or DEFAULT_DASHBOARD_HOST)
    port = _positive_int(dashboard.get("port"), DEFAULT_DASHBOARD_PORT)
    health_path = str(dashboard.get("healthPath") or DEFAULT_DASHBOARD_HEALTH_PATH)
    public_base_url = str(dashboard.get("publicBaseUrl") or f"http://{DEFAULT_DASHBOARD_HOST}:{port}").rstrip("/")
    allowed_origins = [
        str(origin).strip().rstrip("/")
        for origin in (dashboard.get("allowedOrigins") or [])
        if str(origin or "").strip()
    ]
    return {
        "projectRoot": str(project_root),
        "pythonExecutable": str(
            Path(dashboard.get("pythonExecutable") or "python3").expanduser()
        ),
        "appDir": str(Path(dashboard.get("appDir") or project_root / "src" / "dashboard").expanduser().absolute()),
        "host": host,
        "port": port,
        "publicBaseUrl": public_base_url,
        "allowedOrigins": allowed_origins,
        "healthPath": health_path,
        "url": f"http://{host}:{port}{health_path}",
        "logsDir": str(Path(dashboard.get("logsDir") or Path.home() / "Library" / "Logs" / "Actanara").expanduser().absolute()),
        "serviceLabel": str(dashboard.get("serviceLabel") or "com.actanara.dashboard"),
        "watchdogLabel": str(dashboard.get("watchdogLabel") or "com.actanara.dashboard.watchdog"),
    }


def resolve_external_tool_paths(paths: RuntimePaths | None = None) -> dict[str, dict[str, Any]]:
    settings = _read_settings_for_resolution(paths)
    configured = settings.get("externalTools") if isinstance(settings.get("externalTools"), dict) else {}
    defaults = default_external_tool_settings()
    merged = _deep_merge(defaults, configured)
    return {
        tool: {key: _external_tool_value(value) for key, value in values.items()}
        for tool, values in merged.items()
        if isinstance(values, dict)
    }


def external_tool_path(tool: str, key: str, paths: RuntimePaths | None = None) -> Path:
    """Return a configured external tool path, falling back to settings defaults."""
    resolved = resolve_external_tool_paths(paths)
    value = resolved.get(tool, {}).get(key)
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().absolute()
    fallback = default_external_tool_settings().get(tool, {}).get(key)
    if isinstance(fallback, list):
        raise ValueError(f"externalTools.{tool}.{key} is not a single path")
    if fallback is None:
        raise ValueError(f"unknown external tool path: externalTools.{tool}.{key}")
    return Path(str(fallback)).expanduser().absolute()


def default_external_tool_path(tool: str, key: str, home: Path | None = None) -> Path:
    """Return the schema default for a single external tool path."""
    fallback = default_external_tool_settings(home).get(tool, {}).get(key)
    if isinstance(fallback, list):
        raise ValueError(f"externalTools.{tool}.{key} is not a single path")
    if fallback is None:
        raise ValueError(f"unknown external tool path: externalTools.{tool}.{key}")
    return Path(str(fallback)).expanduser().absolute()


def external_tool_path_list(tool: str, key: str, paths: RuntimePaths | None = None) -> list[Path]:
    """Return a configured external tool path list, falling back to settings defaults."""
    resolved = resolve_external_tool_paths(paths)
    value = resolved.get(tool, {}).get(key)
    if value is None:
        value = default_external_tool_settings().get(tool, {}).get(key, [])
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, Path) else Path(str(item)).expanduser().absolute() for item in value]


def resolve_feature_flags(paths: RuntimePaths | None = None) -> dict[str, bool]:
    settings = _read_settings_for_resolution(paths)
    configured = settings.get("features") if isinstance(settings.get("features"), dict) else {}
    merged = _deep_merge(FEATURE_DEFAULTS, configured)
    return {key: _bool_setting(value, bool(FEATURE_DEFAULTS.get(key, False))) for key, value in merged.items()}


def resolve_memory_search_settings(paths: RuntimePaths | None = None) -> dict[str, Any]:
    """Resolve the local/search-router policy without creating index state."""
    selected_paths = paths or load_paths()
    defaults = default_settings(selected_paths)["memorySearch"]
    settings = _read_settings_for_resolution(selected_paths)
    configured = settings.get("memorySearch") if isinstance(settings.get("memorySearch"), dict) else {}
    merged = _deep_merge(defaults, configured)
    local = merged.get("local") if isinstance(merged.get("local"), dict) else {}
    native = merged.get("nativeMemory") if isinstance(merged.get("nativeMemory"), dict) else {}
    tools = native.get("tools") if isinstance(native.get("tools"), dict) else {}
    return {
        "enabled": _bool_setting(merged.get("enabled"), True),
        "backendPolicy": (
            str(merged.get("backendPolicy") or "auto")
            if str(merged.get("backendPolicy") or "auto") in {"auto", "rag", "local"}
            else "auto"
        ),
        "local": {
            "enabled": _bool_setting(local.get("enabled"), True),
            "syncAfterPipeline": _bool_setting(local.get("syncAfterPipeline"), True),
            "maxScanFiles": _positive_int(local.get("maxScanFiles"), 2000),
            "maxScanBytes": _positive_int(local.get("maxScanBytes"), 64 * 1024 * 1024),
            "indexPath": str(selected_paths.state_dir / "cache" / "memory-search.sqlite3"),
        },
        "nativeMemory": {
            "enabled": _bool_setting(native.get("enabled"), True),
            "allowInRag": _bool_setting(native.get("allowInRag"), True),
            "includeInstructions": _bool_setting(native.get("includeInstructions"), True),
            "tools": {
                "codex": _bool_setting(tools.get("codex"), True),
                "claudeCode": _bool_setting(tools.get("claudeCode"), True),
            },
        },
    }


def native_memory_policy_profile(paths: RuntimePaths | None = None) -> dict[str, Any]:
    """Return the complete policy that authorizes native Agent memory reads.

    The profile is safe to persist in rebuildable index metadata: it contains
    only booleans and local path identities, never memory contents. Resolved
    identities make retargeting a configured symlink a policy change even when
    its lexical settings path is unchanged.
    """
    selected_paths = paths or load_paths()
    native = resolve_memory_search_settings(selected_paths)["nativeMemory"]
    codex_home = external_tool_path("codex", "home", selected_paths)
    claude_projects_root = external_tool_path(
        "claudeCode",
        "projectsRoot",
        selected_paths,
    )
    return {
        "schemaVersion": 2,
        "enabled": native["enabled"],
        "allowInRag": native["allowInRag"],
        "includeInstructions": native["includeInstructions"],
        "tools": {
            "codex": native["tools"]["codex"],
            "claudeCode": native["tools"]["claudeCode"],
        },
        "paths": {
            "codexHome": str(codex_home),
            "claudeProjectsRoot": str(claude_projects_root),
        },
        "pathIdentities": {
            "codexHome": _native_memory_path_identity(
                codex_home,
                authorized=(
                    native["enabled"] is True
                    and native["tools"]["codex"] is True
                ),
            ),
            "claudeProjectsRoot": _native_memory_path_identity(
                claude_projects_root,
                authorized=(
                    native["enabled"] is True
                    and native["tools"]["claudeCode"] is True
                ),
            ),
        },
    }


def _native_memory_path_identity(
    path: Path,
    *,
    authorized: bool,
) -> dict[str, Any]:
    configured = path.expanduser().absolute()
    identity: dict[str, Any] = {
        "configuredPath": str(configured),
        "resolvedPath": None,
        "device": None,
        "inode": None,
        "status": "not-authorized",
    }
    if not authorized:
        return identity
    identity["status"] = "missing"
    try:
        resolved = configured.resolve(strict=True)
        path_stat = resolved.stat()
    except (FileNotFoundError, NotADirectoryError):
        return identity
    except (OSError, RuntimeError):
        return {**identity, "status": "unavailable"}
    return {
        **identity,
        "resolvedPath": str(resolved),
        "device": int(path_stat.st_dev),
        "inode": int(path_stat.st_ino),
        "status": "ready",
    }


def native_memory_policy_digest(
    paths: RuntimePaths | None = None,
    *,
    profile: dict[str, Any] | None = None,
) -> str:
    selected_profile = (
        profile
        if isinstance(profile, dict)
        else native_memory_policy_profile(paths)
    )
    payload = json.dumps(
        selected_profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_nova_task_enabled(paths: RuntimePaths | None = None) -> bool:
    return resolve_feature_flags(paths).get("novaTask", True)


def external_tool_access_summary(paths: RuntimePaths | None = None) -> dict:
    resolved = resolve_external_tool_paths(paths)
    checks = {
        "openclaw.agentsRoot": _path_check(_get_nested(resolved, "openclaw.agentsRoot"), patterns=["*/sessions/*.jsonl*"]),
        "openclaw.cronRunsRoot": _path_check(_get_nested(resolved, "openclaw.cronRunsRoot"), patterns=["*.jsonl", "*.jsonl.migrated"]),
        "claudeCode.projectsRoot": _path_check(_get_nested(resolved, "claudeCode.projectsRoot"), recursive_patterns=["*.jsonl"]),
        "codex.sessionsRoot": _path_check(_get_nested(resolved, "codex.sessionsRoot"), recursive_patterns=["rollout-*.jsonl"]),
        "geminiCli.chatsRoot": _path_check(_get_nested(resolved, "geminiCli.chatsRoot"), patterns=["session-*"]),
        "geminiCli.projectsPath": _path_check(_get_nested(resolved, "geminiCli.projectsPath")),
        "hermes.stateDbPath": _path_check(_get_nested(resolved, "hermes.stateDbPath")),
        "opencode.databasePath": _path_check(_get_nested(resolved, "opencode.databasePath")),
        "opencode.storageRoot": _path_check(
            _get_nested(resolved, "opencode.storageRoot"),
            recursive_patterns=["session/*.json", "message/*.json", "part/*.json"],
        ),
        "antigravity.cliConversationsRoot": _path_check(
            _get_nested(resolved, "antigravity.cliConversationsRoot"),
            recursive_patterns=["*.pb", "*.db"],
        ),
        "antigravity.ideConversationsRoot": _path_check(
            _get_nested(resolved, "antigravity.ideConversationsRoot"),
            recursive_patterns=["*.pb", "*.db"],
        ),
        "antigravity.appConversationsRoot": _path_check(
            _get_nested(resolved, "antigravity.appConversationsRoot"),
            recursive_patterns=["*.pb", "*.db"],
        ),
        "cursor.chatsRoot": _path_check(
            _get_nested(resolved, "cursor.chatsRoot"),
            recursive_patterns=["store.db"],
        ),
        "cursor.projectsRoot": _path_check(
            _get_nested(resolved, "cursor.projectsRoot"),
            recursive_patterns=["*.jsonl", "*.txt"],
        ),
        "cursor.ideStateDbCandidates": _path_list_check(
            _get_nested(resolved, "cursor.ideStateDbCandidates"),
        ),
    }
    return {"tools": resolved, "checks": checks}


def resolve_runtime_source(env_name: str, paths: RuntimePaths | None = None) -> str:
    """Resolve one production source switch from settings.json."""
    if env_name not in RUNTIME_SOURCE_FIELDS:
        raise ValueError(f"unknown runtime source: {env_name}")
    settings = _read_settings_for_resolution(paths)
    value = (settings.get("runtimeSources") or {}).get(RUNTIME_SOURCE_FIELDS[env_name])
    if value is None:
        value = getattr(config, env_name)
    return _normalize_runtime_source(value, env_name)


def resolve_runtime_sources(paths: RuntimePaths | None = None) -> dict:
    return {env_name: resolve_runtime_source(env_name, paths) for env_name in RUNTIME_SOURCE_FIELDS}


def write_runtime_sources(update: dict[str, Any], paths: RuntimePaths | None = None) -> dict:
    if not isinstance(update, dict):
        raise ValueError("runtime source update must be an object")
    allowed_fields = set(RUNTIME_SOURCE_FIELDS.values())
    unknown = sorted(set(update) - allowed_fields)
    if unknown:
        raise ValueError("unknown runtime source fields: " + ", ".join(unknown))
    normalized = {
        field: _normalize_runtime_source(value, _runtime_source_env_name(field))
        for field, value in update.items()
    }
    return write_settings({"runtimeSources": normalized}, paths).get("runtimeSources", {})


def resolve_llm_provider(
    paths: RuntimePaths | None = None,
    *,
    redact_secrets: bool = False,
    _migrate_persisted_secrets: bool = True,
) -> dict:
    """Resolve provider settings for runtime use without exposing secrets by default."""
    paths = paths or load_paths()
    settings = (
        _read_settings_for_resolution(paths)
        if redact_secrets or not _migrate_persisted_secrets
        else migrate_persisted_secret_refs(paths)
    )
    provider = settings.get("llmProvider", {}) if isinstance(settings.get("llmProvider"), dict) else {}
    has_provider_block = isinstance(settings.get("llmProvider"), dict)
    provider_has_settings = bool(provider)
    env_provider = _explicit_env_override("LLM_PROVIDER", provider_has_settings)
    env_endpoint = _explicit_env_override("LLM_HOST", provider_has_settings)
    resolved_provider = str(provider.get("provider") or env_provider or CUSTOM_PROVIDER_ID)
    default_endpoint = ""
    default_model = ""
    resolved_endpoint = str(provider.get("endpoint") or env_endpoint or default_endpoint)
    resolved_api = str(provider.get("api") or _explicit_env_override("LLM_API", provider_has_settings) or "")
    if resolved_api is None:
        resolved_api = ""
    if not resolved_api:
        if env_provider is not None or env_endpoint is not None:
            marker = f"{resolved_provider} {resolved_endpoint}".lower()
            resolved_api = "anthropic-messages" if ("minimax" in marker or "anthropic" in marker) else "openai-compatible"
        else:
            resolved_api = "openai-compatible"
    auto_gate = auto_pipeline_gate_tokens(provider.get("contextWindow"), DEFAULT_PIPELINE_GATE_TOKENS)
    settings_gate_mode = _pipeline_gate_mode(provider)
    env_gate = _explicit_env_override("LLM_PIPELINE_GATE_TOKENS", provider_has_settings)
    resolved_gate = _positive_int(
        None if provider.get("pipelineGateTokens") else env_gate,
        provider.get("pipelineGateTokens") if settings_gate_mode == PIPELINE_GATE_MODE_MANUAL else None,
        auto_gate,
    )
    resolved_gate_mode = settings_gate_mode
    api_key_env_name = str(provider.get("apiKeyEnv") or "LLM_API_KEY")
    env_api_key = os.getenv(api_key_env_name)
    settings_api_key = str(provider.get("apiKey") or "")
    secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else None
    stored_api_key = (
        _read_secret_for_paths(secret_ref, paths)
        if secret_ref and not _secret_ref_requires_reentry(settings, secret_ref)
        else ""
    )
    resolved_api_key = stored_api_key or settings_api_key or env_api_key or config.LLM_API_KEY
    resolved = {
        "provider": resolved_provider,
        "endpoint": resolved_endpoint,
        "model": str(provider.get("model") or _explicit_env_override("LLM_MODEL_NAME", provider_has_settings) or default_model),
        "api": resolved_api,
        "contextWindow": provider.get("contextWindow"),
        "maxTokens": provider.get("maxTokens"),
        "pipelineConcurrency": _positive_int(
            None if provider.get("pipelineConcurrency") else _explicit_env_override("LLM_PIPELINE_CONCURRENCY", provider_has_settings),
            provider.get("pipelineConcurrency"),
            DEFAULT_PIPELINE_CONCURRENCY,
        ),
        "pipelineGateMode": resolved_gate_mode,
        "pipelineGateTokens": resolved_gate,
        "autoPipelineGateTokens": auto_gate,
        "pipelineGateDrift": resolved_gate != auto_gate,
        "timeoutSeconds": _positive_int(provider.get("timeoutSeconds"), DEFAULT_LLM_TIMEOUT_SECONDS),
        "apiKey": resolved_api_key,
        "apiKeyEnv": api_key_env_name,
        "secretRef": secret_ref or {},
    }
    resolved["hasApiKey"] = bool(resolved["apiKey"])
    resolved["source"] = {
        "provider": "settings" if provider.get("provider") else ("env" if env_provider is not None else "default"),
        "endpoint": "settings" if provider.get("endpoint") else ("env" if env_endpoint is not None else ("unset" if has_provider_block else "default")),
        "model": "settings" if provider.get("model") else ("env" if _explicit_env_override("LLM_MODEL_NAME", provider_has_settings) is not None else ("unset" if has_provider_block else "default")),
        "apiKey": "secret-store" if stored_api_key else ("settings" if settings_api_key else ("env" if env_api_key else "default")),
    }
    if redact_secrets:
        resolved["apiKey"] = MASKED_SECRET if resolved["hasApiKey"] else ""
    return resolved


def resolve_llm_provider_chain(
    paths: RuntimePaths | None = None,
    redact_secrets: bool = False,
    require_cross_process_secret: bool = False,
) -> list[dict[str, Any]]:
    """Resolve the ordered LLM provider chain using the settings authority.

    Homes written before the additive chain field existed are projected as a
    one-entry chain, so callers do not need a separate compatibility branch.
    """
    paths = paths or load_paths()
    settings = (
        _read_settings_for_resolution(paths)
        if redact_secrets or require_cross_process_secret
        else migrate_persisted_secret_refs(paths)
    )
    configured = _configured_llm_provider_chain(settings)
    if not configured:
        primary = resolve_llm_provider(
            paths,
            redact_secrets=redact_secrets,
            _migrate_persisted_secrets=not require_cross_process_secret,
        )
        primary.update({"entryId": "legacy-primary", "order": 0, "role": "primary"})
        primary["readiness"] = _llm_provider_entry_readiness(
            primary,
            require_cross_process_secret,
        )
        if redact_secrets:
            primary["apiKey"] = MASKED_SECRET if primary.get("hasApiKey") else ""
        return [primary]

    resolved_chain: list[dict[str, Any]] = []
    for index, entry in enumerate(configured):
        resolved = _resolve_configured_llm_provider_entry(
            settings,
            entry,
            paths=paths,
        )
        resolved.update(
            {
                "entryId": str(entry.get("entryId") or f"provider-{index + 1}"),
                "order": index,
                "role": "primary" if index == 0 else "fallback",
            }
        )
        resolved["readiness"] = _llm_provider_entry_readiness(
            resolved,
            require_cross_process_secret,
        )
        if redact_secrets:
            resolved["apiKey"] = MASKED_SECRET if resolved.get("hasApiKey") else ""
        resolved_chain.append(resolved)
    return resolved_chain


def _resolve_configured_llm_provider_entry(
    settings: dict[str, Any],
    provider: dict[str, Any],
    *,
    paths: RuntimePaths,
) -> dict[str, Any]:
    resolved_provider = str(provider.get("provider") or CUSTOM_PROVIDER_ID)
    resolved_endpoint = str(provider.get("endpoint") or "")
    resolved_api = str(provider.get("api") or "openai-compatible")
    auto_gate = auto_pipeline_gate_tokens(
        provider.get("contextWindow"),
        DEFAULT_PIPELINE_GATE_TOKENS,
    )
    settings_gate_mode = _pipeline_gate_mode(provider)
    resolved_gate = _positive_int(
        provider.get("pipelineGateTokens")
        if settings_gate_mode == PIPELINE_GATE_MODE_MANUAL
        else None,
        auto_gate,
    )
    api_key_env_name = str(provider.get("apiKeyEnv") or "LLM_API_KEY")
    env_api_key = os.getenv(api_key_env_name)
    settings_api_key = str(provider.get("apiKey") or "")
    secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else None
    stored_api_key = (
        _read_secret_for_paths(secret_ref, paths)
        if secret_ref and not _secret_ref_requires_reentry(settings, secret_ref)
        else ""
    )
    resolved_api_key = stored_api_key or settings_api_key or env_api_key or config.LLM_API_KEY
    resolved = {
        "provider": resolved_provider,
        "endpoint": resolved_endpoint,
        "model": str(provider.get("model") or ""),
        "api": resolved_api,
        "contextWindow": provider.get("contextWindow"),
        "maxTokens": provider.get("maxTokens"),
        "pipelineConcurrency": _positive_int(
            provider.get("pipelineConcurrency"),
            DEFAULT_PIPELINE_CONCURRENCY,
        ),
        "pipelineGateMode": settings_gate_mode,
        "pipelineGateTokens": resolved_gate,
        "autoPipelineGateTokens": auto_gate,
        "pipelineGateDrift": resolved_gate != auto_gate,
        "timeoutSeconds": _positive_int(
            provider.get("timeoutSeconds"),
            DEFAULT_LLM_TIMEOUT_SECONDS,
        ),
        "apiKey": resolved_api_key,
        "apiKeyEnv": api_key_env_name,
        "secretRef": copy.deepcopy(secret_ref) if secret_ref else {},
    }
    resolved["hasApiKey"] = bool(resolved_api_key)
    resolved["source"] = {
        "provider": "settings",
        "endpoint": "settings" if resolved_endpoint else "unset",
        "model": "settings" if resolved["model"] else "unset",
        "apiKey": (
            "secret-store"
            if stored_api_key
            else ("settings" if settings_api_key else ("env" if env_api_key else "default"))
        ),
    }
    return resolved


def _llm_provider_entry_readiness(
    provider: dict[str, Any],
    require_cross_process_secret: bool,
) -> dict[str, Any]:
    missing = [
        field
        for field in ("endpoint", "model", "apiKey")
        if not str(provider.get(field) or "").strip()
    ]
    api = str(provider.get("api") or "")
    secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else {}
    backend = str(secret_ref.get("backend") or "")
    error = ""
    status = "ready"
    if missing:
        status = "missing-configuration"
        error = "missing " + ", ".join(missing)
    elif api not in SUPPORTED_APIS:
        status = "unsupported-api"
        error = f"unsupported API transport: {api or 'unset'}"
    elif require_cross_process_secret and backend == "memory":
        status = "cross-process-secret-unavailable"
        error = "process-local memory secrets cannot be used by pipeline subprocesses"
    readiness = {
        "ready": not error,
        "status": status,
        "missing": missing,
        "requireCrossProcessSecret": bool(require_cross_process_secret),
        "secretBackend": backend or "environment-or-unset",
    }
    if error:
        readiness["error"] = error
    return readiness


def llm_provider_chain_readiness_error(
    paths: RuntimePaths | None = None,
    require_cross_process_secret: bool = False,
) -> str | None:
    """Return the first safe, user-facing provider-chain readiness error."""
    chain = resolve_llm_provider_chain(
        paths,
        False,
        require_cross_process_secret,
    )
    for entry in chain:
        readiness = entry.get("readiness") if isinstance(entry.get("readiness"), dict) else {}
        if readiness.get("ready"):
            continue
        entry_id = str(entry.get("entryId") or "provider")
        role = str(entry.get("role") or "fallback")
        error = str(readiness.get("error") or "not ready")
        api_key_env = _safe_env_var_name_for_message(
            str(entry.get("apiKeyEnv") or "LLM_API_KEY")
        )
        if "apiKey" in (readiness.get("missing") or []):
            error += f"; save a readable secret or set {api_key_env}"
        return f"LLM provider chain entry {entry_id} ({role}) is not ready: {error}."
    return None


def llm_provider_readiness_error(
    paths: RuntimePaths | None = None,
    *,
    require_cross_process_secret: bool = False,
) -> str | None:
    """Return a user-facing error when the persisted LLM provider is unusable."""
    settings = _read_settings_for_resolution(paths or load_paths())
    if _configured_llm_provider_chain(settings):
        return llm_provider_chain_readiness_error(
            paths,
            require_cross_process_secret,
        )
    # Readiness is a verifier/status boundary, not a secret-migration
    # boundary. In particular, this may run while a Settings transaction is
    # still active; re-entering mutation recovery here could compensate the
    # very transaction being verified.
    provider = resolve_llm_provider(
        paths,
        redact_secrets=False,
        _migrate_persisted_secrets=False,
    )
    missing = [field for field in ("endpoint", "model", "apiKey") if not str(provider.get(field) or "").strip()]
    if not missing:
        secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else {}
        if require_cross_process_secret and secret_ref.get("backend") == "memory":
            return (
                "LLM provider is not ready for pipeline execution: apiKey is stored in the process-local "
                "memory backend, which daily pipeline subprocesses cannot read. Re-save the Provider using "
                "the runtime-file backend or set the configured apiKeyEnv before running."
            )
        return None
    api_key_env = _safe_env_var_name_for_message(str(provider.get("apiKeyEnv") or "LLM_API_KEY"))
    if "apiKey" in missing:
        secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else {}
        backend = str(secret_ref.get("backend") or "").strip()
        if backend:
            return (
                "LLM provider is not ready for pipeline execution: apiKey is not readable from the configured "
                f"{backend} secret reference. Re-save the Provider in Dashboard or set {api_key_env} before running."
            )
        return (
            "LLM provider is not ready for pipeline execution: missing apiKey. "
            f"Save the Provider in Dashboard or set {api_key_env} before running."
        )
    return "LLM provider is not ready for pipeline execution: missing " + ", ".join(missing) + "."


def _safe_env_var_name_for_message(name: str) -> str:
    return name if ENV_VAR_NAME_RE.match(name) else "the configured apiKeyEnv"


def _normalized_persisted_api_key_env(name: str) -> str:
    return name if ENV_VAR_NAME_RE.match(name) else "LLM_API_KEY"


def _pipeline_gate_mode(provider: dict) -> str:
    mode = str(provider.get("pipelineGateMode") or provider.get("pipelineGateSource") or "").strip().lower()
    if mode in {PIPELINE_GATE_MODE_AUTO, PIPELINE_GATE_MODE_MANUAL}:
        return mode
    if provider.get("pipelineGateTokens"):
        return PIPELINE_GATE_MODE_MANUAL
    return PIPELINE_GATE_MODE_AUTO


def _preserve_legacy_manual_pipeline_gate(current: dict, merged: dict) -> None:
    current_provider = current.get("llmProvider") if isinstance(current.get("llmProvider"), dict) else {}
    merged_provider = merged.get("llmProvider") if isinstance(merged.get("llmProvider"), dict) else {}
    if not current_provider or not merged_provider:
        return
    if current_provider.get("pipelineGateTokens") and not current_provider.get("pipelineGateMode"):
        merged_provider["pipelineGateMode"] = PIPELINE_GATE_MODE_MANUAL
        merged_provider["pipelineGateTokens"] = current_provider["pipelineGateTokens"]
        merged_provider["autoPipelineGateTokens"] = auto_pipeline_gate_tokens(
            merged_provider.get("contextWindow"),
            DEFAULT_PIPELINE_GATE_TOKENS,
        )


def _explicit_env_override(name: str, settings_has_value: bool) -> str | None:
    if settings_has_value:
        return None
    value = os.getenv(name)
    if value is None:
        return None
    return value


def _external_tool_value(value: Any) -> Any:
    if isinstance(value, list):
        return [Path(str(item)).expanduser().absolute() for item in value]
    if isinstance(value, str):
        return Path(value).expanduser().absolute()
    return value


def _path_check(value: Any, *, patterns: list[str] | None = None, recursive_patterns: list[str] | None = None) -> dict:
    if not isinstance(value, Path):
        return {"path": str(value), "exists": False, "readable": False, "sampleCount": 0}
    exists = value.exists()
    readable = False
    sample_count = 0
    try:
        if value.is_dir():
            readable = True
            for pattern in patterns or []:
                sample_count += len(list(value.glob(pattern))[:20])
            for pattern in recursive_patterns or []:
                sample_count += len(list(value.rglob(pattern))[:20])
        elif value.is_file():
            with value.open("rb"):
                pass
            readable = True
            sample_count = 1
    except OSError:
        readable = False
    return {"path": str(value), "exists": exists, "readable": readable, "sampleCount": sample_count}


def _path_list_check(value: Any) -> dict:
    paths = value if isinstance(value, list) else []
    checks = [_path_check(path) for path in paths]
    return {
        "path": str(paths[0]) if paths else "",
        "paths": [item["path"] for item in checks],
        "exists": any(item["exists"] for item in checks),
        "readable": any(item["readable"] for item in checks),
        "sampleCount": sum(int(item["sampleCount"]) for item in checks),
    }


def runtime_environment_overrides(paths: RuntimePaths | None = None) -> dict[str, str]:
    """Build child-process env values from settings while preserving explicit env overrides."""
    overrides: dict[str, str] = {}
    if paths is not None:
        overrides["ACTANARA_HOME"] = str(paths.home)
    for env_name, value in resolve_runtime_sources(paths).items():
        overrides[env_name] = value
    provider = resolve_llm_provider(paths, redact_secrets=False)
    provider_env = {
        "LLM_PROVIDER": provider["provider"],
        "LLM_HOST": provider["endpoint"],
        "LLM_MODEL_NAME": provider["model"],
        "LLM_API": provider["api"],
        "LLM_API_KEY": provider["apiKey"],
        "LLM_PIPELINE_CONCURRENCY": provider["pipelineConcurrency"],
        "LLM_PIPELINE_GATE_TOKENS": provider["pipelineGateTokens"],
    }
    api_key_env_name = str(provider.get("apiKeyEnv") or "").strip()
    if api_key_env_name:
        provider_env[api_key_env_name] = provider["apiKey"]
    for env_name, value in provider_env.items():
        overrides[env_name] = str(value)
    return overrides


def runtime_authority_contract(paths: RuntimePaths | None = None, *, persist_defaults: bool = True) -> dict:
    settings = read_settings(paths, redact_secrets=True, persist_defaults=persist_defaults)
    return {
        "settingsPath": settings.get("settingsPath"),
        "precedence": ["settings.json", "managed runtime environment", "checked-in Foundation default"],
        "general": resolve_general_settings(paths),
        "runtimeSources": resolve_runtime_sources(paths),
        "pipeline": resolve_pipeline_settings(paths),
        "dashboard": resolve_dashboard_settings(paths),
        "llmProvider": resolve_llm_provider(paths, redact_secrets=True),
        "llmProviderChain": resolve_llm_provider_chain(paths, redact_secrets=True),
        "settingsAuthority": settings_authority_inventory(paths, persist_defaults=persist_defaults),
        "archivedLegacyAccess": {
            "runtimeSources": list(RUNTIME_SOURCE_FIELDS),
            "normalProductionFallbackAllowed": False,
            "llmProvider": [
                "LLM_PROVIDER",
                "LLM_HOST",
                "LLM_MODEL_NAME",
                "LLM_API",
                "LLM_API_KEY",
                "LLM_PIPELINE_CONCURRENCY",
                "LLM_PIPELINE_GATE_TOKENS",
            ],
        },
    }


def settings_authority_inventory(paths: RuntimePaths | None = None, *, persist_defaults: bool = True) -> dict:
    """Describe the canonical owner and effective source for runtime settings."""
    settings = read_settings(paths, redact_secrets=True, persist_defaults=persist_defaults)
    resolved_general = resolve_general_settings(paths)
    resolved_sources = resolve_runtime_sources(paths)
    resolved_pipeline = resolve_pipeline_settings(paths)
    resolved_dashboard = resolve_dashboard_settings(paths)
    resolved_provider = resolve_llm_provider(paths, redact_secrets=True)
    resolved_provider_chain = resolve_llm_provider_chain(paths, redact_secrets=True)
    groups = []
    for group in SETTINGS_AUTHORITY_GROUPS:
        fields = []
        for field in group["fields"]:
            path = field["path"]
            env_name = field.get("env")
            settings_value = _get_nested(settings, path)
            effective_value = _effective_authority_value(
                path,
                settings_value,
                resolved_general,
                resolved_sources,
                resolved_pipeline,
                resolved_dashboard,
                resolved_provider,
                resolved_provider_chain,
            )
            source = "settings" if settings_value is not None else "default"
            env_override = env_name is not None and os.getenv(str(env_name)) is not None
            if env_override and settings_value is None:
                source = "env"
            fields.append(
                {
                    **field,
                    "source": source,
                    "envOverride": env_override,
                    "settingsPresent": settings_value is not None,
                    "settingsValue": _redact_authority_value(path, settings_value),
                    "effectiveValue": _redact_authority_value(path, effective_value),
                    **_field_mode_metadata(field, settings, resolved_provider),
                }
            )
        groups.append({key: group[key] for key in ("group", "authority", "writableVia", "manualDefaultPolicy")} | {"fields": fields})
    return {
        "schemaVersion": 1,
        "precedence": ["settings.json", "managed runtime environment", "derived/default"],
        "settingsPath": settings.get("settingsPath"),
        "policy": {
            "singleWriter": "$ACTANARA_HOME/config/settings.json for persisted runtime choices",
            "envSemantics": "process-local bootstrap/secret injection or diagnostic noise; persisted settings remain normal authority",
            "manualVsDefault": "manual operator choices must be explicit; derived defaults must expose their source and auto value",
            "secretHandling": "API keys and secret-like values are redacted in read APIs",
        },
        "groups": groups,
    }


def build_agent_schedule_prompt(settings: dict | None = None) -> str:
    settings = settings or read_settings()
    schedule = settings.get("schedule", {})
    paths = settings.get("paths", {})
    runtime = paths.get("runtime", {}) if isinstance(paths, dict) else {}
    actanara_home = str(runtime.get("actanaraHome", config.ACTANARA_HOME))
    pipeline_command = f'"{actanara_home}/bin/actanara" pipeline'
    aggregation_command = (
        f'"{actanara_home}/.venv/bin/python" '
        f'"{actanara_home}/app/source/advanced/pipeline/run_dashboard_foundation_refresh.py"'
    )
    pipeline = settings.get("pipeline", {}) if isinstance(settings.get("pipeline"), dict) else {}
    language_profile = resolve_pipeline_language_profile(pipeline.get("languageProfile"))
    if language_profile.profile_id == "en":
        return "\n".join(
            [
                "Act as the sole external scheduler for Actanara. Trigger the two jobs below; Actanara remains responsible for pipeline and snapshot logic.",
                "Prerequisite: enable this mode only after the managed system scheduler is disabled or uninstalled. Never run both schedulers.",
                f"Timezone: {schedule.get('timezone', config.TARGET_TIMEZONE)}",
                f"ACTANARA_HOME: {actanara_home}",
                f"Job 1 — daily pipeline at {schedule.get('dailyPipelineTime', '04:00')}: {pipeline_command}",
                f"Job 2 — Dashboard Foundation aggregation at {schedule.get('dashboardAggregationTime', '04:30')}: {aggregation_command}",
                "Execution rules: do not overlap runs; do not add --force unless the operator explicitly requests regeneration; inspect every exit status; retry at most once only for a clearly transient failure.",
                "Safety rules: do not edit settings, source data, diary outputs, prompt payloads, RAG indexes, LaunchAgents, or scheduler registration. Do not invent replacement commands.",
                "After each run, report the job name, scheduled time, actual start/end time, exit status, and a concise error summary when unsuccessful.",
            ]
        )
    return "\n".join(
        [
            "请作为 Actanara 唯一的外部定时触发器，按下列计划触发两个任务；管线与 snapshot 逻辑仍由 Actanara 自身负责。",
            "前提：仅在系统托管 scheduler 已停用或卸载后启用此模式，严禁两套 scheduler 同时运行。",
            f"时区：{schedule.get('timezone', config.TARGET_TIMEZONE)}",
            f"ACTANARA_HOME：{actanara_home}",
            f"任务 1 — 每日管线，执行时间 {schedule.get('dailyPipelineTime', '04:00')}：{pipeline_command}",
            f"任务 2 — Dashboard Foundation 聚合，执行时间 {schedule.get('dashboardAggregationTime', '04:30')}：{aggregation_command}",
            "执行规则：禁止任务重叠；除非操作者明确要求重新生成，否则不要添加 --force；检查每次退出状态；仅在明确属于临时故障时最多重试一次。",
            "安全规则：不要修改 settings、源数据、日记产物、prompt payload、RAG index、LaunchAgent 或 scheduler 注册；不要自行发明替代命令。",
            "每次运行后报告任务名称、计划时间、实际开始/结束时间、退出状态；失败时附简短错误摘要。",
        ]
    )


def read_scheduler_state(paths: RuntimePaths | None = None) -> dict:
    paths = paths or load_paths()
    return _read_json(_scheduler_state_path(paths))


def write_scheduler_state(update: dict[str, Any], paths: RuntimePaths | None = None) -> dict:
    paths = paths or load_paths()
    current = read_scheduler_state(paths)
    current.update(update)
    current["updatedAt"] = _now_iso()
    _write_json_atomic(_scheduler_state_path(paths), current)
    return current


def _deep_merge(base: dict, update: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _get_nested(payload: dict, dotted_path: str) -> Any:
    value: Any = payload
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _effective_authority_value(
    path: str,
    settings_value: Any,
    general: dict,
    runtime_sources: dict,
    pipeline: dict,
    dashboard: dict,
    provider: dict,
    provider_chain: list[dict[str, Any]],
) -> Any:
    if path.startswith("general."):
        return general.get(path.split(".", 1)[1], settings_value)
    if path.startswith("runtimeSources."):
        for env_name, settings_field in RUNTIME_SOURCE_FIELDS.items():
            if path == f"runtimeSources.{settings_field}":
                return runtime_sources.get(env_name)
    if path.startswith("pipeline."):
        return pipeline.get(path.split(".", 1)[1], settings_value)
    if path.startswith("dashboard."):
        return dashboard.get(path.split(".", 1)[1], settings_value)
    if path.startswith("llmProvider."):
        key = path.split(".", 1)[1]
        return provider.get(key, settings_value)
    if path == "llmProviderChain":
        return provider_chain
    return settings_value


def _field_mode_metadata(field: dict, settings: dict, provider: dict) -> dict:
    if field.get("path") != "llmProvider.pipelineGateTokens":
        return {}
    return {
        "mode": provider.get("pipelineGateMode") or _get_nested(settings, "llmProvider.pipelineGateMode") or "auto",
        "autoValue": provider.get("autoPipelineGateTokens"),
        "drift": provider.get("pipelineGateDrift"),
    }


def _redact_authority_value(path: str, value: Any) -> Any:
    if value is None:
        return None
    lowered = path.lower()
    if "apikey" in lowered or "secret" in lowered or "token" in lowered and "pipelinegatetokens" not in lowered:
        return MASKED_SECRET if value else ""
    if path == "llmProviderChain" and isinstance(value, list):
        return [_redact_provider_chain_authority_entry(entry) for entry in value]
    return value


def _redact_provider_chain_authority_entry(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, nested_value in value.items():
        lowered = str(key).lower()
        if "apikey" in lowered:
            result[key] = MASKED_SECRET if nested_value else ""
        elif "secret" in lowered and key != "secretBackend":
            result[key] = bool(nested_value) if key == "secretRef" else nested_value
        elif isinstance(nested_value, dict):
            result[key] = _redact_provider_chain_authority_entry(nested_value)
        else:
            result[key] = nested_value
    return result


def _normalize_runtime_source(value: Any, env_name: str) -> str:
    normalized = str(value or "legacy").strip().lower()
    if normalized not in VALID_RUNTIME_SOURCES:
        raise ValueError(f"{env_name} must be one of {sorted(VALID_RUNTIME_SOURCES)}, got {value!r}")
    return normalized


def _runtime_source_env_name(field_name: str) -> str:
    for env_name, settings_field in RUNTIME_SOURCE_FIELDS.items():
        if settings_field == field_name:
            return env_name
    return field_name


def _positive_int(*values: Any) -> int:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 1


def _bool_setting(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _foundation_runtime_settings_enabled() -> bool:
    settings = _read_settings_for_resolution()
    features = settings.get("features") if isinstance(settings.get("features"), dict) else {}
    return _bool_setting(features.get("foundationSnapshots"), True)


def _read_settings_for_resolution(paths: RuntimePaths | None = None) -> dict:
    """Read settings for runtime resolution without creating or migrating files."""
    if paths is not None:
        if not isinstance(paths, RuntimePaths):
            return {}
        return read_settings(paths, redact_secrets=False, persist_defaults=False)
    selected = load_paths()
    return _read_json(_settings_path(selected))
