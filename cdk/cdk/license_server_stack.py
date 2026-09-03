"""EDA License Server Stack — v1.0

EDA 라이센스 서버용 EC2 인스턴스 생성.

기본 운영 모델은 Synopsys floating license(SCL/FlexNet)이며, lmgrd 포트
27000과 snpslmd vendor daemon 포트 27020을 사용한다. 다른 벤더를 사용할
경우 context로 두 포트를 변경할 수 있다.

설계 포인트:
  - ENI를 명시 생성해서 EC2에 attach → instance 교체 시에도 MAC/private IP 보존
    (라이센스가 Host ID = MAC address에 bind되는 경우 재발급 방지)
  - MAC 영속성 (eda:license_retain_eni, default true):
      * ENI와 Security Group에 DeletionPolicy: Retain → 스택을 삭제해도 살아남음
      * SG를 함께 보존하는 이유: retained ENI가 SG를 참조하는 상태에서 SG를 지우면
        DependencyViolation으로 스택 삭제가 DELETE_FAILED로 실패한다
      * ingress 규칙은 전부 독립 리소스(AWS::EC2::SecurityGroupIngress)로 생성 →
        SG에 inline 규칙이 남지 않으므로 재배포 시 InvalidPermission.Duplicate 없음
  - ENI/SG 재사용 (eda:license_eni_id + eda:license_sg_id):
      보존해 둔 ENI를 새 EC2에 attach → MAC/private IP 그대로 유지 → 라이선스 재발급 불필요
      * ENI는 subnet에 고정된다. 반드시 ENI가 생성된 subnet(= 같은 AZ)으로 배포해야 하며
        다른 subnet/VPC로는 이동할 수 없다
      * ENI 상태가 available(detach 상태)이어야 attach 가능
  - eda:license_ami_id: 라이선스 매니저/라이선스 파일이 설치된 자체 AMI로 기동
    (root EBS는 delete_on_termination=True이므로 재생성 시 AMI 없이는 재설치 필요)
  - 전용 SSH KeyPair (eda-license-key-{account})
    KeyPair는 이름이 고정이라 Retain 대상이 아니다 (retain하면 재배포 시 이름 충돌)
    → Private key는 SSM Parameter Store: /ec2/keypair/{key_pair_id}
  - IAM Role: AmazonSSMManagedInstanceCore + CloudWatchAgentServerPolicy
    (user_data는 없지만 추후 SSM/로그 수집용으로 부여)
  - Security Group:
      Ingress:
        * TCP 22       from VPC CIDR (VPN 경유 SSH)
        * TCP 27000    from sg_cluster_nodes (default lmgrd port)
        * TCP 27020    from sg_cluster_nodes (default snpslmd port)
          → 실제 라이선스 파일에도 동일한 manager/vendor 포트를 고정
  - Root EBS: 30 GiB gp3, KMS 암호화
  - IMDS: IMDSv2만 허용 (token required, hop limit 1)
  - user_data: 없음
  - EBS snapshot: 없음

생성 후 운영자 작업:
  1. scripts/get-license-server-key.sh 로 pem 다운로드
  2. ssh -i ~/.ssh/eda-license-key-<account>.pem ec2-user@<private-ip>
  3. (선택) 벤더 문서가 32bit runtime을 요구할 때만 관련 라이브러리 설치:
     sudo dnf -y install glibc.i686 libstdc++.i686 libX11.i686 libXext.i686 \\
                          libXrender.i686 libgcc.i686 ncurses-libs.i686 lsof
     이 명령은 AWS 스택 배포 필수조건이 아니며, 격리망에서는 사용 가능한
     내부 RPM 저장소 또는 오프라인 패키지가 있어야 함.
  4. 벤더 라이선스 매니저 + 라이선스 파일 배치 + 데몬 기동

필수 context:
  - eda:vpc_id
  - eda:subnet_id
옵션:
  - eda:license_instance_type   (default: m7i.large)
  - eda:license_manager_port    (default: 27000)
  - eda:license_vendor_port     (default: 27020)
  - eda:license_key_pair_name   (default: eda-license-key-{account})
  - eda:license_retain_eni      (default: true)  ENI/SG를 스택 삭제 시 보존
  - eda:license_eni_id          (default: none)  기존 ENI 재사용 (MAC 유지)
  - eda:license_sg_id           (default: none)  기존 SG 재사용 (eni_id와 함께 필수)
  - eda:license_ami_id          (default: none)  RHEL 8 lookup 대신 지정 AMI 사용
"""

import re

from aws_cdk import (
    RemovalPolicy,
    Stack,
    Tags,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_ssm as ssm,
    CfnOutput,
)
from constructs import Construct
from cdk.naming import foundation_export_name, resource_prefix, ssm_path


def _aws_id_pattern(prefix: str) -> re.Pattern:
    """EC2 resource IDs are 8 or 17 hex characters after the type prefix."""
    return re.compile(rf"^{prefix}-([0-9a-f]{{8}}|[0-9a-f]{{17}})$")


