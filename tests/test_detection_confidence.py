"""Live confidence changes, persistence and authenticated HTTP boundaries."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from uav.auth import password_hash
from uav.config import ROOT, load_settings, model_spec, read_registry
from uav.detection import Detector
from uav.web import create_app


@pytest.fixture
def client(tmp_path):
    settings=tmp_path/'settings.json'
    settings.write_text((ROOT/'config/settings.json').read_text())
    users=tmp_path/'users.json'
    users.write_text(json.dumps({'test':password_hash('test-password')}))
    config=load_settings(settings)
    app=create_app(config,data_dir=tmp_path/'data',users_path=users,start_workers=False)
    with TestClient(app,base_url=config['web']['origin']) as client:
        yield client,settings


def login(client):
    origin=str(client.base_url).rstrip('/')
    assert client.post('/api/login',json={'username':'test','password':'test-password'},
                       headers={'Origin':origin}).status_code==200
    return {'Origin':origin,'X-CSRF-Token':client.get('/api/me').json()['csrf']}


def test_confidence_needs_login_origin_and_csrf(client):
    client,_=client
    body={'confidence':0.15};origin=str(client.base_url).rstrip('/')
    assert client.post('/api/detection/confidence',json=body,headers={'Origin':origin}).status_code==401
    headers=login(client)
    assert client.post('/api/detection/confidence',json=body,headers={'Origin':origin}).status_code==403
    assert client.post('/api/detection/confidence',json=body,
                       headers={**headers,'Origin':'https://evil.test'}).status_code==403


@pytest.mark.parametrize('value',[0,-0.1,0.009,1.01,True,'0.3',None])
def test_invalid_threshold_leaves_settings_unchanged(client,value):
    client,settings=client;headers=login(client);before=settings.read_bytes()
    assert client.post('/api/detection/confidence',json={'confidence':value},headers=headers).status_code==422
    assert settings.read_bytes()==before


def test_live_threshold_persists_preserving_other_settings_and_custom_path(client):
    client,settings=client;headers=login(client);before=json.loads(settings.read_text())
    models=client.get('/api/models').json()
    assert models['active'] is None and models['default']=='president-cup-v5'
    response=client.post('/api/detection/confidence',json={'confidence':0.15},headers=headers)
    assert response.status_code==200 and response.json()=={'confidence':0.15,'persistent':True}
    assert client.get('/api/status').json()['detection_confidence']==0.15
    before['detection']['vehicle_conf']=0.15
    assert json.loads(settings.read_text())==before
    assert load_settings(settings)['detection']['vehicle_conf']==0.15
    assert load_settings(settings)['detection']['record_after_s']==0
    assert client.post('/api/detection/confidence',json={'confidence':1,'extra':True},headers=headers).status_code==422
    for value in (0.01,1):
        assert client.post('/api/detection/confidence',json={'confidence':value},headers=headers).status_code==200


def test_failed_save_keeps_live_threshold(client,monkeypatch):
    client,settings=client;headers=login(client);before=settings.read_bytes()
    original=client.get('/api/status').json()['detection_confidence']
    def fail(*args,**kwargs):
        raise OSError('disk unavailable')
    monkeypatch.setattr('uav.runtime.atomic_json',fail)
    assert client.post('/api/detection/confidence',json={'confidence':0.15},headers=headers).status_code==503
    assert client.get('/api/status').json()['detection_confidence']==original
    assert settings.read_bytes()==before


def test_each_inference_uses_current_confidence():
    detector=Detector.__new__(Detector)
    detector.config=load_settings()['detection'];detector.kwargs={'conf':0.6,'imgsz':1536}
    calls=[]
    def predict(frame,**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(boxes=None)]
    detector.model=SimpleNamespace(predict=predict)
    frame=np.zeros((32,32,3),np.uint8)
    for confidence in (0.3,0.05,0.8):
        detector.config['vehicle_conf']=confidence
        assert detector.detect(frame)==[]
    detector.config['track_conf']=0.2
    assert detector.detect(frame)==[]
    assert [call['conf'] for call in calls]==[0.1,0.05,0.1,0.2]
    assert all(call['imgsz']==1536 for call in calls)


def test_new_model_registry_and_immediate_recording_default():
    registry=read_registry()
    assert model_spec('president-cup-v5',registry)['path']==str(ROOT/'models/president_cup_v5/best.pt')
    assert model_spec('competition-5class-v1-3',registry)['path']==str(ROOT/'models/competition_5class_v1-3/weights/best.pt')
    assert registry['president-cup-v5']['imgsz']==1280
    assert registry['competition-5class-v1-3']['imgsz']==1536
    for name in ('president-cup-v5','competition-5class-v1-3'):
        assert len(registry[name]['classes'])==5
    assert load_settings()['detection']['model']=='president-cup-v5'
    assert load_settings()['detection']['record_after_s']==0
    # The deployed threshold is editable and persists through the GUI.
    configured=json.loads((ROOT/'config/settings.json').read_text())['detection']['vehicle_conf']
    assert load_settings()['detection']['vehicle_conf']==configured
    assert load_settings()['detection']['track_conf']==0.10
    assert load_settings()['detection']['track_ttl_s']==0.35
    assert (load_settings()['camera']['width'],load_settings()['camera']['height'],load_settings()['camera']['fps'])==(1920,1080,4)


def test_tracking_settings_saved_together_and_reported_in_status(client):
    client,settings=client;headers=login(client)
    body={'confidence':0.5,'track_confidence':0.12,'track_ttl_s':0.25}
    response=client.post('/api/detection/confidence',json=body,headers=headers)
    assert response.status_code==200 and response.json()=={**body,'persistent':True}
    saved=load_settings(settings)['detection'];status=client.get('/api/status').json()
    assert (saved['vehicle_conf'],saved['track_conf'],saved['track_ttl_s'])==(0.5,0.12,0.25)
    assert (status['detection_confidence'],status['tracking_confidence'],status['track_ttl_s'])==(0.5,0.12,0.25)


@pytest.mark.parametrize('field,value',[('track_confidence',0),('track_confidence',1.1),
    ('track_confidence',True),('track_confidence','0.1'),('track_ttl_s',0.05),('track_ttl_s',10.1),('track_ttl_s',True)])
def test_invalid_tracking_setting_does_not_partially_save(client,field,value):
    client,settings=client;headers=login(client);before=settings.read_bytes()
    body={'confidence':0.5,'track_confidence':0.1,'track_ttl_s':0.35,field:value}
    assert client.post('/api/detection/confidence',json=body,headers=headers).status_code==422
    assert settings.read_bytes()==before


def test_failed_combined_save_preserves_all_live_tracking_settings(client,monkeypatch):
    client,settings=client;headers=login(client);before=settings.read_bytes()
    status=client.get('/api/status').json()
    def fail(*args,**kwargs):raise OSError('disk unavailable')
    monkeypatch.setattr('uav.runtime.atomic_json',fail)
    body={'confidence':0.5,'track_confidence':0.2,'track_ttl_s':0.5}
    assert client.post('/api/detection/confidence',json=body,headers=headers).status_code==503
    after=client.get('/api/status').json()
    assert all(after[k]==status[k] for k in ('detection_confidence','tracking_confidence','track_ttl_s'))
    assert settings.read_bytes()==before


def test_missing_delay_defaults_to_immediate_and_negative_delay_is_rejected(tmp_path):
    settings=json.loads((ROOT/'config/settings.json').read_text())
    del settings['detection']['record_after_s']
    path=tmp_path/'settings.json';path.write_text(json.dumps(settings))
    assert load_settings(path)['detection']['record_after_s']==0
    settings['detection']['record_after_s']=-0.1
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError,match='record_after_s'):
        load_settings(path)


def test_loader_requires_all_v5_classes_before_accepting_model(monkeypatch):
    import ultralytics
    spec=model_spec('president-cup-v5')
    names={i:name for i,name in enumerate(spec['classes'])}
    model=SimpleNamespace(names=names,predict=lambda *args,**kwargs:[])
    monkeypatch.setattr(ultralytics,'YOLO',lambda *args,**kwargs:model)
    detector=Detector('president-cup-v5',load_settings()['detection'])
    assert detector.class_ids==list(range(5))
    del names[4]
    with pytest.raises(ValueError,match='amphibious_assault_vehicle'):
        Detector('president-cup-v5',load_settings()['detection'])
