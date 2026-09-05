try:
    from netbox.plugins import PluginConfig
except ModuleNotFoundError as exc:
    # Permit framework-independent helper tests outside a NetBox image.
    if exc.name != "netbox":
        raise
    PluginConfig = object


class NetBoxDCNPortPanelConfig(PluginConfig):
    name = "netbox_dcn_port_panel"
    verbose_name = "DCN Port Panel"
    description = "Physical switch faceplate and server interface connectivity overlay"
    version = "0.2.0"
    base_url = "dcn-port-panel"
    min_version = "4.6.0"
    max_version = "4.6.99"
    default_settings = {"refresh_seconds": 30, "stale_seconds": 180}

config = NetBoxDCNPortPanelConfig
