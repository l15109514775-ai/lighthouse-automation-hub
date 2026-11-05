"""UTC resource estimates, separate from immutable HTTP/gross cost history.

Only hash-verified, durably saved successful source receipts earn an estimate
deduplication credit. X calls without that evidence keep their original gross
bound. The official UTC deduplication rule is a soft guarantee, not an invoice.
"""
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib,json,math,os,re,stat,time,uuid

from .db import encode
from .errors import HubError

VERSION='x-utc-resource-estimate-v1'
SOURCE='https://docs.x.com/x-api/getting-started/pricing'
SOURCE_FILES=('services/hub/x_cost_ledger.py','services/hub/budget.py','adapters/http.py','scheduler/migrations/017_x_utc_cost_ledger.sql')
MICROS={'post':5000,'user':10000}


def fail(code):raise HubError(code,review=True)
def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()
def day(stamp):return datetime.fromtimestamp(stamp,timezone.utc).date().isoformat()


def installed(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_cost_observations'").fetchone())


def source_hashes():
    root=Path(__file__).resolve().parents[2]
    return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in SOURCE_FILES}


def history_fingerprints(db):
    return {name:{'rows':len(rows),'sha256':digest(rows)} for name in ('api_calls','api_resource_usage','source_http_receipts')
        for rows in ([dict(r) for r in db.execute('SELECT * FROM '+name+' ORDER BY call_id')],)}


def _rows(db):
    rows=db.execute('''SELECT a.call_id,a.provider,a.job_id,a.started_at,a.finished_at,a.state,a.cost_usd,a.units,
        r.discovery_job_id,r.method,r.host,r.path,r.params,r.raw_sha256,r.canonical_sha256,r.transport_mode,r.captured_at,
        u.endpoint_kind,u.max_posts,u.max_users,u.returned_posts,u.returned_users,u.post_unit_usd,u.user_unit_usd,
        u.reserved_upper_bound_usd,u.settled_upper_bound_usd,u.state resource_state
        FROM api_calls a LEFT JOIN source_http_receipts r ON r.call_id=a.call_id
        LEFT JOIN api_resource_usage u ON u.call_id=a.call_id WHERE a.provider='x' ORDER BY a.started_at,a.call_id''').fetchall()
    return [dict(r) for r in rows]


def _json(raw):
    def pairs(items):
        value={}
        for k,v in items:
            if k in value:raise ValueError()
            value[k]=v
        return value
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


def observation(row,raw):
    from adapters.http import x_resource_bounds,x_returned_resources
    try:
        if (not isinstance(raw,bytes) or not 0<len(raw)<=5*1024*1024 or row['state']!='done' or row['resource_state']!='estimated' or
            row['job_id']!=row['discovery_job_id'] or row['method']!='GET' or row['host']!='api.x.com' or
            row['transport_mode']!='default_tls' or row['captured_at']!=row['finished_at'] or
            hashlib.sha256(raw).hexdigest()!=row['raw_sha256']):raise ValueError()
        body=_json(raw)
        if digest(body)!=row['canonical_sha256']:raise ValueError()
        bounds=x_resource_bounds('GET','https://api.x.com'+row['path'],json.loads(row['params']))
        counts=x_returned_resources(body,bounds)
        if (bounds!={k:row[v] for k,v in (('endpoint_kind','endpoint_kind'),('posts','max_posts'),('users','max_users'))} or
            counts!={'posts':row['returned_posts'],'users':row['returned_users']} or
            row['post_unit_usd']!=.005 or row['user_unit_usd']!=.010):raise ValueError()
        gross=counts['posts']*.005+counts['users']*.010
        if abs(gross-row['cost_usd'])>1e-9 or abs(gross-row['settled_upper_bound_usd'])>1e-9:raise ValueError()
        start,end=row['started_at'],row['finished_at']
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (start,end)) or not 0<=start<=end<=time.time()+1:raise ValueError()
        resources=[];includes=body.get('includes',{});data=body.get('data',[] if bounds['endpoint_kind']=='timeline' else {})
        posts=(data if isinstance(data,list) else [data]) if bounds['endpoint_kind']!='user_lookup' else []
        users=[data] if bounds['endpoint_kind']=='user_lookup' else []
        for kind,items in (('post',posts+includes.get('tweets',[])),('user',users+includes.get('users',[]))):
            resources.extend({'kind':kind,'id':ident,'unit_microusd':MICROS[kind]} for ident in sorted({v['id'] for v in items}))
        return {'call_id':row['call_id'],'version':VERSION,'state':'verified' if day(start)==day(end) else 'cross_utc_upper_bound',
            'raw_sha256':row['raw_sha256'],'canonical_sha256':row['canonical_sha256'],
            'started_at':start,'finished_at':end,'utc_day':day(start),'resources':resources}
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):fail('X_COST_RECEIPT_NOT_VERIFIED')


