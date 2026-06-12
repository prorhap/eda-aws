[English](./parallel_cluster_configuration.md) | [한국어](./parallel_cluster_configuration.ko.md)

# ParallelCluster 구성 가이드

> `setup.sh`가 무엇을 만드는지, 클러스터가 어떻게 구성되어 있는지, 어떻게 변경하는지를 설명한다.
>
> 배포 사전 준비 및 `setup.sh` 실행 방법은 [README](README.ko.md)를 참고한다.

---

## 1. setup.sh가 만드는 환경

`setup.sh`를 실행하면 아래 환경이 처음부터 끝까지 자동으로 구성된다.

### 1.1 노드 구성

| 노드 | 인스턴스 타입 | 수량 | 역할 |
|---|---|---|---|
| Head Node | `m7i.xlarge` | 1 (상시 가동) | Slurm 컨트롤러, 잡 스케줄러 |
| Login Node | `r7i.2xlarge` | 1 (풀) | 사용자 진입점 — 잡 제출, 파일 접근 |
| Compute Node | `r8i.32xlarge` | 0–2 (자동 증감) | 시뮬레이션 / 리그레션 워크로드 |

- Head Node는 항상 가동 중이다. Slurm 컨트롤러(`slurmctld`)가 동작하며, 시뮬레이션 잡 실행에 사용하면 안 된다.
- Login Node는 ALB 뒤에 위치한다. 사용자는 일상 작업을 위해 이 노드에 SSH로 접속한다.
- Compute Node는 잡이 제출될 때 자동으로 시작(`MinCount: 0`)되며, 15분 유휴 후 자동 종료(`ScaledownIdletime: 15`)된다.

### 1.2 스토리지 레이아웃

| 마운트 경로 | 타입 | 용도 |
|---|---|---|
| `/fsxz/tools` | FSx OpenZFS | EDA 툴, 공유 실행파일 |
| `/fsxz/work` | FSx OpenZFS | RTL 소스, 프로젝트 파일, 시뮬레이션 결과 |
| `/fsxz/scratch` | FSx OpenZFS | 잡별 임시 작업 공간 (`$USER/$SLURM_JOB_ID`) |

세 볼륨 모두 Head, Login, Compute 노드에 부팅 시 NFS로 자동 마운트된다
(`pcluster-config.yaml`의 `SharedStorage` 섹션). 어느 노드에서 쓴 파일이든 즉시 다른 노드에서 보인다.

### 1.3 Slurm 설정

| 설정 | 값 | 효과 |
|---|---|---|
| 스케줄러 | Slurm | — |
| 큐 이름 | `eda-r8i` | 잡 스크립트에서 `#SBATCH --partition=eda-r8i` 사용 |
| Capacity 타입 | On-Demand | Spot 인터럽션 없음 |
| 메모리 기반 스케줄링 | 활성화 (`CR_Core_Memory`) | 잡 스크립트의 `--mem` 값이 실제로 적용됨 |
| 유휴 스케일다운 | 15분 | Compute 노드가 15분 유휴 후 자동 종료 |
| DNS | `UseEc2Hostnames: true` | EC2 기본 hostname 사용. Route 53 미사용 (VPC endpoint 미지원) |
| 잡 독점 할당 | `false` | 하나의 Compute 노드에 여러 잡이 공존 가능 |

### 1.4 OS 및 스토리지

- **OS**: RHEL 8 (`rhel8`)
- **Head Node 루트 볼륨**: 500 GiB gp3, 6000 IOPS, 250 MB/s
- **Compute Node 루트 볼륨**: 200 GiB gp3, 3000 IOPS, 125 MB/s
- **FSx OpenZFS 암호화**: KMS로 at-rest 암호화 (`EdaStorage` 스택의 `OpenZfsKey`)

### 1.5 모니터링

CloudWatch 모니터링은 기본으로 활성화된다.

| 항목 | 설정 |
|---|---|
| CloudWatch Logs | 활성화, 90일 보존 |
| CloudWatch 대시보드 | 활성화 (클러스터 개요) |
| 로그 삭제 정책 | Retain (클러스터 삭제 시에도 로그 유지) |

---

## 2. pcluster-config-template.yaml 구조 이해

`setup.sh`는 `cdk/pcluster-config-template.yaml`의 플레이스홀더를
`cdk/outputs.json`의 값으로 치환하여 `cdk/pcluster-config.yaml`을 생성한다.
수동으로 변경이 필요할 때 템플릿 구조를 이해하면 도움이 된다.

### 2.1 플레이스홀더 치환

플레이스홀더는 스택 접두사와 무관한 중립 키를 사용한다.
`setup.sh`가 현재 `STACK_PREFIX`를 기반으로 `outputs.json`에서 실제 값을 읽어 치환한다.

