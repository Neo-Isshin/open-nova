import json
import io
import os
import sys
import tempfile
import unittest
import importlib
import types
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "agentic_rag"))

from agentic_rag.rag_settings import DEFAULT_INDEXING_SOURCE_SETS, resolve_rag_settings
from agentic_rag import rag_settings
from agentic_rag.rag_status import read_rag_status
from agentic_rag import rag_server_lifecycle
from agentic_rag.rag_active_source import resolve_active_rag_index
from agentic_rag.rag_v2_store import initialize_shadow_build, promote_candidate, rag_v2_operation_lock
from agentic_rag.rag_v2_indexer import build_v2_candidate_index
from agentic_rag.rag_v2_coverage import read_v2_coverage
from agentic_rag.rag_v2_eval import _ndcg_at_k, eval_benchmark_paths, run_rag_eval
from agentic_rag.rag_v2_promote import promote_v2_candidate, required_v2_promotion_confirmation
from agentic_rag.rag_v2_retention import prune_v2_index_store
from agentic_rag.rag_v2_sync import main as rag_v2_sync_main, plan_v2_production_sync, sync_v2_production_index
from agentic_rag.rag_v2_rollback import rollback_v2_manifest, required_v2_manifest_rollback_confirmation
from agentic_rag.rag_retriever import build_query_plan, build_retrieval_passes, fuse_ranked_passes, infer_tags, infer_work_type, rank_chunks
from agentic_rag.rag_reranker import apply_reranker
from agentic_rag.rag_memory_governance import governance_for_chunk
from agentic_rag.query_embedding_provider import CloudQueryEmbeddingProvider, LocalQueryEmbeddingProvider
from data_foundation.diary_markdown import materialize_diary_markdown_day, read_diary_markdown_document
from data_foundation.diary_paths import diary_day_dir
from data_foundation.paths import initialize_home, update_runtime_manifest_paths
from data_foundation.settings import read_settings, write_settings
from data_foundation.secret_store import store_secret
from data_foundation.db import connect, migrate
from data_foundation.jobs import begin_ingestion_run


def _reload_rag_modules(*names: str):
    for name in names:
        sys.modules.pop(name, None)
    return [importlib.import_module(name) for name in names]


