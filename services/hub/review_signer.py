"""Local, interactive operator tool. Not loaded by workers, API or MCP."""
import argparse
import base64
import csv
import getpass
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .db import encode
from .errors import HubError
from .video_reviews import CHECKS, VERSION, digest, read_review_json
from .tweet_reviews import CHECKS as TWEET_CHECKS, VERSION as TWEET_VERSION


def interactive():
    if not sys.stdin.isatty(): raise HubError('INTERACTIVE_HUMAN_SIGNING_REQUIRED')
    if os.getenv('HUB_ROOT')=='/opt/lighthouse-hub': raise HubError('RUN_REVIEW_SIGNER_ON_OPERATOR_COMPUTER')


def public_key(key):
    return base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode('ascii')


def create_key(file):
    interactive()
    file=Path(file).resolve()
    repository=Path(__file__).resolve().parents[2]
    if file.is_relative_to(repository): raise HubError('REVIEW_PRIVATE_KEY_MUST_BE_OUTSIDE_REPOSITORY')
    password=getpass.getpass('New review-key passphrase (at least 12 characters): ')
    if len(password)<12 or password!=getpass.getpass('Repeat passphrase: '):
        raise HubError('REVIEW_KEY_PASSPHRASE_REQUIREMENT')
    key=Ed25519PrivateKey.generate()
    encrypted=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(password.encode('utf-8')))
    file.parent.mkdir(parents=True,exist_ok=True)
    try:
        descriptor=os.open(file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'wb') as stream: stream.write(encrypted)
    except FileExistsError:
        raise HubError('REVIEW_PRIVATE_KEY_ALREADY_EXISTS') from None
    if os.name=='nt':
        identity=subprocess.check_output(['whoami','/user','/fo','csv','/nh'],text=True)
        sid=next(csv.reader(io.StringIO(identity.strip())))[1]
        if not re.fullmatch(r'S-1-[0-9-]+',sid): raise HubError('REVIEW_KEY_ACL_CONFIGURATION_REQUIRED')
        completed=subprocess.run(['icacls',str(file),'/inheritance:r','/grant:r','*'+sid+':(F)'],capture_output=True)
        if completed.returncode: raise HubError('REVIEW_KEY_ACL_CONFIGURATION_REQUIRED')
    return {'private_key_file':str(file),'public_key':public_key(key),'encrypted':True}


def load_key(file):
    interactive()
    try:
        with Path(file).open('rb') as stream: pem=stream.read(16385)
        if len(pem)>16384 or not pem.startswith(b'-----BEGIN ENCRYPTED PRIVATE KEY-----'):
            raise HubError('ENCRYPTED_ED25519_REVIEW_KEY_REQUIRED')
        password=getpass.getpass('Review-key passphrase (hidden): ')
        key=serialization.load_pem_private_key(pem,password=password.encode('utf-8'))
        if not isinstance(key,Ed25519PrivateKey): raise HubError('ENCRYPTED_ED25519_REVIEW_KEY_REQUIRED')
        return key
    except (OSError,ValueError,TypeError):
        raise HubError('REVIEW_PRIVATE_KEY_COULD_NOT_BE_OPENED') from None


def sign(file,key_file,out):
    interactive()
    packet=read_review_json(file)
    if set(packet)!={'manifest','attestation','signature'} or not isinstance(packet.get('attestation'),dict):
        raise HubError('HUMAN_REVIEW_DOCUMENT_INVALID')
    att=packet['attestation']
    version=att.get('version')
    checks={VERSION:CHECKS,TWEET_VERSION:TWEET_CHECKS}.get(version) if isinstance(version,str) else None
    if (checks is None or att.get('manifest_sha256')!=digest(packet['manifest']) or
        not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',str(att.get('reviewer_id',''))) or
        not isinstance(att.get('checks'),dict) or set(att['checks'])!=set(checks) or
        any(type(flag) is not bool for flag in att['checks'].values()) or
        att.get('decision') not in ('approve','reject','revoke')):
        raise HubError('HUMAN_REVIEW_ATTESTATION_INVALID')
    if att['decision']=='approve' and not all(att['checks'].values()):
        raise HubError('HUMAN_REVIEW_CHECKS_INCOMPLETE')
    print(json.dumps({'manifest':packet['manifest'],'decision':att['decision'],'reviewer_id':att['reviewer_id']},ensure_ascii=False,indent=2))
    expected=att['decision'].upper()+' '+att['request_id']
    if input('After your own review, type exactly "'+expected+'": ')!=expected:
        raise HubError('HUMAN_SIGNING_CANCELLED')
    key=load_key(key_file)
    packet['signature']=base64.urlsafe_b64encode(key.sign(encode(att).encode('utf-8'))).decode('ascii')
    output=Path(out).resolve()
    try:
        with output.open('x',encoding='utf-8') as stream: json.dump(packet,stream,ensure_ascii=False,indent=2)
    except FileExistsError:
        raise HubError('SIGNED_REVIEW_OUTPUT_ALREADY_EXISTS') from None
    return {'file':str(output),'decision':att['decision'],'published':False}


def main():
    parser=argparse.ArgumentParser(description='Interactive local signing; never use a worker or model to attest to human review.')
    parser.add_argument('command',choices=['create-key','sign','public-key'])
    parser.add_argument('--key',required=True)
    parser.add_argument('--input')
    parser.add_argument('--out')
    args=parser.parse_args()
    try:
        if args.command=='create-key': result=create_key(args.key)
        elif args.command=='public-key': result={'public_key':public_key(load_key(args.key))}
        else:
            if not args.input or not args.out: parser.error('--input and --out are required')
            result=sign(args.input,args.key,args.out)
        print(json.dumps(result,ensure_ascii=False))
    except HubError as exc:
        print(json.dumps({'error_code':exc.code,'published':False}))
        raise SystemExit(2) from None


if __name__=='__main__': main()
