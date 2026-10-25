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
배포는 중앙 인증 API의 서명·TLS·호환성을 먼저 확인하고, 성공한 뒤에만
기존 API·워커를 멈춥니다. ZOJ 인증 API를 먼저 배포해야 합니다.

서버의 `flock`은 양쪽 러너 배포를 직렬화합니다. 오래된 이벤트는 최신
`main`과 비교해 건너뜁니다. 이미지는 커밋 SHA로 구분합니다. 기존 워커는
새 작업을 받지 않고 실행 중 작업을 마친 뒤 종료하므로 대기열을 유지합니다.
DB 마이그레이션 전에 쓰기를 멈추고 private `pg_dump` 백업을 보관합니다.
마이그레이션·상태 확인 실패는 자동으로 DB를 덮어쓰지 않고 실패로 표시합니다.

정상 릴리스 이미지 목록은 `/srv/zoj-editor/config/release.env`에 기록합니다.
장애 복구에서는 해당 파일의 이전 이미지와 백업을 확인하고 수동으로
복구합니다. DB 스키마의 역호환성을 확인하기 전 이전 이미지를 무작정
재시작하지 마세요. 정상 배포의 수동 점검 명령:

```sh
docker compose --env-file /srv/zoj-editor/config/editor.env \
  --env-file /srv/zoj-editor/config/release.env -f compose.yaml ps
```

기존 ZOJ 서버에 이 구성을 적용하지 않습니다. 새 Linux VM에 두 저장소를
아래 디렉터리명으로 배치하고 Docker Compose를 설치합니다.

```text
/srv/editor-source/editor_backend_v1
/srv/editor-source/editor_frontend_v1
```

## 최초 준비

백엔드의 `deploy` 디렉터리에서 실행합니다.

```sh
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
stat -c '%g' /var/run/docker.sock
```

출력한 무작위 값을 `EDITOR_SESSION_SECRET`에, Docker 소켓 그룹 번호를
`EDITOR_DOCKER_GID`에 넣습니다. DB 비밀번호는 무작위 영문·숫자로
지정합니다. ZOJ 공통 인증 API 주소와 전용 인증 연결 키를 입력합니다.
키와 비밀번호는 Git에 넣지 않습니다.

별도 리버스 프록시를 사용할 경우 `EDITOR_BIND_ADDRESS`는 새 VM의 사설
주소, `EDITOR_INGRESS_CIDR`은 해당 프록시의 정확한 IP/32로 설정합니다.
프록시 외의 주소에서 8090 포트로 들어오지 않도록 서버 방화벽을 설정합니다.
같은 VM의 프록시를 사용하면 기본 loopback 바인딩을 유지합니다.

```sh
sudo install -d -m 0750 -o 1000 -g 1000 /srv/zoj-editor/data
sudo install -d -m 0750 /srv/zoj-editor/postgres
docker compose --profile images build
docker compose up -d db
docker compose run --rm api alembic upgrade head
docker compose up -d api worker frontend
docker compose ps
```

런타임과 렌더러는 이미지 빌드용 서비스입니다. 상시 실행하지 않습니다.
`editor.zoj.kr` DNS와 HTTPS는 새 서비스의 프록시로 연결합니다.
[reverse-proxy.conf.example](reverse-proxy.conf.example)의 새 VM 주소를
교체하고 기존 TLS 가상 호스트에 반영합니다. 프런트엔드는 API와 DB의
공개 포트를 만들지 않습니다. 배포 후 실제 이메일 로그인과 테스트 검증을
진행해야 운영 연결까지 완료됩니다.

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
아래 수동 백업은 자동 백업 구성을 대신하지 않습니다.

```sh
docker compose --profile images build
docker compose stop worker
docker compose run --rm api alembic upgrade head
docker compose up -d --force-recreate api worker frontend
docker compose logs --tail=100 worker
```

백업 시 API/워커 쓰기를 잠시 중단하고 DB와 `/data`를 함께 백업합니다.
DB만 복원하면 파일 참조가 끊어집니다.

```sh
docker compose stop api worker
docker compose exec -T db pg_dump -U editor -d editor -Fc > editor-db.dump
sudo tar -C /srv/zoj-editor -czf editor-data.tar.gz data
docker compose start api worker
```

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
