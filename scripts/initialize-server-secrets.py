"""Run once as root on the server. Print presence only, never values."""
from pathlib import Path
import base64
import os
import secrets

assert os.geteuid() == 0
directory = Path('/etc/lighthouse-hub')
directory.mkdir(mode=0o700, exist_ok=True)
directory.chmod(0o700)
file = directory / 'secrets.env'
if not file.exists():
    values = {'DRY_RUN':'true','HUB_READ_TOKEN':secrets.token_urlsafe(32),
              'HUB_MAINTAIN_TOKEN':secrets.token_urlsafe(32),
              'EVENT_ENCRYPTION_KEY':base64.urlsafe_b64encode(os.urandom(32)).decode()}
    descriptor = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(''.join(f'{key}={value}\n' for key,value in values.items()))
file.chmod(0o600)
print('SERVER_SECRETS_PRESENT; values not displayed')
