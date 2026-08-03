import aws_cdk as cdk
from aws_cdk import assertions, aws_ec2 as ec2

from cdk.license_server_stack import LicenseServerStack
from cdk.storage_stack import StorageStack


ENV = cdk.Environment(account="111111111111", region="ap-northeast-2")


def imported_network(app):
    support = cdk.Stack(app, "NetworkSupport", env=ENV)
    vpc = ec2.Vpc.from_vpc_attributes(
        support,
        "Vpc",
        vpc_id="vpc-0123456789abcdef0",
        vpc_cidr_block="10.0.0.0/16",
        availability_zones=["ap-northeast-2a"],
    )
    subnet = ec2.Subnet.from_subnet_attributes(
        support,
        "Subnet",
        subnet_id="subnet-0123456789abcdef0",
        availability_zone="ap-northeast-2a",
        route_table_id="rtb-0123456789abcdef0",
    )
    cluster_sg = ec2.SecurityGroup.from_security_group_id(
        support, "ClusterSg", "sg-0123456789abcdef0"
    )
    fsx_sg = ec2.SecurityGroup.from_security_group_id(
        support, "FsxSg", "sg-1123456789abcdef0"
    )
    ontap_sg = ec2.SecurityGroup.from_security_group_id(
        support, "OntapSg", "sg-2123456789abcdef0"
    )
    return vpc, subnet, cluster_sg, fsx_sg, ontap_sg


def test_storage_stack_synthesizes_valid_default_openzfs_layout():
    app = cdk.App(
        context={
            "eda:enable_openzfs": True,
            "eda:enable_ontap": False,
            "eda:openzfs_size_gib": 320,
            "eda:openzfs_throughput": 1280,
        }
    )
    vpc, subnet, _, fsx_sg, ontap_sg = imported_network(app)
    stack = StorageStack(
        app,
        "EdaStorage",
        vpc=vpc,
        sg_fsx=fsx_sg,
        sg_ontap=ontap_sg,
        primary_subnet=subnet,
        env=ENV,
    )
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::FSx::FileSystem",
        {
            "FileSystemType": "OPENZFS",
            "StorageCapacity": 320,
            "OpenZFSConfiguration": {
                "DeploymentType": "SINGLE_AZ_HA_2",
                "ThroughputCapacity": 1280,
            },
        },
    )
    volumes = template.find_resources("AWS::FSx::Volume")
    assert len(volumes) == 3
    assert all(
        resource["Properties"]["OpenZFSConfiguration"][
            "StorageCapacityQuotaGiB"
        ]
        <= 320
        for resource in volumes.values()
    )


def test_ontap_secret_uses_generated_name_for_reinstall():
    app = cdk.App(
        context={
            "eda:enable_openzfs": False,
            "eda:enable_ontap": True,
        }
    )
    vpc, subnet, _, fsx_sg, ontap_sg = imported_network(app)
    stack = StorageStack(
        app,
        "EdaStorage",
        vpc=vpc,
        sg_fsx=fsx_sg,
        sg_ontap=ontap_sg,
        primary_subnet=subnet,
        env=ENV,
    )
    template = assertions.Template.from_stack(stack)

    secrets = template.find_resources("AWS::SecretsManager::Secret")
    assert len(secrets) == 1
    assert "Name" not in next(iter(secrets.values()))["Properties"]


def test_license_server_uses_private_static_network_interface(monkeypatch):
    monkeypatch.setattr(
        ec2.MachineImage,
        "lookup",
        staticmethod(
            lambda **kwargs: ec2.MachineImage.generic_linux(
                {"ap-northeast-2": "ami-0123456789abcdef0"}
            )
        ),
    )
    app = cdk.App()
    vpc, subnet, cluster_sg, _, _ = imported_network(app)
    stack = LicenseServerStack(
        app,
        "EdaLicenseServer",
        vpc=vpc,
        sg_cluster_nodes=cluster_sg,
        primary_subnet=subnet,
        env=ENV,
    )
    template = assertions.Template.from_stack(stack)

    template.resource_count_is("AWS::EC2::NetworkInterface", 1)
    template.has_resource_properties(
        "AWS::EC2::Instance",
        {
            "NetworkInterfaces": [
                {
                    "DeviceIndex": "0",
                    "NetworkInterfaceId": {
                        "Ref": assertions.Match.any_value(),
                    },
                }
            ]
        },
    )
