# Polygon 공개 명세 대응표

확인일: 2026-10-12. 비공개 Polygon 코드의 복제물이 아니라 공개된 작업
흐름과 데이터 구조를 참고한 독립 구현입니다. 브랜드·색상·화면은 ZOJ
스타일입니다.

참고한 공식 자료:

- https://polygon.codeforces.com/
- https://codeforces.github.io/polygon-misc/API
- https://codeforces.com/blog/entry/7979
- https://github.com/MikeMirzayanov/testlib

| Polygon 기능 | Editor 구현 |
| --- | --- |
| 문제 목록·생성·메모·삭제 | 소유/공유 문제, 검색, 메모, 복구 가능한 휴지통 |
| 개인 working copy | 사용자별 사본, 버전 충돌 검사, 업데이트·되돌리기 |
| commit/revision | 변경 설명, 공유 스냅샷, 3-way merge, 충돌 선택 UI |
| statement/resource | 다국어 지문·해설, LaTeX 수식 미리보기, 지문 리소스 |
| render statements | 별도 XeLaTeX 컨테이너의 한글 PDF, 오프라인 HTML |
| sources/solutions | C17·C++17/20·Python3·Java17, grader·Java 진입 클래스 |
| checker/validator/interactor | 표준 checker 6종, 사용자 소스, 추가 validator |
| component self tests | validator VALID/INVALID, checker OK/WA/PE/CRASHED |
| tests/scripts/testsets | 수동 입력·정답 업로드, 생성 명령, 다중 테스트 세트 |
| groups/points/dependencies | COMPLETE_GROUP/EACH_TEST, 점수, 의존 그룹 판정 |
| solution tags | MA/OK/RJ/TL/TO/TM/WA/PE/ML/NR/RE와 기대 판정 비교 |
| invocation/stress | 실제 격리 실행, 자원 계측, 입력 검증 후 반례 탐색 |
| access/issues | 직접 OWNER/WRITE/READ, 검수/번역 역할, 이슈/댓글/상태 |
| publishable materials | 선택한 소스·풀이·테스트의 공개 자료 ZIP |
| packages | 커밋한 리비전 전체 검증, problem.xml, 원본·테스트 ZIP |
| contests | 개인 대회 문제 모음, 제목·문제 순서 편집 |

## 현재 동작의 차이

- 로그인은 ZOJ 공통 이메일 인증·비밀번호 계정입니다. Codeforces 계정과 연동하지 않습니다.
- REST API는 Editor용이며 Polygon의 `apiSig` API와 wire-compatible하지
  않습니다. API 토큰·사용자 그룹·문제 PIN·공개 익명 접근은 구현하지 않았습니다.
- 패키지는 Linux 환경에서 확인한 **소스와 테스트 패키지**입니다.
  Polygon의 Windows/Linux 바이너리 패키지와 동일하다고 보장하지 않습니다.
- Editor manifest가 있는 ZIP은 원본 자료를 checksum 검증하여 왕복합니다.
  Polygon `problem.xml` ZIP은 명시한 소스·테스트·지문을 가져옵니다.
  바이너리만 있는 패키지는 원본 풀이 검증을 할 수 없습니다.
- 일부 사용자 정의 LaTeX 매크로는 브라우저 미리보기에서 다르게 보일 수
  있습니다. PDF는 XeLaTeX 실행 결과이며 실패 원문을 표시합니다.
- 모든 테스트의 실제 판정은 운영자에게 보입니다. 그룹 feedback 정책은
  문제 메타데이터/내보내기에 저장하며 참가자용 채점 UI는 이 서비스의
  기능이 아닙니다. 점수는 전체 세트를 실행한 경우에만 합계를 표시합니다.
- 코드 실행은 이 서비스 전용 Docker 환경입니다. 기존 ZOJ의 공식 판정,
  언어 보정, CPU 성능과 동일하지 않으므로 최종 대회 환경 검증이 필요합니다.
- 자동 AI 문법 수정·제출 서비스·대회 스코어보드는 추가하지 않았습니다.

## 원본 라이선스

`vendor/testlib`의 `testlib.h`와 표준 checker는
`1e4e8a24c79c6bad3becbdb5a332ffc352b7d5dd`의 원본입니다.
[upstream 정보](../vendor/testlib/UPSTREAM.md)와 MIT LICENSE를 포함합니다.
수정 가능한 문제 전용 checker/validator와 구분하며 공용 파일 이름으로
업로드하는 것을 금지합니다. KaTeX는 npm 0.19.0의 배포물과 라이선스를
문제 패키지에 포함하여 외부 CDN 없이 수식을 표시합니다.
