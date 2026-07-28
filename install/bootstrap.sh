#!/usr/bin/env zsh
# Keep the hosted bootstrap inside one compound command. If a streamed download
# is truncated, zsh cannot parse the closing `fi` and executes no prefix.
if true; then
set -euo pipefail

BOOTSTRAP_DIR="${0:A:h}"
DEFAULT_CACHE_ROOT="${ACTANARA_INSTALL_CACHE_ROOT:-$HOME/.cache/actanara/installer}"
CACHE_ROOT_EXPLICIT=$([[ -n "${ACTANARA_INSTALL_CACHE_ROOT:-}" ]] && print 1 || print 0)
DEFAULT_SOURCE_URL="https://github.com/Neo-Isshin/actanara.git"
SOURCE_ROOT="${ACTANARA_INSTALL_SOURCE_ROOT:-}"
SOURCE_URL="${ACTANARA_INSTALL_SOURCE_URL:-}"
SOURCE_REF="${ACTANARA_INSTALL_REF:-}"
GIT_BIN="${ACTANARA_INSTALL_GIT:-git}"
PLUTIL_BIN="${ACTANARA_INSTALL_PLUTIL:-/usr/bin/plutil}"
ZSH_BIN="${ACTANARA_INSTALL_ZSH:-${ZSH_VERSION:+/bin/zsh}}"
DRY_RUN=0
OFFLINE=0
INSTALL_ARGS=()
CACHE_SOURCE=0
BOOTSTRAP_LOG_FILE=""
BOOTSTRAP_LANGUAGE="${ACTANARA_INSTALL_LANGUAGE:-zh-CN}"
SPARSE_PATHS=(
  "/pyproject.toml"
  "/MANIFEST.in"
  "/LICENSE"
  "/config.py"
  "/install"
  "/advanced"
  "/src"
)
OFFLINE_SOURCE_PATHS=(
  "pyproject.toml"
  "MANIFEST.in"
  "LICENSE"
  "config.py"
  "install"
  "advanced"
  "src"
)

usage() {
  cat <<'EOF'
Actanara setup

Usage:
  install/bootstrap.sh [bootstrap-options] [-- installer-options]

Preparation options:
  --source-root PATH       Use an existing local copy of Actanara.
  --source-url URL         Download Actanara from this URL.
                          Default: https://github.com/Neo-Isshin/actanara.git
  --ref VERSION           Use an exact 40- or 64-character version ID.
                          Required for network setup unless the Release asset pins it.
  --cache-root PATH        Download cache folder. Default: ~/.cache/actanara/installer
  --git PATH              Git executable. Default: git
  --offline               Use local or previously downloaded files only.
  --dry-run               Preview preparation and setup without writing files.
  -h, --help              Show this help.

Options after -- are passed to the main setup.
EOF
}

bootstrap_text() {
  local key="$1"
  case "$BOOTSTRAP_LANGUAGE" in
    en|en-US|en_US)
      case "$key" in
        preparing_folder) print -r -- "Preparing the download folder" ;;
        downloading) print -r -- "Downloading Actanara" ;;
        checking_updates) print -r -- "Checking the selected Actanara version" ;;
        preparing_files) print -r -- "Preparing installation files" ;;
        cache_ready) print -r -- "Previously downloaded files are ready" ;;
        source_plan_ready) print -r -- "Immutable source plan is ready; no cached installer was executed" ;;
        cache_isolated) print -r -- "The previous download folder was kept; using a new download folder" ;;
        existing_ready) print -r -- "Existing Actanara data will be kept and updated" ;;
        legacy_repair_prompt) print -r -- "This Actanara installation cannot be updated directly. Reinstall it in place? Your data and settings will be kept; only the runtime and dependencies will be rebuilt. [Y/n]" ;;
        legacy_repair_ready) print -r -- "Existing Actanara data and settings will be kept while the runtime and dependencies are rebuilt" ;;
        legacy_repair_cancelled) print -r -- "Actanara recovery cancelled; existing files were not changed" ;;
        legacy_repair_confirmation_required) print -r -- "An earlier Actanara installation needs confirmation. Run again with --yes to keep its data and settings and rebuild the runtime." ;;
        legacy_repair_answer_invalid) print -r -- "Please answer Y or N." ;;
        starting) print -r -- "Starting Actanara setup" ;;
        starting_update) print -r -- "Starting Actanara update" ;;
        step_failed) print -r -- "Could not prepare Actanara. Run again with ACTANARA_INSTALL_VERBOSE=1 for details." ;;
        options_conflict) print -r -- "Some preparation options cannot be used together. Run with --help." ;;
        runtime_incomplete) print -r -- "This Actanara folder is incomplete and cannot be updated safely." ;;
        service_unmatched) print -r -- "An Actanara background service exists, but its installation folder could not be found." ;;
        location_invalid) print -r -- "The saved Actanara location could not be read safely." ;;
        version_unavailable) print -r -- "The selected Actanara version could not be verified." ;;
        version_invalid) print -r -- "Choose an exact Actanara version ID." ;;
        offline_missing) print -r -- "The required Actanara files are not available offline." ;;
        cache_mismatch) print -r -- "This download folder belongs to a different Actanara source. Choose another folder." ;;
        git_missing) print -r -- "Git is required to download Actanara." ;;
        files_missing) print -r -- "Required Actanara installation files are missing." ;;
        shell_missing) print -r -- "zsh is required to start Actanara setup." ;;
        option_value_missing) print -r -- "One setup option is missing its value. Run with --help." ;;
        *) print -r -- "$key" ;;
      esac
      ;;
    *)
      case "$key" in
        preparing_folder) print -r -- "准备下载文件夹" ;;
        downloading) print -r -- "下载 Actanara" ;;
        checking_updates) print -r -- "检查所选 Actanara 版本" ;;
        preparing_files) print -r -- "准备安装文件" ;;
        cache_ready) print -r -- "已准备此前下载的文件" ;;
        source_plan_ready) print -r -- "已生成不可变源码计划，未执行缓存安装器" ;;
        cache_isolated) print -r -- "已保留原下载文件夹，将使用新的下载文件夹" ;;
        existing_ready) print -r -- "已保留现有 Actanara 数据，将直接更新" ;;
        legacy_repair_prompt) print -r -- "当前 Actanara 不能直接升级，是否进行覆盖安装？现有数据与设置不会丢失，只会重建运行环境与依赖。 [Y/n]" ;;
        legacy_repair_ready) print -r -- "将保留现有 Actanara 数据与设置，并重建运行环境与依赖" ;;
        legacy_repair_cancelled) print -r -- "已取消 Actanara 恢复，现有文件未作修改" ;;
        legacy_repair_confirmation_required) print -r -- "检测到较早版本的 Actanara。请添加 --yes 后重试，以保留现有数据与设置并重建运行环境。" ;;
        legacy_repair_answer_invalid) print -r -- "请输入 Y 或 N。" ;;
        starting) print -r -- "启动 Actanara 安装" ;;
        starting_update) print -r -- "启动 Actanara 更新" ;;
        step_failed) print -r -- "未能准备 Actanara，可设置 ACTANARA_INSTALL_VERBOSE=1 后重试以查看详情。" ;;
        options_conflict) print -r -- "部分准备选项不能同时使用，请通过 --help 查看用法。" ;;
        runtime_incomplete) print -r -- "此 Actanara 文件夹不完整，无法安全更新。" ;;
        service_unmatched) print -r -- "检测到 Actanara 后台服务，但未找到对应的安装文件夹。" ;;
        location_invalid) print -r -- "无法安全读取已保存的 Actanara 位置。" ;;
        version_unavailable) print -r -- "未能确认所选 Actanara 版本。" ;;
        version_invalid) print -r -- "请选择完整、准确的 Actanara 版本 ID。" ;;
        offline_missing) print -r -- "离线状态下缺少所需 Actanara 文件。" ;;
        cache_mismatch) print -r -- "此下载文件夹属于其他 Actanara 来源，请选择另一文件夹。" ;;
        git_missing) print -r -- "下载 Actanara 需要 Git。" ;;
        files_missing) print -r -- "缺少 Actanara 安装所需文件。" ;;
        shell_missing) print -r -- "启动 Actanara 安装需要 zsh。" ;;
        option_value_missing) print -r -- "一个安装选项缺少内容，请通过 --help 查看用法。" ;;
        *) print -r -- "$key" ;;
      esac
      ;;
  esac
}

