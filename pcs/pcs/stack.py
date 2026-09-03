"""AWS PCS cluster resources, intentionally separate from ParallelCluster."""

from __future__ import annotations

import hashlib
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Aws,
    CfnOutput,
    Stack,
    Tags,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
    aws_pcs as pcs,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

from pcs.config import PcsConfig


class PcsStack(Stack):
    """Deploy an AWS-managed Slurm control plane and customer-owned node fleets."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: PcsConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config

        Tags.of(self).add("Project", "eda-cluster")
        Tags.of(self).add("DeploymentModel", "AWS-PCS")
        Tags.of(self).add("ClusterName", config.cluster_name)

        cluster_sg = self._create_cluster_security_group()
        ssh_sg = self._create_ssh_security_group()
        if config.create_pcs_vpc_endpoint:
            self._create_pcs_vpc_endpoint(cluster_sg)

        node_role, instance_profile = self._create_node_identity()
        mount_asset = self._create_mount_asset(node_role)
        local_scratch_asset = self._create_local_scratch_asset(node_role)
        login_lifecycle_actions = self._node_lifecycle_actions(mount_asset)
        compute_lifecycle_actions = self._node_lifecycle_actions(
            mount_asset,
            local_scratch_asset=local_scratch_asset,
        )

        login_template = self._create_launch_template(
            "LoginLaunchTemplate",
            "login",
            cluster_sg.ref,
            ssh_sg.ref if ssh_sg else None,
            config.login_root_volume_gib,
        )
        compute_template = self._create_launch_template(
            "ComputeLaunchTemplate",
            "compute",
            cluster_sg.ref,
            None,
            config.compute_root_volume_gib,
        )

        cluster = pcs.CfnCluster(
            self,
            "Cluster",
            name=config.cluster_name,
            size=config.cluster_size,
            scheduler=pcs.CfnCluster.SchedulerProperty(
                type="SLURM",
                version=config.slurm_version,
            ),
            networking=pcs.CfnCluster.NetworkingProperty(
                network_type="IPV4",
                subnet_ids=[config.subnet_id],
                security_group_ids=[cluster_sg.ref],
            ),
            slurm_configuration=self._cluster_slurm_configuration(),
            tags=self._resource_tags(name=config.cluster_name),
        )

        login_group = None
        if config.enable_login_node:
            login_group = pcs.CfnComputeNodeGroup(
                self,
                "LoginComputeNodeGroup",
                cluster_id=cluster.attr_id,
                name=config.login_node_group_name,
                ami_id=config.ami_id,
                subnet_ids=[config.subnet_id],
                purchase_option="ONDEMAND",
                iam_instance_profile_arn=instance_profile.attr_arn,
                custom_launch_template=(
                    pcs.CfnComputeNodeGroup.CustomLaunchTemplateProperty(
                        template_id=login_template.ref,
                        version=login_template.attr_latest_version_number,
                    )
                ),
                scaling_configuration=(
                    pcs.CfnComputeNodeGroup.ScalingConfigurationProperty(
                        min_instance_count=1,
                        max_instance_count=1,
                    )
                ),
                instance_configs=[
                    pcs.CfnComputeNodeGroup.InstanceConfigProperty(
                        instance_type=config.login_instance_type
                    )
                ],
                node_lifecycle_actions=login_lifecycle_actions,
                tags=self._resource_tags(
                    name=config.login_node_group_name,
                ),
            )

        compute_group_props: dict[str, object] = {}
        if config.purchase_option == "SPOT":
            compute_group_props["spot_options"] = (
                pcs.CfnComputeNodeGroup.SpotOptionsProperty(
                    allocation_strategy=config.spot_allocation_strategy
                )
            )

        compute_group = pcs.CfnComputeNodeGroup(
            self,
            "ComputeNodeGroup",
            cluster_id=cluster.attr_id,
            name=config.compute_node_group_name,
            ami_id=config.ami_id,
            subnet_ids=[config.subnet_id],
            purchase_option=config.purchase_option,
            iam_instance_profile_arn=instance_profile.attr_arn,
            custom_launch_template=(
                pcs.CfnComputeNodeGroup.CustomLaunchTemplateProperty(
                    template_id=compute_template.ref,
                    version=compute_template.attr_latest_version_number,
                )
            ),
            scaling_configuration=(
                pcs.CfnComputeNodeGroup.ScalingConfigurationProperty(
                    min_instance_count=config.compute_min_count,
                    max_instance_count=config.compute_max_count,
                )
            ),
            instance_configs=[
                pcs.CfnComputeNodeGroup.InstanceConfigProperty(
                    instance_type=instance_type
                )
                for instance_type in config.compute_instance_types
            ],
            node_lifecycle_actions=compute_lifecycle_actions,
            tags=self._resource_tags(
                name=config.compute_node_group_name,
            ),
            **compute_group_props,
        )

        queue = pcs.CfnQueue(
            self,
            "Queue",
            cluster_id=cluster.attr_id,
            name=config.queue_name,
            compute_node_group_configurations=[
                pcs.CfnQueue.ComputeNodeGroupConfigurationProperty(
                    compute_node_group_id=compute_group.attr_id
                )
            ],
            tags=self._resource_tags(name=config.queue_name),
        )

        self._create_scheduler_log_deliveries(cluster)
        self._allow_storage_access(cluster_sg.ref)
        self._allow_license_access(cluster_sg.ref)
        self._create_outputs(cluster, compute_group, queue, login_group)

    def _create_cluster_security_group(self) -> ec2.CfnSecurityGroup:
        group = ec2.CfnSecurityGroup(
            self,
            "ClusterSecurityGroup",
            group_description=(
                "AWS PCS controller, login, and compute node communication"
            ),
            vpc_id=self.config.vpc_id,
            security_group_egress=[
                ec2.CfnSecurityGroup.EgressProperty(
                    ip_protocol="-1",
                    cidr_ip="0.0.0.0/0",
                    description="Required outbound access for PCS nodes",
                )
            ],
            tags=self._cfn_resource_tags(name=f"{self.config.cluster_name}-cluster-sg"),
        )
        ec2.CfnSecurityGroupIngress(
            self,
            "ClusterSecurityGroupSelfIngress",
            group_id=group.ref,
            ip_protocol="-1",
            source_security_group_id=group.ref,
            description="All traffic within the PCS cluster security group",
        )
        return group

    def _create_ssh_security_group(self) -> ec2.CfnSecurityGroup | None:
        if not self.config.ssh_cidr:
            return None
        return ec2.CfnSecurityGroup(
            self,
            "LoginSshSecurityGroup",
            group_description="Restricted SSH access to AWS PCS login nodes",
            vpc_id=self.config.vpc_id,
            security_group_ingress=[
                ec2.CfnSecurityGroup.IngressProperty(
                    ip_protocol="tcp",
                    from_port=22,
                    to_port=22,
                    cidr_ip=self.config.ssh_cidr,
                    description="SSH from the approved client CIDR",
                )
            ],
            security_group_egress=[
                ec2.CfnSecurityGroup.EgressProperty(
                    ip_protocol="-1",
                    cidr_ip="0.0.0.0/0",
                )
            ],
            tags=self._cfn_resource_tags(name=f"{self.config.cluster_name}-login-ssh"),
        )

    def _create_pcs_vpc_endpoint(self, cluster_sg: ec2.CfnSecurityGroup) -> None:
        endpoint_sg = ec2.CfnSecurityGroup(
            self,
            "PcsEndpointSecurityGroup",
            group_description="HTTPS from AWS PCS nodes to the PCS API endpoint",
            vpc_id=self.config.vpc_id,
            security_group_ingress=[
                ec2.CfnSecurityGroup.IngressProperty(
                    ip_protocol="tcp",
                    from_port=443,
                    to_port=443,
                    source_security_group_id=cluster_sg.ref,
                    description="PCS API HTTPS from cluster nodes",
                )
            ],
            security_group_egress=[
                ec2.CfnSecurityGroup.EgressProperty(
                    ip_protocol="-1",
                    cidr_ip="0.0.0.0/0",
                )
            ],
        )
        ec2.CfnVPCEndpoint(
            self,
            "PcsApiEndpoint",
            service_name=f"com.amazonaws.{self.region}.pcs",
            vpc_id=self.config.vpc_id,
            vpc_endpoint_type="Interface",
            subnet_ids=[self.config.subnet_id],
            security_group_ids=[endpoint_sg.ref],
            private_dns_enabled=True,
        )

    def _create_node_identity(
        self,
    ) -> tuple[iam.Role, iam.CfnInstanceProfile]:
        role_name = self._iam_name(f"AWSPCS-{self.config.cluster_name}-nodes")
        managed_policies = [
            iam.ManagedPolicy.from_aws_managed_policy_name("AWSPCSComputeNodePolicy")
        ]
        if self.config.enable_ssm:
            managed_policies.append(
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                )
            )
        if self.config.enable_cloudwatch_lifecycle_logs:
            managed_policies.append(
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                )
            )

        role = iam.Role(
            self,
            "NodeRole",
            role_name=role_name,
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=managed_policies,
            description="AWS PCS compute node registration and operations",
        )
        if self.config.enable_cloudwatch_lifecycle_logs:
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["s3:GetObject"],
                    resources=[
                        (
                            f"arn:{Aws.PARTITION}:s3:::aws-pcs-repo-"
                            f"{self.region}/aws-pcs-node-lifecycle-scripts/*"
                        )
                    ],
                )
            )

        profile = iam.CfnInstanceProfile(
            self,
            "NodeInstanceProfile",
            instance_profile_name=self._iam_name(
                f"AWSPCS-{self.config.cluster_name}-nodes"
            ),
            roles=[role.role_name],
        )
        return role, profile

    def _create_mount_asset(self, role: iam.Role) -> s3_assets.Asset | None:
        if not (self.config.enable_openzfs_mounts or self.config.enable_ontap_mounts):
            return None
        asset_path = Path(__file__).resolve().parents[1] / "assets" / "mount-nfs.sh"
        asset = s3_assets.Asset(
            self,
            "NfsMountScript",
            path=str(asset_path),
        )
        asset.grant_read(role)
        return asset

    def _create_local_scratch_asset(self, role: iam.Role) -> s3_assets.Asset | None:
        if not self.config.enable_local_scratch:
            return None
        asset_path = (
            Path(__file__).resolve().parents[1] / "assets" / "prepare-local-scratch.sh"
        )
        asset = s3_assets.Asset(
            self,
            "LocalScratchScript",
            path=str(asset_path),
        )
        asset.grant_read(role)
        return asset

    def _cluster_slurm_configuration(
        self,
    ) -> pcs.CfnCluster.SlurmConfigurationProperty:
        accounting = None
        if self.config.enable_accounting:
            accounting = pcs.CfnCluster.AccountingProperty(
                mode="STANDARD",
                default_purge_time_in_days=self.config.accounting_purge_days,
            )
        return pcs.CfnCluster.SlurmConfigurationProperty(
            scale_down_idle_time_in_seconds=self.config.scale_down_idle_seconds,
            accounting=accounting,
        )

    def _create_scheduler_log_deliveries(self, cluster: pcs.CfnCluster) -> None:
        deliveries = (
            (
                "Scheduler",
                "scheduler",
                "scheduler",
                "PCS_SCHEDULER_LOGS",
                self.config.enable_scheduler_log_delivery,
            ),
            (
                "JobCompletion",
                "job-completion",
                "jobs",
                "PCS_JOBCOMP_LOGS",
                self.config.enable_job_completion_log_delivery,
            ),
            (
                "SchedulerAudit",
                "scheduler-audit",
                "audit",
                "PCS_SCHEDULER_AUDIT_LOGS",
                self.config.enable_scheduler_audit_log_delivery,
            ),
        )
        for (
            construct_suffix,
            log_suffix,
            resource_suffix,
            log_type,
            enabled,
        ) in deliveries:
            if not enabled:
                continue
            self._create_log_delivery(
                construct_suffix=construct_suffix,
                log_suffix=log_suffix,
                resource_suffix=resource_suffix,
                log_type=log_type,
                cluster=cluster,
            )

    def _create_log_delivery(
        self,
        *,
        construct_suffix: str,
        log_suffix: str,
        resource_suffix: str,
        log_type: str,
        cluster: pcs.CfnCluster,
    ) -> None:
        # Delivery source and destination names are account/Region scoped. Include
        # the generated PCS cluster ID so a replacement cluster does not collide
        # with retained or orphaned log delivery resources from an older cluster.
        resource_name = (
            f"{self.config.cluster_name}-{resource_suffix}-{cluster.attr_id}"
        )
        log_group = logs.CfnLogGroup(
            self,
            f"{construct_suffix}LogGroup",
            log_group_name=(
                f"/aws/pcs/{self.config.cluster_name}/{cluster.attr_id}/{log_suffix}"
            ),
            retention_in_days=self.config.log_retention_days,
            tags=self._cfn_resource_tags(name=resource_name),
        )
        log_group.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
        destination = logs.CfnDeliveryDestination(
            self,
            f"{construct_suffix}LogDestination",
            name=f"{resource_name}-destination",
            delivery_destination_type="CWL",
            destination_resource_arn=log_group.attr_arn,
            output_format="json",
            tags=self._cfn_resource_tags(name=resource_name),
        )
        source = logs.CfnDeliverySource(
            self,
            f"{construct_suffix}LogSource",
            name=f"{resource_name}-source",
            resource_arn=cluster.attr_arn,
            log_type=log_type,
            tags=self._cfn_resource_tags(name=resource_name),
        )
        logs.CfnDelivery(
            self,
            f"{construct_suffix}LogDelivery",
            delivery_source_name=source.name,
            delivery_destination_arn=destination.attr_arn,
            tags=self._cfn_resource_tags(name=resource_name),
        )

    def _node_lifecycle_actions(
        self,
        mount_asset: s3_assets.Asset | None,
        *,
        local_scratch_asset: s3_assets.Asset | None = None,
    ) -> pcs.CfnComputeNodeGroup.NodeLifecycleActionsProperty | None:
        scripts: list[pcs.CfnComputeNodeGroup.NodeLifecycleScriptProperty] = []

        if self.config.enable_cloudwatch_lifecycle_logs:
            scripts.append(
                pcs.CfnComputeNodeGroup.NodeLifecycleScriptProperty(
                    name="configure-cloudwatch-lifecycle-logs",
                    script_source=(
                        pcs.CfnComputeNodeGroup.ScriptSourceProperty(
                            script_location=(
                                f"s3://aws-pcs-repo-{self.region}/"
                                "aws-pcs-node-lifecycle-scripts/"
                                "configure-cloudwatch-logs-v1.0.0.sh"
                            ),
                            checksum=(
                                "78c2c7b0bb6bbc2e164342a14bd83e44050c1259b8fdc"
                                "4ae05133614f2c6c516"
                            ),
                        )
                    ),
                    execution_policy="FIRST_BOOT_ONLY",
                    on_error="CONTINUE",
                )
            )

        if mount_asset:
            script_path = (
                Path(__file__).resolve().parents[1] / "assets" / "mount-nfs.sh"
            )
            checksum = hashlib.sha256(script_path.read_bytes()).hexdigest()
            script_location = (
                f"s3://{mount_asset.s3_bucket_name}/{mount_asset.s3_object_key}"
            )
            if self.config.enable_openzfs_mounts:
                openzfs_dns = self._storage_value(
                    self.config.openzfs_dns,
                    "storage/OpenZfsDns",
                )
                for name, volume_name, mount_point in (
                    ("tools", "fsxz_tools", "/fsxz/tools"),
                    ("work", "fsxz_work", "/fsxz/work"),
                    ("scratch", "fsxz_scratch", "/fsxz/scratch"),
                ):
                    scripts.append(
                        self._mount_script(
                            name=f"mount-openzfs-{name}",
                            script_location=script_location,
                            checksum=checksum,
                            source=f"{openzfs_dns}:/fsx/{volume_name}",
                            mount_point=mount_point,
                            options=(
                                "nfsvers=3,rsize=1048576,wsize=1048576,"
                                "hard,timeo=600,retrans=2"
                            ),
                        )
                    )

            if self.config.enable_ontap_mounts:
                ontap_dns = self._storage_value(
                    self.config.ontap_svm_nfs_dns,
                    "storage/OntapSvmNfsDns",
                )
                for name, junction_path, mount_point in (
                    ("tools", "/fsxn_tools", "/fsxn/tools"),
                    ("work", "/fsxn_work", "/fsxn/work"),
                    ("scratch", "/fsxn_scratch", "/fsxn/scratch"),
                ):
                    scripts.append(
                        self._mount_script(
                            name=f"mount-ontap-{name}",
                            script_location=script_location,
                            checksum=checksum,
                            source=f"{ontap_dns}:{junction_path}",
                            mount_point=mount_point,
                            options=(
                                "nfsvers=4.1,rsize=1048576,wsize=1048576,"
                                "hard,timeo=600,retrans=2"
                            ),
                        )
                    )

        if local_scratch_asset:
            script_path = (
                Path(__file__).resolve().parents[1]
                / "assets"
                / "prepare-local-scratch.sh"
            )
            scripts.append(
                pcs.CfnComputeNodeGroup.NodeLifecycleScriptProperty(
                    name="prepare-local-scratch",
                    script_source=pcs.CfnComputeNodeGroup.ScriptSourceProperty(
                        script_location=(
                            f"s3://{local_scratch_asset.s3_bucket_name}/"
                            f"{local_scratch_asset.s3_object_key}"
                        ),
                        checksum=hashlib.sha256(script_path.read_bytes()).hexdigest(),
                    ),
                    arguments=[
                        "--mount-point",
                        self.config.local_scratch_mount_point,
                    ],
                    execution_policy="EVERY_BOOT",
                    on_error="TERMINATE",
                )
            )

        if not scripts:
            return None
        return pcs.CfnComputeNodeGroup.NodeLifecycleActionsProperty(
            script_caching_policy="REFRESH_ON_REBOOT",
            stages=pcs.CfnComputeNodeGroup.NodeLifecycleStagesProperty(
                node_bootstrapped=scripts
            ),
        )

    @staticmethod
    def _mount_script(
        *,
        name: str,
        script_location: str,
        checksum: str,
        source: str,
        mount_point: str,
        options: str,
    ) -> pcs.CfnComputeNodeGroup.NodeLifecycleScriptProperty:
        return pcs.CfnComputeNodeGroup.NodeLifecycleScriptProperty(
            name=name,
            script_source=pcs.CfnComputeNodeGroup.ScriptSourceProperty(
                script_location=script_location,
                checksum=checksum,
            ),
            arguments=[
                "--source",
                source,
                "--mount-point",
                mount_point,
                "--options",
                options,
            ],
            execution_policy="EVERY_BOOT",
            on_error="TERMINATE",
        )

    def _shared_parameter(self, parameter_suffix: str) -> str:
        if not self.config.shared_stack_prefix:
            raise ValueError(
                "A shared Foundation prefix is required for this unresolved value"
            )
        component, output_name = {
            "network/KeyPairName": ("base", "KeyPairName"),
            "network/SgFsxId": ("base", "SgFsxId"),
            "network/SgOntapId": ("base", "SgOntapId"),
            "storage/OpenZfsDns": ("storage", "OpenZfsDns"),
            "storage/OntapSvmNfsDns": ("storage", "OntapSvmNfsDns"),
            "license/SgId": ("license", "LicenseSgId"),
            "license/ManagerPort": ("license", "LicenseManagerPort"),
            "license/VendorPort": ("license", "LicenseVendorPort"),
        }[parameter_suffix]
        return cdk.Fn.import_value(
            f"{self.config.shared_stack_prefix.lower()}:{component}:{output_name}"
        )

    def _storage_value(
        self,
        explicit_value: str | None,
        parameter_suffix: str,
    ) -> str:
        if explicit_value:
            return explicit_value
        return self._shared_parameter(parameter_suffix)

    def _create_launch_template(
        self,
        construct_id: str,
        role: str,
        cluster_sg_id: str,
        ssh_sg_id: str | None,
        root_volume_gib: int,
    ) -> ec2.CfnLaunchTemplate:
        security_groups = [cluster_sg_id]
        if ssh_sg_id:
            security_groups.append(ssh_sg_id)
        launch_template_data: dict[str, object] = {
            "security_group_ids": security_groups,
            "metadata_options": ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                http_endpoint="enabled",
                http_tokens="required",
                http_put_response_hop_limit=4,
                instance_metadata_tags="disabled",
            ),
            "block_device_mappings": [
                ec2.CfnLaunchTemplate.BlockDeviceMappingProperty(
                    device_name=self.config.root_device_name,
                    ebs=ec2.CfnLaunchTemplate.EbsProperty(
                        encrypted=True,
                        delete_on_termination=True,
                        volume_type="gp3",
                        volume_size=root_volume_gib,
                    ),
                )
            ],
            "tag_specifications": [
                ec2.CfnLaunchTemplate.TagSpecificationProperty(
                    resource_type="instance",
                    tags=self._cfn_resource_tags(name=self._node_group_name(role)),
                ),
                ec2.CfnLaunchTemplate.TagSpecificationProperty(
                    resource_type="volume",
                    tags=self._cfn_resource_tags(
                        name=f"{self._node_group_name(role)}-root",
                    ),
                ),
            ],
        }
        key_name = self.config.key_pair_name
        if not key_name and self.config.shared_stack_prefix:
            key_name = self._shared_parameter("network/KeyPairName")
        if key_name:
            launch_template_data["key_name"] = key_name

        return ec2.CfnLaunchTemplate(
            self,
            construct_id,
            launch_template_name=self._node_group_name(role),
            launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                **launch_template_data
            ),
        )

    def _node_group_name(self, role: str) -> str:
        if role == "login":
            return self.config.login_node_group_name
        if role == "compute":
            return self.config.compute_node_group_name
        raise ValueError(f"Unsupported PCS node role: {role}")

    def _resource_tags(
        self,
        *,
        name: str,
    ) -> dict[str, str]:
        return {
            "Name": name,
            "Project": "eda-cluster",
            "DeploymentModel": "AWS-PCS",
            "ClusterName": self.config.cluster_name,
        }

    def _cfn_resource_tags(
        self,
        *,
        name: str,
    ) -> list[cdk.CfnTag]:
        return [
            cdk.CfnTag(key=key, value=value)
            for key, value in self._resource_tags(
                name=name,
            ).items()
        ]

    def _allow_storage_access(self, cluster_sg_id: str) -> None:
        if self.config.enable_openzfs_mounts:
            openzfs_sg_id = self._storage_value(
                self.config.openzfs_security_group_id,
                "network/SgFsxId",
            )
            self._allow_nfs_ports(
                construct_prefix="OpenZfs",
                storage_sg_id=openzfs_sg_id,
                cluster_sg_id=cluster_sg_id,
                rules=(
                    ("tcp", 111, 111, "NFS TCP 111 from PCS nodes"),
                    ("udp", 111, 111, "NFS UDP 111 from PCS nodes"),
                    ("tcp", 2049, 2049, "NFS TCP 2049 from PCS nodes"),
                    ("udp", 2049, 2049, "NFS UDP 2049 from PCS nodes"),
                    ("tcp", 20001, 20003, "NFS mount TCP from PCS nodes"),
                    ("udp", 20001, 20003, "NFS mount UDP from PCS nodes"),
                ),
            )

        if self.config.enable_ontap_mounts:
            ontap_sg_id = self._storage_value(
                self.config.ontap_security_group_id,
                "network/SgOntapId",
            )
            self._allow_nfs_ports(
                construct_prefix="Ontap",
                storage_sg_id=ontap_sg_id,
                cluster_sg_id=cluster_sg_id,
                rules=tuple(
                    (
                        protocol,
                        port,
                        port,
                        f"ONTAP NFS {protocol.upper()} {port} from PCS nodes",
                    )
                    for protocol in ("tcp", "udp")
                    for port in (111, 635, 2049, 4045, 4046)
                ),
            )

    def _allow_nfs_ports(
        self,
        *,
        construct_prefix: str,
        storage_sg_id: str,
        cluster_sg_id: str,
        rules: tuple[tuple[str, int, int, str], ...],
    ) -> None:
        for index, (protocol, from_port, to_port, description) in enumerate(rules):
            ec2.CfnSecurityGroupIngress(
                self,
                f"{construct_prefix}Ingress{index}",
                group_id=storage_sg_id,
                ip_protocol=protocol,
                from_port=from_port,
                to_port=to_port,
                source_security_group_id=cluster_sg_id,
                description=description,
            )

    def _allow_license_access(self, cluster_sg_id: str) -> None:
        if not self.config.enable_license_access:
            return
        license_sg_id = self.config.license_security_group_id
        manager_port = self.config.license_manager_port
        vendor_port = self.config.license_vendor_port
        if not license_sg_id:
            license_sg_id = self._shared_parameter("license/SgId")
            manager_port = cdk.Token.as_number(
                self._shared_parameter("license/ManagerPort")
            )
            vendor_port = cdk.Token.as_number(
                self._shared_parameter("license/VendorPort")
            )
        assert manager_port is not None
        assert vendor_port is not None
        for name, port, description in (
            ("Manager", manager_port, "License manager access from PCS nodes"),
            ("Vendor", vendor_port, "License vendor access from PCS nodes"),
        ):
            ec2.CfnSecurityGroupIngress(
                self,
                f"License{name}Ingress",
                group_id=license_sg_id,
                ip_protocol="tcp",
                from_port=port,
                to_port=port,
                source_security_group_id=cluster_sg_id,
                description=description,
            )

    def _create_outputs(
        self,
        cluster: pcs.CfnCluster,
        compute_group: pcs.CfnComputeNodeGroup,
        queue: pcs.CfnQueue,
        login_group: pcs.CfnComputeNodeGroup | None,
    ) -> None:
        CfnOutput(self, "ClusterId", value=cluster.attr_id)
        CfnOutput(self, "ClusterArn", value=cluster.attr_arn)
        CfnOutput(self, "ComputeNodeGroupId", value=compute_group.attr_id)
        CfnOutput(self, "QueueId", value=queue.attr_id)
        if login_group:
            CfnOutput(self, "LoginNodeGroupId", value=login_group.attr_id)
        CfnOutput(
            self,
            "PcsConsoleUrl",
            value=(
                f"https://{self.region}.console.aws.amazon.com/pcs/home"
                f"?region={self.region}#/clusters/{cluster.attr_id}"
            ),
        )

    @staticmethod
    def _iam_name(value: str) -> str:
        return value[:64]
