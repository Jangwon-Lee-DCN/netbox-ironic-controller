from django.urls import path

from . import views

app_name = "netbox_dcn_port_panel"

urlpatterns = [
    path("", views.SwitchListView.as_view(), name="switches"),
    path("assets/port-panel.css", views.AssetView.as_view(), {"asset": "port-panel.css"}, name="panel-css"),
    path("assets/port-panel.js", views.AssetView.as_view(), {"asset": "port-panel.js"}, name="panel-js"),
    path("devices/<int:pk>/status/", views.PortStatusView.as_view(), name="port-status"),
]
