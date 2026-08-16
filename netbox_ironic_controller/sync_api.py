import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from .config import get_settings
from .sync import IronicSyncClient, KubernetesSecretStore, NetBoxIronicController, NetBoxSyncClient


async def reconcile_loop(app: FastAPI) -> None:
    while True:
        try:
            result = await app.state.controller.reconcile()
            app.state.last_result = result.__dict__
            app.state.last_success = datetime.now(timezone.utc).isoformat()
            app.state.last_error = None
        except Exception as exc:
            app.state.last_error = str(exc)
        await asyncio.sleep(app.state.settings.sync_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.controller = NetBoxIronicController(
        settings,
        NetBoxSyncClient(settings),
        IronicSyncClient(settings),
        KubernetesSecretStore(settings.sync_bmc_secret_namespace),
    )
    app.state.last_result, app.state.last_success, app.state.last_error = None, None, None
    task = asyncio.create_task(reconcile_loop(app))
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="NetBox-Ironic Sync Controller", version="0.5.1", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "last_success": app.state.last_success, "last_error": app.state.last_error}


@app.post("/reconcile")
async def reconcile():
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
            "last_result": app.state.last_result}
