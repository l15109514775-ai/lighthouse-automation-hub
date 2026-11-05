"""Real Chromium against owned HTML; new packet is accepted by the actual verifier."""
import json
import os
from pathlib import Path
import subprocess
import pytest
from services.hub.reply_evidence import ReplyEvidence


@pytest.mark.timeout(100)
def test_browser_collects_its_own_evidence_and_verifier_records_the_complete_pair(runtime,tmp_path):
    settings,store=runtime;root=Path(__file__).resolve().parents[1]
    parent_id=store.enqueue('scheduler','owned-browser-reply','x_discovery','tweet',{},'reply-browser-prototype')
    parent=store.claim('tweet',180,4)
    output=settings.root/'media'/parent_id/'reply-machine'
    result=subprocess.run(['node',str(root/'tests/reply_machine_browser_fixture.mjs'),str(output)],cwd=root,
        env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers'),
            'TEMP':str(tmp_path),'TMP':str(tmp_path),'TMPDIR':str(tmp_path)},capture_output=True,timeout=85)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    summary=json.loads(result.stdout)
    assert len(summary['passed'])==9 and summary['external_calls']==0 and summary['human_review'] is False
    packet=json.loads(Path(summary['packet_file']).read_text('utf-8'))
    service=ReplyEvidence(settings,store);service.initialize()
    receipt=service.record(parent,packet)
    checked=service.verify(receipt['receipt_id'])['validated_source']
    assert checked['machine_relationship_supported'] and not checked['automatic_publication_eligible']
    assert checked['parent_source']['post']['text']=='架空原推20:00'
    assert checked['reply_source']['post']['text']=='架空回复21:00'
    assert checked['reply_source']['capture']['human_review'] is False
    # The raw plain-text observer still truthfully reports reply context as
    # unsupported by its original v1 policy; the new relationship rule is separate.
    reply_observation=json.loads(Path(packet['reply_in_thread']['file']).read_text('utf-8'))
    raw=json.loads((Path(packet['reply_in_thread']['file']).parent/'observation.json').read_text('utf-8'))
    assert raw['machine_source_facts']['supported'] is False
    assert raw['machine_source_facts']['failures']==['REPLY_CONTEXT_REQUIRED']
    assert reply_observation['payload']['capture']['source_screenshot_reviewed'] is False
    assert not store.query('SELECT * FROM api_calls') and not store.query('SELECT * FROM publications')
