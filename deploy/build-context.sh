#!/usr/bin/env bash
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

kubectl create namespace netbox-ironic-controller --dry-run=client -o yaml | kubectl apply -f -
kubectl -n netbox-ironic-controller create configmap netbox-ironic-controller-build-context \
  --from-file=Dockerfile="$root/Dockerfile" \
  --from-file=pyproject.toml="$root/pyproject.toml" \
  --from-file=init.py="$root/netbox_ironic_controller/__init__.py" \
  --from-file=config.py="$root/netbox_ironic_controller/config.py" \
  --from-file=sync.py="$root/netbox_ironic_controller/sync.py" \
  --from-file=sync_api.py="$root/netbox_ironic_controller/sync_api.py" \
  --dry-run=client -o yaml | kubectl apply -f -
