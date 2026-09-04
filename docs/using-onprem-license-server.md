[English](./using-onprem-license-server.md) | [한국어](./using-onprem-license-server.ko.md)

# Using an On-Premises License Server

This guide shows how AWS Compute Nodes use an on-premises License Server
through a Site-to-Site VPN.

## Prerequisites

Confirm the following with the customer's license administrator:

- License Server hostname
- `lmgrd` TCP port from the license file `SERVER` line
- Fixed vendor daemon TCP port from the `VENDOR` line

The network must provide:

- Bidirectional routes between AWS and on-premises
- DNS resolution of the License Server hostname from Compute Nodes
- Firewall access to both the `lmgrd` and vendor daemon TCP ports

## Submit a job

For example, if the `lmgrd` port in the `SERVER` line is 27020:

```bash
export SNPSLMD_LICENSE_FILE=27020@license01.corp.example.com

# Set only when required
export LM_LICENSE_FILE="${SNPSLMD_LICENSE_FILE}"

sbatch your-eda-job.sbatch
```

When the license environment variables are set on the Login Node and `sbatch`
is run from the same shell, Slurm passes those variables to the job running on
the Compute Node. The EDA tool in the job uses the values to access the
on-premises License Server.

Replace the hostname and `27020` with the customer's actual values.

## Verify

Check that the job running on the Compute Node received the variables:

```bash
echo "${SNPSLMD_LICENSE_FILE}"
echo "${LM_LICENSE_FILE}"
```

Environment propagation alone does not prove a successful license checkout.
Run a short job with the actual EDA tool and verify that the required feature
is checked out successfully.

The AWS License Server EC2 deployed by this project is not used in this mode.

## References

- [Slurm `sbatch` documentation](https://slurm.schedmd.com/sbatch.html)
- [AWS ParallelCluster Slurm Workload Manager](https://docs.aws.amazon.com/parallelcluster/latest/ug/slurm-workload-manager-v3.html)
- [How AWS Site-to-Site VPN works](https://docs.aws.amazon.com/vpn/latest/s2svpn/how_it_works.html)
- [Forwarding outbound DNS queries with Route 53 Resolver](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-forwarding-outbound-queries.html)
