"""Offline provider simulations; no credentials, CloudBase calls, or deletion."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from adapters.cloudbase_backup import CloudBaseBackup, CloudBaseBackupConfig, CloudBaseClassicStorage, MAX_JSON
from services.hub.errors import HubError


def config(**kwargs):
    return replace(CloudBaseBackupConfig(enabled=True, env_id='backup-test-env', storage_mode='classic',
        storage_hosts=('bucket.cos.ap-shanghai.myqcloud.com',), existing_resource_authorized=True,
        private_storage_verified=True, max_archive_bytes=1000000), **kwargs)


class Response:
    def __init__(self, value=b'', status=200, headers=None):
        self.data = io.BytesIO(value); self.status = status; self.headers = headers or {}
    def read(self, count): return self.data.read(count)
    def getheader(self, name, default=None): return self.headers.get(name, default)


class Connection:
    def __init__(self): self.closed = False
    def close(self): self.closed = True


class Provider:
    """Exact HTTP-contract simulation; verifies real body streaming and full GET."""
    def __init__(self):
        self.objects = {}; self.calls = []; self.connections = []; self.truncate = False
        self.corrupt = False; self.auth = []; self.put_timeout = False; self.before_put = None
        self.bad_host = None; self.api_status = 200; self.api_body = None
    def authorize(self, method, url, body):
        self.auth.append((method, url, body)); return 'Bearer mock-server-secret'
    def request(self, url, allowed_hosts, *, method='GET', headers=None, body=None):
        self.calls.append((method, url, headers)); c = Connection(); self.connections.append(c)
        if method == 'POST':
            assert headers['Authorization'] == 'Bearer mock-server-secret'
            value = json.loads(body)[0]
            if self.api_body is not None: return c, Response(self.api_body, self.api_status)
            if 'upload-info' in url:
                key = value['objectId']; host = self.bad_host or allowed_storage()
                result = {'objectId': key, 'uploadUrl': 'https://'+host+'/'+key,
                    'cloudObjectId': 'cloud://backup-test-env.bucket/'+key,
                    'authorization': 'mock-cos-upload-signature', 'token': 'mock-cos-token', 'cloudObjectMeta': 'mock-meta'}
            else:
                file_id = value['cloudObjectId']; key = file_id.split('/', 3)[3]
                result = {'cloudObjectId': file_id, 'downloadUrl': 'https://'+allowed_storage()+'/'+key+'?signed=mock-read-token'}
            return c, Response(json.dumps([result]).encode(), self.api_status)
        key = url.split('/', 3)[3].split('?', 1)[0]
        if method == 'PUT':
            assert headers['Authorization'] == 'mock-cos-upload-signature'
            assert 'mock-server-secret' not in str(headers)
            if self.put_timeout: raise TimeoutError('DO_NOT_LOG_SIGNED_URL')
            self.objects[key] = body.read()
            assert len(self.objects[key]) == int(headers['Content-Length'])
            if self.before_put: self.before_put()
            return c, Response(b'', 200)
        assert method == 'GET' and not any(k.lower() in ('authorization', 'cookie') for k in headers)
        data = self.objects[key]
        if self.truncate: data = data[:-1]
        if self.corrupt: data = b'X' + data[1:]
        return c, Response(data)


def allowed_storage(): return 'bucket.cos.ap-shanghai.myqcloud.com'


@pytest.fixture
def setup_backup(tmp_path):
    root = tmp_path/'hub'; directory = root/'backups'; directory.mkdir(parents=True)
    file = directory/'consistent.sqlite3'; file.write_bytes(b'frozen SQLite snapshot fixture')
    provider = Provider(); cfg = config()
    transport = CloudBaseClassicStorage(cfg, provider.authorize, request=provider.request)
    return root, file, provider, transport, CloudBaseBackup(root, cfg, transport)


def test_full_upload_readback_manifest_durable_receipt_without_deletion(setup_backup):
    root, file, provider, transport, backup = setup_backup
    result = backup.upload_archive(file, kind='database_snapshot')
    assert result['remote_verified'] and result['archive_readback']['method'] == 'full_remote_get'
    assert result['manifest_readback']['method'] == 'full_remote_get'
    assert [x[0] for x in provider.calls] == ['POST','PUT','POST','GET','POST','PUT','POST','GET']
    assert len(provider.objects) == 2 and transport.stats['object_gets'] == 2
    assert all(c.closed for c in provider.connections)
    receipts = list((root/'backups/cloudbase-receipts').glob('*'))
    assert len(receipts) == 2 and len(list((root/'backups/cloudbase-receipts').glob('*.partial'))) == 0
    saved = Path(result['receipt_file']); assert json.loads(saved.read_bytes())['remote_verified'] is True
    assert file.exists() and not result['local_deletion_performed']
    assert 'mock-server-secret' not in saved.read_text() and 'mock-read-token' not in saved.read_text()
    if os.name == 'posix': assert saved.stat().st_mode & 0o077 == 0
    candidate = backup.archive_prune_eligibility(file, saved)
    assert candidate['additional_retention_policy_required'] and not candidate['local_deletion_performed']


def test_two_uploads_use_different_names_without_overwriting_provider_objects(setup_backup):
    _, file, provider, _, backup = setup_backup
    a = backup.upload_archive(file, kind='database_snapshot'); b = backup.upload_archive(file, kind='database_snapshot')
    assert a['archive_key'] != b['archive_key'] and len(provider.objects) == 4
    assert a['sha256'] == b['sha256']


@pytest.mark.parametrize('cfg,code', [
    (CloudBaseBackupConfig(), 'CLOUDBASE_BACKUP_DISABLED'),
    (config(existing_resource_authorized=False), 'CLOUDBASE_BACKUP_AUTHORIZATION_REQUIRED'),
    (config(private_storage_verified=False), 'CLOUDBASE_BACKUP_AUTHORIZATION_REQUIRED'),
    (config(storage_mode='pg'), 'CLOUDBASE_BACKUP_STORAGE_MODE_UNSUPPORTED'),
    (config(env_id=''), 'CLOUDBASE_BACKUP_CONFIG_INVALID'),
    (config(object_prefix='../secrets'), 'CLOUDBASE_BACKUP_CONFIG_INVALID'),
    (config(object_prefix='backup//nested'), 'CLOUDBASE_BACKUP_CONFIG_INVALID'),
    (config(max_archive_bytes=0), 'CLOUDBASE_BACKUP_CONFIG_INVALID'),
    (config(storage_hosts=()), 'CLOUDBASE_BACKUP_CONFIG_INVALID')])
def test_configuration_stops_before_credential_access_or_network(setup_backup, cfg, code):
    root, file, provider, _, _ = setup_backup
    storage = CloudBaseClassicStorage(cfg, provider.authorize, request=provider.request)
    with pytest.raises(HubError, match=code): CloudBaseBackup(root, cfg, storage).upload_archive(file, kind='database_snapshot')
    assert not provider.auth and not provider.calls


@pytest.mark.parametrize('relative', ['data/hub.sqlite3','media/source.mp4','logs/live.log','backups/db.sqlite3-wal','backups/unfinished.partial'])
def test_active_paths_and_unfinished_files_are_rejected(setup_backup, relative):
    root, _, provider, _, backup = setup_backup
    file = root/relative; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(b'active content')
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_PATH_REJECTED'):
        backup.upload_archive(file, kind='database_snapshot')
    assert not provider.calls and file.exists()


@pytest.mark.parametrize('kind', ['video','active_database','credentials','browser_profile'])
def test_non_backup_kinds_are_rejected(setup_backup, kind):
    _, file, provider, _, backup = setup_backup
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_KIND_REJECTED'): backup.upload_archive(file, kind=kind)
    assert not provider.calls


@pytest.mark.parametrize('fault', ['truncate','corrupt'])
def test_upload_success_and_same_etag_never_substitute_for_readback(setup_backup, fault):
    root, file, provider, _, backup = setup_backup
    setattr(provider, fault, True)
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_READBACK_MISMATCH'):
        backup.upload_archive(file, kind='tweet_archive')
    assert file.exists() and not list((root/'backups/cloudbase-receipts').glob('*.verified.json'))
    assert len(list((root/'backups/cloudbase-receipts').glob('*.intent.json'))) == 1


def test_put_timeout_is_unknown_not_automatically_retried(setup_backup):
    root, file, provider, _, backup = setup_backup; provider.put_timeout = True
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_UPLOAD_UNKNOWN') as error:
        backup.upload_archive(file, kind='log_archive')
    assert not error.value.retryable and error.value.review and str(error.value) == 'CLOUDBASE_BACKUP_UPLOAD_UNKNOWN'
    assert [x[0] for x in provider.calls] == ['POST','PUT']
    assert file.exists() and len(list((root/'backups/cloudbase-receipts').glob('*.intent.json'))) == 1


def test_archive_mutation_after_upload_blocks_final_receipt(setup_backup):
    root, file, provider, _, backup = setup_backup
    provider.before_put = lambda: file.write_bytes(b'CHANGED snapshot during external upload')
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_LOCAL_ARCHIVE_CHANGED'):
        backup.upload_archive(file, kind='database_snapshot')
    assert not list((root/'backups/cloudbase-receipts').glob('*.verified.json'))


@pytest.mark.parametrize('host', ['127.0.0.1','metadata.google.internal','attacker.example'])
def test_signed_upload_url_cannot_escape_exact_storage_host_allowlist(setup_backup, host):
    _, file, provider, _, backup = setup_backup; provider.bad_host = host
    with pytest.raises(HubError, match='HTTPS_DESTINATION_NOT_AUTHORIZED'):
        backup.upload_archive(file, kind='database_snapshot')
    assert len(provider.calls) == 1


@pytest.mark.parametrize('status,code', [(401,'AUTH_EXPIRED'),(403,'ACCESS_DENIED'),(429,'RATE_LIMITED'),(503,'NETWORK_ERROR'),(302,'HTTP_REJECTED')])
def test_status_failures_are_distinct_and_do_not_follow_redirects(setup_backup, status, code):
    _, file, provider, _, backup = setup_backup; provider.api_status = status
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_'+code): backup.upload_archive(file, kind='database_snapshot')
    assert len(provider.calls) == 1


def test_provider_error_does_not_include_server_secret_or_signed_url(setup_backup):
    _, file, provider, _, backup = setup_backup
    provider.api_body = json.dumps([{'code':'ACTION_FORBIDDEN','message':'mock-server-secret https://signed?token=secret'}]).encode()
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_ACCESS_DENIED') as error:
        backup.upload_archive(file, kind='database_snapshot')
    assert 'secret' not in str(error.value)


def test_oversize_api_response_stops_before_upload(setup_backup):
    _, file, provider, _, backup = setup_backup; provider.api_body = b' '*(MAX_JSON+1)
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_RESPONSE_LIMIT'): backup.upload_archive(file, kind='database_snapshot')
    assert len(provider.calls) == 1


def test_a_receipt_does_not_allow_changed_local_archive_or_any_active_log_to_be_pruned(setup_backup):
    root, file, _, _, backup = setup_backup; receipt = backup.upload_archive(file, kind='database_snapshot')
    active = root/'logs/live.log'; active.parent.mkdir(); active.write_bytes(file.read_bytes())
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_PATH_REJECTED'):
        backup.archive_prune_eligibility(active, receipt['receipt_file'])
    file.write_bytes(b'new database snapshot under same file name')
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_RECEIPT_INVALID'):
        backup.archive_prune_eligibility(file, receipt['receipt_file'])
    assert file.exists() and active.exists()


def test_expired_receipt_is_not_a_current_deletion_candidate(setup_backup):
    _, file, _, _, backup = setup_backup; receipt = backup.upload_archive(file, kind='database_snapshot')
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_RECEIPT_INVALID'):
        backup.archive_prune_eligibility(file, receipt['receipt_file'], max_age_seconds=0)


def test_unverified_intent_cannot_authorize_archive_pruning(setup_backup):
    root, file, provider, _, backup = setup_backup; provider.truncate = True
    with pytest.raises(HubError): backup.upload_archive(file, kind='database_snapshot')
    intent = next((root/'backups/cloudbase-receipts').glob('*.intent.json'))
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_RECEIPT_INVALID'): backup.archive_prune_eligibility(file, intent)


@pytest.mark.parametrize('field,value', [('kind','log_archive'),('archive_key','another-prefix/archive'),
    ('manifest_file_id','cloud://wrong-environment.bucket/manifest.json'),('manifest_sha256','0'*64)])
def test_corrupted_receipt_is_bound_to_the_exact_remote_manifest(setup_backup, field, value):
    _, file, _, _, backup = setup_backup; result = backup.upload_archive(file, kind='database_snapshot')
    receipt = Path(result['receipt_file']); saved = json.loads(receipt.read_bytes()); saved[field] = value
    receipt.write_text(json.dumps(saved))
    with pytest.raises(HubError): backup.archive_prune_eligibility(file, receipt)
    assert file.exists()


def test_credential_callback_failure_is_redacted(setup_backup):
    root, file, provider, _, _ = setup_backup
    def unavailable(*args): raise RuntimeError('mock-real-looking-secret-must-not-escape')
    storage = CloudBaseClassicStorage(config(), unavailable, request=provider.request)
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_CREDENTIAL_UNAVAILABLE') as error:
        CloudBaseBackup(root, config(), storage).upload_archive(file, kind='database_snapshot')
    assert 'mock-real-looking-secret' not in str(error.value) and not provider.calls


def test_receipts_cannot_recursively_be_uploaded_as_source_archives(setup_backup):
    _, file, provider, _, backup = setup_backup; result = backup.upload_archive(file, kind='database_snapshot')
    calls = len(provider.calls)
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_PATH_REJECTED'):
        backup.upload_archive(result['receipt_file'], kind='database_snapshot')
    assert len(provider.calls) == calls


def test_no_unlimited_receipt_age_escape(setup_backup):
    _, file, _, _, backup = setup_backup; result = backup.upload_archive(file, kind='database_snapshot')
    with pytest.raises(HubError, match='CLOUDBASE_BACKUP_RECEIPT_INVALID'):
        backup.archive_prune_eligibility(file, result['receipt_file'], max_age_seconds=float('inf'))
