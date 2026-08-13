#!/usr/bin/env python3
"""Read-only AWS validation before an AWS PCS deployment."""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable

import boto3
from botocore.exceptions import BotoCoreError, ClientError


FSX_FILE_SYSTEM_ID_PATTERN = re.compile(r"(fs-[0-9a-f]+)")
ONTAP_SVM_ID_PATTERN = re.compile(r"^(svm-[0-9a-f]+)\.")
PCS_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9-]+$")
DERIVED_CNG_CLUSTER_NAME_MAX = 17


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def csv_values(name: str, default: str = "") -> list[str]:
    return [
        value.strip() for value in os.getenv(name, default).split(",") if value.strip()
    ]


def enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def validate_cluster_name(cluster_name: str) -> None:
    if (
        not 3 <= len(cluster_name) <= DERIVED_CNG_CLUSTER_NAME_MAX
        or not PCS_NAME_PATTERN.fullmatch(cluster_name)
        or cluster_name.startswith("pcs_")
    ):
        raise ValueError(
            "PCS_CLUSTER_NAME must be 3-17 characters, start with a letter, "
            "and contain only letters, digits, or hyphens. The 17-character "
            "limit keeps compute-<cluster-name> within the AWS PCS "
            "25-character Compute Node Group name limit."
        )


def route_table_for_subnet(ec2, vpc_id: str, subnet_id: str) -> dict:
    response = ec2.describe_route_tables(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "association.subnet-id", "Values": [subnet_id]},
        ]
    )
    if response["RouteTables"]:
        return response["RouteTables"][0]
    response = ec2.describe_route_tables(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "association.main", "Values": ["true"]},
        ]
    )
    if not response["RouteTables"]:
        raise ValueError(f"No route table found for subnet {subnet_id}")
    return response["RouteTables"][0]


def has_service_egress(route_table: dict) -> bool:
    for route in route_table.get("Routes", []):
        if route.get("DestinationCidrBlock") != "0.0.0.0/0":
            continue
        if route.get("State") != "active":
            continue
        if any(
            route.get(key)
            for key in (
                "NatGatewayId",
                "TransitGatewayId",
                "InstanceId",
                "NetworkInterfaceId",
            )
        ):
            return True
    return False


def gateway_endpoint_route_tables(ec2, vpc_id: str, service_name: str) -> set[str]:
    response = ec2.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "service-name", "Values": [service_name]},
            {"Name": "vpc-endpoint-state", "Values": ["available"]},
        ]
    )
    route_tables: set[str] = set()
    for endpoint in response["VpcEndpoints"]:
        route_tables.update(endpoint.get("RouteTableIds", []))
    return route_tables


def interface_endpoint_services(ec2, vpc_id: str) -> set[str]:
    response = ec2.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "vpc-endpoint-state", "Values": ["available"]},
        ]
    )
    return {
        endpoint["ServiceName"]
        for endpoint in response["VpcEndpoints"]
        if endpoint.get("VpcEndpointType") == "Interface"
        and endpoint.get("PrivateDnsEnabled")
    }


def validate_offerings(
    ec2, instance_types: Iterable[str], availability_zones: Iterable[str]
) -> None:
    zones = sorted(set(availability_zones))
    for instance_type in sorted(set(instance_types)):
        offered_zones = {
            item["Location"]
            for item in ec2.describe_instance_type_offerings(
                LocationType="availability-zone",
                Filters=[
                    {"Name": "instance-type", "Values": [instance_type]},
                    {"Name": "location", "Values": zones},
                ],
            )["InstanceTypeOfferings"]
        }
        if not offered_zones:
            raise ValueError(
                f"{instance_type} is not offered in configured AZs: {', '.join(zones)}"
            )


def describe_instance_types(ec2, instance_types: Iterable[str]) -> dict[str, dict]:
    unique_types = sorted(set(instance_types))
    response = ec2.describe_instance_types(InstanceTypes=unique_types)
    descriptions = {item["InstanceType"]: item for item in response["InstanceTypes"]}
    missing = sorted(set(unique_types) - descriptions.keys())
    if missing:
        raise ValueError("Unable to describe instance types: " + ", ".join(missing))
    return descriptions


def validate_compute_profile(
    descriptions: dict[str, dict],
    *,
    compute_max_count: int,
    require_instance_store: bool,
) -> int:
    if compute_max_count < 1:
        raise ValueError("PCS_COMPUTE_MAX_COUNT must be at least 1")
    if require_instance_store:
        without_instance_store = [
            instance_type
            for instance_type, description in descriptions.items()
            if not description.get("InstanceStorageSupported", False)
        ]
        if without_instance_store:
            raise ValueError(
                "PCS_ENABLE_LOCAL_SCRATCH=1 requires instance-store support: "
                + ", ".join(sorted(without_instance_store))
            )
    minimum_cores_per_node = min(
        int(description["VCpuInfo"]["DefaultCores"])
        for description in descriptions.values()
    )
    return minimum_cores_per_node * compute_max_count


