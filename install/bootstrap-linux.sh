#!/bin/sh
# Linux bootstrap adapter. Keep it as one compound command so a truncated
# streamed adapter cannot execute a valid prefix.
if true; then
set -eu
umask 077

DEFAULT_SOURCE_URL="https://github.com/Neo-Isshin/actanara.git"
SOURCE_ROOT="${ACTANARA_INSTALL_SOURCE_ROOT:-}"
SOURCE_URL="${ACTANARA_INSTALL_SOURCE_URL:-}"
SOURCE_REF="${ACTANARA_INSTALL_REF:-}"
CACHE_ROOT="${ACTANARA_INSTALL_CACHE_ROOT:-$HOME/.cache/actanara/installer}"
GIT_BIN="${ACTANARA_INSTALL_GIT:-git}"
PYTHON_BIN="${ACTANARA_INSTALL_PYTHON:-python3}"
DRY_RUN="${ACTANARA_INSTALL_DRY_RUN:-0}"
OFFLINE="${ACTANARA_INSTALL_OFFLINE:-0}"
PUBLIC_ENTRY="${ACTANARA_INSTALL_PUBLIC_ENTRY:-0}"
REMOTE_SOURCE_SELECTED=0
CACHE_SOURCE=0

if [ -n "$SOURCE_URL" ] || [ -n "$SOURCE_REF" ]; then
  REMOTE_SOURCE_SELECTED=1
fi

bootstrap_usage() {
  cat <<'EOF'
Actanara Linux setup adapter

Usage:
  install/bootstrap-linux.sh [bootstrap-options] [-- installer-options]

Bootstrap options:
  --source-root PATH   Use an existing local source tree.
  --source-url URL     Git source URL.
  --ref COMMIT         Exact 40- or 64-character commit.
  --cache-root PATH    Installer source/dependency cache root.
  --git PATH           Git executable.
  --python PATH        CPython 3.13 executable.
  --offline            Never access the network.
  --dry-run            Validate and print the Linux installation plan.
EOF
}

bootstrap_error() {
  printf 'Actanara Linux setup: %s\n' "$*" >&2
}

require_value() {
  option="$1"
  value="${2:-}"
  case "$value" in
    ""|--*) bootstrap_error "$option requires a value"; exit 2 ;;
  esac
}

is_full_commit_id() {
  value="$1"
  case "$value" in
    ""|*[!0123456789abcdefABCDEF]*) return 1 ;;
  esac
  [ "${#value}" -eq 40 ] || [ "${#value}" -eq 64 ]
}

git_exec() {
  if [ "$OFFLINE" = "1" ]; then
    GIT_NO_LAZY_FETCH=1 \
    GIT_ALLOW_PROTOCOL= \
    GIT_TERMINAL_PROMPT=0 \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
      "$GIT_BIN" -c protocol.allow=never -c core.hooksPath=/dev/null "$@"
    return $?
  fi
  GIT_TERMINAL_PROMPT=0 \
  GIT_CONFIG_NOSYSTEM=1 \
  GIT_CONFIG_GLOBAL=/dev/null \
    "$GIT_BIN" -c core.hooksPath=/dev/null "$@"
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

source_urls_match() {
  existing="$1"
  requested="$2"
  [ "$(canonical_source_url "$existing")" = "$(canonical_source_url "$requested")" ]
}

validate_cached_origin() {
  source="$1"
  requested="$2"
  existing=$(git_exec -C "$source" remote get-url origin 2>/dev/null || true)
  if [ -z "$existing" ] || ! source_urls_match "$existing" "$requested"; then
    bootstrap_error "installer cache origin does not match the requested source URL"
    return 2
  fi
}

installer_option_takes_value() {
  case "$1" in
    --source-root|--runtime|--python|--language|--dashboard-host|--dashboard-port|--rag-embedding-mode)
      return 0
      ;;
  esac
  return 1
}

