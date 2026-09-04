import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from .config import get_settings
from .access_api import configure_access, router as access_router
from .access_domain import Actor
from .sync import IronicSyncClient, KubernetesSecretStore, NetBoxIronicController, NetBoxSyncClient


async def reconcile_once(app: FastAPI) -> None:
    if app.state.settings.sync_enabled:
        try:
            result = await app.state.controller.reconcile()
            app.state.last_result = result.__dict__
            app.state.last_success = datetime.now(timezone.utc).isoformat()
            app.state.last_error = None
        except Exception as exc:
            app.state.last_error = str(exc)
    if app.state.settings.access_enabled:
        try:
            service_actor = Actor(
                "baremetal-access-service", app.state.settings.access_dcn_project_id,
                frozenset({"baremetal_admin"}),
            )
            app.state.expired_requests = await app.state.access_coordinator.expire_leases(
                service_actor,
            )
            app.state.last_expiry_success = datetime.now(timezone.utc).isoformat()
            app.state.last_expiry_error = None
        except Exception as exc:
            app.state.last_expiry_error = str(exc)


async def reconcile_loop(app: FastAPI) -> None:
    while True:
        await reconcile_once(app)
        await asyncio.sleep(app.state.settings.sync_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    netbox = NetBoxSyncClient(settings)
    ironic = IronicSyncClient(settings)
    app.state.controller = NetBoxIronicController(
        settings, netbox, ironic, KubernetesSecretStore(settings.sync_bmc_secret_namespace),
    )
    app.state.last_result, app.state.last_success, app.state.last_error = None, None, None
    app.state.expired_requests = []
    app.state.last_expiry_success, app.state.last_expiry_error = None, None
    if settings.access_enabled:
        if not settings.access_dcn_project_id:
            raise RuntimeError("RACKD_ACCESS_DCN_PROJECT_ID is required when access is enabled")
        configure_access(app, settings, netbox, ironic)
    task = asyncio.create_task(reconcile_loop(app))
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="DCN Bare Metal Access Service", version="0.6.0", lifespan=lifespan)
app.include_router(access_router)


@app.get("/healthz")
async def healthz():
    database_error = None
    if app.state.settings.access_enabled:
        try:
            await asyncio.to_thread(app.state.access_store.ping)
        except Exception as exc:
            database_error = str(exc)
    expiry_error = getattr(app.state, "last_expiry_error", None)
    payload = {
        "status": "degraded" if app.state.last_error or expiry_error or database_error else "ok",
        "last_success": app.state.last_success,
        "last_error": app.state.last_error,
        "last_expiry_success": getattr(app.state, "last_expiry_success", None),
        "last_expiry_error": expiry_error,
        "database_error": database_error,
    }
    if database_error:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.post("/reconcile")
async def reconcile():
    if not app.state.settings.sync_enabled:
        raise HTTPException(status_code=409, detail="inventory synchronization is disabled")
    try:
        result = await app.state.controller.reconcile()
        app.state.last_result = result.__dict__
        app.state.last_success = datetime.now(timezone.utc).isoformat()
        app.state.last_error = None
        return result.__dict__
    except Exception as exc:
        app.state.last_error = str(exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/status")
async def status():
    return {"last_success": app.state.last_success, "last_error": app.state.last_error,
            "last_result": app.state.last_result,
            "expired_requests": app.state.expired_requests,
            "last_expiry_success": app.state.last_expiry_success,
            "last_expiry_error": app.state.last_expiry_error}
