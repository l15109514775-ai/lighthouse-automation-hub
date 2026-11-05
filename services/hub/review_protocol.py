"""Shared operator signature protocol. Human decisions are never generated here."""
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re
import tomllib
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .db import encode
from .errors import HubError


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def decode_key(value,length):
    try:
        raw=base64.b64decode(value.encode('ascii'),altchars=b'-_',validate=True)
        if len(raw)!=length: raise ValueError()
        return raw
    except (AttributeError,UnicodeError,ValueError,binascii.Error):
        raise HubError('HUMAN_REVIEW_SIGNATURE_INVALID',review=True) from None


def read_review_json(file):
    file=Path(file)
    try:
        if not file.is_file() or file.stat().st_size>65536: raise ValueError()
        def unique(pairs):
            obj={}
            for key,value in pairs:
                if key in obj: raise ValueError()
                obj[key]=value
            return obj
        def invalid_constant(_): raise ValueError()
        with file.open('rb') as stream: data=stream.read(65537)
        if len(data)>65536: raise ValueError()
        value=json.loads(data.decode('utf-8-sig'),object_pairs_hook=unique,parse_constant=invalid_constant)
        if not isinstance(value,dict): raise ValueError()
        return value
    except (OSError,ValueError,UnicodeError,RecursionError):
        raise HubError('HUMAN_REVIEW_DOCUMENT_INVALID',review=True) from None


def load_reviewers(settings,scope):
    file=settings.root/'config/reviewers.toml'
    if not file.exists(): return {}
    try:
        cfg=tomllib.loads(file.read_text('utf-8-sig'))
        if scope not in ('video','tweet') or set(cfg)!={'reviewers'} or not isinstance(cfg['reviewers'],list): raise ValueError()
        result={}
        for person in cfg['reviewers']:
            if (not isinstance(person,dict) or set(person)!={'id','public_key','enabled','scopes'} or
                not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',person['id']) or person['id'] in result or
                type(person['enabled']) is not bool or not isinstance(person['scopes'],list) or
                not person['scopes'] or any(s not in ('video','tweet') for s in person['scopes']) or
                len(set(person['scopes']))!=len(person['scopes'])): raise ValueError()
            key=Ed25519PublicKey.from_public_bytes(decode_key(person['public_key'],32))
            result[person['id']]={'key':key,'enabled':person['enabled'],'scopes':person['scopes']}
        return {name:entry['key'] for name,entry in result.items() if entry['enabled'] and scope in entry['scopes']}
    except (OSError,ValueError,KeyError,TypeError,HubError):
        raise HubError('HUMAN_REVIEW_SIGNER_CONFIG_INVALID',review=True) from None


def verify_packet(value,row,*,version,checks,reviewers):
    if (set(value)!={'manifest','attestation','signature'} or value['manifest']!=json.loads(row['manifest']) or
        digest(value['manifest'])!=row['manifest_sha256']):
        raise HubError('HUMAN_REVIEW_MANIFEST_MISMATCH',review=True)
    att=value['attestation']
    if (not isinstance(att,dict) or set(att)!={'version','request_id','manifest_sha256','reviewer_id','decision','checks','notes'} or
        att['version']!=version or att['request_id']!=row['request_id'] or att['manifest_sha256']!=row['manifest_sha256'] or
        not isinstance(att['reviewer_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',att['reviewer_id']) or
        not isinstance(att['checks'],dict) or set(att['checks'])!=set(checks) or
        any(type(flag) is not bool for flag in att['checks'].values()) or
        not isinstance(att['notes'],str) or len(att['notes'])>2000):
        raise HubError('HUMAN_REVIEW_ATTESTATION_INVALID',review=True)
    if att['decision'] not in ('approve','reject','revoke'):
        raise HubError('HUMAN_REVIEW_DECISION_REQUIRED',review=True)
    key=reviewers.get(att['reviewer_id'])
    if key is None: raise HubError('HUMAN_REVIEW_SIGNER_NOT_AUTHORIZED',review=True)
    try: key.verify(decode_key(value['signature'],64),encode(att).encode('utf-8'))
    except InvalidSignature:
        raise HubError('HUMAN_REVIEW_SIGNATURE_INVALID',review=True) from None
    return att
