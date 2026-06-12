[English](./README.md) | [한국어](./README.ko.md)

# EDA on AWS

AWS 위에서 EDA(simulation/regression) 환경을 ParallelCluster + FSx OpenZFS로
구성하는 프로젝트입니다. (기본 리전: `ap-northeast-2` — `config/default.env`에서 변경 가능)

---

## 1. 개요

- **CDK (Python 3.12, aws-cdk-lib 2.x)**: VPC import, 보안그룹, FSx OpenZFS/ONTAP,
  EDA 라이센스 서버, VPC endpoints, CloudTrail 등을 배포
- **ParallelCluster 3.15.x**: CDK가 만든 리소스 위에 Slurm head node + compute fleet 배포
- **VPC**: 기존 VPC/Private subnet을 재사용 (사이트간 VPN 환경 전제)
- **상세 설계 문서**: [`architecture_guide.md`](architecture_guide.ko.md) /
  [`parallel_cluster_configuration.md`](parallel_cluster_configuration.ko.md)

### 배포되는 CloudFormation 스택

스택 이름은 접두사(prefix)에 따라 붙습니다. 기본 `STACK_PREFIX=Eda`.

| 스택 | 내용 |
|---|---|
| `{prefix}Base` | Security Groups (cluster/FSx/ONTAP + VPC endpoint SG), EC2 KeyPair, CloudTrail + KMS + S3, **VPC Endpoints** (logs/cloudformation/ec2 + s3/dynamodb + 필요 시 elasticloadbalancing/autoscaling) |
| `{prefix}Storage` | FSx OpenZFS (+ `fsxz_tools`, `fsxz_work`, `fsxz_scratch` 볼륨) 또는 FSx ONTAP |
| `{prefix}LicenseServer` | EDA 라이센스 서버용 EC2 + static ENI (MAC 영속성) |
| `hpc-cluster` | ParallelCluster (Slurm) 스택 (pcluster CLI가 생성) |

`STACK_PREFIX`를 다르게 설정하면 같은 계정에 여러 환경 (`EdaDev`, `EdaProd` 등)을
공존시킬 수 있습니다.

---

## 2. 사전 준비

### 2.1 AWS 자격증명

```bash
# 자격증명 설정
aws configure       # 또는: aws sso login (IAM Identity Center)

# 계정/리전 확인
aws sts get-caller-identity
aws configure get region   # ap-northeast-2 이어야 함

# 리전이 다르면 설정
aws configure set region ap-northeast-2
```

### 2.2 필수 도구 설치

아래 도구들은 `setup.sh` 실행 **전** 설치되어 있어야 합니다.
CDK CLI와 pcluster CLI는 `setup.sh`가 자동 설치합니다.

**AWS CLI v2**

```bash
# macOS
brew install awscli

# Linux (x86_64)
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install

# Windows — https://awscli.amazonaws.com/AWSCLIV2.msi

aws --version
```

**python3** (3.9 이상)

```bash
# macOS
brew install python@3.12

# Linux — Debian/Ubuntu
sudo apt-get install -y python3 python3-pip python3-venv

# Linux — RHEL/Amazon Linux
sudo yum install -y python3

python3 --version
```

**Node.js + npm** (CDK CLI에 필요, CDK CLI 자체는 setup.sh가 자동 설치)

```bash
# macOS / Linux — https://nodejs.org/ 에서 LTS 설치 또는 nvm 사용
node --version
npm --version
```

**jq**

```bash
# macOS
brew install jq

# Linux — Debian/Ubuntu
sudo apt-get install -y jq

# Linux — RHEL/Amazon Linux
sudo yum install -y jq
```

**SSM Session Manager Plugin** — `ENABLE_SSM=1` 사용 시에만 필요

```bash
# macOS
brew install --cask session-manager-plugin

# Linux — Debian/Ubuntu (x86_64)
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
  -o session-manager-plugin.deb
sudo dpkg -i session-manager-plugin.deb

# Linux — RHEL/Amazon Linux (x86_64)
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/64bit/session-manager-plugin.rpm" \
  -o session-manager-plugin.rpm
sudo yum install -y session-manager-plugin.rpm
```

### 2.3 VPC / Subnet

