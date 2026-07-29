<h1 align="center">
  <img src="docs/assets/banner.png" alt="Actanara" width="650">
</h1>

<p align="center">
  <strong>Your agents do valuable work. Actanara makes sure it does not disappear with the session.</strong>
  <br>
  Turn sessions, tasks, and evidence from Codex, Claude Code, Gemini CLI, OpenClaw, Hermes, OpenCode, Antigravity, and Cursor into local assets you can find, reuse, and revisit.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Language-English%20·%20Current-2563EB?style=for-the-badge" alt="Current language: English">
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Language-简体中文-C026D3?style=for-the-badge" alt="切换到简体中文 README"></a>
</p>

<p align="center">
  <a href="https://neo-isshin.github.io/actanara/"><img src="https://img.shields.io/badge/Website-GitHub%20Pages-2563EB" alt="Website"></a>
  <a href="https://github.com/Neo-Isshin/actanara/releases/latest"><img src="https://img.shields.io/github/v/release/Neo-Isshin/actanara?display_name=tag&amp;sort=semver" alt="Latest stable Release"></a>
  <a href="https://neo-isshin.github.io/actanara/dashboard-demo/"><img src="https://img.shields.io/badge/Demo-interactive-7C3AED" alt="Interactive Dashboard Demo"></a>
  <a href="#linux-support"><img src="https://img.shields.io/badge/Linux-Debian%20x64%20verified-FCC624?logo=linux&amp;logoColor=111827" alt="Linux support: verified on Debian x86_64"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-16A34A" alt="License"></a>
  <a href="https://discord.gg/JvJHngZWz"><img src="https://img.shields.io/badge/Discord-Join-5865F2" alt="Discord"></a>
</p>

<p align="center">
  <a href="https://neo-isshin.github.io/actanara/dashboard-demo/"><strong>Try the Interactive Dashboard</strong></a> ·
  <a href="#install-actanara"><strong>Install Actanara</strong></a> ·
  <a href="#linux-support"><strong>Linux Support</strong></a> ·
  <a href="docs/local-operations-runbook.md">Operations Runbook</a>
</p>

<p align="center">
  <a href="https://neo-isshin.github.io/actanara/dashboard-demo/">
    <img src="docs/assets/dashboard/dashboard-ai-assets-overview.png" alt="Real Actanara local Dashboard showing AI assets across agent runtimes" width="920">
  </a>
</p>

<p align="center"><sub><b>Real local Dashboard</b> · select the image to explore the interactive demo</sub></p>

## What Actanara gives you

In a single project you might rotate through Codex, Claude Code, Gemini CLI, and other agents within a day. Each tool faithfully records your work—yet the records stay isolated from one another, and once a session ends the investigation, decisions, and results become hard to recover.

Actanara breaks down those barriers: work completed in Claude Code can be found and reused by Codex, scattered sessions become long-term reviewable progress, and deliverables and debugging evidence no longer vanish with a session.

| | Outcome |
| :--- | :--- |
| **Shared memory across agents** | Work completed in Claude Code can be found and reused from Codex through a restricted, read-only retrieval boundary. Search uses `nova-RAG` when it is ready and retains a local lexical fallback when it is not. |
| **A graph of work that actually happened** | `Nova-Task` derives tasks, status, and evidence from conversations, file changes, and tool results—not just manually written tickets. |
| **Automatic work narratives** | Daily, weekly, and monthly reports turn fragmented sessions into a durable record of progress, decisions, and lessons learned. |
| **A local source of truth** | Sessions, usage, generated assets, and task evidence remain in user-controlled local storage with explicit integration boundaries. |

**Design tradeoffs**

- **Parser-first processing:** Source-specific parsers normalize sessions, tasks, usage, and workspace signals before data enters summarization, task-evidence, or RAG workflows. Raw logs are not handed directly to an LLM.
- **Local-first with explicit boundaries:** Actanara reads configured tool locations and writes to its own runtime home. It does not rewrite external-runtime history or take over runtime execution.
- **Model-cost efficient:** Structured prompts, explicit schemas, and controlled orchestration let lightweight models produce useful output without locking the system to one provider.
- **User-controlled integrations:** Tool skills, external-runtime definitions, and critical settings remain visible, editable, and auditable.
- **Protected retrieval:** `nova-RAG` manages semantic retrieval quality through evaluation, candidate promotion, recall calibration, and safe rollback. Without RAG, Memory Search remains available through a lower-quality local lexical index; both backends expose restricted read-only retrieval to external runtimes.

