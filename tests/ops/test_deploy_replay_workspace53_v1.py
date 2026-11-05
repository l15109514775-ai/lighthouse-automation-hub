import importlib.util
from copy import deepcopy
from pathlib import Path
import pytest
s=importlib.util.spec_from_file_location('cutover53',Path(__file__).parents[2]/'scripts/ops/deploy_replay_workspace53_v1.py');op=importlib.util.module_from_spec(s);s.loader.exec_module(op)
def test_only_image_and_exact_retention_source_are_changed():
 before={'services':{'video-worker':{'image':op.OLD_IMAGE,'environment':{'OWNED':'same'},'volumes':[{'target':'/run/qualifications/retention-v5','source':str(op.OLD_PROOF),'read_only':True},{'target':'/owned','source':'/owned','read_only':False}]},'owned-other':{'image':'unchanged'}}}
 after=op.candidate(before,Path('/root/owned-new-proof'));assert before['services']['video-worker']['image']==op.OLD_IMAGE
 back=deepcopy(after);back['services']['video-worker']['image']=op.OLD_IMAGE;back['services']['video-worker']['volumes'][0]['source']=str(op.OLD_PROOF);assert back==before
@pytest.mark.parametrize('values',[['A=1','B=2'],['B=2','A=1']])
def test_env_order_is_irrelevant_but_values_are_exact(values):assert op.env_map(values)=={'A':'1','B':'2'}
def test_duplicate_environment_name_is_refused():
 with pytest.raises(AssertionError):op.env_map(['A=1','A=1'])
