# Actanara Local Operations Runbook

<p align="center">
  <img src="https://img.shields.io/badge/Language-English%20·%20Current-2563EB?style=for-the-badge" alt="Current language: English">
  <a href="local-operations-runbook.zh-CN.md"><img src="https://img.shields.io/badge/Language-简体中文-C026D3?style=for-the-badge" alt="切换到简体中文 Runbook"></a>
</p>

**English** · [简体中文](local-operations-runbook.zh-CN.md) · [Back to the English README](../README.md)

Status: Public operator guide<br>
Scope: Actanara · Local macOS and Linux runtime

## 1. Purpose

The README explains the product and provides a quick start. This Runbook covers the complete operating path from pre-install checks through first-run setup, historical backfill, daily Pipeline use, Dashboard, Nova-Task, `nova-RAG`, updates, backups, and troubleshooting.

In this guide, an **agent runtime** means an AI tool environment with its own sessions, logs, memory, and execution context, such as Codex, Claude Code, Gemini CLI, OpenClaw, Hermes, OpenCode, Antigravity, or Cursor.

## 2. Version and Release Boundaries

- GitHub is the only public release and installation source. Private development archives are not part of the public installation path.
- Published tags, Releases, and artifacts remain immutable.
- The public one-liner downloads the POSIX setup entrypoint from the latest stable immutable Release. The asset is pinned to that Release's exact full source commit and selects the platform adapter from the same commit.
- `v1.0.0` has been withdrawn and remains available for audit only. It should not be installed or recommended.

> [!IMPORTANT]
> The selected source commit carries the exact Runtime dependency lock for supported Python ABIs, platforms, and architectures. Each installation transaction is pinned to that resolved commit.

## 3. Pre-install Checks

Confirm the following:

- 🍎 On macOS, `zsh`, `git`, and `curl` are available on `PATH`;
- 🐧 On Linux, POSIX `sh`, `git`, `curl`, CPython 3.13, and a working `systemctl --user` manager are available;
- 🐍 On supported Apple Silicon and Intel Macs, Python `>=3.11` is required and the installer can download and verify a managed Python when no compatible version is present;
- 🌐 GitHub, the Python package index, and the selected LLM / embedding provider are reachable;
- 💾 Enough local storage is available for the runtime, Dashboard, and optional local-embedding dependencies;
- 🔐 Provider API keys are ready, but have not been written to shell history, a README, or an ordinary configuration file.

Check the base tools:

```bash
command -v zsh
command -v sh
command -v git
command -v curl
python3 --version 2>/dev/null || true
systemctl --user is-system-running 2>/dev/null || true
```

macOS provides the existing guided setup. Linux is non-interactive and supports
fresh install, guarded updates/repairs, and cloud or CPU-only local-embedding
RAG. The Linux installer supports separate x86_64 and arm64 lock targets without
maintaining separate application implementations; the guarded update/repair
release gate is verified on Debian x86_64 with CPython 3.13.

## 4. Install or Refresh from the Latest Stable Release

Use the public one-liner:

```bash
curl -fsSL https://github.com/Neo-Isshin/actanara/releases/latest/download/install.sh | sh
```

The command keeps source selection exact while hiding source plumbing from the user:

1. GitHub serves the POSIX setup entrypoint from the latest stable immutable Release;
2. The Release builder pins that entrypoint to the Release's full source commit;
3. It fetches the macOS or Linux adapter from that exact commit;
4. The adapter installs the same detached commit.

> [!NOTE]
> Published installation follows immutable Release tags, not a moving development branch. Explicit `--source-root` or full `--ref` selection remains available for advanced and offline workflows.

> [!WARNING]
> On both platforms the same command supports new and existing Runtimes and
> preserves user Settings, databases, secrets, logs, and generated assets.
> Linux fails closed before service stop when managed systemd definitions or
> dependency-profile evidence do not match the selected Runtime.

The macOS installation wizard covers, in order:

1. Interface language and Pipeline language;
2. External agent-runtime paths;
3. LLM Provider, Endpoint, Model, and API Key;
4. Whether to enable `nova-RAG`;
5. Local or cloud embedding configuration;
6. macOS Dashboard and scheduler services.

