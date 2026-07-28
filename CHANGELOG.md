# Changelog

All notable public changes to Actanara are documented here.

## Unreleased

## [1.5.0] - 2026-07-27

### Added

- Add read-only local Runtime discovery and normalized session ingestion for
  OpenCode, Antigravity, and Cursor, including configured paths, XDG homes,
  executable evidence, and Cursor IDE-only state.
- Add OpenCode and Antigravity local usage attribution across Foundation,
  AI Assets, Token Clock, diary metrics, and narrative inputs. Merge
  Antigravity CLI, IDE, and app variants into one displayed tool family.

### Changed

- Show Dashboard Runtime cards only when the corresponding tool is detected
  locally, while retaining complete Settings discovery/configuration surfaces
  and historical diary/report facts.
- Keep detected zero-usage tools visible, preserve degraded cards when a local
  usage scanner fails, and exclude Cursor from Token Clock until reliable local
  usage data is available.
- Re-evaluate current tool presence when reading AI Assets snapshots so removed
  tools do not leave stale cards, and recompute visible tool, session, message,
  Agent, storage, skill, and configuration totals consistently.

### Security and compatibility

- Read supported Runtime data without modifying external tool state. Cursor
  integration remains local-only and makes no account or usage API requests.
- Treat a generic Gemini home as insufficient Antigravity evidence, retain
  explicit provenance for every detection result, and fail closed when Runtime
  presence cannot be determined.

## [1.4.0] - 2026-07-21

### Added

- Enable audited Linux x86_64 and arm64 local-embedding nova-RAG installs with
  hash-pinned official PyTorch CPU wheels and the shared local embedding
  settings contract.
- Add guarded Linux Runtime source-only refresh, automatic or forced locked-venv
  upgrade, and explicitly confirmed repair with durable rollback/recovery
  journals.

### Changed

- Route `actanara update` through the POSIX Linux adapter on Linux while
  retaining the existing zsh/macOS transaction and result contract.
- Preserve each managed systemd user unit's exact enabled/active state across
  standard updates, bind aligned definition hashes before service stop, and
  report bounded post-update Doctor evidence.
- Replace Linux Doctor LaunchAgent and macOS-timezone residuals with systemd
  definition alignment and platform-neutral scheduler status.

### Security and compatibility

- Standard Linux updates reject stale, missing, modified, or non-Actanara unit
  definitions before service stop; confirmed repair may reconcile only units
  carrying the Actanara management marker.
- Linux update, repair, failure compensation, and interrupted recovery are
  release-gated on Debian 13 x86_64 with CPython 3.13. No Linux update path
  invokes `sudo` or silently changes linger.

## [1.3.0] - 2026-07-21

### Added

- Add a shared macOS and Linux setup entrypoint plus a fresh-install Linux
  adapter for Debian 13 x86_64 with CPython 3.13 and exact, hash-verified
  Runtime dependencies.
- Add managed systemd user services for Dashboard and optional nova-RAG, and
  managed systemd service/timer pairs for the daily pipeline and Dashboard
  aggregation scheduler.
- Add a platform-neutral service manager with preview, status, install/update,
  uninstall, start, stop, restart, definition-alignment, transactional
  compensation, and interrupted-operation recovery contracts.
- Add explicit Linux linger controls with interactive confirmation,
  `--enable-linger`, `--require-linger`, and `--no-linger-prompt` policies.

### Changed

- Make Dashboard Startup, Scheduler, and RAG service controls select launchd on
  macOS and systemd user services on Linux without exposing LaunchAgent-only
  language or APIs on Linux.
- Reuse the same managed systemd unit generators and lifecycle transactions in
  the installer and Dashboard so settings changes safely reconcile service
  definitions.
- Split isolated release gates by platform while retaining the complete macOS
  regression suite and adding Linux-specific release validation.

### Security and compatibility

- Never invoke `sudo`, never change linger without explicit authorization, and
  never disable linger during uninstall.
- Refuse to overwrite or remove systemd units that do not carry the Actanara
  management marker, and compensate failed or interrupted managed-unit writes.
