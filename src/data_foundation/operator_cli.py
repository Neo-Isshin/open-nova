#!/usr/bin/env python3
"""Actanara command line interface."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from data_foundation.cli_output import friendly_name, render_cli, status_item, status_label
from data_foundation.external_agent_memory import compact_memory_results, search_memory
from data_foundation.daily_completeness import evaluate_daily_completeness
from data_foundation.diary_metrics import (
    write_diary_metrics_readiness_report,
    write_diary_metrics_table_mismatch_approval,
)
from data_foundation.llm_provider_test import check_llm_provider_availability
from data_foundation.nova_task import diary_tasks_snapshot, pending_candidate_count
from data_foundation.paths import default_oneliner_runtime_home, initialize_home, load_paths, runtime_paths_for_home
from data_foundation.pipeline import run_daily_pipeline
from data_foundation.scheduler_reconcile import reconcile_pipeline_schedule
from data_foundation.onboarding_plan import (
    dump_onboarding_approval_packet_json,
    dump_onboarding_apply_blocked_json,
    dump_onboarding_one_liner_dry_run_json,
    dump_onboarding_one_liner_status_json,
    dump_onboarding_one_liner_validation_matrix_json,
    dump_onboarding_release_gate_json,
    dump_onboarding_rollback_plan_status_json,
    dump_onboarding_subsystem_plan_json,
    format_onboarding_approval_packet,
    format_onboarding_apply_blocked,
    format_onboarding_one_liner_dry_run,
    format_onboarding_one_liner_status,
    format_onboarding_one_liner_validation_matrix,
    format_onboarding_release_gate,
    format_onboarding_rollback_plan_status,
    format_onboarding_subsystem_plan,
    onboarding_approval_packet,
    onboarding_apply_blocked,
    onboarding_apply_runtime_bootstrap,
    onboarding_apply_sandbox,
    onboarding_apply_scheduler_register,
    onboarding_apply_scheduler_plist_write,
    onboarding_apply_scheduler_sandbox,
    onboarding_apply_scheduler_unregister,
    onboarding_one_liner_apply,
    onboarding_one_liner_dry_run,
    onboarding_one_liner_release_gate,
    onboarding_one_liner_status,
    onboarding_one_liner_validation_matrix,
    onboarding_release_gate,
    onboarding_rollback_plan_status,
    onboarding_subsystem_plan,
)
from data_foundation.onboarding_status import (
    dump_actanara_onboarding_status_json,
    format_actanara_onboarding_status,
    actanara_onboarding_status,
)
from data_foundation.settings_status import (
    dump_actanara_settings_status_json,
    format_actanara_settings_status,
    actanara_settings_status,
)
from data_foundation.settings import (
    OPERATOR_SETTINGS_WRITE_ALLOWED_TOP_LEVEL,
    OPERATOR_SETTINGS_WRITE_PROTECTED_TOP_LEVEL,
    read_llm_provider,
    read_settings,
    runtime_authority_contract,
    write_llm_api_key_secret,
    write_llm_provider,
    write_operator_settings,
)
from data_foundation.sqlite_cache_rebuild import (
    SQLITE_CACHE_REBUILD_CONFIRMATION,
    plan_sqlite_cache_rebuild,
    rebuild_sqlite_cache,
)
from data_foundation.systemd_user import (
    restart_dashboard_systemd_service,
)
from advanced.dashboard.dashboard_launch_agent import (
    dashboard_launch_defaults,
    restart_service as restart_dashboard_service,
)
from agentic_rag.rag_settings import resolve_rag_settings
from agentic_rag.rag_v2_sync import plan_v2_production_sync, sync_v2_production_index


RAG_REBUILD_CONFIRMATION = "REBUILD AND PROMOTE ACTANARA RAG"
RAG_UPDATE_CONFIRMATION = "UPDATE AND PROMOTE ACTANARA RAG"
DIARY_METRICS_APPROVAL_CONFIRMATION = "APPROVE ACTANARA DIARY METRICS MISMATCH"
DEFAULT_UPDATE_SOURCE_URL = "https://github.com/Neo-Isshin/actanara.git"
UPDATE_FULL_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
LATEST_STABLE_RELEASE_POLICY = "resolve latest stable Release and pin the resolved commit"
UPDATE_RESULT_PREFIX = "ACTANARA_UPDATE_RESULT_JSON="


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        sys.stdout.write(_product_command_guide())
        return 0
    return handler(args)


def _command_help(parser: argparse.ArgumentParser):
    def show_help(_args: argparse.Namespace) -> int:
        parser.print_help()
        return 0

    return show_help


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="actanara",
        description="Create diaries, search your memory, and manage your local Actanara setup.",
        epilog=_product_help_groups(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", title="Commands", metavar="COMMAND")

    doctor = subcommands.add_parser("doctor", help="Check whether Actanara is ready.")
    _add_doctor_args(doctor)
    doctor.set_defaults(handler=_settings_status)

    model = subcommands.add_parser("model", help="Show, choose, or test the AI model used for diaries.")
    model_subcommands = model.add_subparsers(dest="model_command")
    model.set_defaults(handler=_model_show)
    model_show = model_subcommands.add_parser("show", help="Show the current AI model.")
    _add_status_args(model_show)
    model_show.set_defaults(handler=_model_show)
    model_list = model_subcommands.add_parser("list", help="List available AI model services.")
    _add_status_args(model_list)
    model_list.set_defaults(handler=_model_list)
    model_set = model_subcommands.add_parser("set", help="Choose the AI model and connection settings.")
    _add_status_args(model_set)
    model_set.add_argument("--provider", help="Model service name from `model list`, or custom.")
    model_set.add_argument("--model", help="Model name used to create diaries.")
    model_set.add_argument("--endpoint", help="Custom service URL.")
    model_set.add_argument("--api", help="Compatibility mode required by the model service.")
    model_set.add_argument("--context-window", type=int, help="Maximum amount of source text sent to the model.")
    model_set.add_argument("--max-tokens", type=int, help="Maximum length of a model response.")
    model_set.add_argument("--pipeline-concurrency", type=int, help="Number of diary sections created at once.")
    model_set.add_argument("--timeout-seconds", type=int, help="Seconds to wait for the model service.")
    model_set.add_argument("--api-key-env", help="Environment variable that contains the API key.")
    model_set.set_defaults(handler=_model_set)
    model_key = model_subcommands.add_parser("key", help="Save the AI model API key from standard input.")
    _add_status_args(model_key)
    model_key.add_argument("--value-stdin", action="store_true", help="Read the API key from standard input.")
    model_key.set_defaults(handler=_secrets_set_llm_api_key)
    model_test = model_subcommands.add_parser("test", help="Check the current AI model connection without changing settings.")
    _add_status_args(model_test)
    model_test.set_defaults(handler=_model_test)

    onboard = subcommands.add_parser("onboard", help="Check or finish first-time setup.")
    onboard_subcommands = onboard.add_subparsers(dest="onboard_command")
    onboard.set_defaults(handler=_onboarding_doctor)
    onboard_status = onboard_subcommands.add_parser("status", help="Show setup status and next steps.")
    _add_onboarding_args(onboard_status)
    onboard_status.set_defaults(handler=_onboarding_doctor)
    onboard_doctor = onboard_subcommands.add_parser("doctor", help="Check first-time setup.")
    _add_onboarding_args(onboard_doctor)
    onboard_doctor.set_defaults(handler=_onboarding_doctor)
    onboard_plan = onboard_subcommands.add_parser("plan", help="Preview first-time setup.")
    _add_onboarding_args(onboard_plan)
    onboard_plan.set_defaults(handler=_onboarding_plan)
    onboard_apply = onboard_subcommands.add_parser("apply", help="Complete setup after confirmation.")
    _add_onboarding_apply_args(onboard_apply)
    onboard_apply.set_defaults(handler=_onboarding_apply_blocked)

    config = subcommands.add_parser("config", help="Show or change Actanara settings.")
    config_subcommands = config.add_subparsers(dest="config_command")
    config.set_defaults(handler=_config_show)
    config_show = config_subcommands.add_parser("show", help="Show current settings.")
    _add_status_args(config_show)
    config_show.set_defaults(handler=_config_show)
    config_doctor = config_subcommands.add_parser("doctor", help="Check current settings.")
    _add_doctor_args(config_doctor)
    config_doctor.set_defaults(handler=_settings_status)
    config_keys = config_subcommands.add_parser("keys", help="Show settings you can change.")
    _add_status_args(config_keys)
    config_keys.set_defaults(handler=_config_keys)
    config_get = config_subcommands.add_parser("get", help="Show one setting.")
    _add_status_args(config_get)
    config_get.add_argument("path", help="Setting name, for example general.timezone.")
    config_get.set_defaults(handler=_config_get)
    config_set = config_subcommands.add_parser("set", help="Change one supported setting.")
    _add_status_args(config_set)
    config_set.add_argument("path", help="Setting name shown by `config keys`.")
    config_set.add_argument("value", help="New value; JSON values are accepted.")
    config_set.set_defaults(handler=_config_set)

    update = subcommands.add_parser(
        "update",
        help="Check for an update or install it.",
        description=(
            "Check or install an Actanara update. By default, Actanara uses "
            "the latest stable release."
        ),
    )
    _add_status_args(update)
    update_mode = update.add_mutually_exclusive_group()
    update_mode.add_argument("--apply", action="store_true", help="Install the update now.")
    update_mode.add_argument("--dry-run", action="store_true", help="Preview the update without changing anything.")
    update.add_argument(
        "--ref",
        help=(
            "Use an exact 40- or 64-character version ID. "
            "Omit this option to use the latest stable release."
        ),
    )
    update.add_argument("--source-url", default=DEFAULT_UPDATE_SOURCE_URL, help="Address to download the update from.")
    update.add_argument(
        "--source-root",
        help="Use an existing local copy instead of downloading; cannot be combined with --ref.",
    )
    update.add_argument("--cache-root", help="Folder used to keep downloaded update files.")
    update_dependency_mode = update.add_mutually_exclusive_group()
    update_dependency_mode.add_argument(
        "--source-only",
        action="store_true",
        help="Update app files only; stop if the current installation cannot be reused.",
    )
    update_dependency_mode.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Reinstall required software during the update.",
    )
    update.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Do not use the network. Requires --source-root PATH or an exact --ref already "
            "downloaded; reinstalling also requires previously downloaded software."
        ),
    )
    update.set_defaults(handler=_update_run)

    search = subcommands.add_parser("search", help="Search your Actanara memory.")
    _add_rag_search_args(search)
    search.set_defaults(handler=_search_memory)

    task = subcommands.add_parser("task", help="Show task totals.")
    _add_status_args(task)
    task.set_defaults(handler=_task_counts)

    rag_rebuild = subcommands.add_parser("rag-rebuild", help="Rebuild memory after confirmation.")
    _add_status_args(rag_rebuild)
    rag_rebuild.add_argument("--dry-run", action="store_true", help="Preview the rebuild without changing anything.")
    rag_rebuild.add_argument("--confirm", dest="confirmation_text", help=f'Exact confirmation phrase: "{RAG_REBUILD_CONFIRMATION}"')
    rag_rebuild.set_defaults(handler=_rag_rebuild)

    rag_update = subcommands.add_parser(
        "rag-update",
        help="Refresh memory after confirmation.",
    )
    _add_status_args(rag_update)
    rag_update.add_argument("--dry-run", action="store_true", help="Preview the refresh without changing anything.")
    rag_update.add_argument("--confirm", dest="confirmation_text", help=f'Exact confirmation phrase: "{RAG_UPDATE_CONFIRMATION}"')
    rag_update.set_defaults(handler=_rag_update)

    settings = subcommands.add_parser("settings", help="Check Actanara settings.")
    settings_subcommands = settings.add_subparsers(dest="settings_command")
    settings.set_defaults(handler=_command_help(settings))
    status = settings_subcommands.add_parser("status", help="Show settings status.")
    _add_status_args(status)
    status.set_defaults(handler=_settings_status)

    doctor = settings_subcommands.add_parser("doctor", help="Check settings for problems.")
    _add_doctor_args(doctor)
    doctor.set_defaults(handler=_settings_status)

    onboarding = subcommands.add_parser("onboarding", help="Detailed first-time setup commands.")
    onboarding_subcommands = onboarding.add_subparsers(dest="onboarding_command")
    onboarding.set_defaults(handler=_command_help(onboarding))
    onboarding_doctor = onboarding_subcommands.add_parser("doctor", help="Check first-time setup.")
    _add_onboarding_args(onboarding_doctor)
    onboarding_doctor.set_defaults(handler=_onboarding_doctor)
    onboarding_plan = onboarding_subcommands.add_parser("plan", help="Preview setup for selected features.")
    _add_onboarding_args(onboarding_plan)
    onboarding_plan.set_defaults(handler=_onboarding_plan)
    onboarding_one_liner = onboarding_subcommands.add_parser(
        "runtime-dry-run",
        help="Preview how Actanara will prepare its data folder.",
    )
    _add_onboarding_args(onboarding_one_liner)
    onboarding_one_liner.set_defaults(handler=_onboarding_one_liner_dry_run)
    onboarding_one_liner_apply = onboarding_subcommands.add_parser(
        "runtime-apply",
        help="Prepare Actanara; automatic daily runs remain optional.",
    )
    _add_onboarding_args(onboarding_one_liner_apply)
    onboarding_one_liner_apply.add_argument(
        "--confirmation-text",
        help="Exact confirmation phrase.",
    )
    onboarding_one_liner_apply.add_argument(
        "--confirm",
        dest="confirmation_text",
        help="Alias for --confirmation-text. Exact phrase is still required.",
    )
    onboarding_one_liner_apply.add_argument(
        "--select-active-runtime",
        action="store_true",
        help="Use this data folder as the active Actanara installation.",
    )
    onboarding_one_liner_apply.add_argument(
        "--with-scheduler",
        action="store_true",
        help="Also enable automatic daily runs after confirmation.",
    )
    onboarding_one_liner_apply.add_argument(
        "--scheduler-confirmation-text",
        help="Exact confirmation phrase required with --with-scheduler.",
    )
    onboarding_one_liner_apply.add_argument(
        "--use-default-runtime",
        action="store_true",
        help="Use ~/.actanara when --runtime is omitted.",
    )
    onboarding_one_liner_apply.add_argument(
        "--language",
        help="Diary language: zh-CN or en-US.",
    )
    onboarding_one_liner_apply.set_defaults(handler=_onboarding_one_liner_apply)
    onboarding_one_liner_status = onboarding_subcommands.add_parser(
        "runtime-status",
        help="Check files created during setup without changing them.",
    )
    _add_status_args(onboarding_one_liner_status)
    onboarding_one_liner_status.set_defaults(handler=_onboarding_one_liner_status)
    onboarding_one_liner_release = onboarding_subcommands.add_parser(
        "runtime-release-gate",
        help="Check whether setup is ready, with automatic runs optional.",
    )
    _add_onboarding_args(onboarding_one_liner_release)
    onboarding_one_liner_release.add_argument(
        "--with-scheduler",
        action="store_true",
        help="Also check optional automatic daily runs.",
    )
    onboarding_one_liner_release.set_defaults(handler=_onboarding_one_liner_release_gate)
    onboarding_one_liner_matrix = onboarding_subcommands.add_parser(
        "runtime-validation-matrix",
        help="Show setup verification results.",
    )
    _add_status_args(onboarding_one_liner_matrix)
    onboarding_one_liner_matrix.set_defaults(handler=_onboarding_one_liner_validation_matrix)
    onboarding_rollback_plan = onboarding_subcommands.add_parser(
        "rollback-plan",
        help="Show available recovery steps without running them.",
    )
    _add_status_args(onboarding_rollback_plan)
    onboarding_rollback_plan.set_defaults(handler=_onboarding_rollback_plan_status)
    onboarding_release = onboarding_subcommands.add_parser(
        "release-gate",
        help="Check whether detailed setup is ready.",
    )
    _add_onboarding_args(onboarding_release)
    onboarding_release.add_argument(
        "--confirmation-text",
        help="Exact confirmation phrase to verify.",
    )
    onboarding_release.add_argument(
        "--confirm",
        dest="confirmation_text",
        help="Alias for --confirmation-text. Does not enable writes.",
    )
    onboarding_release.set_defaults(handler=_onboarding_release_gate)
    onboarding_approval = onboarding_subcommands.add_parser(
        "approval-checklist",
        help="Show confirmations required before setup can make changes.",
    )
    _add_onboarding_args(onboarding_approval)
    onboarding_approval.add_argument(
        "--confirmation-text",
        help="Exact confirmation phrase to verify.",
    )
    onboarding_approval.add_argument(
        "--confirm",
        dest="confirmation_text",
        help="Alias for --confirmation-text. Does not enable writes.",
    )
    onboarding_approval.set_defaults(handler=_onboarding_approval_packet)
    onboarding_apply = onboarding_subcommands.add_parser(
        "apply",
        help="Run the detailed setup action after confirmation.",
    )
    _add_onboarding_apply_args(onboarding_apply)
    onboarding_apply.set_defaults(handler=_onboarding_apply_blocked)

    pipeline = subcommands.add_parser("pipeline", help="Create a diary for today or a selected date.")
    _add_status_args(pipeline)
    pipeline.add_argument("date", nargs="?", help="Diary date, YYYY-MM-DD or YYMMDD.")
    pipeline.add_argument("--date", dest="date_flag", help="Diary date, YYYY-MM-DD or YYMMDD.")
    pipeline.add_argument(
        "--force",
        action="store_true",
        help="Create the diary again using the activity already collected.",
    )
    pipeline.set_defaults(handler=_pipeline_run)

    dashboard = subcommands.add_parser("dashboard", help="Manage the local Dashboard.")
    dashboard_subcommands = dashboard.add_subparsers(dest="dashboard_command")
    dashboard.set_defaults(handler=_command_help(dashboard))
    dashboard_restart = dashboard_subcommands.add_parser("restart", help="Restart Dashboard.")
    _add_status_args(dashboard_restart)
    dashboard_restart.add_argument("--label", help="Optional custom background-service name.")
    dashboard_restart.set_defaults(handler=_dashboard_restart)

    scheduler = subcommands.add_parser("scheduler", help="Check automatic daily runs.")
    scheduler_subcommands = scheduler.add_subparsers(dest="scheduler_command")
    scheduler.set_defaults(handler=_command_help(scheduler))
    scheduler_reconcile = scheduler_subcommands.add_parser(
        "reconcile",
        help="Find missed diaries and optionally catch up.",
    )
    _add_status_args(scheduler_reconcile)
    scheduler_reconcile.add_argument("--apply", action="store_true", help="Apply catch-up when missing days are within the automatic limit.")
    scheduler_reconcile.add_argument("--lookback-days", type=int, default=7, help="Number of recent days to inspect.")
    scheduler_reconcile.add_argument("--auto-limit-days", type=int, default=3, help="Maximum missing days to auto catch up.")
    scheduler_reconcile.set_defaults(handler=_scheduler_reconcile)

    foundation = subcommands.add_parser("foundation", help="Maintain Actanara's local data.")
    foundation_subcommands = foundation.add_subparsers(dest="foundation_command")
    foundation.set_defaults(handler=_command_help(foundation))
    sqlite_rebuild = foundation_subcommands.add_parser(
        "rebuild-sqlite-cache",
        help="Replace and rebuild the local database after confirmation.",
    )
    _add_status_args(sqlite_rebuild)
    sqlite_rebuild.add_argument("--start-date", help="Optional rebuild start date, YYYY-MM-DD.")
    sqlite_rebuild.add_argument("--end-date", help="Optional rebuild end date, YYYY-MM-DD.")
    sqlite_rebuild.add_argument("--dry-run", action="store_true", help="Preview the rebuild plan without writes.")
    sqlite_rebuild.add_argument(
        "--confirm",
        dest="confirmation_text",
        help=f'Exact confirmation phrase required to execute: "{SQLITE_CACHE_REBUILD_CONFIRMATION}"',
    )
    sqlite_rebuild.set_defaults(handler=_foundation_rebuild_sqlite_cache)
    approve_diary_metrics = foundation_subcommands.add_parser(
        "approve-diary-metrics",
        help="Confirm reviewed activity totals for a selected diary date.",
    )
    _add_status_args(approve_diary_metrics)
    approve_diary_metrics.add_argument("date", help="Diary date, YYYY-MM-DD or YYMMDD.")
    approve_diary_metrics.add_argument("--dry-run", action="store_true", help="Preview the approval without writes.")
    approve_diary_metrics.add_argument("--operator", default="operator", help="Name recorded with the approval.")
    approve_diary_metrics.add_argument("--note", default="", help="Optional note recorded with the approval.")
    approve_diary_metrics.add_argument(
        "--confirm",
        dest="confirmation_text",
        help=f'Exact confirmation phrase required to execute: "{DIARY_METRICS_APPROVAL_CONFIRMATION}"',
    )
    approve_diary_metrics.set_defaults(handler=_foundation_approve_diary_metrics)

    secrets = subcommands.add_parser("secrets", help="Manage local API keys.")
    secrets_subcommands = secrets.add_subparsers(dest="secrets_command")
    secrets.set_defaults(handler=_command_help(secrets))
    set_llm_key = secrets_subcommands.add_parser(
        "set-llm-api-key",
        help="Save the AI model API key from standard input.",
    )
    _add_status_args(set_llm_key)
    set_llm_key.add_argument("--value-stdin", action="store_true", help="Read the API key from standard input.")
    set_llm_key.set_defaults(handler=_secrets_set_llm_api_key)

    return parser


def _product_command_guide() -> str:
    return """Actanara

