"""Owned protocol checks only; no Docker, SSH, accounts or qualification claim.

Synthetic manifests/XML are explicit cryptographic-input fixtures. JUnit is
parsed by the real frozen retention helper, and tar extraction uses real files.
"""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import importlib.util, json, tarfile
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT/file)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


op = load('owned_draft_hydration_qualifier', 'scripts/ops/qualify_draft_hydration_v7.py')
ret = load('owned_draft_hydration_retention_parser', 'scripts/ops/qualify_replay_parts_retention_v5.py')


def xml(group, count, failed=False):
    cases = ''.join('<testcase classname="' + group + '" name="owned_' + str(i) + '">' +
                    ('<failure/>' if failed and i == count - 1 else '') + '</testcase>' for i in range(count))
    return ('<testsuites><testsuite>' + cases + '</testsuite></testsuites>').encode()


@pytest.fixture
def inputs(monkeypatch):
    fixed = set(ret.NODE_SOURCES) | op.PRODUCTION
    files = {f'services/hub/owned_{i:03}.py':{'bytes':1,'sha256':op.sha(b'a')} for i in range(141-len(fixed))}
    files.update({name:{'bytes':1,'sha256':op.sha(b'a')} for name in (*ret.NODE_SOURCES, *op.PRODUCTION)})
    files.update({f'tests/test_owned_ancestor_{i:03}.py':{'bytes':1,'sha256':op.sha(b'a')} for i in range(889-len(files))})
    old_manifest = {'source_commit':'a'*40,'files':files}
    junit = xml('tests.test_owned_parent', 254); old_junit = xml(ret.GROUP, 26)
    parent = {'version':'two-replay-repair-ubuntu-v6','passed':True,'tests':254,'image':op.BASE,
              'junit_sha256':op.sha(junit),'test_groups':{'tests.test_owned_parent':254},'source_manifest':old_manifest}
    parent_raw = op.wire(parent); monkeypatch.setattr(op,'PARENT_SHA',op.sha(parent_raw))
    old26 = {'version':ret.VERSION,'full_suite_passed':True,'image':op.BASE,'qualified_parent_sha256':op.PARENT_SHA,
             'junit_sha256':op.sha(old_junit),'test_groups':{ret.GROUP:26},'source_files':op.runtime_sources(ret,old_manifest)}
    old_raw = op.wire(old26); monkeypatch.setattr(op,'OLD26_SHA',op.sha(old_raw))
    parser_raw = (ROOT/op.RETENTION_PARSER).read_bytes(); assert op.sha(parser_raw) == op.M_SHA
    blobs = {name:b'owned two-repair source delta ' + name.encode() + b'\n' for name in op.PRODUCTION}
    blobs.update({'tests/test_owned_draft_hydration.py':b'owned fixture\n',op.RETENTION_PARSER:parser_raw})
    current = deepcopy(old_manifest); current.update(source_commit='b'*40,expected_test_count=2,
                                                   test_selections=['tests/test_owned_draft_hydration.py'])
    for name, raw in blobs.items(): current['files'][name] = {'bytes':len(raw),'sha256':op.sha(raw)}
    manifest = {'version':'draft-hydration-candidate-v7','base_image':op.BASE,
                'parent_sha256':op.PARENT_SHA,'parent_retention_sha256':op.OLD26_SHA,
                'base_source_manifest':deepcopy(old_manifest),'source_manifest':current,'source_commit':'b'*40,
                'production_runtime_changed_files':sorted(op.PRODUCTION),
                'delta_sha256':{name:op.sha(raw) for name,raw in blobs.items()},
                'test_groups':{'tests.test_owned_draft_hydration':2},'test_selections':current['test_selections'][:]}
    return parent,parent_raw,old_raw,junit,old_junit,manifest,blobs


def test_parent_requires_exact_saved_254_and26_raw_hashes_and_complete_junit(inputs,monkeypatch):
    parent,raw,oldraw,junit,oldxml,_,_ = inputs
    assert op.validate_parent(ret,raw,oldraw,junit,oldxml)[0] == parent
    with pytest.raises(AssertionError): op.validate_parent(ret,raw+b' ',oldraw,junit,oldxml)
    bad = xml('tests.test_owned_parent',254,failed=True)
    changed = {**parent,'junit_sha256':op.sha(bad)}; changed_raw = op.wire(changed)
    monkeypatch.setattr(op,'PARENT_SHA',op.sha(changed_raw))
    with pytest.raises(ValueError,match='ACTUAL_COMPLETE_JUNIT'):op.validate_parent(ret,changed_raw,oldraw,bad,oldxml)


def test_existing_parent_proof_paths_are_not_rewritten_into_new_candidate_area():
    assert op.PARENT.parent.parent.name=='two-replay-repair-v6-tests'
    assert op.PARENT.parent.name=='2873e728-b1ed-4569-9de6-d44a72524105'
    assert op.OLD26.parent.parent.name=='replay-parts-retention-v5-tests'
    assert op.OLD26.parent.name=='8c1a9103-355f-47e3-a146-2424885b3280'


