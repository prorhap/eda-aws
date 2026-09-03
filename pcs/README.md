[English](./README.md) | [한국어](./README.ko.md)

# AWS PCS deployment option

This directory provides an independent AWS Parallel Computing Service (PCS)
deployment path without changing the existing AWS ParallelCluster workflow.

## Deployment boundary

| Area | ParallelCluster | AWS PCS option |
|---|---|---|
| Entry point | root `setup.sh` | `pcs/pcs-setup.sh` |
| Cluster definition | `pcluster-config.yaml` | PCS CDK resources |
| Slurm controller | head node in the customer account | AWS-managed control plane |
| Compute | ParallelCluster queues/resources | PCS Compute Node Groups |
| Login | ParallelCluster LoginNodes | fixed PCS CNG not attached to a queue |
| CLI | requires `pcluster` | no `pcluster` dependency |
| AMI | ParallelCluster AMI | PCS-ready AMI with PCS agent and Slurm |

The models are not mixed. A ParallelCluster AMI or configuration YAML is not
reused for PCS.

## Architecture

This project can reuse an existing shared VPC, OpenZFS storage, endpoints, and
license server. `Shared Foundation` is a project term for those pre-existing
CDK stacks, not an AWS PCS requirement. The PCS stack owns:

- `AWS::PCS::Cluster`
- fixed login CNG
- scalable x8aedz compute CNG and queue, sized for an 864 physical-core PoC
- PCS security groups and PrivateLink endpoint
- `AWSPCS-*` instance role/profile
- launch templates with IMDSv2 and encrypted gp3 root volumes
- lifecycle actions for CloudWatch logs and FSxZ/FSxN mounts
- CloudWatch Logs delivery for scheduler and job completion logs
- optional RAID 0 instance-store NVMe lifecycle action for compatible compute types

FSx mount failures terminate the node before `slurmd` accepts work. Existing
FSx for OpenZFS and FSx for ONTAP endpoints can be supplied explicitly, or the
PCS setup can use Shared Foundation CloudFormation Output Exports. The PCS stack
creates no file systems; it only configures CNG lifecycle mounts and NFS ingress.

Lifecycle changes apply to newly launched or rebooted nodes. Scaled compute
nodes pick up the new configuration automatically, while a persistent login
node must be rebooted or replaced after a mount configuration change.

The default `x8aedz.24xlarge` profile uses 96 physical cores, 3 TiB RAM, and
7.6 TB of local NVMe per node; nine nodes provide 864 physical cores. The
compute lifecycle action prepares that NVMe as ephemeral RAID 0
`/local_scratch`.

## AMI

The default `PCS_AMI_ID=ami-07d7716ef5d114faf` is the Seoul-region AWS PCS
sample AMI for AL2023, x86_64, and Slurm 25.11. It is suitable for a general
EDA PoC, but vendor tools and customer runtime libraries must come from FSx or
be added to a custom image.

Production must replace it with a validated golden AMI containing the PCS
agent, compatible Slurm, NFS client, CloudWatch agent, `mdadm`, `xfsprogs`,
the EDA runtime, and required EFA/GPU drivers. Set
`PCS_ALLOW_SAMPLE_AMI=0` to make preflight reject sample AMIs.

## Usage

With a deployed and healthy `Eda` Shared Foundation, PCS needs no additional
network or storage configuration. The PCS control plane, Login CNG, Compute
CNG, and PCS API endpoint automatically use the Foundation subnet. Run:

```bash
./pcs/pcs-setup.sh deploy
```

`PCS_SHARED_STACK_PREFIX=Eda` is the default. It automatically reads VPC,
subnet, Key Pair, FSx, and license settings from Foundation CloudFormation
Output Exports. Existing Foundation stacks without exports are supported through
an Output lookup fallback. It never deploys or modifies Foundation resources.
Create a `PCS_CONFIG` file only when overriding PCS-specific choices such as the
cluster name, AMI, or instance types.

