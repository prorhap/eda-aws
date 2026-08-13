import pytest

from scripts import resolve_runtime_config
from scripts.preflight import (
    discover_fsx_security_group,
    validate_cluster_name,
    validate_compute_profile,
    validate_ontap_endpoint,
    validate_openzfs_endpoint,
    validate_storage_security_groups,
)


def instance_type(*, cores: int, instance_store: bool) -> dict:
    return {
        "VCpuInfo": {"DefaultCores": cores},
        "InstanceStorageSupported": instance_store,
    }


def test_compute_profile_reports_maximum_physical_cores():
    descriptions = {"x8aedz.24xlarge": instance_type(cores=96, instance_store=True)}

    assert (
        validate_compute_profile(
            descriptions,
            compute_max_count=9,
            require_instance_store=True,
        )
        == 864
    )


def test_cluster_name_supports_derived_login_and_compute_names():
    validate_cluster_name("eda-pcs-cluster")

    with pytest.raises(ValueError, match="3-17 characters"):
        validate_cluster_name("cluster-name-is-too-long")


def test_compute_profile_requires_instance_store_when_enabled():
    descriptions = {"m7i.4xlarge": instance_type(cores=8, instance_store=False)}

    with pytest.raises(ValueError, match="instance-store support"):
        validate_compute_profile(
            descriptions,
            compute_max_count=1,
            require_instance_store=True,
        )


class FakeEc2:
    def describe_security_groups(self, *, GroupIds):
        return {
            "SecurityGroups": [
                {"GroupId": group_id, "VpcId": "vpc-1234"} for group_id in GroupIds
            ]
        }

    def describe_network_interfaces(self, *, NetworkInterfaceIds):
        return {
            "NetworkInterfaces": [
                {
                    "NetworkInterfaceId": network_interface_id,
                    "Groups": [{"GroupId": "sg-storage"}],
                }
                for network_interface_id in NetworkInterfaceIds
            ]
        }


class FakeFsx:
    def describe_file_systems(self, *, FileSystemIds):
        file_system_id = FileSystemIds[0]
        file_system_type = "ONTAP" if file_system_id == "fs-2222" else "OPENZFS"
        return {
            "FileSystems": [
                {
                    "FileSystemId": file_system_id,
                    "FileSystemType": file_system_type,
                    "VpcId": "vpc-1234",
                    "SubnetIds": ["subnet-1111"],
                    "NetworkInterfaceIds": ["eni-1111", "eni-2222"],
                }
            ]
        }

    def describe_storage_virtual_machines(self, *, StorageVirtualMachineIds):
        assert StorageVirtualMachineIds == ["svm-1111"]
        return {
            "StorageVirtualMachines": [
                {
                    "StorageVirtualMachineId": "svm-1111",
                    "FileSystemId": "fs-2222",
                }
            ]
        }


def test_storage_validation_accepts_resources_in_the_pcs_vpc():
    validate_storage_security_groups(
        FakeEc2(),
        vpc_id="vpc-1234",
        security_group_ids=["sg-1111", "sg-2222"],
    )
    validate_openzfs_endpoint(
        FakeFsx(),
        dns_name="fs-1111.fsx.ap-northeast-2.amazonaws.com",
        vpc_id="vpc-1234",
        subnet_id="subnet-1111",
    )
    validate_ontap_endpoint(
        FakeFsx(),
        dns_name=("svm-1111.fs-2222.fsx.ap-northeast-2.amazonaws.com"),
        vpc_id="vpc-1234",
    )


def test_openzfs_validation_requires_the_pcs_subnet_for_same_az_io():
    with pytest.raises(ValueError, match="must use PCS_SUBNET_ID"):
        validate_openzfs_endpoint(
            FakeFsx(),
            dns_name="fs-1111.fsx.ap-northeast-2.amazonaws.com",
            vpc_id="vpc-1234",
            subnet_id="subnet-other",
        )


def test_storage_security_group_is_discovered_from_fsx_network_interfaces():
    assert (
        discover_fsx_security_group(
            FakeEc2(),
            FakeFsx(),
            dns_name="fs-1111.fsx.ap-northeast-2.amazonaws.com",
        )
        == "sg-storage"
    )


