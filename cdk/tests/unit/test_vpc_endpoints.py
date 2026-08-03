import pytest
import aws_cdk as cdk
from aws_cdk import assertions, aws_ec2 as ec2
from botocore.exceptions import ClientError

from cdk.base_stack import BaseStack


LOGS_SERVICE = "com.amazonaws.ap-northeast-2.logs"


class FakePaginator:
    def __init__(self, pages=None, error=None):
        self.pages = pages or []
        self.error = error

    def paginate(self, **kwargs):
        if self.error:
            raise self.error
        return iter(self.pages)


class FakeClient:
    def __init__(
        self,
        *,
        paginators=None,
        supported_azs=None,
        security_groups=None,
        service_error=None,
        security_group_error=None,
    ):
        self.paginators = paginators or {}
        self.supported_azs = supported_azs or []
        self.security_groups = security_groups or []
        self.service_error = service_error
        self.security_group_error = security_group_error

    def get_paginator(self, name):
        return self.paginators[name]

    def describe_vpc_endpoint_services(self, **kwargs):
        if self.service_error:
            raise self.service_error
        return {"ServiceDetails": [{"AvailabilityZones": self.supported_azs}]}

    def describe_security_groups(self, **kwargs):
        if self.security_group_error:
            raise self.security_group_error
        return {"SecurityGroups": self.security_groups}


def client_error(code, message, operation="TestOperation"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        operation,
    )


def interface_endpoint(
    endpoint_id="vpce-1",
    *,
    service_name=LOGS_SERVICE,
    state="available",
    private_dns=True,
    groups=None,
    subnets=None,
):
    return {
        "VpcEndpointId": endpoint_id,
        "ServiceName": service_name,
        "State": state,
        "VpcEndpointType": "Interface",
        "PrivateDnsEnabled": private_dns,
        "Groups": groups if groups is not None else [{"GroupId": "sg-1"}],
        "SubnetIds": subnets if subnets is not None else ["subnet-1"],
        "Tags": [{"Key": "Project", "Value": "eda-cluster"}],
    }


def gateway_endpoint(
    endpoint_id="vpce-gw",
    *,
    state="available",
    route_tables=None,
):
    return {
        "VpcEndpointId": endpoint_id,
        "ServiceName": "com.amazonaws.ap-northeast-2.s3",
        "State": state,
        "VpcEndpointType": "Gateway",
        "RouteTableIds": (
            route_tables if route_tables is not None else ["rtb-primary"]
        ),
    }


def security_group(cidr="10.0.0.0/16", from_port=443, to_port=443):
    return {
        "GroupId": "sg-1",
        "IpPermissions": [
            {
                "IpProtocol": "tcp",
                "FromPort": from_port,
                "ToPort": to_port,
                "IpRanges": [{"CidrIp": cidr}],
            }
        ],
    }


def test_endpoint_ownership_uses_physical_id_not_project_tag():
    endpoints = [
        interface_endpoint("vpce-current"),
        interface_endpoint("vpce-other"),
    ]

    managed, external = BaseStack._partition_endpoints_by_owner(
        endpoints, {"vpce-current"}
    )

    assert [ep["VpcEndpointId"] for ep in managed[LOGS_SERVICE]] == ["vpce-current"]
    assert [ep["VpcEndpointId"] for ep in external[LOGS_SERVICE]] == ["vpce-other"]


def test_collects_only_endpoint_physical_ids_from_current_stack():
    client = FakeClient(
        paginators={
            "list_stack_resources": FakePaginator(
                pages=[
                    {
                        "StackResourceSummaries": [
                            {
                                "ResourceType": "AWS::EC2::VPCEndpoint",
                                "PhysicalResourceId": "vpce-1",
                            },
                            {
                                "ResourceType": "AWS::EC2::SecurityGroup",
                                "PhysicalResourceId": "sg-1",
                            },
                        ]
                    },
                    {
                        "StackResourceSummaries": [
                            {
                                "ResourceType": "AWS::EC2::VPCEndpoint",
                                "PhysicalResourceId": "vpce-2",
                            }
                        ]
                    },
                ]
            )
        }
    )

    assert BaseStack._collect_managed_vpc_endpoint_ids(client, "EdaBase") == {
        "vpce-1",
        "vpce-2",
    }


def test_missing_stack_has_no_managed_endpoints():
    error = client_error("ValidationError", "Stack with id EdaBase does not exist")
    client = FakeClient(paginators={"list_stack_resources": FakePaginator(error=error)})

    assert BaseStack._collect_managed_vpc_endpoint_ids(client, "EdaBase") == set()


def test_cloudformation_access_denied_fails_closed():
    error = client_error("AccessDenied", "not authorized")
    client = FakeClient(paginators={"list_stack_resources": FakePaginator(error=error)})

    with pytest.raises(RuntimeError, match="AccessDenied"):
        BaseStack._collect_managed_vpc_endpoint_ids(client, "EdaBase")


