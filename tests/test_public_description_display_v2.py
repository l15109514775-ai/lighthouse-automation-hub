"""Recorded visible text, independent Python/JS checks and owned DOM only."""
from copy import deepcopy
from pathlib import Path
import json,os,subprocess
import pytest

from services.hub.replay_part_public_evidence_v5 import description_display_evidence,validate_result
from services.hub.youtube_download import digest
from services.hub.errors import HubError
from test_replay_parts_v5 import publisher,reserved,public_evidence
from test_replay_delivery_v5 import delivery
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=json.loads((ROOT/'tests/fixtures/public_description_display_v2.json').read_text(encoding='utf-8'))
EXPECTED=FIXTURE['expected_description'];SOURCE=FIXTURE['source_url'];META=FIXTURE['metadata_sha256']

def clean_env():
    value={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    value['PLAYWRIGHT_BROWSERS_PATH']=str(ROOT/'compositor/runtime/browsers');return value

def js_records(cases):
    code="import {publicDescriptionDisplayEvidence as f} from './adapters/replay_later_result_lookup_v2.mjs';let raw='';for await(const s of process.stdin)raw+=s;console.log(JSON.stringify(JSON.parse(raw).map(x=>f(...x))));"
    result=subprocess.run(['node','--input-type=module','-e',code],cwd=ROOT,env=clean_env(),input=json.dumps(cases),
        text=True,encoding='utf-8',capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)

def test_recorded_198_to241_visible_difference_has_exact_versioned_explanation():
    assert len(EXPECTED)==198 and len(FIXTURE['observed_description'])==241
    assert FIXTURE['stable_snapshots']==3 and FIXTURE['word_joiner_positions']==[103]
    assert digest(FIXTURE['expected_metadata'])==META
    raw=FIXTURE['observed_description'];record=description_display_evidence(raw,EXPECTED,SOURCE,META)
    assert record==js_records([[raw,EXPECTED,SOURCE,META]])[0]
    assert record['candidate']=='source_prefix_once_word_joiners_omitted'
    assert record['omitted_word_joiner_positions']==[103] and record['source_prefix_removed'] is True
    assert EXPECTED[50]=='\u3000' and EXPECTED[103]=='\u2060'

@pytest.mark.parametrize('prefix,omit',[(False,False),(True,False),(False,True),(True,True)])
def test_only_four_explicit_candidates_match_independently(prefix,omit):
    raw=(SOURCE+'\n' if prefix else '')+(EXPECTED.replace('\u2060','') if omit else EXPECTED)
    record=description_display_evidence(raw,EXPECTED,SOURCE,META)
    assert record==js_records([[raw,EXPECTED,SOURCE,META]])[0]
    assert record['source_prefix_removed'] is prefix
    assert record['omitted_word_joiner_positions']==([103] if omit else [])

@pytest.mark.parametrize('change',[
    lambda s:s.replace('YsSXSIYtCoE','foreign0001'),lambda s:s.replace('日语原声','日语假文'),
    lambda s:s.replace('\u3000',' '),lambda s:s.replace('\n','\r\n'),lambda s:s+' ',lambda s:' '+s,
    lambda s:s+'\n',lambda s:SOURCE+'\n'+s,lambda s:s.replace('CORE','CO\u200bRE'),
    lambda s:s.replace('CORE','CO\ufeffRE'),lambda s:s.replace('CORE','CO\u200dRE'),
    lambda s:s.replace('CORE','CO\u2060RE'),lambda s:s.replace('CORE','ＣＯＲＥ'),
])
def test_changed_source_content_spacing_or_other_invisibles_are_rejected(change):
    raw=change(FIXTURE['observed_description'])
    assert description_display_evidence(raw,EXPECTED,SOURCE,META) is None
    assert js_records([[raw,EXPECTED,SOURCE,META]])==[None]

def test_unicode_positions_are_codepoints_and_unexpected_or_partial_joiner_changes_fail():
    expected='😀a\u2060b\u2060c';raw=expected.replace('\u2060','')
    record=description_display_evidence(raw,expected,SOURCE,META)
    assert record==js_records([[raw,expected,SOURCE,META]])[0] and record['omitted_word_joiner_positions']==[2,4]
    partial=expected.replace('\u2060','',1)
    assert description_display_evidence(partial,expected,SOURCE,META) is None

def test_python_v2_validator_preserves_raw_binding_and_rejects_forged_display_claim(reserved):
    f=reserved;job,binding,state=f.publish.snapshot(f.child,write=False)
    def alter(manager,public):
        raw=public['visible_dom']['descriptions'][0]['text']
        public['description_display_evidence']=description_display_evidence(raw,binding['metadata']['description'],binding['source_url'],binding['metadata_sha256'])
    observation=public_evidence(f,alter=alter)
    coverage=validate_result(f.publish,job,binding,state,observation)
    assert coverage['binding_sha256']==digest(binding) and coverage['normalized_description']==binding['metadata']['description']
    assert coverage['description_normalization_version']=='bilibili-public-description-display-v2'
    assert coverage['observed_description']==binding['source_url']+'\n'+binding['metadata']['description']
    def forged(manager,public):
        alter(manager,public);public['description_display_evidence']['source_prefix_removed']=False
    with pytest.raises(HubError):validate_result(f.publish,job,binding,state,public_evidence(f,alter=forged))

@pytest.mark.parametrize('scenario',['recorded','changed_body','changed_space'])
def test_real_owned_chromium_uses_v2_and_saves_raw_failures(tmp_path,scenario):
    result=subprocess.run(['node',str(ROOT/'tests/public_description_display_v2_fixture.mjs'),str(tmp_path),scenario],
        cwd=ROOT,env=clean_env(),capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout.strip().splitlines()[-1]);assert value['owned'] and value['external_requests']==value['submit_clicks']==0
