"""Exact one-file cleanup for the already-public Highest pilot master.

Default is a metadata-only dry plan. Explicit full-SHA planning precedes apply,
which requires the exact saved plan SHA and persistent one-file deletion intent.
No browser, network or publication reconciliation is performed.
The old eight/43 gates and pre-submit files are never modified. This separate
cleanup basis checks six intact historical references plus actual middle/end
playback; it does not claim the overwritten seventh reference was recovered.
"""
from pathlib import Path
import argparse,hashlib,json,math,os,re,sqlite3,stat,time,uuid
from urllib.parse import urlsplit

from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.pilot_published_retention_v2 import PilotPublishedRetentionV2
from services.hub.replay_highest_coverage_v2 import upload_coverage
from services.hub.archive_source_retention_v2 import _inspect,_path

VERSION='published-pilot-master-confirmation-plan-v2'
LEGACY_SHA='a3eb7ec361a4eebe5ecfb46bbe10884a7b9255f12aaa68197e5e766b8b27cf9c'
LEGACY_FILE=Path('/run/pilot-master-v1.py')
PLAYBACK_FOLDER='public-known-bv-playback-v2/6c182453-0ca8-47eb-af3f-57ce6ad27102'
PLAYBACK_SHA='b689ddaa610a51327763c33cfe32273fad059c622f4f412e0c08f51229c4e20a'
QUALIFICATION_SHA='5e85d6e51ad4ad38d4286a12e14827ee8eb5de56a470fea7d3de34b7555311cb'
SIX_REFS=('public_observation','screenshot','manager_observation','original_public_submit','original_unknown_result','original_machine_proof')
RECEIPT_VERSION='published-pilot-master-confirmed-cleanup-receipt-v2'
PRINCIPAL='operator:pilot-master-confirmed-retention-v2'
MARKER='local_original_retention_v2'


def need(value,code):
    if not value:raise HubError(code,review=True)