기존 VPC와 프라이빗 서브넷 ID를 `config/default.env`에 설정하거나, 환경변수로 전달합니다:

```bash
VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh
```

프라이빗 서브넷에 `0.0.0.0/0` 라우트가 없어도 `ENABLE_VPC_ENDPOINTS=1`(기본값)이면
`setup.sh`가 필요한 VPC 엔드포인트를 자동으로 생성합니다.

---

## 3. 배포

```bash
# 기본 설정(config/default.env) 사용
./setup.sh

# 다른 설정 파일 사용
CONFIG=config/prod.env ./setup.sh

# 일부 값만 override
VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh
```

setup.sh 단계:

1. Pre-validation (AWS 자격증명, 필수 도구, subnet 연결성)
2. CDK Python deps 설치
3. pcluster CLI 설치
4. CDK bootstrap + 스택 배포 (`{prefix}Base` → storage / license 병렬)
5. `pcluster-config.yaml` 자동 생성 + SSH key 다운로드 (`~/.ssh/*.pem`)
6. `pcluster create-cluster` 실행 + 완료까지 모니터링 (10–15분)

### 주요 config 플래그 (`config/default.env`)

| 변수 | 기본 | 의미 |
|---|---|---|
| `REGION` | `ap-northeast-2` | 배포 리전 |
| `STACK_PREFIX` | `Eda` | CDK 스택 접두사. 같은 계정에 여러 환경 두려면 다르게 설정 |
| `VPC_ID` / `SUBNET_ID` | (필수) | 기존 VPC/Private subnet |
| `ENABLE_OPENZFS` / `OPENZFS_SIZE_GIB` / `OPENZFS_THROUGHPUT` | `1` / `320` / `1280` | FSx OpenZFS |
| `ENABLE_ONTAP` / `ONTAP_SIZE_GIB` / `ONTAP_TPUT_PER_HA` / `ONTAP_HA_PAIRS` | `0` / `10240` / `3072` / `1` | FSx NetApp ONTAP |
| `ENABLE_LICENSE_SERVER` / `LICENSE_INSTANCE_TYPE` | `1` / `m7i.large` | EDA 라이센스 서버 |
| `ENABLE_LOGIN_NODE` | `1` | 1=ParallelCluster LoginNodes (권장) |
| `ENABLE_VPC_ENDPOINTS` | `1` | 필수 endpoint 자동 생성 |
| `ENABLE_SSM` | `0` | Session Manager 접속 허용 |
| `SKIP_CDK` / `SKIP_CLUSTER` | `0` | 단계 건너뛰기 |

FSx OpenZFS 자식 볼륨(tools/work/scratch)의 quota/reservation은 부모 용량에 비례해
자동 스케일링됩니다(부모 용량보다 큰 quota 지정 불가 제약을 회피).

---

## 4. 접속

### Login Node 

사용자 일상 작업은 Login Node에서 합니다. ALB DNS로 접속하면 풀에 속한 노드로
자동 분산됩니다.

```bash
# Login Node ALB DNS 확인
pcluster describe-cluster --cluster-name <CLUSTER_NAME> --region ap-northeast-2 \
  | jq -r '.loginNodes[0].address'

# 접속 (Head Node와 동일한 pem 키 사용)
ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem ec2-user@<LOGIN_NODE_ALB_DNS>
```

**Login Node의 KeyPair 작동 방식 (pcluster 3.15+):**
- `LoginNodes.Pools[].Ssh.KeyName` 파라미터는 pcluster 3.15부터 제거됐습니다.
- Login Node EC2 자체에는 KeyPair가 붙지 않지만, `/home`이 Head Node에서 NFS로
  마운트되어 **Head Node의 `~ec2-user/.ssh/authorized_keys`가 그대로 공유**됩니다.
- 결과적으로 Head Node에 등록된 pem이 Login Node에서도 동일하게 유효합니다.

### Head Node 

```bash
# 직접 ssh
ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem ec2-user@<HEAD_NODE_IP>

# 또는 pcluster CLI (키 명시 필수, 기본 id_rsa가 맞지 않으면 거부됨)
pcluster ssh --cluster-name <CLUSTER_NAME> --region ap-northeast-2 \
  -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem
```