bootstrap_command_label() {
  case "$*" in
    mkdir\ -p*) bootstrap_text preparing_folder ;;
    *" clone "*|clone\ *) bootstrap_text downloading ;;
    *" fetch "*|fetch\ *) bootstrap_text checking_updates ;;
    *) bootstrap_text preparing_files ;;
  esac
}

bootstrap_ok() {
  print -r -- "  ✓ $*"
}

bootstrap_fail() {
  print -r -- "  ✕ $(bootstrap_text step_failed)" >&2
}

bootstrap_problem() {
  local key="$1"
  local technical_message="$2"
  if [[ -n "$BOOTSTRAP_LOG_FILE" && -d "${BOOTSTRAP_LOG_FILE:h}" ]]; then
    print -r -- "ERROR: ${technical_message}" >> "$BOOTSTRAP_LOG_FILE"
  fi
  if [[ "${ACTANARA_INSTALL_VERBOSE:-0}" == "1" ]]; then
    print -r -- "ERROR: ${technical_message}" >&2
  else
    print -r -- "  ✕ $(bootstrap_text "$key")" >&2
  fi
}

prepare_bootstrap_log() {
  local cache_root="$1"
  /bin/chmod 700 "$cache_root"
  : > "$BOOTSTRAP_LOG_FILE"
  /bin/chmod 600 "$BOOTSTRAP_LOG_FILE"
}

log() {
  if [[ -n "$BOOTSTRAP_LOG_FILE" && -d "${BOOTSTRAP_LOG_FILE:h}" ]]; then
    print -r -- "==> $*" >> "$BOOTSTRAP_LOG_FILE"
  fi
  if [[ "${ACTANARA_INSTALL_VERBOSE:-0}" == "1" ]]; then
    print -r -- "==> $*"
  fi
}

run_cmd() {
  local label="$(bootstrap_command_label "$@")"
  if [[ "${ACTANARA_INSTALL_VERBOSE:-0}" == "1" ]]; then
    print -r -- "+ ${label}"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    bootstrap_ok "$label"
    return 0
  fi
  local rc=0
  if [[ -n "$BOOTSTRAP_LOG_FILE" && -d "${BOOTSTRAP_LOG_FILE:h}" ]]; then
    print -r -- "## ${label}" >> "$BOOTSTRAP_LOG_FILE"
    "$@" >/dev/null 2>&1 || {
      rc=$?
      print -r -- "ERROR: step exited with status ${rc}" >> "$BOOTSTRAP_LOG_FILE"
      bootstrap_fail
      return "$rc"
    }
  else
    "$@" >/dev/null 2>&1 || {
      rc=$?
      bootstrap_fail
      return "$rc"
    }
  fi
  bootstrap_ok "$label"
}

