#!/bin/sh
# Keep the hosted entrypoint inside one compound command. A truncated stream
# cannot parse the closing `fi`, so no prefix of the setup is executed.
if true; then
set -eu
umask 077

DEFAULT_SOURCE_URL="https://github.com/Neo-Isshin/actanara.git"
SOURCE_ROOT=""
SOURCE_URL="${ACTANARA_INSTALL_SOURCE_URL:-$DEFAULT_SOURCE_URL}"
SOURCE_REF="${ACTANARA_INSTALL_REF:-}"
CACHE_ROOT="${ACTANARA_INSTALL_CACHE_ROOT:-$HOME/.cache/actanara/installer}"
GIT_BIN="${ACTANARA_INSTALL_GIT:-git}"
OFFLINE=0
TEMP_ROOT=""
DOWNLOADED_ADAPTER=""

setup_usage() {
  cat <<'EOF'
Actanara cross-platform setup entrypoint

Usage:
  curl -fsSL https://github.com/Neo-Isshin/actanara/releases/latest/download/install.sh | sh

The Release-pinned command selects the macOS or Linux installer. All options
are forwarded to the selected platform adapter.
EOF
}

setup_error() {
  printf 'Actanara setup: %s\n' "$*" >&2
}

cleanup_setup_temp() {
  if [ -n "$TEMP_ROOT" ] && [ -d "$TEMP_ROOT" ]; then
    rm -rf -- "$TEMP_ROOT"
  fi
}

trap cleanup_setup_temp 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

require_option_value() {
  option="$1"
  value="${2:-}"
  case "$value" in
    ""|--*)
      setup_error "$option requires a value"
      exit 2
      ;;
  esac
}

parse_setup_options() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --source-root)
        require_option_value "$1" "${2:-}"
        SOURCE_ROOT="$2"
        shift 2
        ;;
      --source-url)
        require_option_value "$1" "${2:-}"
        SOURCE_URL="$2"
        shift 2
        ;;
      --ref)
        require_option_value "$1" "${2:-}"
        SOURCE_REF="$2"
        shift 2
        ;;
      --cache-root)
        require_option_value "$1" "${2:-}"
        CACHE_ROOT="$2"
        shift 2
        ;;
      --git)
        require_option_value "$1" "${2:-}"
        GIT_BIN="$2"
        shift 2
        ;;
      --offline)
        OFFLINE=1
        shift
        ;;
      -h|--help)
        setup_usage
        exit 0
        ;;
      --)
        break
        ;;
      *)
        shift
        ;;
    esac
  done
}

is_full_commit_id() {
  value="$1"
  case "$value" in
    ""|*[!0123456789abcdefABCDEF]*) return 1 ;;
  esac
  [ "${#value}" -eq 40 ] || [ "${#value}" -eq 64 ]
}

canonical_source_url() {
  case "$1" in
    https://github.com/Neo-Isshin/actanara|https://github.com/Neo-Isshin/actanara/|https://github.com/Neo-Isshin/actanara.git|https://github.com/Neo-Isshin/actanara.git/)
      printf '%s' "$DEFAULT_SOURCE_URL"
      ;;
    *)
      printf '%s' "$1"
      ;;
  esac
}

git_exec() (
  unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE
  unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  unset GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
  unset GIT_CEILING_DIRECTORIES GIT_DISCOVERY_ACROSS_FILESYSTEM GIT_PREFIX
  unset GIT_ALLOW_PROTOCOL GIT_PROTOCOL_FROM_USER GIT_EXEC_PATH GIT_TEMPLATE_DIR
  unset GIT_SSH GIT_SSH_COMMAND
  if [ "$OFFLINE" = "1" ]; then
    GIT_TERMINAL_PROMPT=0 \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_NO_REPLACE_OBJECTS=1 \
    GIT_NO_LAZY_FETCH=1 \
    GIT_ALLOW_PROTOCOL= \
      "$GIT_BIN" \
        -c protocol.allow=never \
        -c protocol.ext.allow=never \
        -c core.hooksPath=/dev/null \
        -c core.fsmonitor=false \
        "$@"
    return $?
  fi
  GIT_TERMINAL_PROMPT=0 \
  GIT_CONFIG_NOSYSTEM=1 \
  GIT_CONFIG_GLOBAL=/dev/null \
  GIT_NO_REPLACE_OBJECTS=1 \
    "$GIT_BIN" \
      -c protocol.ext.allow=never \
      -c core.hooksPath=/dev/null \
      -c core.fsmonitor=false \
      "$@"
)

