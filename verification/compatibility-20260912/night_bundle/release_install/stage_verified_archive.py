"""Stage one exact archive into a new protected directory. Does not run it."""
import hashlib
import io
import os
import stat
import sys
import tarfile
from pathlib import Path

ARCHIVE_SHA = 'b15c44b568680a93f249b1816e7713fbae6eb245e637564a4be7f56c7b5b9ba3'
STAGE = Path('/var/backups/hermes-stage-20260912-4cb3')
NAMES = {
    'manifest.json',
    'files/worker/hermes/ai_worker.py',
    'files/worker/hermes/ai_analyst.py',
    'files/renderer/hermes/ai_analyst.py',
    'release_install/install_once.py',
}

def main():
    if sys.argv[1:] != ['--stage-exact-archive']:
        raise SystemExit('REFUSED: exact staging flag required')
    if os.geteuid() != 0 or os.path.lexists(STAGE):
        raise SystemExit('STOP: root and absent stage required')
    parent = STAGE.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or STAGE.parent.resolve() != STAGE.parent or parent.st_uid != 0:
        raise SystemExit('STOP: unsafe parent')
    raw = sys.stdin.buffer.read(1_000_001)
    if len(raw) > 1_000_000 or hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA:
        raise SystemExit('STOP: archive checksum/size mismatch')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        members = archive.getmembers()
        if len(members) != 5 or {m.name for m in members} != NAMES:
            raise SystemExit('STOP: archive member mismatch')
        if any(not m.isfile() or m.size > 300_000 for m in members):
            raise SystemExit('STOP: non-regular or oversized member')
        files = {m.name: archive.extractfile(m).read() for m in members}
    os.mkdir(STAGE, 0o700)
    os.chmod(STAGE, 0o700)
    for name, contents in files.items():
        target = STAGE / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    print('PASS: exactly five verified files staged; no application execution or service changes')

if __name__ == '__main__':
    main()
