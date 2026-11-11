#!/usr/bin/env bash
# Docker is the runner's authorized privileged boundary. The destination is
# deliberately fixed to the managed helper installed by backup/install.sh.
set -euo pipefail
image=${1:?Tested API image}
[[ $image =~ ^zoj-editor-api:[a-f0-9]{40}$ ]] || exit 2
managed=/usr/local/lib/zoj-editor-backup
[[ -f "$managed/editor_backup.py" ]] || exit 0
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
docker run --rm --network none --read-only --user 0:0 --entrypoint python \
    --mount "type=bind,source=$script_dir/backup/editor_backup.py,target=/source/editor_backup.py,readonly" \
    --mount "type=bind,source=$managed,target=/managed" \
    "$image" -c '
import os, pathlib, stat, tempfile
directory = pathlib.Path("/managed")
target = directory / "editor_backup.py"
assert directory.stat().st_uid == 0 and not directory.stat().st_mode & 0o022
assert target.is_file() and not target.is_symlink() and target.stat().st_uid == 0
data = pathlib.Path("/source/editor_backup.py").read_bytes()
compile(data, str(target), "exec")
temporary = None
try:
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".editor-backup-", delete=False) as stream:
        temporary = pathlib.Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o755)
    os.replace(temporary, target)
finally:
    if temporary is not None:
        temporary.unlink(missing_ok=True)
'