| 플레이스홀더 | outputs.json의 소스 | 값 |
|---|---|---|
| `${BASE.PrimarySubnetId}` | `EdaBase.PrimarySubnetId` | 전체 노드용 서브넷 |
| `${BASE.SgClusterNodesId}` | `EdaBase.SgClusterNodesId` | 공용 보안 그룹 |
| `${BASE.KeyPairName}` | `EdaBase.KeyPairName` | SSH 키페어 이름 |
| `${STORAGE.VolToolsId}` | `EdaStorage.VolToolsId` | /fsxz/tools 볼륨 ID |
| `${STORAGE.VolWorkId}` | `EdaStorage.VolWorkId` | /fsxz/work 볼륨 ID |
| `${STORAGE.VolScratchId}` | `EdaStorage.VolScratchId` | /fsxz/scratch 볼륨 ID |

### 2.2 조건부 블록

`setup.sh`가 config 플래그에 따라 마커 주석 사이의 섹션을 활성화하거나 제거한다.

| 마커 | Config 플래그 | 효과 |
|---|---|---|
| `#OPENZFS_BEGIN` / `#OPENZFS_END` | `ENABLE_OPENZFS` | OpenZFS SharedStorage 항목 포함/제외 |
| `#ONTAP_BEGIN` / `#ONTAP_END` | `ENABLE_ONTAP` | ONTAP SharedStorage 항목 포함/제외 |
| `#LOGINNODES_BEGIN` / `#LOGINNODES_END` | `ENABLE_LOGIN_NODE` | `LoginNodes` 섹션 포함/제외 |
| `#SSM_BEGIN` / `#SSM_END` | `ENABLE_SSM` | Head Node의 SSM IAM 정책 주석 해제 |

### 2.3 주요 섹션 요약

```yaml
HeadNode:
  InstanceType: m7i.xlarge          # Head Node 크기 변경 시 여기 수정
  LocalStorage:
    RootVolume:
      Size: 500                     # GiB — /var가 꽉 찰 경우 늘리기

LoginNodes:
  Pools:
    - Count: 1                      # 동시 Login Node 수
      InstanceType: r7i.2xlarge     # 인터랙티브 작업이 많으면 큰 타입으로 변경

SharedStorage:
  - MountDir: /fsxz/tools           # 전체 노드에서의 마운트 경로
    FsxOpenZfsSettings:
      VolumeId: <outputs.json에서>  # 직접 수정하지 말고 config 재생성

Scheduling:
  SlurmSettings:
    ScaledownIdletime: 15           # 유휴 Compute 종료까지 대기 시간(분)
    EnableMemoryBasedScheduling: true
  SlurmQueues:
    - Name: eda-r8i
      ComputeResources:
        - InstanceType: r8i.32xlarge
          MaxCount: 2               # 최대 동시 Compute 노드 수
```

---

## 3. 설정 변경 방법

### 3.1 인스턴스 타입, 노드 수 등 클러스터 레벨 설정 변경

`cdk/pcluster-config-template.yaml`을 수정한 뒤 config를 재생성하고 적용한다.

```bash
# 수정된 템플릿으로 pcluster-config.yaml 재생성
SKIP_CDK=1 SKIP_CLUSTER=1 ./setup.sh

# 실행 중인 클러스터에 변경 사항 적용
export CLUSTER_NAME="hpc-cluster"
pcluster update-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration cdk/pcluster-config.yaml
```

모든 항목이 실행 중 변경 가능하지는 않다. ParallelCluster가 클러스터 삭제 후 재생성이
필요한 변경 항목을 알려준다.

### 3.2 Compute 노드 인스턴스 타입 또는 수량 변경

`pcluster-config-template.yaml`의 `SlurmQueues[0].ComputeResources` 아래를 수정한다.

```yaml
ComputeResources:
  - Name: r128
    InstanceType: r8i.32xlarge   # 인스턴스 타입 변경
    MinCount: 0
    MaxCount: 2                  # 최대 동시 노드 수 변경
```

`SKIP_CDK=1 SKIP_CLUSTER=1 ./setup.sh`로 재생성 후 `pcluster update-cluster`를 실행한다.

### 3.3 두 번째 큐 추가

다른 인스턴스 타입의 큐를 추가할 때 (예: 대규모 리그레션용 메모리 최적화 큐):

```yaml
SlurmQueues:
  - Name: eda-r8i
    # ... 기존 큐 ...
  - Name: eda-mem
    CapacityType: ONDEMAND
    Networking:
      SubnetIds:
        - ${BASE.PrimarySubnetId}
      AdditionalSecurityGroups:
        - ${BASE.SgClusterNodesId}
    ComputeResources:
      - Name: x2idn
        InstanceType: x2idn.32xlarge
        MinCount: 0
        MaxCount: 4
```

### 3.4 FSx 용량 또는 Throughput 변경

FSx 리소스는 pcluster가 아닌 CDK가 관리한다. `config/default.env`에서 값을 변경하고
CDK 스토리지 스택을 재배포한다.

```bash
# config/default.env 수정:
#   OPENZFS_SIZE_GIB=640
#   OPENZFS_THROUGHPUT=2560

SKIP_CLUSTER=1 ./setup.sh
```

