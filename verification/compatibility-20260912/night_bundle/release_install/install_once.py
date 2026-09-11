#!/usr/bin/env python3
"""One exact future release operation. Default mode refuses without subprocesses."""
from __future__ import annotations

import argparse, hashlib, json, os, stat, subprocess, sys, time
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"
BACKUP = Path("/var/backups/hermes-files-20260912-4cb3")
SPOOL = (Path("/var/lib/hermes-ai/spool/jobs"), Path("/var/lib/hermes-ai/spool/processing"), Path("/var/lib/hermes-ai/spool/results"))
BOT, WORKER, AI_TIMER, AI_ONESHOT, DAILY_ONESHOT = "hermes-bot.service", "hermes-ai-worker.service", "hermes-ai-daily.timer", "hermes-ai-daily.service", "hermes-daily.service"
DB_DUMP_SHA = "21d1b469807d2353c7bd2ba6335a547a7d4839432c60ad149a6b211bf16c9551"
DBPY_SHA = "30ca94ee7f697aecbaa1d992f8909e37791af56f37654dfc904e84c40e4e4376"
SCHEMA_SHA = "a27afe9dae79a188de93f0bbd9a970d911e175a86a149ee29bd43fd48690e7ad"
INTERPRETERS = {"worker_ai_worker":"/usr/bin/python3", "worker_ai_analyst":"/usr/bin/python3", "renderer_ai_analyst":"/opt/hermes/venv/bin/python"}
EXPECTED = {
 "worker_ai_worker": ("files/worker/hermes/ai_worker.py", "/var/lib/hermes-ai/app/hermes/ai_worker.py", "1be5a20a41464e65154c3e8af183e7bb740ae6367dd645408f818cdf56b8b439", "6f3acadeebdd54f7a703c61d5298f2085fef4683fbaf2f13eb5a31f9f8690171"),
 "worker_ai_analyst": ("files/worker/hermes/ai_analyst.py", "/var/lib/hermes-ai/app/hermes/ai_analyst.py", "8675efa855c284c8635a42d766fe8e64650b59834c270713e7286fc5bde05e61", "1bff538fb61a116a650c5210d22f125b2bfd993ffb62be74e2e0c3a75559560b"),
 "renderer_ai_analyst": ("files/renderer/hermes/ai_analyst.py", "/opt/hermes/app/hermes/ai_analyst.py", "9f837ec597b109329d8caf9477881a01e6cec791eef8130354950588385c3242", "8815da3bc31a4bb4c9a398c7c03b73f3c73daae319ab77bb9eac272bfc6ef41d"),
}
DB_DUMP=Path("/var/backups/hermes-night-20260912-4cb3/hermes.custom")
EXPECTED_ROOT = Path('/var/backups/hermes-stage-20260912-4cb3')
ATTRS = {'worker_ai_worker': (997,987,0o644), 'worker_ai_analyst': (997,987,0o644), 'renderer_ai_analyst': (0,0,0o644)}

class Stop(RuntimeError): pass
class Unsafe(Stop): pass
@dataclass(frozen=True)
class Target:
    ident: str; source: Path; target: Path; old: str; new: str

def digest(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()
def safe_regular(path: Path):
    s=os.lstat(path)
    if not stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode) or s.st_nlink != 1: raise Stop("symlink or hardlink rejected")
    return s
def load_targets() -> list[Target]:
    m=json.loads(MANIFEST.read_text()); out=[]
    if m.get("format")!="hermes-night-bundle-v1" or len(m.get("targets",[]))!=3: raise Stop("manifest not exact")
    for x in m["targets"]:
        if x.get("id") not in EXPECTED: raise Stop("manifest route altered")
        rel,target,old,new=EXPECTED[x["id"]];p=ROOT/rel
        if (x.get("package_path"),x.get("target_path"),x.get("expected_previous_sha256"),x.get("target_sha256")) != (rel,target,old,new): raise Stop("manifest route altered")
        safe_regular(p)
        if x.get('source_sha256') != new or digest(p)!=new: raise Stop("bundle hash mismatch")
        out.append(Target(x["id"],p,Path(target),old,new))
    if {x.target for x in out}!={Path("/var/lib/hermes-ai/app/hermes/ai_worker.py"),Path("/var/lib/hermes-ai/app/hermes/ai_analyst.py"),Path("/opt/hermes/app/hermes/ai_analyst.py")}: raise Stop("not exact three targets")
    return out

