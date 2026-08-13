#!/usr/bin/env python3
"""Independent CDK entry point for the AWS PCS deployment model."""

import os

import aws_cdk as cdk

from pcs.config import PcsConfig
from pcs.stack import PcsStack


app = cdk.App()
config = PcsConfig.from_app(app)

region = (
    os.getenv("AWS_REGION")
    or os.getenv("AWS_DEFAULT_REGION")
    or os.getenv("CDK_DEFAULT_REGION")
)
if not region:
    raise ValueError("Set AWS_REGION or AWS_DEFAULT_REGION before synthesizing")

PcsStack(
    app,
    config.stack_name,
    config=config,
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=region,
    ),
)

app.synth()
