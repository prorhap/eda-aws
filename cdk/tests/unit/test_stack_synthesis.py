import string

import aws_cdk as cdk
import pytest
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
            "StorageCapacity": 32768,
            "OpenZFSConfiguration": {
                "DeploymentType": "SINGLE_AZ_HA_2",
                "ThroughputCapacity": 7680,
                "DiskIopsConfiguration": {
                    "Mode": "USER_PROVISIONED",
                    "Iops": 300000,
                },
            },
        },
    )
    volumes = template.find_resources("AWS::FSx::Volume")
    assert len(volumes) == 3
    assert all(
        resource["Properties"]["OpenZFSConfiguration"]["StorageCapacityQuotaGiB"]
        <= 32768
        for resource in volumes.values()
    )
    template.has_output(
        "OpenZfsDns",
        {
            "Export": {"Name": "eda:storage:OpenZfsDns"},
        },
    )


def test_storage_stack_accepts_16_tib_openzfs_profile():
    app = cdk.App(
        context={
            "eda:enable_openzfs": True,
            "eda:enable_ontap": False,
            "eda:openzfs_size_gib": 16_384,
            "eda:openzfs_throughput": 5_120,
            "eda:openzfs_iops": 200_000,
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
            "StorageCapacity": 16_384,
            "OpenZFSConfiguration": {
                "ThroughputCapacity": 5_120,
                "DiskIopsConfiguration": {
                    "Mode": "USER_PROVISIONED",
                    "Iops": 200_000,
                },
            },
        },
    )


@pytest.mark.parametrize(
    ("context", "match"),
    [
        (
            {
                "eda:enable_openzfs": True,
                "eda:enable_ontap": False,
                "eda:openzfs_size_gib": 16_384,
                "eda:openzfs_throughput": 2560,
                "eda:openzfs_iops": 49_151,
            },
            "at least 49152",
        ),
        (
            {
                "eda:enable_openzfs": True,
                "eda:enable_ontap": False,
                "eda:openzfs_size_gib": 16_384,
                "eda:openzfs_throughput": 2560,
                "eda:openzfs_iops": 102_401,
            },
            "tier maximum of 102400",
        ),
    ],
)
def test_openzfs_iops_validation(context, match):
    app = cdk.App(context=context)
    vpc, subnet, _, fsx_sg, ontap_sg = imported_network(app)

    with pytest.raises(ValueError, match=match):
        StorageStack(
            app,
            "EdaStorage",
            vpc=vpc,
            sg_fsx=fsx_sg,
            sg_ontap=ontap_sg,
            primary_subnet=subnet,
            env=ENV,
        )


def test_storage_resources_are_scoped_by_stack_prefix():
    app = cdk.App(
        context={
            "eda:stack_prefix": "EdaProd",
            "eda:enable_openzfs": True,
            "eda:enable_ontap": False,
        }
    )
    vpc, subnet, _, fsx_sg, ontap_sg = imported_network(app)
    stack = StorageStack(
        app,
        "EdaProdStorage",
        vpc=vpc,
        sg_fsx=fsx_sg,
        sg_ontap=ontap_sg,
        primary_subnet=subnet,
        env=ENV,
    )
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::KMS::Alias",
        {"AliasName": "alias/edaprod/fsx-openzfs"},
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": "/edaprod/storage/OpenZfsDns"},
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
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": "/eda/storage/OntapSvmNfsDns"},
    )
    template.resource_count_is("Custom::AWS", 1)


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
        "AWS::EC2::SecurityGroupIngress",
        {"IpProtocol": "tcp", "FromPort": 27000, "ToPort": 27000},
    )
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"IpProtocol": "tcp", "FromPort": 27020, "ToPort": 27020},
    )
    template.has_resource_properties(
        "AWS::EC2::Instance",
        {
            "MetadataOptions": {
                "HttpEndpoint": "enabled",
                "HttpPutResponseHopLimit": 1,
                "HttpTokens": "required",
                "InstanceMetadataTags": "disabled",
            },
            "NetworkInterfaces": [
                {
                    "DeviceIndex": "0",
                    "NetworkInterfaceId": {
                        "Ref": assertions.Match.any_value(),
                    },
                }
            ],
        },
    )

    ingress_descriptions = [
        resource["Properties"].get("Description", "")
        for resource in template.find_resources(
            "AWS::EC2::SecurityGroupIngress"
        ).values()
    ]
    for resource in template.find_resources("AWS::EC2::SecurityGroup").values():
        ingress_descriptions.extend(
            rule.get("Description", "")
            for rule in resource["Properties"].get("SecurityGroupIngress", [])
        )

    allowed_description_chars = set(
        string.ascii_letters + string.digits + ". _-:/()#,@[]+=&;{}!$*"
    )
    assert ingress_descriptions
    assert all(
        description
        and len(description) < 256
        and set(description) <= allowed_description_chars
        for description in ingress_descriptions
    )