def finite(value):return type(value) in (int,float) and math.isfinite(value)
def file_sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def validate_playback(ret,legacy,later,binding,duration,reference,submitted_at):
    folder=ret.root/'media'/legacy.JOB/PLAYBACK_FOLDER
    need(Path(reference['file'])==folder/'public-native-inventory.json' and reference['sha256']==PLAYBACK_SHA,'PILOT_PUBLIC_PLAYBACK_ORIGINAL_REQUIRED')
    raw=later.read_reference(ret.root/'media'/legacy.JOB,reference,maximum=2_000_000);value=json.loads(raw)
    observed=later.when(value['observed_at'])
    need(value['version']=='known-bv-public-playback-inventory-v2' and value['job_id']==legacy.JOB and
        value['attempt_id']==legacy.ATTEMPT and value['external_id']==legacy.BV and value['expected_title']==binding['metadata']['title'] and
        submitted_at<=observed<=time.time() and value['source_or_media_modified'] is False and
        all(type(value[k]) is int and value[k]==0 for k in ('file_selections','save_clicks','submit_clicks')) and
        type(value['player_seeks']) is int and value['player_seeks']==2,'PILOT_PUBLIC_PLAYBACK_BINDING_CHANGED')
    dom=value['visible_dom'];url=urlsplit(dom['url'])
    need(url.scheme=='https' and url.netloc=='www.bilibili.com' and url.path in ('/video/'+legacy.BV,'/video/'+legacy.BV+'/') and
        not url.query and not url.fragment and len(dom['titles'])==1 and dom['titles'][0]['text']==binding['metadata']['title'],'PILOT_PUBLIC_PLAYBACK_BINDING_CHANGED')
    owners=[o for o in dom['owner_candidates'] if o.get('visible') is True and 'up-name' in o.get('class','').split()]
    need(len(owners)==1,'PILOT_PUBLIC_PLAYBACK_OWNER_REQUIRED');owner=owners[0];u=urlsplit(owner['url'])
    need(owner['text']==binding['account_name'] and owner['text'] in owner['card_text'] and u.scheme=='https' and
        u.netloc=='space.bilibili.com' and u.path.rstrip('/')=='/'+binding['site_uid'] and not u.query and not u.fragment,'PILOT_PUBLIC_PLAYBACK_OWNER_REQUIRED')
    descriptions=[d for d in dom['descriptions'] if d.get('visible') is True and 'basic-desc-info' in d.get('class','').split()]
    need(len(descriptions)==1 and descriptions[0]['text'] in (binding['metadata']['description'],binding['source_url']+'\n'+binding['metadata']['description']),
        'PILOT_PUBLIC_PLAYBACK_SOURCE_REQUIRED')
    def player(v):
        ranges=v.get('seekable_ranges')
        return (v.get('visible') is True and finite(v.get('duration_seconds')) and abs(v['duration_seconds']-duration)<=2 and
            finite(v.get('current_time')) and 0<=v['current_time']<=duration+1 and type(v.get('ready_state')) is int and
            2<=v['ready_state']<=4 and v.get('paused') is False and v.get('error_code') is None and
            type(v.get('video_width')) is int and v['video_width']>0 and type(v.get('video_height')) is int and v['video_height']>0 and
            isinstance(ranges,list) and 1<=len(ranges)<=100 and all(set(r)=={'start','end'} and finite(r['start']) and
                finite(r['end']) and 0<=r['start']<r['end']<=duration+1 for r in ranges) and
            any(r['start']<=1 and r['end']>=duration-2 for r in ranges))
    videos=[v for v in dom['videos'] if v.get('visible') is True]
    need(len(videos)==1 and player(videos[0]),'PILOT_PUBLIC_PLAYBACK_PLAYER_REQUIRED')
    samples=dom['playback_samples'];need(len(samples)==2 and [s['label'] for s in samples]==['middle','near-end'],'PILOT_PUBLIC_PLAYBACK_SAMPLES_REQUIRED')
    previous=submitted_at;screenshots=[]
    for sample,label,target in zip(samples,('middle','near-end'),(duration*.5,max(0,duration-10))):
        at=later.when(sample['observed_at']);selected=[v for v in sample['videos'] if v.get('visible') is True]
        need(previous<=at<=observed and observed-at<=120 and sample['play_requested'] is True and finite(sample['target_time']) and
            abs(sample['target_time']-target)<=1 and len(selected)==1 and player(selected[0]) and
            .1<selected[0]['current_time']-sample['target_time']<=15,'PILOT_PUBLIC_PLAYBACK_NOT_ADVANCED')
        ref=sample['screenshot'];need(Path(ref['file'])==folder/('public-'+label+'.png'),'PILOT_PUBLIC_PLAYBACK_SCREENSHOT_REQUIRED')
        png=later.read_reference(ret.root/'media'/legacy.JOB,ref,maximum=20_000_000)
        need(len(png)>=33 and png[:16]==b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' and
            0<int.from_bytes(png[16:20],'big')<=10000 and 0<int.from_bytes(png[20:24],'big')<=10000,'PILOT_PUBLIC_PLAYBACK_SCREENSHOT_REQUIRED')
        screenshots.append(ref);previous=at
    return {'reference':reference,'screenshots':screenshots,'observed_at':value['observed_at'],
        'middle_and_near_end_playback_confirmed':True,'source_duration_seconds':duration,
        'historical_confirmation':True,'current_access_requeried':False}

