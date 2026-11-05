from pathlib import Path
import json
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_submodules_are_exact_reviewed_commits():
    expected = {'compositor': '7b3905cf3fcde78c0dcaa95bf973a962a3c82df1',
                'vendor/subtitle-cloud-worker': 'ca582b00a2ba0e3306e88f6728a49fb6b3528c5b'}
    for path, commit in expected.items():
        if (ROOT / path / '.git').exists():
            assert subprocess.check_output(['git', '-C', str(ROOT / path), 'rev-parse', 'HEAD'], text=True).strip() == commit
        else:
            # Release containers exclude Git metadata; verify all pinned source bytes instead.
            import importlib.util
            spec = importlib.util.spec_from_file_location('verify_upstream', ROOT / 'scripts/verify_upstream.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            manifest = json.loads((ROOT / 'config/upstream-lock.json').read_text('utf-8'))
            assert manifest[path]['commit'] == commit
            assert module.verify() > 100


def test_manual_review_schema_and_safe_defaults():
    schema = json.loads((ROOT / 'compositor/src/schema.json').read_text('utf-8'))
    assert schema['definitions']['post']['properties']['review']['required'] == ['translationConfirmed', 'sourceConfirmed']
    cfg = tomllib.loads((ROOT / 'config/hub.toml').read_text('utf-8'))
    assert cfg['auto_publish'] is False
    assert cfg['first_publish_accepted'] is False
    assert cfg['authorized_bilibili_accounts'] == []
    assert all(not item['paid_authorized'] for item in cfg['budgets'].values())
