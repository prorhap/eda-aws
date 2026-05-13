#!/usr/bin/env python3
"""EDA DCV Instance — CDK App entry point

Deploys a personal DCV remote desktop instance into the EDA VPC.
VPC/Subnet/SG info is read from SSM Parameter Store (set by main CDK).

Usage:
  cdk deploy -c eda:dcv_username=alice
  cdk deploy -c eda:dcv_username=bob -c eda:dcv_instance_type=r7i.4xlarge
"""

import os
import aws_cdk as cdk

from cdk_dcv.cdk_dcv_stack import CdkDcvStack


app = cdk.App()

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region="ap-northeast-2",
)

username = app.node.try_get_context("eda:dcv_username") or "default-user"

CdkDcvStack(
    app, f"EdaDcv-{username}",
    env=env,
)

app.synth()