def test_license_resources_are_scoped_by_stack_prefix(monkeypatch):
    monkeypatch.setattr(
        ec2.MachineImage,
        "lookup",
        staticmethod(
            lambda **kwargs: ec2.MachineImage.generic_linux(
                {"ap-northeast-2": "ami-0123456789abcdef0"}
            )
        ),
    )
    app = cdk.App(context={"eda:stack_prefix": "EdaProd"})
    vpc, subnet, cluster_sg, _, _ = imported_network(app)
    stack = LicenseServerStack(
        app,
        "EdaProdLicenseServer",
        vpc=vpc,
        sg_cluster_nodes=cluster_sg,
        primary_subnet=subnet,
        env=ENV,
    )
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::EC2::KeyPair",
        {"KeyName": "edaprod-license-key-111111111111"},
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": "/edaprod/license/InstanceId"},
    )


def test_license_server_accepts_vendor_specific_ports(monkeypatch):
    monkeypatch.setattr(
        ec2.MachineImage,
        "lookup",
        staticmethod(
            lambda **kwargs: ec2.MachineImage.generic_linux(
                {"ap-northeast-2": "ami-0123456789abcdef0"}
            )
        ),
    )
    app = cdk.App(
        context={
            "eda:license_manager_port": 28000,
            "eda:license_vendor_port": 28020,
        }
    )
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

    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"IpProtocol": "tcp", "FromPort": 28000, "ToPort": 28000},
    )
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"IpProtocol": "tcp", "FromPort": 28020, "ToPort": 28020},
    )


@pytest.fixture
def stub_ami_lookup(monkeypatch):
    monkeypatch.setattr(
        ec2.MachineImage,
        "lookup",
        staticmethod(
            lambda **kwargs: ec2.MachineImage.generic_linux(
                {"ap-northeast-2": "ami-0123456789abcdef0"}
            )
        ),
    )


def synth_license_stack(context):
    app = cdk.App(context=context)
    vpc, subnet, cluster_sg, _, _ = imported_network(app)
    stack = LicenseServerStack(
        app,
        "EdaLicenseServer",
        vpc=vpc,
        sg_cluster_nodes=cluster_sg,
        primary_subnet=subnet,
        env=ENV,
    )
    return assertions.Template.from_stack(stack)


def test_license_identity_is_retained_without_inline_rules(stub_ami_lookup):
    """ENI + SG survive stack deletion so the license host ID (MAC) is preserved.

    The SG must carry no inline ingress rule: a retained SG keeps its inline rules,
    which would collide with the rules a later deploy adds to the same group.
    """
    template = synth_license_stack({})

    for resource in template.find_resources("AWS::EC2::NetworkInterface").values():
        assert resource["DeletionPolicy"] == "Retain"
    for resource in template.find_resources("AWS::EC2::SecurityGroup").values():
        assert resource["DeletionPolicy"] == "Retain"
        assert "SecurityGroupIngress" not in resource["Properties"]
    # SSH plus both license daemon ports, all as standalone rule resources.
    template.resource_count_is("AWS::EC2::SecurityGroupIngress", 3)
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "CidrIp": "0.0.0.0/0"},
    )
    template.has_output("LicenseEniMode", {"Value": "created-retained"})