> FSx 용량은 늘릴 수만 있고 줄일 수 없다. Throughput은 양방향 변경 가능하지만
> 6시간에 한 번만 변경할 수 있다.

### 3.5 이미 실행 중인 클러스터에 SSM 활성화

SSM IAM 정책은 실행 중인 클러스터에 추가할 수 없으므로 클러스터를 재생성해야 한다.

```bash
pcluster delete-cluster --cluster-name $CLUSTER_NAME
# 삭제 완료까지 대기
ENABLE_SSM=1 SKIP_CDK=1 ./setup.sh
```

### 3.6 CDK 재배포 없이 pcluster-config.yaml만 재생성

템플릿을 수정한 뒤 config 파일만 다시 만들고 싶을 때:

```bash
SKIP_CDK=1 SKIP_CLUSTER=1 ./setup.sh
```

---

## 4. Custom AMI

ParallelCluster는 기본적으로 AWS가 관리하는 공식 RHEL 8 AMI를 사용한다.
Custom AMI를 만들면 EDA 툴, 커널 파라미터, OS 설정을 이미지 빌드 시점에 미리
구워 넣을 수 있어, 컴퓨트 노드가 시작할 때마다 실행하는 bootstrap 스크립트 없이
바로 사용 가능한 상태로 뜬다.

### 4.1 Custom AMI가 필요한 경우

| 상황 | 권장 방법 |
|---|---|
| EDA 툴을 공유 FSx 경로에 설치 | Stock AMI (Custom AMI 불필요) |
| 잡 실행 전 반드시 설정해야 하는 커널 파라미터 (`vm.max_map_count`, `ulimit` 등) | Custom AMI 또는 `OnNodeConfigured` 스크립트 |
| 설치 시간이 긴 대형 공통 패키지 (Calibre, StarRC 등) | Custom AMI |
| 사내 CA 인증서, 프록시 설정, 보안 에이전트 | Custom AMI |

이 EDA 환경처럼 툴이 `/fsxz/tools`에 있는 경우 Custom AMI는 선택 사항이지만,
커널 튜닝이나 완전히 재현 가능한 노드 이미지가 필요하다면 권장한다.

### 4.2 EC2 Image Builder로 Custom AMI 빌드

ParallelCluster의 `pcluster build-image` 명령은 EC2 Image Builder를 래핑한다.
공식 ParallelCluster 베이스 AMI에서 시작해 커스터마이징을 적용하므로
호환성이 보장된 AMI가 만들어진다.

**Step 1 — 이미지 설정 파일 작성**

```yaml
# image-config.yaml
Build:
  InstanceType: c6i.2xlarge          # 빌더 인스턴스 — RAM이 충분한 타입 선택
  ParentImage: arn:aws:imagebuilder:ap-northeast-2:aws:image/amazon-linux-2-kernel-510-x86/x.x.x
  SubnetId: subnet-xxxxxxxxxxxxxxxxx  # 클러스터와 동일한 프라이빗 서브넷
  SecurityGroupIds:
    - sg-xxxxxxxxxxxxxxxxx            # 아웃바운드 HTTPS 허용 (yum/pip용); 인바운드 불필요
  Components:
    - Type: script
      Value: |
        #!/bin/bash
        set -euo pipefail

        # ── 커널 / OS 튜닝 ────────────────────────────────────────
        cat >> /etc/sysctl.d/99-eda.conf << 'EOF'
        vm.max_map_count = 1048576
        kernel.pid_max = 4194304
        net.core.somaxconn = 65535
        EOF
        sysctl --system

        # ── EDA 툴용 ulimit ───────────────────────────────────────
        cat >> /etc/security/limits.d/99-eda.conf << 'EOF'
        * soft nofile 1048576
        * hard nofile 1048576
        * soft nproc  unlimited
        * hard nproc  unlimited
        * soft stack  unlimited
        EOF

        # ── 사내 CA 인증서 (필요한 경우) ──────────────────────────
        # cp /path/to/site-ca.crt /etc/pki/ca-trust/source/anchors/
        # update-ca-trust

        # ── 공통 패키지 ───────────────────────────────────────────
        yum install -y tcsh ksh xorg-x11-apps libXScrnSaver

DevSettings:
  Cookbook:
    ExtraChefAttributes: |
      {
        "cluster": {
          "slurm_nodename": "custom"
        }
      }
```

> 리전과 OS에 맞는 공식 ParallelCluster 베이스 AMI ARN은 아래 명령으로 확인:
> ```bash
> pcluster list-official-images --os rhel8 --region ap-northeast-2
> ```

**Step 2 — AMI 빌드**

```bash
pcluster build-image \
  --image-id eda-rhel8-$(date +%Y%m%d) \
  --image-configuration image-config.yaml \
  --region ap-northeast-2
```

빌드는 EC2 Image Builder 파이프라인으로 실행되며 **15–30분** 소요된다.
진행 상황 모니터링:

