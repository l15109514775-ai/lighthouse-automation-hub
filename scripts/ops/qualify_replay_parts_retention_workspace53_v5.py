"""Passed 276 + actual inventory54 + actual workspace53 -> fresh unchanged 26.
Only owned isolation/qualification files are written. No production cutover.
"""
import argparse,hashlib,json,os,re,subprocess,time,types,uuid
from pathlib import Path
HELPER_SHA='4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
ADAPTER_SHA='3b3dd755a3af6750cc5b07f3d523d54eea12d2b6ffea656f56cfbe92e661516f'
DELTA_ARCHIVE='7d4b5a688a27399263392537ac21f6b3e1e8d515e9740fc5e0cd33f6babd5003'
DELTA_COMMIT='1a811c18cbea1bce3b2623500995318bc2917dba'
PARENT_SHA='94771d90416ec9db629842b4b950f7b3ee959f05058434ae026c68dacafa3ff6'
INVENTORY_SHA='7ff2baa13f7e8aa903b2581a458f133bc3830efd8ee510c27c48be7b4fea4a1f'
WORKSPACE_SHA='befe9221443f6cb950f90842a577201d5eb7333085159cde26dcb5fec2ab6a12'
SOURCE='adapters/ytdlp_whole_v4.py'
SOURCE_SHA='8e3fea2da8cbf13a64533eb62ffd5eb990c3f2f092ef47258edb8f63f6afd148'
GROUPS={'tests.test_whole_workspace_race_v5':21,'tests.test_youtube_download_v4':10,'tests.test_verified_track_release_v4':12,'tests.test_replay_delivery_v5':10}

def load_adapter(file):
 raw=Path(file).read_bytes();assert hashlib.sha256(raw).hexdigest()==ADAPTER_SHA
 mod=types.ModuleType('fixed_inventory54_adapter');mod.__file__=str(Path(file).resolve());exec(compile(raw,'<fixed-inventory54-adapter>','exec'),mod.__dict__);return mod

def validate_workspace(m,delta,xml,inventory,prior_lineage):
 m.require(delta['version']=='whole-workspace53-ubuntu-v1' and delta['passed'] is True and delta['tests']==53,'WORKSPACE53_REQUIRED')
 m.require(delta['archive_sha256']==DELTA_ARCHIVE and delta['source_commit']==DELTA_COMMIT and delta['base_image']==inventory['image'],'WORKSPACE_PARENT_CHANGED')
 m.require(delta['exit_code']==delta['failure']==delta['error']==delta['skipped']==0 and delta['test_groups']==GROUPS,'WORKSPACE53_FAILED')
 m.require(delta['network']=='none' and delta['production_data_mounted'] is False and delta['credentials_injected'] is False and delta['oom_killed'] is False,'WORKSPACE_ISOLATION_REQUIRED')
 m.require(delta['resource_limits_passed'] is delta['sources_unchanged'] is delta['production_video_unchanged'] is True,'WORKSPACE_RUNTIME_REQUIRED')
 m.require(m.sha(xml)==delta['junit_sha256'] and m.junit(xml,53)==GROUPS,'WORKSPACE_JUNIT_REQUIRED')
 old=inventory['source_manifest'];current=delta['source_manifest'];expected={n:v['sha256'] for n,v in current['files'].items()}
 m.require(delta['parent_54_source_manifest']==old and delta['manifest']['base_source_manifest']==old,'ACTUAL54_PARENT_REQUIRED')
 m.require(current==delta['manifest']['source_manifest'] and len(old['files'])==851 and len(current['files'])==852,'COMPOSED_FILES_CHANGED')
 m.require({n for n in old['files'] if current['files'].get(n)!=old['files'][n]}=={SOURCE},'EXACT_ONE_WORKSPACE_SOURCE_REQUIRED')
 m.require(set(current['files'])-set(old['files'])=={'tests/test_whole_workspace_race_v5.py'},'EXACT_ONE_NEW_TEST_REQUIRED')
 m.require(current['files'][SOURCE]['sha256']==SOURCE_SHA and current['source_commit']==DELTA_COMMIT and current['expected_test_count']==53,'COMPOSED_SOURCE_CHANGED')
 m.require(delta['source_hashes']==delta['source_before']['sources']==delta['source_after']['sources']==expected,'EXACT_ALL_SOURCES_REQUIRED')
 before=m.source_files(old);after=m.source_files(current)
 m.require(len(before)==len(after)==141 and {n for n in before if before[n]!=after[n]}=={SOURCE},'EXACT_141_SOURCE_SCOPE_REQUIRED')
 m.require(re.fullmatch('sha256:[a-f0-9]{64}',delta['image']) is not None,'BAD_IMAGE')
 return {'version':'retention276-inventory54-workspace53-lineage-v1','original_276_run_sha256':PARENT_SHA,'inventory54_run_sha256':INVENTORY_SHA,'workspace53_run_sha256':WORKSPACE_SHA,'inventory54_qualification_lineage':prior_lineage,'composed_source_manifest_sha256':m.sha(m.wire(current)),'retention_runtime_source_count':141,'runtime_changed_files_from_inventory54':[SOURCE],'parent_276_and_54_not_rerun':True}

