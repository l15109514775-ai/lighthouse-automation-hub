"""Owned configuration-only checks; no host, Docker, credentials or platform."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tomllib
import pytest


def operator():
    file=Path(__file__).resolve().parents[2]/'scripts/ops/activate_x_relations_v4.py'
    spec=importlib.util.spec_from_file_location('owned_v4_activation',file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


RAW=b'''# Owned configuration only.
policy_version = "machine-tweet-v3-api"
tweet_poll_minutes = 30
target_bilibili_site_uid = "1000000000000000"
x_api_enabled = true
x_web_enabled = false
machine_tweet_enabled = true
allowed_media_hosts = ["pbs.twimg.com"]
machine_source_generation = 4
[budgets.x]
daily_usd = 0.50
monthly_usd = 5
'''


def test_shadow_changes_exactly_two_fields_without_altering_original_or_budget():
    m=operator();raw=bytes(RAW);before=tomllib.loads(raw.decode());shadow=m.shadow_config(raw);after=tomllib.loads(shadow.decode())
    assert raw==RAW and set(before)==set(after)
    assert {k for k in before if before[k]!=after[k]}=={'policy_version','allowed_media_hosts'}
    assert after['policy_version']=='machine-tweet-v4-api-relations'
    assert after['allowed_media_hosts']==['pbs.twimg.com','abs.twimg.com'] and after['budgets']==before['budgets']


def test_only_api_tweet_environment_api_fourteen_and_scheduler_one_source_change():
    m=operator();base={'environment':{'HUB_CONFIG':str(m.ROOT/'config/hub.toml'),'OWNED_SYNTHETIC':'value'},
        'image':'owned-image','command':['owned-command'],'volumes':[{'type':'bind','source':str(m.ROOT/'data'),'target':str(m.ROOT/'data'),'read_only':False},
            {'type':'bind','source':str(m.ROOT/'config'),'target':str(m.ROOT/'config'),'read_only':True}]}
    before={'services':{n:deepcopy(base) for n in ('api','tweet-worker','scheduler','video-worker','system-worker')}}
    names={f'adapters/owned{i}.py':Path('/owned/immutable')/f'{i}.py' for i in range(14)}
    after=m.candidate(before,names,Path('/owned/scheduler.py'));restored=deepcopy(after)
    assert before['services']['tweet-worker']['environment']['HUB_CONFIG']==str(m.ROOT/'config/hub.toml')
    for n in ('api','tweet-worker'):
        assert after['services'][n]['environment']['HUB_CONFIG']==str(m.CONFIG)
        restored['services'][n]['environment']=deepcopy(before['services'][n]['environment'])
    assert after['services']['tweet-worker']['volumes']==before['services']['tweet-worker']['volumes']
    assert len(after['services']['api']['volumes'])==16 and all(v['read_only'] is True for v in after['services']['api']['volumes'][2:])
    assert after['services']['scheduler']['environment']==before['services']['scheduler']['environment']
    restored['services']['api']['volumes']=deepcopy(before['services']['api']['volumes'])
    restored['services']['scheduler']['volumes']=deepcopy(before['services']['scheduler']['volumes'])
    assert restored==before and all(after['services'][n]==before['services'][n] for n in m.UNCHANGED)


def test_wrong_origin_policy_poll_window_or_existing_host_does_not_get_silently_changed():
    m=operator()
    for raw in (RAW.replace(b'machine-tweet-v3-api',b'machine-tweet-v4-api-relations'),
            RAW.replace(b'tweet_poll_minutes = 30',b'tweet_poll_minutes = 31'),
            RAW.replace(b'x_web_enabled = false',b'x_web_enabled = true'),
            RAW.replace(b'["pbs.twimg.com"]',b'["pbs.twimg.com","abs.twimg.com"]')):
        with pytest.raises(AssertionError):m.shadow_config(raw)