def confirmation(ret,legacy,later,playback_reference):
    job,binding,state,source,pub=ret._context(legacy.JOB);evidence=state.get('result_reconciliation',{})
    need(legacy.digest(evidence)==legacy.EVIDENCE_SHA and evidence.get('version')==later.VERSION and evidence.get('origin')==later.ORIGIN and
        evidence.get('binding_sha256')==legacy.digest(binding) and all(evidence.get(k) is False for k in
            ('same_submit_attempt_observed','human_review','publication_authorization_inferred')),'PILOT_PUBLIC_COMPLETED_AUDIT_REQUIRED')
    accepted=[]
    for row in ret.store.query('SELECT at,principal,outcome FROM audit WHERE action=? AND target=? ORDER BY at',(later.VERSION,legacy.JOB)):
        try:outcome=json.loads(row['outcome'])
        except (ValueError,TypeError):continue
        if (row['principal']=='operator:ssh-sudo' and finite(row['at']) and 0<=row['at']<=time.time() and
            outcome=={'origin':later.ORIGIN,'evidence_sha256':legacy.EVIDENCE_SHA,'same_submit_attempt_observed':False}):accepted.append(row['at'])
    need(len(accepted)==1,'PILOT_PUBLIC_COMPLETED_AUDIT_REQUIRED')
    refs={k:evidence[k] for k in SIX_REFS}
    raw={k:later.read_reference(ret.root/'media'/legacy.JOB,ref,maximum=20_000_000) for k,ref in refs.items()}
    duration=source['result']['youtube_download_v2']['binding']['duration']
    public=later.validate_public(json.loads(raw['public_observation']),{**binding,'job_id':legacy.JOB},legacy.ATTEMPT,duration,now=accepted[0])
    posted=later.validate_manager(json.loads(raw['manager_observation']),{**binding,'job_id':legacy.JOB},legacy.BV)
    intent=json.loads(raw['original_public_submit']);submitted=later.when(intent['created_at'])
    need(public['external_id']==legacy.BV and public['url']==pub['external_url'] and submitted<=posted<=later.when(public['observed_at'])+60 and
        legacy.sha(raw['original_public_submit'])==legacy.SUBMIT_SHA and
        json.loads(raw['original_unknown_result'])=={'state':'unknown','submitted':None,'reconcile_required':True},'PILOT_PUBLIC_COMPLETED_MAPPING_CHANGED')
    uploaded=upload_coverage(ret.settings,job,binding,state,state['upload_observation']['upload_coverage'],fresh=False)
    need(uploaded==state['upload_coverage'],'PILOT_PUBLIC_ORIGINAL_UPLOAD_CHANGED')
    playback=validate_playback(ret,legacy,later,binding,duration,playback_reference,submitted)
    return {'historical_confirmation':True,'current_access_requeried':False,'external_requests':0,
        'external_id':legacy.BV,'accepted_audit_at':accepted[0],'reconciliation_sha256':legacy.EVIDENCE_SHA,
        'intact_historical_references':refs,'public_playback':playback,
        'pre_submit_inventory_used':False,'unavailable_legacy_reference_preserved':evidence.get('manager_before_submit'),
        'meaning':'cleanup after independently confirmed public playback; no publication permission or replay of submit'}

