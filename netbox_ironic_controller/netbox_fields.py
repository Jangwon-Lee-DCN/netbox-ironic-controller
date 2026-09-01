DEVICE_OBJECT_TYPE = "dcim.device"


def access_custom_fields() -> list[dict]:
    return [
        {
            "name": "baremetal_offer_enabled",
            "label": "Bare Metal Offer Enabled",
            "type": "boolean",
            "object_types": [DEVICE_OBJECT_TYPE],
            "required": False,
            "default": False,
            "description": "DCN operator explicitly allows this node to enter the request pool.",
        },
        {
            "name": "baremetal_profile",
            "label": "Bare Metal Hardware Profile",
            "type": "text",
            "object_types": [DEVICE_OBJECT_TYPE],
            "required": False,
            "description": "Sanitized requester-facing profile; never include BMC or credential data.",
        },
        {
            "name": "baremetal_max_lease_days",
            "label": "Bare Metal Maximum Lease Days",
            "type": "integer",
            "object_types": [DEVICE_OBJECT_TYPE],
            "required": False,
            "default": 30,
            "validation_minimum": 1,
            "validation_maximum": 365,
        },
        {
            "name": "baremetal_lessee_project_id",
            "label": "Bare Metal Lessee Project ID",
            "type": "text",
            "object_types": [DEVICE_OBJECT_TYPE],
            "required": False,
            "description": "Status mirror written by the access service; not an allocation input.",
        },
    ]
