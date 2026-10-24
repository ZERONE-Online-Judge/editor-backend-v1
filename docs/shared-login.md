# ZOJ 공통 계정과 로그인

`zoj.kr`와 `editor.zoj.kr`는 같은 이메일을 같은 중앙 계정 UUID로 취급합니다.
비밀번호와 로그인 연결은 ZOJ DB가 관리합니다. Editor는 문제·폴더·프로젝트
권한을 별도 DB에 저장하고, 기존 이메일 프로필을 중앙 UUID에 연결합니다.
기존 로컬 사용자 ID를 유지하므로 문제 소유권과 협업·폴더는 보존됩니다.

두 서비스는 Secure·HttpOnly·SameSite=Lax 공통 쿠키 `zoj_sso`를
`.zoj.kr` 도메인에 사용합니다. 같은 브라우저에서 한쪽에 로그인하면 다른
서비스에 접속할 때 인증번호나 비밀번호를 다시 입력하지 않습니다.
세션 유효기간은 중앙 ZOJ refresh 세션의 만료 시점입니다. Editor가 별도
기간을 추가하거나 로그인 만료를 독립적으로 연장하지 않습니다.
다른 브라우저·기기는 쿠키를 공유하지 않으며 기존 세션 교체 확인을 사용합니다.

Editor는 `zoj-shared-auth-v1` 용도의 서버 서명으로 ZOJ에 인증을 요청합니다.
브라우저에는 Editor 프로필만 반환하며 ZOJ 운영자 access token은 전달하지
않습니다. 프로젝트 권한은 Editor DB, 대회 문제 등록 권한은 ZOJ DB가 검사합니다.
비밀번호 설정·변경·찾기 인증번호는 기존 ZOJ 메일 큐에서 발송합니다.

Editor 로그인은 ZOJ 공통 로그인 화면으로 이동합니다. 이메일 입력 후
비밀번호가 있으면 비밀번호를 먼저 표시하고, 새 브라우저·IP에서는
이메일 또는 패스키로 추가 인증합니다. 패스키 로그인을 직접 선택한 경우
같은 인증을 반복하지 않습니다. 이메일 인증 후에는 기존 ZOJ의 패스키
등록 제안을 사용하며 등록 성공 또는 나중에 선택 시 원래 Editor 작업
주소로 돌아갑니다.

Editor 내 정보·설정의 패스키 관리에서 공통 패스키를 추가·삭제합니다.
비밀번호 관리도 ZOJ 공통 화면으로 연결해 이메일 또는 패스키로 인증합니다.
모든 WebAuthn 절차는 `zoj.kr`에서 수행하며 Editor 전용 자격 증명을
별도로 생성하거나 다른 origin을 허용하지 않습니다. 패스키·이메일·비밀번호
정책을 우회할 수 없도록 운영 모드의 기존 Editor 로그인 POST API는
`central_login_required`로 거절합니다. 로컬 개발용 OTP 로그인은 유지합니다.

왕복 주소에는 토큰·이메일·비밀번호를 넣지 않습니다. 복귀 origin은
`https://editor.zoj.kr`로 고정하고 프로젝트·도움말 경로만 허용합니다.
인증 후 공유 HttpOnly 쿠키로 Editor 프로필과 기존 프로젝트 권한을 복원합니다.
비밀번호 변경·재설정은 기존 계정의 전체 로그인 연결을 해제합니다.
한 서비스에서 로그아웃하면 같은 중앙 세션이 두 서비스에서 해제됩니다.
운영자의 강제 세션 해제도 Editor에 적용됩니다. 열린 페이지에는 SSE로
해제를 알리고, 다음 API 요청에서도 서버가 활성 세션을 다시 검사합니다.
중앙 인증 서버가 일시적으로 연결되지 않으면 503을 반환하며 기존 화면에서
이를 사용자 로그아웃으로 오인하지 않습니다.

비밀번호는 PBKDF2-HMAC-SHA256 600,000회와 사용자별 무작위 salt로 저장합니다.
이메일 증명은 5분·1회·최대 5회 시도로 제한하며 로그인·발송에도 영속적
빈도 제한을 적용합니다. 정책 근거는
[OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[Forgot Password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html)입니다.

## 서버 설정

ZOJ의 private backend 환경 파일에 `EDITOR_AUTH_BRIDGE_SECRET`을 추가하고,
Editor의 `/srv/zoj-editor/config/editor.env`에 같은 인증 연결 키를 설정합니다.
문제 전송 키와 구분하며 키는 Git·브라우저·로그에 노출하지 않습니다.
Editor에는 `EDITOR_AUTH_API_URL`과 필요한 경우 `EDITOR_AUTH_CA_FILE`도 설정합니다.
양쪽 모두 HTTPS와 `.zoj.kr` 쿠키 도메인을 사용합니다. 로컬 검증에서는
host-only 쿠키를 사용하며 키와 테스트 DB를 별도로 둡니다.

ZOJ 중앙 스키마·인증 API를 먼저 배포한 뒤 Editor를 배포합니다.
`python -m app.auth_preflight`는 로그인 없이 서명·TLS 연결만 검증합니다.
기대 응답은 `authentication_required`이며 잘못된 키·CA·없는 API는 배포를
중단합니다. 구 버전의 Editor-only 세션은 운영 모드에서 받아들이지 않으며
기존 문제·폴더는 중앙 계정으로 처음 로그인할 때 연결됩니다.

## 새 비밀번호 조건

영문·숫자·특수문자만 허용하며 8~128자, 영문·숫자·특수문자 각각 1개
이상을 포함합니다. 한글·공백·전각 문자는 허용하지 않고 대소문자를
구분합니다. 이 조건은 등록·변경·재설정에 적용합니다. 이전 정책으로
등록된 비밀번호는 계속 로그인에 사용할 수 있습니다.

로그인과 설정 입력에는 보기/숨기기 버튼이 있습니다. 설정 화면은 조건별
충족 상태와 확인 입력의 일치 여부를 표시하고, 조건·일치·인증번호를
충족해야 저장 버튼을 활성화합니다. ZOJ에서 최종 정책을 검증하며
Editor도 전달 전에 같은 조건을 확인합니다.
