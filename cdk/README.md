# eda-aws CDK

EDA on AWS 프로젝트의 CDK Python 앱. 루트 `setup.sh`가 자동으로 venv 생성 → deps 설치
→ bootstrap → deploy까지 수행하므로 보통 이 디렉터리를 직접 다룰 일은 없지만,
스택을 개별적으로 관리하거나 context를 바꿀 때 이 문서를 참조합니다.

---

## 스택 구성

`app.py`에서 아래 스택을 조건부로 합성합니다. 스택 이름은 `eda:stack_prefix`
(기본 `Eda`)에 따라 붙습니다.

| 스택 | 파일 | 역할 | 조건 |
|---|---|---|---|
| `{prefix}Base` | `cdk/base_stack.py` | VPC import, Security Groups (cluster/FSx/ONTAP + endpoint SG), EC2 KeyPair, CloudTrail + KMS + S3, **VPC Endpoints** (logs/cloudformation/ec2 + s3/dynamodb + 필요 시 elb/asg) | 항상 |
| `{prefix}Storage` | `cdk/storage_stack.py` | FSx OpenZFS / FSx NetApp ONTAP + 자식 볼륨 (tools/work/scratch) + CloudWatch Alarm | `eda:enable_openzfs` 또는 `eda:enable_ontap` |
| `{prefix}LicenseServer` | `cdk/license_server_stack.py` | EDA 라이선스 서버용 EC2 + static ENI (MAC 영속성) | `eda:enable_license_server=true` |

`slurm_db_stack.py`(Slurm accounting RDS)는 현재 `app.py`에서 합성되지 않는 옵션
스택입니다.

**변경 이력 (v3.0):**
- `EdaNetwork` + `EdaVpcEndpoints` 두 스택을 `{prefix}Base` 하나로 통합
- 스택 접두사를 `eda:stack_prefix` context로 변경 가능 (기본 `Eda`)

---

## Context 플래그

`cdk -c key=value` 또는 `cdk.json`으로 전달합니다. setup.sh는 `config/*.env`
내용을 그대로 `-c`로 넘깁니다.

| Key | 기본 | 타입 | 설명 |
|---|---|---|---|
| `eda:stack_prefix` | `Eda` | str | 스택 접두사 — `{prefix}Base` / `{prefix}Storage` / `{prefix}LicenseServer` |
| `eda:base_stack_name` | `{prefix}Base` | str | Base 스택 이름 개별 override |
| `eda:storage_stack_name` | `{prefix}Storage` | str | Storage 스택 이름 개별 override |
| `eda:license_stack_name` | `{prefix}LicenseServer` | str | License 스택 이름 개별 override |
| `eda:vpc_id` | — (필수) | str | 기존 VPC ID (예: `vpc-xxxx`) |
| `eda:subnet_id` | — (필수) | str | Private subnet ID |
| `eda:enable_vpc_endpoints` | `true` | bool | Base 스택 내부의 VPC endpoint 생성 |
| `eda:enable_login_node` | `true` | bool | ELB/ASG endpoint 포함 여부 |
| `eda:enable_openzfs` | `true` | bool | FSx OpenZFS 생성 |
| `eda:openzfs_size_gib` | `10240` | int | OpenZFS 용량 (64 ~ 524288) |
| `eda:openzfs_throughput` | `2560` | int | MBps: 160·320·640·1280·2560·3840·5120·7680·10240 |
| `eda:enable_ontap` | `false` | bool | FSx ONTAP 생성 |
| `eda:ontap_size_gib` | `10240` | int | ONTAP 용량 (1024 ~ 1048576) |
| `eda:ontap_tput_per_ha` | `3072` | int | MBps/HA: 1536·3072·6144 |
| `eda:ontap_ha_pairs` | `1` | int | HA pair 개수 (1–12) |
| `eda:enable_license_server` | `true` | bool | 라이선스 서버 EC2 생성 |
| `eda:license_instance_type` | `m7i.large` | str | 라이센스 서버 인스턴스 타입 |

---

## 수동 사용법

```bash
# venv 준비 (setup.sh가 이미 했다면 재활성화만)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# synth / diff / deploy
cdk synth  -c eda:vpc_id=vpc-xxx -c eda:subnet_id=subnet-xxx
cdk diff   -c eda:vpc_id=vpc-xxx -c eda:subnet_id=subnet-xxx
cdk deploy --all --require-approval never \
  -c eda:vpc_id=vpc-xxx -c eda:subnet_id=subnet-xxx \
  -c eda:stack_prefix=MyEda \
  -c eda:enable_openzfs=1 -c eda:openzfs_size_gib=320

# 특정 스택만
cdk deploy EdaStorage -c eda:vpc_id=... -c eda:subnet_id=...

# 삭제 (FSx / CloudTrail S3 / KMS는 RemovalPolicy.RETAIN — 수동 삭제 필요)
cdk destroy --all -c eda:vpc_id=... -c eda:subnet_id=...
```

Outputs는 `cdk deploy --outputs-file outputs.json` 으로 받고, setup.sh는 이 값을
`pcluster-config-template.yaml`의 중립 placeholder(`${BASE.*}`, `${STORAGE.*}`)에
주입해 `pcluster-config.yaml`을 생성합니다.

---

## 구현 노트

- **EC2 description non-ASCII 금지**: `AWS::EC2::NetworkInterface`의 `Description`
  필드는 ASCII만 허용됩니다. em-dash(`—`) 등을 쓰지 않습니다.
- **FSx OpenZFS 자식 볼륨 quota**: 각 자식 볼륨의 `storage_capacity_quota_gib`는
  부모 FS 용량 이하여야 하고, 전체 reservation 합은 부모 용량 이하여야 합니다.
  `storage_stack.py`는 부모 용량에 비례해 tools≈10% / work≈40% / scratch≈40%로
  스케일링합니다.
- **VPC endpoint 중복 방지**: `base_stack.py`의 `_create_vpc_endpoints_if_enabled`는
  synth 시점에 boto3로 기존 endpoint를 조회해 스킵하지만, `Project=eda-cluster`
  태그가 붙은 것은 우리 스택이 관리하는 것으로 간주해 재생성 대상에 남깁니다.
  태그를 제외하지 않으면 재배포 시 템플릿에서 빠져 삭제되고 HeadNode 부트스트랩이
  "Unknown error retrieving HeadNodeLaunchTemplate"로 실패합니다.
- **RemovalPolicy.RETAIN**: FSx 파일시스템/볼륨, CloudTrail 버킷·KMS는 데이터 보존을
  위해 retain입니다. 스택 삭제 후 필요시 수동으로 지워야 합니다.
- **Stack prefix 변경**: `eda:stack_prefix`를 바꾸면 CloudFormation 상에서 **새 스택**
  으로 인식되므로, 기존 스택(다른 prefix)은 별도로 삭제해야 합니다. FSx나 KeyPair 같이
  계정 내 고유해야 하는 리소스는 중복 충돌 가능 — 환경별 prefix를 쓸 때는
  `eda:key_pair_name` 등도 함께 다르게 지정하세요.