For a fully standalone PCS deployment, set `PCS_SHARED_STACK_PREFIX=""` and
provide the direct values that are enabled: `PCS_VPC_ID`, `PCS_SUBNET_ID`,
`PCS_KEY_PAIR_NAME` for SSH, FSx DNS and security group IDs for mounts, and
either all `PCS_LICENSE_*` values or `PCS_ENABLE_LICENSE_ACCESS=0`.

Set `PCS_CLUSTER_NAME` to a 3-17 character PCS name. The deployment derives
the Login and Compute CNG names, Launch Template names, and EC2 `Name` tags as
`login-<cluster-name>` and `compute-<cluster-name>`. Related resources also
receive `Project`, `DeploymentModel`, and `ClusterName` tags.

PCS accounting is disabled by default. Set `PCS_ENABLE_ACCOUNTING=1` only
when PCS STANDARD accounting and its purge policy are required.

When an external OpenZFS or ONTAP DNS name is provided without a security
group ID, `pcs-setup.sh` discovers the file system ENIs and uses their single
attached security group. Discovery fails closed when multiple security groups
are attached, requiring an explicit `PCS_*_SECURITY_GROUP_ID`.

Login SSH defaults to `PCS_SSH_CIDR=0.0.0.0/0` because this project assumes
private subnets without public IPs and access only through the closed VPC/VPN
network. Restrict it to the corporate or VPN CIDR if that assumption changes.
`PCS_ENABLE_SSM=0` is the default because the standard Foundation does not
create SSM interface endpoints. Enable it only after adding SSM connectivity or
service egress.

## PCS Scheduler log delivery

`PCS_ENABLE_CLOUDWATCH_LIFECYCLE_LOGS` controls CloudWatch collection for node
lifecycle actions. It is separate from PCS log delivery, which exports logs
from the AWS-managed Slurm control plane to CloudWatch Logs.

| Variable | Default | CloudWatch Log Group | Purpose |
|---|---:|---|---|
| `PCS_ENABLE_SCHEDULER_LOG_DELIVERY` | `1` | `/aws/pcs/<cluster>/<cluster-id>/scheduler` | Scheduler operation and failure logs |
| `PCS_ENABLE_JOB_COMPLETION_LOG_DELIVERY` | `1` | `/aws/pcs/<cluster>/<cluster-id>/job-completion` | Completed-job state, requested/allocated resources, and exit details |
| `PCS_ENABLE_SCHEDULER_AUDIT_LOG_DELIVERY` | `0` | `/aws/pcs/<cluster>/<cluster-id>/scheduler-audit` | Slurm RPC audit logs for compliance or focused investigation |
| `PCS_LOG_RETENTION_DAYS` | `30` | All groups above | CloudWatch Logs retention |

The default enables Scheduler and Job Completion logs for operational
visibility. Scheduler Audit logs are opt-in because, with Slurm `25.11`, their
volume can approach 90% of scheduler logs. Enable audit only when a compliance
or investigation requirement justifies the additional volume and cost.

Log group paths and DeliverySource/DeliveryDestination names include the
unique `cluster-id` generated by AWS PCS. Recreating a PCS cluster with the
same cluster name therefore does not collide with retained log resources, and
historical log groups remain distinguishable by cluster ID. Log groups are
retained when the PCS stack is replaced or deleted. Stored events still expire
after `PCS_LOG_RETENTION_DAYS`, so increase that value when longer history is
required.

These changes add or remove log delivery resources only. They do not replace
the PCS cluster or compute node groups. Review with `./pcs/pcs-setup.sh diff`
before applying with `./pcs/pcs-setup.sh deploy`.

Regular PCS actions never modify the shared CDK stacks. Use
`foundation-diff` and `foundation-deploy` explicitly, with scope `base` for an
existing environment or confirmed scope `all` for a new foundation. This path
never installs or invokes the `pcluster` CLI.

Only `foundation-*` actions require the root `CONFIG` profile. Regular PCS
actions require `PCS_CONFIG` only, whether they reuse Foundation resources or
use standalone direct inputs.

See [README.ko.md](./README.ko.md) for the complete configuration, validation,
and migration strategy.
