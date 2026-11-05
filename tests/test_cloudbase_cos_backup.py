"""Offline COS REST/signature checks; never use real account credentials."""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from adapters.cloudbase_backup import CloudBaseBackup
from adapters.cloudbase_cos_backup import CloudBaseCOSConfig, CloudBaseCOSStorage, cos_canonical_request, cos_sign_headers
from services.hub.errors import HubError


def config(**kw):
    bucket='example-cloudbase-bucket-1250000000';region='ap-shanghai'
    return replace(CloudBaseCOSConfig(enabled=True, env_id='example-cloudbase-env', storage_mode='classic',
        bucket=bucket, region=region, owner_app_id='1250000000', storage_hosts=(bucket+'.cos.'+region+'.myqcloud.com',),
        existing_resource_authorized=True, private_storage_verified=True, max_archive_bytes=1000000,
        cloudbase_storage_binding_reference='Offline fixture binding; not a real CloudBase assertion'), **kw)


class Response:
    def __init__(self, body=b'', status=200, headers=None): self.body=io.BytesIO(body);self.status=status;self.headers=headers or {}
    def read(self, n): return self.body.read(n)
    def getheader(self, k, default=None): return self.headers.get(k, default)


class Connection:
    def __init__(self): self.closed=False
    def close(self): self.closed=True


class COS:
    def __init__(self):
        self.objects={};self.calls=[];self.connections=[];self.auth_calls=0
        self.status=200;self.error=b'';self.corrupt=False;self.timeout=False
    def credentials(self):
        self.auth_calls+=1
        return {'SecretId':'AKIDMOCKEXAMPLE','SecretKey':'mock-secret-do-not-persist','Token':'mock-session-token'}
    def request(self, url, hosts, *, method='GET', headers=None, body=None, timeout=20):
        c=Connection();self.connections.append(c);self.calls.append((method,url,headers))
        assert timeout==900
        assert url.startswith('https://'+hosts[0]+'/lighthouse-hub/backups/v1/')
        assert '.api.tcloudbasegateway.com' not in url
        assert headers['Authorization'].startswith('q-sign-algorithm=sha1&')
        assert headers['x-cos-security-token']=='mock-session-token'
        assert 'x-cos-security-token' in headers['Authorization']
        if self.timeout: raise TimeoutError('secret-bearing-provider-error')
        if self.status != 200: return c, Response(self.error, self.status)
        key=url.split('/',3)[3]
        if method=='PUT':
            assert headers['x-cos-forbid-overwrite']=='true' and headers['x-cos-storage-class']=='STANDARD'
            if key in self.objects: return c,Response(b'',409)
            self.objects[key]=body.read();assert len(self.objects[key])==int(headers['Content-Length'])
            return c,Response()
        if method=='HEAD': return c,Response(headers={'Content-Length':str(len(self.objects[key]))})
        assert method=='GET';data=self.objects[key]
        return c,Response(b'wrong'+data if self.corrupt else data)


@pytest.fixture
def fixture(tmp_path):
    root=tmp_path/'hub';file=root/'backups/frozen.tar.gz';file.parent.mkdir(parents=True);file.write_bytes(b'Frozen backup fixture')
    cfg=config();provider=COS();storage=CloudBaseCOSStorage(cfg,provider.credentials,request=provider.request,clock=lambda:1793520000)
    return root,file,provider,storage,CloudBaseBackup(root,cfg,storage)


def test_cos_direct_roundtrip_uses_only_put_get_and_keeps_valid_durable_receipt(fixture):
    _,file,provider,storage,backup=fixture;result=backup.upload_archive(file,kind='database_snapshot')
    assert [x[0] for x in provider.calls]==['PUT','GET','PUT','GET']
    assert result['archive_file_id'].startswith('cos://example-cloudbase-bucket-1250000000/')
    assert result['archive_readback']['method']=='full_remote_get' and result['remote_verified']
    assert storage.stats['api_requests']==0 and storage.stats['object_gets']==2
    assert all(c.closed for c in provider.connections) and file.exists()
    saved=Path(result['receipt_file']);text=saved.read_text()
    assert 'mock-secret' not in text and 'mock-session-token' not in text and 'Authorization' not in text
    assert backup.archive_prune_eligibility(file,saved)['additional_retention_policy_required']