def validate_storage_security_groups(
    ec2,
    *,
    vpc_id: str,
    security_group_ids: Iterable[str],
) -> None:
    group_ids = sorted(set(security_group_ids))
    if not group_ids:
        return
    groups = ec2.describe_security_groups(GroupIds=group_ids)["SecurityGroups"]
    if {group["GroupId"] for group in groups} != set(group_ids):
        raise ValueError("One or more storage security groups do not exist")
    wrong_vpc = [group["GroupId"] for group in groups if group["VpcId"] != vpc_id]
    if wrong_vpc:
        raise ValueError(
            "Storage security groups must belong to VPC_ID: " + ", ".join(wrong_vpc)
        )


def discover_fsx_security_group(ec2, fsx, *, dns_name: str) -> str:
    match = FSX_FILE_SYSTEM_ID_PATTERN.search(dns_name)
    if not match:
        raise ValueError(f"Unable to extract an FSx file system ID from {dns_name}")
    file_system_id = match.group(1)
    file_systems = fsx.describe_file_systems(FileSystemIds=[file_system_id]).get(
        "FileSystems", []
    )
    if len(file_systems) != 1:
        raise ValueError(f"Unable to describe FSx file system {file_system_id}")
    network_interface_ids = file_systems[0].get("NetworkInterfaceIds", [])
    if not network_interface_ids:
        raise ValueError(
            f"FSx file system {file_system_id} has no discoverable network interfaces"
        )
    interfaces = ec2.describe_network_interfaces(
        NetworkInterfaceIds=network_interface_ids
    ).get("NetworkInterfaces", [])
    security_group_ids = sorted(
        {
            group["GroupId"]
            for interface in interfaces
            for group in interface.get("Groups", [])
        }
    )
    if len(security_group_ids) != 1:
        discovered = ", ".join(security_group_ids) or "none"
        raise ValueError(
            f"FSx file system {file_system_id} has {len(security_group_ids)} "
            f"attached security groups ({discovered}). Set the storage security "
            "group ID explicitly."
        )
    return security_group_ids[0]


def validate_openzfs_endpoint(
    fsx,
    *,
    dns_name: str,
    vpc_id: str,
    subnet_id: str,
) -> None:
    match = FSX_FILE_SYSTEM_ID_PATTERN.search(dns_name)
    if not match:
        raise ValueError("PCS_OPENZFS_DNS must contain an FSx file system ID")
    file_system = fsx.describe_file_systems(FileSystemIds=[match.group(1)])[
        "FileSystems"
    ][0]
    if file_system["FileSystemType"] != "OPENZFS":
        raise ValueError(f"{dns_name} is not an FSx for OpenZFS endpoint")
    if file_system["VpcId"] != vpc_id:
        raise ValueError("FSx for OpenZFS must belong to VPC_ID")
    if subnet_id not in file_system.get("SubnetIds", []):
        raise ValueError(
            "FSx for OpenZFS must use PCS_SUBNET_ID so EDA compute and "
            "storage remain in the same Availability Zone"
        )


def validate_ontap_endpoint(fsx, *, dns_name: str, vpc_id: str) -> None:
    match = ONTAP_SVM_ID_PATTERN.search(dns_name)
    if not match:
        raise ValueError("PCS_ONTAP_SVM_NFS_DNS must start with an ONTAP SVM ID")
    svm = fsx.describe_storage_virtual_machines(
        StorageVirtualMachineIds=[match.group(1)]
    )["StorageVirtualMachines"][0]
    file_system = fsx.describe_file_systems(FileSystemIds=[svm["FileSystemId"]])[
        "FileSystems"
    ][0]
    if file_system["FileSystemType"] != "ONTAP":
        raise ValueError(f"{dns_name} is not an FSx for ONTAP endpoint")
    if file_system["VpcId"] != vpc_id:
        raise ValueError("FSx for ONTAP must belong to VPC_ID")


