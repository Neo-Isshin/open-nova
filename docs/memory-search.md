# Memory Search and Local Recall

Status: current product and security boundary for Actanara Memory Search.

Memory Search keeps cross-agent recall available when `nova-RAG` was not
selected during installation, is disabled, or is temporarily unavailable. It
does not try to imitate semantic retrieval: the fallback is deliberately
smaller, local, lexical, and disposable.

## Routing

The product command defaults to automatic backend selection:

```bash
actanara search "deployment issue" --top-k 5
```

| Mode | Command | Behavior |
| :--- | :--- | :--- |
| Auto | `actanara search QUERY` | Use enabled and available `nova-RAG`; otherwise use local retrieval. |
| Strict RAG | `actanara search QUERY --mode rag` | Require `nova-RAG`; return unavailable instead of falling back. |
| Strict local | `actanara search QUERY --mode local` | Bypass RAG and use only local retrieval. |
| Legacy strict RAG | `actanara rag search-memory QUERY` | Compatibility surface for the RAG-only Dashboard facade; never falls back. |

Use `--json` for automation and inspect `available`, `backend`, and
`capabilities` rather than inferring the backend from whether results exist.
`--caller codex`, `--caller claudeCode`, or another stable caller name lets
ranking avoid echoing the requesting runtime's own lower-authority native
memory when stronger cross-runtime evidence exists.

The common filters work in every product mode:

```bash
actanara search "migration 0019" \
  --date-from 2026-07-01 \
  --date-to 2026-07-31 \
  --project actanara \
  --source-set lessons \
  --top-k 8 \
  --json
```

## What the Backends Mean

An Agentic RAG response identifies itself with
`backend.kind=agentic-rag` and `backend.semantic=true`. It can retrieve
semantically related evidence and exposes the richer RAG quality and citation
contract.

The normal local response uses `backend.kind=local-fts` and
`backend.semantic=false`. Its SQLite sidecar combines Unicode full-text,
trigram, and exact substring candidates, then applies metadata, provenance,
lifecycle, and deduplication rules. If the sidecar cannot be prepared,
Actanara can use `backend.kind=bounded-scan` as a last-resort in-memory scan.
The scan obeys the configured file and byte limits.

Both local paths are **lexical retrieval, not semantic retrieval**. They work
best with:

- exact task or issue IDs;
- dates, ports, commit hashes, and error messages;
- product, project, function, and file names;
- a short quoted phrase from the remembered event;
- a second concise query using a Chinese/English variant.

They can miss paraphrases, synonyms, and conceptual relationships that do not
share terms with the indexed evidence. A local result must not be described as
semantic evidence merely because it ranked highly. Prefer current or canonical
sources, cite the returned source pointer, and validate consequential claims
against the current authoritative file or system.

## Local Index Operations

The sidecar lives at:

```text
$ACTANARA_HOME/state/cache/memory-search.sqlite3
```

With the default Runtime home, that is
`~/.actanara/state/cache/memory-search.sqlite3`. It is derived cache state, not
the source of truth, and can be rebuilt.

```bash
# Read-only status
actanara memory status
actanara memory status --json

# Incrementally re-index changed and deleted sources
actanara memory sync

# Replace the disposable sidecar; restore its prior copy if rebuilding fails
actanara memory rebuild
```

The base Pipeline refreshes the local index after successful materialization
when `memorySearch.local.syncAfterPipeline=true`. A local index refresh failure
is reported as degraded recall; it does not convert already materialized diary
or task outputs back into failures. Run `actanara memory sync` to retry and
`actanara memory status --json` to inspect the reason and capabilities.

The relevant settings and defaults are:

```json
{
  "memorySearch": {
    "enabled": true,
    "backendPolicy": "auto",
    "local": {
      "enabled": true,
      "syncAfterPipeline": true,
      "maxScanFiles": 2000,
      "maxScanBytes": 67108864
    }
  }
}
```

`backendPolicy` can be `auto`, `rag`, or `local`. A command's explicit
`--mode` is the request-level choice; `backendPolicy` is the Runtime default
used by auto routing.

## One Dynamic Skill

Actanara manages one read-only Memory Search Skill for external Agent
Runtimes. It does not install separate “RAG” and “non-RAG” skills. The Skill
calls `actanara search` and inspects `backend.kind`, `backend.semantic`,
`backend.degraded`, and `backend.fallbackFrom` before following the semantic or
lexical retrieval protocol.

Selecting **Not Now** for RAG during installation does not disable Skill
registration. A managed Skill is created only for the intersection:

```text
detected tools ∩ explicitly selected tools ∩ supported Skill targets
```

The current managed targets are OpenClaw, Claude Code, Codex, Gemini CLI, and
Hermes. Detecting OpenCode, Antigravity, or Cursor does not authorize Actanara
to invent a global Skill target for those tools. Upgrades reconcile an
unmodified managed Skill to the current dynamic template; customized Skill
files are preserved unless overwrite is explicitly confirmed.

The installer option is `--register-memory-skills`.
`--register-rag-skills` remains a compatibility alias. Skill registration is
an integration choice and does not enable `nova-RAG`, native-memory ingestion,
or a model provider.

## Optional Codex and Claude Code Native Memory

