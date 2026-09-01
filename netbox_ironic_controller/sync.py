from __future__ import annotations

import base64
import json
from asyncio import to_thread
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import httpx

from .config import Settings


class SecretStore(Protocol):
    async def credentials(self, name: str) -> tuple[str, str]: ...


class KubernetesSecretStore:
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.api = "https://kubernetes.default.svc"
        self.token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        self.ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

    async def credentials(self, name: str) -> tuple[str, str]:
        token = self.token_path.read_text().strip()
        url = f"{self.api}/api/v1/namespaces/{self.namespace}/secrets/{name}"
        async with httpx.AsyncClient(verify=self.ca_path, timeout=10) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
        data = response.json().get("data") or {}
        try:
            username = base64.b64decode(data["username"]).decode()
            password = base64.b64decode(data["password"]).decode()
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"BMC Secret {name}에는 username/password가 필요합니다.") from exc
        return username, password


class NetBoxSyncClient:
    def __init__(self, settings: Settings):
        self.base = settings.netbox_url.rstrip("/")
        scheme = "Bearer" if settings.netbox_token.startswith("nbt_") else "Token"
        self.headers = {"Authorization": f"{scheme} {settings.netbox_token}"}
        self.verify = settings.netbox_verify_tls

    async def all(self, path: str, params: dict | None = None) -> list[dict]:
        rows: list[dict] = []
        url: str | None = f"{self.base}/{path.lstrip('/')}"
        async with httpx.AsyncClient(headers=self.headers, verify=self.verify, timeout=30) as client:
            while url:
                response = await client.get(url, params=params)
                response.raise_for_status()
                body = response.json()
                rows.extend(body["results"])
                url, params = body.get("next"), None
        return rows

    async def create(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(headers=self.headers, verify=self.verify, timeout=30) as client:
            response = await client.post(f"{self.base}/{path.lstrip('/')}", json=payload)
            response.raise_for_status()
            return response.json()

    async def patch(self, path: str, object_id: int | str, payload: dict) -> dict:
        async with httpx.AsyncClient(headers=self.headers, verify=self.verify, timeout=30) as client:
            response = await client.patch(f"{self.base}/{path.strip('/')}/{object_id}/", json=payload)
            response.raise_for_status()
            return response.json()


class IronicSyncClient:
    def __init__(self, settings: Settings):
        from openstack import connection

        scope = ({"project_id": settings.openstack_project_id}
                 if settings.openstack_project_id
                 else {"system_scope": settings.openstack_system_scope})
        self.conn = connection.Connection(
            auth_url=settings.openstack_auth_url,
            username=settings.openstack_username,
            password=settings.openstack_password,
            user_domain_name=settings.openstack_user_domain_name,
            **scope,
            region_name=settings.openstack_region,
            interface=settings.openstack_interface,
            identity_api_version="3",
        )

    async def nodes(self) -> dict[str, dict]:
        nodes = await to_thread(lambda: list(self.conn.baremetal.nodes(details=True)))
        return {
            node.id: {
                "id": node.id,
                "name": node.name,
                "driver": node.driver,
                "power_state": node.power_state,
                "provision_state": node.provision_state,
                "maintenance": bool(node.is_maintenance),
                "instance_uuid": node.instance_id,
                "last_error": node.last_error,
                "owner": node.owner,
                "lessee": node.lessee,
                "resource_class": node.resource_class,
                "traits": list(node.traits or []),
            }
            for node in nodes
        }

    async def create_node(self, *, name: str, driver: str, resource_class: str, address: str,
                          username: str, password: str, properties: dict) -> str:
        prefix = "redfish" if driver.startswith("redfish") else "ipmi"
        driver_info = {
            f"{prefix}_address": address,
            f"{prefix}_username": username,
            f"{prefix}_password": password,
        }
        node = await to_thread(
            self.conn.baremetal.create_node,
            name=name,
            driver=driver,
            resource_class=resource_class,
            driver_info=driver_info,
            properties=properties,
        )
        return node.id

    async def assign_lessee(self, node_uuid: str, project_id: str, dcn_project_id: str) -> None:
        node = await to_thread(self.conn.baremetal.get_node, node_uuid)
        if node.owner != dcn_project_id:
            raise RuntimeError("DCN does not own the requested node")
        if node.lessee and node.lessee != project_id:
            raise RuntimeError("node already has another lessee")
        if node.provision_state != "available" or node.is_maintenance or node.last_error:
            raise RuntimeError("node is not safe to lease")
        await to_thread(self.conn.baremetal.update_node, node, lessee=project_id)

    async def prepare_access_fixture(self, node_uuid: str, dcn_project_id: str) -> None:
        node = await to_thread(self.conn.baremetal.get_node, node_uuid)
        if node.is_maintenance:
            raise RuntimeError("fixture node is not safe to prepare")
        if node.provision_state in {"active", "deploy failed"}:
            node = await to_thread(
                self.conn.baremetal.set_node_provision_state, node, "deleted",
                wait=True, timeout=3600,
            )
        if node.provision_state != "available" or node.last_error:
            raise RuntimeError(f"fixture node did not become available: {node.provision_state}")
        await to_thread(
            self.conn.baremetal.update_node, node, owner=dcn_project_id, lessee=None,
        )

    async def return_and_clean(self, node_uuid: str, clean_steps: list[dict]) -> None:
        if not clean_steps:
            raise RuntimeError("manual cleaning steps are required")
        node = await to_thread(self.conn.baremetal.get_node, node_uuid)
        if node.provision_state == "active":
            node = await to_thread(
                self.conn.baremetal.set_node_provision_state, node, "deleted", wait=True, timeout=3600,
            )
        if node.provision_state != "available" or node.last_error:
            raise RuntimeError(f"node did not complete undeploy: {node.provision_state}")
        node = await to_thread(
            self.conn.baremetal.set_node_provision_state, node, "manage", wait=True, timeout=900,
        )
        if node.provision_state != "manageable" or node.last_error:
            raise RuntimeError(f"node did not become manageable for cleaning: {node.provision_state}")
        node = await to_thread(
            self.conn.baremetal.set_node_provision_state, node, "clean",
            clean_steps=clean_steps, wait=True, timeout=3600,
        )
        if node.provision_state != "manageable" or node.last_error:
            raise RuntimeError(f"node did not complete manual cleaning: {node.provision_state}")
        node = await to_thread(
            self.conn.baremetal.set_node_provision_state, node, "provide", wait=True, timeout=900,
        )
        if node.provision_state != "available" or node.last_error:
            raise RuntimeError(f"node did not return to available after cleaning: {node.provision_state}")

    async def clear_lessee(self, node_uuid: str) -> None:
        node = await to_thread(self.conn.baremetal.get_node, node_uuid)
        if node.provision_state != "available" or node.last_error:
            raise RuntimeError("lessee cannot be cleared before successful cleaning")
        await to_thread(self.conn.baremetal.update_node, node, lessee=None)

    async def deploy(self, node_uuid: str, project_id: str, image_id: str,
                     config_drive: dict, dcn_project_id: str,
                     image_metadata: dict | None = None) -> None:
        node = await to_thread(self.conn.baremetal.get_node, node_uuid)
        self._require_leased_node(node, project_id, dcn_project_id)
        if node.provision_state != "available":
            raise RuntimeError("only an available leased node can be deployed")
        if image_metadata:
            image = SimpleNamespace(**image_metadata)
        else:
            image = await to_thread(self.conn.image.get_image, image_id)
            if not image or not image.checksum or not image.disk_format:
                raise RuntimeError("approved image metadata is incomplete")
        instance_info = {
            "image_source": image.id,
            "image_checksum": image.checksum,
            "image_disk_format": image.disk_format,
        }
        node = await to_thread(self.conn.baremetal.update_node, node, instance_info=instance_info)
        await to_thread(
            self.conn.baremetal.set_node_provision_state, node, "active",
            config_drive=config_drive, wait=True, timeout=3600,
        )

    async def set_power(self, node_uuid: str, project_id: str, action: str,
                        dcn_project_id: str) -> None:
        targets = {
            "on": "power on", "off": "power off", "reboot": "rebooting",
            "soft off": "soft power off", "soft reboot": "soft rebooting",
        }
        if action not in targets:
            raise RuntimeError("unsupported power action")
        node = await to_thread(self.conn.baremetal.get_node, node_uuid)
        self._require_leased_node(node, project_id, dcn_project_id)
        if node.provision_state != "active":
            raise RuntimeError("power operations require an active leased node")
        await to_thread(
            self.conn.baremetal.set_node_power_state, node, targets[action], wait=True, timeout=300,
        )

    async def approved_images(self, image_ids: set[str]) -> list[dict]:
        images = []
        for image_id in sorted(image_ids):
            image = await to_thread(self.conn.image.get_image, image_id)
            if image and image.checksum and image.disk_format:
                images.append({"id": image.id, "name": image.name or image.id})
        return images

    @staticmethod
    def _require_leased_node(node, project_id: str, dcn_project_id: str) -> None:
        if node.owner != dcn_project_id or node.lessee != project_id:
            raise RuntimeError("node owner/lessee does not match the approved lease")
        if node.is_maintenance or node.last_error:
            raise RuntimeError("leased node is not safe to operate")

    async def port_addresses(self, node_uuid: str) -> set[str]:
        ports = await to_thread(lambda: list(self.conn.baremetal.ports(node=node_uuid, details=True)))
        return {str(port.address).lower() for port in ports}

    async def create_port(self, node_uuid: str, address: str) -> None:
        await to_thread(self.conn.baremetal.create_port, node_id=node_uuid, address=address, pxe_enabled=True)


@dataclass
class SyncResult:
    netbox_devices: int = 0
    ironic_nodes: int = 0
    created_in_ironic: int = 0
    discovered_in_netbox: int = 0
    status_updates: int = 0
    ports_created: int = 0
    skipped: int = 0


class NetBoxIronicController:
    def __init__(self, settings: Settings, netbox: NetBoxSyncClient, ironic: IronicSyncClient,
                 secrets: SecretStore):
        self.settings, self.netbox, self.ironic, self.secrets = settings, netbox, ironic, secrets

    async def reconcile(self) -> SyncResult:
        result = SyncResult()
        devices = await self.netbox.all("dcim/devices/", {"role": "server", "limit": 500})
        nodes = await self.ironic.nodes()
        result.netbox_devices, result.ironic_nodes = len(devices), len(nodes)
        mapped: dict[str, dict] = {}

        for device in devices:
            custom = device.get("custom_fields") or {}
            node_uuid = custom.get("ironic_node_uuid")
            if node_uuid:
                mapped[node_uuid] = device
                node = nodes.get(node_uuid)
                if node:
                    updated = dict(custom)
                    updated.update({
                        "ironic_power_state": node.get("power_state") or "",
                        "ironic_provision_state": node.get("provision_state") or "",
                        "ironic_maintenance": bool(node.get("maintenance")),
                        "ironic_last_error": node.get("last_error") or "",
                        "baremetal_lessee_project_id": node.get("lessee") or "",
                    })
                    if updated != custom:
                        await self.netbox.patch("dcim/devices", device["id"], {"custom_fields": updated})
                        result.status_updates += 1
                    result.ports_created += await self._sync_ports(device, node_uuid)
                continue

            if not custom.get("ironic_managed"):
                continue
            required = ["bmc_address", "bmc_secret_name", "ironic_driver", "ironic_resource_class"]
            if any(not custom.get(key) for key in required):
                result.skipped += 1
                continue
            interfaces = await self.netbox.all("dcim/interfaces/", {"device_id": device["id"], "limit": 100})
            if not self._mac_addresses(interfaces):
                result.skipped += 1
                continue
            username, password = await self.secrets.credentials(custom["bmc_secret_name"])
            raw_properties = custom.get("ironic_properties") or {}
            try:
                properties = raw_properties if isinstance(raw_properties, dict) else json.loads(raw_properties)
            except json.JSONDecodeError:
                result.skipped += 1
                continue
            node_uuid = await self.ironic.create_node(
                name=device["name"], driver=custom["ironic_driver"],
                resource_class=custom["ironic_resource_class"], address=custom["bmc_address"],
                username=username, password=password, properties=properties,
            )
            updated = dict(custom)
            updated["ironic_node_uuid"] = node_uuid
            await self.netbox.patch("dcim/devices", device["id"], {"custom_fields": updated})
            mapped[node_uuid] = device
            result.created_in_ironic += 1
            result.ports_created += await self._sync_ports(device, node_uuid, interfaces)

        for node_uuid, node in nodes.items():
            if node_uuid in mapped:
                continue
            await self._create_discovered_device(node)
            result.discovered_in_netbox += 1
        return result

    @staticmethod
    def _mac_addresses(interfaces: list[dict]) -> set[str]:
        addresses = set()
        for interface in interfaces:
            primary = interface.get("primary_mac_address") or {}
            address = primary.get("mac_address") or primary.get("address")
            if address:
                addresses.add(str(address).lower())
        return addresses

    async def _sync_ports(self, device: dict, node_uuid: str,
                          interfaces: list[dict] | None = None) -> int:
        if interfaces is None:
            interfaces = await self.netbox.all("dcim/interfaces/", {"device_id": device["id"], "limit": 100})
        desired = self._mac_addresses(interfaces)
        existing = await self.ironic.port_addresses(node_uuid)
        created = 0
        for address in sorted(desired - existing):
            await self.ironic.create_port(node_uuid, address)
            created += 1
        return created

    async def _create_discovered_device(self, node: dict) -> None:
        types = await self.netbox.all("dcim/device-types/", {"slug": self.settings.sync_discovered_device_type_slug})
        sites = await self.netbox.all("dcim/sites/", {"slug": self.settings.sync_site_slug})
        roles = await self.netbox.all("dcim/device-roles/", {"slug": "server"})
        if not types or not sites or not roles:
            raise RuntimeError("discovered Device Type, Site 또는 Server role이 없습니다.")
        base_name = node.get("name") or f"ironic-{node['id'][:12]}"
        existing = await self.netbox.all("dcim/devices/", {"name": base_name})
        name = base_name if not existing else f"{base_name}-{node['id'][:8]}"
        await self.netbox.create("dcim/devices/", {
            "name": name,
            "device_type": types[0]["id"],
            "role": roles[0]["id"],
            "site": sites[0]["id"],
            "status": "planned",
            "comments": "Ironic에서 자동 발견됨. Rack/U 위치와 실제 모델을 지정하십시오.",
            "custom_fields": {
                "ironic_node_uuid": node["id"],
                "ironic_managed": True,
                "ironic_driver": node.get("driver") or "",
                "ironic_power_state": node.get("power_state") or "",
                "ironic_provision_state": node.get("provision_state") or "",
                "ironic_maintenance": bool(node.get("maintenance")),
                "ironic_last_error": node.get("last_error") or "",
            },
        })
