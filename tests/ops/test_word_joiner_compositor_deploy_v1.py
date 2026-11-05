"""Owned root-deployer checks: no Docker, network, actual DB or live mutation."""
from copy import deepcopy
import importlib.util,json
from pathlib import Path,PurePosixPath
from types import SimpleNamespace
from contextlib import contextmanager
import pytest

REPO=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('owned_word_joiner_deploy',REPO/'scripts/ops/deploy_word_joiner_compositor_v1.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(m,'ROOT',PurePosixPath('/opt/lighthouse-hub'))
    monkeypatch.setattr(m,'INSTALL',PurePosixPath('/usr/local/lib/lighthouse-compositor-word-joiner-v1'))

def baseline():
    common={'image':m.CBC,'restart':'unless-stopped','user':'10001:10001','init':True,'environment':{'OWNED_ONLY':'must-not-log'},
        'volumes':[{'type':'bind','source':'/opt/lighthouse-hub/data','target':'/opt/lighthouse-hub/data'}]}
    value={'name':'lighthouse-hub','services':{key:deepcopy(common) for key in (m.SERVICE,*m.OTHER)}}
    value['services']['video-worker']['image']='sha256:'+'9'*64
    value['services']['video-worker']['volumes'].append({'type':'bind','source':'/owned-video.py','target':'/opt/lighthouse-hub/video.py','read_only':True})
    value['services']['api']['ports']=[{'host_ip':'127.0.0.1','published':'8080','target':8080}]
    return value

def test_canonical_json_changes_only_one_tweet_readonly_core_bind(posix):
    before=baseline();after=json.loads(m.mutate(m.wire(before)));m.assert_delta(before,after)
    assert all(before['services'][name]==after['services'][name] for name in m.OTHER)
    assert after['services']['tweet-worker']['image']==m.CBC
    assert after['services']['tweet-worker']['volumes'][-1]==m.mount()

@pytest.mark.parametrize('change',['image','environment','ports','restart','video','readwrite','wrong_source','create_path','extra'])
def test_effective_delta_rejects_any_unrelated_or_unsafe_mutation(posix,change):
    before=baseline();after=json.loads(m.mutate(m.wire(before)));tweet=after['services']['tweet-worker'];added=tweet['volumes'][-1]
    if change=='video':after['services']['video-worker']['image']=m.CBC
    elif change=='readwrite':added['read_only']=False
    elif change=='wrong_source':added['source']='/etc/owned-invalid'
    elif change=='create_path':added['bind']['create_host_path']=True
    elif change=='extra':tweet['volumes'].append({'type':'bind','source':'/invalid','target':'/invalid'})
    else:tweet[change]='owned invalid change'
    with pytest.raises(AssertionError):m.assert_delta(before,after)

def test_duplicate_core_mount_is_not_added_twice(posix):
    once=m.mutate(m.wire(baseline()))
    with pytest.raises(AssertionError):m.mutate(once)

def test_original_yaml_inherited_mounts_are_copied_only_into_tweet_service(posix):
    raw=(REPO/'compose.yaml').read_bytes();out=m.mutate(raw)
    for name in m.OTHER:
        def section(text):
            start=text.index('  '+name+':');finish=text.find('\n  ',start+3)
            while finish!=-1 and text[finish+1:finish+5]=='    ':finish=text.find('\n  ',finish+3)
            return text[start:finish if finish!=-1 else len(text)]
        assert section(raw.decode())==section(out.decode())
    assert out.count(b'lighthouse-compositor-word-joiner-v1/core.mjs')==1
    assert out.count(b'"read_only":true')==raw.count(b'"read_only":true')+1
    assert b'    volumes:' in out[out.index(b'  tweet-worker:'):out.index(b'  video-worker:')]

@pytest.fixture
def evidence(tmp_path,monkeypatch):
    import uuid
    ev=tmp_path/'ev';folder=ev/('word-joiner-cbc-'+str(uuid.uuid4()));folder.mkdir(parents=True)
    manifest={'version':'word-joiner-cbc-test-overlay-v1','image':m.CBC,'node_tests':25,'api_internal_cases':48,
        'files':{name:{'sha256':m.sha((REPO/name).read_bytes()),'bytes':(REPO/name).stat().st_size} for name in m.FILES},
        'protected':{name:{'sha256':m.sha((REPO/name).read_bytes()),'bytes':(REPO/name).stat().st_size} for name in m.PROTECTED}}
    for name in m.FILES:
        file=folder/'overlay'/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes((REPO/name).read_bytes())
    owned=Path('/home/lighthouse-ops')/('lighthouse-word-joiner-owned-'+folder.name.removeprefix('word-joiner-cbc-'))
    junit=b'<testsuites><testsuite><testcase classname="tests.test_official_api_render" name="owned"/></testsuite></testsuites>'
    log=b'# tests 25\n# pass 25\n# fail 0\n# skipped 0\n# cancelled 0\n'
    result={'version':'word-joiner-cbc-owned-results-v1','passed':True,'npm_exit_code':0,'api_pytest_exit_code':0,
        'node_tests':25,'api_pytest_tests':1,'api_internal_cases_asserted':48,'api_junit_clean':True,'no_pid_or_oom_denial':True,
        'all_source_and_protected_hashes_verified':True,'npm_footer_checks':{k:True for k in ('tests','pass','fail','skipped','cancelled')}}
    a={'version':'word-joiner-cbc-ubuntu-tests-v1','image':m.CBC,'manifest':manifest,'result':result,'full_suite_passed':True,
        'network':'none','real_data_mounted':False,'production_code_deployed':False,'exit_code':0,'oom_killed':False,
        'source_and_font_protections_verified':True,'mount_allowlist_verified':True,'runtime_verified':True,
        'owned':str(owned),'api_junit_sha256':m.sha(junit),'node_log_sha256':m.sha(log)}
    file=folder/'test-attestation.json';file.write_bytes(m.wire(a));original=m.read
    def read(path,*args):
        if str(path)==str(owned/'api-results.xml'):return junit
        if str(path)==str(owned/'npm-tests.log'):return log
        return original(path,*args)
    monkeypatch.setattr(m,'EV',ev);monkeypatch.setattr(m,'read',read)
    return SimpleNamespace(file=file,a=a,junit=junit,log=log)

def test_real_nested_attestation_contract_reopens_bound_core_log_and_junit(evidence):
    body,proof,protected=m.attest(evidence.file,m.sha(evidence.file.read_bytes()))
    assert m.sha(body)==m.NEW and proof['node_tests']==25 and proof['api_internal_cases']==48 and set(protected)==m.PROTECTED

@pytest.mark.parametrize('change',['wrong_image','missing_font','fake_counts','network','oom','api_case_failed','wrong_log_sha','true_as_int','wrong_core'])
def test_attestation_gate_rejects_incomplete_or_forged_acceptance(evidence,change):
    e=evidence;a=deepcopy(e.a)
    if change=='wrong_image':a['image']='sha256:'+'9'*64
    elif change=='missing_font':a['manifest']['protected'].pop('compositor/assets/fonts/NotoColorEmoji.ttf')
    elif change=='fake_counts':a['result']['node_tests']=24
    elif change=='network':a['network']='bridge'
    elif change=='oom':a['oom_killed']=True
    elif change=='api_case_failed':a['result']['api_junit_clean']=False
    elif change=='wrong_log_sha':a['node_log_sha256']='0'*64
    elif change=='true_as_int':a['result']['npm_footer_checks']['pass']=1
    elif change=='wrong_core':a['manifest']['files'][m.SOURCE]['sha256']='0'*64
    e.file.write_bytes(m.wire(a))
    with pytest.raises(AssertionError):m.attest(e.file,m.sha(e.file.read_bytes()))

def test_inspect_filter_never_contains_effective_environment():
    value={'Id':'a'*64,'Image':m.CBC,'State':{'Running':True,'StartedAt':'owned','Pid':123},'RestartCount':0,
        'Config':{'User':'10001:10001','Env':['OWNED_SENTINEL=do-not-copy']},'Mounts':[]}
    out=m.filtered(value);assert 'OWNED_SENTINEL' not in json.dumps(out) and 'Env' not in json.dumps(out)


@pytest.fixture
def flow(tmp_path,monkeypatch):
    root=tmp_path/'root';(root/'config').mkdir(parents=True);(root/'config/hub.toml').write_bytes(b'owned=true\n')
    monkeypatch.setattr(m,'ROOT',root);monkeypatch.setattr(m,'INSTALL',tmp_path/'install');monkeypatch.setattr(m,'STATE',tmp_path/'state')
    before=baseline();original=m.wire(before);(root/'compose.yaml').write_bytes(original)
    state=SimpleNamespace(locked=False,running=True,calls=[],ready_calls=0,fail_ready=False,fail_write=False)
    monkeypatch.setattr(m,'attest',lambda *args:(b'owned tested core',{'owned':True},{}))
    monkeypatch.setattr(m,'parsed',lambda path=None:json.loads((path or root/'compose.yaml').read_bytes()))
    monkeypatch.setattr(m,'run',lambda *args:m.CBC+'\n')
    containers={name:{'id':name,'running':True} for name in m.OTHER}
    monkeypatch.setattr(m,'others',lambda:deepcopy(containers));monkeypatch.setattr(m,'cid',lambda name:name)
    monkeypatch.setattr(m,'inspect',lambda ident:{'State':{'Running':state.running}})
    monkeypatch.setattr(m,'probe',lambda ident,proof,expected:({'core':expected,'container':ident}))
    monkeypatch.setattr(m,'directory',lambda p,mode=0o700:p.mkdir(parents=True,exist_ok=True))
    monkeypatch.setattr(m,'protected',lambda p:None)
    def atomic(path,raw,**kwargs):
        if path==root/'compose.yaml' and raw!=original and state.fail_write:
            state.fail_write=False;raise OSError('owned pre-replace failure')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    monkeypatch.setattr(m,'atomic',atomic)
    @contextmanager
    def lock():
        assert not state.locked;state.locked=True;state.calls.append('lock')
        try:yield
        finally:state.locked=False;state.calls.append('unlock')
    monkeypatch.setattr(m,'tweet_lock',lock)
    monkeypatch.setattr(m,'dbstate',lambda **kwargs:{'processing':[],'heartbeat':123,'paused':0})
    def compose(*args,**kwargs):
        assert args[0]=='stop' and args[-1]==m.SERVICE and state.locked
        state.calls.append('stop-tweet');state.running=False;return ''
    monkeypatch.setattr(m,'compose',compose)
    def up():
        assert not state.locked,'New Worker heartbeat must never wait behind this deployer queue lock'
        state.calls.append('up-tweet');state.running=True
    monkeypatch.setattr(m,'up',up)
    def ready(start):
        assert not state.locked;state.ready_calls+=1
        if state.fail_ready and state.ready_calls==1:raise RuntimeError('owned fresh-heartbeat failure')
        state.calls.append('ready');return {'ready':True,'heartbeat':start+1}
    monkeypatch.setattr(m,'wait_ready',ready)
    state.args=SimpleNamespace(attestation_file=str(tmp_path/'owned-attestation.json'),attestation_sha256='a'*64,activate=False)
    state.root=root;state.original=original
    return state


def test_default_plan_never_stops_installs_or_changes_compose(flow):
    assert m.deploy(flow.args)==0
    assert flow.calls==[] and (flow.root/'compose.yaml').read_bytes()==flow.original
    assert not m.INSTALL.exists() and not m.STATE.exists()


def test_activation_releases_queue_lock_before_only_tweet_up_and_heartbeat(flow):
    flow.args.activate=True;assert m.deploy(flow.args)==0
    assert flow.calls==['lock','stop-tweet','unlock','up-tweet','ready']
    assert (m.INSTALL/'core.mjs').read_bytes()==b'owned tested core'
    after=json.loads((flow.root/'compose.yaml').read_bytes());m.assert_delta(json.loads(flow.original),after)


@pytest.mark.parametrize('failure',['before_replace','after_start'])
def test_failed_activation_rolls_back_exact_compose_without_touching_other_workers(flow,failure):
    flow.args.activate=True
    flow.fail_write=failure=='before_replace';flow.fail_ready=failure=='after_start'
    assert m.deploy(flow.args)==2
    assert (flow.root/'compose.yaml').read_bytes()==flow.original and flow.running is True
    assert flow.calls.count('stop-tweet')==2 and flow.calls[-2:]==['up-tweet','ready']
    reports=list(m.STATE.glob('*/deployment-report.json'));assert len(reports)==1
    report=json.loads(reports[0].read_bytes());assert report['rollback_verified'] is True and report['database_written_by_helper'] is False
