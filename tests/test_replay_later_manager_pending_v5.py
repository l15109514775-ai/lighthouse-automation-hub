"""Captured manager-link shape, owned DOM/video, real V5 SQLite validation.

The saved native captures supply the href/status shape. The routed Chromium
pages, tiny video and Python publication fixtures do not claim live acceptance.
"""
from pathlib import Path
import json,os,subprocess
import pytest

from services.hub.errors import HubError
from services.hub.replay_part_public_evidence_v5 import manager_video_url,video_url
from test_replay_parts_v5 import reserved,publisher,delivery,fifo,tracks,public_evidence,PRINCIPAL,STATE


@pytest.mark.parametrize('scenario',['space_public','space_pending','protocol_public','protocol_pending','pending_no_bv','known_conflict','cover_conflict'])
def test_real_captured_href_shape_and_pending_status_are_read_only_owned_dom(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    value=subprocess.run(['node',str(root/'tests/replay_later_manager_pending_v5_fixture.mjs'),scenario,str(tmp_path)],cwd=root,env=env,
        capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert value.returncode==0,value.stderr
    result=json.loads(value.stdout.strip().splitlines()[-1])
    assert result['owned'] and result['captured_href_shape'] and result['external_requests']==result['clicks']==result['choosers']==0


def protocol_relative(manager,public):
    row=manager['selected_row']
    for link in [*row['title_links'],*row['covers']]:
        link['href']=link['href'].removeprefix('https:')
        link['canonical_url']='https://www.bilibili.com/video/BV1owned0001/'


def test_v5_real_sqlite_admits_recomputed_manager_href_without_weakening_public_proof(reserved):
    f=reserved;observation=public_evidence(f,alter=protocol_relative)
    result=f.publish.record_result(PRINCIPAL,f.child,observation,lookup=True)
    assert result['parent_state']=='completed' and f.db.get(f.child)['result'][STATE]['public_coverage']['complete_part_playability_verified'] is True
    evidence=json.loads(Path(observation['native_result_evidence']['file']).read_bytes())
    manager=json.loads(Path(evidence['manager_observation']['file']).read_bytes())
    assert manager['selected_row']['title_links'][0]['href']=='//www.bilibili.com/video/BV1owned0001/'
    assert len(f.db.query('SELECT * FROM publications'))==1 and f.db.get(f.child)['attempts']==0


@pytest.mark.parametrize('change',['canonical','conflicting_cover'])
def test_coherent_raw_ref_cannot_fake_canonical_or_hide_different_bv_in_sqlite(reserved,change):
    f=reserved
    def altered(manager,public):
        protocol_relative(manager,public);row=manager['selected_row']
        if change=='canonical':row['title_links'][0]['canonical_url']='https://www.bilibili.com/video/BV1234567890/'
        else:
            row['covers'][0]['href']='//www.bilibili.com/video/BV1234567890/'
            row['covers'][0]['canonical_url']='https://www.bilibili.com/video/BV1234567890/'
    observation=public_evidence(f,alter=altered)
    with pytest.raises(HubError,match='PUBLIC_EVIDENCE_REQUIRED'):f.publish.record_result(PRINCIPAL,f.child,observation,lookup=True)
    assert f.db.get(f.child)['result'][STATE]['state']=='submit_unknown'
    assert f.db.query('SELECT * FROM publications')[0]['external_id'] is None


def test_only_exact_official_manager_href_is_normalized_public_url_rule_stays_strict():
    bv='BV1gv4A6cEdK';url='https://www.bilibili.com/video/'+bv+'/'
    assert manager_video_url({'href':url},bv)
    assert manager_video_url({'href':url.removeprefix('https:'),'canonical_url':url},bv)
    assert not video_url(url.removeprefix('https:'),bv)  # The public page is still absolute HTTPS only.
    assert not video_url(url+'?spm_id_from=anything',bv)
    for raw in (url+'?anything=1',url+'#fragment','http:'+url.removeprefix('https:'),'//evil.example/video/'+bv+'/',
            '//www.bilibili.com.evil.example/video/'+bv+'/',url+'\n','javascript:void(0)',None):
        assert not manager_video_url({'href':raw,'canonical_url':url},bv)
    assert not manager_video_url({'href':url.removeprefix('https:')},bv)