def test_parent_retention_must_have_same_image_manifest_and_exact_parent(inputs,monkeypatch):
    _,raw,oldraw,junit,oldxml,_,_ = inputs
    original = json.loads(oldraw)
    for field,value in [('image','sha256:'+'f'*64),('qualified_parent_sha256','f'*64),('source_files',{})]:
        changed = {**original,field:value}; changedraw = op.wire(changed)
        monkeypatch.setattr(op,'OLD26_SHA',op.sha(changedraw))
        with pytest.raises(AssertionError):op.validate_parent(ret,raw,changedraw,junit,oldxml)


def test_actual_group_count_comes_from_manifest_and_not_old254(inputs):
    parent,_,_,_,_,manifest,_ = inputs
    current,sources = op.validate_candidate(ret,manifest,parent)
    assert len(sources) == 141 and current['expected_test_count'] == 2
    manifest['test_groups']['tests.test_owned_draft_hydration'] = 5
    current['expected_test_count'] = 5
    assert op.validate_candidate(ret,manifest,parent)[0]['expected_test_count'] == 5
    current['expected_test_count'] = 254
    with pytest.raises(AssertionError):op.validate_candidate(ret,manifest,parent)


def test_scope_refuses_other_runtime_or_configuration_even_when_self_consistently_hashed(inputs):
    parent,_,_,_,_,manifest,_ = inputs
    for name in ('services/hub/owned_000.py','config/hub.toml','scripts/ops/unrelated_operator.py'):
        changed = deepcopy(manifest); raw = b'forbidden extra change'
        changed['source_manifest']['files'][name] = {'bytes':len(raw),'sha256':op.sha(raw)}
        changed['delta_sha256'][name] = op.sha(raw)
        with pytest.raises(AssertionError):op.validate_candidate(ret,changed,parent)
    # This exact runner may accompany its owned tests; it is outside the
    # immutable 141-path production scope and grants no additional runtime.
    runner_raw = (ROOT/op.RUNNER).read_bytes()
    manifest['source_manifest']['files'][op.RUNNER] = {'bytes':len(runner_raw),'sha256':op.sha(runner_raw)}
    manifest['delta_sha256'][op.RUNNER] = op.sha(runner_raw)
    recovery_raw = b'owned exact recovery operator'
    manifest['source_manifest']['files'][op.RECOVERY_OPERATOR] = {'bytes':len(recovery_raw),'sha256':op.sha(recovery_raw)}
    manifest['delta_sha256'][op.RECOVERY_OPERATOR] = op.sha(recovery_raw)
    assert len(op.validate_candidate(ret,manifest,parent)[1]) == 141
    bad = deepcopy(manifest);bad['delta_sha256'][op.RETENTION_PARSER] = 'f'*64
    bad['source_manifest']['files'][op.RETENTION_PARSER]['sha256'] = 'f'*64
    with pytest.raises(AssertionError):op.validate_candidate(ret,bad,parent)
    bad = deepcopy(manifest);bad['delta_sha256'].pop(op.RETENTION_PARSER)
    bad['source_manifest']['files'].pop(op.RETENTION_PARSER)
    with pytest.raises(AssertionError):op.validate_candidate(ret,bad,parent)


def test_parent_manifest_and_141_source_membership_cannot_drift(inputs):
    parent,_,_,_,_,manifest,_ = inputs
    for change in ('parent','remove','add'):
        bad = deepcopy(manifest)
        if change == 'parent':bad['base_source_manifest']['source_commit'] = 'f'*40
        elif change == 'remove':bad['source_manifest']['files'].pop('services/hub/owned_000.py')
        else:
            name = 'services/hub/unqualified_new.py'; bad['source_manifest']['files'][name] = {'bytes':1,'sha256':op.sha(b'a')}
            bad['delta_sha256'][name] = op.sha(b'a')
        with pytest.raises(AssertionError):op.validate_candidate(ret,bad,parent)


def test_test_counts_are_positive_integers_and_selections_cannot_inject_options(inputs):
    parent,_,_,_,_,manifest,_ = inputs
    for value in (True,0,-1,2.5):
        bad = deepcopy(manifest);bad['test_groups']['tests.test_owned_draft_hydration'] = value
        with pytest.raises(AssertionError):op.validate_candidate(ret,bad,parent)
    for selection in ('-k','tests/../source.py','scripts/ops/run.py'):
        bad = deepcopy(manifest);bad['test_selections'] = [selection];bad['source_manifest']['test_selections'] = [selection]
        with pytest.raises(AssertionError):op.validate_candidate(ret,bad,parent)


def archive(manifest,blobs,*,corrupt=None,link=None):
    stream = BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as tar:
        for name,raw in {'manifest.json':op.wire(manifest),**blobs}.items():
            item = tarfile.TarInfo(name);item.size = len(raw)
            if name == link:item.type = tarfile.SYMTYPE;item.linkname = '/not-owned'
            tar.addfile(item,BytesIO(b'x'*len(raw) if name == corrupt else raw))
    stream.seek(0);return tarfile.open(fileobj=stream,mode='r:gz')


