"""Real SQLite reservation-layer contracts, not media/native acceptance.

The reused fixture is explicitly a synthetic historical registry. These tests
call the protected one-use reservation layer directly to check real database
uniqueness/CAS behavior, with no sending grant, browser or platform call.
"""
from copy import deepcopy
import hashlib,time,uuid
from pathlib import Path

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.media_processing import atomic_json
from services.hub.replay_parts_v5 import ReplayPartVideoWeb,STATE,POLICY,VERSION,part_type,part_kind
from services.hub.youtube_download import digest
from test_replay_part_moderation_v5 import lookup

PRINCIPAL='owned:reservation-layer-only'

def two_prepared(f):
    first=f.db.get(f.parent);p=first['payload'];p.update(part_count=2)
    p['metadata']['title']='Owned synthetic P1/2';p['metadata_sha256']=digest(p['metadata'])
    first['result'][STATE].update(state='uploaded')
    group=f.db.get(f.group);group['payload']['part_count']=2
    with f.db.transaction() as db:
        db.execute('DELETE FROM publications WHERE job_id=?',(f.parent,))  # This fixture's synthetic row only.
        db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(group['payload']),f.group))
        db.execute('UPDATE replay_part_groups_v5 SET part_count=2 WHERE parent_job_id=?',(f.group,))
        db.execute("UPDATE replay_part_children_v5 SET state='prepared',external_id=NULL WHERE child_job_id=?",(f.parent,))
        db.execute("UPDATE jobs SET state='upload_pending',payload=?,result=? WHERE job_id=?",(encode(p),encode(first['result']),f.parent))
    second=deepcopy(p);second.update(part_index=1,artifact_sha256='e'*64)
    second['metadata']['title']='Owned synthetic P2/2';second['metadata_sha256']=digest(second['metadata'])
    key=digest(['bilibili','youtube',first['source_id'],'site_uid:'+p['site_uid'],p['parts_manifest_sha256'],1,second['artifact_sha256'],part_kind(1)])
    second['part_idempotency_key']=key
    ident=f.db.enqueue('youtube',first['source_id'],part_type(1),'video',second,POLICY)
    state={'version':VERSION,'state':'uploaded','key':key,'attempt_id':str(uuid.uuid4()),'human_review':False,'public_submission_approved':False}
    with f.db.transaction() as db:
        db.execute("UPDATE jobs SET state='upload_pending',result=? WHERE job_id=?",(encode({STATE:state}),ident))
        db.execute('INSERT INTO replay_part_children_v5 VALUES(?,?,?,?,?,?,?)',(f.group,1,ident,key,'e'*64,'prepared',None))
    return f.db.get(f.parent),f.db.get(ident)

def reserve(f,job,*,index=None):
    index=job['payload']['part_index'] if index is None else index
    service=ReplayPartVideoWeb(f.s,f.db,part_index=index)
    return service._reserve_submit_once(PRINCIPAL,job,job['payload'],job['result'][STATE],site_uid=job['payload']['site_uid'])


BV='BV1ownedPREV'