```bash
# 빌드 상태 확인
pcluster describe-image \
  --image-id eda-rhel8-$(date +%Y%m%d) \
  --region ap-northeast-2

# 전체 Custom 이미지 목록
pcluster list-images --image-status AVAILABLE --region ap-northeast-2
```

**Step 3 — AMI ID 확인**

```bash
pcluster describe-image \
  --image-id eda-rhel8-$(date +%Y%m%d) \
  --region ap-northeast-2 \
  --query 'ec2AmiInfo.amiId' --output text
```

### 4.3 Custom AMI를 클러스터 설정에 적용

`cdk/pcluster-config-template.yaml`의 `Image` 섹션을 수정한다:

```yaml
# 변경 전 (stock AMI)
Image:
  Os: rhel8

# 변경 후 (custom AMI)
Image:
  CustomAmi: ami-0xxxxxxxxxxxxxxxxx   # 4.2에서 확인한 AMI ID
```

config를 재생성하고 클러스터를 생성(또는 재생성)한다:

```bash
# config 재생성
SKIP_CDK=1 SKIP_CLUSTER=1 ./setup.sh

# Custom AMI로 클러스터 생성
SKIP_CDK=1 ./setup.sh
```

> `CustomAmi`를 지정하면 `Os`는 AMI에서 자동 추론된다. 경고를 없애려면 `Os`를
> 명시적으로 지정해도 되지만, Custom AMI가 우선 적용된다.

### 4.4 실행 중인 클러스터의 AMI 교체

실행 중인 클러스터에서 AMI를 변경하면 Head Node 교체가 필요하므로
compute fleet을 먼저 중단해야 한다:

```bash
export CLUSTER_NAME="hpc-cluster"

# 1. compute fleet 중지
pcluster update-compute-fleet \
  --cluster-name $CLUSTER_NAME \
  --status STOP_REQUESTED

# 2. 클러스터 설정 업데이트 (Head Node 교체 트리거)
pcluster update-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration cdk/pcluster-config.yaml

# 3. 업데이트 모니터링
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query '{Status:clusterStatus,UpdateStatus:lastUpdatedAt}'
```

> Head Node 교체 시 Slurm 컨트롤러가 약 5분간 중단된다.
> 유지보수 시간대에 진행하는 것을 권장한다.

### 4.5 AMI 유지보수 주기

Custom AMI는 ParallelCluster 버전을 올리거나 OS 패치를 적용할 때 재빌드해야 한다.
권장 주기:

1. **월 1회** — `yum update`를 적용하여 AMI 재빌드
2. **ParallelCluster 마이너 버전 업** 시 — 항상 재빌드. 베이스 AMI가 바뀌므로
   기존 Custom AMI가 호환되지 않을 수 있다
3. **툴 설치 변경 시** — AMI에 툴을 직접 구운 경우에만 해당
   (FSx에 있는 툴은 AMI 재빌드 불필요)

빌드 날짜를 이름에 포함하는 네이밍 컨벤션 `eda-rhel8-YYYYMMDD`를 사용하면
`CustomAmi`에 이전 AMI ID를 지정하는 것만으로 간단히 롤백할 수 있다.

---

## 5. 클러스터 생성

### 5.1 setup.sh로 일괄 생성 (권장)

`setup.sh`가 CDK 배포 → 설정 파일 생성 → SSH 키 다운로드 → 클러스터 생성을 자동 수행한다.

```bash
cd /path/to/eda-aws

# 기본 (SSH만, SSM 비활성)
./setup.sh

# SSM Session Manager 활성화
ENABLE_SSM=1 ./setup.sh

# CDK 배포 건너뛰기 (이미 배포된 경우)
SKIP_CDK=1 ./setup.sh

# 설정 파일만 생성 (클러스터 생성 안 함)
SKIP_CLUSTER=1 ./setup.sh
```

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `SKIP_CDK` | `0` | `1`이면 CDK 배포 건너뜀 |
| `SKIP_CLUSTER` | `0` | `1`이면 클러스터 생성 건너뜀 |
| `ENABLE_SSM` | `0` | `1`이면 SSM Session Manager 활성화 |

생성에는 **10-15분** 정도 소요되며, 스크립트가 완료까지 자동 대기한다.

### 4.2 수동 생성

```bash
pcluster create-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration cdk/pcluster-config.yaml
```

### 4.3 상태 모니터링

```bash
# 클러스터 상태 확인
pcluster describe-cluster --cluster-name $CLUSTER_NAME

# clusterStatus만 확인
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query 'clusterStatus'

# CloudFormation 스택 이벤트
aws cloudformation describe-stack-events \
  --stack-name $CLUSTER_NAME \
  --query 'StackEvents[0:10].{Time:Timestamp,Status:ResourceStatus,Resource:LogicalResourceId}' \
  --output table
```

### 4.4 생성 완료 확인

```bash
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query '{Status:clusterStatus,HeadNode:headNode.privateIpAddress,LoginAddress:loginNodes[0].address}'
```

---

## 6. 클러스터 접속

접속 방법은 **SSH**와 **SSM** 두 가지가 있다.

