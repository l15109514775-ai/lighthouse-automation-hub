"""Independent ordering tests over real SQLite and bound small evidence.

Owned completed predecessors below model an already-accepted historical row;
they do not claim full native/media QA or public-player acceptance. Production
result validation remains the separate mandatory service boundary.
"""
from pathlib import Path

import pytest

from services.hub.errors import HubError
from services.hub.replay_parts_v5 import ReplayPartVideoWeb,ReplayPartGroupsV5,STATE,part_kind
from test_replay_part_moderation_v5 import lookup
from test_replay_part_registry_v5 import two_prepared,reserve,accept_owned_predecessor


def test_prior_unknown_blocks_next_part_without_new_publication_row_or_attempt(lookup):
    f=lookup;one,two=two_prepared(f);reserve(f,one)
    with f.db.transaction() as db:db.execute("UPDATE publications SET state='unknown' WHERE job_id=?",(one['job_id'],))
    before=f.db.query('SELECT * FROM publications');second=f.db.get(two['job_id'])
    with pytest.raises(HubError,match='PREVIOUS_PART_REQUIRES_LOOKUP'):reserve(f,second)
    assert f.db.query('SELECT * FROM publications')==before and f.db.get(two['job_id'])==second


def test_prior_completed_bound_bv_allows_next_own_kind_but_never_a_second_send_for_same_part(lookup):
    f=lookup;one,two=two_prepared(f);reserve(f,one);accept_owned_predecessor(f,one)
    assert reserve(f,f.db.get(two['job_id']))['can_click_submit'] is True
    pubs=f.db.query('SELECT * FROM publications ORDER BY kind')
    assert len(pubs)==2 and {p['kind'] for p in pubs}=={part_kind(0),part_kind(1)}
    assert {p['source_id'] for p in pubs}=={one['source_id']} and len({p['account_id'] for p in pubs})==1
    with f.db.transaction() as db:db.execute("UPDATE publications SET state='unknown' WHERE job_id=?",(two['job_id'],))
    assert reserve(f,f.db.get(two['job_id']))=={'can_click_submit':False,'state':'unknown','reconcile_required':True}
    assert len(f.db.query('SELECT * FROM publications'))==2


@pytest.mark.parametrize('change',['evidence_bytes','previous_kind','previous_bv','previous_job_state'])
def test_boolean_completed_or_changed_predecessor_binding_does_not_open_next_send(lookup,change):
    f=lookup;one,two=two_prepared(f);reserve(f,one);ref=accept_owned_predecessor(f,one)
    if change=='evidence_bytes':
        file=Path(ref['file']);raw=file.read_bytes();file.write_bytes(raw+b' ')
    else:
        with f.db.transaction() as db:
            if change=='previous_kind':db.execute("UPDATE publications SET kind='foreign_kind' WHERE job_id=?",(one['job_id'],))
            elif change=='previous_bv':db.execute("UPDATE publications SET external_id='BV1wrongPREV' WHERE job_id=?",(one['job_id'],))
            else:db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(one['job_id'],))
    before=f.db.query('SELECT * FROM publications')
    with pytest.raises(HubError):reserve(f,f.db.get(two['job_id']))
    assert f.db.query('SELECT * FROM publications')==before and f.db.get(two['job_id'])['state']=='upload_pending'


def test_incomplete_group_never_offers_canonical_disposal_even_if_one_part_is_public(lookup):
    f=lookup;one,two=two_prepared(f);reserve(f,one);accept_owned_predecessor(f,one)
    file=f.s.root/'media'/f.job['job_id']/'youtube-whole-v4/original.mp4';file.parent.mkdir(parents=True);file.write_bytes(b'owned retained media sentinel')
    before=file.read_bytes()
    assert ReplayPartVideoWeb(f.s,f.db,part_index=0).cleanup_eligibility(one['job_id'])['eligible'] is False
    with pytest.raises(HubError,match='COMPLETE_GROUP_REQUIRED'):ReplayPartGroupsV5(f.s,f.db).cleanup_summary(f.group)
    assert file.read_bytes()==before and f.db.get(two['job_id'])['state']=='upload_pending'