Linux fresh installs use defaults unless options are passed after `--`.
Dashboard and scheduler services are installed as user-level systemd units.
When this public entrypoint selects an existing managed Runtime, it first shows
the exact pinned upgrade plan and asks for confirmation through the controlling
terminal. If no controlling terminal is available, it exits with status 2,
does not modify the Runtime, and instead prints pinned
`actanara update --dry-run` and `actanara update --apply` commands for the
selected source URL, commit, Runtime, and cache. With a controlling terminal,
a fresh installer also asks before
requesting linger for the current user. It never invokes `sudo`; non-interactive
fresh installs preserve the current linger state unless `--enable-linger` or
`--require-linger` is explicit.

The public installer locale uses `zh-CN` or `en-US`; the runtime's internal Pipeline profile uses `zh` or `en`. Users normally select a language in the installation wizard and should not manually mix values from these two groups.

## 5. Installer Write Locations

| Path | Purpose |
| :--- | :--- |
| `~/.cache/actanara/installer` | Installation source cache |
| `~/.actanara` | Runtime, virtual environment, settings, database, logs, secrets, and generated assets |
| `~/.config/actanara/location.json` | Active runtime pointer |
| `~/.local/bin/actanara` | User-facing CLI entry on `PATH` |
| `~/.zprofile` | macOS only: marked `PATH` block; disable with `--no-shell-path` |
| `~/Desktop/Actanara` | macOS only: desktop shortcut to the diary directory, created by default |
| `~/Library/LaunchAgents/` | macOS only: Dashboard, Scheduler, and optional RAG services |
| `~/.config/systemd/user/` | Linux only: Dashboard, Scheduler, and optional RAG user units |

Provider keys are stored in `$ACTANARA_HOME/state/secrets`. The secrets directory uses mode `0700`, and each secret file uses mode `0600`.

Legacy `macos-keychain` references are used only for compatibility migration: readable secrets are copied to the runtime secret store, but Actanara does not automatically delete old Keychain items. If a legacy secret cannot be read, enter the provider key again in the Dashboard.

## 6. Installation Summary and Basic Verification

Keep the terminal summary after installation. It contains the actual Dashboard URL, runtime location, and common commands.

Default Dashboard address:

```text
http://127.0.0.1:3036/dashboard
```

If `3036` is occupied, the installer selects another port. Always use the installation summary and current runtime settings as the authority.

Run these read-only commands first:

```bash
actanara doctor
actanara model show
actanara onboard status
actanara config show
```

Inspect individual subsystems:

```bash
actanara doctor --installer
actanara doctor --pipeline
actanara doctor --scheduler
actanara doctor --rag
```

A warning does not always block operation. Resolve errors and explicit readiness failures first. Focus on the runtime pointer, Provider, Dashboard, scheduler, and optional RAG Server.

## 7. Configure the LLM Provider

1. Open the Dashboard URL shown in the installation summary;
2. Open LLM Provider settings;
3. Select the Provider and verify the Endpoint and Model;
4. Enter the API Key;
5. Run the availability test;
6. Save only after the test passes.

Then run:

```bash
actanara model show
actanara doctor --pipeline
```

Never put a real key in a README, Issue, log, shell history, Git-tracked file, or ordinary settings field. When an external LLM or cloud embedding provider is configured, relevant derived work content is sent according to the selected endpoint and provider data policy.

## 8. First Historical-Data Generation

### 8.1 Preview the Plan

Select **Generate Historical Data** in the Dashboard, choose a date range, and preview the plan first. Review:

- Pending daily records;
- Weekly and monthly reports;
- Existing artifacts that will be skipped or regenerated;
- Estimated LLM calls;
- Nova-Task tasks;
- `nova-RAG` synchronization tasks, when enabled.

Clear tasks you do not want to run. Plan preview does not write to the runtime.

### 8.2 Queue and Monitor

After confirmation, add the selected tasks to the background queue. Large date ranges take longer; dates without activity may produce only structured placeholder artifacts and may not call an LLM.

Use Background Tasks and Messages to inspect status. A running job can receive a cancellation request, and some partially failed backfill jobs can retry only failed items. Keep at most one active historical-backfill job per runtime.

After completion, inspect:

- Diaries and period reports;
- AI Assets;
- Nova-Task board;
- `nova-RAG` status and search results, when enabled.

