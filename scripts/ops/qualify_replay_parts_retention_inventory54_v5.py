"""Exact passed 276 parent + passed inventory54 delta -> fresh unchanged 26 suite.
No installation, service action, database write, network, profile, or deletion.
"""
import argparse,hashlib,json,os,re,subprocess,time,types,uuid
from pathlib import Path
HELPER_SHA='4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
DELTA_ARCHIVE='946528e10ce71515bd5f108f7508ed482d798069a52e50e4877d95574e4bf1d3'
DELTA_COMMIT='8a0e9886663d9a9f4dc6a4c9f432f818dfcde256'
PARENT_SHA='94771d90416ec9db629842b4b950f7b3ee959f05058434ae026c68dacafa3ff6'
DELTA_SHA='7ff2baa13f7e8aa903b2581a458f133bc3830efd8ee510c27c48be7b4fea4a1f'
SOURCE='services/hub/youtube_natural_plan_v3.py'
SOURCE_SHA='e6763f9a8ec4a4a2fe95ccd061a36d4701499a9f32cc0797cace4b8ce2467ee9'
GROUPS={'tests.test_youtube_real_inventory_v5':11,'tests.test_youtube_natural_plan_v3':27,'tests.test_replay_multipart_v4':16}

def load(path):
 raw=Path(path).read_bytes();assert hashlib.sha256(raw).hexdigest()==HELPER_SHA
 mod=types.ModuleType('qualified_retention276');mod.__file__=str(Path(path).resolve());exec(compile(raw,'<fixed-retention-helper>','exec'),mod.__dict__);return mod

def validate_delta(m,delta,xml,parent):
 m.require(delta['version']=='youtube-real-inventory54-ubuntu-v1' and delta['passed'] is True and delta['tests']==54,'DELTA54_REQUIRED')
 m.require(delta['archive_sha256']==DELTA_ARCHIVE and delta['source_commit']==DELTA_COMMIT and delta['base_image']==parent['image'],'DELTA_PARENT_CHANGED')
 m.require(delta['exit_code']==delta['failure']==delta['error']==delta['skipped']==0 and delta['test_groups']==GROUPS,'DELTA_FAILED')
 m.require(delta['network']=='none' and delta['production_data_mounted'] is False and delta['credentials_injected'] is False and delta['oom_killed'] is False,'DELTA_ISOLATION_REQUIRED')
 m.require(delta['resource_limits_passed'] is delta['sources_unchanged'] is delta['production_video_unchanged'] is True,'DELTA_RUNTIME_REQUIRED')
 m.require(m.sha(xml)==delta['junit_sha256'] and m.junit(xml,54)==GROUPS,'DELTA_JUNIT_REQUIRED')
 old=parent['source_manifest'];current=delta['source_manifest'];expected={n:v['sha256'] for n,v in current['files'].items()}
 m.require(delta['parent_276_source_manifest']==old and delta['manifest']['base_source_manifest']==old,'ORIGINAL_276_REQUIRED')
 m.require(current==delta['manifest']['source_manifest'] and len(old['files'])==848 and len(current['files'])==851,'COMPOSED_FILES_CHANGED')
 m.require({n for n in old['files'] if current['files'].get(n)!=old['files'][n]}=={SOURCE},'EXACT_ONE_RUNTIME_DELTA_REQUIRED')
 m.require(set(current['files'])-set(old['files'])=={'tests/test_youtube_real_inventory_v5.py','tests/fixtures/youtube_whole_observations_v5/6wHqfDmNF0U.json','tests/fixtures/youtube_whole_observations_v5/YsSXSIYtCoE.json'},'EXACT_NEW_TEST_FILES_REQUIRED')
 m.require(current['files'][SOURCE]['sha256']==SOURCE_SHA and current['source_commit']==DELTA_COMMIT and current['expected_test_count']==54,'COMPOSED_SOURCE_CHANGED')
 m.require(delta['source_hashes']==delta['source_before']['sources']==delta['source_after']['sources']==expected,'EXACT_ALL_SOURCES_REQUIRED')
 before=m.source_files(old);after=m.source_files(current)
 m.require(len(before)==len(after)==141 and {n for n in before if before[n]!=after[n]}=={SOURCE},'EXACT_141_SOURCE_SCOPE_REQUIRED')
 m.require(re.fullmatch('sha256:[a-f0-9]{64}',delta['image']) is not None,'BAD_IMAGE')
 return {'version':'retention276-plus-inventory54-lineage-v1','original_276_run_sha256':PARENT_SHA,'inventory54_run_sha256':DELTA_SHA,'composed_source_manifest_sha256':m.sha(m.wire(current)),'retention_runtime_source_count':141,'runtime_changed_files':[SOURCE],'parent_276_not_rerun':True}

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
 p=argparse.ArgumentParser();p.add_argument('--helper',type=Path,required=True);p.add_argument('--parent276',type=Path,required=True);p.add_argument('--delta54',type=Path,required=True);p.add_argument('--authorization-json',type=Path,required=True);p.add_argument('--seccomp',type=Path,default=Path('/opt/lighthouse-hub/config/browser-seccomp.json'));p.add_argument('--qualify',action='store_true');a=p.parse_args()
 assert os.name=='posix' and os.geteuid()==0;os.umask(0o077);m=load(a.helper)
 praw=m.read(a.parent276);draw=m.read(a.delta54);m.require(m.sha(praw)==PARENT_SHA and m.sha(draw)==DELTA_SHA,'EXACT_RAW_RUNS_REQUIRED');parent=json.loads(praw);delta=json.loads(draw)
 m.validate_parent(parent,m.read(Path(parent['junit_file'])));lineage=validate_delta(m,delta,m.read(Path(delta['junit_file'])),parent)
 base=m.inspect_image(parent['image']);candidate=m.inspect_image(delta['image']);m.require(candidate['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers'],'QUALIFIED_PARENT_LAYERS_REQUIRED')
 authorization=json.loads(m.read(a.authorization_json,protected=True));m.owner(authorization)
 if a.qualify:result=qualify(m,delta,draw,authorization,lineage,a.seccomp)
 else:result={'state':'new26_qualification_plan_only','image':delta['image'],'tests_to_run':26,'lineage':lineage,'services_restarted':0,'application_data_mounted':False}
 print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
