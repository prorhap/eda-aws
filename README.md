[English](./README.md) | [한국어](./README.ko.md)

# EDA on AWS

A project for building an EDA simulation/regression environment with FSx
OpenZFS and Slurm. It supports the existing AWS ParallelCluster workflow and
an independent AWS PCS deployment option. Default region: `ap-northeast-2`.

---

## 1. Overview

- **CDK (Python 3.12, aws-cdk-lib 2.x)**: Deploys VPC import, security groups,
  FSx OpenZFS/ONTAP, EDA license server, VPC endpoints, CloudTrail, and more
- **ParallelCluster 3.15.x**: Deploys Slurm head node + compute fleet on top of
  the resources created by CDK
- **AWS PCS option**: Deploys an AWS-managed Slurm control plane and PCS
  Compute Node Groups
- **VPC**: Reuses an existing VPC/private subnet (assumes a site-to-site VPN
  environment)
- **Detailed design docs**: [`architecture_guide.md`](architecture_guide.md) /
  [`parallel_cluster_configuration.md`](parallel_cluster_configuration.md)
- **Independent PCS guide**: [`pcs/README.md`](pcs/README.md)

### CloudFormation stacks deployed

Stack names are based on a prefix. Default `STACK_PREFIX=Eda`.

| Stack | Contents |
|---|---|
| `{prefix}Base` | Security Groups (cluster/FSx/ONTAP + VPC endpoint SG), EC2 KeyPair, CloudTrail + KMS + S3, **VPC Endpoints** (logs/cloudformation/ec2 + s3/dynamodb + elasticloadbalancing/autoscaling when needed) |
| `{prefix}Storage` | FSx OpenZFS (+ `fsxz_tools`, `fsxz_work`, `fsxz_scratch` volumes) or FSx ONTAP |
| `{prefix}LicenseServer` | Always-deployed EC2 + static ENI for the EDA license server (MAC address persistence) |
| `hpc-cluster` | ParallelCluster (Slurm) stack (created by the pcluster CLI) |
| `{prefix}Pcs` | Optional AWS PCS cluster, login/compute CNGs, and queue |

Use `setup.sh` for ParallelCluster and `pcs/pcs-setup.sh` for PCS. The two paths
do not share cluster configuration, AMIs, CLIs, or lifecycle ownership.

For multiple environments in one account, set a unique `STACK_PREFIX` and
either ParallelCluster `CLUSTER_NAME` or PCS `PCS_CLUSTER_NAME` for each.
Physical names and SSM paths are prefix-scoped.
`STACK_PREFIX` must start with a letter, contain only letters, digits, or
hyphens, and be at most 48 characters.

---

## 2. Prerequisites

### 2.1 AWS credentials

```bash
# Configure credentials
aws configure       # or: aws sso login (IAM Identity Center)

# Verify account and region
aws sts get-caller-identity
aws configure get region   # must match REGION in config/default.env

# If region is wrong, set it
aws configure set region ap-northeast-2
```

The deployment identity must be able to inspect the existing network and stack
state. Preflight requires `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`,
`ec2:DescribeVpcEndpoints`, `ec2:DescribeVpcEndpointServices`,
`ec2:DescribeSecurityGroups`, `ec2:DescribeInstanceTypeOfferings`,
`ec2:DescribeVpcAttribute`, `cloudformation:ListStacks`, and
`cloudformation:ListStackResources`, and `servicequotas:GetServiceQuota`, in
addition to the permissions required to deploy the stacks.

### 2.2 Install required tools

The following must be installed **before** running `setup.sh`.
The CDK CLI and pcluster CLI are installed automatically by `setup.sh`.
pcluster is isolated in the project's `.pcluster-venv` to prevent Python
dependency conflicts.

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

**python3** (3.9 or later)

```bash
# macOS
brew install python@3.12

# Linux — Debian/Ubuntu
sudo apt-get install -y python3 python3-pip python3-venv

# Linux — RHEL/Amazon Linux
sudo yum install -y python3

python3 --version
```

**Node.js 22+ and npm** (required for CDK CLI, which setup.sh installs automatically)

```bash
# macOS / Linux — install LTS from https://nodejs.org/ or via nvm
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

**SSM Session Manager Plugin** — only required when using `ENABLE_SSM=1`

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

Set your existing VPC and private subnet IDs in `config/default.env`, or pass them as environment variables:

```bash
VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh
```

Even if the private subnet has no `0.0.0.0/0` route, `setup.sh` automatically
creates the required VPC endpoints when `ENABLE_VPC_ENDPOINTS=1` (the default).

---

## 3. Deployment

```bash
# Use the default settings (config/default.env)
./setup.sh

# Use a different config file
CONFIG=config/prod.env ./setup.sh

