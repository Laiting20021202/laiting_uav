"""Record timing, JPEG content, atomic deletion, migration and HTTP access."""
import io
import json
import sqlite3
import time
from types import SimpleNamespace
import cv2
import numpy as np
import pytest
from PIL import Image
from PIL.JpegImagePlugin import get_sampling
from fastapi.testclient import TestClient
from uav.auth import password_hash
from uav.config import load_settings
from uav.detection import Detector, Tracker
from uav.geo import to_twd97
from uav.records import record_lines, render_record
from uav.storage import EventStore
from uav.runtime import Runtime
from uav.web import create_app


def detection():
    return dict(bbox=[80,80,240,240], pixel=[160,160], target=True, confidence=.95, class_name='truck', track_id=1)


def event(valid=True, example=False):
    g=dict(valid=True, latitude=25, longitude=121, twd97=to_twd97(25,121), orientation_source='assumed_nadir') if valid else dict(valid=False, reason='camera_not_calibrated')
    return dict(timestamp='2026-09-21T07:00:00+00:00', detection=detection(),target_gps=g,
                aircraft={'latitude':24,'longitude':120},demo=False,model='test',example=example)


def test_first_frame_one_record_per_continuous_track():
    tracker=Tracker(load_settings()['detection']);d=detection();location={'valid':True}
    target=tracker.update([d],0)[0]
    assert target['confirmed'] and tracker.should_record(target,location,0)
    tracker.recorded(target,location,0)
    for at in np.arange(.1,20,.1):
        target=tracker.update([d],float(at))[0]
        assert not tracker.should_record(target,location,at)
    assert tracker.update([],20)[0]['retained']
    target=tracker.update([d],20.4)[0]
    assert target['confirmed'] and tracker.should_record(target,location,20.1)


def test_long_frame_gap_and_changed_class_are_recorded_immediately():
    tracker=Tracker(load_settings()['detection']);d=detection()
    tracker.update([d],0)
    assert tracker.update([d],3)[0]['confirmed']
    tracker.update([d],3.5)
    changed=tracker.update([{**d,'class_name':'tank'}],4)[0]
    assert changed['visible_s']==0 and changed['confirmed']


def test_unmarked_vehicle_boxes_are_recording_targets():
    detector=Detector.__new__(Detector);detector.config=load_settings()['detection'];detector.kwargs={}
    rows=[[80,80,240,240,.9,0],[250,80,380,200,.9,1]]
    result=SimpleNamespace(names={0:'car',1:'tank'},boxes=SimpleNamespace(data=SimpleNamespace(cpu=lambda:SimpleNamespace(tolist=lambda:rows))))
    detector.model=SimpleNamespace(predict=lambda frame,**kw:[result])
    result=detector.detect(np.zeros((400,400,3),np.uint8))
    assert {d['class_name'] for d in result}=={'car','tank'}
    assert all(d['target'] and not d['marker_detected'] for d in result)
    assert result[0]['pixel']==[160,160]


@pytest.mark.parametrize('valid,example',[(True,False),(False,False),(True,True)])
def test_jpeg_contains_full_view_box_time_type_and_target_coordinates(valid,example):
    frame=np.full((360,640,3),80,np.uint8);e=event(valid,example)
    jpeg,thumbnail=render_record(frame,e)
    image=Image.open(io.BytesIO(jpeg));thumb=Image.open(io.BytesIO(thumbnail))
    assert image.format=='JPEG' and image.width==640 and image.height>360
    assert get_sampling(image)==0  # Full-resolution local archive uses 4:4:4.
    assert thumb.width<=480 and thumb.height<=480
    description=image.getexif()[270]
    assert '2026-09-21 15:00:00.000' in description and 'truck' in description
    assert 'EPSG:3826' in description
    assert 'EPSG:4326' in description
    assert ('Latitude 25.0000000 deg   Longitude 121.0000000 deg' in description)==valid
    assert 'Latitude 24.0000000' not in description  # Never substitute aircraft GPS.
    assert ('E 250000.00 m' in description)==valid
    assert ('UNAVAILABLE' in description)==(not valid)
    assert ('EXAMPLE coordinates - fictional' in description)==example
    decoded=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
    assert np.max(np.abs(decoded[330,600].astype(int)-80))<5
    assert decoded[200,80].max()-decoded[200,80].min()>60


@pytest.mark.parametrize('gps', [
    {'valid': False, 'latitude': 25, 'longitude': 121},
    {'valid': True, 'latitude': float('nan'), 'longitude': 121},
    {'valid': True, 'latitude': 91, 'longitude': 121},
    {'valid': True},
])
def test_record_does_not_print_invalid_or_missing_gps(gps):
    e=event();e['target_gps']=gps
    assert 'Target WGS84 (EPSG:4326): Latitude / Longitude UNAVAILABLE' in record_lines(e)


