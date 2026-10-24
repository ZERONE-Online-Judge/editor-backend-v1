# ZOJ Editor Backend

`editor.zoj.kr`의 독립 문제 제작 API와 검증 워커입니다.
문제 DB·검증 워커·배포는 별도로 운영하며, 계정·비밀번호·로그인은 ZOJ와 공유합니다.
선택적으로 같은 이메일의 ZOJ 운영자 권한으로 문제를 전송할 수 있습니다.
프런트엔드 저장소: https://github.com/ZERONE-Online-Judge/editor-frontend-v1

## 구현한 작업 흐름

1. ZOJ 공통 계정의 이메일 인증번호 또는 등록한 비밀번호로 로그인합니다.
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

선택 범위 실행과 검수자 권한, 반례 수집·반영 및 서버 실행 한도는
[Invocation 안내](docs/invocations.md)를 확인하세요.

## Polygon Linux 패키지 가져오기

공식 `assets/solutions`, `files/executables`, `assets/validators` 구조와 기존
Editor XML 구조를 읽습니다. 지문은 `properties.json` 또는 Polygon의
`problem-properties.json`, 개별 `statement-sections`, 전체 TeX 순서로
복원하며 전체 문서의 제목·제한·예제 환경을 본문에 섞지 않습니다.
지문 표시용 예제는 실제 테스트 입출력과 분리하여 보관합니다. 생성기
명령·설명·기대 판정·태그·Validator 연결도 복원합니다.

`testlib.h`·표준 Checker는 서비스의 공용 파일을 사용합니다. 원래 소스,
스트레스 설정, 렌더링 파일 등 문서 모델에 직접 대응하지 않는 원본은
실행에 첨부하지 않는 `polygon-…` 보조 파일로 보관합니다. 원본 스트레스
설정은 새 실행 기록으로 간주하지 않습니다. 지원하지 않는 C++23 등은
C++20으로 변환한 사실을 결과에 알리며, 가져온 뒤 다시 검증해야 합니다.
같은 이름으로 덮어쓰게 되는 소스는 가져오기를 중단합니다.

`POST /api/imports`의 `import_report`에는 복원한 자료 수, 변환 경고와
원본 경로/보조 파일명 대응표가 포함됩니다. 기존에 불완전하게 가져온
문제를 자동으로 덮어쓰지 않으므로 수정 후 원본 패키지를 다시 가져오세요.
비공개 패키지 검증은 `EDITOR_REAL_POLYGON_PACKAGE`를 지정한 로컬 테스트로
수행하며 원본 패키지를 저장소에 추가하지 않습니다.

## 새 공유 리비전 감지

`GET /api/problems/{id}/revision-status?after=12&wait_seconds=25`는 최대
25초 기다렸다가 현재 공유 리비전 번호, 작성자 표시명, 커밋 메시지
앞 240자와 생성 시각을 반환합니다. `after`보다 새 리비전이 있으면
바로 반환합니다. 개인 작업 사본 저장은 공유 리비전을 바꾸지 않습니다.

대기 중에는 1초 간격으로 커밋 메타데이터와 직접·공유 폴더 접근 권한만
조회합니다. 지문·파일·작업 사본을 읽거나 생성하지 않으며, DB 연결을
계속 점유하지 않습니다. 응답 전 로그인과 접근 권한을 다시 확인하고,
권한이 사라진 요청에는 메타데이터를 반환하지 않습니다. 숨겨진 탭과
끊어진 클라이언트는 대기를 중단합니다.

UI는 안내만 표시하며, 사용자가 `업데이트`를 눌렀을 때 기존 작업 사본
병합과 충돌 해결 절차를 실행합니다. 이 API를 먼저 배포한 뒤 UI를
배포합니다. 추가 환경 변수·DB 마이그레이션·메시지 서버는 없습니다.

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
않습니다. 이 독립 실행은 로컬 테스트 계정만 사용합니다. 공통 로그인 개발은
ZOJ API를 함께 실행하고 `EDITOR_AUTH_MODE=zoj`와 인증 연결을 설정합니다.
Ctrl+C로 개발 프로세스를 종료합니다. 운영에서는 이 예제와 메일 모드를
사용할 수 없습니다.

## 별도 서버 배포

