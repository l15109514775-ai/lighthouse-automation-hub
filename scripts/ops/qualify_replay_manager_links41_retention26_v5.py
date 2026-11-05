"""Offline manager-link and exact lookup-repair delta over actual 24b/42; same-image retention 26.

Reuse the frozen preceding suite/isolation helper. No production mounts,
credentials, profile, network, deployment, retry or platform operation.
"""
from pathlib import Path,PurePosixPath
from collections import Counter
import argparse,hashlib,json,os,re,tarfile,types,uuid

BASE='sha256:24b0198ba425f527655173c2ef1432445947ebec5fed8660c6bb5527eaa15dde'
BASE_TAG='lighthouse-hub:replay-contention42-159fd49ff54e'
Q_SHA='401ba7df4a291bc0dbdc46b5f72d6287f4afab3b5da5dc22926d48c2f956c387'
M_SHA='4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
PARENT=Path('/var/lib/lighthouse-backup-ops/replay-moderation-contention42-v5-tests/3db974c7-6013-4fed-b433-3063b3cb95fc/test-result.json')
PARENT_SHA='e3c646987e7660a232b9e6cddb5e6a449795bb6b05e0204ced50235983684a5f'
OLD26=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/529d0c40-7ec6-46ca-b31c-c76ed6a34db4/test-attestation.json')
OLD26_SHA='ab8158d642c05a401f63c482c1994b458d82b8e35451016f015ff779e909b855'
PRODUCTION={'adapters/replay_later_result_lookup_v2.mjs','services/hub/replay_part_public_evidence_v5.py','adapters/replay_native_driver.mjs'}
EXPECTED_TESTS=41
ROOT=Path('/opt/lighthouse-hub')
def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def load(file,pin,name):
    raw=file.read_bytes();assert sha(raw)==pin
    m=types.ModuleType(name);m.__file__=str(file);exec(compile(raw,str(file),'exec'),m.__dict__);return m

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('archive','suite-helper','retention-helper'):p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--sha256',required=True);a=p.parse_args()
    assert os.name=='posix' and os.geteuid()==0 and __debug__;os.umask(0o077)
    q=load(a.suite_helper,Q_SHA,'frozen_contention42_suite');m=load(a.retention_helper,M_SHA,'frozen_retention276')
    parentraw=m.read(PARENT,protected=True);oldraw=m.read(OLD26,protected=True)
    assert sha(parentraw)==PARENT_SHA and sha(oldraw)==OLD26_SHA
    parent=json.loads(parentraw);old26=json.loads(oldraw)
    groups42={'tests.test_replay_part_moderation_contention_v5':10,'tests.test_replay_parts_runtime_v5':9,'tests.test_replay_part_moderation_v5':23}
    xml=m.read(Path(parent['junit_file']),protected=True)
    assert sha(xml)==parent['junit_sha256'] and m.junit(xml,42)==groups42
    assert parent['passed'] is True and parent['tests']==42 and parent['test_groups']==groups42 and parent['image']==BASE
    assert parent['source_before']==parent['source_after']==parent['source_hashes'] and len(parent['source_hashes'])==853
    assert parent['production_all_five_unchanged'] is True and parent['resource_limits_passed'] is True
    oldxml=m.read(Path(old26['junit_host_file']),protected=True)
    assert sha(oldxml)==old26['junit_sha256'] and m.junit(oldxml,26,group=m.GROUP)==old26['test_groups']
    assert old26['image']==BASE and old26['tests']==26 and old26['full_suite_passed'] is True
    assert old26['qualified_parent_sha256']==PARENT_SHA and old26['source_files']==m.source_files(parent['source_manifest'])
    assert old26['qualification_lineage']['contention42_run_sha256']==PARENT_SHA
    old_sources=m.source_files(parent['source_manifest']);assert len(old_sources)==141
    owner=json.loads(m.read(q.OWNER,protected=True));assert m.owner(owner)==q.OWNER_SHA
    before=q.production();assert before['video-worker']['image']==BASE
    assert a.archive.parent==Path('/home/lighthouse-ops') and a.archive.resolve()==a.archive and not a.archive.is_symlink()
    assert sha(m.read(a.archive,maximum=800000))==a.sha256
    area=m.root_directory(Path('/var/lib/lighthouse-backup-ops/replay-manager-links41-v5-tests')/str(uuid.uuid4()))
    context=area/'context';context.mkdir(mode=0o700);payload=context/'payload';payload.mkdir(mode=0o755)
    with tarfile.open(a.archive,'r:gz') as tar:
        entries=tar.getmembers();assert len({v.name for v in entries})==len(entries)
        manifest=json.load(tar.extractfile('manifest.json'))
        assert manifest['version']=='replay-manager-links41-candidate-v5' and manifest['base_image']==BASE
        assert manifest['parent42_sha256']==PARENT_SHA and manifest['parent26_sha256']==OLD26_SHA
        assert re.fullmatch('[a-f0-9]{40}',manifest['source_commit']) and manifest['base_source_manifest']==parent['source_manifest']
        pins=manifest['delta_sha256'];current=manifest['source_manifest'];groups=manifest['test_groups'];selections=manifest['test_selections']
        assert manifest['production_runtime_changed_files']==sorted(PRODUCTION) and current['source_commit']==manifest['source_commit']
        assert manifest['manager_fix_commit']=='ee56f173f4893decaccc43a81af4d44a4b4cccde' and manifest['lookup_repair_commit']=='97b3bf6d7ecab70bed846dd98d478556073c2046'
        assert manifest['expected_test_count']==current['expected_test_count']==sum(groups.values())==EXPECTED_TESTS
        assert current['test_selections']==selections and len(selections)==len(set(selections))
        assert all(re.fullmatch('tests[.](?:ops[.])?test_[a-z0-9_]+',k) and type(v) is int and v>0 for k,v in groups.items())
        assert groups.get('tests.ops.test_resume_fixed_part_lookup_v5')==4
        assert set(pins)<=PRODUCTION|{'scripts/ops/resume_fixed_part_lookup_v5.py'}|{n for n in pins if n.startswith('tests/') and n.endswith(('.py','.mjs','.json'))}
        assert set(PRODUCTION)<=set(pins) and len(pins)<=10 and {e.name for e in entries}==set(pins)|{'manifest.json'}
        old=parent['source_manifest']['files'];new=current['files']
        assert set(old)<=set(new) and {n for n in old if old[n]!=new[n]}==set(pins)&set(old)
        assert set(new)-set(old)==set(pins)-set(old)
        assert {n for n,v in m.source_files(current).items() if v!=old_sources[n]}==PRODUCTION
        assert len(m.source_files(current))==141
        for item in entries:
            path=PurePosixPath(item.name);assert item.isfile() and not item.issym() and not item.islnk() and not path.is_absolute() and '..' not in path.parts and 0<item.size<400000
            if item.name=='manifest.json':continue
            raw=tar.extractfile(item).read();assert sha(raw)==pins[item.name]==new[item.name]['sha256'] and len(raw)==new[item.name]['bytes']
            target=payload/item.name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw);target.chmod(0o644)
    for d in payload.rglob('*'):
        if d.is_dir():d.chmod(0o755)
    (payload/'candidate-manifest.json').write_bytes(wire(current)+b'\n');(payload/'candidate-manifest.json').chmod(0o644)
    # Resolve the existing local parent tag to its exact immutable image ID.
    assert q.run(['docker','image','inspect','-f','{{.Id}}',BASE_TAG]).stdout.strip()==BASE
    (context/'Dockerfile').write_text('FROM '+BASE_TAG+'\nCOPY --chown=0:0 payload/ /opt/lighthouse-hub/\n')
    tag='lighthouse-hub:manager-links41-'+a.sha256[:12];base=m.inspect_image(BASE)
    build=q.run(['docker','build','--network=none','--pull=false','-t',tag,str(context)],timeout=240);m.save(area/'build.log',(build.stdout+'\n'+build.stderr).encode())
    image=q.run(['docker','image','inspect','-f','{{.Id}}',tag]).stdout.strip();candidate=m.inspect_image(image)
    assert candidate['RootFS']['Layers'][:-1]==base['RootFS']['Layers'] and len(candidate['RootFS']['Layers'])==len(base['RootFS']['Layers'])+1
    assert all(candidate['Config'].get(k)==base['Config'].get(k) for k in ('Cmd','Entrypoint','User','Env','WorkingDir','Volumes','Labels','Healthcheck'))
    seccomp=ROOT/'config/browser-seccomp.json';assert sha(m.read(seccomp))==current['files']['config/browser-seccomp.json']['sha256']
    result=q.suite(m,image,current,selections,groups,area,seccomp)
    result.update(version='replay-manager-links41-ubuntu-v5',base_image=BASE,source_commit=manifest['source_commit'],archive_sha256=a.sha256,
        manifest=manifest,source_manifest=current,source_layer_delta=1,parent42_sha256=PARENT_SHA,parent26_sha256=OLD26_SHA,
        parent_qualification_lineage=old26['qualification_lineage'],production_before=before,production_after=q.production())
    result['production_all_five_unchanged']=result['production_before']==result['production_after'];result['passed']=result['passed'] and result['production_all_five_unchanged']
    raw=wire(result)+b'\n';m.save(area/'test-result.json',raw)
    print(json.dumps({'stage':'manager_links41','file':str(area/'test-result.json'),'sha256':sha(raw),'image':image,'passed':result['passed']}),flush=True);assert result['passed']
    folder=m.root_directory(m.BASE/str(uuid.uuid4()));m.save(folder/'manager-links41-qualified-run.json',raw);m.save(folder/'manager-links41-results.xml',m.read(Path(result['junit_file'])))
    line={'version':'retention276-inventory54-workspace53-contention42-manager-links41-lineage-v5','prior_lineage':old26['qualification_lineage'],
        'manager_links41_run_sha256':sha(raw),'manager_links41_source_commit':manifest['source_commit'],'runtime_changed_files_from_parent42':sorted(PRODUCTION),
        'retention_runtime_source_count':141,'composed_source_manifest_sha256':sha(wire(current)),'prior_276_54_53_42_not_rerun':True}
    m.save(folder/'lineage.json',wire(line)+b'\n');m.save(folder/'browser-seccomp.json',m.read(seccomp));checked=q.suite(m,image,current,[m.SELECTION],{m.GROUP:26},folder,folder/'browser-seccomp.json')
    after=q.production();proof={'version':m.VERSION,'tests':checked['tests'],'test_groups':checked['test_groups'],'exit_code':checked['exit_code'],
        'failures':checked['failure'],'errors':checked['error'],'skipped':checked['skipped'],'network':'none','application_data_mounted':False,
        'production_profile_mounted':False,'secrets_environment_injected':False,'oom_killed':checked['oom_killed'],'timed_out':checked['timed_out'],
        'full_suite_passed':checked['passed'],'no_pid_or_oom_denial':checked['resource_limits_passed'],
        'runtime_matches_declared_candidate':checked['sources_unchanged'],'mounts_match_isolated_allowlist':True,'mounted_sources_unchanged':checked['sources_unchanged'],
        'source_files':m.source_files(current),'image':image,'container_id':checked['container_id'],'qualified_parent_sha256':sha(raw),
        'source_archive_sha256':a.sha256,'source_commit':manifest['source_commit'],'junit_file':str(m.CONTAINER_PROOF/'results.xml'),
        'junit_host_file':checked['junit_file'],'junit_sha256':checked['junit_sha256'],'resource_before':checked['resource_before'],'resource_after':checked['resource_after'],
        'duration_seconds':checked['seconds'],'host_file':str(folder/'test-attestation.json'),'source_manifest_sha256':sha(wire(current)),
        'qualification_helper_sha256':sha(m.read(Path(__file__).resolve())),'qualification_lineage':line,
        'original_qualification_helper_sha256':M_SHA,'production_before':before,'production_after':after,'production_all_five_unchanged':before==after}
    praw=wire(proof)+b'\n';m.save(folder/'test-attestation.json',praw);assert proof['full_suite_passed'] and proof['production_all_five_unchanged']
    release=m.release_template(owner,proof,praw);assert release['authorization_sha256']==q.OWNER_SHA;m.save(folder/'release-template.json',wire(release)+b'\n')
    print(json.dumps({'stage':'retention26','state':'qualified_not_installed','file':str(folder/'test-attestation.json'),'sha256':sha(praw),'image':image,
        'tests':checked['tests'],'release_template':str(folder/'release-template.json'),'release_template_sha256':sha(m.read(folder/'release-template.json')),
        'readonly_mount':str(folder)+':'+str(m.CONTAINER_PROOF)+':ro'}),flush=True)

if __name__=='__main__':main()
