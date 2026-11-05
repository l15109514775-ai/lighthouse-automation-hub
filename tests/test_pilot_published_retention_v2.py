"""Scope seam only; existing 37 cases exercise actual files/SQLite/FFmpeg."""
from copy import deepcopy
import pytest
from services.hub.errors import HubError
from services.hub.pilot_published_retention_v2 import (PilotPublishedRetentionV2 as Pilot,
    JOB,DOWNLOAD,SOURCE,BV,ATTEMPT)
from services.hub.video_source_retention_v2 import VideoSourceTracksRetentionV2,DISPOSAL

def owned_context():
    return ({'job_id':JOB,'job_type':'replay_highest_video_publish_v2','source_id':SOURCE,'attempts':3,
        'external_publish_id':BV,'result':{'replay_highest_video_web_v2':{}}},
        {'download_job_id':DOWNLOAD,'source_id':SOURCE},
        {'attempt_id':ATTEMPT,'test_retention_policy':{'owned_old_hold':True}},
        {'job_id':DOWNLOAD,'job_type':'youtube_download_v2','source_id':SOURCE}, {'external_id':BV})

def test_scope_accepts_only_bound_protocol_and_does_not_mutate_old_hold():
    value=owned_context();before=deepcopy(value)
    assert Pilot._target_context(value) is value and value==before
    assert Pilot.TEST_COUNT==sum(Pilot.TEST_GROUPS.values())==43
    assert Pilot.RELEASE_FILENAME!=VideoSourceTracksRetentionV2.RELEASE_FILENAME

def test_other_job_and_disposal_cannot_reach_existing_core(monkeypatch):
    calls=[]
    monkeypatch.setattr(VideoSourceTracksRetentionV2,'_context',lambda *a,**k:calls.append('upstream'))
    service=object.__new__(Pilot)
    with pytest.raises(HubError,match='HIGHEST_PILOT_SCOPE'):service._context('owned_unrelated_job')
    with pytest.raises(HubError,match='HIGHEST_PILOT_SCOPE'):service.authorize('owned',scope=DISPOSAL,job_id=JOB)
    assert calls==[]

@pytest.mark.parametrize('changed',['source','download','bv'])
def test_same_completed_status_cannot_substitute_another_artifact_or_publication(changed):
    value=owned_context()
    if changed=='source':value[0]['source_id']='ownedOTHER1'
    elif changed=='download':value[3]['job_id']='owned-other-download'
    else:value[4]['external_id']='BV1ownedOTHER'
    with pytest.raises(HubError,match='HIGHEST_PILOT_SCOPE'):Pilot._target_context(value)


def test_complete_fifty_source_release_accepts_declared_extras_but_not_unknown_or_failed_junit(tmp_path,monkeypatch):
    """Real release verifier over all 50 actual source files; root-FS seam only.

    The synthetic XML is a protocol fixture, not an Ubuntu pass claim. Keeping
    this positive case catches the former fixed-six-source allowlist failure.
    """
    import hashlib,json
    from pathlib import Path
    import services.hub.video_source_retention_v2 as core
    import services.hub.pilot_published_retention_v2 as thin
    from services.hub.db import encode
    from services.hub.video_source_retention_v2 import PUBLISHED
    monkeypatch.setattr(core,'_root_protected',lambda _:None)
    monkeypatch.setattr(thin,'_root_protected',lambda _:None)
    root=Path(core.__file__).resolve().parents[2]
    names=tuple(dict.fromkeys(('services/hub/video_source_retention_v2.py','services/hub/archive_source_retention_v2.py',*Pilot.RELEASE_EXTRA_SOURCES)))
    sources={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}
    assert len(sources)==50 and len(thin.RUNTIME_SOURCES)==45
    junit=tmp_path/'owned-results.xml'
    xml='<testsuites><testsuite>'+''.join('<testcase classname="'+group+'" name="owned-'+str(i)+'"/>'
        for group,count in Pilot.TEST_GROUPS.items() for i in range(count))+'</testsuite></testsuites>'
    junit.write_text(xml,'utf-8')
    evidence={'version':Pilot.TEST_VERSION,'tests':43,'source_files':sources,'network':'none',
        'application_data_mounted':False,'production_profile_mounted':False,'exit_code':0,'full_suite_passed':True,
        'oom_killed':False,'no_pid_or_oom_denial':True,'runtime_matches_declared_candidate':True,'mounts_match_isolated_allowlist':True,
        'junit_file':str(junit),'junit_sha256':hashlib.sha256(junit.read_bytes()).hexdigest(),
        'current_runtime_parent_archive_sha256':thin.PARENT,'current_runtime_source_sha256':{n:sources[n] for n in thin.RUNTIME_SOURCES},
        'qualification_scope':'highest_pilot_current45_only_natural_not_qualified'}
    proof=tmp_path/'owned-attestation.json';config=tmp_path/'config';config.mkdir()
    release=config/Pilot.RELEASE_FILENAME;service=object.__new__(Pilot);service.root=tmp_path
    plan={'job_id':JOB,'scope':PUBLISHED,'authorization_id':'owned-auth-id','proof':{'pipeline_state':'replay_highest_video_web_v2'}}
    def save():
        proof.write_text(encode(evidence),'utf-8')
        release.write_text(encode({'version':'video-source-retention-release-v2','authorization_id':plan['authorization_id'],
            'scope':PUBLISHED,'source_files':sources,'test_attestation':{'file':str(proof),'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}}),'utf-8')
    save();assert service._release(plan)==hashlib.sha256(release.read_bytes()).hexdigest()
    evidence['source_files']={**sources,'services/hub/unqualified_extra.py':'0'*64};save()
    with pytest.raises(HubError,match='RELEASE_REQUIRED'):service._release(plan)
    evidence['source_files']=sources
    junit.write_text(xml.replace('/>','><failure/></testcase>',1),'utf-8');evidence['junit_sha256']=hashlib.sha256(junit.read_bytes()).hexdigest();save()
    with pytest.raises(HubError,match='RELEASE_REQUIRED'):service._release(plan)