def qualify(m, parent, parent_raw, authorization, lineage, seccomp):
    seccomp_raw = m.read(seccomp)
    m.require(m.sha(seccomp_raw) == parent['source_manifest']['files']['config/browser-seccomp.json']['sha256'], 'QUALIFIED_SECCCOMP_REQUIRED')
    profile = json.loads(seccomp_raw)
    image = parent['image']; m.inspect_image(image)
    folder = m.root_directory(m.BASE/str(uuid.uuid4())); m.save(folder/'browser-seccomp.json',seccomp_raw); seccomp=folder/'browser-seccomp.json'; owned = folder/'owned'
    os.mkdir(owned, 0o700); os.chown(owned, 10001, 10001); os.chmod(owned, 0o700)
    m.save(folder/'delta-qualified-run.json', parent_raw); m.save(folder/'lineage.json',m.wire(lineage)); m.save(folder/'parent-qualified-results.xml',m.read(Path(parent['junit_file'])))
    command = ['docker', 'create', '--pull=never', '--init', '--name', 'retention-v5-' + folder.name,
        '--network=none', '--read-only', '--user', '10001:10001', '--cap-drop=ALL',
        '--security-opt=no-new-privileges:true', '--security-opt=seccomp=' + str(seccomp),
        '--memory=1400m', '--cpus=1', '--pids-limit=256', '--shm-size=256m',
        '--tmpfs=/tmp:rw,size=256m,mode=1777', '--tmpfs=' + str(m.ROOT/'media') + ':rw,size=32m,mode=0700,uid=10001,gid=10001',
        '--env=HOME=/tmp/home', '--env=XDG_CONFIG_HOME=/tmp/config', '--env=XDG_CACHE_HOME=/tmp/cache', '--env=PYTHONDONTWRITEBYTECODE=1',
        '--mount=type=bind,source=' + str(owned) + ',target=/owned', image, 'python', '-c', m.BOOTSTRAP]
    cid = m.run(command).stdout.strip(); info = json.loads(m.run(['docker', 'inspect', cid]).stdout)[0]
    m.validate_isolation(info, image=image, owned=owned, seccomp=seccomp, seccomp_profile=profile)
    started = time.time(); m.run(['docker', 'start', cid]); timed_out = False
    try:status = int(m.run(['docker', 'wait', cid], timeout=900).stdout.strip())
    except subprocess.TimeoutExpired:
        timed_out = True; m.run(['docker', 'stop', '-t', '15', cid]); status = 124
    logs = m.run(['docker', 'logs', cid]); m.save(folder/'tests.log', (logs.stdout + '\n' + logs.stderr).encode())
    final = json.loads(m.run(['docker', 'inspect', cid]).stdout)[0]
    m.validate_isolation(final, image=image, owned=owned, seccomp=seccomp, seccomp_profile=profile)
    xml = m.read(owned/'results.xml'); groups = m.junit(xml, 26, group=m.GROUP)
    expected = {n: v['sha256'] for n, v in parent['source_manifest']['files'].items()}
    before = json.loads(m.read(owned/'source-before.json')); after = json.loads(m.read(owned/'source-after.json'))
    rb = json.loads(m.read(owned/'resource-before.json')); ra = json.loads(m.read(owned/'resource-after.json'))
    for name in ('source-before.json', 'source-after.json', 'resource-before.json', 'resource-after.json'):
        m.save(folder/name, m.read(owned/name))
    no_denial = all(c['pids.events'].get('max', -1) == c['memory.events'].get('oom', -1) == c['memory.events'].get('oom_kill', -1) == 0 for c in (rb, ra))
    unchanged = before == after == expected
    proof = {'version': m.VERSION, 'tests': 26, 'test_groups': groups, 'exit_code': status, 'failures': 0, 'errors': 0, 'skipped': 0,
        'network': 'none', 'application_data_mounted': False, 'production_profile_mounted': False, 'secrets_environment_injected': False,
        'oom_killed': final['State']['OOMKilled'], 'timed_out': timed_out, 'full_suite_passed': status == 0 and not timed_out and not final['State']['OOMKilled'],
        'no_pid_or_oom_denial': no_denial, 'runtime_matches_declared_candidate': unchanged,
        'mounts_match_isolated_allowlist': True, 'mounted_sources_unchanged': unchanged,
        'source_files': m.source_files(parent['source_manifest']), 'image': image, 'container_id': cid,
        'qualified_parent_sha256': m.sha(parent_raw), 'source_archive_sha256': DELTA_ARCHIVE, 'source_commit': DELTA_COMMIT,
        'junit_file': str(m.CONTAINER_PROOF/'results.xml'), 'junit_host_file': str(folder/'results.xml'), 'junit_sha256': m.sha(xml),
        'resource_before': rb, 'resource_after': ra, 'duration_seconds': time.time() - started,
        'host_file': str(folder/'test-attestation.json'), 'source_manifest_sha256': lineage['composed_source_manifest_sha256'],
        'qualification_helper_sha256': m.sha(m.read(Path(__file__).resolve())), 'qualification_lineage':lineage, 'original_qualification_helper_sha256':HELPER_SHA}
    m.save(folder/'results.xml', xml)
    raw = m.wire(proof) + b'\n'; m.save(folder/'test-attestation.json', raw)
    m.require(proof['full_suite_passed'] and no_denial and unchanged, 'RETENTION_26_FAILED_CLOSED')
    m.save(folder/'release-template.json', m.wire(m.release_template(authorization, proof, raw)) + b'\n')
    return {'state': 'qualified_not_installed', 'test_attestation': proof['host_file'], 'sha256': m.sha(raw),
        'release_template': str(folder/'release-template.json'), 'readonly_mount': str(folder) + ':' + str(m.CONTAINER_PROOF) + ':ro', 'tests': 26}


