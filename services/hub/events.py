from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from cryptography.fernet import Fernet, InvalidToken
from adapters.safe_https import post_json, validate_destination
from .db import encode
from .errors import HubError

NAMES = ('task.failed','auth.expired','review.required','disk.low','pipeline.stalled')


def headers(secret, event_id, body, subscription_id, timestamp=None):
    try:
        key = base64.b64decode(secret.removeprefix('whsec_'), validate=True)
    except (ValueError, TypeError):
        raise HubError('INVALID_EVENT_SIGNING_KEY') from None
    if not secret.startswith('whsec_') or not 24 <= len(key) <= 64:
        raise HubError('INVALID_EVENT_SIGNING_KEY')
    stamp = str(int(timestamp if timestamp is not None else time.time()))
    signature = base64.b64encode(hmac.new(key, event_id.encode() + b'.' + stamp.encode() + b'.' + body, hashlib.sha256).digest()).decode()
    return {'Content-Type':'application/json','webhook-id':event_id,'webhook-timestamp':stamp,
            'webhook-signature':'v1,' + signature,'X-MCP-Subscription-Id':subscription_id}


class Events:
    def __init__(self, settings, store, transport=post_json):
        self.settings, self.store, self.transport = settings, store, transport

    def cipher(self):
        value = os.getenv('EVENT_ENCRYPTION_KEY')
        if not value:
            raise HubError('EVENT_ENCRYPTION_KEY_REQUIRED')
        try:
            return Fernet(value.encode())
        except ValueError:
            raise HubError('EVENT_ENCRYPTION_KEY_INVALID') from None

    def catalog(self):
        return {'events':[{'name':name,'description':'Lighthouse operational event: ' + name,
            'inputSchema':{'type':'object','properties':{'queue':{'enum':['tweet','video','system']}},'additionalProperties':False},
            'payloadSchema':{'type':'object','additionalProperties':True}} for name in NAMES]}

    def subscribe(self, owner, name, arguments, delivery, *, ttl_ms=86400000, cursor=None):
        if name not in NAMES or not isinstance(arguments, dict) or set(arguments) - {'queue'}:
            raise HubError('INVALID_EVENT_FILTER')
        if 'queue' in arguments and arguments['queue'] not in ('tweet','video','system'):
            raise HubError('INVALID_EVENT_FILTER')
        if cursor is not None and (not str(cursor).isdigit() or int(cursor) < 0):
            raise HubError('INVALID_EVENT_CURSOR')
        if ttl_ms is None:
            ttl_ms = 86400000  # This server always grants finite subscriptions.
        if not isinstance(ttl_ms, int) or not 60000 <= ttl_ms <= 7 * 86400000:
            raise HubError('INVALID_EVENT_TTL')
        if delivery.get('mode') != 'webhook':
            raise HubError('INVALID_EVENT_DELIVERY')
        callback, secret = delivery.get('url',''), delivery.get('secret','')
        hosts = self.settings.config['event_callback_hosts']
        validate_destination(callback, hosts)
        ident = 'sub_' + hashlib.sha256(encode([owner,callback,name,arguments]).encode()).hexdigest()[:40]
        challenge = secrets.token_urlsafe(32)
        body = encode({'type':'verification','challenge':challenge}).encode()
        signature = headers(secret, 'verify_' + secrets.token_hex(16), body, ident)
        started = time.time()
        try:
            status, response = self.transport(callback, hosts, signature, body)
            echoed = json.loads(response).get('challenge', '')
            if not 200 <= status < 300 or time.time() - started > 30 or not isinstance(echoed, str) or not hmac.compare_digest(challenge, echoed):
                raise HubError('CALLBACK_CHALLENGE_FAILED')
        except HubError:
            raise
        except Exception:
            raise HubError('CALLBACK_VERIFICATION_FAILED') from None
        encrypted = self.cipher().encrypt(secret.encode()).decode()
        expiry = time.time() + ttl_ms / 1000
        with self.store.transaction() as db:
            previous = db.execute('SELECT cursor FROM subscriptions WHERE id=?', (ident,)).fetchone()
            # Resume previous accepted cursor. Fresh subscriptions start at the current event boundary unless a cursor is supplied.
            last = int(cursor) if cursor is not None else previous['cursor'] if previous else db.execute('SELECT COALESCE(MAX(seq),0) n FROM events').fetchone()['n']
            db.execute('''INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET
                 encrypted_secret=excluded.encrypted_secret,expires_at=excluded.expires_at,active=1,cursor=excluded.cursor''',
                       (ident,owner,name,encode(arguments),callback,encrypted,expiry,last))
        return {'id':ident,'refreshBefore':datetime.fromtimestamp(expiry,timezone.utc).isoformat(),
                'cursor':str(last),'truncated':False}

    def unsubscribe(self, owner, ident):
        with self.store.transaction() as db:
            changed = db.execute('UPDATE subscriptions SET active=0 WHERE id=? AND owner=?', (ident,owner)).rowcount
        return {'unsubscribed':bool(changed)}

    def dispatch(self):
        now = time.time()
        subscriptions = self.store.query('SELECT * FROM subscriptions WHERE active=1 AND expires_at>?', (now,))
        count = 0
        for sub in subscriptions:
            arguments = json.loads(sub['arguments'])
            # Never advance the accepted cursor beyond an undelivered matching event.
            rows = self.store.query('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 100', (sub['cursor'],))
            for event in rows:
                data = json.loads(event['data'])
                matches = event['name'] == sub['name'] and all(data.get(k) == v for k,v in arguments.items())
                if not matches:
                    with self.store.transaction() as db:
                        db.execute('UPDATE subscriptions SET cursor=? WHERE id=?', (event['seq'],sub['id']))
                    continue
                with self.store.transaction() as db:
                    db.execute('INSERT OR IGNORE INTO deliveries(subscription_id,event_seq) VALUES(?,?)', (sub['id'],event['seq']))
                    delivery = dict(db.execute('SELECT * FROM deliveries WHERE subscription_id=? AND event_seq=?', (sub['id'],event['seq'])).fetchone())
                    if delivery['state'] == 'failed' or delivery['next_due'] > now:
                        break
                    db.execute('UPDATE deliveries SET attempts=attempts+1 WHERE subscription_id=? AND event_seq=?', (sub['id'],event['seq']))
                payload = {'eventId':event['event_id'],'name':event['name'],
                           'timestamp':datetime.fromtimestamp(event['occurred_at'],timezone.utc).isoformat(),
                           'data':data,'cursor':str(event['seq'])}
                body = encode(payload).encode()
                if len(body) > 262144:
                    status = 413
                else:
                    try:
                        secret = self.cipher().decrypt(sub['encrypted_secret'].encode()).decode()
                        signature = headers(secret,event['event_id'],body,sub['id'])
                        status, _ = self.transport(sub['callback'],self.settings.config['event_callback_hosts'],signature,body)
                    except Exception:
                        status = 0
                with self.store.transaction() as db:
                    accepted = 200 <= status < 300
                    exhausted = delivery['attempts'] + 1 >= 5 or status in (410,413)
                    state = 'delivered' if accepted else 'failed' if exhausted else 'pending'
                    db.execute('UPDATE deliveries SET state=?,next_due=?,error_code=? WHERE subscription_id=? AND event_seq=?',
                        (state,now + min(3600,15 * 2 ** delivery['attempts']),None if accepted else f'HTTP_{status}',sub['id'],event['seq']))
                    if accepted:
                        db.execute('UPDATE subscriptions SET cursor=? WHERE id=?', (event['seq'],sub['id']))
                    if status == 410:
                        db.execute('UPDATE subscriptions SET active=0 WHERE id=?', (sub['id'],))
                count += 1
                if not accepted:
                    break
        return {'delivery_attempts':count}