def create_plan(ret,legacy,later,playback_reference,*,verify_current_master=False):
    need(type(verify_current_master) is bool,'PILOT_PUBLIC_PLAN_MODE_INVALID')
    before=legacy.context(ret);accepted=confirmation(ret,legacy,later,playback_reference)
    expected,packet=legacy.prior_proof(ret);file=legacy.MASTER;opened=file.lstat()
    fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:current=os.fstat(fd)
    finally:os.close(fd)
    parent=file.parent.stat()
    metadata={'file':str(file),'bytes':current.st_size,'device':current.st_dev,'inode':current.st_ino,
        'mtime_ns':current.st_mtime_ns,'parent_device':parent.st_dev,'parent_inode':parent.st_ino}
    need(stat.S_ISREG(current.st_mode) and current.st_nlink==1 and
        all(metadata[k]==expected[k] for k in metadata),'PILOT_PUBLIC_CURRENT_MASTER_CHANGED')
    actual=_inspect(ret.root,file,14_000_000_000) if verify_current_master else {**metadata,'expected_sha256':expected['sha256']}
    after=file.lstat()
    need((not verify_current_master or actual==expected) and
        all(getattr(opened,k)==getattr(current,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns')) and
        opened.st_ctime_ns==after.st_ctime_ns and (os.name!='posix' or opened.st_ctime_ns==current.st_ctime_ns),
        'PILOT_PUBLIC_CURRENT_MASTER_CHANGED')
    need(legacy.context(ret)==before,'PILOT_PUBLIC_CONTEXT_CHANGED')
    ret._release({'job_id':legacy.JOB,'scope':legacy.PUBLISHED,'authorization_id':legacy.AUTH,'proof':{'pipeline_state':'replay_highest_video_web_v2'}})
    return {'version':VERSION,'state':'full_sha_reviewable_plan' if verify_current_master else 'metadata_only_plan',
        'plan_id':str(uuid.uuid4()),'created_at':time.time(),'operator_sha256':legacy.sha(legacy.read(Path(__file__).resolve())),
        'job_id':legacy.JOB,'download_job_id':legacy.DOWNLOAD,'source_id':legacy.SOURCE,'external_id':legacy.BV,
        'authorization_id':legacy.AUTH,'authorization_sha256':legacy.AUTH_SHA,'candidate_count':1,
        'candidate':actual,'ctime_ns':opened.st_ctime_ns,'snapshot':before,'historical_public_confirmation':accepted,
        'local_packet_basis':packet,'platform_requests':0,'files_deleted':0,'apply_allowed':verify_current_master,
        'current_master_full_sha_verified':verify_current_master,'full_sha_required_before_any_apply':True,
        'raw_files_already_deleted':2,'private_samples_retained':True,'publication_changed':False,
        'old_eight_and_43_qualification_unchanged':True,'new_cleanup_confirmation_not_an_old_eight_qualification':True}

def apply_plan(ret,legacy,later,plan,plan_sha,index):
    """Caller holds video/moderation/heavy; only a reviewed full-SHA plan applies.

    This is the old one-master journal/double-marker protocol with the separate
    six-reference plus public-playback confirmation. No seventh ref is repaired.
    """
    need(plan.get('version')==VERSION and plan.get('candidate_count')==1 and
        all(plan.get(k)==v for k,v in {'job_id':legacy.JOB,'download_job_id':legacy.DOWNLOAD,'source_id':legacy.SOURCE,
            'external_id':legacy.BV,'authorization_id':legacy.AUTH,'authorization_sha256':legacy.AUTH_SHA}.items()) and
        plan.get('apply_allowed') is True and plan.get('current_master_full_sha_verified') is True and
        plan.get('state')=='full_sha_reviewable_plan' and plan_sha==legacy.digest(plan),'PILOT_PUBLIC_EXACT_FULL_SHA_PLAN_REQUIRED')
    need(plan.get('operator_sha256')==legacy.sha(legacy.read(Path(__file__).resolve())),'PILOT_PUBLIC_OPERATOR_CHANGED')
    try:need(str(uuid.UUID(plan['plan_id']))==plan['plan_id'],'PILOT_PUBLIC_EXACT_FULL_SHA_PLAN_REQUIRED')
    except (ValueError,TypeError,AttributeError):raise HubError('PILOT_PUBLIC_EXACT_FULL_SHA_PLAN_REQUIRED',review=True)
    ret._release({'job_id':legacy.JOB,'scope':legacy.PUBLISHED,'authorization_id':legacy.AUTH,
        'proof':{'pipeline_state':'replay_highest_video_web_v2'}})
    index.execute('PRAGMA synchronous=FULL')
    index.execute('CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,plan_sha TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL)')
    row=index.execute('SELECT plan_sha,state,record FROM operations WHERE id=?',(plan['plan_id'],)).fetchone()
    need(not row or row[0]==plan_sha,'PILOT_PUBLIC_EXACT_FULL_SHA_PLAN_REQUIRED')
    body=json.loads(row[2]) if row else None
    need(not row or row[1] in ('deleting','completed') and
        (row[1]=='completed' or body=={'plan':plan}),'PILOT_PUBLIC_JOURNAL_CHANGED')
    receipt=None
    # BEGIN IMMEDIATE preserves the mature DB CAS boundary through unlink and
    # local marker/audit writes. OS locks are held by main throughout this call.
    with ret.store.transaction() as db:
        current=ret.store.get(legacy.JOB);source=ret.store.get(legacy.DOWNLOAD)
        a=current['result'].get(MARKER);b=source['result'].get(MARKER)
        if a is not None or b is not None or row and row[1]=='completed':
            need(row is not None and a==b and isinstance(a,dict) and
                all(a.get(k)==v for k,v in {'version':RECEIPT_VERSION,'plan_id':plan['plan_id'],'plan_sha256':plan_sha,
                    'job_id':legacy.JOB,'download_job_id':legacy.DOWNLOAD,'source_id':legacy.SOURCE,'external_id':legacy.BV,
                    'file':str(legacy.MASTER),'file_sha256':legacy.MASTER_SHA,'file_bytes':legacy.BYTES,
                    'authorization_id':legacy.AUTH,'local_files_deleted':1}.items()) and
                not os.path.lexists(legacy.MASTER),'PILOT_PUBLIC_RECOVERY_CHANGED')
            need(row[1]!='completed' or body==a,'PILOT_PUBLIC_RECOVERY_CHANGED')
            observed=legacy.context(ret)
            need(all(observed[k]==plan['snapshot'][k] for k in ('publication','authorization')),'PILOT_PUBLIC_RECOVERY_CHANGED')
            for value,key in ((current,'job'),(source,'source')):
                original=json.loads(encode(value));original['result'].pop(MARKER)
                original['updated_at']=plan['snapshot']['updated_at'][value['job_id']]
                need(legacy.digest(original)==plan['snapshot'][key],'PILOT_PUBLIC_RECOVERY_CHANGED')
            audits=db.execute('SELECT principal,outcome FROM audit WHERE action=? AND target=?',(RECEIPT_VERSION,legacy.JOB)).fetchall()
            need(len(audits)==1 and audits[0]['principal']==PRINCIPAL and json.loads(audits[0]['outcome'])==a,'PILOT_PUBLIC_RECOVERY_CHANGED')
            receipt=a
        else:
            need(legacy.context(ret)==plan['snapshot'],'PILOT_PUBLIC_CONTEXT_CHANGED')
        need(confirmation(ret,legacy,later,plan['historical_public_confirmation']['public_playback']['reference'])==
            plan['historical_public_confirmation'],'PILOT_PUBLIC_CONFIRMATION_CHANGED')
        expected,packet=legacy.prior_proof(ret)
        need(plan['candidate']==expected and plan['local_packet_basis']==packet,'PILOT_PUBLIC_EXACT_FULL_SHA_PLAN_REQUIRED')
        ret._unshared(db,[str(legacy.MASTER)],legacy.JOB,legacy.DOWNLOAD)
        if receipt is None:
            need(os.path.lexists(legacy.MASTER) or row is not None,'PILOT_PUBLIC_UNEXPECTED_MISSING')
            # A missing path is recoverable only after this exact FULL journal
            # commit. Current inode/ctime fences later writes without a rescan.
            index.execute('INSERT OR IGNORE INTO operations VALUES(?,?,?,?)',
                (plan['plan_id'],plan_sha,'deleting',encode({'plan':plan})));index.commit()
            if os.name=='posix':
                folder=Path(index.execute('PRAGMA database_list').fetchone()[2]).parent
                fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(fd)
                finally:os.close(fd)
            if os.path.lexists(legacy.MASTER):ret._stat_unlink(plan['candidate'],plan['ctime_ns'])
            receipt={'version':RECEIPT_VERSION,'plan_id':plan['plan_id'],'plan_sha256':plan_sha,'job_id':legacy.JOB,
                'download_job_id':legacy.DOWNLOAD,'source_id':legacy.SOURCE,'external_id':legacy.BV,'scope':legacy.PUBLISHED,
                'file':str(legacy.MASTER),'file_sha256':legacy.MASTER_SHA,'file_bytes':legacy.BYTES,'local_files_deleted':1,
                'total_logical_bytes_deleted':legacy.BYTES,'historical_confirmation':True,'current_access_requeried':False,
                'platform_requests':0,'publication_records_preserved':True,'source_id_idempotency_records_preserved':True,
                'remote_video_deleted':False,'evidence_files_deleted':False,'private_samples_retained':True,
                'authorization_id':legacy.AUTH,'deleted_at':time.time()}
            for value in (current,source):
                db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',
                    (encode({**value['result'],MARKER:receipt}),time.time(),value['job_id']))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (time.time(),PRINCIPAL,RECEIPT_VERSION,legacy.JOB,encode(receipt)))
    index.execute('UPDATE operations SET state=?,record=? WHERE id=?',('completed',encode(receipt),plan['plan_id']))
    index.commit();return receipt

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--legacy-master-helper',type=Path,default=LEGACY_FILE)
    parser.add_argument('--qualification',type=Path,required=True);parser.add_argument('--qualification-sha256',required=True)
    parser.add_argument('--verify-current-master',action='store_true',help='Opt in to one complete SHA scan only in an idle heavy-lock window.')
    parser.add_argument('--apply',action='store_true');parser.add_argument('--plan',type=Path);parser.add_argument('--plan-sha256')
    args=parser.parse_args();need(os.name=='posix' and os.getuid()==os.getgid()==10001 and __debug__,'PILOT_PLAN_APP_UID_REQUIRED')
    need(args.apply==bool(args.plan and args.plan_sha256) and not (not args.apply and (args.plan or args.plan_sha256)) and
        not (args.apply and args.verify_current_master),'PILOT_PUBLIC_EXACT_FULL_SHA_PLAN_REQUIRED')
    os.umask(0o077)
    import types
    path=args.legacy_master_helper;need(path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents)) and
        path.stat().st_size<100000 and file_sha(path)==LEGACY_SHA,'PILOT_PLAN_LEGACY_SOURCE_CHANGED')
    legacy=types.ModuleType('qualified_pilot_master_v1');legacy.__file__=str(path);exec(compile(path.read_bytes(),str(path),'exec'),legacy.__dict__)
    need(args.qualification_sha256==QUALIFICATION_SHA,'PILOT_PLAN_ORIGINAL_43_REQUIRED')
    helper=legacy.load(legacy.HELPER_FILE,legacy.HELPER_SHA,'qualified_current45_cleanup')
    helper.qualify(legacy.ROOT,file=args.qualification,checksum=QUALIFICATION_SHA)
    later=legacy.load(legacy.LATER_FILE,legacy.LATER_SHA,'services.hub.qualified_later_public_v1')
    settings=Settings.load();ret=PilotPublishedRetentionV2(settings,Store(settings.db_path))
    names=('video','moderation')+(('heavy',) if args.verify_current_master or args.apply else ())
    with resource_guard(*(settings.root/'data/locks'/name for name in names)):
        folder=_path(settings.root,settings.root/'data/pilot-master-confirmation-plan-v2');folder.mkdir(mode=0o700,exist_ok=True)
        info=folder.stat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid==10001 and stat.S_IMODE(info.st_mode)==0o700,'PILOT_PLAN_PRIVATE_DIRECTORY_REQUIRED')
        if args.apply:
            path=_path(settings.root,args.plan);raw=legacy.read(path);value=json.loads(raw)
            need(path.parent==folder and path.name==str(uuid.UUID(value['plan_id']))+'.json' and
                legacy.sha(raw)==args.plan_sha256,'PILOT_PUBLIC_EXACT_FULL_SHA_PLAN_REQUIRED')
            index_path=_path(settings.root,folder/'intents.sqlite3')
            if index_path.exists():
                info=index_path.lstat();need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==10001 and
                    stat.S_IMODE(info.st_mode)&0o077==0,'PILOT_PUBLIC_JOURNAL_CHANGED')
            with sqlite3.connect(index_path) as index:
                index.execute('PRAGMA journal_mode=WAL');result=apply_plan(ret,legacy,later,value,args.plan_sha256,index)
            print(encode({'state':'exact_master_only_deleted','receipt':result}));return
        path=settings.root/'media'/legacy.JOB/PLAYBACK_FOLDER/'public-native-inventory.json'
        raw=legacy.read(path,2_000_000);reference={'file':str(path),'bytes':len(raw),'sha256':legacy.sha(raw)}
        plan=create_plan(ret,legacy,later,reference,verify_current_master=args.verify_current_master)
        output=folder/(plan['plan_id']+'.json');checksum=legacy.write(output,plan)
        print(json.dumps({'state':plan['state'],'plan_file':str(output),'plan_sha256':checksum,'candidate':plan['candidate'],
            'files_deleted':0,'platform_requests':0,'apply_allowed':plan['apply_allowed']}))

if __name__=='__main__':main()