def test_real_tar_bytes_are_verified_before_any_payload_is_written(inputs,tmp_path):
    manifest,blobs = inputs[-2:]
    with archive(manifest,blobs,corrupt='tests/test_owned_draft_hydration.py') as tar:
        with pytest.raises(AssertionError):op.extract_payload(tar,tmp_path,manifest)
    assert not list(tmp_path.iterdir())
    with archive(manifest,blobs) as tar:op.extract_payload(tar,tmp_path,manifest)
    assert all((tmp_path/name).read_bytes() == raw for name,raw in blobs.items())


def test_tar_symlinks_and_path_traversal_are_rejected_without_writes(inputs,tmp_path):
    manifest,blobs = deepcopy(inputs[-2]),deepcopy(inputs[-1])
    with archive(manifest,blobs,link=next(iter(blobs))) as tar:
        with pytest.raises(AssertionError):op.extract_payload(tar,tmp_path,manifest)
    assert not list(tmp_path.iterdir())
    blobs['../outside'] = b'bad';manifest['delta_sha256']['../outside'] = op.sha(b'bad')
    with archive(manifest,blobs) as tar:
        with pytest.raises(AssertionError):op.extract_payload(tar,tmp_path,manifest)
    assert not list(tmp_path.iterdir())


def test_parent_six_plus_one_layer_preserves_complete_config_and_platform(tmp_path):
    base = {'Id':op.BASE,'RootFS':{'Layers':['a','b','c','d','e','f']},'Config':{'Env':['OWNED=fixture'],'Cmd':['owned'],'User':'10001:10001'},
            'Os':'linux','Architecture':'amd64','Variant':''}
    candidate = deepcopy(base);candidate['Id'] = 'sha256:'+'e'*64;candidate['RootFS']['Layers'].append('delta')
    op.validate_image_lineage(base,candidate)
    for change in ('ancestry','extra_layer','env','command','platform'):
        bad = deepcopy(candidate)
        if change == 'ancestry':bad['RootFS']['Layers'][0] = 'foreign'
        elif change == 'extra_layer':bad['RootFS']['Layers'].append('extra')
        elif change == 'env':bad['Config']['Env'].append('EXTRA=refused')
        elif change == 'command':bad['Config']['Cmd'] = ['other']
        else:bad['Architecture'] = 'arm64'
        with pytest.raises(AssertionError):op.validate_image_lineage(base,bad)
    calls = []
    def inspect_tag(argv):calls.append(argv);return SimpleNamespace(stdout=op.BASE+'\n')
    op.write_dockerfile(SimpleNamespace(run=inspect_tag),tmp_path)
    assert calls == [['docker','image','inspect','-f','{{.Id}}',op.TAG]]
    assert (tmp_path/'Dockerfile').read_text() == 'FROM '+op.TAG+'\nCOPY --chown=0:0 payload/ /opt/lighthouse-hub/\n'
    (tmp_path/'Dockerfile').unlink()
    with pytest.raises(AssertionError):op.write_dockerfile(SimpleNamespace(run=lambda argv:SimpleNamespace(stdout='sha256:'+'f'*64)),tmp_path)
    assert not (tmp_path/'Dockerfile').exists()


def test_exact_lookup_runtime_delta_is_required_and_no_second_is_accepted(inputs):
    parent,_,_,_,_,manifest,_ = inputs
    assert len(op.PRODUCTION) == 1
    for name in op.PRODUCTION:
        missing = deepcopy(manifest)
        missing['delta_sha256'].pop(name)
        missing['source_manifest']['files'][name] = parent['source_manifest']['files'][name]
        with pytest.raises(AssertionError):op.validate_candidate(ret,missing,parent)
    changed = deepcopy(manifest)
    changed['production_runtime_changed_files'].append('adapters/extra.py')
    with pytest.raises(AssertionError):op.validate_candidate(ret,changed,parent)


def test_existing_identical_frozen_parser_is_the_only_allowed_unchanged_payload(inputs):
    parent,_,_,_,_,manifest,_ = deepcopy(inputs)
    frozen = deepcopy(manifest['source_manifest']['files'][op.RETENTION_PARSER])
    parent['source_manifest']['files'][op.RETENTION_PARSER] = frozen
    manifest['base_source_manifest'] = deepcopy(parent['source_manifest'])
    # The helper must accompany its exact pinned qualification parser even
    # when that parser was already present, byte-identical, in the parent.
    assert manifest['delta_sha256'][op.RETENTION_PARSER] == frozen['sha256'] == op.M_SHA
    assert len(op.validate_candidate(ret,manifest,parent)[1]) == 141
    # This exception cannot silently package an unchanged unrelated script
    # or test as a claimed delta, nor weaken the one-runtime-file equality.
    name = 'tests/test_owned_unchanged.py'
    unchanged = {'bytes':1,'sha256':op.sha(b'a')}
    parent['source_manifest']['files'][name] = deepcopy(unchanged)
    manifest['base_source_manifest'] = deepcopy(parent['source_manifest'])
    manifest['source_manifest']['files'][name] = deepcopy(unchanged)
    manifest['delta_sha256'][name] = unchanged['sha256']
    with pytest.raises(AssertionError):op.validate_candidate(ret,manifest,parent)