Head Node는 Slurm 컨트롤러가 도는 관리 노드이므로 일상 작업은 Login Node에서
하는 것을 권장합니다.

### 라이센스 서버

```bash
ssh -i ~/.ssh/eda-license-key-<ACCOUNT>.pem ec2-user@<LICENSE_IP>

# 클러스터 쪽에서 사용할 환경변수
export LM_LICENSE_FILE=27000@<LICENSE_IP>
```

License Host ID는 `pcluster-config.yaml` 생성 시 setup.sh가 출력하는
License Server MAC address를 사용합니다.

**SSH 22 포트 정책**: License 서버 SG는 Head Node와 동일하게 `0.0.0.0/0`에서
22번을 허용합니다. Private subnet이므로 인터넷에서 도달 불가, VPN 경유
사내망에서만 접속 가능합니다.


### SSH 키 관리 주의사항

- `setup.sh`는 **매번 SSM Parameter Store에서 pem을 재다운로드**합니다 (무조건 덮어쓰기).
  이는 클러스터 재생성 시 KeyPair가 새로 만들어져 로컬 pem이 stale해지는 것을 방지합니다.
- 만약 수동으로 pem 관리 중이고 `Permission denied` 에러가 나면, 로컬 pem의 fingerprint와
  현재 AWS KeyPair fingerprint가 다를 수 있습니다:
  ```bash
  # AWS 쪽 fingerprint
  aws ec2 describe-key-pairs --region ap-northeast-2 \
    --key-names eda-cluster-key-<ACCOUNT> --query 'KeyPairs[0].KeyFingerprint'
  # 로컬 pem fingerprint (RSA는 md5 방식)
  openssl pkcs8 -in ~/.ssh/eda-cluster-key-<ACCOUNT>.pem -nocrypt -topk8 -outform DER \
    | openssl sha1 -c
  ```
  두 값이 다르면 pem을 지우고 setup.sh를 다시 실행하세요.

---

## 5. 데이터 업로드 (rsync over SSH)

RTL·testbench·프로젝트 파일은 별도 S3 없이 **`rsync` over SSH**로 로컬
워크스테이션에서 FSx(`/fsxz/...`)에 바로 올립니다. 추가 인프라·버킷 불필요하고
보안적으로도 안전합니다 (아래 설명).

### 5.1 기본 사용법

```bash
# 로컬 → login/head node → FSx work 볼륨
rsync -az --delete --progress \
  -e "ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem" \
  ./my-rtl-project/ \
  ec2-user@<HEAD_OR_LOGIN_NODE>:/fsxz/work/<user>/my-rtl-project/

# 반대로 결과 회수
rsync -az --progress \
  -e "ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem" \
  ec2-user@<HEAD_OR_LOGIN_NODE>:/fsxz/work/<user>/my-rtl-project/results/ \
  ./results/
```

주요 옵션:
- `-a` (archive): 퍼미션/타임스탬프/심볼릭 링크 보존
- `-z`: 전송 중 압축 (느린 회선에서 효과적)
- `--delete`: 로컬에서 지운 파일을 원격에서도 삭제 (완전 미러링 원할 때)
- `--progress`: 진행률 표시
- `--exclude='*.o' --exclude='build/'`: 필요 없는 파일 제외

### 5.2 보안적으로 안전한가?

**안전합니다.** 이유:

1. **전송 구간 암호화** — rsync가 `-e ssh`로 SSH 터널을 타고 가므로 모든
   트래픽이 TLS 수준 암호화(보통 AES-256-GCM 또는 ChaCha20-Poly1305).
   평문 `rsync://` 프로토콜과 다름.
2. **네트워크 경로가 private** — 현재 VPC는 private subnet + VPN 구성이라
   인터넷 노출 없음. 로컬 ↔ VPN ↔ AWS VPC ↔ head/login node 전부 사설 경로.
3. **인증** — EC2 KeyPair의 private key (`~/.ssh/eda-cluster-key-*.pem`)로만
   접속 가능. 해당 pem 파일은 `~/.ssh` 안에 `chmod 400`으로 저장됨.
   SSM Parameter Store에서도 KMS 암호화되어 보관.
