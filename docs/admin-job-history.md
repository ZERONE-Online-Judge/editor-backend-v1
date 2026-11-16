# ZOJ 서비스 관리자용 Editor 작업 내역

ZOJ 채점 관리의 **작업 내역(Editor)** 탭은 Editor `jobs` 테이블의
영속 작업을 조회합니다. 전체 검증·단일 검증·스트레스·입력 준비·도구 검사·
패키지·지문 렌더링을 포함합니다. ZOJ의 48시간 보관 하위 실행 요청을
작업 원본으로 사용하지 않습니다. 기존 작업과 결과는 갱신하지 않습니다.

## 내부 조회 계약

- `GET /api/internal/admin/jobs`: `page`(기본 1), `page_size`(기본 20,
  최대 100), `q`(최대 200자), `kind`, `category`, `status`.
- `GET /api/internal/admin/jobs/{job_id}`: 32자리 소문자 hex 작업 ID.
- `kind`는 `all`, `prepare`, `verify`, `validator-tests`, `checker-tests`,
  `stress`, `package`, `render`입니다. `category`는 기존 Editor 실행
  범주이며 `verify-single`, `prepare-inputs` 등 세부 구분을 지원합니다.
  `status`는 `all`, `queued`, `running`, `completed`, `failed`, `cancelled`입니다.
- 검색은 실행 이름·ID, 문제 이름·slug, 요청자 이름·handle·이메일을
  대상으로 합니다. `%`, `_`는 와일드카드로 취급하지 않습니다.
- 필터 적용 후 페이지와 전체 상태별 요약을 계산합니다. 최신 생성 시각,
  작업 ID 내림차순으로 정렬하고 초과 페이지는 마지막 페이지로 보정합니다.
  빈 목록은 `page:1`, `pages:1`입니다. 삭제된 문제의 이력도 보존합니다.
- 응답은 Editor 원형 JSON입니다. 목록은 `items`, `total`, `page`,
  `page_size`, `pages`, `summary`를 반환합니다. 각 항목은 문제, 요청자,
  실행 이름·종류·범주·상태·평가, 리비전·작업 사본 버전, 생성·시작·종료
  시각과 `result:{summary,progress}`를 포함합니다. 시각은 Unix 초입니다.
- 목록은 SQL에서 요약·진행률·작은 상태 필드만 응답으로 선택합니다.
  상태 집계는 작업 ID·상태만 조회하고, 그 합으로 전체 건수를 계산합니다.
  정렬·필터·페이지 제한을 먼저 적용한 뒤 해당 페이지의 JSON만 추출합니다.
  PostgreSQL은 `MATERIALIZED` CTE로 이 경계를 유지하고, `json_to_record`로
  옵션·결과를 각각 한 번 파싱합니다. 각 JSON 경로마다 큰 보고서를 다시
  파싱하거나 페이지 밖 작업의 JSON을 파싱하지 않습니다. SQLite는 기존
  JSON leaf 추출을 유지합니다. 잘못된 구조·JSON null·SQL NULL은 빈 객체로
  취급하고 boolean/null 타입도 기존 목록과 동일하게 유지합니다.
  전체 `Job.result`·`Job.options`는 애플리케이션에 적재하지 않으며 소스
  스냅샷 `Job.document`도 제외합니다. 전체 결과는 상세 한 건에서만 읽습니다.
  따라서 목록의 완료된 검증은 `detail_required` 평가로
  상세 확인을 안내합니다. 스트레스 반례 수·진행 상태 등 경량 필드로 확정할
  수 있는 평가는 그대로 표시합니다. `verified:true`만으로 성공을 단정하지 않습니다.

## 서명과 권한

브라우저는 이 주소를 직접 호출하지 않습니다. ZOJ의 서비스 마스터 전용
프록시가 요청마다 현재 권한을 확인한 뒤 기존 Editor bridge secret으로
아래 문자열에 HMAC-SHA256을 적용합니다. 일반 Editor 로그인이나
클라이언트의 서비스 마스터 헤더는 내부 조회 권한을 부여하지 않습니다.

```text
zoj-editor-admin-history-v1
GET
/api/internal/admin/jobs?<정확히 전송하는 query string>
<Unix timestamp>
<새로운 32 hex nonce>
<master email의 URL-safe base64, padding 제외>
<빈 본문의 SHA-256>
```

기존 `X-Editor-Identity`, `X-Editor-Timestamp`, `X-Editor-Nonce`,
`X-Editor-Content-Sha256`, `X-Editor-Signature` 헤더를 사용합니다.
경로와 query는 percent encoding·파라미터 순서까지 서명에 결합합니다.
다른 bridge 목적의 서명은 호환되지 않습니다. 타임스탬프 오차는 90초이며,
기존 `zoj_library_nonces` 테이블의 unique nonce를 원자적으로 소비하여
동시 API 워커에서도 재전송을 막습니다. 작업 데이터는 읽기 전용이고,
이 재전송 방지용 nonce만 추가·만료 정리합니다. 새 마이그레이션은 없습니다.

기존 `EDITOR_ZOJ_BRIDGE_SECRET`과 TLS/CA 연결 설정을 재사용합니다.
키가 없으면 503, 잘못된 서명·시각·nonce는 401, 없는 작업은 404,
잘못된 필터는 422입니다. 키나 임시 인증 정보는 브라우저로 보내지 않습니다.

## 상세 결과의 범위

상세 응답은 동일한 메타데이터와 allowlist 옵션·결과만 제공합니다.
진행 단계·완료 수·전체 수, 풀이명·기대 태그·관측 판정·예상 일치 여부,
테스트별 판정과 시간·메모리, 스트레스 반복·반례 개수 등을 확인할 수 있습니다.
`solutions`와 `stress_results`의 `observed_verdicts`, `passed_tests`,
`failed_tests`, `unconfirmed_tests`, 최대 시간·메모리는 상세 행 제한 이전의
관측 결과에서 계산합니다. 컴파일 오류의 0번 행은 실행된 테스트로 세지 않고,
채점기 오류는 확인되지 않은 테스트로 남깁니다.

지문·소스 스냅샷, 임시 코드, 테스트 입출력, blob 주소·해시, 패키지 경로,
lease·token, 프로그램의 원시 stderr·오류 전문은 반환하지 않습니다.
`has_error`와 `error_code`, 평가 결과로 오류 여부를 확인합니다. 원문 진단은
기존 Editor 권한 아래 실행 결과에서 확인합니다. 결과의 자유 문자열에 포함된
절대 경로·URL·명시적 인증 정보도 가립니다.

`result_redacted:true`는 위 범위가 제외된 결과임을 뜻합니다.
상세 배열은 각각 최대 500행, 동적 맵은 50개, 전체 projection은 16,000항목과
260,000문자로 제한합니다. 제한에 걸리면 `result_truncated:true`이며,
전체 실행 요약·집계는 그대로 유지합니다. 완료 상태만으로 검증 성공을
추정하지 않으며, 오래되거나 불완전한 기록은 평가 미확인 상태로 표시합니다.
