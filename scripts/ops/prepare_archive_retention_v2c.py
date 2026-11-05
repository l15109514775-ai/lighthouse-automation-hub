"""Build only the v2c backup overlay from exact v2b templates and real proofs.

Local preparation only: no SSH, Docker, credentials, network or production I/O.
The existing 139/9 qualification runners are reused by the operator separately.
"""
from pathlib import Path,PurePosixPath
import argparse,ast,hashlib,json,re
import xml.etree.ElementTree as ET

IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
OLD_HELPERS={'run_cloudbase_backup_retention_v2b.py':'79d9715734e080460dcb716d9a39f2450a0a9528940a545cfda989498913c877',
 'run_archive_source_registry_v2b.py':'9bad59e2490572abb091050820e0fef098a310e47d5d5ab5c9a9e80f5a3b42f2',
 'cloudbase_backup_daily_retention_v2b.py':'fe8c9948e0c94633f455d4a7ea12ddd7ab8f22631a7862698a9a210d07e216d9'}
UNCHANGED={'services/hub/archive_source_retention_v2.py':'cb81506a9fc0b65a85f25c3f4417496a50fb0bd22e216b4003826d11f2bb2a5e',
 'scripts/ops/prune_cloudbase_verified_clones_v1.py':'6af8e57a2b50d9a47c69c1a7debc957e45f89f140860368e6c761e1c5cf8a9ce',
 'scripts/archive-source-retention-v2.py':'b5280004058bdd730e8a9debfab11e66bc2a8b9e80eaa4c82944480ce91a065e',
 'services/hub/archive_source_autorun_v2.py':'f55b84a24ccb04364c451ef4a829d85ffa46372138d9d5ee12d2d52ff261aade'}
HOST={'scripts/ops/cloudbase_backup_daily_v1.py':'c31d61724d1e6ea2d6c9ab39d55cec9f07e4ae656edbd4965ca647aa3d10f989',
 'scripts/ops/cloudbase_archive_registration_hook_v2.py':'b85eaf439e738335ae7c2ff04e8beac8c5d7f4a0d4fadc04c2be24b98cb3bf5c'}
SOURCE_FILES=('services/hub/archive_source_retention_v2.py','services/hub/cloudbase_backup_runtime.py','scripts/ops/prune_cloudbase_verified_clones_v1.py')
COUNTS={'tests.test_archive_source_retention_v2':31,'tests.ops.test_cloudbase_archive_registration_hook_v2':5,
 'tests.test_cloudbase_backup_runtime':36,'tests.test_cloudbase_backup':44,'tests.test_cloudbase_cos_backup':23}
OLD_UNIT_SHA='07cb3794b26bb118cca7c1c9f648f72fa483d0f53a69e9323a88ca328755edea'
TIMER_SHA='58ac9795fd895077a3f3461e685528b18ac6d44fece78acdc8744fdea643964a'
OLD_RELEASE_SHA='c0e69b264d049f573091fbae8c055f36cab55f191acec263b25d04fdc9f206b3'
AUTHORIZATION_ID='f3000857-51d3-47bb-871d-465e88cbc817'
REPO=Path(__file__).resolve().parents[2]


def need(value,code):
    if not value:raise ValueError(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def read(path,limit=2_097_152):
    path=Path(path)
    need(path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents)),'UNSAFE_BUILD_PATH')
    need(path.is_file() and 0<path.stat().st_size<=limit,'BOUNDED_BUILD_FILE_REQUIRED')
    return path.read_bytes()


def assignment(source,name):
    return next(ast.literal_eval(node.value) for node in ast.parse(source).body if isinstance(node,ast.Assign) and
                len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id==name)


def replace_once(source,before,after):
    need(source.count(before)==1,'TEMPLATE_SHAPE_CHANGED')
    return source.replace(before,after)


def proof(att_raw,junit_raw,layer,*,autorun=False):
    value=json.loads(att_raw);counts={'tests.test_archive_source_autorun_v2':9} if autorun else COUNTS
    version='archive-source-autorun-v2-ubuntu-tests-v1' if autorun else 'archive-source-retention-v2-ubuntu-tests-v1'
    need(value.get('version')==version and value.get('image')==IMAGE,'PROOF_VERSION_OR_IMAGE_CHANGED')
    need(value.get('tests')==sum(counts.values()) and all(value.get(k)==0 for k in ('failures','errors','skipped','exit_code')),'PROOF_NOT_PASSED')
    for name in ('oom_killed','application_data_mounted','production_profile_mounted','secrets_environment_injected'):
        need(value.get(name) is False,'PROOF_ISOLATION_REQUIRED')
    for name in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist'):
        need(value.get(name) is True,'PROOF_ISOLATION_REQUIRED')
    need(value.get('network')=='none' and sha(junit_raw)==value.get('junit_sha256'),'PROOF_JUNIT_CHANGED')
    groups={};cases=ET.fromstring(junit_raw).findall('.//testcase')
    for case in cases:
        groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
        need(all(case.find(tag) is None for tag in ('failure','error','skipped')),'PROOF_CASE_NOT_PASSED')
    need(groups==counts,'EXACT_PROOF_GROUPS_REQUIRED')
    names=(*SOURCE_FILES,'services/hub/archive_source_autorun_v2.py') if autorun else SOURCE_FILES
    need(value.get('source_files')=={name:layer[name] for name in names},'PROOF_RUNTIME_CHANGED')
    root=PurePosixPath(value['junit_file']).parent
    prefix='/var/lib/lighthouse-backup-ops/archive-autorun-v2-tests' if autorun else '/var/lib/lighthouse-backup-ops/archive-retention-v2-tests'
    need(root.parent.as_posix()==prefix and re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',root.name) and
         PurePosixPath(value['junit_file']).name=='accepted-results.xml','PROOF_SERVER_PATH_INVALID')
    return {'root':str(root),'attestation_sha256':sha(att_raw),'junit_sha256':sha(junit_raw),'source_files':value['source_files'],'tests':len(cases)}