- Preserve the existing macOS launchd transactions and verified behavior. Linux
  support in this release is the x86_64 fresh-install Core boundary; upgrade,
  repair, and local-embedding expansion remain outside that boundary.

## [1.2.0] - 2026-07-19

### Added

- Attribute reported or conservatively estimated LLM tokens to pipeline runs,
  stages, calls, chunks, retries, and fallbacks, with detailed Dashboard views
  that keep missing historical usage explicitly unavailable.
- Add ordered multi-provider execution with per-provider readiness checks,
  secret-safe failure records, and bounded fallback behavior across narrative,
  period-summary, history, and Nova-Task LLM paths.
- Add nova-RAG v2 external content sources with supplement or replace modes,
  multiple recursive paths, include/exclude filters, bounded structured parsing,
  symlink and traversal defenses, incremental metadata, and read-only dry-run
  plans. Legacy `.doc` files remain unsupported and should first be converted to
  `.docx`, PDF, or plain text.
- Add Tailscale installation, login, IP, MagicDNS, reachability, and Serve
  status checks plus explicit tailnet-only Serve controls. Funnel remains
  unavailable and fail-closed, and nova-RAG is never exposed through this path.
- Add browser-local social-share PNG previews for weekly reports, monthly
  reports, and AI Assets, using a privacy allowlist rather than full-page DOM
  capture, with clipboard and download fallbacks.
- Add verified AI Assets backups with consistent SQLite snapshots, staged
  atomic publication, per-file hashes, manifest self-verification, constrained
  retention, scheduler integration, and bilingual Dashboard controls. This
  release supports backup and verification only; restore is intentionally not
  implemented.

### Changed

- Extend settings and machine-facing responses additively for provider chains,
  external RAG sources, Tailscale state, share metadata, and backup policy while
  retaining the legacy single-provider settings mirror.
- Add migration `0019_pipeline_llm_attribution.sql`; all previous migrations
  remain byte-for-byte unchanged and their compatibility hashes remain valid.
- Treat active source metadata as the product-version authority for the CLI and
  backup manifests, including when an older installed distribution remains in a
  reused virtual environment.

### Security and compatibility

- Never include Runtime secret files in backups, redact settings secret values
  and sensitive secret-reference details, and reject unsafe backup targets,
  traversal, escaping symlinks, device files, insufficient space, and partial
  publication.
- Keep Tailscale Funnel disabled because the Dashboard session is not an
  independent user-identity boundary; Serve binds only the loopback Dashboard
  to the tailnet after explicit user action.
- Preserve and upgrade Actanara v1.1.0 Runtime data through additive contracts.
  Runtimes from the former Open Nova product remain outside the supported
  compatibility boundary.
- Write external content only to the nova-RAG v2 candidate store. The legacy
  index is neither migrated nor modified.

## [1.1.0] - 2026-07-18

### Changed

- Rename the main project, package, CLI, Runtime, Dashboard, installer,
  LaunchAgents, documentation, and public repository from Open Nova to
  Actanara.
- Replace main-project environment variables and machine-contract path fields
  with `ACTANARA_*` and `actanaraHome`; `nova-RAG`, `Nova-Task`,
  `NOVA_RAG_*`, and `nova_task_*` remain subsystem contracts.
- Publish Actanara-branded package and Runtime artifacts. The rename is a clean
  project transition and does not retain old main-project aliases.

### Fixed

- Validate only the selected installer payload blobs when an offline update
  reuses a sparse partial-clone cache, without requiring unrelated public-source
  blobs or permitting a lazy fetch.
- Reject an offline update plan unless it names a local source checkout or an
  explicit full commit already present in the installer source cache.

## [1.0.2] - 2026-07-14

### Added

- Add an exact, hash-verified Runtime dependency lock for every supported
  CPython ABI and macOS architecture, distinct from the release-tooling lock.
- Persist an immutable dependency manifest in every new venv generation and
  derive update decisions from its normalized dependency fingerprint.
- Add explicit `--source-only`, `--force-rebuild`, and `--offline` update
  controls plus truthful human/JSON execution results.