> In this README, an **agent runtime** means an AI tool environment with its own sessions, logs, memory, and execution context, such as Codex, Claude Code, Gemini CLI, OpenClaw, Hermes, OpenCode, Antigravity, or Cursor.

<a id="install-actanara"></a>
## Install Actanara

```bash
curl -fsSL https://github.com/Neo-Isshin/actanara/releases/latest/download/install.sh | sh
```

This is the shared macOS/Linux entrypoint and requires no `sudo`. GitHub serves
it from the latest stable immutable Release; the asset is pinned to that
Release's exact source commit before dispatching to the platform adapter.
macOS keeps its guided fresh-install and update behavior;
Linux supports guarded fresh install, source-only refresh, locked dependency
upgrade, and confirmed repair of an existing Runtime. Linux update
transactions preserve the prior systemd user-unit state and fail closed on
definition drift or non-Actanara units. New here? You can explore the
[interactive Dashboard demo](https://neo-isshin.github.io/actanara/dashboard-demo/)
before installing.

When the Linux public entrypoint finds an existing managed Runtime, a run with
a controlling terminal prints the pinned upgrade plan and asks before applying
it. Without a controlling terminal it exits with status 2, makes no Runtime
changes, and prints exact copy-paste `actanara update --dry-run` and
`actanara update --apply` commands that include the resolved source URL,
commit, Runtime, and installer cache.

The stable Runtime shim is `~/.actanara/bin/actanara`; installation also links
`~/.local/bin/actanara` by default. On macOS, `--no-shell-path` and
`--shell-path-file /path/to/profile` control the managed profile block. On
Linux, `--no-shell-path` suppresses the user-bin link and no shell profile is
edited. Advanced source selection uses `--source-root PATH` or an exact `--ref
<full-commit-sha>`; offline operation must explicitly select one of those
sources.

For the exact write locations and launchd/systemd registration boundaries, see
the [Local Operations Runbook](docs/local-operations-runbook.md).

<a id="linux-support"></a>
## 🐧 Linux Support

Actanara has a native, non-root Linux path for Debian-class hosts using
`systemd --user`; it is not a macOS compatibility shim. The current release
gate is exercised on Debian 13 x86_64 with CPython 3.13 and systemd 257. The
dependency lock also has an arm64 target, while x86_64 is the architecture
covered by the real-host functional gate.

| Capability | Linux behavior |
| :--- | :--- |
| **Install and update** | The public Release `install.sh` entrypoint supports guarded fresh install, exact-ref/source-only update, locked dependency upgrade, and explicit repair. Fresh generations are staged and promoted atomically; failures leave the previous Runtime recoverable. |
| **User boundary and services** | Run `setup.sh` as a regular login user, not through `sudo`. Actanara does not invoke `sudo`; Dashboard, optional `nova-RAG`, and scheduled jobs run as user-level systemd units controlled through `systemctl --user`. |
| **RAG readiness** | Fresh install can enable the audited CPU-only local profile using `intfloat/multilingual-e5-small` at 384 dimensions. Fresh managed cloud RAG fails closed until a credential-backed provider profile exists. Installation reports success only after the managed listener, source commit, provider profile, model, and health response agree. |
| **Ports and headless hosts** | Dashboard and RAG bind loopback by default on ports 3036 and 3037. On a headless host, forward the configured Dashboard port over SSH and use the same local port so browser Origin checks remain valid. |
| **Session lifetime** | Non-interactive installation does not change systemd linger. Linger is requested only through an explicit option or an interactive confirmation. |

For a headless host using the default Dashboard port:

```bash
ssh -N -L 3036:127.0.0.1:3036 user@linux-host
# Then open http://127.0.0.1:3036/dashboard locally.
```

Use the actual configured Dashboard port on both sides of the tunnel. For a
quick Linux health check:

```bash
actanara doctor --installer
actanara doctor --scheduler
actanara doctor --rag  # When nova-RAG is enabled.
```

An offline fresh install preflights Python/pip bootstrap and the trusted
dependency cache; if the required bootstrap material is unavailable, it stops
before creating a release generation. Linux uses the selected system Python
and does not install a managed Python runtime.

## 🎥 Quick Start

> [!TIP]
> **Deploy with one command, then let prosperity follow.**

### 1. Basic Verification

After installation, run these read-only commands first. They do not initialize a new runtime or change existing settings:

```bash
actanara doctor
actanara model show
actanara onboard status
actanara config show
```

`actanara doctor` also supports targeted diagnostics (`--installer` / `--pipeline` / `--scheduler` / `--rag`); see the Runbook for details. The installation summary displays the actual Dashboard URL; the default is `http://127.0.0.1:3036/dashboard`.

### 2. Complete the First Run

1. **Open the Dashboard:** Use the URL in the installation summary and check the background-task and message indicators in the upper-right corner.
2. **Configure the LLM Provider:** Verify the Provider, Endpoint, Model, and API Key. Run the availability test before saving.
3. **Preview the historical-data plan:** Select a date range and review pending diaries, weekly and monthly reports, and estimated LLM calls.
4. **Queue generation:** Clear any tasks you do not need, then add the remaining work to the background queue.
5. **Review results:** Monitor progress in Background Tasks and Messages. When complete, refresh Diary, AI Assets, Nova-Task, and optional `nova-RAG` views.

<details>
<summary><strong>First-run checklist</strong></summary>

- [ ] The Dashboard opens successfully.
- [ ] The LLM Provider test passes and the settings are saved.
- [ ] `actanara doctor` reports no blocking errors.
- [ ] The history plan and selected tasks match expectations.
- [ ] The first tasks have completed or are observable in the background.
- [ ] Diary, AI Assets, and Nova-Task contain data.
- [ ] When `nova-RAG` is enabled, the Server and active index are ready.

</details>

For complete pre-install checks, first-run setup, historical backfill, daily operations, updates, and troubleshooting, see the [Local Operations Runbook](docs/local-operations-runbook.md).

## 🧭 How It Works

```text
Supported Agent Runtimes
        ↓
Parsing, Attribution, and Normalization
        ↓
Foundation Local Fact Layer
        ↓
Base Pipeline · Nova-Task · Dashboard
        ↓
Memory Search → Local Lexical Index
        └────→ nova-RAG (optional semantic backend)
        ↓
Read-only Retrieval for External Runtimes
```

| System | Core responsibility |
| :--- | :--- |
| **`Foundation`** | Normalizes AI activity, workspace attribution, snapshots, reports, and task evidence into a local fact layer. |
| **`Base Pipeline`** | Generates diaries, technical progress, learning records, and task summaries from runtime activity. |
| **`Dashboard`** | Presents diaries, AI assets, token usage, settings, background tasks, and task boards in one place. |
| **`Nova-Task`** | Maintains a reviewable task graph based on evidence from real work. |
| **`Memory Search`** | Routes read-only recall to ready `nova-RAG`, or to a local SQLite FTS/bounded-scan fallback when semantic retrieval is unavailable. |
| **`nova-RAG`** | Optional local- or cloud-embedding retrieval subsystem with a protected index lifecycle and external read-only retrieval. |
| **Attribution Parsers** | Identify runtimes, sessions, workspaces, scheduled jobs, and execution evidence, including work launched outside project directories. |

## 💻 Support and Prerequisites

- 🍎 **macOS remains first-class:** Guided installation, updates, local nova-RAG, Dashboard services, and managed scheduling retain their existing user-level `LaunchAgent` behavior.
- 🐧 **Linux has an explicit Core boundary:** Fresh installs are available for Debian-class `systemd --user` hosts with x86_64 and arm64 lock targets, including the audited CPU-only local-embedding RAG profile. Fresh managed cloud RAG fails closed until a credential-backed provider profile exists. Guarded upgrade/repair is release-gated on Debian x86_64 with CPython 3.13; the guided wizard and managed-Python bootstrap remain macOS-only.
- 🛠️ **Base tools:** Requires `git` and `curl`, plus `zsh` on macOS or POSIX `sh` on Linux; no `sudo`.
- 🐍 **Python:** macOS supports Python ≥ 3.11 and can install a verified managed Python; the audited Linux lock currently targets CPython 3.13.
- 🌐 **Network and storage:** Installation needs access to GitHub, the Python package index, and your model services; the first local `nova-RAG` run may download model weights.
- ⏱️ **Linux services:** Dashboard, scheduling, and optional RAG use user-level systemd units. When a controlling terminal is available, the installer asks whether they should continue after logout before making a no-`sudo` linger request. Non-interactive installs preserve linger unless `--enable-linger` or `--require-linger` is explicit.
- 🪟 **Windows:** Not a supported one-line target; some components can still be run from source by advanced users.

**Currently supported agent runtimes:** 🦞 OpenClaw · ✳️ Claude Code · 🤖 Codex · ✨ Gemini CLI · ⚕️ Hermes · 🐙 OpenCode · 🛡️ Antigravity · 🖱️ Cursor. Collection depends on compatible local records and enabled paths. Cursor currently contributes local session/workspace records and explicit dialogue content; Actanara does not call Cursor cloud APIs or infer missing token usage.

## 📊 Dashboard, Screenshots, and Interactive Demo

The Dashboard is Actanara's primary operating surface: daily, weekly, and monthly diaries; live overview with token usage and AI-asset metrics; Foundation operations and data repair; background tasks and messages; LLM Provider and scheduling settings; Memory Search status; the Nova-Task board with evidence review; and, when RAG is enabled, semantic search and retrieval-quality views.

### 🖼️ Real Dashboard Screenshots

The screenshots below come from the real Actanara Dashboard during development and operation, preserving the project's own design and components.

<details>
<summary><strong>Expand the Dashboard home</strong></summary>

<p align="center">
  <a href="docs/assets/dashboard/dashboard-home.png">
    <img src="docs/assets/dashboard/dashboard-home.png" alt="Actanara Dashboard home" width="100%">
  </a>
</p>

</details>

<details>
<summary><strong>Expand the W27 weekly report</strong></summary>

<p align="center">
  <a href="docs/assets/dashboard/dashboard-weekly-full.png">
    <img src="docs/assets/dashboard/dashboard-weekly-overview.png" alt="Actanara Dashboard W27 weekly report overview" width="100%">
  </a>
</p>

</details>

<details>
<summary><strong>Expand the AI Assets overview</strong></summary>

<p align="center">
  <a href="docs/assets/dashboard/dashboard-ai-assets-long.png">
    <img src="docs/assets/dashboard/dashboard-ai-assets-overview.png" alt="Actanara Dashboard AI Assets overview" width="100%">
  </a>
</p>

</details>

<details>
<summary><strong>Expand the Nova-Task work graph</strong></summary>

<p align="center">
  <a href="docs/assets/dashboard/dashboard-nova-task.png">
    <img src="docs/assets/dashboard/dashboard-nova-task.png" alt="Real Actanara Nova-Task work graph" width="100%">
  </a>
</p>

</details>

<details>
<summary><strong>Expand the nova-RAG status and retrieval interface</strong></summary>

<p align="center">
  <a href="docs/assets/dashboard/dashboard-nova-rag.png">
    <img src="docs/assets/dashboard/dashboard-nova-rag.png" alt="Actanara nova-RAG status and retrieval interface" width="100%">
  </a>
</p>

</details>

### ▶️ Interactive Demo

The [Dashboard Static Demo](https://neo-isshin.github.io/actanara/dashboard-demo/) preserves the real Dashboard HTML, CSS, and interaction code, replacing only backend APIs with static data, so it never connects to or modifies your local runtime. The version-controlled snapshot is also available at [`docs/dashboard-demo/index.html`](docs/dashboard-demo/index.html) and can be opened from a local checkout.

<p align="center">
  ▶ <a href="https://neo-isshin.github.io/actanara/dashboard-demo/"><strong>Open the Real Dashboard Static Demo</strong></a>
</p>

### Common Commands

```bash
# Search memory automatically: ready nova-RAG first, then the local lexical fallback
actanara search "deployment issue" --top-k 5

# Require one backend
actanara search "deployment issue" --mode rag --json
actanara search "deployment issue" --mode local --json

# Inspect or refresh the disposable local search index
actanara memory status
actanara memory sync
actanara memory rebuild

# Run the daily Pipeline manually (defaults to the previous calendar day; --force regenerates)
actanara pipeline
actanara pipeline 2026-07-12

# Review or apply an update (default only shows the plan; --apply executes the protected transaction)
actanara update
actanara update --dry-run
actanara update --apply
```

When dependencies are unchanged the updater reuses the venv; otherwise it rebuilds from the hashed lock. Details on venv reuse, `--source-only/--force-rebuild/--offline`, source acquisition, and commit pinning are in the Runbook's *Update* section. Actanara does not yet ship a one-command uninstaller—do not remove only `~/.actanara`; see the Runbook's *Uninstall boundary* section.

On Linux, an explicit `--source-url` or `--ref` makes the nearby bootstrap file
an execution entry only. The selected source is prepared in the installer
cache; its normalized Git `origin` and exact fetched/cached commit are verified
online and offline before the installer runs.

On Linux, ordinary updates require aligned Actanara-managed systemd definitions
and preserve each unit's prior enabled/active state. If trusted Runtime
configuration or managed definitions have drifted, use the explicitly confirmed
repair path described in the Runbook; repair never adopts or removes an
operator-owned unit.

## 📋 Nova-Task: A Graph of Real Work

`Nova-Task` is more than another to-do list. Much valuable work does not begin with a formal ticket but grows naturally through discussion, investigation, repair, experimentation, rollback, and verification—it converts those traces into a reviewable, maintainable task structure.

In automatic-maintenance mode, `Nova-Task` can detect hierarchy, update status, attach subtasks, and refine the task tree: high-impact top-level nodes retain human review, while routine updates proceed under configured rules, and a person can take over at any time. After an RFC, PRD, or Roadmap is imported, Actanara can also ask an LLM to decompose it into an iterative task tree. See [Nova-Task Work-Graph Reconciliation](docs/nova-task-work-graph-reconciliation.md).

## 🔎 Memory Search: Useful Even Without RAG

`actanara search` defaults to `--mode auto`. It uses `nova-RAG` when the semantic service is enabled and available; otherwise it falls back to an incremental local SQLite full-text index, with a bounded scan as a last-resort local path. The fallback is lexical, not semantic: exact names, IDs, dates, error text, and file names work best, while paraphrases can be missed. Use `--mode rag` to require Agentic RAG, `--mode local` to require local retrieval, or the legacy `actanara rag search-memory` command for strict RAG compatibility.

The installer manages one dynamic, read-only Memory Search Skill rather than separate RAG and non-RAG skills. Choosing **Not Now** for RAG does not prevent registration: a Skill is written only for tools in the intersection of detected, explicitly selected, and currently supported Skill targets. The same Skill inspects each response's `backend` metadata and follows either the semantic or lexical protocol.

Importing Codex and Claude Code's own managed memory is enabled by default for new runtimes, including allowlisted instruction files and `nova-RAG` ingestion when RAG is enabled. Each scope remains independently configurable, and an explicit `false` in an existing Runtime is preserved during upgrades. The adapter reads only allowlisted Markdown memory surfaces and does not inspect Cursor's private SQLite databases.

The generic external contract at `/api/memory/external/*` is read-only and loopback-only. For commands, configuration examples, response semantics, native-memory boundaries, and troubleshooting, see [Memory Search and Local Recall](docs/memory-search.md).

## 🤖 nova-RAG: Shared Memory with a Read-Only Boundary

`nova-RAG` is Actanara's optional semantic retrieval subsystem with local or cloud embeddings. When selected by Memory Search, it gives external agent runtimes **read-only** access to your work memory—they can retrieve it, but cannot write memory, change the index, alter settings, or control the service lifecycle.

Retrieval quality is managed at two levels: the server runs a deterministic, baseline-first adaptive pass, and only when it returns weak or ambiguous evidence does the external runtime's own LLM reflect further. `nova-RAG` also manages recall quality through query evaluation, candidate promotion, a protected index lifecycle, and safe rollback. For the complete read-only API, request schema, and error semantics, see the [nova-RAG External Agent Runtime Contract](docs/rag-external-agent-contract.md).

## 🔐 Privacy and Security

- **Local-first:** Runtime state, the database, generated assets, and indexes remain in user-owned local paths.
- **Secret permissions:** Provider keys live in `$ACTANARA_HOME/state/secrets`; the directory uses mode `0700`, and secret files use mode `0600`.
- **External-provider boundary:** When an external LLM or embedding provider is configured, relevant content is sent according to the selected endpoint and provider data policy.
- **Input becomes output:** If source logs or materials already contain secrets or sensitive information, generated diaries, reports, and indexes may faithfully preserve them.
- **Non-invasive boundary:** Actanara does not rewrite supported runtimes' historical data or take over their execution. It creates only its own runtime, CLI shim, optional skills, and managed services.
- **External memory boundary:** The anonymous generic `/api/memory/external/*` facade requires both a loopback peer and loopback Host; native Agent Runtime collection is limited to documented allowlisted files and can be disabled per Runtime, tool, instruction scope, or RAG ingestion scope.

## 📐 Development, Testing, and Reproducible Releases

<details>
<summary><strong>Expand development and test commands</strong></summary>

Create a local editable development environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dashboard,rag-local]"
```

Run the release suite with isolated virtual environment, `HOME`, `ACTANARA_HOME`, and a fixed business clock:

```bash
python tests/run_isolated_release_suite.py
```

Run deterministic frontend and Release Page tests:

```bash
npm ci
node --check src/dashboard/app/static/js/app.js
npm run test:dashboard-live-context
npm run test:release-page
```

Reproduce release artifacts for the current checkout:

```bash
python -B -m pip install -r requirements-release.txt
PROJECT_VERSION="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)" \
python -B -m tools.release.build_release \
  --source-root . \
  --output-dir ../actanara-release-artifacts \
  --expected-commit "$(git rev-parse HEAD)" \
  --expected-version "$PROJECT_VERSION"
```

The release builder accepts only a clean, committed Git worktree and writes output outside the repository. Artifacts include source and runtime-payload manifests, a normalized runtime archive, wheel, sdist, provenance, and `SHA256SUMS`.

</details>

## 📄 Documentation

### User and Daily Operations

- ⚙️ [Local Operations Runbook](docs/local-operations-runbook.md)
- 📖 [New User Onboarding Runbook](docs/new-user-onboarding-runbook.md)
- 🧭 [CLI Product Boundary](docs/cli-boundary.md)
- 🔎 [Memory Search and Local Recall](docs/memory-search.md)

### Integration and Product Design

- 🤖 [nova-RAG External Agent Runtime Contract](docs/rag-external-agent-contract.md)
- 🧩 [Nova-Task Work-Graph Reconciliation](docs/nova-task-work-graph-reconciliation.md)

### Release, Security, and Project History

- ✅ [Release Assurance Archive](docs/v1-release-assurance.md)
- 🧹 [Production Cleanup Inventory](docs/production-clean-inventory.md)
- 🧾 [Changelog](CHANGELOG.md)
- 🔐 [Security Policy](SECURITY.md)
- 🕰️ [Public Project History](HISTORY.md)

## ⚖️ License

Copyright © 2026 Neo-Isshin.

Actanara is licensed under the [MIT License](LICENSE), with SPDX identifier `MIT`.

## 🙏 Acknowledgements

Actanara exists thanks to outstanding AI coding tools and their open-source communities. Their local activity and token-usage logs make unified visualization, asset consolidation, and cross-runtime memory sharing possible. Thanks also to the [getdesign.md](https://getdesign.md) community for inspiration on the Dashboard's layout and visual direction.

<hr>

<a id="give-star"></a>
<div align="center">

<h2>⭐ Give me a Star</h2>

<p>
If Actanara helps you turn fragmented AI work into searchable, reusable local assets,<br>
please give it a Star so more people can discover the project.
</p>

<a href="https://github.com/Neo-Isshin/actanara">
  <img src="https://img.shields.io/github/stars/Neo-Isshin/actanara?style=for-the-badge&amp;logo=github&amp;label=Give%20me%20a%20Star&amp;color=F5B942" alt="Give Actanara a Star">
</a>

</div>
