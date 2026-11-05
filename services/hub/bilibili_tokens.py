"""One-use official OAuth refresh tokens. No website cookies or automatic consent."""
from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import re
import stat
import time
import uuid
from cryptography.fernet import Fernet, InvalidToken
from .db import encode
from .errors import HubError
from .locks import resource_guard

REFRESH_URL = 'https://api.bilibili.com/x/account-oauth2/v1/refresh_token'


def _secret(value):
    return isinstance(value, str) and re.fullmatch(r'[!-~]{1,4096}', value) is not None


class BilibiliTokens:
    def __init__(self, settings, store, http=None):
        self.settings, self.store, self.http = settings, store, http

    def _cipher(self):
        try:
            key = base64.urlsafe_b64decode(os.environ['EVENT_ENCRYPTION_KEY'])
            if len(key) != 32:
                raise ValueError()
            derived = hmac.new(key, b'lighthouse-bilibili-oauth-v1', hashlib.sha256).digest()
            return Fernet(base64.urlsafe_b64encode(derived))
        except (KeyError, ValueError):
            raise HubError('OAUTH_ENCRYPTION_KEY_REQUIRED', review=True) from None

    def _seal(self, account, app, generation, access, refresh):
        data = {'account': account, 'app': app, 'generation': generation,
                'access_token': access, 'refresh_token': refresh}
        return self._cipher().encrypt(encode(data).encode()).decode()

    def _open(self, row):
        try:
            data = json.loads(self._cipher().decrypt(row['encrypted_tokens'].encode()))
            if (data['account'] != row['account_id'] or data['app'] != row['app_id'] or
                    data['generation'] != row['generation'] or
                    not all(_secret(data[key]) for key in ('access_token', 'refresh_token'))):
                raise ValueError()
            return data
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise HubError('OAUTH_CREDENTIAL_INTEGRITY_FAILED', review=True) from None

    def _row(self, account):
        rows = self.store.query('SELECT * FROM bilibili_credentials WHERE account_id=?', (account,))
        return rows[0] if rows else None

    def _binding(self, row):
        if (row['account_id'] != os.getenv('BILIBILI_ACCOUNT_ID') or
                row['app_id'] != os.getenv('BILIBILI_ACCESS_KEY_ID') or
                row['account_id'] not in self.settings.config['authorized_bilibili_accounts']):
            raise HubError('OAUTH_ACCOUNT_OR_APP_CHANGED', review=True)

    def access_token(self, account, app):
        """None allows legacy env credentials only when no persisted row exists."""
        row = self._row(account)
        if row is None:
            return None
        self._binding(row)
        if row['app_id'] != app:
            raise HubError('OAUTH_ACCOUNT_OR_APP_CHANGED', review=True)
        if row['state'] != 'active':
            raise HubError(row['error_code'] or 'OAUTH_VALIDATION_OR_REAUTH_REQUIRED', review=True)
        if row['access_expires_at'] <= time.time():
            raise HubError('AUTH_EXPIRED', review=True)
        return self._open(row)['access_token']

    def status(self):
        rows = self.store.query('SELECT state,access_expires_at,error_code,validation_attempts,next_check_at FROM bilibili_credentials')
        return {'implemented': True, 'enabled': self.settings.config.get('bilibili_token_refresh_enabled', False),
                'configured_accounts': len(rows), 'accounts': rows,
                'one_use_refresh_no_automatic_replay': True}

    def import_file(self, path, principal, *, replace=False):
        if principal != 'maintenance':
            self.store.audit(principal, 'import_bilibili_tokens', 'bilibili', 'denied')
            raise HubError('ACCESS_DENIED')
        path = Path(path).absolute()
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise HubError('OAUTH_IMPORT_PRIVATE_FILE_REQUIRED', review=True)
        try:
            info = path.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 32768 or
                    (os.name != 'nt' and info.st_mode & 0o077)):
                raise ValueError()
            value = json.loads(path.read_text('utf-8-sig'))
            if set(value) != {'version','app_id','account_id','access_token','refresh_token','access_expires_at'}:
                raise ValueError()
            if (type(value['version']) is not int or value['version'] != 1 or
                    not all(_secret(value[key]) for key in ('app_id','account_id','access_token','refresh_token')) or
                    type(value['access_expires_at']) not in (int, float) or
                    not time.time() + 60 < value['access_expires_at'] <= time.time() + 366 * 86400):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise HubError('OAUTH_IMPORT_INVALID', review=True) from None
        self._binding(value)
        with resource_guard(self.settings.root / 'data/locks/bilibili-oauth'):
            with self.store.transaction() as db:
                old = db.execute('SELECT generation FROM bilibili_credentials WHERE account_id=?',
                                 (value['account_id'],)).fetchone()
                if old and not replace:
                    raise HubError('OAUTH_IMPORT_REPLACEMENT_REQUIRED', review=True)
                fingerprint=hashlib.sha256(value['refresh_token'].encode()).hexdigest()
                if db.execute('SELECT 1 FROM bilibili_token_refreshes WHERE account_id=? AND refresh_fingerprint=?',
                              (value['account_id'],fingerprint)).fetchone():
                    raise HubError('OAUTH_REPLACEMENT_CONSUMED_TOKEN', review=True)
                generation = old['generation'] + 1 if old else 1
                sealed = self._seal(value['account_id'], value['app_id'], generation,
                                    value['access_token'], value['refresh_token'])
                db.execute('''INSERT INTO bilibili_credentials(account_id,app_id,generation,encrypted_tokens,
                    access_expires_at,state,updated_at,error_code) VALUES(?,?,?,?,?,'validating',?,NULL)
                    ON CONFLICT(account_id) DO UPDATE SET app_id=excluded.app_id,generation=excluded.generation,
                    encrypted_tokens=excluded.encrypted_tokens,access_expires_at=excluded.access_expires_at,
                    state='validating',updated_at=excluded.updated_at,error_code=NULL,validation_attempts=0,next_check_at=0''',
                    (value['account_id'],value['app_id'],generation,sealed,value['access_expires_at'],time.time()))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                           (time.time(), principal, 'import_bilibili_tokens', 'bilibili', 'pending_identity_validation'))
        return {'imported': True, 'state': 'validating', 'network_requests_sent': 0, 'published': False}

    def _stop(self, row, state, code, refresh_state=None):
        with self.store.transaction() as db:
            db.execute('UPDATE bilibili_credentials SET state=?,error_code=?,updated_at=? WHERE account_id=? AND generation=?',
                       (state,code,time.time(),row['account_id'],row['generation']))
            if refresh_state:
                db.execute('''UPDATE bilibili_token_refreshes SET state=?,error_code=?,updated_at=?
                    WHERE account_id=? AND generation=? AND state='started' ''',
                    (refresh_state,code,time.time(),row['account_id'],row['generation']))
        self.store.event('auth.expired', {'provider':'bilibili','code':code},
                         dedup_key='bilibili-oauth:'+row['account_id']+':'+str(row['generation']))
        return {'state':state,'error_code':code}

    def _validate(self, row):
        from adapters.bilibili import BilibiliOfficial
        self._binding(row)
        if row['access_expires_at'] <= time.time():
            return self._stop(row, 'auth_expired', 'AUTH_EXPIRED')
        if row['validation_attempts'] >= self.settings.config['max_attempts']:
            return self._stop(row, 'auth_expired', 'OAUTH_VALIDATION_RETRY_EXHAUSTED')
        with self.store.transaction() as db:
            db.execute('''UPDATE bilibili_credentials SET validation_attempts=validation_attempts+1
                WHERE account_id=? AND generation=? AND state='validating' ''',
                (row['account_id'],row['generation']))
        try:
            candidate = self._open(row)['access_token']
            adapter = BilibiliOfficial(self.settings,self.store,self.http,access_token_override=candidate)
            adapter.account_identity()
        except HubError as exc:
            # Read-only validation can resume; the one-use refresh request must never repeat.
            if exc.retryable or exc.code in ('API_BUDGET_EXCEEDED','PAID_SERVICE_NOT_AUTHORIZED'):
                delay = max(exc.retry_after, min(3600, 30 * 2 ** row['validation_attempts']))
                with self.store.transaction() as db:
                    db.execute('''UPDATE bilibili_credentials SET next_check_at=?,error_code=?,updated_at=?
                        WHERE account_id=? AND generation=?''',
                        (time.time()+delay,exc.code,time.time(),row['account_id'],row['generation']))
                return {'state':'validating','error_code':exc.code}
            return self._stop(row, 'auth_expired', exc.code)
        with self.store.transaction() as db:
            changed = db.execute('''UPDATE bilibili_credentials SET state='active',error_code=NULL,updated_at=?
                WHERE account_id=? AND generation=? AND state='validating' ''',
                (time.time(),row['account_id'],row['generation'])).rowcount
            if changed != 1:
                raise HubError('OAUTH_GENERATION_CHANGED', review=True)
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (time.time(),'scheduler','bilibili_token_validation','bilibili','verified'))
        return {'state':'active','identity_and_scopes_verified':True}

    @staticmethod
    def _check_response(value):
        if not isinstance(value,dict) or type(value.get('code')) is not int or value['code'] != 0:
            # Undocumented OAuth error codes are not treated as evidence that a refresh was unused.
            raise HubError('OAUTH_REFRESH_RESPONSE_REJECTED', review=True)

    def _refresh(self, row):
        self._binding(row)
        data = self._open(row)
        secret = os.getenv('BILIBILI_ACCESS_KEY_SECRET','')
        if not _secret(secret):
            return {'state':'active','error_code':'BILIBILI_CREDENTIAL_AND_ACCOUNT_REQUIRED'}
        # The HTTP client performs atomic budget reservation before sending.
        if self.http is None:
            raise HubError('BILIBILI_API_NOT_CONFIGURED', review=True)
        now = time.time()
        with self.store.transaction() as db:
            db.execute('INSERT INTO bilibili_token_refreshes VALUES(?,?,?,?,\'started\',?,?,NULL)',
                       (str(uuid.uuid4()),row['account_id'],row['generation'],
                        hashlib.sha256(data['refresh_token'].encode()).hexdigest(),now,now))
            changed = db.execute('''UPDATE bilibili_credentials SET state='refreshing',updated_at=?
                WHERE account_id=? AND generation=? AND state='active' ''',
                (now,row['account_id'],row['generation'])).rowcount
            if changed != 1:
                raise HubError('OAUTH_GENERATION_CHANGED', review=True)
        try:
            value = self.http.request('bilibili','POST',REFRESH_URL,
                headers={'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},
                params={'client_id':row['app_id'],'client_secret':secret,
                        'grant_type':'refresh_token','refresh_token':data['refresh_token']},
                response_check=self._check_response)
            token = value.get('data')
            if (not isinstance(token,dict) or not all(_secret(token.get(key)) for key in ('access_token','refresh_token')) or
                    token['refresh_token'] == data['refresh_token'] or
                    type(token.get('expires_in')) is not int or
                    not time.time() + 60 < token['expires_in'] <= time.time() + 366 * 86400):
                raise HubError('OAUTH_REFRESH_INVALID_RESPONSE', review=True)
            # Persist the rotated pair BEFORE any read-only account/scope validation.
            generation = row['generation'] + 1
            sealed = self._seal(row['account_id'],row['app_id'],generation,token['access_token'],token['refresh_token'])
            with self.store.transaction() as db:
                if db.execute('SELECT 1 FROM bilibili_token_refreshes WHERE account_id=? AND refresh_fingerprint=?',
                              (row['account_id'],hashlib.sha256(token['refresh_token'].encode()).hexdigest())).fetchone():
                    raise HubError('OAUTH_REFRESH_REUSED_TOKEN', review=True)
                changed = db.execute('''UPDATE bilibili_credentials SET generation=?,encrypted_tokens=?,
                    access_expires_at=?,state='validating',error_code=NULL,validation_attempts=0,next_check_at=0,updated_at=?
                    WHERE account_id=? AND generation=? AND state='refreshing' ''',
                    (generation,sealed,token['expires_in'],time.time(),row['account_id'],row['generation'])).rowcount
                if changed != 1:
                    raise HubError('OAUTH_GENERATION_CHANGED', review=True)
                db.execute('''UPDATE bilibili_token_refreshes SET state='confirmed',updated_at=?
                    WHERE account_id=? AND generation=?''',(time.time(),row['account_id'],row['generation']))
        except HubError as exc:
            # Pre-request budget denials demonstrably sent no request. Keep an audit entry
            # and allow the still-active generation after the budget is explicitly fixed.
            if exc.code in ('API_BUDGET_EXCEEDED','PAID_SERVICE_NOT_AUTHORIZED','INVALID_BUDGET'):
                with self.store.transaction() as db:
                    db.execute('DELETE FROM bilibili_token_refreshes WHERE account_id=? AND generation=? AND state=\'started\'',
                               (row['account_id'],row['generation']))
                    db.execute('UPDATE bilibili_credentials SET state=\'active\',error_code=?,updated_at=? WHERE account_id=? AND generation=?',
                               (exc.code,time.time(),row['account_id'],row['generation']))
                    db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                               (time.time(),'scheduler','bilibili_token_refresh','bilibili',exc.code))
                return {'state':'active','error_code':exc.code}
            return self._stop(row,'refresh_unknown','OAUTH_REFRESH_UNKNOWN','unknown')
        except BaseException:
            self._stop(row,'refresh_unknown','OAUTH_REFRESH_UNKNOWN','unknown')
            raise
        return self._validate(self._row(row['account_id']))

    def tick(self, job=None):
        if not self.settings.config.get('bilibili_token_refresh_enabled',False):
            return {'enabled':False,'accounts_checked':0,'network_requests_sent':0}
        results = []
        with resource_guard(self.settings.root / 'data/locks/bilibili-oauth'):
            for row in self.store.query('SELECT * FROM bilibili_credentials ORDER BY account_id'):
                try:
                    if row['state'] == 'refreshing':
                        result = self._stop(row,'refresh_unknown','OAUTH_REFRESH_INTERRUPTED','unknown')
                    elif row['next_check_at'] > time.time():
                        result = {'state':row['state'],'backoff':True}
                    elif row['state'] == 'validating':
                        result = self._validate(row)
                    elif row['state'] != 'active':
                        result = {'state':row['state'],'error_code':row['error_code']}
                    elif row['access_expires_at'] <= time.time():
                        result = self._stop(row,'auth_expired','AUTH_EXPIRED')
                    elif row['access_expires_at'] - time.time() <= self.settings.config.get('bilibili_token_refresh_lead_seconds',900):
                        result = self._refresh(row)
                    else:
                        result = {'state':'active','refresh_due':False}
                except HubError as exc:
                    result = self._stop(row,'auth_expired',exc.code)
                results.append(result)
        return {'enabled':True,'accounts_checked':len(results),'results':results,'published':False}
