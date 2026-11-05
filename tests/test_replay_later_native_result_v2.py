"""Observed row grammar plus genuine SQLite reservation/CAS compatibility."""
from copy import deepcopy
from datetime import datetime,timezone
import time
import pytest
from test_replay_later_native_result_v1 import causal_cas,short_video,replay,owned,native
from services.hub.errors import HubError
from services.hub.replay_later_native_result_v1 import _commit_later_result
from services.hub.replay_later_native_result_v2 import VERSION,ORIGIN,validate_manager_row,validate_manager_before
from services.hub.short_video_web import PRINCIPAL


def row_observation():
    b={'job_id':'owned','site_uid':'123','source_id':'ownedvideo1','source_url':'https://www.youtube.com/watch?v=ownedvideo1',
        'metadata_sha256':'a'*64,'metadata':{'title':'Owned complete replay'}}
    row={'class':'article-card clearfix v2','title_links':[{'tag':'A','class':'name ellipsis','text':b['metadata']['title'],
        'href':'https://www.bilibili.com/video/BV1234567890/','visible':True}],
        'covers':[{'tag':'A','class':'cover-wrp','text':'07:08:35','href':'https://www.bilibili.com/video/BV1234567890/','visible':True}],
        'dates':[{'class':'date','parent_class':'pubdate is-success','text':'2026年11月02日 10:13:11'}],'durations':['07:08:35']}
    v={'version':'replay-native-manager-row-v2','job_id':'owned','attempt_id':'11111111-1111-4111-8111-111111111111',
       'site_uid':'123','source_id':b['source_id'],'source_url':b['source_url'],'metadata_sha256':'a'*64,'binding_sha256':'b'*64,
       'observed_at':datetime.now(timezone.utc).isoformat(),'file_selections':0,'save_clicks':0,'submit_clicks':0,'player_seeks':0,
       'source_or_media_modified':False,'known_external_id':None,'selected_row':row,'visible_dom':{
        'url':'https://member.bilibili.com/platform/upload-manager/article','truncated':False,
        'visible_text':'视频管理\n图文管理\n互动视频管理\n全部稿件 42\n已发布 40\n进行中 2',
        'rows':[{'title_links':[{'text':'Unrelated old replay'}]},row]}}
    return b,v


def test_nonempty_42_publications_uses_one_real_title_anchor_and_same_bv_cover():
    b,v=row_observation()
    assert validate_manager_row(v,b,'BV1234567890',duration_seconds=25715)>0
    # The DIV wrapper around the anchor is not a second title/publication.
    v['visible_dom']['rows'][1]['wrapper_title_text']=b['metadata']['title']
    assert validate_manager_row(v,b,'BV1234567890',duration_seconds=25715)>0


@pytest.mark.parametrize('change',['duplicate','cover_bv','pending','duration','stale','wrong_source','viewer_uid'])
def test_row_only_or_incoherent_readback_cannot_reconcile(change):
    b,v=row_observation();r=v['selected_row']
    if change=='duplicate':v['visible_dom']['rows'].append(deepcopy(r))
    elif change=='cover_bv':r['covers'][0]['href']='https://www.bilibili.com/video/BVabcdefghij/'
    elif change=='pending':r['dates'][0]['parent_class']='pubdate is-pending'
    elif change=='duration':r['durations']=['00:00:06']
    elif change=='stale':v['observed_at']='2000-01-01T00:00:00Z'
    elif change=='wrong_source':v['source_id']='otherVideo1'
    else:v['site_uid']='999'
    with pytest.raises(HubError):validate_manager_row(v,b,'BV1234567890',duration_seconds=25715)


def test_before_nonempty_row_scope_is_bounded_and_does_not_claim_full_history():
    b,v=row_observation();v['visible_dom']['rows']=v['visible_dom']['rows'][:1];v['selected_row']=None
    state={'attempt_id':v['attempt_id']};intent={'created_at':datetime.fromtimestamp(time.time()+20,timezone.utc).isoformat()}
    validate_manager_before(v,b,state,intent,expected_binding_sha256='b'*64)
    v['visible_dom']['rows'].append({'title_links':[{'text':b['metadata']['title']}]})
    with pytest.raises(HubError):validate_manager_before(v,b,state,intent,expected_binding_sha256='b'*64)


def test_versioned_cas_preserves_actual_reservation_no_new_submit_or_human_flag(causal_cas):
    f=causal_cas;o={**f['cas_observation'],'origin':ORIGIN};job_before=f['cas_job']
    _commit_later_result(f['service'],PRINCIPAL,job_before,f['cas_state'],f['cas_pubs'],f['cas_refs'],o,
        {'version':VERSION,'origin':ORIGIN,'same_submit_attempt_observed':False},{},recheck=lambda:None,
        version=VERSION,origin=ORIGIN)
    job=f['db'].get(f['job_id']);state=job['result'][f['service'].STATE]
    assert job['attempts']==3 and job['stage']==VERSION and job['payload']==job_before['payload']
    assert state['result_observation']['same_submit_attempt_observed'] is False and state['human_review'] is False
    assert len(f['db'].query("SELECT * FROM audit WHERE action='short_video_reserve_submit' AND target=?",(f['job_id'],)))==1
    assert len(f['db'].query('SELECT * FROM publications WHERE job_id=?',(f['job_id'],)))==1
