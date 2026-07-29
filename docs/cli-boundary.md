# Actanara CLI Boundary

Status: current-version product boundary for the installable `actanara`
command. This document separates stable user commands from guarded operator
commands and compatibility/debug surfaces.

## Product Commands

These commands are the supported first-line CLI surface after install:

```bash
actanara
actanara doctor
actanara model show
actanara model list
actanara model set --provider PROVIDER --model MODEL
actanara model set --api-key-env LLM_API_KEY
actanara model key --value-stdin
actanara onboard status
actanara onboard doctor
actanara onboard plan
actanara dashboard restart
actanara config show
actanara config keys
actanara config get general.timezone
actanara config set general.timezone Asia/Hong_Kong
actanara search "deployment issue" --top-k 5 --json
actanara search "deployment issue" --mode local --json
actanara memory status
actanara memory sync
actanara memory rebuild
actanara task
actanara pipeline [YYMMDD|YYYY-MM-DD]
actanara rag-update --dry-run
actanara rag-rebuild --dry-run
actanara update --dry-run
```

The default no-argument command prints this product command guide. README and
new-user docs should prefer these commands.

## Memory Search Boundary

`actanara search` defaults to `--mode auto`. Auto mode prefers an enabled,
available `nova-RAG` backend and falls back to the local SQLite full-text
index when RAG is disabled or unavailable. If the index cannot be prepared, a
bounded local scan is the final fallback.

Backend selection is explicit in machine-readable output:

- `backend.kind=agentic-rag` and `backend.semantic=true` identify semantic RAG.
- `backend.kind=local-fts` or `backend.kind=bounded-scan`, together with
  `backend.semantic=false`, identify lexical retrieval.
- Local lexical matches are evidence, not semantic equivalence. Callers should
  prefer exact entities, IDs, dates, file names, and error strings and must not
  claim paraphrase coverage from a lexical result.

Use `--mode rag` when RAG is a hard requirement and `--mode local` when the
request must remain on the local lexical path. `actanara rag search-memory`
is the strict RAG compatibility command and never silently switches to local
retrieval.

The `actanara memory` group manages only the disposable local sidecar:

- `actanara memory status` is read-only.
- `actanara memory sync` incrementally refreshes changed sources.
- `actanara memory rebuild` replaces the sidecar and restores its prior copy
  if rebuilding fails.

These commands do not rebuild, promote, or roll back a `nova-RAG` index. See
[Memory Search and Local Recall](memory-search.md) for routing, Skill,
native-memory, and API boundaries.

## Guarded Maintenance

These commands are user-visible, but write-capable variants require explicit
confirmation phrases:

- `actanara rag-update`
- `actanara rag-rebuild`
- `actanara foundation rebuild-sqlite-cache`
- `actanara foundation approve-diary-metrics`
- `actanara model key --value-stdin`

Dry-run and read-only modes are safe to document for routine operations. Any
write-capable example must include the confirmation requirement and the expected
rollback or audit artifact when one exists.

## Compatibility And Debug Commands

These command groups remain available for migration, installer, dashboard or
operator debugging, but they are not the primary product surface:

- `actanara settings ...`
- `actanara onboarding ...`
- `actanara foundation ...`
- `actanara secrets ...`
- `actanara rag search-memory ...`

`actanara rag search-memory` is a strict compatibility alias for the read-only
Dashboard RAG facade. It never uses the local fallback. Product docs should
prefer `actanara search ...`; use `actanara search ... --mode rag` when the
new product surface must also require RAG.

## Scheduler and Service Boundary

macOS and Linux scheduling use one planner:
`data_foundation.scheduler_preview`.

- On macOS, Dashboard system timer controls retain the established launchd
  handoff: managed LaunchAgent plists, `launchctl`, and scheduler settings are
  updated in one transaction.
- On Linux, the same Dashboard controls install, reconcile, and safely remove
  managed systemd user services and timers through `systemctl --user`.
- Installer and Dashboard Linux flows share the render, alignment, backup,
  compensation, and recovery implementation in
  `data_foundation.systemd_user`; neither flow changes linger implicitly or
  removes an unmanaged unit.
- Dashboard and optional RAG services use the platform service manager:
  launchd on macOS and systemd user units on Linux.

The expected current state is one platform-neutral service contract with
separate, guarded launchd and systemd-user backends.

## Out Of Boundary

Do not add current-version CLI commands that:

- edit prompt payloads, diary schemas, RAG evidence schema, or machine contracts;
- let external governance agents modify or append RAG facts;
- bypass the platform service manager to mutate launchd or systemd definitions;
- hide RAG sync failures as successful pipeline completion.