class Real:
    def run(self,args:Sequence[str]):
        try:r=subprocess.run(list(args),text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90,check=False,env={"PATH":"/usr/bin:/bin:/usr/sbin:/sbin","LANG":"C"})
        except Exception as e: raise Stop("system command failed safely") from e
        return r.returncode,r.stdout.strip()
    def status(self,u):
        rc,text=self.run(['systemctl','show',u,'-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID','-p','NRestarts'])
        result=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
        if rc or result.get('LoadState')!='loaded': raise Stop('service status unavailable')
        return result
    def active(self,u): return self.status(u).get('ActiveState')=='active'
    def stop(self,u):
        if self.run(["systemctl","stop",u])[0]!=0: raise Stop("service stop failed")
    def start(self,u):
        if self.run(["systemctl","start",u])[0]!=0: raise Stop("service start failed")
    def compile_source(self,path,interpreter):
        code="compile(open(%r, encoding='utf-8').read(), %r, 'exec')" % (str(path),str(path))
        if self.run([interpreter,"-B","-c",code])[0]!=0: raise Stop("stage syntax check failed")
    def healthy(self,u):
        s=self.status(u)
        return s.get('ActiveState')=='active' and s.get('SubState')=='running' and int(s.get('MainPID','0'))>0
    def stable(self,u):
        first=self.status(u)
        if not str(first.get('NRestarts','')).isdigit() or not str(first.get('MainPID','')).isdigit(): raise Stop('restart counters unavailable')
        if not self.healthy(u): raise Stop('started service is not running')
        for _ in range(4):
            time.sleep(2)
            now=self.status(u)
            if not self.healthy(u) or any(now.get(k)!=first.get(k) for k in ('MainPID','NRestarts')):
                raise Stop('service restart or instability detected')

def queue_empty() -> bool:
    try:
        for p in SPOOL:
            s=p.lstat()
            if not stat.S_ISDIR(s.st_mode) or p.resolve()!=p or (s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode))!=(997,987,0o2770): return False
            if any(p.iterdir()): return False
        return True
    except OSError:
        return False
def ensure_window():
    now=datetime.now(ZoneInfo("Europe/Moscow"))
    if now.date()!=date(2026,9,12) or (now.hour,now.minute)>=(6,30): raise Stop("safe start window closed")
def preflight_hashes(ts):
    if ROOT != EXPECTED_ROOT or ROOT.is_symlink() or ROOT.lstat().st_uid!=0 or stat.S_IMODE(ROOT.lstat().st_mode)!=0o700: raise Stop('exact protected stage required')
    if os.path.lexists(BACKUP) or not DB_DUMP.is_file() or digest(DB_DUMP)!=DB_DUMP_SHA: raise Stop("backup/DB dump precondition failed")
    if digest(Path("/opt/hermes/app/hermes/db.py"))!=DBPY_SHA or digest(Path("/opt/hermes/app/hermes/schema.sql"))!=SCHEMA_SHA: raise Stop("startup schema guard failed")
    check_targets(ts, allow_new=False)
def check_targets(ts, allow_new):
    for t in ts:
        s=safe_regular(t.target)
        if t.target.resolve()!=t.target or (s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode))!=ATTRS[t.ident]: raise Stop('target attributes changed')
        if digest(t.target) not in ({t.old,t.new} if allow_new else {t.old}): raise Stop('target hash guard failed')
        for suffix in ('.night-4cb3.tmp','.rollback-4cb3.tmp'):
            if os.path.lexists(t.target.with_name('.'+t.target.name+suffix)): raise Stop('target staging path already exists')
def backup_dir() -> None:
    if os.geteuid()!=0 or os.path.lexists(BACKUP): raise Stop("root/new-backup precondition failed")
    parent=os.lstat(BACKUP.parent)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=0 or stat.S_ISLNK(parent.st_mode): raise Stop("unsafe backup parent")
    os.mkdir(BACKUP,0o700); os.chown(BACKUP,0,0); os.chmod(BACKUP,0o700)
    parent_fd=os.open(BACKUP.parent,os.O_RDONLY)
    try: os.fsync(parent_fd)
    finally: os.close(parent_fd)
