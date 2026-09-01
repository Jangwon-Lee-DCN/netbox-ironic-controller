from __future__ import annotations

from asyncio import Lock, to_thread
from time import monotonic

import httpx

from .access_domain import Actor, DomainError
from .config import Settings


class KeystoneTokenValidator:
    def __init__(self, settings: Settings):
        from openstack import connection

        self.endpoint = settings.openstack_auth_url.rstrip("/") + "/auth/tokens"
        self.verify = settings.netbox_verify_tls
        scope = ({"project_id": settings.openstack_project_id}
                 if settings.openstack_project_id
                 else {"system_scope": settings.openstack_system_scope})
        self.connection = connection.Connection(
            auth_url=settings.openstack_auth_url,
            username=settings.openstack_username,
            password=settings.openstack_password,
            user_domain_name=settings.openstack_user_domain_name,
            **scope,
            region_name=settings.openstack_region,
            interface=settings.openstack_interface,
            identity_api_version="3",
        )
        self._cache: dict[str, tuple[float, Actor]] = {}
        self._lock = Lock()

    async def validate(self, subject_token: str) -> Actor:
        if not subject_token:
            raise DomainError("X-Auth-Token is required")
        cached = self._cache.get(subject_token)
        if cached and cached[0] > monotonic():
            return cached[1]
        async with self._lock:
            cached = self._cache.get(subject_token)
            if cached and cached[0] > monotonic():
                return cached[1]
            service_token = await to_thread(self.connection.authorize)
            async with httpx.AsyncClient(verify=self.verify, timeout=10) as client:
                response = await client.get(self.endpoint, headers={
                    "X-Auth-Token": service_token,
                    "X-Subject-Token": subject_token,
                })
            if response.status_code in (401, 404):
                raise DomainError("token is invalid or expired")
            response.raise_for_status()
            actor = actor_from_token_body(response.json())
            self._cache[subject_token] = (monotonic() + 30, actor)
            return actor


def actor_from_token_body(body: dict) -> Actor:
    token = body.get("token") or {}
    project = token.get("project") or {}
    user = token.get("user") or {}
    if not project.get("id"):
        raise DomainError("a project-scoped token is required")
    if not user.get("id"):
        raise DomainError("token user is missing")
    return Actor(
        user_id=user["id"],
        project_id=project["id"],
        roles=frozenset(role["name"] for role in token.get("roles") or [] if role.get("name")),
    )
