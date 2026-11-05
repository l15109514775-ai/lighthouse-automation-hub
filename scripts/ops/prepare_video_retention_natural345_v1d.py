"""Local immutable-parent composition only. Never deploys or deletes a file."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,io,json,tarfile

RETENTION_SHA='53402ed0bc95f00c3a3b594dbedc5a62c10f5a006257575858e7fbb9e37871bc'
NATURAL_SHA='d8b2193d0d5e229f322a440f73c549a468ceac8af4c25f54eb834d7a6a0f854b'
ACTUAL_345_SHA='0a2d704695f6fde158e73a3524859838731b9cdf00121712babd4e4c26dc6f75'
REFRESH='services/hub/video_retention_refresh_v1.py'
EXPECTED_CHANGES={'services/hub/short_video_web.py','services/hub/replay_video_web.py',
    'services/hub/replay_video_browser.py','scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs',
    'adapters/short_video_saved_draft_navigation_control.mjs'}
REPLACEMENTS=[
 ('Natural295 + retention + refresh combination.','Natural345 + retention + refresh combination.'),
 ('video-source-retention-fresh-natural-v1c-release.json','video-source-retention-fresh-natural-v1d-release.json'),
 ('video-source-retention-fresh-natural-v1c-ubuntu-tests','video-source-retention-fresh-natural-v1d-ubuntu-tests'),
 ("NATURAL_PARENT_ARCHIVE_SHA256 = '8fc11a3e27dca0dd2c08c12a34da111e70f37bc38ce74d6cdebb122840578e2a'",
  "NATURAL_PARENT_ARCHIVE_SHA256 = '"+NATURAL_SHA+"'")]
def sha(raw):return hashlib.sha256(raw).hexdigest()
def read_archive(file,expected,count):
    assert sha(file.read_bytes())==expected
    with tarfile.open(file,'r:gz') as tar:
        members=tar.getmembers();assert len(members)==count+1 and len({m.name for m in members})==len(members)
        assert all(m.isfile() and not m.issym() and not m.islnk() and 0<m.size<500000 for m in members)
        bodies={}
        for m in members:
            p=PurePosixPath(m.name);assert not p.is_absolute() and '..' not in p.parts and '\\' not in m.name
            bodies[m.name]=tar.extractfile(m).read()
    mr=bodies.pop('manifest.json');manifest=json.loads(mr)
    assert set(manifest['files'])==set(bodies)
    assert all(sha(bodies[n])==e['sha256'] and len(bodies[n])==e['bytes'] for n,e in manifest['files'].items())
    return manifest,bodies,sha(mr)
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--workspace',default=str(Path(__file__).resolve().parents[4]));args=parser.parse_args()
    base=Path(args.workspace).resolve();assert base.is_dir()
    old,bodies,old_manifest_sha=read_archive(base/'outputs/video-retention-fresh-natural-v1c.tar.gz',RETENTION_SHA,130)
    natural,nraw,natural_manifest_sha=read_archive(base/'outputs/natural-navigation-wait-v3-345.tar.gz',NATURAL_SHA,124)
    assert len(old['release_source_files'])==62 and len(natural['runtime_source_sha256'])==57
    names=set(natural['runtime_source_sha256']);extras=set(old['release_source_files'])-names
    assert len(extras)==5 and REFRESH in extras and names<set(old['release_source_files'])
    differences={n for n in names if sha(bodies[n])!=natural['runtime_source_sha256'][n]};assert differences==EXPECTED_CHANGES
    origins={n:{'kind':'immutable_retention_v1c_parent','archive_sha256':RETENTION_SHA,'member_sha256':sha(v)} for n,v in bodies.items()}
    for n in names:
        bodies[n]=nraw[n];origins[n]={'kind':'immutable_natural345_parent','archive_sha256':NATURAL_SHA,'member_sha256':sha(nraw[n])}
    before=bodies[REFRESH]
    for original,replacement in REPLACEMENTS:
        assert bodies[REFRESH].count(original.encode())==1
        bodies[REFRESH]=bodies[REFRESH].replace(original.encode(),replacement.encode())
    compile(bodies[REFRESH],REFRESH,'exec')
    origins[REFRESH]={'kind':'deterministic_release_identity_only_transform','parent_archive_sha256':RETENTION_SHA,
        'parent_member_sha256':sha(before),'replacements':REPLACEMENTS,'algorithm_behavior_changed':False}
    test='tests/test_video_retention_refresh_v1.py';old_test=bodies[test]
    anchor=b"    assert cls.TEST_COUNT == 74 and sum(cls.TEST_GROUPS.values()) == 74"
    assert old_test.count(anchor)==1
    extra=("\n    assert cls.TEST_VERSION == 'video-source-retention-fresh-natural-v1d-ubuntu-tests'"
        "\n    assert cls.RELEASE_FILENAME == 'video-source-retention-fresh-natural-v1d-release.json'"
        "\n    assert cls.NATURAL_PARENT_ARCHIVE_SHA256 == '"+NATURAL_SHA+"'").encode()
    bodies[test]=old_test.replace(anchor,anchor+extra)
    origins[test]={'kind':'same_owned_test_with_new_release_identity_assertions','parent_archive_sha256':RETENTION_SHA,'parent_member_sha256':sha(old_test),'test_count_changed':False}
    compile(bodies[test],test,'exec')
    source_map={n:sha(bodies[n]) for n in old['release_source_files']}
    assert {n:source_map[n] for n in names}==natural['runtime_source_sha256']
    changed={n for n in old['release_source_files'] if old['files'][n]['sha256']!=source_map[n]}
    assert changed==EXPECTED_CHANGES|{REFRESH}
    files={n:{'sha256':sha(raw),'bytes':len(raw),'origin':origins[n]} for n,raw in sorted(bodies.items())}
    manifest={**old,'version':'video-retention-fresh-natural345-ro-overlay-v1d',
        'parent_retention_v1c_archive_sha256':RETENTION_SHA,'parent_retention_v1c_manifest_sha256':old_manifest_sha,
        'parent_natural_345_archive_sha256':NATURAL_SHA,'parent_natural_345_manifest_sha256':natural_manifest_sha,
        'parent_actual_345_attestation_sha256':ACTUAL_345_SHA,'parent_retention_refresh_commit':old['retention_refresh_commit'],
        'retention_release_version':'video-source-retention-fresh-natural-v1d-ubuntu-tests',
        'retention_release_filename':'video-source-retention-fresh-natural-v1d-release.json',
        'runtime_source_sha256':source_map,'natural_runtime_source_sha256':natural['runtime_source_sha256'],
        'production_runtime_changed_files_from_retention_v1c':sorted(changed),'natural_runtime_changed_files_from_retention_v1c':sorted(differences),
        'files':files,'retention_algorithm_changed':False,'production_deployed':False,
        'live_execution_authorized_by_tests':False,'automatic_pipeline_enabled':False,
        'native_freshness_seconds_unchanged':300,'source_current_full_sha_required_each_call':True}
    for key in ('retention_refresh_commit','parent_natural_295_archive_sha256','parent_natural_source_commit','parent_actual_295_attestation_sha256'):manifest.pop(key,None)
    assert len(files)==130 and len(source_map)==62 and sum(manifest['expected_test_counts'].values())==manifest['expected_total_tests']==74
    raw=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode();archive=base/'outputs/video-retention-fresh-natural345-v1d.tar.gz'
    with archive.open('xb') as stream:
        with tarfile.open(fileobj=stream,mode='w:gz') as tar:
            for n,v in sorted({**bodies,'manifest.json':raw}.items()):
                info=tarfile.TarInfo(n);info.size=len(v);info.mode=0o644;info.mtime=0;tar.addfile(info,io.BytesIO(v))
    with (base/'outputs/video-retention-fresh-natural345-v1d-manifest.json').open('xb') as stream:stream.write(raw)
    stage=base/'work/video-retention-fresh-natural345-v1d-stage';stage.mkdir(mode=0o700)
    for n,v in {**bodies,'manifest.json':raw}.items():
        file=stage/n;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(v)
    print(json.dumps({'archive':str(archive),'sha256':sha(archive.read_bytes()),'manifest_sha256':sha(raw),
        'files':130,'runtime':62,'tests':74,'source_changes':sorted(changed),'refresh_sha256':sha(bodies[REFRESH]),'stage':str(stage)}))
if __name__=='__main__':main()
