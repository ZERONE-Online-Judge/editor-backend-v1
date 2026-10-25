#!/usr/bin/env bash
# Run from the uploaded, reviewed backup directory on the Editor host.
set -euo pipefail
umask 077
[[ $(id -u) == 0 && $(hostname) == zoj-editor ]] || {
  echo 'Run with sudo on the zoj-editor server.' >&2; exit 1;
}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
disk=${1:?Pass the dedicated scsi1 disk by its /dev/disk/by-id path}
[[ "$disk" == /dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1 ]] || {
  echo 'Only the explicitly identified Editor scsi1 backup disk is accepted.' >&2; exit 1;
}
root=/srv/zoj-editor
mount=/srv/zoj-editor-backup
config=/etc/zoj-editor-backup
state=/var/lib/zoj-editor-backup
program=/usr/local/lib/zoj-editor-backup
[[ -d "$root/data" && -f "$root/config/editor.env" && -f "$root/config/deploy.lock" ]] || exit 1
for file in editor_backup.py disk_guard.py zoj-editor-backup.service zoj-editor-backup.timer \
  zoj-editor-backup-maintain.service zoj-editor-backup-maintain.timer \
  zoj-editor-backup-drill.service zoj-editor-backup-drill.timer; do
  [[ -f "$source_dir/$file" ]] || exit 1
done

if [[ ! -f "$config/config.json" ]]; then
  # Check BEFORE installing packages or writing a partition table.
  device=$(python3 "$source_dir/disk_guard.py" "$disk")
  [[ ! -L "$mount" ]] || exit 1
  if [[ -d "$mount" ]] && find "$mount" -mindepth 1 -print -quit | read -r _; then
    echo 'Backup mount directory is not empty.' >&2; exit 1
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends restic parted e2fsprogs
  # Check again immediately before the only destructive operation.
  [[ $(python3 "$source_dir/disk_guard.py" "$disk") == "$device" ]] || exit 1
  parted --script "$device" mklabel gpt mkpart primary ext4 1MiB 100%
  udevadm settle
  partition="${device}1"
  [[ -b "$partition" ]] || exit 1
  mkfs.ext4 -m 1 -L zoj-editor-backup "$partition"
  filesystem_uuid=$(blkid -s UUID -o value "$partition")
  [[ "$filesystem_uuid" =~ ^[a-f0-9-]{36}$ ]] || exit 1
  install -d -m 0700 "$mount" "$config"
  cp -p /etc/fstab "$config/fstab.before-backup"
  printf 'UUID=%s %s ext4 defaults,nofail,nodev,nosuid,noexec 0 2\n' "$filesystem_uuid" "$mount" >> /etc/fstab
  systemctl daemon-reload
  mount "$mount"
  chmod 0700 "$mount"
  python3 - "$config" "$mount" "$filesystem_uuid" <<'PY'
import json, os, secrets, sys
from pathlib import Path
config, mount, uuid = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
password = config / 'repository-password'
with password.open('x') as stream:
    stream.write(secrets.token_urlsafe(48) + '\n')
os.chmod(password, 0o600)
(config / 'config.json').write_text(json.dumps({
    'mount': mount, 'filesystem_uuid': uuid, 'password_file': str(password),
    'storage_root': '/srv/zoj-editor', 'state_dir': '/var/lib/zoj-editor-backup',
}, indent=2) + '\n')
os.chmod(config / 'config.json', 0o600)
PY
  RESTIC_REPOSITORY="$mount/restic" RESTIC_PASSWORD_FILE="$config/repository-password" restic init
else
  # Idempotent reruns never format a disk or replace the encryption key.
  mountpoint -q "$mount" || mount "$mount"
  python3 - "$config/config.json" "$mount" <<'PY'
import json, subprocess, sys
config = json.load(open(sys.argv[1]))
actual = subprocess.check_output(['findmnt','-n','--mountpoint',sys.argv[2],'-o','UUID'],text=True).strip()
assert actual == config['filesystem_uuid'], 'Existing backup disk UUID differs'
PY
fi

install -d -m 0755 "$state" "$program"
install -d -m 0700 "$state/staging" "$state/cache" "$state/restore-drills"
install -m 0755 "$source_dir/editor_backup.py" "$program/editor_backup.py"
install -m 0644 "$source_dir/disk_guard.py" "$program/disk_guard.py"
ln -sfn "$program/editor_backup.py" /usr/local/sbin/zoj-editor-backup
for file in "$source_dir"/*.service "$source_dir"/*.timer; do
  install -m 0644 "$file" /etc/systemd/system/
done

# A second copy of the recovery key is deliverable to the administrator.
# Move/copy it to a different machine or a password manager after installation.
python3 - "$config" <<'PY'
from pathlib import Path
import json, os, pwd, sys
config = Path(sys.argv[1]); user = pwd.getpwnam('zoj-editor')
recovery = Path(user.pw_dir) / 'zoj-editor-backup-recovery.key'
if not recovery.exists():
    recovery.write_text((config / 'repository-password').read_text())
    os.chmod(recovery, 0o600); os.chown(recovery, user.pw_uid, user.pw_gid)
PY

systemctl daemon-reload
systemctl enable --now zoj-editor-backup.timer zoj-editor-backup-maintain.timer zoj-editor-backup-drill.timer
systemctl start zoj-editor-backup.service
systemctl start zoj-editor-backup-drill.service
/usr/local/sbin/zoj-editor-backup status
echo 'Backup installed and a real restore drill passed. Store the recovery key off this server.'
