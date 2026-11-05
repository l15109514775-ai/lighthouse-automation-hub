"""Owned checks for the daily successor timeout and recovery composition."""
from pathlib import Path
from types import SimpleNamespace

from scripts.ops import cloudbase_backup_daily_retention_v2e as module


def test_transfer_timeout_floor_covers_archive_put_and_two_full_gets(monkeypatch):
    calls = []

    def call(script, args, timeout):
        calls.append((script, args, timeout))
        return {'state': 'owned'}

    daily = SimpleNamespace(call=call)
    module.extend_transfer_timeout(daily)
    daily.call('run_cloudbase_backup_sidecar_v1e.py', ['--accept'], 960)
    daily.call('run_cloudbase_backup_retention_v2c.py', ['--accept'], 1200)
    daily.call('prune_cloudbase_verified_clones_v1.py', ['--dry-run'], 300)
    assert calls == [
        ('run_cloudbase_backup_sidecar_v1e.py', ['--accept'], 3600),
        ('run_cloudbase_backup_retention_v2c.py', ['--accept'], 3600),
        ('prune_cloudbase_verified_clones_v1.py', ['--dry-run'], 300),
    ]


def test_daily_gate_requires_the_current_upload_unknown_reconciliation():
    calls = []

    class Recovery:
        Layout = staticmethod(lambda: SimpleNamespace(proof='old-proof'))

        @staticmethod
        def verify_completed(layout):
            calls.append('old')
            return {
                'state': 'remote_absent_reconciled',
                'daily_id': 'old-daily',
                'operation_id': 'old-operation',
            }

    class Current:
        Layout = staticmethod(lambda: SimpleNamespace(proof='current-proof'))

        @staticmethod
        def verify_completed(layout):
            calls.append('current')
            return {
                'state': 'remote_absent_reconciled',
                'daily_id': 'current-daily',
                'operation_id': 'current-operation',
            }

    daily = SimpleNamespace(verify_recovered_gap=None)
    gate = module.bind_recovery_gap(daily, Recovery, Current)
    result = gate(SimpleNamespace(STATE=module.DAILY_STATE))
    assert calls == ['old', 'current']
    assert result['operation_id']=='old-operation'
    assert result['current_upload_unknown_reconciliation']['operation_id']=='current-operation'
    assert daily.verify_recovered_gap is gate


def test_systemd_budget_exceeds_the_inner_transfer_budget():
    root=Path(__file__).resolve().parents[2]
    service=(root/'scripts/ops/lighthouse-cloudbase-backup.service').read_text()
    dropin=(root/'scripts/ops/lighthouse-cloudbase-backup.service.d/90-transfer-timeout.conf').read_text()
    assert 'TimeoutStartSec=30min' in service
    assert dropin=='[Service]\nTimeoutStartSec=75min\n'
    assert module.TRANSFER_TIMEOUT_SECONDS==3600
