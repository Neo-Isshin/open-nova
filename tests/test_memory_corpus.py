import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.memory_corpus import (
    CorpusCollector,
    canonical_lessons_path,
    collect_memory_corpus,
    collect_runtime_memory_corpus,
    merge_lessons_into_canonical,
)
from data_foundation.local_memory_search import (
    search_local_memory,
    sync_local_memory_index,
)
from data_foundation.paths import initialize_home


class MemoryCorpusTests(unittest.TestCase):
    def test_generic_orchestrator_preserves_order_normalizes_and_dedupes(self):
        def first(_settings):
            return (
                [
                    {"id": "shared", "text": "first", "sourceSet": "alpha"},
                    {"id": "alpha-only", "text": "second", "sourceSet": "alpha"},
                ],
                [{"sourceSet": "alpha"}],
            )

        def second(_settings):
            return (
                [
                    {"id": "shared", "text": "duplicate", "sourceSet": "beta"},
                    {"id": "beta-only", "text": "third", "sourceSet": "beta"},
                ],
                [{"sourceSet": "beta", "chunkCount": 2}],
            )

        chunks, sources = collect_memory_corpus(
            object(),
            ["alpha", "beta"],
            [
                CorpusCollector(("alpha",), first),
                CorpusCollector(("beta",), second),
            ],
        )

        self.assertEqual([item["id"] for item in chunks], ["shared", "alpha-only", "beta-only"])
        self.assertEqual(chunks[0]["text"], "first")
        self.assertEqual(chunks[0]["privacyClass"], "local-private")
        self.assertEqual(len(chunks[0]["textHash"]), 64)
        self.assertEqual([item["sourceSet"] for item in sources], ["alpha", "beta"])
        self.assertEqual(sources[0]["chunkCount"], 0)

    def test_merge_lessons_migrates_legacy_and_writes_only_canonical(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            legacy = paths.diary_dir / "lessons.jsonl"
            legacy.write_text(
                json.dumps({"id": "old", "text": "legacy lesson"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            result = merge_lessons_into_canonical(
                paths,
                [
                    {"id": "old", "text": "duplicate legacy lesson"},
                    {"id": "new", "text": "new lesson"},
                ],
            )
            canonical = canonical_lessons_path(paths)
            records = [json.loads(line) for line in canonical.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(result["migrated"], 1)
            self.assertEqual(result["added"], 1)
            self.assertEqual([item["id"] for item in records], ["old", "new"])
            self.assertEqual(
                legacy.read_text(encoding="utf-8"),
                json.dumps({"id": "old", "text": "legacy lesson"}, ensure_ascii=False) + "\n",
            )

            repeated = merge_lessons_into_canonical(paths, [{"id": "new", "text": "new lesson"}])
            self.assertTrue(repeated["unchanged"])

    def test_runtime_corpus_reads_canonical_and_legacy_when_rag_is_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = initialize_home(Path(temporary) / "Actanara")
            (paths.config_dir / "settings.json").write_text(
                json.dumps({"features": {"rag": False}, "rag": {"enabled": False, "mode": "disabled"}}),
                encoding="utf-8",
            )
            canonical = canonical_lessons_path(paths)
            canonical.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "canonical", "text": "canonical memory"}),
                        json.dumps({"id": "shared", "text": "canonical wins"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            legacy = paths.diary_dir / "lessons.jsonl"
            legacy.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "shared", "text": "legacy duplicate"}),
                        json.dumps({"id": "legacy", "text": "legacy compatibility memory"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            chunks, sources = collect_runtime_memory_corpus(paths, ["lessons"])

            self.assertEqual([item["id"] for item in chunks], ["canonical", "shared", "legacy"])
            self.assertEqual(next(item for item in chunks if item["id"] == "shared")["text"], "canonical wins")
            self.assertEqual([Path(item["path"]) for item in sources], [canonical, legacy])

    def test_distinct_legacy_diary_lessons_are_searchable_before_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_root = root / "legacy-diary"
            legacy_root.mkdir(parents=True)
            paths = initialize_home(
                root / "Actanara",
                legacy_diary_root=legacy_root,
            )
            legacy = legacy_root / "lessons.jsonl"
            legacy.write_text(
                json.dumps(
                    {
                        "id": "legacy-distinct",
                        "text": "The pre-migration lesson is silverharbor.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            chunks, sources = collect_runtime_memory_corpus(paths, ["lessons"])
            self.assertEqual([item["id"] for item in chunks], ["legacy-distinct"])
            self.assertEqual([Path(item["path"]) for item in sources], [legacy])

            sync_local_memory_index(paths)
            response = search_local_memory(
                "silverharbor",
                paths=paths,
                ensure_fresh=False,
            )
            self.assertEqual(response["results"][0]["id"], "legacy-distinct")


if __name__ == "__main__":
    unittest.main()
