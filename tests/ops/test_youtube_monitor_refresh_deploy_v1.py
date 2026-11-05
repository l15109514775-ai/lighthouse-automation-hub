"""Owned incremental deployment fixtures. No child process, network or live DB."""
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
from pathlib import Path,PurePosixPath
from types import SimpleNamespace
import uuid

import pytest

REPO=Path(__file__).resolve().parents[2]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

m=load('monitor_refresh_deploy_owned',REPO/'scripts/ops/deploy_youtube_monitor_refresh_v1.py')
previous=load('pilot_deploy_fixture_only',REPO/'scripts/ops/deploy_youtube_acceptance_pilot_v1.py')
previous.ROOT=PurePosixPath('/opt/lighthouse-hub');previous.INSTALL=PurePosixPath('/usr/local/lib/lighthouse-video-pilot-v1')
PILOT_TEXT=previous.mutate_compose((REPO/'compose.yaml').read_bytes())


def baseline():
    common={'image':'lighthouse-hub:0.1.0','restart':'unless-stopped','environment':{'OWNED_ONLY':'never-log-effective-env'},
        'volumes':[{'type':'bind','source':str(m.ROOT/'data'),'target':str(m.ROOT/'data')}]}
    services={s:deepcopy(common) for s in ('api','system-worker','video-worker','scheduler','tweet-worker')}
    services['api']['ports']=[{'host_ip':'127.0.0.1','target':8080,'published':'8080'}]
    for name in m.SERVICES:
        for source in m.OLD:
            services[name]['volumes'].append({'type':'bind','source':str(m.PILOT/Path(source).name),
                'target':str(m.ROOT/source),'read_only':True,'bind':{'create_host_path':False}})
    services['video-worker']['volumes'].insert(1,{'type':'bind','source':str(m.ROOT/'data/browser-profiles/youtube'),
        'target':str(m.ROOT/'data/browser-profiles/youtube'),'read_only':True})
    return {'name':'lighthouse-hub','services':services}


def extended(before):
    value=deepcopy(before)
    for name in m.SERVICES:value['services'][name]['volumes'].append(m.monitor_mount())
    return value


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(m,'ROOT',PurePosixPath('/opt/lighthouse-hub'))
    monkeypatch.setattr(m,'PILOT',PurePosixPath('/usr/local/lib/lighthouse-video-pilot-v1'))
    monkeypatch.setattr(m,'INSTALL',PurePosixPath('/usr/local/lib/lighthouse-youtube-monitor-v1'))


def test_adds_only_one_ro_monitor_for_each_service_and_keeps_existing_two_binds(posix):
    out=m.mutate_compose(PILOT_TEXT)
    assert out.count(b'/usr/local/lib/lighthouse-youtube-monitor-v1/youtube_monitor.py')==3
    assert out.count(b'"read_only":true')==PILOT_TEXT.count(b'"read_only":true')+3
    for old in (b'lighthouse-video-pilot-v1/config.py',b'lighthouse-video-pilot-v1/youtube_download.py',
                b'./data/browser-profiles/youtube:/opt/lighthouse-hub/data/browser-profiles/youtube:ro'):
        assert out.count(old)==PILOT_TEXT.count(old)
    before=baseline();m.assert_delta(before,extended(before))


@pytest.mark.parametrize('field',['image','restart','environment','ports','profile','old_source','other_service'])
def test_effective_config_rejects_every_unrelated_change(posix,field):
    before=baseline();after=extended(before)
    if field=='profile':after['services']['video-worker']['volumes'][1]['read_only']=False
    elif field=='old_source':after['services']['api']['volumes'][1]['source']='/untrusted/changed.py'
    elif field=='other_service':after['services']['scheduler']['restart']='no'
    else:after['services']['api'][field]='owned incorrect field'
    with pytest.raises(AssertionError):m.assert_delta(before,after)


def test_second_run_refuses_duplicate_monitor_binding(posix):
    with pytest.raises(AssertionError):m.mutate_compose(m.mutate_compose(PILOT_TEXT))


def test_missing_existing_pilot_sources_rejected_before_deployment(posix):
    before=baseline();before['services']['api']['volumes']=before['services']['api']['volumes'][:1]
    with pytest.raises(AssertionError):m.assert_delta(before,extended(before))


@pytest.fixture
def receipt(tmp_path,monkeypatch):
    ev=tmp_path/'evidence';folder=ev/('youtube-monitor-publication-overlay-v1-'+str(uuid.uuid4()));folder.mkdir(parents=True)
    target=folder/'overlay'/m.SOURCE;target.parent.mkdir(parents=True);target.write_bytes((REPO/m.SOURCE).read_bytes())
    value={'version':'youtube-monitor-publication-overlay-ubuntu-tests-v1','image':m.TEST_IMAGE,
        'tests':105,'full_suite_passed':True,'groups':m.GROUPS,'failures':0,'errors':0,'skipped':0,'exit_code':0,'oom_killed':False,
        'network':'none','application_data_mounted':False,'whole_tree_deployed':False,'runtime_readonly_overlays_only':True,
        'runtime_matches_production':True,'no_pid_or_oom_denial':True,'real_uploads':0,'publications':0,
        'manifest':{'version':'youtube-monitor-publication-ro-overlay-v1','base_image':m.TEST_IMAGE,'expected_total_tests':105,
            'expected_test_counts':m.GROUPS,'files':{n:{'sha256':d} for n,d in {**m.OLD,m.SOURCE:m.DIGEST}.items()}}}
    value['manifest']['files'].update({n:{'sha256':'owned-fixture'} for n in m.GROUPS})
    path=folder/'test-attestation.json';path.write_bytes(m.wire(value));monkeypatch.setattr(m,'EV',ev)
    return path,value,target