def backup(targets:list[Target]):
    rows=[]
    for t in targets:
        s=safe_regular(t.target); dst=BACKUP/(t.ident+".py")
        raw=t.target.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=t.old: raise Stop('source changed while backing up')
        fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f: f.write(raw);f.flush();os.fsync(f.fileno())
        os.chmod(dst,0o600); rows.append({"id":t.ident,"sha256":digest(dst),"uid":s.st_uid,"gid":s.st_gid,"mode":stat.S_IMODE(s.st_mode),"target":str(t.target)})
    p=BACKUP/"manifest.json"
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(rows,f,sort_keys=True);f.flush();os.fsync(f.fileno())
    os.chmod(p,0o600); write_state("backup-complete",[]); return rows
def validate_backup(targets,rows):
    if len(rows)!=len(targets) or len(rows)!=3: raise Unsafe('backup cardinality mismatch')
    persisted=json.loads((BACKUP/'manifest.json').read_text())
    if persisted!=rows: raise Unsafe('backup manifest changed')
    for t,row in zip(targets,rows):
        b=BACKUP/(t.ident+'.py');s=safe_regular(b)
        if row.get('id')!=t.ident or row.get('target')!=str(t.target) or row.get('sha256')!=t.old: raise Unsafe('backup route mismatch')
        if (row.get('uid'),row.get('gid'),row.get('mode'))!=ATTRS[t.ident]: raise Unsafe('backup attributes mismatch')
        if s.st_uid!=0 or stat.S_IMODE(s.st_mode)!=0o600 or digest(b)!=t.old: raise Unsafe('backup contents invalid')
def write_state(phase,replaced):
    """Durably record intent before replacements; never overwrite a backup manifest."""
    p=BACKUP/"state.json"; tmp=BACKUP/".state-4cb3.tmp"
    if os.path.lexists(tmp): raise Stop("state staging name exists")
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as f:
        json.dump({"phase":phase,"replaced":list(replaced)},f,sort_keys=True);f.flush();os.fsync(f.fileno())
    os.chmod(tmp,0o600);os.replace(tmp,p)
    d=os.open(BACKUP,os.O_RDONLY);os.fsync(d);os.close(d)
def replace(t:Target,row:dict):
    current=safe_regular(t.target)
    if digest(t.target)!=t.old: raise Stop("hash changed before replace")
    tmp=t.target.with_name("."+t.target.name+".night-4cb3.tmp")
    if os.path.lexists(tmp): raise Stop("staging name exists")
    raw=t.source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=t.new: raise Stop('source bundle changed')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    os.chown(tmp,row["uid"],row["gid"]); os.chmod(tmp,row["mode"])
    if digest(tmp)!=t.new: raise Stop("staged hash mismatch")
    os.replace(tmp,t.target)
    d=os.open(t.target.parent,os.O_RDONLY);os.fsync(d);os.close(d)
def rollback(targets,rows):
    if not queue_empty(): raise Unsafe("queue nonempty; retain state")
    validate_backup(targets,rows)
    # Validate every target and every backup before replacing even the first file.
    for t in targets:
        safe_regular(t.target)
        if digest(t.target) not in {t.old,t.new}: raise Unsafe('unknown target change; no overwrite')
        if os.path.lexists(t.target.with_name('.'+t.target.name+'.rollback-4cb3.tmp')): raise Unsafe('rollback staging already exists')
    write_state('rolling-back',[])
    for t,row in zip(targets,rows):
        src=BACKUP/(t.ident+".py"); tmp=t.target.with_name("."+t.target.name+".rollback-4cb3.tmp")
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(src.read_bytes());f.flush();os.fsync(f.fileno())
        if digest(tmp)!=t.old: raise Unsafe('rollback staging checksum mismatch')
        os.chown(tmp,row["uid"],row["gid"]);os.chmod(tmp,row["mode"]);os.replace(tmp,t.target)
        d=os.open(t.target.parent,os.O_RDONLY);os.fsync(d);os.close(d)
    if any(digest(t.target)!=t.old for t in targets): raise Unsafe("rollback hash failed")
    write_state("rolled-back",[])
def wait_empty(seconds=30):
    end=time.monotonic()+seconds
    while not queue_empty() and time.monotonic()<end: time.sleep(2)
    return queue_empty()
