# Actanara Installation Guide

<p align="center">
  <img src="https://img.shields.io/badge/Language-English%20·%20Current-2563EB?style=for-the-badge" alt="Current language: English">
  <a href="new-user-onboarding-runbook.zh-CN.md"><img src="https://img.shields.io/badge/Language-简体中文-C026D3?style=for-the-badge" alt="切换到简体中文安装指南"></a>
</p>

**English (current)** · [简体中文](new-user-onboarding-runbook.zh-CN.md) · [Back to README](../README.md)

This guide covers a new installation, the first health checks, and supported
updates. GitHub Releases in
[`Neo-Isshin/actanara`](https://github.com/Neo-Isshin/actanara) are the
canonical public install and update authority.

## Requirements

- macOS with `zsh`, or Linux with POSIX `sh` and a user-level systemd manager;
- `git` and `curl` on either platform;
- Python 3.11 or newer on macOS; the audited Linux lock currently targets
  CPython 3.13;
- network access to GitHub and Python package indexes during installation;
- a local user account that can create files under its home directory;
- an LLM provider endpoint and credential if diary generation will call an
  external provider.

The default Runtime is `~/.actanara`. Actanara keeps application releases,
the active virtual environment, Settings, SQLite data, logs, generated assets,
and rollback state below that Runtime. The installer records the active Runtime
in `~/.config/actanara/location.json`.

## One-line install or refresh

Run the public installer:

```bash
curl -fsSL https://raw.githubusercontent.com/Neo-Isshin/actanara/main/install/setup.sh | sh
```

GitHub serves the maintained POSIX entrypoint from `main`. It resolves official
`origin/main` to a full commit, downloads the platform adapter from that exact
commit, then invokes the existing macOS installer or the Linux installer.

On macOS, the hosted entrypoint supports both new and existing Runtimes and
retains the established update/repair transaction. Linux supports fresh install,
source-only refresh, automatic or forced locked-venv upgrade, and explicitly
confirmed repair. Both platforms preserve user Settings and data; Linux also
preserves each managed systemd unit's prior enabled/active state.

For an existing managed Linux Runtime, an interactive run previews the pinned
upgrade and asks before applying it. A non-interactive run exits with status 2,
makes no Runtime changes, and prints exact pinned `actanara update --dry-run` and
`actanara update --apply` commands. Explicit remote source selection uses a
matching installer-cache `origin` and the exact requested commit; the
bootstrap's adjacent checkout is never substituted for it.

## Install from a checkout

To inspect the plan without writes:

```bash
sh install/setup.sh --dry-run
```

To install from the current checkout:

```bash
sh install/setup.sh
```

`pyproject.toml` is the direct dependency/profile contract, while
`install/runtime-dependencies.lock.json` is the exact Runtime resolution
authority for every supported Python ABI, platform, and architecture. Fresh installs
and candidate-venv rebuilds use the same wheel-only, SHA-256-verified lock.
`requirements-release.txt` locks release build tooling only and is not a
Runtime dependency lock. The ordinary installation includes Base-Pipeline,
Dashboard, and Nova-Task. nova-RAG is optional. Developer test dependencies are
opt-in and are never part of the default production profile.

Useful non-interactive controls include:

```bash
sh install/setup.sh -- --enable-rag
sh install/setup.sh -- --no-scheduler
sh install/setup.sh -- --no-dashboard-server
sh install/setup.sh -- --runtime /path/to/runtime
sh install/setup.sh -- --no-shell-path
```

The guided `--no-wizard` and `--shell-path-file /path/to/profile` controls are
macOS-only. Linux does not edit a shell profile; without `--no-shell-path` it
creates only the `~/.local/bin/actanara` link. When managed Linux services are
selected, an interactive install asks whether they should continue after
logout. `--enable-linger` explicitly permits a no-`sudo` `loginctl` request,
`--require-linger` fails before Runtime writes unless it is already enabled,
and `--no-linger-prompt` preserves the current state. `--yes` never grants
linger authorization.

The installer performs a preflight before changing the Runtime. It verifies
paths, Python compatibility, source cleanliness, port policy, collision guards,
and the selected dependency groups. A failed preflight stops the transaction.

## Guided choices

The macOS interactive installer asks only for choices needed by the selected
product profile:

- interface language (`zh-CN` or `en-US`);
- whether to enable nova-RAG;
- local or cloud embedding mode when nova-RAG is enabled;
- LLM provider, endpoint, model, and credential reference;
- managed Dashboard, nova-RAG, and scheduler service choices;
- optional external-tool coverage and Desktop diary shortcut.

Provider credentials are written only to the Runtime-local private secret store
with restrictive permissions. Settings contain references and provider
metadata, not raw secret values. Never place a real credential in the repository
or in a command that will be retained in shell history.

## CLI and shell path

The Runtime command shim is:

```text
~/.actanara/bin/actanara
```

The installer also attempts to link it at `~/.local/bin/actanara` and adds a
managed PATH block to the selected shell profile. Use `--no-shell-path` to skip
that profile edit, or `--shell-path-file /path/to/profile` to choose a file.

Start a new shell, then verify:

```bash
actanara doctor
actanara onboard status
actanara model show
actanara config show
```

The macOS installer also runs its post-install doctor before declaring success. Any
blocking result keeps the previous active source and venv available for
recovery.

The Linux installer explicitly initializes Settings and all SQLite migrations,
then registers requested Dashboard and scheduler units with `systemctl --user`.
It changes linger only after explicit authorization and never calls `sudo`. If
the host requires administrator authorization, run
`sudo loginctl enable-linger "$USER"` separately and retry. Actanara never
disables linger during uninstall because other user services may depend on it.

## Dashboard and nova-RAG

The Dashboard normally listens on loopback. Port `3036` is preferred, with
safe fallback ports when it is occupied. The installer prints the selected URL
in its completion summary.

When nova-RAG local mode is enabled, its service normally uses loopback port
`3037`. Linux uses an audited CPU-only PyTorch wheel for local embeddings;
cloud/server RAG remains available. External Agent integrations must use the read-only API described in
[rag-external-agent-contract.md](rag-external-agent-contract.md).

Actanara does not expose these services to a public network by default. Keep
the loopback binding unless you have separately configured authenticated,
private network access.

## Stable updates

Preview an update:

```bash
actanara update --dry-run
```

Apply the latest stable Release:

```bash
actanara update --apply
```

Actanara updates only to a stable GitHub Release whose tag resolves to a full
commit; there is no silent fallback to another host or to `main`. When
dependencies are unchanged it reuses the existing venv; otherwise it rebuilds
from the hash-verified lock and validates before switching. Settings, SQLite,
logs, generated assets, service configuration, and rollback metadata are
preserved, and a failed activation restores the previous source, venv, and
service state.

To upgrade from a local checkout instead:

```bash
zsh install/install.sh --upgrade --runtime /path/to/runtime --source-root "$PWD"
```

For offline, pinned-commit, or forced-rebuild updates, see the
[Local Operations Runbook](local-operations-runbook.md#13-updates).

Offline updates must select an immutable source explicitly:

```bash
actanara update --apply --offline --ref <full-commit-sha>
actanara update --apply --offline --source-root /path/to/source
```

The update workflow above selects launchd on macOS and systemd user services on
Linux. A standard Linux update requires aligned Actanara-managed definitions and
fails before service stop if the inventory has drifted. For a trusted but
damaged Linux Runtime, run the explicitly confirmed repair from a selected
source checkout:

```bash
sh install/setup.sh --source-root "$PWD" -- \
  --runtime /path/to/runtime --repair-existing --yes --no-linger-prompt
```

Repair never calls `sudo`, never changes linger, and refuses non-Actanara unit
definitions.

## Backup and recovery

Before a material update, back up important generated diaries, reports, Runtime
Settings, and SQLite data. Do not copy a live SQLite database without accounting
for its WAL/SHM state.

Useful read-only checks are:

```bash
actanara doctor
actanara doctor --scheduler
actanara update --dry-run
```

Do not move or reuse a published version tag. If a published artifact or
checksum is withdrawn, stop distribution and install a newer version after it is
released.

## Further operations

- [Complete local operations runbook](local-operations-runbook.md)
- [CLI product boundary](cli-boundary.md)
- [nova-RAG external Agent contract](rag-external-agent-contract.md)
- [Security policy](../SECURITY.md)
