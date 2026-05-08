# ParallelCluster 생성 가이드

> CDK로 배포된 인프라(VPC, FSx, Security Groups) 위에 ParallelCluster를 생성하는 실전 가이드.
>
> 이 문서는 `architecture_guide.md` v1.2의 Day 1 구성을 기준으로 한다.

---

## 1. 사전 준비

### 1.1 필수 도구 설치

**AWS CLI v2**

```bash
# macOS
brew install awscli

# Linux (x86_64)
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install

# Windows
# https://awscli.amazonaws.com/AWSCLIV2.msi 다운로드 후 설치

aws --version
```

**SSM Session Manager Plugin** (SSM 경유 접속에 필요)

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

# Windows
# https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe

session-manager-plugin --version
```

**ParallelCluster CLI**

```bash
pip install "aws-parallelcluster"
pcluster version
```

**기타 필수 도구**

```bash
# jq — JSON 처리
# macOS
brew install jq
# Linux — Debian/Ubuntu
sudo apt-get install -y jq
# Linux — RHEL/Amazon Linux
sudo yum install -y jq

# Node.js (CDK CLI에 필요) — https://nodejs.org/ 에서 LTS 설치
node --version
npm --version
```

### 1.2 AWS 자격 증명

```bash
# 현재 계정/리전 확인
aws sts get-caller-identity
aws configure get region   # ap-northeast-2 이어야 함

# 리전이 다르면 설정 (이후 명령에서 --region 생략 가능)
aws configure set region ap-northeast-2
```

> 이 문서의 모든 명령은 기본 리전이 `ap-northeast-2`로 설정된 것을 전제한다.
> 설정되지 않은 경우 각 명령에 `--region ap-northeast-2`를 추가하거나, 위 명령으로 기본 리전을 설정한다.

```bash
# 클러스터 이름 설정 (이후 명령에서 $CLUSTER_NAME 으로 참조)
export CLUSTER_NAME="hpc-cluster"
```

> 이 문서의 모든 CLI 명령은 `$CLUSTER_NAME` 환경변수를 사용한다. 위 명령을 먼저 실행한 뒤 복사-붙여넣기로 사용할 수 있다.

### 1.3 CDK 배포 상태 확인

```bash
# CDK 배포가 완료되어 있어야 함
cd cdk/
cat outputs.json
```

아래 값들이 outputs.json에 있어야 한다 (스택명은 `STACK_PREFIX=Eda` 기준):

| 키 | 용도 |
|---|---|
| `EdaBase.PrimarySubnetId` | Head/Login/Compute 서브넷 |
| `EdaBase.SgClusterNodesId` | 클러스터 노드 보안 그룹 |
| `EdaBase.KeyPairName` | SSH 키페어 이름 |
| `EdaBase.KeyPairId` | SSH 키페어 ID (SSM에서 private key 조회용) |
| `EdaStorage.VolToolsId` | /fsx/tools 볼륨 ID |
| `EdaStorage.VolWorkId` | /fsx/work 볼륨 ID |
| `EdaStorage.VolScratchId` | /fsx/scratch 볼륨 ID |

---

## 2. ParallelCluster 설정 파일 생성

### 2.1 템플릿에서 설정 파일 만들기

`pcluster-config-template.yaml`의 `${...}` 플레이스홀더를 실제 값으로 교체한다.

```bash
cd cdk/

# outputs.json에서 값을 읽어 자동 치환.
# 템플릿 placeholder는 스택 접두사와 무관한 중립 키(${BASE.*}, ${STORAGE.*})이고,
# 실제 값은 outputs.json의 스택명(기본 EdaBase / EdaStorage)에서 읽는다.
SUBNET_ID=$(jq -r '.EdaBase.PrimarySubnetId' outputs.json)
SG_CLUSTER=$(jq -r '.EdaBase.SgClusterNodesId' outputs.json)
KEY_PAIR_NAME=$(jq -r '.EdaBase.KeyPairName' outputs.json)
VOL_TOOLS=$(jq -r '.EdaStorage.VolToolsId' outputs.json)
VOL_WORK=$(jq -r '.EdaStorage.VolWorkId' outputs.json)
VOL_SCRATCH=$(jq -r '.EdaStorage.VolScratchId' outputs.json)

