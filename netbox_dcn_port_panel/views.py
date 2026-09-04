from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View
from django.contrib.auth.mixins import PermissionRequiredMixin

from dcim.models import Device, Interface
from netbox.views import generic

from .panel import natural_port_key, normalize_observation
from importlib.resources import files


def _settings():
    value = settings.PLUGINS_CONFIG.get("netbox_dcn_port_panel", {})
    return int(value.get("refresh_seconds", 30)), int(value.get("stale_seconds", 180))


def _port(device, interface, stale_seconds):
    observation = normalize_observation(cache.get(f"dcn-port-status:{device.pk}:{interface.pk}"), stale_seconds=stale_seconds)
    endpoints = list(interface.connected_endpoints or [])
    peer = endpoints[0] if endpoints else None
    return {
        "id": interface.pk,
        "name": interface.name,
        "url": interface.get_absolute_url(),
        "enabled": interface.enabled,
        "type": interface.get_type_display(),
        "mode": interface.get_mode_display() if interface.mode else "",
        "peer": f"{peer.device.name}:{peer.name}" if isinstance(peer, Interface) else None,
        "peer_url": peer.get_absolute_url() if peer else None,
        "cabled": bool(interface.cable_id),
        **observation,
    }


def _ports(device):
    _, stale_seconds = _settings()
    interfaces = device.interfaces.prefetch_related("cable", "tagged_vlans").all()
    physical = [i for i in interfaces if i.type != "virtual"]
    return [_port(device, i, stale_seconds) for i in sorted(physical, key=lambda x: natural_port_key(x.name))]


class PortPanelView(PermissionRequiredMixin, generic.ObjectView):
    queryset = Device.objects.all()
    template_name = "netbox_dcn_port_panel/port_panel.html"
    permission_required = "dcim.view_interface"

    def get(self, request, pk):
        device = get_object_or_404(self.queryset, pk=pk)
        refresh_seconds, _ = _settings()
        return render(request, self.template_name, {"object": device, "tab": self.tab,
                      "ports": _ports(device), "refresh_seconds": refresh_seconds})


class PortStatusView(PermissionRequiredMixin, View):
    permission_required = "dcim.view_interface"

    def get(self, request, pk):
        device = get_object_or_404(Device, pk=pk)
        return JsonResponse({"device": device.name, "ports": _ports(device)})


class SwitchListView(PermissionRequiredMixin, View):
    permission_required = "dcim.view_device"

    def get(self, request):
        devices = Device.objects.filter(role__slug="network-switch").select_related("device_type", "rack")
        return render(request, "netbox_dcn_port_panel/switches.html", {"devices": devices})


class AssetView(View):
    assets = {"port-panel.css": "text/css", "port-panel.js": "application/javascript"}

    def get(self, request, asset):
        if asset not in self.assets:
            return HttpResponse(status=404)
        body = files("netbox_dcn_port_panel").joinpath("static", "netbox_dcn_port_panel", asset).read_text()
        return HttpResponse(body, content_type=self.assets[asset])