구성 및 실제 명령은 [deploy/README.md](deploy/README.md)에 있습니다.
PostgreSQL 17, API, 별도 워커, 프런트엔드를 새 서버에 올립니다.
코드 실행은 작업마다 네트워크가 없는 컨테이너에서 수행합니다.
API에 Docker 소켓을 연결하지 않습니다.

OpenAPI: `/docs`. 브라우저 변경 요청은 `X-Editor-Request: 1` 헤더가 필요합니다.
인증은 `.zoj.kr`의 HttpOnly 공통 세션 쿠키입니다. 운영은 HTTPS·PostgreSQL과
ZOJ 공통 인증 API 연결이 필요합니다. 비밀번호와 인증 메일은 ZOJ가 처리하며,
Editor에는 비밀번호나 SMTP 자격 증명을 저장하지 않습니다.
자세한 동작은 [공통 로그인](docs/shared-login.md)을 확인하세요.

## 검증

```sh
.venv/bin/pytest -q
RUN_SANDBOX_TESTS=1 .venv/bin/pytest -q
EDITOR_DATABASE_URL=sqlite:////tmp/editor-schema.db .venv/bin/alembic upgrade head
EDITOR_DATABASE_URL=sqlite:////tmp/editor-schema.db .venv/bin/alembic check
```

실제 Docker 실행 테스트는 위 두 런타임 이미지를 빌드해야 통과합니다.
메일 발송은 개발 테스트에서 외부로 전송하지 않습니다. 운영 인증 메일은
기존 ZOJ 메일 큐와 발송 작업자가 처리합니다.

## 브랜치와 커밋

기능별 브랜치를 다음 순서로 쌓습니다. 각 브랜치는 앞 브랜치에 의존합니다.
`integration/editor-v1`은 전체 구현을 포함하는 검토용 브랜치입니다.
기능 브랜치는 hosted CI로 검증하며, `main`만 전용 러너에서 배포합니다.

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
온라인 Main 정답 생성과 풀이 검증은 기존 ZOJ 운영 채점 에이전트에서
같은 언어별 제한 정책으로 실행합니다. 오프라인 재현 패키지의 측정값은
실제 채점 노드와 다를 수 있습니다.

- `feature/project-folders`: 개인 폴더·하위 폴더·일괄 이동
- `feature/zoj-publishing`: ZOJ 형식 패키지·권한 확인·문제 전송

- `feature/zoj-library`: ZOJ에서 본인 문제 가져오기
- `feature/self-hosted-deployment`: 전용 러너와 검증 후 순차 배포
- `feature/shared-login`: 공통 계정·비밀번호·로그인 세션

## 공유 작업 폴더

`feature/shared-workspaces`는 개인 폴더와 구분되는 공동 작업 공간을
추가합니다. DB에는 `alembic upgrade head`가 필요합니다. 기존 개인 폴더,
문제 소유권, 문제별 직접 공유는 그대로 유지됩니다.

- `POST /api/shared-folders`: 최상위 공유 폴더 생성
- `GET /api/shared-folders/invitations`: 로그인 이메일로 받은 초대
- `POST /api/shared-folders/invitations/{id}/accept` 또는 `/decline`
- `GET /api/shared-folders/{id}/members`: 구성원 조회
- `POST /api/shared-folders/{id}/invitations`: 이메일·역할로 초대
- `PUT/DELETE /api/shared-folders/{id}/members/{user_id}`: 권한 변경·해제
- `DELETE /api/shared-folders/{id}/invitations/{invitation_id}`: 초대 취소

초대는 7일간 유효합니다. 이메일을 발송하지 않고 로그인 후 받은 초대에
표시합니다. 수락 전에는 접근 권한이 생기지 않습니다. 폴더 관리자는 모든
하위 폴더에 동일한 구성원 권한을 적용합니다.

| 역할 | 가능한 작업 |
| --- | --- |
| 관리자 | 구성원 초대·권한 관리, 자료 편집·실행·커밋, 폴더 정리 |
| 편집자 | 자료 편집·실행·커밋, 문제 추가와 하위 폴더 정리 |
| 검수자 | 커밋된 자료로 검증·임시 검수 코드 실행, 검수 의견과 이슈 |
| 번역자 | 지문 번역·커밋, 렌더링, 이슈 작성; 기준 원문·공통 자료 제외 |
| 읽기 | 문제 자료·실행 결과 열람 |

