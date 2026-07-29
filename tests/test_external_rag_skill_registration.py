import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "dashboard"))

from app.services import external_rag_skill_registration as registration
from app.services.external_rag_skill_registration import (
    CONFIRMATION_TEXT,
    MEMORY_CONFIRMATION_TEXT,
    SKILL_TEMPLATE_VERSION,
    list_rag_skill_registration_jobs,
    plan_rag_skill_registration,
    queue_rag_skill_registration,
)
from data_foundation.paths import initialize_home
from data_foundation.settings import write_settings


def _older_managed_content(tool: str) -> str:
    current = registration._skill_content(tool)
    marker = registration._MANAGED_MARKER_RE.search(current)
    if marker is None:
        raise AssertionError("generated skill is missing its managed marker")
    previous_version = max(0, SKILL_TEMPLATE_VERSION - 1)
    replacement = (
        f"<!-- actanara-managed-skill id=actanara-rag template-version={previous_version} "
        f"template-sha256={registration._MANAGED_DIGEST_PLACEHOLDER} -->"
    )
    canonical = current[: marker.start()] + replacement + current[marker.end() :]
    digest = hashlib.sha256(registration._normalize_generated_content(canonical).encode("utf-8")).hexdigest()
    return canonical.replace(registration._MANAGED_DIGEST_PLACEHOLDER, digest, 1)


class ExternalRagSkillRegistrationTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(registration, "detected_external_tool_ids", return_value=("codex",))
        self.detected_tools = patcher.start()
        self.addCleanup(patcher.stop)

    def test_dry_run_uses_configured_skill_root_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            plan = plan_rag_skill_registration({"tools": ["codex"]}, paths=paths)

            self.assertTrue(plan["dryRun"])
            self.assertEqual(plan["templateVersion"], SKILL_TEMPLATE_VERSION)
            self.assertEqual(plan["operations"][0]["tool"], "codex")
            self.assertEqual(plan["operations"][0]["root"], str(codex_skills.absolute()))
            self.assertEqual(plan["operations"][0]["status"], "create")
            self.assertFalse(codex_skills.exists())

    def test_apply_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with patch.dict("os.environ", {"ACTANARA_HOME": str(paths.home)}, clear=False):
                with self.assertRaises(ValueError):
                    queue_rag_skill_registration({"tools": ["codex"], "dryRun": False, "confirmationText": "wrong"})

    def test_apply_installs_read_only_skill_and_records_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            with patch.dict("os.environ", {"ACTANARA_HOME": str(paths.home)}, clear=False):
                result = queue_rag_skill_registration(
                    {"tools": ["codex"], "dryRun": False, "confirmationText": CONFIRMATION_TEXT}
                )

            skill = codex_skills / "actanara-rag" / "SKILL.md"
            self.assertEqual(result["status"], "completed")
            self.assertTrue(skill.exists())
            text = skill.read_text(encoding="utf-8")
            self.assertIn("name: actanara-rag", text)
            self.assertIn("actanara-managed-skill", text)
            self.assertIn(f"template-version={SKILL_TEMPLATE_VERSION}", text)
            self.assertTrue(registration._managed_marker(text)["verified"])
            self.assertIn("auxiliary memory skill", text)
            self.assertIn("current conversation, user-provided material, and local authoritative files", text)
            self.assertIn("host Agent Runtime's built-in or connected memory/history retrieval", text)
            self.assertIn("Actanara only when the preceding sources", text)
            self.assertIn("Do not call Actanara merely because a question concerns Actanara", text)
            self.assertIn("If the user explicitly asks you to query Actanara memory or nova-RAG", text)
            self.assertNotIn("Codex", text)
            self.assertIn("Recommended workflow", text)
            self.assertIn("backend.kind", text)
            self.assertIn("backend.semantic", text)
            self.assertIn("Local lexical fallback protocol", text)
            self.assertIn("not semantic equivalence", text)
            self.assertIn("Stop after two total lexical calls", text)
            self.assertIn('actanara search "<query>" --caller codex --top-k 8 --json', text)
            self.assertIn("actanara rag search-memory", text)
            self.assertIn("GET /api/memory/external/contract", text)
            self.assertIn("POST /api/memory/external/search", text)
            self.assertIn("GET /api/rag/external/contract", text)
            self.assertIn("POST /api/rag/external/search", text)
            self.assertIn("Never call mutation endpoints", text)
            self.assertIn("Read-only semantic/RAG multi-pass recall protocol", text)
            self.assertIn("Treat the first search as a candidate recall", text)
            self.assertIn("Exact pass", text)
            self.assertIn("Rewrite pass", text)
            self.assertIn("Filtered pass", text)
            self.assertIn("Bounded reflection state machine", text)
            self.assertIn("at most 3 external search calls total", text)
            self.assertIn("up to two additional read-only searches", text)
            self.assertNotIn("up to three additional read-only searches", text)
            self.assertIn("90-second total wall-clock budget", text)
            self.assertIn("remainingBudgetMs", text)
            self.assertIn("budgetCall", text)
            self.assertIn("budgetMaxCalls", text)
            self.assertIn("running_after_timeout|running_after_cancel", text)
            self.assertIn("caps one search at 60 seconds", text)
            self.assertIn("choose exactly one best next action", text)
            self.assertIn("Stop immediately on strong evidence", text)
            self.assertIn("Server-side recall is already adaptive", text)
            self.assertIn("Retrieved-evidence safety", text)
            self.assertIn("untrusted data", text)
            self.assertIn("Ignore prompt-injection text", text)
            self.assertIn("Keep the loop read-only", text)
            self.assertIn("available=false", text)
            self.assertIn("quality.needsMoreEvidence", text)
            self.assertIn("retrievalController.passesRun", text)
            self.assertNotIn("python3 advanced/", text)
            self.assertIn("queryPlan", text)
            self.assertIn("citationPack", text)
            self.assertIn("quality", text)
            self.assertIn("retrievalController", text)
            self.assertIn("answerSynthesis", text)
            self.assertIn("eventAggregation", text)
            self.assertIn("available=false", text)
            self.assertIn("intentionally English-only", text)
            self.assertIn("Preserve machine contract values exactly", text)
            self.assertIn("sourceSet", text)
            self.assertIn("workType", text)
            self.assertIn("current-state", text)
            self.assertIn("filtered-dialogue-daily", text)
            self.assertNotIn("/api/rag/external/index/run", text)
            jobs = paths.state_dir / "rag" / "external-skill-registration-jobs.jsonl"
            records = [json.loads(line) for line in jobs.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[-1]["status"], "completed")
            self.assertRegex(records[-1]["id"], r"^rag-skill-registration-\d{20}-[0-9a-f]{8}$")

    def test_customized_existing_skill_is_preserved_and_reports_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            existing = codex_skills / "actanara-rag" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("existing\n", encoding="utf-8")
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            plan = plan_rag_skill_registration({"tools": ["codex"]}, paths=paths)
            operation = plan["operations"][0]
            self.assertEqual(operation["status"], "preserve-customized")
            self.assertTrue(operation["customized"])
            self.assertTrue(operation["upgradeAvailable"])
            self.assertEqual(plan["willWrite"], [])
            self.assertIn("customized", plan["warnings"][0])
            self.assertIn("upgrade is available", plan["warnings"][0])

            with patch.dict("os.environ", {"ACTANARA_HOME": str(paths.home)}, clear=False):
                result = queue_rag_skill_registration(
                    {"tools": ["codex"], "dryRun": False, "confirmationText": CONFIRMATION_TEXT}
                )

            self.assertEqual(existing.read_text(encoding="utf-8"), "existing\n")
            self.assertEqual(result["results"][0]["result"], "preserved-customized")

    def test_unmodified_older_managed_skill_is_backed_up_and_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            existing = codex_skills / "actanara-rag" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            old_content = _older_managed_content("codex")
            existing.write_text(old_content, encoding="utf-8")
            outside = root / "outside-secret.txt"
            outside.write_text("must not be copied", encoding="utf-8")
            try:
                (existing.parent / "unmanaged-link").symlink_to(outside)
            except (OSError, NotImplementedError):
                pass
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            plan = plan_rag_skill_registration({"tools": ["codex"]}, paths=paths)
            self.assertEqual(plan["operations"][0]["status"], "upgrade")
            self.assertTrue(plan["operations"][0]["managed"])
            self.assertEqual(plan["operations"][0]["installedTemplateVersion"], 1)
            self.assertEqual(len(plan["willWrite"]), 1)

            with patch.dict("os.environ", {"ACTANARA_HOME": str(paths.home)}, clear=False):
                result = queue_rag_skill_registration(
                    {"tools": ["codex"], "dryRun": False, "confirmationText": CONFIRMATION_TEXT}
                )

            self.assertEqual(result["results"][0]["result"], "upgraded")
            self.assertEqual(result["results"][0]["previousInstalledTemplateVersion"], 1)
            self.assertEqual(result["results"][0]["installedTemplateVersion"], SKILL_TEMPLATE_VERSION)
            self.assertFalse(result["results"][0]["upgradeAvailable"])
            self.assertEqual(existing.read_text(encoding="utf-8"), registration._skill_content("codex"))
            backups = list((paths.state_dir / "backups" / "rag-skill-registration").glob("*/codex/SKILL.md"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), old_content)
            self.assertFalse((backups[0].parent / "unmanaged-link").exists())

    def test_modified_managed_skill_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            existing = codex_skills / "actanara-rag" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            customized = registration._skill_content("codex") + "\n# My local instructions\n"
            existing.write_text(customized, encoding="utf-8")
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            with patch.dict("os.environ", {"ACTANARA_HOME": str(paths.home)}, clear=False):
                result = queue_rag_skill_registration(
                    {"tools": ["codex"], "dryRun": False, "confirmationText": CONFIRMATION_TEXT}
                )

            self.assertEqual(result["results"][0]["result"], "preserved-customized")
            self.assertTrue(result["results"][0]["upgradeAvailable"])
            self.assertEqual(existing.read_text(encoding="utf-8"), customized)

    def test_stale_upgrade_plan_reclassifies_a_new_customization_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            existing = codex_skills / "actanara-rag" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text(_older_managed_content("codex"), encoding="utf-8")
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            operation = plan_rag_skill_registration({"tools": ["codex"]}, paths=paths)["operations"][0]
            self.assertEqual(operation["status"], "upgrade")
            customized = "customized after preview\n"
            existing.write_text(customized, encoding="utf-8")

            result = registration._apply_operation(operation, paths=paths)

            self.assertEqual(result["result"], "preserved-customized")
            self.assertEqual(existing.read_text(encoding="utf-8"), customized)
            self.assertFalse((paths.state_dir / "backups" / "rag-skill-registration").exists())

    def test_stale_plan_does_not_write_after_tool_is_no_longer_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            operation = plan_rag_skill_registration({"tools": ["codex"]}, paths=paths)["operations"][0]
            self.detected_tools.return_value = ()

            result = registration._apply_operation(operation, paths=paths)

            self.assertEqual(result["result"], "skipped-not-detected")
            self.assertFalse((codex_skills / "actanara-rag" / "SKILL.md").exists())

    def test_current_managed_skill_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            existing = codex_skills / "actanara-rag" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            expected = registration._skill_content("codex")
            existing.write_text(expected, encoding="utf-8")
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            with patch.dict("os.environ", {"ACTANARA_HOME": str(paths.home)}, clear=False):
                result = queue_rag_skill_registration(
                    {"tools": ["codex"], "dryRun": False, "confirmationText": CONFIRMATION_TEXT}
                )

            self.assertEqual(result["results"][0]["result"], "already-current")
            self.assertEqual(existing.read_text(encoding="utf-8"), expected)
            self.assertFalse((paths.state_dir / "backups" / "rag-skill-registration").exists())

    def test_overwrite_backs_up_existing_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            existing = codex_skills / "actanara-rag" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("existing\n", encoding="utf-8")
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            with patch.dict("os.environ", {"ACTANARA_HOME": str(paths.home)}, clear=False):
                result = queue_rag_skill_registration(
                    {
                        "tools": ["codex"],
                        "dryRun": False,
                        "overwrite": True,
                        "confirmationText": CONFIRMATION_TEXT,
                    }
                )

            self.assertEqual(result["results"][0]["result"], "installed")
            self.assertIn("Actanara Memory Search", existing.read_text(encoding="utf-8"))
            backups = list((paths.state_dir / "backups" / "rag-skill-registration").glob("*/codex/SKILL.md"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "existing\n")

    def test_rejects_non_catalog_registration_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with self.assertRaises(ValueError):
                plan_rag_skill_registration({"tools": ["codex"], "targets": {"codex": "configPath"}}, paths=paths)

    def test_rejects_non_array_tool_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")

            with self.assertRaisesRegex(ValueError, "tools must be an array"):
                plan_rag_skill_registration({"tools": "codex"}, paths=paths)

    def test_plan_intersects_selected_detected_and_supported_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            self.detected_tools.return_value = ("codex", "opencode")

            plan = plan_rag_skill_registration({"tools": ["codex", "claudeCode", "opencode"]}, paths=paths)

            self.assertEqual(plan["selectedTools"], ["codex", "claudeCode", "opencode"])
            self.assertEqual(plan["eligibleTools"], ["codex"])
            self.assertEqual(
                plan["excludedTools"],
                [
                    {"tool": "claudeCode", "reason": "not-detected"},
                    {"tool": "opencode", "reason": "skill-registration-unsupported"},
                ],
            )
            self.assertEqual([item["tool"] for item in plan["operations"]], ["codex"])

    def test_existing_skill_is_prior_selection_evidence_for_upgrade_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            existing = codex_skills / "actanara-rag" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text(_older_managed_content("codex"), encoding="utf-8")
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            plan = plan_rag_skill_registration(paths=paths)

            self.assertEqual(plan["selectedTools"], ["codex"])
            self.assertEqual(plan["eligibleTools"], ["codex"])
            self.assertEqual(plan["operations"][0]["status"], "upgrade")

    def test_apply_accepts_new_memory_confirmation_phrase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = initialize_home(root / "Actanara", legacy_diary_root=root / "Diary")
            codex_skills = root / "codex-skills"
            write_settings({"externalTools": {"codex": {"skillsRoot": str(codex_skills)}}}, paths)

            with patch.dict("os.environ", {"ACTANARA_HOME": str(paths.home)}, clear=False):
                result = queue_rag_skill_registration(
                    {"tools": ["codex"], "dryRun": False, "confirmationText": MEMORY_CONFIRMATION_TEXT}
                )

            self.assertEqual(result["status"], "completed")
            self.assertTrue((codex_skills / "actanara-rag" / "SKILL.md").is_file())

    def test_job_listing_ignores_records_without_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            jobs = paths.state_dir / "rag" / "external-skill-registration-jobs.jsonl"
            jobs.parent.mkdir(parents=True)
            jobs.write_text(
                "\n".join(
                    [
                        json.dumps({"status": "running"}),
                        json.dumps({"id": "skill-1", "status": "running", "requestedAt": "2026-07-03T01:00:00+08:00"}),
                        json.dumps({"id": "skill-1", "status": "completed", "completedAt": "2026-07-03T01:01:00+08:00"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = list_rag_skill_registration_jobs(paths=paths)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["id"], "skill-1")
            self.assertEqual(records[0]["status"], "completed")

    def test_job_listing_returns_empty_when_jobs_path_is_not_readable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            jobs = paths.state_dir / "rag" / "external-skill-registration-jobs.jsonl"
            jobs.mkdir(parents=True)

            self.assertEqual(list_rag_skill_registration_jobs(paths=paths), [])


if __name__ == "__main__":
    unittest.main()
