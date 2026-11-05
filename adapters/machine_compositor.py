"""Versioned machine rendering; original publish/manual fields stay untouched."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from .compositor import compositor_input
from services.hub.db import encode
from services.hub.evidence import sha256_file
from services.hub.errors import HubError


def machine_input(source,translation):
    value=compositor_input(source,translation)
    p=value['post'];p['sourceMediaCount']=0;p['sourceQuote']='none'
    p['notes']=[]
    return value


def render_machine(settings,input_data,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    file=folder/'machine-input.json';file.write_text(encode(input_data),'utf-8')
    ticket=folder/'machine-render-authorization.json'
    ticket.write_text(encode({'version':'machine-render-authorization-v1','policy':'machine-tweet-v1',
        'input_sha256':sha256_file(file),'machine_source_verified':True,'model_receipts_verified':True,'human_review':False}),'utf-8')
    result=folder/'machine-render-result.json';result.unlink(missing_ok=True)
    env={k:os.environ[k] for k in ('PATH','TEMP','TMP','TMPDIR','PLAYWRIGHT_BROWSERS_PATH') if k in os.environ}
    env.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(settings.root/'compositor/runtime/browsers'))
    try:
        proc=subprocess.run(['node',str(settings.root/'adapters/render_machine.mjs'),str(file),str(folder),
            str(folder.parent),str(ticket)],cwd=settings.root/'compositor',env=env,capture_output=True,timeout=180)
    except (OSError,subprocess.SubprocessError):raise HubError('MACHINE_COMPOSITOR_RUNTIME_UNAVAILABLE') from None
    if proc.returncode or not result.is_file():raise HubError('MACHINE_COMPOSITOR_REAL_CHECK_FAILED')
    report=json.loads(result.read_text('utf-8'));image=Path(report['png']['file'])
    if (not report['machine_publishable'] or report['human_review'] is not False or image.is_symlink() or
        not image.resolve().is_relative_to(folder.resolve()) or sha256_file(image)!=report['png']['sha256']):
        raise HubError('MACHINE_COMPOSITOR_OUTPUT_INVALID')
    return {'png':str(image),'sha256':sha256_file(image),'report_file':str(result),'report_sha256':sha256_file(result),
        'input_file':str(file),'input_sha256':sha256_file(file),'report':report}
