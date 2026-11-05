"""Owned readback shapes and real SQLite/FFmpeg reservation CAS, no platform."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import time

import pytest

from test_short_video_web import short_video, PRINCIPAL
from test_replay_video_web import replay
from test_replay_machine_checks import owned
from test_replay_native_snapshot_delta import native
from services.hub.errors import HubError
from services.hub.replay_later_native_result_v1 import (
    VERSION, ORIGIN, validate_public, validate_manager, _commit_later_result, ref, require)


def public_fixture():
    binding={'job_id':'owned-job','site_uid':'123456','account_name':'Owned uploader',
             'source_url':'https://www.youtube.com/watch?v=ownedvideo1',
             'metadata':{'title':'Owned long replay','description':'Owned line one\nsource https://www.youtube.com/watch?v=ownedvideo1'}}
    at=datetime.now(timezone.utc).isoformat()
    value={'version':'known-bv-public-passive-inventory-v1','observed_at':at,'job_id':'owned-job','attempt_id':'owned-attempt',
        'external_id':'BV1234567890','expected_title':'Owned long replay','file_selections':0,'save_clicks':0,'submit_clicks':0,
        'player_seeks':0,'source_or_media_modified':False,'visible_dom':{
            'url':'https://www.bilibili.com/video/BV1234567890/','titles':[{'text':'Owned long replay'}],
            'owner_candidates':[{'class':'header-entry-mini','text':'Owned viewer','visible':True,'url':'https://space.bilibili.com/123456/','card_text':''},
                {'class':'up-name','text':'Owned uploader','visible':True,'url':'https://space.bilibili.com/123456/','card_text':'Owned uploader\nOwned card'}],
            'descriptions':[{'class':'video-desc-container','visible':True,'text':'Outer includes expand label'},
                {'class':'basic-desc-info','visible':True,'text':binding['source_url']+'\n'+binding['metadata']['description']}],
            'videos':[{'visible':True,'duration_seconds':25715,'current_time':27.25,'ready_state':4,'error_code':None,
                       'video_width':1920,'video_height':1080,'seekable_ranges':[{'start':0,'end':25715}]}]}}
    return binding,value


def test_public_up_card_slash_and_exact_source_prefix_no_viewer_boolean():
    binding,value=public_fixture()
    result=validate_public(value,binding,'owned-attempt',25715)
    assert result['external_id']=='BV1234567890' and result['publisher']['class']=='up-name'
    assert result['description_normalization']['occurrences_removed']==1
    assert result['normalized_description']==binding['metadata']['description']


@pytest.mark.parametrize('change',['viewer_only','wrong_source','short_duration','stale','duplicate_card'])
def test_false_owner_incomplete_media_or_stale_public_readback_refuses(change):
    binding,value=public_fixture();dom=value['visible_dom']
    if change=='viewer_only':dom['owner_candidates']=dom['owner_candidates'][:1]
    elif change=='wrong_source':dom['descriptions'][1]['text']='Wrong source'
    elif change=='short_duration':dom['videos'][0]['duration_seconds']=600
    elif change=='stale':value['observed_at']='2000-01-01T00:00:00Z'
    else:dom['owner_candidates'].append(deepcopy(dom['owner_candidates'][1]))
    with pytest.raises(HubError):validate_public(value,binding,'owned-attempt',25715)


def test_manager_two_links_one_bv_is_unique_but_two_bvs_or_title_only_is_not():
    binding,_=public_fixture();external='BV1234567890'
    value={'url':'https://member.bilibili.com/platform/upload-manager/article',
           'visible_text':'视频管理\n图文管理\n互动视频管理\n全部稿件 1\n进行中 0\n已发布 1\n未通过 0\nOwned long replay\n2026年11月02日 10:13:11',
           'links':[{'url':'https://www.bilibili.com/video/'+external+'/','text':'07:08:35'},
                    {'url':'https://www.bilibili.com/video/'+external+'/','text':'Owned long replay'}]}
    assert validate_manager(value,binding,external)>0
    value['links'].append({'url':'https://www.bilibili.com/video/BVabcdefghij/','text':'Other'})
    with pytest.raises(HubError):validate_manager(value,binding,external)
    value['links']=[]
    with pytest.raises(HubError):validate_manager(value,binding,external)


@pytest.fixture
def causal_cas(native):
    f=native;service=f['service'];ident=f['job_id']
    # Real owned source, full media/cover QA and coordinator reservation.
    service.record_native_prepare(PRINCIPAL,ident,f['observation'])
    binding=f['db'].get(ident)['payload']
    service.reserve_submit(PRINCIPAL,ident,attempt_id=f['attempt'],site_uid=binding['site_uid'],upload_reference_sha256='b'*64)
    service.mark_unknown(PRINCIPAL,ident)
    with f['db'].transaction() as db:db.execute('UPDATE jobs SET attempts=3 WHERE job_id=?',(ident,))
    job,binding,state=service.snapshot(ident,write=False)
    folder=f['s'].root/'media'/ident/'replay-native-intents';folder.mkdir()
    for name in ('video-selection','cover-completion','private-draft-save','public-submit'):
        (folder/(name+'.json')).write_text(json.dumps({'owned_cas_only':name}),'utf-8')
    observation={'version':service.RESULT_OBSERVATION_VERSION,'attempt_id':f['attempt'],'site_uid':binding['site_uid'],
                 'source_url':binding['source_url'],'metadata_sha256':binding['metadata_sha256'],'external_id':'BV1234567890',
                 'url':'https://www.bilibili.com/video/BV1234567890','state':'completed','public_accessible':True,
                 'public_lookup_observed_at':datetime.now(timezone.utc).isoformat(),'origin':ORIGIN,
                 'same_submit_attempt_observed':False,'human_review':False}
    pubs=f['db'].query('SELECT * FROM publications WHERE job_id=?',(ident,))
    refs={p.name:ref(p) for p in folder.iterdir()}
    f.update(cas_job=job,cas_state=state,cas_pubs=pubs,cas_refs=refs,cas_observation=observation)
    return f


def commit(f,*,recheck=lambda:None):
    return _commit_later_result(f['service'],PRINCIPAL,f['cas_job'],f['cas_state'],f['cas_pubs'],f['cas_refs'],
                               f['cas_observation'],{'version':VERSION,'same_submit_attempt_observed':False},{},recheck=recheck)


def test_cas_keeps_original_publication_attempts_history_and_future_known_bv_lookup(causal_cas):
    f=causal_cas;before=f['cas_job'];commit(f)
    job=f['db'].get(f['job_id']);state=job['result'][f['service'].STATE]
    assert job['attempts']==3 and job['lease_owner'] is job['lease_until'] is None
    assert job['payload']==before['payload'] and state['attempt_id']==f['attempt']
    assert state['result_observation']['origin']==ORIGIN and state['result_observation']['same_submit_attempt_observed'] is False
    pubs=f['db'].query('SELECT * FROM publications WHERE job_id=?',(f['job_id'],))
    assert len(pubs)==1 and pubs[0]['idempotency_key']==f['cas_pubs'][0]['idempotency_key']
    assert pubs[0]['state']=='completed' and pubs[0]['external_id']=='BV1234567890'
    # Existing known-BV lookups remain usable without inventing a send flag.
    f['service'].record_result(PRINCIPAL,f['job_id'],f['cas_observation'],lookup=True)
    assert f['db'].get(f['job_id'])['result'][f['service'].STATE]['result_reconciliation']['version']==VERSION
    assert len(f['db'].query("SELECT * FROM audit WHERE action='short_video_reserve_submit' AND target=?",(f['job_id'],)))==1


@pytest.mark.parametrize('change',['publication','job','small_evidence'])
def test_cas_refuses_changed_rows_or_final_evidence_recheck(causal_cas,change):
    f=causal_cas
    if change=='publication':
        with f['db'].transaction() as db:db.execute('UPDATE publications SET updated_at=updated_at+1 WHERE job_id=?',(f['job_id'],))
    elif change=='job':
        with f['db'].transaction() as db:db.execute('UPDATE jobs SET updated_at=updated_at+1 WHERE job_id=?',(f['job_id'],))
    recheck=(lambda:require(False)) if change=='small_evidence' else (lambda:None)
    with pytest.raises(HubError):commit(f,recheck=recheck)
    assert f['db'].get(f['job_id'])['external_publish_id'] is None
    assert f['db'].query('SELECT external_id FROM publications WHERE job_id=?',(f['job_id'],))[0]['external_id'] is None
