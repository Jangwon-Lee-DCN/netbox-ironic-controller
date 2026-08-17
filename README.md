# NetBox-Ironic Controller

NetBox를 기준 정보원으로 사용해 OpenStack Ironic의 베어메탈 인벤토리를 동기화하는 Kubernetes 컨트롤러입니다. 웹 대시보드나 사용자 로그인 화면은 제공하지 않습니다.

## 동기화 방향

- NetBox의 `ironic_managed` 서버를 Ironic Node와 Port로 생성
- Ironic에만 있는 Node를 NetBox의 planned Device로 등록
- Ironic 전원·프로비저닝·maintenance·오류 상태를 NetBox custom field에 반영
- BMC 자격 증명은 `netbox-ironic-controller-bmc` namespace의 개별 Secret에서 조회

## 운영 배포

`deploy/`의 매니페스트는 예제 값(`example.com`, `example-region`)을 사용합니다.
배포 전에 이미지 레지스트리, NetBox URL, OpenStack 리전과 Secret 이름을 환경에
맞게 변경하십시오. 실제 자격 증명은 저장소에 넣지 않고 Kubernetes Secret으로
주입합니다.

배포 매니페스트는 `deploy/controller.yaml`입니다. 컨트롤러 자체의 `/healthz`, `/status`, `/reconcile` endpoint는 ClusterIP Service나 외부 Route에 연결하지 않았으며 Pod probe와 운영 점검 용도로만 사용합니다.

NetBox 자체의 예제 설치와 Secret 동기화는
[`deploy/netbox/README.md`](deploy/netbox/README.md)에 있습니다. 이 문서는
컨트롤러 계약의 일부가 아니라 선택적 개발/검증 환경입니다. 운영 인벤토리와
Ironic 배치 결정은 `openstack-production-datacenter`가 권위 원본입니다.

## 개발 및 테스트

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

이미지 빌드:

```bash
deploy/build-context.sh
kubectl delete job netbox-ironic-controller-image-build -n netbox-ironic-controller --ignore-not-found
kubectl apply -f deploy/kaniko-job.yaml
```

## 보안

- BMC, NetBox 및 OpenStack 자격 증명은 Git에 저장하지 않습니다.
- `.env.example`은 변수 이름만 제공하며 실제 값은 `.env` 또는 Secret에 둡니다.
- 공개 이슈에 로그를 첨부하기 전에 BMC 주소와 장비 식별자를 제거하십시오.

## License

Apache License 2.0. 자세한 내용은 `LICENSE`를 참조하십시오.
