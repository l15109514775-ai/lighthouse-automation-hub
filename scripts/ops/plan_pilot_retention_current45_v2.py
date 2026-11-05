"""App-UID read-only pilot plan. No authorization creation, apply, or browser."""
from contextlib import contextmanager
from pathlib import Path
import argparse,json,os,sqlite3,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.locks import resource_guard
from services.hub.pilot_published_retention_v2 import PilotPublishedRetentionV2,JOB

SNAPSHOT=Path('/run/retention-snapshot/hub.sqlite3')
class ReadOnlySnapshot(Store):
    def __init__(self):
        self.path=SNAPSHOT
        assert self.path.is_file() and self.path.resolve()==self.path and not any(p.is_symlink() for p in (self.path,*self.path.parents))
        assert not self.path.with_name(self.path.name+'-wal').exists() and not self.path.with_name(self.path.name+'-shm').exists()
    def connect(self):
        db=sqlite3.connect(self.path.as_uri()+'?mode=ro&immutable=1',uri=True,isolation_level=None)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');return db
    @contextmanager
    def transaction(self):
        db=self.connect()
        try:db.execute('BEGIN');yield db
        finally:db.rollback();db.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--authorization-id',required=True)
    args=parser.parse_args();assert os.name=='posix' and os.getuid()==os.getgid()==10001 and not __import__('sys').flags.optimize
    settings=Settings.load();store=ReadOnlySnapshot()
    assert store.query('PRAGMA integrity_check')[0]['integrity_check']=='ok' and not store.query('PRAGMA foreign_key_check')
    if store.query("SELECT job_id FROM jobs WHERE queue='video' AND state='processing'"):raise RuntimeError('VIDEO_WORKER_ACTIVE')
    service=PilotPublishedRetentionV2(settings,store)
    with resource_guard(settings.root/'data/locks/video',settings.root/'data/locks/moderation'):
        value=service.plan(JOB,authorization_id=args.authorization_id)
    print(encode({'state':'dry_run_prepared','plan':value,'files_deleted':0,'authorization_created':False,
        'formal_launch_claimed':False,'external_calls':0,'snapshot_readonly':True}))
if __name__=='__main__':main()
