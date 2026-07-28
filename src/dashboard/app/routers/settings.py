from fastapi import APIRouter, BackgroundTasks
try:
    from fastapi import Query
except ImportError:  # pragma: no cover - exercised by lightweight test stubs
    def Query(default=None, *args, **kwargs):
        return default
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from app.services import msgbox, rag_index_jobs, scheduler, service_manager, settings, tailscale
from app.services.dashboard_security import dashboard_security_config
from data_foundation.onboarding_plan import onboarding_subsystem_plan
from data_foundation.onboarding_status import actanara_onboarding_status
from data_foundation.settings_transaction import SettingsTransactionError
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


def _tailscale_status_with_access():
    status = tailscale.tailscale_status()
    access = tailscale.dashboard_access_status(
        status,
        dashboard_security_config().get("allowedOrigins") or [],
    )
    status["dashboardAccess"] = access
    status["requiredOrigin"] = access.get("origin")
    status["configurationReady"] = bool(access.get("ready"))
    status["serve"]["enableConfirmationTextRequired"] = tailscale.ENABLE_CONFIRMATION
    status["serve"]["disableConfirmationTextRequired"] = tailscale.DISABLE_CONFIRMATION
    status["canEnableServe"] = bool(status.get("canEnableServe") and access.get("ready"))
    return status


def _tailscale_serve_action(enabled: bool, payload: dict | None):
    status = _tailscale_status_with_access()
    if enabled and not (status.get("dashboardAccess") or {}).get("ready"):
        raise tailscale.TailscalePolicyError(
            "dashboard-origin-not-allowlisted",
            "Save the detected MagicDNS HTTPS origin in Dashboard publicBaseUrl/allowedOrigins before enabling Serve.",
        )
    return tailscale.set_dashboard_serve(enabled, payload, observed_status=status)


def _tailscale_error_response(error: tailscale.TailscaleError) -> JSONResponse:
    status_code = 502 if isinstance(error, tailscale.TailscaleCommandError) else 409
    return JSONResponse({"error": str(error), "code": error.code}, status_code=status_code)


@router.get("/settings")
async def api_get_settings():
    try:
        return settings.get_settings()
    except Exception as e:
        logger.exception("GET /api/settings failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/settings")