소유자는 관리자 권한을 포함하며 다른 구성원이 해제할 수 없습니다.
문제 삭제는 원래 문제 소유자만 할 수 있습니다. ZOJ 전송은 쓰기 권한이 있는
협업자도 자신의 ZOJ 대회 문제 등록 권한으로 할 수 있습니다. 공유 폴더를 통해
얻은 편집 권한으로 문제에 영구적인 직접 공유를 추가할 수 없습니다.

패키지 화면의 `패키지 생성 전 풀이·구성요소 검증 실행`을 해제하면 커밋된
저장 자료를 그대로 내보냅니다. 생성기·Validator·Main·풀이 및 자체 테스트는
실행하지 않으며, 에이전트가 오프라인이어도 ZIP을 생성합니다. ZOJ·Linux
형식은 모든 대상 테스트의 입력·정답이 저장되어 있어야 합니다. 누락된 자료는
자동 생성하지 않고 테스트 번호를 안내합니다. 미완성 자료는 Editor 형식으로
내보내면 생성 스크립트와 원본 파일을 보관하고 다시 가져올 수 있습니다.

미검증 패키지에도 `verified: false`를 기록합니다. 쓰기 권한이 있는 협업자는
ZOJ 형식의 미검증 패키지도 전송할 수 있지만, 파일 체크섬·최신 커밋 일치와
대상 대회의 현재 문제 등록 권한은 매번 확인합니다. 읽기·검수·번역 권한만
있는 사용자는 전송할 수 없습니다. ZOJ에서 Editor 문제를 가져올 때도 쓰기
권한이 있는 문제만 조회·사용할 수 있으며, 자동 패키지 준비는 검증을 포함합니다.

문제 이동은 기존 `/api/folders/move-problems`를 사용합니다. 공유 범위를
바꾸려면 문제 소유자의 `share_confirmed: true` 확인이 필요합니다.
같은 공유 폴더 안의 정리는 편집자도 할 수 있습니다. 폴더 자체는 개인
영역과 공유 영역 사이 또는 서로 다른 공유 폴더 사이로 이동할 수 없습니다.

탈퇴·내보내기 후에는 폴더를 통한 접근과 실행 권한이 사라집니다. 문제
소유권이나 문제별 직접 공유가 남아 있다면 해당 권한은 계속 적용됩니다.
워커는 진행 중에도 권한을 확인하여 실행 권한이 사라진 작업을 취소합니다.
최상위 공유 폴더는 하위 폴더와 문제(휴지통 포함)가 없을 때 소유자만
삭제할 수 있습니다. 하위 폴더 삭제는 기존처럼 내용을 상위로 옮깁니다.

## 테스트 병렬 처리

독립된 등록 테스트, 풀이별 검증과 Validator·Checker 자체 테스트를 병렬
처리합니다. 스트레스 테스트는 여러 시드의 생성·입력 검증·기준 풀이·비교
풀이 실행을 겹쳐 처리합니다. 생성 스크립트와 선택 테스트 범위는 그대로
유지하며, 결과·오류·첫 반례는 테스트 번호와 시드 순서로 정리합니다.

- `EDITOR_TEST_PARALLELISM=4`: 작업당 테스트·시드 처리 수 (1~16)
- `EDITOR_EXECUTION_CONCURRENCY=4`: 워커 프로세스 전체의 실제 격리 실행과
  ZOJ 최종 검증 연결 수 (1~16). 여러 작업이 같은 실행 한도를 공유합니다.
- `EDITOR_WORKER_CONCURRENCY`: 서로 다른 Editor 작업 수. 기존 설정 유지
- `EDITOR_JUDGE_CONCURRENCY`: 작업당 ZOJ 최종 검증 연결 수. 기존 설정 유지

