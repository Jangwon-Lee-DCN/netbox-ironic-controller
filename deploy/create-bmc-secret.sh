#!/usr/bin/env bash
set -euo pipefail
umask 077

read -r -p "NetBox Device의 BMC Secret Name: " secret_name
if [[ ! "$secret_name" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "Kubernetes Secret 이름 형식이 아닙니다." >&2
  exit 1
fi
read -r -p "BMC username: " username
read -r -s -p "BMC password: " password
printf '\n'
read -r -s -p "BMC password 확인: " password_confirm
printf '\n'
if [[ -z "$username" || "$password" != "$password_confirm" ]]; then
  echo "username이 비어 있거나 비밀번호가 서로 다릅니다." >&2
  exit 1
fi

kubectl -n netbox-ironic-controller-bmc create secret generic "$secret_name" \
  --from-literal=username="$username" \
  --from-literal=password="$password" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
unset username password password_confirm
echo "BMC Secret '$secret_name'이 생성/갱신되었습니다."
