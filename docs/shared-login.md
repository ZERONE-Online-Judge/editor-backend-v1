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

ZOJ 내 정보 또는 Editor 내 정보·설정에서 이메일 인증 후 공통 비밀번호를
등록·변경할 수 있습니다. 로그인 화면에서는 이메일만 입력하고 다음을 누릅니다.
비밀번호가 등록된 이메일은 비밀번호 화면으로 이동하며, 아래의 이메일로
대신 로그인을 누르면 인증번호를 요청합니다. 비밀번호가 없는 이메일은
바로 인증번호 로그인으로 이어집니다. 로그인 방식 조회는 빈도 제한을
적용하며 비밀번호 등록 여부 외에 프로필·권한·계정 존재 정보는 반환하지 않습니다.
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
