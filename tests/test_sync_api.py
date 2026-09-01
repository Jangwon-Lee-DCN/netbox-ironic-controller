from types import SimpleNamespace

from netbox_ironic_controller.sync_api import reconcile_once


class Expirer:
    def __init__(self, error=None):
        self.actor = None
        self.error = error

    async def expire_leases(self, actor):
        self.actor = actor
        if self.error:
            raise RuntimeError(self.error)
        return ["expired-request"]


async def test_access_expiry_runs_when_inventory_sync_is_disabled():
    expirer = Expirer()
    state = SimpleNamespace(
        settings=SimpleNamespace(sync_enabled=False, access_enabled=True,
                                 access_dcn_project_id="dcn"),
        access_coordinator=expirer, expired_requests=[],
        last_expiry_success=None, last_expiry_error=None,
    )
    await reconcile_once(SimpleNamespace(state=state))
    assert state.expired_requests == ["expired-request"]
    assert state.last_expiry_success is not None
    assert state.last_expiry_error is None
    assert expirer.actor.project_id == "dcn"
    assert expirer.actor.roles == frozenset({"baremetal_admin"})


async def test_access_expiry_failure_is_reported_without_claiming_success():
    state = SimpleNamespace(
        settings=SimpleNamespace(sync_enabled=False, access_enabled=True,
                                 access_dcn_project_id="dcn"),
        access_coordinator=Expirer("cleaning failed"), expired_requests=[],
        last_expiry_success=None, last_expiry_error=None,
    )
    await reconcile_once(SimpleNamespace(state=state))
    assert state.last_expiry_success is None
    assert state.last_expiry_error == "cleaning failed"