def run(adapter:Real):
    if os.geteuid()!=0: raise Stop('root required')
    ensure_window()
    ts=load_targets()
    preflight_hashes(ts)
    for t in ts: adapter.compile_source(t.source,INTERPRETERS[t.ident])
    if not adapter.healthy(BOT) or not adapter.healthy(WORKER): raise Stop('initial services unhealthy')
    for unit in (AI_ONESHOT,DAILY_ONESHOT):
        if adapter.status(unit).get('ActiveState')!='inactive': raise Stop('daily job not inactive')
    timer_state=adapter.status(AI_TIMER).get('ActiveState')
    if timer_state not in {'active','inactive'}: raise Stop('timer state unavailable')
    timer=timer_state=='active'
    if not queue_empty(): raise Stop('queue not empty')
    # A filesystem failure during backup leaves the live processes unchanged.
    backup_dir(); rows=backup(ts);validate_backup(ts,rows)
    ensure_window()
    timer_touched=False; bot_touched=False; worker_touched=False
    mutation_started=False; failure=None; uncertain=False
    replaced=[]
    try:
        write_state('quiescing',[])
        if timer:
            timer_touched=True;adapter.stop(AI_TIMER)
        if adapter.status(AI_ONESHOT).get('ActiveState')!='inactive': raise Stop('producer raced timer pause')
        if not wait_empty(): raise Stop('drain timeout')
        bot_touched=True;adapter.stop(BOT)
        if adapter.status(BOT).get('ActiveState')!='inactive': raise Stop('bot did not stop')
        if not queue_empty(): raise Stop('queue changed; no file changes')
        worker_touched=True;adapter.stop(WORKER)
        if adapter.status(WORKER).get('ActiveState')!='inactive' or not queue_empty(): raise Stop('worker not quiescent')
        check_targets(ts,allow_new=False);validate_backup(ts,rows)
        mutation_started=True
        for t,row in zip(ts,rows):
            write_state('replacing',replaced+[t.ident])
            replace(t,row);replaced.append(t.ident)
        write_state('replaced',replaced)
        if any(digest(t.target)!=t.new for t in ts): raise Stop('post-replace checksum mismatch')
        adapter.start(WORKER);adapter.stable(WORKER)
        adapter.start(BOT);adapter.stable(BOT)
        # A legitimate incoming job after startup is not a reason to kill the worker.
        if not adapter.healthy(WORKER): raise Stop('worker unhealthy after bot startup')
        write_state('installed-awaiting-owner-test',replaced)
    except Exception as error:
        failure=error
        if mutation_started:
            try:
                if not queue_empty(): raise Unsafe('pending work: no automatic stop or rollback')
                if adapter.active(BOT): adapter.stop(BOT)
                if adapter.status(BOT).get('ActiveState') not in {'inactive','failed'} or not queue_empty(): raise Unsafe('bot or queue not quiescent for rollback')
                if adapter.active(WORKER): adapter.stop(WORKER)
                if adapter.status(WORKER).get('ActiveState') not in {'inactive','failed'} or not queue_empty(): raise Unsafe('worker or queue not quiescent for rollback')
                rollback(ts,rows)
                adapter.start(WORKER);adapter.stable(WORKER)
                adapter.start(BOT);adapter.stable(BOT)
            except Exception:
                uncertain=True
        else:
            try:
                # No code was changed. Restore only processes touched by this attempt.
                if worker_touched and not adapter.healthy(WORKER): adapter.start(WORKER);adapter.stable(WORKER)
                if bot_touched and not adapter.healthy(BOT): adapter.start(BOT);adapter.stable(BOT)
                write_state('aborted-before-replacement',[])
            except Exception:
                uncertain=True
    finally:
        if timer_touched:
            try:
                if not uncertain and adapter.healthy(BOT) and adapter.healthy(WORKER):
                    adapter.start(AI_TIMER)
                    if not adapter.active(AI_TIMER): raise Stop('timer did not resume')
                else: uncertain=True
            except Exception:
                uncertain=True
    if uncertain: raise Unsafe('manual review required; backup retained; inspect services, queue and timer before any retry')
    if failure is not None: raise Stop('installation aborted; prior files/services restored; backup retained') from failure
    return 'PASS: exact three files installed, services stable, original timer state restored; owner functional test still required'
def main(argv:Optional[Sequence[str]]=None):
    p=argparse.ArgumentParser();p.add_argument("--execute-exact-install",action="store_true");a=p.parse_args(argv)
    if not a.execute_exact_install:
        print("REFUSED: future install requires --execute-exact-install and separate approval");return 2
    try: print(run(Real()));return 0
    except Unsafe as e: print("UNSAFE: "+str(e));return 3
    except Stop as e: print("STOPPED: "+str(e));return 1
    except Exception: print('STOPPED: preflight or filesystem operation failed; inspect state before retry');return 1
if __name__=="__main__":raise SystemExit(main())