- Publish a version-independent `install.sh` asset with every stable Release so
  the public one-liner always resolves through GitHub's latest stable channel.

### Changed

- Reuse the active venv without invoking pip or dependency networking when its
  full dependency contract matches; otherwise build an isolated candidate venv
  from a persistent, verified wheel cache before atomically switching pointers.
- Preserve Settings-selected RAG profiles and marker-selected operational
  profiles across updates, including compatibility with v1.0.1 update flags.
- Treat the active source manifest as the Runtime product-version authority;
  stale `actanara-*.dist-info` in a reused venv is not refreshed in place.
- Publish the real Dashboard static demo and refreshed bilingual product and
  operations documentation.

### Fixed

- Keep Dashboard transport connectivity separate from source-health status,
  restore canonical Actanara titles, and harden session and token discovery.

### Security and release integrity

- Bind Settings bytes, active venv identity, and dependency-marker state into
  the update transaction and revalidate them before any managed service stop.
- Fail closed before service changes for unsafe profile evidence, unsupported
  lock targets, incompatible source-only requests, and offline cache misses.
- Preserve old source/venv generations for rollback and report incomplete
  rollback state as unknown instead of claiming pointers were unchanged.

## [1.0.1] - 2026-07-13

### Fixed

- Rebind managed Dashboard, watchdog, nova-RAG, and Scheduler LaunchAgents from
  stale concrete release/venv directories to stable Runtime pointers during an
  update, including installations whose plist is more than one release behind.
- Publish the source commit actually loaded by Dashboard and nova-RAG health
  endpoints and require it to match the promoted candidate before commit.
- Generate new managed-service definitions from stable Runtime source and venv
  paths so future updates do not retain a prior release directory.
- Refuse managed-service writes when an explicitly selected Runtime cannot be
  validated, instead of falling back to a different default Runtime.

### Security and release integrity

- Fail closed when GitHub's latest published Release is explicitly marked
  `WITHDRAWN`, even if GitHub still reports it as non-draft and non-prerelease.
- Preserve exact pre-update plist bytes for transactional rollback and verify
  canonical service bindings again before promotion, restore, verification,
  and commit.
- Add a locked, minimal-environment release builder that proves the complete
  source file set remains unchanged and emits deterministic manifests,
  Runtime/package artifacts, provenance, and checksums.

## [1.0.0] - 2026-07-13 (WITHDRAWN)

v1.0.0 is retained as immutable public audit history but must not be installed
or recommended. Post-publication E2E found that a successful update could leave
managed background services executing an older concrete source directory.

### Added

- Initial public source release of the Actanara local AI operations runtime.
- Guided macOS installation, local Runtime management, guarded updates, and
  operational diagnostics.
- Foundation, Dashboard, Nova-Task, and optional nova-RAG product surfaces.
- English and Chinese user documentation, a static release page, and
  contributor-facing automated regression suites.

### Security and release integrity

- Default installs and updates resolve the latest stable GitHub Release to an
  exact full commit before source checkout.
- Draft, prerelease, missing, malformed, rate-limited, abbreviated, symbolic,
  and non-commit source selections fail closed.
- The hosted one-liner is versioned at `v1.0.0` and is fresh-install-only.
- Runtime secrets remain in the Runtime-local private secret store and are
  excluded from source and release artifacts.

[1.5.0]: https://github.com/Neo-Isshin/actanara/releases/tag/v1.5.0
[1.4.0]: https://github.com/Neo-Isshin/actanara/releases/tag/v1.4.0
[1.3.0]: https://github.com/Neo-Isshin/actanara/releases/tag/v1.3.0
[1.2.0]: https://github.com/Neo-Isshin/actanara/releases/tag/v1.2.0
[1.1.0]: https://github.com/Neo-Isshin/actanara/releases/tag/v1.1.0
[1.0.2]: https://github.com/Neo-Isshin/actanara/releases/tag/v1.0.2
[1.0.1]: https://github.com/Neo-Isshin/actanara/releases/tag/v1.0.1
[1.0.0]: https://github.com/Neo-Isshin/actanara/releases/tag/v1.0.0
