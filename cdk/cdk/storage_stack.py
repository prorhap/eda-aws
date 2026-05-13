"""EDA Storage Stack — v2.0 (OpenZFS + ONTAP optional)

Deploys one or both FSx file systems based on context flags:

  eda:enable_openzfs       bool (default: True)
  eda:enable_ontap         bool (default: False)
  eda:openzfs_size_gib     int  (default: 10240, 10 TiB)
  eda:openzfs_throughput   int  (default: 2560 MBps)
  eda:ontap_size_gib       int  (default: 10240, 10 TiB)
  eda:ontap_tput_per_ha    int  (default: 3072 MBps, valid: 1536|3072|6144)
  eda:ontap_ha_pairs       int  (default: 1, 1-12)

════════════════════════════════════════════════════════════════════════════════
FSx for OpenZFS — SINGLE_AZ_HA_2 (highest-performance Single-AZ, NVMe L2ARC cache)
────────────────────────────────────────────────────────────────────────────────
  StorageCapacity         64 GiB  ~  524,288 GiB (512 TiB)    (SSD provisioned)
  ThroughputCapacity      Valid values (MBps):
                            160, 320, 640, 1280, 2560, 3840, 5120, 7680, 10240
  NVMe L2ARC cache        40 GB ~ 2,560 GB (auto; 2.5 GB per 1 MBps throughput)
  Peak network throughput up to 21 GBps burst (10,240 MBps tier)
  Ref:  https://docs.aws.amazon.com/fsx/latest/APIReference/API_CreateFileSystemOpenZFSConfiguration.html
        https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/performance-ssd.html

════════════════════════════════════════════════════════════════════════════════
FSx for NetApp ONTAP — SINGLE_AZ_2 (2nd-gen, multi-HA-pair, highest performance)
────────────────────────────────────────────────────────────────────────────────
  HAPairs                 1 ~ 12  (each HA pair ≈ 6 GBps / 200,000 SSD IOPS)
  StorageCapacity         1,024 GiB (1 TiB)  ~  1,048,576 GiB (1 PiB)
                          (evenly distributed across HA pairs' aggregates)
  ThroughputCapacityPerHAPair   1536, 3072, or 6144 MBps
  SSD IOPS                3 IOPS / GiB automatic; max 2,400,000 at 12 HA pairs
  Ref:  https://docs.aws.amazon.com/fsx/latest/APIReference/API_CreateFileSystemOntapConfiguration.html
        https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/HA-pairs.html
"""

from aws_cdk import (
    Stack,
    Tags,
    RemovalPolicy,
    Duration,
    aws_fsx as fsx,
    aws_kms as kms,
    aws_ec2 as ec2,
    aws_cloudwatch as cloudwatch,
    aws_ssm as ssm,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_secretsmanager as secretsmanager,
    CfnOutput,
)
from constructs import Construct


# ─── OpenZFS SINGLE_AZ_HA_2 validation tables ──────────────────────
OPENZFS_THROUGHPUT_VALUES = (160, 320, 640, 1280, 2560, 3840, 5120, 7680, 10240)
OPENZFS_MIN_GIB = 64
OPENZFS_MAX_GIB = 524_288  # 512 TiB

# ─── ONTAP SINGLE_AZ_2 validation tables ───────────────────────────
ONTAP_TPUT_PER_HA_VALUES = (1536, 3072, 6144)
ONTAP_MIN_GIB = 1_024  # 1 TiB
ONTAP_MAX_GIB_PER_HA = 524_288  # 512 TiB per HA pair
ONTAP_MAX_HA_PAIRS = 12


class StorageStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        sg_fsx: ec2.ISecurityGroup,
        sg_ontap: ec2.ISecurityGroup,
        primary_subnet: ec2.ISubnet,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", "eda-cluster")

        subnet_id = primary_subnet.subnet_id
        vpc_cidr = vpc.vpc_cidr_block

        # ── Context / options ────────────────────────────────
        def _ctx_bool(key: str, default: bool) -> bool:
            val = self.node.try_get_context(key)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            return str(val).strip().lower() in ("1", "true", "yes", "on")

        def _ctx_int(key: str, default: int) -> int:
            val = self.node.try_get_context(key)
            return int(val) if val is not None else default

        enable_openzfs = _ctx_bool("eda:enable_openzfs", True)
        enable_ontap = _ctx_bool("eda:enable_ontap", False)

        # ── Shared SNS alarm topic ───────────────────────────
        alarm_topic = sns.Topic(
            self, "FsxAlarmTopic",
            display_name="EDA FSx Alarms",
        )
        alarm_email = self.node.try_get_context("alarm_email")
        if alarm_email:
            alarm_topic.add_subscription(
                subscriptions.EmailSubscription(alarm_email)
            )

        # ══════════════════════════════════════════════════════
        #  FSx for OpenZFS — SINGLE_AZ_HA_2
        # ══════════════════════════════════════════════════════
        if enable_openzfs:
            oz_size = _ctx_int("eda:openzfs_size_gib", 10_240)
            oz_tput = _ctx_int("eda:openzfs_throughput", 2_560)

            if not (OPENZFS_MIN_GIB <= oz_size <= OPENZFS_MAX_GIB):
                raise ValueError(
                    f"eda:openzfs_size_gib={oz_size} must be between "
                    f"{OPENZFS_MIN_GIB} and {OPENZFS_MAX_GIB}"
                )
            if oz_tput not in OPENZFS_THROUGHPUT_VALUES:
                raise ValueError(
                    f"eda:openzfs_throughput={oz_tput} must be one of "
                    f"{OPENZFS_THROUGHPUT_VALUES}"
                )

            oz_key = kms.Key(
                self, "OpenZfsKey",
                alias="eda/fsx-openzfs",
                description="Encryption key for EDA FSx OpenZFS",
                enable_key_rotation=True,
                removal_policy=RemovalPolicy.RETAIN,
            )

            self.openzfs = fsx.CfnFileSystem(
                self, "FsxOpenZfs",
                file_system_type="OPENZFS",
                storage_capacity=oz_size,
                storage_type="SSD",
                subnet_ids=[subnet_id],
                security_group_ids=[sg_fsx.security_group_id],
                kms_key_id=oz_key.key_id,
                open_zfs_configuration=fsx.CfnFileSystem.OpenZFSConfigurationProperty(
                    deployment_type="SINGLE_AZ_HA_2",  # 최고 성능 Single-AZ (NVMe L2ARC)
                    throughput_capacity=oz_tput,
                    automatic_backup_retention_days=7,
                    disk_iops_configuration=fsx.CfnFileSystem.DiskIopsConfigurationProperty(
                        mode="AUTOMATIC",
                    ),
                    options=["DELETE_CHILD_VOLUMES_AND_SNAPSHOTS"],
                ),
            )
            # 데이터 유실 방지: 스택 삭제/리소스 제거 시 파일시스템 보존
            self.openzfs.apply_removal_policy(RemovalPolicy.RETAIN)
            Tags.of(self.openzfs).add("Name", "fsx-eda-openzfs")

            # Scale quotas/reservations proportionally to parent FS capacity.
            # AWS FSx requires each child quota <= parent capacity and sum of reservations <= parent.
            tools_quota       = max(64,  oz_size // 10)   # ~10% of parent, min 64 GiB
            tools_reservation = max(16,  oz_size // 50)
            work_quota        = max(128, oz_size * 4 // 10)  # ~40%
            work_reservation  = max(32,  oz_size * 2 // 10)  # ~20%
            scratch_quota     = max(128, oz_size * 4 // 10)  # ~40%

            self.vol_tools = self._create_openzfs_volume(
                "OzVolTools", "fsxz_tools",
                parent_volume_id=self.openzfs.attr_root_volume_id,
                compression="ZSTD",
                nfs_options=["rw", "crossmnt", "sync"],
                vpc_cidr=vpc_cidr,
                quota_gib=tools_quota,
                reservation_gib=tools_reservation,
            )
            self.vol_work = self._create_openzfs_volume(
                "OzVolWork", "fsxz_work",
                parent_volume_id=self.openzfs.attr_root_volume_id,
                compression="ZSTD",
                nfs_options=["rw", "crossmnt", "sync"],
                vpc_cidr=vpc_cidr,
                quota_gib=work_quota,
                reservation_gib=work_reservation,
            )
            self.vol_scratch = self._create_openzfs_volume(
                "OzVolScratch", "fsxz_scratch",
                parent_volume_id=self.openzfs.attr_root_volume_id,
                compression="LZ4",
                nfs_options=["rw", "crossmnt", "sync"],
                vpc_cidr=vpc_cidr,
                quota_gib=scratch_quota,
                reservation_gib=0,
            )

            self._add_openzfs_alarms(self.openzfs.ref, alarm_topic)

            CfnOutput(self, "OpenZfsFsId", value=self.openzfs.ref)
            CfnOutput(self, "VolToolsId", value=self.vol_tools.ref)
            CfnOutput(self, "VolWorkId", value=self.vol_work.ref)
            CfnOutput(self, "VolScratchId", value=self.vol_scratch.ref)

            ssm.StringParameter(
                self, "SsmOpenZfsDns",
                parameter_name="/eda/storage/OpenZfsDns",
                string_value=self.openzfs.attr_dns_name,
            )
            for name, vol in {
                "VolToolsId": self.vol_tools,
                "VolWorkId": self.vol_work,
                "VolScratchId": self.vol_scratch,
            }.items():
                ssm.StringParameter(
                    self, f"SsmOz{name}",
                    parameter_name=f"/eda/storage/{name}",
                    string_value=vol.ref,
                )

        # ══════════════════════════════════════════════════════
        #  FSx for NetApp ONTAP — SINGLE_AZ_2
        # ══════════════════════════════════════════════════════
        if enable_ontap:
            ot_size = _ctx_int("eda:ontap_size_gib", 10_240)
            ot_tput_per_ha = _ctx_int("eda:ontap_tput_per_ha", 3_072)
            ot_ha_pairs = _ctx_int("eda:ontap_ha_pairs", 1)

            if not (1 <= ot_ha_pairs <= ONTAP_MAX_HA_PAIRS):
                raise ValueError(
                    f"eda:ontap_ha_pairs={ot_ha_pairs} must be 1-{ONTAP_MAX_HA_PAIRS}"
                )
            if ot_tput_per_ha not in ONTAP_TPUT_PER_HA_VALUES:
                raise ValueError(
                    f"eda:ontap_tput_per_ha={ot_tput_per_ha} must be one of "
                    f"{ONTAP_TPUT_PER_HA_VALUES}"
                )
            # SINGLE_AZ_2 최소 1,024 GiB, 최대 1 PiB (HA pair당 512 TiB까지)
            ontap_max = min(1_048_576, ONTAP_MAX_GIB_PER_HA * ot_ha_pairs)
            if not (ONTAP_MIN_GIB <= ot_size <= ontap_max):
                raise ValueError(
                    f"eda:ontap_size_gib={ot_size} must be between "
                    f"{ONTAP_MIN_GIB} and {ontap_max} GiB (with {ot_ha_pairs} HA pairs)"
                )

            ot_key = kms.Key(
                self, "OntapKey",
                alias="eda/fsx-ontap",
                description="Encryption key for EDA FSx ONTAP",
                enable_key_rotation=True,
                removal_policy=RemovalPolicy.RETAIN,
            )

            # fsxadmin password (필수는 아니지만 ONTAP CLI 접속용으로 저장)
            ontap_admin_secret = secretsmanager.Secret(
                self, "OntapAdminSecret",
                secret_name="eda/ontap/fsxadmin",
                description="FSx ONTAP fsxadmin password",
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    password_length=24,
                    exclude_characters='"@/\\\'',
                    require_each_included_type=True,
                ),
            )

            self.ontap = fsx.CfnFileSystem(
                self, "FsxOntap",
                file_system_type="ONTAP",
                storage_capacity=ot_size,
                storage_type="SSD",
                subnet_ids=[subnet_id],
                security_group_ids=[sg_ontap.security_group_id],
                kms_key_id=ot_key.key_id,
                ontap_configuration=fsx.CfnFileSystem.OntapConfigurationProperty(
                    deployment_type="SINGLE_AZ_2",          # 2nd-gen, 최대 12 HA pair
                    ha_pairs=ot_ha_pairs,
                    throughput_capacity_per_ha_pair=ot_tput_per_ha,
                    automatic_backup_retention_days=7,
                    fsx_admin_password=ontap_admin_secret.secret_value.unsafe_unwrap(),
                    disk_iops_configuration=fsx.CfnFileSystem.DiskIopsConfigurationProperty(
                        mode="AUTOMATIC",
                    ),
                ),
            )
            # 데이터 유실 방지
            self.ontap.apply_removal_policy(RemovalPolicy.RETAIN)
            Tags.of(self.ontap).add("Name", "fsx-eda-ontap")

            # Storage Virtual Machine — NFS 접근 entry point
            self.ontap_svm = fsx.CfnStorageVirtualMachine(
                self, "OntapSvm",
                file_system_id=self.ontap.ref,
                name="edasvm",
                root_volume_security_style="UNIX",
            )
            self.ontap_svm.apply_removal_policy(RemovalPolicy.RETAIN)

            # ONTAP volumes — NFS 전용, junction path 로 mount (이름 규칙: [A-Za-z_][A-Za-z0-9_]*)
            #   fsxn_tools    1 TiB
            #   fsxn_work     4 TiB
            #   fsxn_scratch  4 TiB
            self.ontap_vol_tools = self._create_ontap_volume(
                "OntapVolTools", "fsxn_tools", "/fsxn_tools",
                svm_id=self.ontap_svm.attr_storage_virtual_machine_id,
                size_gib=1024,
            )
            self.ontap_vol_work = self._create_ontap_volume(
                "OntapVolWork", "fsxn_work", "/fsxn_work",
                svm_id=self.ontap_svm.attr_storage_virtual_machine_id,
                size_gib=4096,
            )
            self.ontap_vol_scratch = self._create_ontap_volume(
                "OntapVolScratch", "fsxn_scratch", "/fsxn_scratch",
                svm_id=self.ontap_svm.attr_storage_virtual_machine_id,
                size_gib=4096,
            )

            self._add_ontap_alarms(self.ontap.ref, alarm_topic)

            CfnOutput(self, "OntapFsId", value=self.ontap.ref)
            CfnOutput(self, "OntapSvmId",
                      value=self.ontap_svm.attr_storage_virtual_machine_id)
            CfnOutput(self, "OntapVolToolsId", value=self.ontap_vol_tools.ref)
            CfnOutput(self, "OntapVolWorkId", value=self.ontap_vol_work.ref)
            CfnOutput(self, "OntapVolScratchId", value=self.ontap_vol_scratch.ref)
            CfnOutput(self, "OntapAdminSecretArn",
                      value=ontap_admin_secret.secret_arn)

            ssm.StringParameter(
                self, "SsmOntapSvmId",
                parameter_name="/eda/storage/OntapSvmId",
                string_value=self.ontap_svm.attr_storage_virtual_machine_id,
            )
            for name, vol in {
                "OntapVolToolsId": self.ontap_vol_tools,
                "OntapVolWorkId": self.ontap_vol_work,
                "OntapVolScratchId": self.ontap_vol_scratch,
            }.items():
                ssm.StringParameter(
                    self, f"Ssm{name}",
                    parameter_name=f"/eda/storage/{name}",
                    string_value=vol.ref,
                )

        CfnOutput(self, "AlarmTopicArn", value=alarm_topic.topic_arn)

    # ── OpenZFS helpers ─────────────────────────────────────

    def _create_openzfs_volume(
        self,
        construct_id: str,
        name: str,
        *,
        parent_volume_id: str,
        compression: str,
        nfs_options: list[str],
        vpc_cidr: str,
        quota_gib: int,
        reservation_gib: int,
    ) -> fsx.CfnVolume:
        vol = fsx.CfnVolume(
            self, construct_id,
            name=name,
            volume_type="OPENZFS",
            open_zfs_configuration=fsx.CfnVolume.OpenZFSConfigurationProperty(
                parent_volume_id=parent_volume_id,
                data_compression_type=compression,
                nfs_exports=[
                    fsx.CfnVolume.NfsExportsProperty(
                        client_configurations=[
                            fsx.CfnVolume.ClientConfigurationsProperty(
                                clients=vpc_cidr,
                                options=nfs_options,
                            )
                        ]
                    )
                ],
                read_only=False,
                storage_capacity_quota_gib=quota_gib,
                storage_capacity_reservation_gib=reservation_gib,
                copy_tags_to_snapshots=True,
            ),
        )
        # 데이터 유실 방지: 볼륨도 보존
        vol.apply_removal_policy(RemovalPolicy.RETAIN)
        Tags.of(vol).add("Name", name)
        return vol

    def _add_openzfs_alarms(self, fs_id: str, topic: sns.ITopic) -> None:
        for metric_name, threshold, op in [
            ("NetworkThroughputUtilization", 50,
             cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD),
            ("CPUUtilization", 50,
             cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD),
            ("MemoryUtilization", 50,
             cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD),
            ("FileServerCacheHitRatio", 70,
             cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD),
        ]:
            alarm = cloudwatch.Alarm(
                self, f"OzFsx{metric_name}",
                metric=cloudwatch.Metric(
                    namespace="AWS/FSx",
                    metric_name=metric_name,
                    dimensions_map={"FileSystemId": fs_id},
                    period=Duration.minutes(5),
                    statistic="Average",
                ),
                threshold=threshold,
                evaluation_periods=3,
                comparison_operator=op,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=f"FSx OpenZFS — {metric_name}",
            )
            alarm.add_alarm_action(cw_actions.SnsAction(topic))

    # ── ONTAP helpers ───────────────────────────────────────

    def _create_ontap_volume(
        self,
        construct_id: str,
        name: str,
        junction_path: str,
        *,
        svm_id: str,
        size_gib: int,
    ) -> fsx.CfnVolume:
        vol = fsx.CfnVolume(
            self, construct_id,
            name=name,
            volume_type="ONTAP",
            ontap_configuration=fsx.CfnVolume.OntapConfigurationProperty(
                storage_virtual_machine_id=svm_id,
                size_in_bytes=str(size_gib * 1024 * 1024 * 1024),
                junction_path=junction_path,
                security_style="UNIX",
                storage_efficiency_enabled="true",
                ontap_volume_type="RW",
                snapshot_policy="default",
                tiering_policy=fsx.CfnVolume.TieringPolicyProperty(
                    name="NONE",  # EDA는 hot data — capacity pool 티어링 비활성
                ),
            ),
        )
        # 데이터 유실 방지
        vol.apply_removal_policy(RemovalPolicy.RETAIN)
        Tags.of(vol).add("Name", name)
        return vol

    def _add_ontap_alarms(self, fs_id: str, topic: sns.ITopic) -> None:
        for metric_name, threshold, op in [
            ("NetworkThroughputUtilization", 50,
             cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD),
            ("CPUUtilization", 50,
             cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD),
            ("StorageCapacityUtilization", 80,
             cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD),
        ]:
            alarm = cloudwatch.Alarm(
                self, f"OntapFsx{metric_name}",
                metric=cloudwatch.Metric(
                    namespace="AWS/FSx",
                    metric_name=metric_name,
                    dimensions_map={"FileSystemId": fs_id},
                    period=Duration.minutes(5),
                    statistic="Average",
                ),
                threshold=threshold,
                evaluation_periods=3,
                comparison_operator=op,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=f"FSx ONTAP — {metric_name}",
            )
            alarm.add_alarm_action(cw_actions.SnsAction(topic))