git_exec() (
  unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE
  unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  unset GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
  unset GIT_CEILING_DIRECTORIES GIT_DISCOVERY_ACROSS_FILESYSTEM GIT_PREFIX
  unset GIT_ALLOW_PROTOCOL GIT_PROTOCOL_FROM_USER GIT_EXEC_PATH GIT_TEMPLATE_DIR
  unset GIT_SSH GIT_SSH_COMMAND
  if [[ "$OFFLINE" == "1" ]]; then
    # A cached partial clone may otherwise fetch a missing promisor object from
    # inside object inspection or checkout even though bootstrap never invokes
    # `git fetch`.
    # Git >=2.45 honors the no-lazy-fetch request; the empty protocol allowlist
    # is an independent older-Git-compatible transport barrier. Hooks and
    # user/system Git configuration are also excluded.
    GIT_NO_LAZY_FETCH=1 \
    GIT_ALLOW_PROTOCOL= \
    GIT_NO_REPLACE_OBJECTS=1 \
    GIT_TERMINAL_PROMPT=0 \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
      "${GIT_BIN}" \
        -c protocol.allow=never \
        -c protocol.ext.allow=never \
        -c core.hooksPath=/dev/null \
        -c core.fsmonitor=false \
        "$@"
    return $?
  fi
  GIT_NO_REPLACE_OBJECTS=1 \
  GIT_TERMINAL_PROMPT=0 \
  GIT_CONFIG_NOSYSTEM=1 \
  GIT_CONFIG_GLOBAL=/dev/null \
    "${GIT_BIN}" \
      -c protocol.ext.allow=never \
      -c core.hooksPath=/dev/null \
      -c core.fsmonitor=false \
      "$@"
)

cache_git_exec() {
  local root="$1"
  shift
  git_exec \
    --git-dir="${root}/.git" \
    --work-tree="${root}" \
    "$@"
}

validate_cache_layout() {
  local root="$1"
  local physical_root=""
  if [[ ! -d "$root" || -L "$root" || ! -d "$root/.git" || -L "$root/.git" ]]; then
    bootstrap_problem cache_mismatch "installer cache layout is unsafe"
    return 2
  fi
  physical_root="$(cd "$root" 2>/dev/null && pwd -P)" || {
    bootstrap_problem cache_mismatch "installer cache layout could not be resolved"
    return 2
  }
  if [[ "$physical_root" != "$root" ]]; then
    bootstrap_problem cache_mismatch "installer cache source escaped the selected cache root"
    return 2
  fi
}

run_git_cmd() {
  local label="$(bootstrap_command_label "$@")"
  if [[ "${ACTANARA_INSTALL_VERBOSE:-0}" == "1" ]]; then
    print -r -- "+ ${label}"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    bootstrap_ok "$label"
    return 0
  fi
  local rc=0
  if [[ -n "$BOOTSTRAP_LOG_FILE" && -d "${BOOTSTRAP_LOG_FILE:h}" ]]; then
    print -r -- "## ${label}" >> "$BOOTSTRAP_LOG_FILE"
    git_exec "$@" >/dev/null 2>&1 || {
      rc=$?
      print -r -- "ERROR: download step exited with status ${rc}" >> "$BOOTSTRAP_LOG_FILE"
      bootstrap_fail
      return "$rc"
    }
  else
    git_exec "$@" >/dev/null 2>&1 || {
      rc=$?
      bootstrap_fail
      return "$rc"
    }
  fi
  bootstrap_ok "$label"
}

run_cache_git_cmd() {
  local root="$1"
  shift
  run_git_cmd \
    --git-dir="${root}/.git" \
    --work-tree="${root}" \
    "$@"
}

configure_sparse_checkout() {
  local root="$1"
  if [[ "$CACHE_SOURCE" != "1" ]]; then
    return 0
  fi
  log "Configuring installer source sparse checkout"
  run_cache_git_cmd "${root}" sparse-checkout init --no-cone
  run_cache_git_cmd "${root}" sparse-checkout set "${SPARSE_PATHS[@]}"
}

installer_arg_present() {
  local expected="$1"
  local argument=""
  for argument in "${INSTALL_ARGS[@]}"; do
    if [[ "$argument" == "$expected" ]]; then
      return 0
    fi
  done
  return 1
}

verify_offline_source_cache() {
  local root="$1"
  local ref="$2"
  local resolved_object=""
  resolved_object="$(cache_git_exec "${root}" rev-parse --verify "${ref}^{commit}" 2>/dev/null || true)"
  resolved_object="${resolved_object:l}"
  if [[ "$resolved_object" != "$ref" ]]; then
    bootstrap_problem version_unavailable "resolved source object does not match required commit ${ref}"
    return 2
  fi

  # `git archive` is intentionally not used here: on a promisor/partial clone,
  # Git may demand unrelated public-source blobs even when every object in the
  # installer sparse payload is already cached. Inspect only the selected tree
  # entries and then prove that each referenced blob is locally readable.
  # git_exec forbids lazy fetch and every transport for every probe.
  local required_path=""
  for required_path in "${OFFLINE_SOURCE_PATHS[@]}"; do
    if ! cache_git_exec "${root}" cat-file -e "${ref}:${required_path}" 2>/dev/null; then
      bootstrap_problem offline_missing "offline source cache is incomplete for commit ${ref}"
      return 2
    fi
  done

  local inventory_file=""
  inventory_file="$(mktemp "${TMPDIR:-/tmp}/actanara-offline-cache.XXXXXXXX")" || {
    bootstrap_problem offline_missing "unable to create a private offline source inventory"
    return 2
  }
  if ! cache_git_exec "${root}" ls-tree -r -z --full-tree "${ref}" -- "${OFFLINE_SOURCE_PATHS[@]}" > "${inventory_file}"; then
    rm -f "${inventory_file}"
    bootstrap_problem offline_missing "offline source cache inventory is incomplete for commit ${ref}"
    return 2
  fi

  local row=""
  local metadata=""
  local file_mode=""
  local remainder=""
  local object_type=""
  local object_id=""
  local object_count=0
  local inventory_invalid=0
  while IFS= read -r -d $'\0' row; do
    metadata="${row%%$'\t'*}"
    if [[ "$metadata" == "$row" ]]; then
      inventory_invalid=1
      break
    fi
    file_mode="${metadata%% *}"
    remainder="${metadata#* }"
    object_type="${remainder%% *}"
    object_id="${remainder##* }"
    case "${file_mode}:${object_type}" in
      100644:blob|100755:blob|120000:blob)
        ;;
      *)
        inventory_invalid=1
        break
        ;;
    esac
    if ! is_full_commit_id "${object_id}" \
      || ! cache_git_exec "${root}" cat-file blob "${object_id}" > /dev/null 2>&1; then
      inventory_invalid=1
      break
    fi
    object_count=$((object_count + 1))
  done < "${inventory_file}"
  rm -f "${inventory_file}"
  if [[ "$inventory_invalid" == "1" || "$object_count" -eq 0 ]]; then
    bootstrap_problem offline_missing "offline source cache content is incomplete for commit ${ref}"
    return 2
  fi
}