풀이 검증 화면에서는 체크박스로 필요한 테스트 세트를 선택합니다. API의
`testsets: ["tests", "tle"]`는 지정한 세트만 준비·검증하며, 빈 배열이나 없는
세트는 거부합니다. 한 세트일 때만 `test_indices`로 번호 범위를 지정할 수 있습니다.
기존 `testset` 단일 선택과 `all_testsets: true` 전체 선택도 지원하며, 명시적인
`testsets`와 전체 선택 플래그를 함께 보낼 수 없습니다. 번호·그룹·기대 판정은
세트별로 유지합니다. 요청 범위의 `testsets`·`testset_counts`와
결과의 세트별 진행률을 보관합니다. 빈 세트·NR 범위는 풀이 채점 건수에 포함하지 않습니다.
전체 진행률은 입력 확인·Validator 검사·정답 준비·Checker 확인·저장 완료와
실제 풀이별 테스트 판정을 합산합니다. 표시용 예제 검사가 있으면 함께 집계하며,
준비 결과 재사용도 완료로 반영합니다. 준비·채점 내역은 별도로 보관하고, 실패하거나
중단된 단계와 실행하지 못한 테스트는 완료로 처리하지 않습니다.
Editor 격리 실행과 ZOJ 실제 채점기에서 같은 범위를 사용하며, 세트별 기대 판정이
모두 일치하고 검증 범위가 완결되어야 최종 검증 성공입니다.

풀이 검증은 성공한 테스트 준비 단계를 문제별로 재사용합니다. 생성기 코드·
인자와 첨부 리소스가 같으면 생성 입력을, 입력·Validator·세트·그룹이 같으면
입력 검사를, 입력·Main·실행 제한이 같으면 정답 생성을 재사용합니다.
Checker와 지문 표시용 예제 검사도 각각의 실제 의존 자료를 비교합니다.
파일의 내용, 라이브러리, testlib 및 실행 환경이 바뀌면 해당 단계는 다시
실행합니다. 지문 문구나 비교 풀이만 수정하면 준비 결과는 유지됩니다.

이 기능이 적용되기 전의 결과나 가져온 입출력에는 검사 이력이 없으므로
첫 검증은 새로 준비합니다. 실패·취소된 단계는 저장하지 않으며 파일 유실과
내용 불일치는 재사용하지 않습니다. 준비 기록은 30일 뒤 자동 정리합니다.
실제 풀이의 판정·CPU 시간·메모리는 준비 캐시에 저장하지 않습니다.

실행 화면의 **테스트 준비 옵션**에서 재사용을 해제할 수 있습니다.
`POST /api/problems/{id}/jobs`의 풀이 검증 옵션은 `force_prepare: true`입니다.
**입력·정답 생성** 및 부분 재생성, **패키지 생성**은 준비 캐시를 읽지 않고
요청한 단계를 다시 실행합니다. 커밋된 리비전은 저장된 입력·정답을 유지하고
검사만 다시 실행합니다. 실행 기록에는 단계별 재사용·실행 횟수가 저장됩니다.

설정은 `/api/execution-environment`의 `limits`에서 조회합니다. 병렬 작업
수는 두 병렬 설정 중 작은 값입니다. 둘 다 1로 두면 순차 실행합니다.
워커 프로세스를 여러 개 띄우면 전체 실행 상한은 프로세스 수만큼 늘어나므로
총 채점기 슬롯에 맞춰 각각 설정하세요. 채점 노드의 슬롯 수, CPU 시간·
메모리 제한과 격리 정책은 변경하지 않습니다. 이 설정만으로 물리 코어·
메모리 대역폭 간섭이 사라지지는 않습니다.

종료 조건을 만족하면 아직 필요하지 않은 나머지 실행은 취소하고 정리한 뒤
작업을 종료합니다. 병렬 스트레스는 최대 작업당 병렬 수만큼 시드를 미리
실행하므로 단일 실행보다 시도를 조금 더 시작할 수 있습니다. 저장된
`iterations_scheduled`는 예약한 시드 수, `iterations_completed`는 시드
순서로 결과 반영을 마친 수입니다. 반례의 시드·명령은 실제 입력 기준으로
보존합니다. 이미 반례를 찾은 풀이의 추가 검사는 실행 전 또는 결과 반영
시 제외하며, 같은 입력은 진행 중 실행 결과도 재사용합니다. 반례 축소는
각 단계가 이전 결과에 의존하므로 순차 처리합니다.

