from copy import deepcopy
import pytest
from services.hub.errors import HubError
from services.hub.replay_parts_v5 import native_surface

def test_sidebar_space_collapse_keeps_identity_and_cardinality_checks():
    binding={'file':'/owned/original.mp4','metadata':{'title':'龍巻示例主播B  回放 P1/1'}}
    surface={'surface_mode':'single_selected_task_sidebar','actual_outer_count':1,
             'actual_inner_count':1,'metadata_form_count':1,'native_filename':'original',
             'selected_task_title':'龍巻示例主播B 回放 P1/1'}
    assert native_surface(binding,surface,metadata=True)==surface
    exact={**surface,'selected_task_title':binding['metadata']['title']}
    assert native_surface(binding,exact,metadata=True)==exact
    saved={**surface,'surface_mode':'single_editor_no_batch_sidebar','actual_outer_count':0,
           'selected_task_title':None,'native_filename':'龍巻示例主播B 回放 P1/1'}
    assert native_surface(binding,saved,metadata=True)==saved
    with pytest.raises(HubError):native_surface(binding,saved,metadata=False)
    changed={**saved,'native_filename':'龍巻示例主播B 回放 P2/2'}
    with pytest.raises(HubError):native_surface(binding,changed,metadata=True)
    spaced_file={'file':'/owned/raw  video.mp4','metadata':binding['metadata']}
    changed={**saved,'native_filename':'raw video'}
    with pytest.raises(HubError):native_surface(spaced_file,changed,metadata=True)
    for field,value in [('selected_task_title','示例主播A 回放 P1/1'),('selected_task_title','龍巻示例主播B 回放 P2/2'),
                        ('native_filename','another.mp4'),('actual_outer_count',2)]:
        changed=deepcopy(surface);changed[field]=value
        with pytest.raises(HubError):native_surface(binding,changed,metadata=True)
