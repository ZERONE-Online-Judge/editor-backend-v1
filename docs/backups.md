# Editor 자동 백업과 복구

이 구성은 **에디터 전용**입니다. ZOJ 대회 서버의 DB나 채점 노드는 변경하지
않습니다. 백업 설치는 API·워커·진행 중 작업을 중단하지 않습니다.

## 보호하는 자료와 복구 목표

- 문제 기본 정보·지문·설정, 공유 리비전과 커밋 이력
- 각 제작자의 **저장된 작업 사본**과 미커밋 변경
- 소스·생성기·Validator·Checker·입력·정답·이미지·첨부 파일
- 실행 기록·생성 결과·PDF·패키지, 협업자·폴더·권한
- 운영 설정과 인증서, 배포 이미지 식별자, 백업 프로그램과 설정

브라우저에서 아직 서버에 저장하지 않은 입력은 서버 백업에 포함되지 않습니다.
제작 중 변경도 `작업 사본 저장`을 눌러야 보호됩니다.

백업은 15분마다 실행합니다. 정상 작동 중에는 최근 성공한 백업 시점까지 복구할
수 있으며, 일반적인 데이터 손실 범위는 최대 약 15분과 백업 실행 시간입니다.
백업 실패·서버 정지·스케줄 지연 때는 이보다 오래될 수 있습니다. 45분 동안 새
백업이 없으면 상태가 `stale`로 바뀝니다.

이 디스크는 같은 VM에 붙은 로컬 백업입니다. 서버 전체 손실·백업 디스크 파손·
관리자 계정 침해까지 대비하려면 **다른 서버/장소의 백업 한 벌**도 필요합니다.
루트 권한이 있는 프로세스는 로컬 백업도 지울 수 있으므로 불변 저장소는 아닙니다.

## 저장 방식과 일관성

300 GiB scsi1 디스크를 `/srv/zoj-editor-backup`에 UUID로 마운트하고,
그 아래 `restic` 저장소에 암호화·중복 제거한 시점별 백업을 보관합니다.
OS 디스크나 기존 문제 저장 위치는 옮기지 않습니다.

DB는 PostgreSQL의 내보낸 repeatable-read 스냅샷을 사용합니다. 같은 스냅샷에서
`pg_dump -Fc`와 문제·작업 사본·리비전·실행 기록의 수, 파일 참조 목록을 함께
읽습니다. DB에 참조된 파일의 존재와 크기를 확인하고 원본 파일 저장소를 함께
백업합니다. 파일은 내용 해시 이름으로 저장되며, DB 커밋보다 먼저 완성됩니다.
정상 백업 중에는 원본 파일을 수동 삭제하거나 별도 GC를 실행하지 마세요.

라이브 PostgreSQL 데이터 디렉터리를 단순 복사하지 않습니다. 임시 가져오기
파일과 재생성 가능한 작업 디렉터리도 제외합니다. 배포 잠금으로 마이그레이션과
백업을 직렬화하며, 사용자 작업의 일반적인 DB 쓰기는 계속 허용합니다.

완전히 성공한 백업만 `zoj-editor-complete-v1` 태그를 받습니다. 중간에 실패한
스냅샷에는 이 태그가 없어 자동 복구 검사와 정상 시점 목록에서 제외됩니다.

## 보관 정책과 검사 일정

| 항목 | 정책 |
| --- | --- |
| 정기 백업 | 15분마다, 부팅 3분 후 추가 실행, 놓친 일정 보충 |
| 최근 시점 | 최근 24시간의 모든 성공 백업 |
| 장기 시점 | 시간별 72개·일별 30개·주별 12개·월별 12개 |
| 수동 고정 백업 | `--pin`으로 만들면 자동 정리에서 제외 |
| 실제 복구 검사 | 매일 한국 시간 03:20 |
| 무결성 검사·보관 정리 | 매일 한국 시간 04:20 |

일일 복구 검사는 최신 백업을 새 임시 디렉터리에 복원하고 파일 내용 전체를
확인합니다. 네트워크·공개 포트·운영 DB 연결이 없는 임시 PostgreSQL에 DB를
복원한 뒤 작업 사본·리비전·문제·파일 참조 수를 같은 스냅샷의 목록과 비교하고,
모든 참조 파일의 SHA-256을 검사합니다. 성공·실패 모두 임시 컨테이너와 복구
폴더를 제거하며 운영 DB와 문제 파일은 덮어쓰지 않습니다.

일일 정리는 저장소 구조와 날짜별 1/7의 저장 블록을 확인한 뒤 보관 정책을
적용하고 실제 공간을 회수합니다. 검사에 실패하면 정리하지 않습니다. 고정된
시점과 실패하지 않은 마지막 복구 시점이 보존됩니다. 보관 개수는 용량 보장을
의미하지 않습니다. 디스크 85% 사용 시 `low_space`, 여유가 5% 또는 2 GiB보다
작으면 새 백업을 거절합니다. 용량 부족 때문에 마지막 백업을 임의 삭제하지
않으므로 상태를 확인하고 저장소를 확장해야 합니다.

