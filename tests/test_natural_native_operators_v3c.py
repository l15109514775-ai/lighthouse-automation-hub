"""Owned host/source boundaries only: no Docker, platform, live DB or keys.

Root filesystem and prior raw-JUnit qualification are explicit upstream
boundaries here. Real production qualification remains the distinct345 proof.
"""
from copy import deepcopy
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import importlib.util
import json
import pytest

REPO=Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location(name,REPO/'scripts/ops'/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

d=load('deploy_natural_replay_v3c_native')
c=load('issue_natural_candidate_v3c_native')


def target_fixture(monkeypatch):
    new={n:{'sha256':s,'bytes':1} for n,s in d.TARGET_RUNTIME_PINS.items()}
    for n,s in d.TARGET_FROM_295.items():new[n]={'sha256':s,'bytes':1}
    old=deepcopy(new)
    for n in d.TARGET_FROM_295:old.pop(n)
    for n in set(d.TARGET_FROM_295)&set(d.TARGET_RUNTIME_PINS):old[n]={'sha256':'owned-old-'+n,'bytes':1}
    manifest={'files':new,'parent_archive_sha256':d.TARGET_PARENT,'parent_expected_total_tests':340,
        'navigation_source_commit':d.NAV_COMMIT,'python_source_commit':d.PY_COMMIT,'native_source_commit':d.UI_COMMIT,
        'production_runtime_changed_files':[d.NAV],'navigation_source_before_sha256':d.NAV_OLD,
        'navigation_source_after_sha256':d.NAV_SHA,'guard_changed':False,'browser_driver_changed':False,
        'production_deployed':False,'native_freshness_seconds_unchanged':300,
        'source_current_full_sha_required_each_call':True,'natural_sources_installed':True,
        'runtime_source_sha256':deepcopy(d.TARGET_RUNTIME_PINS)}
    raw=b'Owned prior immutable manifest boundary; not production qualification.'
    monkeypatch.setattr(d,'TARGET_MANIFEST_SHA',d.digest(raw))
    native={'manifest':manifest,'groups':deepcopy(d.TARGET_GROUPS),'_attestation_file':'/owned/test-attestation.json',
        'mounted_sources_match_manifest':True,'host_source_sha256_unchanged':True,'timed_out':False,
        'mounted_source_sha256_before':{n:e['sha256'] for n,e in new.items()},
        'mounted_source_sha256_after':{n:e['sha256'] for n,e in new.items()}}
    host=SimpleNamespace(read=lambda *args:raw,runtime_source=lambda n:n in d.TARGET_RUNTIME_PINS)
    return old,native,host


def test_exact345_identity_keeps_all57_sources_and_five_qualified_production_changes(monkeypatch):
    old,native,host=target_fixture(monkeypatch)
    result=d.target_sources(old,native,host)
    assert result is native['manifest']['files']
    assert len(d.TARGET_RUNTIME_PINS)==57 and d.SPECS['native'][4:6]==(345,124)
    assert sum(d.TARGET_GROUPS.values())==345
    assert len(set(d.TARGET_FROM_295)&set(d.TARGET_RUNTIME_PINS))==5
    assert d.SPECS['cache'][4]==295 and d.SPECS['native'][0]!=d.SPECS['cache'][0]


@pytest.mark.parametrize('change',['hidden_runtime','wrong_parent','old_340_only','wrong_group','old_navigation',
    'guard_change','source_hash_gate_off','stale_window','missing_source','source_changed_after_qualification'])
def test_an_unqualified_source_or_relaxed_freshness_cannot_hide_behind345(monkeypatch,change):
    old,value,host=target_fixture(monkeypatch);m=value['manifest']
    if change=='hidden_runtime':m['files']['services/hub/config.py']['sha256']='unqualified'
    elif change=='wrong_parent':m['parent_archive_sha256']='a'*64
    elif change=='old_340_only':m['parent_expected_total_tests']=295
    elif change=='wrong_group':value['groups']['tests/test_saved_draft_navigation_wait.py']=0
    elif change=='old_navigation':m['navigation_source_after_sha256']=d.NAV_OLD
    elif change=='guard_change':m['guard_changed']=True
    elif change=='source_hash_gate_off':m['source_current_full_sha_required_each_call']=False
    elif change=='stale_window':m['native_freshness_seconds_unchanged']=600
    elif change=='missing_source':m['files'].pop('services/hub/replay_natural_launch_v3.py')
    else:value['mounted_source_sha256_after']['services/hub/replay_video_web.py']='changed'
    with pytest.raises(AssertionError):d.target_sources(old,value,host)


def test_runtime_delta_is_not_misreported_as_only_navigation():
    value=d.describe_runtime_delta(d.CURRENT_RUNTIME_PINS,d.TARGET_RUNTIME_PINS)
    assert (value['current_count'],value['target_count'])==(45,57)
    assert len(value['shared_source_changes'])==11 and len(value['added_sources'])==13 and len(value['removed_sources'])==1
    assert value['navigation_already_matches'] is True
    assert 'adapters/replay_native_driver.mjs' in value['shared_source_changes']
    assert 'adapters/browser_replay_video.mjs' in value['shared_source_changes']
    assert set(value['removed_sources'])=={'services/hub/replay_highest_moderation_v2b.py'}


def test_current_source_bytes_and_in_container_readback_are_both_required(tmp_path,monkeypatch):
    root=PurePosixPath('/owned/root');install=tmp_path/'install';code={};files={}
    for n in d.CURRENT_RUNTIME_PINS:
        raw=('Owned fixture source '+n).encode();file=install/'code'/n;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(raw)
        code[n]=d.digest(raw);files[n]=file
    monkeypatch.setattr(d,'CURRENT_RUNTIME_PINS',code)
    monkeypatch.setattr(d,'NAV_OLD',d.digest(b'Owned original nav source'))
    monkeypatch.setattr(d,'protected',lambda p:None) # Only root-owner boundary is mocked.
    repaired={**code,d.NAV:d.NAV_OLD};old=deepcopy(repaired)
    changed=set(d.TARGET_FROM_295)&set(code)-{d.NAV}
    for n in changed:old[n]='owned-pre-repair'
    service={'command':['python','-m','services.hub.replay_highest_moderation_v2b'],
        'volumes':[{'type':'bind','target':str(root/n),'source':str(files[n]),'read_only':True} for n in code]}
    before={'services':{'video-worker':service}};seen=[]
    host=SimpleNamespace(ROOT=root,INSTALL=install,runtime_source=lambda n:n in code,read=lambda p,*a:Path(p).read_bytes(),
        run=lambda argv:(seen.append(argv) or json.dumps({'persistent_runtime_environment_directories_verified':True} if argv[-1].startswith('import json,os') else code)))
    # Production VERIFY_ENV is an explicit prior-tested transport boundary.
    monkeypatch.setattr(d,'verify_runtime_environment',lambda *a:seen.append('verified_environment'))
    operator=SimpleNamespace(validate_running=lambda *a:seen.append('strict_running_validator'))
    args=(host,before,{'Id':'owned-container'},{},operator,{'manifest':{'candidate_runtime_source_sha256':old}},
        {'manifest':{'runtime_source_sha256':repaired}},{'manifest':{'runtime_source_sha256':code}})
    d.current_gate(*args)
    assert seen[0]=='strict_running_validator' and seen[-1]=='verified_environment'
    first=next(iter(files));files[first].write_bytes(b'Changed real file after the proof')
    with pytest.raises(AssertionError):d.current_gate(*args)


def test_pinned_tweet_v4_layout_is_observed_without_reverting_other_services(tmp_path,monkeypatch):
    monkeypatch.setattr(d,'protected',lambda p:None)
    raw=b'Owned qualified scheduler';monkeypatch.setattr(d,'SCHEDULER_SHA',d.digest(raw))
    root=tmp_path/'root';source=tmp_path/'scheduler.py';source.write_bytes(raw)
    before={'services':{'api':{'environment':{'HUB_CONFIG':str(root/'config/tweet-runtime-v4.toml')}},
        'tweet-worker':{'environment':{'HUB_CONFIG':str(root/'config/tweet-runtime-v4.toml')}},
        'scheduler':{'volumes':[{'type':'bind','target':str(root/'scheduler/service.py'),'source':str(source),'read_only':True}]}}}
    saved=deepcopy(before);host=SimpleNamespace(ROOT=root,read=lambda p,*a:Path(p).read_bytes())
    assert d.preserved_tweet_layout(host,before)['other_service_objects_preserved'] is True
    assert before==saved
    before['services']['api']['environment']['HUB_CONFIG']=str(root/'config/hub.toml')
    with pytest.raises(AssertionError):d.preserved_tweet_layout(host,before)


def test_same_pilot_issuer_body_preserves_real_evidence_and_no_network_no_queue_fences():
    old=load('issue_natural_candidate_v3b_cache')
    assert c.ISSUE==old.ISSUE and c.PILOT==old.PILOT
    assert c.INSTALLER_SHA==d.digest(Path(d.__file__).read_bytes())
    assert c.DESTINATION!=old.DESTINATION
    assert 'actual_owner_launch_response=auth' in c.ISSUE
    assert 'Store.claim=Store.recover=Store.enqueue=Store.finish=refuse' in c.ISSUE
    assert "Store('/run/issuer-db/hub.sqlite3')" in c.ISSUE and 'db.get(pilot)==prior' in c.ISSUE


def test_known_producer_starvation_blocks_issue_and_activation_without_an_override_flag():
    assert d.KNOWN_ACTIVATION_BLOCKERS==('NATURAL_PRODUCER_HISTORY_WINDOW_STARVATION',)
    source=Path(d.__file__).read_text();issuer=Path(c.__file__).read_text()
    assert source.index('if KNOWN_ACTIVATION_BLOCKERS:')<source.index("globalfd=os.open('/run/lighthouse-natural-replay-v3-deploy.lock'")
    assert issuer.index('if args.issue and m.KNOWN_ACTIVATION_BLOCKERS:')<issuer.index('destination=DESTINATION/')
    assert 'ignore-starvation' not in source+issuer and 'bypass-blocker' not in source+issuer
