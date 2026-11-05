from pathlib import Path
import hashlib
import time
from .db import encode
from .errors import HubError, PublishRejected, PublishUnknown
from .evidence import sha256_file


class Publishing:
    def __init__(self, settings, store, adapter):
        self.settings, self.store, self.adapter = settings, store, adapter

    def submit(self, job, artifact, account_id, kind):
        if self.settings.dry_run:
            return {'state':'dry_run','sent':False}
        cfg = self.settings.config
        if not cfg['auto_publish'] or not cfg['first_publish_accepted']:
            raise HubError('FIRST_PUBLISH_ACCEPTANCE_REQUIRED', review=True)
        if account_id not in cfg['authorized_bilibili_accounts']:
            raise HubError('TARGET_ACCOUNT_NOT_AUTHORIZED', review=True)
        file = Path(artifact['file']).resolve()
        if not file.is_relative_to((self.settings.root / 'media').resolve()) or not file.is_file():
            raise HubError('PUBLISH_ARTIFACT_PATH_INVALID', review=True)
        actual = sha256_file(file)
        review = self.store.query('SELECT * FROM publication_reviews WHERE job_id=?', (job['job_id'],))
        if (not review or review[0]['review_kind'] != 'human' or not review[0]['reviewer'] or not review[0]['rights_evidence']
                or review[0]['account_id'] != account_id or review[0]['artifact_sha256'] != actual
                or (job['result'].get('rights_evidence') and review[0]['rights_evidence']!=job['result']['rights_evidence'])
                or artifact.get('sha256') != actual or review[0]['source_sha256'] != job['result'].get('source_sha256')):
            raise HubError('PUBLISH_REVIEW_EVIDENCE_REQUIRED', review=True)
        def revalidate():
            if kind=='video':
                from .video_reviews import VideoReviews
                current=self.store.get(job['job_id'])
                if job.get('lease_owner') and (current['state']!='processing' or current.get('lease_owner')!=job['lease_owner']):
                    raise HubError('PUBLICATION_LEASE_LOST',review=True)
                VideoReviews(self.settings,self.store).verify_approval(current,artifact,account_id)
        revalidate()
        if self.adapter.account_identity() != account_id:
            raise HubError('TARGET_ACCOUNT_IDENTITY_MISMATCH', review=True)
        return self._dispatch(job, artifact, account_id, kind, revalidate=revalidate)

    def _dispatch(self, job, artifact, account_id, kind, *, revalidate=None):
        # Internal transport state machine; never exposed as an API or MCP tool.
        # Tests exercise this with an in-memory fake platform, without fabricating human reviews.
        key = hashlib.sha256(encode(['bilibili',job['source_platform'],job['source_id'],account_id,kind]).encode()).hexdigest()
        known=self.store.query('SELECT state FROM publications WHERE idempotency_key=?',(key,))
        if known and known[0]['state']!='rejected':
            return self.reconcile(key)
        prepare=getattr(self.adapter,'prepare',None)
        if prepare is not None:
            prepare(key,artifact)
        # A long upload cannot outlive a revoked review or changed source/metadata.
        if revalidate is not None:
            revalidate()
        with self.store.transaction() as db:
            previous = db.execute('SELECT * FROM publications WHERE idempotency_key=?', (key,)).fetchone()
            if not previous:
                db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (key,job['job_id'],'bilibili',job['source_platform'],job['source_id'],account_id,kind,
                            'dispatching',None,None,time.time()))
            elif previous['state']=='rejected' and not previous['external_id']:
                # Only explicit proof of non-acceptance permits a fresh dispatch attempt.
                db.execute("UPDATE publications SET state='dispatching',updated_at=? WHERE idempotency_key=?",(time.time(),key))
                previous=None
        if previous:
            return self.reconcile(key)
        try:
            reviewed_send=getattr(self.adapter,'send_reviewed',None)
            response = reviewed_send(key,artifact,revalidate) if reviewed_send and revalidate else self.adapter.send(key, artifact)
        except PublishRejected:
            with self.store.transaction() as db:
                db.execute("UPDATE publications SET state='rejected',updated_at=? WHERE idempotency_key=?",(time.time(),key))
            raise
        except BaseException:
            with self.store.transaction() as db:
                db.execute("UPDATE publications SET state='unknown',updated_at=? WHERE idempotency_key=?", (time.time(), key))
            raise PublishUnknown() from None
        if response.get('state') not in ('awaiting_moderation','completed','upload_pending') or not response.get('external_id'):
            with self.store.transaction() as db:
                db.execute("UPDATE publications SET state='unknown',updated_at=? WHERE idempotency_key=?",(time.time(),key))
            raise PublishUnknown()
        with self.store.transaction() as db:
            db.execute('UPDATE publications SET state=?,external_id=?,external_url=?,updated_at=? WHERE idempotency_key=?',
                       (response['state'],response['external_id'],response.get('url'),time.time(),key))
        return response

    def reconcile(self, key):
        rows = self.store.query('SELECT * FROM publications WHERE idempotency_key=?', (key,))
        if not rows:
            raise HubError('PUBLICATION_NOT_FOUND', review=True)
        previous = rows[0]
        if previous['state'] == 'completed':
            return {'state':'completed','external_id':previous['external_id'],'url':previous['external_url']}
        try:
            response = self.adapter.lookup(key, previous['external_id'])
        except HubError:
            # Keep auth, budget and rate-limit evidence while the publication record stays unresolved.
            raise
        except Exception:
            raise PublishUnknown() from None
        if response.get('state') not in ('completed','awaiting_moderation','upload_pending','failed') or not response.get('external_id'):
            # Even not-found is not proof of absence after an asynchronous timeout.
            raise PublishUnknown()
        if response['state']=='failed' and response.get('error_code')!='BILIBILI_MODERATION_REJECTED':
            raise PublishUnknown()
        with self.store.transaction() as db:
            db.execute('UPDATE publications SET state=?,external_id=?,external_url=?,updated_at=? WHERE idempotency_key=?',
                       (response['state'],response['external_id'],response.get('url'),time.time(),key))
        return response