@pytest.mark.parametrize('changes,code', [
    ({'enabled':False},'CLOUDBASE_BACKUP_DISABLED'),({'private_storage_verified':False},'CLOUDBASE_BACKUP_AUTHORIZATION_REQUIRED'),
    ({'storage_mode':'pg'},'CLOUDBASE_BACKUP_STORAGE_MODE_UNSUPPORTED'),
    ({'owner_app_id':'1250000000'},'CLOUDBASE_COS_RESOURCE_BINDING_REQUIRED'),
    ({'cloudbase_storage_binding_reference':''},'CLOUDBASE_COS_RESOURCE_BINDING_REQUIRED'),
    ({'storage_hosts':('different.cos.ap-shanghai.myqcloud.com',)},'CLOUDBASE_COS_RESOURCE_BINDING_REQUIRED')])
def test_resource_and_private_permission_gates_precede_key_access(fixture,changes,code):
    root,file,provider,_,_=fixture;cfg=config(**changes);storage=CloudBaseCOSStorage(cfg,provider.credentials,request=provider.request)
    with pytest.raises(HubError,match=code): CloudBaseBackup(root,cfg,storage).upload_archive(file,kind='tweet_archive')
    assert provider.auth_calls==0 and not provider.calls


def test_cos_key_cannot_escape_backup_prefix_or_walk_directories(fixture):
    _,_,provider,storage,_=fixture
    for key in ('another/prefix','lighthouse-hub/backups/v1/../secret','/lighthouse-hub/backups/v1/x','lighthouse-hub/backups/v1/a?token=x'):
        with pytest.raises(HubError,match='CLOUDBASE_COS_OBJECT_KEY_REJECTED'):storage.put(key,io.BytesIO(b'x'),1)
    assert provider.auth_calls==0 and not provider.calls


def test_head_is_diagnostic_and_cannot_be_used_as_verification(fixture):
    _,file,provider,storage,backup=fixture;result=backup.upload_archive(file,kind='log_archive')
    check=storage.head(result['archive_file_id'],result['archive_key'])
    assert check=={'bytes':file.stat().st_size,'method':'head_only','remote_verified':False}
    assert provider.calls[-1][0]=='HEAD'


def test_archive_transfer_uses_the_bounded_900_second_socket_timeout(fixture):
    _,file,provider,storage,backup=fixture
    result=backup.upload_archive(file,kind='database_snapshot')
    assert result['remote_verified'] is True
    assert storage.request_timeout==900
    assert {call[0] for call in provider.calls}=={'PUT','GET'}


def test_no_overwrite_when_provider_object_already_exists(fixture):
    _,_,_,storage,_=fixture;key='lighthouse-hub/backups/v1/owned-unique-test/object'
    storage.put(key,io.BytesIO(b'x'),1)
    with pytest.raises(HubError,match='CLOUDBASE_COS_OBJECT_EXISTS'):storage.put(key,io.BytesIO(b'y'),1)


@pytest.mark.parametrize('body,code', [(b'<Error><Code>ExpiredToken</Code><Message>secret</Message></Error>','CLOUDBASE_BACKUP_AUTH_EXPIRED'),
    (b'<Error><Code>SignatureDoesNotMatch</Code><Message>secret</Message></Error>','CLOUDBASE_COS_SIGNATURE_REJECTED'),
    (b'<Error><Code>AccessDenied</Code></Error>','CLOUDBASE_BACKUP_ACCESS_DENIED')])
def test_cos_403_errors_are_classified_without_raw_error_leak(fixture,body,code):
    _,file,provider,_,backup=fixture;provider.status=403;provider.error=body
    with pytest.raises(HubError,match=code) as exc:backup.upload_archive(file,kind='database_snapshot')
    assert 'secret' not in str(exc.value) and len(provider.calls)==1


def test_lost_put_result_retains_intent_does_not_retry_or_delete(fixture):
    root,file,provider,_,backup=fixture;provider.timeout=True
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_UPLOAD_UNKNOWN'):backup.upload_archive(file,kind='database_snapshot')
    assert len(provider.calls)==1 and file.exists()
    assert len(list((root/'backups/cloudbase-receipts').glob('*.intent.json')))==1
    assert not list((root/'backups/cloudbase-receipts').glob('*.verified.json'))


@pytest.mark.parametrize('status,provider_code',[(400,'RequestTimeout'),(502,'InternalError')])
def test_ambiguous_put_http_response_is_unknown_with_only_safe_status_and_code(fixture,status,provider_code):
    root,file,provider,_,backup=fixture;provider.status=status
    provider.error=('<Error><Code>'+provider_code+'</Code><Message>owned-secret-message Authorization mock-key</Message></Error>').encode()
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_UPLOAD_UNKNOWN') as error:
        backup.upload_archive(file,kind='database_snapshot')
    assert error.value.safe_http=={'http_status':status,'provider_code':provider_code}
    assert error.value.cause_code in ('CLOUDBASE_BACKUP_HTTP_REJECTED','CLOUDBASE_BACKUP_NETWORK_ERROR')
    assert 'owned-secret' not in str(error.value) and 'Authorization' not in repr(error.value.safe_http)
    assert len(provider.calls)==1 and file.exists() and not list((root/'backups/cloudbase-receipts').glob('*.verified.json'))


