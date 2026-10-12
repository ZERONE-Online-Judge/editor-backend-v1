# Editor 독립 서버 배포

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
지정합니다. SMTP 호스트·STARTTLS 포트·계정·비밀번호·발신자를 입력합니다.
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
프로그램이 stdout을 작성해 감독 기록을 덮어쓸 수 없습니다.

기본 파일 제한은 파일당 16MiB, 실행 입력/소스/리소스 합계 64MiB,
입출력 1MiB, 생성기 출력 16MiB, 프로젝트 blob 8GiB입니다.
ZIP 업로드는 128MiB, 압축 해제 합계는 16GiB입니다.
DB에는 작업 사본·리비전·실행 기록, `/data`에는 원본과 출력 blob·패키지·PDF가
저장됩니다. API와 워커가 같은 `/data`를 사용합니다.

실행 이미지나 컴파일 설정을 변경하면 `EDITOR_RUNTIME_REVISION`을 올리고
API/워커를 재생성합니다. 동일 자료의 이전 환경 검증 결과는 재사용되지
않습니다. 실행 결과에 당시 런타임 이미지 ID를 보관합니다.

## 업데이트와 복구

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
