"""Merge only authorized model settings into root-owned server secrets; never print values."""
from pathlib import Path
import os
import sys
import tempfile

assert os.geteuid()==0
source=Path(sys.argv[1])
allowed={'MODEL_API_KEY','MODEL_BASE_URL','MODEL_ALLOWED_HOSTS','MODEL_API_STYLE','MODEL_NAME','QA_MODEL_NAME',
         'MODEL_MAX_OUTPUT_TOKENS','BUDGET_MODEL_UNLIMITED','BUDGET_MODEL_PAID_AUTHORIZED',
         'OPENAI_API_KEY','OPENAI_MODEL','OPENAI_QA_MODEL','MODEL_INPUT_USD_PER_MILLION','MODEL_OUTPUT_USD_PER_MILLION'}
def read(file):
    return dict(line.split('=',1) for line in file.read_text('utf-8-sig').splitlines() if line and not line.startswith('#') and '=' in line)
changes=read(source)
assert changes and set(changes)<=allowed
target=Path('/etc/lighthouse-hub/secrets.env')
settings=read(target)
settings.update(changes)
descriptor,name=tempfile.mkstemp(prefix='secrets-',suffix='.env',dir=target.parent)
os.fchmod(descriptor,0o600)
with os.fdopen(descriptor,'w') as stream:
    stream.write(''.join(f'{key}={value}\n' for key,value in settings.items()))
    stream.flush()
    os.fsync(stream.fileno())
os.replace(name,target)
source.unlink()
print('MODEL_SETTINGS_INSTALLED: values suppressed; X/B站 billing settings not changed')
