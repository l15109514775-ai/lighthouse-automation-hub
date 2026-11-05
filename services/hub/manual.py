from datetime import datetime
from pathlib import Path
import hashlib
import json
import re
import shutil
import zipfile
from .db import encode
from .errors import HubError


def import_tweet(settings, store, input_file):
    data = json.loads(Path(input_file).read_text('utf-8-sig'))
    post = data.get('post', {})
    author = data.get('author', {})
    allowed = {source['x_handle'] for source in settings.config['sources']}
    if author.get('username') not in allowed or not re.fullmatch(r'[0-9]{1,30}', str(post.get('id', ''))):
        raise HubError('MANUAL_SOURCE_IDENTITY_OR_ID_INVALID')
    if not isinstance(post.get('text'), str) or not post['text'].strip() or not author.get('name'):
        raise HubError('MANUAL_SOURCE_TEXT_REQUIRED')
    try:
        published = datetime.fromisoformat(post['created_at'].replace('Z','+00:00'))
        if published.tzinfo is None:
            raise ValueError('timezone missing')
    except (KeyError, ValueError, TypeError):
        raise HubError('MANUAL_SOURCE_TIMESTAMP_REQUIRED') from None
    count = data.get('source_media_count')
    if count is not None and (not isinstance(count,int) or not 0 <= count <= 4):
        raise HubError('MANUAL_MEDIA_COUNT_INVALID')
    source = {'post':{'id':str(post['id']),'text':post['text'],'created_at':post['created_at'],
                      'referenced_tweets':post.get('referenced_tweets',[])},
              'author':{'name':author['name'],'username':author['username']},
              'source_api':'manual-input','source_media_count':count,
              'references_observed':data.get('references_observed') is True,
              'url':f"https://x.com/{author['username']}/status/{post['id']}",
              'intake_claim':'user supplied source; not independently retrieved from X',
              'manual_input_sha256':hashlib.sha256(encode(data).encode()).hexdigest()}
    # Any supplied model/human review flags are deliberately not imported.
    return store.enqueue('x',post['id'],'tweet_process','tweet',source,settings.config['policy_version'])


def export_review_bundle(settings, store, job_id):
    job = store.get(job_id)
    if not job or job['state'] != 'awaiting_review' or job['stage'] != 'rendered_awaiting_human':
        raise HubError('RENDERED_REVIEW_DRAFT_REQUIRED')
    folder = (settings.root / 'media' / job_id).resolve()
    if not folder.is_relative_to((settings.root / 'media').resolve()):
        raise HubError('INVALID_JOB_DIRECTORY')
    # Exports remain review drafts; no B站 account/API/login is contacted.
    file = folder / 'MANUAL_REVIEW_DRAFT.zip'
    with zipfile.ZipFile(file,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in folder.rglob('*'):
            if path == file or not path.is_file() or path.is_symlink() or path.suffix not in ('.png','.json','.txt'):
                continue
            if path.resolve().is_relative_to(folder):
                archive.write(path,path.relative_to(folder))
        archive.writestr('READ_ME_FIRST.txt','人工审核草稿：来源截图、素材完整性和译文待复核。此包不是已通过审核的成品，未调用B站API，未发布。\n')
    store.audit('local-operator','export_review_bundle',job_id,'draft_only')
    return {'file':str(file),'published':False,'human_review_required':True}