parse_installer_contract_args() {
  INSTALLER_RUNTIME="${ACTANARA_INSTALL_RUNTIME:-$HOME/.actanara}"
  INSTALLER_RUNTIME_EXPLICIT=0
  INSTALLER_UPDATE_MODE=0
  INSTALLER_DRY_RUN="$DRY_RUN"
  if [ -n "${ACTANARA_INSTALL_RUNTIME:-}" ]; then
    INSTALLER_RUNTIME_EXPLICIT=1
  fi
  while [ "$#" -gt 0 ]; do
    argument="$1"
    case "$argument" in
      --source-root|--source-root=*|--python|--python=*)
        bootstrap_error "$argument is a bootstrap option and must appear before --"
        exit 2
        ;;
      --runtime)
        require_value "$argument" "${2:-}"
        INSTALLER_RUNTIME="$2"
        INSTALLER_RUNTIME_EXPLICIT=1
        shift 2
        ;;
      --runtime=*)
        INSTALLER_RUNTIME=${argument#--runtime=}
        require_value --runtime "$INSTALLER_RUNTIME"
        INSTALLER_RUNTIME_EXPLICIT=1
        shift
        ;;
      --upgrade|--source-only|--force-rebuild|--repair-existing)
        INSTALLER_UPDATE_MODE=1
        shift
        ;;
      --dry-run)
        INSTALLER_DRY_RUN=1
        shift
        ;;
      *)
        if installer_option_takes_value "$argument" && [ "$#" -gt 1 ]; then
          shift 2
        else
          shift
        fi
        ;;
    esac
  done
}

extract_plain_json_string() {
  file="$1"
  field="$2"
  [ -f "$file" ] && [ ! -L "$file" ] || return 1
  size=$(wc -c < "$file" 2>/dev/null | tr -d '[:space:]') || return 1
  case "$size" in
    ""|*[!0123456789]*) return 1 ;;
  esac
  [ "$size" -le 65536 ] || return 1
  flattened=$(tr '\r\n' '  ' < "$file" 2>/dev/null) || return 1
  occurrences=$(printf '%s' "$flattened" | grep -o "\"$field\"[[:space:]]*:" 2>/dev/null | wc -l | tr -d '[:space:]') || return 1
  [ "$occurrences" = "1" ] || return 1
  value=$(printf '%s' "$flattened" | sed -n "s/.*\"$field\"[[:space:]]*:[[:space:]]*\"\([^\"\\\\]*\)\".*/\\1/p") || return 1
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}

normalize_runtime_candidate() {
  candidate="$1"
  case "$candidate" in
    "~") candidate="$HOME" ;;
    "~/"*) candidate="$HOME/${candidate#\~/}" ;;
    "~"*) return 1 ;;
  esac
  if [ -d "$candidate" ] && [ ! -L "$candidate" ]; then
    (CDPATH= cd -- "$candidate" 2>/dev/null && pwd -P)
    return $?
  fi
  case "$candidate" in
    /*) printf '%s' "$candidate" ;;
    *) printf '%s/%s' "$(pwd -P)" "$candidate" ;;
  esac
}

select_public_runtime() {
  if [ "$INSTALLER_RUNTIME_EXPLICIT" != "1" ]; then
    if [ -n "${ACTANARA_HOME:-}" ]; then
      INSTALLER_RUNTIME="$ACTANARA_HOME"
    else
      location_file="${ACTANARA_LOCATION_FILE:-$HOME/.config/actanara/location.json}"
      if [ -e "$location_file" ] || [ -L "$location_file" ]; then
        selected=$(extract_plain_json_string "$location_file" actanaraHome || true)
        case "$selected" in
          /*) INSTALLER_RUNTIME="$selected" ;;
          *)
            bootstrap_error "saved Actanara Runtime location is missing or unsafe"
            return 2
            ;;
        esac
      fi
    fi
  fi
  selected=$(normalize_runtime_candidate "$INSTALLER_RUNTIME" || true)
  if [ -z "$selected" ]; then
    bootstrap_error "selected Actanara Runtime path is unsafe"
    return 2
  fi
  INSTALLER_RUNTIME="$selected"
}

runtime_is_managed() {
  runtime="$1"
  [ -d "$runtime" ] && [ ! -L "$runtime" ] || return 1
  [ -d "$runtime/app" ] && [ ! -L "$runtime/app" ] || return 1
  [ -d "$runtime/app/releases" ] && [ ! -L "$runtime/app/releases" ] || return 1
  [ -d "$runtime/config" ] && [ ! -L "$runtime/config" ] || return 1
  [ -f "$runtime/config/settings.json" ] && [ ! -L "$runtime/config/settings.json" ] || return 1
  [ -f "$runtime/config/runtime.json" ] && [ ! -L "$runtime/config/runtime.json" ] || return 1
  [ -x "$runtime/bin/actanara" ] && [ ! -L "$runtime/bin/actanara" ] || return 1
  [ -L "$runtime/app/source" ] || return 1
  source_link=$(readlink "$runtime/app/source" 2>/dev/null || true)
  case "$source_link" in
    releases/*) generation=${source_link#releases/} ;;
    *) return 1 ;;
  esac
  case "$generation" in
    ""|.|..|*/*) return 1 ;;
  esac
  release="$runtime/app/releases/$generation"
  manifest="$release/.actanara-runtime-source.json"
  [ -d "$release" ] && [ ! -L "$release" ] || return 1
  [ -f "$manifest" ] && [ ! -L "$manifest" ] || return 1
  grep -q '"product"[[:space:]]*:[[:space:]]*"actanara"' "$manifest" || return 1
  grep -q '"deploymentMode"[[:space:]]*:[[:space:]]*"release-symlink"' "$manifest" || return 1
}

