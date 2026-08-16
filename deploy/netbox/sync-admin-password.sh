#!/usr/bin/env bash
set -euo pipefail
umask 077

namespace=netbox
release=netbox
chart=oci://ghcr.io/netbox-community/netbox-chart/netbox
chart_version=8.3.37

read -r -s -p "NetBox admin의 현재 비밀번호: " password
printf '\n'
read -r -s -p "비밀번호 확인: " password_confirm
printf '\n'

if [[ "$password" != "$password_confirm" ]]; then
  echo "입력한 비밀번호가 서로 다릅니다." >&2
  exit 1
fi
if (( ${#password} < 12 )); then
  echo "비밀번호는 12자 이상이어야 합니다." >&2
  exit 1
fi

password_values=$(mktemp /tmp/netbox-admin-password.XXXXXX.json)
cleanup() {
  case "$password_values" in
    /tmp/netbox-admin-password.*.json) shred -u -- "$password_values" ;;
  esac
}
trap cleanup EXIT

jq -n --arg password "$password" '{superuser:{password:$password}}' > "$password_values"

# Keep the Helm release and chart-managed netbox-superuser Secret consistent.
helm upgrade "$release" "$chart" \
  --version "$chart_version" \
  --namespace "$namespace" \
  --reuse-values \
  -f "$password_values" \
  --wait \
  --timeout 10m >/dev/null

encoded_password=$(printf '%s' "$password" | base64 -w0)
bootstrap_patch=$(jq -nc --arg value "$encoded_password" \
  '{data:{password:$value,"initial-password":$value}}')
superuser_patch=$(jq -nc --arg value "$encoded_password" \
  '{data:{password:$value}}')
kubectl -n "$namespace" patch secret netbox-bootstrap-credentials \
  --type merge -p "$bootstrap_patch" >/dev/null
kubectl -n "$namespace" patch secret netbox-superuser \
  --type merge -p "$superuser_patch" >/dev/null

stored_bootstrap=$(kubectl -n "$namespace" get secret netbox-bootstrap-credentials \
  -o jsonpath='{.data.password}' | base64 -d)
stored_chart=$(kubectl -n "$namespace" get secret netbox-superuser \
  -o jsonpath='{.data.password}' | base64 -d)

if [[ "$stored_bootstrap" != "$password" || "$stored_chart" != "$password" ]]; then
  echo "Secret 동기화 검증에 실패했습니다." >&2
  exit 1
fi

unset password password_confirm stored_bootstrap stored_chart encoded_password bootstrap_patch superuser_patch
echo "NetBox admin 비밀번호가 Helm 릴리스와 관련 Secret에 동기화되었습니다."
