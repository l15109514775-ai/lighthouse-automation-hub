"""Compose the existing daily runner with an index hook before clone prune.

The root host adapter must supply an app-UID registrar. This module has no
Docker/shell/network implementation and does not replace existing source pins.
The old daily state machine and unknown-acceptance protection remain intact.
"""
from copy import deepcopy
from pathlib import Path

from scripts.ops import cloudbase_backup_daily_v1 as daily


def execute(previous=None,*,registrar,runner=daily.call,saver=daily.save,validator=daily.validate_report):
    latest={}

    def save(record):
        latest['record']=record
        saver(record)

    def call(script,args,timeout):
        if script=='prune_cloudbase_verified_clones_v1.py':
            record=latest.get('record')
            if not record or '--report' not in args:raise RuntimeError('ARCHIVE_REGISTRATION_CONTEXT_REQUIRED')
            reference=record['acceptance'];file=args[args.index('--report')+1]
            if file!=reference['report_file'] or validator(reference)!=reference['report_sha256']:
                raise RuntimeError('ARCHIVE_REGISTRATION_REPORT_CHANGED')
            # Register before the first dry-run/prune. After a partially
            # completed prune, verify the durable registration instead; never
            # fetch again or pretend a missing archive was reverified.
            expected=record.get('archive_source_registration_v2')
            result=registrar(deepcopy(reference),verify_only=expected is not None)
            if (result.get('version')!='archive-source-retention-v2' or result.get('state')!='archive_locations_registered' or
                    result.get('operation_id')!=reference['operation_id'] or result.get('acceptance_id')!=reference['acceptance_id'] or
                    result.get('report_sha256')!=reference['report_sha256'] or result.get('external_requests')!=0 or
                    result.get('source_files_deleted')!=0 or type(result.get('mapped_files')) is not int or result['mapped_files']<0):
                raise RuntimeError('ARCHIVE_REGISTRATION_NOT_VERIFIED')
            if expected is not None and result!=expected:raise RuntimeError('ARCHIVE_REGISTRATION_CHANGED')
            record['archive_source_registration_v2']=result;save(record)
        return runner(script,args,timeout)

    return daily.execute(previous,runner=call,saver=save,validator=validator)