runtime_repair_configuration_pending_status() {
  runtime="$1"
  marker="$runtime/app/.repair-configuration-pending"
  [ -e "$marker" ] || [ -L "$marker" ] || return 1
  [ -f "$marker" ] && [ ! -L "$marker" ] || return 2
  stat_bin="/usr/bin/stat"
  if [ "${ACTANARA_INSTALL_TEST_MODE:-0}" = "1" ] && [ -n "${ACTANARA_INSTALL_STAT:-}" ]; then
    stat_bin="$ACTANARA_INSTALL_STAT"
  fi
  marker_identity=$("$stat_bin" -c '%h:%a:%u' "$marker" 2>/dev/null) || return 2
  expected_uid=$(/usr/bin/id -u 2>/dev/null) || return 2
  [ "$marker_identity" = "1:600:$expected_uid" ] || return 2
  marker_size=$(wc -c < "$marker" 2>/dev/null | tr -d '[:space:]') || return 2
  case "$marker_size" in
    ""|*[!0123456789]*) return 2 ;;
  esac
  [ "$marker_size" -ge 2 ] && [ "$marker_size" -le 129 ] || return 2
  tx_id=$(sed -n '1p' "$marker" 2>/dev/null) || return 2
  printf '%s' "$tx_id" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' || return 2
  [ "$marker_size" -eq $((${#tx_id} + 1)) ] || return 2
  return 0
}

shell_quote() {
  printf "'"
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
  printf "'"
}

print_guarded_update_command() {
  mode="$1"
  shell_quote "$INSTALLER_RUNTIME/bin/actanara"
  printf ' update --%s --runtime ' "$mode"
  shell_quote "$INSTALLER_RUNTIME"
  if [ "$CACHE_SOURCE" = "1" ]; then
    printf ' --source-url '
    shell_quote "$SOURCE_URL"
    printf ' --ref '
    shell_quote "$SOURCE_REF"
    printf ' --cache-root '
    shell_quote "$CACHE_ROOT"
  else
    printf ' --source-root '
    shell_quote "$SOURCE_ROOT"
  fi
  if [ "$OFFLINE" = "1" ]; then
    printf ' --offline'
  fi
  printf '\n'
}

print_guarded_repair_command() {
  mode="$1"
  printf 'sh '
  shell_quote "$SOURCE_ROOT/install/bootstrap-linux.sh"
  if [ "$CACHE_SOURCE" = "1" ]; then
    printf ' --source-url '
    shell_quote "$SOURCE_URL"
    printf ' --ref '
    shell_quote "$SOURCE_REF"
    printf ' --cache-root '
    shell_quote "$CACHE_ROOT"
  else
    printf ' --source-root '
    shell_quote "$SOURCE_ROOT"
  fi
  printf ' --python '
  shell_quote "$PYTHON_BIN"
  if [ "$OFFLINE" = "1" ]; then
    printf ' --offline'
  fi
  printf ' -- --runtime '
  shell_quote "$INSTALLER_RUNTIME"
  printf ' --repair-existing --yes'
  if [ "$mode" = "dry-run" ]; then
    printf ' --dry-run'
  fi
  printf '\n'
}

tty_is_available() {
  [ -r /dev/tty ] && [ -w /dev/tty ] && ( : </dev/tty ) 2>/dev/null
}

run_linux_installer() {
  ACTANARA_INSTALL_DRY_RUN="$DRY_RUN" \
  ACTANARA_INSTALL_OFFLINE="$OFFLINE" \
    "$PYTHON_BIN" "$SOURCE_ROOT/install/install_linux.py" \
      --source-root "$SOURCE_ROOT" \
      --python "$PYTHON_BIN" \
      "$@"
}

handle_public_pending_repair() {
  if [ "$INSTALLER_DRY_RUN" = "1" ]; then
    printf 'Actanara Linux setup: previewing the pending Runtime repair: %s\n' "$INSTALLER_RUNTIME"
    run_linux_installer --repair-existing --yes --dry-run --runtime "$INSTALLER_RUNTIME" "$@"
    return $?
  fi

  if ! tty_is_available; then
    printf 'Actanara Linux setup: a committed Runtime repair is pending: %s\n' "$INSTALLER_RUNTIME"
    printf '%s\n' 'No Runtime changes were made because no controlling terminal is available.'
    printf '%s\n' 'Review the pinned repair plan with:'
    print_guarded_repair_command dry-run
    printf '%s\n' 'Resume that exact pinned repair with:'
    print_guarded_repair_command apply
    return 2
  fi

  printf 'Actanara Linux setup: a committed Runtime repair is pending: %s\n' "$INSTALLER_RUNTIME"
  printf '%s\n' 'Repair plan:'
  run_linux_installer --repair-existing --yes --dry-run --runtime "$INSTALLER_RUNTIME" "$@" || return $?
  if ! printf '%s' 'Resume this managed Runtime repair with the plan above? [y/N] ' > /dev/tty 2>/dev/null; then
    bootstrap_error "could not open the controlling terminal for repair confirmation"
    return 2
  fi
  answer=""
  if ! IFS= read -r answer < /dev/tty; then
    bootstrap_error "could not read the managed Runtime repair confirmation"
    return 2
  fi
  case "$answer" in
    y|Y|yes|YES|Yes)
      run_linux_installer --repair-existing --yes --runtime "$INSTALLER_RUNTIME" "$@"
      ;;
    *)
      printf '%s\n' 'Actanara Linux setup: repair cancelled; no repair was applied.'
      ;;
  esac
}

handle_public_managed_runtime() {
  if runtime_repair_configuration_pending_status "$INSTALLER_RUNTIME"; then
    handle_public_pending_repair "$@"
    return $?
  else
    pending_status=$?
    if [ "$pending_status" -eq 2 ]; then
      bootstrap_error "existing Runtime repair marker is unsafe: $INSTALLER_RUNTIME"
      return 2
    fi
  fi
  if [ "$INSTALLER_DRY_RUN" = "1" ]; then
    printf 'Actanara Linux setup: previewing the existing managed Runtime upgrade: %s\n' "$INSTALLER_RUNTIME"
    run_linux_installer --upgrade --dry-run --runtime "$INSTALLER_RUNTIME" "$@"
    return 0
  fi

  if ! tty_is_available; then
    printf 'Actanara Linux setup: existing managed Runtime detected: %s\n' "$INSTALLER_RUNTIME"
    printf '%s\n' 'No Runtime changes were made because no controlling terminal is available.'
    printf '%s\n' 'Review the pinned guarded upgrade plan with:'
    print_guarded_update_command dry-run
    printf '%s\n' 'Apply that exact pinned upgrade with:'
    print_guarded_update_command apply
    return 2
  fi

  printf 'Actanara Linux setup: existing managed Runtime detected: %s\n' "$INSTALLER_RUNTIME"
  printf '%s\n' 'Upgrade plan:'
  run_linux_installer --upgrade --dry-run --runtime "$INSTALLER_RUNTIME" "$@" || return $?
  if ! printf '%s' 'Upgrade this managed Runtime with the plan above? [y/N] ' > /dev/tty 2>/dev/null; then
    bootstrap_error "could not open the controlling terminal for upgrade confirmation"
    return 2
  fi
  answer=""
  if ! IFS= read -r answer < /dev/tty; then
    bootstrap_error "could not read the managed Runtime upgrade confirmation"
    return 2
  fi
  case "$answer" in
    y|Y|yes|YES|Yes)
      run_linux_installer --upgrade --yes --runtime "$INSTALLER_RUNTIME" "$@"
      ;;
    *)
      printf '%s\n' 'Actanara Linux setup: upgrade cancelled; no upgrade was applied.'
      ;;
  esac
}

