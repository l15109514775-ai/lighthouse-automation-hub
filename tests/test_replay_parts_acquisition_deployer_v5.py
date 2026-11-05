"""Configuration edits preserve existing service settings and actual references."""
from pathlib import Path
import hashlib
import importlib.util
import json
import shutil
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('owned_acquisition_deployer', ROOT/'scripts/ops/deploy_replay_parts_acquisition_v5.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@pytest.mark.parametrize('existing', [False, True])
def test_shadow_config_retains_tables_and_changes_only_two_flags(existing):
    text = ('youtube_natural_download_enabled = false\n' if existing else '') + 'max_attempts = 4\n[budgets.x]\ndaily_usd = 0.5\n[sources]\nname = "owned"\n'
    before = tomllib.loads(text)
    result = deploy.shadow_hub(b'\xef\xbb\xbf'+text.encode())
    assert tomllib.loads(result.decode()) == {**before, **deploy.FLAGS}
    assert '[budgets.x]' in result.decode()


def test_policy_pair_uses_existing_references_and_one_canonical_digest(tmp_path, monkeypatch):
    config = tmp_path/'config'
    config.mkdir()
    names = ('owner_permission_reference', 'acquisition_authorization_reference',
             'quality_authorization_reference', 'owner_risk_acceptance_reference',
             'session_authorization_reference', 'owner_risk_accepted_at', 'session_authorized_at')
    refs = {name: 'owned-existing-reference-'+name for name in names}
    (config/'youtube-highest-download-policy.toml').write_bytes(deploy.toml_bytes(refs))
    shutil.copyfile(ROOT/'config/whole-replay-planning-policy-v4.example.toml', config/'whole-replay-planning-policy-v4.example.toml')
    old = {'planning_policy_sha256': 'a'*64, 'activated_at': 'old', 'owner_queue_reference': 'owned-original-queue-instruction'}
    (config/'replay-fifo-policy-v4.json').write_text(json.dumps(old))
    (config/'hub.toml').write_text('max_attempts = 4\n[budgets.x]\ndaily_usd = 0.5\n')
    monkeypatch.setattr(deploy, 'ROOT', tmp_path)
    files, previous = deploy.policies('2026-11-02T00:00:00Z')
    planning = tomllib.loads(files['whole-replay-planning-policy-v4.toml'].decode())
    acquisition = tomllib.loads(files['youtube-whole-replay-download-policy-v4.toml'].decode())
    fifo = json.loads(files['replay-fifo-policy-v4.json'])
    canonical = hashlib.sha256(json.dumps(planning, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert fifo['planning_policy_sha256'] == acquisition['planning_policy_sha256'] == canonical
    assert previous == hashlib.sha256((config/'replay-fifo-policy-v4.json').read_bytes()).hexdigest()
    assert {name: acquisition[name] for name in names} == refs
    assert fifo['owner_queue_reference'] == old['owner_queue_reference']
    assert planning['raw_track_release_after_verified_merge'] is True
    assert planning['prefer_single_whole_passthrough_v5'] is True
    assert not any('publish' in name for name in files)
