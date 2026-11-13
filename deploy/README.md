# Editor 독립 서버 배포

## GitHub Actions 전용 러너

새 Editor 서버에 저장소별 러너 2개를 등록합니다. 기존 ZOJ 러너는
재사용하지 않습니다. `deploy/setup-runners.sh`는 Ubuntu 24.04 x86_64용이며
Docker·Compose·전용 `editor-runner` 계정과 서비스를 설치합니다. 등록
토큰은 mode 600 파일로 전달하고 설치 종료 시 삭제합니다.

- 백엔드: `zoj-editor-backend`, `zoj-editor,editor-backend` 레이블
- 프런트엔드: `zoj-editor-frontend`, `zoj-editor,editor-frontend` 레이블
- 환경변수: `/srv/zoj-editor/config/editor.env` (Git 외부, mode 600)
- 러너 디렉터리: `/srv/zoj-editor/runners/backend`, `frontend`

설치 방식과 토큰 유효기간은
[GitHub 공식 러너 등록 문서](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)를
따릅니다. 공개 저장소의 PR·기능 브랜치는 GitHub 호스팅 러너에서만
실행합니다. 운영 러너는 검증을 통과한 `main` 배포와 관리자가 수동 실행한
고정된 러너 점검 작업만 실행합니다. Docker 그룹은 호스트 제어 권한이므로
저장소 관리자와 운영 `main` 변경 권한을 제한해야 합니다.
[GitHub 보안 안내](https://docs.github.com/en/actions/reference/security/secure-use)를
참고하세요.

`integration/editor-v1`은 검토용이며 자동 운영 배포를 하지 않습니다.
처음에는 두 저장소에 운영 `main`을 준비하고 hosted 검증을 통과시킵니다.
백엔드 배포가 공통 스택을 준비한 뒤 프런트엔드도 동일한 스택을 사용합니다.
각 배포는 해당 저장소의 검증된 SHA만 변경하며 상대 저장소는 마지막 정상
배포 버전을 유지합니다. 첫 배포만 상대 `main`의 테스트/빌드 성공을 확인합니다.

로그인과 인증 메일은 ZOJ 공통 인증 API를 사용합니다.
`EDITOR_AUTH_API_URL`과 별도 `EDITOR_AUTH_BRIDGE_SECRET`을 양쪽 서버에
설정하세요. 내부 HTTPS 연결은 `EDITOR_AUTH_CA_FILE`로 공개 CA 인증서를
검증합니다. SMTP 비밀번호를 Editor에 복사하지 않습니다. ZOJ 메일 작업자는
기존 설정을 유지합니다. [공통 로그인](../docs/shared-login.md)을 확인하세요.
배포는 중앙 인증 API의 서명·TLS·호환성을 먼저 확인합니다. 기존 API는
새 버전의 상태 확인과 트래픽 전환 중에도 계속 실행합니다. ZOJ 인증 API를
먼저 배포해야 합니다.

## Blue/green 배포와 복구

양쪽 저장소의 배포와 정기 백업은 같은
`/srv/zoj-editor/config/deploy.lock`을 사용합니다. 배포 이벤트의 정확한
테스트 완료 SHA만 변경하고, 상대 저장소는 `release.env`의 마지막 정상
SHA를 유지합니다. 최신 `main`보다 오래된 이벤트는 건너뜁니다. 프런트엔드
배포도 운영 백엔드 SHA의 공통 `deploy-main.sh`로 위임합니다.

- `db`와 `/srv/zoj-editor/data`는 두 슬롯이 공유합니다.
- `api-blue/green`, `frontend-blue/green`은 호스트 포트를 열지 않습니다.
- 안정적인 `frontend` 게이트웨이가 기존 8090 바인딩·신뢰 프록시 ACL을
  유지하고, `internal-gateway`가 기존 9443 TLS·내부망 ACL을 유지합니다.
- API/화면 후보의 DB·인증·실행 노드·정확한 릴리스 SHA를 확인한 뒤
  `traffic/active.conf`를 원자적으로 교체하고 두 Nginx를 reload합니다.
  공개 HTTPS와 내부 게이트웨이가 모두 후보 SHA를 반환해야 성공합니다.
- 트래픽 파일과 게이트웨이 템플릿은 **디렉터리**로 마운트합니다. Git
  checkout이나 원자적 파일 교체 때문에 이전 inode를 계속 읽지 않습니다.
  설정 변경도 스냅샷·재렌더·검사·reload하며 실패하면 원래 파일을 복구합니다.
- 이전 슬롯은 계속 실행합니다. 이전 Nginx 연결이 모두 끝나기 전에는 그
  슬롯을 다음 후보로 재생성하지 않습니다. `/assets/`의 404만 이전 화면
  슬롯으로 fallback하므로 기존 탭의 이전 JS 청크도 한 릴리스 동안 유지합니다.

워커 슬롯은 화면/API 색상과 독립적으로 선택합니다. 화면만 배포하면
워커는 재시작하거나 drain하지 않습니다. 백엔드 배포는 새 워커를 paused
상태로 준비하고, 기존 작업·폴더 내보내기·메일 전송의 완료를 기다립니다.
이전 워커를 완전히 멈춘 뒤, 새 경로의 상태 검증에 성공하면 새 워커를
`SIGUSR2`로 활성화합니다. 큐나 작업 상태를 재설정하지 않습니다.
`SIGUSR1`은 drain, `SIGUSR2`는 재개이며, `/tmp/editor-worker-ready`와
`/tmp/editor-worker-drained`가 상태를 나타냅니다. 활성화 토큰은 컨테이너
재시작에도 유지됩니다. 수동으로 활성 워커를 **재생성**한 경우 새 컨테이너는
paused이므로 경로·다른 워커 중지 여부를 확인한 뒤 `SIGUSR2`가 필요합니다.

공유 DB의 현재 revision이 같으면 마이그레이션을 실행하지 않습니다.
새 revision은 `app/deployment.py`에 정확한 파일 SHA256과 함께 온라인
호환성이 검토된 경우에만 적용합니다. 기존 API·워커와 호환되는 additive
변경만 허용하며, 변경 전에 private `pg_dump`를 남깁니다. 파괴적 변경은
별도 점검 시간 배포가 필요합니다. 자동 DB downgrade나 백업 덮어쓰기는
하지 않습니다. 스키마가 이전 이미지와 다르면 수동 rollback도 거부합니다.

경로·워커 활성화 검증에 실패하면 원래 게이트웨이 설정·경로·두 릴리스
메타데이터·소스 checkout을 복원합니다. 활성 후보 작업은 먼저 끝내고
이전 워커를 재개합니다. 복구 자체가 실패하면 `config/recovery-required`로
이후 배포를 막습니다. 파일을 지우기 전에 실제 경로, DB 호환성, 양쪽 워커와
작업을 확인하고 하나의 소비자만 동작하도록 운영자가 복구해야 합니다.
구버전 워커의 drain 시간 초과도 실행 중 작업을 강제로 중단하지 않습니다.
안전하게 완료될 때까지 다시 기다린 뒤 재시작하며, 완료할 수 없으면 복구
표시를 남깁니다.

`release.env`는 기존 이미지 키를 유지하고 활성 슬롯·워커·두 색상의 이미지와
스키마 revision을 추가합니다. `release.previous.env`는 이전 정상 릴리스입니다.
GitHub Actions의 **Roll back Editor release** 수동 작업이나 다음 명령으로
두 정상 blue/green 슬롯 사이를 되돌립니다. 재실행하면 직전
버전으로 다시 전환할 수 있습니다. 보관된 이미지와 컨테이너를 삭제하지 마세요.
GitHub에 접속할 수 없어도 로컬 메타데이터로 복구합니다.

```sh
bash deploy/rollback.sh

docker compose --project-name zoj-editor \
  --env-file /srv/zoj-editor/config/editor.env \
  --env-file /srv/zoj-editor/config/release.env -f deploy/compose.yaml ps
```

최초 전환은 기존 정상 배포가 필요합니다. 먼저 `/healthz`, `/release.txt`,
`EDITOR_API_UPSTREAM`을 지원하는 테스트 완료 프런트엔드를 기존 배포 방식으로
배포하고, 그 SHA를 유지한 채 백엔드를 blue/green으로 전환합니다. 기존 API는
계속 실행하며 게이트웨이의 포트 소유권을 옮기는 첫 1회에만 짧은 재생성이
있습니다. 이 단계 실패 시 저장된 `config/legacy` Compose와 정확한 구버전
이미지로 복원합니다. 첫 성공 후의 수동 rollback은 정상 blue/green 슬롯이
두 개 생긴 다음 사용합니다. 기존 `api` 컨테이너는 이 전환 동안
복구용으로 유지하며, 두 blue/green 릴리스와 수동 복구 검증이 끝난 뒤에만
중지할 수 있습니다. 실행 중인 작업이 있는 워커를 정리하지 않습니다.

설치된 `/usr/local/lib/zoj-editor-backup/editor_backup.py`도 첫 전환 전에
고정된 관리 경로에서 원자적으로 갱신합니다. 백업·장애 점검·채점 점검 도구는
`release.env`의 활성 API/워커를 선택하며 기존 단일 서비스도 지원합니다.

로컬 검증은 `pytest -q tests/test_deployment.py tests/test_worker.py`와
`pytest -q tests/test_access_mail.py tests/test_backup.py`이며, 실제 Nginx·Docker 검증은
`bash deploy/tests/blue-green-smoke.sh`입니다. 스모크는 별도 프로젝트·격리된
네트워크에서 ACL·TLS·후보 SHA·상태 실패 복구·연속 HTTP·오래 걸리는 응답의
완료·이전 청크 fallback을 검사하고 리소스를 제거합니다.

기존 ZOJ 서버에 이 구성을 적용하지 않습니다. 새 Linux VM에 두 저장소를
아래 디렉터리명으로 배치하고 Docker Compose를 설치합니다.

```text
/srv/editor-source/editor_backend_v1
/srv/editor-source/editor_frontend_v1
```

## 서버 준비

이 Compose는 기존 Editor 서버의 blue/green 전환과 이후 배포용입니다.
새 서버는 DB·데이터·TLS·비공개 환경과 기존 정상 릴리스 메타데이터를 먼저
준비한 뒤 위 최초 전환 절차를 따릅니다. `.env.example`을 참고하여 설정을
Git 밖 `/srv/zoj-editor/config/editor.env`에 mode 600으로 보관합니다.
DB 비밀번호와 세션 키는 무작위 값이어야 하며 중앙 인증 연결 키·CA 파일이
필요합니다. 프록시 외 주소에서 8090에 접근하지 못하도록 호스트 방화벽도
유지합니다. `EDITOR_INGRESS_CIDR`은 신뢰 프록시의 정확한 주소 범위이며
운영 게이트웨이에 `0.0.0.0/0`을 지정하지 않습니다.

## 실행 자원과 데이터

한 테스트 실행은 CPU 1개와 별도 메모리/프로세스/출력/경과 시간 한도를
사용합니다. 워커 동시 작업 기본값은 2개이며, 각 작업의 테스트는 순차
실행합니다. Docker 소켓 권한은 호스트 제어 권한이므로 워커는 이 서비스
전용 서버에서 실행합니다. 컨테이너는 커널을 공유합니다. 이 구성은 별도
커널의 VM 수준 격리가 아닙니다.

업로드한 코드는 UID 1000에서 실행되며 네트워크, 호스트 파일 바인딩,
원본 수정 권한이 없습니다. 감독 프로세스가 원본 출력과 자원 계측을
수집합니다. 모든 임시 컨테이너와 익명 작업 볼륨은 실행 종료 후 제거합니다.
프로그램이 stdout을 작성해 감독 기록을 덮어쓸 수 없습니다. CPU 시간과
최대 메모리는 네이티브 런처가 프로그램과 자식 프로세스에서 수집하며,
Python 감독 프로세스의 시작 메모리를 포함하지 않습니다.

기본 파일 제한은 파일당 16MiB, 실행 입력/소스/리소스 합계 64MiB,
입출력 1MiB, 생성기 출력 16MiB, 프로젝트 blob 8GiB입니다.
ZIP 업로드는 기본 2GiB, 압축 해제 합계는 16GiB입니다.
가져오는 ZIP은 임시 디스크에 저장하고 가져오기 종료 시 제거합니다.
`EDITOR_MAX_ARCHIVE_BYTES`로 ZIP 상한을 조정할 수 있습니다. 값을 변경할
때는 외부 프록시와 프런트엔드의 `client_max_body_size`도 함께 맞춥니다.
화면은 `/api/imports/limits`에서 현재 서버의 한도를 조회합니다.
DB에는 작업 사본·리비전·실행 기록, `/data`에는 원본과 출력 blob·패키지·PDF가
저장됩니다. API와 워커가 같은 `/data`를 사용합니다.

실행 이미지나 컴파일 설정을 변경하면 `EDITOR_RUNTIME_REVISION`을 올리고
API/워커를 재생성합니다. 동일 자료의 이전 환경 검증 결과는 재사용되지
않습니다. 실행 결과에 당시 런타임 이미지 ID를 보관합니다.

## 업데이트와 복구

Editor 전용 300 GiB 디스크의 정기 백업·암호화·보관 정책·실제 복구 검사는
[자동 백업 운영 안내](../docs/backups.md)를 따릅니다. 설치는
`deploy/backup/install.sh`로 진행하며, 저장된 미커밋 작업 사본도 포함합니다.
백업의 보관·복구 검증도 배포와 별도로 유지합니다.

업데이트는 검증된 `main` 워크플로를 사용합니다. 수동 `compose stop api
worker`나 공통 스택의 일괄 재생성은 blue/green 배포 경로를 우회하므로 사용하지
않습니다. 백업은 DB와 `/data`를 함께 보관해야 하며 DB만 복원하면 파일 참조가
끊어집니다. 자동 백업은 동일한 DB 스냅샷과 파일 참조 검증을 사용합니다.

백업 파일에는 비공개 문제·해설·테스트가 있으므로 접근을 제한합니다.
현재 문제 휴지통은 복구 가능한 soft delete입니다. 리비전과 검증 기록은
자동으로 지우지 않습니다. 디스크 사용량은 운영 서버에서 관리합니다.


## 채점 에이전트 전체 실행

운영 워커는 `EDITOR_EXECUTION_TRANSPORT=agent`로 동작합니다. 워커의 Docker
소켓과 Docker CLI를 제거했으며 모든 실행 작업은 ZOJ 노드가 처리합니다.
Main 정답 생성과 풀이 검증은 같은 실제 채점기를 사용하고, 생성기·Validator·
Checker 자체 테스트·PDF 실행은 노드의 제작 실행이 처리합니다. 파일·패키지
구성과 DB 저장은 이 서버가 맡습니다.

ZOJ 백엔드의 제작 큐·정답 생성 프로토콜과 agent 0.3.1의 실행/PDF 이미지 준비를
먼저 적용하세요. 관리자 채점 노드에 Editor를 허용해야 합니다. 배포 사전
점검은 서명된 내부 API로 준비된 실행 노드를 확인하며, 없으면 기존 서비스를
멈추기 전에 종료합니다. 구버전 에이전트는 제작 실행을 가져가지 않습니다.
실행 이미지는 채점 VM에서 빌드하며 Editor 운영 배포는 API/화면만 빌드합니다.
명시적인 오프라인 재현·Docker 회귀 테스트만 자체 실행기를 사용합니다.
서비스의 작업 생성과 워커는 개발 설정에서도 ZOJ 에이전트를 강제합니다.

## Paired frontend and backend releases

For a coordinated release, run Editor Frontend CI manually on its current
`main` SHA. Manual frontend runs perform all build, test and container checks
without deploying. After that run succeeds, dispatch Editor Backend CI on
`main` with its exact `frontend_sha` and `frontend_ci_run`.

The backend workflow verifies the frontend repository, workflow, branch,
commit and successful build, then runs its complete backend checks. The
coordinator selects both tested commits in one cutover, retaining the normal
deployment lock, connection drain, worker handoff, health checks and rollback.
If either main branch has moved, the superseded release is skipped. Ordinary
push deployments continue to update one component at a time.