Turn your daily activity into a useful diary.

Usage:
  actanara <command> [options]

Start here:
  actanara doctor                         Check whether Actanara is ready
  actanara onboard status                 Finish first-time setup
  actanara dashboard restart              Restart Dashboard

Create and find:
  actanara pipeline [YYMMDD|YYYY-MM-DD]   Create a diary
  actanara search "query"                 Search your memory
  actanara task                           Show task totals

AI model:
  actanara model show                     Show the current model
  actanara model list                     List available model services
  actanara model set --provider P --model M
  actanara model key --value-stdin        Save the model API key

Settings and updates:
  actanara config show                    Show current settings
  actanara config keys                    Show settings you can change
  actanara update                         Check the update plan
  actanara update --apply                 Install the update

Memory maintenance:
  actanara rag-update                     Refresh memory
  actanara rag-rebuild                    Rebuild memory

Run `actanara <command> --help` for command-specific options.
"""


def _product_help_groups() -> str:
    return """Common tasks:
  Get ready      actanara doctor | actanara onboard status
  Create diary   actanara pipeline [YYMMDD|YYYY-MM-DD]
  Search memory  actanara search "query"
  Choose model   actanara model show | actanara model set
  Update         actanara update

Run `actanara <command> --help` for details."""


def _add_status_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime", help="Use a specific Actanara data folder.")
    parser.add_argument("--legacy-diary-root", help="Use an existing diary folder with --runtime.")
    parser.add_argument("--json", action="store_true", help="Print JSON for scripts and automation.")


def _add_doctor_args(parser: argparse.ArgumentParser) -> None:
    _add_status_args(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--installer", action="store_const", const="installer", dest="doctor_profile", help="Check installation files and required software.")
    group.add_argument("--pipeline", action="store_const", const="pipeline", dest="doctor_profile", help="Check daily diary creation and activity sources.")
    group.add_argument("--scheduler", action="store_const", const="scheduler", dest="doctor_profile", help="Check automatic daily runs.")
    group.add_argument("--rag", action="store_const", const="rag", dest="doctor_profile", help="Check memory search.")


def _add_onboarding_args(parser: argparse.ArgumentParser) -> None:
    _add_status_args(parser)
    parser.add_argument(
        "--profile",
        action="append",
        dest="profiles",
        help="Include an optional feature. May be repeated.",
    )


def _add_onboarding_apply_args(parser: argparse.ArgumentParser) -> None:
    _add_onboarding_args(parser)
    parser.add_argument(
        "--confirmation-text",
        help="Exact confirmation phrase.",
    )
    parser.add_argument(
        "--confirm",
        dest="confirmation_text",
        help="Short form of --confirmation-text; the exact phrase is still required.",
    )
    parser.add_argument(
        "--sandbox-apply",
        action="store_true",
        help="Test setup in the selected --runtime folder only.",
    )
    parser.add_argument(
        "--runtime-bootstrap-apply",
        action="store_true",
        help="Prepare the selected --runtime folder without enabling automatic runs.",
    )
    parser.add_argument(
        "--scheduler-sandbox-apply",
        action="store_true",
        help="Test automatic-run files under --scheduler-home.",
    )
    parser.add_argument(
        "--scheduler-plist-apply",
        action="store_true",
        help="Write automatic-run files without enabling them.",
    )
    parser.add_argument(
        "--scheduler-register-apply",
        action="store_true",
        help="Enable existing automatic-run files after confirmation.",
    )
    parser.add_argument(
        "--scheduler-unregister-apply",
        action="store_true",
        help="Disable automatic daily runs after confirmation.",
    )
    parser.add_argument(
        "--scheduler-home",
        help="Temporary home folder used to test automatic runs.",
    )
    parser.add_argument(
        "--select-active-runtime",
        action="store_true",
        help="Use the prepared folder as the active Actanara installation.",
    )
    parser.add_argument(
        "--use-default-runtime",
        action="store_true",
        help="Use ~/.actanara when --runtime is omitted.",
    )
    parser.add_argument(
        "--language",
        help="Diary language: zh-CN or en-US.",
    )


def _add_rag_search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", help="Words or question to search for")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum number of results, up to 20")
    parser.add_argument("--dashboard-url", default=None, help="Use a specific Dashboard URL")
    parser.add_argument("--timeout", type=float, default=65, help="Seconds to wait for results")
    parser.add_argument("--date", default="", help="Search one date")
    parser.add_argument("--date-from", default="", help="Search from this date")
    parser.add_argument("--date-to", default="", help="Search through this date")
    parser.add_argument("--project", default="", help="Search one project")
    parser.add_argument("--role", default="", help="Search one assistant or role")
    parser.add_argument("--source-set", action="append", default=[], help="Search one kind of memory; may be repeated")
    parser.add_argument("--json", action="store_true", help="Print JSON for scripts and automation")


def _settings_status(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    payload = actanara_settings_status(paths, doctor_profile=getattr(args, "doctor_profile", None) or "all")
    output = dump_actanara_settings_status_json(payload) if args.json else format_actanara_settings_status(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 1 if payload.get("summary", {}).get("errors") else 0


def _model_show(args: argparse.Namespace) -> int:
    provider = read_llm_provider(_paths_from_args(args), persist_defaults=False)
    if args.json:
        sys.stdout.write(json.dumps(provider, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            render_cli(
                "AI model",
                fields=(
                    ("Status", "Ready" if provider.get("hasApiKey") else "Needs an API key"),
                    ("Service", provider.get("provider") or "Not set"),
                    ("Model", provider.get("model") or "Not set"),
                    ("Connection", provider.get("api") or "Not set"),
                    ("URL", provider.get("endpoint")),
                ),
                next_steps=(() if provider.get("hasApiKey") else ("actanara model key --value-stdin",)),
            )
        )
    return 0


def _model_list(args: argparse.Namespace) -> int:
    provider = read_llm_provider(_paths_from_args(args), persist_defaults=False)
    catalog = provider.get("catalog") if isinstance(provider.get("catalog"), list) else []
    payload = {
        "providers": catalog,
        "count": len(catalog),
        "current": {
            "provider": provider.get("provider"),
            "model": provider.get("model"),
        },
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        entries: list[str] = []
        for item in catalog:
            if not isinstance(item, dict):
                continue
            models = item.get("models") if isinstance(item.get("models"), list) else []
            sample = ", ".join(str(model.get("id")) for model in models[:3] if isinstance(model, dict) and model.get("id"))
            entry = f"{item.get('name', item.get('id', 'Model service'))} ({item.get('id', '-')})"
            if sample:
                entry += f"\n   Models: {sample}"
            entries.append(entry)
        current = payload.get("current") or {}
        sys.stdout.write(
            render_cli(
                "Available AI models",
                fields=(
                    ("Services", len(catalog)),
                    ("Current", f"{current.get('provider') or 'Not set'} / {current.get('model') or 'Not set'}"),
                ),
                sections=(("Model services", entries),),
                next_steps=("actanara model set --provider SERVICE --model MODEL",),
            )
        )
    return 0


def _model_set(args: argparse.Namespace) -> int:
    update = {
        key: value
        for key, value in {
            "provider": args.provider,
            "model": args.model,
            "endpoint": args.endpoint,
            "api": args.api,
            "contextWindow": args.context_window,
            "maxTokens": args.max_tokens,
            "pipelineConcurrency": args.pipeline_concurrency,
            "timeoutSeconds": args.timeout_seconds,
            "apiKeyEnv": args.api_key_env,
        }.items()
        if value is not None
    }
    if not update:
        sys.stderr.write("Error: choose at least one model setting to change.\nTry: actanara model set --help\n")
        return 2
    try:
        provider = write_llm_provider(update, _paths_from_args(args))
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.json:
        sys.stdout.write(json.dumps(provider, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            render_cli(
                "AI model updated",
                fields=(
                    ("Status", "Ready"),
                    ("Service", provider.get("provider") or "Not set"),
                    ("Model", provider.get("model") or "Not set"),
                ),
                next_steps=("actanara model test",),
            )
        )
    return 0


def _model_test(args: argparse.Namespace) -> int:
    result = check_llm_provider_availability(_paths_from_args(args))
    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            render_cli(
                "AI model check",
                fields=(("Status", "Ready" if result.get("ok") else "Failed"),),
                sections=(("Details", (_friendly_model_error(result.get("error")),)),) if result.get("error") else (),
                next_steps=(() if result.get("ok") else ("actanara model show",)),
            )
        )
    return 0 if result.get("ok") else 1


def _friendly_model_error(value: object) -> str:
    message = str(value or "").casefold()
    if any(token in message for token in ("api key", "unauthorized", "401", "403")):
        return "The AI provider rejected the API key."
    if any(token in message for token in ("timeout", "timed out")):
        return "The AI provider took too long to respond."
    if any(token in message for token in ("connection", "network", "unreachable")):
        return "The AI provider could not be reached."
    return "The AI model check did not finish."


def _secrets_set_llm_api_key(args: argparse.Namespace) -> int:
    if not args.value_stdin:
        sys.stderr.write("Error: the API key must be read from standard input.\nTry: actanara model key --value-stdin\n")
        return 2
    value = sys.stdin.read().strip()
    if not value:
        sys.stderr.write("Error: no API key was provided.\n")
        return 2
    try:
        paths = _paths_from_args(args) or load_paths()
        provider = write_llm_api_key_secret(value, paths)
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    secret_ref = provider.get("secretRef") if isinstance(provider.get("secretRef"), dict) else {}
    if args.json:
        import json

        sys.stdout.write(
            json.dumps(
                {
                    "status": "stored",
                    "runtime": str(paths.home),
                    "backend": secret_ref.get("backend"),
                    "service": secret_ref.get("service"),
                    "account": secret_ref.get("account"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        sys.stdout.write(
            render_cli(
                "AI model key",
                fields=(("Status", "Saved"), ("Data folder", paths.home)),
                next_steps=("actanara model test",),
            )
        )
    return 0


def _config_show(args: argparse.Namespace) -> int:
    payload = read_settings(_paths_from_args(args), persist_defaults=False)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
        enabled = [friendly_name(key) for key, value in features.items() if value]
        sys.stdout.write(
            render_cli(
                "Settings",
                fields=(
                    ("Status", "Ready"),
                    ("Settings file", payload.get("settingsPath", "—")),
                    ("Features", ", ".join(enabled) if enabled else "Standard"),
                ),
                next_steps=("actanara config keys",),
            )
        )
    return 0


def _config_get(args: argparse.Namespace) -> int:
    payload = read_settings(_paths_from_args(args), persist_defaults=False)
    try:
        value = _get_dot_path(payload, args.path)
    except KeyError:
        sys.stderr.write(f"Error: setting not found: {args.path}\nTry: actanara config keys\n")
        return 1
    if args.json:
        sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_cli("Setting", fields=(("Name", args.path), ("Value", _format_scalar(value)))))
    return 0


def _config_keys(args: argparse.Namespace) -> int:
    contract = runtime_authority_contract(_paths_from_args(args))
    authority = contract.get("settingsAuthority") if isinstance(contract.get("settingsAuthority"), dict) else {}
    groups = authority.get("groups") if isinstance(authority.get("groups"), list) else []
    payload = {
        "settingsPath": contract.get("settingsPath"),
        "writableGroups": sorted(OPERATOR_SETTINGS_WRITE_ALLOWED_TOP_LEVEL),
        "protectedGroups": sorted(OPERATOR_SETTINGS_WRITE_PROTECTED_TOP_LEVEL),
        "dedicatedCommands": {
            "llmProvider": "actanara model ...",
            "rag": "actanara rag-update / actanara rag-rebuild / Dashboard RAG controls",
        },
        "authorityGroups": groups,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        writable = [friendly_name(item) for item in payload["writableGroups"]]
        sys.stdout.write(
            render_cli(
                "Settings you can change",
                fields=(("Settings file", payload.get("settingsPath", "—")),),
                sections=(("Available groups", writable),),
                next_steps=(
                    "actanara config get general.timezone",
                    "actanara config set general.timezone Asia/Hong_Kong",
                    "Use `actanara model` for AI model settings",
                ),
            )
        )
    return 0


def _config_set(args: argparse.Namespace) -> int:
    try:
        update = _nested_update(args.path, _parse_config_value(args.value))
        saved = write_operator_settings(update, _paths_from_args(args))
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    value = _get_dot_path(saved, args.path)
    if args.json:
        sys.stdout.write(json.dumps({"path": args.path, "value": value}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            render_cli(
                "Setting updated",
                fields=(("Status", "Ready"), ("Name", args.path), ("Value", _format_scalar(value))),
            )
        )
    return 0


def _update_run(args: argparse.Namespace) -> int:
    try:
        _validate_update_source_selection(args)
        paths = _paths_from_args(args) or load_paths()
        command = _update_bootstrap_command(args, paths.home)
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"Error: {_friendly_update_start_error(exc)}\nTry: actanara update --help\n")
        return 2
    should_execute = bool(args.apply or args.dry_run)
    source_selection = _update_source_selection(args)
    payload = {
        "status": "ready" if not should_execute else "running",
        "dryRun": bool(args.dry_run),
        "apply": bool(args.apply),
        "updateConfigured": True,
        "runtime": str(paths.home),
        "sourceUrl": None if args.source_root else args.source_url,
        "sourceRoot": args.source_root,
        "ref": args.ref,
        "sourceSelection": source_selection,
        "command": command,
        "updateMode": "not-evaluated",
        "dependenciesInstalled": False,
        "reusesRuntimeVenv": None,
        "sourceUpdated": False,
        "cacheUsed": False,
        "servicesStopped": False,
        "plannedDependenciesInstall": None,
        "rollbackComplete": None,
        "stateCertain": None,
        "resultAvailable": False,
        "stage": "plan",
        "mutationPolicy": {
            "settingsMutated": False,
            "dependenciesInstalled": False,
            "sourceUpdated": False,
            "managedServicesStoppedBeforePortSelection": False,
            "managedServicesStoppedAfterPreflight": False,
            "schedulerChanged": False,
            "managedServiceDefinitionsMayNormalize": None,
            "reusesRuntimeVenv": None,
            "preservesSettingsAndUserData": True,
        },
    }
    if not should_execute:
        payload["reason"] = "use --apply to run the guarded installer upgrade, or --dry-run to preview installer actions"
        if args.json:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        else:
            source = "Local copy" if args.source_root else "Selected version" if args.ref else "Latest stable release"
            sys.stdout.write(
                render_cli(
                    "Update",
                    fields=(
                        ("Status", "Ready"),
                        ("Data folder", paths.home),
                        ("Source", source),
                    ),
                    next_steps=("actanara update --dry-run", "actanara update --apply"),
                )
            )
        return 0

    if not args.json:
        sys.stdout.write(
            render_cli(
                "Update",
                fields=(("Status", "Previewing" if args.dry_run else "Installing"), ("Data folder", paths.home)),
            )
        )
    run_kwargs = {"text": True, "capture_output": True, "check": False}
    if platform.system() == "Linux":
        environment = os.environ.copy()
        for name in (
            "ACTANARA_INSTALL_SOURCE_ROOT",
            "ACTANARA_INSTALL_SOURCE_URL",
            "ACTANARA_INSTALL_REF",
        ):
            environment.pop(name, None)
        run_kwargs["env"] = environment
    result = subprocess.run(command, **run_kwargs)
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    envelope = _update_result_envelope(stdout)
    _apply_update_execution_result(payload, envelope, result.returncode)
    payload["stdout"] = stdout
    payload["stderr"] = stderr

    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return result.returncode

    visible_stdout = _update_output_without_result_envelopes(stdout)
    if visible_stdout:
        sys.stdout.write(visible_stdout)
        if not visible_stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
    _write_update_human_result(payload, result.returncode)
    return result.returncode


def _update_bootstrap_command(args: argparse.Namespace, runtime_home: Path) -> list[str]:
    _validate_update_source_selection(args)
    bootstrap = _update_bootstrap_path(args, runtime_home)
    linux = platform.system() == "Linux"
    shell = (
        shutil.which("sh") or "/bin/sh"
        if linux
        else shutil.which("zsh") or "/bin/zsh"
    )
    command = [shell, str(bootstrap)]
    if args.dry_run:
        command.append("--dry-run")
    if args.offline:
        command.append("--offline")
    if args.source_root:
        command.extend(["--source-root", str(Path(args.source_root).expanduser())])
    else:
        command.extend(["--source-url", str(args.source_url or DEFAULT_UPDATE_SOURCE_URL)])
    if args.ref:
        command.extend(["--ref", str(args.ref)])
    if args.cache_root:
        command.extend(["--cache-root", str(Path(args.cache_root).expanduser())])
    if linux:
        command.extend(["--python", str(runtime_home / ".venv" / "bin" / "python")])
    command.append("--")
    if args.source_only:
        command.append("--source-only")
    else:
        command.append("--upgrade")
    if args.force_rebuild:
        command.append("--force-rebuild")
    if args.offline:
        command.append("--offline")
    command.extend(["--result-json", "--runtime", str(runtime_home), "--yes"])
    command.extend(_update_preserved_installer_args(runtime_home))
    return command


def _friendly_update_start_error(error: Exception) -> str:
    message = str(error)
    if "--source-root cannot be combined with --ref" in message:
        return "choose either a local copy or a version, not both"
    if "--offline requires --source-root" in message:
        return "offline update needs a local copy or a previously downloaded full version"
    if "custom --source-url" in message:
        return "choose an exact version when using a custom download source"
    if "full 40- or 64-character hexadecimal commit ID" in message:
        return "the version ID must contain exactly 40 or 64 hexadecimal characters"
    if "installer bootstrap not found under --source-root" in message:
        return "the selected local copy cannot update Actanara"
    if "installer bootstrap is unavailable" in message:
        return "Actanara's update files are missing; reinstall Actanara or choose a local copy"
    return f"update cannot start: {message}"


def _update_result_envelope(stdout: str) -> dict | None:
    """Return the final fixed-prefix installer result object, if it is valid."""
    marker_lines = [line for line in str(stdout or "").splitlines() if line.startswith(UPDATE_RESULT_PREFIX)]
    if not marker_lines:
        return None
    encoded = marker_lines[-1][len(UPDATE_RESULT_PREFIX) :]
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    fields = {
        "schemaVersion",
        "status",
        "updateMode",
        "dependenciesInstalled",
        "reusesRuntimeVenv",
        "sourceUpdated",
        "reason",
        "cacheUsed",
        "servicesStopped",
        "plannedDependenciesInstall",
        "managedServiceDefinitionsNormalized",
        "rollbackComplete",
        "stateCertain",
        "stage",
    }
    if (
        not isinstance(decoded, dict)
        or set(decoded) != fields
        or type(decoded.get("schemaVersion")) is not int
        or decoded.get("schemaVersion") != 1
        or decoded.get("status") not in {"completed", "failed"}
        or any(
            not isinstance(decoded.get(field), str) or not decoded[field].strip()
            for field in ("updateMode", "reason", "stage")
        )
        or any(
            type(decoded.get(field)) is not bool
            for field in (
                "dependenciesInstalled",
                "reusesRuntimeVenv",
                "cacheUsed",
                "servicesStopped",
                "plannedDependenciesInstall",
                "stateCertain",
            )
        )
        or any(
            value is not None and type(value) is not bool
            for value in (
                decoded.get("sourceUpdated"),
                decoded.get("managedServiceDefinitionsNormalized"),
                decoded.get("rollbackComplete"),
            )
        )
    ):
        return None
    return decoded


def _update_result_bool(envelope: dict, field: str) -> bool | None:
    value = envelope.get(field)
    return value if isinstance(value, bool) else None


def _update_result_text(envelope: dict, field: str) -> str | None:
    value = envelope.get(field)
    return value if isinstance(value, str) and value.strip() else None


def _apply_update_execution_result(payload: dict, envelope: dict | None, returncode: int) -> None:
    payload["status"] = "completed" if returncode == 0 else "failed"
    payload["returncode"] = returncode
    if envelope is None:
        payload.update(
            {
                "updateMode": "unknown",
                "dependenciesInstalled": None,
                "reusesRuntimeVenv": None,
                "sourceUpdated": None,
                "cacheUsed": None,
                "servicesStopped": None,
                "plannedDependenciesInstall": None,
                "rollbackComplete": None,
                "stateCertain": None,
                "resultAvailable": False,
                "stage": "bootstrap" if returncode else "bootstrap-completed-without-result",
                "reason": (
                    f"bootstrap failed with exit status {returncode} without a valid installer result envelope"
                    if returncode
                    else "bootstrap completed without a valid installer result envelope"
                ),
            }
        )
        payload["mutationPolicy"].update(
            {
                "dependenciesInstalled": None,
                "sourceUpdated": None,
                "managedServicesStoppedAfterPreflight": None,
                "managedServiceDefinitionsMayNormalize": None,
                "reusesRuntimeVenv": None,
            }
        )
        return

    update_mode = _update_result_text(envelope, "updateMode") or "unknown"
    dependencies_installed = _update_result_bool(envelope, "dependenciesInstalled")
    reuses_runtime_venv = _update_result_bool(envelope, "reusesRuntimeVenv")
    source_updated = _update_result_bool(envelope, "sourceUpdated")
    cache_used = _update_result_bool(envelope, "cacheUsed")
    services_stopped = _update_result_bool(envelope, "servicesStopped")
    planned_dependencies_install = _update_result_bool(envelope, "plannedDependenciesInstall")
    rollback_complete = _update_result_bool(envelope, "rollbackComplete")
    state_certain = _update_result_bool(envelope, "stateCertain")
    if rollback_complete is True:
        # A completed rollback is the stronger transaction fact: the prior
        # source, venv, control files, and service state were restored.
        state_certain = True
    reason = _update_result_text(envelope, "reason")
    stage = _update_result_text(envelope, "stage") or ("complete" if returncode == 0 else "installer")
    payload.update(
        {
            "updateMode": update_mode,
            "dependenciesInstalled": dependencies_installed,
            "reusesRuntimeVenv": reuses_runtime_venv,
            "sourceUpdated": source_updated,
            "reason": reason,
            "cacheUsed": cache_used,
            "servicesStopped": services_stopped,
            "plannedDependenciesInstall": planned_dependencies_install,
            "rollbackComplete": rollback_complete,
            "stateCertain": state_certain,
            "resultAvailable": True,
            "stage": stage,
        }
    )
    payload["mutationPolicy"].update(
        {
            "dependenciesInstalled": dependencies_installed,
            "sourceUpdated": source_updated,
            "managedServicesStoppedAfterPreflight": services_stopped,
            "managedServiceDefinitionsMayNormalize": _update_result_bool(
                envelope, "managedServiceDefinitionsNormalized"
            ),
            "reusesRuntimeVenv": reuses_runtime_venv,
        }
    )


def _update_output_without_result_envelopes(stdout: str) -> str:
    return "".join(
        line
        for line in str(stdout or "").splitlines(keepends=True)
        if not line.startswith(UPDATE_RESULT_PREFIX)
    )


def _write_update_human_result(payload: dict, returncode: int) -> None:
    target = sys.stdout if returncode == 0 else sys.stderr
    if returncode == 0:
        target.write(
            render_cli(
                "Update complete",
                fields=(("Status", "Ready"),),
                sections=(("Result", ("Actanara is up to date.",)),),
                next_steps=("actanara doctor",),
            )
        )
        return
    details = ["The update did not finish."]
    if payload.get("stateCertain") is False:
        details.append("Actanara could not confirm that recovery finished.")
    target.write(
        render_cli(
            "Update failed",
            fields=(("Status", "Failed"),),
            sections=(("What happened", details),),
            next_steps=("actanara doctor --installer", "Review the update log above, then try again"),
        )
    )


def _update_bootstrap_path(args: argparse.Namespace, runtime_home: Path) -> Path:
    bootstrap_name = "bootstrap-linux.sh" if platform.system() == "Linux" else "bootstrap.sh"
    if args.source_root:
        source_bootstrap = Path(args.source_root).expanduser() / "install" / bootstrap_name
        if source_bootstrap.is_file():
            return source_bootstrap
        raise FileNotFoundError(f"installer bootstrap not found under --source-root: {source_bootstrap}")
    checkout_bootstrap = ROOT / "install" / bootstrap_name
    if checkout_bootstrap.is_file():
        return checkout_bootstrap
    runtime_bootstrap = runtime_home.expanduser() / "app" / "source" / "install" / bootstrap_name
    if runtime_bootstrap.is_file():
        return runtime_bootstrap
    raise FileNotFoundError(
        "installer bootstrap is unavailable in both the Actanara package checkout and active Runtime app/source; "
        "reinstall Actanara or provide --source-root PATH"
    )


def _validate_update_source_selection(args: argparse.Namespace) -> None:
    if args.source_root and args.ref:
        raise ValueError("--source-root cannot be combined with --ref")
    if args.offline and not args.source_root and not args.ref:
        raise ValueError(
            "--offline requires --source-root PATH or an explicit full commit via --ref "
            "already present in the installer source cache"
        )
    if not args.source_root and not args.ref and str(args.source_url or "") != DEFAULT_UPDATE_SOURCE_URL:
        raise ValueError("a custom --source-url requires an explicit full commit via --ref")
    if args.ref and not UPDATE_FULL_COMMIT_RE.fullmatch(str(args.ref)):
        raise ValueError(
            "--ref must be a full 40- or 64-character hexadecimal commit ID for remote updates; "
            "branches, tags, and abbreviated commit IDs are not accepted"
        )


def _update_source_selection(args: argparse.Namespace) -> dict:
    if args.source_root:
        return {
            "mode": "local-source-root",
            "policy": "use the supplied local checkout without remote ref resolution",
            "resolvedBy": "caller",
            "commitPinnedByBootstrap": False,
        }
    if args.ref:
        return {
            "mode": "explicit-remote-commit",
            "policy": "fetch and pin the explicitly requested full commit",
            "resolvedBy": "bootstrap",
            "commitPinnedByBootstrap": True,
            "requestedCommit": str(args.ref),
        }
    return {
        "mode": "latest-stable-release",
        "policy": LATEST_STABLE_RELEASE_POLICY,
        "resolvedBy": "bootstrap",
        "commitPinnedByBootstrap": True,
    }


def _update_preserved_installer_args(runtime_home: Path) -> list[str]:
    try:
        settings = read_settings(
            runtime_paths_for_home(runtime_home),
            redact_secrets=False,
            persist_defaults=False,
        )
    except Exception as exc:
        raise ValueError(
            "Runtime Settings could not be read; the active dependency profile cannot be preserved safely"
        ) from exc
    if not isinstance(settings, dict):
        raise ValueError(
            "Runtime Settings are invalid; the active dependency profile cannot be preserved safely"
        )
    raw_rag = settings.get("rag", {})
    raw_features = settings.get("features", {})
    if not isinstance(raw_rag, dict) or not isinstance(raw_features, dict):
        raise ValueError(
            "Runtime Settings contain an invalid RAG profile; update is blocked before service changes"
        )
    rag_enabled = raw_rag.get("enabled", False)
    feature_enabled = raw_features.get("rag", False)
    if type(rag_enabled) is not bool or type(feature_enabled) is not bool:
        raise ValueError(
            "Runtime Settings contain an ambiguous RAG profile; update is blocked before service changes"
        )
    if rag_enabled != feature_enabled:
        raise ValueError(
            "Runtime Settings contain conflicting RAG dependency profile flags"
        )
    if rag_enabled:
        embedding = raw_rag.get("embedding")
        if not isinstance(embedding, dict):
            raise ValueError(
                "Runtime Settings do not identify the active RAG embedding dependency profile"
            )
        mode = str(embedding.get("mode") or "").strip()
        if mode not in {"local", "cloud"}:
            raise ValueError(
                "Runtime Settings do not identify a supported RAG embedding dependency profile"
            )
    # The installer inherits this profile independently from Runtime Settings.
    # Passing RAG flags here would turn preservation into an explicit Settings
    # rewrite and could replace a custom provider/model with installer defaults.
    return []


def _shell_join(command: list[str]) -> str:
    return " ".join(_shell_quote(part) for part in command)


def _shell_quote(value: str) -> str:
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _search_memory(args: argparse.Namespace) -> int:
    filters = {
        "date": args.date,
        "dateFrom": args.date_from,
        "dateTo": args.date_to,
        "project": args.project,
        "role": args.role,
        "sourceSets": args.source_set,
    }
    try:
        result = search_memory(
            args.query,
            top_k=args.top_k,
            dashboard_url=args.dashboard_url,
            timeout_seconds=args.timeout,
            filters=filters,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(compact_memory_results(result, max_results=args.top_k))
    return 0


def _task_counts(args: argparse.Namespace) -> int:
    try:
        paths = _paths_from_args(args) or load_paths()
        snapshot = diary_tasks_snapshot(paths)
        pending = pending_candidate_count(paths)
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    payload = {
        "authority": "Nova-Task v2 SQLite",
        "runtime": str(paths.home),
        "inProgress": int(snapshot.get("InProgress", 0)),
        "completed": int(snapshot.get("Completed", 0)),
        "pendingCandidates": int(pending),
    }
    payload["total"] = payload["inProgress"] + payload["completed"]
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            render_cli(
                "Tasks",
                fields=(
                    ("Total", payload["total"]),
                    ("In progress", payload["inProgress"]),
                    ("Completed", payload["completed"]),
                    ("Waiting for review", payload["pendingCandidates"]),
                ),
            )
        )
    return 0


def _onboarding_doctor(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    try:
        payload = actanara_onboarding_status(paths, selected_profiles=args.profiles)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    output = dump_actanara_onboarding_status_json(payload) if args.json else format_actanara_onboarding_status(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 1 if ((payload.get("readiness") or {}).get("status") == "error") else 0


def _onboarding_plan(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    try:
        payload = onboarding_subsystem_plan(args.profiles, paths)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    output = dump_onboarding_subsystem_plan_json(payload) if args.json else format_onboarding_subsystem_plan(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _onboarding_one_liner_dry_run(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    try:
        payload = onboarding_one_liner_dry_run(args.profiles, paths)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    output = dump_onboarding_one_liner_dry_run_json(payload) if args.json else format_onboarding_one_liner_dry_run(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _onboarding_one_liner_apply(args: argparse.Namespace) -> int:
    try:
        if args.use_default_runtime and args.runtime:
            raise ValueError("--use-default-runtime and --runtime cannot be used together")
        paths = _runtime_bootstrap_paths_from_args(args)
        if paths is None:
            raise ValueError("runtime apply requires --use-default-runtime or an explicit --runtime path")
        payload = onboarding_one_liner_apply(
            args.profiles,
            paths,
            confirmation_text=args.confirmation_text,
            select_active_runtime=args.select_active_runtime,
            language_profile=args.language,
            with_scheduler=args.with_scheduler,
            scheduler_confirmation_text=args.scheduler_confirmation_text,
        )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    output = dump_onboarding_apply_blocked_json(payload) if args.json else format_onboarding_apply_blocked(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return int(payload.get("exitCode", 1))


def _onboarding_one_liner_status(args: argparse.Namespace) -> int:
    paths = _candidate_paths_from_args(args)
    payload = onboarding_one_liner_status(paths)
    output = dump_onboarding_one_liner_status_json(payload) if args.json else format_onboarding_one_liner_status(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0 if payload.get("status") == "initialized" else 1


def _onboarding_one_liner_release_gate(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    try:
        payload = onboarding_one_liner_release_gate(args.profiles, paths, with_scheduler=args.with_scheduler)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    output = dump_onboarding_release_gate_json(payload) if args.json else format_onboarding_release_gate(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0 if payload.get("status") == "passed" else 1


def _onboarding_one_liner_validation_matrix(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    payload = onboarding_one_liner_validation_matrix(paths)
    output = (
        dump_onboarding_one_liner_validation_matrix_json(payload)
        if args.json
        else format_onboarding_one_liner_validation_matrix(payload)
    )
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0 if payload.get("status") == "passed" else 1


def _onboarding_rollback_plan_status(args: argparse.Namespace) -> int:
    paths = _candidate_paths_from_args(args)
    payload = onboarding_rollback_plan_status(paths)
    output = dump_onboarding_rollback_plan_status_json(payload) if args.json else format_onboarding_rollback_plan_status(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0 if payload.get("status") == "available" else 1


def _onboarding_apply_blocked(args: argparse.Namespace) -> int:
    try:
        apply_modes = [
            args.sandbox_apply,
            args.runtime_bootstrap_apply,
            args.scheduler_sandbox_apply,
            args.scheduler_plist_apply,
            args.scheduler_register_apply,
            args.scheduler_unregister_apply,
        ]
        if sum(1 for enabled in apply_modes if enabled) > 1:
            raise ValueError("--sandbox-apply, --runtime-bootstrap-apply, --scheduler-sandbox-apply, --scheduler-plist-apply, --scheduler-register-apply and --scheduler-unregister-apply cannot be used together")
        if args.select_active_runtime and not args.runtime_bootstrap_apply:
            raise ValueError("--select-active-runtime requires --runtime-bootstrap-apply")
        if args.use_default_runtime and not args.runtime_bootstrap_apply:
            raise ValueError("--use-default-runtime requires --runtime-bootstrap-apply")
        if args.use_default_runtime and args.runtime:
            raise ValueError("--use-default-runtime and --runtime cannot be used together")
        if args.runtime_bootstrap_apply:
            paths = _runtime_bootstrap_paths_from_args(args)
            payload = onboarding_apply_runtime_bootstrap(
                args.profiles,
                paths,
                confirmation_text=args.confirmation_text,
                select_active_runtime=args.select_active_runtime,
                language_profile=args.language,
            )
        elif args.scheduler_sandbox_apply:
            paths = _sandbox_paths_from_args(args) if args.runtime else None
            payload = onboarding_apply_scheduler_sandbox(
                args.profiles,
                paths,
                scheduler_home=Path(args.scheduler_home).expanduser() if args.scheduler_home else None,
                confirmation_text=args.confirmation_text,
            )
        elif args.scheduler_plist_apply:
            paths = _sandbox_paths_from_args(args) if args.runtime else None
            payload = onboarding_apply_scheduler_plist_write(
                args.profiles,
                paths,
                confirmation_text=args.confirmation_text,
            )
        elif args.scheduler_register_apply:
            paths = _sandbox_paths_from_args(args) if args.runtime else None
            payload = onboarding_apply_scheduler_register(
                args.profiles,
                paths,
                confirmation_text=args.confirmation_text,
            )
        elif args.scheduler_unregister_apply:
            paths = _sandbox_paths_from_args(args) if args.runtime else None
            payload = onboarding_apply_scheduler_unregister(
                args.profiles,
                paths,
                confirmation_text=args.confirmation_text,
            )
        elif args.sandbox_apply:
            paths = _sandbox_paths_from_args(args) if args.runtime else None
            payload = onboarding_apply_sandbox(
                args.profiles,
                paths,
                confirmation_text=args.confirmation_text,
                language_profile=args.language,
            )
        else:
            payload = onboarding_apply_blocked(args.profiles, confirmation_text=args.confirmation_text)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    output = dump_onboarding_apply_blocked_json(payload) if args.json else format_onboarding_apply_blocked(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return int(payload["exitCode"]) if "exitCode" in payload else 1


def _onboarding_release_gate(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    try:
        payload = onboarding_release_gate(args.profiles, paths, confirmation_text=args.confirmation_text)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    output = dump_onboarding_release_gate_json(payload) if args.json else format_onboarding_release_gate(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 1 if payload.get("status") != "passed" else 0


def _onboarding_approval_packet(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    try:
        payload = onboarding_approval_packet(args.profiles, paths, confirmation_text=args.confirmation_text)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    output = dump_onboarding_approval_packet_json(payload) if args.json else format_onboarding_approval_packet(payload)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 1 if payload.get("status") != "approved" else 0


def _paths_from_args(args: argparse.Namespace):
    paths = None
    if args.runtime:
        current = load_paths()
        paths = runtime_paths_for_home(
            Path(args.runtime).expanduser(),
            legacy_diary_root=Path(args.legacy_diary_root).expanduser() if args.legacy_diary_root else current.legacy_diary_root,
        )
    return paths


def _candidate_paths_from_args(args: argparse.Namespace):
    if args.runtime:
        current = load_paths()
        return runtime_paths_for_home(
            Path(args.runtime).expanduser(),
            legacy_diary_root=Path(args.legacy_diary_root).expanduser() if args.legacy_diary_root else current.legacy_diary_root,
        )
    return None


def _sandbox_paths_from_args(args: argparse.Namespace):
    current = load_paths()
    return runtime_paths_for_home(
        Path(args.runtime).expanduser(),
        legacy_diary_root=Path(args.legacy_diary_root).expanduser() if args.legacy_diary_root else current.legacy_diary_root,
    )


def _runtime_bootstrap_paths_from_args(args: argparse.Namespace):
    if args.runtime:
        return _sandbox_paths_from_args(args)
    if args.use_default_runtime:
        current = load_paths()
        return runtime_paths_for_home(
            default_oneliner_runtime_home(),
            legacy_diary_root=Path(args.legacy_diary_root).expanduser() if args.legacy_diary_root else current.legacy_diary_root,
        )
    return None


def _pipeline_run(args: argparse.Namespace) -> int:
    if getattr(args, "date_flag", None) and getattr(args, "date", None):
        sys.stderr.write("Error: provide the date once, either after `pipeline` or with --date.\n")
        return 2
    try:
        target_date = _normalize_cli_date(getattr(args, "date_flag", None) or getattr(args, "date", None))
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    paths = _paths_from_args(args)
    force = bool(getattr(args, "force", False))
    if not force and _daily_diary_complete_for_cli(paths, target_date):
        sys.stderr.write(
            f"Error: the diary for {target_date} is already complete. "
            "Use --force to create it again from the activity already collected.\n"
        )
        return 2
    if force:
        result = run_daily_pipeline(
            target_date,
            paths=paths,
            trigger="manual-regeneration-frozen",
            reuse_foundation_inputs=True,
        )
    else:
        result = run_daily_pipeline(target_date, paths=paths)
    result_sections = ()
    next_steps = ()
    if result.failed_step:
        result_sections = (("What happened", (f"Stopped while working on {_friendly_diary_step(result.failed_step)}.",)),)
        next_steps = ("actanara doctor --pipeline",)
    sys.stdout.write(
        render_cli(
            "Daily diary",
            fields=(
                ("Status", "Ready" if result.success else "Failed"),
                ("Date", result.business_date),
                ("Progress", f"{result.succeeded_steps} of {result.total_steps} steps"),
            ),
            sections=result_sections,
            next_steps=next_steps,
        )
    )
    return 0 if result.success else 1


def _friendly_diary_step(value: object) -> str:
    text = str(value or "").casefold()
    if "rag" in text or "search" in text:
        return "search memory"
    if "task" in text:
        return "task updates"
    if "source" in text or "collect" in text:
        return "activity collection"
    if any(token in text for token in ("narrative", "technical", "learning")):
        return "diary writing"
    if any(token in text for token in ("foundation", "materialization", "input")):
        return "diary preparation"
    if "language" in text:
        return "diary language settings"
    if "lock" in text:
        return "starting the diary"
    return "the daily diary"


def _scheduler_reconcile(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    payload = reconcile_pipeline_schedule(
        paths,
        apply=bool(args.apply),
        lookback_days=args.lookback_days,
        auto_limit_days=args.auto_limit_days,
    )
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        missing = payload.get("missingDates") or []
        runs = []
        for run in payload.get("runs") or []:
            runs.append(
                status_item(
                    bool(run.get("success")),
                    f"Diary for {run.get('date')} was created",
                    f"Diary for {run.get('date')} could not be created",
                )
            )
        next_steps = ()
        if payload.get("requiresConfirmation"):
            next_steps = ("Review the missed dates before starting a larger catch-up",)
        sys.stdout.write(
            render_cli(
                "Automatic daily runs",
                fields=(
                    ("Status", status_label(payload.get("status"))),
                    ("Missed diaries", payload.get("missingCount", 0)),
                    ("Date range", f"{payload.get('targetStart')} to {payload.get('targetEnd')}"),
                ),
                sections=(
                    ("Missed dates", [", ".join(map(str, missing))] if missing else []),
                    ("Catch-up", runs),
                ),
                next_steps=next_steps,
            )
        )
    return 1 if payload.get("status") == "partial" else 0


def _daily_diary_complete_for_cli(paths, target_date: str) -> bool:
    try:
        selected = paths or load_paths()
        return bool(evaluate_daily_completeness(selected, date.fromisoformat(target_date)).get("ready"))
    except Exception:
        return False


def _dashboard_restart(args: argparse.Namespace) -> int:
    if platform.system() == "Linux":
        paths = _paths_from_args(args) or load_paths()
        try:
            result = restart_dashboard_systemd_service(paths)
        except Exception as exc:
            if args.json:
                sys.stdout.write(
                    json.dumps(
                        {
                            "status": "failed",
                            "provider": "systemd-user",
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            else:
                sys.stderr.write(f"Dashboard restart failed: {exc}\n")
            return 1
        if args.json:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(
                render_cli(
                    "Dashboard",
                    fields=(("Status", "Restarted"),),
                    next_steps=("Open Dashboard and refresh the page",),
                )
            )
        return 0
    defaults = dashboard_launch_defaults()
    label = getattr(args, "label", None) or str(defaults.get("label") or "com.actanara.dashboard")
    code = restart_dashboard_service(label)
    if code == 0:
        sys.stdout.write(
            render_cli(
                "Dashboard",
                fields=(("Status", "Restarted"),),
                next_steps=("Open Dashboard and refresh the page",),
            )
        )
    return code


def _rag_rebuild(args: argparse.Namespace) -> int:
    return _rag_sync_command(
        args,
        action="rag-rebuild",
        confirmation=RAG_REBUILD_CONFIRMATION,
        requested_by="actanara-cli-rag-rebuild",
    )


def _rag_update(args: argparse.Namespace) -> int:
    return _rag_sync_command(
        args,
        action="rag-update",
        confirmation=RAG_UPDATE_CONFIRMATION,
        requested_by="actanara-cli-rag-update",
    )


def _rag_sync_command(
    args: argparse.Namespace,
    *,
    action: str,
    confirmation: str,
    requested_by: str,
) -> int:
    rag_settings = resolve_rag_settings(_paths_from_args(args))
    if not args.dry_run and args.confirmation_text is not None and str(args.confirmation_text or "") != confirmation:
        sys.stderr.write(f"Error: confirmation must exactly match: {confirmation}\n")
        return 2
    if args.dry_run or str(args.confirmation_text or "") != confirmation:
        plan = plan_v2_production_sync(
            rag_settings,
            action=action,
            requested_by=requested_by,
            promote=True,
            confirmation_text=confirmation,
        )
        if args.json:
            sys.stdout.write(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        else:
            title = "Rebuild memory" if action == "rag-rebuild" else "Refresh memory"
            sys.stdout.write(
                render_cli(
                    title,
                    fields=(("Status", "Ready to start"),),
                    next_steps=(f'actanara {action} --confirm "{confirmation}"',),
                )
            )
        return 0
    try:
        result = sync_v2_production_index(rag_settings, requested_by=requested_by, promote=True)
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if args.json:
        sys.stdout.write(json.dumps({"action": action, **result}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        title = "Memory rebuilt" if action == "rag-rebuild" else "Memory refreshed"
        sys.stdout.write(
            render_cli(
                title,
                fields=(("Status", status_label(result.get("status"))),),
                next_steps=(() if result.get("status") == "promoted" else ("actanara doctor --rag",)),
            )
        )
    return 0 if result.get("status") == "promoted" else 1


def _parse_optional_date(value: str | None):
    if not value:
        return None

    return date.fromisoformat(value)


def _normalize_cli_date(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if re.fullmatch(r"\d{6}", raw):
        normalized = f"20{raw[0:2]}-{raw[2:4]}-{raw[4:6]}"
        date.fromisoformat(normalized)
        return normalized
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        date.fromisoformat(raw)
        return raw
    raise ValueError("date must be YYYY-MM-DD or YYMMDD")


def _parse_config_value(value: str):
    raw = str(value)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _nested_update(path: str, value) -> dict:
    parts = [part for part in str(path or "").split(".") if part]
    if len(parts) < 2:
        raise ValueError("config set path must include a settings group and field")
    root: dict = {}
    cursor = root
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    return root


def _get_dot_path(payload: dict, path: str):
    cursor = payload
    for part in [part for part in str(path or "").split(".") if part]:
        if not isinstance(cursor, dict) or part not in cursor:
            raise KeyError(path)
        cursor = cursor[part]
    return cursor


def _format_scalar(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def _foundation_rebuild_sqlite_cache(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args) or load_paths()
    try:
        start_date = _parse_optional_date(args.start_date)
        end_date = _parse_optional_date(args.end_date)
        if args.dry_run or not args.confirmation_text:
            payload = plan_sqlite_cache_rebuild(paths, start_date=start_date, end_date=end_date)
        else:
            payload = rebuild_sqlite_cache(
                paths,
                confirmation_text=args.confirmation_text,
                start_date=start_date,
                end_date=end_date,
            )
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        next_steps = (
            (f'actanara foundation rebuild-sqlite-cache --confirm "{SQLITE_CACHE_REBUILD_CONFIRMATION}"',)
            if payload.get("dryRun")
            else ()
        )
        backup = payload.get("backup") if isinstance(payload.get("backup"), dict) else {}
        sys.stdout.write(
            render_cli(
                "Rebuild local data",
                fields=(
                    ("Status", "Ready to start" if payload.get("dryRun") else status_label(payload.get("status", "completed"))),
                    ("Data folder", payload.get("runtime")),
                    ("Backup", backup.get("backupDir")),
                ),
                next_steps=next_steps,
            )
        )
    return 0


def _foundation_approve_diary_metrics(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args) or load_paths()
    try:
        business_date = date.fromisoformat(_normalize_cli_date(args.date) or "")
        plan = _diary_metrics_approval_plan(paths, business_date)
        if (
            not args.dry_run
            and args.confirmation_text is not None
            and str(args.confirmation_text or "") != DIARY_METRICS_APPROVAL_CONFIRMATION
        ):
            sys.stderr.write(f"Error: confirmation must exactly match: {DIARY_METRICS_APPROVAL_CONFIRMATION}\n")
            return 2
        if args.dry_run or str(args.confirmation_text or "") != DIARY_METRICS_APPROVAL_CONFIRMATION:
            payload = plan
        else:
            approval = write_diary_metrics_table_mismatch_approval(
                paths,
                business_date,
                operator=args.operator,
                note=args.note,
            )
            report = write_diary_metrics_readiness_report(
                paths,
                business_date,
                approve_model_usage_normalization=True,
                approve_session_count_normalization=True,
            )
            payload = {
                **plan,
                "dryRun": False,
                "status": "approved",
                "mutationPolicy": {
                    "sourceFactsChanged": False,
                    "sqliteUsageRowsChanged": False,
                    "approvalAuditAppended": True,
                    "readinessReportRegenerated": True,
                },
                "approval": approval,
                "readiness": {
                    "status": report.get("status"),
                    "canEnable": report.get("canEnable"),
                    "path": str(paths.state_dir / "migration" / f"diary-metrics-readiness-{business_date.isoformat()}.json"),
                },
            }
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        next_steps = (
            (
                "actanara foundation approve-diary-metrics "
                f"{payload.get('businessDate')} --confirm \"{DIARY_METRICS_APPROVAL_CONFIRMATION}\""
            ,)
            if payload.get("dryRun")
            else ()
        )
        sys.stdout.write(
            render_cli(
                "Approve diary totals",
                fields=(
                    ("Status", "Ready to review" if payload.get("dryRun") else "Approved"),
                    ("Date", payload.get("businessDate")),
                    ("Data folder", payload.get("runtime")),
                ),
                next_steps=next_steps,
            )
        )
    return 0


def _diary_metrics_approval_plan(paths, business_date: date) -> dict:
    report_path = paths.state_dir / "migration" / f"diary-metrics-readiness-{business_date.isoformat()}.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"missing diary metrics readiness report: {report_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid diary metrics readiness report: {report_path}") from exc
    differences = ((report.get("tableMetrics") or {}).get("differences") or {})
    can_enable = (report.get("canEnable") or {}).get("diaryMetricsSourceFoundation")
    from data_foundation.diary_metrics import _stable_json_digest

    return {
        "action": "approve-diary-metrics",
        "businessDate": business_date.isoformat(),
        "runtime": str(paths.home),
        "reportPath": str(report_path),
        "dryRun": True,
        "status": "plan",
        "currentReadinessStatus": report.get("status"),
        "alreadyEnabled": bool(can_enable),
        "hasTableDifferences": bool(differences),
        "differencesDigest": _stable_json_digest(differences) if differences else "",
        "differences": differences,
        "confirmationTextRequired": DIARY_METRICS_APPROVAL_CONFIRMATION,
        "mutationPolicy": {
            "sourceFactsChanged": False,
            "sqliteUsageRowsChanged": False,
            "approvalAuditAppended": False,
            "readinessReportRegenerated": False,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
