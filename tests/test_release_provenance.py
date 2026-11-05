"""Regression for stale same-size commit metadata in the real Docker build context."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


def test_release_refreshes_mtime_for_same_size_files_and_pins_clean_commit(tmp_path):
    source=Path(__file__).resolve().parents[1]
    root=tmp_path/'synthetic-release';(root/'scripts').mkdir(parents=True);(root/'config').mkdir()
    (root/'empty-hooks').mkdir()
    shutil.copyfile(source/'scripts/pack_release.py',root/'scripts/pack_release.py')
    (root/'config/upstream-lock.json').write_text('{}','utf-8')
    app=root/'app.txt';app.write_text('owned synthetic version 1','utf-8')
    def git(*args):
        return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()
    git('init')
    for name,value in [('user.name','synthetic-provenance-fixture'),('user.email','fixture@example.invalid'),
                       ('commit.gpgsign','false'),('core.hooksPath',str(root/'empty-hooks')),('core.autocrlf','false')]:
        git('config',name,value)
    git('add','.');git('commit','-m','Owned release fixture one')
    first_commit=git('rev-parse','HEAD')
    first=tmp_path/'first.tar.gz'
    subprocess.run([sys.executable,str(root/'scripts/pack_release.py'),'--out',str(first)],check=True,
                   stdout=subprocess.DEVNULL,timeout=10)
    prior_stat=app.stat()
    app.write_text('owned synthetic version 2','utf-8')
    os.utime(app,ns=(prior_stat.st_atime_ns,prior_stat.st_mtime_ns))
    # Populate the fixture index from actual bytes; its deliberately unchanged stat
    # must not let Git's own stat cache hide the same-size mutation.
    blob=git('hash-object','-w','app.txt')
    git('update-index','--cacheinfo','100644',blob,'app.txt')
    git('commit','-m','Owned release fixture two')
    second_commit=git('rev-parse','HEAD')
    second=tmp_path/'second.tar.gz'
    subprocess.run([sys.executable,str(root/'scripts/pack_release.py'),'--out',str(second)],check=True,
                   stdout=subprocess.DEVNULL,timeout=10)
    with tarfile.open(first) as a,tarfile.open(second) as b:
        old=a.getmember('source-version.json');new=b.getmember('source-version.json')
        assert old.size==new.size and old.mtime>0 and new.mtime>old.mtime
        assert a.getmember('app.txt').size==b.getmember('app.txt').size
        assert b.getmember('app.txt').mtime>a.getmember('app.txt').mtime
        assert json.load(a.extractfile(old))['commit']==first_commit
        assert json.load(b.extractfile(new))['commit']==second_commit
        assert b.extractfile('app.txt').read()==b'owned synthetic version 2'
        assert not any('.git' in Path(item.name).parts for item in b)