sed \
  -e "s|\${BASE.PrimarySubnetId}|${SUBNET_ID}|g" \
  -e "s|\${BASE.SgClusterNodesId}|${SG_CLUSTER}|g" \
  -e "s|\${BASE.KeyPairName}|${KEY_PAIR_NAME}|g" \
  -e "s|\${STORAGE.VolToolsId}|${VOL_TOOLS}|g" \
  -e "s|\${STORAGE.VolWorkId}|${VOL_WORK}|g" \
  -e "s|\${STORAGE.VolScratchId}|${VOL_SCRATCH}|g" \
  pcluster-config-template.yaml > pcluster-config.yaml

echo "생성 완료: pcluster-config.yaml"
```

### 2.2 생성된 설정 파일 검증

```bash
# 플레이스홀더가 남아있지 않은지 확인
grep '${' pcluster-config.yaml && echo "ERROR: 치환 안 된 값 있음" || echo "OK: 모든 값 치환됨"

# ParallelCluster config 검증
pcluster configure validate --config pcluster-config.yaml
```

### 2.3 현재 값 확인

```bash
# outputs.json에서 전체 값 확인
jq '.' outputs.json
```

> 수동으로 설정 파일을 만들 필요 없이, `setup.sh`가 이 과정을 자동으로 수행한다.

---

## 3. 클러스터 생성

### 3.1 setup.sh로 일괄 생성 (권장)

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

### 3.2 수동 생성

```bash
pcluster create-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration cdk/pcluster-config.yaml
```

### 3.3 상태 모니터링

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

### 3.4 생성 완료 확인

```bash
pcluster describe-cluster --cluster-name $CLUSTER_NAME \
  --query '{Status:clusterStatus,HeadNode:headNode.privateIpAddress,LoginAddress:loginNodes[0].address}'
```

---

## 4. 클러스터 접속

접속 방법은 **SSH**와 **SSM** 두 가지가 있다.

| 방법 | 필요 조건 | 대상 노드 | 용도 |
|---|---|---|---|
| SSH (VPN 경유) | VPN 연결 + SSH 키페어 | Head Node, Login Node | 일반 작업, 잡 제출 |
| SSM Session Manager | IAM 자격 증명 + `ENABLE_SSM=1`로 클러스터 생성 | Head Node | 관리/디버깅, VPN 없이 접속 |

### 4.1 SSH 접속 (VPN 경유)

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

### 4.2 SSM Session Manager 접속 (VPN/키 불필요)

SSM은 IAM 자격 증명만으로 접속 가능하며, SSH 키나 VPN 연결이 필요 없다.
Head Node에만 접속 가능하다.

**사전 조건**

1. 클러스터 생성 시 `ENABLE_SSM=1` 옵션 사용:
   ```bash
   ENABLE_SSM=1 ./setup.sh
   ```
   이 옵션이 `AmazonSSMManagedInstanceCore` IAM 정책을 Head Node에 추가한다.
   기본값은 비활성화(`ENABLE_SSM=0`)이다.

2. 로컬에 `session-manager-plugin` 설치:
   ```bash
   # macOS
   brew install --cask session-manager-plugin

   # Linux — Debian/Ubuntu
   curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
     -o session-manager-plugin.deb
   sudo dpkg -i session-manager-plugin.deb

   # Linux — RHEL/Amazon Linux
   curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/64bit/session-manager-plugin.rpm" \
     -o session-manager-plugin.rpm
   sudo yum install -y session-manager-plugin.rpm
   ```

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

## 5. 클러스터 검증 테스트

클러스터 생성 후 Login Node에 접속하여 아래 항목을 순서대로 확인한다.

### 5.1 Slurm 상태 확인

```bash
# 파티션/노드 상태
sinfo
# 기대: eda-r8i 파티션, idle~ 상태 (compute node가 아직 안 떠있음)

# 상세 정보
scontrol show partition
```

### 5.2 FSx 마운트 확인

```bash
# 마운트 상태
df -h | grep fsx
# 기대: /fsx/tools, /fsx/work, /fsx/scratch 세 개 모두 보여야 함