| 방법 | 필요 조건 | 대상 노드 | 용도 |
|---|---|---|---|
| SSH (VPN 경유) | VPN 연결 + SSH 키페어 | Head Node, Login Node | 일반 작업, 잡 제출 |
| SSM Session Manager | IAM 자격 증명 + `ENABLE_SSM=1`로 클러스터 생성 | Head Node | 관리/디버깅, VPN 없이 접속 |

### 6.1 SSH 접속 (VPN 경유)

SSH 접속은 Site-to-Site VPN으로 private IP에 도달 가능해야 한다.
SSH 키는 CDK에서 생성하며, 스크립트 실행 시 `~/.ssh/eda-cluster-key.pem`에 자동 다운로드된다.

**Head Node 접속**

```bash
# pcluster ssh 래퍼 사용
pcluster ssh --cluster-name $CLUSTER_NAME \
  -i ~/.ssh/eda-cluster-key.pem

# 또는 직접 SSH
# macOS / Linux
ssh -i ~/.ssh/eda-cluster-key.pem ec2-user@<head-node-private-ip>

# Windows (PowerShell)
ssh -i $env:USERPROFILE\.ssh\eda-cluster-key.pem ec2-user@<head-node-private-ip>
```

**Login Node 접속 (사용자용)**

Login Node는 NLB 뒤에 있으며, HeadNode의 SSH 키를 자동 상속한다.

```bash
# Login Node NLB 주소 확인
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query 'loginNodes[0].address' --output text

# macOS / Linux
ssh -i ~/.ssh/eda-cluster-key.pem ec2-user@<login-nodes-address>

# Windows (PowerShell)
ssh -i $env:USERPROFILE\.ssh\eda-cluster-key.pem ec2-user@<login-nodes-address>
```

> Day 1에서는 사내망 → Site-to-Site VPN → Login Node NLB(private IP) 경로로 접속한다.

**Head Node private IP 확인**

```bash
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query 'headNode.privateIpAddress' --output text
```

### 6.2 SSM Session Manager 접속 (VPN/키 불필요)

SSM은 IAM 자격 증명만으로 접속 가능하며, SSH 키나 VPN 연결이 필요 없다.
Head Node에만 접속 가능하다.

**사전 조건**

1. 클러스터 생성 시 `ENABLE_SSM=1` 옵션 사용:
   ```bash
   ENABLE_SSM=1 ./setup.sh
   ```
   이 옵션이 `AmazonSSMManagedInstanceCore` IAM 정책을 Head Node에 추가한다.
   기본값은 비활성화(`ENABLE_SSM=0`)이다.

2. 로컬에 `session-manager-plugin` 설치 (README § 2.2 참고).

**접속 방법**

```bash
# Head Node 인스턴스 ID 확인
INSTANCE_ID=$(pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query 'headNode.instanceId' --output text)

# SSM 세션 시작
aws ssm start-session --target $INSTANCE_ID
```

> SSM 세션은 `ssm-user`로 접속된다. `ec2-user`로 전환하려면: `sudo su - ec2-user`

---

## 7. 클러스터 검증 테스트

클러스터 생성 후 Login Node에 접속하여 아래 항목을 순서대로 확인한다.

### 7.1 Slurm 상태 확인

```bash
# 파티션/노드 상태
sinfo
# 기대: eda-r8i 파티션, idle~ 상태 (compute node가 아직 안 떠있음)

# 상세 정보
scontrol show partition
```

### 7.2 FSx 마운트 확인

```bash
# 마운트 상태
df -h | grep fsx
# 기대: /fsxz/tools, /fsxz/work, /fsxz/scratch 세 개 모두 보여야 함

# 마운트 옵션 확인 (nconnect, rsize/wsize)
nfsstat -m

# 읽기/쓰기 테스트
ls /fsxz/tools/
touch /fsxz/work/test_write && rm /fsxz/work/test_write
touch /fsxz/scratch/test_write && rm /fsxz/scratch/test_write
```

### 7.3 메모리 기반 스케줄링 확인

```bash
scontrol show config | grep -i memory
# SelectTypeParameters = CR_Core_Memory 가 보여야 함
```

### 7.4 테스트 잡 제출

Compute Node는 `MinCount: 0`이므로 잡을 제출하면 자동으로 시작된다 (2-5분 소요).

```bash
mkdir -p /fsxz/scratch/$USER

cat > /fsxz/scratch/$USER/test_job.sh << 'EOF'
#!/bin/bash
#SBATCH --job-name=cluster-test
#SBATCH --partition=eda-r8i
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=00:10:00

echo "=== Job Info ==="
echo "Job ID: $SLURM_JOB_ID"
echo "Node: $(hostname)"
echo "CPUs: $SLURM_CPUS_PER_TASK"
echo "Memory: ${SLURM_MEM_PER_NODE:-N/A} MB"

echo ""
echo "=== Mount Points ==="
df -h | grep fsx

echo ""
echo "=== CPU Info ==="
lscpu | grep -E "^(Architecture|CPU\(s\)|Model name|Thread)"

echo ""
echo "=== Memory Info ==="
free -h
EOF

sbatch /fsxz/scratch/$USER/test_job.sh
```