require_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == --* ]]; then
    bootstrap_problem option_value_missing "${option} requires a value"
    exit 2
  fi
}

is_full_commit_id() {
  local value="$1"
  [[ "$value" =~ "^[[:xdigit:]]{40}$|^[[:xdigit:]]{64}$" ]]
}

extract_json_string() {
  local payload="$1"
  local field="$2"
  local value=""
  if [[ -x "$PLUTIL_BIN" ]]; then
    if value="$(print -rn -- "$payload" | "$PLUTIL_BIN" -extract "$field" raw -o - - 2>/dev/null)"; then
      [[ -n "$value" ]] || return 1
      print -rn -- "$value"
      return 0
    fi
  fi

  # Safe dependency-free fallback: accept one plain JSON string only. Escaped
  # values are deliberately rejected rather than decoded by shell code.
  local flattened=""
  local occurrences=""
  flattened="$(print -rn -- "$payload" | /usr/bin/tr '\r\n' '  ' 2>/dev/null)" || return 1
  occurrences="$(print -rn -- "$flattened" | /usr/bin/grep -o "\"${field}\"[[:space:]]*:" 2>/dev/null | /usr/bin/wc -l | /usr/bin/tr -d '[:space:]')" || return 1
  [[ "$occurrences" == "1" ]] || return 1
  value="$(print -rn -- "$flattened" | /usr/bin/sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\\([^\"\\\\]*\\)\".*/\\1/p")" || return 1
  [[ -n "$value" ]] || return 1
  print -rn -- "$value"
}

installer_option_takes_value() {
  case "$1" in
    --runtime|--diary-output|--desktop-diary-link|--shell-path-file|--reports-output|--snapshots-output|--archives-output|--source-root|--python|--dashboard-port|--dashboard-host|--rag-embedding-mode|--rag-local-model|--rag-local-dimension|--llm-provider|--llm-endpoint|--llm-model|--llm-api-key-env|--rag-cloud-provider|--rag-cloud-endpoint|--rag-cloud-model|--rag-cloud-dimension|--rag-cloud-api-key-env|--language)
      return 0
      ;;
  esac
  return 1
}

