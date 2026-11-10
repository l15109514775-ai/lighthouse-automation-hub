"""Daily v2c entry with a durable recovery proof that permits later completed work.

The prior recovery verifier compared the entire immutable request ledger. A
later successful backup naturally changes that ledger, causing the 03:30 daily
entry to be refused even though the old operation is terminal and no operation
is unresolved. This wrapper preserves all recovery receipts and original
request/plan checks while allowing only later terminal ledger history.
"""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import importlib.util
import json
import os
import sys

OPS = Path('/usr/local/lib/lighthouse-backup-ops')
DAILY_STATE = Path('/var/lib/lighthouse-backup-ops/retention-v2b/daily-state')
SOURCES = {
    'cloudbase_backup_budget_recovery.py':
        'ea18a1bb3bb30e49c89816f94dace7b4ca74133aa67ed97d64d5880e256691ed',
    'cloudbase_backup_daily_retention_v2c.py':
        'fe6642eea6ddeeb1a829a0bf75e62a012712a827f2174b9d118c4908d321587f',
    'recover_cloudbase_remote_absent_v1.py':
        'ce0524dddda6c516c4ba09a517faf73553e51d9f65fb3cd3273a3b6822dac830',
    'reconcile_cloudbase_upload_unknown_v1.py':
        'e246ec31041f11a3654a993e56d26786d677f853b7d411ca18ca4a00870e8926',
}
SIDECAR_SHA256 = '22e936ffd5cb03b34d76ec0dc9173a733a8184ab9e26aeb6d6bc90b26dd6a454'
RETENTION_SHA256 = '2e1ab7807093da8184908ce34ad15e5ad621beedb274a90eadfe07856492200f'
ACCEPT_SHA256 = '1508b1a70777806b30ec7b8eeb1c3ced9a2bcd8053199c11a36941979ec74563'
TRANSFER_TIMEOUT_SECONDS = 3600


def protected(file):
    file = Path(file)
    assert file.is_absolute() and file.resolve() == file
    assert not any(path.is_symlink() for path in (file, *file.parents))
    for path in (file, *file.parents):
        assert path.stat().st_uid == 0 and not path.stat().st_mode & 0o022
    assert file.is_file() and 0 < file.stat().st_size <= 262144


