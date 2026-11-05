import subprocess
from pathlib import Path


def test_machine_browser_rechecks_expiry_at_actual_send_control():
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node','--input-type=module','-e',r'''
import assert from 'node:assert/strict';
import {assertPublicationWindow as check} from './adapters/publication_window.mjs';
const spec={machine_policy:'machine-tweet-v1',dispatch_expires_at:100};
check(spec,99999);
check({...spec,machine_policy:'machine-tweet-v2'},99999);
assert.throws(()=>check({...spec,machine_policy:'machine-tweet-v2'},100000),/OWNER_TWO_HOUR_WINDOW_EXPIRED/);
assert.throws(()=>check(spec,100000),/OWNER_TWO_HOUR_WINDOW_EXPIRED/);
assert.throws(()=>check(spec,100001),/OWNER_TWO_HOUR_WINDOW_EXPIRED/);
assert.throws(()=>check({...spec,dispatch_expires_at:NaN},0),/WINDOW_INVALID/);
assert.throws(()=>check({...spec,machine_policy:'forged'},0),/WINDOW_INVALID/);
check({},100001); // Old genuine human-review path keeps its existing contract.
'''],cwd=root,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
