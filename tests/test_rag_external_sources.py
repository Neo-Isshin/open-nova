import json
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentic_rag.rag_external_sources import (
    PDF_DEPENDENCY_SUGGESTION,
    UNSUPPORTED_DOC_SUGGESTION,
    collect_external_source_chunks,
    plan_external_sources,
)
from agentic_rag.rag_settings import effective_indexing_source_sets, resolve_rag_settings
from agentic_rag.rag_v2_indexer import build_v2_candidate_index
from agentic_rag.rag_v2_coverage import read_v2_coverage
from agentic_rag.rag_v2_promote import promote_v2_candidate, required_v2_promotion_confirmation
from data_foundation.paths import initialize_home
from data_foundation.settings import normalize_rag_settings_update


def _docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{value}</w:t></w:r></w:p>" for value in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)


class RagExternalSourceTests(unittest.TestCase):
    def _settings(
        self,
        root: Path,
        source_paths: list[Path],
        *,
        mode: str = "supplement",
        recursive: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        max_file_bytes: int = 10 * 1024 * 1024,
        max_total_bytes: int = 256 * 1024 * 1024,
        max_files: int = 10_000,
        symlink_policy: str = "reject",
    ):
        paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
        external = {
            "enabled": True,
            "mode": mode,
            "paths": [str(path) for path in source_paths],
            "recursive": recursive,
            "maxFileBytes": max_file_bytes,
            "maxTotalBytes": max_total_bytes,
            "maxFiles": max_files,
            "symlinkPolicy": symlink_policy,
        }
        if include is not None:
            external["include"] = include
        if exclude is not None:
            external["exclude"] = exclude
        settings = resolve_rag_settings(
            paths,
            settings={
                "rag": {
                    "enabled": True,
                    "mode": "v2",
                    "embedding": {"model": "fixture", "dimension": 2, "batchSize": 8},
                    "indexing": {
                        "enabled": True,
                        "sourceSets": ["lessons"],
                        "externalSources": external,
                    },
                }
            },
        )
        return paths, settings

    def test_settings_resolve_supplement_and_replace_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sources"
            source.mkdir()
            _paths, supplement = self._settings(root, [source], mode="supplement")
            self.assertEqual(
                effective_indexing_source_sets(supplement),
                (
                    "lessons",
                    "agent-native-memory",
                    "agent-native-instructions",
                    "external-content",
                ),
            )
            _paths, replace = self._settings(root, [source], mode="replace")
            self.assertEqual(effective_indexing_source_sets(replace), ("external-content",))
            self.assertEqual(replace.to_dict()["external_sources"]["paths"], [str(source)])

    def test_settings_reject_relative_paths_and_traversal_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            with self.assertRaisesRegex(ValueError, "absolute paths"):
                resolve_rag_settings(
                    paths,
                    settings={"rag": {"indexing": {"externalSources": {"enabled": True, "paths": ["../outside"]}}}},
                )
            with self.assertRaisesRegex(ValueError, "unsafe traversal pattern"):
                resolve_rag_settings(
                    paths,
                    settings={
                        "rag": {
                            "indexing": {
                                "externalSources": {
                                    "enabled": True,
                                    "paths": [str(root)],
                                    "include": ["../*.md"],
                                }
                            }
                        }
                    },
                )

    def test_settings_write_normalizer_rejects_invalid_external_source_contract(self):
        accepted = normalize_rag_settings_update(
            {
                "indexing": {
                    "externalSources": {
                        "enabled": True,
                        "mode": "replace",
                        "paths": ["/tmp/content", "/tmp/content"],
                        "recursive": False,
                        "include": ["*.md"],
                        "exclude": ["private/**"],
                        "maxFileBytes": "1024",
                        "maxTotalBytes": 2048,
                        "maxFiles": 5,
                        "symlinkPolicy": "within-root",
                    }
                }
            }
        )
        external = accepted["indexing"]["externalSources"]
        self.assertEqual(external["paths"], ["/tmp/content"])
        self.assertEqual(external["maxFileBytes"], 1024)
        for payload, message in (
            ({"paths": ["relative"]}, "absolute paths"),
            ({"include": ["../secret"]}, "unsafe traversal"),
            ({"maxFiles": 0}, "positive integer"),
            ({"symlinkPolicy": "follow-all"}, "reject or within-root"),
            ({"mode": "merge"}, "supplement or replace"),
            ({"recursive": "yes"}, "must be a boolean"),
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                normalize_rag_settings_update({"indexing": {"externalSources": payload}})

    def test_stdlib_parsers_cover_supported_non_pdf_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sources"
            source.mkdir()
            (source / "a.md").write_text("# Heading\n\nMarkdown body", encoding="utf-8")
            (source / "b.markdown").write_text("# Other\n\nMarkdown extension", encoding="utf-8")
            (source / "c.txt").write_text("Plain text paragraph", encoding="utf-8")
            (source / "d.log").write_text("2026-01-01 structured log entry", encoding="utf-8")
            _docx(source / "e.docx", ["DOCX paragraph", "Second paragraph"])
            (source / "f.html").write_text("<html><body><h1>Title</h1><p>HTML body</p><script>ignore me</script></body></html>", encoding="utf-8")
            (source / "g.htm").write_text("<p>HTM body</p>", encoding="utf-8")
            (source / "h.rtf").write_bytes(b"{\\rtf1\\ansi RTF body\\par second line}")
            (source / "i.csv").write_text("name,value\nalpha,1\n", encoding="utf-8")
            (source / "j.tsv").write_text("name\tvalue\nbeta\t2\n", encoding="utf-8")
            (source / "k.json").write_text(json.dumps({"title": "JSON title", "body": "JSON body"}), encoding="utf-8")
            (source / "l.jsonl").write_text(json.dumps({"event": "JSONL event"}) + "\n", encoding="utf-8")
            _paths, settings = self._settings(root, [source])

            chunks, records = collect_external_source_chunks(settings)

            self.assertEqual({record["parserStatus"] for record in records}, {"parsed"})
            self.assertEqual(len(records), 12)
            text = "\n".join(chunk["text"] for chunk in chunks)
            for marker in (
                "Markdown body",
                "Markdown extension",
                "Plain text paragraph",
                "structured log entry",
                "DOCX paragraph",
                "HTML body",
                "HTM body",
                "RTF body",
                "alpha",
                "beta",
                "JSON body",
                "JSONL event",
            ):
                self.assertIn(marker, text)
            self.assertTrue(all(record["contentHash"] for record in records))
            self.assertTrue(all(record["parserVersion"] for record in records))
            self.assertTrue(all(record["mtimeNs"] for record in records))

    def test_pdf_parser_has_explicit_missing_dependency_error_and_success_seam(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "document.pdf").write_bytes(b"%PDF-fixture")
            _paths, settings = self._settings(root, [source])
            with patch.dict(sys.modules, {"pypdf": None}):
                chunks, records = collect_external_source_chunks(settings)
            self.assertEqual(chunks, [])
            self.assertEqual(records[0]["parserStatus"], "error")
            self.assertIn("missing-pdf-parser", records[0]["parserError"])
            self.assertEqual(records[0]["suggestion"], PDF_DEPENDENCY_SUGGESTION)

            class FakePage:
                def extract_text(self):
                    return "PDF page evidence"

            class FakeReader:
                def __init__(self, stream):
                    self.pages = [FakePage()]

            with patch.dict(sys.modules, {"pypdf": types.SimpleNamespace(PdfReader=FakeReader)}):
                chunks, records = collect_external_source_chunks(settings)
            self.assertEqual(records[0]["parserStatus"], "parsed")
            self.assertEqual(chunks[0]["text"], "PDF page evidence")
            self.assertEqual(chunks[0]["provenance"]["parserVersion"], "pdf-pypdf-v1")

    def test_doc_is_explicitly_unsupported_and_blocks_candidate_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "legacy.doc").write_bytes(b"legacy binary doc")
            (source / "valid.txt").write_text("valid external evidence", encoding="utf-8")
            legacy_index = root / "Diary" / "__diary_rag" / "index.jsonl"
            legacy_index.parent.mkdir(parents=True)
            legacy_index.write_text("legacy-unchanged\n", encoding="utf-8")
            _paths, settings = self._settings(root, [source], mode="replace")

            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            records = [json.loads(line) for line in Path(result["sourcesPath"]).read_text(encoding="utf-8").splitlines()]
            unsupported = next(record for record in records if record["path"].endswith("legacy.doc"))
            self.assertEqual(unsupported["parserStatus"], "unsupported")
            self.assertEqual(unsupported["suggestion"], UNSUPPORTED_DOC_SUGGESTION)
            self.assertEqual(result["manifest"]["status"], "partial")
            self.assertEqual(result["manifest"]["blockingExternalSourceCount"], 1)
            self.assertEqual(legacy_index.read_text(encoding="utf-8"), "legacy-unchanged\n")

    def test_include_exclude_recursive_size_total_and_file_count_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            nested = source / "nested"
            nested.mkdir(parents=True)
            (source / "keep.txt").write_text("keep root", encoding="utf-8")
            (source / "skip.log").write_text("excluded", encoding="utf-8")
            (nested / "keep.txt").write_text("keep nested", encoding="utf-8")
            (nested / "large.txt").write_text("x" * 50, encoding="utf-8")
            _paths, settings = self._settings(
                root,
                [source],
                include=["*.txt", "**/*.txt"],
                exclude=["skip*"],
                max_file_bytes=30,
                max_total_bytes=35,
                max_files=10,
            )
            chunks, records = collect_external_source_chunks(settings)
            by_name = {Path(record["path"]).name: record for record in records}
            self.assertEqual(by_name["large.txt"]["parserError"], "file-too-large")
            self.assertNotIn("skip.log", by_name)
            self.assertIn("keep root", "\n".join(chunk["text"] for chunk in chunks))
            self.assertIn("keep nested", "\n".join(chunk["text"] for chunk in chunks))

            _paths, nonrecursive = self._settings(root, [source], recursive=False)
            chunks, _records = collect_external_source_chunks(nonrecursive)
            self.assertNotIn("keep nested", "\n".join(chunk["text"] for chunk in chunks))

            _paths, total_limited = self._settings(root, [source], max_file_bytes=100, max_total_bytes=12)
            _chunks, records = collect_external_source_chunks(total_limited)
            self.assertIn("total-size-limit-exceeded", {record["parserError"] for record in records})

            _paths, one_file = self._settings(root, [source], max_files=1)
            _chunks, records = collect_external_source_chunks(one_file)
            self.assertLessEqual(len([record for record in records if record["regularFile"]]), 1)

    def test_content_hash_deduplicates_identical_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "one.txt").write_text("identical external evidence", encoding="utf-8")
            (source / "two.txt").write_text("identical external evidence", encoding="utf-8")
            _paths, settings = self._settings(root, [source])
            chunks, records = collect_external_source_chunks(settings)
            self.assertEqual(len(chunks), 1)
            self.assertEqual(sorted(record["parserStatus"] for record in records), ["duplicate", "parsed"])
            duplicate = next(record for record in records if record["parserStatus"] == "duplicate")
            self.assertTrue(duplicate["duplicateOf"])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_symlink_policy_rejects_links_outside_root_and_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            outside = root / "outside.txt"
            outside.write_text("must not be indexed", encoding="utf-8")
            (source / "outside-link.txt").symlink_to(outside)
            (source / "loop").symlink_to(source, target_is_directory=True)
            _paths, rejected = self._settings(root, [source], symlink_policy="reject")
            chunks, records = collect_external_source_chunks(rejected)
            self.assertEqual(chunks, [])
            self.assertTrue(all(record["parserError"] == "symlink-rejected" for record in records))

            _paths, bounded = self._settings(root, [source], symlink_policy="within-root")
            chunks, records = collect_external_source_chunks(bounded)
            self.assertEqual(chunks, [])
            errors = {record["parserError"] for record in records}
            self.assertIn("symlink-target-outside-root", errors)
            self.assertIn("symlink-loop-or-directory-cycle", errors)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_device_like_files_are_never_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            fifo = source / "named-pipe.txt"
            os.mkfifo(fifo)
            _paths, settings = self._settings(root, [source])
            chunks, records = collect_external_source_chunks(settings)
            self.assertEqual(chunks, [])
            self.assertEqual(records[0]["parserError"], "not-a-regular-file")
            self.assertEqual(records[0]["parserStatus"], "skipped")

    def test_dry_run_plan_is_read_only_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "bad.jsonl").write_text("not-json\n", encoding="utf-8")
            _paths, settings = self._settings(root, [source])
            self.assertFalse(settings.v2_store_path.exists())
            plan = plan_external_sources(settings)
            self.assertFalse(plan["canExecute"])
            self.assertEqual(plan["summary"]["parseErrorCount"], 1)
            self.assertTrue(plan["mutationPolicy"]["planIsReadOnly"])
            self.assertFalse(plan["wouldMutateOnIndex"]["legacyIndex"])
            self.assertFalse(settings.v2_store_path.exists())

    def test_discovery_to_open_identity_change_is_source_local_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "stable.txt").write_text("stable source", encoding="utf-8")
            _paths, settings = self._settings(root, [source])
            real_fstat = os.fstat

            def changed_identity(descriptor):
                value = real_fstat(descriptor)
                values = list(value)
                values[1] += 1
                return os.stat_result(values)

            with patch("agentic_rag.rag_external_sources.os.fstat", side_effect=changed_identity):
                chunks, records = collect_external_source_chunks(settings)
            self.assertEqual(chunks, [])
            self.assertEqual(records[0]["parserStatus"], "error")
            self.assertIn("file-identity-changed", records[0]["parserError"])

    def test_incremental_candidate_reuses_unchanged_external_chunks_and_embeddings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            document = source / "evidence.txt"
            document.write_text("stable external evidence", encoding="utf-8")
            _paths, settings = self._settings(root, [source], mode="replace")
            first = build_v2_candidate_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            run_id = first["run"]["runId"]
            promote_v2_candidate(
                settings,
                run_id=run_id,
                confirm=True,
                confirmation_text=required_v2_promotion_confirmation(run_id),
                requested_by="test",
            )

            def no_embeddings(texts):
                raise AssertionError(f"unchanged external chunks should reuse active embeddings: {texts!r}")

            second = build_v2_candidate_index(settings, requested_by="test", embedding_fn=no_embeddings)
            records = [json.loads(line) for line in Path(second["sourcesPath"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["parserStatus"], "unchanged")
            self.assertTrue(records[0]["incrementalReuse"])
            self.assertEqual(second["manifest"]["reusedEmbeddingCount"], 1)
            self.assertEqual(second["manifest"]["generatedEmbeddingCount"], 0)
            self.assertEqual(second["manifest"]["sourceSets"], ["external-content"])

            coverage = read_v2_coverage(settings)
            external = next(item for item in coverage["sourceSets"] if item["sourceSet"] == "external-content")
            self.assertEqual(external["expected"]["kind"], "external-local")
            self.assertEqual(external["coverageStatus"], "covered")


if __name__ == "__main__":
    unittest.main()