def test_vpc_endpoint_lookup_access_denied_fails_closed():
    error = client_error("UnauthorizedOperation", "not authorized")
    client = FakeClient(
        paginators={"describe_vpc_endpoints": FakePaginator(error=error)}
    )

    with pytest.raises(RuntimeError, match="ec2:DescribeVpcEndpoints"):
        BaseStack._lookup_vpc_endpoints(client, "vpc-1")


def test_single_subnet_az_must_be_supported():
    client = FakeClient(supported_azs=["ap-northeast-2a", "ap-northeast-2c"])

    BaseStack._validate_service_supports_az(client, LOGS_SERVICE, "ap-northeast-2a")

    with pytest.raises(ValueError, match="Supported AZs"):
        BaseStack._validate_service_supports_az(client, LOGS_SERVICE, "ap-northeast-2b")


def test_endpoint_service_lookup_access_denied_fails_closed():
    client = FakeClient(
        service_error=client_error("UnauthorizedOperation", "not authorized")
    )

    with pytest.raises(RuntimeError, match="ec2:DescribeVpcEndpointServices"):
        BaseStack._validate_service_supports_az(client, LOGS_SERVICE, "ap-northeast-2a")


def test_reuses_healthy_interface_endpoint():
    client = FakeClient(security_groups=[security_group()])

    BaseStack._validate_external_interface_endpoint(
        client,
        LOGS_SERVICE,
        [interface_endpoint()],
        "10.0.10.0/24",
    )


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (interface_endpoint(state="pending"), "state=pending"),
        (interface_endpoint(private_dns=False), "privateDns=False"),
    ],
)
def test_rejects_unhealthy_interface_endpoint(endpoint, message):
    with pytest.raises(ValueError, match=message):
        BaseStack._validate_external_interface_endpoint(
            FakeClient(),
            LOGS_SERVICE,
            [endpoint],
            "10.0.10.0/24",
        )


def test_rejects_interface_endpoint_without_https_from_subnet():
    client = FakeClient(security_groups=[security_group(cidr="10.0.20.0/24")])

    with pytest.raises(ValueError, match="TCP 443"):
        BaseStack._validate_external_interface_endpoint(
            client,
            LOGS_SERVICE,
            [interface_endpoint()],
            "10.0.10.0/24",
        )


def test_security_group_allows_all_protocols_from_parent_cidr():
    group = {
        "IpPermissions": [
            {
                "IpProtocol": "-1",
                "IpRanges": [{"CidrIp": "10.0.0.0/16"}],
            }
        ]
    }

    assert BaseStack._security_group_allows_https_from_cidr(group, "10.0.10.0/24")


def test_gateway_endpoint_requires_selected_route_table():
    BaseStack._validate_external_gateway_endpoint(
        "com.amazonaws.ap-northeast-2.s3",
        [gateway_endpoint()],
        "rtb-primary",
    )

    with pytest.raises(ValueError, match="rtb-other"):
        BaseStack._validate_external_gateway_endpoint(
            "com.amazonaws.ap-northeast-2.s3",
            [gateway_endpoint()],
            "rtb-other",
        )


def test_managed_endpoints_must_be_available():
    BaseStack._validate_managed_interface_endpoint(LOGS_SERVICE, [interface_endpoint()])
    BaseStack._validate_managed_gateway_endpoint(
        "com.amazonaws.ap-northeast-2.s3", [gateway_endpoint()]
    )

    with pytest.raises(ValueError, match="CloudFormation-managed"):
        BaseStack._validate_managed_interface_endpoint(
            LOGS_SERVICE, [interface_endpoint(state="failed")]
        )
    with pytest.raises(ValueError, match="CloudFormation-managed"):
        BaseStack._validate_managed_gateway_endpoint(
            "com.amazonaws.ap-northeast-2.s3",
            [gateway_endpoint(state="failed")],
        )


