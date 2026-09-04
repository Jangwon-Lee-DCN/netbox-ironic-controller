# DCN Bare Metal Access Service

NetBox를 물리 자산 기준 정보원으로 사용해 OpenStack Ironic 인벤토리를 동기화하고,
Keystone 프로젝트별 베어메탈 신청·승인·임대·반납 수명주기를 제공하는 작은
Kubernetes 서비스입니다. 사용자 화면은 별도 Horizon plugin이 소유하며 이
서비스는 인증된 JSON API만 제공합니다.

## 접근 모델

- 물리 자산의 Ironic `owner`는 DCN 프로젝트에 유지합니다.
- 승인된 프로젝트는 기간 제한 `lessee`가 됩니다.
- 전체 Ironic 작업과 신청 승인은 `dcn` 프로젝트의 `baremetal_admin`만 수행합니다.
- DCN 도메인의 현재 프로젝트 `member`/`admin` 또는 명시적
  `baremetal_requester`/`baremetal_operator`만 자신의 신청과 임대 노드를 볼 수
  있습니다. 토큰의 사용자·프로젝트 도메인이 모두 설정된 DCN 도메인 UUID와
  일치해야 하며 도메인 상속 역할은 사용하지 않습니다.
- 임대 노드의 배포·전원·반납은 현재 프로젝트의 `admin` 또는 명시적
  `baremetal_operator`만 수행합니다. 승인 전, 거부, 취소, 반납 완료 요청은
  조회만 가능하며 노드 작업 API가 거부합니다.
- 노드 reservation은 DB 고유 제약과 request version 비교로 중복 승인을 막습니다.
- 반납과 만료는 Ironic undeploy/cleaning 성공 후에만 lessee와 reservation을 해제합니다.

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

## DCN switch port panel

`netbox_dcn_port_panel` is an optional NetBox 4.6 plugin which adds a physical
port-panel tab to Device pages and a switch-panel index. It renders NetBox
Interface/Cable state immediately and refreshes operational observations every
30 seconds. Missing or stale observations fail closed to `UNKNOWN`; configured
`enabled` or Cable presence is never presented as live carrier state.

The production values deliberately leave the plugin disabled until a custom
NetBox image built from `Dockerfile.netbox` passes authenticated browser
acceptance and its immutable digest is recorded by the production change
contract. Switch credentials are not accepted by or stored in the plugin.

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