def accept_owned_predecessor(f,first):
    """Persist exact historical predecessor gate shape, not a live acceptance."""
    current=f.db.get(first['job_id']);binding=current['payload'];state=current['result'][STATE]
    service=ReplayPartVideoWeb(f.s,f.db,part_index=0);base=service._native_base(current,binding,state)
    file=f.s.root/'media'/current['job_id']/service.NATIVE_DIRECTORY/state['attempt_id']/'later-lookups-v2'/str(uuid.uuid4())/'reconciliation.json'
    file.parent.mkdir(parents=True)
    raw={**base,'version':'replay-later-native-result-observation-v2','external_id':BV}
    atomic_json(file,raw);ref={'file':str(file),'bytes':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    state.update(state='completed',result_observation={'version':service.RESULT_OBSERVATION_VERSION,'external_id':BV,
        'public_accessible':True,'origin':'later_native_lookup_reconciled_v5','same_submit_attempt_observed':False,
        'native_result_evidence':ref},public_coverage={'complete_part_playability_verified':True})
    with f.db.transaction() as db:
        db.execute("UPDATE jobs SET state='completed',result=?,external_publish_id=?,updated_at=? WHERE job_id=?",(encode(current['result']),BV,time.time(),current['job_id']))
        db.execute("UPDATE publications SET state='completed',external_id=?,external_url=? WHERE job_id=?",(BV,'https://www.bilibili.com/video/'+BV,current['job_id']))
        db.execute("UPDATE replay_part_children_v5 SET state='completed',external_id=? WHERE child_job_id=?",(BV,current['job_id']))
    return ref


def test_same_youtube_source_and_uid_reserve_two_distinct_real_publication_kinds_once(lookup):
    f=lookup;one,two=two_prepared(f)
    first=reserve(f,one);accept_owned_predecessor(f,one);second=reserve(f,two)
    assert first['can_click_submit'] is second['can_click_submit'] is True
    pubs=f.db.query('SELECT * FROM publications ORDER BY kind')
    assert len(pubs)==2 and {p['kind'] for p in pubs}=={part_kind(0),part_kind(1)}
    assert {p['source_id'] for p in pubs}=={one['source_id']} and {p['account_id'] for p in pubs}=={'site_uid:'+one['payload']['site_uid']}
    assert len({p['idempotency_key'] for p in pubs})==2 and {p['state'] for p in pubs}=={'completed','dispatching'}
    assert f.db.get(one['job_id'])['attempts']==3 and f.db.get(two['job_id'])['attempts']==0


def test_same_part_dispatching_or_unknown_never_creates_second_row_or_second_send_authorization(lookup):
    f=lookup;one,two=two_prepared(f);reserve(f,one);accept_owned_predecessor(f,one);reserve(f,two)
    before=f.db.query('SELECT * FROM publications ORDER BY job_id')
    assert reserve(f,f.db.get(two['job_id']))=={'can_click_submit':False,'state':'dispatching','reconcile_required':True}
    with f.db.transaction() as db:db.execute("UPDATE publications SET state='unknown' WHERE job_id=?",(two['job_id'],))
    assert reserve(f,f.db.get(two['job_id']))=={'can_click_submit':False,'state':'unknown','reconcile_required':True}
    after=f.db.query('SELECT * FROM publications ORDER BY job_id')
    assert len(after)==len(before)==2 and {p['idempotency_key'] for p in after}=={p['idempotency_key'] for p in before}


@pytest.mark.parametrize('change',['ordinal','foreign_kind','foreign_job','changed_payload_after_snapshot'])
def test_incoherent_part_or_foreign_publication_cannot_reserve_an_additional_send(lookup,change):
    f=lookup;one,two=two_prepared(f);reserve(f,one)
    if change=='ordinal':
        with pytest.raises(HubError):reserve(f,two,index=0)
        assert len(f.db.query('SELECT * FROM publications'))==1;return
    unrelated=f.db.enqueue('maintenance','owned-foreign','owned-other','system',{},'owned') if change=='foreign_job' else None
    with f.db.transaction() as db:
        if change=='foreign_kind':db.execute("UPDATE publications SET kind='video_website_replay_highest_v2' WHERE job_id=?",(one['job_id'],))
        elif change=='foreign_job':
            db.execute('UPDATE publications SET job_id=? WHERE job_id=?',(unrelated,one['job_id']))
        else:
            changed=deepcopy(two['payload']);changed['metadata']['title']='Changed after actual snapshot'
            db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(changed),two['job_id']))
    before=f.db.query('SELECT * FROM publications')
    with pytest.raises(HubError):reserve(f,two)
    assert f.db.query('SELECT * FROM publications')==before and f.db.get(two['job_id'])['state']=='upload_pending'
