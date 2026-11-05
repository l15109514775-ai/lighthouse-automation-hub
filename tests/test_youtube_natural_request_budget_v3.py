"""Finite free transport budgets; retained ledger history and real reset dates."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from adapters.ytdlp_natural_v3 import natural_budget_limits
from adapters.ytdlp_highest_v2 import preflight_budget
from services.hub.budget import Budget
from services.hub.errors import HubError
from services.hub.worker import Worker
from services.hub.youtube_download_v3 import YouTubeNaturalPrepare,PREPARE_JOB_TYPE,JOB_TYPE
from test_youtube_download_v3 import natural,write_policy,owned_planner


@pytest.mark.parametrize('key,value',[
    ('daily_http_requests',None),('daily_http_requests',True),('daily_http_requests',0),('daily_http_requests',6001),
    ('daily_http_requests',6000.0),('daily_http_requests','6000'),('monthly_http_requests',None),
    ('monthly_http_requests',True),('monthly_http_requests',0),('monthly_http_requests',150001),
    ('monthly_http_requests',150000.0),('monthly_http_requests','150000')])
def test_configured_transport_budget_has_finite_integer_upper_bounds(natural,key,value):
    f=natural;f.p[key]=value
    if value is None:
        # TOML has no null: omission tests the equivalent absent configuration.
        f.p.pop(key)
    write_policy(f.s,f.p)
    with pytest.raises(HubError,match='POLICY_INVALID'):f.service.policy()
    assert not f.db.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('daily,monthly',[(500,15000),(2500,30000),(6000,150000)])
def test_binding_reads_configured_budgets_without_changing_existing_provider_rows(natural,daily,monthly):
    f=natural;f.p.update(daily_http_requests=daily,monthly_http_requests=monthly);write_policy(f.s,f.p)
    ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4);binding=f.service._binding(job)[1]
    limits=natural_budget_limits(binding)['youtube_download_http']
    assert limits['daily_calls']==daily and limits['monthly_calls']==monthly
    assert limits['max_call_usd']==0 and limits['paid_authorized'] is False
    assert binding['transport_budget']['history_reset'] is False and not f.db.query('SELECT * FROM api_calls')


def test_two_11gb_plan_allowances_fit_normal_budget_and_keep_old_charge(natural):
    f=natural;ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4);binding=f.service._binding(job)[1]
    budget=Budget(f.db,natural_budget_limits(binding))
    # Owned aggregate quota reservations, no actual HTTP dispatch: each real
    # observed11GB plan takes1436, plus bounded64 metadata planning per source.
    ids=[]
    for units in (60,64,1436,64,1436):
        key=budget.reserve('youtube_download_http',units=units);budget.finish(key,'done');ids.append(key)
    before=f.db.query('SELECT * FROM api_calls');preflight_budget(f.db,binding,130,limits_function=natural_budget_limits)
    assert f.db.query('SELECT * FROM api_calls')==before and sum(row['units'] for row in before)==3060
    assert all(row['cost_usd']==0 for row in before) and len({row['call_id'] for row in before})==len(ids)


def test_monthly_exhaustion_schedules_next_real_month_without_refund_or_reset(natural):
    f=natural;f.p['monthly_http_requests']=6000;write_policy(f.s,f.p);ident=f.service.enqueue_if_eligible(f.payload)
    class Exhausted:
        def __init__(self,s):pass
        def acquire(self,job,binding,*,check):
            check();budget=Budget(f.db,natural_budget_limits(binding));key=budget.reserve('youtube_download_http',units=6000)
            budget.finish(key,'done');budget.reserve('youtube_download_http')
    handler=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=Exhausted)
    assert Worker(f.s,f.db,{PREPARE_JOB_TYPE:handler.process}).once('video') is True
    job=f.db.get(ident);due=datetime.fromtimestamp(job['next_run'],ZoneInfo('Asia/Shanghai'));now=datetime.now(ZoneInfo('Asia/Shanghai'))
    assert job['state']=='retry_scheduled' and job['attempts']==1 and job['error_code']=='API_BUDGET_EXCEEDED'
    assert due.day==1 and due.hour==due.minute==0 and (due.year,due.month)!=(now.year,now.month)
    assert sum(row['units'] for row in f.db.query('SELECT * FROM api_calls'))==6000


def test_impossible_whole_plan_budget_stops_before_creating_media_job(natural):
    f=natural;f.p['daily_http_requests']=64;write_policy(f.s,f.p)
    f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4)
    service=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=owned_planner(f))
    with pytest.raises(HubError,match='PLAN_EXCEEDS_REQUEST_BUDGET'):service.process(job)
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)) and not f.db.query('SELECT * FROM api_calls')