def test_photo_retention_clear_restart_and_ids_never_reused(tmp_path):
    path=tmp_path/'events.sqlite3';store=EventStore(path,max_events=2,min_free_mb=0)
    photo,thumb=render_record(np.zeros((360,640,3),np.uint8),event())
    ids=[store.add(event(),photo,thumb)['id'] for _ in range(3)]
    assert store.image(ids[0]) is None and store.image(ids[-1])==photo
    assert store.clear()==2
    assert store.conn.execute('SELECT COUNT(*) FROM event_images').fetchone()[0]==0
    store.close();store=EventStore(path,min_free_mb=0)
    assert store.list()==[] and store.image(ids[-1]) is None
    assert store.add(event(),photo,thumb)['id']>ids[-1]
    store.close()


def test_existing_text_records_migrate_without_loss(tmp_path):
    path=tmp_path/'events.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, payload TEXT NOT NULL)')
        conn.execute('INSERT INTO events VALUES (41,?,?)',(event()['timestamp'],json.dumps(event())))
    store=EventStore(path,min_free_mb=0)
    assert store.list()[0]['id']==41 and not store.list()[0]['has_image']
    assert store.clear()==1 and store.add(event())['id']==42
    store.close()


def test_clear_records_next_detection_and_rejects_inflight_old_frame(tmp_path):
    config=load_settings();store=EventStore(tmp_path/'events.sqlite3',min_free_mb=0);rt=Runtime(config,store)
    frame=np.zeros((360,640,3),np.uint8);d=detection();start=time.monotonic()-4
    for delta in (0,.1,.2,.3,.4):rt._process(frame,{'connected':False},start+delta,1720000000,[d])
    assert store.count()==1
    assert rt.clear_events()==1
    cutoff=rt.record_cutoff
    rt._process(frame,{'connected':False},cutoff-.1,1720000000,[d])
    assert not rt.tracker.tracks and store.count()==0
    for delta in (.1,.2,.3,.4,.5):
        rt._process(frame,{'connected':False},cutoff+delta,1720000000,[d])
        assert store.count()==1
    rt._process(frame,{'connected':False},cutoff+.6,1720000000,[d])
    assert store.count()==1
    store.close()


def test_photo_download_clear_permissions_and_pinned_example(tmp_path):
    config=load_settings();users=tmp_path/'users.json';users.write_text(json.dumps({'test':password_hash('test-password')}))
    app=create_app(config,data_dir=tmp_path/'data',users_path=users,start_workers=False)
    origin=config['web']['origin'];store=app.state.store
    photo,thumb=render_record(np.zeros((360,640,3),np.uint8),event(example=True))
    example=store.add(event(example=True),photo,thumb)
    newer=store.add(event(),photo,thumb)
    with TestClient(app,base_url=origin) as client:
        path=f"/api/events/{example['id']}/image.jpg"
        assert client.get(path).status_code==401
        assert client.delete('/api/events',headers={'Origin':origin}).status_code==401
        assert client.post('/api/login',json={'username':'test','password':'test-password'},headers={'Origin':origin}).status_code==200
        csrf=client.get('/api/me').json()['csrf']
        response=client.get(path+'?download=true')
        assert response.content==photo and response.headers['content-type']=='image/jpeg'
        assert 'attachment;' in response.headers['content-disposition']
        assert client.get(path+'?thumbnail=true').content==thumb
        batch=client.get('/api/events?limit=1').json()
        assert batch['events'][0]['example'] and batch['events'][1]['id']==newer['id']
        assert client.get(f"/api/events?before={newer['id']}").json()['events']==[]
        assert 'twd97_easting_m' in client.get('/api/events.csv').text
        assert client.delete('/api/events',headers={'Origin':origin}).status_code==403
        assert client.delete('/api/events',headers={'Origin':'https://evil.test','X-CSRF-Token':csrf}).status_code==403
        assert store.count()==2
        assert client.delete('/api/events',headers={'Origin':origin,'X-CSRF-Token':csrf}).json()['deleted']==2
        assert client.get(path).status_code==404 and client.get('/api/events').json()['events']==[]


def test_standard_gps_estimate_prints_coordinates_and_assumptions():
    from uav.geo import locate
    config=load_settings()['geolocation']
    flight=dict(connected=True,latitude=25,longitude=121,roll=0,pitch=0,yaw=0,relative_altitude_m=60,fix_type=3,
                ages_s={'GLOBAL_POSITION_INT':.1,'GPS_RAW_INT':.1,'ATTITUDE':.1})
    e=event();e['target_gps']=locate((1397.5,443),(1920,1080),flight,config)
    photo,_=render_record(np.zeros((1080,1920,3),np.uint8),e)
    description=Image.open(io.BytesIO(photo)).getexif()[270]
    assert 'UNAVAILABLE' not in description
    assert 'GPS: GPS 3D' in description and 'ESTIMATE / uncalibrated lens' in description
    assert 'target ground at takeoff elevation' in description
    assert f"Latitude {e['target_gps']['latitude']:.7f}" in description
