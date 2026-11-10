import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.ops import cloudbase_backup_budget_recovery as m


def digest(value):
    return hashlib.sha256(m.encoded(value)).hexdigest()


def fixture():
    previous = {'daily_id': 'bf4d9cce-2df7-463a-9aa2-86102b51ea40', 'state': 'accept_started'}
    op = {'operation_id': '7e1f6b00-ead5-45ae-8ae4-be0e8cbf2b2e', 'state': 'resolved_remote_absent',
          'error_code': 'CLOUDBASE_BACKUP_UPLOAD_CONFIRMED_ABSENT', 'receipt': None}
    requests = [dict(request_id=str(n), operation_id=op['operation_id'], created_at=n,
                     day='2026-11-11', month='2026-11', method='GET' if n < 3 else 'PUT' if n == 3 else 'HEAD',
                     reserved_bytes=188636659 if n == 3 else 300000,
                     state='done' if n < 3 else 'reserved' if n == 3 else 'error',
                     error_code=None if n < 4 else 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND') for n in range(5)]
    plan = {'operation_id': op['operation_id'], 'maximum_requests': 7}
    proof = {'version': m.ABSENT_VERSION, 'daily_id': previous['daily_id'],
             'failed_daily_sha256': digest(previous), 'operation_id': op['operation_id'],
             'operation_sha256': digest(op), 'requests_sha256': digest(requests), 'plan_sha256': digest(plan),
             'original_requests_sha256': digest(requests[:4]), 'head_request_id': '4',
             'head_result': {'operation_id': op['operation_id'], 'state': 'absent', 'request': 'HEAD',
                             'error_code': 'CLOUDBASE_BACKUP_REMOTE_NOT_FOUND', 'remote_verified': False,
                             'puts': 0, 'gets': 0}, 'budget_refunded': False, 'backup_success_claimed': False}
    return previous, op, requests, plan, proof


@pytest.mark.parametrize('mutation', ['present', 'unresolved', 'head_unknown', 'refund', 'new_put', 'history', 'operation'])
def test_absence_proof_rejects_ambiguous_or_changed_state(mutation):
    previous, op, requests, plan, proof = fixture()
    unresolved = 0
    history = digest(previous)
    if mutation == 'present': proof['head_result']['state'] = 'present'
    if mutation == 'unresolved': unresolved = 1
    if mutation == 'head_unknown':
        requests[-1]['state'] = 'unknown'; proof['requests_sha256'] = digest(requests)
    if mutation == 'refund': proof['budget_refunded'] = True
    if mutation == 'new_put': requests.append(dict(requests[3], request_id='second-put'))
    if mutation == 'history': history = 'f' * 64
    if mutation == 'operation': op['state'] = 'uploading'
    with pytest.raises(ValueError, match='ABSENT_RECOVERY_'):
        m.verify_absence(previous, proof, op, requests, plan, latest_sha=digest(previous),
                         history_sha=history, unresolved_count=unresolved)


def test_exact_absence_preserves_history_and_budget_when_deferring(tmp_path):
    previous, op, requests, plan, proof = fixture()
    m.verify_absence(previous, proof, op, requests, plan, latest_sha=digest(previous),
                     history_sha=digest(previous), unresolved_count=0)
    proof_file = m.absent_proof_path(tmp_path, previous)
    proof_file.parent.mkdir(); proof_file.write_bytes(m.encoded(proof))
    calls=[]
    daily=SimpleNamespace(execute=lambda p, **kw: calls.append(p), atomic=lambda *a: None)
    m.bind(daily, state=tmp_path, inspector=lambda *a: {'state': 'budget_deferred'})
    with pytest.raises(m.BudgetDeferred): daily.execute(previous)
    assert not calls and previous['state']=='accept_started' and requests[3]['reserved_bytes']==188636659


def test_sqlite_inspection_accepts_exact_proof_then_refuses_later_request(tmp_path):
    previous, op, requests, plan, proof = fixture()
    root, state = tmp_path/'app', tmp_path/'state'
    for p in (root/'config', root/'data/cloudbase-backup-state', state/'history', state/'remote-absent-recovery'):
        p.mkdir(parents=True)
    limits=dict(daily_network_bytes=512000000,monthly_network_bytes=8000000000,daily_requests=50,
                monthly_requests=250,max_archive_bytes=192000000)
    (root/'config/cloudbase-backup.toml').write_text('[limits]\n'+'\n'.join(f'{k}={v}' for k,v in limits.items()))
    for path, value in [(state/'latest.json',previous), (state/'history'/(previous['daily_id']+'.json'),previous),
                        (m.absent_proof_path(state,previous),proof)]: path.write_bytes(m.encoded(value))
    ledger=root/'data/cloudbase-backup-state/ledger.sqlite3'
    with sqlite3.connect(ledger) as db:
        db.execute('PRAGMA journal_mode=WAL')
        for table, rows in [('operations',[op]),('requests',requests),('plans',[plan])]:
            keys=list(rows[0]);db.execute('CREATE TABLE '+table+'('+','.join(keys)+')')
            db.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in keys)+')', [tuple(r[k] for k in keys) for r in rows])
    def read(path, *a, **kw):
        raw=path.read_bytes();return json.loads(raw),hashlib.sha256(raw).hexdigest()
    daily=SimpleNamespace(safe=lambda p,*a:p,protected=lambda *a,**kw:None,ident=lambda v:v,read_record=read)
    assert m.inspect(daily,root,state,previous)['external_requests']==0
    with sqlite3.connect(ledger) as db: db.execute("UPDATE requests SET state='unknown' WHERE request_id='4'")
    with pytest.raises(ValueError,match='ABSENT_RECOVERY_LEDGER_CHANGED'):m.inspect(daily,root,state,previous)


def test_unattested_interruption_keeps_original_refusal_path(tmp_path):
    previous, *_ = fixture()
    calls=[]
    daily=SimpleNamespace(execute=lambda p,**kw:calls.append(p),atomic=lambda *a:None)
    def forbidden(*a): raise AssertionError('must keep original gate')
    m.bind(daily,state=tmp_path,inspector=forbidden)
    daily.execute(previous)
    assert calls==[previous]
