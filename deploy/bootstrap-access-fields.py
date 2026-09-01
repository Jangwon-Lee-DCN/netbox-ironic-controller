#!/usr/bin/env python3
import os

import httpx

from netbox_ironic_controller.netbox_fields import access_custom_fields


def main() -> None:
    base = os.environ["RACKD_NETBOX_URL"].rstrip("/")
    token = os.environ["RACKD_NETBOX_TOKEN"]
    scheme = "Bearer" if token.startswith("nbt_") else "Token"
    verify = os.environ.get("RACKD_NETBOX_VERIFY_TLS", "true").lower() == "true"
    with httpx.Client(headers={"Authorization": f"{scheme} {token}"}, verify=verify, timeout=30) as client:
        for payload in access_custom_fields():
            response = client.get(f"{base}/extras/custom-fields/", params={"name": payload["name"]})
            response.raise_for_status()
            rows = response.json()["results"]
            if rows:
                result = client.patch(f"{base}/extras/custom-fields/{rows[0]['id']}/", json=payload)
            else:
                result = client.post(f"{base}/extras/custom-fields/", json=payload)
            result.raise_for_status()


if __name__ == "__main__":
    main()