def load_module(filename):
    file = OPS / filename
    protected(file)
    assert hashlib.sha256(file.read_bytes()).hexdigest() == SOURCES[filename]
    spec = importlib.util.spec_from_file_location(
        'lighthouse_pinned_' + file.stem, file
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bind_recovery_gap(daily, recovery, current):
    def verify_recovered_gap(context):
        assert context.STATE == DAILY_STATE
        layout = recovery.Layout()
        try:
            prior = recovery.verify_completed(layout)
        except ValueError as exc:
            assert str(exc) == 'ABSENT_TERMINAL_LEDGER_CHANGED'
        else:
            current_receipt = current.verify_completed(current.Layout())
            assert current_receipt['state'] == 'remote_absent_reconciled'
            return {**prior, 'current_upload_unknown_reconciliation': current_receipt}

        intent = json.loads(recovery.read(layout.proof / 'intent.json'))
        observed = intent['plan']
        digest = recovery.sha(recovery.wire(observed))
        assert intent == {
            'version': recovery.VERSION,
            'state': 'recovery_intent_saved',
            'plan_sha256': digest,
            'plan': observed,
        }
        assert (
            observed['version'] == recovery.VERSION
            and observed['operation_id'] == recovery.OPERATION
            and observed['daily_id'] == recovery.DAILY
            and observed['evidence'] == recovery.evidence(layout)
        )
        original = json.loads(recovery.read(layout.proof / 'original-operation.json'))
        assert original == observed['evidence']['baseline']
        transition_raw = recovery.read(layout.proof / 'ledger-transition.json')
        resolved = json.loads(transition_raw)
        assert (
            resolved['version'] == recovery.VERSION
            and resolved['operation_id'] == recovery.OPERATION
            and resolved['state'] == recovery.TERMINAL
            and resolved['operation']['state'] == recovery.TERMINAL
            and resolved['operation']['error_code'] == recovery.ERROR
            and resolved['ledger_unchanged'] == observed['ledger_unchanged']
            and resolved['refund_bytes'] == resolved['external_requests'] == 0
        )

        # The recovery ledger view still enforces that the original requests,
        # plan and terminal fields are unchanged and that no later operation is
        # preparing/uploading/unknown. It intentionally does not require the
        # whole history hash to equal the pre-successor snapshot.
        live = recovery.ledger_snapshot(layout, observed['evidence'], True)
        current_operation = live['operation']
        baseline = observed['evidence']['baseline']['operation']
        stable = ('operation_id', 'created_at', 'policy_sha256')
        assert (
            live['terminal']
            and current_operation == resolved['operation']
            and current_operation['state'] == recovery.TERMINAL
            and current_operation['error_code'] == recovery.ERROR
            and all(current_operation[key] == baseline[key] for key in stable)
        )

        receipt = json.loads(recovery.read(layout.proof / 'receipt.json'))
        expected = {
            'version': recovery.VERSION,
            'state': 'remote_absent_reconciled',
            'operation_id': recovery.OPERATION,
            'daily_id': recovery.DAILY,
            'plan_sha256': digest,
            'head_receipt': observed['evidence']['head_receipt'],
            'ledger_transition_sha256': recovery.sha(transition_raw),
            'failed_latest_sha256': recovery.DAILY_SHA,
            'history_sha256': recovery.DAILY_SHA,
            'ledger_terminal_state': recovery.TERMINAL,
            'active_latest_absent': True,
            'history_preserved': True,
            'original_operation_preserved': True,
            'no_refund': True,
            'puts': 0,
            'gets': 0,
            'deletions': 0,
            'external_requests': 0,
            'retry_performed': False,
            'backup_success_claimed': False,
        }
        assert receipt == expected
        assert not layout.latest.exists()
        assert recovery.sha(recovery.read(layout.archive)) == recovery.DAILY_SHA
        current_receipt = current.verify_completed(current.Layout())
        assert current_receipt['state'] == 'remote_absent_reconciled'
        return {**receipt, 'current_upload_unknown_reconciliation': current_receipt}

    daily.verify_recovered_gap = verify_recovered_gap
    return verify_recovered_gap


def extend_transfer_timeout(module):
    original = module.call

    def call(script, args, timeout):
        if script in (
            'run_cloudbase_backup_sidecar_v1e.py',
            'run_cloudbase_backup_retention_v2c.py',
        ):
            timeout = max(timeout, TRANSFER_TIMEOUT_SECONDS)
        return original(script, args, timeout)

    module.call = call
    return call


def bind_transfer_timeout(host=OPS / 'retention-v2c/host'):
    sys.path.insert(0, str(host))
    from scripts.ops import cloudbase_backup_daily_v1

    extend_transfer_timeout(cloudbase_backup_daily_v1)
    return cloudbase_backup_daily_v1


def main():
    assert os.name == 'posix' and os.geteuid() == 0
    os.umask(0o077)
    daily = load_module('cloudbase_backup_daily_retention_v2c.py')
    recovery = load_module('recover_cloudbase_remote_absent_v1.py')
    current = load_module('reconcile_cloudbase_upload_unknown_v1.py')
    pins = dict(daily.PINS)
    pins['run_cloudbase_backup_sidecar_v1e.py'] = SIDECAR_SHA256
    pins['run_cloudbase_backup_retention_v2c.py'] = RETENTION_SHA256
    pins['accept_cloudbase_backup_capture_v1.py'] = ACCEPT_SHA256
    daily.PINS = pins
    core = bind_transfer_timeout()
    budget = load_module('cloudbase_backup_budget_recovery.py')
    budget.bind(core)
    verify = bind_recovery_gap(daily, recovery, current)
    if sys.argv[1:] == ['--verify-budget']:
        previous, _ = core.read_record(DAILY_STATE / 'latest.json', DAILY_STATE, root_owned=True)
        daily.validate_previous(previous)
        print(json.dumps(budget.inspect(core, core.ROOT, DAILY_STATE, previous)))
        return 0
    if sys.argv[1:] == ['--verify-recovery']:
        value = verify(SimpleNamespace(STATE=DAILY_STATE))
        print(json.dumps({
            'state': 'verified_recovery_with_later_terminal_history',
            'version': 'cloudbase-daily-recovery-successor-v2e',
            'daily_id': value['daily_id'],
            'operation_id': value['operation_id'],
            'external_requests': 0,
            'backup_started': False,
            'failure_history_preserved': True,
            'budget_refunded': False,
        }))
        return 0
    try:
        return daily.main()
    except budget.BudgetDeferred as exc:
        print(json.dumps(exc.report))
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({
            'state': 'stopped',
            'error_code': 'DAILY_RECOVERY_SUCCESSOR_REFUSED',
            'backup_started': False,
        }))
        raise SystemExit(2) from None