def main() -> int:
    region = os.getenv("REGION") or os.getenv("AWS_REGION") or "ap-northeast-2"
    cluster_name = required("PCS_CLUSTER_NAME")
    validate_cluster_name(cluster_name)
    vpc_id = required("PCS_VPC_ID")
    subnet_id = required("PCS_SUBNET_ID")
    ami_id = required("PCS_AMI_ID")
    slurm_version = os.getenv("PCS_SLURM_VERSION", "25.11")
    root_device_name = os.getenv("PCS_ROOT_DEVICE_NAME", "/dev/xvda")
    instance_types = csv_values("PCS_COMPUTE_INSTANCE_TYPES", "x8aedz.24xlarge")
    compute_max_count = int(os.getenv("PCS_COMPUTE_MAX_COUNT", "9"))
    if enabled("PCS_ENABLE_LOGIN_NODE", True):
        instance_types.append(os.getenv("PCS_LOGIN_INSTANCE_TYPE", "m7i.2xlarge"))

    if slurm_version not in {"25.05", "25.11"}:
        raise ValueError("PCS_SLURM_VERSION must be 25.05 or 25.11 for new clusters")
    ec2 = boto3.client("ec2", region_name=region)
    fsx = boto3.client("fsx", region_name=region)
    pcs = boto3.client("pcs", region_name=region)
    subnets = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"]
    if len(subnets) != 1:
        raise ValueError(f"PCS subnet {subnet_id} does not exist")
    subnet = subnets[0]
    if subnet["VpcId"] != vpc_id:
        raise ValueError("PCS_SUBNET_ID must belong to PCS_VPC_ID")

    vpc = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    if vpc.get("InstanceTenancy") != "default":
        raise ValueError("AWS PCS requires VPC instance tenancy 'default'")
    for attribute in ("enableDnsSupport", "enableDnsHostnames"):
        response = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute=attribute)
        response_key = attribute[0].upper() + attribute[1:]
        if not response[response_key]["Value"]:
            raise ValueError(f"VPC {attribute} must be enabled for AWS PCS")

    image = ec2.describe_images(ImageIds=[ami_id])["Images"]
    if not image or image[0]["State"] != "available":
        raise ValueError(f"PCS AMI {ami_id} is not available")
    image = image[0]
    if image.get("Architecture") != "x86_64":
        raise ValueError("This EDA PCS profile currently requires an x86_64 AMI")
    if image.get("RootDeviceName") != root_device_name:
        raise ValueError(
            f"PCS_ROOT_DEVICE_NAME={root_device_name} does not match AMI root "
            f"device {image.get('RootDeviceName')}"
        )
    image_name = image.get("Name", "")
    if image_name.startswith("aws-pcs-sample_ami") and not enabled(
        "PCS_ALLOW_SAMPLE_AMI", False
    ):
        raise ValueError(
            "AWS PCS sample AMIs are demonstration-only. Set "
            "PCS_ALLOW_SAMPLE_AMI=1 for a PoC or use a validated custom AMI."
        )
    if "slurm-" in image_name and f"slurm-{slurm_version}" not in image_name:
        raise ValueError(f"AMI {image_name} does not match Slurm {slurm_version}")

    availability_zone = subnet["AvailabilityZone"]
    validate_offerings(
        ec2,
        csv_values("PCS_COMPUTE_INSTANCE_TYPES", "x8aedz.24xlarge"),
        [availability_zone],
    )
    compute_type_descriptions = describe_instance_types(
        ec2,
        csv_values("PCS_COMPUTE_INSTANCE_TYPES", "x8aedz.24xlarge"),
    )
    guaranteed_physical_cores = validate_compute_profile(
        compute_type_descriptions,
        compute_max_count=compute_max_count,
        require_instance_store=enabled("PCS_ENABLE_LOCAL_SCRATCH", True),
    )
    if enabled("PCS_ENABLE_LOGIN_NODE", True):
        validate_offerings(
            ec2,
            [os.getenv("PCS_LOGIN_INSTANCE_TYPE", "m7i.2xlarge")],
            [availability_zone],
        )

    route_tables = {subnet_id: route_table_for_subnet(ec2, vpc_id, subnet_id)}
    needs_s3 = (
        enabled("PCS_ENABLE_OPENZFS_MOUNTS", True)
        or enabled("PCS_ENABLE_ONTAP_MOUNTS", False)
        or enabled("PCS_ENABLE_CLOUDWATCH_LIFECYCLE_LOGS", True)
    )
    if needs_s3:
        s3_service = f"com.amazonaws.{region}.s3"
        s3_endpoint_tables = gateway_endpoint_route_tables(ec2, vpc_id, s3_service)
        for subnet_id, route_table in route_tables.items():
            if route_table[
                "RouteTableId"
            ] not in s3_endpoint_tables and not has_service_egress(route_table):
                raise ValueError(
                    f"Subnet {subnet_id} has neither service egress nor an S3 "
                    "gateway endpoint. PCS lifecycle scripts cannot be downloaded."
                )

    interface_services = interface_endpoint_services(ec2, vpc_id)
    if enabled("PCS_ENABLE_SSM", True):
        required_services = {
            f"com.amazonaws.{region}.ssm",
            f"com.amazonaws.{region}.ssmmessages",
            f"com.amazonaws.{region}.ec2messages",
        }
        if not required_services.issubset(interface_services) and not all(
            has_service_egress(route_table) for route_table in route_tables.values()
        ):
            missing = sorted(required_services - interface_services)
            raise ValueError(
                "SSM is enabled but private connectivity is missing for: "
                + ", ".join(missing)
            )

    key_pair_name = os.getenv("PCS_KEY_PAIR_NAME", "").strip()
    if key_pair_name:
        ec2.describe_key_pairs(KeyNames=[key_pair_name])
    elif os.getenv("PCS_SSH_CIDR", "").strip():
        if not os.getenv("PCS_SHARED_STACK_PREFIX", "").strip():
            raise ValueError(
                "PCS_KEY_PAIR_NAME is required for SSH when "
                "PCS_SHARED_STACK_PREFIX is empty"
            )
        raise ValueError(
            "PCS_KEY_PAIR_NAME was not resolved from Shared Foundation "
            "CloudFormation outputs"
        )

    license_sg_id = os.getenv("PCS_LICENSE_SECURITY_GROUP_ID", "").strip()
    license_manager_port = os.getenv("PCS_LICENSE_MANAGER_PORT", "").strip()
    license_vendor_port = os.getenv("PCS_LICENSE_VENDOR_PORT", "").strip()
    direct_license_values = (
        license_sg_id,
        license_manager_port,
        license_vendor_port,
    )
    if enabled("PCS_ENABLE_LICENSE_ACCESS", True):
        if all(direct_license_values):
            for port_name, port_value in (
                ("PCS_LICENSE_MANAGER_PORT", license_manager_port),
                ("PCS_LICENSE_VENDOR_PORT", license_vendor_port),
            ):
                if not port_value.isdigit() or not 1 <= int(port_value) <= 65535:
                    raise ValueError(f"{port_name} must be between 1 and 65535")
        elif any(direct_license_values):
            raise ValueError(
                "PCS_LICENSE_SECURITY_GROUP_ID, PCS_LICENSE_MANAGER_PORT, and "
                "PCS_LICENSE_VENDOR_PORT must be set together"
            )
        else:
            raise ValueError(
                "Set all direct PCS license values or deploy a Shared Foundation "
                "with license CloudFormation outputs"
            )

    openzfs_dns = os.getenv("PCS_OPENZFS_DNS", "").strip()
    openzfs_sg_id = os.getenv("PCS_OPENZFS_SECURITY_GROUP_ID", "").strip()
    ontap_dns = os.getenv("PCS_ONTAP_SVM_NFS_DNS", "").strip()
    ontap_sg_id = os.getenv("PCS_ONTAP_SECURITY_GROUP_ID", "").strip()
    if openzfs_dns and not openzfs_sg_id:
        openzfs_sg_id = discover_fsx_security_group(
            ec2,
            fsx,
            dns_name=openzfs_dns,
        )
    if ontap_dns and not ontap_sg_id:
        ontap_sg_id = discover_fsx_security_group(
            ec2,
            fsx,
            dns_name=ontap_dns,
        )
    if enabled("PCS_ENABLE_OPENZFS_MOUNTS", True):
        if not openzfs_sg_id:
            raise ValueError(
                "PCS_OPENZFS_SECURITY_GROUP_ID was not resolved from Shared "
                "Foundation CloudFormation outputs"
            )
        if not openzfs_dns:
            raise ValueError(
                "PCS_OPENZFS_DNS was not resolved from Shared Foundation "
                "CloudFormation outputs"
            )
    if enabled("PCS_ENABLE_ONTAP_MOUNTS", False):
        if not ontap_sg_id:
            raise ValueError(
                "PCS_ONTAP_SECURITY_GROUP_ID was not resolved from Shared "
                "Foundation CloudFormation outputs"
            )
        if not ontap_dns:
            raise ValueError(
                "PCS_ONTAP_SVM_NFS_DNS was not resolved from Shared Foundation "
                "CloudFormation outputs"
            )
    if enabled("PCS_ENABLE_OPENZFS_MOUNTS", True):
        validate_openzfs_endpoint(
            fsx,
            dns_name=openzfs_dns,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
        )
    if enabled("PCS_ENABLE_ONTAP_MOUNTS", False):
        validate_ontap_endpoint(
            fsx,
            dns_name=ontap_dns,
            vpc_id=vpc_id,
        )
    validate_storage_security_groups(
        ec2,
        vpc_id=vpc_id,
        security_group_ids=[
            group_id
            for group_id in (openzfs_sg_id, ontap_sg_id, license_sg_id)
            if group_id
        ],
    )

    pcs.list_clusters(maxResults=1)
    print(
        "PCS preflight passed: "
        f"region={region}, vpc={vpc_id}, ami={ami_id}, "
        f"slurm={slurm_version}, compute={','.join(instance_types)}, "
        f"physical_cores={guaranteed_physical_cores}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, BotoCoreError, ClientError) as exc:
        print(f"PCS preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
