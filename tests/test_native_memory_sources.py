from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.native_memory_sources import (
    INSTRUCTIONS_SOURCE_SET,
    PARSER_VERSION,
    SOURCE_SET,
    collect_claude_native_memory,
    collect_codex_native_memory,
    collect_native_memory_sources,
)


class NativeMemorySourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_codex_collects_only_allowlisted_memory_markdown(self) -> None:
        home = self.root / "codex"
        memory_root = home / "memories"
        self._write(memory_root / "MEMORY.md", "# Codex memory\n\nGlobal reusable note.")
        self._write(
            memory_root / "memory_summary.md",
            "# Memory summary\n\nA compact native summary.",
        )
        rollout = self._write(
            memory_root / "rollout_summaries" / "rollout-one.md",
            "\n".join(
                (
                    "thread_id: thread-123",
                    "updated_at: 2026-07-29T01:02:03+00:00",
                    "cwd: /Volumes/SSD/DEV/actanara",
                    "derivedFromActanara: true",
                    "",
                    "# Completed native-memory work",
                    "",
                    "Validated the local implementation.",
                )
            ),
        )
        self._write(memory_root / "other.md", "must not be collected")
        self._write(
            memory_root / "rollout_summaries" / "raw" / "secret.md",
            "raw content must not be collected",
        )
        self._write(
            memory_root / "rollout_summaries" / ".git" / "metadata.md",
            "git metadata must not be collected",
        )
        self._write(
            memory_root / "rollout_summaries" / "extensions" / "plugin.md",
            "extension content must not be collected",
        )
        self._write(
            memory_root / "rollout_summaries" / "sessions" / "session.md",
            "session content must not be collected",
        )
        self._write(home / "sessions" / "MEMORY.md", "raw session memory lookalike")
        self._write(home / "AGENTS.md", "# Instructions\n\nDefault-off instructions.")

        result = collect_codex_native_memory(home)

        self.assertEqual(result["sourceSet"], SOURCE_SET)
        self.assertEqual(result["manifest"]["sourceCount"], 3)
        self.assertEqual(result["manifest"]["documentCount"], 3)
        self.assertFalse(result["manifest"]["instructionsIncluded"])
        self.assertEqual(
            {item["nativeKind"] for item in result["documents"]},
            {"memory-index", "memory-summary", "rollout-summary"},
        )
        by_kind = {item["nativeKind"]: item for item in result["documents"]}
        self.assertNotEqual(
            by_kind["memory-index"]["lineageFamily"],
            by_kind["memory-summary"]["lineageFamily"],
        )
        serialized_paths = "\n".join(item["sourcePath"] for item in result["documents"])
        for forbidden in ("other.md", "/raw/", "/.git/", "/extensions/", "/sessions/"):
            self.assertNotIn(forbidden, serialized_paths)

        rollout_document = next(
            item
            for item in result["documents"]
            if item["nativeKind"] == "rollout-summary"
        )
        self.assertEqual(rollout_document["scopeType"], "project")
        self.assertEqual(rollout_document["scopeKey"], "/Volumes/SSD/DEV/actanara")
        self.assertEqual(rollout_document["lineageFamily"], "codex-thread:thread-123")
        self.assertTrue(rollout_document["derivedFromActanara"])
        self.assertTrue(rollout_document["requiresValidation"])
        self.assertEqual(rollout_document["metadata"]["sourceSet"], SOURCE_SET)
        self.assertEqual(rollout_document["date"], "2026-07-29")
        self.assertEqual(
            next(item for item in result["sources"] if item["path"] == str(rollout))[
                "contentHash"
            ],
            hashlib.sha256(rollout.read_bytes()).hexdigest(),
        )

        with_instructions = collect_codex_native_memory(
            home,
            include_instructions=True,
        )
        self.assertEqual(with_instructions["manifest"]["sourceCount"], 4)
        self.assertTrue(with_instructions["manifest"]["instructionsIncluded"])
        self.assertIn(
            "instructions",
            {item["nativeKind"] for item in with_instructions["documents"]},
        )
        self.assertEqual(
            {
                item["sourceSet"]
                for item in with_instructions["documents"]
                if item["nativeKind"] == "instructions"
            },
            {INSTRUCTIONS_SOURCE_SET},
        )

    def test_claude_collects_only_project_memory_with_non_lossy_scope(self) -> None:
        claude_home = self.root / "claude"
        projects = claude_home / "projects"
        first_slug = "-Volumes-SSD-DEV-actanara"
        second_slug = "-Volumes-SSD-DEV-actanara-with-hyphen"
        first_memory = projects / first_slug / "memory"
        second_memory = projects / second_slug / "memory"
        self._write(first_memory / "MEMORY.md", "# Project memory\n\n- [Style](style.md)")
        self._write(
            first_memory / "style.md",
            "\n".join(
                (
                    "---",
                    "name: Actanara style",
                    "metadata:",
                    "  originSessionId: claude-session-1",
                    "---",
                    "",
                    "# Documentation style",
                    "",
                    "Keep the paired documents aligned.",
                )
            ),
        )
        self._write(first_memory / "ignored.jsonl", '{"raw": true}\n')
        self._write(first_memory / ".git" / "hidden.md", "repository metadata")
        self._write(projects / first_slug / "outside-memory.md", "not native memory")
        self._write(second_memory / "style.md", "# Other project\n\nSame filename, distinct scope.")
        self._write(claude_home / "CLAUDE.md", "# Global instructions\n\nDefault off.")

        result = collect_claude_native_memory(
            projects,
            project_scope_map={first_slug: "/Volumes/SSD/DEV/actanara"},
        )

        self.assertEqual(result["manifest"]["sourceCount"], 3)
        first_documents = [
            item
            for item in result["documents"]
            if item["scopeKey"] == "/Volumes/SSD/DEV/actanara"
        ]
        self.assertEqual(len(first_documents), 2)
        self.assertTrue(all(item["scopeType"] == "project" for item in first_documents))
        self.assertTrue(all(item["scopeReliable"] for item in first_documents))
        self.assertTrue(
            all(
                item["scopeEvidence"] == "explicit-project-scope-map"
                for item in first_documents
            )
        )
        style_document = next(
            item for item in first_documents if item["nativeKind"] == "project-memory"
        )
        self.assertEqual(
            style_document["lineageFamily"],
            "claude-session:claude-session-1",
        )
        self.assertEqual(style_document["title"], "Actanara style")

        second_document = next(
            item
            for item in result["documents"]
            if item["scopeKey"].startswith("claude-project:")
        )
        self.assertEqual(
            second_document["scopeKey"],
            f"claude-project:{second_slug}",
        )
        self.assertEqual(
            second_document["scopeEvidence"],
            "claude-native-project-directory-key",
        )
        self.assertNotEqual(style_document["sourceId"], second_document["sourceId"])
        self.assertFalse(
            any(item["nativeKind"] == "instructions" for item in result["documents"])
        )

        with_instructions = collect_claude_native_memory(
            projects,
            include_instructions=True,
        )
        instructions = [
            item
            for item in with_instructions["documents"]
            if item["nativeKind"] == "instructions"
        ]
        self.assertEqual(len(instructions), 1)
        self.assertEqual(instructions[0]["scopeKey"], "claude-code:global")
        self.assertEqual(instructions[0]["sourceSet"], INSTRUCTIONS_SOURCE_SET)

    def test_symlinks_are_followed_only_when_contained_by_memory_root(self) -> None:
        home = self.root / "codex"
        rollout_root = home / "memories" / "rollout_summaries"
        target = self._write(rollout_root / "target.md", "# Target\n\nContained.")
        forbidden_target = self._write(
            rollout_root / "raw" / "secret.md",
            "# Raw target\n\nMust not be read through an allowlisted-looking symlink.",
        )
        outside = self._write(self.root / "outside.md", "# Outside\n\nMust not be read.")
        internal_link = rollout_root / "internal.md"
        forbidden_link = rollout_root / "visible.md"
        outside_link = rollout_root / "outside.md"
        try:
            internal_link.symlink_to(target)
            forbidden_link.symlink_to(forbidden_target)
            outside_link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        result = collect_codex_native_memory(home)

        paths = {item["sourcePath"] for item in result["documents"]}
        self.assertIn(str(target), paths)
        self.assertIn(str(internal_link), paths)
        self.assertNotIn(str(forbidden_link), paths)
        self.assertNotIn(str(outside_link), paths)
        internal_source = next(
            item for item in result["sources"] if item["path"] == str(internal_link)
        )
        self.assertTrue(internal_source["symlink"])
        outside_diagnostic = next(
            item for item in result["diagnostics"] if item["path"] == str(outside_link)
        )
        self.assertIn("symlink-target-outside-root", outside_diagnostic["reason"])
        forbidden_diagnostic = next(
            item for item in result["diagnostics"] if item["path"] == str(forbidden_link)
        )
        self.assertIn("forbidden-target-path", forbidden_diagnostic["reason"])

    def test_codex_memory_root_symlink_cannot_escape_codex_home(self) -> None:
        home = self.root / "codex"
        outside_memory = self.root / "outside-memories"
        home.mkdir(parents=True)
        self._write(outside_memory / "MEMORY.md", "# Outside\n\nMust not be read.")
        try:
            (home / "memories").symlink_to(outside_memory, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        result = collect_codex_native_memory(home)

        self.assertEqual(result["documents"], [])
        self.assertEqual(result["sources"], [])
        diagnostic = next(
            item
            for item in result["diagnostics"]
            if item["path"] == str(home / "memories")
            and item["status"] == "skipped"
        )
        self.assertEqual(
            diagnostic["reason"],
            "codex-memories-symlink-target-outside-home",
        )

    def test_codex_memory_root_symlink_cannot_alias_forbidden_session_store(self) -> None:
        home = self.root / "codex"
        session_root = home / "sessions"
        self._write(
            session_root / "MEMORY.md",
            "# Raw session\n\nThis allowlisted-looking alias must not be read.",
        )
        try:
            (home / "memories").symlink_to(session_root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        result = collect_codex_native_memory(home)

        self.assertEqual(result["documents"], [])
        self.assertEqual(result["sources"], [])
        diagnostic = next(
            item
            for item in result["diagnostics"]
            if item["path"] == str(home / "memories")
            and item["status"] == "skipped"
        )
        self.assertEqual(
            diagnostic["reason"],
            "codex-memories-symlink-rejected",
        )

    def test_recursive_allowlist_directory_symlinks_cannot_alias_forbidden_stores(self) -> None:
        codex_home = self.root / "codex"
        memory_root = codex_home / "memories"
        session_root = codex_home / "sessions"
        memory_root.mkdir(parents=True)
        self._write(session_root / "rollout.md", "# Raw rollout\n\nMust not be read.")
        try:
            (memory_root / "rollout_summaries").symlink_to(
                session_root,
                target_is_directory=True,
            )
            (codex_home / "instructions").symlink_to(
                session_root,
                target_is_directory=True,
            )
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        result = collect_codex_native_memory(
            codex_home,
            include_instructions=True,
        )

        self.assertEqual(result["documents"], [])
        rejected = {
            item["path"]: item["reason"]
            for item in result["diagnostics"]
            if item["status"] == "skipped"
        }
        self.assertEqual(
            rejected[str(memory_root / "rollout_summaries")],
            "memory-directory-symlink-rejected",
        )
        self.assertEqual(
            rejected[str(codex_home / "instructions")],
            "memory-directory-symlink-rejected",
        )

    def test_claude_memory_directory_symlink_cannot_alias_project_sessions(self) -> None:
        projects = self.root / "claude" / "projects"
        project = projects / "-workspace"
        session_root = project / "sessions"
        self._write(
            session_root / "MEMORY.md",
            "# Raw Claude session\n\nMust not be read.",
        )
        try:
            (project / "memory").symlink_to(
                session_root,
                target_is_directory=True,
            )
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        result = collect_claude_native_memory(projects)

        self.assertEqual(result["documents"], [])
        self.assertEqual(result["sources"], [])
        diagnostic = next(
            item
            for item in result["diagnostics"]
            if item["path"] == str(project / "memory")
        )
        self.assertEqual(
            diagnostic["reason"],
            "memory-directory-symlink-rejected",
        )

    def test_claude_project_symlink_cannot_escape_projects_root(self) -> None:
        projects = self.root / "claude" / "projects"
        projects.mkdir(parents=True)
        real_project = projects / "-real"
        self._write(real_project / "memory" / "MEMORY.md", "# Real project")
        outside_project = self.root / "outside-project"
        self._write(outside_project / "memory" / "MEMORY.md", "# Outside project")
        escaped = projects / "-escaped"
        internal_alias = projects / "-internal-alias"
        try:
            escaped.symlink_to(outside_project, target_is_directory=True)
            internal_alias.symlink_to(real_project, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        result = collect_claude_native_memory(projects)

        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["documents"][0]["scopeKey"], "claude-project:-real")
        diagnostic = next(
            item for item in result["diagnostics"] if item["path"] == str(escaped)
        )
        self.assertEqual(
            diagnostic["reason"],
            "project-symlink-target-outside-root",
        )
        internal_diagnostic = next(
            item
            for item in result["diagnostics"]
            if item["path"] == str(internal_alias)
        )
        self.assertEqual(
            internal_diagnostic["reason"],
            "project-symlink-rejected-for-reliable-scope",
        )

    def test_manifest_fingerprint_tracks_content_not_mtime_only(self) -> None:
        home = self.root / "codex"
        source = self._write(
            home / "memories" / "MEMORY.md",
            "# Stable memory\n\nFirst content.",
        )
        first = collect_codex_native_memory(home)
        first_source = first["sources"][0]
        original_ns = source.stat().st_mtime_ns
        os.utime(source, ns=(original_ns + 2_000_000_000, original_ns + 2_000_000_000))
        touched = collect_codex_native_memory(home)
        touched_source = touched["sources"][0]

        self.assertNotEqual(first_source["mtimeNs"], touched_source["mtimeNs"])
        self.assertEqual(first_source["fingerprint"], touched_source["fingerprint"])
        self.assertEqual(
            first["manifest"]["manifestHash"],
            touched["manifest"]["manifestHash"],
        )

        source.write_text("# Stable memory\n\nChanged content.", encoding="utf-8")
        changed = collect_codex_native_memory(home)
        self.assertNotEqual(
            touched_source["fingerprint"],
            changed["sources"][0]["fingerprint"],
        )
        self.assertNotEqual(
            touched["manifest"]["manifestHash"],
            changed["manifest"]["manifestHash"],
        )
        self.assertEqual(changed["sources"][0]["parserVersion"], PARSER_VERSION)

    def test_oversized_and_invalid_utf8_files_are_source_local_failures(self) -> None:
        home = self.root / "codex"
        rollout_root = home / "memories" / "rollout_summaries"
        self._write(home / "memories" / "MEMORY.md", "# Valid")
        self._write(rollout_root / "large.md", "x" * 128)
        invalid = rollout_root / "invalid.md"
        invalid.parent.mkdir(parents=True, exist_ok=True)
        invalid.write_bytes(b"\xff\xfe\x00")

        result = collect_codex_native_memory(home, max_file_bytes=32)

        self.assertEqual(len(result["documents"]), 1)
        reasons = {item["path"]: item["reason"] for item in result["diagnostics"]}
        self.assertIn("file-too-large", reasons[str(rollout_root / "large.md")])
        self.assertIn("invalid-utf8", reasons[str(invalid)])

    def test_codex_instruction_directory_is_shallow(self) -> None:
        home = self.root / "codex"
        (home / "memories").mkdir(parents=True)
        direct = self._write(
            home / "instructions" / "direct.md",
            "# Direct instruction",
        )
        nested = self._write(
            home / "instructions" / "archive" / "private.md",
            "# Nested instruction must stay outside the allowlist",
        )

        result = collect_codex_native_memory(home, include_instructions=True)

        source_paths = {item["sourcePath"] for item in result["documents"]}
        self.assertIn(str(direct), source_paths)
        self.assertNotIn(str(nested), source_paths)

    def test_file_and_discovery_limits_return_truncated_diagnostics(self) -> None:
        file_limited_home = self.root / "codex-files"
        rollout_root = file_limited_home / "memories" / "rollout_summaries"
        for index in range(3):
            self._write(rollout_root / f"{index}.md", f"# Memory {index}")

        file_limited = collect_codex_native_memory(
            file_limited_home,
            max_files=2,
        )

        self.assertEqual(file_limited["manifest"]["sourceCount"], 2)
        self.assertTrue(file_limited["truncated"])
        self.assertEqual(file_limited["usage"]["filesConsidered"], 2)
        self.assertTrue(
            any(
                item["status"] == "truncated"
                and item["reason"] == "max-files-reached:limit=2"
                for item in file_limited["diagnostics"]
            )
        )

        discovery_limited_home = self.root / "codex-discovery"
        discovery_root = (
            discovery_limited_home / "memories" / "rollout_summaries"
        )
        for index in range(3):
            self._write(discovery_root / f"{index}.txt", "not markdown")

        discovery_limited = collect_codex_native_memory(
            discovery_limited_home,
            max_discovery_entries=2,
        )

        self.assertTrue(discovery_limited["truncated"])
        self.assertEqual(discovery_limited["usage"]["discoveryEntries"], 2)
        self.assertTrue(
            any(
                item["status"] == "truncated"
                and item["reason"] == "max-discovery-entries-reached:limit=2"
                for item in discovery_limited["diagnostics"]
            )
        )

    def test_total_byte_limit_is_hard_and_reported(self) -> None:
        home = self.root / "codex"
        rollout_root = home / "memories" / "rollout_summaries"
        self._write(rollout_root / "one.md", "# one\nxx")
        self._write(rollout_root / "two.md", "# two\nyy")

        result = collect_codex_native_memory(home, max_total_bytes=8)

        self.assertEqual(result["manifest"]["sourceCount"], 1)
        self.assertEqual(result["usage"]["bytesRead"], 8)
        self.assertTrue(result["truncated"])
        self.assertTrue(
            any(
                item["status"] == "truncated"
                and item["reason"] == "max-total-bytes-reached:limit=8"
                for item in result["diagnostics"]
            )
        )

    def test_composite_collectors_share_one_file_budget(self) -> None:
        codex_home = self.root / "codex"
        claude_projects = self.root / "claude" / "projects"
        self._write(codex_home / "memories" / "MEMORY.md", "# Codex")
        self._write(
            claude_projects / "-workspace" / "memory" / "MEMORY.md",
            "# Claude",
        )

        result = collect_native_memory_sources(
            codex_home=codex_home,
            claude_projects_root=claude_projects,
            max_files=1,
        )

        self.assertEqual(result["manifest"]["sourceCount"], 1)
        self.assertEqual(result["documents"][0]["producerTool"], "codex")
        self.assertEqual(result["usage"]["filesConsidered"], 1)
        self.assertTrue(result["truncated"])
        self.assertTrue(
            any(
                item["producerTool"] == "claude-code"
                and item["status"] == "truncated"
                and item["reason"] == "max-files-reached:limit=1"
                for item in result["diagnostics"]
            )
        )

    def test_composite_result_keeps_one_source_set_and_no_cursor_input(self) -> None:
        codex_home = self.root / "codex"
        claude_projects = self.root / "claude" / "projects"
        self._write(codex_home / "memories" / "MEMORY.md", "# Codex")
        self._write(
            claude_projects / "-workspace" / "memory" / "MEMORY.md",
            "# Claude",
        )
        # A private Cursor-like database is deliberately outside both adapter
        # roots and there is no Cursor argument or discovery path.
        self._write(self.root / "cursor" / "state.vscdb", "private")

        result = collect_native_memory_sources(
            codex_home=codex_home,
            claude_projects_root=claude_projects,
        )

        self.assertEqual(result["manifest"]["sourceCount"], 2)
        self.assertEqual(
            {item["sourceSet"] for item in result["documents"]},
            {SOURCE_SET},
        )
        self.assertEqual(
            {item["producerTool"] for item in result["documents"]},
            {"codex", "claude-code"},
        )
        self.assertNotIn(
            "cursor",
            "\n".join(item["sourcePath"].lower() for item in result["documents"]),
        )


if __name__ == "__main__":
    unittest.main()
