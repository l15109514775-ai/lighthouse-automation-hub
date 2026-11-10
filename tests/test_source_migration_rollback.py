"""Pure plan tests: no Docker, systemd, networking or production mutation."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('source_rollback', Path(__file__).parents[1] / 'scripts/restore_fenced_source.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RollbackPlanTests(unittest.TestCase):
    def setUp(self):
        self.record = {'version': 'lighthouse-source-fence-v1', 'target_activated': False,
                       'target_ip': '192.0.2.2', 'phase': 'writers_fenced', 'containers_before': [],
                       'units_before': [{'name':'lighthouse-hub.service','enabled':'enabled','active':'active'}]}
        self.actual = []
        for name, running in [('lighthouse-tweet', True), ('lighthouse-video', False)]:
            self.record['containers_before'].append({'name':name,'id':name,'image':'sha256:test',
                'running':running,'restart_policy':{'Name':'unless-stopped','MaximumRetryCount':0}})
            self.actual.append({'Name':'/'+name,'Id':name,'Image':'sha256:test','State':{'Running':False}})
        self.proof = {'version':'lighthouse-target-quiescent-v1','target_ip':'192.0.2.2',
                      'ever_activated':False,'all_containers_stopped':True,'owner_record_present':False,'checked_at':1000}

    def test_only_original_active_containers_restart_and_no_compose_up(self):
        commands, dropin, running = module.plan(self.record, self.proof, self.actual, 1010)
        self.assertEqual(running, ['lighthouse-tweet'])
        self.assertEqual([c for c in commands if c[:2] == ['docker','start']], [['docker','start','lighthouse-tweet']])
        self.assertNotIn('lighthouse-video', dropin)
        self.assertNotIn('compose', dropin)

    def test_target_proof_must_be_fresh_and_never_activated(self):
        for key, value in [('ever_activated',True),('all_containers_stopped',False),('owner_record_present',True),('checked_at',800),('checked_at',2000),('target_ip','192.0.2.9')]:
            with self.subTest(key=key, value=value):
                proof = {**self.proof, key:value}
                with self.assertRaisesRegex(ValueError, 'TARGET_NOT_PROVEN'):
                    module.plan(self.record, proof, self.actual, 1010)

    def test_container_identity_or_retired_running_drift_stops_plan(self):
        for change in ('image', 'running'):
            actual = copy.deepcopy(self.actual)
            if change == 'image': actual[0]['Image'] = 'sha256:other'
            else: actual[1]['State']['Running'] = True
            with self.assertRaises(ValueError): module.plan(self.record, self.proof, actual, 1010)

    def test_forbidden_unit_name_and_repeat_restore(self):
        self.record['units_before'][0]['name'] = '../../ssh.service'
        with self.assertRaisesRegex(ValueError, 'UNIT_NAME'): module.plan(self.record, self.proof, self.actual, 1010)
        self.record['phase'] = 'source_restored'
        with self.assertRaisesRegex(ValueError, 'ALREADY_RESTORED'): module.plan(self.record, self.proof, self.actual, 1010)


if __name__ == '__main__': unittest.main()