# Bootstrap options are consumed until `--` or the first installer option.
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-root)
      require_value "$1" "${2:-}"; SOURCE_ROOT="$2"; shift 2 ;;
    --source-url)
      require_value "$1" "${2:-}"; SOURCE_URL="$2"; REMOTE_SOURCE_SELECTED=1; shift 2 ;;
    --ref)
      require_value "$1" "${2:-}"; SOURCE_REF="$2"; REMOTE_SOURCE_SELECTED=1; shift 2 ;;
    --cache-root)
      require_value "$1" "${2:-}"; CACHE_ROOT="$2"; shift 2 ;;
    --git)
      require_value "$1" "${2:-}"; GIT_BIN="$2"; shift 2 ;;
    --python)
      require_value "$1" "${2:-}"; PYTHON_BIN="$2"; shift 2 ;;
    --offline)
      OFFLINE=1; shift ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    -h|--help)
      bootstrap_usage; exit 0 ;;
    --)
      shift; break ;;
    *)
      break ;;
  esac
done

parse_installer_contract_args "$@"

if [ "$(uname -s 2>/dev/null || printf unknown)" != "Linux" ] && [ "${ACTANARA_INSTALL_TEST_MODE:-0}" != "1" ]; then
  bootstrap_error "this adapter only supports Linux"
  exit 2