### 7.5 잡 상태 확인 및 결과

```bash
# 잡 상태 확인 (PENDING → CONFIGURING → RUNNING → COMPLETED)
squeue

# 잡 완료 후 결과 확인
sacct --format=JobID,JobName,Partition,State,Elapsed,MaxRSS,NodeList

# 잡 출력 확인
cat /fsxz/scratch/$USER/slurm-*.out
```

> PENDING에서 2-5분간 Compute Node가 시작된다. `squeue`로 상태 변화를 확인한다.

---

## 8. 첫 번째 잡 실행

### 8.1 VCS Simulation 잡 예시

```bash
cat > /fsxz/scratch/$USER/run_vcs.sh << 'EOF'
#!/bin/bash
#SBATCH --job-name=vcs_smoke
#SBATCH --partition=eda-r8i
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=02:00:00

WORKDIR=/fsxz/scratch/$USER/$SLURM_JOB_ID
mkdir -p $WORKDIR && cd $WORKDIR

# Tool setup
source /fsxz/tools/eda/env/vcs_setup.sh

# Copy project files
cp -r /fsxz/work/projects/chipA/rtl .
cp -r /fsxz/work/projects/chipA/tb .
cp /fsxz/work/projects/chipA/filelist/smoke.f .

# Run VCS compile + simulation
vcs -full64 -f smoke.f -o simv
./simv +ntb_random_seed=1234

# Save results
RESULT_DIR=/fsxz/work/results/chipA/$(date +%Y%m%d)/$SLURM_JOB_ID
mkdir -p $RESULT_DIR
cp -r $WORKDIR/*.log $WORKDIR/*.fsdb $RESULT_DIR/ 2>/dev/null || true

echo "Results saved to: $RESULT_DIR"
EOF

sbatch /fsxz/scratch/$USER/run_vcs.sh
```

---

## 9. 운영 관리

### 9.1 클러스터 업데이트

설정 변경 시:
```bash
pcluster update-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration pcluster-config.yaml
```

### 9.2 클러스터 중지/시작

비용 절감을 위해 사용하지 않을 때:
```bash
# compute fleet 중지 (head/login은 유지, compute만 종료)
pcluster update-compute-fleet \
  --cluster-name $CLUSTER_NAME \
  --status STOP_REQUESTED

# compute fleet 재시작
pcluster update-compute-fleet \
  --cluster-name $CLUSTER_NAME \
  --status START_REQUESTED
```

### 9.3 클러스터 삭제

```bash
pcluster delete-cluster --cluster-name $CLUSTER_NAME
# FSx, VPC 등 CDK 리소스는 영향 없음
```

### 9.4 CloudWatch 알람 이메일 구독

```bash
# SNS 토픽 ARN 확인 (outputs.json에서)
ALARM_TOPIC=$(jq -r '.EdaStorage.AlarmTopicArn' cdk/outputs.json)

# 이메일 구독 추가
aws sns subscribe \
  --topic-arn "$ALARM_TOPIC" \
  --protocol email \
  --notification-endpoint your-team@example.com
```

---

## 10. 트러블슈팅

### 클러스터 생성 실패

```bash
# CloudFormation 이벤트에서 실패 원인 확인
pcluster describe-cluster --cluster-name $CLUSTER_NAME
# 상세 로그
pcluster get-cluster-log-events --cluster-name $CLUSTER_NAME \
  --log-stream-name cfn-init
```

### Compute Node가 시작되지 않음

```bash
# head node에서 확인
sudo tail -f /var/log/parallelcluster/clustermgtd.log
sudo tail -f /var/log/parallelcluster/slurm_resume.log

# EC2 인스턴스 제한 확인
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-74FC7D96
```

### FSx 마운트 문제

```bash
# FSx ID 확인
FSX_ID=$(jq -r '.EdaStorage.FsxId' cdk/outputs.json)

# NFS 포트 연결 테스트 (head node에서)
FSX_DNS=$(aws fsx describe-file-systems --file-system-ids $FSX_ID \
  --query 'FileSystems[0].DNSName' --output text)
nc -zv $FSX_DNS 2049

# 보안 그룹 확인
SG_FSX=$(jq -r '.EdaBase.SgFsxId' cdk/outputs.json)
aws ec2 describe-security-groups --group-ids $SG_FSX \
  --query 'SecurityGroups[0].IpPermissions'
```

### 잡이 PENDING 상태에서 멈춤

```bash
# pending 이유 확인
squeue -j <job_id> -o "%R"

# 가능한 원인:
# - Resources: compute node 시작 대기 중 (2-5분 정상)
# - Priority: 다른 잡이 우선
# - ReqNodeNotAvail: 노드 부팅 중
```

---

## 11. VPC Flow Logs

