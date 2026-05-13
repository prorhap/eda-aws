"""EDA License Server Stack — v1.0

EDA 라이센스 서버용 EC2 인스턴스 생성.

설계 포인트:
  - ENI를 명시 생성해서 EC2에 attach → instance 교체 시에도 MAC/private IP 보존
    (라이센스가 Host ID = MAC address에 bind되는 경우 재발급 방지)
  - 전용 SSH KeyPair (eda-license-key-{account})
    → Private key는 SSM Parameter Store: /ec2/keypair/{key_pair_id}
  - IAM Role: AmazonSSMManagedInstanceCore + CloudWatchAgentServerPolicy
    (user_data는 없지만 추후 SSM/로그 수집용으로 부여)
  - Security Group:
      Ingress:
        * TCP 22       from VPC CIDR (VPN 경유 SSH)
        * TCP 27000    from sg_cluster_nodes (license manager main port)
        * TCP 27020    from sg_cluster_nodes (vendor daemon port)
          → 라이선스 파일에 vendor 포트를 27020으로 고정하는 것을 전제
  - Root EBS: 30 GiB gp3, KMS 암호화
  - user_data: 없음
  - EBS snapshot: 없음

생성 후 운영자 작업:
  1. scripts/get-license-server-key.sh 로 pem 다운로드
  2. ssh -i ~/.ssh/eda-license-key-<account>.pem ec2-user@<private-ip>
  3. 필요한 32bit 라이브러리 설치:
     sudo dnf -y install glibc.i686 libstdc++.i686 libX11.i686 libXext.i686 \\
                          libXrender.i686 libgcc.i686 ncurses-libs.i686 lsof
  4. 벤더 라이선스 매니저 + 라이선스 파일 배치 + 데몬 기동

필수 context:
  - eda:vpc_id
  - eda:subnet_id
옵션:
  - eda:license_instance_type   (default: m7i.large)
  - eda:license_key_pair_name   (default: eda-license-key-{account})
"""

from aws_cdk import (
    Stack,
    Tags,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_ssm as ssm,
    CfnOutput,
)
from constructs import Construct


class LicenseServerStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        sg_cluster_nodes: ec2.ISecurityGroup,
        primary_subnet: ec2.ISubnet,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", "eda-cluster")
        Tags.of(self).add("Role", "license-server")

        instance_type = (
            self.node.try_get_context("eda:license_instance_type") or "m7i.large"
        )

        # ── Security Group ────────────────────────────────────
        self.sg_license = ec2.SecurityGroup(
            self, "SgLicenseServer",
            vpc=vpc,
            description="EDA license server",
            allow_all_outbound=True,
        )
        # SSH: VPC CIDR (VPN 경유 on-prem → 프라이빗 서브넷)
        # SSH 22: 0.0.0.0/0 (HeadNode의 pcluster-managed SG와 동일 정책).
        # Private subnet이라 외부 인터넷에서 실제로 도달할 수 없고, VPN source NAT
        # 유무와 무관하게 사내망 워크스테이션에서도 접속 가능하도록 허용.
        self.sg_license.add_ingress_rule(
            ec2.Peer.ipv4("0.0.0.0/0"),
            ec2.Port.tcp(22),
            "SSH (private subnet — reachable only via VPN)",
        )
        # License manager main + vendor daemon — cluster node에서만
        self.sg_license.add_ingress_rule(
            sg_cluster_nodes,
            ec2.Port.tcp(27000),
            "License manager main port from cluster",
        )
        self.sg_license.add_ingress_rule(
            sg_cluster_nodes,
            ec2.Port.tcp(27020),
            "License vendor daemon port from cluster",
        )

        # ── SSH Key Pair (전용) ───────────────────────────────
        key_pair_name = (
            self.node.try_get_context("eda:license_key_pair_name")
            or f"eda-license-key-{Stack.of(self).account}"
        )
        self.key_pair = ec2.KeyPair(
            self, "LicenseKeyPair",
            key_pair_name=key_pair_name,
            type=ec2.KeyPairType.RSA,
        )

        # ── IAM Role ──────────────────────────────────────────
        role = iam.Role(
            self, "LicenseInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                ),
            ],
        )

        instance_profile = iam.CfnInstanceProfile(
            self, "LicenseInstanceProfile",
            roles=[role.role_name],
        )

        # ── Static ENI (MAC/IP 영속성) ───────────────────────
        # EC2 교체해도 ENI만 detach → 새 instance에 attach 하면 MAC 유지
        self.eni = ec2.CfnNetworkInterface(
            self, "LicenseEni",
            subnet_id=primary_subnet.subnet_id,
            description="EDA license server static ENI (MAC persistence)",
            group_set=[self.sg_license.security_group_id],
            tags=[{"key": "Name", "value": "eda-license-eni"}],
        )

        # ── RHEL 8 AMI lookup ────────────────────────────────
        # Red Hat 공식 AMI owner: 309956199498
        rhel8 = ec2.MachineImage.lookup(
            name="RHEL-8.*_HVM-*-x86_64-*-Hourly2-GP3",
            owners=["309956199498"],
        )

        # ── EC2 Instance (network_interfaces로 ENI attach) ──
        self.instance = ec2.CfnInstance(
            self, "LicenseInstance",
            instance_type=instance_type,
            image_id=rhel8.get_image(self).image_id,
            iam_instance_profile=instance_profile.ref,
            key_name=self.key_pair.key_pair_name,
            monitoring=True,
            block_device_mappings=[
                ec2.CfnInstance.BlockDeviceMappingProperty(
                    device_name="/dev/sda1",
                    ebs=ec2.CfnInstance.EbsProperty(
                        volume_size=30,
                        volume_type="gp3",
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                ),
            ],
            network_interfaces=[
                ec2.CfnInstance.NetworkInterfaceProperty(
                    device_index="0",
                    network_interface_id=self.eni.ref,
                )
            ],
            tags=[{"key": "Name", "value": "eda-license-server"}],
        )
        self.instance.add_dependency(self.eni)
        self.instance.add_dependency(instance_profile)

        # ── Outputs ──────────────────────────────────────────
        CfnOutput(self, "LicenseInstanceId", value=self.instance.ref)
        CfnOutput(self, "LicenseEniId", value=self.eni.ref)
        CfnOutput(
            self, "LicensePrivateIp",
            value=self.eni.attr_primary_private_ip_address,
            description="Use: export LM_LICENSE_FILE=27000@<this-ip>",
        )
        CfnOutput(
            self, "LicenseMacHint",
            value=(
                "Run: aws ec2 describe-network-interfaces "
                "--network-interface-ids <LicenseEniId> "
                "--query 'NetworkInterfaces[0].MacAddress'"
            ),
            description="MAC address (license Host ID) lookup command",
        )
        CfnOutput(self, "LicenseKeyPairName", value=self.key_pair.key_pair_name)
        CfnOutput(
            self, "LicenseKeyPairId",
            value=self.key_pair.key_pair_id,
            description="aws ssm get-parameter --name /ec2/keypair/<this> --with-decryption",
        )
        CfnOutput(self, "LicenseSgId", value=self.sg_license.security_group_id)

        # ── SSM Parameters ──────────────────────────────────
        for name, value in {
            "InstanceId": self.instance.ref,
            "PrivateIp": self.eni.attr_primary_private_ip_address,
            "KeyPairName": self.key_pair.key_pair_name,
            "SgId": self.sg_license.security_group_id,
        }.items():
            ssm.StringParameter(
                self, f"SsmLicense{name}",
                parameter_name=f"/eda/license/{name}",
                string_value=value,
            )