async def api_update_settings(payload: dict):
    try:
        return settings.update_settings(payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("PUT /api/settings failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/settings/bundle")
async def api_update_settings_bundle(payload: dict):
    try:
        return settings.update_settings_bundle(payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("PUT /api/settings/bundle failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/llm-provider")
async def api_get_llm_provider():
    try:
        return settings.get_llm_provider()
    except Exception as e:
        logger.exception("GET /api/llm-provider failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/llm-provider")
async def api_update_llm_provider(payload: dict):
    try:
        return await run_in_threadpool(settings.update_llm_provider, payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("PUT /api/llm-provider failed")
        return JSONResponse({"error": str(e)}, status_code=500)


def _settings_transaction_error_response(error: SettingsTransactionError) -> JSONResponse:
    status_code = 409 if error.summary.get("conflict") else 500
    return JSONResponse(
        {"error": str(error), "settingsTransaction": error.summary},
        status_code=status_code,
    )


@router.post("/llm-provider/test")
async def api_test_llm_provider(payload: dict | None = None):
    try:
        return await run_in_threadpool(settings.test_llm_provider, payload)
    except Exception as e:
        logger.exception("POST /api/llm-provider/test failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/llm-provider-chain")
async def api_get_llm_provider_chain():
    try:
        return await run_in_threadpool(settings.get_llm_provider_chain)
    except Exception as e:
        logger.exception("GET /api/llm-provider-chain failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/llm-provider-chain")
async def api_update_llm_provider_chain(payload: dict):
    try:
        return await run_in_threadpool(settings.update_llm_provider_chain, payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("PUT /api/llm-provider-chain failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/llm-provider-chain/test")
async def api_test_llm_provider_chain_entry(payload: dict | None = None):
    try:
        return await run_in_threadpool(settings.test_llm_provider_chain_entry, payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/llm-provider-chain/test failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/msgbox")
async def api_msgbox(limit: int = 20):
    try:
        return msgbox.message_box(limit=limit)
    except Exception as e:
        logger.exception("GET /api/msgbox failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/msgbox/{message_id}/read")
async def api_msgbox_mark_read(message_id: str):
    try:
        return msgbox.mark_read(message_id)
    except Exception as e:
        logger.exception("POST /api/msgbox/%s/read failed", message_id)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/scheduler")
async def api_scheduler_status():
    try:
        return scheduler.scheduler_status()
    except Exception as e:
        logger.exception("GET /api/settings/scheduler failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/tailscale/status")
async def api_tailscale_status():
    try:
        return await run_in_threadpool(_tailscale_status_with_access)
    except Exception as e:
        logger.exception("GET /api/settings/tailscale/status failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/tailscale/serve/enable")
async def api_tailscale_serve_enable(payload: dict | None = None):
    try:
        return await run_in_threadpool(_tailscale_serve_action, True, payload)
    except tailscale.TailscaleError as e:
        return _tailscale_error_response(e)
    except Exception as e:
        logger.exception("POST /api/settings/tailscale/serve/enable failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/tailscale/serve/disable")
async def api_tailscale_serve_disable(payload: dict | None = None):
    try:
        return await run_in_threadpool(_tailscale_serve_action, False, payload)
    except tailscale.TailscaleError as e:
        return _tailscale_error_response(e)
    except Exception as e:
        logger.exception("POST /api/settings/tailscale/serve/disable failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/scheduler/system-timer/preview")
async def api_scheduler_system_timer_preview():
    try:
        return scheduler.preview_system_timer()
    except Exception as e:
        logger.exception("GET /api/settings/scheduler/system-timer/preview failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/onboarding/status")
async def api_onboarding_status(profile: list[str] | None = Query(None)):
    try:
        return actanara_onboarding_status(selected_profiles=profile)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("GET /api/onboarding/status failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/onboarding/plan")
async def api_onboarding_plan(profile: list[str] | None = Query(None)):
    try:
        return onboarding_subsystem_plan(profile)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("GET /api/onboarding/plan failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/scheduler/system-timer/install")
async def api_scheduler_system_timer_install(payload: dict | None = None):
    try:
        return scheduler.install_system_timer(payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/scheduler/system-timer/install failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/scheduler/system-timer/uninstall")
async def api_scheduler_system_timer_uninstall(payload: dict | None = None):
    try:
        return scheduler.uninstall_system_timer(payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/scheduler/system-timer/uninstall failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/launcher/dashboard/preview")
async def api_dashboard_launch_agent_preview():
    try:
        return service_manager.preview_service("dashboard")
    except Exception as e:
        logger.exception("GET /api/settings/launcher/dashboard/preview failed")
        return JSONResponse({"error": str(e)}, status_code=500)


def _service_manager_mutation(kind: str, action: str, payload: dict | None):
    if service_manager.service_action_requires_async(kind, action, payload):
        queued = service_manager.enqueue_service_action(kind, action, payload)
        return JSONResponse(queued, status_code=202)
    if action == "install":
        return service_manager.install_service(kind, payload)
    if action == "uninstall":
        return service_manager.uninstall_service(kind, payload)
    if action in {"start", "stop", "restart"}:
        return service_manager.control_service(kind, action, payload)
    raise ValueError("service action must be install, uninstall, start, stop, or restart")


@router.post("/settings/launcher/dashboard/install")
async def api_dashboard_launch_agent_install(payload: dict | None = None):
    try:
        return _service_manager_mutation("dashboard", "install", payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except service_manager.ServiceManagerError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/launcher/dashboard/install failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/launcher/dashboard/uninstall")
async def api_dashboard_launch_agent_uninstall(payload: dict | None = None):
    try:
        return _service_manager_mutation("dashboard", "uninstall", payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except service_manager.ServiceManagerError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/launcher/dashboard/uninstall failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/launcher/rag/preview")
async def api_rag_launch_agent_preview():
    try:
        return service_manager.preview_service("rag")
    except Exception as e:
        logger.exception("GET /api/settings/launcher/rag/preview failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/launcher/rag/install")
async def api_rag_launch_agent_install(payload: dict | None = None):
    try:
        return _service_manager_mutation("rag", "install", payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except service_manager.ServiceManagerError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/launcher/rag/install failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/launcher/rag/uninstall")
async def api_rag_launch_agent_uninstall(payload: dict | None = None):
    try:
        return _service_manager_mutation("rag", "uninstall", payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except service_manager.ServiceManagerError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/launcher/rag/uninstall failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/services/{kind}/preview")
async def api_service_manager_preview(kind: str):
    try:
        return service_manager.preview_service(kind)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("GET /api/settings/services/%s/preview failed", kind)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/services/{kind}/{action}")
async def api_service_manager_action(kind: str, action: str, payload: dict | None = None):
    try:
        return _service_manager_mutation(kind, action, payload)
    except SettingsTransactionError as e:
        return _settings_transaction_error_response(e)
    except service_manager.ServiceManagerError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/services/%s/%s failed", kind, action)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/path-browser")
async def api_path_browser(path: str | None = None):
    try:
        return settings.browse_path(path)
    except Exception as e:
        logger.exception("GET /api/settings/path-browser failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/workspace-attribution")
async def api_workspace_attribution_status():
    try:
        return settings.workspace_attribution_status()
    except Exception as e:
        logger.exception("GET /api/settings/workspace-attribution failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/workspace-attribution/path/validate")
async def api_workspace_attribution_path_validate(path: str):
    try:
        return settings.workspace_attribution_path_validate(path)
    except Exception as e:
        logger.exception("GET /api/settings/workspace-attribution/path/validate failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/workspace-attribution/rules/preview")
async def api_workspace_attribution_rule_preview(payload: dict):
    try:
        return settings.workspace_attribution_rule_preview(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/workspace-attribution/rules/preview failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/workspace-attribution/rules")
async def api_workspace_attribution_rule_add(payload: dict):
    try:
        return settings.workspace_attribution_rule_add(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/workspace-attribution/rules failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/external-tools/catalog")
async def api_external_tool_catalog():
    try:
        return settings.external_tool_catalog()
    except Exception as e:
        logger.exception("GET /api/settings/external-tools/catalog failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/external-tools/rediscover")
async def api_external_tool_rediscover():
    try:
        return settings.rediscover_external_tool_paths()
    except Exception as e:
        logger.exception("POST /api/settings/external-tools/rediscover failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/external-tools/add")
async def api_external_tool_add(payload: dict):
    try:
        return settings.add_external_tool(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/external-tools/add failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/runtime-path")
async def api_current_runtime_path():
    try:
        return settings.current_runtime_path()
    except Exception as e:
        logger.exception("GET /api/settings/runtime-path failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/diary-path/consistency")
async def api_diary_path_consistency():
    try:
        return settings.diary_path_consistency()
    except Exception as e:
        logger.exception("GET /api/settings/diary-path/consistency failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/diary-path/rebuild")
async def api_rebuild_diary_path_projection(payload: dict):
    try:
        return settings.rebuild_diary_path_projection(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/diary-path/rebuild failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/diary-path/rebuild/jobs")
async def api_diary_path_rebuild_jobs(limit: int = 20):
    try:
        return settings.diary_path_rebuild_jobs(limit=limit)
    except Exception as e:
        logger.exception("GET /api/settings/diary-path/rebuild/jobs failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/sqlite-cache/rebuild")
async def api_sqlite_cache_rebuild(payload: dict):
    try:
        return settings.sqlite_cache_rebuild(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/sqlite-cache/rebuild failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/runtime-path/validate")
async def api_validate_runtime_path(path: str | None = None):
    try:
        return settings.validate_runtime_path(path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("GET /api/settings/runtime-path/validate failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/runtime-path/select")
async def api_select_runtime_path(payload: dict):
    try:
        return settings.select_runtime_path(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/runtime-path/select failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/rag/status")
async def api_rag_status(probe: bool = True):
    try:
        return settings.get_rag_status(probe_server=probe)
    except Exception as e:
        logger.exception("GET /api/rag/status failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/rag/settings")
async def api_get_rag_settings():
    try:
        return settings.get_rag_settings()
    except Exception as e:
        logger.exception("GET /api/rag/settings failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/rag/settings")
async def api_update_rag_settings(payload: dict):
    try:
        return settings.update_rag_settings(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("PUT /api/rag/settings failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/external-sources/plan")
async def api_rag_external_sources_plan(payload: dict | None = None):
    try:
        return await run_in_threadpool(settings.plan_rag_external_sources, payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/external-sources/plan failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/server/start")
async def api_rag_server_start(payload: dict | None = None):
    try:
        return settings.rag_operator_action("server-start", payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/server/start failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/server/stop")
async def api_rag_server_stop(payload: dict | None = None):
    try:
        return settings.rag_operator_action("server-stop", payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/server/stop failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/index/run")
async def api_rag_index_run(background_tasks: BackgroundTasks):
    try:
        queued = rag_index_jobs.queue_candidate_refresh(requested_by="dashboard")
        if not queued.get("accepted", True):
            return queued
        background_tasks.add_task(rag_index_jobs.execute_candidate_refresh, queued["jobId"])
        return JSONResponse(queued, status_code=202)
    except Exception as e:
        logger.exception("POST /api/rag/index/run failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/sync/run")
async def api_rag_sync_run(background_tasks: BackgroundTasks):
    try:
        queued = rag_index_jobs.queue_production_sync(requested_by="dashboard")
        if not queued.get("accepted", True):
            return queued
        background_tasks.add_task(rag_index_jobs.execute_production_sync, queued["jobId"])
        return JSONResponse(queued, status_code=202)
    except Exception as e:
        logger.exception("POST /api/rag/sync/run failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/profile/migrate/plan")
async def api_rag_profile_migrate_plan(payload: dict):
    try:
        return rag_index_jobs.plan_profile_migration(payload, requested_by="dashboard")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/profile/migrate/plan failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/profile/migrate")
async def api_rag_profile_migrate(background_tasks: BackgroundTasks, payload: dict):
    try:
        queued = rag_index_jobs.queue_profile_migration(payload, requested_by="dashboard")
        if not queued.get("accepted", True):
            return queued
        background_tasks.add_task(rag_index_jobs.execute_profile_migration, queued["jobId"])
        return JSONResponse(queued, status_code=202)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/profile/migrate failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/search")
async def api_rag_search(payload: dict):
    try:
        return settings.rag_search(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/search failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/rag/stats")
async def api_rag_stats():
    try:
        return settings.rag_stats()
    except Exception as e:
        logger.exception("GET /api/rag/stats failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/rag/coverage")
async def api_rag_coverage():
    try:
        return settings.rag_coverage()
    except Exception as e:
        logger.exception("GET /api/rag/coverage failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/rag/eval/latest")
async def api_rag_eval_latest():
    try:
        return settings.rag_eval_latest()
    except Exception as e:
        logger.exception("GET /api/rag/eval/latest failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/v2/promote")
async def api_rag_v2_promote(payload: dict):
    try:
        return settings.rag_v2_promote(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/v2/promote failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/v2/manifest/rollback")
async def api_rag_v2_manifest_rollback(payload: dict):
    try:
        return settings.rag_v2_manifest_rollback(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/v2/manifest/rollback failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/rag/external/health")
async def api_rag_external_health(probe: bool = True):
    try:
        status = settings.get_rag_status(probe_server=probe)
        return {
            "available": bool(status.get("searchAvailable")),
            "readOnly": True,
            "mutationAllowed": False,
            "externalAgentContract": _external_agent_contract(),
            "ragStatus": status,
        }
    except Exception as e:
        logger.exception("GET /api/rag/external/health failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/rag/external/stats")
async def api_rag_external_stats():
    try:
        result = settings.rag_stats()
        result["externalAgentContract"] = _external_agent_contract()
        return result
    except Exception as e:
        logger.exception("GET /api/rag/external/stats failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/rag/external/contract")
async def api_rag_external_contract():
    return settings.rag_external_agent_contract()


@router.get("/settings/external-tools/rag-skill-registration/plan")
async def api_rag_external_skill_registration_plan():
    try:
        return settings.rag_external_skill_registration_plan({"dryRun": True})
    except Exception as e:
        logger.exception("GET /api/settings/external-tools/rag-skill-registration/plan failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/settings/external-tools/rag-skill-registration")
async def api_rag_external_skill_registration(payload: dict):
    try:
        return settings.rag_external_skill_registration(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("POST /api/settings/external-tools/rag-skill-registration failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/settings/external-tools/rag-skill-registration/jobs")
async def api_rag_external_skill_registration_jobs(limit: int = 20):
    try:
        return settings.rag_external_skill_registration_jobs(limit=limit)
    except Exception as e:
        logger.exception("GET /api/settings/external-tools/rag-skill-registration/jobs failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/rag/external/search")
async def api_rag_external_search(payload: dict):
    query = str((payload or {}).get("query") or "")
    try:
        top_k = int((payload or {}).get("topK") or (payload or {}).get("top_k") or 5)
    except (TypeError, ValueError):
        top_k = 5
    try:
        result = settings.normalize_external_rag_search_response(
            settings.rag_search(payload),
            query=query,
            top_k=top_k,
        )
        result["externalAgentContract"] = _external_agent_contract()
        return result
    except ValueError as e:
        result = settings.normalize_external_rag_search_response(
            {"available": False, "reason": str(e), "error": str(e), "results": []},
            query=query,
            top_k=top_k,
        )
        result["externalAgentContract"] = _external_agent_contract()
        return JSONResponse(result, status_code=400)
    except Exception as e:
        logger.exception("POST /api/rag/external/search failed")
        result = settings.normalize_external_rag_search_response(
            {"available": False, "reason": str(e), "error": str(e), "results": []},
            query=query,
            top_k=top_k,
        )
        result["externalAgentContract"] = _external_agent_contract()
        return JSONResponse(result, status_code=500)


@router.put("/rag/external/settings")
@router.post("/rag/external/index/run")
@router.post("/rag/external/server/start")
@router.post("/rag/external/server/stop")
@router.post("/rag/external/memory/write")
@router.post("/rag/external/source/create")
async def api_rag_external_reject_mutation():
    return JSONResponse(
        {
            "error": "rag-external-api-read-only",
            "message": "External RAG agent API is read-only; settings, server, index, memory and source mutations are not allowed.",
            "externalAgentContract": _external_agent_contract(),
        },
        status_code=403,
    )


def _external_agent_contract() -> dict:
    return settings.rag_external_agent_contract()
