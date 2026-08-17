# NetBox-Ironic controller Agent contract

Read `/home/ubuntu/AGENTS.md` first. This repository owns controller code and
its NetBox/Ironic synchronization contract. Production hardware inventory,
Ironic placement, credentials, and deployment approval belong to
`openstack-production-datacenter`.

Use a central change contract for cross-repository work. Never commit BMC,
NetBox, or OpenStack credentials. Run the full pytest suite before completion.
