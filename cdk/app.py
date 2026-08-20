#!/usr/bin/env python3
"""EDA on AWS — CDK App entry point (v3.0)

Stacks (with default prefix "Eda"):
  1. {prefix}Base           — SG, KeyPair, CloudTrail, VPC Endpoints (기본 네트워크 + 엔드포인트 통합)
                              (기존 VPC/subnet을 import — eda:vpc_id / eda:subnet_id 필요)
  2. {prefix}Storage        — FSx for OpenZFS / FSx for NetApp ONTAP (옵션)
  3. {prefix}LicenseServer  — EDA 라이센스 서버 EC2 (항상 생성)

스택 접두사(prefix)는 컨텍스트 `eda:stack_prefix`로 변경 가능 (기본: "Eda").
예: -c eda:stack_prefix=MyEda  → MyEdaBase, MyEdaStorage, MyEdaLicenseServer

Context flags (cdk -c 또는 cdk.json):
  # Prefix
  eda:stack_prefix            str  (default: "Eda")
  # Network / endpoints
  eda:enable_vpc_endpoints    bool (default: true)
  eda:enable_login_node       bool (default: true)  — ELB/ASG endpoint 생성 여부에 영향
  eda:enable_ssm              bool (default: false) — SSM endpoint 생성 여부에 영향
  # Storage
  eda:enable_openzfs          bool (default: true)
  eda:enable_ontap            bool (default: false)
  eda:openzfs_size_gib        int  (default: 32768)
  eda:openzfs_throughput      int  (default: 7680 MBps)
  eda:openzfs_iops            int  (default: 300000)
  eda:ontap_size_gib          int  (default: 10240)
  eda:ontap_tput_per_ha       int  (default: 3072 MBps, valid: 1536|3072|6144)
  eda:ontap_ha_pairs          int  (default: 1, 1-12)
  # License server
  eda:license_instance_type   str  (default: m7i.large)
  eda:license_manager_port    int  (default: 27000)
  eda:license_vendor_port     int  (default: 27020)

Stack name override (각 스택 이름을 개별로 바꾸고 싶을 때):
  eda:base_stack_name         str  (default: "{prefix}Base")
  eda:storage_stack_name      str  (default: "{prefix}Storage")
  eda:license_stack_name      str  (default: "{prefix}LicenseServer")

After deployment, use the CfnOutputs to fill in the ParallelCluster config.
See pcluster/pcluster-config-template.yaml at the repository root.
"""

import os
import aws_cdk as cdk

from cdk.base_stack import BaseStack
from cdk.storage_stack import StorageStack
from cdk.license_server_stack import LicenseServerStack


app = cdk.App()

deployment_region = (
    os.getenv("AWS_REGION")
    or os.getenv("AWS_DEFAULT_REGION")
    or os.getenv("CDK_DEFAULT_REGION")
)
if not deployment_region:
    raise ValueError(
        "Deployment region is required. Set AWS_REGION or AWS_DEFAULT_REGION."
    )

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=deployment_region,
)


def _ctx_bool(key: str, default: bool) -> bool:
    val = app.node.try_get_context(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


prefix = app.node.try_get_context("eda:stack_prefix") or "Eda"

base_stack_name    = app.node.try_get_context("eda:base_stack_name")    or f"{prefix}Base"
storage_stack_name = app.node.try_get_context("eda:storage_stack_name") or f"{prefix}Storage"
license_stack_name = app.node.try_get_context("eda:license_stack_name") or f"{prefix}LicenseServer"

base = BaseStack(app, base_stack_name, env=env)

enable_openzfs = _ctx_bool("eda:enable_openzfs", True)
enable_ontap   = _ctx_bool("eda:enable_ontap", False)

if enable_openzfs or enable_ontap:
    storage = StorageStack(
        app, storage_stack_name,
        vpc=base.vpc,
        sg_fsx=base.sg_fsx,
        sg_ontap=base.sg_ontap,
        primary_subnet=base.primary_subnet,
        env=env,
    )
    storage.add_dependency(base)

license_server = LicenseServerStack(
    app, license_stack_name,
    vpc=base.vpc,
    sg_cluster_nodes=base.sg_cluster_nodes,
    primary_subnet=base.primary_subnet,
    env=env,
)
license_server.add_dependency(base)

app.synth()