## 9. Daily Base Pipeline Operation

Run manually:

```bash
actanara pipeline
actanara pipeline YYYY-MM-DD
```

Without a date, `actanara pipeline` processes the **previous calendar day in the configured time zone**; it is not a shortcut for processing today. Use `YYYY-MM-DD` when specifying a date.

The Pipeline writes diaries, reports, and Foundation data. If the target date has already been generated completely, only an explicit `--force` regenerates it from the frozen Foundation input.

Actanara attributes workspaces using execution evidence from external runtimes. It does not treat the CLI's current directory as the sole attribution source.

Check managed scheduling:

```bash
actanara doctor --scheduler
```

If an external automation system will invoke the Pipeline, prevent duplicate execution with the installer-managed schedule.

## 10. Daily Dashboard Operations

Use the Dashboard to:

- Review daily, weekly, and monthly diaries;
- Inspect live usage, AI Assets, and workspace attribution;
- Configure Providers, external-tool paths, and scheduling;
- Plan historical backfills;
- Monitor background tasks and messages;
- Review Nova-Task;
- Operate optional `nova-RAG`.

If the service becomes unavailable:

```bash
actanara dashboard restart
actanara doctor --installer
```

Do not assume the Dashboard always uses port `3036`. Check the installation summary or `actanara config show` first.

## 11. Nova-Task Operations

Open the task board in the Dashboard. The CLI `task` command only reads and prints task statistics; it does not open the interface:

```bash
actanara task
actanara task --json
```

Nova-Task is a Beta subsystem. It derives a task structure from real runtime activity, conversations, file changes, tool results, and execution evidence.

Operating principles:

- Retain human review for top-level or high-impact tasks;
- Allow the system to maintain routine subtasks, but check status and attribution periodically;
- Before importing an RFC, PRD, Roadmap, or Audit, confirm that its contents are appropriate for the local task graph;
- Treat the task graph as a description of real work, not a traditional hand-written to-do list.

## 12. nova-RAG Operations

Check status:

```bash
actanara doctor --rag
```

Search local memory:

```bash
actanara search "deployment issue" --top-k 5
actanara search "deployment issue" --top-k 5 --json
```

Automation consuming JSON output should check the `available` field. When RAG is unavailable, the command may still return a successful structured status response.

Preview maintenance operations first:

```bash
actanara rag-update --dry-run
actanara rag-rebuild --dry-run
```

External agent runtimes should prefer the Dashboard's read-only facade:

```text
GET  /api/rag/external/health
GET  /api/rag/external/stats
GET  /api/rag/external/contract
POST /api/rag/external/search
```

The default Dashboard base URL is `http://127.0.0.1:3036`; the direct RAG Server defaults to `http://127.0.0.1:3037`. The runtime settings determine the actual ports.

The external-runtime contract allows only health checks, statistics, contract reads, and search. It does not permit memory writes, index changes, global-setting changes, or service-lifecycle control.

## 13. Updates

> [!IMPORTANT]
> macOS retains its existing launchd transaction. Linux uses the same update
> command through its POSIX adapter, preserves exact systemd enabled/active
> state, and refuses definition drift before service stop. Use confirmed repair
> rather than a standard update when a trusted Linux Runtime is damaged.

View the update plan:

```bash
actanara update
```

Run a no-change preview:

```bash
actanara update --dry-run
```

Apply a protected update:

```bash
actanara update --apply
```

- With no arguments, the command displays only the plan;
- `--dry-run` runs a bootstrap and installer preview and reports whether the active venv can be reused or a locked candidate rebuild is required; a cold remote source cache can still limit the preview to source acquisition;
- Only `--apply` performs the real update transaction.

The installer and updater use the same dependency contract and exact Runtime lock
from the selected Release or source commit. The default apply reuses the active venv only
when dependencies, Python ABI, enabled profiles, and the live venv contents **all
match**—it switches the source pointer without running pip; otherwise it builds a
separate candidate venv from the hash-verified lock and atomically switches
pointers only after validation, never installing into the active venv. Missing or
ambiguous evidence fails closed rather than guessing a dependency selection.

```bash
actanara update --apply --offline --ref <full-commit-sha>        # cached remote commit
actanara update --apply --offline --source-root /path/to/source  # local checkout
actanara update --apply --source-only                            # require venv reuse or fail closed
actanara update --apply --force-rebuild                          # require a new locked candidate venv
```