Runtime-managed native memory is enabled by default for new Runtimes. Codex,
Claude Code, allowlisted instruction Markdown, and RAG ingestion are all on
unless explicitly disabled. Additive settings normalization gives the same
defaults to fields that are absent from an older `settings.json`; an explicit
`false` already stored by the user is never replaced. Four controls remain
independent:

1. `nativeMemory.enabled` permits native-memory collection.
2. `nativeMemory.tools.codex` and/or `nativeMemory.tools.claudeCode` select the
   runtimes whose allowlisted files may be read.
3. `includeInstructions` permits instruction files, while `allowInRag`
   separately permits the selected native memory to enter `nova-RAG`.

For example, this keeps native memory available to local lexical recall while
disabling instruction files and semantic ingestion:

```bash
actanara config set memorySearch.nativeMemory.includeInstructions false
actanara config set memorySearch.nativeMemory.allowInRag false
actanara memory sync
```

Disable the entire native-memory surface, or only one tool, when wanted:

```bash
actanara config set memorySearch.nativeMemory.enabled false
actanara config set memorySearch.nativeMemory.tools.claudeCode false
```

The same scope controls are available under **Dashboard → Settings →
Advanced → Memory**. Codex, Claude Code, instruction-file ingestion, and RAG
ingestion remain separate switches.

`includeInstructions=true` does not imply `allowInRag=true`, and
`allowInRag=true` does not imply `includeInstructions=true`. Native RAG
ingestion still requires native memory to be enabled and its runtime tool to
be selected.

The adapters read only narrow Markdown surfaces:

- Codex: `MEMORY.md`, `memory_summary.md`, and Markdown rollout summaries
  below its managed memories root;
- Claude Code: Markdown below each managed project `memory` directory;
- when `includeInstructions=true`, Codex `AGENTS.md` and
  `instructions/*.md`, plus Claude Code's global `CLAUDE.md`.

They reject unsafe or out-of-bound paths and do not inspect raw chat/session
stores, IDE extensions, or private IDE databases. In particular, native-memory
ingestion does **not** read Cursor's private SQLite databases. Cursor's
separate, existing runtime parser support remains outside this native-memory
collection scope.

Native discovery is bounded per collection: at most 2,000 eligible files,
64 MiB of aggregate reads, 20,000 discovered filesystem entries, and 2 MiB per
file. Codex `instructions/*.md` is shallow; native memory and rollout-summary
directories retain bounded recursive discovery. Truncation is reported in the
collector diagnostics and manifest instead of silently expanding the boundary.

Native memory is historical recall evidence, not current authority. Source
records retain producer, scope, path, hash, lifecycle, and lineage metadata so
Actanara can deduplicate content and reduce self-echo. Instructions use the
distinct `agent-native-instructions` source set; other native memory uses
`agent-native-memory`.

## Read-only External API

The generic external facade follows the same routing contract:

- `GET /api/memory/external/health`
- `GET /api/memory/external/contract`
- `POST /api/memory/external/search`

Example against the default Dashboard listener:

```bash
curl -sS http://127.0.0.1:3036/api/memory/external/search \
  -H 'Content-Type: application/json' \
  --data '{"query":"migration 0019","topK":5,"mode":"auto","caller":"codex","remainingBudgetMs":90000,"budgetCall":1,"budgetMaxCalls":3}'
```

This `/api/memory/external/*` surface is anonymous, read-only, and
**loopback-only**. The network peer and requested Host must both resolve as
loopback, and forwarded-client headers cause the anonymous request to be
rejected. Do not publish it through a reverse proxy or bind it as a remote
agent API. Use the local `actanara` CLI on the Runtime host or an authenticated
Dashboard API for remote operator workflows.

Agent callers send positive-integer `remainingBudgetMs`, `budgetCall`, and
`budgetMaxCalls` values on every HTTP search. Actanara clamps one request to
the Skill's 90-second total budget and rejects invalid call metadata; callers
must carry the decreasing monotonic budget across their own follow-up calls.

The older `/api/rag/external/*` facade remains a RAG-only compatibility
surface and enforces the same loopback peer, loopback Host, and forwarded-header
rejection boundary. Neither external facade permits settings changes, index
sync or rebuild, candidate promotion, rollback, server control, or writes to
source memory.

## Troubleshooting

Start with:

```bash
actanara memory status --json
actanara search "one exact remembered phrase" --mode local --json
actanara search "the same question" --mode rag --json
```

- `available=false` with strict RAG means the semantic service is not ready;
  auto mode may still provide local recall.
- `backend.fallbackFrom` explains why auto mode left RAG.
- `backend.semantic=false` is expected for the local fallback.
- An empty local result is not proof that the event did not happen. Retry once
  with rarer exact terms or a bilingual variant.
- A stale or unavailable sidecar can usually be repaired with
  `actanara memory sync`, followed by `actanara memory rebuild` if necessary.
- Native-memory setting changes take effect after the local index is refreshed;
  entering RAG additionally requires `allowInRag` and an enabled RAG service.

For the protected semantic index lifecycle and its detailed response fields,
see the
[nova-RAG External Agent Runtime Contract](rag-external-agent-contract.md).
For the command support policy, see the [CLI Boundary](cli-boundary.md).
