"""EDA Base Stack — v2.0

기존 VPC/Private Subnet을 import해서 사용.
새로 생성하는 리소스:
  - Security Groups (cluster nodes, FSx, ONTAP, VPC endpoints)
  - EC2 KeyPair (ParallelCluster SSH용)
  - CloudTrail + KMS + S3 (계정 레벨 감사 로그)
  - VPC Endpoints (ParallelCluster 필수) — private subnet에서도 AWS API 접근 가능
    * Interface: logs, cloudformation, ec2 (+ elb, autoscaling when login node)
    * Gateway:   s3, dynamodb

생성하지 않는 리소스 (기존 VPC에 이미 있다고 가정):
  - VPC / Subnet / NAT Gateway / Route Tables / VPC Flow Logs

필수 context:
  - eda:vpc_id        : 기존 VPC ID
  - eda:subnet_id     : FSx/컴퓨트가 들어갈 private subnet ID
옵션 context:
  - eda:enable_vpc_endpoints   (default: true)
  - eda:enable_login_node      (default: true) — ELB/ASG endpoint 생성 여부
"""

import boto3

from aws_cdk import (
    Stack,
    Tags,
    RemovalPolicy,
    Duration,
    aws_ec2 as ec2,
    aws_s3 as s3,
    aws_cloudtrail as cloudtrail,
    aws_kms as kms,
    aws_iam as iam,
    aws_ssm as ssm,
    CfnOutput,
)
from constructs import Construct


# ParallelCluster가 private subnet에서 동작하기 위해 필요한 VPC Endpoint
INTERFACE_ALWAYS = ["logs", "cloudformation", "ec2"]
GATEWAY_ALWAYS = ["s3", "dynamodb"]
INTERFACE_LOGIN_NODE = ["elasticloadbalancing", "autoscaling"]


class BaseStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", "eda-cluster")

        # ── 기존 VPC / Subnet import ─────────────────────────
        vpc_id = self.node.try_get_context("eda:vpc_id")
        subnet_id = self.node.try_get_context("eda:subnet_id")
        if not vpc_id or not subnet_id:
            raise ValueError(
                "Context 'eda:vpc_id' and 'eda:subnet_id' are required. "
                "Set them in cdk.json or pass via `-c eda:vpc_id=... -c eda:subnet_id=...`"
            )

        # subnet의 AZ는 boto3로 조회 (synth 시점). 결과는 cdk.context.json에 캐시.
        subnet_az = self.node.try_get_context(f"eda:subnet_az:{subnet_id}")
        if not subnet_az:
            ec2_client = boto3.client("ec2", region_name=Stack.of(self).region)
            resp = ec2_client.describe_subnets(SubnetIds=[subnet_id])
            subnet_az = resp["Subnets"][0]["AvailabilityZone"]

        self.vpc = ec2.Vpc.from_lookup(self, "EdaVpc", vpc_id=vpc_id)

        self.primary_subnet = ec2.Subnet.from_subnet_attributes(
            self, "EdaPrimarySubnet",
            subnet_id=subnet_id,
            availability_zone=subnet_az,
        )

        # ── Security Groups ──────────────────────────────────
        # 클러스터 노드 (head, login, compute)
        self.sg_cluster_nodes = ec2.SecurityGroup(
            self, "SgClusterNodes",
            vpc=self.vpc,
            description="ParallelCluster nodes - FSx client access",
            allow_all_outbound=True,
        )

        # FSx for OpenZFS 파일시스템용 SG
        # Ports: 111 (rpcbind), 2049 (NFS), 20001-20003 (mount/NLM/status)
        self.sg_fsx = ec2.SecurityGroup(
            self, "SgFsx",
            vpc=self.vpc,
            description="FSx for OpenZFS file system",
            allow_all_outbound=True,
        )
        for port in [111, 2049]:
            self.sg_fsx.add_ingress_rule(
                self.sg_cluster_nodes, ec2.Port.tcp(port), f"NFS TCP {port}"
            )
            self.sg_fsx.add_ingress_rule(
                self.sg_cluster_nodes, ec2.Port.udp(port), f"NFS UDP {port}"
            )
        self.sg_fsx.add_ingress_rule(
            self.sg_cluster_nodes, ec2.Port.tcp_range(20001, 20003), "NFS mount TCP"
        )
        self.sg_fsx.add_ingress_rule(
            self.sg_cluster_nodes, ec2.Port.udp_range(20001, 20003), "NFS mount UDP"
        )

        # FSx for NetApp ONTAP SVM용 SG
        # 참조: https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/limit-access-security-groups.html
        # Ports 요약:
        #   TCP 111 (rpcbind), 635 (mount), 2049 (NFS), 4045 (NFS lock), 4046 (network status)
        #   UDP 111, 635, 2049, 4045, 4046
        #   TCP 3260 (iSCSI), 4420/4421 (NVMe/TCP — 6개 이하 HA pair만)
        #   TCP 443 (ONTAP REST/HTTPS), 22 (SSH management)
        self.sg_ontap = ec2.SecurityGroup(
            self, "SgOntap",
            vpc=self.vpc,
            description="FSx for NetApp ONTAP file system",
            allow_all_outbound=True,
        )
        ontap_tcp_ports = [22, 111, 443, 635, 2049, 3260, 4045, 4046, 4420, 4421]
        ontap_udp_ports = [111, 635, 2049, 4045, 4046]
        for port in ontap_tcp_ports:
            self.sg_ontap.add_ingress_rule(
                self.sg_cluster_nodes, ec2.Port.tcp(port), f"ONTAP TCP {port}"
            )
        for port in ontap_udp_ports:
            self.sg_ontap.add_ingress_rule(
                self.sg_cluster_nodes, ec2.Port.udp(port), f"ONTAP UDP {port}"
            )

        # ── EC2 Key Pair ─────────────────────────────────────
        # Private key는 SSM Parameter Store에 저장:
        #   /ec2/keypair/{key_pair_id}
        # 계정 내 유니크 보장 위해 context로 override 가능 (default: eda-cluster-key-{account})
        key_pair_name = (
            self.node.try_get_context("eda:key_pair_name")
            or f"eda-cluster-key-{Stack.of(self).account}"
        )
        self.key_pair = ec2.KeyPair(
            self, "EdaKeyPair",
            key_pair_name=key_pair_name,
            type=ec2.KeyPairType.RSA,
        )

        # ── CloudTrail ───────────────────────────────────────
        trail_key = kms.Key(
            self, "TrailKey",
            alias="eda/cloudtrail",
            description="Encryption key for EDA CloudTrail logs",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        trail_key.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowCloudTrailEncrypt",
            actions=["kms:GenerateDataKey*"],
            principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "AWS:SourceArn": f"arn:aws:cloudtrail:{Stack.of(self).region}:{Stack.of(self).account}:trail/eda-trail",
                },
                "StringLike": {
                    "kms:EncryptionContext:aws:cloudtrail:arn": f"arn:aws:cloudtrail:{Stack.of(self).region}:{Stack.of(self).account}:trail/*",
                },
            },
        ))
        trail_key.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowCloudTrailDescribeKey",
            actions=["kms:DescribeKey"],
            principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
            resources=["*"],
        ))

        trail_bucket = s3.Bucket(
            self, "TrailBucket",
            bucket_name=f"eda-cloudtrail-{Stack.of(self).account}",
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="archive-to-glacier",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(90),
                        ),
                    ],
                    expiration=Duration.days(365),
                ),
            ],
        )

        cloudtrail.Trail(
            self, "EdaTrail",
            trail_name="eda-trail",
            bucket=trail_bucket,
            encryption_key=trail_key,
            is_multi_region_trail=False,
            include_global_service_events=True,
            management_events=cloudtrail.ReadWriteType.ALL,
        )

        # ── VPC Endpoints ─────────────────────────────────────
        # ParallelCluster가 인터넷 없는 private subnet에서 동작하도록 필수 endpoint를 생성.
        # 공식 참조: https://docs.aws.amazon.com/parallelcluster/latest/ug/aws-parallelcluster-in-a-single-public-subnet-no-internet-v3.html
        self._create_vpc_endpoints_if_enabled()

        # ── Outputs ──────────────────────────────────────────
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(self, "PrimarySubnetId", value=self.primary_subnet.subnet_id)
        CfnOutput(self, "PrimaryAz", value=self.primary_subnet.availability_zone)
        CfnOutput(
            self, "SgClusterNodesId",
            value=self.sg_cluster_nodes.security_group_id,
        )
        CfnOutput(self, "SgFsxId", value=self.sg_fsx.security_group_id)
        CfnOutput(self, "SgOntapId", value=self.sg_ontap.security_group_id)
        CfnOutput(self, "KeyPairName", value=self.key_pair.key_pair_name)
        CfnOutput(
            self, "KeyPairId",
            value=self.key_pair.key_pair_id,
            description="Use: aws ssm get-parameter --name /ec2/keypair/<this-value> --with-decryption",
        )
        CfnOutput(self, "TrailBucketName", value=trail_bucket.bucket_name)

        # ── SSM Parameters (cross-stack sharing) ────────────
        for name, value in {
            "VpcId": self.vpc.vpc_id,
            "PrimarySubnetId": self.primary_subnet.subnet_id,
            "PrimaryAz": self.primary_subnet.availability_zone,
            "SgClusterNodesId": self.sg_cluster_nodes.security_group_id,
            "KeyPairName": self.key_pair.key_pair_name,
        }.items():
            ssm.StringParameter(
                self, f"Ssm{name}",
                parameter_name=f"/eda/network/{name}",
                string_value=value,
            )

    # ── VPC endpoints helpers ────────────────────────────────

    def _create_vpc_endpoints_if_enabled(self) -> None:
        """eda:enable_vpc_endpoints=true 일 때만 endpoint + SG를 생성."""
        if not self._ctx_bool("eda:enable_vpc_endpoints", True):
            return

        region = Stack.of(self).region
        vpc = self.vpc
        primary_subnet = self.primary_subnet

        # 기존 endpoint 조회 (중복 생성 방지).
        # 단, 이 스택이 이전에 생성한 endpoint (Project=eda-cluster 태그)는
        # "기존"으로 간주하지 않는다. 그래야 재배포 시에도 CFN 템플릿에
        # 계속 포함되어 삭제되지 않음.
        try:
            ec2_client = boto3.client("ec2", region_name=region)
            existing = ec2_client.describe_vpc_endpoints(
                Filters=[{"Name": "vpc-id", "Values": [vpc.vpc_id]}]
            )
            existing_services = set()
            for ep in existing.get("VpcEndpoints", []):
                tags = {t["Key"]: t["Value"] for t in ep.get("Tags", [])}
                if tags.get("Project") == "eda-cluster":
                    continue  # 우리가 만든 것 → 재생성 대상
                existing_services.add(ep["ServiceName"])
        except Exception:
            existing_services = set()

        def _svc_name(short: str) -> str:
            return f"com.amazonaws.{region}.{short}"

        # Endpoint 전용 SG (VPC CIDR 내부에서 HTTPS 443만 허용)
        self.sg_vpce = ec2.SecurityGroup(
            self, "SgVpcEndpoints",
            vpc=vpc,
            description="Endpoint SG - HTTPS from VPC CIDR",
            allow_all_outbound=True,
        )
        self.sg_vpce.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "HTTPS from VPC",
        )

        # Interface endpoints
        enable_login = self._ctx_bool("eda:enable_login_node", True)
        interface_services = list(INTERFACE_ALWAYS)
        if enable_login:
            interface_services += INTERFACE_LOGIN_NODE

        created_interface = []
        skipped_interface = []
        for short in interface_services:
            full = _svc_name(short)
            if full in existing_services:
                skipped_interface.append(short)
                continue
            ep = ec2.CfnVPCEndpoint(
                self, f"IfcEp{short.capitalize()}",
                vpc_id=vpc.vpc_id,
                service_name=full,
                vpc_endpoint_type="Interface",
                subnet_ids=[primary_subnet.subnet_id],
                security_group_ids=[self.sg_vpce.security_group_id],
                private_dns_enabled=True,
            )
            Tags.of(ep).add("Name", f"eda-vpce-{short}")
            created_interface.append(short)

        # Gateway endpoints (S3, DynamoDB) — route table에 바인딩
        subnet_rtb_id = self._lookup_route_table(vpc.vpc_id, primary_subnet.subnet_id)

        created_gateway = []
        skipped_gateway = []
        for short in GATEWAY_ALWAYS:
            full = _svc_name(short)
            if full in existing_services:
                skipped_gateway.append(short)
                continue
            if not subnet_rtb_id:
                skipped_gateway.append(f"{short} (route table not resolvable)")
                continue
            ep = ec2.CfnVPCEndpoint(
                self, f"GwEp{short.capitalize()}",
                vpc_id=vpc.vpc_id,
                service_name=full,
                vpc_endpoint_type="Gateway",
                route_table_ids=[subnet_rtb_id],
            )
            Tags.of(ep).add("Name", f"eda-vpce-{short}")
            created_gateway.append(short)

        # Outputs
        if created_interface:
            CfnOutput(self, "CreatedInterfaceEndpoints", value=",".join(created_interface))
        if skipped_interface:
            CfnOutput(
                self, "SkippedInterfaceEndpoints",
                value=",".join(skipped_interface),
                description="Already existed in VPC",
            )
        if created_gateway:
            CfnOutput(self, "CreatedGatewayEndpoints", value=",".join(created_gateway))
        if skipped_gateway:
            CfnOutput(
                self, "SkippedGatewayEndpoints",
                value=",".join(skipped_gateway),
                description="Already existed or route table not found",
            )
        CfnOutput(self, "VpcEndpointSgId", value=self.sg_vpce.security_group_id)

    def _ctx_bool(self, key: str, default: bool) -> bool:
        val = self.node.try_get_context(key)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def _lookup_route_table(self, vpc_id: str, subnet_id: str) -> str | None:
        """Subnet의 explicit RT를 찾고, 없으면 VPC main RT로 fallback."""
        try:
            ec2_client = boto3.client("ec2", region_name=Stack.of(self).region)
            resp = ec2_client.describe_route_tables(
                Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
            )
            rtbs = resp.get("RouteTables", [])
            if rtbs:
                return rtbs[0]["RouteTableId"]
            resp = ec2_client.describe_route_tables(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "association.main", "Values": ["true"]},
                ]
            )
            rtbs = resp.get("RouteTables", [])
            if rtbs:
                return rtbs[0]["RouteTableId"]
        except Exception:
            pass
        return None
