from netbox_ironic_controller.netbox_fields import access_custom_fields


def test_offer_fields_are_fail_closed_and_device_scoped():
    fields = {field["name"]: field for field in access_custom_fields()}
    offer = fields["baremetal_offer_enabled"]
    assert offer["type"] == "boolean"
    assert offer["default"] is False
    assert offer["object_types"] == ["dcim.device"]
    assert fields["baremetal_max_lease_days"]["validation_minimum"] == 1


def test_lessee_field_is_documented_as_status_mirror():
    field = {item["name"]: item for item in access_custom_fields()}["baremetal_lessee_project_id"]
    assert "Status mirror" in field["description"]
