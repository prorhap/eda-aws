"""EDA DCV Instance Stack

Deploys a personal DCV remote desktop instance into the EDA VPC.
Reads VPC/subnet/SG/FSx info from SSM Parameter Store (set by main CDK).
Mounts FSx volumes (/fsx/tools, /fsx/work, /fsx/scratch) via NFS.
"""

from aws_cdk import (
    Stack,
    Tags,
    CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_ssm as ssm,
)
from constructs import Construct


class CdkDcvStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", "eda-cluster")

        # ── Read infra params from SSM ───────────────────────
        vpc_id = ssm.StringParameter.value_from_lookup(self, "/eda/network/VpcId")
        subnet_id = ssm.StringParameter.value_from_lookup(self, "/eda/network/PrimarySubnetId")
        subnet_az = ssm.StringParameter.value_from_lookup(self, "/eda/network/PrimaryAz")
        sg_id = ssm.StringParameter.value_from_lookup(self, "/eda/network/SgClusterNodesId")
        key_pair_name = ssm.StringParameter.value_from_lookup(self, "/eda/network/KeyPairName")
        fsx_dns = ssm.StringParameter.value_from_lookup(self, "/eda/storage/FsxDns")

        # ── Context values ───────────────────────────────────
        username = self.node.try_get_context("eda:dcv_username") or "default-user"
        instance_type = self.node.try_get_context("eda:dcv_instance_type") or "r7i.2xlarge"

        # ── Import existing resources ────────────────────────
        vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=vpc_id)
        subnet = ec2.Subnet.from_subnet_attributes(
            self, "Subnet",
            subnet_id=subnet_id,
            availability_zone=subnet_az,
        )
        cluster_sg = ec2.SecurityGroup.from_security_group_id(
            self, "ClusterSg", sg_id,
        )

        # ── Security Group ───────────────────────────────────
        dcv_sg = ec2.SecurityGroup(
            self, "DcvSg",
            vpc=vpc,
            description="DCV instance - SSH + DCV access",
            allow_all_outbound=True,
        )
        # SSH from VPC
        dcv_sg.add_ingress_rule(
            ec2.Peer.ipv4("10.0.0.0/16"),
            ec2.Port.tcp(22),
            "SSH from VPC",
        )
        # DCV from VPC
        dcv_sg.add_ingress_rule(
            ec2.Peer.ipv4("10.0.0.0/16"),
            ec2.Port.tcp(8443),
            "DCV from VPC",
        )

        # ── IAM Role ─────────────────────────────────────────
        role = iam.Role(
            self, "DcvRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                # DCV license check via S3
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonS3ReadOnlyAccess"
                ),
                # SSM for management
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )

        # ── User Data ────────────────────────────────────────
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -ex",
            "",
            "# Install DCV",
            'rpm --import https://d1uj6qtbmh3dt5.cloudfront.net/NICE-GPG-KEY',
            'wget -q https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-el8-x86_64.tgz',
            'tar -xzf nice-dcv-el8-x86_64.tgz',
            'cd nice-dcv-*-el8-x86_64',
            'yum install -y nice-dcv-server nice-dcv-web-viewer nice-xdcv',
            'cd ..',
            "",
            "# Enable and start DCV",
            'systemctl enable dcvserver',
            'systemctl start dcvserver',
            "",
            "# Install desktop environment",
            'yum groupinstall -y "Server with GUI"',
            'systemctl set-default graphical.target',
            'systemctl isolate graphical.target',
            "",
            f"# Create user: {username}",
            f'useradd -m {username} 2>/dev/null || true',
            f'echo "{username}:changeme123" | chpasswd',
            "",
            "# Mount FSx volumes",
            'yum install -y nfs-utils',
            'mkdir -p /fsx/tools /fsx/work /fsx/scratch',
            f'mount -t nfs -o nconnect=16 {fsx_dns}:/fsx/tools/ /fsx/tools',
            f'mount -t nfs -o nconnect=16 {fsx_dns}:/fsx/work/ /fsx/work',
            f'mount -t nfs -o nconnect=16 {fsx_dns}:/fsx/scratch/ /fsx/scratch',
            "",
            "# Persist mounts in fstab",
            f'echo "{fsx_dns}:/fsx/tools/ /fsx/tools nfs nconnect=16,defaults 0 0" >> /etc/fstab',
            f'echo "{fsx_dns}:/fsx/work/ /fsx/work nfs nconnect=16,defaults 0 0" >> /etc/fstab',
            f'echo "{fsx_dns}:/fsx/scratch/ /fsx/scratch nfs nconnect=16,defaults 0 0" >> /etc/fstab',
            "",
            f"# Create DCV session for {username}",
            f'dcv create-session --owner {username} --type virtual {username}-session',
        )

        # ── EC2 Instance ─────────────────────────────────────
        # Amazon Linux 2023
        ami = ec2.MachineImage.latest_amazon_linux2023(
            cpu_type=ec2.AmazonLinuxCpuType.X86_64,
        )

        instance = ec2.Instance(
            self, "DcvInstance",
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ami,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=[subnet]),
            security_group=dcv_sg,
            key_pair=ec2.KeyPair.from_key_pair_name(
                self, "KeyPair", key_pair_name,
            ),
            role=role,
            user_data=user_data,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        100,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                    ),
                ),
            ],
        )

        # Add cluster SG so instance can access FSx
        instance.add_security_group(cluster_sg)

        Tags.of(instance).add("Name", f"dcv-{username}")
        Tags.of(instance).add("User", username)

        # ── SSM Parameter (for stop/start management) ────────
        ssm.StringParameter(
            self, "SsmInstanceId",
            parameter_name=f"/eda/dcv/{username}/InstanceId",
            string_value=instance.instance_id,
            description=f"DCV instance ID for {username}",
        )

        # ── Outputs ──────────────────────────────────────────
        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "PrivateIp", value=instance.instance_private_ip)
        CfnOutput(
            self, "DcvUrl",
            value=f"https://{instance.instance_private_ip}:8443",
            description="DCV web client URL (via VPN)",
        )
        CfnOutput(
            self, "SshCommand",
            value=f"ssh -i ~/.ssh/{key_pair_name}.pem ec2-user@{instance.instance_private_ip}",
        )