def main():
 p=argparse.ArgumentParser();p.add_argument('--helper',type=Path,required=True);p.add_argument('--inventory-adapter',type=Path,required=True);p.add_argument('--parent276',type=Path,required=True);p.add_argument('--inventory54',type=Path,required=True);p.add_argument('--workspace53',type=Path,required=True);p.add_argument('--authorization-json',type=Path,required=True);p.add_argument('--seccomp',type=Path,default=Path('/opt/lighthouse-hub/config/browser-seccomp.json'));p.add_argument('--qualify',action='store_true');a=p.parse_args()
 assert os.name=='posix' and os.geteuid()==0;os.umask(0o077);previous=load_adapter(a.inventory_adapter);m=previous.load(a.helper)
 praw=m.read(a.parent276);iraw=m.read(a.inventory54);wraw=m.read(a.workspace53);m.require(m.sha(praw)==PARENT_SHA and m.sha(iraw)==INVENTORY_SHA and m.sha(wraw)==WORKSPACE_SHA,'EXACT_RAW_RUNS_REQUIRED');parent=json.loads(praw);inventory=json.loads(iraw);workspace=json.loads(wraw)
 m.validate_parent(parent,m.read(Path(parent['junit_file'])));prior=previous.validate_delta(m,inventory,m.read(Path(inventory['junit_file'])),parent);lineage=validate_workspace(m,workspace,m.read(Path(workspace['junit_file'])),inventory,prior)
 images=[m.inspect_image(v['image']) for v in (parent,inventory,workspace)]
 for before,after in zip(images,images[1:]):m.require(after['RootFS']['Layers'][:len(before['RootFS']['Layers'])]==before['RootFS']['Layers'],'QUALIFIED_PARENT_LAYERS_REQUIRED')
 authorization=json.loads(m.read(a.authorization_json,protected=True));m.owner(authorization)
 if a.qualify:result=qualify(m,workspace,wraw,authorization,lineage,a.seccomp)
 else:result={'state':'workspace_new26_plan_only','image':workspace['image'],'tests_to_run':26,'lineage':lineage,'services_restarted':0,'application_data_mounted':False}
 print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
