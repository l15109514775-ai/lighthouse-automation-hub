"""Local operator/timer entry; disabled without explicit private configuration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tomllib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from adapters.cloudbase_cos_backup import CloudBaseCOSConfig
from services.hub.cloudbase_backup_runtime import BackupLedger, BackupLimits, CloudBaseBackupRuntime, LEDGER_RELATIVE, ListRates
from services.hub.errors import HubError


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=os.getenv('HUB_ROOT','/opt/lighthouse-hub'))
    parser.add_argument('--config')
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--run-once',action='store_true')
    mode.add_argument('--prepare-only',action='store_true')
    mode.add_argument('--status',action='store_true')
    args=parser.parse_args();root=Path(args.root).resolve();file=Path(args.config) if args.config else root/'config/cloudbase-backup.toml'
    if not file.exists():
        print(json.dumps({'enabled':False,'configured':False,'remote_verified':False,'error_code':'CLOUDBASE_BACKUP_CONFIG_MISSING'}))
        return 0 if args.status else 2
    if file.is_symlink() or any(p.is_symlink() for p in file.parents) or not file.is_file() or file.stat().st_size>65536:
        raise HubError('CLOUDBASE_BACKUP_CONFIG_INVALID')
    if os.name=='posix' and file.stat().st_mode & 0o022: raise HubError('CLOUDBASE_BACKUP_CONFIG_NOT_PRIVATE')
    raw=file.read_bytes();value=tomllib.loads(raw.decode('utf-8-sig'));options=dict(value.get('cloudbase',{}))
    canary=options.pop('private_canary_reference','');canary_sha256=options.pop('private_canary_sha256','')
    secret_file=options.pop('credential_file','');options['storage_hosts']=tuple(options.get('storage_hosts',[]))
    config=CloudBaseCOSConfig(**options);limits=BackupLimits(**value.get('limits',{}));rates=ListRates(**value.get('list_rates',{}))
    if args.status:
        path=root/LEDGER_RELATIVE
        result={'enabled':config.enabled,'configured':True,'private_storage_verified':config.private_storage_verified,
                'ledger':BackupLedger(path,limits,list_rates=rates).status() if path.exists() else None,'remote_reverified_by_status':False}
    else:
        def credentials():
            # Dedicated child identity only; never fall back to main-account or
            # unrelated model/X/Bilibili environment credentials.
            if secret_file:
                path=Path(secret_file)
                if (not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size>16384 or
                    (os.name=='posix' and path.stat().st_mode & 0o007)):
                    raise HubError('CLOUDBASE_BACKUP_CREDENTIAL_UNAVAILABLE')
                value=json.loads(path.read_bytes())
                if not isinstance(value,dict) or set(value)-{'SecretId','SecretKey','Token','ExpirationEpoch'}:
                    raise HubError('CLOUDBASE_BACKUP_CREDENTIAL_INVALID')
                return value
            result={'SecretId':os.getenv('CLOUDBASE_COS_SECRET_ID',''),'SecretKey':os.getenv('CLOUDBASE_COS_SECRET_KEY','')}
            token=os.getenv('CLOUDBASE_COS_SESSION_TOKEN')
            expiry=os.getenv('CLOUDBASE_COS_EXPIRATION_EPOCH')
            if token:result['Token']=token
            if expiry:result['ExpirationEpoch']=float(expiry)
            return result
        runtime=CloudBaseBackupRuntime(root,config,limits,private_canary_reference=canary,
            private_canary_sha256=canary_sha256,policy_sha256=hashlib.sha256(raw).hexdigest(),credentials=credentials,list_rates=rates)
        result=runtime.run_once(prepare_only=args.prepare_only)
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    try: raise SystemExit(main())
    except HubError as exc:
        print(json.dumps({'error_code':exc.code,'remote_verified':False,'local_deletion_performed':False}));raise SystemExit(2) from None
    except (ValueError,TypeError,OSError):
        print(json.dumps({'error_code':'CLOUDBASE_BACKUP_CONFIGURATION_OR_IO_ERROR','remote_verified':False,
                          'local_deletion_performed':False}));raise SystemExit(2) from None