# Override only specific values
VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh
```

setup.sh stages:

1. Pre-validation (AWS credentials, required tools, subnet connectivity)
2. CDK Python deps installation
3. pcluster CLI installation
4. CDK bootstrap + stack deployment (`{prefix}Base` → storage / license in parallel)
5. Auto-generate `pcluster-config.yaml` + download SSH key (`~/.ssh/*.pem`)
6. Run `pcluster create-cluster` and monitor until completion (10–15 min)

### Key config flags (`config/default.env`)

| Variable | Default | Meaning |
|---|---|---|
| `REGION` | `ap-northeast-2` | Deployment region |
| `STACK_PREFIX` | `Eda` | Prefix for CDK stacks, physical resources, and SSM paths |
| `VPC_ID` / `SUBNET_ID` | (required) | Existing VPC/private subnet |
| `ENABLE_OPENZFS` / `OPENZFS_SIZE_GIB` / `OPENZFS_THROUGHPUT` / `OPENZFS_IOPS` | `1` / `32768` / `7680` / `300000` | High-performance FSx OpenZFS baseline |
| `ENABLE_ONTAP` / `ONTAP_SIZE_GIB` / `ONTAP_TPUT_PER_HA` / `ONTAP_HA_PAIRS` | `0` / `10240` / `3072` / `1` | FSx NetApp ONTAP |
| `LICENSE_INSTANCE_TYPE` | `m7i.large` | Mandatory EDA license server instance |
| `LICENSE_MANAGER_PORT` / `LICENSE_VENDOR_PORT` | `27000` / `27020` | Synopsys `lmgrd` / `snpslmd` defaults |
| `ENABLE_LOGIN_NODE` | `1` | 1=ParallelCluster LoginNodes (recommended) |
| `ENABLE_DCV` / `DCV_ALLOWED_IPS` | `0` / (required CIDR) | Enable DCV, select a `g6.4xlarge` Login Node, and restrict its source network |
| `ENABLE_VPC_ENDPOINTS` | `1` | Auto-create required endpoints |
| `ENABLE_SSM` | `0` | Allow Session Manager access |
| `SKIP_CDK` / `SKIP_CLUSTER` | `0` | Skip stages |

The quota/reservation of FSx OpenZFS child volumes (tools/work/scratch) is
auto-scaled in proportion to the parent capacity (to avoid the constraint
where a quota larger than the parent is not allowed).

The default OpenZFS throughput and IOPS values are one tier below the maximum.
The setup preflight checks the configured quota values. Before deployment, also
verify that aggregate usage including existing OpenZFS file systems remains
within the regional quotas.

---

## 4. Access

Activate the project-local environment before running pcluster commands
manually:

```bash
source .pcluster-venv/bin/activate
```

### Login Node (recommended)

Day-to-day user work happens on the Login Node. Connecting via the NLB DNS
distributes connections automatically across nodes in the pool.

```bash
# Look up the Login Node NLB DNS
pcluster describe-cluster --cluster-name <CLUSTER_NAME> --region ap-northeast-2 \
  | jq -r '.loginNodes[0].address'

# Connect (uses the same pem key as the Head Node)
ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem ec2-user@<LOGIN_NODE_NLB_DNS>
```

**How the Login Node KeyPair works (pcluster 3.15+):**
- The `LoginNodes.Pools[].Ssh.KeyName` parameter has been removed since pcluster 3.15.
- The Login Node EC2 itself does not have a KeyPair attached, but `/home` is
  NFS-mounted from the Head Node, so **the Head Node's
  `~ec2-user/.ssh/authorized_keys` is shared as-is**.
- As a result, the pem registered on the Head Node is also valid on the Login Node.

### Login Node DCV (optional)

This project uses ParallelCluster-managed DCV on the Login Node instead of a
separate DCV EC2 stack. It does not download packages from the internet, and
license checks use the existing S3 Gateway endpoint. When DCV is enabled,
`setup.sh` selects `g6.4xlarge` (one NVIDIA L4 GPU); otherwise the Login Node
remains `r7i.2xlarge`.

```bash
ENABLE_DCV=1 DCV_ALLOWED_IPS=172.16.4.0/24 \
  VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh

# Find an actual Login Node private IP, then connect
LOGIN_IP=$(.pcluster-venv/bin/pcluster describe-cluster-instances \
  --cluster-name <CLUSTER_NAME> --node-type LoginNode \
  --region ap-northeast-2 --query 'instances[0].privateIpAddress' | jq -r .)

.pcluster-venv/bin/pcluster dcv-connect --cluster-name <CLUSTER_NAME> \
  --login-node-ip "$LOGIN_IP" \
  --key-path ~/.ssh/<KEY_PAIR_NAME>.pem \
  --region ap-northeast-2
```

### Head Node (administration)

```bash
# Direct ssh
ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem ec2-user@<HEAD_NODE_IP>

# Or pcluster CLI (key must be specified — refuses if default id_rsa doesn't match)
pcluster ssh --cluster-name <CLUSTER_NAME> --region ap-northeast-2 \
  -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem
```

The Head Node is a management node where the Slurm controller runs, so it is
recommended to do daily work on the Login Node.

### End-to-end Slurm smoke test

After deployment, run `examples/hello-slurm/submit.sh` from the project root
on the local workstation to validate the VPN, Login Node, Slurm, Compute Node,
FSx working path, and result download in one flow. This example does not use
an EDA tool or floating license. Run the following in the shell where you
activated `.pcluster-venv` in the preceding section.

```bash
CLUSTER_NAME=hpc-cluster
REGION=ap-northeast-2
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
LOGIN_ADDR=$(pcluster describe-cluster --cluster-name "$CLUSTER_NAME" \
  --region "$REGION" --query 'loginNodes[0].address' --output text)

REMOTE_HOST="$LOGIN_ADDR" \
SSH_KEY="$HOME/.ssh/eda-cluster-key-${ACCOUNT_ID}.pem" \
./examples/hello-slurm/submit.sh
```

The command uses the default KeyPair name. If you changed the KeyPair name,
set `SSH_KEY` to the pem path printed by `setup.sh`. On success, the script
prints `Slurm smoke test passed` and downloads output
to `examples/hello-slurm/results/hello-<JOB_ID>/`. Run it from a VPN-connected
local workstation with `ssh` and `rsync` installed.

### License server

The License Server stack is always deployed. The defaults model a Synopsys
SCL/FlexNet floating license server with `lmgrd` on TCP 27000 and a fixed
`snpslmd` vendor daemon on TCP 27020. Override the two port settings for another
vendor and make the license file use the same values.

```bash
ssh -i ~/.ssh/eda-license-key-<ACCOUNT>.pem ec2-user@<LICENSE_IP>

# Use the configured manager port (default: 27000)
export LM_LICENSE_FILE=<LICENSE_MANAGER_PORT>@<LICENSE_IP>
```

For the License Host ID, use the License Server MAC address printed by
setup.sh when generating `pcluster-config.yaml`.

**SSH port 22 policy**: The License server SG, like the Head Node, allows port
22 from `0.0.0.0/0`. Since it is in a private subnet, it is not reachable from
the internet — only from the corporate network via VPN.

### SSH key management notes

- `setup.sh` **re-downloads the pem from SSM Parameter Store every time**
  (always overwrites). This prevents the local pem from going stale when the
  cluster is recreated and a new KeyPair is generated.
- If you manage the pem manually and get a `Permission denied` error, the
  fingerprint of the local pem may differ from the current AWS KeyPair
  fingerprint:
  ```bash
  # AWS-side fingerprint
  aws ec2 describe-key-pairs --region ap-northeast-2 \
    --key-names eda-cluster-key-<ACCOUNT> --query 'KeyPairs[0].KeyFingerprint'
  # Local pem fingerprint (RSA uses md5)
  openssl pkcs8 -in ~/.ssh/eda-cluster-key-<ACCOUNT>.pem -nocrypt -topk8 -outform DER \
    | openssl sha1 -c
  ```
  If the two values differ, delete the pem and re-run setup.sh.

---

## 5. Data upload (rsync over SSH)

RTL, testbench, and project files are uploaded directly from your local
workstation to FSx (`/fsxz/...`) via **`rsync` over SSH**, without a separate
S3. No additional infrastructure or buckets are required, and it is also
secure (see below).

### 5.1 Basic usage

```bash
# Local → login/head node → FSx work volume
rsync -az --delete --progress \
  -e "ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem" \
  ./my-rtl-project/ \
  ec2-user@<HEAD_OR_LOGIN_NODE>:/fsxz/work/<user>/my-rtl-project/

# Or pull results back
rsync -az --progress \
  -e "ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem" \
  ec2-user@<HEAD_OR_LOGIN_NODE>:/fsxz/work/<user>/my-rtl-project/results/ \
  ./results/
```

Key options:
- `-a` (archive): preserve permissions/timestamps/symlinks
- `-z`: compress in transit (effective on slow links)
- `--delete`: delete files on the remote that were removed locally (full mirror)
- `--progress`: show progress
- `--exclude='*.o' --exclude='build/'`: exclude unneeded files

### 5.2 Is it secure?

**Yes.** Reasons:

1. **Encrypted in transit** — rsync rides an SSH tunnel via `-e ssh`, so all
   traffic is TLS-grade encrypted (typically AES-256-GCM or
   ChaCha20-Poly1305). Different from the plaintext `rsync://` protocol.
2. **Private network path** — the current VPC is configured with a private
   subnet + VPN, so there is no internet exposure. local ↔ VPN ↔ AWS VPC ↔
   head/login node — all on private paths.
3. **Authentication** — only accessible with the EC2 KeyPair private key
   (`~/.ssh/eda-cluster-key-*.pem`). The pem file is stored under `~/.ssh`
   with `chmod 400`. It is also stored KMS-encrypted in SSM Parameter Store.
4. **Encryption at rest** — FSx OpenZFS is encrypted at rest with KMS by
   default (`OpenZfsKey` in the `EdaStorage` stack). Safe even in a physical
   disk theft scenario.
5. **Audit logs** — CloudTrail records AWS API activity, and the head/login node
   records SSH authentication in `/var/log/secure`. Enable VPC Flow Logs
   separately when network-flow auditing is required.

**Caveats:**
- Manage the pem key carefully — do not commit to Git (`*.pem` is in
  `.gitignore`); do not share with others. If lost, immediately delete the
  KeyPair and redeploy the cluster.
- Don't permanently set `StrictHostKeyChecking=no` in your local SSH config
  (it weakens MITM defense). Use `accept-new` only on first connection to
  register the host key.

### 5.3 Tips for large / repeated uploads

```bash
# Dry run (preview what will be transferred)
rsync -az --dry-run --itemize-changes ...

# Bandwidth limit (avoid saturating the link, e.g., 10 MB/s)
rsync -az --bwlimit=10000 ...

# Reuse .gitignore patterns (skip files git doesn't track)
rsync -az --exclude-from=.gitignore ...

# Speed up by reusing an SSH master connection
# Add to ~/.ssh/config:
#   Host 10.0.*.*
#     ControlMaster auto
#     ControlPath ~/.ssh/cm-%r@%h:%p
#     ControlPersist 10m
```

---

## 6. Teardown

```bash
# 1. Cluster
pcluster delete-cluster --cluster-name hpc-cluster --region ap-northeast-2

# 2. CDK stacks (FSx/CloudTrail/KMS use RemovalPolicy.RETAIN — manual deletion required)
cd cdk
cdk destroy --all -c eda:vpc_id=$VPC_ID -c eda:subnet_id=$SUBNET_ID
```

The FSx file system, CloudTrail S3 bucket, and KMS keys are set to retain to
prevent accidental loss, so delete them via the console / CLI separately if
needed.

---

## 7. Notes for redeployment

- The VPC endpoint logic in the `{prefix}Base` stack automatically skips
  reusable **already-existing endpoints**. It identifies endpoints owned by
  the current stack using CloudFormation physical resource IDs, not tags, so
  endpoints from older or differently prefixed stacks are never recreated.
- Reused Interface endpoints must be available, have private DNS enabled, and
  allow HTTPS from the selected subnet CIDR. Reused Gateway endpoints must be
  available and associated with the selected subnet's route table.
- VPC endpoints are VPC-scoped shared infrastructure. When multiple prefixed
  environments use the same VPC, the first Base stack that created an endpoint
  owns it and later stacks reuse it. Do not delete that owner Base stack while
  another environment still depends on its endpoints; use a dedicated VPC per
  environment when independent lifecycle is required.
- The selected single subnet's Availability Zone must support every Interface
  endpoint that the stack needs to create.
- If an initial Base, Storage, or License stack creation left a
  `ROLLBACK_COMPLETE` or `CREATE_FAILED` stack, `setup.sh` deletes that failed
  stack and recreates it. Previously usable stacks are never auto-deleted.
- A failed initial ParallelCluster is also deleted and recreated with the same
  name after deletion completes.
- If `cdk.context.json` caches a different VPC, setup.sh automatically backs
  it up and deletes it.
- When migrating from an older version (the two-stack structure of
  `EdaNetwork`, `EdaVpcEndpoints`), first `cdk destroy` the existing stacks
  or delete them from the console, then deploy fresh (the new structure
  consolidates them into a single `{prefix}Base`).

---

## 8. Directory structure

```
eda-aws/
├── setup.sh                    # Local one-click deploy script
├── create-cluster.sh           # Recreate the cluster only
├── config/
│   ├── default.env             # Default settings
│   └── example.env             # Example
├── cdk/                        # CDK Python project
│   ├── app.py
│   ├── cdk/                    # Stack modules
│   │   ├── base_stack.py            # {prefix}Base: SG + KeyPair + CloudTrail + VPC endpoints
│   │   ├── storage_stack.py         # {prefix}Storage: FSx OpenZFS / ONTAP
│   │   └── license_server_stack.py  # {prefix}LicenseServer: EDA license server
│   ├── pcluster-config-template.yaml
│   └── requirements.txt
├── pcs/                        # Independent AWS PCS CDK and deployment path
│   ├── pcs-setup.sh
│   ├── pcs/stack.py
│   ├── scripts/preflight.py
│   └── README.md
├── architecture_guide.md       # Overall architecture design
└── parallel_cluster_configuration.md   # ParallelCluster environment and configuration guide
```
