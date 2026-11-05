"""Offline X receipt calibration: default plan, explicit hash-bound apply.

No HTTP, credentials, invoice claim, budget increase or historical reset.
Run from the qualified source root; apply as app UID 10001, never SQLite root.
"""
import argparse,json,os,sqlite3,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from services.hub.db import Store,encode
from services.hub.x_cost_ledger import calibration_plan,apply_calibration,digest


class ReadOnlyStore(Store):
    def connect(self):
        db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=15)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');return db


def safe(file):
    if not file.is_absolute() or file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):raise ValueError('UNSAFE_PATH')
    return file


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('/opt/lighthouse-hub'))
    p.add_argument('--output',type=Path);p.add_argument('--apply',action='store_true');p.add_argument('--plan',type=Path);p.add_argument('--plan-sha256')
    a=p.parse_args();root=safe(a.root);dbfile=safe(root/'data/hub.sqlite3')
    if not dbfile.is_file():raise ValueError('EXISTING_DATABASE_REQUIRED')
    if a.apply:
        if os.name!='posix' or os.getuid()!=10001:raise ValueError('APP_UID_REQUIRED_FOR_SQLITE_APPLY')
        file=safe(a.plan);raw=file.read_bytes()
        if len(raw)>10_000_000:raise ValueError('PLAN_BOUND')
        plan=json.loads(raw)
        # The operator pins canonical structured plan bytes, independent of
        # shell/newline formatting. Original receipts are not rewritten.
        if digest(plan)!=a.plan_sha256:raise ValueError('EXACT_PLAN_SHA256_REQUIRED')
        result=apply_calibration(root,Store(dbfile),plan)
    else:
        if a.plan is not None:raise ValueError('USE_PLAN_ONLY_WITH_EXPLICIT_APPLY')
        result=calibration_plan(root,ReadOnlyStore(dbfile))
        result={'state':'plan_only','plan_sha256':digest(result),'plan':result}
    if a.output:
        output=safe(a.output)
        if not output.parent.is_dir():raise ValueError('EXISTING_PRIVATE_OUTPUT_DIRECTORY_REQUIRED')
        # Apply consumes the actual plan, rather than a presentation wrapper.
        value=result['plan'] if not a.apply else result
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(encode(value).encode()+b'\n');stream.flush();os.fsync(stream.fileno())
        if os.name=='posix':
            dfd=os.open(output.parent,os.O_RDONLY|os.O_DIRECTORY|getattr(os,'O_NOFOLLOW',0))
            try:os.fsync(dfd)
            finally:os.close(dfd)
        print(json.dumps({'state':result['state'],'file':str(output),'plan_sha256':result.get('plan_sha256'),'periods':value.get('periods')},sort_keys=True))
    else:print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':main()
