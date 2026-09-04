import json
import sys

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from dcim.models import Device, Interface

from ...panel import validate_observation


class Command(BaseCommand):
    help = "Ingest read-only switch observations from JSON stdin into the transient cache"

    def handle(self, *args, **options):
        try:
            payload = json.load(sys.stdin)
        except (ValueError, TypeError) as exc:
            raise CommandError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise CommandError("payload must be a list")
        accepted = 0
        for raw in payload:
            try:
                item = validate_observation(raw)
                device = Device.objects.get(name=item["device"], role__slug="network-switch")
                interface = Interface.objects.get(device=device, name=item["interface"])
            except (ValueError, Device.DoesNotExist, Interface.DoesNotExist) as exc:
                raise CommandError(str(exc)) from exc
            observation = {key: value for key, value in item.items() if key not in {"device", "interface"}}
            cache.set(f"dcn-port-status:{device.pk}:{interface.pk}", observation, timeout=None)
            accepted += 1
        self.stdout.write(json.dumps({"accepted": accepted}))