로컬 격리 환경에서 순차·병렬 실행을 비교하려면 다음 명령을 사용합니다.
기존 계정·문제 DB를 사용하지 않고 임시 테스트 DB를 따로 만듭니다.
컴파일 캐시를 같은 상태로 준비한 뒤 6개 테스트의 C++·Python 풀이 검증과
6개 시드의 스트레스 테스트를 각각 1개·4개 병렬로 비교합니다. 절대 시간은
실행하는 컴퓨터와 Docker 자원 설정에 따라 달라집니다.

```sh
python tools/benchmark_parallel.py /tmp/editor-parallel-benchmark.json
```

### Feature permissions

Problem sharing and shared-folder membership use the same capability checklist.
Reading problem data, files, revisions, and saved results is the baseline. Editing
settings, statements, files/components, solutions/expected verdicts, tests, and
public materials are separate grants. Translation can be granted without editing
the source language. Each granted edit scope permits saving and committing only
that section; revoked scopes also block committing older drafts.

Verification/stress execution, statement rendering, test preparation, package
creation, ZOJ transfer, issue/review comments, and membership administration are
independent. Test preparation does not imply permission to apply its outputs;
that requires test editing. ZOJ transfer still requires the acting user's ZOJ
contest permission. Creating a package for transfer requires package permission.
Team folders additionally offer folder organization and new-problem creation.

Existing roles remain effective when `permissions` is SQL NULL. An explicit
empty checklist is read-only. Folder and direct problem grants combine; removing
one grant does not remove another. Owners retain all permissions. A manager may
only delegate permissions they currently hold; invitations recheck the inviter
at acceptance, and queued/running jobs recheck capabilities. Folder membership
cannot be turned into permanent direct problem access by a shared editor or
manager. Problem deletion and moving between sharing scopes remain owner-only.

### 협업·팀 폴더 안내 메일

문제의 직접 협업 권한을 처음 부여하거나 권한을 추가하면 해당 계정의
이메일로 안내합니다. 팀 폴더는 처음 초대할 때와 초대 대기자·구성원의
권한을 추가할 때 안내합니다. 읽기 전용 초대도 포함하며, 동일 설정의
재저장과 권한 축소·해제에는 새 메일을 만들지 않습니다. 기존 권한을
소급해 발송하지 않습니다.

권한 변경과 `access_mail_outbox` 예약은 같은 DB 트랜잭션입니다. 워커의
별도 메일 스레드가 기존 ZOJ 서버의 메일 큐로 전달하므로 메일 장애가
권한 저장이나 채점 슬롯을 막지 않습니다. 네트워크 오류는 15초부터
최대 1시간 간격으로 재시도하며, 최대 20회 또는 7일 후 중단합니다.
전송 전 해제·만료·변경된 권한의 안내는 취소하고, 기록은 30일 보관합니다.

운영 환경은 기존 `EDITOR_AUTH_API_URL`, `EDITOR_AUTH_CA_FILE`,
`EDITOR_AUTH_BRIDGE_SECRET`을 사용합니다. 별도 SMTP 설정은 필요 없습니다.
`POST /api/editor/mail/access`는 전용 HMAC 목적
`zoj-editor-access-mail-v1`로 고정된 권한 안내만 요청하며 사용자 세션을
보내지 않습니다. ZOJ 메일 API를 먼저 배포한 뒤 Editor를 배포하세요.
`delivery_id`는 ZOJ 메일 큐 접수 영수증이며 실제 SMTP 발송 완료와 다릅니다.
메일 발송 상태는 ZOJ 서비스 관리의 이메일 로그에서 확인합니다.
로컬 로그인 모드에서는 메일을 보내지 않고 이벤트 ID만 로그에 남깁니다.

### Large testcase files

Individual uploads and package entries support up to 128 MiB. Set
`EDITOR_MAX_UPLOAD_BYTES` to a lower positive value to reduce this limit.
Package ZIP uploads remain bounded at 2 GiB by default. Generators and
solution/reference output support 128 MiB; compiler artifacts and result
previews retain their separate limits. Large final-verification suites are
split into bounded 512 MiB requests without dropping cases. This requires
ZOJ API and judge agent 0.2.27 or later before updating Editor workers.
Participant submission output limits are unchanged.
