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
  - eda:enable_ssm             (default: false) — SSM endpoint 생성 여부
"""

from collections import defaultdict
from ipaddress import ip_network

import boto3
from botocore.exceptions import BotoCoreError, ClientError

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
INTERFACE_SSM = ["ssm", "ssmmessages", "ec2messages"]


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

        # Endpoint AZ/SG/route validation에도 사용하므로 subnet 정보를 fail-closed로 조회.
        ec2_client = boto3.client("ec2", region_name=Stack.of(self).region)
        try:
            resp = ec2_client.describe_subnets(SubnetIds=[subnet_id])
            subnet = resp["Subnets"][0]
            subnet_az = subnet["AvailabilityZone"]
            self.primary_subnet_cidr = subnet["CidrBlock"]
        except (BotoCoreError, ClientError, IndexError, KeyError) as exc:
            raise RuntimeError(
                f"Unable to inspect subnet {subnet_id} in {Stack.of(self).region}: {exc}"
            ) from exc

        self.primary_route_table_id = self._lookup_route_table(vpc_id, subnet_id)

        self.vpc = ec2.Vpc.from_lookup(self, "EdaVpc", vpc_id=vpc_id)

        self.primary_subnet = ec2.Subnet.from_subnet_attributes(
            self, "EdaPrimarySubnet",
            subnet_id=subnet_id,
            availability_zone=subnet_az,
            route_table_id=self.primary_route_table_id,
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

        ec2_client = boto3.client("ec2", region_name=region)
        managed_endpoint_ids = self._lookup_managed_vpc_endpoint_ids()
        endpoints = self._lookup_vpc_endpoints(ec2_client, vpc.vpc_id)
        managed_by_service, external_by_service = self._partition_endpoints_by_owner(
            endpoints, managed_endpoint_ids
        )

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
            ec2.Peer.ipv4(self.primary_subnet_cidr),
            ec2.Port.tcp(443),
            "HTTPS from primary subnet",
        )

        # Interface endpoints
        enable_login = self._ctx_bool("eda:enable_login_node", True)
        enable_ssm = self._ctx_bool("eda:enable_ssm", False)
        interface_services = list(INTERFACE_ALWAYS)
        if enable_login:
            interface_services += INTERFACE_LOGIN_NODE
        if enable_ssm:
            interface_services += INTERFACE_SSM

        created_interface = []
        skipped_interface = []
        for short in interface_services:
            full = _svc_name(short)
            if managed_by_service[full]:
                self._validate_managed_interface_endpoint(
                    full, managed_by_service[full]
                )
                if not any(
                    primary_subnet.subnet_id in endpoint.get("SubnetIds", [])
                    for endpoint in managed_by_service[full]
                ):
                    self._validate_service_supports_az(
                        ec2_client, full, primary_subnet.availability_zone
                    )
            elif external_by_service[full]:
                self._validate_external_interface_endpoint(
                    ec2_client,
                    full,
                    external_by_service[full],
                    self.primary_subnet_cidr,
                )
                skipped_interface.append(short)
                continue
            else:
                self._validate_service_supports_az(
                    ec2_client, full, primary_subnet.availability_zone
                )
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
        created_gateway = []
        skipped_gateway = []
        for short in GATEWAY_ALWAYS:
            full = _svc_name(short)
            if managed_by_service[full]:
                self._validate_managed_gateway_endpoint(
                    full, managed_by_service[full]
                )
            elif external_by_service[full]:
                self._validate_external_gateway_endpoint(
                    full,
                    external_by_service[full],
                    self.primary_route_table_id,
                )
                skipped_gateway.append(short)
                continue
            ep = ec2.CfnVPCEndpoint(
                self, f"GwEp{short.capitalize()}",
                vpc_id=vpc.vpc_id,
                service_name=full,
                vpc_endpoint_type="Gateway",
                route_table_ids=[self.primary_route_table_id],
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

    def _lookup_managed_vpc_endpoint_ids(self) -> set[str]:
        """Return endpoint physical IDs owned by this exact CloudFormation stack."""
        client = boto3.client(
            "cloudformation", region_name=Stack.of(self).region
        )
        return self._collect_managed_vpc_endpoint_ids(client, self.stack_name)

    @staticmethod
    def _collect_managed_vpc_endpoint_ids(client, stack_name: str) -> set[str]:
        try:
            endpoint_ids = set()
            paginator = client.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=stack_name):
                for resource in page.get("StackResourceSummaries", []):
                    if resource.get("ResourceType") == "AWS::EC2::VPCEndpoint":
                        physical_id = resource.get("PhysicalResourceId")
                        if physical_id:
                            endpoint_ids.add(physical_id)
            return endpoint_ids
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if (
                error.get("Code") == "ValidationError"
                and "does not exist" in error.get("Message", "")
            ):
                return set()
            raise RuntimeError(
                f"Unable to inspect CloudFormation ownership for {stack_name}: {exc}"
            ) from exc
        except BotoCoreError as exc:
            raise RuntimeError(
                f"Unable to inspect CloudFormation ownership for {stack_name}: {exc}"
            ) from exc

    @staticmethod
    def _lookup_vpc_endpoints(client, vpc_id: str) -> list[dict]:
        try:
            endpoints = []
            paginator = client.get_paginator("describe_vpc_endpoints")
            for page in paginator.paginate(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ):
                endpoints.extend(page.get("VpcEndpoints", []))
            return endpoints
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(
                f"Unable to inspect existing VPC endpoints in {vpc_id}: {exc}. "
                "The deployment principal requires ec2:DescribeVpcEndpoints."
            ) from exc

    @staticmethod
    def _partition_endpoints_by_owner(
        endpoints: list[dict], managed_endpoint_ids: set[str]
    ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        managed_by_service = defaultdict(list)
        external_by_service = defaultdict(list)
        for endpoint in endpoints:
            target = (
                managed_by_service
                if endpoint["VpcEndpointId"] in managed_endpoint_ids
                else external_by_service
            )
            target[endpoint["ServiceName"]].append(endpoint)
        return managed_by_service, external_by_service

    @staticmethod
    def _validate_service_supports_az(
        client, service_name: str, availability_zone: str
    ) -> None:
        try:
            response = client.describe_vpc_endpoint_services(
                ServiceNames=[service_name]
            )
            details = response.get("ServiceDetails", [])
            supported_azs = details[0].get("AvailabilityZones", []) if details else []
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(
                f"Unable to validate Availability Zone support for {service_name}: "
                f"{exc}. The deployment principal requires "
                "ec2:DescribeVpcEndpointServices."
            ) from exc

        if availability_zone not in supported_azs:
            supported = ", ".join(sorted(supported_azs)) or "none"
            raise ValueError(
                f"VPC endpoint service {service_name} does not support the selected "
                f"single-subnet Availability Zone {availability_zone}. Supported AZs: "
                f"{supported}. Choose a SUBNET_ID in a supported AZ or disable the "
                "feature that requires this endpoint."
            )

    @staticmethod
    def _validate_managed_interface_endpoint(
        service_name: str, endpoints: list[dict]
    ) -> None:
        if any(
            endpoint.get("State") == "available"
            and endpoint.get("VpcEndpointType") == "Interface"
            and endpoint.get("PrivateDnsEnabled") is True
            for endpoint in endpoints
        ):
            return
        raise ValueError(
            f"CloudFormation-managed endpoint for {service_name} is not an available "
            "Interface endpoint with PrivateDnsEnabled=true. Resolve the endpoint or "
            "stack state before redeploying."
        )

    @staticmethod
    def _validate_managed_gateway_endpoint(
        service_name: str, endpoints: list[dict]
    ) -> None:
        if any(
            endpoint.get("State") == "available"
            and endpoint.get("VpcEndpointType") == "Gateway"
            for endpoint in endpoints
        ):
            return
        raise ValueError(
            f"CloudFormation-managed endpoint for {service_name} is not an available "
            "Gateway endpoint. Resolve the endpoint or stack state before redeploying."
        )

    @classmethod
    def _validate_external_interface_endpoint(
        cls,
        client,
        service_name: str,
        endpoints: list[dict],
        source_cidr: str,
    ) -> None:
        available = [
            endpoint
            for endpoint in endpoints
            if endpoint.get("State") == "available"
            and endpoint.get("VpcEndpointType") == "Interface"
            and endpoint.get("PrivateDnsEnabled") is True
        ]
        if not available:
            details = ", ".join(
                f"{ep.get('VpcEndpointId')} state={ep.get('State')} "
                f"type={ep.get('VpcEndpointType')} "
                f"privateDns={ep.get('PrivateDnsEnabled')}"
                for ep in endpoints
            )
            raise ValueError(
                f"Existing endpoint(s) for {service_name} cannot be reused: {details}. "
                "An available Interface endpoint with PrivateDnsEnabled=true is required."
            )

        group_ids = sorted({
            group["GroupId"]
            for endpoint in available
            for group in endpoint.get("Groups", [])
            if group.get("GroupId")
        })
        if not group_ids:
            raise ValueError(
                f"Existing endpoint(s) for {service_name} have no security groups."
            )

        try:
            response = client.describe_security_groups(GroupIds=group_ids)
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(
                f"Unable to validate security groups for {service_name}: {exc}. "
                "The deployment principal requires ec2:DescribeSecurityGroups."
            ) from exc

        security_groups = {
            group["GroupId"]: group
            for group in response.get("SecurityGroups", [])
        }
        for endpoint in available:
            if any(
                cls._security_group_allows_https_from_cidr(
                    security_groups.get(group["GroupId"], {}), source_cidr
                )
                for group in endpoint.get("Groups", [])
                if group.get("GroupId")
            ):
                return

        raise ValueError(
            f"Existing endpoint(s) for {service_name} do not allow inbound TCP 443 "
            f"from the selected subnet CIDR {source_cidr}. Update an endpoint security "
            "group before deploying."
        )

    @staticmethod
    def _security_group_allows_https_from_cidr(
        security_group: dict, source_cidr: str
    ) -> bool:
        source_network = ip_network(source_cidr)
        for permission in security_group.get("IpPermissions", []):
            protocol = str(permission.get("IpProtocol"))
            if protocol != "-1":
                if protocol not in ("tcp", "6"):
                    continue
                from_port = permission.get("FromPort")
                to_port = permission.get("ToPort")
                if from_port is None or to_port is None:
                    continue
                if not from_port <= 443 <= to_port:
                    continue
            for ip_range in permission.get("IpRanges", []):
                cidr = ip_range.get("CidrIp")
                if cidr and source_network.subnet_of(ip_network(cidr)):
                    return True
        return False

    @staticmethod
    def _validate_external_gateway_endpoint(
        service_name: str,
        endpoints: list[dict],
        route_table_id: str,
    ) -> None:
        for endpoint in endpoints:
            if (
                endpoint.get("State") == "available"
                and endpoint.get("VpcEndpointType") == "Gateway"
                and route_table_id in endpoint.get("RouteTableIds", [])
            ):
                return

        details = ", ".join(
            f"{ep.get('VpcEndpointId')} state={ep.get('State')} "
            f"type={ep.get('VpcEndpointType')} "
            f"routeTables={ep.get('RouteTableIds', [])}"
            for ep in endpoints
        )
        raise ValueError(
            f"Existing endpoint(s) for {service_name} are not usable by route table "
            f"{route_table_id}: {details}. Associate that route table with an available "
            "Gateway endpoint before deploying."
        )

    def _lookup_route_table(self, vpc_id: str, subnet_id: str) -> str:
        """Find the subnet's explicit route table, falling back to the VPC main table."""
        ec2_client = boto3.client("ec2", region_name=Stack.of(self).region)
        try:
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
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(
                f"Unable to resolve the route table for subnet {subnet_id}: {exc}. "
                "The deployment principal requires ec2:DescribeRouteTables."
            ) from exc
        raise ValueError(
            f"No explicit subnet route table or VPC main route table was found for "
            f"subnet {subnet_id} in VPC {vpc_id}."
        )
