"""Qualify bounded native draft hydration and retention26 on one isolated image.

The existing 254/26 image is immutable. This builds one COPY layer, runs only
the manifest's exact test selections and the complete retention suite, and
writes qualification evidence. It never deploys or opens production accounts.
"""
from pathlib import Path, PurePosixPath
import argparse, hashlib, json, os, re, tarfile, types, uuid

ROOT = Path('/opt/lighthouse-hub')
BASE = 'sha256:2bf4c8496b6c4e74d660fd2658812a55af2665fc2dc331421d2cb8e3334d9891'
TAG = 'lighthouse-hub:two-replay-repair-980abd885f33'
Q_SHA = '401ba7df4a291bc0dbdc46b5f72d6287f4afab3b5da5dc22926d48c2f956c387'
M_SHA = '4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
PARENT = Path('/var/lib/lighthouse-backup-ops/two-replay-repair-v6-tests/2873e728-b1ed-4569-9de6-d44a72524105/test-result.json')
PARENT_SHA = 'b92d7591f8db007673d1f3be52bddb4d86df9f1025371415683bced4d18e55a4'
OLD26 = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/8c1a9103-355f-47e3-a146-2424885b3280/test-attestation.json')
OLD26_SHA = '06a6d93aa663e74b06af5b135727d4eb4f4483396e860bf0adec9ea3df9f2f17'
PRODUCTION = {'adapters/replay_later_result_lookup_v2.mjs'}
RUNNER = 'scripts/ops/qualify_draft_hydration_v7.py'
RECOVERY_OPERATOR = 'scripts/ops/retire_streamer-b_preclick_dispatch_v1.py'
RETENTION_PARSER = 'scripts/ops/qualify_replay_parts_retention_v5.py'


