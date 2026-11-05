"""Read-only current-runtime acceptance; never retries deployment or changes jobs."""
from pathlib import Path
import argparse,hashlib,json,os,sys,time,types,uuid,urllib.request

OPERATOR_SHA='0518420bb62817b8d4384b8868ad7ff96fcae3d4c274efc3ae68cd21ef8e6f64'
def load(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents))
    with file.open('rb') as stream:raw=stream.read(200001)
    assert hashlib.sha256(raw).hexdigest()==OPERATOR_SHA
    module=types.ModuleType('readonly_x_relations_operator');module.__file__=str(file)
    exec(compile(raw,str(file),'exec'),module.__dict__);return module
def env(values):
    result=dict(v.split('=',1) for v in values);assert len(result)==len(values);return result
def tmpfs(value):
    def options(text):
        result=dict((v.split('=',1)+[True])[:2] for v in text.split(',') if v and v!='rw')
        if 'size' in result:
            number=str(result['size']).lower();result['size']=int(number[:-1])*{'k':1024,'m':1024**2,'g':1024**3}[number[-1]] if number[-1] in 'kmg' else int(number)
        if 'mode' in result:result['mode']=int(str(result['mode']),8)
        return result
    if isinstance(value,list):value=dict(v.split(':',1) for v in value)
    return {key:options(text) for key,text in value.items()}
def accept(args):
    m=load(args.operator_file);d=m.load_helper(args.base_helper);path=d.safe(Path(args.deployment_report));d.protected(path)
    raw=d.read(path,500000);assert m.sha(raw)==args.deployment_sha256;original=json.loads(raw)
    assert original['version']=='x-relations-overlay-deployment-v1' and original['state']=='compose_overlay_installed_restart_pending'
    assert original['hub_sha256']==m.HUB_SHA and original['policy_activated'] is False
    bodies,manifest,qualification=m.qualification(d,original['qualification']['file'],original['qualification']['sha256'],args.archive)
    assert qualification==original['qualification'] and manifest['runtime_source_sha256']==original['production_sources']
    before=d.parsed(path=path.parent/'compose-before.yaml');current=d.parsed();folder=m.INSTALL/m.MANIFEST_SHA
    assert current==m.candidate(before,folder)
    cid=d.cid('tweet-worker');actual=d.inspect(cid);assert actual['State']['Running'] and actual['Image']==m.CBC
    names=list(original['source_hashes_before']);expected={**original['source_hashes_before'],**manifest['runtime_source_sha256']}
    assert m.hashes(d,cid,names)==expected and expected['compositor/src/core.mjs']==m.CORE_SHA
    assert m.identities(d)==original['other_four']
    assert all(m.sha(d.read(Path(p),2000000))==value for p,value in original['fixed_config_sha256'].items())
    declared=current['services']['tweet-worker'];hc=actual['HostConfig'];config=actual['Config']
    declared_bind={v['target']:(v['source'],not v.get('read_only',False)) for v in declared['volumes']}
    assert len(declared_bind)==len(declared['volumes'])==20 and all(v['type']=='bind' for v in declared['volumes'])
    actual_bind={v['Destination']:(v['Source'],v['RW']) for v in actual['Mounts'] if v['Type']=='bind'}
    assert len(actual_bind)==20 and actual_bind==declared_bind and all(v['Type'] in ('bind','tmpfs') for v in actual['Mounts'])
    assert tmpfs(hc['Tmpfs'])==tmpfs(declared['tmpfs'])
    full=json.loads(d.compose('config','--format','json',path=path.parent/'compose-before.yaml'))['services']['tweet-worker']
    image=json.loads(d.run(['/usr/bin/docker','image','inspect',m.CBC]))[0]['Config'];expected_env=env(image.get('Env') or [])
    for key,value in full.get('environment',{}).items():
        if value is None:expected_env.pop(key,None)
        else:expected_env[key]=str(value)
    assert env(config['Env'])==expected_env
    assert config['User']==full.get('user',image.get('User'))=='10001:10001'
    assert config['Cmd']==full.get('command',image.get('Cmd')) and config['Entrypoint']==full.get('entrypoint',image.get('Entrypoint'))
    assert config['WorkingDir']==full.get('working_dir',image.get('WorkingDir'))
    assert config['Hostname']==full.get('hostname',cid[:12])
    assert hc['Memory']==int(full['mem_limit']) and hc['PidsLimit']==int(full['pids_limit']) and hc['ShmSize']==int(full['shm_size'])
    assert hc['NanoCpus']==round(float(full['cpus'])*1000000000) and hc['ReadonlyRootfs'] is True and not hc['Privileged'] and not hc['PortBindings']
    m.qualification_security(d,hc)
    receipt=d.safe(Path(args.job_preservation_receipt));receipt_raw=d.read(receipt,2000000);assert m.sha(receipt_raw)==args.job_preservation_sha256
    historical=json.loads(receipt_raw);assert isinstance(historical,dict)
    now=m.observation(d);health=d.dbstate()
    with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:ready=json.loads(response.read(65537))
    assert ready['status']=='ready' and ready['stale_services']==[] and health['heartbeat']>=time.time()-180
    result={'version':'x-relations-current-runtime-reconciliation-v1','state':'current_declared_runtime_accepted',
        'observed_at':time.time(),'container_id':cid,'qualification':qualification,'production_sources':manifest['runtime_source_sha256'],
        'deployment_report':{'file':str(path),'sha256':args.deployment_sha256},'other_four':original['other_four'],
        'checks':{'exact_candidate':True,'all_source_hashes':True,'core_01b':True,'fixed_configs':True,'bind_mounts':True,'tmpfs_hostconfig':True,
            'declared_resources_security':True,'image_and_resolved_original_env_equal':True,'ready_and_heartbeat':True},
        'job_preservation_provenance':{'file':str(receipt),'sha256':args.job_preservation_sha256,'original_predeployment_rows_sha256':original['job_rows_sha256'],
            'semantic_status':'operator-provided historical receipt; current authorized backfill changes are not asserted unchanged'},
        'current_tweet_row_count':len(now['rows']),'current_tweet_rows_sha256':m.sha(d.wire(now['rows'])),
        'original_runtime_assert_cause':'not_determined','original_retired_raw_runtime_available':False,
        'original_assert_and_report_preserved':True,'service_or_configuration_changed_by_this_script':False,'database_written':False,
        'limitations':['This accepts current desired configuration and observations, not an unavailable original raw runtime comparison.',
            'Historical task preservation is referenced as provenance; the separately authorized two-source run may now change its jobs.']}
    output=path.parent/('reconciliation-'+str(uuid.uuid4())+'.json');d.atomic(output,d.wire(result));return output,result
def main():
    assert os.name=='posix' and os.geteuid()==0 and not sys.flags.optimize;os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('operator-file','base-helper','deployment-report','deployment-sha256','archive','job-preservation-receipt','job-preservation-sha256'):parser.add_argument('--'+name,required=True)
    output,result=accept(parser.parse_args());print(json.dumps({'state':result['state'],'report_file':str(output),'service_changed':False,'database_written':False}))
if __name__=='__main__':
    try:main()
    except Exception as error:print(json.dumps({'state':'read_only_acceptance_stopped','error_class':type(error).__name__,'service_changed':False}));raise SystemExit(2) from None