# 마운트 옵션 확인 (nconnect, rsize/wsize)
nfsstat -m

# 읽기/쓰기 테스트
ls /fsx/tools/
touch /fsx/work/test_write && rm /fsx/work/test_write
touch /fsx/scratch/test_write && rm /fsx/scratch/test_write
```

### 5.3 메모리 기반 스케줄링 확인

```bash
scontrol show config | grep -i memory
# SelectTypeParameters = CR_Core_Memory 가 보여야 함
```

### 5.4 테스트 잡 제출

Compute Node는 `MinCount: 0`이므로 잡을 제출하면 자동으로 시작된다 (2-5분 소요).

```bash
mkdir -p /fsx/scratch/$USER

cat > /fsx/scratch/$USER/test_job.sh << 'EOF'
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

sbatch /fsx/scratch/$USER/test_job.sh
```

### 5.5 잡 상태 확인 및 결과

```bash
# 잡 상태 확인 (PENDING → CONFIGURING → RUNNING → COMPLETED)
squeue

# 잡 완료 후 결과 확인
sacct --format=JobID,JobName,Partition,State,Elapsed,MaxRSS,NodeList

# 잡 출력 확인
cat /fsx/scratch/$USER/slurm-*.out
```

> PENDING에서 2-5분간 Compute Node가 시작된다. `squeue`로 상태 변화를 확인한다.

---

## 6. 첫 번째 잡 실행

### 6.3 VCS Simulation 잡 예시

```bash
cat > /fsx/scratch/$USER/run_vcs.sh << 'EOF'
#!/bin/bash
#SBATCH --job-name=vcs_smoke
#SBATCH --partition=eda-r8i
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=02:00:00

WORKDIR=/fsx/scratch/$USER/$SLURM_JOB_ID
mkdir -p $WORKDIR && cd $WORKDIR

# Tool setup
source /fsx/tools/eda/env/vcs_setup.sh

# Copy project files
cp -r /fsx/work/projects/chipA/rtl .
cp -r /fsx/work/projects/chipA/tb .
cp /fsx/work/projects/chipA/filelist/smoke.f .

# Run VCS compile + simulation
vcs -full64 -f smoke.f -o simv
./simv +ntb_random_seed=1234

