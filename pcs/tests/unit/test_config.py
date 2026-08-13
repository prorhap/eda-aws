from dataclasses import replace

import aws_cdk as cdk
import pytest

from pcs.config import PcsConfig


def context(**overrides):
    values = {
        "pcs:vpc_id": "vpc-0123456789abcdef0",
        "pcs:subnet_id": "subnet-0123456789abcdef0",
        "pcs:ami_id": "ami-0123456789abcdef0",
        "pcs:shared_stack_prefix": "Eda",
    }
    values.update(overrides)
    return values


def test_defaults_model_the_eda_pcs_profile():
    config = PcsConfig.from_app(cdk.App(context=context()))

    assert config.slurm_version == "25.11"
    assert config.cluster_size == "SMALL"
    assert config.cluster_name == "eda-pcs-cluster"
    assert config.subnet_id == "subnet-0123456789abcdef0"
    assert config.login_node_group_name == "login-eda-pcs-cluster"
    assert config.compute_node_group_name == "compute-eda-pcs-cluster"
    assert config.login_instance_type == "m7i.2xlarge"
    assert config.compute_instance_types == ("x8aedz.24xlarge",)
    assert config.compute_min_count == 0
    assert config.compute_max_count == 9
    assert config.queue_name == "default-eda-queue"
    assert config.enable_accounting is False
    assert config.enable_openzfs_mounts is True
    assert config.openzfs_dns is None
    assert config.enable_ontap_mounts is False
    assert config.ontap_svm_nfs_dns is None
    assert config.enable_local_scratch is True
    assert config.local_scratch_mount_point == "/local_scratch"
    assert config.enable_scheduler_log_delivery is True
    assert config.enable_job_completion_log_delivery is True
    assert config.enable_scheduler_audit_log_delivery is False
    assert config.log_retention_days == 30
    assert config.create_pcs_vpc_endpoint is True


def test_standalone_mode_accepts_direct_storage_license_and_key_pair_values():
    config = PcsConfig.from_app(
        cdk.App(
            context=context(
                **{
                    "pcs:shared_stack_prefix": "",
                    "pcs:key_pair_name": "eda-pcs-key",
                    "pcs:license_security_group_id": "sg-0123456789abcdef0",
                    "pcs:license_manager_port": 27000,
                    "pcs:license_vendor_port": 27020,
                    "pcs:openzfs_dns": (
                        "fs-0123456789abcdef0.fsx.ap-northeast-2.amazonaws.com"
                    ),
                    "pcs:openzfs_security_group_id": "sg-1123456789abcdef0",
                }
            )
        )
    )

    assert config.shared_stack_prefix is None
    assert config.key_pair_name == "eda-pcs-key"
    assert config.license_manager_port == 27000


def test_standalone_mode_requires_direct_values_for_enabled_dependencies():
    with pytest.raises(ValueError, match="key_pair_name"):
        PcsConfig.from_app(
            cdk.App(
                context=context(
                    **{
                        "pcs:shared_stack_prefix": "",
                        "pcs:ssh_cidr": "10.0.0.0/8",
                        "pcs:enable_license_access": "0",
                        "pcs:enable_openzfs_mounts": "0",
                    }
                )
            )
        )


def test_new_cluster_rejects_unsupported_slurm_version():
    with pytest.raises(ValueError, match="supported version"):
        PcsConfig.from_app(cdk.App(context=context(**{"pcs:slurm_version": "24.11"})))


def test_cluster_name_must_fit_derived_compute_node_group_name():
    with pytest.raises(ValueError, match="25-character Compute Node Group"):
        PcsConfig.from_app(
            cdk.App(context=context(**{"pcs:cluster_name": "cluster-name-is-too-long"}))
        )


def test_small_cluster_capacity_includes_login_node():
    config = PcsConfig.from_app(cdk.App(context=context()))

    with pytest.raises(ValueError, match="exceeds PCS SMALL"):
        replace(config, compute_max_count=32).validate()


def test_subnet_id_is_required():
    with pytest.raises(ValueError, match="pcs:subnet_id"):
        PcsConfig.from_app(cdk.App(context=context(**{"pcs:subnet_id": ""})))


def test_explicit_storage_endpoints_and_security_groups_are_accepted():
    config = PcsConfig.from_app(
        cdk.App(
            context=context(
                **{
                    "pcs:openzfs_dns": (
                        "fs-0123456789abcdef0.fsx.ap-northeast-2.amazonaws.com"
                    ),
                    "pcs:openzfs_security_group_id": ("sg-0123456789abcdef0"),
                    "pcs:enable_ontap_mounts": "1",
                    "pcs:ontap_svm_nfs_dns": (
                        "svm-0123456789abcdef0."
                        "fs-0123456789abcdef0.fsx."
                        "ap-northeast-2.amazonaws.com"
                    ),
                    "pcs:ontap_security_group_id": ("sg-1123456789abcdef0"),
                }
            )
        )
    )

    assert config.openzfs_security_group_id == "sg-0123456789abcdef0"
    assert config.enable_ontap_mounts is True
    assert config.ontap_security_group_id == "sg-1123456789abcdef0"


def test_local_scratch_mount_point_must_be_below_root():
    with pytest.raises(ValueError, match="absolute path"):
        PcsConfig.from_app(
            cdk.App(
                context=context(**{"pcs:local_scratch_mount_point": "local_scratch"})
            )
        )


def test_log_retention_must_be_a_cloudwatch_supported_period():
    with pytest.raises(ValueError, match="supported CloudWatch Logs retention"):
        PcsConfig.from_app(
            cdk.App(context=context(**{"pcs:log_retention_days": 31}))
        )
