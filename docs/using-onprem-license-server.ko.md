[English](./using-onprem-license-server.md) | [한국어](./using-onprem-license-server.ko.md)

# 온프레미스 License Server 활용하기

AWS Compute Node에서 Site-to-Site VPN을 통해 온프레미스 License Server를
사용하는 방법입니다.

## 사전 확인

고객 라이선스 관리자에게 다음 정보를 확인합니다.

- License Server hostname
- 라이선스 파일 `SERVER` 행의 `lmgrd` TCP 포트
- 라이선스 파일 `VENDOR` 행의 고정 vendor daemon TCP 포트

네트워크에서는 다음 조건이 필요합니다.

- AWS와 온프레미스 간 양방향 route
- Compute Node에서 License Server hostname을 해석할 수 있는 DNS
- `lmgrd`와 vendor daemon TCP 포트에 대한 방화벽 허용

## Job 제출

예를 들어 `SERVER` 행의 `lmgrd` 포트가 27020인 경우:

```bash
export SNPSLMD_LICENSE_FILE=27020@license01.corp.example.com

# 필요한 경우에만 설정
export LM_LICENSE_FILE="${SNPSLMD_LICENSE_FILE}"

sbatch your-eda-job.sbatch
```

Login Node에서 라이선스 환경변수를 설정하고 같은 shell에서 `sbatch` 명령으로
Job을 제출하면, Slurm이 해당 환경변수를 Compute Node에서 실행되는 Job에
전달합니다. Job에서 실행되는 EDA 도구는 전달받은 값을 사용하여 온프레미스
License Server에 접근합니다.

`27020`과 hostname은 예시입니다. 실제 고객 환경의 값으로 바꿉니다.

## 확인

Compute Node에서 실행되는 Job이 환경변수를 받았는지 확인합니다.

```bash
echo "${SNPSLMD_LICENSE_FILE}"
echo "${LM_LICENSE_FILE}"
```

환경변수 확인만으로 license checkout이 검증되는 것은 아닙니다. 실제 EDA
도구의 짧은 Job을 실행하여 필요한 feature가 정상적으로 checkout되는지
확인합니다.

AWS 내부에 생성된 License Server EC2는 이 방식에서 사용하지 않습니다.

## 참고 문서

- [Slurm `sbatch` 공식 매뉴얼](https://slurm.schedmd.com/sbatch.html)
- [AWS ParallelCluster Slurm Workload Manager](https://docs.aws.amazon.com/parallelcluster/latest/ug/slurm-workload-manager-v3.html)
- [AWS Site-to-Site VPN 동작 방식](https://docs.aws.amazon.com/vpn/latest/s2svpn/how_it_works.html)
- [Route 53 Resolver를 통한 온프레미스 DNS 질의 전달](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-forwarding-outbound-queries.html)