DAILY_GUARDS='''
RECOVERY_DAILY='6cfa6b1d-46db-4ed9-888e-f6564f07d956'
RECOVERY_OPERATION='7505562a-3b39-4e9c-897b-a704a05aa6a9'

def validate_previous(previous):
    assert previous['version']=='cloudbase-daily-accept-prune-v1' and previous['image']==IMAGE
    assert previous['helper_pins']==PINS or (previous['state']=='completed' and previous['helper_pins']==LEGACY_PINS)

def verify_recovered_gap(d):
    # Only the exact root-authorized failed latest was relocated; history stays.
    folder=Path('/var/lib/lighthouse-backup-ops/preupload-recovery-v1')/RECOVERY_DAILY
    def load(file):
        protected(file);assert file.is_file() and 0<file.stat().st_size<=262144
        raw=file.read_bytes();return json.loads(raw),hashlib.sha256(raw).hexdigest()
    receipt,_=load(folder/'receipt.json');intent,_=load(folder/'intent.json')
    assert receipt['version']==intent['version']=='cloudbase-preupload-daily-recovery-v1'
    assert receipt['state']=='preupload_failure_reconciled' and intent['state']=='relocation_intent_saved'
    plan=intent['plan'];digest=hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert digest==intent['plan_sha256']==receipt['plan_sha256']
    assert plan['daily_id']==receipt['daily_id']==RECOVERY_DAILY and plan['operation_id']==receipt['operation_id']==RECOVERY_OPERATION
    assert receipt['active_latest_absent'] is receipt['history_preserved'] is True
    assert receipt['ledger_modified'] is receipt['backup_success_claimed'] is receipt['core_services_restarted'] is False
    assert receipt['external_requests']==0 and plan['external_requests']==0 and plan['ledger_modified'] is False
    failed,failed_sha=load(folder/'failed-latest.json');history,history_sha=load(d.STATE/'history'/(RECOVERY_DAILY+'.json'))
    legacy,legacy_sha=load(Path('/var/lib/lighthouse-backup-ops/latest.json'))
    assert failed_sha==plan['source_hashes']['latest'] and history_sha==plan['source_hashes']['history']
    assert legacy_sha==plan['source_hashes']['legacy'] and legacy['state']=='completed'
    assert failed['daily_id']==history['daily_id']==RECOVERY_DAILY and failed['state']==history['state']=='accept_failed'
    assert failed['helper_pins']==history['helper_pins']==LEGACY_PINS
    assert not (d.STATE/'latest.json').exists()

'''


def helpers(templates,layer,core_proof):
    def common(text):
        text=replace_once(text,"BASE=OPS/'retention-v2b'","BASE=OPS/'retention-v2c'")
        text=re.sub(r'^LAYER=.*$',lambda m:'LAYER='+repr(layer),text,flags=re.M)
        text=re.sub(r'^PROOF_TARGET=.*$',lambda m:'PROOF_TARGET=Path('+repr(core_proof['root'])+')',text,flags=re.M)
        text=replace_once(text,"PROOF_MIRROR=Path('/var/lib/lighthouse-backup-ops/retention-v2b/readiness')",
                          "PROOF_MIRROR=Path('/var/lib/lighthouse-backup-ops/retention-v2c/readiness')")
        # Repository policy is eol=lf; emitted pins must equal committed bytes.
        return text.replace('\r\n','\n')
    result={}
    for stem in ('run_cloudbase_backup_retention','run_archive_source_registry'):
        result[stem+'_v2c.py']=common(templates[stem+'_v2b.py']).encode()
    old=templates['cloudbase_backup_daily_retention_v2b.py'];legacy=assignment(old,'PINS')
    text=common(old)
    for stem in ('run_cloudbase_backup_retention','run_archive_source_registry'):
        text=text.replace(stem+'_v2b.py',stem+'_v2c.py').replace(OLD_HELPERS[stem+'_v2b.py'],sha(result[stem+'_v2c.py']))
    text=replace_once(text,'def main():', 'LEGACY_PINS='+repr(legacy)+'\n'+DAILY_GUARDS+'\ndef main():')
    old_check="assert previous['version']=='cloudbase-daily-accept-prune-v1' and previous['helper_pins']==PINS and previous['image']==IMAGE"
    text=replace_once(text,old_check,'validate_previous(previous)')
    text=replace_once(text,"        elif (oldstate/'latest.json').exists():", "        else:\n            verify_recovered_gap(d)\n        if previous is None and (oldstate/'latest.json').exists():")
    result['cloudbase_backup_daily_retention_v2c.py']=text.encode()
    for name,raw in result.items():ast.parse(raw.decode(),filename=name)
    need("d.STATE=Path('/var/lib/lighthouse-backup-ops/retention-v2b/daily-state')" in text,'ORIGINAL_DAILY_STATE_REQUIRED')
    return result


