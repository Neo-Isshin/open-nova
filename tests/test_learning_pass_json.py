import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.llm_json import parse_llm_json_object
from data_foundation.diary_paths import diary_learning_report_path
from data_foundation.db import migrate
from data_foundation.infrastructure import infrastructure_events_for_date, list_infrastructure_entities
from data_foundation.paths import initialize_home
from diary_generator import learning_pass


class LearningPassJsonTests(unittest.TestCase):
    def setUp(self):
        self._stdout_capture = redirect_stdout(io.StringIO())
        self._stdout_capture.__enter__()

    def tearDown(self):
        self._stdout_capture.__exit__(None, None, None)

    def test_parser_accepts_fenced_json(self):
        parsed = parse_llm_json_object('```json\n{"lessons": [], "infra": []}\n```')
        self.assertEqual(parsed.data, {"lessons": [], "infra": []})

    def test_parser_extracts_object_from_surrounding_text(self):
        parsed = parse_llm_json_object('前言\n{"lessons": [{"text": "x"}], "infra": []}\n说明')
        self.assertEqual(parsed.data["lessons"][0]["text"], "x")

    def test_learning_pass_writes_markdown_and_jsonl_from_structured_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            home = root / "Actanara"
            markdown = """# 2026-05-23 智慧沉淀与基建审计

## 🧠 黄金教训 (Lessons)
### 【codex】LLM JSON 输出不稳定
#### 问题
Learning Pass 要求模型直接返回严格 JSON，偶发格式漂移会导致解析失败。
#### 根因
LLM 更擅长生成自然文本和 Markdown，跨 provider 时 JSON 标点与包裹文本容易漂移。
#### 建议
改为结构化 Markdown contract，并由程序解析为 JSONL。

## 📡 基建变动 (Infrastructure)
| 对象 | 变动描述 | 当前值 |
| :--- | :--- | :--- |
| Learning Pass | 输出协议 | `structured-markdown` |
"""
            with (
                patch("diary_generator.learning_pass.call_llm", return_value=markdown),
                patch(
                    "diary_generator.learning_pass.load_paths",
                    return_value=type(
                        "Paths",
                        (),
                        {"home": home, "diary_dir": diary_root, "legacy_diary_root": None},
                    )(),
                ),
                patch("diary_generator.learning_pass.config.ACTANARA_HOME", home),
            ):
                self.assertTrue(learning_pass.process_learning("2026-05-23", "summary"))

            report = diary_learning_report_path(diary_root, "2026-05-23")
            self.assertTrue(report.exists())
            self.assertIn("#### 根因", report.read_text(encoding="utf-8"))
            lessons = (home / "artifacts" / "learning" / "lessons.jsonl").read_text(encoding="utf-8")
            self.assertIn('"rootCause"', lessons)
            self.assertIn("跨 provider", lessons)
            infra = (diary_root / "infrastructure.jsonl").read_text(encoding="utf-8")
            self.assertIn("structured-markdown", infra)
            self.assertNotIn("`structured-markdown", infra)

    def test_learning_pass_updates_infrastructure_graph_from_strict_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara")
            migrate(paths)
            markdown = """# 2026-07-03 智慧沉淀与基建审计

## 🧠 黄金教训 (Lessons)
无

## 📡 基建变动 (Infrastructure)
| 实体ID | 类型 | 对象 | 宿主/位置 | 变动类型 | 字段 | 变动描述 | 当前值 | 证据 | 置信度 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| new | service | Dashboard server | Mac mini | port_changed | port | Dashboard server port confirmed | `3036` | technical report says dashboard listens on 3036 | high |
| new | service | RAG API | Mac mini | credential_rotated | credential | RAG API credential rotated | `token=raw-secret` | technical report says token=raw-secret | high |
"""
            with (
                patch("diary_generator.learning_pass.call_llm", return_value=markdown),
                patch("diary_generator.learning_pass.load_paths", return_value=paths),
                patch("diary_generator.learning_pass.config.ACTANARA_HOME", paths.home),
            ):
                self.assertTrue(learning_pass.process_learning("2026-07-03", "summary"))

            entities = {item["name"]: item for item in list_infrastructure_entities(paths)}
            self.assertEqual(entities["Dashboard server"]["port"], "3036")
            events = infrastructure_events_for_date(paths, "2026-07-03")
            self.assertEqual(len(events), 2)
            by_name = {event["name"]: event for event in events}
            self.assertEqual(by_name["Dashboard server"]["currentValue"], "3036")
            self.assertEqual(by_name["RAG API"]["currentValue"], "[redacted]")
            self.assertNotIn("raw-secret", " ".join(by_name["RAG API"]["evidence"]))

    def test_learning_pass_repairs_invalid_markdown_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary_root = root / "Diary"
            home = root / "Actanara"
            repaired = """# 2026-05-23 智慧沉淀与基建审计

## 🧠 黄金教训 (Lessons)
无

## 📡 基建变动 (Infrastructure)
无
"""
            with (
                patch("diary_generator.learning_pass.call_llm", side_effect=["not markdown", repaired]),
                patch(
                    "diary_generator.learning_pass.load_paths",
                    return_value=type(
                        "Paths",
                        (),
                        {"home": home, "diary_dir": diary_root, "legacy_diary_root": None},
                    )(),
                ),
                patch("diary_generator.learning_pass.config.ACTANARA_HOME", home),
            ):
                self.assertTrue(learning_pass.process_learning("2026-05-23", "summary"))

            report = diary_learning_report_path(diary_root, "2026-05-23")
            self.assertTrue(report.exists())
            debug_files = sorted((home / "state" / "logs" / "learning-pass").glob("2026-05-23-*.txt"))
            self.assertEqual(len(debug_files), 1)

    def test_learning_pass_fails_after_markdown_repair_parse_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Actanara"
            with (
                patch("diary_generator.learning_pass.call_llm", side_effect=["not markdown", "still not markdown"]),
                patch("diary_generator.learning_pass.load_paths", return_value=type("Paths", (), {"diary_dir": root / "Diary"})()),
                patch("diary_generator.learning_pass.config.ACTANARA_HOME", home),
            ):
                with self.assertRaises(learning_pass.LearningPassError):
                    learning_pass.process_learning("2026-05-23", "summary")
            debug_files = sorted((home / "state" / "logs" / "learning-pass").glob("2026-05-23-*.txt"))
            self.assertEqual(len(debug_files), 2)


if __name__ == "__main__":
    unittest.main()
