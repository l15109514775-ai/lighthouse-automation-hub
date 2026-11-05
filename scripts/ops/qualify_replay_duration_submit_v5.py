"""Run the frozen duration/submit-observation delta and retention suite on one isolated image.

Reuses the existing suite and retention validators. No production data mounts,
provider requests, queue changes, or deployment. Arguments are owner-staged files.
"""
from pathlib import Path, PurePosixPath
import argparse, hashlib, json, os, re, subprocess, sys, tarfile, types, uuid

ROOT = Path('/opt/lighthouse-hub')
BASE = 'sha256:09a9db1b11e35fe562480577bdf93813ba619f28b9e3cd792a319e9e76746c24'
TAG = 'lighthouse-hub:readiness-loading-39beb479f3a1'
Q_SHA = '401ba7df4a291bc0dbdc46b5f72d6287f4afab3b5da5dc22926d48c2f956c387'
M_SHA = '4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
PARENT = Path('/var/lib/lighthouse-backup-ops/replay-readiness-loading-v5-tests/81817c53-dc4d-47f2-89cf-f6b5f74f62b3/test-result.json')
PARENT_SHA = '788f1dd515ac3de111682a6942ab74ea8de9b268e483913ae6dcd64c9032d321'
OLD26 = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/c57c0901-168d-409a-8ae3-93eeebaacbe8/test-attestation.json')
OLD26_SHA = '30756b8adc0eede0cd29f9b304cb5084ff7f7f19baca7ccb03fc3a81ba3ee826'
PRODUCTION = {'services/hub/replay_fifo_v4.py','services/hub/youtube_download_v4.py','adapters/replay_native_driver.mjs'}


