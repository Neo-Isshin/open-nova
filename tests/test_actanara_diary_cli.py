import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ["ACTANARA_SECRET_BACKEND"] = "memory"


def _load_cli_module():
    module_path = ROOT / "src" / "data_foundation" / "operator_cli.py"
    spec = importlib.util.spec_from_file_location("actanara_cli", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _update_result_line(**overrides):
    payload = {
        "schemaVersion": 1,
        "status": "completed",
        "updateMode": "reuse-existing-venv",
        "dependenciesInstalled": False,
        "reusesRuntimeVenv": True,
        "sourceUpdated": True,
        "reason": "dependency fingerprint matched",
        "cacheUsed": False,
        "servicesStopped": True,
        "plannedDependenciesInstall": False,
        "managedServiceDefinitionsNormalized": None,
        "rollbackComplete": None,
        "stateCertain": True,
        "stage": "complete",
    }
    payload.update(overrides)
    return "ACTANARA_UPDATE_RESULT_JSON=" + json.dumps(payload, sort_keys=True) + "\n"


@dataclass(frozen=True)
class _FakePipelineResult:
    business_date: str
    succeeded_steps: int
    total_steps: int
    success: bool
    failed_step: str | None = None


class ActanaraCliTests(unittest.TestCase):
    def test_no_args_prints_product_command_guide(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main([])

        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Actanara", text)
        self.assertIn("Start here:", text)
        self.assertIn("Create and find:", text)
        self.assertIn("actanara doctor", text)
        self.assertIn("actanara pipeline [YYMMDD|YYYY-MM-DD]", text)
        self.assertIn("actanara rag-update", text)
        self.assertIn("actanara search \"query\"", text)
        self.assertIn("actanara dashboard restart", text)
        self.assertNotIn("service boundaries", text)

    def test_doctor_top_level_uses_settings_status(self):
        cli = _load_cli_module()
        payload = {"summary": {"errors": 0}}
        with (
            patch.object(cli, "actanara_settings_status", return_value=payload) as status,
            patch.object(cli, "format_actanara_settings_status", return_value="Actanara · System status\n") as formatter,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["doctor"])

        self.assertEqual(code, 0)
        status.assert_called_once_with(None, doctor_profile="all")
        formatter.assert_called_once_with(payload)
        self.assertIn("Actanara · System status", output.getvalue())

    def test_bare_product_groups_keep_their_default_actions(self):
        cli = _load_cli_module()
        parser = cli._parser()

        self.assertIs(parser.parse_args(["model"]).handler, cli._model_show)
        self.assertIs(parser.parse_args(["onboard"]).handler, cli._onboarding_doctor)
        self.assertIs(parser.parse_args(["config"]).handler, cli._config_show)

    def test_model_show_prints_llm_provider(self):
        cli = _load_cli_module()
        with (
            patch.object(
                cli,
                "read_llm_provider",
                return_value={"provider": "openai-compatible", "model": "daily-model", "api": "openai-compatible", "hasApiKey": True},
            ) as provider,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["model", "show"])

        self.assertEqual(code, 0)
        provider.assert_called_once_with(None, persist_defaults=False)
        self.assertIn("Actanara · AI model", output.getvalue())
        self.assertIn("openai-compatible", output.getvalue())
        self.assertIn("daily-model", output.getvalue())

    def test_model_list_prints_provider_catalog(self):
        cli = _load_cli_module()
        catalog = [
            {
                "id": "minimax-cn",
                "name": "MiniMax CN",
                "api": "anthropic-messages",
                "models": [{"id": "MiniMax-M2.7-highspeed"}, {"id": "MiniMax-M3"}],
            }
        ]
        with (
            patch.object(cli, "read_llm_provider", return_value={"provider": "minimax-cn", "model": "MiniMax-M3", "catalog": catalog}) as provider,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["model", "list"])

        self.assertEqual(code, 0)
        provider.assert_called_once_with(None, persist_defaults=False)
        self.assertIn("minimax-cn", output.getvalue())
        self.assertIn("MiniMax-M2.7-highspeed", output.getvalue())

    def test_model_set_uses_llm_provider_boundary(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "write_llm_provider", return_value={"provider": "custom", "model": "m"}) as write,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(
                [
                    "model",
                    "set",
                    "--provider",
                    "custom",
                    "--model",
                    "m",
                    "--endpoint",
                    "https://llm.invalid",
                    "--api-key-env",
                    "CUSTOM_LLM_KEY",
                ]
            )

        self.assertEqual(code, 0)
        write.assert_called_once_with(
            {
                "provider": "custom",
                "model": "m",
                "endpoint": "https://llm.invalid",
                "apiKeyEnv": "CUSTOM_LLM_KEY",
            },
            None,
        )
        self.assertIn("Actanara · AI model updated", output.getvalue())
        self.assertIn("custom", output.getvalue())
        self.assertIn("Model    m", output.getvalue())

    def test_model_set_switches_an_explicit_catalog_provider_from_custom_to_preset(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = str(Path(tmp) / "Actanara")
            with redirect_stdout(io.StringIO()):
                custom_code = cli.main(
                    [
                        "model",
                        "set",
                        "--runtime",
                        runtime,
                        "--provider",
                        "custom",
                        "--model",
                        "closeout-smoke",
                        "--endpoint",
                        "http://127.0.0.1:63185/v1",
                        "--api",
                        "openai-compatible",
                    ]
                )
            with redirect_stdout(io.StringIO()) as output:
                preset_code = cli.main(
                    [
                        "model",
                        "set",
                        "--runtime",
                        runtime,
                        "--provider",
                        "minimax-cn",
                        "--model",
                        "MiniMax-M2.5",
                        "--json",
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(custom_code, 0)
        self.assertEqual(preset_code, 0)
        self.assertEqual(payload["mode"], "preset")
        self.assertEqual(payload["provider"], "minimax-cn")
        self.assertEqual(payload["model"], "MiniMax-M2.5")
        self.assertEqual(payload["endpoint"], "https://api.minimaxi.com")
        self.assertEqual(payload["api"], "anthropic-messages")

    def test_config_set_rejects_protected_groups(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(["config", "set", "llmProvider.model", "bad"])

        self.assertEqual(code, 2)
        self.assertIn("protected settings groups", error.getvalue())

    def test_config_keys_lists_writable_and_protected_groups(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["config", "keys", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertIn("general", payload["writableGroups"])
        self.assertIn("llmProvider", payload["protectedGroups"])
        self.assertEqual(payload["dedicatedCommands"]["llmProvider"], "actanara model ...")

    def test_search_top_level_uses_external_memory_facade(self):
        cli = _load_cli_module()
        payload = {"available": True, "results": [{"textPreview": "memory"}]}
        with (
            patch.object(cli, "search_memory", return_value=payload) as search,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["search", "memory policy", "--top-k", "3", "--json"])

        self.assertEqual(code, 0)
        search.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())["results"][0]["textPreview"], "memory")

    def test_pipeline_short_command_accepts_yymmdd(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "_daily_diary_complete_for_cli", return_value=False),
            patch.object(cli, "run_daily_pipeline", return_value=_FakePipelineResult("2026-04-06", 3, 3, True)) as run,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["pipeline", "260406"])

        self.assertEqual(code, 0)
        run.assert_called_once_with("2026-04-06", paths=None)
        self.assertIn("Actanara · Daily diary", output.getvalue())
        self.assertIn("2026-04-06", output.getvalue())

    def test_pipeline_short_command_accepts_runtime(self):
        cli = _load_cli_module()
        from data_foundation.paths import initialize_home

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with (
                patch.object(cli, "_daily_diary_complete_for_cli", return_value=False),
                patch.object(cli, "run_daily_pipeline", return_value=_FakePipelineResult("2026-04-06", 3, 3, True)) as run,
                redirect_stdout(io.StringIO()),
            ):
                code = cli.main(["pipeline", "--runtime", str(paths.home), "260406"])

        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], "2026-04-06")
        self.assertEqual(run.call_args.kwargs["paths"].home, paths.home)

    def test_pipeline_existing_diary_requires_force(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "_daily_diary_complete_for_cli", return_value=True) as complete,
            patch.object(cli, "run_daily_pipeline") as run,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["pipeline", "260406"])

        self.assertEqual(code, 2)
        complete.assert_called_once_with(None, "2026-04-06")
        run.assert_not_called()
        self.assertIn("already complete", error.getvalue())
        self.assertIn("--force", error.getvalue())

    def test_pipeline_force_existing_diary_uses_manual_regeneration_frozen(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "_daily_diary_complete_for_cli", return_value=True) as complete,
            patch.object(cli, "run_daily_pipeline", return_value=_FakePipelineResult("2026-04-06", 3, 3, True)) as run,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["pipeline", "260406", "--force"])

        self.assertEqual(code, 0)
        complete.assert_not_called()
        run.assert_called_once_with(
            "2026-04-06",
            paths=None,
            trigger="manual-regeneration-frozen",
            reuse_foundation_inputs=True,
        )
        self.assertIn("Actanara · Daily diary", output.getvalue())
        self.assertIn("2026-04-06", output.getvalue())

    def test_dashboard_restart_uses_launch_agent_boundary(self):
        cli = _load_cli_module()
        with (
            patch.object(cli.platform, "system", return_value="Darwin"),
            patch.object(cli, "dashboard_launch_defaults", return_value={"label": "com.actanara.dashboard"}) as defaults,
            patch.object(cli, "restart_dashboard_service", return_value=0) as restart,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["dashboard", "restart"])

        self.assertEqual(code, 0)
        defaults.assert_called_once()
        restart.assert_called_once_with("com.actanara.dashboard")
        self.assertIn("Actanara · Dashboard", output.getvalue())
        self.assertIn("Restarted", output.getvalue())

    def test_dashboard_restart_uses_systemd_user_boundary_on_linux(self):
        cli = _load_cli_module()
        with (
            patch.object(cli.platform, "system", return_value="Linux"),
            patch.object(cli, "restart_dashboard_systemd_service", return_value={"status": "running"}) as restart,
            patch.object(cli, "dashboard_launch_defaults") as defaults,
            patch.object(cli, "restart_dashboard_service") as launchctl_restart,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["dashboard", "restart", "--runtime", "/tmp/Actanara"])

        self.assertEqual(code, 0)
        restart.assert_called_once()
        self.assertEqual(restart.call_args.args[0].home, Path("/tmp/Actanara"))
        defaults.assert_not_called()
        launchctl_restart.assert_not_called()
        self.assertIn("Restarted", output.getvalue())

    def test_dashboard_restart_reports_linux_systemd_failure_without_traceback_or_launchctl(self):
        cli = _load_cli_module()
        with (
            patch.object(cli.platform, "system", return_value="Linux"),
            patch.object(cli, "restart_dashboard_systemd_service", side_effect=OSError("system bus unavailable")),
            patch.object(cli, "restart_dashboard_service") as launchctl_restart,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["dashboard", "restart"])

        self.assertEqual(code, 1)
        launchctl_restart.assert_not_called()
        self.assertIn("system bus unavailable", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

    def test_pipeline_rejects_positional_run_with_date_flag(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "run_daily_pipeline", return_value=_FakePipelineResult("2026-04-06", 3, 3, True)) as run,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["pipeline", "run", "--date", "260406"])

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("provide the date once", error.getvalue())

    def test_pipeline_rejects_invalid_yymmdd_date(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "run_daily_pipeline") as run,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["pipeline", "260231"])

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertRegex(error.getvalue(), r"day is out of range|day 31 must be in range")

    def test_scheduler_reconcile_blocked_confirmation_is_successful_command(self):
        cli = _load_cli_module()
        payload = {"status": "blocked", "missingCount": 4, "missingDates": ["2026-06-20"], "requiresConfirmation": True}
        with (
            patch.object(cli, "reconcile_pipeline_schedule", return_value=payload) as reconcile,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["scheduler", "reconcile", "--apply"])

        self.assertEqual(code, 0)
        reconcile.assert_called_once()
        self.assertIn("Review the missed dates", output.getvalue())

    def test_task_counts_use_nova_task_authority(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "diary_tasks_snapshot", return_value={"InProgress": 2, "Completed": 5}) as snapshot,
            patch.object(cli, "pending_candidate_count", return_value=3) as pending,
            patch.object(cli, "load_paths") as load_paths,
            redirect_stdout(io.StringIO()) as output,
        ):
            load_paths.return_value.home = Path("/tmp/actanara")
            code = cli.main(["task", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        snapshot.assert_called_once()
        pending.assert_called_once()
        self.assertEqual(payload["authority"], "Nova-Task v2 SQLite")
        self.assertEqual(payload["total"], 7)
        self.assertEqual(payload["pendingCandidates"], 3)

    def test_rag_rebuild_defaults_to_plan_without_sync(self):
        cli = _load_cli_module()
        rag_settings = object()
        plan = {
            "action": "rag-rebuild",
            "dryRun": True,
            "status": "plan",
            "reason": "backend planner",
            "confirmationTextRequired": "REBUILD AND PROMOTE ACTANARA RAG",
            "mutationPolicy": {"candidateBuilt": False, "activeSnapshotPromoted": False},
        }
        with (
            patch.object(cli, "resolve_rag_settings", return_value=rag_settings) as resolve,
            patch.object(cli, "plan_v2_production_sync", return_value=plan) as planner,
            patch.object(cli, "sync_v2_production_index") as sync,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["rag-rebuild", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "plan")
        self.assertEqual(payload["confirmationTextRequired"], "REBUILD AND PROMOTE ACTANARA RAG")
        planner.assert_called_once_with(
            rag_settings,
            action="rag-rebuild",
            requested_by="actanara-cli-rag-rebuild",
            promote=True,
            confirmation_text="REBUILD AND PROMOTE ACTANARA RAG",
        )
        resolve.assert_called_once_with(None)
        sync.assert_not_called()

    def test_rag_update_confirm_runs_candidate_sync_with_promote(self):
        cli = _load_cli_module()
        rag_settings = object()
        with (
            patch.object(cli, "resolve_rag_settings", return_value=rag_settings) as resolve,
            patch.object(cli, "sync_v2_production_index", return_value={"status": "promoted"}) as sync,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["rag-update", "--confirm", "UPDATE AND PROMOTE ACTANARA RAG"])

        self.assertEqual(code, 0)
        resolve.assert_called_once_with(None)
        sync.assert_called_once_with(rag_settings, requested_by="actanara-cli-rag-update", promote=True)
        self.assertIn("Actanara · Memory refreshed", output.getvalue())
        self.assertIn("Ready", output.getvalue())

    def test_rag_update_confirm_honors_runtime_argument(self):
        cli = _load_cli_module()
        candidate_paths = object()
        rag_settings = object()
        with (
            patch.object(cli, "_paths_from_args", return_value=candidate_paths) as paths_from_args,
            patch.object(cli, "resolve_rag_settings", return_value=rag_settings) as resolve,
            patch.object(cli, "sync_v2_production_index", return_value={"status": "promoted"}) as sync,
            redirect_stdout(io.StringIO()),
        ):
            code = cli.main(
                [
                    "rag-update",
                    "--runtime",
                    "/tmp/actanara-candidate",
                    "--confirm",
                    "UPDATE AND PROMOTE ACTANARA RAG",
                ]
            )

        self.assertEqual(code, 0)
        paths_from_args.assert_called_once()
        resolve.assert_called_once_with(candidate_paths)
        sync.assert_called_once_with(rag_settings, requested_by="actanara-cli-rag-update", promote=True)

    def test_rag_update_wrong_confirmation_returns_usage_error(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "resolve_rag_settings", return_value=object()),
            patch.object(cli, "plan_v2_production_sync") as planner,
            patch.object(cli, "sync_v2_production_index") as sync,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["rag-update", "--confirm", "wrong"])

        self.assertEqual(code, 2)
        planner.assert_not_called()
        sync.assert_not_called()
        self.assertIn("confirmation must exactly match", error.getvalue())

    def test_update_defaults_to_plan_without_running_bootstrap(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.subprocess, "run") as run,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["update", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ready")
        self.assertFalse(payload["apply"])
        self.assertEqual(payload["sourceSelection"]["mode"], "latest-stable-release")
        self.assertEqual(
            payload["sourceSelection"]["policy"],
            "resolve latest stable Release and pin the resolved commit",
        )
        self.assertTrue(payload["sourceSelection"]["commitPinnedByBootstrap"])
        self.assertIn("--upgrade", payload["command"])
        self.assertIn("--result-json", payload["command"])
        self.assertIn("/tmp/actanara", payload["command"])
        self.assertNotIn("--ref", payload["command"])
        self.assertFalse(payload["mutationPolicy"]["managedServicesStoppedBeforePortSelection"])
        self.assertFalse(payload["mutationPolicy"]["managedServicesStoppedAfterPreflight"])
        self.assertFalse(payload["mutationPolicy"]["settingsMutated"])
        self.assertFalse(payload["mutationPolicy"]["schedulerChanged"])
        self.assertFalse(payload["mutationPolicy"]["managedServiceDefinitionsMayNormalize"])
        self.assertIsNone(payload["mutationPolicy"]["reusesRuntimeVenv"])
        self.assertTrue(payload["mutationPolicy"]["preservesSettingsAndUserData"])
        self.assertEqual(payload["updateMode"], "not-evaluated")
        self.assertFalse(payload["dependenciesInstalled"])
        self.assertFalse(payload["sourceUpdated"])
        self.assertFalse(payload["servicesStopped"])
        self.assertIsNone(payload["plannedDependenciesInstall"])
        self.assertFalse(payload["resultAvailable"])
        self.assertEqual(payload["stage"], "plan")
        run.assert_not_called()

    def test_update_help_describes_latest_stable_release_commit_pinning(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as raised:
            cli.main(["update", "--help"])

        self.assertEqual(raised.exception.code, 0)
        text = output.getvalue()
        self.assertIn("latest stable release", text)
        self.assertIn("exact 40- or 64-character version ID", " ".join(text.split()))
        normalized = " ".join(text.split())
        self.assertIn("Requires --source-root PATH or an exact --ref already downloaded", normalized)
        self.assertIn("previously downloaded software", normalized)

    def test_update_dry_run_invokes_bootstrap_without_mutation(self):
        cli = _load_cli_module()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": "dry\n"
                + _update_result_line(
                    updateMode="rebuild-candidate-venv",
                    reusesRuntimeVenv=False,
                    sourceUpdated=False,
                    reason="active dependency manifest missing",
                    servicesStopped=False,
                    plannedDependenciesInstall=True,
                    stage="preflight",
                ),
                "stderr": "",
            },
        )()
        with (
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.shutil, "which", return_value="/bin/zsh"),
            patch.object(cli.subprocess, "run", return_value=completed) as run,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["update", "--dry-run", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "completed")
        command = run.call_args.args[0]
        self.assertIn("--dry-run", command)
        self.assertIn("--upgrade", command)
        self.assertIn("--runtime", command)
        self.assertIn("/tmp/actanara", command)
        self.assertFalse(payload["mutationPolicy"]["sourceUpdated"])
        self.assertFalse(payload["mutationPolicy"]["settingsMutated"])
        self.assertFalse(payload["mutationPolicy"]["schedulerChanged"])
        self.assertIsNone(payload["mutationPolicy"]["managedServiceDefinitionsMayNormalize"])
        self.assertFalse(payload["mutationPolicy"]["reusesRuntimeVenv"])
        self.assertEqual(payload["updateMode"], "rebuild-candidate-venv")
        self.assertFalse(payload["dependenciesInstalled"])
        self.assertFalse(payload["sourceUpdated"])
        self.assertFalse(payload["servicesStopped"])
        self.assertTrue(payload["plannedDependenciesInstall"])
        self.assertEqual(payload["reason"], "active dependency manifest missing")
        self.assertTrue(payload["resultAvailable"])
        self.assertEqual(payload["stage"], "preflight")

    def test_update_apply_json_reports_actual_installer_reuse_result(self):
        cli = _load_cli_module()
        candidate_paths = type("Paths", (), {"home": Path("/tmp/actanara-candidate")})()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": "upgrade complete\n" + _update_result_line(),
                "stderr": "",
            },
        )()
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            bootstrap = source_root / "install" / "bootstrap.sh"
            bootstrap.parent.mkdir(parents=True)
            bootstrap.write_text("#!/usr/bin/env zsh\n", encoding="utf-8")
            with (
                patch.object(cli.platform, "system", return_value="Darwin"),
                patch.object(cli, "_paths_from_args", return_value=candidate_paths),
                patch.object(cli, "read_settings", return_value={}),
                patch.object(cli.shutil, "which", return_value="/bin/zsh"),
                patch.object(cli.subprocess, "run", return_value=completed) as run,
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(
                    [
                        "update",
                        "--apply",
                        "--json",
                        "--runtime",
                        str(candidate_paths.home),
                        "--source-root",
                        str(source_root),
                    ]
                )

        payload = json.loads(output.getvalue())
        policy = payload["mutationPolicy"]
        self.assertEqual(code, 0)
        self.assertNotIn("env", run.call_args.kwargs)
        self.assertFalse(policy["dependenciesInstalled"])
        self.assertTrue(policy["sourceUpdated"])
        self.assertTrue(policy["managedServicesStoppedAfterPreflight"])
        self.assertIsNone(policy["managedServiceDefinitionsMayNormalize"])
        self.assertFalse(policy["settingsMutated"])
        self.assertFalse(policy["schedulerChanged"])
        self.assertTrue(policy["reusesRuntimeVenv"])
        self.assertTrue(policy["preservesSettingsAndUserData"])
        self.assertEqual(payload["updateMode"], "reuse-existing-venv")
        self.assertFalse(payload["dependenciesInstalled"])
        self.assertTrue(payload["reusesRuntimeVenv"])
        self.assertTrue(payload["sourceUpdated"])
        self.assertFalse(payload["cacheUsed"])
        self.assertTrue(payload["servicesStopped"])
        self.assertFalse(payload["plannedDependenciesInstall"])
        self.assertEqual(payload["reason"], "dependency fingerprint matched")

    def test_update_bootstrap_command_forwards_source_only_and_offline_to_both_layers(self):
        cli = _load_cli_module()
        with patch.object(cli.shutil, "which", return_value="/bin/zsh"), patch.object(
            cli, "read_settings", return_value={}
        ):
            args = cli._parser().parse_args(
                ["update", "--dry-run", "--source-only", "--offline", "--source-root", str(ROOT)]
            )
            command = cli._update_bootstrap_command(args, Path("/tmp/actanara"))

        separator = command.index("--")
        self.assertIn("--offline", command[:separator])
        self.assertIn("--offline", command[separator + 1 :])
        self.assertEqual(command.count("--offline"), 2)
        self.assertIn("--source-only", command[separator + 1 :])
        self.assertNotIn("--upgrade", command[separator + 1 :])
        self.assertIn("--result-json", command[separator + 1 :])

    def test_update_bootstrap_command_forwards_force_rebuild_with_upgrade(self):
        cli = _load_cli_module()
        with patch.object(cli.shutil, "which", return_value="/bin/zsh"), patch.object(
            cli, "read_settings", return_value={}
        ):
            args = cli._parser().parse_args(["update", "--force-rebuild", "--source-root", str(ROOT)])
            command = cli._update_bootstrap_command(args, Path("/tmp/actanara"))

        installer_args = command[command.index("--") + 1 :]
        self.assertIn("--upgrade", installer_args)
        self.assertIn("--force-rebuild", installer_args)
        self.assertIn("--result-json", installer_args)
        self.assertNotIn("--source-only", installer_args)

    def test_linux_update_uses_posix_adapter_and_active_runtime_python(self):
        cli = _load_cli_module()
        runtime = Path("/tmp/actanara-linux-runtime")
        with (
            patch.object(cli.platform, "system", return_value="Linux"),
            patch.object(cli.shutil, "which", side_effect=lambda name: "/bin/sh" if name == "sh" else None),
            patch.object(cli, "read_settings", return_value={}),
        ):
            args = cli._parser().parse_args(
                ["update", "--source-only", "--source-root", str(ROOT)]
            )
            command = cli._update_bootstrap_command(args, runtime)

        separator = command.index("--")
        self.assertEqual(command[0], "/bin/sh")
        self.assertEqual(Path(command[1]).name, "bootstrap-linux.sh")
        self.assertEqual(
            command[command.index("--python") + 1],
            str(runtime / ".venv" / "bin" / "python"),
        )
        self.assertIn("--source-only", command[separator + 1 :])
        self.assertIn("--result-json", command[separator + 1 :])

    def test_linux_remote_update_clears_ambient_bootstrap_source_selection(self):
        cli = _load_cli_module()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": _update_result_line(sourceUpdated=False, stage="preflight"),
                "stderr": "",
            },
        )()
        ambient = {
            "ACTANARA_INSTALL_SOURCE_ROOT": "/tmp/adjacent-checkout",
            "ACTANARA_INSTALL_SOURCE_URL": "https://example.invalid/ambient.git",
            "ACTANARA_INSTALL_REF": "b" * 40,
        }
        with (
            patch.dict(os.environ, ambient, clear=False),
            patch.object(cli.platform, "system", return_value="Linux"),
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.shutil, "which", return_value="/bin/sh"),
            patch.object(cli.subprocess, "run", return_value=completed) as run,
            redirect_stdout(io.StringIO()),
        ):
            code = cli.main(["update", "--dry-run", "--ref", "a" * 40, "--json"])

        self.assertEqual(code, 0)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("ACTANARA_INSTALL_SOURCE_ROOT", environment)
        self.assertNotIn("ACTANARA_INSTALL_SOURCE_URL", environment)
        self.assertNotIn("ACTANARA_INSTALL_REF", environment)
        command = run.call_args.args[0]
        self.assertIn("--source-url", command)
        self.assertEqual(command[command.index("--ref") + 1], "a" * 40)

    def test_update_source_only_and_force_rebuild_are_mutually_exclusive(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as raised:
            cli.main(["update", "--source-only", "--force-rebuild"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", error.getvalue())

    def test_update_json_uses_last_fixed_prefix_result_envelope(self):
        cli = _load_cli_module()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": _update_result_line(
                    updateMode="rebuild-candidate-venv",
                    dependenciesInstalled=True,
                    reusesRuntimeVenv=False,
                )
                + "installer progress\n"
                + _update_result_line(),
                "stderr": "",
            },
        )()
        with (
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.subprocess, "run", return_value=completed),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["update", "--apply", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["updateMode"], "reuse-existing-venv")
        self.assertFalse(payload["dependenciesInstalled"])
        self.assertTrue(payload["reusesRuntimeVenv"])

    def test_update_bootstrap_failure_without_envelope_reports_unknown_actuals_and_stage(self):
        cli = _load_cli_module()
        completed = type(
            "Completed",
            (),
            {"returncode": 19, "stdout": "bootstrap progress\n", "stderr": "cache miss\n"},
        )()
        with (
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.subprocess, "run", return_value=completed),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["update", "--apply", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 19)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["stage"], "bootstrap")
        self.assertEqual(payload["updateMode"], "unknown")
        self.assertIsNone(payload["dependenciesInstalled"])
        self.assertIsNone(payload["reusesRuntimeVenv"])
        self.assertIsNone(payload["sourceUpdated"])
        self.assertIsNone(payload["cacheUsed"])
        self.assertIsNone(payload["servicesStopped"])
        self.assertFalse(payload["resultAvailable"])
        self.assertIn("exit status 19", payload["reason"])

    def test_completed_rollback_envelope_restores_certain_state_and_avoids_uncertain_warning(self):
        cli = _load_cli_module()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 19,
                "stdout": "rollback restored prior state\n"
                + _update_result_line(
                    status="failed",
                    sourceUpdated=False,
                    reason="update-failed-rolled-back",
                    managedServiceDefinitionsNormalized=False,
                    rollbackComplete=True,
                    stateCertain=False,
                    stage="rollback-complete",
                ),
                "stderr": "update failed after transaction start\n",
            },
        )()
        with (
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.subprocess, "run", return_value=completed),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["update", "--apply"])

        self.assertEqual(code, 19)
        self.assertIn("rollback restored prior state", output.getvalue())
        self.assertNotIn("could not confirm that recovery finished", error.getvalue())

        with (
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.subprocess, "run", return_value=completed),
            redirect_stdout(io.StringIO()) as json_output,
        ):
            json_code = cli.main(["update", "--apply", "--json"])

        payload = json.loads(json_output.getvalue())
        self.assertEqual(json_code, 19)
        self.assertTrue(payload["rollbackComplete"])
        self.assertTrue(payload["stateCertain"])
        self.assertEqual(payload["stage"], "rollback-complete")

    def test_update_json_preserves_uncertain_state_from_incomplete_rollback(self):
        cli = _load_cli_module()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 70,
                "stdout": "rollback warning\n"
                + _update_result_line(
                    status="failed",
                    sourceUpdated=None,
                    reason="update-rollback-incomplete",
                    managedServiceDefinitionsNormalized=None,
                    rollbackComplete=False,
                    stateCertain=False,
                    stage="rollback-incomplete",
                ),
                "stderr": "inspect preserved journal\n",
            },
        )()
        with (
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.subprocess, "run", return_value=completed),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["update", "--apply", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 70)
        self.assertTrue(payload["resultAvailable"])
        self.assertIsNone(payload["sourceUpdated"])
        self.assertFalse(payload["rollbackComplete"])
        self.assertFalse(payload["stateCertain"])
        self.assertEqual(payload["stage"], "rollback-incomplete")
        self.assertEqual(payload["reason"], "update-rollback-incomplete")
        self.assertIsNone(payload["mutationPolicy"]["sourceUpdated"])
        self.assertIsNone(
            payload["mutationPolicy"]["managedServiceDefinitionsMayNormalize"]
        )

    def test_update_human_warns_when_incomplete_rollback_leaves_state_uncertain(self):
        cli = _load_cli_module()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 70,
                "stdout": "installer progress\n"
                + _update_result_line(
                    status="failed",
                    sourceUpdated=None,
                    reason="update-rollback-incomplete",
                    managedServiceDefinitionsNormalized=None,
                    rollbackComplete=False,
                    stateCertain=False,
                    stage="rollback-incomplete",
                ),
                "stderr": "inspect preserved journal\n",
            },
        )()
        with (
            patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": Path("/tmp/actanara")})()),
            patch.object(cli, "read_settings", return_value={}),
            patch.object(cli.subprocess, "run", return_value=completed),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["update", "--apply"])

        self.assertEqual(code, 70)
        self.assertIn("installer progress", output.getvalue())
        self.assertNotIn("ACTANARA_UPDATE_RESULT_JSON=", output.getvalue())
        self.assertIn("Actanara · Update failed", error.getvalue())
        self.assertIn("could not confirm that recovery finished", error.getvalue())
        self.assertIn("actanara doctor --installer", error.getvalue())
        self.assertNotIn("update-rollback-incomplete", error.getvalue())

    def test_update_apply_validates_rag_profile_without_configuration_flags(self):
        cli = _load_cli_module()
        candidate_paths = type("Paths", (), {"home": Path("/tmp/actanara-candidate")})()
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "installer progress\n" + _update_result_line(), "stderr": ""},
        )()
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source checkout"
            bootstrap = source_root / "install" / "bootstrap.sh"
            bootstrap.parent.mkdir(parents=True)
            bootstrap.write_text("#!/usr/bin/env zsh\n", encoding="utf-8")
            with (
                patch.object(cli.platform, "system", return_value="Darwin"),
                patch.object(cli, "_paths_from_args", return_value=candidate_paths) as paths_from_args,
                patch.object(
                    cli,
                    "read_settings",
                    return_value={
                        "features": {"rag": True},
                        "rag": {"enabled": True, "embedding": {"mode": "local"}},
                        "externalTools": {"installerV2SkillRegistration": {"supportedNow": True}},
                    },
                ) as read_settings,
                patch.object(cli.shutil, "which", return_value="/bin/zsh"),
                patch.object(cli.subprocess, "run", return_value=completed) as run,
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(
                    [
                        "update",
                        "--apply",
                        "--runtime",
                        "/tmp/actanara-candidate",
                        "--source-root",
                        str(source_root),
                    ]
                )

        self.assertEqual(code, 0)
        paths_from_args.assert_called_once()
        read_settings.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/bin/zsh")
        self.assertEqual(command[1], str(bootstrap))
        self.assertIn("--source-root", command)
        self.assertIn(str(source_root), command)
        self.assertNotIn("--ref", command)
        self.assertIn("--upgrade", command)
        self.assertIn("--yes", command)
        self.assertIn("/tmp/actanara-candidate", command)
        self.assertNotIn("--enable-rag", command)
        self.assertNotIn("--rag-embedding-mode", command)
        self.assertNotIn("--register-rag-skills", command)
        human_output = output.getvalue()
        self.assertIn("Actanara · Update", human_output)
        self.assertIn("installer progress", human_output)
        self.assertIn("Actanara · Update complete", human_output)
        self.assertNotIn("reuse-existing-venv", human_output)
        self.assertNotIn("dependencies installed=", human_output)
        self.assertNotIn("source updated=", human_output)
        self.assertNotIn("ACTANARA_UPDATE_RESULT_JSON=", human_output)

    def test_update_fails_closed_when_runtime_dependency_profile_cannot_be_read(self):
        cli = _load_cli_module()
        candidate_paths = type("Paths", (), {"home": Path("/tmp/actanara-candidate")})()
        with (
            patch.object(cli, "_paths_from_args", return_value=candidate_paths),
            patch.object(cli, "read_settings", side_effect=OSError("synthetic unreadable settings")),
            patch.object(cli.subprocess, "run") as run,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(
                [
                    "update",
                    "--apply",
                    "--runtime",
                    "/tmp/actanara-candidate",
                    "--source-root",
                    str(ROOT),
                ]
            )

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("active dependency profile cannot be preserved safely", error.getvalue())
        self.assertNotIn("synthetic unreadable settings", error.getvalue())

    def test_update_fails_closed_for_ambiguous_rag_dependency_profile(self):
        cli = _load_cli_module()
        candidate_paths = type("Paths", (), {"home": Path("/tmp/actanara-candidate")})()
        with (
            patch.object(cli, "_paths_from_args", return_value=candidate_paths),
            patch.object(
                cli,
                "read_settings",
                return_value={
                    "features": {"rag": True},
                    "rag": {"enabled": True, "embedding": {"mode": "unknown"}},
                },
            ),
            patch.object(cli.subprocess, "run") as run,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(
                [
                    "update",
                    "--dry-run",
                    "--runtime",
                    "/tmp/actanara-candidate",
                    "--source-root",
                    str(ROOT),
                ]
            )

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("supported RAG embedding dependency profile", error.getvalue())

    def test_update_rejects_source_root_with_ref_before_bootstrap(self):
        cli = _load_cli_module()
        with (
            patch.object(cli.subprocess, "run") as run,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(
                [
                    "update",
                    "--dry-run",
                    "--source-root",
                    str(ROOT),
                    "--ref",
                    "a" * 40,
                ]
            )

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("choose either a local copy or a version", error.getvalue())
        self.assertNotIn("--source-root cannot be combined", error.getvalue())

    def test_update_offline_requires_local_source_or_cached_full_ref_before_bootstrap(self):
        cli = _load_cli_module()
        for mode in ([], ["--dry-run"], ["--apply"]):
            with self.subTest(mode=mode), patch.object(cli.subprocess, "run") as run, redirect_stderr(
                io.StringIO()
            ) as error:
                code = cli.main(["update", *mode, "--offline", "--json"])

            self.assertEqual(code, 2)
            run.assert_not_called()
            self.assertIn("offline update needs a local copy", error.getvalue())
            self.assertNotIn("installer source cache", error.getvalue())

    def test_update_remote_ref_requires_full_hex_commit(self):
        cli = _load_cli_module()
        invalid_refs = ["main", "v9.9.9", "abc1234", "a" * 39, "g" * 40, "a" * 63]
        for ref in invalid_refs:
            with self.subTest(ref=ref), patch.object(cli.subprocess, "run") as run, redirect_stderr(io.StringIO()) as error:
                code = cli.main(["update", "--dry-run", "--ref", ref])

            self.assertEqual(code, 2)
            run.assert_not_called()
            self.assertIn("version ID must contain exactly 40 or 64 hexadecimal characters", error.getvalue())

        for ref in ("a" * 40, "B" * 64):
            with self.subTest(ref=ref), patch.object(cli.shutil, "which", return_value="/bin/zsh"):
                args = cli._parser().parse_args(["update", "--dry-run", "--ref", ref])
                command = cli._update_bootstrap_command(args, Path("/tmp/actanara"))

            self.assertEqual(command[command.index("--ref") + 1], ref)

    def test_update_custom_source_url_requires_explicit_full_commit(self):
        cli = _load_cli_module()
        with patch.object(cli.subprocess, "run") as run, redirect_stderr(io.StringIO()) as error:
            code = cli.main(
                [
                    "update",
                    "--dry-run",
                    "--source-url",
                    "https://example.invalid/actanara.git",
                ]
            )

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("choose an exact version", error.getvalue())

    def test_update_missing_bootstrap_fails_closed_without_subprocess(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            missing_root = Path(tmp) / "installed-package"
            with (
                patch.object(cli, "ROOT", missing_root),
                patch.object(cli, "load_paths", return_value=type("Paths", (), {"home": runtime})()),
                patch.object(cli.subprocess, "run") as run,
                redirect_stderr(io.StringIO()) as error,
            ):
                code = cli.main(["update", "--dry-run"])

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("Actanara's update files are missing", error.getvalue())
        self.assertNotIn("installer bootstrap", error.getvalue())

    def test_update_from_installed_package_uses_active_runtime_bootstrap(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            bootstrap = runtime / "app" / "source" / "install" / "bootstrap.sh"
            bootstrap.parent.mkdir(parents=True)
            bootstrap.write_text("#!/usr/bin/env zsh\n", encoding="utf-8")
            args = cli._parser().parse_args(["update", "--dry-run"])
            with (
                patch.object(cli.platform, "system", return_value="Darwin"),
                patch.object(cli, "ROOT", Path(tmp) / "installed" / "lib" / "python3.12"),
            ):
                command = cli._update_bootstrap_command(args, runtime)

        self.assertEqual(command[1], str(bootstrap))
        self.assertIn("--dry-run", command)
        self.assertIn("--source-url", command)

    def test_upgrade_alias_is_not_available(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as raised:
                cli.main(["upgrade", "--json"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", error.getvalue())

    def test_foundation_approve_diary_metrics_dry_run_reads_current_report(self):
        cli = _load_cli_module()
        from data_foundation.paths import initialize_home

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            report_path = paths.state_dir / "migration" / "diary-metrics-readiness-2026-06-23.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "status": "table_metrics_mismatch",
                        "tableMetrics": {"differences": {"openclaw": {"total_tokens": 10}}},
                        "canEnable": {"diaryMetricsSourceFoundation": False},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(cli, "write_diary_metrics_table_mismatch_approval") as approve,
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(
                    [
                        "foundation",
                        "approve-diary-metrics",
                        "--runtime",
                        str(paths.home),
                        "--dry-run",
                        "--json",
                        "260623",
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["businessDate"], "2026-06-23")
        self.assertEqual(payload["status"], "plan")
        self.assertTrue(payload["dryRun"])
        self.assertTrue(payload["hasTableDifferences"])
        self.assertFalse(payload["mutationPolicy"]["approvalAuditAppended"])
        approve.assert_not_called()

    def test_foundation_approve_diary_metrics_confirm_records_approval_and_rebuilds_readiness(self):
        cli = _load_cli_module()
        from data_foundation.paths import initialize_home

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            report_path = paths.state_dir / "migration" / "diary-metrics-readiness-2026-06-23.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "status": "table_metrics_mismatch",
                        "tableMetrics": {"differences": {"openclaw": {"total_tokens": 10}}},
                        "canEnable": {"diaryMetricsSourceFoundation": False},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    cli,
                    "write_diary_metrics_table_mismatch_approval",
                    return_value={"differencesDigest": "digest", "approvalPath": "approval.jsonl"},
                ) as approve,
                patch.object(
                    cli,
                    "write_diary_metrics_readiness_report",
                    return_value={"status": "ready_with_operator_approved_table_metrics_change", "canEnable": {"diaryMetricsSourceFoundation": True}},
                ) as readiness,
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(
                    [
                        "foundation",
                        "approve-diary-metrics",
                        "--runtime",
                        str(paths.home),
                        "--operator",
                        "release-gate",
                        "--note",
                        "known frozen mismatch",
                        "--confirm",
                        "APPROVE ACTANARA DIARY METRICS MISMATCH",
                        "--json",
                        "2026-06-23",
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "approved")
        self.assertFalse(payload["dryRun"])
        self.assertTrue(payload["mutationPolicy"]["approvalAuditAppended"])
        self.assertTrue(payload["mutationPolicy"]["readinessReportRegenerated"])
        self.assertFalse(payload["mutationPolicy"]["sourceFactsChanged"])
        self.assertFalse(payload["mutationPolicy"]["sqliteUsageRowsChanged"])
        approve.assert_called_once()
        self.assertEqual(approve.call_args.kwargs["operator"], "release-gate")
        self.assertEqual(approve.call_args.kwargs["note"], "known frozen mismatch")
        readiness.assert_called_once()
        self.assertTrue(readiness.call_args.kwargs["approve_model_usage_normalization"])
        self.assertTrue(readiness.call_args.kwargs["approve_session_count_normalization"])

    def test_foundation_approve_diary_metrics_wrong_confirmation_is_usage_error(self):
        cli = _load_cli_module()
        from data_foundation.paths import initialize_home

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            report_path = paths.state_dir / "migration" / "diary-metrics-readiness-2026-06-23.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "status": "table_metrics_mismatch",
                        "tableMetrics": {"differences": {"openclaw": {"total_tokens": 10}}},
                        "canEnable": {"diaryMetricsSourceFoundation": False},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(cli, "write_diary_metrics_table_mismatch_approval") as approve,
                patch.object(cli, "write_diary_metrics_readiness_report") as readiness,
                redirect_stderr(io.StringIO()) as error,
            ):
                code = cli.main(
                    [
                        "foundation",
                        "approve-diary-metrics",
                        "--runtime",
                        str(paths.home),
                        "--confirm",
                        "wrong",
                        "2026-06-23",
                    ]
                )

        self.assertEqual(code, 2)
        approve.assert_not_called()
        readiness.assert_not_called()
        self.assertIn("confirmation must exactly match", error.getvalue())

    def test_secrets_set_llm_api_key_reads_stdin_without_plaintext_settings(self):
        cli = _load_cli_module()
        from data_foundation.paths import initialize_home
        from data_foundation.settings import read_settings, resolve_llm_provider

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with (
                patch("sys.stdin", io.StringIO("secret-from-stdin\n")),
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(
                    [
                        "secrets",
                        "set-llm-api-key",
                        "--runtime",
                        str(paths.home),
                        "--value-stdin",
                        "--json",
                    ]
                )

            payload = json.loads(output.getvalue())
            raw = read_settings(paths, redact_secrets=False)
            resolved = resolve_llm_provider(paths, redact_secrets=False)

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "stored")
        self.assertEqual(payload["backend"], "memory")
        self.assertEqual(raw["llmProvider"]["apiKey"], "")
        self.assertEqual(raw["llmProvider"]["secretRef"]["backend"], "memory")
        self.assertEqual(resolved["apiKey"], "secret-from-stdin")

    def test_model_key_json_without_runtime_reports_selected_runtime(self):
        cli = _load_cli_module()
        from data_foundation.paths import initialize_home

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with (
                patch.object(cli, "load_paths", return_value=paths),
                patch("sys.stdin", io.StringIO("secret-from-stdin\n")),
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(["model", "key", "--value-stdin", "--json"])

            payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "stored")
        self.assertEqual(payload["runtime"], str(paths.home))
        self.assertEqual(payload["backend"], "memory")

    def test_readonly_runtime_commands_do_not_initialize_missing_runtime(self):
        cli = _load_cli_module()

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "MissingRuntime"
            commands = [
                ["config", "show", "--runtime", str(runtime), "--json"],
                ["config", "get", "general.timezone", "--runtime", str(runtime)],
                ["model", "show", "--runtime", str(runtime), "--json"],
                ["settings", "doctor", "--runtime", str(runtime), "--json"],
            ]
            results = []
            for command in commands:
                with self.subTest(command=command), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    results.append(cli.main(command))
                    self.assertFalse(runtime.exists())

        self.assertEqual(results[:3], [0, 0, 0])
        self.assertIn(results[3], (0, 1))

    def test_secrets_set_llm_api_key_readonly_backend_fails_without_plaintext_settings(self):
        cli = _load_cli_module()
        from data_foundation.paths import initialize_home
        from data_foundation.settings import read_settings

        with tempfile.TemporaryDirectory() as tmp:
            paths = initialize_home(Path(tmp) / "Actanara", legacy_diary_root=Path(tmp) / "Diary")
            with (
                patch.dict(os.environ, {"ACTANARA_SECRET_BACKEND": "process-env"}),
                patch("sys.stdin", io.StringIO("secret-from-stdin\n")),
                redirect_stderr(io.StringIO()) as error,
            ):
                code = cli.main(
                    [
                        "secrets",
                        "set-llm-api-key",
                        "--runtime",
                        str(paths.home),
                        "--value-stdin",
                    ]
                )
            raw = read_settings(paths, redact_secrets=False)

        self.assertEqual(code, 1)
        self.assertIn("read-only", error.getvalue())
        self.assertEqual(raw["llmProvider"]["apiKey"], "")
        self.assertNotIn("secretRef", raw["llmProvider"])

    def test_settings_status_prints_readonly_status(self):
        cli = _load_cli_module()
        payload = {"summary": {"errors": 0}}
        with (
            patch.object(cli, "actanara_settings_status", return_value=payload) as status,
            patch.object(cli, "format_actanara_settings_status", return_value="Actanara · System status\n") as formatter,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["settings", "status"])

        self.assertEqual(code, 0)
        status.assert_called_once_with(None, doctor_profile="all")
        formatter.assert_called_once_with(payload)
        self.assertIn("Actanara · System status", output.getvalue())

    def test_settings_doctor_json_returns_nonzero_for_errors(self):
        cli = _load_cli_module()
        payload = {"summary": {"errors": 1}}
        with (
            patch.object(cli, "actanara_settings_status", return_value=payload),
            patch.object(cli, "dump_actanara_settings_status_json", return_value='{"summary":{"errors":1}}'),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["settings", "doctor", "--json"])

        self.assertEqual(code, 1)
        self.assertIn('"errors":1', output.getvalue())

    def test_foundation_rebuild_sqlite_cache_defaults_to_dry_run_without_confirmation(self):
        cli = _load_cli_module()
        from data_foundation.paths import initialize_home

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diary = root / "Diary"
            (diary / "diary-2026-06-07").mkdir(parents=True)
            (diary / "diary-2026-06-07" / "日记-260607.md").write_text("# 日记\n", encoding="utf-8")
            paths = initialize_home(root / "Actanara", legacy_diary_root=diary)
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main([
                    "foundation",
                    "rebuild-sqlite-cache",
                    "--runtime",
                    str(paths.home),
                    "--json",
                ])

            payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertTrue(payload["dryRun"])
        self.assertTrue(payload["dangerous"])
        self.assertEqual(payload["confirmationTextRequired"], "REBUILD ACTANARA SQLITE CACHE")

    def test_onboarding_doctor_prints_readonly_status(self):
        cli = _load_cli_module()
        payload = {"readiness": {"status": "warn"}}
        with (
            patch.object(cli, "actanara_onboarding_status", return_value=payload) as status,
            patch.object(cli, "format_actanara_onboarding_status", return_value="Actanara · Setup status\n") as formatter,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["onboarding", "doctor"])

        self.assertEqual(code, 0)
        status.assert_called_once_with(None, selected_profiles=None)
        formatter.assert_called_once_with(payload)
        self.assertIn("Actanara · Setup status", output.getvalue())

    def test_onboarding_doctor_json_returns_nonzero_for_errors(self):
        cli = _load_cli_module()
        payload = {"readiness": {"status": "error"}}
        with (
            patch.object(cli, "actanara_onboarding_status", return_value=payload),
            patch.object(cli, "dump_actanara_onboarding_status_json", return_value='{"readiness":{"status":"error"}}'),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["onboarding", "doctor", "--json"])

        self.assertEqual(code, 1)
        self.assertIn('"status":"error"', output.getvalue())

    def test_onboarding_plan_prints_readonly_plan(self):
        cli = _load_cli_module()
        payload = {"summary": {"status": "ready"}}
        with (
            patch.object(cli, "onboarding_subsystem_plan", return_value=payload) as plan,
            patch.object(cli, "format_onboarding_subsystem_plan", return_value="Actanara · Setup preview\n") as formatter,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["onboarding", "plan", "--profile", "dashboard", "--profile", "nova-rag"])

        self.assertEqual(code, 0)
        plan.assert_called_once_with(["dashboard", "nova-rag"], None)
        formatter.assert_called_once_with(payload)
        self.assertIn("Actanara · Setup preview", output.getvalue())

    def test_onboarding_plan_unknown_profile_returns_usage_error(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "onboarding_subsystem_plan", side_effect=ValueError("unknown onboarding profile(s): bad")),
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["onboarding", "plan", "--profile", "bad"])

        self.assertEqual(code, 2)
        self.assertIn("unknown onboarding profile", error.getvalue())

    def test_onboarding_one_liner_dry_run_prints_readonly_schema(self):
        cli = _load_cli_module()
        payload = {"summary": {"status": "ready"}}
        with (
            patch.object(cli, "onboarding_one_liner_dry_run", return_value=payload) as dry_run,
            patch.object(cli, "format_onboarding_one_liner_dry_run", return_value="Actanara · Setup preview\n") as formatter,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["onboarding", "runtime-dry-run", "--profile", "nova-rag"])

        self.assertEqual(code, 0)
        dry_run.assert_called_once_with(["nova-rag"], None)
        formatter.assert_called_once_with(payload)
        self.assertIn("Actanara · Setup preview", output.getvalue())

    def test_onboarding_one_liner_dry_run_json_returns_usage_error_for_unknown_profile(self):
        cli = _load_cli_module()
        with (
            patch.object(cli, "onboarding_one_liner_dry_run", side_effect=ValueError("unknown onboarding profile(s): bad")),
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["onboarding", "runtime-dry-run", "--profile", "bad", "--json"])

        self.assertEqual(code, 2)
        self.assertIn("unknown onboarding profile", error.getvalue())

    def test_onboarding_release_gate_prints_blocked_gate_report(self):
        cli = _load_cli_module()
        payload = {"status": "blocked", "selectedProfiles": ["actanara"], "summary": {"passed": 3, "blocked": 2, "failed": 0}, "blockingGates": ["apply-preflight"]}
        with (
            patch.object(cli, "onboarding_release_gate", return_value=payload) as release_gate,
            patch.object(cli, "format_onboarding_release_gate", return_value="Actanara · Setup readiness\n") as formatter,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["onboarding", "release-gate", "--profile", "nova-rag", "--confirmation-text", "APPLY ACTANARA ONBOARDING"])

        self.assertEqual(code, 1)
        release_gate.assert_called_once_with(["nova-rag"], None, confirmation_text="APPLY ACTANARA ONBOARDING")
        formatter.assert_called_once_with(payload)
        self.assertIn("Actanara · Setup readiness", output.getvalue())

    def test_onboarding_release_gate_json_is_readonly_and_blocked(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(
                [
                    "onboarding",
                    "release-gate",
                    "--confirmation-text",
                    "APPLY ACTANARA ONBOARDING",
                    "--json",
                ]
            )

        payload = json.loads(output.getvalue())

        self.assertEqual(code, 1)
        self.assertTrue(payload["readOnly"])
        self.assertTrue(payload["releaseGateOnly"])
        self.assertEqual(payload["status"], "blocked")
        self.assertTrue(payload["confirmationAccepted"])
        self.assertIn("apply-preflight", payload["blockingGates"])
        self.assertNotIn("scheduler-registration", payload["blockingGates"])
        self.assertIn("sandbox-apply-harness", {gate["id"] for gate in payload["gates"]})
        self.assertIn("runtime-bootstrap-apply", {gate["id"] for gate in payload["gates"]})
        self.assertIn("default-runtime-target", {gate["id"] for gate in payload["gates"]})
        self.assertIn("active-runtime-selection", {gate["id"] for gate in payload["gates"]})
        self.assertIn("scheduler-managed-plist-serialization", {gate["id"] for gate in payload["gates"]})
        self.assertIn("scheduler-plist-write-gate", {gate["id"] for gate in payload["gates"]})
        self.assertTrue(payload["sourcePayloads"]["sandboxApplyHarnessIncluded"])
        self.assertTrue(payload["sourcePayloads"]["runtimeBootstrapApplyIncluded"])
        self.assertTrue(payload["sourcePayloads"]["defaultRuntimeTargetIncluded"])
        self.assertTrue(payload["sourcePayloads"]["activeRuntimeSelectionIncluded"])
        self.assertTrue(payload["sourcePayloads"]["schedulerManagedPlistSerializationIncluded"])
        self.assertTrue(payload["sourcePayloads"]["schedulerPlistWriteGateIncluded"])

    def test_onboarding_release_gate_unknown_profile_returns_usage_error(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(["onboarding", "release-gate", "--profile", "bad"])

        self.assertEqual(code, 2)
        self.assertIn("unknown onboarding profile", error.getvalue())

    def test_onboarding_approval_checklist_prints_required_approvals(self):
        cli = _load_cli_module()
        payload = {
            "status": "approval-required",
            "selectedProfiles": ["actanara"],
            "summary": {"requiredBeforeImplementation": 2, "blockingGates": 1},
            "operatorApprovalItems": [{"id": "approve-settings-writes", "label": "Settings writes"}],
        }
        with (
            patch.object(cli, "onboarding_approval_packet", return_value=payload) as approval_packet,
            patch.object(cli, "format_onboarding_approval_packet", return_value="Actanara · Setup confirmation\n") as formatter,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(["onboarding", "approval-checklist", "--profile", "nova-rag", "--confirmation-text", "APPLY ACTANARA ONBOARDING"])

        self.assertEqual(code, 1)
        approval_packet.assert_called_once_with(["nova-rag"], None, confirmation_text="APPLY ACTANARA ONBOARDING")
        formatter.assert_called_once_with(payload)
        self.assertIn("Actanara · Setup confirmation", output.getvalue())

    def test_onboarding_approval_checklist_json_is_readonly(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(
                [
                    "onboarding",
                    "approval-checklist",
                    "--confirmation-text",
                    "APPLY ACTANARA ONBOARDING",
                    "--json",
                ]
            )

        payload = json.loads(output.getvalue())

        self.assertEqual(code, 1)
        self.assertTrue(payload["readOnly"])
        self.assertTrue(payload["approvalPacketOnly"])
        self.assertEqual(payload["status"], "approval-required")
        self.assertFalse(payload["implementationReadiness"]["readyForWriteImplementation"])
        self.assertIn("approve-settings-writes", payload["implementationReadiness"]["requiredApprovalItems"])
        self.assertIn("no dependency installation without explicit approval", payload["nonNegotiableBoundaries"])

    def test_onboarding_approval_checklist_unknown_profile_returns_usage_error(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(["onboarding", "approval-checklist", "--profile", "bad"])

        self.assertEqual(code, 2)
        self.assertIn("unknown onboarding profile", error.getvalue())

    def test_onboarding_apply_is_blocked_skeleton(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["onboarding", "apply", "--profile", "nova-rag"])

        self.assertEqual(code, 1)
        self.assertIn("Actanara · Setup", output.getvalue())
        self.assertIn("Needs attention", output.getvalue())
        self.assertIn("Setup was not changed", output.getvalue())
        self.assertNotIn("writesSettings", output.getvalue())

    def test_onboarding_apply_json_has_no_side_effect_policy(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(["onboarding", "apply", "--runtime", str(runtime), "--json"])

            payload = json.loads(output.getvalue())

        self.assertEqual(code, 1)
        self.assertFalse(runtime.exists())
        self.assertTrue(payload["blocked"])
        self.assertTrue(payload["noSideEffects"])
        self.assertFalse(payload["executionPolicy"]["writesSettings"])
        self.assertFalse(payload["executionPolicy"]["writesLaunchdPlist"])
        self.assertFalse(payload["executionPolicy"]["callsLaunchctl"])
        self.assertFalse(payload["executionPolicy"]["installsDependencies"])
        self.assertTrue(payload["applyWriteContract"]["readOnly"])
        self.assertFalse(payload["applyWriteContract"]["writesAllowed"])
        self.assertFalse(payload["applyWriteContract"]["auditPlan"]["writesAudit"])
        self.assertFalse(payload["applyWriteContract"]["rollbackPlan"]["writesAllowed"])
        self.assertFalse(payload["applyPreflight"]["confirmationProvided"])
        self.assertFalse(payload["applyPreflight"]["confirmationAccepted"])
        self.assertFalse(payload["applyPreflight"]["allowedToApply"])
        self.assertIn("exact-confirmation", payload["applyPreflight"]["blockingReasons"])

    def test_onboarding_apply_confirmation_is_preflight_only_and_still_blocked(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--runtime",
                        str(runtime),
                        "--confirmation-text",
                        "APPLY ACTANARA ONBOARDING",
                        "--json",
                    ]
                )

            payload = json.loads(output.getvalue())

        self.assertEqual(code, 1)
        self.assertFalse(runtime.exists())
        self.assertTrue(payload["applyPreflight"]["confirmationProvided"])
        self.assertTrue(payload["applyPreflight"]["confirmationAccepted"])
        self.assertFalse(payload["applyPreflight"]["allowedToApply"])
        self.assertIn("apply-implementation-blocked", payload["applyPreflight"]["blockingReasons"])
        self.assertFalse(payload["executionPolicy"]["allowed"])
        self.assertFalse(payload["executionPolicy"]["writesSettings"])

    def test_onboarding_apply_confirm_alias_does_not_bypass_exact_phrase(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["onboarding", "apply", "--confirm", "yes", "--json"])

        payload = json.loads(output.getvalue())

        self.assertEqual(code, 1)
        self.assertTrue(payload["applyPreflight"]["confirmationProvided"])
        self.assertFalse(payload["applyPreflight"]["confirmationAccepted"])
        self.assertIn("exact-confirmation", payload["applyPreflight"]["blockingReasons"])

    def test_onboarding_apply_sandbox_requires_explicit_runtime(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(["onboarding", "apply", "--sandbox-apply", "--confirmation-text", "APPLY ACTANARA ONBOARDING", "--json"])

        self.assertEqual(code, 2)
        self.assertIn("sandbox apply requires an explicit runtime path", error.getvalue())

    def test_onboarding_apply_sandbox_rejects_bad_confirmation_without_writes(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(["onboarding", "apply", "--sandbox-apply", "--runtime", str(runtime), "--confirm", "yes", "--json"])

            payload = json.loads(output.getvalue())

        self.assertEqual(code, 1)
        self.assertFalse(runtime.exists())
        self.assertEqual(payload["status"], "sandbox-rejected")
        self.assertFalse(payload["safetyPolicy"]["writesSettings"])

    def test_onboarding_apply_sandbox_writes_temp_runtime_only(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--sandbox-apply",
                        "--runtime",
                        str(runtime),
                        "--confirmation-text",
                        "APPLY ACTANARA ONBOARDING",
                        "--json",
                    ]
                )

            payload = json.loads(output.getvalue())
            settings_path = runtime / "config" / "settings.json"
            audit_path = runtime / "state" / "onboarding" / "onboarding-audit.jsonl"
            rollback_path = runtime / "state" / "onboarding" / "rollback-plan.json"

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "sandbox-applied")
            self.assertTrue(settings_path.exists())
            self.assertTrue(audit_path.exists())
            self.assertTrue(rollback_path.exists())
            self.assertFalse(payload["safetyPolicy"]["writesLaunchdPlist"])
            self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])

    def test_onboarding_apply_runtime_bootstrap_requires_explicit_runtime(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(["onboarding", "apply", "--runtime-bootstrap-apply", "--confirmation-text", "APPLY ACTANARA ONBOARDING", "--json"])

        self.assertEqual(code, 2)
        self.assertIn("runtime bootstrap apply requires an explicit runtime path", error.getvalue())

    def test_onboarding_apply_modes_are_mutually_exclusive(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stderr(io.StringIO()) as error:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--sandbox-apply",
                        "--runtime-bootstrap-apply",
                        "--runtime",
                        str(runtime),
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("cannot be used together", error.getvalue())

    def test_onboarding_apply_scheduler_sandbox_requires_fake_home(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stderr(io.StringIO()) as error:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--scheduler-sandbox-apply",
                        "--runtime",
                        str(runtime),
                        "--confirmation-text",
                        "REGISTER ACTANARA SCHEDULER",
                        "--json",
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("fake scheduler home", error.getvalue())

    def test_onboarding_apply_scheduler_plist_requires_explicit_runtime(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(
                [
                    "onboarding",
                    "apply",
                    "--scheduler-plist-apply",
                    "--confirmation-text",
                    "WRITE ACTANARA LAUNCHAGENTS",
                    "--json",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("explicit runtime path", error.getvalue())

    def test_onboarding_apply_scheduler_plist_is_mutually_exclusive(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stderr(io.StringIO()) as error:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--runtime-bootstrap-apply",
                        "--scheduler-plist-apply",
                        "--runtime",
                        str(runtime),
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("cannot be used together", error.getvalue())

    def test_onboarding_apply_scheduler_register_requires_explicit_runtime(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(
                [
                    "onboarding",
                    "apply",
                    "--scheduler-register-apply",
                    "--confirmation-text",
                    "REGISTER ACTANARA SCHEDULER",
                    "--json",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("explicit runtime path", error.getvalue())

    def test_onboarding_apply_scheduler_register_is_mutually_exclusive(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stderr(io.StringIO()) as error:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--scheduler-plist-apply",
                        "--scheduler-register-apply",
                        "--runtime",
                        str(runtime),
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("cannot be used together", error.getvalue())

    def test_onboarding_apply_scheduler_unregister_requires_explicit_runtime(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(
                [
                    "onboarding",
                    "apply",
                    "--scheduler-unregister-apply",
                    "--confirmation-text",
                    "UNREGISTER ACTANARA SCHEDULER",
                    "--json",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("explicit runtime path", error.getvalue())

    def test_onboarding_apply_scheduler_unregister_is_mutually_exclusive(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stderr(io.StringIO()) as error:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--scheduler-register-apply",
                        "--scheduler-unregister-apply",
                        "--runtime",
                        str(runtime),
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("cannot be used together", error.getvalue())

    def test_onboarding_apply_scheduler_sandbox_writes_fake_home_only(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "Actanara"
            fake_home = root / "FakeHome"
            with (
                patch("data_foundation.settings.default_timer_provider", return_value="launchd"),
                patch("data_foundation.scheduler_preview.platform.system", return_value="Darwin"),
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--scheduler-sandbox-apply",
                        "--runtime",
                        str(runtime),
                        "--scheduler-home",
                        str(fake_home),
                        "--confirmation-text",
                        "REGISTER ACTANARA SCHEDULER",
                        "--json",
                    ]
                )

            payload = json.loads(output.getvalue())
            launch_agents = fake_home / "Library" / "LaunchAgents"

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "scheduler-sandbox-applied")
            self.assertTrue(list(launch_agents.glob("*.plist")))
            self.assertFalse(payload["safetyPolicy"]["writesRealLaunchAgents"])
            self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])

    def test_onboarding_apply_runtime_bootstrap_writes_explicit_runtime_only(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--runtime-bootstrap-apply",
                        "--runtime",
                        str(runtime),
                        "--confirmation-text",
                        "APPLY ACTANARA ONBOARDING",
                        "--json",
                    ]
                )

            payload = json.loads(output.getvalue())
            settings_path = runtime / "config" / "settings.json"
            audit_path = runtime / "state" / "onboarding" / "onboarding-audit.jsonl"
            rollback_path = runtime / "state" / "onboarding" / "runtime-bootstrap-rollback-plan.json"

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "runtime-bootstrap-applied")
            self.assertTrue(settings_path.exists())
            self.assertTrue(audit_path.exists())
            self.assertTrue(rollback_path.exists())
            self.assertFalse(payload["runtime"]["selectedAsActiveRuntime"])
            self.assertFalse(payload["safetyPolicy"]["writesBootstrapLocation"])
            self.assertFalse(payload["safetyPolicy"]["writesLaunchdPlist"])
            self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])

    def test_onboarding_apply_select_active_runtime_requires_runtime_bootstrap(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(["onboarding", "apply", "--select-active-runtime", "--json"])

        self.assertEqual(code, 2)
        self.assertIn("requires --runtime-bootstrap-apply", error.getvalue())

    def test_onboarding_apply_use_default_runtime_requires_runtime_bootstrap(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(["onboarding", "apply", "--use-default-runtime", "--json"])

        self.assertEqual(code, 2)
        self.assertIn("requires --runtime-bootstrap-apply", error.getvalue())

    def test_onboarding_apply_use_default_runtime_rejects_explicit_runtime(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stderr(io.StringIO()) as error:
                code = cli.main(
                    [
                        "onboarding",
                        "apply",
                        "--runtime-bootstrap-apply",
                        "--use-default-runtime",
                        "--runtime",
                        str(runtime),
                        "--confirmation-text",
                        "APPLY ACTANARA ONBOARDING",
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("cannot be used together", error.getvalue())

    def test_onboarding_apply_runtime_bootstrap_can_use_default_runtime(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            home.mkdir()
            expected_runtime = home / ".actanara"
            with patch.dict(os.environ, {"HOME": str(home), "ACTANARA_LOCATION_FILE": str(Path(tmp) / "location.json")}, clear=False):
                with redirect_stdout(io.StringIO()) as output:
                    code = cli.main(
                        [
                            "onboarding",
                            "apply",
                            "--runtime-bootstrap-apply",
                            "--use-default-runtime",
                            "--confirmation-text",
                            "APPLY ACTANARA ONBOARDING",
                            "--json",
                        ]
                    )

            payload = json.loads(output.getvalue())

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "runtime-bootstrap-applied")
            self.assertEqual(payload["runtime"]["actanaraHome"], str(expected_runtime))
            self.assertTrue((expected_runtime / "config" / "settings.json").exists())
            self.assertFalse(payload["runtime"]["selectedAsActiveRuntime"])
            self.assertFalse(payload["safetyPolicy"]["writesBootstrapLocation"])

    def test_onboarding_apply_runtime_bootstrap_can_select_active_runtime_pointer(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            bootstrap = Path(tmp) / "location.json"
            with patch.dict(os.environ, {"ACTANARA_LOCATION_FILE": str(bootstrap)}, clear=False):
                with redirect_stdout(io.StringIO()) as output:
                    code = cli.main(
                        [
                            "onboarding",
                            "apply",
                            "--runtime-bootstrap-apply",
                            "--select-active-runtime",
                            "--runtime",
                            str(runtime),
                            "--confirmation-text",
                            "APPLY ACTANARA ONBOARDING",
                            "--json",
                        ]
                    )

            payload = json.loads(output.getvalue())
            pointer = json.loads(bootstrap.read_text(encoding="utf-8"))

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "runtime-bootstrap-applied")
            self.assertEqual(pointer["actanaraHome"], str(runtime))
            self.assertTrue(payload["runtime"]["selectedAsActiveRuntime"])
            self.assertTrue(payload["safetyPolicy"]["writesBootstrapLocation"])
            self.assertFalse(payload["safetyPolicy"]["writesLaunchdPlist"])
            self.assertFalse(payload["safetyPolicy"]["callsLaunchctl"])

    def test_onboarding_one_liner_apply_uses_default_runtime_without_scheduler_registration(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            home.mkdir()
            expected_runtime = home / ".actanara"
            with patch.dict(os.environ, {"HOME": str(home), "ACTANARA_LOCATION_FILE": str(Path(tmp) / "location.json")}, clear=False):
                with redirect_stdout(io.StringIO()) as output:
                    code = cli.main(
                        [
                            "onboarding",
                            "runtime-apply",
                            "--use-default-runtime",
                            "--confirmation-text",
                            "APPLY ACTANARA ONBOARDING",
                            "--json",
                        ]
                    )

            payload = json.loads(output.getvalue())

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "one-liner-applied")
            self.assertEqual(payload["runtimeBootstrap"]["runtime"]["actanaraHome"], str(expected_runtime))
            self.assertTrue((expected_runtime / "config" / "settings.json").exists())
            self.assertFalse(payload["schedulerRegistration"]["registersScheduler"])
            self.assertFalse(payload["schedulerRegistration"]["callsLaunchctl"])

    def test_onboarding_one_liner_apply_can_select_english_language_contract(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Home"
            home.mkdir()
            expected_runtime = home / ".actanara"
            with patch.dict(os.environ, {"HOME": str(home), "ACTANARA_LOCATION_FILE": str(Path(tmp) / "location.json")}, clear=False):
                with redirect_stdout(io.StringIO()) as output:
                    code = cli.main(
                        [
                            "onboarding",
                            "runtime-apply",
                            "--use-default-runtime",
                            "--language",
                            "en-US",
                            "--confirmation-text",
                            "APPLY ACTANARA ONBOARDING",
                            "--json",
                        ]
                    )

            payload = json.loads(output.getvalue())
            settings = json.loads((expected_runtime / "config" / "settings.json").read_text(encoding="utf-8"))

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "one-liner-applied")
            self.assertEqual(settings["general"]["locale"], "en-US")
            self.assertEqual(settings["pipeline"]["languageProfile"], "en")
            self.assertTrue(settings["pipeline"]["englishEnabled"])
            self.assertEqual(settings["rag"]["languageProfile"], "en")

    def test_onboarding_one_liner_apply_text_reports_runtime_write_side_effects(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(
                    [
                        "onboarding",
                        "runtime-apply",
                        "--runtime",
                        str(runtime),
                        "--confirmation-text",
                        "APPLY ACTANARA ONBOARDING",
                    ]
                )

        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("Actanara is ready", text)
        self.assertIn("Automatic daily runs were left off", text)
        self.assertIn("Settings were saved", text)
        self.assertIn("Automatic daily runs were not changed", text)

    def test_onboarding_one_liner_apply_with_scheduler_requires_scheduler_confirmation(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(
                    [
                        "onboarding",
                        "runtime-apply",
                        "--runtime",
                        str(runtime),
                        "--with-scheduler",
                        "--confirmation-text",
                        "APPLY ACTANARA ONBOARDING",
                        "--json",
                    ]
                )

            payload = json.loads(output.getvalue())

            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "one-liner-rejected")
            self.assertEqual(payload["schedulerRegistration"]["status"], "scheduler-confirmation-missing")
            self.assertFalse(payload["schedulerRegistration"]["callsLaunchctl"])

    def test_onboarding_one_liner_status_reads_runtime_artifacts(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()):
                apply_code = cli.main(
                    [
                        "onboarding",
                        "runtime-apply",
                        "--runtime",
                        str(runtime),
                        "--confirmation-text",
                        "APPLY ACTANARA ONBOARDING",
                        "--json",
                    ]
                )
            with redirect_stdout(io.StringIO()) as output:
                status_code = cli.main(["onboarding", "runtime-status", "--runtime", str(runtime), "--json"])

            payload = json.loads(output.getvalue())

            self.assertEqual(apply_code, 0)
            self.assertEqual(status_code, 0)
            self.assertEqual(payload["status"], "initialized")
            self.assertTrue(payload["artifacts"]["runtimeBootstrapRollback"]["exists"])
            self.assertFalse(payload["schedulerRegistration"]["callsLaunchctl"])

    def test_onboarding_one_liner_release_gate_json_passes_minimal_v1(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["onboarding", "runtime-release-gate", "--json"])

        payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["selectedProfiles"], ["actanara", "dashboard", "nova-task"])
        self.assertFalse(payload["withScheduler"])

    def test_onboarding_one_liner_release_gate_json_with_scheduler_passes(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["onboarding", "runtime-release-gate", "--with-scheduler", "--json"])

        payload = json.loads(output.getvalue())
        gate_ids = {gate["id"] for gate in payload["gates"]}

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["withScheduler"])
        self.assertIn("scheduler-registration-gate", gate_ids)

    def test_onboarding_one_liner_validation_matrix_json_passes(self):
        cli = _load_cli_module()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["onboarding", "runtime-validation-matrix", "--json"])

        payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["oneLinerValidationMatrix"])
        self.assertEqual(payload["failedCases"], [])
        self.assertEqual(payload["summary"]["cases"], 5)
        self.assertIn("clean-deployment-artifact-scan", {case["id"] for case in payload["cases"]})

    def test_onboarding_rollback_plan_reports_missing_without_runtime_writes(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(["onboarding", "rollback-plan", "--runtime", str(runtime), "--json"])

            payload = json.loads(output.getvalue())

            self.assertEqual(code, 1)
            self.assertFalse(runtime.exists())
            self.assertEqual(payload["status"], "missing")
            self.assertFalse(payload["executionPolicy"]["executesRollback"])

    def test_onboarding_rollback_plan_reads_existing_runtime_plan(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "Actanara"
            with redirect_stdout(io.StringIO()):
                cli.main(
                    [
                        "onboarding",
                        "runtime-apply",
                        "--runtime",
                        str(runtime),
                        "--confirmation-text",
                        "APPLY ACTANARA ONBOARDING",
                        "--json",
                    ]
                )
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(["onboarding", "rollback-plan", "--runtime", str(runtime), "--json"])

            payload = json.loads(output.getvalue())

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "available")
            self.assertFalse(payload["executionPolicy"]["deletesFiles"])
            self.assertGreater(payload["summary"]["operations"], 0)

    def test_onboarding_apply_unknown_profile_returns_usage_error(self):
        cli = _load_cli_module()
        with redirect_stderr(io.StringIO()) as error:
            code = cli.main(["onboarding", "apply", "--profile", "bad"])

        self.assertEqual(code, 2)
        self.assertIn("unknown onboarding profile", error.getvalue())

    def test_pipeline_run_alias_is_rejected_with_date_flag(self):
        cli = _load_cli_module()
        result = _FakePipelineResult("2026-05-19", 9, 9, True)
        with (
            patch.object(cli, "run_daily_pipeline", return_value=result) as run,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = cli.main(["pipeline", "run", "--date", "2026-05-19"])

        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("provide the date once", error.getvalue())


if __name__ == "__main__":
    unittest.main()
