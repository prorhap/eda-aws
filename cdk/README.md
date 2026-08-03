[English](./README.md) | [한국어](./README.ko.md)

# eda-aws CDK

The CDK Python app for the EDA on AWS project. The root `setup.sh` automates
venv creation → deps install → bootstrap → deploy, so you usually don't need
to touch this directory directly. Refer to this document when managing
stacks individually or changing context.

---

## Stack composition

`app.py` synthesizes the stacks below. Stack names are based on the
`eda:stack_prefix` (default `Eda`).

| Stack | File | Role | Condition |
|---|---|---|---|
| `{prefix}Base` | `cdk/base_stack.py` | VPC import, Security Groups (cluster/FSx/ONTAP + endpoint SG), EC2 KeyPair, CloudTrail + KMS + S3, **VPC Endpoints** (logs/cloudformation/ec2 + s3/dynamodb + optional elb/asg or SSM endpoints) | Always |
| `{prefix}Storage` | `cdk/storage_stack.py` | FSx OpenZFS / FSx NetApp ONTAP + child volumes (tools/work/scratch) + CloudWatch Alarm | `eda:enable_openzfs` or `eda:enable_ontap` |
| `{prefix}LicenseServer` | `cdk/license_server_stack.py` | EC2 + static ENI for the EDA license server (MAC persistence) | Always |

`slurm_db_stack.py` (Slurm accounting RDS) is currently an optional stack
not synthesized in `app.py`.

**Change history (v3.0):**
- Consolidated `EdaNetwork` + `EdaVpcEndpoints` into a single `{prefix}Base`
- Stack prefix is now configurable via the `eda:stack_prefix` context
  (default `Eda`)

---

## Context flags

Pass via `cdk -c key=value` or `cdk.json`. setup.sh forwards
`config/*.env` contents as `-c` flags as-is.

| Key | Default | Type | Description |
|---|---|---|---|
| `eda:stack_prefix` | `Eda` | str | Stack prefix — `{prefix}Base` / `{prefix}Storage` / `{prefix}LicenseServer` |
| `eda:base_stack_name` | `{prefix}Base` | str | Override Base stack name individually |
| `eda:storage_stack_name` | `{prefix}Storage` | str | Override Storage stack name individually |
| `eda:license_stack_name` | `{prefix}LicenseServer` | str | Override License stack name individually |
| `eda:vpc_id` | — (required) | str | Existing VPC ID (e.g., `vpc-xxxx`) |
| `eda:subnet_id` | — (required) | str | Private subnet ID |
| `eda:enable_vpc_endpoints` | `true` | bool | Create VPC endpoints inside the Base stack |
| `eda:enable_login_node` | `true` | bool | Whether to include ELB/ASG endpoints |
| `eda:enable_ssm` | `false` | bool | Whether to include SSM/SSM Messages/EC2 Messages endpoints |
| `eda:enable_openzfs` | `true` | bool | Create FSx OpenZFS |
| `eda:openzfs_size_gib` | `10240` | int | OpenZFS capacity (64 – 524288) |
| `eda:openzfs_throughput` | `2560` | int | MBps: 160·320·640·1280·2560·3840·5120·7680·10240 |
| `eda:enable_ontap` | `false` | bool | Create FSx ONTAP |
| `eda:ontap_size_gib` | `10240` | int | ONTAP capacity (1024 – 1048576) |
| `eda:ontap_tput_per_ha` | `3072` | int | MBps/HA: 1536·3072·6144 |
| `eda:ontap_ha_pairs` | `1` | int | Number of HA pairs (1–12) |
| `eda:license_instance_type` | `m7i.large` | str | License server instance type |
| `eda:license_manager_port` | `27000` | int | License manager port (`lmgrd` for Synopsys) |
| `eda:license_vendor_port` | `27020` | int | Fixed vendor daemon port (`snpslmd` for Synopsys) |

---

## Manual usage

```bash
# Prepare venv (just reactivate if setup.sh already created it)
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

# A specific stack only
cdk deploy EdaStorage -c eda:vpc_id=... -c eda:subnet_id=...

# Destroy (FSx / CloudTrail S3 / KMS use RemovalPolicy.RETAIN — manual deletion needed)
cdk destroy --all -c eda:vpc_id=... -c eda:subnet_id=...
```

Outputs are produced via `cdk deploy --outputs-file outputs.json`. setup.sh
injects these values into the neutral placeholders (`${BASE.*}`,
`${STORAGE.*}`) of `pcluster-config-template.yaml` to generate
`pcluster-config.yaml`.

---

## Implementation notes

- **Non-ASCII not allowed in EC2 description**: The `Description` field of
  `AWS::EC2::NetworkInterface` accepts ASCII only. Don't use em-dash (`—`)
  etc.
- **FSx OpenZFS child volume quota**: Each child volume's
  `storage_capacity_quota_gib` must be ≤ parent FS capacity, and the total
  reservation must be ≤ parent capacity. `storage_stack.py` scales tools≈10%
  / work≈40% / scratch≈40% in proportion to the parent capacity.
- **VPC endpoint dedup**:
  `_create_vpc_endpoints_if_enabled` in `base_stack.py` queries existing
  endpoints with boto3 at synth time and skips reusable external endpoints.
  Endpoint ownership is determined from the current CloudFormation stack's
  physical resource IDs rather than tags. This keeps stack-managed endpoints
  in the template without recreating endpoints owned by another stack.
- **VPC endpoint validation**: Reused endpoints are checked for state, type,
  private DNS, security-group HTTPS access, and Gateway route-table
  association. The selected single-subnet AZ is checked against each
  Interface endpoint service before synthesis.
- **RemovalPolicy.RETAIN**: FSx file systems/volumes, the CloudTrail bucket,
  and the KMS key retain for data preservation. Delete manually after stack
  deletion if needed.
- **Stack prefix isolation**: `eda:stack_prefix` scopes physical KeyPair,
  CloudTrail, KMS alias, and SSM Parameter names in addition to stack names.
  The default `Eda` keeps `/eda/...`; `EdaProd` uses `/edaprod/...`. A prefix
  must start with a letter, use only letters, digits, or hyphens, and be at
  most 48 characters.
- **Shared endpoint lifecycle**: VPC endpoints are VPC-scoped. If multiple
  prefixed environments share a VPC, later Base stacks reuse endpoints owned
  by the first Base stack. Keep that owner stack until all dependent
  environments are removed, or use separate VPCs for independent lifecycle.
