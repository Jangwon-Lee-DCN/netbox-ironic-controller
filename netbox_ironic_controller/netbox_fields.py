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


def bootstrap() -> None:
    """Create or converge the NetBox fields required by the access workflow."""
    import os

    import httpx

    base = os.environ["RACKD_NETBOX_URL"].rstrip("/")
    token = os.environ["RACKD_NETBOX_TOKEN"]
    scheme = "Bearer" if token.startswith("nbt_") else "Token"
    verify = os.environ.get("RACKD_NETBOX_VERIFY_TLS", "true").lower() == "true"
    headers = {"Authorization": f"{scheme} {token}"}
    with httpx.Client(headers=headers, verify=verify, timeout=30) as client:
        for payload in access_custom_fields():
            response = client.get(
                f"{base}/extras/custom-fields/", params={"name": payload["name"]},
            )
            response.raise_for_status()
            rows = response.json()["results"]
            if rows:
                result = client.patch(
                    f"{base}/extras/custom-fields/{rows[0]['id']}/", json=payload,
                )
            else:
                result = client.post(f"{base}/extras/custom-fields/", json=payload)
            result.raise_for_status()


if __name__ == "__main__":
    bootstrap()
