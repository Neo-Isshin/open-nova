#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
DEFAULT_SOURCE_ROOT="${SCRIPT_DIR:h}"
SOURCE_ROOT="${ACTANARA_INSTALL_SOURCE_ROOT:-$DEFAULT_SOURCE_ROOT}"
PYTHON_BIN="${ACTANARA_INSTALL_PYTHON:-python3}"
PYTHON_AUTO_INSTALL="${ACTANARA_INSTALL_PYTHON_AUTO_INSTALL:-1}"
PYTHON_CANDIDATES="${ACTANARA_INSTALL_PYTHON_CANDIDATES:-}"
PYTHON_INSTALL_PLANNED=0
PYTHON_STANDALONE_RELEASE="${ACTANARA_INSTALL_PYTHON_STANDALONE_RELEASE:-20260623}"
PYTHON_STANDALONE_VERSION="${ACTANARA_INSTALL_PYTHON_STANDALONE_VERSION:-3.13.14}"
PYTHON_STANDALONE_BASE_URL="${ACTANARA_INSTALL_PYTHON_STANDALONE_BASE_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_STANDALONE_RELEASE}}"
PYTHON_STANDALONE_URL="${ACTANARA_INSTALL_PYTHON_STANDALONE_URL:-}"
PYTHON_STANDALONE_SHA256="${ACTANARA_INSTALL_PYTHON_STANDALONE_SHA256:-}"
PYTHON_STANDALONE_DIR="${ACTANARA_INSTALL_PYTHON_STANDALONE_DIR:-}"
CURL_BIN="${ACTANARA_INSTALL_CURL:-}"
TAR_BIN="${ACTANARA_INSTALL_TAR:-}"
SHASUM_BIN="${ACTANARA_INSTALL_SHASUM:-}"
OPENSSL_BIN="${ACTANARA_INSTALL_OPENSSL:-}"
RUNTIME_HOME="${ACTANARA_INSTALL_RUNTIME:-$HOME/.actanara}"
DIARY_OUTPUT_DIR="${ACTANARA_INSTALL_DIARY_OUTPUT:-}"
DESKTOP_DIARY_LINK="${ACTANARA_INSTALL_DESKTOP_DIARY_LINK:-$HOME/Desktop/Actanara}"
REPORTS_OUTPUT_DIR="${ACTANARA_INSTALL_REPORTS_OUTPUT:-}"
SNAPSHOTS_OUTPUT_DIR="${ACTANARA_INSTALL_SNAPSHOTS_OUTPUT:-}"
ARCHIVES_OUTPUT_DIR="${ACTANARA_INSTALL_ARCHIVES_OUTPUT:-}"
VENV_DIR=""
NO_SCHEDULER=0
NO_DASHBOARD_SERVER=0
ENABLE_DASHBOARD=1
ENABLE_RAG=0
DEPLOY_EMBEDDING_SERVER=0
ENABLE_NOVA_TASK=1
ENABLE_LLM_GENERATION=1
ENABLE_DEV_TEST=0
CREATE_DESKTOP_DIARY_LINK=1
ENABLE_SHELL_PATH=1
ENABLE_SKILL_REGISTRATION=0
DASHBOARD_HOST="${ACTANARA_INSTALL_DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${ACTANARA_INSTALL_DASHBOARD_PORT:-${ACTANARA_DASHBOARD_PORT:-3036}}"
DASHBOARD_PORT_AUTO="${ACTANARA_INSTALL_DASHBOARD_PORT_AUTO:-1}"
DASHBOARD_PORT_CANDIDATES="${ACTANARA_INSTALL_DASHBOARD_PORT_CANDIDATES:-3036 8765 8766 8767 8768}"
LSOF_BIN="${ACTANARA_INSTALL_LSOF:-}"
INSTALL_TEST_MODE="${ACTANARA_INSTALL_TEST_MODE:-0}"
if [[ "$INSTALL_TEST_MODE" != "0" && "$INSTALL_TEST_MODE" != "1" ]]; then
  print -r -- "ACTANARA_INSTALL_TEST_MODE must be 0 or 1" >&2
  exit 2
fi
LAUNCHCTL_BIN=""
if [[ "$INSTALL_TEST_MODE" == "1" ]]; then
  LAUNCHCTL_BIN="${ACTANARA_INSTALL_LAUNCHCTL:-}"
fi
RAG_EMBEDDING_MODE="${ACTANARA_INSTALL_RAG_EMBEDDING_MODE:-local}"
RAG_LOCAL_MODEL="${ACTANARA_INSTALL_RAG_LOCAL_MODEL:-intfloat/multilingual-e5-small}"
RAG_LOCAL_DIMENSION="${ACTANARA_INSTALL_RAG_LOCAL_DIMENSION:-384}"
RAG_LOCAL_MODEL_SET=0
if [[ -n "${ACTANARA_INSTALL_RAG_LOCAL_MODEL:-}" ]]; then
  RAG_LOCAL_MODEL_SET=1
fi
LLM_PROVIDER_MODE="${ACTANARA_INSTALL_LLM_PROVIDER_MODE:-custom}"
LLM_PROVIDER="${ACTANARA_INSTALL_LLM_PROVIDER:-custom}"
LLM_API="${ACTANARA_INSTALL_LLM_API:-openai-compatible}"
LLM_ENDPOINT="${ACTANARA_INSTALL_LLM_ENDPOINT:-}"
LLM_MODEL="${ACTANARA_INSTALL_LLM_MODEL:-}"
LLM_API_KEY_ENV="${ACTANARA_INSTALL_LLM_API_KEY_ENV:-LLM_API_KEY}"
LLM_API_KEY_VALUE="${ACTANARA_INSTALL_LLM_API_KEY_VALUE:-}"
RAG_CLOUD_PROVIDER="${ACTANARA_INSTALL_RAG_CLOUD_PROVIDER:-openai-compatible}"
RAG_CLOUD_ENDPOINT="${ACTANARA_INSTALL_RAG_CLOUD_ENDPOINT:-}"
RAG_CLOUD_MODEL="${ACTANARA_INSTALL_RAG_CLOUD_MODEL:-}"
RAG_CLOUD_DIMENSION="${ACTANARA_INSTALL_RAG_CLOUD_DIMENSION:-}"
RAG_CLOUD_API_KEY_ENV="${ACTANARA_INSTALL_RAG_CLOUD_API_KEY_ENV:-NOVA_RAG_CLOUD_API_KEY}"
INSTALL_LANGUAGE="${ACTANARA_INSTALL_LANGUAGE:-zh-CN}"
PIPELINE_LANGUAGE_PROFILE="zh"
PIPELINE_ENGLISH_ENABLED=0
PIPELINE_DIARY_SCHEMA_VERSION="diary-v1-zh"
PIPELINE_PROMPT_PAYLOAD_PROFILE="zh-CN"
RAG_LANGUAGE_PROFILE="zh"
DRY_RUN=0
UPGRADE=0
UPGRADE_EXPLICIT=0
REPAIR_EXISTING=0
SOURCE_ONLY=0
FORCE_REBUILD=0
FORCE_REBUILD_EXPLICIT=0
OFFLINE=0
RESULT_JSON=0
UPDATE_RESULT_EMITTED=0
UPDATE_RESULT_STAGE="initializing"
UPDATE_MODE="not-evaluated"
UPDATE_REASON="dependency-plan-not-evaluated"
UPDATE_REUSES_VENV=0
UPDATE_DEPENDENCIES_INSTALLED=0
UPDATE_SOURCE_UPDATED=0
UPDATE_CACHE_USED=0
UPDATE_SERVICES_STOPPED=0
UPDATE_PLISTS_NORMALIZED=0
UPDATE_PLANNED_DEPENDENCIES_INSTALL=0
UPDATE_NOOP=0
UPDATE_ROLLBACK_COMPLETE=-1
UPDATE_STATE_CERTAIN=1
UPDATE_DEPENDENCY_FINGERPRINT=""
UPDATE_DEPENDENCY_PYTHON=""
UPDATE_PYTHON_SELECTION_REASON=""
DEPENDENCY_PLAN_CACHE_HIT=0
DEPENDENCY_PLAN_FAIL_BEFORE_STOP=0
DEPENDENCY_PROFILE_SOURCE="installer-arguments"
DEPENDENCY_PROFILE_SETTINGS_SHA256=""
DEPENDENCY_PROFILE_ACTIVE_VENV_TARGET=""
DEPENDENCY_PROFILE_MARKER_STATUS=""
DEPENDENCY_PROFILE_MARKER_SHA256=""
WIZARD_MODE="${ACTANARA_INSTALL_WIZARD:-auto}"
YES=0
WIZARD_CONFIRMED=0
SUMMARY_ONLY=0
UNAME_BIN="$(command -v uname 2>/dev/null || true)"
if [[ -z "$UNAME_BIN" && -x "/usr/bin/uname" ]]; then
  UNAME_BIN="/usr/bin/uname"
fi
ID_BIN="$(command -v id 2>/dev/null || true)"
if [[ -z "$ID_BIN" && -x "/usr/bin/id" ]]; then
  ID_BIN="/usr/bin/id"
fi
if [[ "$INSTALL_TEST_MODE" == "1" && -n "${ACTANARA_INSTALL_PLATFORM:-}" ]]; then
  PLATFORM="$ACTANARA_INSTALL_PLATFORM"
elif [[ -n "$UNAME_BIN" ]]; then
  PLATFORM="$("$UNAME_BIN" -s)"
else
  PLATFORM="unknown"
fi
RUNTIME_SET=0
DIARY_OUTPUT_SET=0
DESKTOP_DIARY_LINK_SET=0
REPORTS_OUTPUT_SET=0
SNAPSHOTS_OUTPUT_SET=0
ARCHIVES_OUTPUT_SET=0
PYTHON_SET=0
NO_SCHEDULER_SET=0
NO_DASHBOARD_SERVER_SET=0
DASHBOARD_PORT_SET=0
DASHBOARD_HOST_SET=0
RAG_SET=0
RAG_ENABLE_SET=0
RAG_DETAIL_SET=0
DEV_TEST_SET=0
EMBEDDING_SERVER_SET=0
LLM_SET=0
RAG_EMBEDDING_MODE_SET=0
LANGUAGE_SET=0
LANGUAGE_SELECTED=0
SHELL_PATH_SET=0
SELECTED_EXTERNAL_TOOLS=""
CLI_SHIM=""
USER_CLI_SHIM="${ACTANARA_INSTALL_USER_CLI_SHIM:-$HOME/.local/bin/actanara}"
SHELL_PATH_FILE="${ACTANARA_INSTALL_SHELL_PATH_FILE:-}"
INSTALLER_LOG_FILE=""
INSTALLER_LOG_ACTIVE=0
STAGED_RELEASE_ID=""
STAGED_RELEASE_TARGET=""
UPDATE_TRANSACTION_ACTIVE=0
UPDATE_TRANSACTION_ID=""
UPDATE_TRANSACTION_DIR=""
UPDATE_TRANSACTION_JOURNAL=""
REPAIR_BACKUP_DIR=""
REPAIR_RAG_SERVICE_ENABLED=0
REPAIR_CONFIGURATION_COMPLETE=0
UPDATE_VALIDATION_RUNTIME=""
UPDATE_SERVICE_STATE_FILE=""
UPDATE_PRIOR_SOURCE_KIND="missing"
UPDATE_PRIOR_SOURCE_TARGET=""
UPDATE_PRIOR_SOURCE_BACKUP=""
UPDATE_STAGED_VENV=""
FRESH_STAGED_VENV=""
UPDATE_PRIOR_VENV_BACKUP=""
UPDATE_MUTABLE_STATE_CAPTURED=0
UPDATE_ROLLBACK_RUNNING=0
UPDATE_COMMITTED=0
UPDATE_TEST_MODE="${INSTALL_TEST_MODE}"
UPDATE_TEST_FAIL_PHASE="${ACTANARA_INSTALL_TEST_FAIL_PHASE:-}"
UPDATE_TEST_HOOK="${ACTANARA_INSTALL_TEST_HOOK:-}"
UPDATE_TRANSACTION_HELPER="${SCRIPT_DIR}/update_transaction.py"
DEPENDENCY_CONTRACT_HELPER="${SCRIPT_DIR}/dependency_contract.py"
RUNTIME_DEPENDENCY_LOCK="${SOURCE_ROOT}/install/runtime-dependencies.lock.json"

usage() {
  cat <<'EOF'
Actanara setup

Usage:
  install/install.sh [options]

Options:
  --runtime PATH              Actanara folder. Default: ~/.actanara
  --diary-output PATH         Diary folder. Default: <Actanara folder>/artifacts/diary
  --desktop-diary-link PATH   Desktop diary shortcut. Default: ~/Desktop/Actanara
  --no-desktop-diary-link     Do not create the Desktop diary shortcut.
  --no-shell-path             Do not make actanara available in new Terminal sessions.
  --shell-path-file PATH      Terminal profile to update. Default: ~/.zprofile on macOS/zsh.
  --reports-output PATH       Report folder. Default: <Actanara folder>/artifacts/reports
  --snapshots-output PATH     Snapshot folder. Default: <Actanara folder>/snapshots
  --archives-output PATH      Imported-source folder. Default: <Actanara folder>/sources/archives
  --source-root PATH          Use an existing local copy of Actanara.
  --python PATH               Python command for Actanara. Default: python3
  --no-python-auto-install    Do not prepare Python automatically when Python 3.11+ is missing.
  --no-scheduler              Do not run Actanara automatically each day.
  --no-dashboard              Deprecated: use --no-dashboard-server.
  --no-dashboard-server       Install Dashboard without its background service.
  --dashboard-host HOST       Dashboard address. Default: 127.0.0.1.
  --dashboard-port PORT       Preferred Dashboard port. Default: 3036.
  --dashboard-port-auto       Choose another port when the preferred port is in use. Default.
  --no-dashboard-port-auto    Stop if the preferred Dashboard port is unavailable.
  --enable-rag                Enable memory and search.
  --register-memory-skills    Connect memory and search to selected external apps.
  --register-rag-skills       Compatibility alias for --register-memory-skills.
  --enable-dev-test           Advanced: include developer and test software.
  --rag-embedding-mode MODE   Memory model location: local or cloud. Default: local
  --rag-local-model MODEL     Local memory model.
  --rag-local-dimension N     Local memory model dimension.
  --deploy-embedding-server   Prepare local memory and search in the background.
                              Default when local memory and search is enabled.
  --no-deploy-embedding-server
                              Do not prepare local memory and search in the background.
  --llm-provider NAME         AI service.
  --llm-endpoint URL          AI service URL.
  --llm-model MODEL           AI model.
  --llm-api-key-env NAME      Environment variable that contains the AI API key.
  --rag-cloud-provider NAME   Cloud memory service.
  --rag-cloud-endpoint URL    Cloud memory service URL.
  --rag-cloud-model MODEL     Cloud memory model.
  --rag-cloud-dimension N     Cloud memory model dimension.
  --rag-cloud-api-key-env NAME
                              Environment variable that contains the cloud memory API key.
  --language LOCALE           Setup language: zh-CN or en-US. Default: zh-CN.
  --dry-run                   Print the plan without writing files or running commands.
  --upgrade                   Update an existing Actanara installation.
  --repair-existing           Rebuild a legacy Actanara installation while preserving user data.
  --source-only               Update Actanara files without changing installed software.
  --force-rebuild             Reinstall required software during an update.
  --offline                   Install using previously downloaded files only.
  --result-json               Print one final JSON update result for automation.
  --wizard                    Always use guided setup.
  --no-wizard                 Skip guided setup.
  --yes                       Skip final interactive confirmation.
  --summary-only              Print only the summary and next steps.
  -h, --help                  Show this help.

Privacy:
  Actanara reads the local activity and files you select to create diaries,
  reports, and search memory. Sensitive information in those sources may also
  appear in generated content. API keys are stored securely and never printed.
EOF
}

log() {
  if [[ "$SUMMARY_ONLY" == "1" ]]; then
    return 0
  fi
  local log_file=""
  log_file="$(installer_log_file)"
  if [[ -d "${log_file:h}" ]]; then
    print -r -- "==> $*" >> "$log_file"
  fi
  if [[ "${ACTANARA_INSTALL_VERBOSE:-0}" == "1" ]]; then
    print -r -- "==> $*"
  fi
}

emit_update_result() {
  local rc="${1:-0}"
  if [[ "$RESULT_JSON" != "1" || "$UPGRADE" != "1" || "$UPDATE_RESULT_EMITTED" == "1" ]]; then
    return 0
  fi
  local dependencies_installed="false"
  local reuses_runtime_venv="false"
  local source_updated="null"
  local cache_used="false"
  local services_stopped="false"
  local planned_dependencies_install="false"
  local plists_normalized="null"
  local rollback_complete="null"
  local state_certain="false"
  if [[ "$UPDATE_DEPENDENCIES_INSTALLED" == "1" ]]; then dependencies_installed="true"; fi
  if [[ "$UPDATE_REUSES_VENV" == "1" ]]; then reuses_runtime_venv="true"; fi
  if [[ "$UPDATE_SOURCE_UPDATED" == "1" ]]; then source_updated="true"; fi
  if [[ "$UPDATE_SOURCE_UPDATED" == "0" ]]; then source_updated="false"; fi
  if [[ "$UPDATE_CACHE_USED" == "1" ]]; then cache_used="true"; fi
  if [[ "$UPDATE_SERVICES_STOPPED" == "1" ]]; then services_stopped="true"; fi
  if [[ "$UPDATE_PLANNED_DEPENDENCIES_INSTALL" == "1" ]]; then planned_dependencies_install="true"; fi
  if [[ "$UPDATE_PLISTS_NORMALIZED" == "1" ]]; then plists_normalized="true"; fi
  if [[ "$UPDATE_PLISTS_NORMALIZED" == "0" ]]; then plists_normalized="false"; fi
  if [[ "$UPDATE_ROLLBACK_COMPLETE" == "1" ]]; then rollback_complete="true"; fi
  if [[ "$UPDATE_ROLLBACK_COMPLETE" == "0" ]]; then rollback_complete="false"; fi
  if [[ "$UPDATE_STATE_CERTAIN" == "1" ]]; then state_certain="true"; fi
  local result_status="failed"
  if [[ "$rc" == "0" ]]; then result_status="completed"; fi
  UPDATE_RESULT_EMITTED=1
  print -r -- "ACTANARA_UPDATE_RESULT_JSON={\"schemaVersion\":1,\"status\":\"${result_status}\",\"updateMode\":\"${UPDATE_MODE}\",\"dependenciesInstalled\":${dependencies_installed},\"reusesRuntimeVenv\":${reuses_runtime_venv},\"sourceUpdated\":${source_updated},\"reason\":\"${UPDATE_REASON}\",\"cacheUsed\":${cache_used},\"servicesStopped\":${services_stopped},\"plannedDependenciesInstall\":${planned_dependencies_install},\"managedServiceDefinitionsNormalized\":${plists_normalized},\"rollbackComplete\":${rollback_complete},\"stateCertain\":${state_certain},\"stage\":\"${UPDATE_RESULT_STAGE}\"}"
}

warn() {
  local technical_message="$*"
  if [[ "$SUMMARY_ONLY" == "1" ]]; then
    return 0
  fi
  local log_file=""
  log_file="$(installer_log_file)"
  if [[ -d "${log_file:h}" ]]; then
    print -r -- "WARN: ${technical_message}" >> "$log_file"
  fi
  if [[ "${ACTANARA_INSTALL_VERBOSE:-0}" == "1" ]]; then
    print -r -- "WARN: ${technical_message}" >&2
    return 0
  fi
  local friendly_message=""
  case "$technical_message" in
    *"Desktop diary shortcut"*) friendly_message="$(installer_text warning_diary_shortcut)" ;;
    *"PATH shim"*|*"Shell PATH"*) friendly_message="$(installer_text warning_terminal_command)" ;;
    *"Dashboard port"*|*"lsof"*) friendly_message="$(installer_text warning_dashboard_port)" ;;
    *"standalone Python"*|*"Python >=3.11"*) friendly_message="$(installer_text warning_python)" ;;
    *"external nova-RAG skill registration"*|*"external Memory Search skill"*) friendly_message="$(installer_text warning_tool_connection)" ;;
    *"SSE server disabled"*) friendly_message="$(installer_text warning_dashboard_service_disabled)" ;;
    *"Static snapshot pages"*) friendly_message="" ;;
    *"failed; continuing"*) friendly_message="$(installer_text warning_optional_step)" ;;
    *) friendly_message="$(installer_text warning_generic)" ;;
  esac
  if [[ -n "$friendly_message" ]]; then
    print -r -- "  ${TTY_YELLOW}!${TTY_RESET} ${friendly_message}" >&2
  fi
}

error() {
  local technical_message="$*"
  local log_file=""
  local friendly_key="error_setup"
  log_file="$(installer_log_file)"
  if [[ "$INSTALLER_LOG_ACTIVE" == "1" && -d "${log_file:h}" ]]; then
    print -r -- "ERROR: ${technical_message}" >> "$log_file"
  fi
  if [[ "${ACTANARA_INSTALL_VERBOSE:-0}" == "1" ]]; then
    print -r -- "ERROR: ${technical_message}" >&2
  else
    case "$technical_message" in
      *"CLI shim"*) friendly_key="error_terminal_command" ;;
      *"existing Actanara Runtime state"*) friendly_key="error_existing_install" ;;
      *"upgrade requires an existing runtime"*) friendly_key="error_update_missing" ;;
      *"language must"*) friendly_key="error_language" ;;
      *"Interactive wizard"*) friendly_key="error_terminal" ;;
      *"credential rotation"*) friendly_key="error_ai_key" ;;
      *"LLM API key environment variable name"*) friendly_key="error_ai_key_setting" ;;
      *"Dashboard is required"*) friendly_key="error_dashboard_required" ;;
      *"repair configuration is incomplete"*) friendly_key="error_repair_incomplete" ;;
      *"Unknown option"*|*"mutually exclusive"*|*"requires --upgrade"*|*"must be local or cloud"*) friendly_key="error_options" ;;
      *"pyproject.toml"*|*"source root"*|*"source staging"*|*"source release"*) friendly_key="error_source_files" ;;
      *"dependency"*|*"wheelhouse"*) friendly_key="error_software" ;;
      *"Python"*|*"venv"*) friendly_key="error_python" ;;
      *"RAG"*|*"embedding"*) friendly_key="error_memory" ;;
      *"Dashboard"*"port"*) friendly_key="error_dashboard" ;;
      *"rollback"*|*"transaction"*|*"promotion"*) friendly_key="error_recovery" ;;
    esac
    progress_fail "$(installer_text "$friendly_key")"
  fi
}

progress_label() {
  local cmd="$1"
  shift || true
  local command_line="$cmd $*"
  case "$command_line" in
    "mkdir -p "*)
      case "$command_line" in
        *"/app/venvs"*) installer_text step_prepare_python_files ;;
        *"/artifacts/diary"*|*"/artifacts/reports"*|*"/snapshots"*|*"/sources/archives"*) installer_text step_prepare_output_folders ;;
        *) installer_text step_prepare_folder ;;
      esac
      ;;
    *" -m venv "*)
      installer_text step_create_python
      ;;
    *" -m pip install --upgrade pip"*)
      installer_text step_prepare_components
      ;;
    *" -m pip install "*)
      installer_text step_install_components
      ;;
    *"dependency_contract.py cache-status "*)
      installer_text step_check_components
      ;;
    *"dependency_contract.py materialize-cache "*)
      installer_text step_prepare_components
      ;;
    *"dependency_contract.py install "*)
      installer_text step_install_components
      ;;
    *"dependency_contract.py write-marker "*)
      installer_text step_save_components
      ;;
    *"dependency_contract.py verify-marker "*)
      installer_text step_verify_components
      ;;
    *"dependency_contract.py"*)
      installer_text step_finish_components
      ;;
    *)
      installer_text step_continue_setup
      ;;
  esac
}

friendly_step_label() {
  local technical_label="$1"
  case "$technical_label" in
    "Runtime bootstrap apply") installer_text step_set_up_actanara ;;
    "Runtime status doctor") installer_text step_check_files ;;
    "Installer doctor") installer_text step_check_components ;;
    "Pipeline doctor") installer_text step_check_diary ;;
    "Scheduler doctor") installer_text step_check_daily ;;
    "nova-RAG doctor") installer_text step_check_memory ;;
    "Scheduler LaunchAgent plist write") installer_text step_prepare_daily ;;
    "Scheduler LaunchAgent registration") installer_text step_enable_daily ;;
    "SSE server LaunchAgent service registration") installer_text step_start_dashboard ;;
    "nova-RAG server LaunchAgent service registration") installer_text step_start_memory ;;
    "Candidate installer doctor") installer_text step_check_update ;;
    *) print -r -- "$technical_label" ;;
  esac
}

PROGRESS_ACTIVE=0

progress_start() {
  if [[ "$SUMMARY_ONLY" == "1" ]]; then
    return 0
  fi
  if [[ -t 1 ]]; then
    print -n -- "\r\033[2K  ${TTY_BLUE}●${TTY_RESET} $*"
    PROGRESS_ACTIVE=1
  fi
}

progress_ok() {
  if [[ "$SUMMARY_ONLY" == "1" ]]; then
    return 0
  fi
  if [[ -t 1 && "$PROGRESS_ACTIVE" == "1" ]]; then
    print -r -- "\r\033[2K  ${TTY_GREEN}✓${TTY_RESET} $*"
  else
    print -r -- "  ${TTY_GREEN}✓${TTY_RESET} $*"
  fi
  PROGRESS_ACTIVE=0
}

progress_fail() {
  if [[ -t 2 && "$PROGRESS_ACTIVE" == "1" ]]; then
    print -r -- "\r\033[2K  ${TTY_RED}✕${TTY_RESET} $*" >&2
  else
    print -r -- "  ${TTY_RED}✕${TTY_RESET} $*" >&2
  fi
  PROGRESS_ACTIVE=0
}

