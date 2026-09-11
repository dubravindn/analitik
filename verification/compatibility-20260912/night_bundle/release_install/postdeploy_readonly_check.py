"""Fixed-scope status reader for the owned Hermes server; no mutation methods."""
import hashlib
import json
import pathlib
import subprocess
import time

FILES = {
    '/var/lib/hermes-ai/app/hermes/ai_worker.py': '6f3acadeebdd54f7a703c61d5298f2085fef4683fbaf2f13eb5a31f9f8690171',
    '/var/lib/hermes-ai/app/hermes/ai_analyst.py': '1bff538fb61a116a650c5210d22f125b2bfd993ffb62be74e2e0c3a75559560b',
    '/opt/hermes/app/hermes/ai_analyst.py': '8815da3bc31a4bb4c9a398c7c03b73f3c73daae319ab77bb9eac272bfc6ef41d',
}
UNITS = ('hermes-bot.service', 'hermes-ai-worker.service', 'hermes-ai-daily.timer', 'hermes-daily.timer')

def check():
    result = {'files': {}, 'units': {}, 'queue': {}, 'errors': []}
    for name, expected in FILES.items():
        try:
            result['files'][name] = hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest() == expected
        except OSError:
            result['errors'].append('file-unreadable:' + name)
    for name in UNITS:
        try:
            run = subprocess.run(
                ['systemctl', 'show', name, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID', '-p', 'NRestarts'],
                capture_output=True, text=True, timeout=15,
            )
            if run.returncode:
                result['errors'].append('unit-unreadable:' + name)
            else:
                result['units'][name] = dict(line.split('=', 1) for line in run.stdout.splitlines() if '=' in line)
        except (OSError, subprocess.TimeoutExpired):
            result['errors'].append('unit-unreadable:' + name)
    now = time.time()
    for name in ('jobs', 'processing', 'results'):
        try:
            entries = list((pathlib.Path('/var/lib/hermes-ai/spool') / name).iterdir())
            ages = [max(0, now - entry.stat().st_mtime) for entry in entries]
            result['queue'][name] = {'entries': len(entries), 'oldest_age_seconds': round(max(ages, default=0))}
        except OSError:
            result['errors'].append('queue-unreadable:' + name)
    print(json.dumps(result, sort_keys=True))

if __name__ == '__main__':
    check()
