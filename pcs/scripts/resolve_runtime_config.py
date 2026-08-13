#!/usr/bin/env python3
"""Resolve AWS-backed PCS values before CDK synthesis."""

from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

try:
    from scripts.preflight import discover_fsx_security_group, enabled
except ModuleNotFoundError:
    from preflight import discover_fsx_security_group, enabled


def emit(name: str, value: str) -> None:
    print(f"{name}\t{value}")


def foundation_stack_name(prefix: str, component: str) -> str:
    suffixes = {
        "base": "Base",
        "storage": "Storage",
        "license": "LicenseServer",
    }
    return f"{prefix}{suffixes[component]}"


def foundation_export_name(prefix: str, component: str, output_name: str) -> str:
    return f"{prefix.lower()}:{component}:{output_name}"


def foundation_exports(cloudformation) -> dict[str, str]:
    exports: dict[str, str] = {}
    next_token: str | None = None
    while True:
        kwargs = {"NextToken": next_token} if next_token else {}
        response = cloudformation.list_exports(**kwargs)
        exports.update(
            {export["Name"]: export["Value"] for export in response.get("Exports", [])}
        )
        next_token = response.get("NextToken")
        if not next_token:
            return exports


def stack_outputs(cloudformation, stack_name: str) -> dict[str, str]:
    response = cloudformation.describe_stacks(StackName=stack_name)
    outputs = response["Stacks"][0].get("Outputs", [])
    return {output["OutputKey"]: output["OutputValue"] for output in outputs}


def legacy_output_value(
    region: str,
    component: str,
    output_name: str,
    outputs: dict[str, str],
) -> str | None:
    if component == "storage" and output_name == "OpenZfsDns":
        file_system_id = outputs.get("OpenZfsFsId")
        if file_system_id:
            fsx = boto3.client("fsx", region_name=region)
            return fsx.describe_file_systems(FileSystemIds=[file_system_id])[
                "FileSystems"
            ][0]["DNSName"]
    if component == "storage" and output_name == "OntapSvmNfsDns":
        svm_id = outputs.get("OntapSvmId")
        if svm_id:
            fsx = boto3.client("fsx", region_name=region)
            return fsx.describe_storage_virtual_machines(
                StorageVirtualMachineIds=[svm_id]
            )["StorageVirtualMachines"][0]["Endpoints"]["Nfs"]["DNSName"]
    return None


def resolve_foundation_values(
    region: str,
    requests: tuple[tuple[str, str, str], ...],
) -> None:
    prefix = os.getenv("PCS_SHARED_STACK_PREFIX", "").strip()
    missing = tuple(
        request for request in requests if not os.getenv(request[0], "").strip()
    )
    if not missing:
        return
    if not prefix:
        return

    cloudformation = boto3.client("cloudformation", region_name=region)
    exports = foundation_exports(cloudformation)
    output_cache: dict[str, dict[str, str]] = {}
    unresolved: list[str] = []

    for config_name, component, output_name in missing:
        exported_value = exports.get(
            foundation_export_name(prefix, component, output_name)
        )
        if exported_value:
            emit(config_name, exported_value)
            continue

        stack_name = foundation_stack_name(prefix, component)
        if stack_name not in output_cache:
            try:
                output_cache[stack_name] = stack_outputs(cloudformation, stack_name)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ValidationError":
                    output_cache[stack_name] = {}
                else:
                    raise
        output_value = output_cache[stack_name].get(output_name)
        if not output_value:
            output_value = legacy_output_value(
                region,
                component,
                output_name,
                output_cache[stack_name],
            )
        if output_value:
            emit(config_name, output_value)
        else:
            unresolved.append(
                f"{stack_name}.{output_name} "
                f"(or export {foundation_export_name(prefix, component, output_name)})"
            )

    if unresolved:
        raise ValueError(
            "Shared Foundation outputs are missing: " + ", ".join(unresolved)
        )


def resolve_foundation_network(region: str) -> None:
    resolve_foundation_values(
        region,
        (
            ("PCS_VPC_ID", "base", "VpcId"),
            ("PCS_SUBNET_ID", "base", "PrimarySubnetId"),
        ),
    )


def resolve_foundation_dependencies(region: str) -> None:
    requests: list[tuple[str, str, str]] = []
    if os.getenv("PCS_SSH_CIDR", "").strip():
        requests.append(("PCS_KEY_PAIR_NAME", "base", "KeyPairName"))
    if enabled("PCS_ENABLE_OPENZFS_MOUNTS", True):
        requests.extend(
            (
                ("PCS_OPENZFS_DNS", "storage", "OpenZfsDns"),
                ("PCS_OPENZFS_SECURITY_GROUP_ID", "base", "SgFsxId"),
            )
        )
    if enabled("PCS_ENABLE_ONTAP_MOUNTS", False):
        requests.extend(
            (
                ("PCS_ONTAP_SVM_NFS_DNS", "storage", "OntapSvmNfsDns"),
                ("PCS_ONTAP_SECURITY_GROUP_ID", "base", "SgOntapId"),
            )
        )
    if enabled("PCS_ENABLE_LICENSE_ACCESS", True):
        requests.extend(
            (
                ("PCS_LICENSE_SECURITY_GROUP_ID", "license", "LicenseSgId"),
                ("PCS_LICENSE_MANAGER_PORT", "license", "LicenseManagerPort"),
                ("PCS_LICENSE_VENDOR_PORT", "license", "LicenseVendorPort"),
            )
        )
    resolve_foundation_values(region, tuple(requests))


def main() -> int:
    region = (
        os.getenv("PCS_REGION")
        or os.getenv("REGION")
        or os.getenv("AWS_REGION")
        or "ap-northeast-2"
    )
    resolve_foundation_network(region)
    resolve_foundation_dependencies(region)
    if not enabled("PCS_RESOLVE_STORAGE", True):
        return 0

    ec2 = boto3.client("ec2", region_name=region)
    fsx = boto3.client("fsx", region_name=region)

    for enabled_name, dns_name, security_group_name in (
        (
            "PCS_ENABLE_OPENZFS_MOUNTS",
            "PCS_OPENZFS_DNS",
            "PCS_OPENZFS_SECURITY_GROUP_ID",
        ),
        (
            "PCS_ENABLE_ONTAP_MOUNTS",
            "PCS_ONTAP_SVM_NFS_DNS",
            "PCS_ONTAP_SECURITY_GROUP_ID",
        ),
    ):
        if not enabled(enabled_name, enabled_name == "PCS_ENABLE_OPENZFS_MOUNTS"):
            continue
        dns_value = os.getenv(dns_name, "").strip()
        security_group_value = os.getenv(security_group_name, "").strip()
        if dns_value and not security_group_value:
            emit(
                security_group_name,
                discover_fsx_security_group(
                    ec2,
                    fsx,
                    dns_name=dns_value,
                ),
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, BotoCoreError, ClientError) as exc:
        print(f"PCS runtime configuration resolution failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