## 첫 설치

서버 콘솔에서 검토한 파일이 있는 디렉터리의 설치 프로그램을 실행합니다.
관리자 권한은 디스크 초기화·마운트·패키지·systemd 등록에 필요합니다.

```sh
sudo bash deploy/backup/install.sh \
  /dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1
```

설치기는 호스트 이름 `zoj-editor`, 새 디스크 300 GiB, 파티션·마운트·파일시스템·
기존 서명이 없는 상태를 확인한 경우에만 scsi1을 초기화합니다. OS 디스크나
자료가 있는 디스크는 거절합니다. 설치를 다시 실행할 때는 포맷하거나 암호화
키를 바꾸지 않고 기존 UUID를 확인합니다. 디스크 초기화 직후 실패했다면
기존 파티션을 지워서 재시도하지 말고 마운트/설정 상태부터 확인하세요.

초기 백업과 실제 DB 복구 검사가 모두 성공해야 설치 완료로 표시됩니다.
설치 후 `/home/zoj-editor/zoj-editor-backup-recovery.key`를 서버 밖의 안전한
저장소나 비밀번호 관리자에 보관하세요. 이 파일을 잃고 OS 디스크까지 손실되면
암호화 백업을 읽을 수 없습니다. 키 내용은 로그·Git·대화·공유 폴더에 넣지
않습니다. 운영 키 원본은 `/etc/zoj-editor-backup/repository-password`입니다.

## 상태 확인과 수동 실행

```sh
zoj-editor-backup status
systemctl list-timers 'zoj-editor-backup*'
sudo journalctl -u zoj-editor-backup.service -u zoj-editor-backup-drill.service
sudo zoj-editor-backup backup --pin
sudo zoj-editor-backup drill
sudo zoj-editor-backup maintain
```

`status`는 일반 관리자 SSH 계정에서도 볼 수 있습니다. 정상은 `ok`, 백업
지연은 `stale`, 복구 검사가 없거나 48시간 이상 오래됐으면 `unverified`, 실행
실패는 `failed`입니다. 새 백업이 성공해도 이전 복구 검사 실패는 지우지 않으며,
해당 검사를 다시 성공해야 해제됩니다. 이 구성은 상태·systemd 실패 기록을
제공하며 외부 메일/메신저 알림은 별도로 연결해야 합니다.

## 이전 시점에서 복원하기

정상 운영 중에는 먼저 **별도 빈 복구 디렉터리**로 꺼냅니다. 최신 운영 DB를
자동으로 덮어쓰는 명령은 제공하지 않습니다.

```sh
sudo env RESTIC_REPOSITORY=/srv/zoj-editor-backup/restic \
  RESTIC_PASSWORD_FILE=/etc/zoj-editor-backup/repository-password \
  restic snapshots --tag zoj-editor-complete-v1

sudo env RESTIC_REPOSITORY=/srv/zoj-editor-backup/restic \
  RESTIC_PASSWORD_FILE=/etc/zoj-editor-backup/repository-password \
  restic restore <선택한-스냅샷-ID> --target /srv/zoj-editor-recovery --verify
```

복구 디렉터리 아래 원래 절대 경로 구조가 유지됩니다.

- `srv/zoj-editor/data/`: 문제 파일·결과·패키지
- `srv/zoj-editor/config/`, `srv/zoj-editor/certs/`: 운영 설정·인증서
- `var/lib/zoj-editor-backup/staging/editor.dump`: PostgreSQL 백업
- 같은 폴더의 `database-manifest.json`: 해당 시점의 수량과 파일 참조
- 같은 폴더의 `backup-manifest.json`: 파일 체크섬·당시 이미지 ID·경로

운영 복구가 필요한 장애 때는 복구용 DB에 먼저 `pg_restore --no-owner
--no-privileges --exit-on-error`로 확인하고, 그 뒤에 API·워커를 멈춘 상태에서
DB와 파일 저장소를 **같은 백업 시점의 한 쌍**으로 교체합니다. 현재 자료도
별도 보관하며, 정상 확인 전에는 이전 자료나 백업을 삭제하지 않습니다.
다른 문제의 최신 변경을 보존해야 한다면 전체 롤백 대신 복구용 DB의 문제를
패키지로 추출하여 새 문제로 가져오는 절차를 먼저 검토하세요.

새 서버에서는 PostgreSQL 주 버전을 먼저 맞추고 복구 키를 권한 600인 파일로
전달합니다. 기존 암호화 저장소에 `restic init`을 다시 실행하거나 새 키로
덮어쓰지 않습니다. 초기 복구에는 원래 OS의 로그인 세션 복구가 필요하지
않지만, ZOJ 공통 인증·에이전트 연결 설정은 함께 복원해야 합니다.

참고: [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[Restic 백업](https://restic.readthedocs.io/en/stable/040_backup.html),
[보관 정책](https://restic.readthedocs.io/en/stable/060_forget.html).