# Save results
RESULT_DIR=/fsx/work/results/chipA/$(date +%Y%m%d)/$SLURM_JOB_ID
mkdir -p $RESULT_DIR
cp -r $WORKDIR/*.log $WORKDIR/*.fsdb $RESULT_DIR/ 2>/dev/null || true

echo "Results saved to: $RESULT_DIR"
EOF

sbatch /fsx/scratch/$USER/run_vcs.sh
```

---

## 7. 운영 관리

### 7.1 클러스터 업데이트

설정 변경 시:
```bash
pcluster update-cluster \
  --cluster-name $CLUSTER_NAME \
  --cluster-configuration pcluster-config.yaml
```

### 7.2 클러스터 중지/시작

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

### 7.3 클러스터 삭제

```bash
pcluster delete-cluster --cluster-name $CLUSTER_NAME
# FSx, VPC 등 CDK 리소스는 영향 없음
```

### 7.4 CloudWatch 알람 이메일 구독

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

## 8. Day 1 체크리스트

클러스터 생성 후 첫 2-3일 내에 확인할 항목:

- [ ] 섹션 5의 검증 테스트 수행 (Slurm, FSx, 테스트 잡)
- [ ] compute node 자동 시작/종료 확인 (ScaledownIdletime=15분)
- [ ] Login Node SSH 접속 확인
- [ ] VCS compile + simulation 동작 확인
- [ ] `/fsx/scratch/$USER/$SLURM_JOB_ID` 작업 디렉터리 패턴 확인
- [ ] CloudWatch FSx 메트릭 확인 (콘솔에서 AWS/FSx 네임스페이스)
- [ ] 알람 이메일 구독 설정
- [ ] DCV 인스턴스 배포 및 접속 확인 (GUI 작업이 필요한 경우)
- [ ] CloudTrail 이벤트 기록 확인
- [ ] VPC Flow Logs 기록 확인

---

## 9. 트러블슈팅

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

## 10. VPC Flow Logs

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

## 11. CloudTrail (감사 로그)

CDK 배포 시 CloudTrail이 자동 활성화된다. 모든 AWS API 호출이 S3에 기록된다.

### 11.1 구성 요약

| 항목 | 설정 |
|---|---|
| Trail 이름 | `eda-trail` |
| 로그 저장 | `s3://eda-cloudtrail-<account-id>/` |
| 암호화 | KMS (`eda/cloudtrail`) |
| 대상 이벤트 | 관리 이벤트 (읽기+쓰기) |
| S3 라이프사이클 | 90일 → Glacier, 365일 → 삭제 |
| 비용 | 관리 이벤트 1개 trail 무료, S3 저장 비용 미미 |

### 11.2 로그 확인

```bash
# 최근 이벤트 확인 (CloudTrail 콘솔 대신 CLI)
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventSource,AttributeValue=ec2.amazonaws.com \
  --max-results 10 \
  --query 'Events[].{Time:EventTime,Name:EventName,User:Username}'
```

### 11.3 Athena로 쿼리

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

## 12. DCV 원격 데스크톱

EDA GUI 작업(Verdi, DVE 등)을 위한 사용자별 DCV 인스턴스를 별도 CDK 프로젝트(`cdk-dcv/`)로 관리한다.
Login Node(SSH 잡 제출용)와 DCV 인스턴스(GUI 작업용)는 분리하는 것이 일반적이다.

### 12.1 구조

```
cdk/       → VPC, FSx, SG → SSM Parameter Store (/eda/network/*, /eda/storage/*)
cdk-dcv/   → SSM에서 읽어 DCV EC2 인스턴스 생성 (사용자별 독립 스택)
```

### 12.2 DCV 인스턴스 배포

```bash
cd cdk-dcv/

# 사용자별 DCV 인스턴스 생성
./deploy-dcv.sh alice
./deploy-dcv.sh bob
./deploy-dcv.sh alice r7i.4xlarge   # 인스턴스 타입 지정 (기본: r7i.2xlarge)
```

배포 시 자동으로:
- 메인 CDK의 VPC/Subnet/SG를 SSM에서 참조
- FSx 볼륨 (`/fsx/tools`, `/fsx/work`, `/fsx/scratch`) NFS 마운트
- DCV 서버 설치 + GUI 데스크톱 구성
- SSM Parameter에 인스턴스 ID 기록 (`/eda/dcv/<username>/InstanceId`)
- EC2 태그에 `User: <username>` 설정

### 12.3 DCV 접속

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

### 12.4 DCV 인스턴스 관리

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

### 12.5 DCV 인스턴스 삭제

```bash
./destroy-dcv.sh alice
```

---

## 13. CDK 설정 (cdk.json)

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

## 14. SSM Parameter Store 구조

메인 CDK가 배포하는 SSM 파라미터 (DCV 등 외부 프로젝트에서 참조):

| 파라미터 | 소스 | 용도 |
|---|---|---|
| `/eda/network/VpcId` | {prefix}Base | VPC ID |
| `/eda/network/PrimarySubnetId` | {prefix}Base | Private Subnet ID |
| `/eda/network/PrimaryAz` | {prefix}Base | Subnet AZ |
| `/eda/network/SgClusterNodesId` | {prefix}Base | 클러스터 노드 SG |
| `/eda/network/KeyPairName` | {prefix}Base | SSH 키페어 이름 |
| `/eda/storage/FsxDns` | EdaStorage | FSx DNS 엔드포인트 |
| `/eda/storage/VolToolsId` | EdaStorage | /fsx/tools 볼륨 ID |
| `/eda/storage/VolWorkId` | EdaStorage | /fsx/work 볼륨 ID |
| `/eda/storage/VolScratchId` | EdaStorage | /fsx/scratch 볼륨 ID |
| `/eda/dcv/<user>/InstanceId` | cdk-dcv | DCV 인스턴스 ID (사용자별) |

```bash
# 전체 파라미터 조회
aws ssm get-parameters-by-path --path /eda/ --recursive \
  --query 'Parameters[].{Name:Name,Value:Value}' --output table
```

---

## 15. 클린 재설치

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

## 16. 비용 관리 팁

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
