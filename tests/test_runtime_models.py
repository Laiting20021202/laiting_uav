"""Model replacement is committed only after successful construction and warmup."""
from uav.config import load_settings
from uav.runtime import Runtime
from uav.storage import EventStore
import json
import pytest


def test_failed_model_switch_preserves_previous_model(tmp_path,monkeypatch):
    store=EventStore(tmp_path/'events.sqlite3',min_free_mb=0)
    rt=Runtime(load_settings(),store)
    old=object();rt.detector=old;rt.state['model']='working-model'
    def fail(*args):raise RuntimeError('bad engine')
    monkeypatch.setattr('uav.runtime.Detector',fail)
    rt._load_pending()
    assert rt.detector is old and rt.state['model']=='working-model'
    assert rt.state['model_error'] and not rt.state['model_loading']
    replacement=object()
    monkeypatch.setattr('uav.runtime.Detector',lambda *args:replacement)
    rt.switch_model('visdrone-pt');rt._load_pending()
    assert rt.detector is replacement and rt.state['model']=='visdrone-pt'
    assert rt.state['model_error'] is None
    store.close()


def test_grouped_model_registry_and_escape_rejection(tmp_path,monkeypatch):
    from uav import config
    group=tmp_path/'models'/'preliminary round';group.mkdir(parents=True)
    (group/'vehicle.pt').write_bytes(b'fixture')
    manifest={'car':{'file':'vehicle.pt','imgsz':640,'classes':[0]}}
    (group/'registry.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(config,'ROOT',tmp_path)
    registry=config.read_registry()
    assert registry['car']['file']=='preliminary round/vehicle.pt'
    assert config.model_spec('car')['path']==str(group/'vehicle.pt')
    outside=tmp_path/'outside.pt';outside.write_bytes(b'outside')
    (group/'escape.pt').symlink_to(outside)
    manifest['car']['file']='escape.pt'
    (group/'registry.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):config.read_registry()
