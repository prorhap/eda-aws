[English](./README.md) | [한국어](./README.ko.md)

# EDA on AWS

A project for building an EDA (simulation/regression) environment on AWS using
ParallelCluster + FSx OpenZFS. (Default region: `ap-northeast-2` — configurable
via `config/default.env`)

---

## 1. Overview

- **CDK (Python 3.12, aws-cdk-lib 2.x)**: Deploys VPC import, security groups,
  FSx OpenZFS/ONTAP, EDA license server, VPC endpoints, CloudTrail, and more
- **ParallelCluster 3.15.x**: Deploys Slurm head node + compute fleet on top of
  the resources created by CDK
- **VPC**: Reuses an existing VPC/private subnet (assumes a site-to-site VPN
  environment)
- **Detailed design docs**: [`architecture_guide.md`](architecture_guide.md) /
  [`parallelcluster_guide.md`](parallelcluster_guide.md)

### CloudFormation stacks deployed

Stack names are based on a prefix. Default `STACK_PREFIX=Eda`.

| Stack | Contents |
|---|---|
| `{prefix}Base` | Security Groups (cluster/FSx/ONTAP + VPC endpoint SG), EC2 KeyPair, CloudTrail + KMS + S3, **VPC Endpoints** (logs/cloudformation/ec2 + s3/dynamodb + elasticloadbalancing/autoscaling when needed) |
| `{prefix}Storage` | FSx OpenZFS (+ `fsxz_tools`, `fsxz_work`, `fsxz_scratch` volumes) or FSx ONTAP |
| `{prefix}LicenseServer` | EC2 + static ENI for the EDA license server (MAC address persistence) |
| `hpc-cluster` | ParallelCluster (Slurm) stack (created by the pcluster CLI) |

By setting `STACK_PREFIX` differently, multiple environments (e.g., `EdaDev`,
`EdaProd`) can coexist in the same account.

---

## 2. Prerequisites

1. AWS credentials: `aws configure` (or SSO) with `ap-northeast-2` ready to use
2. Local tools: `python3`, `node`, `npm`, `jq`
3. Existing VPC/private subnet: set in `VPC_ID`, `SUBNET_ID` of `config/default.env`
4. (Optional) Even if the private subnet has no `0.0.0.0/0` route, setup.sh
   will automatically create the required endpoints when `ENABLE_VPC_ENDPOINTS=1`

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
| `STACK_PREFIX` | `Eda` | CDK stack prefix. Use a different value to keep multiple environments in one account |
| `VPC_ID` / `SUBNET_ID` | (required) | Existing VPC/private subnet |
| `ENABLE_OPENZFS` / `OPENZFS_SIZE_GIB` / `OPENZFS_THROUGHPUT` | `1` / `320` / `1280` | FSx OpenZFS |
| `ENABLE_ONTAP` / `ONTAP_SIZE_GIB` / `ONTAP_TPUT_PER_HA` / `ONTAP_HA_PAIRS` | `0` / `10240` / `3072` / `1` | FSx NetApp ONTAP |
| `ENABLE_LICENSE_SERVER` / `LICENSE_INSTANCE_TYPE` | `1` / `m7i.large` | EDA license server |
| `ENABLE_LOGIN_NODE` | `1` | 1=ParallelCluster LoginNodes (recommended), 0=on-prem sbatch |
| `ENABLE_VPC_ENDPOINTS` | `1` | Auto-create required endpoints |
| `ENABLE_SSM` | `0` | Allow Session Manager access |
| `SKIP_CDK` / `SKIP_CLUSTER` | `0` | Skip stages |

The quota/reservation of FSx OpenZFS child volumes (tools/work/scratch) is
auto-scaled in proportion to the parent capacity (to avoid the constraint
where a quota larger than the parent is not allowed).

---

## 4. Access

### Login Node (recommended)

Day-to-day user work happens on the Login Node. Connecting via the ALB DNS
distributes connections automatically across nodes in the pool.

```bash
# Look up the Login Node ALB DNS
pcluster describe-cluster --cluster-name <CLUSTER_NAME> --region ap-northeast-2 \
  | jq -r '.loginNodes[0].address'

# Connect (uses the same pem key as the Head Node)
ssh -i ~/.ssh/eda-cluster-key-<ACCOUNT>.pem ec2-user@<LOGIN_NODE_ALB_DNS>
```

**How the Login Node KeyPair works (pcluster 3.15+):**
- The `LoginNodes.Pools[].Ssh.KeyName` parameter has been removed since pcluster 3.15.
- The Login Node EC2 itself does not have a KeyPair attached, but `/home` is
  NFS-mounted from the Head Node, so **the Head Node's
  `~ec2-user/.ssh/authorized_keys` is shared as-is**.
- As a result, the pem registered on the Head Node is also valid on the Login Node.

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

### License server

```bash
ssh -i ~/.ssh/eda-license-key-<ACCOUNT>.pem ec2-user@<LICENSE_IP>

# Environment variable to use on the cluster side
export LM_LICENSE_FILE=27000@<LICENSE_IP>
```

For the License Host ID, use the License Server MAC address printed by
setup.sh when generating `pcluster-config.yaml`.

**SSH port 22 policy**: The License server SG, like the Head Node, allows port
22 from `0.0.0.0/0`. Since it is in a private subnet, it is not reachable from
the internet — only from the corporate network via VPN.

### Direct sbatch from on-prem (without LoginNode)

If `ENABLE_LOGIN_NODE=0`, the on-prem client submits sbatch jobs directly to
the head node. The following conditions must be met:

- Same Slurm version (RHEL 8 recommended)
- Copy `/etc/munge/munge.key` from the head node
- UID/GID identical to cluster users
- TCP 6817 reachable to the head node via VPN

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
5. **Audit logs** — SSH login attempts are recorded in VPC Flow Logs +
   CloudTrail + the head/login node `/var/log/secure`.

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
  **already-existing endpoints**, but excludes endpoints created by this
  stack (tagged `Project=eda-cluster`) from skipping. (If skipped without
  the tag, the endpoint would be missing from the template on redeploy and
  get deleted, causing the HeadNode bootstrap to fail.)
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
├── setup-from-cloudshell.sh    # Deploy script for CloudShell
├── create-cluster.sh           # Recreate the cluster only
├── config/
│   ├── default.env             # Default settings
│   └── example.env             # Example
├── cdk/                        # CDK Python project
│   ├── app.py
│   ├── cdk/                    # Stack modules
│   │   ├── base_stack.py            # {prefix}Base: SG + KeyPair + CloudTrail + VPC endpoints
│   │   ├── storage_stack.py         # {prefix}Storage: FSx OpenZFS / ONTAP
│   │   ├── license_server_stack.py  # {prefix}LicenseServer: EDA license server
│   │   └── slurm_db_stack.py        # (unused option) Slurm accounting RDS
│   ├── pcluster-config-template.yaml
│   └── requirements.txt
├── cdk-dcv/                    # (Optional) DCV-related CDK
├── architecture_guide.md       # Overall architecture design
└── parallelcluster_guide.md    # Practical ParallelCluster guide
```