def build(repo,runtime_sha,core_att,core_junit,auto_att,auto_junit):
    need(re.fullmatch('[a-f0-9]{64}',runtime_sha),'EXACT_RUNTIME_SHA_REQUIRED')
    layer={'services/hub/cloudbase_backup_runtime.py':runtime_sha,**UNCHANGED};files={};templates={}
    for name,digest in {**layer,**HOST}.items():
        raw=read(repo/name);need(sha(raw)==digest,'REVIEWED_RUNTIME_OR_HOST_CHANGED')
        files[('runtime/' if name in layer else 'host/')+name]=raw
    for name,digest in OLD_HELPERS.items():
        raw=read(repo/'scripts/ops'/name);need(sha(raw)==digest,'V2B_TEMPLATE_CHANGED');templates[name]=raw.decode()
    cp=proof(core_att,core_junit,layer);ap=proof(auto_att,auto_junit,layer,autorun=True)
    produced=helpers(templates,layer,cp)
    files.update({'helpers/'+name:raw for name,raw in produced.items()})
    files.update({'readiness/test-attestation.json':core_att,'readiness/accepted-results.xml':core_junit,
                  'autorun-readiness/test-attestation.json':auto_att,'autorun-readiness/accepted-results.xml':auto_junit})
    release={'version':'archive-source-retention-release-v2','authorization_id':AUTHORIZATION_ID,'mode':'verified_archive_members_only',
             'formal_video_launch_claimed':False,'source_files':cp['source_files'],
             'test_attestation':{'file':cp['root']+'/test-attestation.json','sha256':cp['attestation_sha256']}}
    files['candidate-release.json']=(json.dumps(release,indent=2)+'\n').encode()
    service=read(repo/'scripts/ops/lighthouse-cloudbase-backup.service').replace(b'cloudbase_backup_daily_v1.py',b'cloudbase_backup_daily_retention_v2b.py')
    need(sha(service)==OLD_UNIT_SHA,'OLD_SERVICE_TEMPLATE_CHANGED')
    files['previous.service']=service
    files['candidate.service']=service.replace(b'cloudbase_backup_daily_retention_v2b.py',b'cloudbase_backup_daily_retention_v2c.py')
    manifest={'version':'archive-retention-host-kit-v2c','image':IMAGE,'runtime_files':layer,'host_files':HOST,
              'helper_files':{name:sha(raw) for name,raw in produced.items()},'legacy_helper_pins':assignment(templates['cloudbase_backup_daily_retention_v2b.py'],'PINS'),
              'new_helper_pins':assignment(produced['cloudbase_backup_daily_retention_v2c.py'].decode(),'PINS'),
              'core_proof':cp,'autorun_proof':ap,'old_unit_sha256':OLD_UNIT_SHA,'timer_sha256':TIMER_SHA,
              'old_release_sha256':OLD_RELEASE_SHA,'authorization_id':AUTHORIZATION_ID,'daily_state':'/var/lib/lighthouse-backup-ops/retention-v2b/daily-state',
              'files':{name:{'sha256':sha(raw),'bytes':len(raw)} for name,raw in files.items()},'production_execution':False}
    return manifest,files


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runtime-sha256',required=True)
    for name in ('core-attestation','core-junit','autorun-attestation','autorun-junit','output-root'):p.add_argument('--'+name,required=True,type=Path)
    a=p.parse_args();manifest,files=build(REPO,a.runtime_sha256,read(a.core_attestation),read(a.core_junit),read(a.autorun_attestation),read(a.autorun_junit))
    need(a.output_root.is_absolute() and a.output_root.resolve()==a.output_root,'ABSOLUTE_OUTPUT_REQUIRED')
    raw=wire(manifest)+b'\n';target=a.output_root/sha(raw);target.mkdir(parents=True,exist_ok=True)
    for name,body in {**files,'manifest.json':raw}.items():
        file=target/name;file.parent.mkdir(parents=True,exist_ok=True)
        if file.exists():need(read(file)==body,'BUILD_OUTPUT_CHANGED')
        else:
            with file.open('xb') as out:out.write(body)
    print(json.dumps({'state':'prepared_not_deployed','kit':str(target),'manifest_sha256':sha(raw),
                      'runtime_sha256':a.runtime_sha256,'core_tests':139,'autorun_tests':9,'external_requests':0}))


if __name__=='__main__':main()
