from dcim.models import Device
from utilities.views import ViewTab, register_model_view

from .views import PortPanelView


@register_model_view(Device, name="port-panel", path="port-panel")
class DevicePortPanelView(PortPanelView):
    tab = ViewTab(label="Interface Panel", permission="dcim.view_interface")


template_extensions = []