_ENI_ID_PATTERN = _aws_id_pattern("eni")
_SG_ID_PATTERN = _aws_id_pattern("sg")
_AMI_ID_PATTERN = _aws_id_pattern("ami")


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
        prefix = resource_prefix(self.node)

        instance_type = (
            self.node.try_get_context("eda:license_instance_type") or "m7i.large"
        )
        manager_port = self._ctx_port("eda:license_manager_port", 27000)
        vendor_port = self._ctx_port("eda:license_vendor_port", 27020)
        if manager_port == vendor_port:
            raise ValueError(
                "eda:license_manager_port and eda:license_vendor_port must differ"
            )

        retain_identity = self._ctx_bool("eda:license_retain_eni", True)
        reuse_eni_id = self._ctx_id("eda:license_eni_id", _ENI_ID_PATTERN)
        reuse_sg_id = self._ctx_id("eda:license_sg_id", _SG_ID_PATTERN)
        ami_id = self._ctx_id("eda:license_ami_id", _AMI_ID_PATTERN)
        if bool(reuse_eni_id) != bool(reuse_sg_id):
            raise ValueError(
                "eda:license_eni_id and eda:license_sg_id must be set together. "
                "A reused ENI keeps the security groups it was created with, so "
                "the ingress rules must be attached to that same group."
            )

        # ── Security Group ────────────────────────────────────
        # 재사용 모드에서는 보존된 SG를 그대로 import한다. 새 SG를 만들면 기존 ENI가
        # 여전히 옛 SG를 참조하므로 라이선스 포트가 열리지 않는다.
        if reuse_sg_id:
            self.sg_license = ec2.SecurityGroup.from_security_group_id(
                self,
                "SgLicenseServer",
                reuse_sg_id,
                mutable=True,
            )
        else:
            self.sg_license = ec2.SecurityGroup(
                self,
                "SgLicenseServer",
                vpc=vpc,
                description="EDA license server",
                allow_all_outbound=True,
            )
            if retain_identity:
                # 보존된 ENI가 이 SG를 참조하므로 SG도 함께 남겨야 스택 삭제가 성공한다.
                self.sg_license.node.default_child.apply_removal_policy(
                    RemovalPolicy.RETAIN
                )

        # SSH 22: 0.0.0.0/0 (HeadNode의 pcluster-managed SG와 동일 정책).
        # Private subnet이라 외부 인터넷에서 실제로 도달할 수 없고, VPN source NAT
        # 유무와 무관하게 사내망 워크스테이션에서도 접속 가능하도록 허용.
        # inline 규칙 대신 독립 리소스로 만든다 → 보존된 SG에 규칙이 남지 않으므로
        # 재배포 시 InvalidPermission.Duplicate가 발생하지 않는다.
        ec2.CfnSecurityGroupIngress(
            self,
            "SgLicenseServerSsh",
            group_id=self.sg_license.security_group_id,
            ip_protocol="tcp",
            from_port=22,
            to_port=22,
            cidr_ip="0.0.0.0/0",
            description="SSH (private subnet - reachable only via VPN)",
        )
        # License manager main + vendor daemon — cluster node에서만
        self.sg_license.add_ingress_rule(
            sg_cluster_nodes,
            ec2.Port.tcp(manager_port),
            "License manager (lmgrd) port from cluster",
        )
        self.sg_license.add_ingress_rule(
            sg_cluster_nodes,
            ec2.Port.tcp(vendor_port),
            "License vendor daemon (snpslmd by default) port from cluster",
        )

        # ── SSH Key Pair (전용) ───────────────────────────────
        key_pair_name = (
            self.node.try_get_context("eda:license_key_pair_name")
            or f"{prefix}-license-key-{Stack.of(self).account}"
        )
        self.key_pair = ec2.KeyPair(
            self,
            "LicenseKeyPair",
            key_pair_name=key_pair_name,
            type=ec2.KeyPairType.RSA,
        )

        # ── IAM Role ──────────────────────────────────────────
        role = iam.Role(
            self,
            "LicenseInstanceRole",
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
            self,
            "LicenseInstanceProfile",
            roles=[role.role_name],
        )

        # ── Static ENI (MAC/IP 영속성) ───────────────────────
        # EC2 교체해도 ENI만 detach → 새 instance에 attach 하면 MAC 유지
        if reuse_eni_id:
            # 보존해 둔 ENI를 그대로 attach. ENI가 subnet에 고정되어 있으므로
            # 인스턴스는 primary_subnet이 아니라 ENI의 subnet에 생성된다.
            self.eni = None
            eni_id = reuse_eni_id
        else:
            self.eni = ec2.CfnNetworkInterface(
                self,
                "LicenseEni",
                subnet_id=primary_subnet.subnet_id,
                description="EDA license server static ENI (MAC persistence)",
                group_set=[self.sg_license.security_group_id],
                tags=[{"key": "Name", "value": f"{prefix}-license-eni"}],
            )
            if retain_identity:
                self.eni.apply_removal_policy(RemovalPolicy.RETAIN)
            eni_id = self.eni.ref

        # ── AMI ──────────────────────────────────────────────
        # 기본은 RHEL 8 공식 AMI (Red Hat owner: 309956199498).
        # 라이선스 매니저가 설치된 자체 AMI가 있으면 eda:license_ami_id로 지정한다.
        if ami_id:
            image_id = ami_id
        else:
            rhel8 = ec2.MachineImage.lookup(
                name="RHEL-8.*_HVM-*-x86_64-*-Hourly2-GP3",
                owners=["309956199498"],
            )
            image_id = rhel8.get_image(self).image_id

        # ── EC2 Instance (network_interfaces로 ENI attach) ──
        self.instance = ec2.CfnInstance(
            self,
            "LicenseInstance",
            instance_type=instance_type,
            image_id=image_id,
            iam_instance_profile=instance_profile.ref,
            key_name=self.key_pair.key_pair_name,
            monitoring=True,
            metadata_options=ec2.CfnInstance.MetadataOptionsProperty(
                http_endpoint="enabled",
                http_tokens="required",
                http_put_response_hop_limit=1,
                instance_metadata_tags="disabled",
            ),
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
                    network_interface_id=eni_id,
                )
            ],
            tags=[{"key": "Name", "value": f"{prefix}-license-server"}],
        )

        # ── Outputs ──────────────────────────────────────────
        CfnOutput(self, "LicenseInstanceId", value=self.instance.ref)
        CfnOutput(self, "LicenseEniId", value=eni_id)
        CfnOutput(
            self,
            "LicenseEniMode",
            value=(
                "reused-existing"
                if reuse_eni_id
                else ("created-retained" if retain_identity else "created-ephemeral")
            ),
            description="created-ephemeral means the MAC address is lost on stack deletion",
        )
        CfnOutput(
            self,
            "LicensePrivateIp",
            # ENI를 재사용할 때는 스택에 ENI 리소스가 없으므로 인스턴스에서 읽는다
            # (primary private IP == 붙어 있는 ENI의 primary private IP).
            value=self.instance.attr_private_ip,
            description=f"Use: export LM_LICENSE_FILE={manager_port}@<this-ip>",
        )
        CfnOutput(
            self,
            "LicenseManagerPort",
            value=str(manager_port),
            export_name=foundation_export_name(
                self.node,
                "license",
                "LicenseManagerPort",
            ),
        )
        CfnOutput(
            self,
            "LicenseVendorPort",
            value=str(vendor_port),
            export_name=foundation_export_name(
                self.node,
                "license",
                "LicenseVendorPort",
            ),
        )
        CfnOutput(
            self,
            "LicenseMacHint",
            value=(
                "Run: aws ec2 describe-network-interfaces "
                "--network-interface-ids <LicenseEniId> "
                "--query 'NetworkInterfaces[0].MacAddress'"
            ),
            description="MAC address (license Host ID) lookup command",
        )
        CfnOutput(self, "LicenseKeyPairName", value=self.key_pair.key_pair_name)
        CfnOutput(
            self,
            "LicenseKeyPairId",
            value=self.key_pair.key_pair_id,
            description="aws ssm get-parameter --name /ec2/keypair/<this> --with-decryption",
        )
        CfnOutput(
            self,
            "LicenseSgId",
            value=self.sg_license.security_group_id,
            export_name=foundation_export_name(
                self.node,
                "license",
                "LicenseSgId",
            ),
        )

        # ── SSM Parameters ──────────────────────────────────
        for name, value in {
            "InstanceId": self.instance.ref,
            "EniId": eni_id,
            "PrivateIp": self.instance.attr_private_ip,
            "KeyPairName": self.key_pair.key_pair_name,
            "SgId": self.sg_license.security_group_id,
            "ManagerPort": str(manager_port),
            "VendorPort": str(vendor_port),
        }.items():
            ssm.StringParameter(
                self,
                f"SsmLicense{name}",
                parameter_name=ssm_path(self.node, f"license/{name}"),
                string_value=value,
            )

    def _ctx_bool(self, key: str, default: bool) -> bool:
        value = self.node.try_get_context(key)
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _ctx_id(self, key: str, pattern: re.Pattern) -> str | None:
        """Return a validated EC2 resource ID from context, or None if unset."""
        value = self.node.try_get_context(key)
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if not pattern.fullmatch(value):
            raise ValueError(
                f"{key} must match {pattern.pattern} (found: {value})"
            )
        return value

    def _ctx_port(self, key: str, default: int) -> int:
        value = self.node.try_get_context(key)
        try:
            port = int(value) if value is not None else default
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer TCP port") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{key} must be between 1 and 65535")
        return port