def sha(raw): return hashlib.sha256(raw).hexdigest()
def wire(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
def load(file, pin, name):
    raw = file.read_bytes(); assert sha(raw) == pin
    module = types.ModuleType(name); module.__file__ = str(file)
    exec(compile(raw, str(file), 'exec'), module.__dict__); return module

def runtime_sources(m, manifest):
    selected={n for n in manifest['files'] if n.startswith(('services/hub/','adapters/')) and n.endswith('.py')} | set(m.NODE_SOURCES)
    assert len(selected)==141
    return {n:manifest['files'][n]['sha256'] for n in sorted(selected)}

def updated_sources(m, current, previous):
    prior = runtime_sources(m, previous)
    selected = {n for n in current['files'] if n.startswith(('services/hub/', 'adapters/')) and n.endswith('.py')} | set(m.NODE_SOURCES)
    assert selected == set(prior) and len(selected) == 141
    result = {n:current['files'][n]['sha256'] for n in sorted(selected)}
    assert all(re.fullmatch('[a-f0-9]{64}', v) for v in result.values())
    assert {n for n in result if result[n] != prior[n]} == PRODUCTION
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('archive', 'suite-helper', 'retention-helper'): parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--sha256', required=True); args = parser.parse_args()
    assert os.name == 'posix' and os.geteuid() == 0 and __debug__; os.umask(0o077)
    q = load(args.suite_helper, Q_SHA, 'frozen_suite'); m = load(args.retention_helper, M_SHA, 'frozen_retention')
    parent_raw = m.read(PARENT, protected=True); old_raw = m.read(OLD26, protected=True)
    assert sha(parent_raw) == PARENT_SHA and sha(old_raw) == OLD26_SHA
    parent = json.loads(parent_raw); old26 = json.loads(old_raw)
    assert parent['passed'] and parent['image'] == old26['image'] == BASE
    parent_xml = m.read(Path(parent['junit_file'])); old_xml = m.read(Path(old26['junit_host_file']))
    assert sha(parent_xml) == parent['junit_sha256'] and sha(old_xml) == old26['junit_sha256']
    assert m.junit(parent_xml, 112) == parent['test_groups']
    assert m.junit(old_xml, 26, group=m.GROUP) == old26['test_groups']
    assert old26['full_suite_passed'] and old26['source_files'] == runtime_sources(m,parent['source_manifest'])
    owner = json.loads(m.read(q.OWNER, protected=True)); assert m.owner(owner) == q.OWNER_SHA
    before = q.production(); assert before['video-worker']['image'] == BASE
    assert args.archive.parent == Path('/home/lighthouse-ops') and args.archive.resolve() == args.archive
    assert sha(m.read(args.archive, maximum=2_000_000)) == args.sha256
    area = m.root_directory(Path('/var/lib/lighthouse-backup-ops/replay-duration-submit-v5-tests') / str(uuid.uuid4()))
    context = area / 'context'; context.mkdir(mode=0o700)
    payload = context / 'payload'; payload.mkdir(mode=0o755)
    with tarfile.open(args.archive, 'r:gz') as tar:
        entries = tar.getmembers(); manifest = json.load(tar.extractfile('manifest.json'))
        assert manifest['version'] == 'replay-duration-submit-candidate-v5' and manifest['base_image'] == BASE
        assert manifest['parent_sha256'] == PARENT_SHA and manifest['parent_retention_sha256'] == OLD26_SHA
        assert manifest['base_source_manifest'] == parent['source_manifest']
        current = manifest['source_manifest']; pins = manifest['delta_sha256']; groups = manifest['test_groups']
        assert re.fullmatch('[a-f0-9]{40}', manifest['source_commit']) and current['source_commit'] == manifest['source_commit']
        assert manifest['production_runtime_changed_files'] == sorted(PRODUCTION)
        assert current['expected_test_count'] == sum(groups.values()) > 0
        assert all(re.fullmatch(r'tests[.](?:ops[.])?test_[a-z0-9_]+', k) and type(v) is int and v > 0 for k,v in groups.items())
        assert len(entries) == len(pins) + 1 and {e.name for e in entries} == set(pins) | {'manifest.json'}
        assert set(PRODUCTION) <= set(pins) and len(pins) <= 30
        assert all(n in PRODUCTION or n.startswith(('tests/', 'scripts/ops/resume_')) for n in pins)
        old = parent['source_manifest']['files']; new = current['files']
        assert set(old) <= set(new) and {n for n in old if old[n] != new[n]} == set(pins) & set(old)
        assert set(new) - set(old) == set(pins) - set(old)
        old_sources = runtime_sources(m,parent['source_manifest']); new_sources = updated_sources(m, current, parent['source_manifest'])
        assert set(old_sources) == set(new_sources) and {n for n in old_sources if old_sources[n] != new_sources[n]} == PRODUCTION
        for item in entries:
            rel = PurePosixPath(item.name)
            assert item.isfile() and not rel.is_absolute() and '..' not in rel.parts and 0 < item.size < 1_500_000
            if item.name == 'manifest.json': continue
            raw = tar.extractfile(item).read()
            assert sha(raw) == pins[item.name] == new[item.name]['sha256'] and len(raw) == new[item.name]['bytes']
            target = payload / item.name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw); target.chmod(0o644)
    for d in payload.rglob('*'):
        if d.is_dir(): d.chmod(0o755)
    (payload / 'candidate-manifest.json').write_bytes(wire(current) + b'\n'); (payload / 'candidate-manifest.json').chmod(0o644)
    assert q.run(['docker','image','inspect','-f','{{.Id}}',TAG]).stdout.strip() == BASE
    (context / 'Dockerfile').write_text('FROM ' + TAG + '\nCOPY --chown=0:0 payload/ /opt/lighthouse-hub/\n')
    tag = 'lighthouse-hub:duration-submit-' + args.sha256[:12]
    build = q.run(['docker','build','--network=none','--pull=false','-t',tag,str(context)], timeout=240)
    m.save(area/'build.log', (build.stdout + '\n' + build.stderr).encode())
    image = q.run(['docker','image','inspect','-f','{{.Id}}',tag]).stdout.strip()
    base = m.inspect_image(BASE); candidate = m.inspect_image(image)
    assert len(base['RootFS']['Layers'])==3 and len(candidate['RootFS']['Layers'])==4
    assert candidate['RootFS']['Layers'][:-1] == base['RootFS']['Layers']
    assert candidate['Config']==base['Config'] and all(candidate.get(k)==base.get(k) for k in ('Os','Architecture','Variant'))
    seccomp = ROOT/'config/browser-seccomp.json'; assert sha(m.read(seccomp)) == current['files']['config/browser-seccomp.json']['sha256']
    result = q.suite(m, image, current, manifest['test_selections'], groups, area, seccomp)
    result.update(version='replay-duration-submit-ubuntu-v5', base_image=BASE, source_commit=manifest['source_commit'],
                  archive_sha256=args.sha256, manifest=manifest, source_manifest=current, source_layer_delta=1,
                  parent_sha256=PARENT_SHA, parent_retention_sha256=OLD26_SHA,
                  production_before=before, production_after=q.production())
    result['production_all_five_unchanged'] = before == result['production_after']
    result['passed'] = result['passed'] and result['production_all_five_unchanged']
    raw = wire(result) + b'\n'; m.save(area/'test-result.json', raw)
    print(json.dumps({'stage':'duration_submit','file':str(area/'test-result.json'),'sha256':sha(raw),'image':image,'passed':result['passed']}), flush=True)
    assert result['passed']
    folder = m.root_directory(m.BASE/str(uuid.uuid4())); m.save(folder/'browser-seccomp.json', m.read(seccomp))
    checked = q.suite(m,image,current,[m.SELECTION],{m.GROUP:26},folder,folder/'browser-seccomp.json')
    proof = {'version':m.VERSION,'tests':checked['tests'],'test_groups':checked['test_groups'],'exit_code':checked['exit_code'],
             'failures':checked['failure'],'errors':checked['error'],'skipped':checked['skipped'],'network':'none',
             'application_data_mounted':False,'production_profile_mounted':False,'secrets_environment_injected':False,
             'oom_killed':checked['oom_killed'],'timed_out':checked['timed_out'],'full_suite_passed':checked['passed'],
             'no_pid_or_oom_denial':checked['resource_limits_passed'],'runtime_matches_declared_candidate':checked['sources_unchanged'],
             'mounts_match_isolated_allowlist':checked['runtime_matches_allowlist'],'mounted_sources_unchanged':checked['sources_unchanged'],
             'source_files':new_sources,'image':image,'container_id':checked['container_id'],
             'qualified_parent_sha256':sha(raw),'source_archive_sha256':args.sha256,'source_commit':manifest['source_commit'],
             'junit_file':str(m.CONTAINER_PROOF/'results.xml'),'junit_host_file':checked['junit_file'],'junit_sha256':checked['junit_sha256'],
             'resource_before':checked['resource_before'],'resource_after':checked['resource_after'],'duration_seconds':checked['seconds'],
             'host_file':str(folder/'test-attestation.json'),'source_manifest_sha256':sha(wire(current)),
             'qualification_lineage':{'prior_lineage':old26['qualification_lineage'],'duration_submit_sha256':sha(raw),'runtime_changed_files':sorted(PRODUCTION)},
             'production_before':before,'production_after':q.production()}
    proof['production_all_five_unchanged'] = proof['production_before'] == proof['production_after']
    praw = wire(proof) + b'\n'; m.save(folder/'test-attestation.json',praw)
    assert proof['full_suite_passed'] and proof['production_all_five_unchanged']
    release = m.release_template(owner,proof,praw); m.save(folder/'release-template.json',wire(release)+b'\n')
    print(json.dumps({'stage':'retention26','file':str(folder/'test-attestation.json'),'sha256':sha(praw),'image':image,
                      'release_template':str(folder/'release-template.json'),
                      'release_template_sha256':sha(wire(release)+b'\n'),'tests':26,'passed':True}),flush=True)

if __name__ == '__main__':
    try: main()
    except subprocess.CalledProcessError as error:
        if error.stderr:sys.stderr.write(error.stderr[-12000:])
        raise
