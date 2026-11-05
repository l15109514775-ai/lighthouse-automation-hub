"""One persistent synthetic text-only production-queue acceptance. No X/B站 requests."""
from pathlib import Path
import json
import time
from services.hub.config import Settings
from services.hub.db import Store

settings=Settings.load()
store=Store(settings.db_path)
store.init()
source={'source_api':'synthetic_acceptance','synthetic':True,
    'post':{'id':'1000000000000000021','text':'これは自動化テスト用の架空の文章です。\n今日は20:00からテスト配信の予定です。\n配信後にまたお知らせします！',
            'created_at':'2026-10-31T01:00:00Z'},
    'author':{'name':'API验收模拟账号（非主播）','username':'fixture_only'},
    'url':'https://x.com/fixture_only/status/1000000000000000021'}
ident=store.enqueue('synthetic_acceptance','relay-text-smoke-v1','tweet_process','tweet',source,settings.config['policy_version'])
deadline=time.time()+150
while time.time()<deadline:
    job=store.get(ident)
    if job['state'] in ('awaiting_review','failed','auth_expired','publish_unknown','completed'):
        break
    time.sleep(1)
else:
    raise SystemExit('RELAY_ACCEPTANCE_NOT_FINISHED_WITHIN_150_SECONDS')
usage=store.query('SELECT state,error_code,COUNT(*) attempts FROM api_calls WHERE job_id=? GROUP BY state,error_code',(ident,))
tokens=store.query('''SELECT u.model,u.input_tokens,u.output_tokens,u.estimated_cost_usd FROM api_usage_details u
                      JOIN api_calls a ON a.call_id=u.call_id WHERE a.job_id=? ORDER BY a.started_at''',(ident,))
result={'job_id':ident,'synthetic_only':True,'state':job['state'],'error_code':job['error_code'],'stage':job['stage'],
        'api_attempts':usage,'token_usage':tokens,'model_checks':job['result'].get('evidence'),
        'render':{key:job['result'].get('render',{}).get(key) for key in ('png','sha256')},
        'publications':store.query('SELECT state,external_id FROM publications WHERE job_id=?',(ident,))}
print(json.dumps(result,ensure_ascii=False,indent=2))
assert job['state']=='awaiting_review'
assert job['error_code']=='HUMAN_SOURCE_AND_TRANSLATION_REVIEW_REQUIRED'
assert job['result']['evidence']['machine_checks_passed'] is True
assert len(tokens)==2
assert not result['publications']
assert job['result']['render']['report']['publishable'] is False