def test_requires_actual_105_attestation_and_reads_exact_tested_source(receipt):
    path,_,_=receipt;body,record=m.attest(path,m.sha(path.read_bytes()))
    assert m.sha(body)==m.DIGEST and record['tests']==105


@pytest.mark.parametrize('change',['count','hash','network','oom','source'])
def test_wrong_attestation_or_changed_source_rejected(receipt,change):
    path,value,source=receipt
    if change=='count':value['tests']=104
    elif change=='network':value['network']='bridge'
    elif change=='oom':value['oom_killed']=True
    elif change=='source':source.write_bytes(source.read_bytes()+b'\n# owned changed source\n')
    path.write_bytes(m.wire(value));digest=m.sha(path.read_bytes()) if change!='hash' else '0'*64
    with pytest.raises(AssertionError):m.attest(path,digest)


@pytest.fixture
def owned_deploy(tmp_path,monkeypatch):
    root=tmp_path/'hub';(root/'config').mkdir(parents=True)
    original=root/'compose.yaml';original.write_bytes(PILOT_TEXT)
    fixed={root/'config/hub.toml':b'youtube_download_enabled=true\nyoutube_download_acceptance_only=true\n',
        root/'config/youtube-download-policy.toml':b'acceptance_video_ids=["9T5tn1WGlJg"]\nmax_bytes=3800000000\n'}
    for p,body in fixed.items():p.write_bytes(body)
    monkeypatch.setattr(m,'ROOT',root);monkeypatch.setattr(m,'INSTALL',tmp_path/'install')
    monkeypatch.setattr(m,'attest',lambda *a:(b'owned tested monitor',{'tests':105}))
    monkeypatch.setattr(m,'install',lambda body:None)
    monkeypatch.setattr(m,'worker_locks',lambda:nullcontext())
    before=baseline();after=extended(before)
    monkeypatch.setattr(m,'parsed',lambda p:before if p.read_bytes()==PILOT_TEXT else after)
    monkeypatch.setattr(m,'run',lambda *a,**k:m.CBC+'\n')
    state={'active':[],'runnable':[],'short_record_hashes':{'owned-short':'unchanged'},'download_job_count':3}
    monkeypatch.setattr(m,'dbstate',lambda **kw:deepcopy(state))
    monkeypatch.setattr(m,'probe_services',lambda:{'owned':'real probes mocked only in this test'})
    monkeypatch.setattr(m,'ready',lambda:{'status':'ready','stale_services':[]})
    calls=[];monkeypatch.setattr(m,'compose',lambda *a,**kw:calls.append(a) or '')
    def write(p,body,**kw):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
    monkeypatch.setattr(m,'atomic',write)
    args=SimpleNamespace(attestation_file=str(tmp_path/'owned-attestation.json'),attestation_sha256='1'*64,deploy=True)
    return root,original,fixed,calls,write,args


def test_plan_does_not_modify_policy_counts_or_services(owned_deploy):
    root,original,fixed,calls,_,args=owned_deploy;args.deploy=False
    assert m.deploy(args)==0 and not calls and original.read_bytes()==PILOT_TEXT
    assert all(p.read_bytes()==body for p,body in fixed.items())


def test_success_preserves_three_existing_download_jobs_and_all_operating_config(owned_deploy):
    root,original,fixed,calls,_,args=owned_deploy
    assert m.deploy(args)==0 and [c[0] for c in calls]==['stop','up']
    assert calls[0][1:]==('api','system-worker','video-worker')
    assert '--no-build' in calls[1] and calls[1][-3:]==('api','system-worker','video-worker')
    assert all(p.read_bytes()==body for p,body in fixed.items())
    manifest=json.loads((m.INSTALL/'manifest.json').read_bytes())
    assert manifest['protected_database_state']['download_job_count']==3
    assert manifest['job_queued'] is manifest['database_written'] is manifest['operating_policy_changed'] is False


@pytest.mark.parametrize('failure',['partial_stop','after_compose_rename'])
def test_failure_rolls_back_only_compose_and_restarts_preserved_pilot(owned_deploy,monkeypatch,failure):
    root,original,fixed,calls,write,args=owned_deploy;once=[False]
    if failure=='partial_stop':
        def compose(*a,**kw):
            calls.append(a)
            if a[0]=='stop' and not once[0]:once[0]=True;raise RuntimeError('owned partial stop')
            return ''
        monkeypatch.setattr(m,'compose',compose)
    else:
        def atomic(p,body,**kw):
            write(p,body,**kw)
            if p==original and body!=PILOT_TEXT and not once[0]:once[0]=True;raise OSError('owned after rename failure')
        monkeypatch.setattr(m,'atomic',atomic)
    assert m.deploy(args)==2 and once[0] and original.read_bytes()==PILOT_TEXT
    assert all(p.read_bytes()==body for p,body in fixed.items())
    report=json.loads(next((root/'backups/youtube-monitor-deploy').glob('*/deployment-report.json')).read_bytes())
    assert report['rollback_verified'] is True and len([c for c in calls if c[0]=='up'])==1