select_platform_adapter() {
  detected_platform="$(uname -s 2>/dev/null || printf unknown)"
  if [ "${ACTANARA_INSTALL_TEST_MODE:-0}" = "1" ] && [ -n "${ACTANARA_SETUP_PLATFORM:-}" ]; then
    detected_platform="$ACTANARA_SETUP_PLATFORM"
  fi
  case "$detected_platform" in
    Darwin)
      ADAPTER_PATH="install/bootstrap.sh"
      ADAPTER_SHELL="${ACTANARA_INSTALL_ZSH:-}"
      if [ -z "$ADAPTER_SHELL" ]; then
        ADAPTER_SHELL="$(command -v zsh 2>/dev/null || true)"
      fi
      if [ -z "$ADAPTER_SHELL" ]; then
        setup_error "zsh is required by the macOS setup adapter"
        exit 2
      fi
      ;;
    Linux)
      ADAPTER_PATH="install/bootstrap-linux.sh"
      ADAPTER_SHELL="${ACTANARA_INSTALL_SH:-/bin/sh}"
      ;;
    *)
      setup_error "unsupported platform: $detected_platform"
      exit 2
      ;;
  esac
}

resolve_local_adapter_root() {
  [ -n "$SOURCE_ROOT" ] || return 1
  printf '%s' "$SOURCE_ROOT"
}

create_private_temp_root() {
  if [ -n "$TEMP_ROOT" ]; then
    return 0
  fi
  TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/actanara-setup.XXXXXXXX")" || {
    setup_error "could not create a private setup directory"
    exit 2
  }
}

cached_source_contains_selected_adapter() (
  candidate_source="$1"
  if [ ! -d "$candidate_source" ] || [ -L "$candidate_source" ] ||
    [ ! -d "$candidate_source/.git" ] || [ -L "$candidate_source/.git" ]; then
    return 1
  fi
  candidate_origin="$(
    git_exec \
      --git-dir="$candidate_source/.git" \
      --work-tree="$candidate_source" \
      remote get-url origin 2>/dev/null ||
      true
  )"
  if [ "$(canonical_source_url "$candidate_origin")" != "$canonical_url" ]; then
    return 1
  fi
  candidate_ref="$(
    git_exec \
      --git-dir="$candidate_source/.git" \
      --work-tree="$candidate_source" \
      rev-parse --verify "$SOURCE_REF^{commit}" 2>/dev/null ||
      true
  )"
  candidate_ref="$(printf '%s' "$candidate_ref" | tr '[:upper:]' '[:lower:]')"
  [ "$candidate_ref" = "$SOURCE_REF" ] || return 1
  git_exec \
    --git-dir="$candidate_source/.git" \
    --work-tree="$candidate_source" \
    cat-file -e "$SOURCE_REF:$ADAPTER_PATH" 2>/dev/null
)

extract_cached_exact_adapter() {
  cached_source=""
  primary_cached_source="$CACHE_ROOT/source"
  isolated_cached_source="$CACHE_ROOT/official-release/source"
  if cached_source_contains_selected_adapter "$primary_cached_source"; then
    cached_source="$primary_cached_source"
  elif [ "$canonical_url" = "$DEFAULT_SOURCE_URL" ] &&
    cached_source_contains_selected_adapter "$isolated_cached_source"; then
    cached_source="$isolated_cached_source"
  fi
  if [ -z "$cached_source" ]; then
    setup_error "offline setup requires the selected commit in a matching installer cache"
    exit 2
  fi
  create_private_temp_root
  adapter_file="$TEMP_ROOT/adapter"
  if ! git_exec \
    --git-dir="$cached_source/.git" \
    --work-tree="$cached_source" \
    cat-file blob "$SOURCE_REF:$ADAPTER_PATH" > "$adapter_file"; then
    setup_error "the cached commit does not contain $ADAPTER_PATH"
    exit 2
  fi
  chmod 700 "$adapter_file"
  DOWNLOADED_ADAPTER="$adapter_file"
}

