"""Validated configuration for the AWS PCS CDK application."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import aws_cdk as cdk


_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9-]+$")
_AMI_PATTERN = re.compile(r"^ami-[0-9a-f]+$")
_RESOURCE_ID_PATTERN = re.compile(r"^[a-z]+-[0-9a-f]+$")
SUPPORTED_SLURM_VERSIONS = ("25.05", "25.11")
CLUSTER_SIZES = ("SMALL", "MEDIUM", "LARGE")
PURCHASE_OPTIONS = ("ONDEMAND", "SPOT")
DERIVED_CNG_CLUSTER_NAME_MAX = 17
LOG_RETENTION_DAYS = (
    1,
    3,
    5,
    7,
    14,
    30,
    60,
    90,
    120,
    150,
    180,
    365,
    400,
    545,
    731,
    1096,
    1827,
    2192,
    2557,
    2922,
    3288,
    3653,
)


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int, key: str) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _csv(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return tuple(item.strip() for item in str(value).split(",") if item.strip())


def _optional_context(
    node: cdk.Node,
    key: str,
    *,
    default: str | None = None,
) -> str | None:
    value = node.try_get_context(key)
    if value is None:
        return default
    return str(value).strip() or None


def _required(node: cdk.Node, key: str) -> str:
    value = node.try_get_context(key)
    if value is None or not str(value).strip():
        raise ValueError(
            f"Context '{key}' is required. Set it in pcs/cdk.json or pass -c {key}=..."
        )
    return str(value).strip()


def _validate_name(value: str, key: str, maximum: int) -> None:
    if not 3 <= len(value) <= maximum or not _NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{key} must be 3-{maximum} characters, start with a letter, "
            "and contain only letters, digits, or hyphens"
        )
    if value.startswith("pcs_"):
        raise ValueError(f"{key} must not start with pcs_")


@dataclass(frozen=True)
class PcsConfig:
    stack_name: str
    shared_stack_prefix: str | None
    cluster_name: str
    cluster_size: str
    slurm_version: str
    vpc_id: str
    subnet_id: str
    ami_id: str
    login_instance_type: str
    compute_instance_types: tuple[str, ...]
    compute_min_count: int
    compute_max_count: int
    queue_name: str
    purchase_option: str
    spot_allocation_strategy: str
    enable_login_node: bool
    ssh_cidr: str | None
    enable_ssm: bool
    key_pair_name: str | None
    enable_license_access: bool
    license_security_group_id: str | None
    license_manager_port: int | None
    license_vendor_port: int | None
    enable_accounting: bool
    accounting_purge_days: int
    scale_down_idle_seconds: int
    enable_openzfs_mounts: bool
    openzfs_dns: str | None
    openzfs_security_group_id: str | None
    enable_ontap_mounts: bool
    ontap_svm_nfs_dns: str | None
    ontap_security_group_id: str | None
    enable_local_scratch: bool
    local_scratch_mount_point: str
    enable_cloudwatch_lifecycle_logs: bool
    enable_scheduler_log_delivery: bool
    enable_job_completion_log_delivery: bool
    enable_scheduler_audit_log_delivery: bool
    log_retention_days: int
    create_pcs_vpc_endpoint: bool
    root_device_name: str
    login_root_volume_gib: int
    compute_root_volume_gib: int

    @classmethod
    def from_app(cls, app: cdk.App) -> "PcsConfig":
        node = app.node
        config = cls(
            stack_name=str(node.try_get_context("pcs:stack_name") or "EdaPcs"),
            shared_stack_prefix=_optional_context(
                node,
                "pcs:shared_stack_prefix",
                default="Edb",
            ),
            cluster_name=str(
                node.try_get_context("pcs:cluster_name") or "eda-pcs-cluster"
            ),
            cluster_size=str(
                node.try_get_context("pcs:cluster_size") or "SMALL"
            ).upper(),
            slurm_version=str(node.try_get_context("pcs:slurm_version") or "25.11"),
            vpc_id=_required(node, "pcs:vpc_id"),
            subnet_id=_required(node, "pcs:subnet_id"),
            ami_id=_required(node, "pcs:ami_id"),
            login_instance_type=str(
                node.try_get_context("pcs:login_instance_type") or "m7i.2xlarge"
            ),
            compute_instance_types=(
                _csv(node.try_get_context("pcs:compute_instance_types"))
                or ("x8aedz.24xlarge",)
            ),
            compute_min_count=_int(
                node.try_get_context("pcs:compute_min_count"),
                0,
                "pcs:compute_min_count",
            ),
            compute_max_count=_int(
                node.try_get_context("pcs:compute_max_count"),
                9,
                "pcs:compute_max_count",
            ),
            queue_name=str(
                node.try_get_context("pcs:queue_name") or "default-eda-queue"
            ),
            purchase_option=str(
                node.try_get_context("pcs:purchase_option") or "ONDEMAND"
            ).upper(),
            spot_allocation_strategy=str(
                node.try_get_context("pcs:spot_allocation_strategy")
                or "price-capacity-optimized"
            ),
            enable_login_node=_bool(
                node.try_get_context("pcs:enable_login_node"), True
            ),
            ssh_cidr=(
                str(node.try_get_context("pcs:ssh_cidr")).strip()
                if node.try_get_context("pcs:ssh_cidr")
                else None
            ),
            enable_ssm=_bool(node.try_get_context("pcs:enable_ssm"), True),
            key_pair_name=(
                str(node.try_get_context("pcs:key_pair_name")).strip()
                if node.try_get_context("pcs:key_pair_name")
                else None
            ),
            enable_license_access=_bool(
                node.try_get_context("pcs:enable_license_access"), True
            ),
            license_security_group_id=(
                str(node.try_get_context("pcs:license_security_group_id")).strip()
                if node.try_get_context("pcs:license_security_group_id")
                else None
            ),
            license_manager_port=(
                _int(
                    node.try_get_context("pcs:license_manager_port"),
                    0,
                    "pcs:license_manager_port",
                )
                if node.try_get_context("pcs:license_manager_port")
                else None
            ),
            license_vendor_port=(
                _int(
                    node.try_get_context("pcs:license_vendor_port"),
                    0,
                    "pcs:license_vendor_port",
                )
                if node.try_get_context("pcs:license_vendor_port")
                else None
            ),
            enable_accounting=_bool(
                node.try_get_context("pcs:enable_accounting"), False
            ),
            accounting_purge_days=_int(
                node.try_get_context("pcs:accounting_purge_days"),
                90,
                "pcs:accounting_purge_days",
            ),
            scale_down_idle_seconds=_int(
                node.try_get_context("pcs:scale_down_idle_seconds"),
                900,
                "pcs:scale_down_idle_seconds",
            ),
            enable_openzfs_mounts=_bool(
                node.try_get_context("pcs:enable_openzfs_mounts"), True
            ),
            openzfs_dns=(
                str(node.try_get_context("pcs:openzfs_dns")).strip()
                if node.try_get_context("pcs:openzfs_dns")
                else None
            ),
            openzfs_security_group_id=(
                str(node.try_get_context("pcs:openzfs_security_group_id")).strip()
                if node.try_get_context("pcs:openzfs_security_group_id")
                else None
            ),
            enable_ontap_mounts=_bool(
                node.try_get_context("pcs:enable_ontap_mounts"), False
            ),
            ontap_svm_nfs_dns=(
                str(node.try_get_context("pcs:ontap_svm_nfs_dns")).strip()
                if node.try_get_context("pcs:ontap_svm_nfs_dns")
                else None
            ),
            ontap_security_group_id=(
                str(node.try_get_context("pcs:ontap_security_group_id")).strip()
                if node.try_get_context("pcs:ontap_security_group_id")
                else None
            ),
            enable_local_scratch=_bool(
                node.try_get_context("pcs:enable_local_scratch"), True
            ),
            local_scratch_mount_point=str(
                node.try_get_context("pcs:local_scratch_mount_point")
                or "/local_scratch"
            ),
            enable_cloudwatch_lifecycle_logs=_bool(
                node.try_get_context("pcs:enable_cloudwatch_lifecycle_logs"),
                True,
            ),
            enable_scheduler_log_delivery=_bool(
                node.try_get_context("pcs:enable_scheduler_log_delivery"),
                True,
            ),
            enable_job_completion_log_delivery=_bool(
                node.try_get_context("pcs:enable_job_completion_log_delivery"),
                True,
            ),
            enable_scheduler_audit_log_delivery=_bool(
                node.try_get_context("pcs:enable_scheduler_audit_log_delivery"),
                False,
            ),
            log_retention_days=_int(
                node.try_get_context("pcs:log_retention_days"),
                30,
                "pcs:log_retention_days",
            ),
            create_pcs_vpc_endpoint=_bool(
                node.try_get_context("pcs:create_pcs_vpc_endpoint"), True
            ),
            root_device_name=str(
                node.try_get_context("pcs:root_device_name") or "/dev/xvda"
            ),
            login_root_volume_gib=_int(
                node.try_get_context("pcs:login_root_volume_gib"),
                100,
                "pcs:login_root_volume_gib",
            ),
            compute_root_volume_gib=_int(
                node.try_get_context("pcs:compute_root_volume_gib"),
                200,
                "pcs:compute_root_volume_gib",
            ),
        )
        config.validate()
        return config

    @property
    def login_node_group_name(self) -> str:
        return f"login-{self.cluster_name}"

    @property
    def compute_node_group_name(self) -> str:
        return f"compute-{self.cluster_name}"

    def validate(self) -> None:
        _validate_name(self.cluster_name, "pcs:cluster_name", 40)
        if len(self.cluster_name) > DERIVED_CNG_CLUSTER_NAME_MAX:
            raise ValueError(
                "pcs:cluster_name must be no longer than "
                f"{DERIVED_CNG_CLUSTER_NAME_MAX} characters because "
                "compute-<cluster-name> must fit the AWS PCS 25-character "
                "Compute Node Group name limit"
            )
        _validate_name(self.queue_name, "pcs:queue_name", 25)
        if self.cluster_size not in CLUSTER_SIZES:
            raise ValueError(
                f"pcs:cluster_size must be one of {', '.join(CLUSTER_SIZES)}"
            )
        if self.slurm_version not in SUPPORTED_SLURM_VERSIONS:
            raise ValueError(
                "pcs:slurm_version must be an AWS PCS supported version: "
                + ", ".join(SUPPORTED_SLURM_VERSIONS)
            )
        if not _AMI_PATTERN.fullmatch(self.ami_id):
            raise ValueError(
                "pcs:ami_id must be an AMI ID such as ami-0123456789abcdef0"
            )
        for key, value, prefix in (
            ("pcs:vpc_id", self.vpc_id, "vpc-"),
            ("pcs:subnet_id", self.subnet_id, "subnet-"),
        ):
            if not value.startswith(prefix) or not _RESOURCE_ID_PATTERN.fullmatch(
                value
            ):
                raise ValueError(f"{key} must be a valid {prefix.rstrip('-')} ID")
        for key, value in (
            ("pcs:license_security_group_id", self.license_security_group_id),
            ("pcs:openzfs_security_group_id", self.openzfs_security_group_id),
            ("pcs:ontap_security_group_id", self.ontap_security_group_id),
        ):
            if value and (
                not value.startswith("sg-") or not _RESOURCE_ID_PATTERN.fullmatch(value)
            ):
                raise ValueError(f"{key} must be a valid security group ID")
        if self.ssh_cidr and not (self.key_pair_name or self.shared_stack_prefix):
            raise ValueError(
                "pcs:key_pair_name is required when pcs:ssh_cidr is set and "
                "pcs:shared_stack_prefix is empty"
            )
        direct_license_values = (
            self.license_security_group_id,
            self.license_manager_port,
            self.license_vendor_port,
        )
        if self.enable_license_access and not (
            self.shared_stack_prefix or all(direct_license_values)
        ):
            raise ValueError(
                "Set all direct PCS license values or pcs:shared_stack_prefix "
                "when pcs:enable_license_access is enabled"
            )
        if any(value is not None for value in direct_license_values) and not all(
            direct_license_values
        ):
            raise ValueError(
                "pcs:license_security_group_id, pcs:license_manager_port, and "
                "pcs:license_vendor_port must be set together"
            )
        for key, value in (
            ("pcs:license_manager_port", self.license_manager_port),
            ("pcs:license_vendor_port", self.license_vendor_port),
        ):
            if value is not None and not 1 <= value <= 65535:
                raise ValueError(f"{key} must be between 1 and 65535")
        for key, value in (
            ("pcs:openzfs_dns", self.openzfs_dns),
            ("pcs:ontap_svm_nfs_dns", self.ontap_svm_nfs_dns),
        ):
            if value and (
                "/" in value or ":" in value or not value.endswith(".amazonaws.com")
            ):
                raise ValueError(f"{key} must be an FSx DNS name without a mount path")
        for enabled, values, name in (
            (
                self.enable_openzfs_mounts,
                (self.openzfs_dns, self.openzfs_security_group_id),
                "OpenZFS",
            ),
            (
                self.enable_ontap_mounts,
                (self.ontap_svm_nfs_dns, self.ontap_security_group_id),
                "ONTAP",
            ),
        ):
            if enabled and not (self.shared_stack_prefix or all(values)):
                raise ValueError(
                    f"Set both direct {name} DNS/security group values or "
                    "pcs:shared_stack_prefix"
                )
        if not self.compute_instance_types:
            raise ValueError(
                "pcs:compute_instance_types must contain at least one type"
            )
        if self.compute_min_count < 0:
            raise ValueError("pcs:compute_min_count must be at least 0")
        if self.compute_max_count < self.compute_min_count:
            raise ValueError(
                "pcs:compute_max_count must be greater than or equal to the minimum"
            )
        cluster_limit = {"SMALL": 32, "MEDIUM": 512, "LARGE": 2048}[self.cluster_size]
        if self.compute_max_count + int(self.enable_login_node) > cluster_limit:
            raise ValueError(
                f"configured node maximum exceeds PCS {self.cluster_size} limit "
                f"of {cluster_limit}"
            )
        if self.purchase_option not in PURCHASE_OPTIONS:
            raise ValueError(
                f"pcs:purchase_option must be one of {', '.join(PURCHASE_OPTIONS)}"
            )
        if (
            self.accounting_purge_days == 0
            or not -1 <= self.accounting_purge_days <= 10000
        ):
            raise ValueError(
                "pcs:accounting_purge_days must be -1 or between 1 and 10000"
            )
        if not 1 <= self.scale_down_idle_seconds <= 10_000_000:
            raise ValueError(
                "pcs:scale_down_idle_seconds must be between 1 and 10000000"
            )
        if self.log_retention_days not in LOG_RETENTION_DAYS:
            values = ", ".join(str(value) for value in LOG_RETENTION_DAYS)
            raise ValueError(
                "pcs:log_retention_days must be a supported CloudWatch Logs "
                f"retention period: {values}"
            )
        if self.login_root_volume_gib < 30 or self.compute_root_volume_gib < 30:
            raise ValueError("PCS root volumes must be at least 30 GiB")
        if not self.root_device_name.startswith("/dev/"):
            raise ValueError("pcs:root_device_name must be a Linux device path")
        if (
            not self.local_scratch_mount_point.startswith("/")
            or self.local_scratch_mount_point == "/"
        ):
            raise ValueError(
                "pcs:local_scratch_mount_point must be an absolute path below /"
            )