def saved_raw(root,row):
    try:
        owner=str(uuid.UUID(row['discovery_job_id']));ident=str(uuid.UUID(row['call_id']))
        if owner!=row['discovery_job_id'] or ident!=row['call_id']:raise ValueError()
        root=Path(root);file=root/'media'/owner/'source-receipts'/(ident+'.json')
        if file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):raise ValueError()
        fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=5*1024*1024:raise ValueError()
            raw=stream.read(5*1024*1024+1);after=os.fstat(stream.fileno())
        current=file.lstat()
        keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        # Windows fstat exposes ctime differently from path.lstat; Linux keeps
        # the strict metadata-change check for the qualified production path.
        path_keys=keys if os.name=='posix' else keys[:-1]
        if (len(raw)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in keys) or
            any(getattr(before,k)!=getattr(current,k) for k in path_keys)):raise ValueError()
        return file,raw
    except (ValueError,TypeError,OSError):fail('X_COST_SAVED_RECEIPT_REQUIRED')


def _put(db,value):
    previous=db.execute('SELECT * FROM x_cost_observations WHERE call_id=?',(value['call_id'],)).fetchone()
    body={**value,'resources':encode(value['resources'])}
    if previous and previous['state']!='pending':
        if any(previous[k]!=v for k,v in body.items()):fail('X_COST_OBSERVATION_CHANGED')
        return False
    if previous and any(previous[k]!=body[k] for k in ('version','raw_sha256','canonical_sha256','started_at','finished_at','utc_day')):fail('X_COST_OBSERVATION_CHANGED')
    db.execute('INSERT OR REPLACE INTO x_cost_observations VALUES(?,?,?,?,?,?,?,?,?,?)',
        (*[body[k] for k in ('call_id','version','state','raw_sha256','canonical_sha256','started_at','finished_at','utc_day','resources')],time.time()))
    db.execute('INSERT INTO x_cost_events VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),value['call_id'],VERSION,value['state'],encode(value),time.time()))
    return True


def note_receipt(store,call_id):
    """HTTP has a DB receipt, but no durable raw-file credit yet."""
    with store.transaction() as db:
        if not installed(db):return
        rows=[r for r in _rows(db) if r['call_id']==call_id]
        if len(rows)!=1:fail('X_COST_CALL_REQUIRED')
        r=rows[0]
        if r['state']!='done' or not r['raw_sha256']:fail('X_COST_CALL_REQUIRED')
        if db.execute('SELECT 1 FROM x_cost_observations WHERE call_id=?',(call_id,)).fetchone():return
        _put(db,{'call_id':call_id,'version':VERSION,'state':'pending','raw_sha256':r['raw_sha256'],'canonical_sha256':r['canonical_sha256'],
            'started_at':r['started_at'],'finished_at':r['finished_at'],'utc_day':day(r['started_at']),'resources':[]})


def confirm_saved_receipt(root,store,call_id):
    with closing(store.connect()) as db:
        if not installed(db):return
        rows=[r for r in _rows(db) if r['call_id']==call_id]
    if len(rows)!=1:fail('X_COST_CALL_REQUIRED')
    row=rows[0]
    # A custom transport is an offline test, never evidence of a real charge.
    if row['transport_mode']!='default_tls':return
    _,raw=saved_raw(root,row);value=observation(row,raw)
    with store.transaction() as db:
        current=[r for r in _rows(db) if r['call_id']==call_id]
        if current!=[row]:fail('X_COST_CALL_CHANGED')
        _put(db,value)


def estimates(rows,observations,starts):
    byid={r['call_id']:r for r in rows};trusted={};first={}
    for value in observations:
        r=byid.get(value['call_id'])
        if value['state']!='verified':continue
        if (not r or value['version']!=VERSION or r['state']!='done' or r['resource_state']!='estimated' or
            any(value[k]!=r[k] for k in ('raw_sha256','canonical_sha256','started_at','finished_at')) or
            day(r['started_at'])!=day(r['finished_at']) or value['utc_day']!=day(r['started_at'])):fail('X_COST_LEDGER_BINDING_CHANGED')
        trusted[r['call_id']]=value
    for r in sorted(rows,key=lambda v:(v['started_at'],v['call_id'])):
        if r['call_id'] not in trusted:continue
        for item in trusted[r['call_id']]['resources']:
            if item['kind'] not in MICROS or item['unit_microusd']!=MICROS[item['kind']] or not re.fullmatch('[0-9]{1,30}',item['id']):fail('X_COST_LEDGER_BINDING_CHANGED')
            first.setdefault((day(r['started_at']),item['kind'],item['id']),(r['started_at'],item['unit_microusd']))
    output={}
    for label,start in starts.items():
        selected=[r for r in rows if r['started_at']>=start]
        gross=sum(r['cost_usd'] for r in selected);unknown=[r for r in selected if r['call_id'] not in trusted]
        residual=sum(r['cost_usd'] for r in unknown);unique=sum(v for at,v in first.values() if at>=start)/1_000_000
        output[label]={'gross_conservative_upper_bound_usd':round(gross,9),'unique_resources_estimate_usd':round(unique,9),
            'unverified_gross_retained_usd':round(residual,9),'budget_estimate_with_unverified_usd':round(unique+residual,9),
            'http_attempts':len(selected),'verified_receipt_calls':len(selected)-len(unknown),'unverified_calls':len(unknown)}
    return output


def load_observations(db):
    values=[]
    for r in db.execute('SELECT * FROM x_cost_observations'):
        value={**dict(r),'resources':json.loads(r['resources'])};body={k:v for k,v in value.items() if k!='recorded_at'}
        if not db.execute('SELECT 1 FROM x_cost_events WHERE call_id=? AND version=? AND kind=? AND record=?',
            (value['call_id'],VERSION,value['state'],encode(body))).fetchone():fail('X_COST_LEDGER_BINDING_CHANGED')
        values.append(value)
    return values


def assert_ready(db):
    if not installed(db):fail('X_COST_CALIBRATION_REQUIRED')
    value=db.execute('SELECT * FROM x_cost_control WHERE id=1').fetchone()
    if not value or value['version']!=VERSION or json.loads(value['source_files'])!=source_hashes():fail('X_COST_CALIBRATION_REQUIRED')
    calibration=db.execute('SELECT version FROM x_cost_calibrations WHERE calibration_id=?',(value['calibration_id'],)).fetchone()
    if not calibration or calibration['version']!=VERSION:fail('X_COST_CALIBRATION_REQUIRED')


def period_cost(db,start):
    assert_ready(db)
    return estimates(_rows(db),load_observations(db),{'period':start})['period']['budget_estimate_with_unverified_usd']


def record_reservation(db,call_id,limit,gross_bound):
    body={'version':VERSION,'basis':'utc_unique_estimate_plus_unverified_gross',
        'pre_request_full_gross_bound_usd':gross_bound,'daily_usd':limit['daily_usd'],'monthly_usd':limit['monthly_usd'],
        'pricing_source':SOURCE,'deduplication_soft_guarantee':True,'invoice_charge_verified':False,'source_files':source_hashes()}
    db.execute('INSERT INTO x_cost_events VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),call_id,VERSION,'full_gross_reservation',encode(body),time.time()))


def report(store,starts):
    with closing(store.connect()) as db:
        if not installed(db):return {'version':VERSION,'state':'migration_required','invoice_charge_verified':False}
        ready=False
        try:assert_ready(db);ready=True
        except HubError:pass
        return {'version':VERSION,'calibration_ready':ready,'budget_period_timezone':'Asia/Shanghai','resource_dedup_timezone':'UTC',
            'pricing_source':SOURCE,'deduplication_soft_guarantee':True,'invoice_charge_verified':False,'invoice_amount_usd':None,
            'periods':estimates(_rows(db),load_observations(db),starts)}


def calibration_plan(root,store,*,at=None):
    at=time.time() if at is None else at
    with closing(store.connect()) as db:
        if not installed(db):fail('X_COST_MIGRATION_REQUIRED')
        rows=_rows(db)
    if len(rows)>20000:fail('X_COST_CALIBRATION_SCOPE_TOO_LARGE')
    files=[];values=[];missing=[];total=0
    for row in rows:
        if row['state']!='done' or row['transport_mode']!='default_tls' or not row['raw_sha256']:continue
        try:file,raw=saved_raw(root,row)
        except HubError as exc:
            if exc.code!='X_COST_SAVED_RECEIPT_REQUIRED':raise
            missing.append(row['call_id']);continue
        total+=len(raw)
        if total>256*1024*1024:fail('X_COST_CALIBRATION_SCOPE_TOO_LARGE')
        values.append(observation(row,raw));files.append({'call_id':row['call_id'],'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    local=datetime.fromtimestamp(at,ZoneInfo('Asia/Shanghai'));utc=datetime.fromtimestamp(at,timezone.utc)
    starts={'budget_daily_shanghai':local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp(),
        'budget_monthly_shanghai':local.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp(),
        'utc_day':utc.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()}
    return {'version':VERSION,'plan_id':str(uuid.uuid4()),'created_at':at,'source_files':source_hashes(),'snapshot_sha256':digest(rows),
        'receipt_files':files,'missing_raw_calls':missing,'observations':values,'periods':estimates(rows,values,starts),
        'invoice_charge_verified':False,'deduplication_soft_guarantee':True,'history_reset':False,'limits_changed':False}


def apply_calibration(root,store,plan):
    if plan['version']!=VERSION or not 0<=time.time()-plan['created_at']<=3600:fail('X_COST_CALIBRATION_PLAN_EXPIRED')
    fresh=calibration_plan(root,store,at=plan['created_at'])
    if any(fresh[k]!=plan[k] for k in ('source_files','snapshot_sha256','receipt_files','missing_raw_calls','observations','periods')):fail('X_COST_CALIBRATION_PLAN_CHANGED')
    with store.transaction() as db:
        if digest(_rows(db))!=plan['snapshot_sha256']:fail('X_COST_CALIBRATION_PLAN_CHANGED')
        before=history_fingerprints(db)
        previous=db.execute('SELECT * FROM x_cost_calibrations WHERE calibration_id=?',(plan['plan_id'],)).fetchone()
        if previous:
            if previous['version']!=VERSION or previous['record']!=encode(plan):fail('X_COST_CALIBRATION_PLAN_CHANGED')
            return {'version':VERSION,'state':'already_calibrated_config_not_enabled','calibration_id':plan['plan_id'],
                'periods':plan['periods'],'api_history_changed':False,'limits_changed':False,
                'history_before':before,'history_after':before}
        for value in plan['observations']:_put(db,value)
        db.execute('INSERT INTO x_cost_calibrations VALUES(?,?,?,?)',(plan['plan_id'],VERSION,encode(plan),time.time()))
        db.execute('INSERT OR REPLACE INTO x_cost_control VALUES(1,?,?,?)',(VERSION,plan['plan_id'],encode(plan['source_files'])))
        after=history_fingerprints(db)
        if after!=before:fail('X_COST_ORIGINAL_HISTORY_CHANGED')
        db.execute('INSERT INTO x_cost_events VALUES(?,NULL,?,?,?,?)',(str(uuid.uuid4()),VERSION,'calibration_history_guard',
            encode({'calibration_id':plan['plan_id'],'history_before':before,'history_after':after}),time.time()))
        store._event(db,'budget.x_utc_calibrated',{'calibration_id':plan['plan_id'],'version':VERSION,'snapshot_sha256':plan['snapshot_sha256'],'invoice_charge_verified':False})
    return {'version':VERSION,'state':'calibrated_config_not_enabled','calibration_id':plan['plan_id'],'periods':plan['periods'],
        'api_history_changed':False,'limits_changed':False,'history_before':before,'history_after':after}
