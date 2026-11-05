"""App-UID v1b encoding-checked operator record helper. Default is read/check only; no browser action."""
from pathlib import Path
import argparse,hashlib,json,os,sys,uuid
JOB='2b3cc3ac-2922-478d-a84a-df8fca441235';BV='BV1cVxb67EMm';UID='1000000000000000'
META='1886a531fab14d905670d5290ba1c78bcb8aa0db3bb0a18ed7afb4affcb3ea9b'

EVIDENCE_ID='c56c03b4-76a2-4c9c-8bbd-00c41c3be6c0'
SHOTS=('native.png','public.png','source-native-form.png','deletion-native.png','deletion-public.png')

def validate_normalized_screenshots(folder,plan):
    # This is encoding validation before opening SQLite, not human verification.
    assert folder.name==EVIDENCE_ID and plan['version']=='actual-short-assisted-evidence-plan-v1b'
    assert plan['supersedes_encoding_only']['original_evidence_archive_sha256']=='3b47ee45598f52282140864f7d7722109de0a361af7a6224617d5f5fcbce6403'
    expected={'deletion-native.png','deletion-public.png','deletion.json','native.json','native.png',
        'operator-requests.json','public.json','public.png','source-deletion-native.json',
        'source-deletion-public.json','source-manager.json','source-native-form.json','source-native-form.png',
        'source-public.json','source-published-cover.jpg','evidence-conversion-provenance.json',
        'native-original.jpg','public-original.jpg','source-native-form-original.jpg',
        'deletion-native-original.jpg','deletion-public-original.jpg'}
    assert set(plan['files'])==expected
    assert {p.name for p in folder.iterdir()}==expected|{'recording-plan.json'}
    pfile=folder/'evidence-conversion-provenance.json'
    assert plan['screenshot_encoding_normalization']==plan['files'][pfile.name]
    p=json.loads(pfile.read_bytes())
    assert p['version']=='screenshot-encoding-normalization-v1' and p['evidence_uuid']==EVIDENCE_ID
    assert p['original_evidence_archive_sha256']==plan['supersedes_encoding_only']['original_evidence_archive_sha256']
    assert p['observation_times_rewritten'] is False and p['image_content_edited'] is False
    assert p['ai_image_generation_or_editing'] is False and len(p['conversions'])==5
    assert {item['original_archive_member'] for item in p['conversions']}==set(SHOTS)
    for item in p['conversions']:
        name=item['original_archive_member'];rawname=Path(name).stem+'-original.jpg'
        assert item['normalized_png']==plan['files'][name] and item['raw_original']==plan['files'][rawname]
        raw=(folder/rawname).read_bytes();png=(folder/name).read_bytes()
        assert raw[:3]==bytes.fromhex('ffd8ff') and raw[6:11]==b'JFIF'+bytes([0])
        assert png[:16]==bytes.fromhex('89504e470d0a1a0a0000000d49484452')
        assert (int.from_bytes(png[16:20],'big'),int.from_bytes(png[20:24],'big'))==(item['width'],item['height'])
        assert item['decoded_pixel_format']=='rgb24' and item['decoded_pixel_bytes']==item['width']*item['height']*3
        assert item['source_decoded_pixel_sha256']==item['png_decoded_pixel_sha256']
        assert len(item['source_decoded_pixel_sha256'])==64 and item['decoded_pixel_bytes_identical'] is True
        assert item['comparison']=='full_decoded_RGB24_byte_equality'
        assert item['auto_rotation'] is False and item['resize'] is False and item['crop'] is False and item['filters']==[]
        assert item['original_encoded_bytes_preserved'] is True and item['normalized_png_is_original_encoded_bytes'] is False
    # Preserve the actual five-line observations. Normalization remains exactly
    # one platform-added source URL + LF, independent of screenshot encoding.
    native=json.loads((folder/'native.json').read_bytes());public=json.loads((folder/'public.json').read_bytes())
    prefix=plan['normalized_metadata']['source']+'\n'
    for observation in (native,public):
        assert observation['observed_description']==prefix+plan['normalized_metadata']['description']
        assert observation['normalized_description']==plan['normalized_metadata']['description']
    return {'version':p['version'],'conversion_provenance_sha256':plan['files'][pfile.name]['sha256'],
        'png_headers_checked':5,'raw_jpeg_inputs_retained':5,'database_written':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir',required=True)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--record-result',action='store_true');mode.add_argument('--record-deletion',action='store_true')
    parser.add_argument('--auth-reference');parser.add_argument('--operator-reference');parser.add_argument('--owner-delete-response');parser.add_argument('--owner-delete-reference')
    args=parser.parse_args()
    assert os.name=='posix' and os.getuid()==os.getgid()==10001,'RUN_AS_APPLICATION_UID_10001_ONLY'
    root=Path('/opt/lighthouse-hub');folder=Path(args.evidence_dir)
    assert folder.parent==root/'media'/JOB/'owner-assisted-v1' and str(uuid.UUID(folder.name))==folder.name and folder.resolve()==folder
    assert not any(p.is_symlink() for p in (folder,*folder.parents))
    plan=json.loads((folder/'recording-plan.json').read_bytes())
    assert plan['server_directory']==str(folder) and plan['job_id']==JOB and plan['external_id']==BV and plan['site_uid']==UID and plan['metadata_sha256']==META
    for name,reference in plan['files'].items():
        file=folder/name;assert file.parent==folder and not file.is_symlink() and file.is_file()
        raw=file.read_bytes();assert reference=={'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    encoding_evidence=validate_normalized_screenshots(folder,plan)
    os.environ['HUB_ROOT']=str(root);sys.path.insert(0,str(root))
    from services.hub.config import Settings
    from services.hub.db import Store
    from services.hub.short_video_web import ShortVideoWeb,PRINCIPAL
    from services.hub.youtube_download import digest
    from services.hub.owner_assisted_short_result_v1 import record_owner_assisted_result_v1,record_owner_authorized_remote_deletion_v1
    settings=Settings.load();assert settings.db_path==root/'data/hub.sqlite3' and settings.db_path.is_file()
    service=ShortVideoWeb(settings,Store(settings.db_path));job,binding,state=service.snapshot(JOB,write=False)
    assert binding['site_uid']==UID and binding['metadata_sha256']==META and binding['metadata']==plan['normalized_metadata']
    if not args.record_result and not args.record_deletion:
        print(json.dumps({'state':'checked_recording_plan_only','job_id':JOB,'current_job_state':job['state'],
            'pipeline_state':state['state'],'binding_sha256':digest(binding),'screenshot_encoding_evidence':encoding_evidence,'database_written':False}));return
    requests=json.loads((folder/'operator-requests.json').read_bytes()) if (folder/'operator-requests.json').is_file() else {}
    if args.record_result:
        requested=requests.get('result',{});auth_reference=args.auth_reference or requested.get('auth_response_reference')
        operator_reference=args.operator_reference or requested.get('actual_operator_reference')
        assert auth_reference and operator_reference
        result=record_owner_assisted_result_v1(service,PRINCIPAL,JOB,binding_sha256=digest(binding),
            actual_auth_response='验证成功了',auth_response_reference=auth_reference,actual_operator_reference=operator_reference,
            native_observation_file=plan['native_observation']['file'],native_observation_sha256=plan['native_observation']['sha256'],
            public_observation_file=plan['public_observation']['file'],public_observation_sha256=plan['public_observation']['sha256'])
    else:
        requested=requests.get('deletion',{});owner_response=args.owner_delete_response or requested.get('actual_owner_delete_response')
        owner_reference=args.owner_delete_reference or requested.get('owner_response_reference')
        operator_reference=args.operator_reference or requested.get('actual_operator_reference')
        assert owner_response and owner_reference and operator_reference
        file=folder/'deletion.json';assert file.is_file() and not file.is_symlink()
        result=record_owner_authorized_remote_deletion_v1(service,PRINCIPAL,JOB,external_id=BV,
            actual_owner_delete_response=owner_response,owner_response_reference=owner_reference,
            actual_operator_reference=operator_reference,deletion_observation_file=str(file),
            deletion_observation_sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