4. **저장 시 암호화** — FSx OpenZFS는 기본적으로 KMS로 at-rest 암호화
   (`EdaStorage` 스택의 `OpenZfsKey`). 디스크 물리적 탈취 시나리오에도 안전.
5. **감사 로그** — SSH 접속 시도는 VPC Flow Logs + CloudTrail + head/login node
   `/var/log/secure`에 기록됨.

**주의할 점:**
- pem 키 파일 관리 — Git에 커밋 금지 (`.gitignore`에 `*.pem` 포함됨), 다른 사람과
  공유 금지. 분실 시 즉시 해당 KeyPair 삭제하고 클러스터 재배포 필요.
- 로컬 SSH config에 `StrictHostKeyChecking=no`를 상시 설정하지 말 것
  (MITM 방어 약화). 최초 접속 시만 `accept-new`로 호스트키 등록.

### 5.3 대용량 / 반복 업로드 팁

```bash
# 드라이런 (뭐가 전송될지 미리 확인)
rsync -az --dry-run --itemize-changes ...

# 대역폭 제한 (회선 부하 방지, 예: 10 MB/s)
rsync -az --bwlimit=10000 ...

# .gitignore 패턴 재사용 (git이 추적하지 않는 파일 전송 제외)
rsync -az --exclude-from=.gitignore ...

# ssh master connection 재사용으로 속도 개선
# ~/.ssh/config 에 아래 추가:
#   Host 10.0.*.*
#     ControlMaster auto
#     ControlPath ~/.ssh/cm-%r@%h:%p
#     ControlPersist 10m
```

---

## 6. 삭제

```bash
# 1. 클러스터
pcluster delete-cluster --cluster-name hpc-cluster --region ap-northeast-2

# 2. CDK 스택 (FSx/CloudTrail/KMS는 RemovalPolicy.RETAIN — 수동 삭제 필요)
cd cdk
cdk destroy --all -c eda:vpc_id=$VPC_ID -c eda:subnet_id=$SUBNET_ID
```

FSx 파일시스템, CloudTrail S3 버킷, KMS 키는 실수 방지를 위해 retain 설정이라
필요하면 콘솔 / CLI에서 별도 삭제합니다.

---

## 7. 재배포 시 유의사항

- `{prefix}Base` 스택의 VPC endpoint 로직은 **이미 존재하는 endpoint**를 자동으로
  skip하지만, 이 스택이 `Project=eda-cluster` 태그로 만든 endpoint는 skip 대상에서
  제외합니다. (태그 없이 skip하면 재배포 시 템플릿에서 빠져 삭제되어 HeadNode
  부트스트랩이 실패하기 때문)
- `cdk.context.json`이 다른 VPC를 캐시하고 있으면 setup.sh가 자동으로 백업 후 삭제합니다.
- 구버전(`EdaNetwork`, `EdaVpcEndpoints` 두 스택 구조)에서 올라오는 경우, 기존 스택을
  먼저 `cdk destroy` 또는 콘솔에서 삭제한 뒤 새로 배포하세요 (새 구조는 `{prefix}Base`
  하나로 통합).

---

## 8. 디렉터리 구조

```
eda-aws/
├── setup.sh                    # 로컬 원클릭 배포 스크립트
├── setup-from-cloudshell.sh    # CloudShell용 배포 스크립트
├── create-cluster.sh           # 클러스터만 재생성할 때
├── config/
│   ├── default.env             # 기본 설정
│   └── example.env             # 예시
├── cdk/                        # CDK Python 프로젝트
│   ├── app.py
│   ├── cdk/                    # 스택 모듈
│   │   ├── base_stack.py            # {prefix}Base: SG + KeyPair + CloudTrail + VPC endpoints
│   │   ├── storage_stack.py         # {prefix}Storage: FSx OpenZFS / ONTAP
│   │   ├── license_server_stack.py  # {prefix}LicenseServer: EDA 라이선스 서버
│   │   └── slurm_db_stack.py        # (미사용 옵션) Slurm accounting RDS
│   ├── pcluster-config-template.yaml
│   └── requirements.txt
├── cdk-dcv/                    # (선택) DCV 관련 CDK
├── architecture_guide.md       # 전체 아키텍처 설계
└── parallel_cluster_configuration.md   # ParallelCluster 환경 구성 및 설정 가이드
```