CDK 배포 시 VPC Flow Log가 자동 활성화된다. 모든 네트워크 트래픽(허용+거부)이 CloudWatch Logs에 기록된다.

| 항목 | 설정 |
|---|---|
| 저장 대상 | CloudWatch Logs (`/eda/vpc-flow-logs`) |
| 트래픽 타입 | ALL (허용 + 거부) |
| 보존 기간 | 90일 (기본, `cdk.json`에서 변경 가능) |

보존 기간 변경:

```json
// cdk/cdk.json
"eda:vpc_flow_log_retention_days": 180
```

지원 값: `30`, `60`, `90` (기본), `180`, `365`

CloudWatch Logs Insights로 조회:

```
# 거부된 트래픽 확인
fields @timestamp, srcAddr, dstAddr, dstPort, action
| filter action = "REJECT"
| sort @timestamp desc
| limit 20
```

---

## 12. CloudTrail (감사 로그)

CDK 배포 시 CloudTrail이 자동 활성화된다. 모든 AWS API 호출이 S3에 기록된다.

### 12.1 구성 요약

| 항목 | 설정 |
|---|---|
| Trail 이름 | `eda-trail` |
| 로그 저장 | `s3://eda-cloudtrail-<account-id>/` |
| 암호화 | KMS (`eda/cloudtrail`) |
| 대상 이벤트 | 관리 이벤트 (읽기+쓰기) |
| S3 라이프사이클 | 90일 → Glacier, 365일 → 삭제 |
| 비용 | 관리 이벤트 1개 trail 무료, S3 저장 비용 미미 |

### 12.2 로그 확인

```bash
# 최근 이벤트 확인 (CloudTrail 콘솔 대신 CLI)
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=ec2.amazonaws.com \
  --max-results 10 \
  --query 'Events[].{Time:EventTime,Name:EventName,User:Username}'
```

### 12.3 Athena로 쿼리

CloudTrail 콘솔 → Event history → Create Athena table 로 테이블을 자동 생성한 뒤 Athena에서 SQL로 조회할 수 있다.

```sql
-- 누가 EC2 인스턴스를 시작했는지
SELECT eventtime, useridentity.arn, requestparameters
FROM cloudtrail_logs
WHERE eventsource = 'ec2.amazonaws.com'
  AND eventname = 'RunInstances'
ORDER BY eventtime DESC
LIMIT 10;

-- 보안 그룹 변경 이력
SELECT eventtime, useridentity.arn, eventname, requestparameters
FROM cloudtrail_logs
WHERE eventsource = 'ec2.amazonaws.com'
  AND eventname LIKE '%SecurityGroup%'
ORDER BY eventtime DESC;

-- 특정 사용자의 최근 활동
SELECT eventtime, eventsource, eventname
FROM cloudtrail_logs
WHERE useridentity.arn LIKE '%username%'
ORDER BY eventtime DESC
LIMIT 20;
```

---

## 13. DCV 원격 데스크톱

EDA GUI 작업(Verdi, DVE 등)을 위한 사용자별 DCV 인스턴스를 별도 CDK 프로젝트(`cdk-dcv/`)로 관리한다.
Login Node(SSH 잡 제출용)와 DCV 인스턴스(GUI 작업용)는 분리하는 것이 일반적이다.

### 13.1 구조

```
cdk/       → VPC, FSx, SG → SSM Parameter Store (/eda/network/*, /eda/storage/*)
cdk-dcv/   → SSM에서 읽어 DCV EC2 인스턴스 생성 (사용자별 독립 스택)
```

### 13.2 DCV 인스턴스 배포

```bash
cd cdk-dcv/

# 사용자별 DCV 인스턴스 생성
./deploy-dcv.sh alice
./deploy-dcv.sh bob
./deploy-dcv.sh alice r7i.4xlarge   # 인스턴스 타입 지정 (기본: r7i.2xlarge)
```

배포 시 자동으로:
- 메인 CDK의 VPC/Subnet/SG를 SSM에서 참조
- FSx 볼륨 (`/fsxz/tools`, `/fsxz/work`, `/fsxz/scratch`) NFS 마운트
- DCV 서버 설치 + GUI 데스크톱 구성
- SSM Parameter에 인스턴스 ID 기록 (`/eda/dcv/<username>/InstanceId`)
- EC2 태그에 `User: <username>` 설정

### 13.3 DCV 접속

VPN 경유로 브라우저에서 접속:

```
https://<private-ip>:8443
```

DCV 로그인 정보:
- Username: 배포 시 지정한 사용자명
- Password: `changeme123` (초기, 첫 로그인 후 변경)

SSH 접속:
```bash
ssh -i ~/.ssh/eda-cluster-key.pem ec2-user@<private-ip>
```

### 13.4 DCV 인스턴스 관리