def test_get_http_rejection_keeps_safe_diagnostics_without_becoming_a_put(fixture):
    _,_,provider,storage,_=fixture;provider.status=400;provider.error=b'<Error><Code>InvalidRequest</Code><Message>owned-private-message</Message></Error>'
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_HTTP_REJECTED') as error:
        storage.verify('cos://'+storage.config.bucket+'/lighthouse-hub/backups/v1/owned-object','lighthouse-hub/backups/v1/owned-object',1,'0'*64)
    assert error.value.safe_http=={'http_status':400,'provider_code':'InvalidRequest'} and len(provider.calls)==1
    assert 'private-message' not in str(error.value.safe_http)


def test_full_get_content_mismatch_prevents_receipt_even_if_put_succeeded(fixture):
    root,file,provider,_,backup=fixture;provider.corrupt=True
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_READBACK_MISMATCH'):backup.upload_archive(file,kind='database_snapshot')
    assert not list((root/'backups/cloudbase-receipts').glob('*.verified.json')) and file.exists()


def test_cos_reference_is_not_interchangeable_with_a_different_bucket(fixture):
    _,_,provider,storage,_=fixture
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_REMOTE_ID_INVALID'):
        storage.verify('cos://another-bucket-1250000000/lighthouse-hub/backups/v1/x','lighthouse-hub/backups/v1/x',1,'0'*64)
    assert not provider.calls


def test_canonical_request_matches_official_cos_unicode_example():
    # Official COS signature documentation, PUT example. This is public example
    # request data, never a real account secret or a live signature.
    request,names=cos_canonical_request('PUT','/exampleobject(腾讯云)',{
        'Date':'Thu, 16 May 2019 06:45:51 GMT','Host':'examplebucket-1250000000.cos.ap-beijing.myqcloud.com',
        'Content-Type':'text/plain','Content-Length':'13','Content-MD5':'mQ/fVh815F3k6TAUm8m0eg==',
        'x-cos-acl':'private','x-cos-grant-read':'uin="100000000011"'})
    assert hashlib.sha1(request.encode()).hexdigest()=='8b2751e77f43a0995d6e9eb9477f4b685cca4172'
    assert names=='content-length;content-md5;content-type;date;host;x-cos-acl;x-cos-grant-read'


def test_signer_uses_short_expiry_signed_host_and_short_lived_sts_token():
    headers=cos_sign_headers('GET','/lighthouse-hub/backups/v1/x',{'Host':'example.cos.ap-shanghai.myqcloud.com'},
        {'SecretId':'AKIDMOCKEXAMPLE','SecretKey':'mock-secret','Token':'mock-sts','ExpirationEpoch':1793520050},now=1793520000)
    params=parse_qs(headers['Authorization'],keep_blank_values=True)
    assert params['q-sign-time']==['1793519990;1793520050']
    assert params['q-key-time']==params['q-sign-time'] and params['q-sign-algorithm']==['sha1']
    assert params['q-header-list']==['host;x-cos-security-token']
    assert params['q-url-param-list']==[''] and len(params['q-signature'][0])==40
    assert headers['x-cos-security-token']=='mock-sts'


def test_sts_expiry_is_rejected_before_network_and_time_is_not_extended():
    with pytest.raises(HubError,match='CLOUDBASE_BACKUP_AUTH_EXPIRED'):
        cos_sign_headers('GET','/x',{'Host':'example.cos.ap-shanghai.myqcloud.com'},
            {'SecretId':'AKIDMOCKEXAMPLE','SecretKey':'mock-secret','ExpirationEpoch':1793520010},now=1793520000)


def test_actual_cam_template_is_prefix_and_ip_scoped_without_admin_or_delete_actions():
    template=json.loads((Path(__file__).resolve().parents[1]/'config/cloudbase-cos-backup-cam.example.json').read_text())
    assert {a for s in template['statement'] for a in s['action']}=={'name/cos:PutObject','name/cos:GetObject','name/cos:HeadObject'}
    for statement in template['statement']:
        assert statement['resource']==['qcs::cos:ap-shanghai:uid/1250000000:examplebucket-1250000000/lighthouse-hub/backups/v1/*']
        assert statement['condition']['ip_equal']=={'qcs:ip':['203.0.113.10/32']}
        assert statement['condition']['bool_equal']=={'cos:secure-transport':'true'}