fi

if [ -z "$SOURCE_ROOT" ] && [ "$REMOTE_SOURCE_SELECTED" != "1" ]; then
  case "$0" in
    */*)
      script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || script_dir=""
      if [ -n "$script_dir" ] && [ -f "$script_dir/install_linux.py" ]; then
        SOURCE_ROOT=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
      fi
      ;;
  esac
fi

if [ -n "$SOURCE_ROOT" ] && [ "$REMOTE_SOURCE_SELECTED" = "1" ]; then
  bootstrap_error "--source-root cannot be combined with --source-url or --ref"
  exit 2
fi

if [ -z "$SOURCE_ROOT" ]; then
  [ -n "$SOURCE_URL" ] || SOURCE_URL="$DEFAULT_SOURCE_URL"
  if ! command -v "$GIT_BIN" >/dev/null 2>&1 && [ ! -x "$GIT_BIN" ]; then
    bootstrap_error "Git is required to download Actanara"
    exit 2
  fi
  if [ -z "$SOURCE_REF" ]; then
    if [ "$OFFLINE" = "1" ]; then
      bootstrap_error "offline setup requires an exact cached source commit"
      exit 2
    fi
    if ! source_urls_match "$SOURCE_URL" "$DEFAULT_SOURCE_URL"; then
      bootstrap_error "a custom source URL requires an exact 40- or 64-character commit"
      exit 2
    fi
  elif ! is_full_commit_id "$SOURCE_REF"; then
    bootstrap_error "the selected source ref must be an exact 40- or 64-character commit"
    exit 2
  else
    SOURCE_REF=$(printf '%s' "$SOURCE_REF" | tr '[:upper:]' '[:lower:]')
  fi

  CACHE_SOURCE=1
  SOURCE_ROOT="$CACHE_ROOT/source"
  if [ -e "$SOURCE_ROOT" ] && [ ! -d "$SOURCE_ROOT/.git" ]; then
    bootstrap_error "installer cache exists but is not a Git checkout"
    exit 2
  fi
  if [ -d "$SOURCE_ROOT/.git" ]; then
    validate_cached_origin "$SOURCE_ROOT" "$SOURCE_URL" || exit $?
  elif [ "$OFFLINE" = "1" ]; then
    if [ -z "$SOURCE_REF" ]; then
      bootstrap_error "offline setup requires an exact cached source commit"
    fi
    bootstrap_error "offline installer cache is missing: $SOURCE_ROOT"
    exit 2
  else
    mkdir -p "$CACHE_ROOT"
    if ! git_exec clone --filter=blob:none --sparse --no-checkout "$SOURCE_URL" "$SOURCE_ROOT"; then
      bootstrap_error "could not clone the selected source URL"
      exit 2
    fi
    git_exec -C "$SOURCE_ROOT" sparse-checkout init --no-cone
    git_exec -C "$SOURCE_ROOT" sparse-checkout set /pyproject.toml /MANIFEST.in /LICENSE /config.py /install /advanced /src
    validate_cached_origin "$SOURCE_ROOT" "$SOURCE_URL" || exit $?
  fi

  if [ -z "$SOURCE_REF" ]; then
    if ! git_exec -C "$SOURCE_ROOT" fetch --force origin '+refs/heads/main:refs/remotes/origin/main'; then
      bootstrap_error "could not resolve the selected source main branch"
      exit 2
    fi
    SOURCE_REF=$(git_exec -C "$SOURCE_ROOT" rev-parse --verify 'refs/remotes/origin/main^{commit}' 2>/dev/null || true)
  elif [ "$OFFLINE" != "1" ]; then
    if ! git_exec -C "$SOURCE_ROOT" fetch --force origin "$SOURCE_REF"; then
      bootstrap_error "the selected source ref is unavailable from the requested origin"
      exit 2
    fi
    fetched_ref=$(git_exec -C "$SOURCE_ROOT" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)
    fetched_ref=$(printf '%s' "$fetched_ref" | tr '[:upper:]' '[:lower:]')
    if [ "$fetched_ref" != "$SOURCE_REF" ]; then
      bootstrap_error "the requested origin did not resolve the exact selected commit"
      exit 2
    fi
  fi
  if ! is_full_commit_id "$SOURCE_REF"; then
    bootstrap_error "the selected source did not resolve to an exact commit"
    exit 2
  fi
  SOURCE_REF=$(printf '%s' "$SOURCE_REF" | tr '[:upper:]' '[:lower:]')
  resolved_ref=$(git_exec -C "$SOURCE_ROOT" rev-parse --verify "$SOURCE_REF^{commit}" 2>/dev/null || true)
  resolved_ref=$(printf '%s' "$resolved_ref" | tr '[:upper:]' '[:lower:]')
  if [ "$resolved_ref" != "$SOURCE_REF" ]; then
    bootstrap_error "the cached source does not match the selected commit"
    exit 2
  fi
  git_exec -C "$SOURCE_ROOT" checkout --detach "$SOURCE_REF"
  git_exec -C "$SOURCE_ROOT" reset --hard "$SOURCE_REF"
  # The cache is installer-owned. Remove both ignored and ordinary untracked
  # files so the payload is exactly the requested commit, not a commit plus
  # stale cache content.
  git_exec -C "$SOURCE_ROOT" clean -fdx
  deployed_ref=$(git_exec -C "$SOURCE_ROOT" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)
  deployed_ref=$(printf '%s' "$deployed_ref" | tr '[:upper:]' '[:lower:]')
  if [ "$deployed_ref" != "$SOURCE_REF" ]; then
    bootstrap_error "the installer cache did not deploy the exact selected commit"
    exit 2
  fi
fi

normalized_source_root=$(CDPATH= cd -- "$SOURCE_ROOT" 2>/dev/null && pwd -P) || {
  bootstrap_error "selected source root is unavailable or unsafe: $SOURCE_ROOT"
  exit 2
}
SOURCE_ROOT="$normalized_source_root"
if [ "$CACHE_SOURCE" = "1" ]; then
  normalized_cache_root=$(CDPATH= cd -- "$CACHE_ROOT" 2>/dev/null && pwd -P) || {
    bootstrap_error "installer cache root is unavailable or unsafe: $CACHE_ROOT"
    exit 2
  }
  CACHE_ROOT="$normalized_cache_root"
fi

if [ ! -f "$SOURCE_ROOT/install/install_linux.py" ]; then
  bootstrap_error "Linux installer not found under source root: $SOURCE_ROOT"
  exit 2
fi

if ! "$PYTHON_BIN" -c 'import platform, sys; raise SystemExit(0 if platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 13) else 1)' >/dev/null 2>&1; then
  bootstrap_error "the current Linux Runtime lock requires CPython 3.13"
  exit 2
fi

if [ "$PUBLIC_ENTRY" = "1" ] && [ "$INSTALLER_UPDATE_MODE" != "1" ]; then
  select_public_runtime || exit $?
  fresh_staging="$INSTALLER_RUNTIME/app/install-staging"
  if [ -e "$fresh_staging" ] || [ -L "$fresh_staging" ]; then
    printf 'Actanara Linux setup: recovering an interrupted fresh install: %s\n' "$INSTALLER_RUNTIME"
    run_linux_installer --runtime "$INSTALLER_RUNTIME" "$@"
    exit $?
  fi
  if runtime_is_managed "$INSTALLER_RUNTIME"; then
    handle_public_managed_runtime "$@"
    exit $?
  fi
fi

run_linux_installer "$@"
fi
