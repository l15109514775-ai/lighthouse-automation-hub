"""Real SQLite/Worker/CAS persistence; raw browser input is an owned fixture.

No source/media QA, account, live draft, upload or submission is claimed here.
Native DOM traversal is independently exercised by the Chromium fixture.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,uuid

import pytest

from services.hub import replay_part_moderation_v5 as mod
from services.hub.replay_parts_v5 import STATE
from test_replay_part_moderation_v5 import lookup,schedule,parent_snapshot


def evidence(f,*,source=None):
    parent=f.db.get(f.parent);binding=parent['payload'];state=parent['result'][STATE]
    common={**f.lookup._native_base(parent,binding,state),'file_selections':0,'save_clicks':0,'submit_clicks':0,'player_seeks':0,'source_or_media_modified':False}
    folder=f.s.root/'media'/f.parent/'replay-browser-part-v5'/state['attempt_id']/'later-lookups-v2'/str(uuid.uuid4())
    folder.mkdir(parents=True);now=datetime.now(timezone.utc).isoformat()
    def write(name,value):
        raw=value if isinstance(value,bytes) else (json.dumps(value,ensure_ascii=False)+'\n').encode()
        file=folder/name;file.write_bytes(raw)
        return {'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    identity={'site_uid':binding['site_uid'],'profile_href':'https://space.bilibili.com/'+binding['site_uid']+'/','url':'https://t.bilibili.com/','observed_at':now}
    url='https://member.bilibili.com/platform/upload/video/frame?type=draft&draftId=4069503';title=binding['metadata']['title']
    manager=write('manager-row.json',{'version':'replay-native-manager-row-v2',**common,'observed_at':now,'selected_row':None,'known_external_id':None,'visible_dom':{'truncated':False,'declared_all_count':0,'rows':[]}})
    row={'titles':[{'title':title,'links':[{'text':title,'href':url}]}],'covers':[{'href':url,'alts':[title]}],'saved_times':['2026年11月03日 05:26:15']}
    drafts=write('draft-manager.json',{'version':'replay-part-native-draft-manager-v5',**common,'observed_at':now,'account_observation':identity,'declared_draft_count':1,
        'visible_dom':{'url':'https://member.bilibili.com/platform/upload-manager/article?group=draft&page=1','section_count':1,'truncated':False,'rows':[row],
        'pagination':[' 共1页 / 1个， '],'selected_pages':['1']},'screenshot':write('draft-manager.png',b'owned PNG bytes seam'),'read_only':True})
    editor=write('draft-editor.json',{'version':'replay-part-native-draft-editor-v5',**common,'observed_at':now,'account_observation':identity,
        'visible_dom':{'url':url,'title':title,'source_url':source or binding['source_url']},'screenshot':write('draft-editor.png',b'owned PNG bytes seam'),'read_only':True})
    body={'version':'replay-part-native-draft-observation-v5',**common,'state':'draft_found','observed_at':now,'draft_id':'4069503','draft_url':url,'saved_at_text':row['saved_times'][0],
        'manager_observation':manager,'draft_manager_observation':drafts,'draft_editor_observation':editor,'public_accessible':False,'server_file_hash_verified':False,'human_review':False,
        'read_only':True,'publication_authorized':False,'navigation_actions':{'draft_tab_clicks':1,'draft_editor_navigations':1}}
    return body,write('draft-observation.json',body)


def test_normal_lookup_worker_persists_draft_location_without_completing_or_resending(lookup):
    f=lookup;before,pubs=parent_snapshot(f);intent=f.intent.read_bytes();body,ref=evidence(f)
    class Browser:
        @contextmanager
        def session(self,manifest,*,assert_lease,before_launch):
            assert_lease();before_launch()
            class Session:
                def operation(self,name,**kwargs):
                    assert name=='lookup' and kwargs=={};assert_lease()
                    return {'state':'unknown','native_result_evidence':{'state':'draft_found','draft_observation':body,'draft_reference':ref}}
            yield Session()
    f.worker=mod.build_lookup_worker(f.s,f.db,service=f.lookup,browser=Browser(),manifest_builder=f.manifest_builder)
    child=schedule(f)['scheduled'][0];assert f.worker.once('moderation')
    current=f.db.get(f.parent)
    assert f.db.get(child)['state']=='completed' and current['state']==before['state']=='publish_unknown'
    assert current['attempts']==before['attempts'] and current['payload']==before['payload'] and current['error_code']==before['error_code']
    assert current['result'][STATE]['state']==before['result'][STATE]['state']=='submit_unknown'
    assert current['result'][STATE]['draft_observation']==body and current['result'][STATE]['draft_observation_reference']==ref
    assert f.db.query('SELECT * FROM publications WHERE job_id=?',(f.parent,))==pubs and f.intent.read_bytes()==intent
    row=f.db.query('SELECT * FROM '+mod.TABLE)[0]
    assert row['status']=='draft_found' and row['next_check'] is None and json.loads(row['record'])['draft_id']=='4069503'
    assert schedule(f)['scheduled']==[] and len(f.db.query('SELECT * FROM '+mod.ATTEMPTS))==1


@pytest.mark.parametrize('change',['source','tampered_ref','lost_lease'])
def test_changed_source_raw_bytes_or_lookup_lease_cannot_persist_draft(lookup,monkeypatch,change):
    f=lookup;before=parent_snapshot(f);body,ref=evidence(f,source='https://www.youtube.com/watch?v=foreign0001' if change=='source' else None)
    child=schedule(f)['scheduled'][0];job=mod.QueueView(f.db).claim('moderation',600,4);assert job['job_id']==child
    if change=='tampered_ref':
        from pathlib import Path
        Path(body['draft_editor_observation']['file']).write_bytes(b'changed')
    if change=='lost_lease':
        original=mod.reference_body;calls=[]
        def steal(*args,**kwargs):
            result=original(*args,**kwargs);calls.append(1)
            if len(calls)==2:
                with f.db.transaction() as db:db.execute('UPDATE jobs SET lease_owner=? WHERE job_id=?',('foreign-owner',child))
            return result
        monkeypatch.setattr(mod,'reference_body',steal)
    with pytest.raises((mod.HubError,RuntimeError)):f.lookup.record_draft_lookup(job,body,result_reference=ref)
    assert parent_snapshot(f)==before and f.lookup._lookup_job is None