class RagSubsystemStatusTests(unittest.TestCase):
    def tearDown(self):
        for name in ("rag_config", "rag_active_source", "embedding_server"):
            sys.modules.pop(name, None)

    def test_rag_timestamp_business_date_uses_configured_timezone(self):
        from agentic_rag import rag_v2_indexer

        with patch.dict(os.environ, {"TARGET_TIMEZONE": "UTC"}, clear=False):
            self.assertEqual(rag_v2_indexer._hkt_date("2026-05-22T03:30:00Z"), "2026-05-21")

    def test_default_rag_settings_are_chinese_first_v2_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            settings = resolve_rag_settings(paths)
            self.assertTrue(settings.enabled)
            self.assertEqual(settings.mode, "v2")
            self.assertEqual(settings.language_profile, "zh")
            self.assertEqual(settings.embedding_model, "intfloat/multilingual-e5-small")
            self.assertEqual(settings.embedding_dimension, 384)
            self.assertEqual(settings.legacy_index_path, root / "Diary" / "__diary_rag" / "index.jsonl")
            self.assertEqual(settings.diary_source_root, paths.diary_dir)
            self.assertEqual(settings.v2_store_path, paths.home / "reserved" / "rag" / "v2")
            self.assertEqual(settings.indexing_source_sets, DEFAULT_INDEXING_SOURCE_SETS)
            self.assertNotIn("legacy-diary-daily", settings.indexing_source_sets)
            self.assertIn("filtered-dialogue-daily", settings.indexing_source_sets)
            self.assertFalse(settings.reranker_enabled)
            self.assertEqual(settings.retrieval_latency_budget_seconds, 60.0)
            self.assertEqual(settings.retrieval_max_concurrent_searches, 2)
            self.assertEqual(settings.reranker_provider, "none")
            self.assertIsNone(settings.reranker_model)
            self.assertTrue(settings.server_enabled)
            self.assertFalse(settings.v2_store_path.exists())

    def test_rag_latency_budget_is_configurable_with_a_safe_upper_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            configured = resolve_rag_settings(
                paths,
                settings={"rag": {"retrieval": {"latencyBudgetSeconds": 90, "maxConcurrentSearches": 3}}},
            )
            self.assertEqual(configured.retrieval_latency_budget_seconds, 90.0)
            self.assertEqual(configured.retrieval_max_concurrent_searches, 3)
            with self.assertRaisesRegex(ValueError, "at most 120 seconds"):
                resolve_rag_settings(
                    paths,
                    settings={"rag": {"retrieval": {"latencyBudgetSeconds": 121}}},
                )

    def test_ndcg_penalizes_missing_gold_evidence(self):
        self.assertEqual(_ndcg_at_k([True], 10, total_relevant=2), 0.6131)

    def test_rag_server_enabled_respects_server_and_feature_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")

            write_settings({"rag": {"enabled": True, "mode": "v2", "server": {"enabled": False}}}, paths)
            settings = resolve_rag_settings(paths)
            self.assertTrue(settings.enabled)
            self.assertFalse(settings.server_enabled)

            write_settings({"features": {"embeddingServer": False}, "rag": {"enabled": True, "mode": "v2"}}, paths)
            settings = resolve_rag_settings(paths)
            self.assertTrue(settings.enabled)
            self.assertFalse(settings.server_enabled)

            write_settings({"features": {"embeddingServer": False}, "rag": {"enabled": True, "mode": "v2", "server": {"enabled": True}}}, paths)
            settings = resolve_rag_settings(paths)
            self.assertTrue(settings.server_enabled)

    def test_rag_v2_source_root_follows_settings_center_not_legacy_index_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary = root / "Diary"
            paths = initialize_home(root / "Actanara", legacy_diary_root=diary)
            write_settings(
                {
                    "rag": {
                        "legacy": {
                            "indexPath": str(root / "ArtifactsOnly" / "__diary_rag" / "index.jsonl"),
                        }
                    }
                },
                paths,
            )

            settings = resolve_rag_settings(paths)
            coverage = read_v2_coverage(settings)

            self.assertEqual(settings.legacy_index_path, root / "ArtifactsOnly" / "__diary_rag" / "index.jsonl")
            self.assertEqual(settings.diary_source_root, paths.diary_dir)
            self.assertEqual(coverage["paths"]["diaryRoot"], str(paths.diary_dir))
            self.assertIn(str(paths.diary_dir / "__diary_daily"), coverage["paths"]["filteredDialoguePattern"])

    def test_rag_v2_sources_follow_runtime_paths_over_legacy_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "LegacyDiary"
            current = root / "GeneratedDiary"
            custom_db = root / "runtime" / "foundation.sqlite3"
            custom_board = root / "runtime" / "tasks" / "TASK_BOARD.md"
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            paths = update_runtime_manifest_paths(
                paths.home,
                generated_diary_root=current,
                legacy_diary_root=legacy,
                database_path=custom_db,
                task_board_path=custom_board,
            )
            stale_filtered = legacy / "__diary_daily" / "2026-06-05" / "_filtered" / "codex"
            fresh_filtered = current / "__diary_daily" / "2026-06-05" / "_filtered" / "codex"
            stale_filtered.mkdir(parents=True)
            fresh_filtered.mkdir(parents=True)
            (stale_filtered / "old.jsonl").write_text(
                json.dumps({"role": "user", "content": "stale legacy dialogue should not be indexed"}) + "\n",
                encoding="utf-8",
            )
            (fresh_filtered / "new.jsonl").write_text(
                json.dumps({"role": "user", "content": "fresh generated diary dialogue should be indexed"}) + "\n",
                encoding="utf-8",
            )
            lessons = paths.home / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True, exist_ok=True)
            lessons.write_text(
                json.dumps({"id": "runtime-lesson", "text": "runtime lessons path should be indexed", "date": "2026-06-05", "agent": "codex"}) + "\n",
                encoding="utf-8",
            )
            custom_board.parent.mkdir(parents=True, exist_ok=True)
            custom_board.write_text(
                "# TASK BOARD\n\n## Active\n### Actanara\n- [ ] runtime task board path should be indexed ← **@codex**\n",
                encoding="utf-8",
            )
            migrate(paths)
            run_id = begin_ingestion_run(paths, trigger_type="test-rag-runtime-paths", business_date=datetime(2026, 6, 5).date())
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO tool_sources(tool_key, display_name, adapter_version, capabilities_json, enabled, created_at, updated_at)
                    VALUES ('codex', 'Codex', 'test', '{}', 1, '2026-06-05T00:00:00+08:00', '2026-06-05T00:00:00+08:00')
                    """,
                )
                connection.execute(
                    """
                    INSERT INTO daily_tool_usage(business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id)
                    VALUES ('2026-06-05', 'codex', 123, 4, 1, 2, ?)
                    """,
                    (run_id,),
                )
            write_settings({"rag": {"embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)

            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["filtered-dialogue-daily", "lessons", "task-board-snapshot", "foundation-usage-rollups"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            chunks = [json.loads(line) for line in Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()]
            chunk_text = "\n".join(chunk["text"] for chunk in chunks)
            source_paths = {chunk["sourcePath"] for chunk in chunks}
            coverage = read_v2_coverage(settings)

            self.assertEqual(settings.diary_source_root, current)
            self.assertEqual(settings.foundation_db_path, custom_db)
            self.assertEqual(settings.task_board_path, custom_board)
            self.assertEqual(settings.lessons_path, lessons)
            self.assertIn("fresh generated diary dialogue should be indexed", chunk_text)
            self.assertNotIn("stale legacy dialogue should not be indexed", chunk_text)
            self.assertIn(str(fresh_filtered / "new.jsonl"), source_paths)
            self.assertIn(str(lessons), source_paths)
            self.assertIn(str(custom_board), source_paths)
            self.assertIn(str(custom_db), source_paths)
            self.assertEqual(result["manifest"]["sourceProfile"]["diarySourceRoot"], str(current))
            self.assertEqual(result["manifest"]["sourceProfile"]["foundationDbPath"], str(custom_db))
            self.assertEqual(result["manifest"]["sourceProfile"]["taskBoardPath"], str(custom_board))
            self.assertEqual(result["manifest"]["sourceProfile"]["lessonsPath"], str(lessons))
            self.assertEqual(coverage["paths"]["diaryRoot"], str(current))
            self.assertEqual(coverage["paths"]["foundationDbPath"], str(custom_db))
            self.assertEqual(coverage["paths"]["taskBoardPath"], str(custom_board))
            self.assertEqual(coverage["paths"]["lessonsPath"], str(lessons))

    def test_query_embedding_provider_is_lazy_and_shapes_vectors(self):
        calls = []

        class FakeModel:
            def __init__(self):
                self.device = None

            def to(self, device):
                self.device = device

            def encode(self, texts, show_progress_bar=False):
                calls.append({"texts": list(texts), "showProgress": show_progress_bar, "device": self.device})
                return [[1, 0], [0, 1]][: len(texts)]

        provider = LocalQueryEmbeddingProvider("fake-model", device="cpu", model_factory=lambda name: FakeModel())

        self.assertFalse(provider.ready)
        self.assertEqual(provider.encode_query("boot"), [1.0, 0.0])
        self.assertTrue(provider.ready)
        self.assertEqual(provider.encode(["first", "second"]), [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(provider.encode_query("query"), [1.0, 0.0])
        self.assertEqual(calls[0]["texts"], ["boot"])
        self.assertEqual(calls[0]["device"], "cpu")
        self.assertFalse(calls[0]["showProgress"])

    def test_cloud_query_embedding_provider_has_no_local_fallback(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"vectors": [[0.25, 0.75]]}).encode("utf-8")

        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.headers.get("Authorization")
            return FakeResponse()

        provider = CloudQueryEmbeddingProvider("cloud-model", endpoint="https://embed.example.invalid/v1", api_key="secret")
        with patch("agentic_rag.query_embedding_provider.urllib.request.urlopen", side_effect=fake_urlopen) as urlopen:
            self.assertEqual(provider.encode_query("hello"), [0.25, 0.75])

        urlopen.assert_called_once()
        self.assertEqual(captured["url"], "https://embed.example.invalid/v1")
        self.assertEqual(captured["timeout"], 60)
        self.assertEqual(captured["payload"]["model"], "cloud-model")
        self.assertEqual(captured["payload"]["texts"], ["hello"])
        self.assertEqual(captured["authorization"], "Bearer secret")

    def test_cloud_query_embedding_provider_requires_cloud_configuration(self):
        provider = CloudQueryEmbeddingProvider("cloud-model", endpoint="", api_key="")
        with self.assertRaises(RuntimeError):
            provider.encode_query("hello")

    def test_english_profile_uses_english_default_dimension_when_model_not_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"languageProfile": "en", "embedding": {"model": None, "dimension": None}}}, paths)
            settings = resolve_rag_settings(paths)
            self.assertEqual(settings.language_profile, "en")
            self.assertEqual(settings.embedding_model, "all-MiniLM-L6-v2")
            self.assertEqual(settings.embedding_dimension, 384)

    def test_rag_embedding_model_catalog_has_two_zh_and_two_en_dimensions(self):
        by_language = {"zh": [], "en": []}
        for option in rag_settings.EMBEDDING_MODEL_OPTIONS:
            by_language[option["language"]].append(option)

        self.assertEqual({item["dimension"] for item in by_language["zh"]}, {384, 1024})
        self.assertEqual({item["dimension"] for item in by_language["en"]}, {384, 1024})
        self.assertEqual(rag_settings.MODEL_DIMENSIONS["intfloat/multilingual-e5-small"], 384)
        self.assertEqual(rag_settings.MODEL_DIMENSIONS["BAAI/bge-large-en-v1.5"], 1024)

    def test_rag_known_model_dimension_defaults_from_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"languageProfile": "en", "embedding": {"model": "BAAI/bge-large-en-v1.5", "dimension": None}}}, paths)

            settings = resolve_rag_settings(paths)

            self.assertEqual(settings.embedding_model, "BAAI/bge-large-en-v1.5")
            self.assertEqual(settings.embedding_dimension, 1024)

    def test_cloud_provider_id_is_separate_from_local_cloud_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "embedding": {
                            "mode": "cloud",
                            "provider": "cloud",
                            "providerId": "example-cloud",
                            "model": "example-embedding",
                            "dimension": 1024,
                            "endpoint": "https://embed.example.invalid/v1",
                        }
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)
            self.assertEqual(settings.embedding_provider, "cloud")
            self.assertEqual(settings.embedding_provider_id, "example-cloud")

    def test_rag_status_exposes_unified_local_cloud_provider_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "embedding": {
                            "mode": "cloud",
                            "providerId": "example-cloud",
                            "model": "example-embedding",
                            "dimension": 768,
                            "endpoint": "https://embed.example.invalid/v1",
                            "apiKeyEnv": "EXAMPLE_RAG_KEY",
                            "secretRef": {
                                "backend": "process-env",
                                "service": "actanara",
                                "account": "EXAMPLE_RAG_KEY",
                            },
                            "batchSize": 8,
                        }
                    }
                },
                paths,
            )
            with patch.dict(os.environ, {"EXAMPLE_RAG_KEY": "configured"}, clear=False):
                status = read_rag_status(settings=resolve_rag_settings(paths), count_legacy_entries=False)

        provider = status["provider"]
        serving = status["serving"]
        self.assertEqual(provider["schemaVersion"], 1)
        self.assertEqual(provider["mode"], "cloud")
        self.assertEqual(provider["providerId"], "example-cloud")
        self.assertEqual(provider["model"], "example-embedding")
        self.assertEqual(provider["dimension"], 768)
        self.assertEqual(provider["active"]["mode"], "cloud")
        self.assertEqual(provider["active"]["providerId"], "example-cloud")
        self.assertEqual(provider["active"]["model"], "example-embedding")
        self.assertEqual(provider["active"]["dimension"], 768)
        self.assertTrue(provider["active"]["endpointConfigured"])
        self.assertFalse(provider["active"]["requiresServer"])
        self.assertTrue(provider["active"]["requiresApiKeyEnv"])
        self.assertTrue(provider["active"]["apiKeyConfigured"])
        self.assertTrue(provider["cloud"]["enabled"])
        self.assertEqual(provider["cloud"]["providerId"], "example-cloud")
        self.assertEqual(provider["cloud"]["model"], "example-embedding")
        self.assertEqual(provider["cloud"]["dimension"], 768)
        self.assertTrue(provider["cloud"]["endpointConfigured"])
        self.assertFalse(provider["cloud"]["requiresServer"])
        self.assertTrue(provider["cloud"]["requiresApiKeyEnv"])
        self.assertTrue(provider["cloud"]["apiKeyConfigured"])
        self.assertEqual(provider["cloud"]["apiKeyEnv"], "EXAMPLE_RAG_KEY")
        self.assertTrue(provider["cloud"]["hasSecretRef"])
        self.assertEqual(provider["cloud"]["secretRef"]["account"], "EXAMPLE_RAG_KEY")
        self.assertFalse(provider["cloud"]["storesSecretValue"])
        self.assertFalse(provider["local"]["enabled"])
        self.assertEqual(provider["local"]["providerId"], "local")
        self.assertEqual(provider["local"]["model"], "example-embedding")
        self.assertEqual(provider["local"]["dimension"], 768)
        self.assertTrue(provider["local"]["endpointConfigured"])
        self.assertTrue(provider["local"]["requiresServer"])
        self.assertFalse(provider["local"]["requiresApiKeyEnv"])
        self.assertEqual(serving["role"], "rag-search-server")
        self.assertTrue(serving["requiresSearchServer"])
        self.assertEqual(serving["queryEmbeddingProvider"], "cloud")
        self.assertFalse(serving["requiresLocalEmbeddingRuntime"])
        self.assertEqual(serving["localEmbeddingRuntimePolicy"], "not-required")
        self.assertTrue(status["queryEmbedding"]["configured"])
        self.assertEqual(status["queryEmbedding"]["provider"], "cloud")

    def test_rag_status_cloud_api_key_configured_requires_readable_secret_or_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "embedding": {
                            "mode": "cloud",
                            "providerId": "example-cloud",
                            "endpoint": "https://embed.example.invalid/v1",
                            "apiKeyEnv": "MISSING_RAG_KEY",
                            "secretRef": {
                                "backend": "process-env",
                                "service": "actanara",
                                "account": "MISSING_RAG_KEY",
                            },
                        }
                    }
                },
                paths,
            )
            with patch.dict(os.environ, {"MISSING_RAG_KEY": ""}, clear=False):
                missing = read_rag_status(settings=resolve_rag_settings(paths), count_legacy_entries=False)
            with patch.dict(os.environ, {"MISSING_RAG_KEY": "configured"}, clear=False):
                configured = read_rag_status(settings=resolve_rag_settings(paths), count_legacy_entries=False)

        self.assertFalse(missing["provider"]["cloud"]["apiKeyConfigured"])
        self.assertEqual(missing["queryEmbedding"]["failureMode"], "cloud-embedding-not-configured")
        self.assertTrue(configured["provider"]["cloud"]["apiKeyConfigured"])
        self.assertIsNone(configured["queryEmbedding"]["failureMode"])

    def test_rag_status_cloud_api_key_configured_from_memory_secret_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            secret_ref = {"backend": "memory", "service": "actanara", "account": f"rag-{paths.home.name}"}
            store_secret(secret_ref, "configured")
            write_settings(
                {
                    "rag": {
                        "embedding": {
                            "mode": "cloud",
                            "providerId": "example-cloud",
                            "endpoint": "https://embed.example.invalid/v1",
                            "apiKeyEnv": "MISSING_RAG_KEY",
                            "secretRef": secret_ref,
                        }
                    }
                },
                paths,
            )
            with patch.dict(os.environ, {"MISSING_RAG_KEY": ""}, clear=False):
                status = read_rag_status(settings=resolve_rag_settings(paths), count_legacy_entries=False)

        self.assertTrue(status["provider"]["cloud"]["apiKeyConfigured"])
        self.assertTrue(status["queryEmbedding"]["apiKeyConfigured"])

    def test_rag_status_reads_runtime_file_from_explicit_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_value = "target-runtime-secret"
            root = Path(tmp)
            active_paths = initialize_home(root / "Active", legacy_diary_root=root / "ActiveDiary")
            target_paths = initialize_home(root / "Target", legacy_diary_root=root / "TargetDiary")
            with patch.dict(
                os.environ,
                {
                    "ACTANARA_HOME": str(active_paths.home),
                    "ACTANARA_SECRET_BACKEND": "runtime-file",
                },
                clear=False,
            ):
                write_settings(
                    {
                        "rag": {
                            "embedding": {
                                "mode": "cloud",
                                "provider": "cloud",
                                "providerId": "example-cloud",
                                "endpoint": "https://embed.example.invalid/v1",
                                "apiKey": secret_value,
                            }
                        }
                    },
                    target_paths,
                )
                resolved = resolve_rag_settings(target_paths)
                status = read_rag_status(settings=resolved, count_legacy_entries=False)

            self.assertEqual(resolved.runtime_home, target_paths.home)
            self.assertTrue(status["provider"]["cloud"]["apiKeyConfigured"])
            self.assertTrue(status["queryEmbedding"]["apiKeyConfigured"])
            self.assertFalse((active_paths.state_dir / "secrets").exists())

    def test_unreadable_legacy_rag_keychain_ref_is_attempted_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"ACTANARA_SECRET_BACKEND": "runtime-file"},
            clear=False,
        ), patch("data_foundation.settings.platform.system", return_value="Linux"):
            root = Path(tmp)
            paths = initialize_home(root / "Runtime", legacy_diary_root=root / "Diary")
            read_settings(paths, redact_secrets=False)
            settings_path = paths.config_dir / "settings.json"
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            legacy_ref = {
                "backend": "macos-keychain",
                "service": "actanara",
                "account": f"{paths.home}:rag-embedding-api-key-example-cloud",
            }
            raw["rag"]["embedding"].update(
                {
                    "mode": "cloud",
                    "provider": "cloud",
                    "providerId": "example-cloud",
                    "endpoint": "https://embed.example.invalid/v1",
                    "secretRef": legacy_ref,
                }
            )
            settings_path.write_text(json.dumps(raw), encoding="utf-8")

            with patch("data_foundation.settings.read_secret", return_value="") as migration_read:
                read_settings(paths, redact_secrets=False)
                read_settings(paths, redact_secrets=False)
                resolved_before_attempt = resolve_rag_settings(paths)
                self.assertEqual(migration_read.call_count, 0)
                write_settings({}, paths)
                write_settings({}, paths)
            resolved = resolve_rag_settings(paths)
            with patch("agentic_rag.rag_status.read_secret") as status_read:
                status = read_rag_status(settings=resolved, count_legacy_entries=False)

            self.assertEqual(migration_read.call_count, 1)
            status_read.assert_not_called()
            self.assertTrue(resolved_before_attempt.embedding_secret_migration_required)
            self.assertTrue(resolved.embedding_secret_migration_required)
            self.assertTrue(status["provider"]["cloud"]["secretMigrationRequired"])
            self.assertFalse(status["provider"]["cloud"]["apiKeyConfigured"])

    def test_legacy_cloud_provider_value_is_treated_as_provider_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            settings = resolve_rag_settings(
                paths,
                settings={
                    "rag": {
                        "embedding": {
                            "provider": "example-cloud",
                            "model": "example-embedding",
                            "dimension": 1024,
                        }
                    }
                },
            )
            self.assertEqual(settings.embedding_provider, "cloud")
            self.assertEqual(settings.embedding_provider_id, "example-cloud")

    def test_settings_drive_rag_runtime_even_when_env_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "enabled": False,
                        "mode": "disabled",
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 384},
                        "server": {"port": 18787},
                    }
                },
                paths,
            )
            before = read_settings(paths, redact_secrets=False)
            with patch.dict(
                os.environ,
                {
                    "NOVA_RAG_MODE": "v2",
                    "NOVA_RAG_ENABLED": "true",
                    "NOVA_RAG_SERVER_PORT": "3037",
                    "NOVA_RAG_EMBEDDING_MODEL": "BAAI/bge-large-zh-v1.5",
                    "NOVA_RAG_EMBEDDING_DIMENSION": "1024",
                },
            ):
                settings = resolve_rag_settings(paths)
            after = read_settings(paths, redact_secrets=False)
            self.assertFalse(settings.enabled)
            self.assertEqual(settings.mode, "disabled")
            self.assertFalse(settings.server_enabled)
            self.assertEqual(settings.server_port, 18787)
            self.assertEqual(settings.embedding_dimension, 384)
            self.assertEqual(before["rag"], after["rag"])

    def test_legacy_rag_mode_env_does_not_control_rag_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            with patch.dict(os.environ, {"RAG_MODE": "local"}):
                settings = resolve_rag_settings(paths)
            self.assertEqual(settings.mode, "v2")

    def test_read_only_status_reports_legacy_index_without_creating_v2_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(
                json.dumps({"id": "a", "embedding": [0.1, 0.2]}) + "\n"
                + json.dumps({"id": "b", "embedding": [0.3, 0.4]}) + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"mode": "legacy"}}, paths)
            settings = resolve_rag_settings(paths)
            status = read_rag_status(settings=settings, inspect_legacy_sample=True, include_legacy_metadata=True)
            self.assertEqual(status["activeSource"], "retired")
            self.assertFalse(status["ready"])
            self.assertFalse(status["searchAvailable"])
            self.assertEqual(status["legacy"]["entries"], 2)
            self.assertEqual(status["legacy"]["embeddingDimension"], 2)
            self.assertTrue(status["legacy"]["dimensionMismatch"])
            self.assertIn("lifecycle", status)
            self.assertEqual(status["lifecycle"]["statePath"], str(paths.state_dir / "rag" / "server-state.json"))
            self.assertFalse(settings.v2_store_path.exists())

    def test_default_rag_status_does_not_read_retired_legacy_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps({"id": "legacy", "embedding": [0.1, 0.2]}) + "\n", encoding="utf-8")
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"mode": "legacy"}}, paths)

            status = read_rag_status(settings=resolve_rag_settings(paths))

            self.assertEqual(status["activeSource"], "retired")
            self.assertFalse(status["legacy"]["metadataRead"])
            self.assertIsNone(status["legacy"]["exists"])
            self.assertIsNone(status["legacy"]["entries"])
            self.assertIsNone(status["legacy"]["sizeMB"])
            self.assertEqual(status["legacy"]["reason"], "legacy-rag-retired")

    def test_rag_server_lifecycle_start_records_runtime_state_without_starting_in_tests(self):
        class FakeProcess:
            pid = 4321

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {"rag": {"enabled": True, "mode": "legacy", "server": {"enabled": True}}},
                paths,
            )
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(rag_server_lifecycle, "_is_linux", return_value=False),
                patch.object(rag_server_lifecycle, "_select_server_python", return_value="/usr/bin/python3"),
                patch.object(
                    rag_server_lifecycle,
                    "_probe_health",
                    return_value={"url": "http://127.0.0.1:3037/health", "healthy": False, "statusCode": None, "error": "URLError"},
                ),
                patch.object(rag_server_lifecycle.subprocess, "Popen", return_value=FakeProcess()) as popen,
            ):
                settings = resolve_rag_settings(paths)
                result = rag_server_lifecycle.start_rag_server(settings, requested_by="test", wait_timeout_seconds=0)

            state_path = paths.state_dir / "rag" / "server-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(result["accepted"])
            self.assertEqual(result["status"], "starting")
            self.assertEqual(state["pid"], 4321)
            self.assertEqual(state["requestedBy"], "test")
            self.assertEqual(state["command"][1], str(rag_server_lifecycle.SERVER_SCRIPT))
            self.assertEqual(str(paths.state_dir / "logs" / "rag-server.log"), result["lifecycle"]["logPath"])
            token_path = paths.state_dir / "rag" / "internal-token"
            credential_value = token_path.read_text(encoding="utf-8").strip()
            self.assertGreaterEqual(len(credential_value), 32)
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(popen.call_args.kwargs["env"]["NOVA_RAG_INTERNAL_TOKEN_FILE"], str(token_path))
            self.assertEqual(popen.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertNotIn(credential_value, json.dumps(state))
            self.assertNotIn(credential_value, " ".join(state["command"]))
            popen.assert_called_once()

    def test_nonloopback_legacy_setting_is_read_compatible_but_start_and_probe_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            settings = resolve_rag_settings(
                paths,
                settings={
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "server": {"enabled": True, "host": "0.0.0.0", "port": 3037},
                    }
                },
            )
            with (
                patch.object(rag_server_lifecycle.subprocess, "Popen") as popen,
                patch.object(rag_server_lifecycle.urllib.request, "urlopen") as urlopen,
            ):
                result = rag_server_lifecycle.start_rag_server(settings, requested_by="test")
                health = rag_server_lifecycle._probe_health(settings, timeout_seconds=0.1)

            self.assertEqual(settings.server_host, "0.0.0.0")
            self.assertFalse(result["accepted"])
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["issueCode"], "rag-server-non-loopback")
            self.assertEqual(health["error"], "rag-server-non-loopback")
            popen.assert_not_called()
            urlopen.assert_not_called()

    def test_nonloopback_status_blocks_search_without_network_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            settings = resolve_rag_settings(
                paths,
                settings={
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "server": {"enabled": True, "host": "192.0.2.10", "port": 3037},
                    }
                },
            )
            with patch("agentic_rag.rag_status.urllib.request.urlopen") as urlopen:
                status = read_rag_status(settings=settings, probe_server=True)

            self.assertFalse(status["searchAvailable"])
            self.assertEqual(status["networkBoundary"]["status"], "blocked")
            self.assertEqual(status["networkBoundary"]["issueCode"], "rag-server-non-loopback")
            self.assertEqual(status["server"]["error"], "rag-server-non-loopback")
            urlopen.assert_not_called()

    def test_rag_server_lifecycle_treats_healthy_endpoint_as_running_when_pid_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {"rag": {"enabled": True, "mode": "v2", "server": {"enabled": True}}},
                paths,
            )
            state_path = paths.state_dir / "rag" / "server-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"pid": 999999, "startedAt": "2026-06-29T00:00:00+08:00"}), encoding="utf-8")
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(rag_server_lifecycle, "_is_linux", return_value=False),
                patch.object(rag_server_lifecycle, "_pid_running", return_value=False),
                patch.object(
                    rag_server_lifecycle,
                    "_probe_health",
                    return_value={"url": "http://127.0.0.1:3037/health", "healthy": True, "statusCode": 200},
                ),
            ):
                state = rag_server_lifecycle.read_server_process_state(
                    resolve_rag_settings(paths),
                    probe_health=True,
                )

            self.assertTrue(state["running"])
            self.assertEqual(state["status"], "healthy")

    def test_rag_server_python_selection_prefers_current_runtime_before_system_python(self):
        current_python = "/tmp/actanara-current-python"
        with (
            patch.object(rag_server_lifecycle.sys, "executable", current_python),
            patch.object(rag_server_lifecycle, "_runtime_venv_python", return_value=None),
            patch.object(rag_server_lifecycle.Path, "exists", return_value=True),
            patch.object(
                rag_server_lifecycle,
                "_python_has_modules",
                side_effect=lambda path, **_kwargs: path in {current_python, "/usr/bin/python3"},
            ),
            patch.dict(os.environ, {"NOVA_RAG_SERVER_PYTHON": ""}, clear=False),
        ):
            self.assertEqual(rag_server_lifecycle._select_server_python(), current_python)

    def test_rag_server_python_selection_does_not_implicitly_fallback_to_system_python(self):
        with (
            patch.object(rag_server_lifecycle.sys, "executable", "/tmp/actanara-current-python"),
            patch.object(rag_server_lifecycle, "_runtime_venv_python", return_value=None),
            patch.object(rag_server_lifecycle.Path, "exists", return_value=True),
            patch.object(
                rag_server_lifecycle,
                "_python_has_modules",
                side_effect=lambda path, **_kwargs: path == "/usr/bin/python3",
            ),
            patch.dict(os.environ, {"NOVA_RAG_SERVER_PYTHON": ""}, clear=False),
        ):
            self.assertIsNone(rag_server_lifecycle._select_server_python())

    def test_rag_server_start_reports_rag_local_install_group_when_runtime_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {"rag": {"enabled": True, "mode": "v2", "server": {"enabled": True}}},
                paths,
            )
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(rag_server_lifecycle, "read_server_process_state", return_value={"running": False, "status": "stopped", "health": None}),
                patch.object(rag_server_lifecycle, "_select_server_python", return_value=None),
            ):
                settings = resolve_rag_settings(paths)
                result = rag_server_lifecycle.start_rag_server(settings, requested_by="test", wait_timeout_seconds=0)

            self.assertFalse(result["accepted"])
            self.assertEqual(result["status"], "missing-runtime")
            self.assertEqual(result["requiredInstallGroup"], "rag-local")
            self.assertIn("rag-local", result["installHint"])

    def test_cloud_rag_server_does_not_require_local_embedding_package(self):
        settings = resolve_rag_settings(
            settings={
                "rag": {
                    "enabled": True,
                    "mode": "v2",
                    "embedding": {"mode": "cloud", "provider": "cloud", "providerId": "test-cloud"},
                    "server": {"enabled": True},
                }
            }
        )
        self.assertNotIn("sentence_transformers", rag_server_lifecycle._required_server_modules(settings))
        self.assertIn("fastapi", rag_server_lifecycle._required_server_modules(settings))

    def test_rag_server_python_module_check_requires_supported_python_version(self):
        class FakeCompleted:
            returncode = 0

        with patch.object(rag_server_lifecycle.subprocess, "run", return_value=FakeCompleted()) as run:
            self.assertTrue(rag_server_lifecycle._python_has_modules("/tmp/python"))

        command = run.call_args.args[0]
        self.assertEqual(command[0], "/tmp/python")
        self.assertIn("sys.version_info >= (3, 10)", command[2])
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_rag_server_python_module_check_allows_a_bounded_probe_timeout_override(self):
        class FakeCompleted:
            returncode = 0

        with (
            patch.dict(
                os.environ,
                {"NOVA_RAG_MODULE_IMPORT_PROBE_TIMEOUT_SECONDS": "45"},
                clear=False,
            ),
            patch.object(rag_server_lifecycle.subprocess, "run", return_value=FakeCompleted()) as run,
        ):
            self.assertTrue(rag_server_lifecycle._python_has_modules("/tmp/python"))

        self.assertEqual(run.call_args.kwargs["timeout"], 45)

    def test_rag_server_lifecycle_stop_only_terminates_recorded_rag_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            state_path = paths.state_dir / "rag" / "server-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "pid": 4321,
                        "command": ["/usr/bin/python3", str(rag_server_lifecycle.SERVER_SCRIPT)],
                        "cwd": str(rag_server_lifecycle.ROOT),
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch.object(rag_server_lifecycle, "_is_linux", return_value=False),
                patch.object(rag_server_lifecycle, "_pid_running", side_effect=[True, False, False, False]),
                patch.object(rag_server_lifecycle.os, "kill") as kill,
            ):
                settings = resolve_rag_settings(paths)
                result = rag_server_lifecycle.stop_rag_server(settings, requested_by="test", wait_timeout_seconds=0.1)

            self.assertTrue(result["accepted"])
            self.assertEqual(result["status"], "stopped")
            kill.assert_called_once_with(4321, rag_server_lifecycle.signal.SIGTERM)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["requestedBy"], "test")
            self.assertEqual(state["status"], "stopped")

    def test_rag_server_pid_probe_reaps_an_owned_zombie_before_existence_check(self):
        with (
            patch.object(rag_server_lifecycle.os, "waitpid", return_value=(4321, 0)) as waitpid,
            patch.object(rag_server_lifecycle.os, "kill") as kill,
        ):
            self.assertFalse(rag_server_lifecycle._pid_running(4321))

        waitpid.assert_called_once_with(4321, rag_server_lifecycle.os.WNOHANG)
        kill.assert_not_called()

    def test_v2_manifest_dimension_mismatch_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"mode": "v2"}}, paths)
            store = paths.home / "reserved" / "rag" / "v2"
            store.mkdir(parents=True)
            (store / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "active",
                        "model": "BAAI/bge-large-zh-v1.5",
                        "dimension": 1024,
                        "chunkCount": 10,
                        "activeIndexPath": str(store / "indexes" / "active" / "index.jsonl"),
                    }
                ),
                encoding="utf-8",
            )
            (store / "indexes" / "active").mkdir(parents=True)
            (store / "indexes" / "active" / "index.jsonl").write_text("{}\n", encoding="utf-8")
            settings = resolve_rag_settings(paths)
            status = read_rag_status(settings=settings, count_legacy_entries=False)
            self.assertEqual(status["activeSource"], "v2")
            self.assertTrue(status["ready"])
            self.assertTrue(status["v2"]["dimensionMismatch"])
            self.assertEqual(status["v2"]["dimension"], 1024)
            self.assertEqual(status["settings"]["v2_store_path"], str(store))

    def test_active_v2_profile_mismatch_blocks_search_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            store = paths.home / "reserved" / "rag" / "v2"
            active_index = store / "indexes" / "active" / "index.jsonl"
            active_index.parent.mkdir(parents=True)
            active_index.write_text(json.dumps({"id": "v2", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            (store / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "active",
                        "model": "all-MiniLM-L6-v2",
                        "dimension": 2,
                        "embeddingProvider": "local",
                        "embeddingProviderId": "local",
                        "activeIndexPath": str(active_index),
                    }
                ),
                encoding="utf-8",
            )
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "v2": {"storePath": str(store)},
                        "embedding": {
                            "mode": "cloud",
                            "providerId": "example-cloud",
                            "model": "cloud-embed",
                            "dimension": 2,
                        },
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)
            status = read_rag_status(settings=settings, count_legacy_entries=False)
            self.assertTrue(status["ready"])
            self.assertTrue(status["profile"]["mismatch"])
            self.assertTrue(status["profile"]["migrationRequired"])
            self.assertEqual(status["freshness"]["status"], "embedding-profile-mismatch")
            self.assertFalse(status["searchAvailable"])

    def test_candidate_ready_v2_manifest_is_not_active_search_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            store = paths.home / "reserved" / "rag" / "v2"
            candidate = store / "indexes" / "candidates" / "run-1" / "index.jsonl"
            candidate.parent.mkdir(parents=True)
            candidate.write_text(json.dumps({"id": "candidate", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            (store / "manifest.json").write_text(
                json.dumps({"status": "candidate-ready", "dimension": 2, "candidateIndexPath": str(candidate)}),
                encoding="utf-8",
            )
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "v2": {"storePath": str(store)},
                        "embedding": {"dimension": 2},
                    }
                },
                paths,
            )

            status = read_rag_status(settings=resolve_rag_settings(paths), count_legacy_entries=False)

            self.assertFalse(status["ready"])
            self.assertFalse(status["searchAvailable"])
            self.assertFalse(status["v2"]["activeReady"])
            self.assertTrue(status["v2"]["candidateReady"])
            self.assertEqual(status["v2"]["candidateIndexPath"], str(candidate))
            self.assertIsNone(status["v2"]["activeIndexPath"])

    def test_status_reports_source_profile_mismatch_without_invalidating_active_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "LegacyDiary"
            current = root / "GeneratedDiary"
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            paths = update_runtime_manifest_paths(
                paths.home,
                generated_diary_root=current,
                legacy_diary_root=legacy,
                task_board_path=root / "tasks" / "TASK_BOARD.md",
            )
            store = paths.home / "reserved" / "rag" / "v2"
            active = store / "indexes" / "active" / "run-1" / "index.jsonl"
            active.parent.mkdir(parents=True)
            active.write_text(json.dumps({"id": "v2", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            (store / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "active",
                        "dimension": 2,
                        "activeIndexPath": str(active),
                        "embeddingProfile": {"mode": "local", "providerId": "local", "model": "m", "dimension": 2},
                        "sourceProfile": {
                            "schemaVersion": 1,
                            "diarySourceRoot": str(legacy),
                            "filteredDialogueRoot": str(legacy / "__diary_daily"),
                            "lessonsPath": str(root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"),
                            "taskBoardPath": str(legacy / "TASK_BOARD.md"),
                            "foundationDbPath": str(paths.db_path),
                            "sourceSets": list(DEFAULT_INDEXING_SOURCE_SETS),
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "v2": {"storePath": str(store)},
                        "embedding": {"model": "m", "dimension": 2},
                    }
                },
                paths,
            )

            status = read_rag_status(settings=resolve_rag_settings(paths), count_legacy_entries=False)

            self.assertTrue(status["ready"])
            self.assertFalse(status["searchAvailable"])
            self.assertTrue(status["sourceProfile"]["mismatch"])
            self.assertTrue(status["sourceProfile"]["migrationRequired"])
            self.assertEqual(status["sourceProfile"]["configured"]["diarySourceRoot"], str(current))
            self.assertEqual(status["sourceProfile"]["active"]["diarySourceRoot"], str(legacy))
            self.assertEqual(status["freshness"]["status"], "source-profile-mismatch")

    def test_active_rag_index_resolver_retires_legacy_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            legacy_index = legacy / "__diary_rag" / "index.jsonl"
            legacy_index.parent.mkdir(parents=True)
            legacy_index.write_text(json.dumps({"id": "legacy", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"mode": "legacy", "embedding": {"dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)

            legacy_active = resolve_active_rag_index(settings)
            self.assertEqual(legacy_active.source, "retired")
            self.assertIsNone(legacy_active.index_path)
            self.assertFalse(legacy_active.ready)
            self.assertIn("retired", legacy_active.reason)

            write_settings({"rag": {"mode": "v2-shadow"}}, paths)
            shadow = resolve_active_rag_index(resolve_rag_settings(paths))
            self.assertEqual(shadow.source, "retired")
            self.assertIsNone(shadow.index_path)
            self.assertFalse(shadow.ready)

            store = paths.home / "reserved" / "rag" / "v2"
            candidate = store / "indexes" / "candidates" / "run-1" / "index.jsonl"
            candidate.parent.mkdir(parents=True)
            candidate.write_text(json.dumps({"id": "v2", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            store.mkdir(parents=True, exist_ok=True)
            (store / "manifest.json").write_text(
                json.dumps({"status": "candidate-ready", "dimension": 2, "candidateIndexPath": str(candidate)}),
                encoding="utf-8",
            )
            write_settings({"rag": {"mode": "v2"}}, paths)
            blocked = resolve_active_rag_index(resolve_rag_settings(paths))
            self.assertEqual(blocked.source, "v2")
            self.assertFalse(blocked.ready)
            self.assertIsNone(blocked.index_path)
            self.assertIn("not-active", blocked.reason)

            (store / "manifest.json").write_text(
                json.dumps({"status": "active", "dimension": 2, "candidateIndexPath": str(candidate)}),
                encoding="utf-8",
            )
            active = resolve_active_rag_index(resolve_rag_settings(paths))
            self.assertEqual(active.source, "v2")
            self.assertIsNone(active.index_path)
            self.assertFalse(active.ready)
            self.assertEqual(active.reason, "v2-active-index-missing")

            (store / "manifest.json").write_text(
                json.dumps({"status": "active", "dimension": 2, "activeIndexPath": str(candidate)}),
                encoding="utf-8",
            )
            active = resolve_active_rag_index(resolve_rag_settings(paths))
            self.assertEqual(active.source, "v2")
            self.assertEqual(active.index_path, candidate)
            self.assertTrue(active.ready)

            write_settings({"rag": {"mode": "disabled"}}, paths)
            disabled = resolve_active_rag_index(resolve_rag_settings(paths))
            self.assertEqual(disabled.source, "disabled")
            self.assertIsNone(disabled.index_path)
            self.assertFalse(disabled.ready)

    def test_rag_config_uses_active_v2_index_only_when_mode_is_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            store = root / "Actanara" / "reserved" / "rag" / "v2"
            active_index = store / "indexes" / "active" / "index.jsonl"
            active_index.parent.mkdir(parents=True)
            active_index.write_text(json.dumps({"id": "v2", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            (store / "manifest.json").write_text(
                json.dumps({"status": "active", "dimension": 2, "activeIndexPath": str(active_index)}),
                encoding="utf-8",
            )
            legacy_index = root / "Diary" / "__diary_rag" / "index.jsonl"
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "v2": {"storePath": str(store)},
                        "legacy": {"indexPath": str(legacy_index)},
                        "embedding": {"dimension": 2},
                    }
                },
                paths,
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}):
                _, rag_config = _reload_rag_modules("rag_active_source", "rag_config")

            self.assertEqual(rag_config.INDEX_SOURCE, "v2")
            self.assertEqual(rag_config.INDEX_FILE, active_index)
            self.assertEqual(rag_config.LEGACY_INDEX_FILE, legacy_index)
            self.assertTrue(rag_config.INDEX_READY)

    def test_embedding_server_reloads_when_active_v2_index_changes_after_startup(self):
        class FakeArray(list):
            def __truediv__(self, other):
                return self

        fake_numpy = types.SimpleNamespace(
            float32="float32",
            array=lambda values, dtype=None: FakeArray(values),
            linalg=types.SimpleNamespace(norm=lambda *args, **kwargs: 1.0),
        )
        class FakeFastAPI:
            title = "fake"

            def __init__(self, title=None, lifespan=None):
                self.title = title or "fake"

            def get(self, *_args, **_kwargs):
                return lambda fn: fn

            def post(self, *_args, **_kwargs):
                return lambda fn: fn

        fake_fastapi = types.SimpleNamespace(FastAPI=FakeFastAPI, HTTPException=Exception)
        fake_pydantic = types.SimpleNamespace(BaseModel=object, Field=lambda **_kwargs: [])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            store = paths.home / "reserved" / "rag" / "v2"
            first = store / "indexes" / "active" / "run-1" / "index.jsonl"
            second = store / "indexes" / "active" / "run-2" / "index.jsonl"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text(json.dumps({"id": "first", "embedding": [1.0, 0.0], "text": "old"}) + "\n", encoding="utf-8")
            second.write_text(json.dumps({"id": "second", "embedding": [0.0, 1.0], "text": "new"}) + "\n", encoding="utf-8")
            (store / "manifest.json").write_text(
                json.dumps({"status": "active", "dimension": 2, "activeIndexPath": str(first)}),
                encoding="utf-8",
            )
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "v2": {"storePath": str(store)},
                        "embedding": {"dimension": 2},
                    }
                },
                paths,
            )
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.dict(
                    sys.modules,
                    {
                        "uvicorn": MagicMock(),
                        "numpy": fake_numpy,
                        "fastapi": fake_fastapi,
                        "pydantic": fake_pydantic,
                    },
                ),
            ):
                _, _, embedding_server = _reload_rag_modules("rag_active_source", "rag_config", "embedding_server")
                _, ids, _ = embedding_server.get_emb_matrix()
                self.assertEqual(ids, ["first"])
                (store / "manifest.json").write_text(
                    json.dumps({"status": "active", "dimension": 2, "activeIndexPath": str(second)}),
                    encoding="utf-8",
                )
                _, ids, _ = embedding_server.get_emb_matrix()
                self.assertEqual(ids, ["second"])

    def test_embedding_server_fails_closed_when_active_resolver_errors(self):
        class FakeArray(list):
            def __truediv__(self, other):
                return self

        fake_numpy = types.SimpleNamespace(
            float32="float32",
            array=lambda values, dtype=None: FakeArray(values),
            linalg=types.SimpleNamespace(norm=lambda *args, **kwargs: 1.0),
        )

        class FakeFastAPI:
            title = "fake"

            def __init__(self, title=None, lifespan=None):
                self.title = title or "fake"

            def get(self, *_args, **_kwargs):
                return lambda fn: fn

            def post(self, *_args, **_kwargs):
                return lambda fn: fn

        fake_fastapi = types.SimpleNamespace(FastAPI=FakeFastAPI, HTTPException=Exception)
        fake_pydantic = types.SimpleNamespace(BaseModel=object, Field=lambda **_kwargs: [])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            legacy_index = root / "Diary" / "__diary_rag" / "index.jsonl"
            legacy_index.parent.mkdir(parents=True)
            legacy_index.write_text(json.dumps({"id": "legacy", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            write_settings({"rag": {"mode": "v2", "embedding": {"dimension": 2}}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch.dict(
                    sys.modules,
                    {
                        "uvicorn": MagicMock(),
                        "numpy": fake_numpy,
                        "fastapi": fake_fastapi,
                        "pydantic": fake_pydantic,
                    },
                ),
            ):
                _, _, embedding_server = _reload_rag_modules("rag_active_source", "rag_config", "embedding_server")
                embedding_server.resolve_active_rag_index = MagicMock(side_effect=RuntimeError("resolver failed"))

                matrix, ids, chunks = embedding_server.get_emb_matrix()
                state = embedding_server._current_index_state()

            self.assertIsNone(matrix)
            self.assertIsNone(ids)
            self.assertIsNone(chunks)
            self.assertEqual(state["source"], "unavailable")
            self.assertIsNone(state["path"])
            self.assertFalse(state["ready"])

    def test_status_requires_server_to_have_loaded_active_index_for_search(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "status": "ok",
                        "model": "m",
                        "dimension": 2,
                        "provider": "local",
                        "providerId": "local",
                        "embeddingProfile": {"mode": "local", "providerId": "local", "model": "m", "dimension": 2},
                        "providerLoaded": True,
                        "indexPath": "/tmp/stale-legacy/index.jsonl",
                        "indexLoaded": False,
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            store = paths.home / "reserved" / "rag" / "v2"
            active = store / "indexes" / "active" / "run-1" / "index.jsonl"
            active.parent.mkdir(parents=True)
            active.write_text(json.dumps({"id": "v2", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            (store / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "active",
                        "dimension": 2,
                        "activeIndexPath": str(active),
                        "embeddingProfile": {"mode": "local", "providerId": "local", "model": "m", "dimension": 2},
                    }
                ),
                encoding="utf-8",
            )
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "v2": {"storePath": str(store)},
                        "embedding": {"model": "m", "dimension": 2},
                        "server": {"enabled": True},
                    }
                },
                paths,
            )
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}),
                patch("agentic_rag.rag_status.urllib.request.urlopen", return_value=FakeResponse()),
            ):
                status = read_rag_status(settings=resolve_rag_settings(paths), probe_server=True)

            self.assertTrue(status["ready"])
            self.assertTrue(status["server"]["healthy"])
            self.assertFalse(status["server"]["searchReady"])
            self.assertFalse(status["searchAvailable"])
            self.assertEqual(status["freshness"]["status"], "server-index-not-ready")

    def test_status_blocks_search_when_running_server_profile_is_stale(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "status": "ok",
                        "model": "old-model",
                        "dimension": 2,
                        "provider": "local",
                        "providerId": "local",
                        "embeddingProfile": {"mode": "local", "providerId": "local", "model": "old-model", "dimension": 2},
                        "providerLoaded": True,
                        "indexPath": str(self.index_path),
                        "indexLoaded": True,
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            store = paths.home / "reserved" / "rag" / "v2"
            active = store / "indexes" / "active" / "run-1" / "index.jsonl"
            FakeResponse.index_path = active
            active.parent.mkdir(parents=True)
            active.write_text(json.dumps({"id": "v2", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            (store / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "active",
                        "dimension": 2,
                        "activeIndexPath": str(active),
                        "embeddingProfile": {"mode": "local", "providerId": "local", "model": "new-model", "dimension": 2},
                    }
                ),
                encoding="utf-8",
            )
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "v2": {"storePath": str(store)},
                        "embedding": {"model": "new-model", "dimension": 2},
                        "server": {"enabled": True},
                    }
                },
                paths,
            )
            with patch("agentic_rag.rag_status.urllib.request.urlopen", return_value=FakeResponse()):
                status = read_rag_status(settings=resolve_rag_settings(paths), probe_server=True)

            self.assertTrue(status["ready"])
            self.assertTrue(status["server"]["healthy"])
            self.assertTrue(status["server"]["indexMatchesActive"])
            self.assertTrue(status["server"]["profileStale"])
            self.assertFalse(status["server"]["profileMatchesSettings"])
            self.assertFalse(status["server"]["searchReady"])
            self.assertFalse(status["searchAvailable"])
            self.assertEqual(status["freshness"]["status"], "server-profile-stale")

    def test_legacy_rag_config_shim_has_no_secret_or_directory_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            index = root / "Diary" / "__diary_rag" / "index.jsonl"
            write_settings(
                {
                    "rag": {
                        "mode": "legacy",
                        "legacy": {"indexPath": str(index)},
                        "embedding": {
                            "provider": "local",
                            "model": "all-MiniLM-L6-v2",
                            "dimension": 384,
                        },
                    }
                },
                paths,
            )
            with patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home), "RAG_MODE": "cloud"}):
                (rag_config,) = _reload_rag_modules("rag_config")
            self.assertEqual(rag_config.INDEX_FILE, index)
            self.assertEqual(rag_config.MODEL_NAME, "all-MiniLM-L6-v2")
            self.assertEqual(rag_config.EMBEDDING_DIM, 384)
            self.assertEqual(rag_config.PRODUCTION_MODE, "local")
            self.assertEqual(rag_config.CLOUD_API_KEY, "")
            self.assertFalse(index.parent.exists())

    def test_retriever_fuses_dense_keyword_recency_and_tags_without_writing_index(self):
        chunks = [
            {
                "id": "dense-only",
                "text": "unrelated memory",
                "date": "2026-06-04",
                "agent": "codex",
                "layer": "narrative",
                "embedding": [1.0, 0.0],
            },
            {
                "id": "keyword-coding",
                "text": "部署 pytest src/ regression fix",
                "date": "2026-06-04",
                "agent": "codex",
                "layer": "technical",
                "embedding": [0.8, 0.6],
            },
            {
                "id": "old-lesson",
                "text": "部署 lesson decision",
                "date": "2026-01-01",
                "agent": "codex",
                "layer": "lesson",
                "embedding": [0.8, 0.6],
            },
            {
                "id": "mismatch",
                "text": "部署",
                "date": "2026-06-04",
                "agent": "codex",
                "embedding": [1.0, 0.0, 0.0],
            },
        ]
        ranked = rank_chunks(
            query="部署 pytest",
            query_embedding=[1.0, 0.0],
            chunks=chunks,
            top_k=5,
            similarity_weight=0.6,
            keyword_weight=0.4,
            recency_half_life_days=30,
            now=datetime(2026, 6, 5),
        )
        self.assertIn("coding", ranked["queryPlan"]["preferredTags"])
        self.assertEqual(ranked["schemaVersion"], 2)
        self.assertTrue(ranked["available"])
        self.assertEqual(ranked["queryPlan"]["schemaVersion"], 2)
        self.assertEqual(ranked["skippedDimension"], 1)
        self.assertEqual(ranked["results"][0]["id"], "keyword-coding")
        self.assertGreater(ranked["results"][0]["scoreComponents"]["keyword"], 0)
        self.assertGreater(ranked["results"][0]["scoreComponents"]["intentBoost"], 1.0)
        self.assertIn("coding", ranked["results"][0]["tags"])
        self.assertEqual(ranked["results"][0]["workType"], "coding")
        old_lesson = next(item for item in ranked["results"] if item["id"] == "old-lesson")
        self.assertLess(old_lesson["scoreComponents"]["recency"], ranked["results"][0]["scoreComponents"]["recency"])
        self.assertGreater(old_lesson["scoreComponents"]["recencyFactor"], 1.0)

    def test_retriever_metadata_tag_filter_and_work_type_inference(self):
        incident = {"text": "Traceback Error: failed migration", "layer": "technical"}
        lesson = {"text": "decision record", "layer": "lesson"}
        self.assertIn("incident", infer_tags(incident))
        self.assertEqual(infer_work_type(incident), "incident")
        self.assertEqual(infer_work_type(lesson), "lesson")

        ranked = rank_chunks(
            query="incident migration",
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "incident",
                    "text": "Traceback Error: failed migration",
                    "date": "2026-06-04",
                    "layer": "technical",
                    "embedding": [1.0, 0.0],
                },
                {
                    "id": "daily",
                    "text": "migration note",
                    "date": "2026-06-04",
                    "layer": "narrative",
                    "embedding": [1.0, 0.0],
                },
            ],
            top_k=5,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            tag_filter=["incident"],
            now=datetime(2026, 6, 5),
        )
        self.assertEqual(ranked["returned"], 1)
        self.assertEqual(ranked["results"][0]["id"], "incident")
        self.assertIn("incident", ranked["queryPlan"]["intents"])
        self.assertEqual(ranked["queryPlan"]["explicitFilters"]["tags"], ["incident"])

    def test_query_plan_uses_soft_intent_boost_without_hard_filtering(self):
        ranked = rank_chunks(
            query="最近 task 进度",
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "task",
                    "text": "任务 看板 进度 update",
                    "date": "2026-06-05",
                    "layer": "task",
                    "embedding": [1.0, 0.0],
                },
                {
                    "id": "general",
                    "text": "general progress note",
                    "date": "2026-06-05",
                    "layer": "narrative",
                    "embedding": [1.0, 0.0],
                },
            ],
            top_k=5,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            now=datetime(2026, 6, 5),
        )
        self.assertEqual(ranked["returned"], 2)
        self.assertEqual(ranked["queryPlan"]["recencyBias"], "strong")
        self.assertIn("task", ranked["queryPlan"]["preferredTags"])
        self.assertEqual(ranked["results"][0]["id"], "task")
        self.assertGreater(ranked["results"][0]["scoreComponents"]["intentBoost"], 1.0)

    def test_agentic_response_adds_citations_decomposition_and_extractive_synthesis(self):
        ranked = rank_chunks(
            query="最近 RAG bug 修复进度",
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "incident-fix",
                    "text": "RAG bug 修复完成，pytest regression passed.",
                    "date": "2026-06-05",
                    "agent": "codex",
                    "layer": "technical",
                    "sourceSet": "technical-report-task-events",
                    "sourceType": "markdown",
                    "sourceId": "technical:2026-06-05",
                    "sourcePath": "/tmp/技术进展-260605.md",
                    "dedupeKey": "technical:2026-06-05:rag",
                    "embedding": [1.0, 0.0],
                },
                {
                    "id": "task-state",
                    "text": "RAG task progress updated in task board.",
                    "date": "2026-06-05",
                    "agent": "codex",
                    "layer": "task",
                    "embedding": [0.9, 0.1],
                },
            ],
            top_k=2,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            now=datetime(2026, 6, 5),
        )

        self.assertIn("citationPack", ranked)
        self.assertEqual(ranked["citationPack"][0]["citationId"], "C1")
        self.assertEqual(ranked["citationPack"][0]["resultId"], "incident-fix")
        self.assertEqual(ranked["results"][0]["sourceSet"], "technical-report-task-events")
        self.assertEqual(ranked["results"][0]["sourceType"], "markdown")
        self.assertEqual(ranked["results"][0]["sourceId"], "technical:2026-06-05")
        self.assertEqual(ranked["results"][0]["provenance"]["sourceSet"], "technical-report-task-events")
        self.assertEqual(ranked["results"][0]["provenance"]["dedupeKey"], "technical:2026-06-05:rag")
        self.assertEqual(ranked["citationPack"][0]["provenance"]["sourceType"], "markdown")
        self.assertIn("whySelected", ranked["citationPack"][0])
        self.assertEqual(ranked["answerSynthesis"]["status"], "ready")
        self.assertEqual(ranked["answerSynthesis"]["method"], "extractive")
        self.assertEqual(ranked["answerSynthesis"]["answerType"], "incident")
        self.assertIn("C1", ranked["answerSynthesis"]["citationIds"])
        self.assertEqual(ranked["eventAggregation"]["status"], "ready")
        self.assertEqual(ranked["eventAggregation"]["eventCount"], 2)
        self.assertEqual(ranked["agentic"]["eventAggregation"]["eventCount"], 2)
        self.assertEqual(ranked["agentic"]["schemaVersion"], 2)
        self.assertIn("event-aggregation", ranked["agentic"]["implementedCapabilities"])
        self.assertIn("C1", ranked["eventAggregation"]["resolutionCitations"])
        self.assertIn("query-decomposition", ranked["agentic"]["implementedCapabilities"])
        self.assertIn("multi-hop-evidence-linking", ranked["agentic"]["implementedCapabilities"])
        self.assertIn("query-decomposition", ranked["queryPlan"]["stages"])
        self.assertIn("multi-hop-lexical-expansion", ranked["queryPlan"]["stages"])
        self.assertGreaterEqual(len(ranked["queryPlan"]["subQueries"]), 2)
        self.assertGreaterEqual(len(ranked["agentic"]["decomposition"]["subqueries"]), 2)
        self.assertTrue(ranked["agentic"]["decomposition"]["multiHopLinks"])
        self.assertEqual(ranked["quality"]["status"], "strong")
        self.assertFalse(ranked["quality"]["needsMoreEvidence"])
        self.assertIn("quality-gate", ranked["queryPlan"]["stages"])
        self.assertTrue(ranked["retrievalController"]["serverSide"])
        self.assertIn("exact-entity-recall", ranked["retrievalController"]["passesRun"])
        self.assertIn("quality-gate", ranked["agentic"]["implementedCapabilities"])
        self.assertIn("bounded-multi-pass-retrieval", ranked["agentic"]["implementedCapabilities"])
        self.assertEqual(ranked["agentic"]["quality"]["status"], "strong")

    def test_retriever_prefers_old_exact_evidence_over_recent_generic_match(self):
        chunks = [
            {
                "id": "recent-generic",
                "text": "dashboard 最近任务记录和运维备注，没有端口原因。",
                "date": "2026-07-05",
                "layer": "narrative",
                "embedding": [1.0, 0.0],
            },
            {
                "id": "old-port",
                "text": "2096 端口不可用，因为目标环境 firewall DROP 该端口。",
                "date": "2026-05-04",
                "layer": "technical",
                "sourceSet": "lessons",
                "embedding": [0.82, 0.57],
            },
        ]

        ranked = rank_chunks(
            query="2096 端口为什么不可用？",
            query_embedding=[1.0, 0.0],
            chunks=chunks,
            top_k=2,
            similarity_weight=0.8,
            keyword_weight=0.2,
            recency_half_life_days=7,
            now=datetime(2026, 7, 7),
            language_profile="zh",
        )

        self.assertEqual(ranked["queryPlan"]["recencyBias"], "normal")
        self.assertEqual(ranked["results"][0]["id"], "old-port")
        self.assertGreater(ranked["results"][0]["scoreComponents"]["exactCoverage"], 0.9)
        self.assertGreater(ranked["results"][0]["scoreComponents"]["citableExactCoverage"], 0.9)
        self.assertGreater(ranked["results"][0]["scoreComponents"]["recencyFactor"], 1.0)
        self.assertLess(ranked["results"][0]["scoreComponents"]["recencyFactor"], 1.01)
        self.assertEqual(ranked["quality"]["coveredTerms"], ["2096", "端口", "不可用"])
        self.assertEqual(ranked["quality"]["coverageBasis"], "citable-text-only")
        self.assertEqual(ranked["quality"]["status"], "strong")
        self.assertIn("2096 端口不可用", ranked["citationPack"][0]["excerpt"])

    def test_retriever_quality_ignores_opaque_metadata_term_matches_without_changing_retrieval_rank(self):
        ranked = rank_chunks(
            query="2096 端口为什么不可用？",
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "opaque-2096-端口-不可用",
                    "text": "Generic operational note without the requested cause.",
                    "sourceId": "lesson:2096:端口:不可用",
                    "sourcePath": "/tmp/2096-端口-不可用.md",
                    "dedupeKey": "2096:端口:不可用",
                    "date": "2026-07-05",
                    "layer": "technical",
                    "embedding": [0.95, 0.3122499],
                },
                {
                    "id": "dense-generic",
                    "text": "Another unrelated operational note.",
                    "date": "2026-07-05",
                    "layer": "technical",
                    "embedding": [1.0, 0.0],
                },
            ],
            top_k=2,
            similarity_weight=0.8,
            keyword_weight=0.2,
            recency_half_life_days=7,
            now=datetime(2026, 7, 7),
            language_profile="zh",
        )

        self.assertEqual(ranked["results"][0]["id"], "opaque-2096-端口-不可用")
        self.assertEqual(ranked["results"][0]["scoreComponents"]["exactCoverage"], 1.0)
        self.assertEqual(ranked["results"][0]["scoreComponents"]["retrievalExactCoverage"], 1.0)
        self.assertEqual(ranked["results"][0]["scoreComponents"]["citableExactCoverage"], 0.0)
        self.assertEqual(ranked["quality"]["retrievalCoveredTerms"], ["2096", "端口", "不可用"])
        self.assertEqual(ranked["quality"]["retrievalCoverage"], 1.0)
        self.assertEqual(ranked["quality"]["coveredTerms"], [])
        self.assertEqual(ranked["quality"]["coverage"], 0.0)
        self.assertEqual(ranked["quality"]["status"], "weak")
        self.assertFalse(ranked["quality"]["flags"]["hasNonMetaExactEvidence"])
        self.assertTrue(ranked["quality"]["flags"]["metadataOnlyTermCoverage"])

    def test_retriever_quality_accepts_english_citable_text_not_opaque_metadata(self):
        ranked = rank_chunks(
            query="Why is port 2096 unavailable?",
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "opaque-why-is-port-2096-unavailable",
                    "text": "Generic operational note.",
                    "sourceId": "why-is-port-2096-unavailable",
                    "sourcePath": "/tmp/why-is-port-2096-unavailable.md",
                    "dedupeKey": "why-is-port-2096-unavailable",
                    "date": "2026-07-05",
                    "layer": "technical",
                    "embedding": [1.0, 0.0],
                },
                {
                    "id": "citable-port-evidence",
                    "text": "Port 2096 is unavailable because the firewall drops inbound traffic.",
                    "date": "2026-05-04",
                    "layer": "lesson",
                    "sourceSet": "lessons",
                    "embedding": [0.9, 0.4358899],
                },
            ],
            top_k=2,
            similarity_weight=0.8,
            keyword_weight=0.2,
            recency_half_life_days=7,
            now=datetime(2026, 7, 7),
            language_profile="en",
        )

        by_id = {item["id"]: item for item in ranked["results"]}
        self.assertEqual(by_id["opaque-why-is-port-2096-unavailable"]["scoreComponents"]["exactCoverage"], 1.0)
        self.assertEqual(
            by_id["opaque-why-is-port-2096-unavailable"]["scoreComponents"]["citableExactCoverage"],
            0.0,
        )
        self.assertEqual(by_id["citable-port-evidence"]["scoreComponents"]["citableExactCoverage"], 1.0)
        self.assertEqual(ranked["quality"]["coverage"], 1.0)
        self.assertEqual(ranked["quality"]["status"], "strong")
        self.assertTrue(ranked["quality"]["flags"]["hasNonMetaExactEvidence"])
        citation_excerpt = next(
            citation["excerpt"]
            for citation in ranked["citationPack"]
            if citation["resultId"] == "citable-port-evidence"
        )
        self.assertIn("Port 2096 is unavailable", citation_excerpt)

    def test_retriever_suppresses_meta_discussion_for_fact_queries(self):
        chunks = [
            {
                "id": "recent-meta",
                "text": "nova-RAG 真实索引下召回质量差：2096 端口为什么不可用 Top-1000 benchmark needsMoreEvidence 测试结果。",
                "date": "2026-07-07",
                "layer": "technical",
                "sourceSet": "filtered-dialogue-daily",
                "embedding": [1.0, 0.0],
            },
            {
                "id": "old-port",
                "text": "2096 端口不可用，因为目标环境 firewall DROP 该端口。",
                "date": "2026-05-04",
                "layer": "technical",
                "sourceSet": "lessons",
                "governance": {"lifecycle": "canonical", "authorityRank": 95, "provenanceScore": 1.0},
                "embedding": [0.98, 0.2],
            },
        ]

        ranked = rank_chunks(
            query="2096 端口为什么不可用？",
            query_embedding=[1.0, 0.0],
            chunks=chunks,
            top_k=2,
            similarity_weight=0.8,
            keyword_weight=0.2,
            recency_half_life_days=7,
            now=datetime(2026, 7, 7),
            language_profile="zh",
        )

        self.assertEqual(ranked["results"][0]["id"], "old-port")
        meta = next(item for item in ranked["results"] if item["id"] == "recent-meta")
        self.assertGreaterEqual(meta["scoreComponents"]["metaDiscussion"], 0.5)
        self.assertFalse(ranked["quality"]["flags"]["metaDiscussionTop"])
        self.assertEqual(ranked["quality"]["status"], "strong")

        meta_only = rank_chunks(
            query="2096 端口为什么不可用？",
            query_embedding=[1.0, 0.0],
            chunks=[chunks[0]],
            top_k=1,
            similarity_weight=0.8,
            keyword_weight=0.2,
            recency_half_life_days=7,
            now=datetime(2026, 7, 7),
            language_profile="zh",
        )
        self.assertEqual(meta_only["quality"]["status"], "weak")
        self.assertTrue(meta_only["quality"]["flags"]["metaDiscussionTop"])
        self.assertIn("retry-with-meta-discussion-suppressed", meta_only["quality"]["recommendations"])

    def test_retriever_builds_and_fuses_bounded_retrieval_passes(self):
        query = "2096 端口为什么不可用？"
        query_plan = build_query_plan(query)
        passes = build_retrieval_passes(query, query_plan)
        pass_ids = [item["id"] for item in passes]

        self.assertIn("baseline-hybrid", pass_ids)
        self.assertIn("exact-entity-recall", pass_ids)
        self.assertIn("authoritative-source-pass", pass_ids)
        network_plan = build_query_plan("dashboard 网络 回环地址 tailscale")
        self.assertIn("config", network_plan["intents"])
        self.assertIn(
            "authoritative-source-pass",
            [item["id"] for item in build_retrieval_passes("dashboard 网络 回环地址 tailscale", network_plan)],
        )

        result = {
            "id": "port-fact",
            "score": 0.8,
            "text": "2096 端口不可用，因为目标环境 firewall DROP 该端口。",
            "textPreview": "2096 端口不可用，因为目标环境 firewall DROP 该端口。",
            "sourceSet": "lessons",
            "scoreComponents": {"exactCoverage": 1.0, "metaDiscussion": 0.0, "evidenceAuthority": 1.0},
            "governance": {"lifecycle": "canonical", "authorityRank": 95, "provenanceScore": 1.0},
            "provenance": {"sourceId": "lesson:2096"},
        }
        fused = fuse_ranked_passes(
            query=query,
            query_plan=query_plan,
            ranked_passes=[
                {"id": "baseline-hybrid", "query": query, "weight": 1.0, "ranked": {"results": [result], "filtered": 0}},
                {"id": "exact-entity-recall", "query": "2096 端口 不可用", "weight": 1.05, "ranked": {"results": [result], "filtered": 0}},
            ],
            total_indexed=1,
            top_k=1,
            reranker_policy={"enabled": False, "provider": "none"},
            language_profile="zh",
        )

        self.assertEqual(fused["quality"]["status"], "strong")
        self.assertIn("baseline-hybrid", fused["results"][0]["retrievalPasses"])
        self.assertIn("exact-entity-recall", fused["results"][0]["retrievalPasses"])
        self.assertIn("exact-entity-recall", fused["retrievalController"]["passesRun"])

    def test_retriever_marks_dense_only_low_coverage_recall_as_weak(self):
        ranked = rank_chunks(
            query="2096 端口为什么不可用？",
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "dense-only",
                    "text": "unrelated dashboard note",
                    "date": "2026-07-05",
                    "layer": "narrative",
                    "embedding": [1.0, 0.0],
                }
            ],
            top_k=1,
            similarity_weight=0.8,
            keyword_weight=0.2,
            recency_half_life_days=7,
            now=datetime(2026, 7, 7),
        )

        self.assertEqual(ranked["quality"]["status"], "weak")
        self.assertTrue(ranked["quality"]["needsMoreEvidence"])
        self.assertIn("2096", ranked["quality"]["missingTerms"])
        self.assertIn("retry-exact-missing-terms", ranked["quality"]["recommendations"][0])
        self.assertEqual(ranked["retrievalController"]["qualityStatus"], "weak")

    def test_retriever_requires_citable_anchor_entities_without_requiring_every_generic_term(self):
        query = "Session B release audit V1-A-029 SQLite WAL update rollback"
        unrelated = rank_chunks(
            query=query,
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "unrelated-packaging",
                    "text": "Session release audit update issue 029 for unrelated TokenClock packaging.",
                    "date": "2026-07-11",
                    "layer": "technical",
                    "embedding": [1.0, 0.0],
                }
            ],
            top_k=1,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            now=datetime(2026, 7, 11),
        )

        self.assertGreater(unrelated["quality"]["coverage"], 0.5)
        self.assertEqual(unrelated["quality"]["status"], "weak")
        self.assertTrue(unrelated["quality"]["needsMoreEvidence"])
        self.assertIn("v1-a-029", unrelated["quality"]["missingTerms"])
        self.assertIn("sqlite", unrelated["quality"]["missingTerms"])
        self.assertIn("wal", unrelated["quality"]["missingTerms"])

        anchor_evidence = rank_chunks(
            query=query,
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "wal-evidence",
                    "text": "V1-A-029 SQLite WAL rollback evidence.",
                    "date": "2026-07-11",
                    "layer": "technical",
                    "embedding": [1.0, 0.0],
                }
            ],
            top_k=1,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            now=datetime(2026, 7, 11),
        )

        self.assertEqual(anchor_evidence["quality"]["status"], "strong")
        self.assertFalse(anchor_evidence["quality"]["needsMoreEvidence"])
        self.assertIn("session", anchor_evidence["quality"]["missingTerms"])
        self.assertIn("release", anchor_evidence["quality"]["missingTerms"])

    def test_retriever_treats_paths_ids_and_short_uppercase_technical_terms_as_anchors(self):
        ranked = rank_chunks(
            query="Review src/actanara/update.py RC-204 SQL rollback",
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "generic-update",
                    "text": "Review src actanara update py issue 204 rollback.",
                    "date": "2026-07-11",
                    "layer": "technical",
                    "embedding": [1.0, 0.0],
                }
            ],
            top_k=1,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            now=datetime(2026, 7, 11),
        )

        self.assertGreaterEqual(ranked["quality"]["coverage"], 0.5)
        self.assertEqual(ranked["quality"]["status"], "weak")
        self.assertIn("src/actanara/update.py", ranked["quality"]["missingTerms"])
        self.assertIn("rc-204", ranked["quality"]["missingTerms"])
        self.assertIn("sql", ranked["quality"]["missingTerms"])

        uppercase_instruction = rank_chunks(
            query="WHY IS WAL unavailable",
            query_embedding=[1.0, 0.0],
            chunks=[
                {
                    "id": "wal-cause",
                    "text": "WAL is unavailable because the database is locked.",
                    "date": "2026-07-11",
                    "layer": "technical",
                    "embedding": [1.0, 0.0],
                }
            ],
            top_k=1,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            now=datetime(2026, 7, 11),
        )
        self.assertEqual(uppercase_instruction["quality"]["status"], "strong")
        self.assertNotIn("why", uppercase_instruction["quality"]["keyTerms"])
        self.assertNotIn("is", uppercase_instruction["quality"]["keyTerms"])

    def test_retriever_sanitizes_non_finite_dense_scores(self):
        ranked = rank_chunks(
            query="deploy",
            query_embedding=[float("nan"), 0.0],
            chunks=[
                {
                    "id": "finite-defense",
                    "text": "deploy rollback",
                    "date": "2026-06-05",
                    "embedding": [1.0, 0.0],
                }
            ],
            top_k=1,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
        )

        self.assertEqual(ranked["returned"], 1)
        self.assertEqual(ranked["results"][0]["scoreComponents"]["dense"], 0.0)

    def test_reranker_policy_is_disabled_by_default_and_never_reorders_without_provider(self):
        chunks = [
            {"id": "best", "text": "deploy rollback", "date": "2026-06-05", "embedding": [1.0, 0.0]},
            {"id": "second", "text": "deploy", "date": "2026-06-05", "embedding": [0.8, 0.6]},
        ]
        base = rank_chunks(
            query="deploy",
            query_embedding=[1.0, 0.0],
            chunks=chunks,
            top_k=2,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
        )
        self.assertEqual([item["id"] for item in base["results"]], ["best", "second"])
        self.assertEqual(base["reranker"]["status"], "disabled")
        self.assertEqual(base["reranker"]["reason"], "reranker-disabled")

        disabled = rank_chunks(
            query="deploy",
            query_embedding=[1.0, 0.0],
            chunks=chunks,
            top_k=2,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            reranker_policy={"enabled": False, "provider": "none"},
        )
        enabled_none = apply_reranker(
            query="deploy",
            ranked=base,
            policy={"enabled": True, "provider": "none"},
        )

        self.assertEqual([item["id"] for item in disabled["results"]], ["best", "second"])
        self.assertEqual([item["id"] for item in enabled_none["results"]], ["best", "second"])
        self.assertEqual(disabled["reranker"]["status"], "disabled")
        self.assertFalse(disabled["reranker"]["applied"])
        self.assertEqual(enabled_none["reranker"]["status"], "deferred")
        self.assertEqual(enabled_none["reranker"]["reason"], "provider-none-noop")

    def test_local_score_reranker_is_explicit_and_uses_existing_score_components(self):
        chunks = [
            {
                "id": "dense-only",
                "text": "unrelated archive note",
                "date": "2026-06-05",
                "embedding": [1.0, 0.0],
            },
            {
                "id": "keyword-intent",
                "text": "deploy rollback task progress",
                "date": "2026-06-05",
                "layer": "task",
                "embedding": [0.95, 0.05],
            },
        ]
        ranked = rank_chunks(
            query="deploy task",
            query_embedding=[1.0, 0.0],
            chunks=chunks,
            top_k=2,
            similarity_weight=0.95,
            keyword_weight=0.05,
            recency_half_life_days=7,
            reranker_policy={"enabled": True, "provider": "local-score"},
            now=datetime(2026, 6, 5),
        )

        self.assertEqual(ranked["reranker"]["status"], "applied")
        self.assertTrue(ranked["reranker"]["applied"])
        self.assertEqual(ranked["results"][0]["id"], "keyword-intent")
        self.assertIn("rerankerLocalScore", ranked["results"][0]["scoreComponents"])

    def test_local_score_reranker_prefers_exact_authoritative_evidence(self):
        ranked = apply_reranker(
            query="2096 端口不可用",
            ranked={
                "results": [
                    {
                        "id": "high-base-weak-evidence",
                        "score": 0.9,
                        "textPreview": "Generic VPS troubleshooting archive.",
                        "scoreComponents": {"keyword": 0.0, "recency": 0.8, "intentBoost": 1.0},
                        "governance": {"lifecycle": "episodic", "authorityRank": 70, "provenanceScore": 0.5},
                    },
                    {
                        "id": "exact-authoritative",
                        "score": 0.78,
                        "textPreview": "2096 端口不可用 because the target environment blocks that port.",
                        "scoreComponents": {"keyword": 1.0, "recency": 0.8, "intentBoost": 1.08},
                        "governance": {"lifecycle": "canonical", "authorityRank": 95, "provenanceScore": 1.0},
                    },
                ]
            },
            policy={"enabled": True, "provider": "local-score"},
        )

        self.assertEqual(ranked["results"][0]["id"], "exact-authoritative")
        components = ranked["results"][0]["scoreComponents"]
        self.assertGreater(components["rerankerTermOverlap"], 0.0)
        self.assertEqual(components["rerankerProvenance"], 1.0)
        self.assertGreater(components["rerankerLifecycle"], 0.9)

    def test_reranker_provider_rejects_unapproved_external_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"retrieval": {"reranker": {"enabled": True, "provider": "external"}}}}, paths)
            with self.assertRaises(ValueError):
                resolve_rag_settings(paths)

    def test_v2_shadow_build_initializes_candidate_without_touching_legacy_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text("legacy\n", encoding="utf-8")
            before = index.stat().st_mtime_ns
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            settings = resolve_rag_settings(paths)
            result = initialize_shadow_build(settings, requested_by="test")
            after = index.stat().st_mtime_ns
            store = settings.v2_store_path
            self.assertEqual(before, after)
            self.assertTrue((store / "config.json").exists())
            self.assertTrue((store / "manifest.json").exists())
            self.assertTrue((store / "build-runs.jsonl").exists())
            self.assertTrue(Path(result["candidateManifest"]).exists())
            manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "candidate-initialized")
            self.assertEqual(manifest["lastBuildRunId"], result["run"]["runId"])
            status = read_rag_status(settings=settings, count_legacy_entries=False)
            self.assertEqual(status["v2"]["latestBuildRun"]["runId"], result["run"]["runId"])
            self.assertFalse(result["run"]["activePromotionAllowed"])

    def test_candidate_promotion_requires_ready_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            settings = resolve_rag_settings(paths)
            result = initialize_shadow_build(settings, requested_by="test")
            with self.assertRaises(ValueError):
                promote_candidate(settings, result["run"]["runId"])

    def test_v2_candidate_index_builds_ready_files_without_touching_legacy_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            raw_dir = root / "Actanara" / "artifacts" / "diary" / "__diary_daily" / "2026-06-05" / "_filtered" / "codex"
            raw_dir.mkdir(parents=True)
            (raw_dir / "messages.jsonl").write_text(
                json.dumps(
                    {
                        "role": "assistant",
                        "time": "16:00",
                        "content": "Implemented v2 candidate indexing with deterministic source metadata.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lessons_path = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons_path.parent.mkdir(parents=True)
            lessons_path.write_text(
                json.dumps({"id": "lesson-1", "text": "Always validate candidate indexes before promotion.", "date": "2026-06-05", "agent": "codex"})
                + "\n",
                encoding="utf-8",
            )
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text("legacy\n", encoding="utf-8")
            before = index.stat().st_mtime_ns
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings(
                {"rag": {"embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2, "batchSize": 2}}},
                paths,
            )
            settings = resolve_rag_settings(paths)
            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
                source_sets=["filtered-dialogue-daily", "lessons"],
            )
            after = index.stat().st_mtime_ns
            self.assertEqual(before, after)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["manifest"]["chunkCount"], 2)
            self.assertEqual(result["manifest"]["embeddingCount"], 2)
            self.assertEqual(result["manifest"]["dimensionMismatchCount"], 0)
            self.assertTrue(Path(result["candidateIndex"]).exists())
            self.assertTrue(Path(result["chunksPath"]).exists())
            self.assertTrue(Path(result["embeddingsPath"]).exists())
            self.assertTrue(Path(result["sourcesPath"]).exists())
            self.assertTrue(Path(result["buildReportPath"]).exists())
            self.assertFalse((settings.v2_store_path / "indexes" / "active" / "manifest.json").exists())
            root_manifest = json.loads((settings.v2_store_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["status"], "candidate-ready")
            chunk = json.loads(Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(chunk["sourceSet"], "filtered-dialogue-daily")
            self.assertEqual(chunk["sourceType"], "filtered-dialogue-jsonl")
            self.assertIn("sourceId", chunk)
            self.assertIn("dedupeKey", chunk)
            self.assertIn("textHash", chunk)
            self.assertIn("workType", chunk)
            source = json.loads(Path(result["sourcesPath"]).read_text(encoding="utf-8").splitlines()[0])
            self.assertIn("fingerprint", source)
            self.assertIn("governance", chunk)
            self.assertIn("governance", source)
            self.assertEqual(chunk["governance"]["sourceSet"], "filtered-dialogue-daily")
            self.assertIn("provenanceScore", chunk["governance"])

    def test_default_v2_candidate_sources_use_filtered_and_foundation_records_not_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            current_diary = root / "Actanara" / "artifacts" / "diary"
            filtered_dir = current_diary / "__diary_daily" / "2026-06-05" / "_filtered" / "codex"
            filtered_dir.mkdir(parents=True)
            (filtered_dir / "one.jsonl").write_text(
                json.dumps({"role": "user", "time": "10:00", "content": "继续 RAG coverage redesign"}) + "\n"
                + json.dumps({"role": "assistant", "time": "10:05", "content": "完成 filtered source collector"}) + "\n",
                encoding="utf-8",
            )
            raw_dir = current_diary / "__diary_daily" / "2026-06-05" / "codex"
            raw_dir.mkdir(parents=True)
            (raw_dir / "raw.jsonl").write_text(
                json.dumps({"content": "raw noisy tool payload should not be indexed by default", "timestamp": "2026-06-05T08:00:00Z"})
                + "\n",
                encoding="utf-8",
            )
            lessons_path = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons_path.parent.mkdir(parents=True)
            lessons_path.write_text(
                json.dumps({"id": "lesson-1", "text": "Prefer cleaned and structured RAG sources.", "date": "2026-06-05", "agent": "codex"})
                + "\n",
                encoding="utf-8",
            )
            day = current_diary / "diary-2026-06-05"
            day.mkdir()
            (day / "日记-260605.md").write_text(
                "# 2026年06月05日 日记\n\n## 今日概要\nRAG coverage redesign。\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            paths.task_board_path.parent.mkdir(parents=True, exist_ok=True)
            paths.task_board_path.write_text(
                "# TASK BOARD\n\n## Active\n### Actanara\n- [ ] RAG coverage redesign ← **@codex**\n",
                encoding="utf-8",
            )
            migrate(paths)
            run_id = begin_ingestion_run(paths, trigger_type="test-rag-source", business_date=datetime(2026, 6, 5).date())
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO tool_sources(tool_key, display_name, adapter_version, capabilities_json, enabled, created_at, updated_at)
                    VALUES ('codex', 'Codex', 'test', '{}', 1, '2026-06-05T00:00:00+08:00', '2026-06-05T00:00:00+08:00')
                    """,
                )
                connection.execute(
                    """
                    INSERT INTO daily_tool_usage(business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id)
                    VALUES ('2026-06-05', 'codex', 100, 2, 1, 2, ?)
                    """,
                    (run_id,),
                )
                connection.execute(
                    """
                    INSERT INTO dashboard_snapshots(snapshot_key, projection_type, payload_json, generated_at, source_run_id, status)
                    VALUES ('diary:memory-stats:2026-06-05:non-rag', 'legacy-diary-memory-stats-v1', ?, '2026-06-05T12:00:00+08:00', ?, 'ready')
                    """,
                    (json.dumps({"sessionFiles": 1, "totalSizeMB": 0.1}), run_id),
                )
                connection.execute(
                    """
                    INSERT INTO period_reports(report_key, period_type, start_date, end_date, projection_type, metrics_json, generated_at, source_run_id, status)
                    VALUES ('diary-period-summary-v1:2026-06-01:2026-06-07', 'week', '2026-06-01', '2026-06-07', 'diary-period-summary-v1', ?, '2026-06-07T12:00:00+08:00', ?, 'ready')
                    """,
                    (json.dumps({"summary": "RAG coverage changed to filtered and Foundation sources."}), run_id),
                )
            write_settings({"rag": {"embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            chunks = [json.loads(line) for line in Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()]
            source_sets = {chunk["sourceSet"] for chunk in chunks}
            self.assertIn("filtered-dialogue-daily", source_sets)
            self.assertIn("foundation-usage-rollups", source_sets)
            self.assertIn("foundation-dashboard-snapshots", source_sets)
            self.assertIn("foundation-period-projections", source_sets)
            self.assertIn("diary-markdown-sections", source_sets)
            self.assertIn("task-board-snapshot", source_sets)
            self.assertIn("lessons", source_sets)
            self.assertNotIn("legacy-diary-daily", source_sets)
            filtered = next(chunk for chunk in chunks if chunk["sourceSet"] == "filtered-dialogue-daily")
            self.assertEqual(filtered["agent"], "codex")
            self.assertEqual(filtered["date"], "2026-06-05")
            self.assertEqual(filtered["provenance"]["role"], "user")

    def test_v2_coverage_reports_pipeline_aligned_paths_and_active_source_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            current_diary = root / "Actanara" / "artifacts" / "diary"
            filtered = current_diary / "__diary_daily" / "2026-06-05" / "_filtered" / "codex"
            filtered.mkdir(parents=True)
            (filtered / "one.jsonl").write_text(json.dumps({"content": "cleaned dialogue"}) + "\n", encoding="utf-8")
            missing_index_filtered = current_diary / "__diary_daily" / "2026-06-06" / "_filtered" / "codex"
            missing_index_filtered.mkdir(parents=True)
            (missing_index_filtered / "one.jsonl").write_text(json.dumps({"content": "cleaned dialogue missing from index"}) + "\n", encoding="utf-8")
            day = current_diary / "diary-2026-06-05"
            day.mkdir(parents=True)
            (day / "日记-260605.md").write_text("# 日记\n\n## 今日概要\nRAG coverage。\n", encoding="utf-8")
            missing_index_day = current_diary / "diary-2026-06-06"
            missing_index_day.mkdir(parents=True)
            (missing_index_day / "日记-260606.md").write_text("# 日记\n\n## 今日概要\nRAG coverage missing index。\n", encoding="utf-8")
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            migrate(paths)
            run_id = begin_ingestion_run(paths, trigger_type="test-rag-coverage", business_date=datetime(2026, 6, 6).date())
            with connect(paths) as connection:
                connection.execute(
                    """
                    INSERT INTO tool_sources(tool_key, display_name, adapter_version, capabilities_json, enabled, created_at, updated_at)
                    VALUES ('codex', 'Codex', 'test', '{}', 1, '2026-06-06T00:00:00+08:00', '2026-06-06T00:00:00+08:00')
                    """,
                )
                connection.execute(
                    """
                    INSERT INTO daily_tool_usage(business_date, tool_key, tokens, messages, sessions, api_calls, source_run_id)
                    VALUES ('2026-06-06', 'codex', 123, 4, 1, 2, ?)
                    """,
                    (run_id,),
                )
            settings = resolve_rag_settings(paths)
            sources = settings.v2_store_path / "indexes" / "active" / "run-1" / "sources.jsonl"
            sources.parent.mkdir(parents=True)
            sources.write_text(
                json.dumps(
                    {
                        "sourceSet": "filtered-dialogue-daily",
                        "path": str(filtered / "one.jsonl"),
                        "chunkCount": 1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (settings.v2_store_path / "manifest.json").write_text(
                json.dumps({"status": "active", "activeRunId": "run-1", "sourcesPath": str(sources)}),
                encoding="utf-8",
            )

            coverage = read_v2_coverage(settings)
            by_set = {item["sourceSet"]: item for item in coverage["sourceSets"]}
            self.assertEqual(coverage["schemaVersion"], 1)
            self.assertIn("__diary_daily", coverage["paths"]["filteredDialoguePattern"])
            self.assertIn("_filtered", coverage["paths"]["filteredDialoguePattern"])
            self.assertEqual(by_set["filtered-dialogue-daily"]["coverageStatus"], "covered")
            self.assertTrue(by_set["filtered-dialogue-daily"]["expected"]["pathAlignedWithPipeline"])
            self.assertGreaterEqual(by_set["diary-markdown-sections"]["discoveredSourceCount"], 1)
            self.assertTrue(coverage["dateCoverage"]["summary"]["recommendRagSync"])
            self.assertIn("2026-06-06", coverage["dateCoverage"]["onlyMissingRagIndexDates"])
            date_rows = {item["date"]: item for item in coverage["dateCoverage"]["dates"]}
            self.assertEqual(date_rows["2026-06-06"]["recommendedAction"], "run-rag-sync")
            self.assertEqual(date_rows["2026-06-06"]["upstreamStatus"], "complete")
            self.assertFalse(coverage["mutationPolicy"]["v2StoreMutated"])

    def test_v2_candidate_incremental_build_reuses_active_embeddings_for_unchanged_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            lessons = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True)
            lessons.write_text(
                json.dumps({"id": "lesson-1", "text": "Reuse stable embeddings for unchanged memories.", "date": "2026-06-05", "agent": "codex"})
                + "\n",
                encoding="utf-8",
            )
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(
                json.dumps({"id": "legacy", "text": "Reuse stable embeddings for unchanged memories.", "embedding": [1.0, 0.0]}) + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            first = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            first_run_id = first["run"]["runId"]
            promote_v2_candidate(
                settings,
                run_id=first_run_id,
                confirm=True,
                confirmation_text=required_v2_promotion_confirmation(first_run_id),
                requested_by="test",
            )

            def fail_if_called(texts):
                raise AssertionError(f"unchanged chunks should reuse active embeddings, got {texts!r}")

            second = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=fail_if_called,
            )
            indexed = json.loads(Path(second["candidateIndex"]).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(second["manifest"]["embeddingCount"], 1)
            self.assertEqual(second["manifest"]["reusedEmbeddingCount"], 1)
            self.assertEqual(second["manifest"]["generatedEmbeddingCount"], 0)
            self.assertEqual(second["manifest"]["incremental"]["mode"], "active-embedding-reuse")
            self.assertEqual(indexed["embedding"], [1.0, 0.0])

    def test_v2_candidate_reuses_embeddings_after_diary_root_relocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_diary = root / "DiaryA"
            filtered = old_diary / "__diary_daily" / "2026-06-05" / "_filtered" / "codex"
            filtered.mkdir(parents=True)
            (filtered / "one.jsonl").write_text(
                json.dumps({"content": "Relocated diary source should keep the same vector identity.", "role": "user"})
                + "\n",
                encoding="utf-8",
            )
            legacy_index = old_diary / "__diary_rag" / "index.jsonl"
            legacy_index.parent.mkdir(parents=True, exist_ok=True)
            legacy_index.write_text(
                json.dumps(
                    {
                        "id": "legacy",
                        "text": "Relocated diary source should keep the same vector identity.",
                        "model": "all-MiniLM-L6-v2",
                        "embedding": [1.0, 0.0],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=old_diary)
            paths = update_runtime_manifest_paths(paths.home, generated_diary_root=old_diary, legacy_diary_root=old_diary)
            write_settings({"rag": {"embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            first = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["filtered-dialogue-daily"],
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

            new_diary = root / "DiaryB"
            new_filtered = new_diary / "__diary_daily" / "2026-06-05" / "_filtered" / "codex"
            new_filtered.mkdir(parents=True)
            (new_filtered / "one.jsonl").write_text((filtered / "one.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            relocated_settings = replace(settings, diary_source_root=new_diary)

            def fail_if_called(texts):
                raise AssertionError(f"relocated unchanged chunks should reuse active embeddings, got {texts!r}")

            second = build_v2_candidate_index(
                relocated_settings,
                requested_by="test",
                source_sets=["filtered-dialogue-daily"],
                embedding_fn=fail_if_called,
            )
            indexed = json.loads(Path(second["candidateIndex"]).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(second["manifest"]["embeddingCount"], 1)
            self.assertEqual(second["manifest"]["reusedEmbeddingCount"], 1)
            self.assertEqual(second["manifest"]["generatedEmbeddingCount"], 0)
            self.assertEqual(indexed["embedding"], [1.0, 0.0])
            self.assertEqual(second["manifest"]["sourceProfile"]["diarySourceRoot"], str(new_diary))
            self.assertEqual(indexed["sourcePath"], str(new_filtered / "one.jsonl"))

    def test_v2_production_sync_promotes_candidate_without_legacy_index_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            lessons = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True)
            lessons.write_text(
                json.dumps(
                    {
                        "id": "lesson-1",
                        "text": "RAG v2 production sync does not require the retired legacy index.",
                        "date": "2026-06-06",
                        "agent": "codex",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "indexing": {"sourceSets": ["lessons"]},
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)
            result = sync_v2_production_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )

            self.assertEqual(result["status"], "promoted")
            self.assertEqual(result["gates"]["status"], "passed")
            self.assertFalse(result["mutationPolicy"]["legacyMutated"])
            self.assertFalse(result["mutationPolicy"]["legacyReadRequired"])
            self.assertTrue(result["mutationPolicy"]["activeSnapshotPromoted"])
            self.assertEqual(result["promotion"]["status"], "promoted")
            root_manifest = json.loads((settings.v2_store_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["status"], "active")
            active = resolve_active_rag_index(settings)
            self.assertTrue(active.ready)

    def test_v2_production_sync_can_build_candidate_without_promoting_when_explicitly_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            lessons = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True)
            lessons.write_text(
                json.dumps(
                    {
                        "id": "lesson-1",
                        "text": "Explicit candidate-only nova-RAG production sync remains available.",
                        "date": "2026-06-06",
                        "agent": "codex",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "indexing": {"sourceSets": ["lessons"]},
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)
            result = sync_v2_production_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
                promote=False,
            )

            self.assertEqual(result["status"], "candidate-ready")
            self.assertFalse(result["mutationPolicy"]["activeSnapshotPromoted"])
            self.assertIsNone(result["promotion"])
            root_manifest = json.loads((settings.v2_store_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["status"], "candidate-ready")
            active = resolve_active_rag_index(settings)
            self.assertFalse(active.ready)

    def test_v2_candidate_only_sync_does_not_prune_current_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True)
            lessons.write_text(
                json.dumps({"id": "lesson-1", "text": "Candidate-only sync should keep current active.", "date": "2026-06-06"})
                + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "indexing": {"sourceSets": ["lessons"]},
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)
            promoted = sync_v2_production_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            active_index = Path(promoted["promotion"]["activeIndexPath"])
            self.assertTrue(active_index.exists())

            candidate_only = sync_v2_production_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[0.0, 1.0] for _ in texts],
                promote=False,
            )

            self.assertEqual(candidate_only["status"], "candidate-ready")
            self.assertTrue(active_index.exists())
            self.assertTrue(Path(candidate_only["build"]["candidateIndex"]).exists())
            active = resolve_active_rag_index(settings)
            self.assertTrue(active.ready)
            self.assertEqual(active.index_path, active_index)

    def test_v2_production_sync_rejects_concurrent_sync_promote_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "indexing": {"sourceSets": ["lessons"]},
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)

            with (
                rag_v2_operation_lock(settings, operation="test-held"),
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index") as build,
            ):
                result = sync_v2_production_index(
                    settings,
                    requested_by="test",
                    embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
                    promote=False,
                )

        self.assertEqual(result["status"], "blocked")
        self.assertIn("already running", result["reason"])
        self.assertTrue(result["singleFlight"]["locked"])
        self.assertEqual(result["singleFlight"]["operation"], "sync-candidate")
        build.assert_not_called()

    def test_v2_production_sync_plan_uses_backend_contract_without_building(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "indexing": {"enabled": True, "sourceSets": ["lessons"]},
                        "embedding": {"model": "intfloat/multilingual-e5-small", "dimension": 384},
                        "server": {"enabled": True},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)

            with (
                patch(
                    "agentic_rag.rag_v2_sync.read_server_process_state",
                    return_value={
                        "status": "healthy",
                        "health": {
                            "healthy": True,
                            "payload": {
                                "model": "intfloat/multilingual-e5-small",
                                "dimension": 384,
                                "provider": "local",
                            },
                        },
                    },
                ) as lifecycle,
                patch("agentic_rag.rag_v2_sync.read_rag_internal_token", return_value="internal-test-token"),
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index") as build,
                patch("agentic_rag.rag_v2_sync.promote_v2_candidate") as promote,
            ):
                plan = plan_v2_production_sync(
                    settings,
                    action="rag-update",
                    requested_by="actanara-cli-rag-update",
                    confirmation_text="UPDATE AND PROMOTE ACTANARA RAG",
                )

            self.assertEqual(plan["status"], "plan")
            self.assertTrue(plan["canExecute"])
            self.assertEqual(plan["backend"], "agentic_rag.rag_v2_sync.sync_v2_production_index")
            self.assertEqual(plan["executionModel"], "candidate-build-validate-promote")
            self.assertEqual(plan["indexing"]["buildScope"], "full-candidate-snapshot")
            self.assertEqual(plan["indexing"]["embeddingReuse"], "active-embedding-reuse")
            self.assertEqual(plan["plannedCall"]["embeddingSource"], "server")
            self.assertEqual(plan["singleFlight"]["lockPath"], str(settings.v2_store_path / "locks" / "sync-promote.lock"))
            self.assertEqual(plan["confirmationTextRequired"], "UPDATE AND PROMOTE ACTANARA RAG")
            self.assertFalse(plan["mutationPolicy"]["candidateBuilt"])
            self.assertTrue(plan["wouldMutateOnConfirm"]["candidateBuilt"])
            self.assertTrue(plan["wouldMutateOnConfirm"]["activeSnapshotPromoted"])
            lifecycle.assert_called_once_with(settings, probe_health=True, timeout_seconds=2.0)
            build.assert_not_called()
            promote.assert_not_called()

    def test_v2_sync_and_plan_refuse_nonloopback_before_lock_or_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            settings = resolve_rag_settings(
                paths,
                settings={
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "indexing": {"enabled": True, "sourceSets": ["lessons"]},
                        "server": {"enabled": True, "host": "192.0.2.10"},
                    }
                },
            )
            with (
                patch("agentic_rag.rag_v2_sync.rag_v2_operation_lock") as lock,
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index") as build,
                patch("agentic_rag.rag_v2_sync.read_server_process_state") as lifecycle,
            ):
                result = sync_v2_production_index(
                    settings,
                    embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
                )
                plan = plan_v2_production_sync(settings)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "rag-server-non-loopback")
            self.assertFalse(result["mutationPolicy"]["candidateBuilt"])
            self.assertFalse(plan["canExecute"])
            self.assertIn("rag-server-non-loopback", {item["code"] for item in plan["blockers"]})
            lock.assert_not_called()
            build.assert_not_called()
            lifecycle.assert_not_called()

    def test_v2_sync_ready_server_without_internal_token_is_stably_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "indexing": {"enabled": True, "sourceSets": ["lessons"]},
                        "server": {"enabled": True},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)
            with (
                patch(
                    "agentic_rag.rag_v2_sync.read_server_process_state",
                    return_value={"status": "healthy", "health": {"healthy": True}},
                ),
                patch("agentic_rag.rag_v2_sync.read_rag_internal_token", return_value=""),
                patch("agentic_rag.rag_v2_sync.urllib.request.urlopen") as urlopen,
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index") as build,
            ):
                result = sync_v2_production_index(settings, server_wait_timeout_seconds=0)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "rag-internal-authorization-unavailable")
            self.assertFalse(result["mutationPolicy"]["candidateBuilt"])
            urlopen.assert_not_called()
            build.assert_not_called()

    def test_v2_production_sync_plan_reports_server_blocker_without_building(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "indexing": {"enabled": True, "sourceSets": ["lessons"]},
                        "server": {"enabled": True},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)

            with (
                patch(
                    "agentic_rag.rag_v2_sync.read_server_process_state",
                    return_value={"status": "stopped", "health": {"healthy": False}},
                ),
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index") as build,
            ):
                plan = plan_v2_production_sync(settings, action="rag-rebuild")

            self.assertEqual(plan["status"], "plan")
            self.assertFalse(plan["canExecute"])
            self.assertEqual(plan["blockers"][0]["code"], "server-not-ready")
            self.assertFalse(plan["wouldMutateOnConfirm"]["candidateBuilt"])
            self.assertFalse(plan["wouldMutateOnConfirm"]["activeSnapshotPromoted"])
            build.assert_not_called()

    def test_v2_production_sync_skips_when_nova_rag_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"mode": "disabled"}}, paths)
            settings = resolve_rag_settings(paths)

            result = sync_v2_production_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: self.fail("disabled nova-RAG should not build a candidate"),
            )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "nova-RAG mode is disabled by settings.")
            self.assertFalse(result["mutationPolicy"]["candidateBuilt"])
            self.assertFalse(result["mutationPolicy"]["activeSnapshotPromoted"])

    def test_v2_sync_cli_returns_nonzero_when_sync_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"mode": "disabled"}}, paths)
            with (
                patch.dict(os.environ, {"ACTANARA_HOME": str(paths.home)}, clear=False),
                patch("sys.stdout", new=io.StringIO()),
            ):
                self.assertEqual(rag_v2_sync_main(["--no-promote"]), 1)

    def test_v2_production_sync_requires_ready_server_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "indexing": {"enabled": True, "sourceSets": ["lessons"]},
                        "server": {"enabled": True},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)

            with (
                patch(
                    "agentic_rag.rag_v2_sync.read_server_process_state",
                    return_value={"status": "stopped", "health": {"healthy": False}},
                ) as lifecycle,
                patch("agentic_rag.rag_v2_sync.time.sleep"),
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index") as build,
            ):
                result = sync_v2_production_index(settings, requested_by="test", server_wait_timeout_seconds=0)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "nova-RAG server is not ready for candidate indexing.")
            self.assertEqual(result["gates"]["status"], "blocked")
            self.assertEqual(result["embeddingSource"], "server")
            self.assertFalse(result["mutationPolicy"]["candidateBuilt"])
            lifecycle.assert_called_once_with(settings, probe_health=True, timeout_seconds=2.0)
            build.assert_not_called()

    def test_v2_production_sync_retries_transient_server_health_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "indexing": {"enabled": True, "sourceSets": ["lessons"]},
                        "server": {"enabled": True, "host": "127.0.0.1", "port": 3037},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)
            build_result = {
                "run": {"runId": "candidate-1"},
                "manifest": {
                    "status": "ready",
                    "chunkCount": 1,
                    "embeddingCount": 1,
                    "dimensionMismatchCount": 0,
                    "sourceSets": ["lessons"],
                },
            }

            with (
                patch(
                    "agentic_rag.rag_v2_sync.read_server_process_state",
                    side_effect=[
                        {"status": "running", "health": {"healthy": False, "error": "TimeoutError"}},
                        {"status": "healthy", "health": {"healthy": True, "payload": {"model": settings.embedding_model, "dimension": settings.embedding_dimension, "provider": settings.embedding_provider}}},
                    ],
                ) as lifecycle,
                patch("agentic_rag.rag_v2_sync.time.sleep") as sleep,
                patch("agentic_rag.rag_v2_sync.read_rag_internal_token", return_value="internal-test-token"),
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index", return_value=build_result),
                patch("agentic_rag.rag_v2_sync.promote_v2_candidate", return_value={"status": "promoted"}),
            ):
                result = sync_v2_production_index(settings, requested_by="test", server_wait_timeout_seconds=5)

            self.assertEqual(result["status"], "promoted")
            self.assertEqual(lifecycle.call_count, 2)
            lifecycle.assert_any_call(settings, probe_health=True, timeout_seconds=2.0)
            sleep.assert_called_once()

    def test_v2_production_sync_uses_server_embedding_when_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "indexing": {"enabled": True, "sourceSets": ["lessons"]},
                        "server": {"enabled": True, "host": "127.0.0.1", "port": 3037},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)

            def fake_build(build_settings, *, requested_by, embedding_fn, **_kwargs):
                self.assertIs(build_settings, settings)
                self.assertEqual(requested_by, "test")
                self.assertEqual(embedding_fn(["hello"]), [[1.0, 0.0]])
                return {
                    "manifest": {
                        "status": "ready",
                        "chunkCount": 1,
                        "embeddingCount": 1,
                        "dimensionMismatchCount": 0,
                        "sourceSets": ["lessons"],
                    },
                    "run": {"runId": "run-1"},
                }

            class FakeResponse:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return json.dumps([[1.0, 0.0]]).encode("utf-8")

            captured = {}

            def fake_urlopen(request, timeout=0):
                captured["url"] = request.full_url
                captured["timeout"] = timeout
                captured["payload"] = json.loads(request.data.decode("utf-8"))
                captured["token"] = request.headers.get("X-actanara-rag-internal-token")
                return FakeResponse()

            with (
                patch(
                    "agentic_rag.rag_v2_sync.read_server_process_state",
                    return_value={"status": "healthy", "health": {"healthy": True}},
                ),
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index", side_effect=fake_build),
                patch("agentic_rag.rag_v2_sync.read_rag_internal_token", return_value="internal-test-token"),
                patch("agentic_rag.rag_v2_sync.urllib.request.urlopen", side_effect=fake_urlopen),
            ):
                result = sync_v2_production_index(settings, requested_by="test", promote=False)

            self.assertEqual(result["status"], "candidate-ready")
            self.assertEqual(result["embeddingSource"], "server")
            self.assertEqual(captured["url"], "http://127.0.0.1:3037/encode")
            self.assertEqual(captured["payload"], {"texts": ["hello"]})
            self.assertEqual(captured["token"], "internal-test-token")

    def test_v2_production_sync_skips_when_server_profile_mismatches_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "enabled": True,
                        "mode": "v2",
                        "indexing": {"enabled": True, "sourceSets": ["lessons"]},
                        "embedding": {"model": "intfloat/multilingual-e5-small", "dimension": 384},
                        "server": {"enabled": True},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)

            with (
                patch(
                    "agentic_rag.rag_v2_sync.read_server_process_state",
                    return_value={
                        "status": "healthy",
                        "health": {
                            "healthy": True,
                            "payload": {"model": "all-MiniLM-L6-v2", "dimension": 384, "provider": "local"},
                        },
                    },
                ),
                patch("agentic_rag.rag_v2_sync.build_v2_candidate_index") as build,
            ):
                result = sync_v2_production_index(settings, requested_by="test")

            self.assertEqual(result["status"], "blocked")
            self.assertIn("server model mismatch", result["reason"])
            self.assertEqual(result["gates"]["status"], "blocked")
            build.assert_not_called()

    def test_v2_candidate_index_requires_explicit_embedding_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            lessons = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True)
            lessons.write_text(
                json.dumps({"id": "lesson-1", "text": "Explicit embedding function is required.", "date": "2026-06-06"}) + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "indexing": {"sourceSets": ["lessons"]},
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)

            with self.assertRaisesRegex(ValueError, "embedding_fn is required"):
                build_v2_candidate_index(settings, requested_by="test", source_sets=["lessons"])

    def test_v2_production_sync_blocks_when_server_dependency_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            settings = resolve_rag_settings(paths)
            with patch(
                "agentic_rag.rag_v2_sync.read_server_process_state",
                return_value={"healthy": False, "health": {"healthy": False}},
            ):
                result = sync_v2_production_index(settings, requested_by="test")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "nova-RAG server is not ready for candidate indexing.")
            self.assertFalse(result["mutationPolicy"]["candidateBuilt"])
            self.assertFalse(result["mutationPolicy"]["activeSnapshotPromoted"])
            self.assertFalse(result["mutationPolicy"]["serverLifecycleChanged"])

    def test_v2_candidate_dimension_mismatch_is_partial_and_not_promotable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            raw_dir = root / "Actanara" / "artifacts" / "diary" / "__diary_daily" / "2026-06-05" / "_filtered" / "codex"
            raw_dir.mkdir(parents=True)
            (raw_dir / "messages.jsonl").write_text(
                json.dumps({"content": "This chunk has enough content for candidate indexing.", "timestamp": "2026-06-05T08:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"embedding": {"dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0, 0.0] for _ in texts],
                source_sets=["filtered-dialogue-daily"],
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["manifest"]["embeddingCount"], 0)
            self.assertEqual(result["manifest"]["dimensionMismatchCount"], 1)
            self.assertFalse(result["manifest"]["activePromotionAllowed"])
            with self.assertRaises(ValueError):
                promote_candidate(settings, result["run"]["runId"])

    def test_v2_candidate_index_rejects_retired_legacy_daily_source_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            settings = resolve_rag_settings(paths)

            with self.assertRaisesRegex(ValueError, "retired RAG sourceSets"):
                build_v2_candidate_index(
                    settings,
                    requested_by="test",
                    embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
                    source_sets=["legacy-diary-daily"],
                )

    def test_second_tier_sources_are_candidate_only_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            day = root / "Actanara" / "artifacts" / "diary" / "diary-2026-06-05"
            day.mkdir(parents=True)
            (day / "日记-260605.md").write_text(
                """# 2026年06月05日 日记

## 今日概要
完成 RAG second-tier source expansion。

```json
{"date": "2026-06-05", "metrics": {"total": 1}, "cronTasks": []}
```
""",
                encoding="utf-8",
            )
            (day / "技术进展-260605.md").write_text(
                """# 2026-06-05 技术进展报告

## 二、任务更新

date: 2026-06-05
task_updates:
  - id: T-RAG-8B
    parent_id: actanara
    title: RAG second tier source expansion
    status: InProgress
    progress_delta: 20
""",
                encoding="utf-8",
            )
            (legacy / "TASK_BOARD.md").parent.mkdir(parents=True, exist_ok=True)
            (legacy / "TASK_BOARD.md").write_text(
                """# TASK BOARD

## 🔵 Active
### Actanara
- [ ] [T-RAG-8B] RAG second tier source expansion ← **@codex**
""",
                encoding="utf-8",
            )
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text("legacy\n", encoding="utf-8")
            before = index.stat().st_mtime_ns
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            paths.task_board_path.parent.mkdir(parents=True, exist_ok=True)
            paths.task_board_path.write_text(
                """# TASK BOARD

## 🔵 Active
### Actanara
- [ ] [T-RAG-8B] RAG second tier source expansion ← **@codex**
""",
                encoding="utf-8",
            )
            settings = resolve_rag_settings(paths)
            self.assertNotIn("legacy-diary-daily", settings.indexing_source_sets)
            self.assertIn("diary-markdown-sections", settings.indexing_source_sets)

            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=[
                    "diary-markdown-sections",
                    "diary-markdown-embedded-json",
                    "technical-report-task-events",
                    "task-board-snapshot",
                ],
                embedding_fn=lambda texts: [[0.1] * settings.embedding_dimension for _ in texts],
            )
            after = index.stat().st_mtime_ns
            self.assertEqual(before, after)
            self.assertEqual(result["status"], "ready")
            self.assertFalse((settings.v2_store_path / "indexes" / "active" / "manifest.json").exists())
            chunks = [
                json.loads(line)
                for line in Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            source_sets = {chunk["sourceSet"] for chunk in chunks}
            self.assertEqual(
                source_sets,
                {
                    "diary-markdown-sections",
                    "diary-markdown-embedded-json",
                    "technical-report-task-events",
                    "task-board-snapshot",
                },
            )
            section_chunk = next(chunk for chunk in chunks if chunk["sourceSet"] == "diary-markdown-sections")
            self.assertNotIn("```json", section_chunk["text"])
            embedded_chunk = next(chunk for chunk in chunks if chunk["sourceSet"] == "diary-markdown-embedded-json")
            self.assertEqual(
                embedded_chunk["provenance"]["topLevelKeys"],
                ["cronTasks", "date", "metrics"],
            )
            self.assertIn("top-level keys", embedded_chunk["text"])
            technical_chunk = next(chunk for chunk in chunks if chunk["sourceSet"] == "technical-report-task-events")
            self.assertIn("historical observations", technical_chunk["provenance"]["authority"])
            board_chunk = next(
                chunk
                for chunk in chunks
                if chunk["sourceSet"] == "task-board-snapshot" and chunk["provenance"]["recordType"] == "task-item"
            )
            self.assertIn("Nova-Task v2 SQLite authority", board_chunk["provenance"]["authority"])
            self.assertIn("TASK_BOARD.md projection", board_chunk["provenance"]["authority"])

    def test_v2_candidate_index_parses_current_nova_task_projection_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            paths.task_board_path.parent.mkdir(parents=True, exist_ok=True)
            paths.task_board_path.write_text(
                """# TASK_BOARD.md
> Generated from Nova-Task v2 SQLite authority.

## Active
- [ ] **[NT-9699b64f762b]** Actanara (task - Active)
  - [ ] **[NT-7a745c01ebe6]** Dashboard network settings (task - Active)

## Done
- [x] **[NT-done0000001]** Previous migration (task - Done)
""",
                encoding="utf-8",
            )
            write_settings({"rag": {"embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)

            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["task-board-snapshot"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            chunks = [
                json.loads(line)
                for line in Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        task_items = [
            chunk
            for chunk in chunks
            if chunk["sourceSet"] == "task-board-snapshot" and chunk["provenance"]["recordType"] == "task-item"
        ]
        self.assertEqual(result["status"], "ready")
        self.assertGreaterEqual(len(task_items), 3)
        child = next(chunk for chunk in task_items if chunk["provenance"]["identifiedTaskId"] == "NT-7a745c01ebe6")
        self.assertIn("Dashboard network settings", child["text"])
        self.assertEqual(child["project"], "Actanara")
        self.assertEqual(child["provenance"]["section"], "Active")

    def test_v2_candidate_index_uses_english_technical_filename_for_english_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            day = root / "Actanara" / "artifacts" / "diary" / "diary-2026-06-05"
            day.mkdir(parents=True)
            (day / "technical-260605.md").write_text(
                """# 2026-06-05 Technical Progress Report

## Nova-Task Evidence

date: 2026-06-05
task_updates:
  - id: T-EN
    parent_id: actanara
    title: English technical report
    status: Done
    progress_delta: 100
""",
                encoding="utf-8",
            )
            (day / "技术进展-260605.md").write_text(
                """# 技术进展

date: 2026-06-05
task_updates:
  - id: T-ZH
    parent_id: actanara
    title: Chinese technical report
    status: Done
    progress_delta: 100
""",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"languageProfile": "en"}}, paths)
            settings = resolve_rag_settings(paths)

            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["technical-report-task-events"],
                embedding_fn=lambda texts: [[0.1] * settings.embedding_dimension for _ in texts],
            )
            chunks = [
                json.loads(line)
                for line in Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            sources = [
                json.loads(line)
                for line in Path(result["sourcesPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            coverage = read_v2_coverage(settings)
            by_set = {item["sourceSet"]: item for item in coverage["sourceSets"]}

        self.assertEqual(len(chunks), 1)
        self.assertIn("T-EN", chunks[0]["text"])
        self.assertNotIn("T-ZH", chunks[0]["text"])
        self.assertEqual(Path(sources[0]["path"]).name, "technical-260605.md")
        self.assertEqual(by_set["technical-report-task-events"]["discoveredSourceCount"], 1)
        self.assertTrue(by_set["technical-report-task-events"]["expected"]["paths"][0].endswith("technical-260605.md"))

    def test_v2_candidate_index_reads_nova_task_work_graph_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "LegacyDiary")
            recon_dir = paths.state_dir / "nova-task" / "work-graph"
            recon_dir.mkdir(parents=True)
            artifact = recon_dir / "2026-06-30-20260630-120000.md"
            artifact.write_text(
                """# Nova-Task Work Graph Reconciliation

- businessDate: 2026-06-30
- applied: true

```yaml
nova_task:
  date: "2026-06-30"
  matched_tasks:
    - task_id: "NT-OPEN"
      confidence: high
      event_type: progress
      summary: "actanara reconciliation improved task classification."
      evidence: ["technical:chronicle"]
  candidate_actions:
    - candidate_id: "NTC-OLD"
      action: attach_existing
      target_node_id: "NT-OPEN"
      reason: "Old pending candidate is represented by the actanara root."
      confidence: high
      evidence: ["candidate:NTC-OLD"]
```
""",
                encoding="utf-8",
            )
            settings = resolve_rag_settings(paths)

            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["nova-task-work-graph-events"],
                embedding_fn=lambda texts: [[0.1] * settings.embedding_dimension for _ in texts],
            )
            chunks = [
                json.loads(line)
                for line in Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            sources = [
                json.loads(line)
                for line in Path(result["sourcesPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            coverage = read_v2_coverage(settings)
            by_set = {item["sourceSet"]: item for item in coverage["sourceSets"]}

        self.assertEqual({chunk["sourceSet"] for chunk in chunks}, {"nova-task-work-graph-events"})
        self.assertTrue(any("candidate_action" in chunk["text"] and "NTC-OLD" in chunk["text"] for chunk in chunks))
        self.assertTrue(any(chunk["provenance"]["recordType"] == "matched_task" for chunk in chunks))
        self.assertEqual(Path(sources[0]["path"]).name, artifact.name)
        self.assertEqual(by_set["nova-task-work-graph-events"]["discoveredSourceCount"], 1)
        self.assertIn("work-graph", by_set["nova-task-work-graph-events"]["expected"]["paths"][0])

    def test_v2_candidate_index_skips_unapplied_nova_task_work_graph_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "LegacyDiary")
            recon_dir = paths.state_dir / "nova-task" / "work-graph"
            recon_dir.mkdir(parents=True)
            artifact = recon_dir / "2026-06-30-20260630-120000.md"
            artifact.write_text(
                """# Nova-Task Work Graph Reconciliation

- businessDate: 2026-06-30
- applied: false

```yaml
nova_task:
  date: "2026-06-30"
  matched_tasks:
    - task_id: "NT-DRY-RUN"
      confidence: high
      event_type: progress
      summary: "Dry-run work graph should not enter RAG evidence."
      evidence: ["technical:dry-run"]
```
""",
                encoding="utf-8",
            )
            settings = resolve_rag_settings(paths)

            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["nova-task-work-graph-events"],
                embedding_fn=lambda texts: [[0.1] * settings.embedding_dimension for _ in texts],
            )
            chunks = [
                json.loads(line)
                for line in Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            sources = [
                json.loads(line)
                for line in Path(result["sourcesPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(chunks, [])
        self.assertEqual(Path(sources[0]["path"]).name, artifact.name)
        self.assertEqual(sources[0]["chunkCount"], 0)
        self.assertEqual(sources[0]["skippedReason"], "not-applied")

    def test_v2_candidate_index_reads_legacy_nova_task_reconciliation_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "LegacyDiary")
            recon_dir = paths.state_dir / "nova-task" / "candidate-reconciliation"
            recon_dir.mkdir(parents=True)
            artifact = recon_dir / "2026-06-30-20260630-120000.md"
            artifact.write_text(
                """# Nova-Task Candidate Reconciliation

- businessDate: 2026-06-30
- applied: true

```yaml
nova_task:
  date: "2026-06-30"
  matched_tasks:
    - task_id: "NT-OPEN"
      confidence: high
      event_type: progress
      summary: "Legacy reconciliation artifact remains indexable."
      evidence: ["technical:chronicle"]
```
""",
                encoding="utf-8",
            )
            settings = resolve_rag_settings(paths)

            result = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["nova-task-reconciliation-events"],
                embedding_fn=lambda texts: [[0.1] * settings.embedding_dimension for _ in texts],
            )
            chunks = [
                json.loads(line)
                for line in Path(result["chunksPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            sources = [
                json.loads(line)
                for line in Path(result["sourcesPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            coverage = read_v2_coverage(settings)
            by_set = {item["sourceSet"]: item for item in coverage["sourceSets"]}

        self.assertEqual({chunk["sourceSet"] for chunk in chunks}, {"nova-task-reconciliation-events"})
        self.assertTrue(any("Legacy reconciliation artifact remains indexable" in chunk["text"] for chunk in chunks))
        self.assertEqual(Path(sources[0]["path"]).name, artifact.name)
        self.assertEqual(by_set["nova-task-reconciliation-events"]["discoveredSourceCount"], 1)
        self.assertIn("candidate-reconciliation", by_set["nova-task-reconciliation-events"]["expected"]["paths"][0])

    def test_english_artifacts_materialize_index_and_query_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "LegacyDiary")
            day = diary_day_dir(paths.diary_dir, datetime(2026, 6, 6).date())
            day.mkdir(parents=True)
            (day / "diary-260606.md").write_text(
                """# 2026-06-06 Diary

## Daily Overview
* **English pipeline artifact filenames**: diary-260606.md, technical-260606.md, and learning-260606.md were generated for the English profile.

```json
{"date":"2026-06-06","summary":"English pipeline artifact filenames stayed isolated"}
```
""",
                encoding="utf-8",
            )
            (day / "technical-260606.md").write_text(
                """# 2026-06-06 Technical Progress Report

## Engineering Objectives and Outcomes

English RAG indexing smoke kept profile artifacts isolated.

## Nova-Task Reconciliation Hooks

None
""",
                encoding="utf-8",
            )
            (day / "learning-260606.md").write_text(
                """# 2026-06-06 Learning and Infrastructure Audit

## Lessons
### [codex] English RAG query smoke
#### Problem
English artifacts needed a query smoke.
#### Recommendation
Keep sourceSet metadata stable while snippets remain English.
""",
                encoding="utf-8",
            )
            (day / "日记-260606.md").write_text("# 中文日记\n\n## 今日概要\n不应进入 English profile RAG。\n", encoding="utf-8")
            (day / "技术进展-260606.md").write_text("# 中文技术进展\n\n不应进入 English profile RAG。\n", encoding="utf-8")
            (day / "智慧沉淀-260606.md").write_text("# 中文智慧沉淀\n\n不应进入 English profile RAG。\n", encoding="utf-8")
            write_settings(
                {
                    "pipeline": {"languageProfile": "en", "englishEnabled": True},
                    "rag": {"languageProfile": "en", "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}},
                },
                paths,
            )
            migrate(paths)
            recon_dir = paths.state_dir / "nova-task" / "work-graph"
            recon_dir.mkdir(parents=True)
            (recon_dir / "2026-06-06-20260606-120000.md").write_text(
                """# Nova-Task Work Graph Reconciliation

- businessDate: 2026-06-06
- applied: true

```yaml
nova_task:
  date: "2026-06-06"
  matched_tasks:
    - task_id: "T-EN-SMOKE"
      confidence: high
      event_type: progress
      summary: "English RAG indexing smoke"
      evidence: ["technical:English RAG indexing smoke"]
```
""",
                encoding="utf-8",
            )

            materialized = materialize_diary_markdown_day(paths, datetime(2026, 6, 6).date(), source_run_id=None)
            documents = [read_diary_markdown_document(paths, key) for key in materialized["documentKeys"]]
            settings = resolve_rag_settings(paths)
            build = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["diary-markdown-sections", "diary-markdown-embedded-json", "nova-task-work-graph-events"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            indexed_chunks = [
                json.loads(line)
                for line in Path(build["candidateIndex"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            source_records = [
                json.loads(line)
                for line in Path(build["sourcesPath"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            ranked = rank_chunks(
                query="English pipeline artifact filenames",
                query_embedding=[1.0, 0.0],
                chunks=indexed_chunks,
                top_k=5,
                similarity_weight=0.7,
                keyword_weight=0.3,
                recency_half_life_days=7,
                now=datetime(2026, 6, 6),
                language_profile="en",
            )
            evidence_ranked = rank_chunks(
                query="T-EN-SMOKE",
                query_embedding=[1.0, 0.0],
                chunks=indexed_chunks,
                top_k=5,
                similarity_weight=0.7,
                keyword_weight=0.3,
                recency_half_life_days=7,
                now=datetime(2026, 6, 6),
                language_profile="en",
            )

        materialized_paths = sorted(Path(document["relative_path"]).name for document in documents)
        self.assertEqual(materialized_paths, ["diary-260606.md", "learning-260606.md", "technical-260606.md"])
        self.assertEqual(build["manifest"]["languageProfile"], "en")
        self.assertEqual(set(build["manifest"]["sourceSets"]), {"diary-markdown-sections", "diary-markdown-embedded-json", "nova-task-work-graph-events"})
        indexed_text = "\n".join(str(chunk.get("text") or "") for chunk in indexed_chunks)
        indexed_paths = {Path(str(source["path"])).name for source in source_records}
        self.assertIn("diary-260606.md", indexed_paths)
        self.assertIn("technical-260606.md", indexed_paths)
        self.assertIn("learning-260606.md", indexed_paths)
        self.assertNotIn("日记-260606.md", indexed_paths)
        self.assertIn("English pipeline artifact filenames", indexed_text)
        self.assertIn("T-EN-SMOKE", indexed_text)
        self.assertNotIn("不应进入 English profile RAG", indexed_text)
        self.assertIn("citationPack", ranked)
        self.assertEqual(ranked["answerSynthesis"]["status"], "ready")
        self.assertTrue(any("English pipeline artifact filenames" in item.get("textPreview", "") for item in ranked["results"]))
        self.assertTrue(any(item.get("sourceSet") == "nova-task-work-graph-events" for item in evidence_ranked["results"]))
        self.assertTrue(any("T-EN-SMOKE" in item.get("textPreview", "") for item in evidence_ranked["results"]))

    def test_guarded_v2_promotion_copies_active_snapshot_and_prunes_candidate_without_switching_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            (root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl").parent.mkdir(parents=True)
            (root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl").write_text(
                json.dumps(
                    {
                        "id": "promote-lesson",
                        "text": "Promotion creates an active v2 snapshot.",
                        "date": "2026-06-05",
                        "agent": "codex",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(
                json.dumps(
                    {
                        "id": "promote-lesson",
                        "text": "Promotion creates an active v2 snapshot.",
                        "date": "2026-06-05",
                        "agent": "codex",
                        "sourceSet": "lessons",
                        "model": "all-MiniLM-L6-v2",
                        "embedding": [1.0, 0.0],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before_mtime = index.stat().st_mtime_ns
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"mode": "legacy", "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            build = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            run_id = build["run"]["runId"]
            candidate_dir = settings.v2_store_path / "indexes" / "candidates" / run_id
            candidate_paths = [
                Path(build["candidateManifest"]),
                Path(build["candidateIndex"]),
                Path(build["chunksPath"]),
                Path(build["embeddingsPath"]),
                Path(build["sourcesPath"]),
                Path(build["buildReportPath"]),
            ]
            self.assertTrue(all(path.exists() for path in candidate_paths))
            settings_before = read_settings(paths, redact_secrets=False)
            root_manifest_before = json.loads((settings.v2_store_path / "manifest.json").read_text(encoding="utf-8"))

            result = promote_v2_candidate(
                settings,
                run_id=run_id,
                confirm=True,
                confirmation_text=required_v2_promotion_confirmation(run_id),
                requested_by="test",
                reason="unit test promotion",
            )

            self.assertTrue(result["accepted"])
            self.assertEqual(result["status"], "promoted")
            self.assertEqual(index.stat().st_mtime_ns, before_mtime)
            self.assertEqual(read_settings(paths, redact_secrets=False), settings_before)
            self.assertFalse(candidate_dir.exists())
            self.assertTrue(result["mutationPolicy"]["candidateFilesMutated"])
            self.assertEqual(result["retention"]["policy"], {"keepActiveRuns": 1, "keepCandidates": 0})
            self.assertEqual([item["runId"] for item in result["retention"]["deleted"]["candidates"]], [run_id])
            active_index = settings.v2_store_path / "indexes" / "active" / run_id / "index.jsonl"
            active_manifest_path = settings.v2_store_path / "indexes" / "active" / run_id / "manifest.json"
            self.assertTrue(active_index.exists())
            self.assertTrue(active_manifest_path.exists())
            active_manifest = json.loads(active_manifest_path.read_text(encoding="utf-8"))
            root_manifest = json.loads((settings.v2_store_path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["status"], "active")
            self.assertEqual(root_manifest["activeIndexPath"], str(active_index))
            self.assertEqual(root_manifest["activeManifestPath"], str(active_manifest_path))
            self.assertEqual(root_manifest["promotedFromRunId"], run_id)
            self.assertEqual(root_manifest["rollbackMode"], "previous-v2-manifest")
            self.assertNotIn("candidateIndexPath", root_manifest)
            self.assertNotIn("candidatePath", root_manifest)
            self.assertNotIn("buildReportPath", root_manifest)
            self.assertNotIn("manifestPath", root_manifest)
            provenance = root_manifest["promotionProvenance"]
            self.assertEqual(provenance["candidateRunId"], run_id)
            self.assertNotIn("candidatePath", provenance)
            self.assertNotIn("candidateIndexPath", provenance)
            self.assertNotIn("candidateManifestPath", provenance)
            self.assertEqual(active_manifest["activeIndexPath"], str(active_index))
            status = read_rag_status(settings=resolve_rag_settings(paths), count_legacy_entries=False)
            self.assertTrue(status["v2"]["activeReady"])
            self.assertNotIn("/candidates/", str(status["v2"]["candidateIndexPath"] or ""))
            backup_path = Path(result["previousManifestBackupPath"])
            self.assertTrue(backup_path.exists())
            backup = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual(backup["status"], root_manifest_before["status"])
            self.assertFalse(resolve_active_rag_index(resolve_rag_settings(paths)).ready)

            write_settings({"rag": {"mode": "v2-shadow"}}, paths)
            shadow = resolve_active_rag_index(resolve_rag_settings(paths))
            self.assertEqual(shadow.source, "retired")
            self.assertIsNone(shadow.index_path)

            write_settings({"rag": {"mode": "v2"}}, paths)
            active = resolve_active_rag_index(resolve_rag_settings(paths))
            self.assertEqual(active.source, "v2")
            self.assertTrue(active.ready)
            self.assertEqual(active.index_path, active_index)
            self.assertIn("v2-active-ready", active.reason)

    def test_guarded_v2_promotion_prunes_old_active_run_and_keeps_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True)
            lessons.write_text(
                json.dumps({"id": "retention-lesson", "text": "Retention keeps only the current active run.", "date": "2026-06-05"})
                + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2},
                        "indexing": {"sourceSets": ["lessons"]},
                    }
                },
                paths,
            )
            settings = resolve_rag_settings(paths)
            first = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            first_run_id = first["run"]["runId"]
            promote_v2_candidate(
                settings,
                run_id=first_run_id,
                confirm=True,
                confirmation_text=required_v2_promotion_confirmation(first_run_id),
            )
            first_active_dir = settings.v2_store_path / "indexes" / "active" / first_run_id
            self.assertTrue(first_active_dir.exists())

            second = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[0.0, 1.0] for _ in texts],
            )
            second_run_id = second["run"]["runId"]
            second_promotion = promote_v2_candidate(
                settings,
                run_id=second_run_id,
                confirm=True,
                confirmation_text=required_v2_promotion_confirmation(second_run_id),
            )
            second_active_dir = settings.v2_store_path / "indexes" / "active" / second_run_id

            self.assertFalse(first_active_dir.exists())
            self.assertTrue(second_active_dir.exists())
            self.assertEqual([item["runId"] for item in second_promotion["retention"]["deleted"]["activeRuns"]], [first_run_id])
            self.assertEqual(list((settings.v2_store_path / "indexes" / "candidates").iterdir()), [])
            active = resolve_active_rag_index(settings)
            self.assertTrue(active.ready)
            self.assertEqual(active.index_path, second_active_dir / "index.jsonl")

    def test_v2_retention_prune_does_not_delete_outside_store_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"mode": "v2", "embedding": {"dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            active_dir = settings.v2_store_path / "indexes" / "active" / "current"
            active_dir.mkdir(parents=True)
            (active_dir / "index.jsonl").write_text("{}\n", encoding="utf-8")
            candidates = settings.v2_store_path / "indexes" / "candidates"
            candidates.mkdir(parents=True)
            normal_candidate = candidates / "old-candidate"
            normal_candidate.mkdir()
            (normal_candidate / "index.jsonl").write_text("{}\n", encoding="utf-8")
            outside = root / "outside-store"
            outside.mkdir()
            outside_file = outside / "keep.txt"
            outside_file.write_text("do not delete\n", encoding="utf-8")
            escape = candidates / "escape"
            try:
                escape.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            result = prune_v2_index_store(settings, active_run_id="current")

            self.assertEqual(result["status"], "partial")
            self.assertTrue(outside_file.exists())
            self.assertTrue(escape.is_symlink())
            self.assertFalse(normal_candidate.exists())
            self.assertTrue(active_dir.exists())
            self.assertIn("outside the v2 index store boundary", result["errors"][0]["error"])

    def test_guarded_v2_promotion_rejects_embedding_profile_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True)
            lessons.write_text(
                json.dumps({"id": "profile-mismatch", "text": "Promotion validates embedding profile hashes.", "date": "2026-06-05"})
                + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2},
                        "indexing": {"sourceSets": ["lessons"]},
                    }
                },
                paths,
            )
            build_settings = resolve_rag_settings(paths)
            build = build_v2_candidate_index(
                build_settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            write_settings({"rag": {"embedding": {"model": "intfloat/multilingual-e5-small", "dimension": 2}}}, paths)
            current_settings = resolve_rag_settings(paths)

            with self.assertRaisesRegex(ValueError, "embeddingProfileHash"):
                promote_v2_candidate(
                    current_settings,
                    run_id=build["run"]["runId"],
                    confirm=True,
                    confirmation_text=required_v2_promotion_confirmation(build["run"]["runId"]),
                )

    def test_guarded_v2_promotion_rejects_source_profile_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lessons = root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl"
            lessons.parent.mkdir(parents=True)
            lessons.write_text(
                json.dumps({"id": "source-mismatch", "text": "Promotion validates source profile hashes.", "date": "2026-06-05"})
                + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings(
                {
                    "rag": {
                        "mode": "v2",
                        "embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2},
                        "indexing": {"sourceSets": ["lessons"]},
                    }
                },
                paths,
            )
            build_settings = resolve_rag_settings(paths)
            build = build_v2_candidate_index(
                build_settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            write_settings({"rag": {"source": {"root": str(root / "DifferentDiary")}}}, paths)
            current_settings = resolve_rag_settings(paths)

            with self.assertRaisesRegex(ValueError, "sourceProfileHash"):
                promote_v2_candidate(
                    current_settings,
                    run_id=build["run"]["runId"],
                    confirm=True,
                    confirmation_text=required_v2_promotion_confirmation(build["run"]["runId"]),
                )

    def test_guarded_v2_promotion_rejects_bad_confirmation_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            (root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl").parent.mkdir(parents=True)
            (root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl").write_text(
                json.dumps({"id": "bad-confirm", "text": "Bad confirmation fixture.", "date": "2026-06-05"})
                + "\n",
                encoding="utf-8",
            )
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(
                json.dumps(
                    {
                        "id": "bad-confirm",
                        "text": "Bad confirmation fixture.",
                        "sourceSet": "lessons",
                        "model": "all-MiniLM-L6-v2",
                        "embedding": [1.0, 0.0],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            build = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            root_manifest_before = (settings.v2_store_path / "manifest.json").read_text(encoding="utf-8")
            legacy_mtime = index.stat().st_mtime_ns

            with self.assertRaises(ValueError):
                promote_v2_candidate(
                    settings,
                    run_id=build["run"]["runId"],
                    confirm=True,
                    confirmation_text="wrong",
                )

            self.assertEqual(index.stat().st_mtime_ns, legacy_mtime)
            self.assertEqual((settings.v2_store_path / "manifest.json").read_text(encoding="utf-8"), root_manifest_before)
            self.assertFalse((settings.v2_store_path / "indexes" / "active" / build["run"]["runId"]).exists())

    def test_guarded_v2_promotion_rejects_partial_candidate_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            raw_dir = legacy / "__diary_daily" / "2026-06-05" / "codex"
            raw_dir.mkdir(parents=True)
            (raw_dir / "messages.jsonl").write_text(
                json.dumps({"content": "Partial candidate should not promote.", "timestamp": "2026-06-05T08:00:00Z"})
                + "\n",
                encoding="utf-8",
            )
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps({"id": "legacy", "embedding": [1.0, 0.0]}) + "\n", encoding="utf-8")
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"embedding": {"dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            build = build_v2_candidate_index(
                settings,
                requested_by="test",
                embedding_fn=lambda texts: [[1.0, 0.0, 0.0] for _ in texts],
            )
            root_manifest_before = (settings.v2_store_path / "manifest.json").read_text(encoding="utf-8")

            with self.assertRaises(ValueError):
                promote_v2_candidate(
                    settings,
                    run_id=build["run"]["runId"],
                    confirm=True,
                    confirmation_text=required_v2_promotion_confirmation(build["run"]["runId"]),
                )

            self.assertEqual((settings.v2_store_path / "manifest.json").read_text(encoding="utf-8"), root_manifest_before)
            self.assertFalse((settings.v2_store_path / "indexes" / "active" / build["run"]["runId"]).exists())

    def test_guarded_v2_manifest_rollback_rejects_pruned_active_backup_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "Diary"
            lessons = [
                {"id": "rollback-one", "text": "First promoted active snapshot.", "date": "2026-06-05"},
                {"id": "rollback-two", "text": "Second promoted active snapshot.", "date": "2026-06-05"},
            ]
            (root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl").parent.mkdir(parents=True)
            (root / "Actanara" / "artifacts" / "learning" / "lessons.jsonl").write_text(
                "\n".join(json.dumps(item) for item in lessons) + "\n",
                encoding="utf-8",
            )
            index = legacy / "__diary_rag" / "index.jsonl"
            index.parent.mkdir(parents=True)
            index.write_text(
                "\n".join(
                    json.dumps(
                        {
                            **item,
                            "sourceSet": "lessons",
                            "model": "all-MiniLM-L6-v2",
                            "embedding": [1.0, 0.0],
                        }
                    )
                    for item in lessons
                )
                + "\n",
                encoding="utf-8",
            )
            legacy_mtime = index.stat().st_mtime_ns
            paths = initialize_home(root / "Actanara", legacy_diary_root=legacy)
            write_settings({"rag": {"embedding": {"model": "all-MiniLM-L6-v2", "dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            first = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            first_run_id = first["run"]["runId"]
            first_promotion = promote_v2_candidate(
                settings,
                run_id=first_run_id,
                confirm=True,
                confirmation_text=required_v2_promotion_confirmation(first_run_id),
            )
            first_active_index = Path(first_promotion["activeIndexPath"])
            self.assertTrue(first_active_index.exists())
            second = build_v2_candidate_index(
                settings,
                requested_by="test",
                source_sets=["lessons"],
                embedding_fn=lambda texts: [[0.0, 1.0] for _ in texts],
            )
            second_run_id = second["run"]["runId"]
            second_promotion = promote_v2_candidate(
                settings,
                run_id=second_run_id,
                confirm=True,
                confirmation_text=required_v2_promotion_confirmation(second_run_id),
            )
            backup_path = Path(second_promotion["previousManifestBackupPath"])
            backup_manifest = json.loads(backup_path.read_text(encoding="utf-8"))
            self.assertEqual(backup_manifest["status"], "active")
            self.assertEqual(backup_manifest["activeIndexPath"], str(first_active_index))
            self.assertFalse(first_active_index.exists())
            settings_before = read_settings(paths, redact_secrets=False)
            root_manifest_before_rollback = (settings.v2_store_path / "manifest.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "active backup index is missing"):
                rollback_v2_manifest(
                    settings,
                    backup_name=backup_path.name,
                    confirm=True,
                    confirmation_text=required_v2_manifest_rollback_confirmation(backup_path.name),
                    requested_by="test",
                    reason="unit test rollback",
                )

            self.assertEqual(index.stat().st_mtime_ns, legacy_mtime)
            self.assertEqual(read_settings(paths, redact_secrets=False), settings_before)
            self.assertEqual((settings.v2_store_path / "manifest.json").read_bytes(), root_manifest_before_rollback)
            self.assertFalse(any(path.name.startswith("20") and "before-rollback" in path.name for path in backup_path.parent.iterdir()))

    def test_guarded_v2_manifest_rollback_rejects_bad_confirmation_and_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"embedding": {"dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            backups = settings.v2_store_path / "manifest.backups"
            backups.mkdir(parents=True)
            backup = backups / "ok.json"
            backup.write_text(
                json.dumps({"schemaVersion": 1, "status": "empty", "dimension": 2}),
                encoding="utf-8",
            )
            root_manifest = settings.v2_store_path / "manifest.json"
            root_manifest.parent.mkdir(parents=True, exist_ok=True)
            root_manifest.write_text(json.dumps({"schemaVersion": 1, "status": "active"}), encoding="utf-8")
            before = root_manifest.read_text(encoding="utf-8")

            with self.assertRaises(ValueError):
                rollback_v2_manifest(settings, backup_name="ok.json", confirm=True, confirmation_text="wrong")
            with self.assertRaises(ValueError):
                rollback_v2_manifest(
                    settings,
                    backup_name="../ok.json",
                    confirm=True,
                    confirmation_text=required_v2_manifest_rollback_confirmation("../ok.json"),
                )
            with self.assertRaises(ValueError):
                rollback_v2_manifest(
                    settings,
                    backup_name="..",
                    confirm=True,
                    confirmation_text=required_v2_manifest_rollback_confirmation(".."),
                )

            self.assertEqual(root_manifest.read_text(encoding="utf-8"), before)
            self.assertFalse(any(path.name.startswith("20") for path in backups.iterdir()))

    def test_guarded_v2_manifest_rollback_rejects_invalid_active_backup_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            write_settings({"rag": {"embedding": {"dimension": 2}}}, paths)
            settings = resolve_rag_settings(paths)
            backups = settings.v2_store_path / "manifest.backups"
            backups.mkdir(parents=True)
            root_manifest = settings.v2_store_path / "manifest.json"
            root_manifest.parent.mkdir(parents=True, exist_ok=True)
            root_manifest.write_text(json.dumps({"schemaVersion": 1, "status": "empty"}), encoding="utf-8")
            before = root_manifest.read_text(encoding="utf-8")

            outside = root / "outside.jsonl"
            outside.write_text("{}\n", encoding="utf-8")
            cases = {
                "missing.json": {"schemaVersion": 1, "status": "active", "dimension": 2, "activeIndexPath": str(settings.v2_store_path / "indexes" / "active" / "missing.jsonl")},
                "directory.json": {"schemaVersion": 1, "status": "active", "dimension": 2, "activeIndexPath": str(backups)},
                "outside.json": {"schemaVersion": 1, "status": "active", "dimension": 2, "activeIndexPath": str(outside)},
            }
            active_index = settings.v2_store_path / "indexes" / "active" / "run-1" / "index.jsonl"
            active_index.parent.mkdir(parents=True)
            active_index.write_text("{}\n", encoding="utf-8")
            cases["dimension.json"] = {
                "schemaVersion": 1,
                "status": "active",
                "dimension": 3,
                "activeIndexPath": str(active_index),
            }
            for name, manifest in cases.items():
                (backups / name).write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    rollback_v2_manifest(
                        settings,
                        backup_name=name,
                        confirm=True,
                        confirmation_text=required_v2_manifest_rollback_confirmation(name),
                    )
                self.assertEqual(root_manifest.read_text(encoding="utf-8"), before)

    def test_memory_governance_signals_are_deterministic_and_visible_in_ranking(self):
        lesson = {
            "id": "lesson",
            "text": "RAG ranking should prefer governed lesson evidence.",
            "embedding": [1.0, 0.0],
            "sourceSet": "lessons",
            "sourceId": "s1",
            "sourcePath": "/tmp/lessons.jsonl",
            "sourceType": "jsonl",
            "textHash": "h1",
            "dedupeKey": "d1",
            "layer": "lesson",
            "date": "2026-06-06",
            "provenance": {"authority": "test"},
        }
        dialogue = {
            "id": "dialogue",
            "text": "RAG ranking should prefer governed dialogue evidence.",
            "embedding": [1.0, 0.0],
            "sourceSet": "filtered-dialogue-daily",
            "sourceId": "s2",
            "sourcePath": "/tmp/dialogue.jsonl",
            "sourceType": "filtered-dialogue-jsonl",
            "textHash": "h2",
            "dedupeKey": "d2",
            "layer": "dialogue",
            "date": "2026-06-06",
            "provenance": {"authority": "test"},
        }
        lesson["governance"] = governance_for_chunk(lesson)
        dialogue["governance"] = governance_for_chunk(dialogue)

        ranked = rank_chunks(
            query="lesson decision evidence",
            query_embedding=[1.0, 0.0],
            chunks=[dialogue, lesson],
            top_k=2,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            now=datetime(2026, 6, 6),
        )

        self.assertEqual(ranked["results"][0]["id"], "lesson")
        self.assertIn("governance", ranked["results"][0])
        self.assertEqual(ranked["results"][0]["governance"]["lifecycle"], "canonical")
        self.assertGreater(ranked["results"][0]["scoreComponents"]["governanceWeight"], 1.0)
        self.assertEqual(ranked["results"][0]["scoreComponents"]["provenanceScore"], 1.0)
        self.assertTrue(ranked["results"][0]["governance"]["canonicalCandidate"])
        self.assertIn("duplicateGroupKey", ranked["results"][0]["governance"])

        filtered = rank_chunks(
            query="lesson decision evidence",
            query_embedding=[1.0, 0.0],
            chunks=[dialogue, lesson],
            top_k=2,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            lifecycle_filter=["canonical"],
            source_set_filter=["lessons"],
            now=datetime(2026, 6, 6),
        )
        self.assertEqual(filtered["returned"], 1)
        self.assertEqual(filtered["results"][0]["id"], "lesson")

    def test_ranked_results_are_deduped_before_top_k_truncation(self):
        duplicate_low = {
            "id": "duplicate-low",
            "text": "Repeated VPS incident evidence should only occupy one retrieval slot.",
            "embedding": [1.0, 0.0],
            "sourceSet": "filtered-dialogue-daily",
            "sourceId": "same-event",
            "sourcePath": "/tmp/dialogue.jsonl",
            "sourceType": "filtered-dialogue-jsonl",
            "textHash": "same-hash",
            "dedupeKey": "same-dedupe",
            "date": "2026-06-06",
            "provenance": {"authority": "test"},
        }
        duplicate_high = {
            **duplicate_low,
            "id": "duplicate-high",
            "text": "Repeated VPS incident evidence should only occupy one retrieval slot with stronger keyword match.",
        }
        distinct = {
            "id": "distinct",
            "text": "Distinct resolution evidence should remain visible after duplicate removal.",
            "embedding": [0.9, 0.1],
            "sourceSet": "technical-report-task-events",
            "sourceId": "resolution-event",
            "sourcePath": "/tmp/report.md",
            "sourceType": "markdown",
            "textHash": "resolution-hash",
            "dedupeKey": "resolution-dedupe",
            "date": "2026-06-06",
            "provenance": {"authority": "test"},
        }
        for chunk in (duplicate_low, duplicate_high, distinct):
            chunk["governance"] = governance_for_chunk(chunk)

        ranked = rank_chunks(
            query="VPS incident stronger keyword match resolution",
            query_embedding=[1.0, 0.0],
            chunks=[duplicate_low, duplicate_high, distinct],
            top_k=3,
            similarity_weight=0.7,
            keyword_weight=0.3,
            recency_half_life_days=7,
            now=datetime(2026, 6, 6),
        )

        result_ids = [item["id"] for item in ranked["results"]]
        self.assertEqual(ranked["returned"], 2)
        self.assertEqual(ranked["dedupe"]["duplicatesRemoved"], 1)
        self.assertIn("duplicate-high", result_ids)
        self.assertIn("distinct", result_ids)
        self.assertNotIn("duplicate-low", result_ids)

    def test_query_decomposition_subquery_match_boosts_config_evidence(self):
        generic = {
            "id": "generic-vps",
            "text": "VPS archive note with broad infrastructure context.",
            "embedding": [1.0, 0.0],
            "sourceSet": "filtered-dialogue-daily",
            "sourceId": "generic",
            "sourcePath": "/tmp/dialogue.jsonl",
            "sourceType": "filtered-dialogue-jsonl",
            "textHash": "generic-hash",
            "dedupeKey": "generic",
            "date": "2026-06-06",
            "provenance": {"authority": "test"},
        }
        config = {
            "id": "port-config",
            "text": "2096 端口不可用 because the target config blocks that port.",
            "embedding": [0.86, 0.14],
            "sourceSet": "lessons",
            "sourceId": "port-config",
            "sourcePath": "/tmp/lessons.jsonl",
            "sourceType": "jsonl",
            "textHash": "port-config-hash",
            "dedupeKey": "port-config",
            "date": "2026-06-06",
            "provenance": {"authority": "test"},
        }
        for chunk in (generic, config):
            chunk["governance"] = governance_for_chunk(chunk)

        ranked = rank_chunks(
            query="2096 端口为什么不可用？",
            query_embedding=[1.0, 0.0],
            chunks=[generic, config],
            top_k=2,
            similarity_weight=0.8,
            keyword_weight=0.2,
            recency_half_life_days=7,
            now=datetime(2026, 6, 6),
        )

        self.assertIn("config", ranked["queryPlan"]["intents"])
        self.assertEqual(ranked["results"][0]["id"], "port-config")
        self.assertGreater(ranked["results"][0]["scoreComponents"]["subQueryMatch"], 0.0)
        self.assertGreater(ranked["results"][0]["scoreComponents"]["subQueryBoost"], 1.0)
        self.assertEqual(ranked["answerSynthesis"]["answerType"], "configuration")
        self.assertIn("Configuration evidence", ranked["answerSynthesis"]["summary"])

    def test_rag_answer_synthesis_summary_uses_language_profile_without_schema_change(self):
        chunk = {
            "id": "port-config",
            "text": "2096 端口不可用，因为目标环境限制该端口。",
            "embedding": [1.0, 0.0],
            "layer": "technical",
            "sourceSet": "lessons",
            "date": "2026-06-06",
            "provenance": {"authority": "test"},
        }
        chunk["governance"] = governance_for_chunk(chunk)

        ranked = rank_chunks(
            query="2096 端口为什么不可用？",
            query_embedding=[1.0, 0.0],
            chunks=[chunk],
            top_k=1,
            similarity_weight=0.8,
            keyword_weight=0.2,
            recency_half_life_days=7,
            now=datetime(2026, 6, 6),
            language_profile="zh",
        )

        synthesis = ranked["answerSynthesis"]
        self.assertEqual(synthesis["status"], "ready")
        self.assertEqual(synthesis["method"], "extractive")
        self.assertEqual(synthesis["answerType"], "configuration")
        self.assertEqual(synthesis["citationIds"], ["C1"])
        self.assertIn("配置证据为 C1", synthesis["summary"])

    def test_rag_eval_runner_scores_expected_search_contract_fields(self):
        responses = {
            "rag-current-task": {
                "results": [
                    {
                        "id": "task",
                        "sourceSet": "task-board-snapshot",
                        "workType": "task",
                        "project": "actanara",
                        "governance": {"lifecycle": "current-state", "authorityRank": 92, "provenanceScore": 1.0},
                        "score": 0.9,
                    }
                ]
            },
            "vps-fact-ip": {
                "results": [
                    {
                        "id": "vps-premium-ip",
                        "sourceSet": "lessons",
                        "workType": "general",
                        "textPreview": "精品 VPS IP 已记录，作为后续连接配置依据。",
                        "provenance": {"dedupeKey": "vps-premium-ip"},
                        "governance": {"lifecycle": "canonical", "authorityRank": 90, "provenanceScore": 1.0},
                        "score": 0.91,
                    }
                ],
                "citationPack": [{"citationId": "C1", "resultId": "vps-premium-ip", "excerpt": "精品 VPS IP 已记录。"}],
                "answerSynthesis": {"status": "ready", "summary": "精品 VPS IP 已记录。", "bullets": []},
            },
            "vps-incident-count": {
                "results": [
                    {"id": "vps-incident-1", "sourceSet": "filtered-dialogue-daily", "workType": "incident", "textPreview": "VPS 第一次问题。", "provenance": {"dedupeKey": "vps-incident-1"}, "governance": {"lifecycle": "episodic"}, "score": 0.9},
                    {"id": "vps-incident-2", "sourceSet": "filtered-dialogue-daily", "workType": "incident", "textPreview": "VPS 第二次问题。", "provenance": {"dedupeKey": "vps-incident-2"}, "governance": {"lifecycle": "episodic"}, "score": 0.8},
                    {"id": "vps-incident-3", "sourceSet": "filtered-dialogue-daily", "workType": "incident", "textPreview": "VPS 第三次问题。", "provenance": {"dedupeKey": "vps-incident-3"}, "governance": {"lifecycle": "episodic"}, "score": 0.7},
                ],
                "citationPack": [
                    {"citationId": "C1", "resultId": "vps-incident-1", "excerpt": "VPS 第一次问题。"},
                    {"citationId": "C2", "resultId": "vps-incident-2", "excerpt": "VPS 第二次问题。"},
                    {"citationId": "C3", "resultId": "vps-incident-3", "excerpt": "VPS 第三次问题。"},
                ],
                "eventAggregation": {"eventCount": 3},
                "answerSynthesis": {"status": "ready", "summary": "共 3 次 VPS 问题。", "bullets": []},
            },
            "vps-worst-incident-review": {
                "results": [
                    {
                        "id": "vps-worst-incident",
                        "sourceSet": "technical-report-task-events",
                        "workType": "incident",
                        "textPreview": "最严重 VPS 故障已复盘，解决方式是迁移配置并恢复服务。",
                        "provenance": {"dedupeKey": "vps-worst-incident"},
                        "governance": {"lifecycle": "task-history"},
                        "score": 0.86,
                    }
                ],
                "citationPack": [{"citationId": "C1", "resultId": "vps-worst-incident", "excerpt": "最严重 VPS 故障，解决方式是迁移配置。"}],
                "answerSynthesis": {"status": "ready", "summary": "最严重 VPS 故障通过迁移配置解决。", "bullets": []},
            },
            "vps-config-2096-port": {
                "results": [
                    {
                        "id": "vps-port-2096",
                        "sourceSet": "lessons",
                        "workType": "general",
                        "textPreview": "2096 端口不可用，因为目标环境限制该端口。",
                        "provenance": {"dedupeKey": "vps-port-2096"},
                        "governance": {"lifecycle": "canonical"},
                        "score": 0.88,
                    }
                ],
                "citationPack": [{"citationId": "C1", "resultId": "vps-port-2096", "excerpt": "2096 端口不可用。"}],
                "answerSynthesis": {"status": "ready", "summary": "2096 端口不可用。", "bullets": []},
            },
            "vps-migration-cross-time": {
                "results": [
                    {
                        "id": "vps-migration-summary",
                        "sourceSet": "foundation-period-projections",
                        "workType": "general",
                        "textPreview": "从旧 VPS 到精品 VPS 迁移了连接配置、服务配置和验证记录。",
                        "provenance": {"dedupeKey": "vps-migration-summary"},
                        "governance": {"lifecycle": "period-summary"},
                        "score": 0.89,
                    }
                ],
                "citationPack": [{"citationId": "C1", "resultId": "vps-migration-summary", "excerpt": "旧 VPS 到精品 VPS 迁移。"}],
                "answerSynthesis": {"status": "ready", "summary": "从旧 VPS 到精品 VPS 完成迁移。", "bullets": []},
            },
            "dashboard-network-loopback-tailscale": {
                "results": [
                    {
                        "id": "dashboard-network-loopback-tailscale",
                        "sourceSet": "technical-report-task-events",
                        "workType": "config",
                        "textPreview": "dashboard 网络设置包含回环地址策略，未来 tailscale 接入需要 publicBaseUrl 与 allowedOrigins。",
                        "provenance": {"dedupeKey": "dashboard-network-loopback-tailscale"},
                        "governance": {"lifecycle": "task-history"},
                        "score": 0.9,
                    }
                ],
                "citationPack": [
                    {
                        "citationId": "C1",
                        "resultId": "dashboard-network-loopback-tailscale",
                        "excerpt": "dashboard tailscale publicBaseUrl allowedOrigins。",
                    }
                ],
                "answerSynthesis": {"status": "ready", "summary": "dashboard 网络配置证据。", "bullets": []},
            },
            "actanara-batch-c-recoverability": {
                "results": [
                    {
                        "id": "actanara-batch-c-recoverability",
                        "sourceSet": "technical-report-task-events",
                        "workType": "task",
                        "textPreview": "Batch C 运维可恢复性 已完成验证。",
                        "provenance": {"dedupeKey": "actanara-batch-c-recoverability"},
                        "governance": {"lifecycle": "task-history"},
                        "score": 0.9,
                    }
                ],
                "citationPack": [
                    {
                        "citationId": "C1",
                        "resultId": "actanara-batch-c-recoverability",
                        "excerpt": "Batch C 运维可恢复性。",
                    }
                ],
                "answerSynthesis": {"status": "ready", "summary": "Batch C 运维可恢复性。", "bullets": []},
            }
        }

        def search(payload):
            self.assertFalse(payload["includeFullText"])
            query = payload["query"]
            case_id = "rag-current-task" if payload.get("sourceSets") == ["task-board-snapshot"] else "fallback"
            query_cases = {
                "精品 VPS IP 是多少？": "vps-fact-ip",
                "之前 VPS 出现几次问题？": "vps-incident-count",
                "最严重一次 VPS 故障是什么，怎么解决？": "vps-worst-incident-review",
                "2096 端口为什么不可用？": "vps-config-2096-port",
                "从旧 VPS 到精品 VPS 迁移了什么？": "vps-migration-cross-time",
                "dashboard 网络 回环地址 tailscale": "dashboard-network-loopback-tailscale",
                "Batch C 运维可恢复性": "actanara-batch-c-recoverability",
            }
            case_id = query_cases.get(query, case_id)
            return responses.get(
                case_id,
                {
                    "results": [
                        {
                            "id": "generic",
                            "sourceSet": payload.get("sourceSets", ["lessons"])[0],
                            "workType": (payload.get("workType") or ["general"])[0],
                            "governance": {"lifecycle": (payload.get("lifecycle") or ["canonical"])[0], "authorityRank": 90, "provenanceScore": 1.0},
                            "score": 0.5,
                        }
                    ]
                },
            )

        result = run_rag_eval(search_fn=search)
        self.assertEqual(result["schemaVersion"], 2)
        self.assertEqual(result["caseCount"], 13)
        self.assertEqual(result["evaluatedCount"], 12)
        self.assertEqual(result["skippedCount"], 1)
        self.assertEqual(result["unexpectedSkipCount"], 0)
        self.assertEqual(result["profile"], "default")
        self.assertEqual(result["profileDisposition"], "configured")
        self.assertEqual(result["failedCount"], 0)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["sourceCoverage"]["status"], "passed")
        self.assertIn("task-board-snapshot", result["sourceCoverage"]["observedSourceSets"])
        self.assertFalse(result["sourceCoverage"]["missingSourceSets"])
        self.assertEqual(result["metrics"]["recallAtK"], 1.0)
        self.assertEqual(result["metrics"]["recallAt5"], 1.0)
        self.assertEqual(result["metrics"]["recallAt10"], 1.0)
        self.assertEqual(result["metrics"]["mrr"], 1.0)
        self.assertEqual(result["metrics"]["ndcgAt10"], 1.0)
        self.assertIn("latencyP50Ms", result["metrics"])
        self.assertIn("latencyP95Ms", result["metrics"])
        self.assertEqual(result["metrics"]["timeoutRate"], 0.0)
        self.assertLessEqual(result["metrics"]["duplicateRate"], 0.2)
        self.assertEqual(result["variant"]["embeddingModel"], "intfloat/multilingual-e5-small")
        self.assertEqual(result["variant"]["embeddingDimension"], 384)
        self.assertEqual(result["variant"]["rerankerProvider"], "none")
        self.assertEqual(result["variant"]["retrievalTopK"], 8)
        self.assertIn("rag_eval_queries.jsonl", result["benchmarkPaths"][0])
        by_id = {case["id"]: case for case in result["cases"]}
        self.assertEqual(by_id["rag-technical-history"]["status"], "skipped")
        self.assertTrue(by_id["rag-technical-history"]["expectedSkip"])
        self.assertEqual(by_id["rag-technical-history"]["skipReason"], "source-set-not-configured")
        self.assertEqual(by_id["vps-incident-count"]["quality"]["aggregationCorrectness"], 1.0)
        self.assertEqual(
            by_id["dashboard-network-loopback-tailscale"]["quality"]["evidenceTermsMissing"],
            [],
        )
        self.assertIn("technical-report-task-events", by_id["actanara-batch-c-recoverability"]["observed"]["sourceSets"])

        extended_settings = replace(
            resolve_rag_settings(),
            indexing_source_sets=tuple(DEFAULT_INDEXING_SOURCE_SETS) + ("technical-report-task-events",),
        )
        extended = run_rag_eval(settings=extended_settings, search_fn=search, profile="extended")
        self.assertEqual(extended["status"], "passed")
        self.assertEqual(extended["profileDisposition"], "configured")
        self.assertEqual(extended["evaluatedCount"], 13)
        self.assertEqual(extended["skippedCount"], 0)
        self.assertEqual(extended["unexpectedSkipCount"], 0)

        configured_out = run_rag_eval(search_fn=search, profile="extended")
        self.assertEqual(configured_out["status"], "blocked")
        self.assertEqual(configured_out["profileDisposition"], "configured-out")
        self.assertEqual(configured_out["unexpectedSkipCount"], 1)

    def test_rag_eval_runner_reports_missing_source_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmark = Path(tmp) / "bench.jsonl"
            benchmark.write_text(
                json.dumps(
                    {
                        "id": "missing-task-board",
                        "query": "current task",
                        "topK": 3,
                        "expect": {"sourceSets": ["task-board-snapshot"], "minResults": 1},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_rag_eval(
                benchmark_path=benchmark,
                search_fn=lambda _payload: {
                    "results": [
                        {
                            "id": "lesson",
                            "sourceSet": "lessons",
                            "workType": "lesson",
                            "governance": {"lifecycle": "canonical"},
                        }
                    ]
                },
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["sourceCoverage"]["status"], "failed")
        self.assertEqual(result["sourceCoverage"]["method"], "search-result-observed")
        self.assertIn("does not by itself prove the active index is missing", result["sourceCoverage"]["diagnostic"])
        self.assertEqual(result["sourceCoverage"]["missingSourceSets"], ["task-board-snapshot"])
        self.assertEqual(result["sourceCoverage"]["missingCases"][0]["id"], "missing-task-board")

    def test_rag_eval_runner_selects_english_profile_fixtures(self):
        settings = replace(resolve_rag_settings(), language_profile="en")
        responses = {
            "vps-fact-ip": {
                "results": [
                    {
                        "id": "vps-premium-ip",
                        "sourceSet": "lessons",
                        "workType": "general",
                        "textPreview": "The premium VPS IP is recorded for connection configuration.",
                        "provenance": {"dedupeKey": "vps-premium-ip"},
                        "governance": {"lifecycle": "canonical", "authorityRank": 90, "provenanceScore": 1.0},
                        "score": 0.91,
                    }
                ],
                "citationPack": [{"citationId": "C1", "resultId": "vps-premium-ip", "excerpt": "The premium VPS IP is recorded."}],
                "answerSynthesis": {"status": "ready", "summary": "The premium VPS IP is recorded.", "bullets": []},
            },
            "vps-incident-count": {
                "results": [
                    {"id": "vps-incident-1", "sourceSet": "filtered-dialogue-daily", "workType": "incident", "textPreview": "The first VPS incident.", "provenance": {"dedupeKey": "vps-incident-1"}, "governance": {"lifecycle": "episodic"}, "score": 0.9},
                    {"id": "vps-incident-2", "sourceSet": "filtered-dialogue-daily", "workType": "incident", "textPreview": "The second VPS incident.", "provenance": {"dedupeKey": "vps-incident-2"}, "governance": {"lifecycle": "episodic"}, "score": 0.8},
                    {"id": "vps-incident-3", "sourceSet": "filtered-dialogue-daily", "workType": "incident", "textPreview": "The third VPS incident.", "provenance": {"dedupeKey": "vps-incident-3"}, "governance": {"lifecycle": "episodic"}, "score": 0.7},
                ],
                "citationPack": [
                    {"citationId": "C1", "resultId": "vps-incident-1", "excerpt": "The first VPS incident."},
                    {"citationId": "C2", "resultId": "vps-incident-2", "excerpt": "The second VPS incident."},
                    {"citationId": "C3", "resultId": "vps-incident-3", "excerpt": "The third VPS incident."},
                ],
                "eventAggregation": {"eventCount": 3},
                "answerSynthesis": {"status": "ready", "summary": "Found 3 VPS incidents.", "bullets": []},
            },
            "vps-worst-incident-review": {
                "results": [
                    {
                        "id": "vps-worst-incident",
                        "sourceSet": "technical-report-task-events",
                        "workType": "incident",
                        "textPreview": "The worst VPS incident was resolved by migrating configuration and restoring service.",
                        "provenance": {"dedupeKey": "vps-worst-incident"},
                        "governance": {"lifecycle": "task-history"},
                        "score": 0.86,
                    }
                ],
                "citationPack": [{"citationId": "C1", "resultId": "vps-worst-incident", "excerpt": "The worst VPS incident was resolved by migrating configuration."}],
                "answerSynthesis": {"status": "ready", "summary": "The worst VPS incident was resolved.", "bullets": []},
            },
            "vps-config-2096-port": {
                "results": [
                    {
                        "id": "vps-port-2096",
                        "sourceSet": "lessons",
                        "workType": "general",
                        "textPreview": "Port 2096 was unavailable because the target environment blocked that port.",
                        "provenance": {"dedupeKey": "vps-port-2096"},
                        "governance": {"lifecycle": "canonical"},
                        "score": 0.88,
                    }
                ],
                "citationPack": [{"citationId": "C1", "resultId": "vps-port-2096", "excerpt": "Port 2096 was unavailable."}],
                "answerSynthesis": {"status": "ready", "summary": "Port 2096 was unavailable.", "bullets": []},
            },
            "vps-migration-cross-time": {
                "results": [
                    {
                        "id": "vps-migration-summary",
                        "sourceSet": "foundation-period-projections",
                        "workType": "general",
                        "textPreview": "Connection configuration, service settings, and validation records were migrated from the old VPS to the premium VPS.",
                        "provenance": {"dedupeKey": "vps-migration-summary"},
                        "governance": {"lifecycle": "period-summary"},
                        "score": 0.89,
                    }
                ],
                "citationPack": [{"citationId": "C1", "resultId": "vps-migration-summary", "excerpt": "The old VPS to premium VPS migration was completed."}],
                "answerSynthesis": {"status": "ready", "summary": "The old VPS to premium VPS migration was completed.", "bullets": []},
            },
        }

        seen_queries = []

        def search(payload):
            seen_queries.append(payload["query"])
            query_cases = {
                "What is the premium VPS IP?": "vps-fact-ip",
                "How many VPS incidents happened before?": "vps-incident-count",
                "What was the worst VPS incident and how was it resolved?": "vps-worst-incident-review",
                "Why was port 2096 unavailable?": "vps-config-2096-port",
                "What was migrated from the old VPS to the premium VPS?": "vps-migration-cross-time",
            }
            case_id = query_cases.get(payload["query"])
            if case_id:
                return responses[case_id]
            return {
                "results": [
                    {
                        "id": "generic",
                        "sourceSet": payload.get("sourceSets", ["lessons"])[0],
                        "workType": (payload.get("workType") or ["general"])[0],
                        "governance": {"lifecycle": (payload.get("lifecycle") or ["canonical"])[0], "authorityRank": 90, "provenanceScore": 1.0},
                        "score": 0.5,
                    }
                ]
            }

        result = run_rag_eval(settings=settings, search_fn=search)

        self.assertEqual([path.name for path in eval_benchmark_paths("en")], ["rag_eval_queries.jsonl", "rag_eval_queries.en.jsonl"])
        self.assertEqual(result["caseCount"], 11)
        self.assertEqual(result["evaluatedCount"], 10)
        self.assertEqual(result["skippedCount"], 1)
        self.assertEqual(result["unexpectedSkipCount"], 0)
        self.assertEqual(result["failedCount"], 0)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["variant"]["languageProfile"], "en")
        self.assertIn("rag_eval_queries.en.jsonl", result["benchmarkPaths"][1])
        self.assertIn("What is the premium VPS IP?", seen_queries)
        self.assertNotIn("精品 VPS IP 是多少？", seen_queries)


if __name__ == "__main__":
    unittest.main()
