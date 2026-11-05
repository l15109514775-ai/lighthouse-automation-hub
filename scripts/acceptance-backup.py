from pathlib import Path
import json
import shutil
import tempfile
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.maintenance import backup

settings=Settings.load()
store=Store(settings.db_path)
file=settings.root / 'backups/acceptance-consistent.sqlite3'
result=backup(store,file)
snapshot=Store(file)
expected=snapshot.query('SELECT COUNT(*) n FROM jobs')[0]['n']
with tempfile.TemporaryDirectory(prefix='lighthouse-restore-') as folder:
    restored_file=Path(folder) / 'restored.sqlite3'
    shutil.copy2(file,restored_file)
    restored=Store(restored_file)
    assert restored.query('PRAGMA integrity_check')[0]['integrity_check']=='ok'
    assert restored.query('SELECT COUNT(*) n FROM jobs')[0]['n']==expected
    witness=restored.query("SELECT state FROM jobs WHERE source_platform='synthetic_acceptance' AND source_id='relay-text-smoke-v1'")
    assert len(witness)==1 and witness[0]['state']=='awaiting_review'
print(json.dumps({'backup':result,'restored_job_count':expected,'actual_copy_and_restore_read_verified':True,
                  'scheduled_external_backup':'not_configured'}))
