"""Owned subprocess lifecycle only; no browser, network or real publication."""
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from services.hub.errors import HubError
from services.hub.web_publish import _run_website


@pytest.fixture
def website_case(runtime):
    settings,store=runtime
    ident=store.enqueue('synthetic_lifecycle','owned-process-cleanup','owned_website','tweet',{},'owned-only')
    job=store.get(ident);calls=[]
    manifest={'source_url':'https://x.com/fixture_only/status/1','target_site_uid':'1','target_name':'Owned fixture',
        'render':{'png':str(settings.root/'media/owned-placeholder.png'),'sha256':'a'*64},
        'caption':'Owned fixture only','caption_sha256':'b'*64}
    service=SimpleNamespace(_snapshot=lambda _: (job,manifest),
        reserve=lambda *a,**k:calls.append('reserve') or {'can_click_publish':True},
        record_visible=lambda *a:calls.append('record') or {'state':'completed','owned_fixture':True})
    return settings,store,ident,service,calls


@pytest.mark.parametrize('outcome',['published_visible','owned_error'])
def test_result_file_allows_real_child_finally_to_remove_owned_lock_before_return(website_case,outcome,monkeypatch):
    settings,store,ident,service,calls=website_case
    adapter=settings.root/'adapters/browser_publisher.mjs';adapter.parent.mkdir()
    adapter.write_text("""
import fs from 'node:fs/promises';
import path from 'node:path';
const chunks=[];for await(const chunk of process.stdin)chunks.push(chunk);
const spec=JSON.parse(Buffer.concat(chunks).toString('utf8'));
const root=process.env.HUB_ROOT,lock=path.join(root,'data/owned-ui-lock');
const folder=path.join(root,'media',spec.job_id,'website-attempts',spec.attempt_id);
await fs.writeFile(lock,'Owned process lock, never an account profile');
try{
 const result=OUTCOME==='published_visible'?{state:OUTCOME,evidence:{owned_fixture:true}}:
  {state:'blocked',error_code:'OWNED_UI_ERROR'};
 await fs.writeFile(path.join(folder,'result.partial'),JSON.stringify(result));
 await fs.rename(path.join(folder,'result.partial'),path.join(folder,'result.json'));
}finally{
 await new Promise(resolve=>setTimeout(resolve,700));
 await fs.unlink(lock);
 await fs.writeFile(path.join(root,'data/owned-child-exited'),'Natural finally completed');
}
""".replace('OUTCOME',json.dumps(outcome)),encoding='utf-8')
    real_popen=subprocess.Popen
    def owned_popen(*args,**kwargs):
        if os.name=='nt':
            # Windows Node's CSPRNG needs its system directory. This owned
            # runtime shim does not change the production Linux child env.
            kwargs['env'].update({key:os.environ[key] for key in ('SYSTEMROOT','WINDIR') if key in os.environ})
        return real_popen(*args,**kwargs)
    monkeypatch.setattr('services.hub.web_publish.subprocess.Popen',owned_popen)
    started=time.monotonic()
    if outcome=='published_visible':
        assert _run_website(settings,store,ident,service,'owned-kind','owned-fixture')=={'state':'completed','owned_fixture':True}
        assert calls==['reserve','record']
    else:
        with pytest.raises(HubError,match='OWNED_UI_ERROR'):_run_website(settings,store,ident,service,'owned-kind','owned-fixture')
        assert not calls
    assert time.monotonic()-started>=.65
    assert not (settings.root/'data/owned-ui-lock').exists()
    assert (settings.root/'data/owned-child-exited').read_text()=='Natural finally completed'
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('stubborn',[False,True])
def test_cleanup_escalates_only_after_bounded_natural_then_term_wait(website_case,monkeypatch,stubborn):
    settings,store,ident,service,calls=website_case;events=[]
    class Input:
        def write(self,raw):
            spec=json.loads(raw);folder=settings.root/'media'/spec['job_id']/'website-attempts'/spec['attempt_id']
            (folder/'result.json').write_text(json.dumps({'state':'published_visible','evidence':{'owned_fixture':True}}),'utf-8')
        def close(self):pass
    class OwnedProcess:
        stdin=Input()
        def poll(self):return None
        def wait(self,timeout):
            events.append(('wait',timeout))
            if len(events)==1 or stubborn and len(events)==3:raise subprocess.TimeoutExpired('owned-only',timeout)
            return 0
        def terminate(self):events.append(('terminate',))
        def kill(self):events.append(('kill',))
    monkeypatch.setattr('services.hub.web_publish.subprocess.Popen',lambda *a,**k:OwnedProcess())
    assert _run_website(settings,store,ident,service,'owned-kind','owned-fixture')['owned_fixture']
    expected=[('wait',10),('terminate',),('wait',10)]
    if stubborn:expected.extend([('kill',),('wait',5)])
    assert events==expected
    assert calls==['reserve','record']
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')