run_cmd() {
  local label=""
  local log_file=""
  if [[ "$SUMMARY_ONLY" == "1" && "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  label="$(progress_label "$@")"
  progress_start "$label"
  if [[ "$DRY_RUN" != "1" ]]; then
    log_file="$(installer_log_file)"
    mkdir -p "${log_file:h}"
    print -r -- "" >> "$log_file"
    print -r -- "## ${label}: $*" >> "$log_file"
    if ! "$@" >> "$log_file" 2>&1; then
      progress_fail "$(installer_text step_failed) ${log_file}"
      return 1
    fi
  fi
  progress_ok "$label"
}

run_optional_cmd() {
  local technical_label="$1"
  shift
  local label=""
  local log_file=""
  if [[ "$SUMMARY_ONLY" == "1" && "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  label="$(friendly_step_label "$technical_label")"
  progress_start "$label"
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_ok "$label"
    return 0
  fi
  log_file="$(installer_log_file)"
  mkdir -p "${log_file:h}"
  print -r -- "" >> "$log_file"
  print -r -- "## ${technical_label}: $*" >> "$log_file"
  if ! "$@" >> "$log_file" 2>&1; then
    warn "${technical_label} failed; continuing because this is not required for core runtime install"
    return 0
  fi
  progress_ok "$label"
}

installer_log_file() {
  if [[ -n "$INSTALLER_LOG_FILE" ]]; then
    print -r -- "$INSTALLER_LOG_FILE"
  else
    print -r -- "${RUNTIME_HOME}/state/logs/installer-v2.log"
  fi
}

run_json_cmd() {
  local technical_label="$1"
  shift
  local label=""
  local log_file=""
  if [[ "$SUMMARY_ONLY" == "1" && "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  label="$(friendly_step_label "$technical_label")"
  progress_start "$label"
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_ok "$label"
    return 0
  fi
  log_file="$(installer_log_file)"
  mkdir -p "${log_file:h}"
  print -r -- "" >> "$log_file"
  print -r -- "## ${technical_label}: $*" >> "$log_file"
  if ! "$@" >> "$log_file" 2>&1; then
    progress_fail "$(installer_text step_failed) ${log_file}"
    return 1
  fi
  progress_ok "$label"
}

run_optional_json_cmd() {
  local technical_label="$1"
  shift
  local label=""
  local log_file=""
  if [[ "$SUMMARY_ONLY" == "1" && "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  label="$(friendly_step_label "$technical_label")"
  progress_start "$label"
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_ok "$label"
    return 0
  fi
  log_file="$(installer_log_file)"
  mkdir -p "${log_file:h}"
  print -r -- "" >> "$log_file"
  print -r -- "## ${technical_label}: $*" >> "$log_file"
  if ! "$@" >> "$log_file" 2>&1; then
    warn "${technical_label} failed; continuing because this is not required for core runtime install. See ${log_file}"
    return 0
  fi
  progress_ok "$label"
}

TTY_CLEAR=$'\033[2J\033[H'
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  TTY_BLUE=$'\033[36m'
  TTY_GREEN=$'\033[32m'
  TTY_YELLOW=$'\033[33m'
  TTY_RED=$'\033[31m'
  TTY_DIM=$'\033[2m'
  TTY_RESET=$'\033[0m'
else
  TTY_BLUE=""
  TTY_GREEN=""
  TTY_YELLOW=""
  TTY_RED=""
  TTY_DIM=""
  TTY_RESET=""
fi
TTY_DEEP_BLUE="$TTY_BLUE"

installer_version() {
  local version=""
  version="$(awk -F'"' '/^version = / { print $2; exit }' "${SOURCE_ROOT}/pyproject.toml" 2>/dev/null || true)"
  print -r -- "${version:-0.0.0}"
}

render_installer_header() {
  local version=""
  version="$(installer_version)"
  print -r -- "${TTY_BLUE}Actanara ${version}${TTY_RESET}" > /dev/tty
  print -r -- "$(installer_text setup_title)" > /dev/tty
  print -r -- "────────────────────────────────────────" > /dev/tty
}

render_console_header() {
  if [[ "$SUMMARY_ONLY" == "1" ]]; then
    return 0
  fi
  local version=""
  version="$(installer_version)"
  print -r -- "${TTY_BLUE}Actanara ${version}${TTY_RESET}"
  print -r -- "$(installer_text setup_title)"
  print -r -- "────────────────────────────────────────"
}

print_phase() {
  if [[ "$SUMMARY_ONLY" == "1" ]]; then
    return 0
  fi
  print -r -- ""
  print -r -- "${TTY_DEEP_BLUE}$(installer_text "$1")${TTY_RESET}"
}

print_installer_data_notice() {
  log "Data notice: Actanara processes local diaries, agent/tool history, and user-selected source material."
  log "Data notice: generated diaries, reports, snapshots, and indexes may preserve sensitive information already present in those inputs."
  log "Data notice: installer-managed settings store secret references or environment variable names, not secret values."
}

installer_text() {
  local key="$1"
  local text_language="$INSTALL_LANGUAGE"
  if [[ "$LANGUAGE_SET" != "1" && "$LANGUAGE_SELECTED" != "1" ]]; then
    text_language="en-US"
  fi
  case "$text_language" in
    en-US|en|en_US)
      case "$key" in
        nav) print -r -- "Use Up/Down or j/k, then Return." ;;
        yes_recommended) print -r -- "Yes (recommended)" ;;
        no) print -r -- "No" ;;
        setup_title) print -r -- "Setup" ;;
        phase_checking) print -r -- "Checking your system" ;;
        phase_preparing) print -r -- "Preparing Actanara" ;;
        phase_installing) print -r -- "Installing Actanara" ;;
        phase_configuring) print -r -- "Setting up your features" ;;
        phase_verifying) print -r -- "Finishing up" ;;
        step_prepare_folder) print -r -- "Preparing the Actanara folder" ;;
        step_prepare_output_folders) print -r -- "Preparing diaries and reports" ;;
        step_prepare_python_files) print -r -- "Preparing Python files" ;;
        step_prepare_python) print -r -- "Preparing Python" ;;
        step_create_python) print -r -- "Creating the Python environment" ;;
        step_prepare_components) print -r -- "Preparing required software" ;;
        step_install_components) print -r -- "Installing required software" ;;
        step_save_components) print -r -- "Saving installed software details" ;;
        step_verify_components) print -r -- "Confirming installed software" ;;
        step_check_components) print -r -- "Checking required software" ;;
        step_finish_components) print -r -- "Finishing software setup" ;;
        step_continue_setup) print -r -- "Continuing setup" ;;
        step_save_choices) print -r -- "Saving your choices" ;;
        step_save_ai_key) print -r -- "Saving your AI key securely" ;;
        step_diary_shortcut) print -r -- "Creating the Desktop diary shortcut" ;;
        step_install_command) print -r -- "Installing the actanara command" ;;
        step_terminal_command) print -r -- "Making actanara available in Terminal" ;;
        step_prepare_files) print -r -- "Preparing Actanara files" ;;
        step_activate_files) print -r -- "Activating Actanara files" ;;
        step_finish_install) print -r -- "Finishing the installation" ;;
        step_check_install) print -r -- "Checking the installation" ;;
        step_check_files) print -r -- "Checking Actanara files" ;;
        step_check_diary) print -r -- "Checking diary creation" ;;
        step_start_dashboard) print -r -- "Starting Dashboard" ;;
        step_start_memory) print -r -- "Starting memory and search" ;;
        step_connect_tools) print -r -- "Connecting selected tools" ;;
        step_check_update) print -r -- "Checking the update" ;;
        step_prepare_memory) print -r -- "Preparing memory and search" ;;
        step_set_up_actanara) print -r -- "Setting up Actanara" ;;
        step_set_up_daily) print -r -- "Setting up daily runs" ;;
        step_prepare_daily) print -r -- "Preparing daily runs" ;;
        step_enable_daily) print -r -- "Enabling daily runs" ;;
        step_check_daily) print -r -- "Checking daily runs" ;;
        step_check_memory) print -r -- "Checking memory and search" ;;
        step_failed) print -r -- "This step did not finish. See the setup log:" ;;
        update_needs_full_install) print -r -- "This update also needs component changes; run a full update instead." ;;
        warning_diary_shortcut) print -r -- "The Desktop diary shortcut was not changed; Actanara can still be used normally." ;;
        warning_terminal_command) print -r -- "The actanara command could not be added to new Terminal sessions automatically." ;;
        warning_dashboard_port) print -r -- "Dashboard will use the best available local address." ;;
        warning_python) print -r -- "Python could not be prepared safely; setup cannot continue." ;;
        warning_tool_connection) print -r -- "One selected tool could not be connected; you can retry from Dashboard Settings." ;;
        warning_dashboard_service_disabled) print -r -- "The Dashboard background service is turned off by your setup choices." ;;
        warning_optional_step) print -r -- "An optional step was not completed; core Actanara features are still available." ;;
        warning_generic) print -r -- "Setup needs your attention. See the setup log for details." ;;
        error_terminal_command) print -r -- "The actanara command could not be installed." ;;
        error_options) print -r -- "Some setup options cannot be used together. Run the setup with --help." ;;
        error_existing_install) print -r -- "Actanara is already installed in this folder. Use: actanara update --apply" ;;
        error_source_files) print -r -- "Required Actanara files are missing or unreadable." ;;
        error_update_missing) print -r -- "No existing Actanara installation was found in the selected folder." ;;
        error_software) print -r -- "Required software could not be prepared or verified." ;;
        error_python) print -r -- "Python could not be prepared safely." ;;
        error_memory) print -r -- "Memory and search settings need attention before setup can continue." ;;
        error_dashboard) print -r -- "The selected Dashboard address is not available." ;;
        error_language) print -r -- "Choose setup language zh-CN or en-US." ;;
        error_terminal) print -r -- "Interactive setup needs a Terminal. For automation, use --no-wizard." ;;
        error_ai_key) print -r -- "The AI key is not changed during an update. Save it after the update finishes." ;;
        error_ai_key_setting) print -r -- "The AI key setting is not valid." ;;
        error_dashboard_required) print -r -- "Dashboard is included with Actanara. Use --no-dashboard-server to leave its background service off." ;;
        error_recovery) print -r -- "The update did not finish cleanly. Your previous installation was kept; see the setup log." ;;
        error_repair_incomplete) print -r -- "Actanara was rebuilt, but setup did not finish. Your data is safe; run the one-liner again." ;;
        error_setup) print -r -- "Setup could not finish safely. See the setup log for details." ;;
        next_steps) print -r -- "Next steps" ;;
        plan_summary) print -r -- "Setup plan" ;;
        install_complete) print -r -- "Actanara is ready." ;;
        dry_run_complete) print -r -- "Your Actanara setup plan is ready." ;;
        upgrade_complete) print -r -- "Actanara is up to date." ;;
        upgrade_plan_complete) print -r -- "Your Actanara update plan is ready." ;;
        repair_complete) print -r -- "Actanara was rebuilt. Your settings and data were kept." ;;
        repair_plan_complete) print -r -- "Your Actanara rebuild plan is ready." ;;
        repair_backup) print -r -- "Recovery backup" ;;
        repair_incomplete) print -r -- "Actanara was rebuilt, but setup did not finish. Your data is safe; run the one-liner again." ;;
        update_no_changes) print -r -- "Actanara is already up to date." ;;
        source_update_complete) print -r -- "Actanara files are up to date." ;;
        source_update_plan_complete) print -r -- "Your Actanara file update plan is ready." ;;
        update_plan_summary) print -r -- "Update plan" ;;
        update_summary) print -r -- "Update summary" ;;
        label_command) print -r -- "Command line" ;;
        label_folder) print -r -- "Actanara folder" ;;
        label_diary) print -r -- "Diaries" ;;
        label_dashboard) print -r -- "Dashboard" ;;
        label_daily) print -r -- "Daily runs" ;;
        label_ai) print -r -- "AI generation" ;;
        label_memory) print -r -- "Memory and search" ;;
        label_tools) print -r -- "Connected tools" ;;
        detail_planned) print -r -- "will be prepared" ;;
        detail_ready) print -r -- "ready" ;;
        detail_disabled) print -r -- "not enabled" ;;
        detail_dashboard_app) print -r -- "installed; background service is off" ;;
        detail_daily_on) print -r -- "enabled" ;;
        detail_daily_off) print -r -- "not enabled" ;;
        detail_ai_needs_setup) print -r -- "finish setup in Dashboard Settings" ;;
        detail_local) print -r -- "local" ;;
        detail_cloud) print -r -- "cloud" ;;
        detail_none) print -r -- "none" ;;
        details_log) print -r -- "Setup log" ;;
        readiness_python) print -r -- "Python" ;;
        readiness_dashboard) print -r -- "Dashboard" ;;
        readiness_components) print -r -- "Required software" ;;
        readiness_memory_model) print -r -- "Memory model" ;;
        readiness_memory_service) print -r -- "Memory and search" ;;
        readiness_ready) print -r -- "ready" ;;
        readiness_will_prepare) print -r -- "will be prepared during setup" ;;
        readiness_missing_python) print -r -- "Python 3.11 or newer was not found" ;;
        readiness_python_unverified) print -r -- "could not confirm Python 3.11 or newer" ;;
        readiness_dashboard_missing) print -r -- "required Dashboard files are missing" ;;
        readiness_cloud_ready) print -r -- "cloud settings are ready" ;;
        readiness_cloud_later) print -r -- "finish cloud settings before the first sync" ;;
        check_source_failed) print -r -- "required Actanara files are missing" ;;
        check_python_failed) print -r -- "Python 3.11 or newer is unavailable" ;;
        check_folder_failed) print -r -- "a selected folder is not writable" ;;
        check_dashboard_failed) print -r -- "the selected Dashboard port is unavailable" ;;
        check_failed) print -r -- "Setup cannot continue until the item above is resolved." ;;
        welcome)
          print -r -- "Welcome to Actanara. Dashboard, diaries, daily runs, and Nova-Task are included by default."
          print -r -- "Continue only if you understand that enabled features process local data and may use a small amount of your AI service allowance."
          ;;
        welcome_cancelled) print -r -- "Install cancelled from welcome screen" ;;
        language_prompt) print -r -- "Choose the Actanara language" ;;
        invalid_env) print -r -- "Invalid environment variable name. Press Return to try again." ;;
        ai_key_next_step) print -r -- "Save the key in Dashboard Settings, or run: actanara model key --value-stdin" ;;
        core_dependency_title) print -r -- "Ready to install" ;;
        core_dependency_action) print -r -- "checking your system" ;;
        rag_dependency_title) print -r -- "Memory and search" ;;
        rag_dependency_action) print -r -- "checking your memory and search choices" ;;
        press_return) print -r -- "Press Return to continue." ;;
        detecting_tools) print -r -- "Detecting tools... just a minute" ;;
        detected_tools) print -r -- "Detected tools" ;;
        tools_help)
          print -r -- "Selected tools will be covered by Actanara; unselected tools will not be collected."
          print -r -- "Use Up/Down or j/k to move, Space to toggle, and Return to continue."
          ;;
        no_tools) print -r -- "No known tool paths were detected. Choose manual to add one." ;;
        manual_tool_name) print -r -- "Manual tool name" ;;
        manual_tool_path) print -r -- "Manual tool path" ;;
        manual_add) print -r -- "Add manually" ;;
        manual_add_help) print -r -- "Enter a tool name and folder" ;;
        rag_choice_prompt) print -r -- "Choose how memory and search will work" ;;
        rag_not_now) print -r -- "Not now" ;;
        rag_local) print -r -- "On this Mac" ;;
        rag_cloud) print -r -- "Cloud service" ;;
        rag_local_model_prompt) print -r -- "Choose the local memory model" ;;
        llm_provider_prompt) print -r -- "Choose an AI provider" ;;
        llm_provider_help) print -r -- "A small, fast model works well for daily use; larger models may improve writing quality." ;;
        llm_model_prompt) print -r -- "Choose an AI model" ;;
        custom_input) print -r -- "custom input" ;;
        custom_llm_endpoint) print -r -- "Custom AI service URL" ;;
        custom_llm_model) print -r -- "Custom AI model name" ;;
        llm_model_id) print -r -- "AI model name" ;;
        llm_api_key_value_prompt) print -r -- "Paste the AI API key (stored securely on this Mac; leave blank to configure later)" ;;
        llm_api_key_env_prompt) print -r -- "AI API key environment variable (for example LLM_API_KEY; do not paste the key itself)" ;;
        cloud_provider) print -r -- "Cloud memory provider" ;;
        cloud_endpoint) print -r -- "Cloud memory service URL" ;;
        cloud_model) print -r -- "Cloud memory model" ;;
        cloud_dimension) print -r -- "Cloud memory model dimension" ;;
        cloud_key_env) print -r -- "Cloud memory API key environment variable (the key itself is not stored here)" ;;
        useful_commands) print -r -- "Useful commands:" ;;
        install_summary) print -r -- "Install summary" ;;
        proceed_upgrade)
          print -r -- "Proceed with upgrade now?"
          print -r -- "Actanara will keep your settings and data."
          ;;
        proceed_install)
          print -r -- "Proceed with install now?"
          print -r -- "Actanara will create its folder and set up your selected features."
          ;;
        upgrade_cancelled) print -r -- "Upgrade cancelled before making changes" ;;
        install_cancelled) print -r -- "Install cancelled before making changes" ;;
        *) print -r -- "$key" ;;
      esac
      ;;
    *)
      case "$key" in
        nav) print -r -- "使用方向键或 j/k 选择，然后按 Return。" ;;
        yes_recommended) print -r -- "是（推荐）" ;;
        no) print -r -- "否" ;;
        setup_title) print -r -- "安装助手" ;;
        phase_checking) print -r -- "检查系统环境" ;;
        phase_preparing) print -r -- "准备 Actanara" ;;
        phase_installing) print -r -- "安装 Actanara" ;;
        phase_configuring) print -r -- "配置所选功能" ;;
        phase_verifying) print -r -- "完成最后检查" ;;
        step_prepare_folder) print -r -- "准备 Actanara 文件夹" ;;
        step_prepare_output_folders) print -r -- "准备日记与报告文件夹" ;;
        step_prepare_python_files) print -r -- "准备 Python 文件" ;;
        step_prepare_python) print -r -- "准备 Python" ;;
        step_create_python) print -r -- "创建 Python 环境" ;;
        step_prepare_components) print -r -- "准备所需软件" ;;
        step_install_components) print -r -- "安装所需软件" ;;
        step_save_components) print -r -- "保存已安装软件信息" ;;
        step_verify_components) print -r -- "确认已安装软件" ;;
        step_check_components) print -r -- "检查所需软件" ;;
        step_finish_components) print -r -- "完成软件配置" ;;
        step_continue_setup) print -r -- "继续安装" ;;
        step_save_choices) print -r -- "保存你的选择" ;;
        step_save_ai_key) print -r -- "安全保存 AI 密钥" ;;
        step_diary_shortcut) print -r -- "创建桌面日记快捷方式" ;;
        step_install_command) print -r -- "安装 actanara 命令" ;;
        step_terminal_command) print -r -- "让新终端可以使用 actanara" ;;
        step_prepare_files) print -r -- "准备 Actanara 文件" ;;
        step_activate_files) print -r -- "启用 Actanara 文件" ;;
        step_finish_install) print -r -- "完成安装" ;;
        step_check_install) print -r -- "检查安装结果" ;;
        step_check_files) print -r -- "检查 Actanara 文件" ;;
        step_check_diary) print -r -- "检查日记创建" ;;
        step_start_dashboard) print -r -- "启动 Dashboard" ;;
        step_start_memory) print -r -- "启动记忆与搜索" ;;
        step_connect_tools) print -r -- "连接所选工具" ;;
        step_check_update) print -r -- "检查更新结果" ;;
        step_prepare_memory) print -r -- "准备记忆与搜索" ;;
        step_set_up_actanara) print -r -- "配置 Actanara" ;;
        step_set_up_daily) print -r -- "配置每日自动运行" ;;
        step_prepare_daily) print -r -- "准备每日自动运行" ;;
        step_enable_daily) print -r -- "启用每日自动运行" ;;
        step_check_daily) print -r -- "检查每日自动运行" ;;
        step_check_memory) print -r -- "检查记忆与搜索" ;;
        step_failed) print -r -- "这个步骤未能完成，请查看安装日志：" ;;
        update_needs_full_install) print -r -- "本次更新还需要更新组件，请改用完整更新。" ;;
        warning_diary_shortcut) print -r -- "桌面日记快捷方式未更改，Actanara 仍可正常使用。" ;;
        warning_terminal_command) print -r -- "未能自动让新终端识别 actanara 命令。" ;;
        warning_dashboard_port) print -r -- "Dashboard 将使用当前可用的本地地址。" ;;
        warning_python) print -r -- "无法安全准备 Python，安装不能继续。" ;;
        warning_tool_connection) print -r -- "一个所选工具未能连接，可稍后在 Dashboard 设置中重试。" ;;
        warning_dashboard_service_disabled) print -r -- "已按你的选择关闭 Dashboard 后台服务。" ;;
        warning_optional_step) print -r -- "一个可选步骤未完成，Actanara 核心功能仍可使用。" ;;
        warning_generic) print -r -- "安装需要你留意，请查看安装日志了解详情。" ;;
        error_terminal_command) print -r -- "未能安装 actanara 命令。" ;;
        error_options) print -r -- "部分安装选项不能同时使用，请通过 --help 查看用法。" ;;
        error_existing_install) print -r -- "此文件夹中已安装 Actanara，请运行：actanara update --apply" ;;
        error_source_files) print -r -- "缺少 Actanara 所需文件，或文件无法读取。" ;;
        error_update_missing) print -r -- "所选文件夹中未找到可更新的 Actanara。" ;;
        error_software) print -r -- "未能准备或确认 Actanara 所需软件。" ;;
        error_python) print -r -- "未能安全准备 Python。" ;;
        error_memory) print -r -- "记忆与搜索设置需要先处理，安装才能继续。" ;;
        error_dashboard) print -r -- "所选 Dashboard 地址不可用。" ;;
        error_language) print -r -- "安装语言请选择 zh-CN 或 en-US。" ;;
        error_terminal) print -r -- "交互式安装需要终端；自动化运行请使用 --no-wizard。" ;;
        error_ai_key) print -r -- "更新期间不会更改 AI 密钥；请在更新完成后保存密钥。" ;;
        error_ai_key_setting) print -r -- "AI 密钥设置无效。" ;;
        error_dashboard_required) print -r -- "Dashboard 是 Actanara 的内置功能；如不需要后台服务，请使用 --no-dashboard-server。" ;;
        error_recovery) print -r -- "更新未能完整结束；原安装已保留，请查看安装日志。" ;;
        error_repair_incomplete) print -r -- "Actanara 环境已重建，但配置未完成。原有数据仍安全，请重新运行 one-liner。" ;;
        error_setup) print -r -- "安装未能安全完成，请查看安装日志了解详情。" ;;
        next_steps) print -r -- "接下来" ;;
        plan_summary) print -r -- "安装计划" ;;
        install_complete) print -r -- "Actanara 已准备就绪。" ;;
        dry_run_complete) print -r -- "Actanara 安装计划已生成。" ;;
        upgrade_complete) print -r -- "Actanara 已更新完成。" ;;
        upgrade_plan_complete) print -r -- "Actanara 更新计划已生成。" ;;
        repair_complete) print -r -- "Actanara 已完成重建，原有设置和数据均已保留。" ;;
        repair_plan_complete) print -r -- "Actanara 重建计划已生成。" ;;
        repair_backup) print -r -- "恢复备份" ;;
        repair_incomplete) print -r -- "Actanara 环境已重建，但配置未完成。原有数据仍安全，请重新运行 one-liner。" ;;
        update_no_changes) print -r -- "Actanara 已是最新状态。" ;;
        source_update_complete) print -r -- "Actanara 文件已更新。" ;;
        source_update_plan_complete) print -r -- "Actanara 文件更新计划已生成。" ;;
        update_plan_summary) print -r -- "更新计划" ;;
        update_summary) print -r -- "更新摘要" ;;
        label_command) print -r -- "命令行" ;;
        label_folder) print -r -- "Actanara 文件夹" ;;
        label_diary) print -r -- "日记" ;;
        label_dashboard) print -r -- "Dashboard" ;;
        label_daily) print -r -- "每日自动运行" ;;
        label_ai) print -r -- "AI 生成" ;;
        label_memory) print -r -- "记忆与搜索" ;;
        label_tools) print -r -- "已连接工具" ;;
        detail_planned) print -r -- "将会准备" ;;
        detail_ready) print -r -- "已就绪" ;;
        detail_disabled) print -r -- "未启用" ;;
        detail_dashboard_app) print -r -- "已安装，后台服务未启用" ;;
        detail_daily_on) print -r -- "已启用" ;;
        detail_daily_off) print -r -- "未启用" ;;
        detail_ai_needs_setup) print -r -- "请在 Dashboard 设置中完成配置" ;;
        detail_local) print -r -- "本地" ;;
        detail_cloud) print -r -- "云端" ;;
        detail_none) print -r -- "无" ;;
        details_log) print -r -- "安装日志" ;;
        readiness_python) print -r -- "Python" ;;
        readiness_dashboard) print -r -- "Dashboard" ;;
        readiness_components) print -r -- "所需软件" ;;
        readiness_memory_model) print -r -- "记忆模型" ;;
        readiness_memory_service) print -r -- "记忆与搜索" ;;
        readiness_ready) print -r -- "已就绪" ;;
        readiness_will_prepare) print -r -- "将在安装时准备" ;;
        readiness_missing_python) print -r -- "未找到 Python 3.11 或更高版本" ;;
        readiness_python_unverified) print -r -- "无法确认 Python 是否满足要求" ;;
        readiness_dashboard_missing) print -r -- "缺少 Dashboard 所需文件" ;;
        readiness_cloud_ready) print -r -- "云端设置已就绪" ;;
        readiness_cloud_later) print -r -- "请在首次同步前补全云端设置" ;;
        check_source_failed) print -r -- "缺少 Actanara 所需文件" ;;
        check_python_failed) print -r -- "无法使用 Python 3.11 或更高版本" ;;
        check_folder_failed) print -r -- "一个所选文件夹不可写" ;;
        check_dashboard_failed) print -r -- "所选 Dashboard 端口不可用" ;;
        check_failed) print -r -- "请解决上方问题后重新运行安装。" ;;
        welcome)
          print -r -- "欢迎使用 Actanara。Dashboard、日记、每日自动运行和 Nova-Task 默认安装。"
          print -r -- "请确认你理解：启用的功能会处理本地数据，并可能产生少量 AI 模型用量。"
          ;;
        welcome_cancelled) print -r -- "已在欢迎页取消安装" ;;
        language_prompt) print -r -- "选择 Actanara 界面语言" ;;
        invalid_env) print -r -- "环境变量名无效。按 Return 后重试。" ;;
        ai_key_next_step) print -r -- "请在 Dashboard 设置中保存密钥，或运行：actanara model key --value-stdin" ;;
        core_dependency_title) print -r -- "安装准备" ;;
        core_dependency_action) print -r -- "正在检查系统环境" ;;
        rag_dependency_title) print -r -- "记忆与搜索" ;;
        rag_dependency_action) print -r -- "正在检查记忆与搜索设置" ;;
        press_return) print -r -- "按 Return 继续。" ;;
        detecting_tools) print -r -- "正在检测工具……请稍候" ;;
        detected_tools) print -r -- "已检测到的工具" ;;
        tools_help)
          print -r -- "选中的工具会纳入 Actanara 覆盖范围；未选中的工具不会被采集。"
          print -r -- "使用方向键或 j/k 移动，空格切换，按 Return 继续。"
          ;;
        no_tools) print -r -- "未检测到已知工具，可选择“手动添加”。" ;;
        manual_tool_name) print -r -- "手动工具名称" ;;
        manual_tool_path) print -r -- "手动工具路径" ;;
        manual_add) print -r -- "手动添加" ;;
        manual_add_help) print -r -- "输入工具名称和文件夹" ;;
        rag_choice_prompt) print -r -- "选择记忆与搜索方式" ;;
        rag_not_now) print -r -- "暂不启用" ;;
        rag_local) print -r -- "本地处理" ;;
        rag_cloud) print -r -- "云端处理" ;;
        rag_local_model_prompt) print -r -- "选择本地记忆模型" ;;
        llm_provider_prompt) print -r -- "选择 AI 服务商" ;;
        llm_provider_help) print -r -- "日常使用选择小型快速模型即可；更大的模型可能提升文字质量。" ;;
        llm_model_prompt) print -r -- "选择 AI 模型" ;;
        custom_input) print -r -- "自定义输入" ;;
        custom_llm_endpoint) print -r -- "自定义 AI 服务地址" ;;
        custom_llm_model) print -r -- "自定义 AI 模型名称" ;;
        llm_model_id) print -r -- "AI 模型名称" ;;
        llm_api_key_value_prompt) print -r -- "粘贴 AI 密钥（会安全保存在本机；留空则稍后配置）" ;;
        llm_api_key_env_prompt) print -r -- "AI 密钥环境变量名（例如 LLM_API_KEY；不要在这里粘贴密钥）" ;;
        cloud_provider) print -r -- "云端记忆服务商" ;;
        cloud_endpoint) print -r -- "云端记忆服务地址" ;;
        cloud_model) print -r -- "云端记忆模型" ;;
        cloud_dimension) print -r -- "云端记忆模型维度" ;;
        cloud_key_env) print -r -- "云端记忆密钥环境变量名（不会存储密钥值）" ;;
        useful_commands) print -r -- "常用命令：" ;;
        install_summary) print -r -- "安装摘要" ;;
        proceed_upgrade)
          print -r -- "现在继续升级吗？"
          print -r -- "Actanara 会保留你的设置和数据。"
          ;;
        proceed_install)
          print -r -- "现在继续安装吗？"
          print -r -- "Actanara 会创建自己的文件夹，并配置你选择的功能。"
          ;;
        upgrade_cancelled) print -r -- "升级已取消，尚未修改文件" ;;
        install_cancelled) print -r -- "安装已取消，尚未修改文件" ;;
        *) print -r -- "$key" ;;
      esac
      ;;
  esac
}

clear_tty_menu() {
  print -n -- "$TTY_CLEAR" > /dev/tty
  render_installer_header
}

print_tty_copy() {
  local copy="$1"
  local width="${COLUMNS:-80}"
  local rendered=""
  if [[ "$width" != <40-999> ]]; then
    width=80
  fi
  if rendered="$(ACTANARA_INSTALL_COPY="$copy" ACTANARA_INSTALL_COPY_WIDTH="$width" "$PYTHON_BIN" -c '
import os
import re
import unicodedata

text = os.environ.get("ACTANARA_INSTALL_COPY", "")
limit = max(32, int(os.environ.get("ACTANARA_INSTALL_COPY_WIDTH", "80")) - 2)
ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

def width(value):
    clean = ansi.sub("", value)
    return sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 0 if unicodedata.combining(ch) else 1 for ch in clean)

for semantic_line in text.splitlines() or [""]:
    remaining = semantic_line
    while width(remaining) > limit:
        used = 0
        cut = 0
        last_space = -1
        for index, char in enumerate(remaining):
            used += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 0 if unicodedata.combining(char) else 1
            if char.isspace():
                last_space = index
            if used > limit:
                cut = last_space if last_space > 0 else index
                break
        print(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    print(remaining)
' 2>/dev/null)"; then
    print -r -- "$rendered" > /dev/tty
  else
    print -r -- "$copy" > /dev/tty
  fi
}

prompt_line() {
  local prompt="$1"
  local default_value="$2"
  local answer=""
  print -n -- "${prompt} [${default_value}]: " > /dev/tty
  IFS= read -r answer < /dev/tty || answer=""
  if [[ -n "$answer" ]]; then
    print -r -- "$answer"
  else
    print -r -- "$default_value"
  fi
}

prompt_line_page() {
  clear_tty_menu
  prompt_line "$@"
}

valid_env_var_name() {
  local value="$1"
  [[ "$value" =~ '^[A-Za-z_][A-Za-z0-9_]*$' ]]
}

safe_env_var_label() {
  local value="$1"
  if valid_env_var_name "$value"; then
    print -r -- "$value"
  else
    print -r -- "configured secret reference"
  fi
}

llm_api_key_env_error() {
  error "LLM API key environment variable name is invalid"
  print -r -- "  $(installer_text ai_key_next_step)" >&2
}

validate_llm_api_key_env() {
  if valid_env_var_name "$LLM_API_KEY_ENV"; then
    return 0
  fi
  llm_api_key_env_error
  return 2
}

prompt_llm_api_key_env() {
  local answer=""
  while true; do
    answer="$(prompt_line_page "$(installer_text llm_api_key_env_prompt)" "$LLM_API_KEY_ENV")"
    if valid_env_var_name "$answer"; then
      LLM_API_KEY_ENV="$answer"
      return 0
    fi
    print -r -- "$(installer_text invalid_env)" > /dev/tty
    IFS= read -r _ < /dev/tty || true
  done
}

prompt_secret_line_page() {
  local prompt="$1"
  local answer=""
  clear_tty_menu
  print -n -- "${prompt}: " > /dev/tty
  IFS= read -rs answer < /dev/tty || answer=""
  print -r -- "" > /dev/tty
  print -r -- "$answer"
}

prompt_llm_api_key_value() {
  local answer=""
  answer="$(prompt_secret_line_page "$(installer_text llm_api_key_value_prompt)")"
  if [[ -n "$answer" ]]; then
    LLM_API_KEY_VALUE="$answer"
  fi
}

prompt_yes_no() {
  local prompt="$1"
  local default_value="$2"
  local selected=1
  local key=""
  local escape=""
  local options=("$(installer_text yes_recommended)" "$(installer_text no)")
  if [[ "$default_value" != "yes" ]]; then
    selected=2
  fi
  while true; do
    clear_tty_menu
    print_tty_copy "$prompt"
    print -r -- "$(installer_text nav)" > /dev/tty
    for idx in {1..2}; do
      if [[ "$idx" == "$selected" ]]; then
        print -r -- "${TTY_GREEN}  > ${options[$idx]}${TTY_RESET}" > /dev/tty
      else
        print -r -- "    ${options[$idx]}" > /dev/tty
      fi
    done
    IFS= read -rs -k 1 key < /dev/tty || key=""
    if [[ "$key" == $'\e' ]]; then
      IFS= read -rs -k 2 escape < /dev/tty || escape=""
      case "$escape" in
        "[A") selected=$(( selected == 1 ? 2 : selected - 1 )) ;;
        "[B") selected=$(( selected == 2 ? 1 : selected + 1 )) ;;
      esac
    elif [[ "$key" == "k" ]]; then
      selected=$(( selected == 1 ? 2 : selected - 1 ))
    elif [[ "$key" == "j" ]]; then
      selected=$(( selected == 2 ? 1 : selected + 1 ))
    elif [[ "$key" == $'\n' || "$key" == $'\r' || -z "$key" ]]; then
      [[ "$selected" == "1" ]]
      return $?
    fi
  done
}