```bash
# 인스턴스 ID 조회
INSTANCE_ID=$(aws ssm get-parameter --name /eda/dcv/alice/InstanceId \
  --query 'Parameter.Value' --output text)

# 정지 (비용 절감)
aws ec2 stop-instances --instance-ids $INSTANCE_ID

# 시작
aws ec2 start-instances --instance-ids $INSTANCE_ID

# 전체 DCV 인스턴스 조회
aws ec2 describe-instances \
  --filters "Name=tag:User,Values=*" "Name=tag:Name,Values=dcv-*" \
  --query 'Reservations[].Instances[].{User:Tags[?Key==`User`]|[0].Value,Id:InstanceId,State:State.Name,Ip:PrivateIpAddress}' \
  --output table
```

### 13.5 DCV 인스턴스 삭제

```bash
./destroy-dcv.sh alice
```

---

## 14. CDK 설정 (cdk.json)

`cdk/cdk.json`에서 변경 가능한 프로젝트 설정:

| 키 | 기본값 | 설명 |
|---|---|---|
| `eda:stack_prefix` | `Eda` | 스택 접두사 — `{prefix}Base`, `{prefix}Storage`, `{prefix}LicenseServer` |
| `eda:base_stack_name` | `{prefix}Base` | Base 스택 이름 개별 override |
| `eda:storage_stack_name` | `{prefix}Storage` | Storage 스택 이름 개별 override |
| `eda:license_stack_name` | `{prefix}LicenseServer` | License 스택 이름 개별 override |
| `eda:vpc_flow_log_retention_days` | `90` | VPC Flow Log 보존 기간 (일) |

> 스택 이름을 변경하면 새 스택으로 생성된다. 기존 스택을 삭제 후 재배포 필요.

---

## 15. SSM Parameter Store 구조

메인 CDK가 배포하는 SSM 파라미터 (DCV 등 외부 프로젝트에서 참조):

| 파라미터 | 소스 | 용도 |
|---|---|---|
| `/eda/network/VpcId` | {prefix}Base | VPC ID |
| `/eda/network/PrimarySubnetId` | {prefix}Base | Private Subnet ID |
| `/eda/network/PrimaryAz` | {prefix}Base | Subnet AZ |
| `/eda/network/SgClusterNodesId` | {prefix}Base | 클러스터 노드 SG |
| `/eda/network/KeyPairName` | {prefix}Base | SSH 키페어 이름 |
| `/eda/storage/FsxDns` | EdaStorage | FSx DNS 엔드포인트 |
| `/eda/storage/VolToolsId` | EdaStorage | /fsxz/tools 볼륨 ID |
| `/eda/storage/VolWorkId` | EdaStorage | /fsxz/work 볼륨 ID |
| `/eda/storage/VolScratchId` | EdaStorage | /fsxz/scratch 볼륨 ID |
| `/eda/dcv/<user>/InstanceId` | cdk-dcv | DCV 인스턴스 ID (사용자별) |

```bash
# 전체 파라미터 조회
aws ssm get-parameters-by-path --path /eda/ --recursive \
  --query 'Parameters[].{Name:Name,Value:Value}' --output table
```

---

## 16. 클린 재설치

전체 환경을 삭제하고 처음부터 재설치하는 절차:

```bash
# 1. DCV 인스턴스 삭제 (있는 경우)
cd cdk-dcv/
./destroy-dcv.sh alice
./destroy-dcv.sh bob

# 2. ParallelCluster 삭제 (10분 소요)
pcluster delete-cluster --cluster-name $CLUSTER_NAME
# "does not exist" 나올 때까지 대기
watch -n 30 'pcluster describe-cluster --cluster-name $CLUSTER_NAME 2>&1 | head -3'

# 3. CDK 스택 삭제 (FSx 삭제에 20-30분 소요)
cd cdk/
cdk destroy --all

# 4. 재배포
cd ..
./setup.sh
```

> FSx와 KMS 키는 `RemovalPolicy.RETAIN`으로 설정되어 있어 `cdk destroy`로 삭제되지 않는다.
> 완전히 제거하려면 AWS 콘솔 또는 CLI로 수동 삭제 필요.

---

## 17. 비용 관리 팁

1. **사용하지 않을 때 compute fleet STOP** — head/login만 유지하면 비용이 크게 줄어든다
2. **DCV 인스턴스 정지** — 미사용 시 `aws ec2 stop-instances`로 비용 절감
3. **ScaledownIdletime=15** — idle compute node는 15분 후 자동 종료
4. **FSx throughput 모니터링** — CloudWatch `NetworkThroughputUtilization`이 지속 50% 이상이면 512→1024 MBps 검토
5. **야간/주말 잡 없으면** — compute fleet STOP 자동화 고려 (EventBridge + Lambda)

---

## 참고

- [AWS ParallelCluster User Guide](https://docs.aws.amazon.com/parallelcluster/latest/ug/)
- [pcluster CLI Reference](https://docs.aws.amazon.com/parallelcluster/latest/ug/pcluster-v3.html)
- [FSx for OpenZFS Performance](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/performance.html)
- [Amazon DCV Admin Guide](https://docs.aws.amazon.com/dcv/latest/adminguide/)
- 본 프로젝트의 `architecture_guide.md` v1.2
