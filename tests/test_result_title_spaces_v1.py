from services.hub.replay_part_public_evidence_v5 import title_display_matches
from services.hub.replay_part_public_evidence_v5 import description_display_evidence


def test_only_expected_ascii_space_collapse_is_accepted():
    expected='龍巻示例主播B  回放 P1/1'
    assert title_display_matches(expected,expected)
    assert title_display_matches('龍巻示例主播B 回放 P1/1',expected)
    for changed in ('示例主播A 回放 P1/1','龍巻示例主播B 回放 P2/2','龍巻示例主播B回放 P1/1',
                    '龍巻示例主播B\u3000回放 P1/1','龍巻示例主播B\t回放 P1/1',None):
        assert not title_display_matches(changed,expected)


def test_description_collapse_is_versioned_and_preserves_other_text():
    source='https://www.youtube.com/watch?v=c18fHra3k2I';expected='原题：龍巻示例主播B  回放\n原视频：'+source
    raw=expected.replace('  ',' ');args=(expected,source,'a'*64)
    assert description_display_evidence(raw,*args) is None
    value=description_display_evidence(raw,*args,allow_ascii_space_collapse=True)
    assert value['version']=='bilibili-public-description-display-v3' and value['ascii_spaces_collapsed'] is True
    assert description_display_evidence(expected,*args,allow_ascii_space_collapse=True)['version']=='bilibili-public-description-display-v2'
    for changed in (raw.replace('龍巻示例主播B','示例主播A'),raw.replace('\n',' '),raw.replace('c18fHra3k2I','c18fHra3k2J'),raw.replace(' 回放','\t回放'),raw+' '):
        assert description_display_evidence(changed,*args,allow_ascii_space_collapse=True) is None
