"""Bookkeeping for an explicitly reviewed, supervised website publication.

No browser, cookies, network sender, scheduler entry, API or MCP write endpoint.
The operator records the authenticated owner's real approval; this is distinct
from the existing cryptographically signed human-review protocol.
"""
import hashlib
import json
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .locks import resource_guard
from .model_receipts import ModelReceipts
from .tweet_reviews import TweetReviews

VERSION = 'supervised-website-acceptance-v1'
KIND = 'dynamic_website_supervised'


class SupervisedWeb:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.files = TweetReviews(settings, store)

    def _snapshot(self, job_id):
        job = self.store.get(job_id)
        if not job or job['source_platform'] != 'x' or job['job_type'] != 'tweet_process':
            raise HubError('SUPERVISED_TWEET_REQUIRED', review=True)
        try:
            review = job['result']['supervised_web']
            path = self.files._file(job_id, review['manifest_file'])
            if sha256_file(path) != review['manifest_sha256']:
                raise ValueError()
            manifest = json.loads(path.read_text('utf-8'))
            source = self.files._file(job_id, self.settings.root/'media'/job_id/'source.json')
            owner_file = self.files._file(job_id, manifest['owner_review_file'])
            owner = json.loads(owner_file.read_text('utf-8'))
            render = manifest['render']
            image = self.files._file(job_id, render['png'])
            report_file = self.files._file(job_id, render['report_file'])
            report = json.loads(report_file.read_text('utf-8'))['report']
            input_file = self.files._file(job_id, report_file.parent.parent/'compositor-input.json')
            item = json.loads(input_file.read_text('utf-8'))
            post = item['post']
            if (manifest['version'] != VERSION or manifest['job_id'] != job_id or
                    manifest['post_id'] != job['source_id'] or manifest['source_url'] != job['payload']['url'] or
                    manifest['source_sha256'] != job['result']['source_sha256'] or
                    sha256_file(source) != manifest['source_sha256'] or json.loads(source.read_text('utf-8')) != job['payload'] or
                    sha256_file(owner_file) != manifest['owner_review_sha256'] or
                    owner.get('version') != 'operator-transcribed-owner-review-v1' or
                    owner.get('approval_basis') != 'authenticated_owner_chat_transcribed_by_operator' or
                    owner.get('cryptographically_signed_by_owner') is not False or
                    owner.get('automatic_publication_authorized') is not False or
                    not isinstance(owner.get('owner_response'), str) or not owner['owner_response'].strip() or
                    owner.get('target_site_uid') != self.settings.config['target_bilibili_site_uid'] or
                    manifest['target_site_uid'] != owner['target_site_uid'] or
                    not any(x.get('job_id') == job_id and x.get('post_id') == job['source_id'] for x in owner['jobs']) or
                    render['sha256'] != review['artifact_sha256'] or sha256_file(image) != render['sha256'] or
                    report != render['report'] or not report['publishable'] or report['mode'] != 'publish' or
                    report['png']['sha256'] != render['sha256'] or
                    any(x['severity'] == 'error' for x in report['issues']) or
                    sha256_file(input_file) != render['input_sha256'] or
                    post['review'] != {'sourceConfirmed': True, 'translationConfirmed': True} or
                    post['sourceScreenshot']['reviewed'] is not True or
                    post['originalText'] != job['payload']['post']['text'] or
                    post['id'] != job['source_id'] or post['url'] != manifest['source_url'] or
                    hashlib.sha256(manifest['caption'].encode('utf-8')).hexdigest() != manifest['caption_sha256']):
                raise ValueError()
            proof = ModelReceipts(self.store).assess(job, post['translation'])
            if not (proof['proof_integrity_passed'] and proof['separate_requests_verified'] and
                    proof['qa_recommends_pass'] and self.files._grant(job, manifest['source_sha256'])['valid']):
                raise ValueError()
            # Keep original human semantics and require the real final rendering.
            return job, manifest
        except (ValueError, KeyError, TypeError, OSError):
            raise HubError('SUPERVISED_REVIEW_OR_ARTIFACT_INVALID', review=True) from None

    def reserve(self, principal, job_id, *, site_uid, artifact_sha256, caption_sha256):
        if principal != 'maintenance':
            self.store.audit(principal, 'reserve_supervised_website', job_id, 'denied')
            raise HubError('ACCESS_DENIED')
        with resource_guard(self.settings.root/'data/locks/tweet-review'):
            job, manifest = self._snapshot(job_id)
            if (site_uid != manifest['target_site_uid'] or artifact_sha256 != manifest['render']['sha256'] or
                    caption_sha256 != manifest['caption_sha256']):
                raise HubError('SUPERVISED_WEBSITE_DRAFT_MISMATCH', review=True)
            key = hashlib.sha256(encode(['bilibili', 'x', job['source_id'], 'site_uid:'+site_uid, KIND]).encode()).hexdigest()
            with self.store.transaction() as db:
                # Check across policy versions and transports, not just the current task.
                rows = db.execute("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=?",
                                  (job['source_id'],)).fetchall()
                prior=rows[0] if rows else None
                retry=False
                receipt=job['result'].get('web_nonacceptance',{})
                if (len(rows)==1 and prior['state']=='rejected' and not prior['external_id'] and
                        prior['idempotency_key']==key and receipt.get('confirmed') is True and receipt.get('consumed') is False):
                    proof=self.files._file(job_id,receipt['file'])
                    attempts=db.execute("SELECT COUNT(*) n FROM audit WHERE target=? AND action='reserve_supervised_website' AND outcome='one_click_authorized_by_owner_review'",(job_id,)).fetchone()['n']
                    retry=sha256_file(proof)==receipt.get('sha256') and attempts<self.settings.config['max_attempts']
                if prior and not retry:
                    return {'can_click_publish': False, 'state': prior['state'],
                            'idempotency_key': prior['idempotency_key'], 'external_id': prior['external_id'],
                            'reconcile_before_retry': prior['state'] != 'completed'}
                current = db.execute('SELECT state,stage,updated_at FROM jobs WHERE job_id=?', (job_id,)).fetchone()
                if (current['state'] != 'awaiting_review' or current['stage'] != 'human_reviewed_render_ready' or
                        current['updated_at'] != job['updated_at']):
                    raise HubError('SUPERVISED_JOB_CHANGED', review=True)
                now = time.time()
                if retry:
                    db.execute("UPDATE publications SET state='dispatching',updated_at=? WHERE idempotency_key=?",(now,key))
                    updated={**job['result'],'web_nonacceptance':{**receipt,'consumed':True}}
                    db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(updated),job_id))
                else:
                    db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (key, job_id, 'bilibili', 'x', job['source_id'], 'site_uid:'+site_uid,
                                KIND, 'dispatching', None, None, now))
                db.execute("UPDATE jobs SET state='publish_unknown',stage='website_dispatch_reserved',error_code='SUPERVISED_WEBSITE_RECONCILE_REQUIRED',updated_at=? WHERE job_id=?",
                           (now, job_id))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                           (now, principal, 'reserve_supervised_website', job_id, 'one_click_authorized_by_owner_review'))
            return {'can_click_publish': True, 'idempotency_key': key, 'state': 'dispatching',
                    'automatic_publication_enabled': False}

    def record_nonacceptance(self, principal, job_id, proof_file):
        """Maintenance recovery: explicit blocked UI AND a fresh read-only lookup.

        A timeout or an empty lookup alone never authorizes another send. The
        operator's interpretation is labelled as such, with immutable evidence;
        it does not impersonate an owner-signed or machine-generated receipt.
        """
        if principal!='maintenance':raise HubError('ACCESS_DENIED')
        job,manifest=self._snapshot(job_id)
        file=self.files._file(job_id,proof_file)
        try:
            proof=json.loads(file.read_text('utf-8'))
            if (proof['version']!='supervised-web-nonacceptance-v1' or proof['job_id']!=job_id or
                    proof['source_url']!=manifest['source_url'] or proof['target_site_uid']!=manifest['target_site_uid'] or
                    proof['observer_kind']!='operator_transcribed_browser_evidence' or
                    proof['basis']!='incomplete_upload_confirmation_left_unconfirmed' or
                    proof['final_confirmation_not_clicked'] is not True):raise ValueError()
            for name in ('blocking_screenshot','lookup','dispatch_implementation'):
                artifact=self.files._file(job_id,proof[name]['file'])
                if sha256_file(artifact)!=proof[name]['sha256']:raise ValueError()
            lookup=json.loads(self.files._file(job_id,proof['lookup']['file']).read_text('utf-8'))
            checked=datetime.fromisoformat(lookup['checked_at'].replace('Z','+00:00'))
            age=(datetime.now(timezone.utc)-checked).total_seconds()
            if (lookup['mode']!='lookup' or lookup['source_url']!=manifest['source_url'] or
                    lookup['actual_site_uid']!=manifest['target_site_uid'] or lookup['found'] is not False or
                    lookup['visible_source_matches']!=0 or not 0<=age<=300):raise ValueError()
        except (KeyError,ValueError,TypeError,OSError):
            raise HubError('WEB_NONACCEPTANCE_EVIDENCE_REQUIRED',review=True) from None
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM publications WHERE job_id=? AND kind=?',(job_id,KIND)).fetchone()
            if not row or row['external_id'] or row['state'] not in ('dispatching','unknown'):
                raise HubError('WEB_UNRESOLVED_DISPATCH_REQUIRED',review=True)
            current=db.execute('SELECT updated_at FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            if current['updated_at']!=job['updated_at']:raise HubError('SUPERVISED_JOB_CHANGED',review=True)
            result={**job['result'],'web_nonacceptance':{'confirmed':True,'consumed':False,
                'file':str(file),'sha256':sha256_file(file),'observer_kind':proof['observer_kind']}}
            now=time.time()
            db.execute("UPDATE publications SET state='rejected',updated_at=? WHERE idempotency_key=?",(now,row['idempotency_key']))
            db.execute("UPDATE jobs SET state='awaiting_review',stage='human_reviewed_render_ready',error_code='SUPERVISED_WEBSITE_RETRY_READY',result=?,updated_at=? WHERE job_id=?",(encode(result),now,job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,principal,'record_website_nonacceptance',job_id,'explicit_blocking_ui_and_fresh_lookup'))
        return {'state':'rejected','bounded_retry_requires_revalidation':True,'sent':False}

    def record_visible(self, principal, job_id, evidence_file):
        if principal != 'maintenance':
            raise HubError('ACCESS_DENIED')
        job, manifest = self._snapshot(job_id)
        file = self.files._file(job_id, evidence_file)
        try:
            evidence = json.loads(file.read_text('utf-8'))
            url = urlsplit(evidence['url'])
            ident = re.fullmatch(r'/([0-9]+)/*', url.path) if url.hostname == 't.bilibili.com' else (
                re.fullmatch(r'/opus/([0-9]+)/*', url.path) if url.hostname == 'www.bilibili.com' else None)
            screenshot = self.files._file(job_id, evidence['screenshot_file'])
            if (evidence['version'] != 'operator-browser-publication-observation-v1' or
                    url.scheme != 'https' or url.username or url.password or url.query or url.fragment or not ident or
                    evidence['site_uid'] != manifest['target_site_uid'] or
                    evidence['caption'] != manifest['caption'] or evidence['artifact_sha256'] != manifest['render']['sha256'] or
                    sha256_file(screenshot) != evidence['screenshot_sha256'] or
                    evidence.get('visible_in_owner_browser') is not True):
                raise ValueError()
        except (KeyError, TypeError, ValueError, OSError):
            raise HubError('SUPERVISED_VISIBLE_RESULT_INVALID', review=True) from None
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM publications WHERE job_id=? AND kind=?', (job_id, KIND)).fetchone()
            if not row or row['state'] not in ('dispatching', 'unknown', 'completed'):
                raise HubError('SUPERVISED_DISPATCH_RECORD_REQUIRED', review=True)
            if row['external_id'] and row['external_id'] != ident[1]:
                raise HubError('SUPERVISED_VISIBLE_RESULT_CHANGED', review=True)
            now = time.time()
            db.execute("UPDATE publications SET state='completed',external_id=?,external_url=?,updated_at=? WHERE idempotency_key=?",
                       (ident[1], evidence['url'], now, row['idempotency_key']))
            result = {**job['result'],
                'previous_missing_review':job['result'].get('missing_review',[]),'missing_review':[],
                'supervised_web':{**job['result']['supervised_web'],'state':'completed',
                    'external_publish_id':ident[1],'external_url':evidence['url']},
                'publication':{'state':'completed','external_id':ident[1],'url':evidence['url'],'transport':'website_ui'},
                'website_publication_evidence': {'file': str(file), 'sha256': sha256_file(file),
                'visibility': 'verified_in_logged_in_owner_browser', 'moderation': 'not_exposed_by_page'}}
            db.execute("UPDATE jobs SET state='completed',stage='website_publication_visible',error_code=NULL,external_publish_id=?,result=?,updated_at=? WHERE job_id=?",
                       (ident[1], encode(result), now, job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now, principal, 'record_supervised_website_result', job_id, 'visible_result_link_saved'))
        return {'state': 'completed', 'external_id': ident[1], 'url': evidence['url'],
                'visibility': 'verified_in_logged_in_owner_browser', 'automatic_publication_enabled': False}
