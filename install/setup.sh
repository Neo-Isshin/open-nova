#!/bin/sh
# Keep the hosted entrypoint inside one compound command. A truncated stream
# cannot parse the closing `fi`, so no prefix of the setup is executed.
if true; then
set -eu
umask 077

DEFAULT_SOURCE_URL="https://github.com/Neo-Isshin/actanara.git"
SOURCE_ROOT="${ACTANARA_INSTALL_SOURCE_ROOT:-}"
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
  curl -fsSL https://raw.githubusercontent.com/Neo-Isshin/actanara/main/install/setup.sh | sh

The same command selects the existing macOS installer or the Linux installer.
All options are forwarded to the selected platform adapter.
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

git_exec() {
  GIT_TERMINAL_PROMPT=0 \
  GIT_CONFIG_NOSYSTEM=1 \
  GIT_CONFIG_GLOBAL=/dev/null \
    "$GIT_BIN" -c core.hooksPath=/dev/null "$@"
}

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
  if [ -n "$SOURCE_ROOT" ]; then
    printf '%s' "$SOURCE_ROOT"
    return 0
  fi
  if [ "$OFFLINE" = "1" ] && [ -f "$CACHE_ROOT/source/$ADAPTER_PATH" ]; then
    printf '%s' "$CACHE_ROOT/source"
    return 0
  fi
  case "$0" in
    */*)
      script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || return 1
      candidate_root=$(CDPATH= cd -- "$script_dir/.." 2>/dev/null && pwd -P) || return 1
      if [ -f "$candidate_root/$ADAPTER_PATH" ]; then
        printf '%s' "$candidate_root"
        return 0
      fi
      ;;
  esac
  return 1
}

download_exact_adapter() {
  if [ "$OFFLINE" = "1" ]; then
    setup_error "offline setup requires --source-root or a populated installer cache"
    exit 2
  fi
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
    remote_line="$(git_exec ls-remote --exit-code "$SOURCE_URL" refs/heads/main 2>/dev/null || true)"
    SOURCE_REF="${remote_line%%[[:space:]]*}"
  fi
  if ! is_full_commit_id "$SOURCE_REF"; then
    setup_error "the selected source did not resolve to an exact commit"
    exit 2
  fi
  SOURCE_REF="$(printf '%s' "$SOURCE_REF" | tr '[:upper:]' '[:lower:]')"

  TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/actanara-setup.XXXXXXXX")" || {
    setup_error "could not create a private setup directory"
    exit 2
  }
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
      ACTANARA_INSTALL_PUBLIC_ENTRY=1 "$ADAPTER_SHELL" "$adapter_file" "$@"
    else
      "$ADAPTER_SHELL" "$adapter_file" "$@"
    fi
    return $?
  fi

  download_exact_adapter
  adapter_file="$DOWNLOADED_ADAPTER"
  if [ "$ADAPTER_PATH" = "install/bootstrap-linux.sh" ]; then
    ACTANARA_INSTALL_PUBLIC_ENTRY=1 "$ADAPTER_SHELL" "$adapter_file" \
      --source-url "$SOURCE_URL" --ref "$SOURCE_REF" "$@"
  else
    "$ADAPTER_SHELL" "$adapter_file" --source-url "$SOURCE_URL" --ref "$SOURCE_REF" "$@"
  fi
}

parse_setup_options "$@"
select_platform_adapter
run_platform_adapter "$@"
fi