def sha(raw): return hashlib.sha256(raw).hexdigest()
def wire(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def load(file, pin, name):
    raw = file.read_bytes(); assert sha(raw) == pin
    module = types.ModuleType(name); module.__file__ = str(file)
    exec(compile(raw, str(file), 'exec'), module.__dict__); return module


def runtime_sources(m, manifest):
    files = manifest['files']
    selected = {n for n in files if n.startswith(('services/hub/', 'adapters/')) and n.endswith('.py')} | set(m.NODE_SOURCES)
    assert len(selected) == 141 and selected <= set(files)
    result = {n:files[n]['sha256'] for n in sorted(selected)}
    assert all(re.fullmatch('[a-f0-9]{64}', v) for v in result.values())
    return result


def validate_parent(m, parent_raw, old_raw, parent_xml, old_xml):
    assert sha(parent_raw) == PARENT_SHA and sha(old_raw) == OLD26_SHA
    parent = json.loads(parent_raw); old26 = json.loads(old_raw)
    assert parent['version'] == 'two-replay-repair-ubuntu-v6'
    assert len(parent['source_manifest']['files']) == 889
    assert parent['passed'] is True and parent['tests'] == 254 and parent['image'] == old26['image'] == BASE
    assert sha(parent_xml) == parent['junit_sha256'] and sha(old_xml) == old26['junit_sha256']
    assert m.junit(parent_xml, 254) == parent['test_groups']
    assert m.junit(old_xml, 26, group=m.GROUP) == old26['test_groups']
    assert old26['version'] == m.VERSION and old26['full_suite_passed'] is True
    assert old26['qualified_parent_sha256'] == PARENT_SHA
    assert old26['source_files'] == runtime_sources(m, parent['source_manifest'])
    return parent, old26


def validate_candidate(m, manifest, parent):
    assert manifest['version'] == 'draft-hydration-candidate-v7' and manifest['base_image'] == BASE
    assert manifest['parent_sha256'] == PARENT_SHA and manifest['parent_retention_sha256'] == OLD26_SHA
    assert manifest['base_source_manifest'] == parent['source_manifest']
    current = manifest['source_manifest']; pins = manifest['delta_sha256']; groups = manifest['test_groups']
    assert re.fullmatch('[a-f0-9]{40}', manifest['source_commit']) and current['source_commit'] == manifest['source_commit']
    assert manifest['production_runtime_changed_files'] == sorted(PRODUCTION)
    assert groups and all(re.fullmatch(r'tests[.](?:ops[.])?test_[a-z0-9_]+', k) and type(v) is int and v > 0 for k,v in groups.items())
    assert type(current['expected_test_count']) is int and current['expected_test_count'] == sum(groups.values())
    selections = manifest['test_selections']
    assert selections and len(selections) == len(set(selections))
    for selection in selections:
        file = selection.split('::', 1)[0]
        assert re.fullmatch(r'tests/(?:ops/)?test_[a-z0-9_]+[.]py', file)
    assert current['test_selections'] == selections
    assert PRODUCTION <= set(pins) and len(pins) <= 40
    assert pins.get(RETENTION_PARSER) == M_SHA
    assert all(n in PRODUCTION or n in (RUNNER, RECOVERY_OPERATOR, RETENTION_PARSER) or (n.startswith('tests/') and '\\' not in n and '..' not in PurePosixPath(n).parts) for n in pins)
    assert all(re.fullmatch('[a-f0-9]{64}', v) for v in pins.values())
    old = parent['source_manifest']['files']; new = current['files']
    # The frozen retention parser is deliberately shipped and hash checked
    # even when this parent already contains those exact parser bytes.
    unchanged_helper = {RETENTION_PARSER} if old.get(RETENTION_PARSER) == new.get(RETENTION_PARSER) else set()
    assert set(old) <= set(new) and {n for n in old if old[n] != new[n]} == (set(pins) & set(old)) - unchanged_helper
    assert set(new) - set(old) == set(pins) - set(old)
    prior = runtime_sources(m, parent['source_manifest']); result = runtime_sources(m, current)
    assert set(prior) == set(result) and {n for n in prior if prior[n] != result[n]} == PRODUCTION
    assert all(pins[n] == new[n]['sha256'] for n in pins)
    return current, result


def extract_payload(tar, payload, manifest):
    entries = tar.getmembers(); pins = manifest['delta_sha256']; files = manifest['source_manifest']['files']
    assert len(entries) == len(pins) + 1 and {e.name for e in entries} == set(pins) | {'manifest.json'}
    for item in entries:
        rel = PurePosixPath(item.name)
        assert item.isfile() and not rel.is_absolute() and '..' not in rel.parts and '\\' not in item.name
        assert 0 < item.size < 1_500_000
    # Validate all bytes before creating any payload files.
    blobs = {}
    for item in entries:
        if item.name == 'manifest.json': continue
        raw = tar.extractfile(item).read()
        assert sha(raw) == pins[item.name] == files[item.name]['sha256'] and len(raw) == files[item.name]['bytes']
        blobs[item.name] = raw
    for name, raw in blobs.items():
        target = payload / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw); target.chmod(0o644)


def validate_image_lineage(base, candidate):
    assert base['Id'] == BASE
    assert len(base['RootFS']['Layers']) == 6 and len(candidate['RootFS']['Layers']) == 7
    assert candidate['RootFS']['Layers'][:-1] == base['RootFS']['Layers']
    assert candidate['Config'] == base['Config']
    assert all(candidate.get(k) == base.get(k) for k in ('Os', 'Architecture', 'Variant'))