download_exact_adapter() {
  if ! command -v "$GIT_BIN" >/dev/null 2>&1 && [ ! -x "$GIT_BIN" ]; then
    setup_error "Git is required to resolve the platform adapter"
    exit 2
  fi
  canonical_url="$(canonical_source_url "$SOURCE_URL")"
  if [ -z "$SOURCE_REF" ]; then
    if [ "$canonical_url" != "$DEFAULT_SOURCE_URL" ]; then
      setup_error "a custom source URL requires an exact 40- or 64-character --ref"
      exit 2
    fi
    setup_error "network setup requires a Release-pinned install.sh or an exact --ref"
    exit 2
  fi
  if ! is_full_commit_id "$SOURCE_REF"; then
    setup_error "the selected source did not resolve to an exact commit"
    exit 2
  fi
  SOURCE_REF="$(printf '%s' "$SOURCE_REF" | tr '[:upper:]' '[:lower:]')"

  if [ "$OFFLINE" = "1" ]; then
    extract_cached_exact_adapter
    return 0
  fi

  create_private_temp_root
  git_exec init --quiet "$TEMP_ROOT/source"
  git_exec -C "$TEMP_ROOT/source" remote add origin "$SOURCE_URL"
  git_exec -C "$TEMP_ROOT/source" fetch --quiet --depth=1 --filter=blob:none origin "$SOURCE_REF"
  resolved_ref="$(git_exec -C "$TEMP_ROOT/source" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)"
  resolved_ref="$(printf '%s' "$resolved_ref" | tr '[:upper:]' '[:lower:]')"
  if [ "$resolved_ref" != "$SOURCE_REF" ]; then
    setup_error "the downloaded platform adapter does not match the selected commit"
    exit 2
  fi
  adapter_file="$TEMP_ROOT/adapter"
  if ! git_exec -C "$TEMP_ROOT/source" show "$SOURCE_REF:$ADAPTER_PATH" > "$adapter_file"; then
    setup_error "the selected commit does not contain $ADAPTER_PATH"
    exit 2
  fi
  chmod 700 "$adapter_file"
  DOWNLOADED_ADAPTER="$adapter_file"
}

run_platform_adapter() {
  local_root="$(resolve_local_adapter_root || true)"
  if [ -n "$local_root" ]; then
    adapter_file="$local_root/$ADAPTER_PATH"
    if [ ! -f "$adapter_file" ]; then
      setup_error "platform adapter not found: $adapter_file"
      exit 2
    fi
    if [ "$ADAPTER_PATH" = "install/bootstrap-linux.sh" ]; then
      ACTANARA_INSTALL_SOURCE_ROOT= \
      ACTANARA_INSTALL_SOURCE_URL= \
      ACTANARA_INSTALL_REF= \
      ACTANARA_INSTALL_PUBLIC_ENTRY=1 \
        "$ADAPTER_SHELL" "$adapter_file" "$@"
    else
      ACTANARA_INSTALL_SOURCE_ROOT= \
      ACTANARA_INSTALL_SOURCE_URL= \
      ACTANARA_INSTALL_REF= \
        "$ADAPTER_SHELL" "$adapter_file" "$@"
    fi
    return $?
  fi

  download_exact_adapter
  adapter_file="$DOWNLOADED_ADAPTER"
  if [ "$ADAPTER_PATH" = "install/bootstrap-linux.sh" ]; then
    ACTANARA_INSTALL_SOURCE_ROOT= \
    ACTANARA_INSTALL_SOURCE_URL= \
    ACTANARA_INSTALL_REF= \
    ACTANARA_INSTALL_PUBLIC_ENTRY=1 \
      "$ADAPTER_SHELL" "$adapter_file" --source-url "$SOURCE_URL" \
      --ref "$SOURCE_REF" "$@"
  else
    ACTANARA_INSTALL_SOURCE_ROOT= \
    ACTANARA_INSTALL_SOURCE_URL= \
    ACTANARA_INSTALL_REF= \
      "$ADAPTER_SHELL" "$adapter_file" --source-url "$SOURCE_URL" \
      --ref "$SOURCE_REF" "$@"
  fi
}

parse_setup_options "$@"
select_platform_adapter
run_platform_adapter "$@"
fi