def test_storage_security_group_discovery_rejects_ambiguous_groups():
    class AmbiguousEc2(FakeEc2):
        def describe_network_interfaces(self, *, NetworkInterfaceIds):
            return {
                "NetworkInterfaces": [
                    {
                        "NetworkInterfaceId": NetworkInterfaceIds[0],
                        "Groups": [
                            {"GroupId": "sg-storage"},
                            {"GroupId": "sg-shared"},
                        ],
                    }
                ]
            }

    with pytest.raises(ValueError, match="Set the storage security group ID"):
        discover_fsx_security_group(
            AmbiguousEc2(),
            FakeFsx(),
            dns_name="fs-1111.fsx.ap-northeast-2.amazonaws.com",
        )


def test_foundation_network_values_are_resolved_when_not_overridden(
    monkeypatch, capsys
):
    class FakeCloudFormation:
        def list_exports(self):
            return {"Exports": []}

        def describe_stacks(self, *, StackName):
            assert StackName == "EdaBase"
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {"OutputKey": "VpcId", "OutputValue": "vpc-1234"},
                            {
                                "OutputKey": "PrimarySubnetId",
                                "OutputValue": "subnet-1234",
                            },
                        ]
                    }
                ]
            }

    monkeypatch.setenv("PCS_SHARED_STACK_PREFIX", "Eda")
    monkeypatch.delenv("PCS_VPC_ID", raising=False)
    monkeypatch.delenv("PCS_SUBNET_ID", raising=False)
    monkeypatch.setattr(
        resolve_runtime_config.boto3,
        "client",
        lambda service, **_: (
            FakeCloudFormation() if service == "cloudformation" else None
        ),
    )

    resolve_runtime_config.resolve_foundation_network("ap-northeast-2")

    assert capsys.readouterr().out.splitlines() == [
        "PCS_VPC_ID\tvpc-1234",
        "PCS_SUBNET_ID\tsubnet-1234",
    ]


def test_foundation_exports_take_priority_over_stack_outputs(monkeypatch, capsys):
    class FakeCloudFormation:
        def list_exports(self):
            return {
                "Exports": [
                    {"Name": "eda:base:VpcId", "Value": "vpc-exported"},
                    {
                        "Name": "eda:base:PrimarySubnetId",
                        "Value": "subnet-exported",
                    },
                ]
            }

        def describe_stacks(self, *, StackName):
            pytest.fail(f"DescribeStacks must not be called for {StackName}")

    monkeypatch.setenv("PCS_SHARED_STACK_PREFIX", "Eda")
    monkeypatch.delenv("PCS_VPC_ID", raising=False)
    monkeypatch.delenv("PCS_SUBNET_ID", raising=False)
    monkeypatch.setattr(
        resolve_runtime_config.boto3,
        "client",
        lambda service, **_: (
            FakeCloudFormation() if service == "cloudformation" else None
        ),
    )

    resolve_runtime_config.resolve_foundation_network("ap-northeast-2")

    assert capsys.readouterr().out.splitlines() == [
        "PCS_VPC_ID\tvpc-exported",
        "PCS_SUBNET_ID\tsubnet-exported",
    ]


def test_legacy_storage_output_resolves_openzfs_dns_from_file_system_id(monkeypatch):
    class FakeFsx:
        def describe_file_systems(self, *, FileSystemIds):
            assert FileSystemIds == ["fs-1234"]
            return {
                "FileSystems": [{"DNSName": "fs-1234.fsx.ap-northeast-2.amazonaws.com"}]
            }

    monkeypatch.setattr(
        resolve_runtime_config.boto3,
        "client",
        lambda service, **_: FakeFsx() if service == "fsx" else None,
    )

    assert (
        resolve_runtime_config.legacy_output_value(
            "ap-northeast-2",
            "storage",
            "OpenZfsDns",
            {"OpenZfsFsId": "fs-1234"},
        )
        == "fs-1234.fsx.ap-northeast-2.amazonaws.com"
    )


def test_foundation_network_resolution_respects_explicit_overrides(monkeypatch, capsys):
    monkeypatch.setenv("PCS_SHARED_STACK_PREFIX", "Eda")
    monkeypatch.setenv("PCS_VPC_ID", "vpc-explicit")
    monkeypatch.setenv("PCS_SUBNET_ID", "subnet-explicit")
    monkeypatch.setattr(
        resolve_runtime_config.boto3,
        "client",
        lambda *_args, **_kwargs: pytest.fail("CloudFormation must not be called"),
    )

    resolve_runtime_config.resolve_foundation_network("ap-northeast-2")

    assert capsys.readouterr().out == ""
