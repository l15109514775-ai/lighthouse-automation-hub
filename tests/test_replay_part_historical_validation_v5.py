"""Replay accepted immutable evidence at its real SQLite audit time.

No current sending/native300-second gate receives a time override. Only the
pure reader has the explicit historical interface used by retention.
"""
import json,time
import pytest

from services.hub.errors import HubError
from services.hub.short_video_web import PRINCIPAL
from services.hub import replay_part_public_evidence_v5 as pure
from test_replay_parts_v5 import publisher,reserved,public_evidence
from test_replay_delivery_v5 import delivery
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks


def test_historical_reader_uses_actual_accepted_audit_and_preserves_raw_observation_bytes(reserved,monkeypatch):
    f=reserved;observation=public_evidence(f)
    f.publish.record_result(PRINCIPAL,f.child,observation,lookup=True)
    accepted=f.db.query("SELECT * FROM audit WHERE target=? AND action='replay_part_later_public_result_v5'",(f.child,))[0]
    outcome=json.loads(accepted['outcome'])
    assert outcome['external_id']==observation['external_id'] and outcome['native_result_evidence']==observation['native_result_evidence']
    job,binding,state=f.publish.snapshot(f.child,write=False)
    reference=observation['native_result_evidence'];file=__import__('pathlib').Path(reference['file']);raw=file.read_bytes()
    now=time.time();monkeypatch.setattr(pure.time,'time',lambda:now+400)
    with pytest.raises(HubError):pure.validate_result(f.publish,job,binding,state,observation)
    replay=pure.validate_result(f.publish,job,binding,state,observation,validation_time=accepted['at'])
    assert replay==state['public_coverage'] and file.read_bytes()==raw
    assert replay['observed_at']==state['public_coverage']['observed_at']


def test_historical_reader_rejects_nonfinite_boolean_future_or_wrong_old_instant(reserved):
    f=reserved;o=public_evidence(f);job,binding,state=f.publish.snapshot(f.child,write=False)
    for value in (True,float('nan'),float('inf'),-1,time.time()+100,time.time()-10000):
        with pytest.raises(HubError):pure.validate_result(f.publish,job,binding,state,o,validation_time=value)