def write_dockerfile(q, context):
    assert q.run(['docker','image','inspect','-f','{{.Id}}',TAG]).stdout.strip() == BASE
    (context / 'Dockerfile').write_text('FROM ' + TAG + '\nCOPY --chown=0:0 payload/ /opt/lighthouse-hub/\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('archive', 'suite-helper', 'retention-helper'): parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--sha256', required=True); args = parser.parse_args()
    assert os.name == 'posix' and os.geteuid() == 0 and __debug__; os.umask(0o077)
    q = load(args.suite_helper, Q_SHA, 'frozen_suite'); m = load(args.retention_helper, M_SHA, 'frozen_retention')
    parent_raw = m.read(PARENT, protected=True); old_raw = m.read(OLD26, protected=True)
    assert sha(parent_raw) == PARENT_SHA and sha(old_raw) == OLD26_SHA
    parent = json.loads(parent_raw); old26 = json.loads(old_raw)
    parent, old26 = validate_parent(m, parent_raw, old_raw, m.read(Path(parent['junit_file'])), m.read(Path(old26['junit_host_file'])))
    owner = json.loads(m.read(q.OWNER, protected=True)); assert m.owner(owner) == q.OWNER_SHA
    before = q.production(); assert before['video-worker']['image'] == BASE
    assert args.archive.parent == Path('/home/lighthouse-ops') and args.archive.resolve() == args.archive
    assert sha(m.read(args.archive, maximum=2_000_000)) == args.sha256
    area = m.root_directory(Path('/var/lib/lighthouse-backup-ops/draft-hydration-v7-tests') / str(uuid.uuid4()))
    context = area / 'context'; context.mkdir(mode=0o700)
    payload = context / 'payload'; payload.mkdir(mode=0o755)
    with tarfile.open(args.archive, 'r:gz') as tar:
        entry = tar.getmember('manifest.json'); assert entry.isfile() and 0 < entry.size < 1_500_000
        manifest = json.loads(tar.extractfile(entry).read())
        current, new_sources = validate_candidate(m, manifest, parent)
        extract_payload(tar, payload, manifest)
    groups = manifest['test_groups']
    for folder in payload.rglob('*'):
        if folder.is_dir(): folder.chmod(0o755)
    (payload / 'candidate-manifest.json').write_bytes(wire(current) + b'\n'); (payload / 'candidate-manifest.json').chmod(0o644)
    write_dockerfile(q, context)
    tag = 'lighthouse-hub:draft-hydration-' + args.sha256[:12]
    build = q.run(['docker','build','--network=none','--pull=false','-t',tag,str(context)], timeout=240)
    m.save(area/'build.log', (build.stdout + '\n' + build.stderr).encode())
    image = q.run(['docker','image','inspect','-f','{{.Id}}',tag]).stdout.strip()
    validate_image_lineage(m.inspect_image(BASE), m.inspect_image(image))
    seccomp = ROOT/'config/browser-seccomp.json'; assert sha(m.read(seccomp)) == current['files']['config/browser-seccomp.json']['sha256']
    result = q.suite(m, image, current, manifest['test_selections'], groups, area, seccomp)
    result.update(version='draft-hydration-ubuntu-v7', base_image=BASE, source_commit=manifest['source_commit'],
                  archive_sha256=args.sha256, manifest=manifest, source_manifest=current, source_layer_delta=1,
                  parent_sha256=PARENT_SHA, parent_retention_sha256=OLD26_SHA,
                  production_before=before, production_after=q.production())
    result['production_all_five_unchanged'] = before == result['production_after']
    result['passed'] = result['passed'] and result['production_all_five_unchanged']
    raw = wire(result) + b'\n'; m.save(area/'test-result.json', raw)
    print(json.dumps({'stage':'draft_hydration','file':str(area/'test-result.json'),'sha256':sha(raw),'image':image,'passed':result['passed']}), flush=True)
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
             'qualification_lineage':{'prior_lineage':old26['qualification_lineage'],'draft_hydration_sha256':sha(raw),'runtime_changed_files':sorted(PRODUCTION)},
             'production_before':before,'production_after':q.production()}
    proof['production_all_five_unchanged'] = proof['production_before'] == proof['production_after']
    praw = wire(proof) + b'\n'; m.save(folder/'test-attestation.json',praw)
    assert proof['full_suite_passed'] and proof['production_all_five_unchanged']
    release = m.release_template(owner,proof,praw); m.save(folder/'release-template.json',wire(release)+b'\n')
    print(json.dumps({'stage':'retention26','file':str(folder/'test-attestation.json'),'sha256':sha(praw),'image':image,
                      'release_template':str(folder/'release-template.json'),
                      'release_template_sha256':sha(wire(release)+b'\n')}), flush=True)


if __name__ == '__main__': main()
