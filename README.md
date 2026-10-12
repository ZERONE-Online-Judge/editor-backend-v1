# ZOJ Editor Backend

`editor.zoj.kr`의 독립 문제 제작 API와 검증 워커입니다.
계정·DB·채점 노드·배포는 기존 ZOJ와 별도로 운영합니다.
선택적으로 같은 이메일의 ZOJ 운영자 권한으로 문제를 전송할 수 있습니다.
프런트엔드 저장소: https://github.com/ZERONE-Online-Judge/editor-frontend-v1

## 구현한 작업 흐름

1. 이메일 인증번호로 독립 계정에 로그인합니다.
   내 폴더·하위 폴더에 문제를 정리하고 여러 문제를 한 번에 옮깁니다.
2. 문제 생성 후 개인 작업 사본에서 지문·파일·풀이·테스트를 편집합니다.
3. 생성기로 입력을 만들고 validator, Main 풀이, checker를 실행합니다.
4. 풀이의 실제 판정과 MA/OK/WA/TL/ML 등 기대 판정을 비교합니다.
5. 동료와 이슈·권한·리비전을 공유하고, 충돌을 명시적으로 해결합니다.
6. 커밋한 문제를 검증하여 Editor 또는 ZOJ ZIP으로 내보냅니다.
   Editor에서 내보낸 두 형식 모두 작업 자료를 복원할 수 있습니다.
7. ZOJ 문제 등록 권한이 있는 대회·참가 유형을 선택해 새 문제로 전송합니다.

설정·지원 범위는 [ZOJ 전송 안내](docs/zoj-publishing.md)를 확인하세요.

다국어 LaTeX/KaTeX 지문, 한글 PDF, 표준/사용자 checker, validator 자체
테스트, 추가 validator, interactor, grader 리소스, 테스트 세트·그룹·부분
점수·의존 그룹, 스트레스 테스트와 유효한 반례 보관을 포함합니다.

## 개발 실행

Python 3.13, Docker, Node 22.12+가 필요합니다. 두 저장소는 다음 이름으로
같은 부모 디렉터리에 체크아웃합니다.

```text
editor_backend_v1/
editor_frontend_v1/
```

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
docker build -t zoj-editor-runtime:v1 runner
docker build -t zoj-editor-renderer:v1 -f runner/Renderer.Dockerfile runner
(cd ../editor_frontend_v1 && npm ci)
bash deploy/dev.sh
```

http://127.0.0.1:5190 에서 확인합니다. 개발 스크립트는 별도
`data/preview` DB를 사용하고 명시적으로 로컬 메일 모드로 실행합니다.
`editor@example.com`에 보낸 인증번호는
`data/preview/development-mail/*.json`에 저장됩니다. 외부로 메일을 보내지
않습니다. 다른 이메일로 로그인하면 새로운 독립 계정이 만들어집니다.
Ctrl+C로 개발 프로세스를 종료합니다. 운영에서는 이 예제와 메일 모드를
사용할 수 없습니다.

## 별도 서버 배포

구성 및 실제 명령은 [deploy/README.md](deploy/README.md)에 있습니다.
PostgreSQL 17, API, 별도 워커, 프런트엔드를 새 서버에 올립니다.
코드 실행은 작업마다 네트워크가 없는 컨테이너에서 수행합니다.
API에 Docker 소켓을 연결하지 않습니다.

OpenAPI: `/docs`. 브라우저 변경 요청은 `X-Editor-Request: 1` 헤더가 필요합니다.
인증은 HttpOnly 세션 쿠키입니다. 운영은 HTTPS·PostgreSQL·실제 SMTP·무작위
세션 비밀키가 없으면 시작하지 않습니다.

## 검증

```sh
.venv/bin/pytest -q
RUN_SANDBOX_TESTS=1 .venv/bin/pytest -q
EDITOR_DATABASE_URL=sqlite:////tmp/editor-schema.db .venv/bin/alembic upgrade head
EDITOR_DATABASE_URL=sqlite:////tmp/editor-schema.db .venv/bin/alembic check
```

실제 Docker 실행 테스트는 위 두 런타임 이미지를 빌드해야 통과합니다.
SMTP 발송은 개발 테스트에서 외부로 전송하지 않습니다. 운영 SMTP는 서버
환경변수를 넣은 뒤 실제 수신 메일로 별도 확인해야 합니다.

## 브랜치와 커밋

기능별 브랜치를 다음 순서로 쌓습니다. 각 브랜치는 앞 브랜치에 의존합니다.
`integration/editor-v1`은 전체 구현을 포함하는 검토용 브랜치입니다.
원격 main 반영이나 운영 배포를 실행하는 CI는 없습니다.

- `feature/platform-foundation`: 독립 설정·데이터 모델·마이그레이션
- `feature/email-auth`: 인증번호 로그인·세션·프로필
- `feature/problem-authoring`: 작업 사본·협업·리비전·충돌·파일
- `feature/verification-runtime`: 격리 실행·생성·검증·채점·스트레스
- `feature/problem-packages`: 문제 ZIP·가져오기·한글 지문 렌더링
- `feature/service-deployment`: 별도 서버 배포·CI·문서
- `fix/runtime-accounting`: 감독 프로세스와 프로그램의 CPU·메모리 분리

모든 커밋은 `AGENTS.md`의 타입·제목·본문 규칙을 따릅니다.

## Polygon 참고 범위

공개 기능 명세에 따라 독립 구현했습니다. 대응표와 차이는
[docs/polygon-reference.md](docs/polygon-reference.md)를 확인하세요.
Codeforces의 비공개 소스나 서비스 계정을 가져오지 않습니다.
`testlib.h`와 표준 checker는 upstream 원본과 MIT 라이선스를 포함합니다.
이 문제 제작 서비스의 시간·메모리 수치는 기존 ZOJ 운영 채점 VM에서
측정한 값이 아닙니다. 최종 제한은 배포 대상 채점 환경에서 재검증하세요.

- `feature/project-folders`: 개인 폴더·하위 폴더·일괄 이동
- `feature/zoj-publishing`: ZOJ 형식 패키지·권한 확인·문제 전송
