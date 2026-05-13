"""Slurm Accounting Database Stack.

RDS MySQL 8.0 (single-AZ, db.t4g.medium) for Slurm accounting.
Secrets Manager secret with plain-text password (ParallelCluster compatible).
"""

from aws_cdk import (
    Stack,
    Tags,
    RemovalPolicy,
    Duration,
    aws_rds as rds,
    aws_ec2 as ec2,
    aws_secretsmanager as secretsmanager,
    CfnOutput,
)
from constructs import Construct


class SlurmDbStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        sg_slurm_db: ec2.ISecurityGroup,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", "eda-cluster")

        # Plain-text password secret — ParallelCluster requires
        # a Secrets Manager secret containing ONLY the password string.
        self.db_password_secret = secretsmanager.Secret(
            self, "SlurmDbPassword",
            description="Slurm accounting DB password (plain text for ParallelCluster)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True,
                password_length=32,
            ),
        )

        self.db = rds.DatabaseInstance(
            self, "SlurmDb",
            engine=rds.DatabaseInstanceEngine.mysql(
                version=rds.MysqlEngineVersion.VER_8_0,
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T4G, ec2.InstanceSize.MEDIUM,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
            security_groups=[sg_slurm_db],
            database_name="slurm_acct",
            credentials=rds.Credentials.from_password(
                username="slurm",
                password=self.db_password_secret.secret_value,
            ),
            allocated_storage=20,
            max_allocated_storage=100,
            storage_type=rds.StorageType.GP3,
            multi_az=False,
            publicly_accessible=False,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
            backup_retention=Duration.days(7),
        )

        # ── Outputs ──────────────────────────────────────────
        CfnOutput(
            self, "SlurmDbEndpoint",
            value=f"{self.db.db_instance_endpoint_address}:{self.db.db_instance_endpoint_port}",
        )
        CfnOutput(
            self, "SlurmDbPasswordSecretArn",
            value=self.db_password_secret.secret_arn,
            description="Use this ARN for ParallelCluster Database.PasswordSecretArn",
        )