parse_installer_safety_args() {
  INSTALLER_UPGRADE=0
  INSTALLER_REPAIR=0
  INSTALLER_SPECIAL_UPDATE=0
  INSTALLER_RUNTIME="${ACTANARA_INSTALL_RUNTIME:-$HOME/.actanara}"
  INSTALLER_RUNTIME_ARG=0
  INSTALLER_RUNTIME_ENV=0
  INSTALLER_YES=0
  if [[ -n "${ACTANARA_INSTALL_RUNTIME:-}" ]]; then
    INSTALLER_RUNTIME_ENV=1
  fi
  local index=1
  local arg=""
  local value=""
  while (( index <= ${#INSTALL_ARGS[@]} )); do
    arg="${INSTALL_ARGS[$index]}"
    case "$arg" in
      --upgrade)
        INSTALLER_UPGRADE=1
        ;;
      --source-only|--sync-runtime-source)
        INSTALLER_UPGRADE=1
        INSTALLER_SPECIAL_UPDATE=1
        ;;
      --repair-existing)
        INSTALLER_UPGRADE=1
        INSTALLER_REPAIR=1
        ;;
      --yes)
        INSTALLER_YES=1
        ;;
      --runtime)
        if (( index >= ${#INSTALL_ARGS[@]} )); then
          bootstrap_problem option_value_missing "--runtime requires a value"
          return 2
        fi
        index=$(( index + 1 ))
        value="${INSTALL_ARGS[$index]}"
        if [[ -z "$value" || "$value" == --* ]]; then
          bootstrap_problem option_value_missing "--runtime requires a path value"
          return 2
        fi
        INSTALLER_RUNTIME="$value"
        INSTALLER_RUNTIME_ARG=1
        ;;
      *)
        # A token consumed as another option's value must never be mistaken for
        # an update approval flag.
        if ! installer_option_takes_value "$arg"; then
          index=$(( index + 1 ))
          continue
        fi
        if (( index < ${#INSTALL_ARGS[@]} )); then
          index=$(( index + 1 ))
        fi
        ;;
    esac
    index=$(( index + 1 ))
  done
}

ensure_installer_flag_once() {
  local expected="$1"
  local argument=""
  local found=0
  local normalized=()
  local index=1
  while (( index <= ${#INSTALL_ARGS[@]} )); do
    argument="${INSTALL_ARGS[$index]}"
    if installer_option_takes_value "$argument"; then
      normalized+=("$argument")
      if (( index < ${#INSTALL_ARGS[@]} )); then
        index=$(( index + 1 ))
        normalized+=("${INSTALL_ARGS[$index]}")
      fi
      index=$(( index + 1 ))
      continue
    fi
    if [[ "$argument" == "$expected" ]]; then
      if [[ "$found" == "0" ]]; then
        normalized+=("$argument")
        found=1
      fi
      index=$(( index + 1 ))
      continue
    fi
    normalized+=("$argument")
    index=$(( index + 1 ))
  done
  if [[ "$found" == "0" ]]; then
    normalized+=("$expected")
  fi
  INSTALL_ARGS=("${normalized[@]}")
}

remove_installer_flag() {
  local unwanted="$1"
  local argument=""
  local normalized=()
  local index=1
  while (( index <= ${#INSTALL_ARGS[@]} )); do
    argument="${INSTALL_ARGS[$index]}"
    if installer_option_takes_value "$argument"; then
      normalized+=("$argument")
      if (( index < ${#INSTALL_ARGS[@]} )); then
        index=$(( index + 1 ))
        normalized+=("${INSTALL_ARGS[$index]}")
      fi
    elif [[ "$argument" != "$unwanted" ]]; then
      normalized+=("$argument")
    fi
    index=$(( index + 1 ))
  done
  INSTALL_ARGS=("${normalized[@]}")
}

normalize_runtime_candidate() {
  local candidate="$1"
  if [[ "$candidate" == "~" ]]; then
    candidate="$HOME"
  elif [[ "$candidate" == "~/"* ]]; then
    candidate="${HOME}/${candidate#\~/}"
  elif [[ "$candidate" == "~"* ]]; then
    return 1
  fi
  print -rn -- "${candidate:a}"
}

canonical_source_url() {
  local value="$1"
  case "$value" in
    "https://github.com/Neo-Isshin/actanara"|"https://github.com/Neo-Isshin/actanara/"|"https://github.com/Neo-Isshin/actanara.git"|"https://github.com/Neo-Isshin/actanara.git/")
      print -rn -- "$DEFAULT_SOURCE_URL"
      ;;
    *)
      print -rn -- "$value"
      ;;
  esac
}

source_urls_match() {
  local existing="$1"
  local requested="$2"
  [[ "$(canonical_source_url "$existing")" == "$(canonical_source_url "$requested")" ]]
}

runtime_has_actanara_marker() {
  local candidate="$1"
  local marker=""
  for marker in \
    "${candidate}/app/source" \
    "${candidate}/.venv" \
    "${candidate}/config/runtime.json" \
    "${candidate}/config/settings.json" \
    "${candidate}/data/actanara_data.sqlite3" \
    "${candidate}/bin/actanara"; do
    if [[ -e "$marker" || -L "$marker" ]]; then
      return 0
    fi
  done
  return 1
}

runtime_repair_configuration_pending_status() {
  local candidate="$1"
  local marker="${candidate}/app/.repair-configuration-pending"
  local marker_size=""
  local marker_identity=""
  local expected_uid=""
  local tx_id=""
  if [[ ! -e "$marker" && ! -L "$marker" ]]; then
    return 1
  fi
  if [[ ! -f "$marker" || -L "$marker" ]]; then
    return 2
  fi
  marker_identity="$(/usr/bin/stat -f '%l:%Lp:%u' "$marker" 2>/dev/null)" || return 2
  expected_uid="$(/usr/bin/id -u 2>/dev/null)" || return 2
  [[ "$marker_identity" == "1:600:${expected_uid}" ]] || return 2
  marker_size="$(/usr/bin/wc -c < "$marker" | /usr/bin/tr -d '[:space:]')" || return 2
  [[ "$marker_size" =~ "^[0-9]+$" ]] || return 2
  (( marker_size >= 2 && marker_size <= 129 )) || return 2
  tx_id="$(<"$marker")" || return 2
  [[ "$tx_id" =~ "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" ]] || return 2
  (( marker_size == ${#tx_id} + 1 )) || return 2
  return 0
}

runtime_root_is_unsafe() {
  local candidate="$1"
  [[ -L "$candidate" ]] && return 0
  [[ -e "$candidate" && ! -d "$candidate" ]] && return 0
  return 1
}

runtime_source_manifest_path() {
  local candidate="$1"
  local source_pointer="${candidate}/app/source"
  local source_raw=""
  local generation=""
  local source_target=""
  local manifest=""
  local manifest_size=""
  [[ -d "$candidate" && ! -L "$candidate" ]] || return 1
  [[ -d "${candidate}/app" && ! -L "${candidate}/app" ]] || return 1
  if [[ -L "$source_pointer" ]]; then
    source_raw="$(/usr/bin/readlink "$source_pointer" 2>/dev/null)" || return 1
    [[ "$source_raw" == releases/* ]] || return 1
    generation="${source_raw#releases/}"
    [[ -n "$generation" && "$generation" != "." && "$generation" != ".." && "$generation" != */* ]] || return 1
    [[ -d "${candidate}/app/releases" && ! -L "${candidate}/app/releases" ]] || return 1
    source_target="${candidate}/app/releases/${generation}"
  elif [[ -d "$source_pointer" ]]; then
    source_target="$source_pointer"
  else
    return 1
  fi
  [[ -d "$source_target" && ! -L "$source_target" ]] || return 1
  manifest="${source_target}/.actanara-runtime-source.json"
  [[ -f "$manifest" && ! -L "$manifest" ]] || return 1
  manifest_size="$(/usr/bin/wc -c < "$manifest" | /usr/bin/tr -d '[:space:]')" || return 1
  [[ "$manifest_size" =~ "^[0-9]+$" ]] || return 1
  (( manifest_size > 0 && manifest_size <= 1048576 )) || return 1
  print -rn -- "$manifest"
}

runtime_has_explicit_foreign_manifest() {
  local candidate="$1"
  local manifest=""
  local manifest_payload=""
  local product=""
  manifest="$(runtime_source_manifest_path "$candidate")" || return 1
  manifest_payload="$(<"$manifest")" || return 1
  product="$(extract_json_string "$manifest_payload" "product")" || return 1
  [[ "$product" != "actanara" ]]
}

runtime_is_updateable_actanara() {
  local candidate="$1"
  local manifest=""
  local manifest_payload=""
  local product=""
  local deployment_mode=""
  [[ -d "$candidate" && ! -L "$candidate" ]] || return 1
  [[ -d "${candidate}/config" && ! -L "${candidate}/config" ]] || return 1
  [[ -d "${candidate}/app" && ! -L "${candidate}/app" ]] || return 1
  [[ -d "${candidate}/app/releases" && ! -L "${candidate}/app/releases" ]] || return 1
  [[ -f "${candidate}/config/settings.json" && ! -L "${candidate}/config/settings.json" ]] || return 1
  [[ -f "${candidate}/config/runtime.json" && ! -L "${candidate}/config/runtime.json" ]] || return 1
  [[ -L "${candidate}/app/source" ]] || return 1
  manifest="$(runtime_source_manifest_path "$candidate")" || return 1
  manifest_payload="$(<"$manifest")" || return 1
  product="$(extract_json_string "$manifest_payload" "product")" || return 1
  deployment_mode="$(extract_json_string "$manifest_payload" "deploymentMode")" || return 1
  [[ "$product" == "actanara" && "$deployment_mode" == "release-symlink" ]]
}

confirm_legacy_runtime_repair() {
  if [[ "$INSTALLER_YES" == "1" ]]; then
    return 0
  fi
  local answer=""
  while true; do
    if ! print -rn -- "$(bootstrap_text legacy_repair_prompt) " 2>/dev/null > /dev/tty; then
      bootstrap_problem legacy_repair_confirmation_required "legacy Actanara repair requires confirmation or --yes"
      return 2
    fi
    if ! IFS= read -r answer 2>/dev/null < /dev/tty; then
      bootstrap_problem legacy_repair_confirmation_required "legacy Actanara repair confirmation could not be read; rerun with --yes"
      return 2
    fi
    case "${answer:l}" in
      ""|y|yes)
        return 0
        ;;
      n|no)
        print -r -- "  • $(bootstrap_text legacy_repair_cancelled)"
        return 3
        ;;
      *)
        print -r -- "$(bootstrap_text legacy_repair_answer_invalid)" 2>/dev/null > /dev/tty
        ;;
    esac
  done
}

read_saved_runtime_location() {
  local location_file="${ACTANARA_LOCATION_FILE:-$HOME/.config/actanara/location.json}"
  local location_payload=""
  local pointed_runtime=""
  if ! location_file="$(normalize_runtime_candidate "$location_file")"; then
    bootstrap_problem location_invalid "cannot safely resolve the saved Actanara location"
    return 2
  fi
  if [[ -e "$location_file" || -L "$location_file" ]]; then
    if [[ ! -f "$location_file" || -L "$location_file" ]]; then
      bootstrap_problem location_invalid "saved Actanara location is not a regular file: ${location_file}"
      return 2
    fi
    local location_size=""
    location_size="$(/usr/bin/wc -c < "$location_file" | /usr/bin/tr -d '[:space:]')" || return 2
    if [[ ! "$location_size" =~ "^[0-9]+$" ]] || (( location_size > 65536 )); then
      bootstrap_problem location_invalid "saved Actanara location is invalid: ${location_file}"
      return 2
    fi
    location_payload="$(<"$location_file")" || return 2
    if ! pointed_runtime="$(extract_json_string "$location_payload" "actanaraHome")"; then
      bootstrap_problem location_invalid "saved Actanara location could not be parsed: ${location_file}"
      return 2
    fi
    if [[ "$pointed_runtime" != /* || "$pointed_runtime" == *$'\n'* || "$pointed_runtime" == *$'\r'* ]]; then
      bootstrap_problem location_invalid "saved Actanara location is not an absolute path: ${location_file}"
      return 2
    fi
  fi
  print -rn -- "$pointed_runtime"
}

select_installer_mode_before_source_writes() {
  parse_installer_safety_args || return $?
  local selected_runtime="$INSTALLER_RUNTIME"
  local pointed_runtime=""
  local normalized_runtime=""
  local repair_pending_status=1

  if [[ "$INSTALLER_RUNTIME_ARG" != "1" && "$INSTALLER_RUNTIME_ENV" != "1" ]]; then
    if [[ -n "${ACTANARA_HOME:-}" ]]; then
      selected_runtime="$ACTANARA_HOME"
    else
      pointed_runtime="$(read_saved_runtime_location)" || return $?
      if [[ -n "$pointed_runtime" ]]; then
        selected_runtime="$pointed_runtime"
      fi
    fi
  fi
  if ! normalized_runtime="$(normalize_runtime_candidate "$selected_runtime")"; then
    bootstrap_problem location_invalid "cannot safely resolve the selected Actanara folder"
    return 2
  fi
  if [[ "$INSTALLER_RUNTIME_ARG" != "1" ]]; then
    INSTALL_ARGS+=(--runtime "$normalized_runtime")
  fi

  if runtime_root_is_unsafe "$normalized_runtime"; then
    bootstrap_problem runtime_incomplete "selected Actanara Runtime root is a symlink or non-directory: ${normalized_runtime}"
    return 2
  fi
  if runtime_has_actanara_marker "$normalized_runtime"; then
    if runtime_has_explicit_foreign_manifest "$normalized_runtime"; then
      bootstrap_problem runtime_incomplete "selected Runtime source manifest belongs to another product: ${normalized_runtime}"
      return 2
    fi
    set +e
    runtime_repair_configuration_pending_status "$normalized_runtime"
    repair_pending_status=$?
    set -e
    if [[ "$repair_pending_status" == "2" ]]; then
      bootstrap_problem runtime_incomplete "selected Runtime repair marker is unsafe: ${normalized_runtime}"
      return 2
    fi
    if [[ "$repair_pending_status" != "0" ]] && runtime_is_updateable_actanara "$normalized_runtime"; then
      if [[ "$INSTALLER_UPGRADE" == "1" ]]; then
        return 0
      fi
      ensure_installer_flag_once --upgrade
      ensure_installer_flag_once --yes
      INSTALLER_YES=1
      INSTALLER_UPGRADE=1
      bootstrap_ok "$(bootstrap_text existing_ready)"
      return 0
    fi

    if [[ "$INSTALLER_SPECIAL_UPDATE" == "1" ]]; then
      return 0
    fi
    confirm_legacy_runtime_repair || return $?
    # A legacy Runtime cannot use the ordinary update contract. Convert an
    # explicit --upgrade request into the confirmed repair mode instead of
    # forwarding mutually exclusive installer flags.
    remove_installer_flag --upgrade
    ensure_installer_flag_once --repair-existing
    ensure_installer_flag_once --yes
    INSTALLER_REPAIR=1
    INSTALLER_UPGRADE=1
    INSTALLER_YES=1
    bootstrap_ok "$(bootstrap_text legacy_repair_ready)"
    return 0
  fi

  if [[ "$INSTALLER_UPGRADE" == "1" ]]; then
    return 0
  fi

  local launch_agent=""
  for launch_agent in "$HOME"/Library/LaunchAgents/com.actanara*.plist(N); do
    if [[ -e "$launch_agent" || -L "$launch_agent" ]]; then
      bootstrap_problem service_unmatched "existing Actanara background service has no selected Runtime: ${launch_agent}"
      return 2
    fi
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-root)
      require_value "$1" "${2:-}"
      SOURCE_ROOT="$2"
      shift 2
      ;;
    --source-url)
      require_value "$1" "${2:-}"
      SOURCE_URL="$2"
      shift 2
      ;;
    --ref)
      require_value "$1" "${2:-}"
      SOURCE_REF="$2"
      shift 2
      ;;
    --cache-root)
      require_value "$1" "${2:-}"
      DEFAULT_CACHE_ROOT="$2"
      CACHE_ROOT_EXPLICIT=1
      shift 2
      ;;
    --git)
      require_value "$1" "${2:-}"
      GIT_BIN="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      INSTALL_ARGS+=("--dry-run")
      shift
      ;;
    --offline)
      OFFLINE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        INSTALL_ARGS+=("$1")
        shift
      done
      ;;
    *)
      INSTALL_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ "$OFFLINE" == "1" ]] && ! installer_arg_present "--offline"; then
  INSTALL_ARGS+=("--offline")
fi

for (( bootstrap_index = 1; bootstrap_index <= ${#INSTALL_ARGS[@]}; bootstrap_index++ )); do
  if [[ "${INSTALL_ARGS[$bootstrap_index]}" == "--language" ]] && (( bootstrap_index < ${#INSTALL_ARGS[@]} )); then
    BOOTSTRAP_LANGUAGE="${INSTALL_ARGS[$((bootstrap_index + 1))]}"
    break
  fi
done

if [[ -z "$SOURCE_ROOT" && -z "$SOURCE_URL" ]]; then
  checkout_candidate="${BOOTSTRAP_DIR:h}"
  # Only a bootstrap executed from a real file may infer its adjacent checkout.
  # Hosted/stdin execution must not silently adopt an unrelated current working tree.
  if [[ -f "$0" && -f "${checkout_candidate}/pyproject.toml" && -x "${checkout_candidate}/install/install.sh" ]]; then
    SOURCE_ROOT="$checkout_candidate"
  fi
fi

if [[ -n "$SOURCE_ROOT" && -n "$SOURCE_REF" ]]; then
  bootstrap_problem options_conflict "--source-root and --ref cannot be combined"
  exit 2
fi

# Runtime selection intentionally runs before cache directory creation, clone,
# fetch, sparse-checkout, or any other installer-owned source write.
runtime_selection_status=0
select_installer_mode_before_source_writes || runtime_selection_status=$?
if [[ "$runtime_selection_status" == "3" ]]; then
  exit 0
elif [[ "$runtime_selection_status" != "0" ]]; then
  exit "$runtime_selection_status"
fi

if [[ -z "$SOURCE_ROOT" ]]; then
  if [[ -z "$SOURCE_URL" ]]; then
    SOURCE_URL="$DEFAULT_SOURCE_URL"
  fi
  if ! command -v "$GIT_BIN" >/dev/null 2>&1 && [[ ! -x "$GIT_BIN" ]]; then
    bootstrap_problem git_missing "Git executable not found: ${GIT_BIN}"
    exit 2
  fi
  if [[ -z "$SOURCE_REF" ]]; then
    if [[ "$OFFLINE" == "1" ]]; then
      bootstrap_problem offline_missing "offline setup requires a local source or exact cached version"
      exit 2
    fi
    if ! source_urls_match "$SOURCE_URL" "$DEFAULT_SOURCE_URL"; then
      bootstrap_problem version_invalid "custom source URL requires an exact version ID"
      exit 2
    fi
    bootstrap_problem version_invalid "network setup requires a Release-pinned installer or an exact version ID"
    exit 2
  elif ! is_full_commit_id "$SOURCE_REF"; then
    bootstrap_problem version_invalid "remote version must be a full 40- or 64-character commit ID"
    exit 2
  else
    SOURCE_REF="${SOURCE_REF:l}"
  fi
  cache_root="${DEFAULT_CACHE_ROOT:A}"
  SOURCE_ROOT="${cache_root}/source"
  BOOTSTRAP_LOG_FILE="${cache_root}/bootstrap.log"
  CACHE_SOURCE=1
  if [[ -d "${SOURCE_ROOT}/.git" ]]; then
    validate_cache_layout "${SOURCE_ROOT}" || exit $?
    existing_source_url="$(cache_git_exec "${SOURCE_ROOT}" remote get-url origin 2>/dev/null || true)"
    if ! source_urls_match "$existing_source_url" "$SOURCE_URL"; then
      if [[ "$CACHE_ROOT_EXPLICIT" == "0" ]] && source_urls_match "$SOURCE_URL" "$DEFAULT_SOURCE_URL"; then
        cache_root="${cache_root}/official-release"
        SOURCE_ROOT="${cache_root}/source"
        BOOTSTRAP_LOG_FILE="${cache_root}/bootstrap.log"
        bootstrap_ok "$(bootstrap_text cache_isolated)"
      else
        bootstrap_problem cache_mismatch "download cache source does not match requested source"
        exit 2
      fi
    fi
  fi
  if [[ "$DRY_RUN" != "1" && -d "$cache_root" ]]; then
    prepare_bootstrap_log "$cache_root"
  fi
  if [[ -d "${SOURCE_ROOT}/.git" ]]; then
    validate_cache_layout "${SOURCE_ROOT}" || exit $?
    existing_source_url="$(cache_git_exec "${SOURCE_ROOT}" remote get-url origin 2>/dev/null || true)"
    if ! source_urls_match "$existing_source_url" "$SOURCE_URL"; then
      bootstrap_problem cache_mismatch "download cache source does not match requested source"
      exit 2
    fi
    log "Updating existing source checkout: ${SOURCE_ROOT}"
    if [[ "$OFFLINE" != "1" ]]; then
      configure_sparse_checkout "${SOURCE_ROOT}"
      run_cache_git_cmd "${SOURCE_ROOT}" fetch --force origin "${SOURCE_REF}"
      if [[ "$DRY_RUN" != "1" ]]; then
        fetched_ref="$(cache_git_exec "${SOURCE_ROOT}" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)"
        fetched_ref="${fetched_ref:l}"
        if [[ "$fetched_ref" != "$SOURCE_REF" ]]; then
          bootstrap_problem version_unavailable "fetched source object does not match required commit ${SOURCE_REF}"
          exit 2
        fi
      fi
    else
      verify_offline_source_cache "${SOURCE_ROOT}" "${SOURCE_REF}" || exit $?
      configure_sparse_checkout "${SOURCE_ROOT}"
      log "Offline mode: using the existing installer-owned source cache without fetch"
      bootstrap_ok "$(bootstrap_text cache_ready)"
    fi
  else
    if [[ "$OFFLINE" == "1" ]]; then
      bootstrap_problem offline_missing "offline source cache is missing: ${SOURCE_ROOT}"
      exit 2
    fi
    log "Downloading the selected Actanara version"
    run_cmd mkdir -p "${cache_root}"
    if [[ "$DRY_RUN" != "1" ]]; then
      prepare_bootstrap_log "$cache_root"
    fi
    run_git_cmd clone --filter=blob:none --sparse --no-checkout "${SOURCE_URL}" "${SOURCE_ROOT}"
    configure_sparse_checkout "${SOURCE_ROOT}"
    run_cache_git_cmd "${SOURCE_ROOT}" fetch --force origin "${SOURCE_REF}"
    if [[ "$DRY_RUN" != "1" ]]; then
      fetched_ref="$(cache_git_exec "${SOURCE_ROOT}" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)"
      fetched_ref="${fetched_ref:l}"
      if [[ "$fetched_ref" != "$SOURCE_REF" ]]; then
        bootstrap_problem version_unavailable "fetched source object does not match required commit ${SOURCE_REF}"
        exit 2
      fi
    fi
  fi
fi

if [[ "$CACHE_SOURCE" == "1" && "$DRY_RUN" == "1" ]]; then
  log "Dry-run planned immutable source commit ${SOURCE_REF}; no cached worktree was executed"
  bootstrap_ok "$(bootstrap_text source_plan_ready)"
  exit 0
fi

SOURCE_ROOT="${SOURCE_ROOT:A}"
SELECTED_REF=""
if [[ "$CACHE_SOURCE" == "1" ]]; then
  SELECTED_REF="$SOURCE_REF"
  if [[ "$DRY_RUN" != "1" ]]; then
    resolved_object="$(cache_git_exec "${SOURCE_ROOT}" rev-parse --verify "${SOURCE_REF}^{commit}" 2>/dev/null || true)"
    resolved_object="${resolved_object:l}"
    if [[ "$resolved_object" != "$SOURCE_REF" ]]; then
      bootstrap_problem version_unavailable "resolved source object does not match required commit ${SOURCE_REF}"
      exit 2
    fi
  fi
  log "Selecting immutable source commit: ${SOURCE_REF}"
  run_cache_git_cmd "${SOURCE_ROOT}" checkout --detach "${SOURCE_REF}"
fi

if [[ "$CACHE_SOURCE" == "1" ]]; then
  selected_ref="$SELECTED_REF"
  log "Resetting installer-owned source checkout to ${selected_ref}"
  run_cache_git_cmd "${SOURCE_ROOT}" reset --hard "${selected_ref}"
  log "Cleaning untracked artifacts from installer-owned source checkout"
  run_cache_git_cmd "${SOURCE_ROOT}" clean -fdx --
fi

if [[ "$DRY_RUN" == "1" && ! -f "${SOURCE_ROOT}/install/install.sh" ]]; then
  log "Dry-run assumes cloned source will contain install/install.sh"
elif [[ ! -f "${SOURCE_ROOT}/install/install.sh" ]]; then
  bootstrap_problem files_missing "main setup script not found under source root: ${SOURCE_ROOT}"
  exit 2
fi

log "Running installer from source root: ${SOURCE_ROOT}"
if [[ -z "$ZSH_BIN" || ! -x "$ZSH_BIN" ]]; then
  ZSH_BIN="$(command -v zsh 2>/dev/null || true)"
fi
if [[ -z "$ZSH_BIN" && -x "/bin/zsh" ]]; then
  ZSH_BIN="/bin/zsh"
fi
if [[ -z "$ZSH_BIN" ]]; then
  bootstrap_problem shell_missing "zsh executable not found"
  exit 2
fi
bootstrap_start_key="starting"
if [[ "$INSTALLER_UPGRADE" == "1" ]]; then
  bootstrap_start_key="starting_update"
fi
if [[ "${ACTANARA_INSTALL_VERBOSE:-0}" == "1" ]]; then
  print -r -- "+ $(bootstrap_text "$bootstrap_start_key")"
fi
bootstrap_ok "$(bootstrap_text "$bootstrap_start_key")"
if [[ "$DRY_RUN" == "1" ]]; then
  if [[ -f "${SOURCE_ROOT}/install/install.sh" ]]; then
    "$ZSH_BIN" "${SOURCE_ROOT}/install/install.sh" --source-root "${SOURCE_ROOT}" "${INSTALL_ARGS[@]}"
  fi
else
  "$ZSH_BIN" "${SOURCE_ROOT}/install/install.sh" --source-root "${SOURCE_ROOT}" "${INSTALL_ARGS[@]}"
fi
fi