@pytest.mark.parametrize(
    ("enable_ssm", "expected_interface_count"),
    [(False, 4), (True, 7)],
)
def test_base_stack_synth_reuses_external_endpoints(
    monkeypatch, enable_ssm, expected_interface_count
):
    class SynthEc2Client(FakeClient):
        def describe_subnets(self, **kwargs):
            return {
                "Subnets": [
                    {
                        "AvailabilityZone": "ap-northeast-2a",
                        "CidrBlock": "10.0.1.0/24",
                    }
                ]
            }

        def describe_route_tables(self, **kwargs):
            return {"RouteTables": [{"RouteTableId": "rtb-primary"}]}

    external_logs = interface_endpoint()
    external_s3 = gateway_endpoint()
    ec2_client = SynthEc2Client(
        paginators={
            "describe_vpc_endpoints": FakePaginator(
                pages=[{"VpcEndpoints": [external_logs, external_s3]}]
            )
        },
        supported_azs=["ap-northeast-2a"],
        security_groups=[security_group()],
    )
    cfn_client = FakeClient(
        paginators={
            "list_stack_resources": FakePaginator(
                pages=[{"StackResourceSummaries": []}]
            )
        }
    )

    def fake_boto3_client(service_name, **kwargs):
        if service_name == "ec2":
            return ec2_client
        if service_name == "cloudformation":
            return cfn_client
        raise AssertionError(f"Unexpected boto3 client: {service_name}")

    def fake_vpc_lookup(scope, construct_id, **kwargs):
        return ec2.Vpc(
            scope,
            "SyntheticVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            max_azs=1,
            nat_gateways=0,
        )

    monkeypatch.setattr("cdk.base_stack.boto3.client", fake_boto3_client)
    monkeypatch.setattr(ec2.Vpc, "from_lookup", staticmethod(fake_vpc_lookup))

    app = cdk.App(
        context={
            "eda:vpc_id": "vpc-123",
            "eda:subnet_id": "subnet-123",
            "eda:enable_ssm": enable_ssm,
        }
    )
    stack = BaseStack(
        app,
        "EdaBase",
        env=cdk.Environment(
            account="111111111111",
            region="ap-northeast-2",
        ),
    )
    template = assertions.Template.from_stack(stack)
    endpoint_resources = template.find_resources("AWS::EC2::VPCEndpoint")
    interface_resources = [
        resource
        for resource in endpoint_resources.values()
        if resource["Properties"]["VpcEndpointType"] == "Interface"
    ]
    gateway_resources = [
        resource
        for resource in endpoint_resources.values()
        if resource["Properties"]["VpcEndpointType"] == "Gateway"
    ]

    assert len(interface_resources) == expected_interface_count
    assert len(gateway_resources) == 1
    template.has_output(
        "SkippedInterfaceEndpoints",
        {"Value": "logs"},
    )
    template.has_output(
        "SkippedGatewayEndpoints",
        {"Value": "s3"},
    )
    trail_buckets = template.find_resources(
        "AWS::S3::Bucket",
        {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
        },
    )
    assert len(trail_buckets) == 1
    assert "BucketName" not in next(iter(trail_buckets.values()))["Properties"]


def test_attachment_failure_stops_before_cloudformation(monkeypatch):
    autoscaling_service = "com.amazonaws.ap-northeast-2.autoscaling"
    existing_services = [
        LOGS_SERVICE,
        "com.amazonaws.ap-northeast-2.cloudformation",
        "com.amazonaws.ap-northeast-2.ec2",
        "com.amazonaws.ap-northeast-2.elasticloadbalancing",
    ]

    class AttachmentEc2Client(FakeClient):
        def __init__(self):
            super().__init__(
                paginators={
                    "describe_vpc_endpoints": FakePaginator(
                        pages=[
                            {
                                "VpcEndpoints": [
                                    interface_endpoint(
                                        f"vpce-{index}",
                                        service_name=service_name,
                                    )
                                    for index, service_name in enumerate(
                                        existing_services
                                    )
                                ]
                            }
                        ]
                    )
                },
                security_groups=[security_group()],
            )
            self.az_checks = []

        def describe_subnets(self, **kwargs):
            return {
                "Subnets": [
                    {
                        "AvailabilityZone": "ap-northeast-2a",
                        "CidrBlock": "10.0.1.0/24",
                    }
                ]
            }

        def describe_route_tables(self, **kwargs):
            return {"RouteTables": [{"RouteTableId": "rtb-primary"}]}

        def describe_vpc_endpoint_services(self, **kwargs):
            service_name = kwargs["ServiceNames"][0]
            self.az_checks.append(service_name)
            supported_azs = (
                ["ap-northeast-2b"]
                if service_name == autoscaling_service
                else ["ap-northeast-2a"]
            )
            return {
                "ServiceDetails": [
                    {"AvailabilityZones": supported_azs}
                ]
            }

    ec2_client = AttachmentEc2Client()
    cfn_client = FakeClient(
        paginators={
            "list_stack_resources": FakePaginator(
                pages=[{"StackResourceSummaries": []}]
            )
        }
    )

    def fake_boto3_client(service_name, **kwargs):
        return cfn_client if service_name == "cloudformation" else ec2_client

    def fake_vpc_lookup(scope, construct_id, **kwargs):
        return ec2.Vpc(
            scope,
            "SyntheticVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            max_azs=1,
            nat_gateways=0,
        )

    monkeypatch.setattr(
        "cdk.base_stack.boto3.client", fake_boto3_client
    )
    monkeypatch.setattr(
        ec2.Vpc, "from_lookup", staticmethod(fake_vpc_lookup)
    )

    app = cdk.App(
        context={
            "eda:vpc_id": "vpc-attachment",
            "eda:subnet_id": "subnet-attachment",
        }
    )
    with pytest.raises(
        ValueError,
        match=(
            "autoscaling.*does not support.*ap-northeast-2a"
        ),
    ):
        BaseStack(
            app,
            "EdaBase",
            env=cdk.Environment(
                account="111111111111",
                region="ap-northeast-2",
            ),
        )

    partial_stack = app.node.find_child("EdaBase")
    template = assertions.Template.from_stack(partial_stack)
    assert template.find_resources("AWS::EC2::VPCEndpoint") == {}
    assert ec2_client.az_checks == [autoscaling_service]