Repair a trusted Linux Runtime from a selected checkout:

```bash
sh install/setup.sh --source-root /path/to/source -- \
  --runtime /path/to/runtime --repair-existing --yes --no-linger-prompt
```

Linux repair reconciles only Actanara-managed unit files. It never invokes
`sudo`, changes linger, or removes an operator-owned unit.

Offline mode accepts only a full commit already present in the installer cache or
a local `--source-root`; it never resolves `latest`.

Select an immutable full commit:

```bash
actanara update --dry-run --ref <full-commit-sha>
actanara update --apply --ref <full-commit-sha>
```

On Linux, explicit `--source-url`/`--ref` selection never adopts the checkout
beside the bootstrap as source. The bootstrap is only the execution entry; the
installer cache must have a normalized `origin` matching the request, and both
online fetches and offline cache reuse must resolve and deploy the exact commit.

Before updating:

1. Confirm that the Pipeline and background tasks have finished;
2. Save the output of `actanara doctor`;
3. Back up runtime settings and important generated assets;
4. Record the current runtime and commit;
5. Run the plan or Dry Run first.

## 14. Logs and Troubleshooting

Check these locations first:

```text
~/.actanara/state/logs/
~/.actanara/config/settings.json
~/.config/actanara/location.json
```

Troubleshoot in this order:

1. Is the runtime pointer correct?
2. Does the Dashboard URL and port match the installation summary?
3. Does the LLM Provider test pass?
4. Does `$ACTANARA_HOME/state/secrets` use mode `0700`, with secret files using `0600`?
5. Do the Pipeline and the platform service manager (LaunchAgents on macOS or
   systemd user units on Linux) use the same `ACTANARA_HOME`?
6. Do external-tool paths exist, and are they enabled?
7. Is the Scheduler duplicated, missing, or failing?
8. Are the RAG Server and active index ready?
9. Are background tasks failed, canceled, or retryable?

When filing an Issue, include commands, necessary output, log paths, the runtime pointer, and relevant Doctor results. Remove secrets, email addresses, usernames, private project names, machine paths, and work content.

## 15. Data, Backups, and Privacy

- Do not commit runtime databases, logs, caches, secrets, generated diaries, or indexes;
- Before publishing screenshots, check for email addresses, usernames, project names, machine paths, token / RAG metrics, and work content;
- Prefer a disposable, isolated runtime with fully synthetic data when publishing screenshots;
- External LLM / embedding requests are processed by the user's configured provider;
- Back up important diaries, reports, Nova-Task data, and runtime settings regularly;
- Before deleting or migrating a runtime, stop managed services and confirm that the backup can be restored.

## 16. Uninstallation Boundary

Actanara does not currently include a product-level one-command uninstaller. Do not remove only `~/.actanara`, because this can leave behind:

- macOS LaunchAgents or Linux systemd user units;
- The `~/.local/bin/actanara` CLI shim;
- The runtime location pointer;
- The marked `PATH` block in `~/.zprofile`;
- The desktop shortcut;
- The installation source cache.

Until a verified uninstall workflow is published, keep the runtime or make a complete backup first, then review every write location in the installation summary individually.

## 17. Completion Checklist

- [ ] The Dashboard is reachable, and its URL matches the installation summary;
- [ ] The LLM Provider test passes;
- [ ] `actanara doctor` reports no blocking errors;
- [ ] Scheduler status matches expectations;
- [ ] The first historical backfill is complete or observable;
- [ ] Diaries, AI Assets, and Nova-Task contain expected data;
- [ ] When `nova-RAG` is enabled, the Server, active index, and search work correctly;
- [ ] Log, update, backup, and uninstallation boundaries are understood.

## 18. Related References

- [English README](../README.md)
- [Chinese README](../README.zh-CN.md)
- [Chinese Local Operations Runbook](local-operations-runbook.zh-CN.md)
- [New User Onboarding Runbook](new-user-onboarding-runbook.md)
- [CLI Product Boundary](cli-boundary.md)
- [nova-RAG External Agent Runtime Contract](rag-external-agent-contract.md)
- [GitHub Releases](https://github.com/Neo-Isshin/actanara/releases)