def test_license_identity_retention_can_be_disabled(stub_ami_lookup):
    template = synth_license_stack({"eda:license_retain_eni": False})

    for resource in template.find_resources("AWS::EC2::NetworkInterface").values():
        assert "DeletionPolicy" not in resource
    for resource in template.find_resources("AWS::EC2::SecurityGroup").values():
        assert "DeletionPolicy" not in resource
    template.has_output("LicenseEniMode", {"Value": "created-ephemeral"})


def test_license_server_reuses_preserved_eni_and_security_group(stub_ami_lookup):
    eni_id = "eni-0123456789abcdef0"
    sg_id = "sg-0fedcba9876543210"
    template = synth_license_stack(
        {"eda:license_eni_id": eni_id, "eda:license_sg_id": sg_id}
    )

    # Neither resource is recreated — the preserved pair keeps MAC and private IP.
    template.resource_count_is("AWS::EC2::NetworkInterface", 0)
    template.resource_count_is("AWS::EC2::SecurityGroup", 0)
    template.has_resource_properties(
        "AWS::EC2::Instance",
        {"NetworkInterfaces": [{"DeviceIndex": "0", "NetworkInterfaceId": eni_id}]},
    )
    template.resource_count_is("AWS::EC2::SecurityGroupIngress", 3)
    for resource in template.find_resources("AWS::EC2::SecurityGroupIngress").values():
        assert resource["Properties"]["GroupId"] == sg_id
    template.has_output("LicenseEniId", {"Value": eni_id})
    template.has_output("LicenseEniMode", {"Value": "reused-existing"})


def test_license_server_uses_custom_ami(stub_ami_lookup):
    template = synth_license_stack({"eda:license_ami_id": "ami-0abcdef1234567890"})

    template.has_resource_properties(
        "AWS::EC2::Instance", {"ImageId": "ami-0abcdef1234567890"}
    )


@pytest.mark.parametrize(
    ("context", "message"),
    [
        ({"eda:license_eni_id": "eni-0123456789abcdef0"}, "must be set together"),
        ({"eda:license_sg_id": "sg-0123456789abcdef0"}, "must be set together"),
        ({"eda:license_eni_id": "eni-nothex"}, "eda:license_eni_id must match"),
        ({"eda:license_ami_id": "i-0123456789abcdef0"}, "eda:license_ami_id must match"),
    ],
)
def test_license_server_rejects_invalid_identity_context(
    stub_ami_lookup, context, message
):
    app = cdk.App(context=context)
    vpc, subnet, cluster_sg, _, _ = imported_network(app)

    with pytest.raises(ValueError, match=message):
        LicenseServerStack(
            app,
            "EdaLicenseServer",
            vpc=vpc,
            sg_cluster_nodes=cluster_sg,
            primary_subnet=subnet,
            env=ENV,
        )


@pytest.mark.parametrize(
    ("context", "message"),
    [
        ({"eda:license_manager_port": 0}, "between 1 and 65535"),
        (
            {
                "eda:license_manager_port": 27020,
                "eda:license_vendor_port": 27020,
            },
            "must differ",
        ),
    ],
)
def test_license_server_rejects_invalid_ports(monkeypatch, context, message):
    monkeypatch.setattr(
        ec2.MachineImage,
        "lookup",
        staticmethod(
            lambda **kwargs: ec2.MachineImage.generic_linux(
                {"ap-northeast-2": "ami-0123456789abcdef0"}
            )
        ),
    )
    app = cdk.App(context=context)
    vpc, subnet, cluster_sg, _, _ = imported_network(app)

    with pytest.raises(ValueError, match=message):
        LicenseServerStack(
            app,
            "EdaLicenseServer",
            vpc=vpc,
            sg_cluster_nodes=cluster_sg,
            primary_subnet=subnet,
            env=ENV,
        )