prompt_choice_impl() {
  local prompt="$1"
  local help_text="$2"
  local default_value="$3"
  shift 3
  local options=("$@")
  local selected=1
  local idx=1
  local key=""
  local escape=""
  for (( idx = 1; idx <= ${#options[@]}; idx++ )); do
    if [[ "${options[$idx]}" == "$default_value" ]]; then
      selected="$idx"
    fi
  done
  while true; do
    clear_tty_menu
    print_tty_copy "$prompt"
    if [[ -n "$help_text" ]]; then
      print_tty_copy "$help_text"
    fi
    print -r -- "$(installer_text nav)" > /dev/tty
    for (( idx = 1; idx <= ${#options[@]}; idx++ )); do
      if [[ "$idx" == "$selected" ]]; then
        print -r -- "${TTY_GREEN}  > ${options[$idx]}${TTY_RESET}" > /dev/tty
      else
        print -r -- "    ${options[$idx]}" > /dev/tty
      fi
    done
    IFS= read -rs -k 1 key < /dev/tty || key=""
    if [[ "$key" == $'\e' ]]; then
      IFS= read -rs -k 2 escape < /dev/tty || escape=""
      case "$escape" in
        "[A") selected=$(( selected == 1 ? ${#options[@]} : selected - 1 )) ;;
        "[B") selected=$(( selected == ${#options[@]} ? 1 : selected + 1 )) ;;
      esac
    elif [[ "$key" == "k" ]]; then
      selected=$(( selected == 1 ? ${#options[@]} : selected - 1 ))
    elif [[ "$key" == "j" ]]; then
      selected=$(( selected == ${#options[@]} ? 1 : selected + 1 ))
    elif [[ "$key" == $'\n' || "$key" == $'\r' || -z "$key" ]]; then
      print -r -- "${options[$selected]}"
      return 0
    fi
  done
}

prompt_choice() {
  local prompt="$1"
  local default_value="$2"
  shift 2
  prompt_choice_impl "$prompt" "" "$default_value" "$@"
}

prompt_choice_with_help() {
  local prompt="$1"
  local help_text="$2"
  local default_value="$3"
  shift 3
  prompt_choice_impl "$prompt" "$help_text" "$default_value" "$@"
}

rag_local_model_dimension() {
  case "$1" in
    "BAAI/bge-large-zh-v1.5") print -r -- "1024" ;;
    "intfloat/multilingual-e5-small") print -r -- "384" ;;
    "BAAI/bge-large-en-v1.5") print -r -- "1024" ;;
    "all-MiniLM-L6-v2") print -r -- "384" ;;
    *) print -r -- "" ;;
  esac
}

apply_language_profile() {
  case "$INSTALL_LANGUAGE" in
    zh|zh-CN|zh_CN)
      INSTALL_LANGUAGE="zh-CN"
      PIPELINE_LANGUAGE_PROFILE="zh"
      PIPELINE_ENGLISH_ENABLED=0
      PIPELINE_DIARY_SCHEMA_VERSION="diary-v1-zh"
      PIPELINE_PROMPT_PAYLOAD_PROFILE="zh-CN"
      RAG_LANGUAGE_PROFILE="zh"
      if [[ "$RAG_LOCAL_MODEL_SET" != "1" ]]; then
        RAG_LOCAL_MODEL="intfloat/multilingual-e5-small"
        RAG_LOCAL_DIMENSION="384"
      fi
      ;;
    en|en-US|en_US)
      INSTALL_LANGUAGE="en-US"
      PIPELINE_LANGUAGE_PROFILE="en"
      PIPELINE_ENGLISH_ENABLED=1
      PIPELINE_DIARY_SCHEMA_VERSION="diary-v1-en"
      PIPELINE_PROMPT_PAYLOAD_PROFILE="en-US"
      RAG_LANGUAGE_PROFILE="en"
      if [[ "$RAG_LOCAL_MODEL_SET" != "1" ]]; then
        RAG_LOCAL_MODEL="all-MiniLM-L6-v2"
        RAG_LOCAL_DIMENSION="384"
      fi
      ;;
    *)
      error "language must be zh-CN or en-US"
      exit 2
      ;;
  esac
}

prompt_language_profile() {
  local selected_label=""
  local default_label="Chinese (zh-CN)"
  if [[ "$INSTALL_LANGUAGE" == "en" || "$INSTALL_LANGUAGE" == "en-US" || "$INSTALL_LANGUAGE" == "en_US" ]]; then
    default_label="English (en-US)"
  fi
  selected_label="$(prompt_choice "$(installer_text language_prompt)" "$default_label" "Chinese (zh-CN)" "English (en-US)")"
  if [[ "$selected_label" == "English (en-US)" ]]; then
    INSTALL_LANGUAGE="en-US"
  else
    INSTALL_LANGUAGE="zh-CN"
  fi
  LANGUAGE_SET=1
  LANGUAGE_SELECTED=1
  apply_language_profile
}

prompt_rag_local_model() {
  local labels=(
    "中文/多语 384 · intfloat/multilingual-e5-small"
    "中文 1024 · BAAI/bge-large-zh-v1.5"
    "English 384 · all-MiniLM-L6-v2"
    "English 1024 · BAAI/bge-large-en-v1.5"
  )
  local models=(
    "intfloat/multilingual-e5-small"
    "BAAI/bge-large-zh-v1.5"
    "all-MiniLM-L6-v2"
    "BAAI/bge-large-en-v1.5"
  )
  local default_label="${labels[1]}"
  local selected_label=""
  local idx=1
  for (( idx = 1; idx <= ${#models[@]}; idx++ )); do
    if [[ "${models[$idx]}" == "$RAG_LOCAL_MODEL" ]]; then
      default_label="${labels[$idx]}"
      break
    fi
  done
  selected_label="$(prompt_choice "$(installer_text rag_local_model_prompt)" "$default_label" "${labels[@]}")"
  for (( idx = 1; idx <= ${#labels[@]}; idx++ )); do
    if [[ "${labels[$idx]}" == "$selected_label" ]]; then
      RAG_LOCAL_MODEL="${models[$idx]}"
      RAG_LOCAL_DIMENSION="$(rag_local_model_dimension "$RAG_LOCAL_MODEL")"
      break
    fi
  done
}

llm_provider_catalog_rows() {
  resolve_python_bin || return 0
  PYTHONPATH="${SOURCE_ROOT}:${SOURCE_ROOT}/src" "${PYTHON_BIN}" - <<'PY'
from data_foundation.llm_provider_catalog import llm_provider_catalog

for provider in llm_provider_catalog():
    if provider.get("enabled") or provider.get("id") == "custom":
        print("\t".join([
            str(provider.get("id") or ""),
            str(provider.get("name") or provider.get("id") or ""),
            str(provider.get("api") or "openai-compatible"),
            str(provider.get("endpoint") or ""),
        ]))
PY
  true
}

llm_model_catalog_rows() {
  local provider_id="$1"
  resolve_python_bin || return 0
  ACTANARA_SELECTED_LLM_PROVIDER="$provider_id" PYTHONPATH="${SOURCE_ROOT}:${SOURCE_ROOT}/src" "${PYTHON_BIN}" - <<'PY'
import os
from data_foundation.llm_provider_catalog import find_provider

provider = find_provider(os.environ.get("ACTANARA_SELECTED_LLM_PROVIDER"), require_enabled=True)
if not provider:
    raise SystemExit(0)
for model in provider.get("models") or []:
    print("\t".join([
        str(model.get("id") or ""),
        str(model.get("name") or model.get("id") or ""),
    ]))
PY
  true
}

prompt_llm_provider_from_catalog() {
  local provider_rows=("${(@f)$(llm_provider_catalog_rows)}")
  local provider_ids=()
  local provider_names=()
  local provider_apis=()
  local provider_endpoints=()
  local provider_labels=()
  local fields=()
  local row=""
  local idx=1
  local selected_label=""
  local selected_idx=1
  local provider_id=""
  local model_rows=()
  local model_ids=()
  local model_names=()
  local model_labels=()

  if [[ "${#provider_rows[@]}" -eq 0 ]]; then
    LLM_PROVIDER_MODE="custom"
    LLM_PROVIDER="custom"
    LLM_API="openai-compatible"
    LLM_ENDPOINT="$(prompt_line_page "$(installer_text custom_llm_endpoint)" "${LLM_ENDPOINT:-https://api.openai.com/v1}")"
    LLM_MODEL="$(prompt_line_page "$(installer_text custom_llm_model)" "$LLM_MODEL")"
    prompt_llm_api_key_value
    return 0
  fi

  for row in "${provider_rows[@]}"; do
    fields=("${(@ps:\t:)row}")
    provider_ids+=("${fields[1]}")
    provider_names+=("${fields[2]}")
    provider_apis+=("${fields[3]}")
    provider_endpoints+=("${fields[4]}")
    provider_labels+=("${fields[2]} (${fields[1]})")
  done
  for (( idx = 1; idx <= ${#provider_ids[@]}; idx++ )); do
    if [[ "${provider_ids[$idx]}" == "openai" ]]; then
      selected_idx="$idx"
      break
    fi
  done

  selected_label="$(prompt_choice_with_help "$(installer_text llm_provider_prompt)" "$(installer_text llm_provider_help)" "${provider_labels[$selected_idx]}" "${provider_labels[@]}")"
  for (( idx = 1; idx <= ${#provider_labels[@]}; idx++ )); do
    if [[ "${provider_labels[$idx]}" == "$selected_label" ]]; then
      selected_idx="$idx"
      break
    fi
  done

  provider_id="${provider_ids[$selected_idx]}"
  if [[ "$provider_id" == "custom" ]]; then
    LLM_PROVIDER_MODE="custom"
    LLM_PROVIDER="custom"
    LLM_API="openai-compatible"
    LLM_ENDPOINT="$(prompt_line_page "$(installer_text custom_llm_endpoint)" "${LLM_ENDPOINT:-https://api.openai.com/v1}")"
    LLM_MODEL="$(prompt_line_page "$(installer_text custom_llm_model)" "$LLM_MODEL")"
    prompt_llm_api_key_value
    return 0
  fi

  LLM_PROVIDER_MODE="preset"
  LLM_PROVIDER="$provider_id"
  LLM_API="${provider_apis[$selected_idx]}"
  LLM_ENDPOINT="${provider_endpoints[$selected_idx]}"
  model_rows=("${(@f)$(llm_model_catalog_rows "$provider_id")}")
  for row in "${model_rows[@]}"; do
    fields=("${(@ps:\t:)row}")
    model_ids+=("${fields[1]}")
    model_names+=("${fields[2]}")
    model_labels+=("${fields[2]} (${fields[1]})")
  done
  if [[ "${#model_ids[@]}" -gt 0 ]]; then
    model_labels+=("$(installer_text custom_input)")
    selected_label="$(prompt_choice "$(installer_text llm_model_prompt)" "${model_labels[1]}" "${model_labels[@]}")"
    if [[ "$selected_label" == "$(installer_text custom_input)" ]]; then
      LLM_MODEL="$(prompt_line_page "$(installer_text custom_llm_model)" "$LLM_MODEL")"
    else
      for (( idx = 1; idx <= ${#model_ids[@]}; idx++ )); do
        if [[ "${model_labels[$idx]}" == "$selected_label" ]]; then
          LLM_MODEL="${model_ids[$idx]}"
          break
        fi
      done
    fi
  else
    LLM_MODEL="$(prompt_line_page "$(installer_text llm_model_id)" "$LLM_MODEL")"
  fi
  prompt_llm_api_key_value
}

detect_external_tool_rows() {
  resolve_python_bin || return 0
  if ! ACTANARA_INSTALL_USER_HOME="$HOME" \
    PYTHONPATH="${SOURCE_ROOT}:${SOURCE_ROOT}/src" \
    "${PYTHON_BIN}" - <<'PY'
import os
from pathlib import Path

from data_foundation.external_tool_catalog import detect_external_tools


def safe_field(value: object) -> str:
    return str(value or "").replace("|", "%7C").replace("\r", " ").replace("\n", " ")


user_home = Path(os.environ["ACTANARA_INSTALL_USER_HOME"])
detection = detect_external_tools(user_home=user_home)
presence = detection.get("toolPresence")
presence = presence if isinstance(presence, dict) else {}
for tool_id in detection.get("detectedToolKeys") or []:
    detail = presence.get(tool_id)
    if not isinstance(detail, dict) or detail.get("detected") is not True:
        continue
    configured_home = str(detail.get("configuredHome") or "")
    evidence = detail.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    evidence_path = next(
        (
            str(item.get("path") or "")
            for item in evidence
            if isinstance(item, dict) and str(item.get("path") or "")
        ),
        "",
    )
    display_path = configured_home if configured_home and Path(configured_home).exists() else evidence_path
    print(
        "|".join(
            (
                safe_field(tool_id),
                safe_field(detail.get("name") or tool_id),
                safe_field(detail.get("emoji")),
                safe_field(display_path or configured_home),
            )
        )
    )
PY
  then
    log "shared external tool detection failed; continuing without preselected tools"
    return 0
  fi
}

prompt_external_tools() {
  clear_tty_menu
  print -r -- "$(installer_text detecting_tools)" > /dev/tty
  sleep 0.2

  local rows=("${(@f)$(detect_external_tool_rows)}")
  local keys=()
  local labels=()
  local emojis=()
  local paths=()
  local counts=()
  local selected=()
  local row=""
  local fields=()
  local idx=1
  local cursor=1
  local key=""
  local escape=""
  local manual_key=""
  local manual_name=""
  local manual_path=""
  local chosen=()

  for row in "${rows[@]}"; do
    if [[ -z "$row" ]]; then
      continue
    fi
    fields=("${(@ps:|:)row}")
    if [[ "${#fields[@]}" -lt 4 ]]; then
      continue
    fi
    keys+=("${fields[1]}")
    labels+=("${fields[2]}")
    emojis+=("${fields[3]}")
    paths+=("${fields[4]}")
    selected+=(1)
  done
  keys+=("manual")
  labels+=("$(installer_text manual_add)")
  emojis+=("✍️")
  paths+=("$(installer_text manual_add_help)")
  selected+=(0)

  while true; do
    clear_tty_menu
    print -r -- "$(installer_text detected_tools)" > /dev/tty
    print -r -- "$(installer_text tools_help)" > /dev/tty
    if [[ "${#rows[@]}" -eq 0 ]]; then
      print -r -- "$(installer_text no_tools)" > /dev/tty
    fi
    counts=()
    for (( idx = 1; idx <= ${#keys[@]}; idx++ )); do
      local marker="[ ]"
      local display_label="${labels[$idx]}"
      [[ "${selected[$idx]}" == "1" ]] && marker="[✓]"
      counts[$idx]=1
      local same_seen=0
      local j=1
      for (( j = 1; j <= idx; j++ )); do
        if [[ "${keys[$j]}" == "${keys[$idx]}" ]]; then
          same_seen=$(( same_seen + 1 ))
        fi
      done
      local same_total=0
      for (( j = 1; j <= ${#keys[@]}; j++ )); do
        if [[ "${keys[$j]}" == "${keys[$idx]}" ]]; then
          same_total=$(( same_total + 1 ))
        fi
      done
      if [[ "$same_total" -gt 1 && "${keys[$idx]}" != "manual" ]]; then
        display_label="${display_label} ${same_seen}"
      fi
      if [[ "$idx" == "$cursor" ]]; then
        print -r -- "${TTY_GREEN}  > ${marker} ${emojis[$idx]} ${display_label} - ${paths[$idx]}${TTY_RESET}" > /dev/tty
      else
        print -r -- "    ${marker} ${emojis[$idx]} ${display_label} - ${paths[$idx]}" > /dev/tty
      fi
    done
    IFS= read -rs -k 1 key < /dev/tty || key=""
    if [[ "$key" == $'\e' ]]; then
      IFS= read -rs -k 2 escape < /dev/tty || escape=""
      case "$escape" in
        "[A") cursor=$(( cursor == 1 ? ${#keys[@]} : cursor - 1 )) ;;
        "[B") cursor=$(( cursor == ${#keys[@]} ? 1 : cursor + 1 )) ;;
      esac
    elif [[ "$key" == "k" ]]; then
      cursor=$(( cursor == 1 ? ${#keys[@]} : cursor - 1 ))
    elif [[ "$key" == "j" ]]; then
      cursor=$(( cursor == ${#keys[@]} ? 1 : cursor + 1 ))
    elif [[ "$key" == " " ]]; then
      selected[$cursor]=$(( selected[$cursor] == 1 ? 0 : 1 ))
    elif [[ "$key" == $'\n' || "$key" == $'\r' || -z "$key" ]]; then
      chosen=()
      for (( idx = 1; idx <= ${#keys[@]}; idx++ )); do
        if [[ "${selected[$idx]}" == "1" ]]; then
          if [[ "${keys[$idx]}" == "manual" ]]; then
            manual_name="$(prompt_line_page "$(installer_text manual_tool_name)" "")"
            manual_path="$(prompt_line_page "$(installer_text manual_tool_path)" "")"
            if [[ -n "$manual_name" && -n "$manual_path" ]]; then
              manual_key="manual-${manual_name:l}"
              manual_key="${manual_key// /-}"
              chosen+=("${manual_key}|${manual_name}|${manual_path:A}")
            fi
          else
            chosen+=("${keys[$idx]}|${labels[$idx]}|${paths[$idx]}")
          fi
        fi
      done
      SELECTED_EXTERNAL_TOOLS="${(j:;;:)chosen}"
      return 0
    fi
  done
}

prompt_rag_choice() {
  if [[ "$RAG_SET" == "1" ]]; then
    return 0
  fi

  local selected_label=""
  local rag_not_now_label="$(installer_text rag_not_now)"
  local rag_local_label="$(installer_text rag_local)"
  local rag_cloud_label="$(installer_text rag_cloud)"
  selected_label="$(prompt_choice "$(installer_text rag_choice_prompt)" "$rag_not_now_label" "$rag_not_now_label" "$rag_local_label" "$rag_cloud_label")"
  case "$selected_label" in
    "$rag_local_label")
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="local"
      RAG_EMBEDDING_MODE_SET=1
      ;;
    "$rag_cloud_label")
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="cloud"
      RAG_EMBEDDING_MODE_SET=1
      DEPLOY_EMBEDDING_SERVER=0
      ;;
    *)
      ENABLE_RAG=0
      ;;
  esac
  RAG_SET=1
}

wizard_dependency_line() {
  local dep_status="$1"
  local label="$2"
  local detail="$3"
  local marker="~"
  local color="$TTY_YELLOW"
  case "$dep_status" in
    ok)
      marker="✓"
      color="$TTY_BLUE"
      ;;
    error)
      marker="x"
      color="$TTY_RED"
      ;;
    *)
      marker="~"
      color="$TTY_YELLOW"
      ;;
  esac
  print -r -- "  ${color}${marker}${TTY_RESET} ${label}: ${detail}" > /dev/tty
}

wizard_dependency_page() {
  local title="$1"
  local action="$2"
  shift 2
  local row=""
  local fields=()
  local frame=""
  local continue_key=""

  for frame in "/" "-" "\\" "|"; do
    clear_tty_menu
    print -r -- "$title" > /dev/tty
    print -r -- "${TTY_YELLOW}${frame}${TTY_RESET} ${action}" > /dev/tty
    sleep 0.05
  done

  clear_tty_menu
  print -r -- "$title" > /dev/tty
  for row in "$@"; do
    fields=("${(@ps:|:)row}")
    if [[ "${#fields[@]}" -lt 3 ]]; then
      continue
    fi
    wizard_dependency_line "${fields[1]}" "${fields[2]}" "${fields[3]}"
  done
  print -r -- "" > /dev/tty
  print -r -- "$(installer_text press_return)" > /dev/tty
  IFS= read -r continue_key < /dev/tty || true
}

wizard_core_dependency_gate() {
  local rows=()
  local python_probe=""
  local python_status=0
  local static_missing=0
  local asset=""
  local static_assets=(
    "src/dashboard/app/static/index.html"
    "src/dashboard/app/static/css/style.css"
    "src/dashboard/app/static/js/app.js"
  )

  if ensure_python_bin; then
    set +e
    python_probe="$(python_version_probe "$PYTHON_BIN")"
    python_status=$?
    set -e
    if [[ "$python_status" == "0" && -n "$python_probe" ]]; then
      rows+=("ok|$(installer_text readiness_python)|Python ${python_probe} · $(installer_text readiness_ready)")
    else
      rows+=("error|$(installer_text readiness_python)|$(installer_text readiness_python_unverified)")
    fi
    if "$PYTHON_BIN" -c "import venv" >/dev/null 2>&1; then
      true
    else
      rows+=("error|$(installer_text readiness_components)|$(installer_text readiness_python_unverified)")
    fi
  elif [[ "$DRY_RUN" == "1" && "$PYTHON_INSTALL_PLANNED" == "1" ]]; then
    rows+=("pending|$(installer_text readiness_python)|Python ${PYTHON_STANDALONE_VERSION} · $(installer_text readiness_will_prepare)")
  else
    rows+=("error|$(installer_text readiness_python)|$(installer_text readiness_missing_python)")
  fi

  for asset in "${static_assets[@]}"; do
    if [[ ! -f "${SOURCE_ROOT}/${asset}" ]]; then
      static_missing=$(( static_missing + 1 ))
    fi
  done
  if [[ "$static_missing" == "0" ]]; then
    rows+=("ok|$(installer_text readiness_dashboard)|$(installer_text readiness_ready)")
  else
    rows+=("error|$(installer_text readiness_dashboard)|$(installer_text readiness_dashboard_missing)")
  fi

  rows+=("pending|$(installer_text readiness_components)|$(installer_text readiness_will_prepare)")
  wizard_dependency_page "$(installer_text core_dependency_title)" "$(installer_text core_dependency_action)" "${rows[@]}"
}

wizard_rag_dependency_gate() {
  if [[ "$ENABLE_RAG" != "1" ]]; then
    return 0
  fi

  local rows=()
  if [[ "$RAG_EMBEDDING_MODE" == "local" ]]; then
    rows+=("ok|$(installer_text readiness_memory_model)|${RAG_LOCAL_MODEL}")
    rows+=("pending|$(installer_text readiness_memory_service)|$(installer_text readiness_will_prepare)")
  elif [[ "$RAG_EMBEDDING_MODE" == "cloud" ]]; then
    rows+=("ok|$(installer_text readiness_memory_service)|$(installer_text detail_cloud)")
    if [[ -n "$RAG_CLOUD_ENDPOINT" && -n "$RAG_CLOUD_MODEL" && -n "$RAG_CLOUD_DIMENSION" ]]; then
      rows+=("ok|$(installer_text readiness_memory_model)|$(installer_text readiness_cloud_ready)")
    else
      rows+=("pending|$(installer_text readiness_memory_model)|$(installer_text readiness_cloud_later)")
    fi
  fi
  wizard_dependency_page "$(installer_text rag_dependency_title)" "$(installer_text rag_dependency_action)" "${rows[@]}"
}

wizard_enabled() {
  case "$WIZARD_MODE" in
    1|yes|true|on)
      return 0
      ;;
    0|no|false|off)
      return 1
      ;;
    auto)
      [[ -t 1 && -r /dev/tty ]]
      return $?
      ;;
    *)
      error "Invalid ACTANARA_INSTALL_WIZARD value: ${WIZARD_MODE}"
      exit 2
      ;;
  esac
}

apply_installer_settings_overlay() {
  local log_file=""
  local display_label="$(installer_text step_save_choices)"
  log "Applying installer settings overlay"
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$display_label"
    progress_ok "$display_label"
    return 0
  fi
  log_file="$(installer_log_file)"
  mkdir -p "${log_file:h}"
  progress_start "$display_label"
  if ! {
  ACTANARA_INSTALL_SOURCE_ROOT="${SOURCE_ROOT}" \
  ACTANARA_INSTALL_DEPLOY_SOURCE_ROOT="${DEPLOY_SOURCE_ROOT}" \
  ACTANARA_INSTALL_RUNTIME="${RUNTIME_HOME}" \
  ACTANARA_INSTALL_UPGRADE="${UPGRADE}" \
  ACTANARA_INSTALL_DIARY_OUTPUT="${DIARY_OUTPUT_DIR}" \
  ACTANARA_INSTALL_DIARY_OUTPUT_SET="${DIARY_OUTPUT_SET}" \
  ACTANARA_INSTALL_REPORTS_OUTPUT="${REPORTS_OUTPUT_DIR}" \
  ACTANARA_INSTALL_REPORTS_OUTPUT_SET="${REPORTS_OUTPUT_SET}" \
  ACTANARA_INSTALL_SNAPSHOTS_OUTPUT="${SNAPSHOTS_OUTPUT_DIR}" \
  ACTANARA_INSTALL_SNAPSHOTS_OUTPUT_SET="${SNAPSHOTS_OUTPUT_SET}" \
  ACTANARA_INSTALL_ARCHIVES_OUTPUT="${ARCHIVES_OUTPUT_DIR}" \
  ACTANARA_INSTALL_ARCHIVES_OUTPUT_SET="${ARCHIVES_OUTPUT_SET}" \
  ACTANARA_INSTALL_SELECTED_EXTERNAL_TOOLS="${SELECTED_EXTERNAL_TOOLS}" \
  ACTANARA_INSTALL_ENABLE_SKILL_REGISTRATION="${ENABLE_SKILL_REGISTRATION}" \
  ACTANARA_INSTALL_ENABLE_DASHBOARD="${ENABLE_DASHBOARD}" \
  ACTANARA_INSTALL_ENABLE_DASHBOARD_SERVER="$([[ "$NO_DASHBOARD_SERVER" == "1" ]] && print 0 || print 1)" \
  ACTANARA_INSTALL_DASHBOARD_SERVER_SET="${NO_DASHBOARD_SERVER_SET}" \
  ACTANARA_INSTALL_DASHBOARD_HOST="${DASHBOARD_HOST}" \
  ACTANARA_INSTALL_DASHBOARD_HOST_SET="${DASHBOARD_HOST_SET}" \
  ACTANARA_INSTALL_DASHBOARD_PORT="${DASHBOARD_PORT}" \
  ACTANARA_INSTALL_DASHBOARD_PORT_SET="${DASHBOARD_PORT_SET}" \
  ACTANARA_INSTALL_ENABLE_NOVA_TASK="${ENABLE_NOVA_TASK}" \
  ACTANARA_INSTALL_ENABLE_LLM_GENERATION="${ENABLE_LLM_GENERATION}" \
  ACTANARA_INSTALL_LLM_SET="${LLM_SET}" \
  ACTANARA_INSTALL_LLM_PROVIDER_MODE="${LLM_PROVIDER_MODE}" \
  ACTANARA_INSTALL_LLM_PROVIDER="${LLM_PROVIDER}" \
  ACTANARA_INSTALL_LLM_API="${LLM_API}" \
  ACTANARA_INSTALL_LLM_ENDPOINT="${LLM_ENDPOINT}" \
  ACTANARA_INSTALL_LLM_MODEL="${LLM_MODEL}" \
  ACTANARA_INSTALL_LLM_API_KEY_ENV="${LLM_API_KEY_ENV}" \
  ACTANARA_INSTALL_LANGUAGE="${INSTALL_LANGUAGE}" \
  ACTANARA_INSTALL_LANGUAGE_SET="${LANGUAGE_SET}" \
  ACTANARA_INSTALL_PIPELINE_LANGUAGE_PROFILE="${PIPELINE_LANGUAGE_PROFILE}" \
  ACTANARA_INSTALL_PIPELINE_ENGLISH_ENABLED="${PIPELINE_ENGLISH_ENABLED}" \
  ACTANARA_INSTALL_PIPELINE_DIARY_SCHEMA_VERSION="${PIPELINE_DIARY_SCHEMA_VERSION}" \
  ACTANARA_INSTALL_PIPELINE_PROMPT_PAYLOAD_PROFILE="${PIPELINE_PROMPT_PAYLOAD_PROFILE}" \
  ACTANARA_INSTALL_RAG_LANGUAGE_PROFILE="${RAG_LANGUAGE_PROFILE}" \
  ACTANARA_INSTALL_ENABLE_RAG="${ENABLE_RAG}" \
  ACTANARA_INSTALL_RAG_SET="${RAG_SET}" \
  ACTANARA_INSTALL_RAG_EMBEDDING_MODE="${RAG_EMBEDDING_MODE}" \
  ACTANARA_INSTALL_RAG_EMBEDDING_MODE_SET="${RAG_EMBEDDING_MODE_SET}" \
  ACTANARA_INSTALL_RAG_LOCAL_MODEL="${RAG_LOCAL_MODEL}" \
  ACTANARA_INSTALL_RAG_LOCAL_MODEL_SET="${RAG_LOCAL_MODEL_SET}" \
  ACTANARA_INSTALL_RAG_LOCAL_DIMENSION="${RAG_LOCAL_DIMENSION}" \
  ACTANARA_INSTALL_RAG_CLOUD_PROVIDER="${RAG_CLOUD_PROVIDER}" \
  ACTANARA_INSTALL_RAG_CLOUD_ENDPOINT="${RAG_CLOUD_ENDPOINT}" \
  ACTANARA_INSTALL_RAG_CLOUD_MODEL="${RAG_CLOUD_MODEL}" \
  ACTANARA_INSTALL_RAG_CLOUD_DIMENSION="${RAG_CLOUD_DIMENSION}" \
  ACTANARA_INSTALL_RAG_CLOUD_API_KEY_ENV="${RAG_CLOUD_API_KEY_ENV}" \
  PYTHONPATH="${SOURCE_ROOT}:${SOURCE_ROOT}/src" \
  "${VENV_PY}" - <<'PY'
import os
from pathlib import Path

from data_foundation.paths import runtime_paths_for_home
from data_foundation.settings import write_settings

runtime = Path(os.environ["ACTANARA_INSTALL_RUNTIME"]).expanduser()
deploy_source_root = Path(os.environ["ACTANARA_INSTALL_DEPLOY_SOURCE_ROOT"]).expanduser()
paths = runtime_paths_for_home(runtime)

is_upgrade = os.environ["ACTANARA_INSTALL_UPGRADE"] == "1"
enable_rag = os.environ["ACTANARA_INSTALL_ENABLE_RAG"] == "1"
enable_llm = os.environ["ACTANARA_INSTALL_ENABLE_LLM_GENERATION"] == "1"
enable_skill_registration = os.environ["ACTANARA_INSTALL_ENABLE_SKILL_REGISTRATION"] == "1"
embedding_mode = os.environ["ACTANARA_INSTALL_RAG_EMBEDDING_MODE"]
llm_provider_mode = os.environ["ACTANARA_INSTALL_LLM_PROVIDER_MODE"]
selected_external_tools_raw = os.environ.get("ACTANARA_INSTALL_SELECTED_EXTERNAL_TOOLS", "")


def flag(name: str) -> bool:
    return os.environ.get(name) == "1"


def first_install_or(flag_name: str) -> bool:
    return (not is_upgrade) or flag(flag_name)


rag_embedding = {
    "mode": embedding_mode,
    "provider": embedding_mode,
    "providerId": "local" if embedding_mode == "local" else os.environ["ACTANARA_INSTALL_RAG_CLOUD_PROVIDER"],
    "model": os.environ["ACTANARA_INSTALL_RAG_CLOUD_MODEL"] if embedding_mode == "cloud" else os.environ["ACTANARA_INSTALL_RAG_LOCAL_MODEL"],
    "device": "auto",
}
if embedding_mode == "cloud":
    dimension = os.environ["ACTANARA_INSTALL_RAG_CLOUD_DIMENSION"].strip()
    if dimension:
        rag_embedding["dimension"] = int(dimension)
    rag_embedding["endpoint"] = os.environ["ACTANARA_INSTALL_RAG_CLOUD_ENDPOINT"]
    rag_embedding["apiKeyEnv"] = os.environ["ACTANARA_INSTALL_RAG_CLOUD_API_KEY_ENV"]
else:
    dimension = os.environ["ACTANARA_INSTALL_RAG_LOCAL_DIMENSION"].strip()
    if dimension:
        rag_embedding["dimension"] = int(dimension)

update = {
    "general": {
        "locale": os.environ["ACTANARA_INSTALL_LANGUAGE"],
        "workspaceRoot": str(deploy_source_root),
        "tmpWorkspace": str(runtime / "state" / "tmp"),
    },
    "paths": {
        "install": {
            "workspace": str(deploy_source_root),
            "dashboardApp": str(deploy_source_root / "src" / "dashboard"),
        },
        "runtime": {
            "actanaraHome": str(runtime),
            "database": str(runtime / "data" / "actanara_data.sqlite3"),
        },
        "diary": {},
        "intermediate": {},
        "tasks": {
            "taskBoard": str(runtime / "artifacts" / "tasks" / "TASK_BOARD.md"),
            "legacyTaskDatabase": str(runtime / "data" / "nova_tasks.db"),
        },
        "logsCacheTmp": {
            "logs": str(runtime / "state" / "logs"),
            "cache": str(runtime / "state" / "cache"),
            "tmp": str(runtime / "state" / "tmp"),
            "backups": str(runtime / "state" / "backups"),
        },
    },
    "features": {},
    "dashboard": {
        "projectRoot": str(deploy_source_root),
        "pythonExecutable": str(runtime / ".venv" / "bin" / "python"),
        "appDir": str(deploy_source_root / "src" / "dashboard"),
    },
    "pipeline": {
        "pythonExecutable": str(runtime / ".venv" / "bin" / "python"),
        "workingDirectory": str(deploy_source_root),
    },
}

if first_install_or("ACTANARA_INSTALL_LANGUAGE_SET"):
    update["general"]["locale"] = os.environ["ACTANARA_INSTALL_LANGUAGE"]
    update["pipeline"].update(
        {
            "languageProfile": os.environ["ACTANARA_INSTALL_PIPELINE_LANGUAGE_PROFILE"],
            "englishEnabled": os.environ["ACTANARA_INSTALL_PIPELINE_ENGLISH_ENABLED"] == "1",
            "diarySchemaVersion": os.environ["ACTANARA_INSTALL_PIPELINE_DIARY_SCHEMA_VERSION"],
            "promptPayloadProfile": os.environ["ACTANARA_INSTALL_PIPELINE_PROMPT_PAYLOAD_PROFILE"],
        }
    )
    update.setdefault("rag", {})["languageProfile"] = os.environ["ACTANARA_INSTALL_RAG_LANGUAGE_PROFILE"]

if first_install_or("ACTANARA_INSTALL_SNAPSHOTS_OUTPUT_SET"):
    update["paths"]["runtime"]["snapshots"] = os.environ["ACTANARA_INSTALL_SNAPSHOTS_OUTPUT"]
if first_install_or("ACTANARA_INSTALL_DIARY_OUTPUT_SET"):
    update["paths"]["diary"]["generatedDiary"] = os.environ["ACTANARA_INSTALL_DIARY_OUTPUT"]
    update["paths"]["diary"]["legacyDiaryRoot"] = os.environ["ACTANARA_INSTALL_DIARY_OUTPUT"]
if first_install_or("ACTANARA_INSTALL_REPORTS_OUTPUT_SET"):
    update["paths"]["diary"]["reports"] = os.environ["ACTANARA_INSTALL_REPORTS_OUTPUT"]
if first_install_or("ACTANARA_INSTALL_ARCHIVES_OUTPUT_SET"):
    update["paths"]["intermediate"]["archives"] = os.environ["ACTANARA_INSTALL_ARCHIVES_OUTPUT"]

if first_install_or("ACTANARA_INSTALL_DASHBOARD_HOST_SET"):
    update["dashboard"]["host"] = os.environ["ACTANARA_INSTALL_DASHBOARD_HOST"]
if first_install_or("ACTANARA_INSTALL_DASHBOARD_PORT_SET"):
    update["dashboard"]["port"] = int(os.environ["ACTANARA_INSTALL_DASHBOARD_PORT"])
if first_install_or("ACTANARA_INSTALL_DASHBOARD_SERVER_SET"):
    update["dashboard"]["server"] = {
        "enabled": os.environ["ACTANARA_INSTALL_ENABLE_DASHBOARD_SERVER"] == "1",
    }

if not is_upgrade:
    update["features"].update(
        {
            "dashboard": os.environ["ACTANARA_INSTALL_ENABLE_DASHBOARD"] == "1",
            "novaTask": os.environ["ACTANARA_INSTALL_ENABLE_NOVA_TASK"] == "1",
            "taskAuditSink": os.environ["ACTANARA_INSTALL_ENABLE_NOVA_TASK"] == "1",
        }
    )
if first_install_or("ACTANARA_INSTALL_RAG_SET"):
    update["features"].update(
        {
            "rag": enable_rag,
            "embeddingServer": enable_rag,
        }
    )
    update.setdefault("rag", {}).update(
        {
            "enabled": enable_rag,
            "mode": "v2" if enable_rag else "disabled",
            "embedding": rag_embedding,
            "server": {
                "enabled": enable_rag,
            },
        }
    )
if first_install_or("ACTANARA_INSTALL_LLM_SET"):
    update["features"]["llmGeneration"] = enable_llm

for group_name in ("features",):
    if not update[group_name]:
        update.pop(group_name, None)
for parent, child in (("paths", "diary"), ("paths", "intermediate")):
    if not update[parent][child]:
        update[parent].pop(child, None)

selected_external_tools = []
external_tools_update = {}
for raw_item in [item for item in selected_external_tools_raw.split(";;") if item.strip()]:
    try:
        key, name, path_value = raw_item.split("|", 2)
    except ValueError:
        continue
    item = {"key": key, "name": name, "path": path_value}
    selected_external_tools.append(item)
    if key == "openclaw":
        root = Path(path_value)
        external_tools_update["openclaw"] = {
            "home": str(root),
            "agentsRoot": str(root / "agents"),
            "configPath": str(root / "config.json"),
            "credentialsPath": str(root / "credentials.json"),
            "workspaceRoot": str(root / "workspace"),
            "workspaceCoderRoot": str(root / "workspace-coder"),
            "projectsRoot": str(root / "workspace" / "PROJECTS"),
            "skillsRoot": str(root / "workspace" / "skills"),
            "systemSkillsRoot": str(root / "skills"),
            "memoryRoot": str(root / "memory"),
            "cronJobsPath": str(root / "cron" / "jobs.json"),
            "cronJobsMigratedPath": str(root / "cron" / "jobs.json.migrated"),
            "cronRunsRoot": str(root / "cron" / "runs"),
            "infrastructurePath": str(root / "workspace" / "infrastructure.md"),
            "toolConfigSnapshotPath": str(root / "workspace" / ".dashboard-tool-configs.json"),
        }
    elif key == "claudeCode":
        root = Path(path_value)
        external_tools_update["claudeCode"] = {
            "home": str(root),
            "projectsRoot": str(root / "projects"),
            "skillsRoot": str(root / "skills"),
            "commandsRoot": str(root / "commands"),
            "pluginsRoot": str(root / "plugins"),
            "configPath": str(root / "settings.json"),
        }
    elif key == "codex":
        root = Path(path_value)
        external_tools_update["codex"] = {
            "home": str(root),
            "sessionsRoot": str(root / "sessions"),
            "skillsRoot": str(root / "skills"),
            "configPath": str(root / "config.toml"),
        }
    elif key == "geminiCli":
        root = Path(path_value)
        external_tools_update["geminiCli"] = {
            "home": str(root),
            "chatsRoot": str(root / "tmp" / "ssd" / "chats"),
            "projectsPath": str(root / "projects.json"),
            "skillsRoot": str(root / "skills"),
            "configPath": str(root / "settings.json"),
        }
    elif key == "hermes":
        root = Path(path_value)
        external_tools_update["hermes"] = {
            "home": str(root),
            "stateDbPath": str(root / "state.db"),
            "skillsRoot": str(root / "hermes-agent" / "skills"),
            "optionalSkillsRoot": str(root / "hermes-agent" / "optional-skills"),
            "pluginsRoot": str(root / "hermes-agent" / "plugins"),
            "profilesRoot": str(root / "profiles"),
            "configPath": str(root / "config.yaml"),
        }

if selected_external_tools:
    external_tools_update["installerSelectedTools"] = selected_external_tools

if enable_skill_registration and selected_external_tools:
    external_tools_update["installerV2SkillRegistration"] = {
        "status": "installer-selected",
        "supportedNow": True,
        "purpose": "Actanara跨Agent记忆检索",
        "scope": "Installer apply and Dashboard-managed registration of one Actanara Memory Search skill into detected, selected, supported tools' global layer",
        "backendPolicy": "runtime-auto: nova-RAG when healthy, otherwise local lexical recall",
        "selectedTools": selected_external_tools,
        "dryRunEndpoint": "GET /api/settings/external-tools/memory-skill-registration/plan",
        "applyEndpoint": "POST /api/settings/external-tools/memory-skill-registration",
        "confirmationTextRequired": "INSTALL ACTANARA MEMORY SKILL",
        "acceptedConfirmationTexts": [
            "INSTALL ACTANARA MEMORY SKILL",
            "INSTALL ACTANARA RAG SKILL",
        ],
        "mutationPolicy": "installer writes missing skill files after final install confirmation; exact unmodified generated versions are backed up and upgraded; customized files are preserved unless Dashboard overwrite is explicitly confirmed",
    }

if external_tools_update:
    update["externalTools"] = external_tools_update

if first_install_or("ACTANARA_INSTALL_LLM_SET") and enable_llm:
    if llm_provider_mode == "preset":
        update["llmProvider"] = {
            "mode": "preset",
            "provider": os.environ["ACTANARA_INSTALL_LLM_PROVIDER"],
            "presetProvider": os.environ["ACTANARA_INSTALL_LLM_PROVIDER"],
            "endpoint": os.environ["ACTANARA_INSTALL_LLM_ENDPOINT"],
            "model": os.environ["ACTANARA_INSTALL_LLM_MODEL"],
            "api": os.environ["ACTANARA_INSTALL_LLM_API"],
            "apiKey": "",
            "apiKeyEnv": os.environ["ACTANARA_INSTALL_LLM_API_KEY_ENV"],
        }
    else:
        update["llmProvider"] = {
            "mode": "custom",
            "provider": "custom",
            "presetProvider": "",
            "endpoint": os.environ["ACTANARA_INSTALL_LLM_ENDPOINT"],
            "model": os.environ["ACTANARA_INSTALL_LLM_MODEL"],
            "api": os.environ["ACTANARA_INSTALL_LLM_API"],
            "apiKey": "",
            "apiKeyEnv": os.environ["ACTANARA_INSTALL_LLM_API_KEY_ENV"],
        }

write_settings(update, paths)
PY
  } >> "$log_file" 2>&1; then
    progress_fail "$(installer_text step_failed) ${log_file}"
    return 1
  fi
  progress_ok "$display_label"
}

migrate_legacy_settings_for_repair() {
  if [[ "$REPAIR_EXISTING" != "1" || "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if ! PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" \
    "${DEPENDENCY_CONTRACT_HELPER}" migrate-legacy-settings \
    --runtime "${RUNTIME_HOME}" \
    --scheduler-enabled "$(( 1 - NO_SCHEDULER ))" \
    --dashboard-enabled "${ENABLE_DASHBOARD}" \
    --dashboard-server-enabled "$(( 1 - NO_DASHBOARD_SERVER ))" \
    --rag-server-enabled "${REPAIR_RAG_SERVICE_ENABLED}" >/dev/null; then
    error "legacy Actanara Settings could not be migrated safely"
    return 1
  fi
}

store_installer_llm_api_key_secret() {
  local technical_label="Storing LLM API key in secret store"
  local label="$(installer_text step_save_ai_key)"
  local log_file=""
  if [[ "$ENABLE_LLM_GENERATION" != "1" || -z "$LLM_API_KEY_VALUE" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$label"
    progress_ok "$label"
    return 0
  fi
  log_file="$(installer_log_file)"
  mkdir -p "${log_file:h}"
  progress_start "$label"
  print -r -- "" >> "$log_file"
  print -r -- "## ${technical_label}: actanara model key --value-stdin --runtime ${RUNTIME_HOME}" >> "$log_file"
  if ! print -r -- "$LLM_API_KEY_VALUE" | "${VENV_PY}" -m data_foundation.cli \
    model key \
    --value-stdin \
    --runtime "${RUNTIME_HOME}" \
    --json >> "$log_file" 2>&1; then
    progress_fail "$(installer_text step_failed) ${log_file}"
    return 1
  fi
  LLM_API_KEY_VALUE=""
  progress_ok "$label"
}

create_desktop_diary_link() {
  if [[ "$CREATE_DESKTOP_DIARY_LINK" != "1" ]]; then
    log "Desktop diary shortcut skipped"
    return 0
  fi
  log "Creating Desktop diary shortcut"
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$(installer_text step_diary_shortcut)"
    progress_ok "$(installer_text step_diary_shortcut)"
    return 0
  fi
  if ! mkdir -p "${DESKTOP_DIARY_LINK:h}"; then
    warn "Desktop diary shortcut parent could not be created; continuing without Desktop shortcut: ${DESKTOP_DIARY_LINK:h}"
    return 0
  fi
  if [[ -L "$DESKTOP_DIARY_LINK" ]]; then
    local current_target=""
    current_target="$(readlink "$DESKTOP_DIARY_LINK" || true)"
    if [[ "$current_target" == "$DIARY_OUTPUT_DIR" ]]; then
      return 0
    fi
    warn "Desktop diary shortcut already exists and points elsewhere: ${DESKTOP_DIARY_LINK}"
    return 0
  fi
  if [[ -e "$DESKTOP_DIARY_LINK" ]]; then
    warn "Desktop diary shortcut path already exists; leaving it unchanged: ${DESKTOP_DIARY_LINK}"
    return 0
  fi
  if ! ln -s "$DIARY_OUTPUT_DIR" "$DESKTOP_DIARY_LINK"; then
    warn "Desktop diary shortcut could not be created; continuing without Desktop shortcut: ${DESKTOP_DIARY_LINK}"
    return 0
  fi
}

create_cli_shim() {
  log "Creating actanara CLI shim"
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$(installer_text step_install_command)"
    progress_ok "$(installer_text step_install_command)"
    return 0
  fi
  mkdir -p "${CLI_SHIM:h}"
  local shim_tmp="${CLI_SHIM}.tmp.$$"
  if ! cat > "${shim_tmp}" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
export ACTANARA_HOME="${RUNTIME_HOME}"
export ACTANARA_LOCATION_FILE="${LOCATION_FILE}"
export PYTHONPATH="${DEPLOY_SOURCE_ROOT}:${DEPLOY_SOURCE_ROOT}/src"
export PYTHONDONTWRITEBYTECODE="1"
unset WORKSPACE_DIR DIARY_OUTPUT_DIR TMP_WORKSPACE ACTANARA_DATA_DB_PATH ACTANARA_DATA_EXPORT_DIR TASK_DB_PATH
exec "${VENV_PY}" -m data_foundation.cli "\$@"
EOF
  then
    rm -f "${shim_tmp}"
    error "Actanara CLI shim could not be staged: ${CLI_SHIM}"
    return 1
  fi
  if ! chmod +x "${shim_tmp}" || ! mv -f "${shim_tmp}" "${CLI_SHIM}"; then
    rm -f "${shim_tmp}"
    error "Actanara CLI shim could not be installed atomically: ${CLI_SHIM}"
    return 1
  fi
  if mkdir -p "${USER_CLI_SHIM:h}" 2>/dev/null; then
    if ! ln -sf "${CLI_SHIM}" "${USER_CLI_SHIM}" 2>/dev/null; then
      warn "User PATH shim could not be linked; runtime CLI remains available at ${CLI_SHIM}"
    fi
  else
    warn "User PATH shim directory could not be created; runtime CLI remains available at ${CLI_SHIM}"
  fi
}

resolve_shell_path_file() {
  if [[ -n "$SHELL_PATH_FILE" ]]; then
    print -r -- "${SHELL_PATH_FILE:A}"
    return 0
  fi
  local shell_name="${SHELL:t}"
  if [[ "$PLATFORM" == "Darwin" || "$shell_name" == "zsh" ]]; then
    print -r -- "${HOME}/.zprofile"
  elif [[ "$shell_name" == "bash" ]]; then
    print -r -- "${HOME}/.bash_profile"
  else
    print -r -- "${HOME}/.profile"
  fi
}

ensure_cli_on_shell_path() {
  if [[ "$ENABLE_SHELL_PATH" != "1" ]]; then
    log "Shell PATH update skipped by --no-shell-path"
    return 0
  fi
  local shim_dir="${USER_CLI_SHIM:h}"
  local profile_path
  local path_expr
  local marker_start="# >>> actanara installer PATH >>>"
  local marker_end="# <<< actanara installer PATH <<<"
  profile_path="$(resolve_shell_path_file)"
  if [[ "$shim_dir" == "${HOME}/.local/bin" ]]; then
    path_expr="\$HOME/.local/bin"
  else
    path_expr="${shim_dir}"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$(installer_text step_terminal_command)"
    progress_ok "$(installer_text step_terminal_command)"
    return 0
  fi
  if [[ -f "$profile_path" ]] && grep -Fq "$marker_start" "$profile_path"; then
    return 0
  fi
  if [[ ":${PATH}:" == *":${shim_dir}:"* ]]; then
    return 0
  fi
  if ! mkdir -p "${profile_path:h}" 2>/dev/null; then
    warn "Shell PATH profile directory could not be created; add ${shim_dir} to PATH manually."
    return 0
  fi
  if ! {
    print -r -- ""
    print -r -- "$marker_start"
    print -r -- "# Added by Actanara installer so the actanara CLI resolves in new shells."
    print -r -- "export PATH=\"${path_expr}:\$PATH\""
    print -r -- "$marker_end"
  } >> "$profile_path" 2>/dev/null; then
    warn "Shell PATH profile could not be updated; add ${shim_dir} to PATH manually."
    return 0
  fi
  log "Added ${shim_dir} to shell PATH via ${profile_path}"
}

export_runtime_environment() {
export ACTANARA_HOME="${RUNTIME_HOME}"
export ACTANARA_LOCATION_FILE="${LOCATION_FILE}"
export PYTHONPATH="${DEPLOY_SOURCE_ROOT}:${DEPLOY_SOURCE_ROOT}/src:${DEPLOY_SOURCE_ROOT}/src/dashboard"
}

stage_runtime_source() {
  log "Staging source snapshot for runtime"
  STAGED_RELEASE_ID=""
  STAGED_RELEASE_TARGET=""
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$(installer_text step_prepare_files)"
    progress_ok "$(installer_text step_prepare_files)"
    return 0
  fi
  local app_root="${DEPLOY_SOURCE_ROOT:h}"
  local releases_root="${app_root}/releases"
  local release_id="${UPDATE_TRANSACTION_ID:-$(date +%Y%m%dT%H%M%S)-$$-${RANDOM}}"
  local release_tmp="${releases_root}/.tmp-${release_id}"
  local release_target="${releases_root}/${release_id}"
  local transaction_owned_stage=0
  local stage_command=("${PYTHON_BIN}" "-")
  if [[ "$UPDATE_TRANSACTION_ACTIVE" == "1" && -n "$UPDATE_TRANSACTION_JOURNAL" ]]; then
    release_tmp="$(update_transaction_command reserve-artifact \
      --state "${UPDATE_TRANSACTION_JOURNAL}" \
      --kind source-temp)"
    if [[ "$release_tmp" != "${releases_root}/.tmp-${release_id}" ]]; then
      error "transaction source reservation returned an unexpected path"
      return 1
    fi
    transaction_owned_stage=1
    stage_command=(
      "${PYTHON_BIN}" "${UPDATE_TRANSACTION_HELPER}"
      run-candidate-command
      --state "${UPDATE_TRANSACTION_JOURNAL}"
      --phase candidate-source-stage
      --
      /usr/bin/env
      -i
      "PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
      "USER=actanara-candidate"
      "LOGNAME=actanara-candidate"
      "SHELL=/bin/zsh"
      "LC_ALL=C"
      "LANG=C"
      "ACTANARA_HOME=${UPDATE_VALIDATION_RUNTIME}"
      "ACTANARA_LOCATION_FILE=${UPDATE_VALIDATION_RUNTIME}/location.json"
      "HOME=${UPDATE_VALIDATION_RUNTIME}/home"
      "TMPDIR=${UPDATE_VALIDATION_RUNTIME}/tmp"
      "XDG_CONFIG_HOME=${UPDATE_VALIDATION_RUNTIME}/xdg"
      "PIP_CONFIG_FILE=/dev/null"
      "PIP_CACHE_DIR=${UPDATE_VALIDATION_RUNTIME}/pip-cache"
      "PYTHONNOUSERSITE=1"
      "ACTANARA_SECRET_BACKEND=memory"
      "PYTHONDONTWRITEBYTECODE=1"
      "${PYTHON_BIN}" "-"
    )
  fi
  mkdir -p "${releases_root}"
  if [[ "$transaction_owned_stage" != "1" ]]; then
    rm -rf "${release_tmp}"
  fi
  if ! PYTHONDONTWRITEBYTECODE=1 "${stage_command[@]}" "${SOURCE_ROOT}" "${release_tmp}" "${DEPLOY_SOURCE_ROOT}" "${release_target}" "${transaction_owned_stage}" <<'PY'
import json
import hashlib
import os
import pwd
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

source = Path(sys.argv[1])
target = Path(sys.argv[2])
deploy_target = Path(sys.argv[3])
release_target = Path(sys.argv[4])
precreated = sys.argv[5] == "1"

allowed_top_level = {
    "advanced",
    "config.py",
    "install",
    "LICENSE",
    "MANIFEST.in",
    "pyproject.toml",
    "src",
}
excluded_names = {
    ".DS_Store",
    ".env",
    ".git",
    ".mypy_cache",
    ".playwright-cli",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "build",
    "cache",
    "data",
    "dist",
    "htmlcov",
    "location.json",
    "logs",
    "runtime.json",
    "reserved",
    "settings.json",
    "snapshots",
    "state",
    "tmp",
    "venv",
    "wheelhouse",
}
excluded_suffixes = (
    ".db",
    ".egg-info",
    ".log",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
)


def ignore(directory, names):
    ignored = set()
    for name in names:
        if name in excluded_names or name.startswith(".env."):
            ignored.add(name)
            continue
        if any(name.endswith(suffix) for suffix in excluded_suffixes):
            ignored.add(name)
    return ignored


def split_sql_statements(body, version):
    statements = []
    pending = []
    state = "normal"
    index = 0
    while index < len(body):
        character = body[index]
        following = body[index + 1] if index + 1 < len(body) else ""
        if state == "line-comment":
            if character in "\r\n":
                pending.append("\n")
                state = "normal"
            index += 1
            continue
        if state == "block-comment":
            if character == "*" and following == "/":
                pending.append(" ")
                state = "normal"
                index += 2
            else:
                index += 1
            continue
        if state == "normal":
            if character == "-" and following == "-":
                state = "line-comment"
                index += 2
                continue
            if character == "/" and following == "*":
                state = "block-comment"
                index += 2
                continue
            if character in {"'", '"', "`", "["}:
                state = {"'": "single", '"': "double", "`": "backtick", "[": "bracket"}[
                    character
                ]
                pending.append(character)
                index += 1
                continue
            if character == ";":
                text = "".join(pending).strip()
                if text:
                    statements.append(text)
                pending = []
                index += 1
                continue
            pending.append(character)
            index += 1
            continue
        closing = {"single": "'", "double": '"', "backtick": "`", "bracket": "]"}[state]
        pending.append(character)
        if character == closing:
            if following == closing and state != "bracket":
                pending.append(following)
                index += 2
                continue
            state = "normal"
        index += 1
    if state not in {"normal", "line-comment"}:
        raise SystemExit(f"candidate additive migration has unterminated SQL syntax: {version}")
    text = "".join(pending).strip()
    if text:
        statements.append(text)
    return statements


if precreated:
    marker = target / ".actanara-update-owner"
    if target.is_symlink() or not target.is_dir() or marker.is_symlink() or not marker.is_file():
        raise SystemExit("transaction source reservation is missing or unsafe")
    if any(path != marker for path in target.iterdir()):
        raise SystemExit("transaction source reservation was modified before staging")
else:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
for name in sorted(allowed_top_level):
    source_path = source / name
    target_path = target / name
    if not source_path.exists():
        continue
    if source_path.is_dir():
        shutil.copytree(source_path, target_path, ignore=ignore, symlinks=True)
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path, follow_symlinks=False)
if any(path.is_symlink() for path in target.rglob("*")):
    raise SystemExit("candidate runtime source payload must not contain symlinks")

def privacy_safe_source_locator(source_path):
    try:
        login_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        resolved_source = source_path.resolve()
        relative = resolved_source.relative_to(login_home)
    except (KeyError, OSError, RuntimeError, ValueError):
        return {"kind": "unavailable", "issue": "outside-login-home"}
    components = list(relative.parts)
    if not components or any(not item or item in {".", ".."} or "/" in item or "\\" in item for item in components):
        return {"kind": "unavailable", "issue": "invalid-relative-components"}
    return {"kind": "login-home-relative", "pathComponents": components}

manifest = {
    "schemaVersion": 2,
    "product": "actanara",
    "sourceLocator": privacy_safe_source_locator(source),
    "deployedSourceLocator": {"kind": "runtime-relative", "pathComponents": ["app", "source"]},
    "releaseLocator": {"kind": "runtime-relative", "pathComponents": ["app", "releases", release_target.name]},
    "deploymentMode": "release-symlink",
    "copiedAt": datetime.now().astimezone().isoformat(),
    "pyprojectVersion": None,
    "git": {
        "available": False,
        "commit": None,
        "branch": None,
        "remote": None,
        "dirty": None,
    },
}

contract_path = target / "src" / "data_foundation" / "migration_compatibility.json"
migrations_root = target / "src" / "data_foundation" / "migrations"
try:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit("candidate migration compatibility contract is missing or invalid") from exc
records = contract.get("migrations") if isinstance(contract.get("migrations"), list) else []
if (
    contract.get("schemaVersion") != 1
    or contract.get("policy") != "rollback-compatible-additive-only"
    or contract.get("preCommitWriterContract") != "prior-reader-compatible-v1"
    or contract.get("minimumReadableSchema") != "unversioned"
    or not records
):
    raise SystemExit("candidate migration compatibility contract has an unsupported policy")
normalized_records = []
seen_versions = set()
migration_set_digest = hashlib.sha256()
for record in records:
    if not isinstance(record, dict):
        raise SystemExit("candidate migration compatibility record is invalid")
    version = str(record.get("version") or "")
    expected_hash = str(record.get("sha256") or "")
    rollback_class = str(record.get("rollbackClass") or "")
    if (
        not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+", version)
        or version in seen_versions
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        or rollback_class not in {"rollback-compatible-additive", "breaking"}
    ):
        raise SystemExit("candidate migration compatibility record is unsafe")
    migration_path = migrations_root / f"{version}.sql"
    if not migration_path.is_file() or migration_path.is_symlink():
        raise SystemExit(f"candidate migration is missing from its compatibility contract: {version}")
    actual_hash = hashlib.sha256(migration_path.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise SystemExit(f"candidate migration body changed without a new compatibility version: {version}")
    if rollback_class == "rollback-compatible-additive":
        try:
            body = migration_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemExit(f"candidate additive migration is unreadable: {version}") from exc
        statements = split_sql_statements(body, version)
        if not statements:
            raise SystemExit(f"candidate additive migration is empty: {version}")
        for statement in statements:
            create_allowed = re.match(
                r"(?is)^\s*CREATE\s+(?:TABLE|(?:UNIQUE\s+)?INDEX|VIEW)\b",
                statement,
            )
            alter_allowed = re.match(
                r"(?is)^\s*ALTER\s+TABLE\s+(?:\S+)\s+ADD\s+(?:COLUMN\s+)?\S+",
                statement,
            )
            if not create_allowed and not alter_allowed:
                raise SystemExit(
                    f"candidate additive migration contains a prior-reader-unsafe statement: {version}"
                )
    seen_versions.add(version)
    normalized_records.append(
        {"version": version, "sha256": expected_hash, "rollbackClass": rollback_class}
    )
    migration_set_digest.update(version.encode("ascii"))
    migration_set_digest.update(b"\0")
    migration_set_digest.update(expected_hash.encode("ascii"))
    migration_set_digest.update(b"\0")
    migration_set_digest.update(rollback_class.encode("ascii"))
    migration_set_digest.update(b"\n")
migration_entries = list(migrations_root.glob("*.sql"))
if any(not path.is_file() or path.is_symlink() for path in migration_entries):
    raise SystemExit("candidate migration inventory contains an unsafe entry")
actual_versions = {path.stem for path in migration_entries}
if actual_versions != seen_versions or [item["version"] for item in normalized_records] != sorted(seen_versions):
    raise SystemExit("candidate migration set does not exactly match its compatibility contract")
if contract.get("maximumReadableSchema") != normalized_records[-1]["version"]:
    raise SystemExit("candidate migration readable-schema bound does not match its migration set")
manifest["databaseCompatibility"] = {
    "schemaVersion": 1,
    "policy": contract["policy"],
    "preCommitWriterContract": contract["preCommitWriterContract"],
    "minimumReadableSchema": contract["minimumReadableSchema"],
    "maximumReadableSchema": contract["maximumReadableSchema"],
    "migrationSetSha256": migration_set_digest.hexdigest(),
    "migrations": normalized_records,
}
try:
    pyproject = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    manifest["pyprojectVersion"] = (pyproject.get("project") or {}).get("version")
except Exception:
    pass

def git_value(*args):
    return subprocess.check_output(("git", "-C", str(source), *args), text=True, stderr=subprocess.DEVNULL).strip()

def git_optional(*args):
    try:
        value = git_value(*args)
    except Exception:
        return None
    return value or None

def redact_git_remote(value):
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except Exception:
        return None
    if parsed.scheme == "file" or (not parsed.scheme and value.startswith(("/", "~"))):
        return None
    if parsed.scheme in {"https", "ssh"} and parsed.netloc:
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    scp_remote = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(.+)", value)
    if scp_remote:
        host, remote_path = scp_remote.groups()
        return f"ssh://{host}/{remote_path.lstrip('/')}"
    return None

try:
    remote = git_optional("config", "--get", "remote.origin.url")
    if remote is None:
        remote_names = git_optional("remote")
        first_remote = (remote_names or "").splitlines()[0] if remote_names else ""
        if first_remote:
            remote = git_optional("remote", "get-url", first_remote)
    manifest["git"].update(
        {
            "available": True,
            "commit": git_value("rev-parse", "HEAD"),
            "branch": git_value("rev-parse", "--abbrev-ref", "HEAD"),
            "remote": redact_git_remote(remote),
            "dirty": bool(git_value("status", "--porcelain")),
        }
    )
except Exception:
    pass
(target / ".actanara-runtime-source.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)

# Scan the exact copied payload, not the checkout that was used as its source.
# Findings never include candidate values; only path/kind metadata may leave the
# scanner, and a blocked payload is discarded before any source pointer switch.
sys.path.insert(0, str(target / "src"))
from data_foundation.release_clean import repository_clean_deployment_check

clean_result = repository_clean_deployment_check(target)
if clean_result.get("status") != "passed":
    print("staged runtime source payload failed release-clean validation", file=sys.stderr)
    raise SystemExit(9)

payload_files = []
payload_digest = hashlib.sha256()
manifest_path = target / ".actanara-runtime-source.json"
for payload_path in sorted(target.rglob("*")):
    if (
        payload_path == manifest_path
        or payload_path.name == ".actanara-update-owner"
        or not (payload_path.is_file() or payload_path.is_symlink())
    ):
        continue
    relative = payload_path.relative_to(target).as_posix()
    if payload_path.is_symlink():
        raise SystemExit("candidate runtime source payload must not contain symlinks")
    content = payload_path.read_bytes()
    size = len(content)
    file_hash = hashlib.sha256(content).hexdigest()
    payload_files.append({"path": relative, "sha256": file_hash, "size": size})
    payload_digest.update(relative.encode("utf-8"))
    payload_digest.update(b"\0")
    payload_digest.update(file_hash.encode("ascii"))
    payload_digest.update(b"\n")

manifest["payload"] = {
    "fileCount": len(payload_files),
    "files": payload_files,
    "sha256": payload_digest.hexdigest(),
}
manifest["cleanScan"] = {
    "status": "passed",
    "scanner": "data_foundation.release_clean.repository_clean_deployment_check",
    "scannedFiles": int(clean_result.get("scannedFiles") or 0),
    "findingCount": 0,
}
manifest_path.write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  then
    if [[ "$transaction_owned_stage" != "1" ]]; then
      rm -rf "${release_tmp}"
    fi
    error "runtime source staging failed before source pointer switch"
    return 1
  fi
  if [[ ! -f "${release_tmp}/.actanara-runtime-source.json" || ! -f "${release_tmp}/pyproject.toml" ]]; then
    if [[ "$transaction_owned_stage" != "1" ]]; then
      rm -rf "${release_tmp}"
    fi
    error "runtime source release failed validation before switch"
    return 1
  fi
  if ! PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" - "${release_tmp}/.actanara-runtime-source.json" "${release_id}" <<'PY'
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

manifest_path = Path(sys.argv[1])
expected_release_id = sys.argv[2]
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
expected_fields = {
    "schemaVersion", "product", "sourceLocator", "deployedSourceLocator", "releaseLocator",
    "deploymentMode", "copiedAt", "pyprojectVersion", "git", "databaseCompatibility",
    "payload", "cleanScan",
}
if type(manifest.get("schemaVersion")) is not int or manifest.get("schemaVersion") != 2:
    raise SystemExit("staged source manifest has an unsupported privacy schema")
if set(manifest) != expected_fields:
    raise SystemExit("staged source manifest has an invalid exact schema")
if manifest.get("product") != "actanara" or manifest.get("deploymentMode") != "release-symlink":
    raise SystemExit("staged source manifest has invalid release semantics")
try:
    datetime.fromisoformat(manifest.get("copiedAt"))
except (TypeError, ValueError) as exc:
    raise SystemExit("staged source manifest has an invalid copied timestamp") from exc
version = manifest.get("pyprojectVersion")
if version is not None and (
    not isinstance(version, str)
    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version)
):
    raise SystemExit("staged source manifest has an invalid project version")
source_locator = manifest.get("sourceLocator") if isinstance(manifest.get("sourceLocator"), dict) else {}
source_kind = source_locator.get("kind")
source_components = source_locator.get("pathComponents")
if source_kind == "login-home-relative":
    source_valid = set(source_locator) == {"kind", "pathComponents"} and isinstance(source_components, list) and bool(source_components) and all(
        isinstance(item, str)
        and item
        and item not in {".", ".."}
        and "/" not in item
        and "\\" not in item
        and "\0" not in item
        for item in source_components
    )
elif source_kind == "unavailable":
    source_valid = set(source_locator) == {"kind", "issue"} and source_locator.get("issue") in {"outside-login-home", "invalid-relative-components"}
else:
    source_valid = False
if not source_valid:
    raise SystemExit("staged source manifest has an invalid source locator")
for field in ("deployedSourceLocator", "releaseLocator"):
    locator = manifest.get(field) if isinstance(manifest.get(field), dict) else {}
    components = locator.get("pathComponents")
    if (
        set(locator) != {"kind", "pathComponents"}
        or
        locator.get("kind") != "runtime-relative"
        or not isinstance(components, list)
        or not components
        or not all(
            isinstance(item, str)
            and item
            and item not in {".", ".."}
            and "/" not in item
            and "\\" not in item
            and "\0" not in item
            for item in components
        )
    ):
        raise SystemExit("staged source manifest has an invalid runtime locator")
if manifest["deployedSourceLocator"]["pathComponents"] != ["app", "source"]:
    raise SystemExit("staged source manifest has an invalid deployed source locator")
if manifest["releaseLocator"]["pathComponents"] != ["app", "releases", expected_release_id]:
    raise SystemExit("staged source manifest release locator does not match its candidate")
git = manifest.get("git")
if not isinstance(git, dict) or set(git) != {"available", "commit", "branch", "remote", "dirty"}:
    raise SystemExit("staged source manifest has invalid git provenance")
if type(git.get("available")) is not bool:
    raise SystemExit("staged source manifest has invalid git provenance")
if git.get("dirty") is not None and type(git.get("dirty")) is not bool:
    raise SystemExit("staged source manifest has invalid git provenance")
commit = git.get("commit")
if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit)):
    raise SystemExit("staged source manifest has invalid git provenance")
branch = git.get("branch")
if branch is not None and (
    not isinstance(branch, str)
    or not branch
    or branch.startswith(("/", "~/", "file:"))
    or "/Users/" in branch
    or any(character in branch for character in "\0\r\n")
):
    raise SystemExit("staged source manifest has invalid git provenance")
remote = git.get("remote")
if remote is not None:
    if not isinstance(remote, str):
        raise SystemExit("staged source manifest has an unsafe git remote")
    try:
        parsed = urlsplit(remote)
    except (TypeError, ValueError) as exc:
        raise SystemExit("staged source manifest has an unsafe git remote") from exc
    if (
        parsed.scheme not in {"https", "ssh"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("staged source manifest has an unsafe git remote")
clean = manifest.get("cleanScan") if isinstance(manifest.get("cleanScan"), dict) else {}
payload = manifest.get("payload") if isinstance(manifest.get("payload"), dict) else {}
if set(clean) != {"status", "scanner", "scannedFiles", "findingCount"}:
    raise SystemExit("staged source manifest has invalid clean-scan evidence")
if set(payload) != {"fileCount", "files", "sha256"}:
    raise SystemExit("staged source manifest has invalid payload evidence")
if clean.get("status") != "passed" or clean.get("findingCount") != 0:
    raise SystemExit("staged source manifest has no passing clean scan")
if (
    clean.get("scanner") != "data_foundation.release_clean.repository_clean_deployment_check"
    or type(clean.get("scannedFiles")) is not int
    or clean.get("scannedFiles") < 0
    or type(payload.get("fileCount")) is not int
    or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("sha256") or ""))
):
    raise SystemExit("staged source manifest has invalid clean-scan evidence")
if not payload.get("files") or payload.get("fileCount") != len(payload["files"]):
    raise SystemExit("staged source manifest has no complete payload inventory")
expected_paths = set()
aggregate = hashlib.sha256()
for record in payload["files"]:
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
        raise SystemExit("staged source payload inventory contains an invalid record")
    relative_text = record.get("path")
    relative = Path(relative_text) if isinstance(relative_text, str) else Path()
    if (
        not relative_text
        or "\0" in relative_text
        or relative_text.startswith(("~/", "file:"))
        or re.match(r"^[A-Za-z]:[\\/]", relative_text)
        or relative.is_absolute()
        or "." in relative.parts
        or ".." in relative.parts
        or relative.as_posix() in expected_paths
    ):
        raise SystemExit("staged source payload inventory contains an unsafe or duplicate path")
    candidate_file = manifest_path.parent / relative
    if candidate_file.is_symlink() or not candidate_file.is_file():
        raise SystemExit("staged source payload file is missing or unsafe")
    content = candidate_file.read_bytes()
    actual_hash = hashlib.sha256(content).hexdigest()
    if (
        actual_hash != record.get("sha256")
        or type(record.get("size")) is not int
        or len(content) != record.get("size")
    ):
        raise SystemExit("staged source payload changed after release-clean scan")
    normalized = relative.as_posix()
    expected_paths.add(normalized)
    aggregate.update(normalized.encode("utf-8"))
    aggregate.update(b"\0")
    aggregate.update(actual_hash.encode("ascii"))
    aggregate.update(b"\n")
candidate_entries = list(manifest_path.parent.rglob("*"))
if any(item.is_symlink() for item in candidate_entries):
    raise SystemExit("staged source payload contains a symlink after release-clean scan")
owner_marker = manifest_path.parent / ".actanara-update-owner"
actual_paths = {
    item.relative_to(manifest_path.parent).as_posix()
    for item in candidate_entries
    if item not in {manifest_path, owner_marker} and item.is_file()
}
if actual_paths != expected_paths or aggregate.hexdigest() != payload.get("sha256"):
    raise SystemExit("staged source payload file set or aggregate changed after release-clean scan")
compatibility = manifest.get("databaseCompatibility") if isinstance(manifest.get("databaseCompatibility"), dict) else {}
compatibility_fields = {
    "schemaVersion", "policy", "preCommitWriterContract", "minimumReadableSchema",
    "maximumReadableSchema", "migrationSetSha256", "migrations",
}
if set(compatibility) != compatibility_fields:
    raise SystemExit("staged source manifest has invalid migration compatibility evidence")
if (
    type(compatibility.get("schemaVersion")) is not int
    or compatibility.get("schemaVersion") != 1
    or compatibility.get("policy") != "rollback-compatible-additive-only"
    or compatibility.get("preCommitWriterContract") != "prior-reader-compatible-v1"
    or compatibility.get("minimumReadableSchema") != "unversioned"
    or not compatibility.get("migrations")
):
    raise SystemExit("staged source manifest has no migration compatibility contract")
if not re.fullmatch(r"[0-9a-f]{64}", str(compatibility.get("migrationSetSha256") or "")):
    raise SystemExit("staged source manifest has invalid migration compatibility hash")
versions = []
for record in compatibility["migrations"]:
    if (
        not isinstance(record, dict)
        or set(record) != {"version", "sha256", "rollbackClass"}
        or not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+", str(record.get("version") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256") or ""))
        or record.get("rollbackClass") not in {"rollback-compatible-additive", "breaking"}
    ):
        raise SystemExit("staged source manifest has invalid migration compatibility record")
    versions.append(record["version"])
if len(set(versions)) != len(versions) or compatibility.get("maximumReadableSchema") != versions[-1]:
    raise SystemExit("staged source manifest has inconsistent migration compatibility bounds")
PY
  then
    if [[ "$transaction_owned_stage" != "1" ]]; then
      rm -rf "${release_tmp}"
    fi
    error "runtime source release failed clean-scan manifest validation before switch"
    return 1
  fi
  if [[ "$transaction_owned_stage" == "1" ]]; then
    local promoted_source=""
    promoted_source="$(update_transaction_command promote-source-artifact \
      --state "${UPDATE_TRANSACTION_JOURNAL}")"
    if [[ "$promoted_source" != "$release_target" ]]; then
      error "transaction source promotion returned an unexpected path"
      return 1
    fi
  else
    mv "${release_tmp}" "${release_target}"
  fi
  STAGED_RELEASE_ID="${release_id}"
  STAGED_RELEASE_TARGET="${release_target}"
}

promote_fresh_runtime_pointer() {
  local candidate="$1"
  local pointer="$2"
  local store_relative="$3"
  "${PYTHON_BIN}" - "${candidate}" "${pointer}" "${store_relative}" <<'PY'
import os
import sys
from pathlib import Path, PurePosixPath

candidate = Path(os.path.abspath(sys.argv[1]))
pointer = Path(os.path.abspath(sys.argv[2]))
store_relative = PurePosixPath(sys.argv[3])
store_parts = store_relative.parts
if (
    not store_parts
    or store_relative.is_absolute()
    or any(part in {"", ".", ".."} for part in store_parts)
):
    raise SystemExit("managed Runtime store path is unsafe")
expected_store = pointer.parent
for component in (None, *store_parts):
    if component is not None:
        expected_store = expected_store / component
    if expected_store.is_symlink() or not expected_store.is_dir():
        raise SystemExit("managed Runtime store is unavailable or unsafe")
if candidate.is_symlink() or not candidate.is_dir() or candidate.parent != expected_store:
    raise SystemExit("managed Runtime candidate is outside its store")
if os.path.lexists(pointer):
    raise SystemExit("managed Runtime pointer appeared concurrently; rerun with --upgrade")
relative = candidate.relative_to(pointer.parent)
if relative.parts != (*store_parts, candidate.name):
    raise SystemExit("managed Runtime candidate has an unexpected relative target")
raw_target = relative.as_posix()
os.symlink(raw_target, pointer)
descriptor = os.open(pointer.parent, os.O_RDONLY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
try:
    valid = (
        pointer.is_symlink()
        and os.readlink(pointer) == raw_target
        and pointer.resolve(strict=True) == candidate.resolve(strict=True)
    )
except (OSError, RuntimeError):
    valid = False
if not valid:
    raise SystemExit("managed Runtime pointer changed during promotion")
PY
}

promote_staged_runtime_source() {
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$(installer_text step_activate_files)"
    progress_ok "$(installer_text step_activate_files)"
    return 0
  fi
  if [[ -z "$STAGED_RELEASE_TARGET" ]]; then
    error "runtime source promotion requested without a staged release"
    return 1
  fi
  if [[ "${STAGED_RELEASE_TARGET:A}" == "${DEPLOY_SOURCE_ROOT:A}" ]]; then
    return 0
  fi
  local release_target="${STAGED_RELEASE_TARGET}"
  if [[ -e "${DEPLOY_SOURCE_ROOT}" || -L "${DEPLOY_SOURCE_ROOT}" ]]; then
    error "runtime source already exists; use --upgrade for an existing Actanara Runtime"
    return 1
  fi
  if ! promote_fresh_runtime_pointer \
    "${release_target}" \
    "${DEPLOY_SOURCE_ROOT}" \
    "releases"
  then
    return 1
  fi
  if [[ ! -f "${DEPLOY_SOURCE_ROOT}/.actanara-runtime-source.json" ]]; then
    error "runtime source release switch failed validation; existing files were preserved"
    return 1
  fi
}

deploy_runtime_source() {
  stage_runtime_source
  promote_staged_runtime_source
}

staged_source_matches_active() {
  if [[ -z "$STAGED_RELEASE_TARGET" ]]; then
    return 1
  fi
  PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" - "${RUNTIME_HOME}" "${STAGED_RELEASE_TARGET}" <<'PY'
import hashlib
import json
import os
import re
import sys
from pathlib import Path

runtime = Path(os.path.abspath(sys.argv[1]))
candidate = Path(os.path.abspath(sys.argv[2]))
pointer = runtime / "app" / "source"
store = runtime / "app" / "releases"


def fail():
    raise SystemExit(1)


try:
    if not pointer.is_symlink() or store.is_symlink() or not store.is_dir():
        fail()
    raw_target = Path(os.readlink(pointer))
    if (
        raw_target.is_absolute()
        or len(raw_target.parts) != 2
        or raw_target.parts[0] != "releases"
        or any(part in {"", ".", ".."} for part in raw_target.parts)
    ):
        fail()
    active = pointer.parent / raw_target
    if active.is_symlink() or not active.is_dir() or active.parent != store:
        fail()
    if active.resolve(strict=True).parent != store.resolve(strict=True):
        fail()
    if candidate.is_symlink() or not candidate.is_dir() or candidate.parent != store:
        fail()
except (IndexError, OSError, RuntimeError):
    fail()


def manifest_and_verified_payload(root: Path):
    manifest_path = root / ".actanara-runtime-source.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        fail()
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 2
        or manifest.get("product") != "actanara"
        or manifest.get("deploymentMode") != "release-symlink"
    ):
        fail()
    payload = manifest.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"fileCount", "files", "sha256"}:
        fail()
    records = payload.get("files")
    if not isinstance(records, list) or type(payload.get("fileCount")) is not int:
        fail()
    expected = {}
    aggregate = hashlib.sha256()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            fail()
        relative_text = record.get("path")
        digest = record.get("sha256")
        size = record.get("size")
        if (
            not isinstance(relative_text, str)
            or not relative_text
            or not re.fullmatch(r"[0-9a-f]{64}", str(digest or ""))
            or type(size) is not int
            or size < 0
        ):
            fail()
        relative = Path(relative_text)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            fail()
        path = root / relative
        if path.is_symlink() or not path.is_file():
            fail()
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if len(content) != size or actual != digest or relative.as_posix() in expected:
            fail()
        normalized = relative.as_posix()
        expected[normalized] = (actual, size)
        aggregate.update(normalized.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(actual.encode("ascii"))
        aggregate.update(b"\n")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path != manifest_path and path.name != ".actanara-update-owner" and path.is_file()
    }
    if (
        any(path.is_symlink() for path in root.rglob("*"))
        or actual_paths != set(expected)
        or payload["fileCount"] != len(expected)
        or aggregate.hexdigest() != payload["sha256"]
    ):
        fail()
    return manifest, payload


active_manifest, active_payload = manifest_and_verified_payload(active)
candidate_manifest, candidate_payload = manifest_and_verified_payload(candidate)
if (
    active_payload != candidate_payload
    or active_manifest.get("pyprojectVersion") != candidate_manifest.get("pyprojectVersion")
    or active_manifest.get("databaseCompatibility") != candidate_manifest.get("databaseCompatibility")
):
    fail()
PY
}

create_fresh_runtime_venv() {
  local generation="${STAGED_RELEASE_ID:-<release-id>}"
  local venv_store="${RUNTIME_HOME}/app/venvs"
  local venv_target="${venv_store}/${generation}"
  if [[ "$DRY_RUN" != "1" && -z "$STAGED_RELEASE_ID" ]]; then
    error "runtime venv creation requested without a staged source generation"
    return 1
  fi
  if [[ "$DRY_RUN" != "1" && ( -e "$VENV_DIR" || -L "$VENV_DIR" ) ]]; then
    error "runtime venv pointer already exists; use --upgrade for an existing Actanara Runtime"
    return 1
  fi
  if [[ "$DRY_RUN" != "1" && ( -e "$venv_target" || -L "$venv_target" ) ]]; then
    error "runtime venv generation already exists; refusing to overwrite it"
    return 1
  fi
  run_cmd mkdir -p "${venv_store}"
  run_cmd "${PYTHON_BIN}" -m venv "${venv_target}"
  FRESH_STAGED_VENV="${venv_target}"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if [[ ! -f "${venv_target}/bin/python" ]]; then
    error "runtime venv generation failed validation before pointer promotion"
    return 1
  fi
  VENV_DIR="${venv_target}"
  VENV_PY="${venv_target}/bin/python"
}

promote_fresh_runtime_artifacts() {
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if [[ -z "$STAGED_RELEASE_TARGET" || -z "$FRESH_STAGED_VENV" ]]; then
    error "fresh Runtime promotion requires validated source and venv candidates"
    return 1
  fi
  local stable_venv="${RUNTIME_HOME}/.venv"
  promote_staged_runtime_source
  if ! promote_fresh_runtime_pointer \
    "${FRESH_STAGED_VENV}" \
    "${stable_venv}" \
    "app/venvs"
  then
    PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" - \
      "${DEPLOY_SOURCE_ROOT}" "${STAGED_RELEASE_TARGET}" <<'PY'
import os
import sys
from pathlib import Path

pointer = Path(os.path.abspath(sys.argv[1]))
candidate = Path(os.path.abspath(sys.argv[2]))
try:
    if pointer.is_symlink() and pointer.resolve(strict=True) == candidate.resolve(strict=True):
        pointer.unlink()
        descriptor = os.open(pointer.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
except (OSError, RuntimeError):
    raise SystemExit(1)
PY
    error "runtime venv pointer promotion failed; existing files were preserved"
    return 1
  fi
  VENV_DIR="${stable_venv}"
  VENV_PY="${stable_venv}/bin/python"
}

format_selected_external_tools() {
  if [[ -z "$SELECTED_EXTERNAL_TOOLS" ]]; then
    print -r -- "none"
    return 0
  fi
  local items=("${(@ps:;;:)SELECTED_EXTERNAL_TOOLS}")
  local item=""
  local fields=()
  local labels=()
  for item in "${items[@]}"; do
    fields=("${(@ps:|:)item}")
    if [[ "${#fields[@]}" -ge 3 ]]; then
      labels+=("${fields[2]}=${fields[3]}")
    fi
  done
  if [[ "${#labels[@]}" -eq 0 ]]; then
    print -r -- "none"
  else
    print -r -- "${(j:, :)labels}"
  fi
}

format_connected_tools() {
  if [[ -z "$SELECTED_EXTERNAL_TOOLS" ]]; then
    print -r -- "none"
    return 0
  fi
  local items=("${(@ps:;;:)SELECTED_EXTERNAL_TOOLS}")
  local item=""
  local fields=()
  local labels=()
  for item in "${items[@]}"; do
    fields=("${(@ps:|:)item}")
    if [[ "${#fields[@]}" -ge 2 ]]; then
      labels+=("${fields[2]}")
    fi
  done
  if [[ "${#labels[@]}" -eq 0 ]]; then
    print -r -- "none"
  else
    print -r -- "${(j:, :)labels}"
  fi
}

nearest_existing_parent() {
  local target_path="$1"
  local parent="${target_path:h}"
  while [[ ! -e "$parent" && "$parent" != "/" && "$parent" != "." ]]; do
    parent="${parent:h}"
  done
  print -r -- "$parent"
}

preflight_check() {
  local check_status="$1"
  local severity="$2"
  local check_id="$3"
  local message="$4"
  local log_file=""
  log_file="$(installer_log_file)"
  if [[ -d "${log_file:h}" ]]; then
    print -r -- "preflight ${check_status}: ${check_id}: ${message}" >> "$log_file"
  fi
  if [[ "$check_status" != "ok" && "$severity" == "error" ]]; then
    case "$check_id" in
      source-file) progress_fail "$(installer_text check_source_failed)" ;;
      python-*) progress_fail "$(installer_text check_python_failed)" ;;
      writable-target) progress_fail "$(installer_text check_folder_failed)" ;;
      dashboard-port) progress_fail "$(installer_text check_dashboard_failed)" ;;
      *) progress_fail "$(installer_text check_failed)" ;;
    esac
  fi
  if [[ "$check_status" != "ok" && "$severity" == "error" ]]; then
    return 1
  fi
  return 0
}

valid_tcp_port() {
  local value="$1"
  [[ "$value" == <-> ]] && [[ "$value" -ge 1 && "$value" -le 65535 ]]
}

tcp_port_in_use() {
  local port="$1"
  resolve_lsof_bin
  if [[ -z "$LSOF_BIN" ]]; then
    return 2
  fi
  if "$LSOF_BIN" -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

resolve_lsof_bin() {
  if [[ -n "$LSOF_BIN" ]]; then
    return 0
  fi
  LSOF_BIN="$(command -v lsof 2>/dev/null || true)"
  if [[ -z "$LSOF_BIN" && -x "/usr/sbin/lsof" ]]; then
    LSOF_BIN="/usr/sbin/lsof"
  fi
}

resolve_launchctl_bin() {
  if [[ -n "$LAUNCHCTL_BIN" ]]; then
    return 0
  fi
  LAUNCHCTL_BIN="$(command -v launchctl 2>/dev/null || true)"
  if [[ -z "$LAUNCHCTL_BIN" && -x "/bin/launchctl" ]]; then
    LAUNCHCTL_BIN="/bin/launchctl"
  fi
}

resolve_python_bin() {
  if [[ "$PYTHON_SET" == "1" ]]; then
    if command -v "$PYTHON_BIN" >/dev/null 2>&1 || [[ -x "$PYTHON_BIN" ]]; then
      return 0
    fi
    return 1
  fi
  local candidate=""
  local resolved=""
  local first_existing=""
  local seen=" "
  local candidates=()
  local managed_candidates=()
  if [[ -n "$PYTHON_CANDIDATES" ]]; then
    candidates=("${(@z)PYTHON_CANDIDATES}")
  else
    candidates=(
      python3.13
      python3.12
      python3.11
      /opt/homebrew/bin/python3.13
      /opt/homebrew/bin/python3.12
      /opt/homebrew/bin/python3.11
      /usr/local/bin/python3.13
      /usr/local/bin/python3.12
      /usr/local/bin/python3.11
      "$PYTHON_BIN"
      python3
      /opt/homebrew/bin/python3
      /usr/local/bin/python3
      /usr/bin/python3
    )
  fi
  managed_candidates=("${RUNTIME_HOME}"/state/deps/python/*/bin/python3(N))
  candidates+=("${managed_candidates[@]}")
  for candidate in "${candidates[@]}"; do
    resolved="$(resolve_executable "$candidate")"
    if [[ -z "$resolved" || "$seen" == *" ${resolved} "* ]]; then
      continue
    fi
    seen+="${resolved} "
    if [[ -z "$first_existing" ]]; then
      first_existing="$resolved"
    fi
    if python_meets_minimum_version "$resolved"; then
      PYTHON_BIN="$resolved"
      return 0
    fi
  done
  if [[ -n "$first_existing" ]]; then
    PYTHON_BIN="$first_existing"
  fi
  return 1
}

resolve_executable() {
  local candidate="$1"
  local resolved=""
  if [[ -z "$candidate" ]]; then
    return 1
  fi
  if [[ "$candidate" == */* ]]; then
    if [[ -x "$candidate" ]]; then
      print -r -- "$candidate"
      return 0
    fi
    return 1
  fi
  resolved="$(command -v "$candidate" 2>/dev/null || true)"
  if [[ -n "$resolved" ]]; then
    print -r -- "$resolved"
    return 0
  fi
  return 1
}

resolve_curl_bin() {
  local candidate=""
  local resolved=""
  for candidate in "${CURL_BIN:-curl}" /usr/bin/curl; do
    resolved="$(resolve_executable "$candidate" || true)"
    if [[ -n "$resolved" ]]; then
      CURL_BIN="$resolved"
      return 0
    fi
  done
  return 1
}

resolve_tar_bin() {
  local candidate=""
  local resolved=""
  for candidate in "${TAR_BIN:-tar}" /usr/bin/tar /bin/tar; do
    resolved="$(resolve_executable "$candidate" || true)"
    if [[ -n "$resolved" ]]; then
      TAR_BIN="$resolved"
      return 0
    fi
  done
  return 1
}

resolve_shasum_bin() {
  local candidate=""
  local resolved=""
  for candidate in "${SHASUM_BIN:-shasum}" /usr/bin/shasum; do
    resolved="$(resolve_executable "$candidate" || true)"
    if [[ -n "$resolved" ]]; then
      SHASUM_BIN="$resolved"
      return 0
    fi
  done
  return 1
}

resolve_openssl_bin() {
  local candidate=""
  local resolved=""
  for candidate in "${OPENSSL_BIN:-openssl}" /usr/bin/openssl; do
    resolved="$(resolve_executable "$candidate" || true)"
    if [[ -n "$resolved" ]]; then
      OPENSSL_BIN="$resolved"
      return 0
    fi
  done
  return 1
}

python_version_probe() {
  local python="$1"
  "$python" - <<'PY' 2>/dev/null
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
raise SystemExit(0 if sys.version_info >= (3, 11) else 3)
PY
}

python_meets_minimum_version() {
  local python="$1"
  local probe=""
  local probe_status=0
  set +e
  probe="$(python_version_probe "$python")"
  probe_status=$?
  set -e
  [[ "$probe_status" == "0" && -n "$probe" ]]
}

standalone_python_target_arch() {
  local machine="${ACTANARA_INSTALL_MACHINE:-}"
  if [[ -z "$machine" && -n "$UNAME_BIN" ]]; then
    machine="$("$UNAME_BIN" -m)"
  fi
  case "$machine" in
    arm64|aarch64)
      print -r -- "aarch64"
      ;;
    x86_64|amd64)
      print -r -- "x86_64"
      ;;
    *)
      return 1
      ;;
  esac
}

standalone_python_default_sha256() {
  local target_arch="$1"
  if [[ "$PYTHON_STANDALONE_RELEASE" == "20260623" && "$PYTHON_STANDALONE_VERSION" == "3.13.14" ]]; then
    case "$target_arch" in
      aarch64)
        print -r -- "804c86c8665b18eb0df5070a79d828229018d145baea38a71a5c74c03f9b11d4"
        return 0
        ;;
      x86_64)
        print -r -- "cd0023fb84de358d285c8e116cffd2f433086b943e752955dade521c12e78cab"
        return 0
        ;;
    esac
  fi
  return 1
}

standalone_python_install_dir() {
  local target_arch="$1"
  if [[ -n "$PYTHON_STANDALONE_DIR" ]]; then
    print -r -- "$PYTHON_STANDALONE_DIR"
  else
    print -r -- "${RUNTIME_HOME}/state/deps/python/cpython-${PYTHON_STANDALONE_VERSION}-${target_arch}-apple-darwin"
  fi
}

standalone_python_url() {
  local target_arch="$1"
  if [[ -n "$PYTHON_STANDALONE_URL" ]]; then
    print -r -- "$PYTHON_STANDALONE_URL"
  else
    print -r -- "${PYTHON_STANDALONE_BASE_URL}/cpython-${PYTHON_STANDALONE_VERSION}%2B${PYTHON_STANDALONE_RELEASE}-${target_arch}-apple-darwin-install_only.tar.gz"
  fi
}

standalone_python_sha256() {
  local target_arch="$1"
  if [[ -n "$PYTHON_STANDALONE_SHA256" ]]; then
    print -r -- "${PYTHON_STANDALONE_SHA256#sha256:}"
  else
    standalone_python_default_sha256 "$target_arch"
  fi
}

managed_standalone_python_bin() {
  local target_arch=""
  local install_dir=""
  target_arch="$(standalone_python_target_arch)" || return 1
  install_dir="$(standalone_python_install_dir "$target_arch")"
  if [[ -x "${install_dir}/bin/python3" ]]; then
    print -r -- "${install_dir}/bin/python3"
    return 0
  fi
  return 1
}

sha256_file() {
  local file="$1"
  if resolve_shasum_bin; then
    "$SHASUM_BIN" -a 256 "$file" | awk '{ print $1 }'
    return 0
  fi
  if resolve_openssl_bin; then
    "$OPENSSL_BIN" dgst -sha256 -r "$file" | awk '{ print $1 }'
    return 0
  fi
  return 1
}

verify_sha256_file() {
  local file="$1"
  local expected="$2"
  local actual=""
  if [[ -z "$expected" ]]; then
    warn "No SHA-256 checksum is configured for managed standalone Python; refusing to execute an unverified interpreter."
    return 1
  fi
  actual="$(sha256_file "$file")" || {
    warn "Cannot verify managed standalone Python because neither shasum nor openssl is available."
    return 1
  }
  if [[ "$actual" != "$expected" ]]; then
    warn "Managed standalone Python checksum mismatch: expected ${expected}, got ${actual}"
    return 1
  fi
  return 0
}

install_managed_standalone_python() {
  local target_arch=""
  local install_dir=""
  local python_bin=""
  local url=""
  local expected_sha=""
  local tmp_dir=""
  local extract_dir=""
  local archive=""
  local broken_dir=""
  local label=""
  local log_file=""

  if [[ "$PYTHON_SET" == "1" || "$PYTHON_AUTO_INSTALL" != "1" ]]; then
    return 1
  fi
  if [[ "$PLATFORM" != "Darwin" ]]; then
    warn "Python >=3.11 was not found. Managed standalone Python auto-install is currently supported on macOS only; rerun with --python /path/to/python3.13."
    return 1
  fi
  target_arch="$(standalone_python_target_arch)" || {
    warn "Python >=3.11 was not found and this macOS CPU architecture is not supported by the managed standalone Python installer."
    return 1
  }
  install_dir="$(standalone_python_install_dir "$target_arch")"
  python_bin="${install_dir}/bin/python3"
  if [[ -x "$python_bin" ]] && python_meets_minimum_version "$python_bin"; then
    PYTHON_BIN="$python_bin"
    return 0
  fi
  url="$(standalone_python_url "$target_arch")"
  expected_sha="$(standalone_python_sha256 "$target_arch" || true)"
  if [[ "$DRY_RUN" == "1" ]]; then
    if [[ "$PYTHON_INSTALL_PLANNED" != "1" ]]; then
      progress_start "$(installer_text step_prepare_python) ${PYTHON_STANDALONE_VERSION}"
      progress_ok "$(installer_text step_prepare_python) ${PYTHON_STANDALONE_VERSION}"
    fi
    PYTHON_INSTALL_PLANNED=1
    return 1
  fi
  if ! resolve_curl_bin; then
    warn "Python >=3.11 was not found and curl is unavailable; cannot download managed standalone Python."
    return 1
  fi
  if ! resolve_tar_bin; then
    warn "Python >=3.11 was not found and tar is unavailable; cannot extract managed standalone Python."
    return 1
  fi

  log "Python >=3.11 not found; installing managed standalone Python ${PYTHON_STANDALONE_VERSION} for ${target_arch}"
  tmp_dir="${RUNTIME_HOME}/state/tmp/python-standalone.$$"
  extract_dir="${tmp_dir}/extract"
  archive="${tmp_dir}/python.tar.gz"
  label="$(installer_text step_prepare_python) ${PYTHON_STANDALONE_VERSION}"
  log_file="$(installer_log_file)"
  progress_start "$label"
  /bin/mkdir -p "${log_file:h}" || {
    progress_fail "$(installer_text step_failed) ${log_file}"
    return 1
  }
  : >> "$log_file"
  /bin/chmod 600 "$log_file"
  if ! (
    set -e
    print -r -- "## Preparing Python ${PYTHON_STANDALONE_VERSION} for ${target_arch}"
    /bin/mkdir -p "$extract_dir" "${install_dir:h}"
    "$CURL_BIN" --silent --show-error -fL --retry 3 --retry-delay 1 -o "$archive" "$url"
    verify_sha256_file "$archive" "$expected_sha"
    "$TAR_BIN" -xzf "$archive" -C "$extract_dir"
    [[ -x "${extract_dir}/python/bin/python3" ]]
    if [[ -e "$install_dir" ]]; then
      broken_dir="${install_dir}.broken.$(/bin/date +%Y%m%d%H%M%S)"
      /bin/mv "$install_dir" "$broken_dir"
    fi
    /bin/mv "${extract_dir}/python" "$install_dir"
    /bin/rm -rf "$tmp_dir"
  ) >> "$log_file" 2>&1; then
    /bin/rm -rf "$tmp_dir"
    progress_fail "$(installer_text step_failed) ${log_file}"
    return 1
  fi
  if ! python_meets_minimum_version "$python_bin"; then
    print -r -- "ERROR: prepared Python did not meet the minimum version" >> "$log_file"
    progress_fail "$(installer_text step_failed) ${log_file}"
    return 1
  fi
  PYTHON_BIN="$python_bin"
  progress_ok "$label"
  return 0
}

maybe_install_python_runtime() {
  if install_managed_standalone_python; then
    return 0
  fi
  return 1
}

ensure_python_bin() {
  if resolve_python_bin; then
    return 0
  fi
  maybe_install_python_runtime || true
  resolve_python_bin
}

dashboard_port_candidates() {
  local candidates=()
  local seen=" "
  local candidate=""
  candidates+=("$DASHBOARD_PORT")
  for candidate in ${(z)DASHBOARD_PORT_CANDIDATES}; do
    candidates+=("$candidate")
  done
  for candidate in "${candidates[@]}"; do
    if valid_tcp_port "$candidate" && [[ "$seen" != *" ${candidate} "* ]]; then
      seen+="${candidate} "
      print -r -- "$candidate"
    fi
  done
}

select_dashboard_port() {
  if [[ "$NO_DASHBOARD_SERVER" == "1" ]]; then
    return 0
  fi
  if ! valid_tcp_port "$DASHBOARD_PORT"; then
    error "Dashboard port must be between 1 and 65535"
    exit 2
  fi
  if [[ "$DASHBOARD_PORT_AUTO" != "1" ]]; then
    return 0
  fi
  if [[ "$UPGRADE" == "1" ]]; then
    return 0
  fi
  local candidate=""
  local probe_status=0
  for candidate in $(dashboard_port_candidates); do
    set +e
    tcp_port_in_use "$candidate"
    probe_status=$?
    set -e
    if [[ "$probe_status" == "1" ]]; then
      if [[ "$candidate" != "$DASHBOARD_PORT" ]]; then
        warn "Dashboard port ${DASHBOARD_PORT} is in use; falling back to ${candidate}"
        DASHBOARD_PORT="$candidate"
      fi
      return 0
    fi
    if [[ "$probe_status" == "2" ]]; then
      warn "lsof not found; cannot auto-select Dashboard fallback port. Using ${DASHBOARD_PORT}."
      return 0
    fi
  done
  warn "All Dashboard fallback ports are in use: $(dashboard_port_candidates | tr '\n' ' ')"
  return 0
}

require_repair_runtime_identity() {
  if [[ "$REPAIR_EXISTING" != "1" ]]; then
    return 0
  fi
  if [[ -L "$RUNTIME_HOME" || ! -d "$RUNTIME_HOME" ]]; then
    error "--repair-existing requires a legacy Actanara Runtime: ${RUNTIME_HOME}"
    return 2
  fi
  if [[ ! -f "${RUNTIME_HOME}/config/settings.json" || -L "${RUNTIME_HOME}/config/settings.json" ]]; then
    error "--repair-existing requires a legacy Actanara Runtime with preservable Settings"
    return 2
  fi
  local marker=""
  local marker_count=0
  for marker in \
    "${RUNTIME_HOME}/app/source" \
    "${RUNTIME_HOME}/.venv" \
    "${RUNTIME_HOME}/config/runtime.json" \
    "${RUNTIME_HOME}/data/actanara_data.sqlite3" \
    "${RUNTIME_HOME}/bin/actanara" \
    "${RUNTIME_HOME}/bin/actanara"; do
    if [[ -e "$marker" || -L "$marker" ]]; then
      (( marker_count += 1 ))
    fi
  done
  if (( marker_count == 0 )); then
    error "--repair-existing requires a legacy Actanara Runtime"
    return 2
  fi
}

require_fresh_runtime_empty() {
  if [[ "$UPGRADE" == "1" || "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if [[ -L "${RUNTIME_HOME}/app" || ( -e "${RUNTIME_HOME}/app" && ! -d "${RUNTIME_HOME}/app" ) ]]; then
    error "existing Actanara Runtime state requires --upgrade: ${RUNTIME_HOME}"
    return 2
  fi
  local marker=""
  for marker in \
    "${RUNTIME_HOME}/app/source" \
    "${RUNTIME_HOME}/app/releases" \
    "${RUNTIME_HOME}/app/venvs" \
    "${RUNTIME_HOME}/app/update-transactions" \
    "${RUNTIME_HOME}/app/.update-transaction.lock" \
    "${RUNTIME_HOME}/.venv" \
    "${RUNTIME_HOME}/config/runtime.json" \
    "${RUNTIME_HOME}/config/settings.json" \
    "${RUNTIME_HOME}/data/actanara_data.sqlite3" \
    "${RUNTIME_HOME}/bin/actanara"; do
    if [[ -e "$marker" || -L "$marker" ]]; then
      error "existing Actanara Runtime state requires --upgrade: ${RUNTIME_HOME}"
      return 2
    fi
  done
  return 0
}

run_installer_preflight() {
  require_fresh_runtime_empty || return 2
  log "Installer preflight/doctor"
  local errors=0
  local python_probe=""
  local python_status=0
  local required_file=""
  local required_files=(
    "LICENSE"
    "pyproject.toml"
    "install/dependency_contract.py"
    "install/runtime-dependencies.lock.json"
    "advanced/cli/actanara.py"
    "advanced/dashboard/dashboard_launch_agent.py"
    "advanced/dashboard/rag_server_launch_agent.py"
    "advanced/pipeline/run_daily_pipeline.py"
    "advanced/pipeline/run_dashboard_foundation_refresh.py"
    "src/dashboard/app/main.py"
    "src/dashboard/app/static/index.html"
    "src/data_foundation/migrations/0001_initial.sql"
  )
  local parent=""
  local target_path=""
  local dashboard_port="${DASHBOARD_PORT}"

  for required_file in "${required_files[@]}"; do
    if [[ -f "${SOURCE_ROOT}/${required_file}" ]]; then
      preflight_check ok error "source-file" "found ${SOURCE_ROOT}/${required_file}" || errors=$(( errors + 1 ))
    else
      preflight_check error error "source-file" "missing ${SOURCE_ROOT}/${required_file}" || errors=$(( errors + 1 ))
    fi
  done

  if ensure_python_bin; then
    set +e
    python_probe="$(python_version_probe "$PYTHON_BIN")"
    python_status=$?
    set -e
    if [[ "$python_status" == "0" && -n "$python_probe" ]]; then
      preflight_check ok error "python-version" "${PYTHON_BIN} reports Python ${python_probe}" || errors=$(( errors + 1 ))
    elif [[ "$python_status" == "3" ]]; then
      if [[ "$PYTHON_SET" != "1" && "$PYTHON_AUTO_INSTALL" == "1" ]]; then
        maybe_install_python_runtime || true
        set +e
        python_probe="$(python_version_probe "$PYTHON_BIN")"
        python_status=$?
        set -e
      fi
      if [[ "$python_status" == "0" && -n "$python_probe" ]]; then
        preflight_check ok error "python-version" "${PYTHON_BIN} reports Python ${python_probe}" || errors=$(( errors + 1 ))
      elif [[ "$DRY_RUN" == "1" && "$PYTHON_INSTALL_PLANNED" == "1" ]]; then
        preflight_check warn warn "python-bootstrap" "Python >=3.11 not found; dry-run would install managed standalone Python ${PYTHON_STANDALONE_VERSION}" || true
      else
        preflight_check error error "python-version" "${PYTHON_BIN} reports Python ${python_probe}; Python >=3.11 is required" || errors=$(( errors + 1 ))
      fi
    else
      preflight_check warn warn "python-version" "${PYTHON_BIN} is executable but version could not be verified before venv creation" || true
    fi
    if "$PYTHON_BIN" -c "import venv" >/dev/null 2>&1
    then
      preflight_check ok error "python-venv" "${PYTHON_BIN} can create virtual environments" || errors=$(( errors + 1 ))
    else
      preflight_check error error "python-venv" "${PYTHON_BIN} cannot run python -m venv" || errors=$(( errors + 1 ))
    fi
  else
    if [[ "$DRY_RUN" == "1" && "$PYTHON_INSTALL_PLANNED" == "1" ]]; then
      preflight_check warn warn "python-bootstrap" "Python >=3.11 not found; dry-run would install managed standalone Python ${PYTHON_STANDALONE_VERSION}" || true
    elif [[ "$DRY_RUN" == "1" && "$PYTHON_SET" != "1" ]]; then
      preflight_check warn warn "python-command" "Python executable not found during dry-run preview: ${PYTHON_BIN}" || true
    else
      preflight_check error error "python-command" "Python executable not found: ${PYTHON_BIN}" || errors=$(( errors + 1 ))
    fi
  fi

  for target_path in "$RUNTIME_HOME" "$DIARY_OUTPUT_DIR" "$REPORTS_OUTPUT_DIR" "$SNAPSHOTS_OUTPUT_DIR" "$ARCHIVES_OUTPUT_DIR" "${LOCATION_FILE:h}"; do
    parent="$(nearest_existing_parent "$target_path")"
    if [[ -n "$parent" && -w "$parent" ]]; then
      preflight_check ok error "writable-target" "${target_path} can be created under ${parent}" || errors=$(( errors + 1 ))
    else
      preflight_check error error "writable-target" "${target_path} cannot be created; nearest parent is not writable: ${parent:-unknown}" || errors=$(( errors + 1 ))
    fi
  done

  if [[ "$CREATE_DESKTOP_DIARY_LINK" == "1" ]]; then
    parent="$(nearest_existing_parent "$DESKTOP_DIARY_LINK")"
    if [[ -n "$parent" && -w "$parent" ]]; then
      preflight_check ok warn "desktop-shortcut" "${DESKTOP_DIARY_LINK} can be created under ${parent}" || true
    else
      preflight_check warn warn "desktop-shortcut" "${DESKTOP_DIARY_LINK} may not be creatable; installer will continue without core failure" || true
    fi
  fi

  if [[ "$PLATFORM" == "Darwin" && ( "$NO_SCHEDULER" != "1" || "$NO_DASHBOARD_SERVER" != "1" ) ]]; then
    resolve_launchctl_bin
    if [[ -n "$LAUNCHCTL_BIN" ]]; then
      preflight_check ok warn "launchctl" "${LAUNCHCTL_BIN} is available" || true
      local uid=""
      uid="$("$ID_BIN" -u 2>/dev/null || print -r -- "")"
      if [[ -n "$uid" ]] && "$LAUNCHCTL_BIN" print "gui/${uid}" >/dev/null 2>&1; then
        preflight_check ok warn "launchagent-domain" "gui/${uid} launchd domain is available" || true
      else
        preflight_check warn warn "launchagent-domain" "launchd gui domain is not available; service registration may be skipped or fail" || true
      fi
    else
      preflight_check warn warn "launchctl" "launchctl not found; managed service registration may be skipped or fail" || true
    fi
    parent="$(nearest_existing_parent "$HOME/Library/LaunchAgents")"
    if [[ -n "$parent" && -w "$parent" ]]; then
      preflight_check ok warn "launchagents-writable" "$HOME/Library/LaunchAgents can be created under ${parent}" || true
    else
      preflight_check warn warn "launchagents-writable" "$HOME/Library/LaunchAgents may not be writable; scheduler/SSE service registration may fail" || true
    fi
  fi

  if [[ "$NO_DASHBOARD_SERVER" != "1" ]]; then
    if ! valid_tcp_port "$dashboard_port"; then
      preflight_check error error "dashboard-port" "TCP port ${dashboard_port} is invalid" || errors=$(( errors + 1 ))
    else
      local port_probe_status=0
      resolve_lsof_bin
      set +e
      tcp_port_in_use "$dashboard_port"
      port_probe_status=$?
      set -e
      case "$port_probe_status" in
        0)
          if [[ "$UPGRADE" == "1" ]]; then
            preflight_check warn warn "dashboard-port" "TCP port ${dashboard_port} is already in use; upgrade will replace/restart the managed SSE service on this port" || true
          elif [[ "$DASHBOARD_PORT_AUTO" == "1" ]]; then
            preflight_check warn warn "dashboard-port" "TCP port ${dashboard_port} is already in use after fallback selection; SSE server registration may fail" || true
          else
            preflight_check error error "dashboard-port" "TCP port ${dashboard_port} is already in use and --no-dashboard-port-auto is set" || errors=$(( errors + 1 ))
          fi
          ;;
        1)
          preflight_check ok warn "dashboard-port" "TCP port ${dashboard_port} appears available" || true
          ;;
        *)
          preflight_check warn warn "dashboard-port" "lsof not found; cannot preflight TCP port ${dashboard_port}" || true
          ;;
      esac
    fi
  fi

  preflight_check ok warn "pip-network" "pip network access is deferred to dependency installation; failures will be reported with the pip command" || true

  if [[ "$errors" -gt 0 ]]; then
    print -r -- "$(installer_text check_failed)" >&2
    exit 2
  fi
}

run_post_install_doctor() {
  log "Post-install doctor"
  run_json_cmd "Runtime status doctor" \
    "${VENV_PY}" -m data_foundation.cli \
    onboarding runtime-status \
    --runtime "${RUNTIME_HOME}" \
    --json
  run_optional_json_cmd "Installer doctor" \
    "${VENV_PY}" -m data_foundation.cli \
    doctor --installer \
    --runtime "${RUNTIME_HOME}" \
    --json
  run_optional_json_cmd "Pipeline doctor" \
    "${VENV_PY}" -m data_foundation.cli \
    doctor --pipeline \
    --runtime "${RUNTIME_HOME}" \
    --json
  run_optional_json_cmd "Scheduler doctor" \
    "${VENV_PY}" -m data_foundation.cli \
    doctor --scheduler \
    --runtime "${RUNTIME_HOME}" \
    --json
  if [[ "$ENABLE_RAG" == "1" ]]; then
    run_optional_json_cmd "nova-RAG doctor" \
      "${VENV_PY}" -m data_foundation.cli \
      doctor --rag \
      --runtime "${RUNTIME_HOME}" \
      --json
  fi
}

cleanup_runtime_source_artifacts() {
  if [[ "$DRY_RUN" == "1" || ! -e "${DEPLOY_SOURCE_ROOT}" ]]; then
    return 0
  fi
  progress_start "$(installer_text step_finish_install)"
  rm -rf "${DEPLOY_SOURCE_ROOT}/build" "${DEPLOY_SOURCE_ROOT}/dist"
  find -H "${DEPLOY_SOURCE_ROOT}" \
    \( -type d -name "__pycache__" -o -type d -name "*.egg-info" \) \
    -prune -exec rm -rf {} + 2>/dev/null || true
  progress_ok "$(installer_text step_finish_install)"
}

run_runtime_dependency_check() {
  local missing_file="$1"
  local dependency_actanara_home="${RUNTIME_HOME}"
  local dependency_location_file="${LOCATION_FILE}"
  local -a check_command
  check_command=("${VENV_PY}" -)
  if [[ "$UPDATE_TRANSACTION_ACTIVE" == "1" ]]; then
    check_command=(
      "${PYTHON_BIN}" "${UPDATE_TRANSACTION_HELPER}"
      run-candidate-command
      --state "${UPDATE_TRANSACTION_JOURNAL}"
      --phase candidate-dependency-check
      --
      /usr/bin/env
      -i
      "PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
      "USER=actanara-candidate"
      "LOGNAME=actanara-candidate"
      "SHELL=/bin/zsh"
      "LC_ALL=C"
      "LANG=C"
      "ACTANARA_INSTALL_DEPLOY_SOURCE_ROOT=${DEPLOY_SOURCE_ROOT}"
      "ACTANARA_INSTALL_ENABLE_RAG=${ENABLE_RAG}"
      "ACTANARA_INSTALL_RAG_EMBEDDING_MODE=${RAG_EMBEDDING_MODE}"
      "ACTANARA_INSTALL_MISSING_DEPENDENCIES_FILE=${missing_file}"
      "ACTANARA_HOME=${UPDATE_VALIDATION_RUNTIME}"
      "ACTANARA_LOCATION_FILE=${UPDATE_VALIDATION_RUNTIME}/location.json"
      "PYTHONPATH=${DEPLOY_SOURCE_ROOT}:${DEPLOY_SOURCE_ROOT}/src:${DEPLOY_SOURCE_ROOT}/src/dashboard"
      "HOME=${UPDATE_VALIDATION_RUNTIME}/home"
      "TMPDIR=${UPDATE_VALIDATION_RUNTIME}/tmp"
      "XDG_CONFIG_HOME=${UPDATE_VALIDATION_RUNTIME}/xdg"
      "PIP_CONFIG_FILE=/dev/null"
      "PIP_CACHE_DIR=${UPDATE_VALIDATION_RUNTIME}/pip-cache"
      "PYTHONNOUSERSITE=1"
      "ACTANARA_SECRET_BACKEND=memory"
      "PYTHONDONTWRITEBYTECODE=1"
      "${VENV_PY}" -
    )
  fi
  if [[ "$UPDATE_TRANSACTION_ACTIVE" == "1" ]]; then
    dependency_actanara_home="${UPDATE_VALIDATION_RUNTIME}"
    dependency_location_file="${UPDATE_VALIDATION_RUNTIME}/location.json"
  fi
  rm -f "${missing_file}"
  ACTANARA_INSTALL_DEPLOY_SOURCE_ROOT="${DEPLOY_SOURCE_ROOT}" \
  ACTANARA_INSTALL_ENABLE_RAG="${ENABLE_RAG}" \
  ACTANARA_INSTALL_RAG_EMBEDDING_MODE="${RAG_EMBEDDING_MODE}" \
  ACTANARA_INSTALL_MISSING_DEPENDENCIES_FILE="${missing_file}" \
  ACTANARA_HOME="${dependency_actanara_home}" \
  ACTANARA_LOCATION_FILE="${dependency_location_file}" \
  PYTHONPATH="${DEPLOY_SOURCE_ROOT}:${DEPLOY_SOURCE_ROOT}/src:${DEPLOY_SOURCE_ROOT}/src/dashboard" \
  PYTHONDONTWRITEBYTECODE=1 \
    "${check_command[@]}" <<'PY'
import importlib
import os
import sys
from pathlib import Path

source_root = Path(os.environ["ACTANARA_INSTALL_DEPLOY_SOURCE_ROOT"])
missing_file = Path(os.environ["ACTANARA_INSTALL_MISSING_DEPENDENCIES_FILE"])
required_static = [
    source_root / "src" / "dashboard" / "app" / "static" / "index.html",
    source_root / "src" / "dashboard" / "app" / "static" / "css" / "style.css",
    source_root / "src" / "dashboard" / "app" / "static" / "js" / "app.js",
]
dashboard_checks = [
    ("fastapi", "fastapi>=0.110,<1", "Dashboard API"),
    ("uvicorn", "uvicorn>=0.29,<1", "Dashboard server"),
    ("yaml", "PyYAML>=6,<7", "Dashboard settings YAML"),
    ("croniter", "croniter>=2,<7", "Dashboard scheduler"),
]
rag_checks = []
if os.environ.get("ACTANARA_INSTALL_ENABLE_RAG") == "1":
    rag_checks = [
        ("numpy", "numpy>=1.26,<3", "nova-RAG vectors"),
        ("pydantic", "pydantic>=2,<3", "nova-RAG API schema"),
    ]
    if os.environ.get("ACTANARA_INSTALL_RAG_EMBEDDING_MODE") == "local":
        rag_checks[0:0] = [
            ("sentence_transformers", "sentence-transformers>=3,<6", "nova-RAG local embeddings"),
            ("torch", "torch>=2,<3", "nova-RAG local embeddings"),
        ]
checks = dashboard_checks + rag_checks

errors = []
missing_packages = []
for path in required_static:
    if not path.is_file():
        errors.append(f"missing Dashboard static asset: {path}")

for module_name, package_spec, purpose in checks:
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        errors.append(f"{purpose} dependency import failed: {module_name}: {exc}")
        missing_packages.append(package_spec)

try:
    importlib.import_module("app.main")
except Exception as exc:
    errors.append(f"Dashboard application import failed: app.main: {exc}")

if errors:
    if missing_packages:
        missing_file.parent.mkdir(parents=True, exist_ok=True)
        missing_file.write_text("\n".join(dict.fromkeys(missing_packages)) + "\n", encoding="utf-8")
    for item in errors:
        print(f"dependency gate error: {item}", file=sys.stderr)
    raise SystemExit(1)

print("dependency gate ok: Dashboard static assets: index.html, style.css, app.js")
print("dependency gate ok: Dashboard dependencies: fastapi, uvicorn, PyYAML, croniter")
if rag_checks:
    print("dependency gate ok: nova-RAG local dependencies: sentence-transformers, torch, numpy, pydantic")
PY
}

run_runtime_dependency_gate() {
  local technical_label="Verifying runtime dependency gate"
  local label="$(installer_text step_check_components)"
  local log_file=""
  log "Verifying runtime Dashboard dependency gate"
  log "Dependency gate: Dashboard dependencies (fastapi, uvicorn, PyYAML, croniter) and static UI assets"
  if [[ "$ENABLE_RAG" == "1" && "$RAG_EMBEDDING_MODE" == "local" ]]; then
    log "Dependency gate: nova-RAG local dependencies (sentence-transformers, torch, numpy, pydantic)"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    if [[ "$SUMMARY_ONLY" == "1" ]]; then
      return 0
    fi
    progress_start "$label"
    progress_ok "$label"
    return 0
  fi
  local missing_file="${RUNTIME_HOME}/state/tmp/installer-dependency-gate-missing.txt"
  if [[ "$UPDATE_TRANSACTION_ACTIVE" == "1" && -n "$UPDATE_TRANSACTION_DIR" ]]; then
    missing_file="${UPDATE_TRANSACTION_DIR}/candidate-dependency-gate-missing.txt"
  fi
  mkdir -p "${missing_file:h}"
  log_file="$(installer_log_file)"
  mkdir -p "${log_file:h}"
  progress_start "$label"
  print -r -- "" >> "$log_file"
  print -r -- "## ${technical_label}" >> "$log_file"
  if run_runtime_dependency_check "${missing_file}" >> "$log_file" 2>&1; then
    progress_ok "$label"
    return 0
  fi
  progress_fail "$(installer_text step_failed) ${log_file}"
  return 1
}

run_dashboard_service_launch_agent_apply() {
  if [[ "$UPGRADE" == "1" ]]; then
    run_json_cmd "SSE server LaunchAgent service registration" \
      "${VENV_PY}" -c 'import json; from app.services import launcher; print(json.dumps(launcher.install_dashboard_launch_agent({"confirmationText": launcher.DASHBOARD_INSTALL_CONFIRMATION}), ensure_ascii=False, indent=2))'
  else
    run_optional_json_cmd "SSE server LaunchAgent service registration" \
      "${VENV_PY}" -c 'import json; from app.services import launcher; print(json.dumps(launcher.install_dashboard_launch_agent({"confirmationText": launcher.DASHBOARD_INSTALL_CONFIRMATION}), ensure_ascii=False, indent=2))'
  fi
}

run_rag_service_launch_agent_apply() {
  if [[ "$UPGRADE" == "1" ]]; then
    run_json_cmd "nova-RAG server LaunchAgent service registration" \
      "${VENV_PY}" -c 'import json; from app.services import launcher; print(json.dumps(launcher.install_rag_launch_agent({"confirmationText": launcher.RAG_INSTALL_CONFIRMATION}), ensure_ascii=False, indent=2))'
  else
    run_optional_json_cmd "nova-RAG server LaunchAgent service registration" \
      "${VENV_PY}" -c 'import json; from app.services import launcher; print(json.dumps(launcher.install_rag_launch_agent({"confirmationText": launcher.RAG_INSTALL_CONFIRMATION}), ensure_ascii=False, indent=2))'
  fi
}

run_external_rag_skill_registration_apply() {
  local technical_label="Reconciling Actanara Memory Search skill for selected external tools"
  local label="$(installer_text step_connect_tools)"
  local log_file=""
  if [[ "$ENABLE_SKILL_REGISTRATION" != "1" && "$UPGRADE" != "1" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$label"
    progress_ok "$label"
    return 0
  fi
  log_file="$(installer_log_file)"
  progress_start "$label"
  print -r -- "" >> "$log_file"
  print -r -- "## ${technical_label}" >> "$log_file"
  if ACTANARA_HOME="${RUNTIME_HOME}" \
    ACTANARA_LOCATION_FILE="${LOCATION_FILE}" \
    ACTANARA_INSTALL_UPGRADE_RECONCILE="${UPGRADE}" \
    ACTANARA_INSTALL_EXPLICIT_SKILL_REGISTRATION="${ENABLE_SKILL_REGISTRATION}" \
    PYTHONPATH="${DEPLOY_SOURCE_ROOT}:${DEPLOY_SOURCE_ROOT}/src:${DEPLOY_SOURCE_ROOT}/src/dashboard" \
    "${VENV_PY}" - >> "$log_file" 2>&1 <<'PY'
import json
import os

print(json.dumps({"event": "memory-skill-reconcile-start"}), flush=True)

from app.services.external_rag_skill_registration import (
    DEFAULT_TARGETS,
    MEMORY_CONFIRMATION_TEXT,
    existing_memory_skill_tools,
    queue_memory_skill_registration,
)
from data_foundation.external_tool_catalog import detected_external_tool_ids
from data_foundation.paths import load_paths
from data_foundation.settings import read_settings, write_settings

paths = load_paths()
settings = read_settings(paths, redact_secrets=True)
external = settings.get("externalTools") if isinstance(settings.get("externalTools"), dict) else {}
preference = external.get("installerV2SkillRegistration") if isinstance(external.get("installerV2SkillRegistration"), dict) else {}
upgrade_reconcile = os.environ.get("ACTANARA_INSTALL_UPGRADE_RECONCILE") == "1"
explicit_registration = os.environ.get("ACTANARA_INSTALL_EXPLICIT_SKILL_REGISTRATION") == "1"
existing_tools = existing_memory_skill_tools(paths)
previously_enabled = (
    preference.get("supportedNow") is True
    and str(preference.get("status") or "") in {
        "dashboard-controlled",
        "installer-applied",
        "installer-selected",
        "reconcile-pending",
        "reconciled",
    }
) or bool(existing_tools)
print(json.dumps({
    "event": "memory-skill-reconcile-plan",
    "upgrade": upgrade_reconcile,
    "explicit": explicit_registration,
    "previouslyEnabled": previously_enabled,
}, sort_keys=True))
if upgrade_reconcile and not explicit_registration and not previously_enabled:
    print(json.dumps({"accepted": True, "status": "skipped", "reason": "memory-skill-registration-not-previously-enabled"}))
    raise SystemExit(0)
selected = preference.get("selectedTools") if isinstance(preference.get("selectedTools"), list) else external.get("installerSelectedTools")
selected = selected if isinstance(selected, list) else []
auto_detect = explicit_registration and not selected
if auto_detect:
    selected = list(detected_external_tool_ids(paths))
if not selected:
    selected = existing_tools
tools = []
for item in selected:
    key = str(item.get("key") or "") if isinstance(item, dict) else str(item)
    if key in DEFAULT_TARGETS and key not in tools:
        tools.append(key)
print(json.dumps({"event": "memory-skill-reconcile-targets", "tools": tools}, sort_keys=True))
if not tools:
    if explicit_registration:
        write_settings({"externalTools": {"installerV2SkillRegistration": {
            "status": "reconcile-pending" if upgrade_reconcile else "installer-selected",
            "supportedNow": True,
            "selectedTools": [],
            "autoDetectTools": auto_detect,
            "lastBackendPolicy": "runtime-auto",
        }}})
    print(json.dumps({"accepted": True, "status": "skipped", "reason": "no-supported-installer-selected-tools"}))
else:
    result = queue_memory_skill_registration({
        "tools": tools,
        "dryRun": False,
        "overwrite": False,
        "confirmationText": MEMORY_CONFIRMATION_TEXT,
    }, requested_by="installer-v2-upgrade-reconcile" if upgrade_reconcile else "installer-v2")
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    eligible = job.get("eligibleTools") if isinstance(job.get("eligibleTools"), list) else []
    excluded = job.get("excludedTools") if isinstance(job.get("excludedTools"), list) else []
    write_settings({"externalTools": {"installerV2SkillRegistration": {
        "status": (
            ("reconciled" if upgrade_reconcile else "installer-applied")
            if eligible
            else ("reconcile-pending" if upgrade_reconcile else "installer-selected")
        ),
        "supportedNow": True,
        "selectedTools": tools,
        "autoDetectTools": auto_detect,
        "eligibleTools": eligible,
        "excludedTools": excluded,
        "lastBackendPolicy": "runtime-auto",
    }}})
    print(json.dumps(result, ensure_ascii=False, indent=2))
PY
  then
    progress_ok "$label"
  else
    progress_fail "$(installer_text step_failed) ${log_file}"
    warn "Actanara installation completed, but external Memory Search skill reconciliation failed; retry from Dashboard Settings."
  fi
}

maybe_fail_update_phase() {
  local phase="$1"
  if [[ "$UPDATE_TEST_MODE" != "1" ]]; then
    return 0
  fi
  if [[ -n "$UPDATE_TEST_HOOK" ]]; then
    if [[ "$UPDATE_TEST_HOOK" != /* || ! -x "$UPDATE_TEST_HOOK" ]]; then
      print -r -- "ACTANARA_INSTALL_TEST_HOOK must be an absolute executable path" >&2
      return 2
    fi
    "$UPDATE_TEST_HOOK" "$phase"
  fi
  if [[ -z "$UPDATE_TEST_FAIL_PHASE" ]]; then
    return 0
  fi
  if [[ "$UPDATE_TEST_FAIL_PHASE" == *[^a-z0-9-]* ]]; then
    print -r -- "ACTANARA_INSTALL_TEST_FAIL_PHASE must be a stable lowercase phase id" >&2
    return 2
  fi
  if [[ "$UPDATE_TEST_FAIL_PHASE" == "$phase" ]]; then
    print -r -- "synthetic update failure at phase ${phase}" >&2
    return 97
  fi
}

update_transaction_command() {
  "${PYTHON_BIN}" "${UPDATE_TRANSACTION_HELPER}" "$@"
}

dependency_profile_args() {
  local profile=""
  for profile in "${INSTALL_EXTRAS[@]}"; do
    print -r -- "--profile"
    print -r -- "$profile"
  done
}

inherit_upgrade_dependency_profiles() {
  if [[ "$UPGRADE" != "1" ]]; then
    return 0
  fi
  if [[ "$RAG_DETAIL_SET" == "1" ]]; then
    error "upgrade cannot change detailed RAG configuration; update Runtime Settings separately"
    return 2
  fi
  local profile_json=""
  local parsed=""
  local recovery_allowed=0
  local profile_command=(
    "${DEPENDENCY_CONTRACT_HELPER}"
    runtime-profiles
    --runtime "${RUNTIME_HOME}"
  )
  if [[ "$SOURCE_ONLY" != "1" ]]; then
    recovery_allowed=1
    profile_command+=(--allow-untrusted-active-venv)
  fi
  if [[ "$REPAIR_EXISTING" == "1" ]]; then
    profile_command+=(--allow-legacy-settings)
  fi
  profile_json="$(PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" \
    "${profile_command[@]}")" || return $?
  parsed="$(print -rn -- "$profile_json" | "${PYTHON_BIN}" -c '
import json
import sys

recovery_allowed = len(sys.argv) == 2 and sys.argv[1] == "1"
value = json.load(sys.stdin)
if (
    not isinstance(value, dict)
    or value.get("schemaVersion") != 1
    or value.get("status") != "ok"
    or set(value) != {"schemaVersion", "status", "profiles", "rag", "evidence"}
):
    raise SystemExit(2)
profiles = value.get("profiles")
rag = value.get("rag")
evidence = value.get("evidence")
if (
    not isinstance(profiles, list)
    or not isinstance(rag, dict)
    or set(rag) != {"enabled", "embeddingMode"}
    or not isinstance(evidence, dict)
    or set(evidence) != {
        "settingsSha256", "activeVenvTarget",
        "activeMarkerStatus", "activeMarkerSha256",
    }
):
    raise SystemExit(2)
enabled = rag.get("enabled")
mode = rag.get("embeddingMode")
expected = ["dashboard"]
if enabled is True:
    if mode not in {"local", "cloud"}:
        raise SystemExit(2)
    expected.append("rag-server")
    if mode == "local":
        expected.append("rag-local")
elif enabled is False:
    if mode is not None:
        raise SystemExit(2)
else:
    raise SystemExit(2)
dev_test = "dev-test" in profiles
if dev_test:
    expected.append("dev-test")
if profiles != sorted(expected):
    raise SystemExit(2)
settings_sha = evidence.get("settingsSha256")
venv_target = evidence.get("activeVenvTarget")
marker_status = evidence.get("activeMarkerStatus")
marker_sha = evidence.get("activeMarkerSha256")
if not isinstance(settings_sha, str) or not __import__("re").fullmatch(r"[0-9a-f]{64}", settings_sha):
    raise SystemExit(2)
if (
    not isinstance(venv_target, str)
    or not __import__("os").path.isabs(venv_target)
    or any(character in venv_target for character in "\0\r\n\t")
):
    raise SystemExit(2)
if marker_status == "missing":
    if marker_sha is not None:
        raise SystemExit(2)
    marker_sha_text = "none"
elif marker_status == "trusted":
    if not isinstance(marker_sha, str) or not __import__("re").fullmatch(r"[0-9a-f]{64}", marker_sha):
        raise SystemExit(2)
    marker_sha_text = marker_sha
elif marker_status == "unavailable":
    if not recovery_allowed or marker_sha is not None or dev_test:
        raise SystemExit(2)
    marker_sha_text = "none"
else:
    raise SystemExit(2)
print("\t".join((
    "1" if enabled else "0",
    mode or "none",
    "1" if dev_test else "0",
    settings_sha,
    venv_target,
    marker_status,
    marker_sha_text,
)))
' "$recovery_allowed")" || return 2
  local fields=("${(@ps:\t:)parsed}")
  if [[ "${#fields[@]}" != "7" ]]; then
    return 2
  fi
  local inherited_enabled="${fields[1]}"
  local inherited_mode="${fields[2]}"
  local inherited_dev_test="${fields[3]}"
  if [[ "$inherited_mode" == "none" ]]; then
    inherited_mode=""
  fi
  if [[ "$RAG_ENABLE_SET" == "1" && "$inherited_enabled" != "$ENABLE_RAG" ]]; then
    error "upgrade RAG enablement arguments conflict with Runtime Settings"
    return 2
  fi
  if [[ "$RAG_EMBEDDING_MODE_SET" == "1" && \
    ( "$inherited_enabled" != "1" || "$inherited_mode" != "$RAG_EMBEDDING_MODE" ) ]]; then
    error "upgrade RAG embedding mode argument conflicts with Runtime Settings"
    return 2
  fi
  case "${inherited_enabled}|${inherited_mode}" in
    "0|")
      ENABLE_RAG=0
      ;;
    "1|local"|"1|cloud")
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="${inherited_mode}"
      ;;
    *)
      return 2
      ;;
  esac
  if [[ "$DEV_TEST_SET" != "1" ]]; then
    ENABLE_DEV_TEST="$inherited_dev_test"
  fi
  DEPENDENCY_PROFILE_SETTINGS_SHA256="${fields[4]}"
  DEPENDENCY_PROFILE_ACTIVE_VENV_TARGET="${fields[5]}"
  DEPENDENCY_PROFILE_MARKER_STATUS="${fields[6]}"
  if [[ "${fields[7]}" == "none" ]]; then
    DEPENDENCY_PROFILE_MARKER_SHA256=""
  else
    DEPENDENCY_PROFILE_MARKER_SHA256="${fields[7]}"
  fi
  if [[ "$DEPENDENCY_PROFILE_MARKER_STATUS" == "unavailable" ]]; then
    FORCE_REBUILD=1
  fi
  # Upgrade dependency selection never requests a Settings rewrite.  This is
  # also compatible with v1.0.1, whose CLI forwards matching RAG profile flags.
  RAG_SET=0
  RAG_ENABLE_SET=0
  RAG_EMBEDDING_MODE_SET=0
  if [[ "$DEPENDENCY_PROFILE_MARKER_STATUS" == "unavailable" ]]; then
    DEPENDENCY_PROFILE_SOURCE="runtime-settings-recovery"
  else
    DEPENDENCY_PROFILE_SOURCE="runtime-settings+active-marker"
  fi
}

parse_dependency_plan() {
  local payload="$1"
  print -rn -- "$payload" | "${PYTHON_BIN}" -c '
import json
import os
import re
import sys

try:
    value = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    raise SystemExit(2)
required = {
    "schemaVersion", "status", "updateMode", "reason",
    "dependencyFingerprint", "reusesRuntimeVenv",
    "plannedDependenciesInstalled", "offline", "cacheUsed",
    "cache", "failBeforeServiceStop", "selectedPython",
    "pythonSelectionReason",
}
if not isinstance(value, dict) or not required.issubset(value):
    raise SystemExit(2)
mode = value["updateMode"]
reason = value["reason"]
fingerprint = value["dependencyFingerprint"]
selected_python = value["selectedPython"]
selection_reason = value["pythonSelectionReason"]
if mode not in {"reuse-existing-venv", "rebuild-candidate-venv"}:
    raise SystemExit(2)
if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9-]+", reason):
    raise SystemExit(2)
if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
    raise SystemExit(2)
if (
    not isinstance(selected_python, str)
    or not os.path.isabs(selected_python)
    or any(character in selected_python for character in "\0\r\n\t")
):
    raise SystemExit(2)
if not isinstance(selection_reason, str) or not re.fullmatch(r"[a-z0-9-]+", selection_reason):
    raise SystemExit(2)
boolean_fields = (
    "reusesRuntimeVenv", "plannedDependenciesInstalled", "cacheUsed",
    "failBeforeServiceStop",
)
if any(type(value[field]) is not bool for field in boolean_fields):
    raise SystemExit(2)
print("\t".join((
    mode,
    reason,
    fingerprint,
    "1" if value["reusesRuntimeVenv"] else "0",
    "1" if value["plannedDependenciesInstalled"] else "0",
    "1" if value["cacheUsed"] else "0",
    "1" if value["failBeforeServiceStop"] else "0",
    selected_python,
    selection_reason,
)))
'
}

parse_dependency_error() {
  local payload="$1"
  print -rn -- "$payload" | "${PYTHON_BIN}" -c '
import json
import re
import sys

try:
    value = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    raise SystemExit(2)
if not isinstance(value, dict) or set(value) != {"schemaVersion", "status", "error"}:
    raise SystemExit(2)
error = value.get("error")
if (
    value.get("schemaVersion") != 1
    or value.get("status") != "error"
    or not isinstance(error, dict)
    or set(error) != {"code", "message"}
):
    raise SystemExit(2)
code = error.get("code")
message = error.get("message")
if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9-]+", code):
    raise SystemExit(2)
if not isinstance(message, str) or not message or any(character in message for character in "\0\r\n\t"):
    raise SystemExit(2)
print(code)
'
}

run_dependency_update_plan() {
  local candidate_source="$1"
  local mode="auto"
  local plan_json=""
  local plan_rc=0
  local parsed=""
  local profiles=()
  profiles=("${(@f)$(dependency_profile_args)}")
  if [[ "$SOURCE_ONLY" == "1" ]]; then
    mode="explicit-source-only"
  elif [[ "$FORCE_REBUILD" == "1" ]]; then
    mode="force-rebuild"
  fi
  local command=(
    "${PYTHON_BIN}" "${candidate_source}/install/dependency_contract.py" plan
    --lock "${candidate_source}/install/runtime-dependencies.lock.json"
    --pyproject "${candidate_source}/pyproject.toml"
    "${profiles[@]}"
    --runtime "${RUNTIME_HOME}"
    --cache-root "${RUNTIME_HOME}/app/dependency-cache/v1"
    --mode "$mode"
  )
  if [[ "$PYTHON_SET" == "1" || "$FORCE_REBUILD" == "1" ]]; then
    command+=(--python "${PYTHON_BIN}")
  fi
  if [[ "$OFFLINE" == "1" ]]; then
    command+=(--offline)
  fi
  UPDATE_RESULT_STAGE="dependency-plan"
  if plan_json="$(PYTHONDONTWRITEBYTECODE=1 "${command[@]}" 2>&1)"; then
    plan_rc=0
  else
    plan_rc=$?
  fi
  if [[ -n "$plan_json" ]]; then
    local parse_rc=0
    if parsed="$(parse_dependency_plan "$plan_json")"; then
      parse_rc=0
    else
      parse_rc=$?
    fi
    if [[ "$parse_rc" != "0" ]]; then
      local error_code=""
      if [[ "$plan_rc" != "0" ]] && error_code="$(parse_dependency_error "$plan_json")"; then
        if [[ "$SOURCE_ONLY" == "1" ]]; then
          UPDATE_MODE="reuse-existing-venv"
        else
          UPDATE_MODE="rebuild-candidate-venv"
        fi
        UPDATE_REASON="$error_code"
        UPDATE_REUSES_VENV=0
        UPDATE_PLANNED_DEPENDENCIES_INSTALL=0
        DEPENDENCY_PLAN_CACHE_HIT=0
        DEPENDENCY_PLAN_FAIL_BEFORE_STOP=1
        error "runtime dependency plan blocked before service stop: ${error_code}"
        return "$plan_rc"
      fi
      UPDATE_MODE="rebuild-candidate-venv"
      UPDATE_REASON="dependency-plan-output-invalid"
      return 70
    fi
    local fields=("${(@ps:\t:)parsed}")
    if [[ "${#fields[@]}" != "9" ]]; then
      UPDATE_MODE="rebuild-candidate-venv"
      UPDATE_REASON="dependency-plan-output-invalid"
      return 70
    fi
    UPDATE_MODE="${fields[1]}"
    UPDATE_REASON="${fields[2]}"
    UPDATE_DEPENDENCY_FINGERPRINT="${fields[3]}"
    UPDATE_REUSES_VENV="${fields[4]}"
    UPDATE_PLANNED_DEPENDENCIES_INSTALL="${fields[5]}"
    DEPENDENCY_PLAN_CACHE_HIT="${fields[6]}"
    DEPENDENCY_PLAN_FAIL_BEFORE_STOP="${fields[7]}"
    UPDATE_DEPENDENCY_PYTHON="${fields[8]}"
    UPDATE_PYTHON_SELECTION_REASON="${fields[9]}"
  else
    UPDATE_MODE="rebuild-candidate-venv"
    UPDATE_REASON="dependency-contract-invalid-or-unsupported"
  fi
  if [[ "$plan_rc" != "0" ]]; then
    return "$plan_rc"
  fi
  if [[ "$UPDATE_MODE" == "reuse-existing-venv" ]]; then
    UPDATE_REUSES_VENV=1
    UPDATE_PLANNED_DEPENDENCIES_INSTALL=0
  else
    UPDATE_REUSES_VENV=0
    UPDATE_PLANNED_DEPENDENCIES_INSTALL=1
  fi
  return 0
}

materialize_update_dependency_cache() {
  if [[ "$UPDATE_MODE" != "rebuild-candidate-venv" || "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  local source_root="$1"
  local profiles=()
  profiles=("${(@f)$(dependency_profile_args)}")
  local command=(
    "${PYTHON_BIN}" "${source_root}/install/dependency_contract.py" materialize-cache
    --lock "${source_root}/install/runtime-dependencies.lock.json"
    --pyproject "${source_root}/pyproject.toml"
    "${profiles[@]}"
    --python "${UPDATE_DEPENDENCY_PYTHON}"
    --cache-root "${RUNTIME_HOME}/app/dependency-cache/v1"
  )
  if [[ "$OFFLINE" == "1" ]]; then
    command+=(--offline)
  fi
  UPDATE_RESULT_STAGE="dependency-cache"
  run_cmd "${command[@]}"
}

install_candidate_locked_dependencies() {
  local source_root="$1"
  local venv_root="$2"
  local profiles=()
  profiles=("${(@f)$(dependency_profile_args)}")
  UPDATE_RESULT_STAGE="candidate-dependencies"
  run_update_candidate_cmd candidate-locked-dependency-install \
    "${PYTHON_BIN}" "${source_root}/install/dependency_contract.py" install \
    --lock "${source_root}/install/runtime-dependencies.lock.json" \
    --pyproject "${source_root}/pyproject.toml" \
    "${profiles[@]}" \
    --cache-root "${RUNTIME_HOME}/app/dependency-cache/v1" \
    --venv-python "${venv_root}/bin/python"
  UPDATE_DEPENDENCIES_INSTALLED=1
  UPDATE_CACHE_USED=1
  run_update_candidate_cmd candidate-dependency-manifest-write \
    "${PYTHON_BIN}" "${source_root}/install/dependency_contract.py" write-marker \
    --lock "${source_root}/install/runtime-dependencies.lock.json" \
    --pyproject "${source_root}/pyproject.toml" \
    "${profiles[@]}" \
    --python "${venv_root}/bin/python" \
    --venv "${venv_root}"
  run_update_candidate_cmd candidate-dependency-manifest-verify \
    "${PYTHON_BIN}" "${source_root}/install/dependency_contract.py" verify-marker \
    --lock "${source_root}/install/runtime-dependencies.lock.json" \
    --pyproject "${source_root}/pyproject.toml" \
    "${profiles[@]}" \
    --python "${venv_root}/bin/python" \
    --venv "${venv_root}"
}

prepare_fresh_dependency_cache() {
  local source_root="$1"
  local profiles=()
  profiles=("${(@f)$(dependency_profile_args)}")
  local status_json=""
  status_json="$(PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" \
    "${source_root}/install/dependency_contract.py" cache-status \
    --lock "${source_root}/install/runtime-dependencies.lock.json" \
    --pyproject "${source_root}/pyproject.toml" \
    "${profiles[@]}" \
    --python "${PYTHON_BIN}" \
    --cache-root "${RUNTIME_HOME}/app/dependency-cache/v1")"
  local cache_status=""
  cache_status="$(print -rn -- "$status_json" | "${PYTHON_BIN}" -c '
import json
import sys
value = json.load(sys.stdin)
status = value.get("status") if isinstance(value, dict) else None
if status not in {"hit", "miss"}:
    raise SystemExit(2)
print(status)
')" || {
    error "runtime dependency cache status returned an invalid result"
    return 70
  }
  if [[ "$OFFLINE" == "1" && "$cache_status" != "hit" ]]; then
    error "offline fresh install requires a complete trusted runtime dependency wheelhouse"
    return 3
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    log "fresh dependency cache plan: ${cache_status}"
    return 0
  fi
  local materialize_command=(
    "${PYTHON_BIN}" "${source_root}/install/dependency_contract.py" materialize-cache
    --lock "${source_root}/install/runtime-dependencies.lock.json"
    --pyproject "${source_root}/pyproject.toml"
    "${profiles[@]}"
    --python "${PYTHON_BIN}"
    --cache-root "${RUNTIME_HOME}/app/dependency-cache/v1"
  )
  if [[ "$OFFLINE" == "1" ]]; then
    materialize_command+=(--offline)
  fi
  run_cmd "${materialize_command[@]}"
}

install_fresh_locked_dependencies() {
  local source_root="$1"
  local venv_root="$2"
  local profiles=()
  profiles=("${(@f)$(dependency_profile_args)}")
  run_cmd "${PYTHON_BIN}" "${source_root}/install/dependency_contract.py" install \
    --lock "${source_root}/install/runtime-dependencies.lock.json" \
    --pyproject "${source_root}/pyproject.toml" \
    "${profiles[@]}" \
    --cache-root "${RUNTIME_HOME}/app/dependency-cache/v1" \
    --venv-python "${venv_root}/bin/python"
  run_cmd "${PYTHON_BIN}" "${source_root}/install/dependency_contract.py" write-marker \
    --lock "${source_root}/install/runtime-dependencies.lock.json" \
    --pyproject "${source_root}/pyproject.toml" \
    "${profiles[@]}" \
    --python "${venv_root}/bin/python" \
    --venv "${venv_root}"
  run_cmd "${PYTHON_BIN}" "${source_root}/install/dependency_contract.py" verify-marker \
    --lock "${source_root}/install/runtime-dependencies.lock.json" \
    --pyproject "${source_root}/pyproject.toml" \
    "${profiles[@]}" \
    --python "${venv_root}/bin/python" \
    --venv "${venv_root}"
}

run_update_candidate_cmd() {
  local phase="$1"
  shift
  run_cmd \
    "${PYTHON_BIN}" "${UPDATE_TRANSACTION_HELPER}" \
    run-candidate-command \
    --state "${UPDATE_TRANSACTION_JOURNAL}" \
    --phase "$phase" \
    -- /usr/bin/env -i \
    "PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    "USER=actanara-candidate" \
    "LOGNAME=actanara-candidate" \
    "SHELL=/bin/zsh" \
    "LC_ALL=C" \
    "LANG=C" \
    "ACTANARA_HOME=${UPDATE_VALIDATION_RUNTIME}" \
    "ACTANARA_LOCATION_FILE=${UPDATE_VALIDATION_RUNTIME}/location.json" \
    "HOME=${UPDATE_VALIDATION_RUNTIME}/home" \
    "TMPDIR=${UPDATE_VALIDATION_RUNTIME}/tmp" \
    "XDG_CONFIG_HOME=${UPDATE_VALIDATION_RUNTIME}/xdg" \
    "PIP_CONFIG_FILE=/dev/null" \
    "PIP_CACHE_DIR=${UPDATE_VALIDATION_RUNTIME}/pip-cache" \
    "PYTHONNOUSERSITE=1" \
    "ACTANARA_SECRET_BACKEND=memory" \
    "PYTHONDONTWRITEBYTECODE=1" \
    "$@"
}

prepare_update_validation_runtime() {
  local reserved=""
  reserved="$(update_transaction_command reserve-artifact \
    --state "${UPDATE_TRANSACTION_JOURNAL}" \
    --kind validation-runtime)"
  if [[ "$reserved" != "$UPDATE_VALIDATION_RUNTIME" ]]; then
    error "transaction validation Runtime reservation returned an unexpected path"
    return 1
  fi
  mkdir -p \
    "${UPDATE_VALIDATION_RUNTIME}/home" \
    "${UPDATE_VALIDATION_RUNTIME}/tmp" \
    "${UPDATE_VALIDATION_RUNTIME}/xdg" \
    "${UPDATE_VALIDATION_RUNTIME}/pip-cache"
}

update_exit_handler() {
  local original_rc="$1"
  local rollback_rc=0
  trap - ZERR INT TERM HUP
  if [[ "$UPDATE_TRANSACTION_ACTIVE" == "1" && "$UPDATE_COMMITTED" != "1" && "$UPDATE_ROLLBACK_RUNNING" != "1" ]]; then
    UPDATE_ROLLBACK_RUNNING=1
    set +e
    update_transaction_command rollback --state "${UPDATE_TRANSACTION_JOURNAL}"
    rollback_rc=$?
    set -e
    if [[ "$rollback_rc" -ne 0 ]]; then
      UPDATE_REASON="update-rollback-incomplete"
      UPDATE_RESULT_STAGE="rollback-incomplete"
      UPDATE_ROLLBACK_COMPLETE=0
      UPDATE_STATE_CERTAIN=0
      UPDATE_SOURCE_UPDATED=-1
      UPDATE_PLISTS_NORMALIZED=-1
      error "Actanara update rollback was incomplete; inspect the preserved transaction journal: ${UPDATE_TRANSACTION_JOURNAL}"
      original_rc=70
    else
      UPDATE_SOURCE_UPDATED=0
      UPDATE_PLISTS_NORMALIZED=0
      UPDATE_ROLLBACK_COMPLETE=1
      UPDATE_REASON="update-failed-rolled-back"
      UPDATE_RESULT_STAGE="rollback-complete"
      error "Actanara update failed; rollback restored the prior source, venv, control files, and services"
    fi
  elif [[ "$REPAIR_EXISTING" == "1" && "$UPDATE_COMMITTED" == "1" && "$REPAIR_CONFIGURATION_COMPLETE" != "1" && -n "$REPAIR_BACKUP_DIR" ]]; then
    error "repair configuration is incomplete"
    print -r -- "$(installer_text repair_backup): ${REPAIR_BACKUP_DIR}" >&2
  fi
  # zsh does not reliably invoke the outer EXIT trap when this handler exits
  # from inside ZERR/signal processing. Emit here so every recoverable
  # post-begin failure has the same truthful machine-readable result contract.
  emit_update_result "$original_rc"
  exit "$original_rc"
}

begin_update_transaction() {
  local uid="0"
  local launchctl_path="/usr/bin/true"
  local mode="upgrade"
  local begin_rc=0
  local profile_evidence_args=(
    --expected-settings-sha256 "${DEPENDENCY_PROFILE_SETTINGS_SHA256}"
  )
  UPDATE_TRANSACTION_ID="$(date +%Y%m%dT%H%M%S)-$$-${RANDOM}"
  if [[ "$UPDATE_REUSES_VENV" == "1" ]]; then
    mode="source-only"
  elif [[ "$REPAIR_EXISTING" == "1" ]]; then
    mode="repair"
  fi
  if [[ "$PLATFORM" == "Darwin" ]]; then
    resolve_launchctl_bin
    if [[ -z "$LAUNCHCTL_BIN" ]]; then
      error "launchctl not found; update transaction cannot capture managed service state"
      return 1
    fi
    launchctl_path="$LAUNCHCTL_BIN"
    uid="$("$ID_BIN" -u 2>/dev/null || print -r -- "")"
    if [[ -z "$uid" ]]; then
      error "current uid could not be determined; update transaction cannot capture managed service state"
      return 1
    fi
  fi
  if [[ "$DEPENDENCY_PROFILE_MARKER_STATUS" == "unavailable" ]]; then
    profile_evidence_args+=(--settings-only-profile-evidence)
  else
    profile_evidence_args+=(
      --expected-active-venv-target "${DEPENDENCY_PROFILE_ACTIVE_VENV_TARGET}"
      --expected-active-marker-status "${DEPENDENCY_PROFILE_MARKER_STATUS}"
    )
    if [[ -n "$DEPENDENCY_PROFILE_MARKER_SHA256" ]]; then
      profile_evidence_args+=(
        --expected-active-marker-sha256 "$DEPENDENCY_PROFILE_MARKER_SHA256"
      )
    fi
  fi
  update_transaction_command recover --runtime "${RUNTIME_HOME}"
  set +e
  UPDATE_TRANSACTION_JOURNAL="$(update_transaction_command begin \
    --runtime "${RUNTIME_HOME}" \
    --home "${HOME}" \
    --source-pointer "${DEPLOY_SOURCE_ROOT}" \
    --venv-pointer "${VENV_DIR}" \
    "${profile_evidence_args[@]}" \
    --mode "${mode}" \
    --tx-id "${UPDATE_TRANSACTION_ID}" \
    --owner-pid "$$" \
    --platform "${PLATFORM}" \
    --launchctl "${launchctl_path}" \
    --uid "${uid}")"
  begin_rc=$?
  set -e
  if [[ "$begin_rc" != "0" || -z "$UPDATE_TRANSACTION_JOURNAL" ]]; then
    if [[ "$begin_rc" == "0" ]]; then
      begin_rc=70
    fi
    UPDATE_TRANSACTION_JOURNAL=""
    return "$begin_rc"
  fi
  UPDATE_TRANSACTION_DIR="${UPDATE_TRANSACTION_JOURNAL:h}"
  UPDATE_VALIDATION_RUNTIME="${UPDATE_TRANSACTION_DIR}/candidate-runtime"
  UPDATE_TRANSACTION_ACTIVE=1
}

record_update_candidate() {
  local kind="$1"
  local candidate="$2"
  update_transaction_command record-candidate \
    --state "${UPDATE_TRANSACTION_JOURNAL}" \
    --kind "$kind" \
    --candidate "$candidate"
}

stage_update_candidate_venv() {
  local active_source="${DEPLOY_SOURCE_ROOT}"
  local active_venv_dir="${VENV_DIR}"
  local active_venv_py="${VENV_PY}"
  local candidate_spec="${STAGED_RELEASE_TARGET}"
  local candidate_root="${RUNTIME_HOME}/app/venvs"
  UPDATE_STAGED_VENV="$(update_transaction_command reserve-artifact \
    --state "${UPDATE_TRANSACTION_JOURNAL}" \
    --kind venv)"
  if [[ "$UPDATE_STAGED_VENV" != "${candidate_root}/${UPDATE_TRANSACTION_ID}" ]]; then
    error "transaction venv reservation returned an unexpected path"
    return 1
  fi
  run_update_candidate_cmd candidate-venv-create \
    "${UPDATE_DEPENDENCY_PYTHON}" -m venv "${UPDATE_STAGED_VENV}"
  VENV_DIR="${UPDATE_STAGED_VENV}"
  VENV_PY="${UPDATE_STAGED_VENV}/bin/python"
  DEPLOY_SOURCE_ROOT="${STAGED_RELEASE_TARGET}"
  install_candidate_locked_dependencies "${STAGED_RELEASE_TARGET}" "${UPDATE_STAGED_VENV}"
  run_runtime_dependency_gate
  DEPLOY_SOURCE_ROOT="${active_source}"
  VENV_DIR="${active_venv_dir}"
  VENV_PY="${active_venv_py}"
  record_update_candidate venv "${UPDATE_STAGED_VENV}"
  maybe_fail_update_phase candidate-venv
}

run_source_only_candidate_gate() {
  local active_source="${DEPLOY_SOURCE_ROOT}"
  local missing_file="${UPDATE_TRANSACTION_DIR}/source-only-dependency-gate-missing.txt"
  local log_file="$(installer_log_file)"
  if ! PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" - "${RUNTIME_HOME}" "${VENV_DIR}" <<'PY'
import os
import sys
from pathlib import Path

runtime = Path(os.path.abspath(sys.argv[1]))
pointer = Path(os.path.abspath(sys.argv[2]))
store = runtime / "app" / "venvs"
try:
    if pointer != runtime / ".venv" or not pointer.is_symlink():
        raise ValueError
    raw_target = Path(os.readlink(pointer))
    if raw_target.is_absolute() or raw_target.parts[:2] != ("app", "venvs") or len(raw_target.parts) != 3:
        raise ValueError
    if any(part in {"", ".", ".."} for part in raw_target.parts):
        raise ValueError
    lexical_target = pointer.parent / raw_target
    if lexical_target.is_symlink() or not lexical_target.is_dir() or lexical_target.parent != store:
        raise ValueError
    if store.is_symlink() or lexical_target.resolve(strict=True).parent != store.resolve(strict=True):
        raise ValueError
    if not (pointer / "bin" / "python").is_file():
        raise ValueError
except (OSError, RuntimeError, ValueError):
    raise SystemExit("source-only update requires a relative managed Runtime venv pointer; run a full upgrade first")
PY
  then
    return 1
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    error "source-only update requires the existing runtime venv: ${VENV_PY}"
    return 1
  fi
  DEPLOY_SOURCE_ROOT="${STAGED_RELEASE_TARGET}"
  mkdir -p "${log_file:h}"
  progress_start "$(installer_text step_check_update)"
  if ! run_runtime_dependency_check "$missing_file" >> "$log_file" 2>&1; then
    DEPLOY_SOURCE_ROOT="${active_source}"
    progress_fail "$(installer_text update_needs_full_install)"
    return 1
  fi
  DEPLOY_SOURCE_ROOT="${active_source}"
  progress_ok "$(installer_text step_check_update)"
}

run_update_candidate_doctor() {
  log "Atomic upgrade candidate doctor"
  run_json_cmd "Candidate installer doctor" \
    "${PYTHON_BIN}" "${UPDATE_TRANSACTION_HELPER}" \
    run-candidate-command \
    --state "${UPDATE_TRANSACTION_JOURNAL}" \
    --phase candidate-doctor \
    -- \
    /usr/bin/env -i \
      PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin \
      USER=actanara-candidate \
      LOGNAME=actanara-candidate \
      SHELL=/bin/zsh \
      LC_ALL=C \
      LANG=C \
      ACTANARA_HOME="${RUNTIME_HOME}" \
      ACTANARA_LOCATION_FILE="${LOCATION_FILE}" \
      HOME="${UPDATE_VALIDATION_RUNTIME}/home" \
      TMPDIR="${UPDATE_VALIDATION_RUNTIME}/tmp" \
      XDG_CONFIG_HOME="${UPDATE_VALIDATION_RUNTIME}/xdg" \
      PIP_CONFIG_FILE=/dev/null \
      PIP_CACHE_DIR="${UPDATE_VALIDATION_RUNTIME}/pip-cache" \
      PYTHONNOUSERSITE=1 \
      ACTANARA_SECRET_BACKEND=memory \
      PYTHONPATH="${DEPLOY_SOURCE_ROOT}:${DEPLOY_SOURCE_ROOT}/src:${DEPLOY_SOURCE_ROOT}/src/dashboard" \
      PYTHONDONTWRITEBYTECODE=1 \
      "${VENV_PY}" -m data_foundation.cli \
      doctor --installer \
      --runtime "${RUNTIME_HOME}" \
      --json
}

clean_staged_candidate_build_artifacts() {
  if [[ -z "$STAGED_RELEASE_TARGET" || ! -d "$STAGED_RELEASE_TARGET" ]]; then
    return 0
  fi
  if [[ "$UPDATE_TRANSACTION_ACTIVE" == "1" ]]; then
    update_transaction_command clean-source-build-artifacts \
      --state "${UPDATE_TRANSACTION_JOURNAL}"
    return 0
  fi
  # The clean staged payload excludes these paths. Any occurrence here was
  # produced by candidate wheel/import validation and is transaction-owned.
  rm -rf "${STAGED_RELEASE_TARGET}/build" "${STAGED_RELEASE_TARGET}/dist"
  find -P "${STAGED_RELEASE_TARGET}" \
    \( -type d -name "__pycache__" -o -type d -name "*.egg-info" \) \
    -prune -exec rm -rf {} + 2>/dev/null || true
}

capture_update_mutable_state() {
  local shell_profile=""
  shell_profile="$(resolve_shell_path_file)"
  update_transaction_command capture-mutable \
    --state "${UPDATE_TRANSACTION_JOURNAL}" \
    --location "${LOCATION_FILE}" \
    --cli-shim "${CLI_SHIM}" \
    --user-cli-shim "${USER_CLI_SHIM}" \
    --desktop-link "${DESKTOP_DIARY_LINK}" \
    --shell-profile "${shell_profile}"
  UPDATE_MUTABLE_STATE_CAPTURED=1
}

commit_update_transaction() {
  update_transaction_command commit --state "${UPDATE_TRANSACTION_JOURNAL}"
  UPDATE_COMMITTED=1
  UPDATE_TRANSACTION_ACTIVE=0
}

record_service_normalization_result() {
  local changed=""
  changed="$(PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" - "${UPDATE_TRANSACTION_JOURNAL}" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(os.path.abspath(sys.argv[1]))
if path.is_symlink() or not path.is_file():
    raise SystemExit(2)
try:
    state = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    raise SystemExit(2)
services = state.get("services") if isinstance(state, dict) else None
if not isinstance(services, list) or state.get("servicePlistNormalizationComplete") is not True:
    raise SystemExit(2)
print("1" if any(
    isinstance(service, dict) and service.get("plistNormalizationRequired") is True
    for service in services
) else "0")
PY
)" || return 1
  if [[ "$changed" != "0" && "$changed" != "1" ]]; then
    return 1
  fi
  UPDATE_PLISTS_NORMALIZED="$changed"
}

inherit_repair_service_state() {
  if [[ "$REPAIR_EXISTING" != "1" || "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  local parsed=""
  parsed="$(PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" - "${UPDATE_TRANSACTION_JOURNAL}" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(os.path.abspath(sys.argv[1]))
if path.is_symlink() or not path.is_file():
    raise SystemExit(2)
state = json.loads(path.read_text(encoding="utf-8"))
services = state.get("services") if isinstance(state, dict) else None
if state.get("mode") != "repair" or state.get("status") != "committed" or not isinstance(services, list):
    raise SystemExit(2)
loaded = {"scheduler": False, "dashboard": False, "rag": False}
for service in services:
    if not isinstance(service, dict) or type(service.get("loaded")) is not bool:
        raise SystemExit(2)
    if not service["loaded"]:
        continue
    kind = service.get("kind")
    if kind in {"scheduler-pipeline", "scheduler-aggregation"}:
        loaded["scheduler"] = True
    elif kind in {"dashboard", "watchdog"}:
        loaded["dashboard"] = True
    elif kind == "rag":
        loaded["rag"] = True

runtime = Path(str(state.get("runtime") or ""))
settings_path = runtime / "config" / "settings.json"
if settings_path.is_symlink() or not settings_path.is_file():
    raise SystemExit(2)
settings = json.loads(settings_path.read_text(encoding="utf-8"))
if not isinstance(settings, dict):
    raise SystemExit(2)

features = settings.get("features") if isinstance(settings.get("features"), dict) else {}
schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}
dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
dashboard_server = dashboard.get("server") if isinstance(dashboard.get("server"), dict) else {}
rag = settings.get("rag") if isinstance(settings.get("rag"), dict) else {}
rag_server = rag.get("server") if isinstance(rag.get("server"), dict) else {}


def explicit_bool(mapping, key):
    value = mapping.get(key)
    return value if type(value) is bool else None


def resolve_desired(values, fallback):
    explicit = [value for value in values if type(value) is bool]
    if False in explicit:
        return False
    if True in explicit:
        return True
    return fallback


def resolve_preferred(values, fallback):
    for value in values:
        if type(value) is bool:
            return value
    return fallback


rag_product_enabled = resolve_preferred(
    [
        explicit_bool(rag, "enabled"),
        explicit_bool(features, "rag"),
    ],
    loaded["rag"],
)


desired = {
    "scheduler": resolve_desired(
        [explicit_bool(schedule, "enabled")],
        loaded["scheduler"],
    ),
    "dashboard": resolve_desired(
        [
            explicit_bool(features, "dashboard"),
            explicit_bool(dashboard_server, "enabled"),
        ],
        loaded["dashboard"],
    ),
    "rag": resolve_preferred(
        [
            explicit_bool(rag_server, "enabled"),
        ],
        rag_product_enabled,
    ),
}
print("\t".join("1" if desired[key] else "0" for key in ("scheduler", "dashboard", "rag")))
PY
)" || return 2
  local fields=("${(@ps:\t:)parsed}")
  if [[ "${#fields[@]}" != "3" ]] || [[ "${fields[*]}" == *[^01\ ]* ]]; then
    return 2
  fi
  if [[ "$NO_SCHEDULER_SET" != "1" ]]; then
    NO_SCHEDULER=$(( 1 - fields[1] ))
  fi
  if [[ "$NO_DASHBOARD_SERVER_SET" != "1" ]]; then
    NO_DASHBOARD_SERVER=$(( 1 - fields[2] ))
  fi
  REPAIR_RAG_SERVICE_ENABLED="${fields[3]}"
  if [[ "$ENABLE_RAG" != "1" ]]; then
    REPAIR_RAG_SERVICE_ENABLED=0
  fi
}

record_services_stopped_result() {
  local stopped=""
  stopped="$(PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BIN}" - "${UPDATE_TRANSACTION_JOURNAL}" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(os.path.abspath(sys.argv[1]))
if path.is_symlink() or not path.is_file():
    raise SystemExit(2)
try:
    state = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    raise SystemExit(2)
services = state.get("services") if isinstance(state, dict) else None
if not isinstance(services, list) or state.get("status") != "stopped":
    raise SystemExit(2)
print("1" if any(
    isinstance(service, dict) and service.get("stoppedByTransaction") is True
    for service in services
) else "0")
PY
)" || return 1
  if [[ "$stopped" != "0" && "$stopped" != "1" ]]; then
    return 1
  fi
  UPDATE_SERVICES_STOPPED="$stopped"
}

run_guarded_update_transaction() {
  if [[ "$DRY_RUN" == "1" ]]; then
    UPDATE_CACHE_USED="${DEPENDENCY_PLAN_CACHE_HIT}"
    UPDATE_RESULT_STAGE="plan"
    return 0
  fi
  if [[ -n "$LLM_API_KEY_VALUE" ]]; then
    error "credential rotation is not part of an atomic upgrade"
    return 2
  fi
  if ! materialize_update_dependency_cache "${SOURCE_ROOT}"; then
    UPDATE_REASON="dependency-cache-materialization-failed"
    UPDATE_RESULT_STAGE="dependency-cache"
    return 1
  fi
  local expected_mode="${UPDATE_MODE}"
  local expected_fingerprint="${UPDATE_DEPENDENCY_FINGERPRINT}"
  local expected_python="${UPDATE_DEPENDENCY_PYTHON}"
  UPDATE_RESULT_STAGE="transaction-begin"
  if ! begin_update_transaction; then
    UPDATE_REASON="update-transaction-begin-failed"
    return 1
  fi
  # ZERR and signal traps are scoped to this outer transaction driver. This
  # avoids changing normal installer control flow while still covering every
  # non-zero post-begin phase under zsh ERR_EXIT semantics.
  trap 'update_exit_handler $?' ZERR
  trap 'update_exit_handler 130' INT
  trap 'update_exit_handler 143' TERM
  trap 'update_exit_handler 129' HUP
  prepare_update_validation_runtime
  maybe_fail_update_phase prior-captured
  stage_runtime_source
  record_update_candidate source "${STAGED_RELEASE_TARGET}"
  local migration_compatibility_args=(
    verify-migration-compatibility
    --state "${UPDATE_TRANSACTION_JOURNAL}"
  )
  if [[ "$REPAIR_EXISTING" == "1" ]]; then
    migration_compatibility_args+=(--allow-legacy-repair)
  fi
  update_transaction_command "${migration_compatibility_args[@]}"
  maybe_fail_update_phase migration-compatibility-verified
  run_dependency_update_plan "${STAGED_RELEASE_TARGET}"
  if [[ \
    "$UPDATE_MODE" != "$expected_mode" \
    || "$UPDATE_DEPENDENCY_FINGERPRINT" != "$expected_fingerprint" \
    || "$UPDATE_DEPENDENCY_PYTHON" != "$expected_python" \
  ]]; then
    UPDATE_REASON="staged-dependency-contract-changed-after-plan"
    return 1
  fi
  if [[ "$UPDATE_REUSES_VENV" == "1" ]] && staged_source_matches_active; then
    update_transaction_command rollback --state "${UPDATE_TRANSACTION_JOURNAL}"
    UPDATE_TRANSACTION_ACTIVE=0
    UPDATE_COMMITTED=1
    UPDATE_NOOP=1
    UPDATE_MODE="no-op"
    UPDATE_REASON="source-and-dependency-contract-unchanged"
    UPDATE_RESULT_STAGE="complete"
    return 0
  fi
  if [[ "$UPDATE_REUSES_VENV" == "1" ]]; then
    run_source_only_candidate_gate
  else
    stage_update_candidate_venv
  fi
  clean_staged_candidate_build_artifacts
  update_transaction_command cleanup-validation-runtime --state "${UPDATE_TRANSACTION_JOURNAL}"
  maybe_fail_update_phase source-staged
  maybe_fail_update_phase payload-scanned
  update_transaction_command stop --state "${UPDATE_TRANSACTION_JOURNAL}"
  record_services_stopped_result
  maybe_fail_update_phase services-stopped
  capture_update_mutable_state
  update_transaction_command normalize-service-plists --state "${UPDATE_TRANSACTION_JOURNAL}"
  record_service_normalization_result
  update_transaction_command promote --state "${UPDATE_TRANSACTION_JOURNAL}"
  UPDATE_SOURCE_UPDATED=1
  maybe_fail_update_phase source-promoted
  if [[ "$UPDATE_REUSES_VENV" != "1" ]]; then
    maybe_fail_update_phase venv-promoted
  fi
  if [[ "$REPAIR_EXISTING" == "1" ]]; then
    update_transaction_command commit-repair --state "${UPDATE_TRANSACTION_JOURNAL}"
    UPDATE_COMMITTED=1
    UPDATE_TRANSACTION_ACTIVE=0
    UPDATE_MODE="repair"
    UPDATE_RESULT_STAGE="repair-configuration"
    REPAIR_BACKUP_DIR="${UPDATE_TRANSACTION_DIR}/backups"
    return 0
  fi
  update_transaction_command restore-services --state "${UPDATE_TRANSACTION_JOURNAL}"
  maybe_fail_update_phase services-restored
  if [[ "$SOURCE_ONLY" != "1" ]]; then
    maybe_fail_update_phase candidate-doctor-started
    run_update_candidate_doctor
    maybe_fail_update_phase candidate-doctor-passed
  fi
  update_transaction_command verify --state "${UPDATE_TRANSACTION_JOURNAL}"
  maybe_fail_update_phase candidate-verified
  commit_update_transaction
  UPDATE_RESULT_STAGE="complete"
}

print_useful_commands() {
  print -r -- "$(installer_text next_steps)"
  print -r -- "  actanara"
  print -r -- "  actanara doctor"
  if [[ "$NO_DASHBOARD_SERVER" != "1" ]]; then
    print -r -- "  actanara dashboard restart"
  fi
  print -r -- "  actanara update --dry-run"
}

print_completion() {
  if [[ "$SUMMARY_ONLY" == "1" && -t 1 && -r /dev/tty ]]; then
    clear_tty_menu
  fi
  print -r -- ""
  print -r -- "${TTY_GREEN}✓${TTY_RESET} ${COMPLETION_TEXT}"
  print_install_summary
  print -r -- ""
  print_useful_commands
}

summary_line() {
  local line_status="$1"
  local label="$2"
  local detail="$3"
  local mark="[ok]"
  local color="$TTY_BLUE"
  case "$line_status" in
    ok)
      mark="✓"
      color="$TTY_BLUE"
      ;;
    warn)
      mark="!"
      color="$TTY_YELLOW"
      ;;
    off)
      mark="–"
      color="$TTY_DIM"
      ;;
    error)
      mark="✕"
      color="$TTY_RED"
      ;;
    plan)
      mark="•"
      color="$TTY_BLUE"
      ;;
  esac
  print -r -- "  ${color}${mark}${TTY_RESET} ${label} · ${detail}"
}

effective_dashboard_url() {
  if [[ -f "${RUNTIME_HOME}/config/settings.json" ]]; then
    local python_for_settings="${VENV_PY:-$PYTHON_BIN}"
    if [[ -x "$python_for_settings" ]]; then
      local configured_url=""
      configured_url="$("$python_for_settings" - "${RUNTIME_HOME}/config/settings.json" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
dashboard = settings.get("dashboard") if isinstance(settings.get("dashboard"), dict) else {}
host = str(dashboard.get("host") or "127.0.0.1")
port = int(dashboard.get("port") or 3036)
print(f"http://{host}:{port}/dashboard")
PY
)"
      if [[ -n "$configured_url" ]]; then
        print -r -- "$configured_url"
        return 0
      fi
    fi
  fi
  print -r -- "http://${DASHBOARD_HOST}:${DASHBOARD_PORT}/dashboard"
}

effective_llm_summary() {
  if [[ -f "${RUNTIME_HOME}/config/settings.json" ]]; then
    local python_for_settings="${VENV_PY:-$PYTHON_BIN}"
    if [[ -x "$python_for_settings" ]]; then
      local configured_summary=""
      configured_summary="$("$python_for_settings" - "${RUNTIME_HOME}/config/settings.json" <<'PY' 2>/dev/null || true
import json
import re
import sys
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
features = settings.get("features") if isinstance(settings.get("features"), dict) else {}
if features.get("llmGeneration") is False:
    print("warn\tdisabled")
    raise SystemExit
provider = settings.get("llmProvider") if isinstance(settings.get("llmProvider"), dict) else {}
model = str(provider.get("model") or "")
endpoint = str(provider.get("endpoint") or "")
secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else {}
secret_backend = str(secret_ref.get("backend") or "").strip()
if endpoint and model:
    status = "warn" if secret_backend == "memory" else "ok"
    print(f"{status}\t{model}")
else:
    print(f"warn\t{model}")
PY
)"
      if [[ -n "$configured_summary" ]]; then
        print -r -- "$configured_summary"
        return 0
      fi
    fi
  fi
  if [[ "$ENABLE_LLM_GENERATION" != "1" ]]; then
	print -r -- "warn	disabled"
  elif [[ -n "$LLM_ENDPOINT" && -n "$LLM_MODEL" ]]; then
	print -r -- "ok	${LLM_MODEL}"
  else
	print -r -- "warn	${LLM_MODEL}"
  fi
}

effective_external_tools_summary() {
  if [[ -f "${RUNTIME_HOME}/config/settings.json" ]]; then
    local python_for_settings="${VENV_PY:-$PYTHON_BIN}"
    if [[ -x "$python_for_settings" ]]; then
      local configured_tools=""
      configured_tools="$("$python_for_settings" - "${RUNTIME_HOME}/config/settings.json" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
external = settings.get("externalTools") if isinstance(settings.get("externalTools"), dict) else {}
selected = external.get("installerSelectedTools") if isinstance(external.get("installerSelectedTools"), list) else []
items = []
for item in selected:
    if not isinstance(item, dict):
        continue
    name = str(item.get("name") or item.get("key") or "").strip()
    if name:
        items.append(name)
if not items:
    fallback_labels = {
        "openclaw": "OpenClaw",
        "claudeCode": "Claude Code",
        "codex": "Codex",
        "geminiCli": "Gemini CLI",
        "hermes": "Hermes",
    }
    for key, label in fallback_labels.items():
        value = external.get(key)
        if isinstance(value, dict) and value.get("home"):
            items.append(label)
print(", ".join(items) if items else "none")
PY
)"
      if [[ -n "$configured_tools" ]]; then
        print -r -- "$configured_tools"
        return 0
      fi
    fi
  fi
  format_connected_tools
}

print_install_summary() {
  local dashboard_detail=""
  local scheduler_detail=""
  local llm_detail=""
  local llm_status=""
  local rag_detail=""
  local summary_status="ok"
  local command_detail="actanara"
  local connected_tools=""

  print -r -- ""
  if [[ "$DRY_RUN" == "1" ]]; then
    if [[ "$UPGRADE" == "1" ]]; then
      print -r -- "$(installer_text update_plan_summary)"
    else
      print -r -- "$(installer_text plan_summary)"
    fi
    summary_status="plan"
    command_detail="$(installer_text detail_planned)"
  elif [[ "$UPGRADE" == "1" ]]; then
    print -r -- "$(installer_text update_summary)"
  else
    print -r -- "$(installer_text install_summary)"
  fi
  summary_line "$summary_status" "$(installer_text label_command)" "$command_detail"
  summary_line "$summary_status" "$(installer_text label_folder)" "${RUNTIME_HOME}"
  summary_line "$summary_status" "$(installer_text label_diary)" "${DIARY_OUTPUT_DIR}"

  if [[ "$NO_DASHBOARD_SERVER" == "1" ]]; then
    dashboard_detail="$(installer_text detail_dashboard_app)"
    summary_line off "$(installer_text label_dashboard)" "$dashboard_detail"
  else
    dashboard_detail="$(effective_dashboard_url)"
    summary_line "$summary_status" "$(installer_text label_dashboard)" "$dashboard_detail"
  fi

  if [[ "$PLATFORM" == "Darwin" && "$NO_SCHEDULER" != "1" ]]; then
    scheduler_detail="$(installer_text detail_daily_on)"
    summary_line "$summary_status" "$(installer_text label_daily)" "$scheduler_detail"
  elif [[ "$NO_SCHEDULER" == "1" ]]; then
    scheduler_detail="$(installer_text detail_daily_off)"
    summary_line off "$(installer_text label_daily)" "$scheduler_detail"
  else
    scheduler_detail="$(installer_text detail_daily_off)"
    summary_line off "$(installer_text label_daily)" "$scheduler_detail"
  fi

  IFS=$'\t' read -r llm_status llm_detail <<<"$(effective_llm_summary)"
  if [[ "$llm_detail" == "disabled" ]]; then
    llm_detail="$(installer_text detail_disabled)"
  elif [[ -z "$llm_detail" ]]; then
    llm_detail="$(installer_text detail_ai_needs_setup)"
  fi
  summary_line "${llm_status:-warn}" "$(installer_text label_ai)" "$llm_detail"

  if [[ "$ENABLE_RAG" != "1" ]]; then
    summary_line off "$(installer_text label_memory)" "$(installer_text detail_disabled)"
  elif [[ "$RAG_EMBEDDING_MODE" == "local" ]]; then
    rag_detail="$(installer_text detail_local) · ${RAG_LOCAL_MODEL}"
    summary_line "$summary_status" "$(installer_text label_memory)" "$rag_detail"
  else
    rag_detail="$(installer_text detail_cloud) · ${RAG_CLOUD_MODEL:-${RAG_CLOUD_PROVIDER}}"
    summary_line "$summary_status" "$(installer_text label_memory)" "$rag_detail"
  fi

  connected_tools="$(effective_external_tools_summary)"
  if [[ "$connected_tools" == "none" ]]; then
    connected_tools="$(installer_text detail_none)"
  fi
  summary_line "$summary_status" "$(installer_text label_tools)" "$connected_tools"
  if [[ "$DRY_RUN" != "1" ]]; then
    print -r -- ""
    print -r -- "${TTY_DIM}$(installer_text details_log): ${INSTALLER_LOG_FILE}${TTY_RESET}"
  fi
}

run_wizard() {
  if [[ ! -r /dev/tty ]]; then
    error "Interactive wizard requires a terminal"
    exit 2
  fi
  local default_diary_output=""
  local default_reports_output=""
  local default_snapshots_output=""
  local default_archives_output=""

  if ! prompt_yes_no "$(installer_text welcome)" "yes"; then
    log "$(installer_text welcome_cancelled)"
    exit 0
  fi
  WIZARD_CONFIRMED=1
  if [[ "$DRY_RUN" == "1" ]]; then
    SUMMARY_ONLY=1
  fi

  if [[ "$LANGUAGE_SET" != "1" ]]; then
    prompt_language_profile
  else
    apply_language_profile
  fi

  wizard_core_dependency_gate
  if [[ "$ENABLE_LLM_GENERATION" == "1" && "$LLM_SET" != "1" ]]; then
    prompt_llm_provider_from_catalog
  fi
  prompt_external_tools
  prompt_rag_choice

  ENABLE_DASHBOARD=1
  if [[ "$PLATFORM" == "Darwin" && "$NO_SCHEDULER_SET" != "1" ]]; then
    NO_SCHEDULER=0
  elif [[ "$PLATFORM" != "Darwin" ]]; then
    NO_SCHEDULER=1
  fi
  ENABLE_NOVA_TASK=1

  default_diary_output="${RUNTIME_HOME}/artifacts/diary"
  default_reports_output="${RUNTIME_HOME}/artifacts/reports"
  default_snapshots_output="${RUNTIME_HOME}/snapshots"
  default_archives_output="${RUNTIME_HOME}/sources/archives"
  DIARY_OUTPUT_DIR="${DIARY_OUTPUT_DIR:-$default_diary_output}"
  REPORTS_OUTPUT_DIR="${REPORTS_OUTPUT_DIR:-$default_reports_output}"
  SNAPSHOTS_OUTPUT_DIR="${SNAPSHOTS_OUTPUT_DIR:-$default_snapshots_output}"
  ARCHIVES_OUTPUT_DIR="${ARCHIVES_OUTPUT_DIR:-$default_archives_output}"

  if [[ "$ENABLE_RAG" == "1" ]]; then
    if [[ "$RAG_EMBEDDING_MODE" == "local" ]]; then
      prompt_rag_local_model
      if [[ "$EMBEDDING_SERVER_SET" != "1" ]]; then
        DEPLOY_EMBEDDING_SERVER=1
      fi
    elif [[ "$RAG_EMBEDDING_MODE" == "cloud" ]]; then
      DEPLOY_EMBEDDING_SERVER=0
      RAG_CLOUD_PROVIDER="$(prompt_line "$(installer_text cloud_provider)" "$RAG_CLOUD_PROVIDER")"
      RAG_CLOUD_ENDPOINT="$(prompt_line "$(installer_text cloud_endpoint)" "$RAG_CLOUD_ENDPOINT")"
      RAG_CLOUD_MODEL="$(prompt_line "$(installer_text cloud_model)" "$RAG_CLOUD_MODEL")"
      RAG_CLOUD_DIMENSION="$(prompt_line "$(installer_text cloud_dimension)" "$RAG_CLOUD_DIMENSION")"
      RAG_CLOUD_API_KEY_ENV="$(prompt_line "$(installer_text cloud_key_env)" "$RAG_CLOUD_API_KEY_ENV")"
    fi
    wizard_rag_dependency_gate
  fi
  if [[ -n "$SELECTED_EXTERNAL_TOOLS" ]]; then
    ENABLE_SKILL_REGISTRATION=1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime)
      RUNTIME_HOME="$2"
      RUNTIME_SET=1
      shift 2
      ;;
    --diary-output)
      DIARY_OUTPUT_DIR="$2"
      DIARY_OUTPUT_SET=1
      shift 2
      ;;
    --desktop-diary-link)
      DESKTOP_DIARY_LINK="$2"
      CREATE_DESKTOP_DIARY_LINK=1
      DESKTOP_DIARY_LINK_SET=1
      shift 2
      ;;
    --no-desktop-diary-link)
      CREATE_DESKTOP_DIARY_LINK=0
      DESKTOP_DIARY_LINK_SET=1
      shift
      ;;
    --no-shell-path)
      ENABLE_SHELL_PATH=0
      SHELL_PATH_SET=1
      shift
      ;;
    --shell-path-file)
      SHELL_PATH_FILE="$2"
      SHELL_PATH_SET=1
      shift 2
      ;;
    --reports-output)
      REPORTS_OUTPUT_DIR="$2"
      REPORTS_OUTPUT_SET=1
      shift 2
      ;;
    --snapshots-output)
      SNAPSHOTS_OUTPUT_DIR="$2"
      SNAPSHOTS_OUTPUT_SET=1
      shift 2
      ;;
    --archives-output)
      ARCHIVES_OUTPUT_DIR="$2"
      ARCHIVES_OUTPUT_SET=1
      shift 2
      ;;
    --source-root)
      SOURCE_ROOT="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      PYTHON_SET=1
      shift 2
      ;;
    --no-python-auto-install)
      PYTHON_AUTO_INSTALL=0
      shift
      ;;
    --no-scheduler)
      NO_SCHEDULER=1
      NO_SCHEDULER_SET=1
      shift
      ;;
    --no-dashboard)
      error "--no-dashboard is no longer supported because Dashboard is required"
      exit 2
      ;;
    --no-dashboard-server)
      NO_DASHBOARD_SERVER=1
      NO_DASHBOARD_SERVER_SET=1
      shift
      ;;
    --dashboard-port)
      DASHBOARD_PORT="$2"
      DASHBOARD_PORT_SET=1
      shift 2
      ;;
    --dashboard-host)
      DASHBOARD_HOST="$2"
      DASHBOARD_HOST_SET=1
      shift 2
      ;;
    --dashboard-port-auto)
      DASHBOARD_PORT_AUTO=1
      shift
      ;;
    --no-dashboard-port-auto)
      DASHBOARD_PORT_AUTO=0
      shift
      ;;
    --enable-rag)
      ENABLE_RAG=1
      RAG_SET=1
      RAG_ENABLE_SET=1
      shift
      ;;
    --register-memory-skills|--register-rag-skills)
      ENABLE_SKILL_REGISTRATION=1
      shift
      ;;
    --enable-dev-test)
      ENABLE_DEV_TEST=1
      DEV_TEST_SET=1
      shift
      ;;
    --rag-embedding-mode)
      RAG_EMBEDDING_MODE="$2"
      RAG_EMBEDDING_MODE_SET=1
      shift 2
      ;;
    --rag-local-model)
      RAG_LOCAL_MODEL="$2"
      RAG_LOCAL_DIMENSION="$(rag_local_model_dimension "$RAG_LOCAL_MODEL")"
      RAG_LOCAL_MODEL_SET=1
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="local"
      RAG_SET=1
      RAG_ENABLE_SET=1
      RAG_DETAIL_SET=1
      RAG_EMBEDDING_MODE_SET=1
      shift 2
      ;;
    --rag-local-dimension)
      RAG_LOCAL_DIMENSION="$2"
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="local"
      RAG_SET=1
      RAG_ENABLE_SET=1
      RAG_DETAIL_SET=1
      RAG_EMBEDDING_MODE_SET=1
      shift 2
      ;;
    --deploy-embedding-server)
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="local"
      DEPLOY_EMBEDDING_SERVER=1
      RAG_SET=1
      RAG_ENABLE_SET=1
      RAG_DETAIL_SET=1
      RAG_EMBEDDING_MODE_SET=1
      EMBEDDING_SERVER_SET=1
      shift
      ;;
    --no-deploy-embedding-server)
      DEPLOY_EMBEDDING_SERVER=0
      EMBEDDING_SERVER_SET=1
      RAG_DETAIL_SET=1
      shift
      ;;
    --llm-provider)
      LLM_PROVIDER="$2"
      LLM_PROVIDER_MODE="preset"
      LLM_SET=1
      shift 2
      ;;
    --llm-endpoint)
      LLM_ENDPOINT="$2"
      LLM_SET=1
      shift 2
      ;;
    --llm-model)
      LLM_MODEL="$2"
      LLM_SET=1
      shift 2
      ;;
    --llm-api-key-env)
      LLM_API_KEY_ENV="$2"
      LLM_SET=1
      shift 2
      ;;
    --rag-cloud-provider)
      RAG_CLOUD_PROVIDER="$2"
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="cloud"
      RAG_SET=1
      RAG_ENABLE_SET=1
      RAG_DETAIL_SET=1
      RAG_EMBEDDING_MODE_SET=1
      shift 2
      ;;
    --rag-cloud-endpoint)
      RAG_CLOUD_ENDPOINT="$2"
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="cloud"
      RAG_SET=1
      RAG_ENABLE_SET=1
      RAG_DETAIL_SET=1
      RAG_EMBEDDING_MODE_SET=1
      shift 2
      ;;
    --rag-cloud-model)
      RAG_CLOUD_MODEL="$2"
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="cloud"
      RAG_SET=1
      RAG_ENABLE_SET=1
      RAG_DETAIL_SET=1
      RAG_EMBEDDING_MODE_SET=1
      shift 2
      ;;
    --rag-cloud-dimension)
      RAG_CLOUD_DIMENSION="$2"
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="cloud"
      RAG_SET=1
      RAG_ENABLE_SET=1
      RAG_DETAIL_SET=1
      RAG_EMBEDDING_MODE_SET=1
      shift 2
      ;;
    --rag-cloud-api-key-env)
      RAG_CLOUD_API_KEY_ENV="$2"
      ENABLE_RAG=1
      RAG_EMBEDDING_MODE="cloud"
      RAG_SET=1
      RAG_ENABLE_SET=1
      RAG_DETAIL_SET=1
      RAG_EMBEDDING_MODE_SET=1
      shift 2
      ;;
    --language)
      INSTALL_LANGUAGE="$2"
      LANGUAGE_SET=1
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --summary-only)
      SUMMARY_ONLY=1
      shift
      ;;
    --upgrade)
      UPGRADE=1
      UPGRADE_EXPLICIT=1
      shift
      ;;
    --repair-existing)
      REPAIR_EXISTING=1
      shift
      ;;
    --source-only|--sync-runtime-source)
      SOURCE_ONLY=1
      UPGRADE=1
      UPGRADE_EXPLICIT=1
      WIZARD_MODE=0
      shift
      ;;
    --force-rebuild)
      FORCE_REBUILD=1
      FORCE_REBUILD_EXPLICIT=1
      shift
      ;;
    --offline)
      OFFLINE=1
      shift
      ;;
    --result-json)
      RESULT_JSON=1
      shift
      ;;
    --wizard)
      WIZARD_MODE=1
      shift
      ;;
    --no-wizard)
      WIZARD_MODE=0
      shift
      ;;
    --yes)
      YES=1
      WIZARD_MODE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      error "Unknown option: $1"
      usage >&2
      exit 2
      ;;
  esac
done

trap 'emit_update_result $?' EXIT

if [[ "$REPAIR_EXISTING" == "1" && (
  "$UPGRADE_EXPLICIT" == "1" ||
  "$SOURCE_ONLY" == "1" ||
  "$FORCE_REBUILD_EXPLICIT" == "1"
) ]]; then
  UPDATE_RESULT_STAGE="argument-validation"
  UPDATE_REASON="repair-existing-has-conflicting-update-mode"
  error "--repair-existing cannot be combined with --upgrade, --source-only, or --force-rebuild"
  exit 2
fi
if [[ "$REPAIR_EXISTING" == "1" ]]; then
  UPGRADE=1
  FORCE_REBUILD=1
  WIZARD_MODE=0
  if [[ "$DRY_RUN" != "1" && "$YES" != "1" ]]; then
    UPDATE_RESULT_STAGE="argument-validation"
    UPDATE_REASON="repair-existing-requires-confirmation"
    error "--repair-existing requires confirmation through the one-liner or --yes"
    exit 2
  fi
fi
if [[ "$SOURCE_ONLY" == "1" && "$FORCE_REBUILD" == "1" ]]; then
  UPDATE_RESULT_STAGE="argument-validation"
  UPDATE_REASON="source-only-and-force-rebuild-are-mutually-exclusive"
  error "--source-only and --force-rebuild are mutually exclusive"
  exit 2
fi
if [[ "$FORCE_REBUILD" == "1" && "$UPGRADE" != "1" ]]; then
  UPDATE_RESULT_STAGE="argument-validation"
  UPDATE_REASON="force-rebuild-requires-upgrade"
  error "--force-rebuild requires --upgrade"
  exit 2
fi
if [[ "$OFFLINE" == "1" ]]; then
  PYTHON_AUTO_INSTALL=0
fi

SOURCE_ROOT="${SOURCE_ROOT:A}"
if [[ ! -f "${SOURCE_ROOT}/pyproject.toml" ]]; then
  error "pyproject.toml not found under source root: ${SOURCE_ROOT}"
  exit 2
fi
RUNTIME_DEPENDENCY_LOCK="${SOURCE_ROOT}/install/runtime-dependencies.lock.json"
RUNTIME_HOME="${RUNTIME_HOME:a}"
require_repair_runtime_identity || exit 2
RUNTIME_HOME="${RUNTIME_HOME:A}"
require_fresh_runtime_empty || exit 2
resolve_python_bin || true

if wizard_enabled; then
  run_wizard
fi
apply_language_profile
LANGUAGE_SELECTED=1
if ! validate_llm_api_key_env; then
  exit 2
fi

RUNTIME_HOME="${RUNTIME_HOME:A}"
DIARY_OUTPUT_DIR="${DIARY_OUTPUT_DIR:-${RUNTIME_HOME}/artifacts/diary}"
DESKTOP_DIARY_LINK="${DESKTOP_DIARY_LINK:A}"
REPORTS_OUTPUT_DIR="${REPORTS_OUTPUT_DIR:-${RUNTIME_HOME}/artifacts/reports}"
SNAPSHOTS_OUTPUT_DIR="${SNAPSHOTS_OUTPUT_DIR:-${RUNTIME_HOME}/snapshots}"
ARCHIVES_OUTPUT_DIR="${ARCHIVES_OUTPUT_DIR:-${RUNTIME_HOME}/sources/archives}"
DIARY_OUTPUT_DIR="${DIARY_OUTPUT_DIR:A}"
REPORTS_OUTPUT_DIR="${REPORTS_OUTPUT_DIR:A}"
SNAPSHOTS_OUTPUT_DIR="${SNAPSHOTS_OUTPUT_DIR:A}"
ARCHIVES_OUTPUT_DIR="${ARCHIVES_OUTPUT_DIR:A}"
VENV_DIR="${RUNTIME_HOME}/.venv"
VENV_PY="${VENV_DIR}/bin/python"
CLI_SHIM="${RUNTIME_HOME}/bin/actanara"
DEPLOY_SOURCE_ROOT="${RUNTIME_HOME}/app/source"
INSTALLER_LOG_FILE="${RUNTIME_HOME}/state/logs/installer-v2.log"
LOCATION_FILE="${ACTANARA_LOCATION_FILE:-$HOME/.config/actanara/location.json}"
if [[ "$UPGRADE" == "1" && ! -d "$RUNTIME_HOME" && "$DRY_RUN" != "1" ]]; then
  error "--upgrade requires an existing runtime: ${RUNTIME_HOME}"
  exit 2
fi
if [[ "$UPGRADE" == "1" ]]; then
  UPDATE_RESULT_STAGE="dependency-profile"
  if ! inherit_upgrade_dependency_profiles; then
    UPDATE_REASON="runtime-dependency-profile-untrusted"
    error "Runtime dependency profile could not be read safely; update blocked before service changes"
    emit_update_result 2
    exit 2
  fi
fi
select_dashboard_port
INSTALL_SPEC="${DEPLOY_SOURCE_ROOT}"
INSTALL_EXTRAS=()
INSTALL_EXTRAS+=("dashboard")
if [[ "$ENABLE_RAG" == "1" ]]; then
  INSTALL_EXTRAS+=("rag-server")
fi
if [[ "$ENABLE_RAG" == "1" && "$RAG_EMBEDDING_MODE" == "local" ]]; then
  INSTALL_EXTRAS+=("rag-local")
  if [[ "$EMBEDDING_SERVER_SET" != "1" ]]; then
    DEPLOY_EMBEDDING_SERVER=1
  fi
fi
if [[ "$ENABLE_DEV_TEST" == "1" ]]; then
  INSTALL_EXTRAS+=("dev-test")
fi
if [[ "${#INSTALL_EXTRAS[@]}" -gt 0 ]]; then
  INSTALL_SPEC="${DEPLOY_SOURCE_ROOT}[${(j:,:)INSTALL_EXTRAS}]"
fi

case "$RAG_EMBEDDING_MODE" in
  local|cloud) ;;
  *)
    error "--rag-embedding-mode must be local or cloud"
    exit 2
    ;;
esac

require_fresh_runtime_empty || exit 2
INSTALLER_LOG_ACTIVE=1
log "Actanara installer v2"
print_installer_data_notice
if [[ "$WIZARD_CONFIRMED" != "1" || ! -t 1 ]]; then
  render_console_header
fi
log "mode: $([[ "$REPAIR_EXISTING" == "1" ]] && print repair || ([[ "$SOURCE_ONLY" == "1" ]] && print source-only || ([[ "$UPGRADE" == "1" ]] && print upgrade || print install)))"
log "source root: ${SOURCE_ROOT}"
log "runtime: ${RUNTIME_HOME}"
log "language: ${INSTALL_LANGUAGE} (pipeline=${PIPELINE_LANGUAGE_PROFILE}, diarySchema=${PIPELINE_DIARY_SCHEMA_VERSION}, promptPayload=${PIPELINE_PROMPT_PAYLOAD_PROFILE})"
log "deployed runtime source: ${DEPLOY_SOURCE_ROOT}"
log "generated diary output: ${DIARY_OUTPUT_DIR}"
log "Desktop diary shortcut: $([[ "$CREATE_DESKTOP_DIARY_LINK" == "1" ]] && print "${DESKTOP_DIARY_LINK}" || print disabled)"
log "selected external tools: $(format_selected_external_tools)"
if [[ "$ENABLE_SKILL_REGISTRATION" == "1" ]]; then
  log "skill registration: installer reconciles one dynamic Memory Search skill for detected, selected, supported external tools; customized skills are preserved"
fi
log "reports output: ${REPORTS_OUTPUT_DIR}"
log "snapshots output: ${SNAPSHOTS_OUTPUT_DIR}"
log "archives/intermediate output: ${ARCHIVES_OUTPUT_DIR}"
log "location pointer: ${LOCATION_FILE}"
log "install dependency spec: ${INSTALL_SPEC}"
log "dashboard UI: $([[ "$ENABLE_DASHBOARD" == "1" ]] && print enabled || print disabled)"
log "SSE server: $([[ "$NO_DASHBOARD_SERVER" == "1" ]] && print disabled || print enabled)"
if [[ "$NO_DASHBOARD_SERVER" != "1" ]]; then
  log "Dashboard URL: http://${DASHBOARD_HOST}:${DASHBOARD_PORT}/dashboard"
fi
log "scheduler: $([[ "$NO_SCHEDULER" == "1" ]] && print disabled || print default)"
log "Nova-Task: $([[ "$ENABLE_NOVA_TASK" == "1" ]] && print enabled || print disabled)"
log "dev-test: $([[ "$ENABLE_DEV_TEST" == "1" ]] && print enabled || print disabled)"
log "LLM generation: $([[ "$ENABLE_LLM_GENERATION" == "1" ]] && print enabled || print disabled)"
if [[ "$ENABLE_LLM_GENERATION" == "1" ]]; then
  log "LLM provider: ${LLM_PROVIDER_MODE}/${LLM_PROVIDER}; model: ${LLM_MODEL:-unset}; endpoint: ${LLM_ENDPOINT:-unset}; api key env: $(safe_env_var_label "$LLM_API_KEY_ENV")"
fi
log "nova-RAG: $([[ "$ENABLE_RAG" == "1" ]] && print enabled || print disabled)"
if [[ "$ENABLE_RAG" == "1" ]]; then
  log "nova-RAG embedding mode: ${RAG_EMBEDDING_MODE}"
  if [[ "$RAG_EMBEDDING_MODE" == "local" ]]; then
    log "nova-RAG local embedding: model=${RAG_LOCAL_MODEL}; dimension=${RAG_LOCAL_DIMENSION:-unset}"
  elif [[ "$RAG_EMBEDDING_MODE" == "cloud" ]]; then
    log "nova-RAG cloud embedding: provider=${RAG_CLOUD_PROVIDER}; model=${RAG_CLOUD_MODEL:-unset}; endpoint=${RAG_CLOUD_ENDPOINT:-unset}; dimension=${RAG_CLOUD_DIMENSION:-unset}; api key env=$(safe_env_var_label "$RAG_CLOUD_API_KEY_ENV")"
  fi
fi

if [[ "$NO_DASHBOARD_SERVER" == "1" ]]; then
  warn "SSE server disabled: only the Dashboard UI realtime overview and task board pages will be unavailable."
  warn "Static snapshot pages such as AI Assets, other Dashboard pages, and Nova-Task remain available."
fi

print_phase phase_checking
run_installer_preflight

if [[ "$UPGRADE" == "1" ]]; then
  dependency_plan_rc=0
  if run_dependency_update_plan "${SOURCE_ROOT}"; then
    dependency_plan_rc=0
  else
    dependency_plan_rc=$?
    emit_update_result "$dependency_plan_rc"
    exit "$dependency_plan_rc"
  fi
  log "dependency update plan: mode=${UPDATE_MODE}; reason=${UPDATE_REASON}; python=${UPDATE_PYTHON_SELECTION_REASON}"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run only; no files will be written and no commands will be executed"
elif wizard_enabled && [[ "$YES" != "1" && "$WIZARD_CONFIRMED" != "1" ]]; then
  if [[ "$UPGRADE" == "1" ]]; then
    if ! prompt_yes_no "$(installer_text proceed_upgrade)" "no"; then
      log "$(installer_text upgrade_cancelled)"
      exit 0
    fi
  elif ! prompt_yes_no "$(installer_text proceed_install)" "no"; then
    log "$(installer_text install_cancelled)"
    exit 0
  fi
fi

if [[ "$UPGRADE" == "1" ]]; then
  print_phase phase_installing
  run_guarded_update_transaction
  if [[ "$UPDATE_NOOP" == "1" ]]; then
    log "Actanara update is a no-op; source payload and dependency contract are already active"
    if [[ "$SOURCE_ONLY" != "1" ]]; then
      run_external_rag_skill_registration_apply
    fi
    COMPLETION_TEXT="$(installer_text update_no_changes)"
    print_completion
    exit 0
  fi
  inherit_repair_service_state
  UPDATE_RESULT_STAGE="cli-shim"
  create_cli_shim
  if [[ "$REPAIR_EXISTING" == "1" ]]; then
    if [[ "$DESKTOP_DIARY_LINK_SET" == "1" && "$CREATE_DESKTOP_DIARY_LINK" == "1" ]]; then
      create_desktop_diary_link
    fi
    ensure_cli_on_shell_path
    UPDATE_RESULT_STAGE="$([[ "$DRY_RUN" == "1" ]] && print plan || print repair-configuration)"
  elif [[ "$DRY_RUN" == "1" ]]; then
    UPDATE_RESULT_STAGE="plan"
  else
    UPDATE_RESULT_STAGE="complete"
  fi
  if [[ "$SOURCE_ONLY" == "1" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
      log "source-only dry-run complete; no source pointer, settings, dependencies, LaunchAgents, or RAG manifests were changed"
      COMPLETION_TEXT="$(installer_text source_update_plan_complete)"
    else
      log "Actanara runtime source-only sync complete; the prior managed service state was restored"
      log "Settings, dependencies, Keychain references, and user data were not changed; legacy Python LaunchAgents may receive cache-suppression environment metadata"
      COMPLETION_TEXT="$(installer_text source_update_complete)"
    fi
    print_completion
    exit 0
  fi
  if [[ "$REPAIR_EXISTING" != "1" ]]; then
    run_external_rag_skill_registration_apply
    if [[ "$DRY_RUN" == "1" ]]; then
      COMPLETION_TEXT="$(installer_text upgrade_plan_complete)"
    else
      COMPLETION_TEXT="$(installer_text upgrade_complete)"
      log "Upgrade preserved Settings, runtime manifest, location pointer, live SQLite state without rewind, service loaded/running state, and configured Dashboard port; legacy Python LaunchAgents were transactionally normalized when required"
      log "Credential rotation and background embedding deployment were not performed inside the update transaction; managed external Memory Search skills were reconciled afterward on a best-effort basis"
    fi
    print_completion
    exit 0
  fi
fi

if [[ "$UPGRADE" != "1" ]]; then
  print_phase phase_preparing
  prepare_fresh_dependency_cache "${SOURCE_ROOT}"
  run_cmd mkdir -p "${RUNTIME_HOME}"
  run_cmd mkdir -p "${DIARY_OUTPUT_DIR}" "${REPORTS_OUTPUT_DIR}" "${SNAPSHOTS_OUTPUT_DIR}" "${ARCHIVES_OUTPUT_DIR}"
  create_desktop_diary_link
  stage_runtime_source
  create_fresh_runtime_venv
  print_phase phase_installing
  stable_source_root="${DEPLOY_SOURCE_ROOT}"
  DEPLOY_SOURCE_ROOT="${STAGED_RELEASE_TARGET:-${SOURCE_ROOT}}"
  install_fresh_locked_dependencies "${DEPLOY_SOURCE_ROOT}" "${FRESH_STAGED_VENV:-${VENV_DIR}}"
  run_runtime_dependency_gate
  DEPLOY_SOURCE_ROOT="${stable_source_root}"
  promote_fresh_runtime_artifacts
  create_cli_shim
  ensure_cli_on_shell_path
fi

print_phase phase_configuring
log "Applying runtime bootstrap and active runtime pointer"
export_runtime_environment
migrate_legacy_settings_for_repair
runtime_apply_args=(
  -m data_foundation.cli
  onboarding runtime-apply
  --runtime "${RUNTIME_HOME}"
  --select-active-runtime
  --confirmation-text "APPLY ACTANARA ONBOARDING"
  --json
)
if [[ "$UPGRADE" != "1" || "$LANGUAGE_SET" == "1" ]]; then
  runtime_apply_args+=(--language "${INSTALL_LANGUAGE}")
fi
run_json_cmd "Runtime bootstrap apply" "${VENV_PY}" "${runtime_apply_args[@]}"
apply_installer_settings_overlay
run_external_rag_skill_registration_apply
store_installer_llm_api_key_secret

if [[ "$PLATFORM" == "Darwin" && "$NO_SCHEDULER" != "1" ]]; then
  log "Registering managed Actanara scheduler LaunchAgents"
  if [[ "$UPGRADE" == "1" ]]; then
    run_json_cmd "Scheduler LaunchAgent plist write" \
      "${VENV_PY}" -m data_foundation.cli \
      onboarding apply \
      --scheduler-plist-apply \
      --runtime "${RUNTIME_HOME}" \
      --confirmation-text "WRITE ACTANARA LAUNCHAGENTS" \
      --json
    run_json_cmd "Scheduler LaunchAgent registration" \
      "${VENV_PY}" -m data_foundation.cli \
      onboarding apply \
      --scheduler-register-apply \
      --runtime "${RUNTIME_HOME}" \
      --confirmation-text "REGISTER ACTANARA SCHEDULER" \
      --json
  else
    run_optional_json_cmd "Scheduler LaunchAgent plist write" \
      "${VENV_PY}" -m data_foundation.cli \
      onboarding apply \
      --scheduler-plist-apply \
      --runtime "${RUNTIME_HOME}" \
      --confirmation-text "WRITE ACTANARA LAUNCHAGENTS" \
      --json
    run_optional_json_cmd "Scheduler LaunchAgent registration" \
      "${VENV_PY}" -m data_foundation.cli \
      onboarding apply \
      --scheduler-register-apply \
      --runtime "${RUNTIME_HOME}" \
      --confirmation-text "REGISTER ACTANARA SCHEDULER" \
      --json
  fi
elif [[ "$NO_SCHEDULER" == "1" ]]; then
  log "Scheduler registration skipped by --no-scheduler"
else
  log "Scheduler registration skipped on unsupported platform: ${PLATFORM}"
fi

if [[ "$NO_DASHBOARD_SERVER" != "1" ]]; then
  if [[ "$PLATFORM" == "Darwin" ]]; then
    log "Installing SSE server LaunchAgent service"
    run_dashboard_service_launch_agent_apply
  else
    log "SSE server service registration skipped on unsupported platform: ${PLATFORM}"
  fi
fi

if [[ "$ENABLE_RAG" == "1" && ( "$REPAIR_EXISTING" != "1" || "$REPAIR_RAG_SERVICE_ENABLED" == "1" ) ]]; then
  if [[ "$PLATFORM" == "Darwin" ]]; then
    log "Installing nova-RAG server LaunchAgent service"
    run_rag_service_launch_agent_apply
  else
    log "nova-RAG server LaunchAgent registration skipped on unsupported platform: ${PLATFORM}"
  fi
fi

if [[ "$DEPLOY_EMBEDDING_SERVER" == "1" && "$PLATFORM" == "Darwin" && "$ENABLE_RAG" == "1" ]]; then
  log "nova-RAG embedding server lifecycle is managed by its LaunchAgent; direct background start skipped"
  progress_start "$(installer_text step_prepare_memory)"
  progress_ok "$(installer_text step_prepare_memory)"
elif [[ "$DEPLOY_EMBEDDING_SERVER" == "1" ]]; then
  JOB_DIR="${RUNTIME_HOME}/state/jobs"
  JOB_SCRIPT="${JOB_DIR}/deploy-embedding-server.sh"
  JOB_LOG="${RUNTIME_HOME}/state/logs/embedding-server-deploy.log"
  log "Queueing background embedding server deployment"
  if [[ "$DRY_RUN" == "1" ]]; then
    progress_start "$(installer_text step_prepare_memory)"
    progress_ok "$(installer_text step_prepare_memory)"
  else
    mkdir -p "${JOB_DIR}" "${RUNTIME_HOME}/state/logs"
    cat > "${JOB_SCRIPT}" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
export ACTANARA_HOME="${RUNTIME_HOME}"
export ACTANARA_LOCATION_FILE="${LOCATION_FILE}"
export PYTHONPATH="${DEPLOY_SOURCE_ROOT}:${DEPLOY_SOURCE_ROOT}/src"
"${VENV_PY}" - <<'PY'
from agentic_rag.rag_server_lifecycle import start_rag_server
print(start_rag_server(requested_by="installer-v2", wait_timeout_seconds=1.0))
PY
EOF
    chmod +x "${JOB_SCRIPT}"
    nohup "${JOB_SCRIPT}" > "${JOB_LOG}" 2>&1 &
    print -r -- "$!" > "${JOB_DIR}/deploy-embedding-server.pid"
  fi
fi

print_phase phase_verifying
run_post_install_doctor
cleanup_runtime_source_artifacts
if [[ "$REPAIR_EXISTING" == "1" && "$DRY_RUN" != "1" ]]; then
  update_transaction_command complete-repair --state "${UPDATE_TRANSACTION_JOURNAL}"
  REPAIR_CONFIGURATION_COMPLETE=1
fi

if [[ "$DRY_RUN" == "1" ]]; then
  if [[ "$REPAIR_EXISTING" == "1" ]]; then
    COMPLETION_TEXT="$(installer_text repair_plan_complete)"
  else
    COMPLETION_TEXT="$(installer_text dry_run_complete)"
  fi
elif [[ "$REPAIR_EXISTING" == "1" ]]; then
  UPDATE_RESULT_STAGE="complete"
  COMPLETION_TEXT="$(installer_text repair_complete)"
elif [[ "$UPGRADE" == "1" ]]; then
  COMPLETION_TEXT="$(installer_text upgrade_complete)"
else
  COMPLETION_TEXT="$(installer_text install_complete)"
fi

print_completion
if [[ "$REPAIR_EXISTING" == "1" && -n "$REPAIR_BACKUP_DIR" ]]; then
  print -r -- "$(installer_text repair_backup): ${REPAIR_BACKUP_DIR}"
fi
