"""Target filtering, immediate confirmation and durable event retention."""
import cv2
import numpy as np
from uav.config import load_settings
from uav.detection import Tracker, classify_target
from uav.storage import EventStore
from uav.runtime import Runtime
from uav.detection import Detector
from types import SimpleNamespace
import pytest


def test_white_car_alone_is_not_target_and_roof_red_is():
    c=load_settings()['detection']
    image=np.zeros((400,400,3),dtype=np.uint8)
    image[100:300,100:300]=240
    assert not classify_target(image,[100,100,300,300],c)['target']
    image[170:225,170:225]=(0,0,220)
    d=classify_target(image,[100,100,300,300],c)
    assert d['target'] and d['method']=='roof_red_fallback'
    image[170:225,170:225]=240
    image[100:200,315:370]=(0,0,220)
    assert not classify_target(image,[100,100,300,300],c)['target']


def test_cross_marker_detection():
    c=load_settings()['detection']; c['roof_red_fallback']=False
    image=np.zeros((400,400,3),np.uint8);image[75:325,75:325]=240
    image[150:250,150:250]=(0,0,220)
    cv2.line(image,(164,164),(236,236),(255,255,255),20)
    cv2.line(image,(236,164),(164,236),(255,255,255),20)
    d=classify_target(image,[75,75,325,325],c)
    assert d['target'] and d['method']=='red_white_x'


def test_track_retains_miss_and_matches_one_to_one():
    c=load_settings()['detection'];t=Tracker(c)
    d=dict(bbox=[10,10,100,100],target=True,confidence=.9)
    assert t.update([d],1)[0]['confirmed']
    assert t.update([d],1.1)[0]['confirmed']
    retained=t.update([],1.2)[0]
    assert retained['retained'] and retained['hits']==2
    assert t.update([d],1.3)[0]['hits']==3
    t.update([d],1.4)
    assert t.update([d],1.5)[0]['confirmed']
    results=t.update([d,d],1.6)
    assert len({x['track_id'] for x in results})==2


def test_event_survives_restart_and_retention(tmp_path):
    path=tmp_path/'events.sqlite3';store=EventStore(path,max_events=2,min_free_mb=0)
    for i in range(3):store.add({'test':i})
    store.close();store=EventStore(path,max_events=2,min_free_mb=0)
    assert [e['test'] for e in store.list()]==[2,1]
    assert store.count()==2
    store.close()


@pytest.mark.parametrize('preview_width,preview_quality',[(160,10),(960,55)])
def test_inference_and_record_receive_original_pixels_despite_preview_compression(tmp_path,monkeypatch,preview_width,preview_quality):
    c=load_settings();c['preview'].update(width=preview_width,jpeg_quality=preview_quality)
    store=EventStore(tmp_path/'events.sqlite3',min_free_mb=0);rt=Runtime(c,store)
    frame=np.random.default_rng(42).integers(0,256,(1080,1920,3),dtype=np.uint8)
    original=frame.copy();sample=dict(frame=frame,flight={'connected':False},at=1.,timestamp=1720000000.,sequence=1,epoch=0)
    d=dict(bbox=[300,300,500,500],pixel=[400,400],target=True,confidence=.9,class_name='car')
    seen=[]
    detector=Detector.__new__(Detector);detector.config=c['detection'];detector.kwargs={'imgsz':1536}
    def predict(image,**kwargs):
        assert image is frame and kwargs['imgsz']==1536
        np.testing.assert_array_equal(image,original)
        seen.append('inference')
        return [SimpleNamespace(boxes=None)]
    detector.model=SimpleNamespace(predict=predict)
    rt.detector=detector;rt.pending_model=None
    monkeypatch.setattr(rt.camera,'get',lambda:sample)
    monkeypatch.setattr(rt.camera,'is_current',lambda sample:True)
    render=__import__('uav.runtime',fromlist=['render_record']).render_record
    def record(image,event):
        assert image is frame
        np.testing.assert_array_equal(image,original)
        seen.append('record')
        return render(image,event)
    monkeypatch.setattr('uav.runtime.render_record',record)
    process=rt._process
    def process_once(image,flight,at,timestamp,detections):
        assert seen==['inference']
        process(image,flight,at,timestamp,[d])
        rt.stop_event.set()
    monkeypatch.setattr(rt,'_process',process_once)
    monkeypatch.setattr('uav.runtime.time.monotonic',lambda:1.1)
    try:
        rt._run()
        assert seen==['inference','record'] and store.count()==1
        np.testing.assert_array_equal(frame,original)
        preview=cv2.imdecode(np.frombuffer(rt.jpeg,np.uint8),cv2.IMREAD_COLOR)
        assert preview.shape[:2]==(round(1080*preview_width/1920),preview_width)
        saved=cv2.imdecode(np.frombuffer(store.image(store.list()[0]['id']),np.uint8),cv2.IMREAD_COLOR)
        assert saved.shape[1]==1920 and saved.shape[0]>1080
    finally:
        store.close()


def test_first_detection_is_saved_without_fabricated_gps(tmp_path):
    c=load_settings();store=EventStore(tmp_path/'events.sqlite3',min_free_mb=0);rt=Runtime(c,store)
    frame=np.zeros((1080,1920,3),np.uint8)
    d=dict(bbox=[300,300,500,500],pixel=[400,400],target=True,confidence=.9,class_name='car')
    rt._process(frame,{'connected':False},1,1720000000,[d])
    assert store.count()==1
    event=store.list()[0]
    assert not event['target_gps']['valid']
    assert event['target_gps']['reason']=='telemetry_disconnected'
    assert 'latitude' not in event['target_gps']
    assert event['has_image']
    assert event['detection']['visible_s']==0 and event['detection']['confirmed']
    for at in np.arange(1.1,3.5,.1):
        rt._process(frame,{'connected':False},at,1720000001,[d])
        assert store.count()==1
    store.close()


def test_weak_observations_only_continue_matching_class_and_box():
    c=load_settings()['detection'];c['vehicle_conf']=.45;t=Tracker(c)
    d=dict(bbox=[10,10,100,100],target=True,confidence=.31,class_name='car')
    assert t.update([d],0)==[]
    strong=t.update([{**d,'confidence':.45}],.05)[0]
    weak=t.update([d],.1)[0]
    assert weak['track_id']==strong['track_id'] and weak['confidence']==.31 and not weak['retained']
    unmatched=[{**d,'bbox':[200,200,300,300]}, {**d,'class_name':'tank'}, {**d,'confidence':.09}]
    results=t.update(unmatched,.15)
    assert len(results)==1 and results[0]['retained']
    assert results[0]['confidence']==.31
    assert not t.update([d],.46)  # Low confidence cannot revive an expired track.


def test_hold_expires_from_last_observation_and_uses_time_not_frame_count():
    t=Tracker(load_settings()['detection'])
    d=dict(bbox=[10,10,100,100],target=True,confidence=.9,class_name='car')
    original=t.update([d],0)[0]
    t.recorded(original,{},0)
    for at in (.05,.1,.2,.35):
        held=t.update([],at)[0]
        assert held['retained'] and held['missed_s']==pytest.approx(at)
        assert held['track_id']==original['track_id']
        assert not t.should_record(held,{},at)
    assert t.update([],.351)==[]
    fresh=t.update([d],.4)[0]
    assert fresh['track_id']!=original['track_id'] and t.should_record(fresh,{},.4)


def test_gap_recovery_keeps_record_and_low_confidence_respects_one_to_one():
    t=Tracker(load_settings()['detection'])
    d=dict(bbox=[10,10,100,100],target=True,confidence=.9,class_name='car')
    original=t.update([d],0)[0];t.recorded(original,{},0)
    t.update([],.1)
    weak={**d,'confidence':.1}
    results=t.update([weak,weak],.2)
    assert len(results)==1 and results[0]['track_id']==original['track_id']
    assert not t.should_record(results[0],{},.2)


def test_gui_threshold_changes_apply_to_new_targets_and_continuation():
    c=load_settings()['detection'];c['vehicle_conf']=.45;t=Tracker(c)
    d=dict(bbox=[10,10,100,100],target=True,confidence=.2,class_name='car')
    assert not t.update([d],0)
    c['vehicle_conf']=.15
    assert t.update([d],.1)[0]['confidence']==.2
    c['track_conf']=.25
    assert t.update([{**d,'confidence':.1}],.2)[0]['retained']
    c['vehicle_conf']=.05
    assert not t.update([{**d,'confidence':.05}],.25)[0]['retained']


def test_retained_preview_has_no_new_gps_or_event_and_unmatched_weak_box_is_hidden(tmp_path,monkeypatch):
    c=load_settings();c['detection']['vehicle_conf']=.45
    store=EventStore(tmp_path/'events.sqlite3',min_free_mb=0);rt=Runtime(c,store)
    frame=np.zeros((1080,1920,3),np.uint8)
    d=dict(bbox=[300,300,500,500],pixel=[400,400],target=True,confidence=.9,class_name='car')
    rt._process(frame,{'connected':False},0,1720000000,[d])
    assert store.count()==1
    boxes=[]
    rectangle=cv2.rectangle
    monkeypatch.setattr('uav.runtime.cv2.rectangle',lambda image,a,b,color,width: (boxes.append((a,b,color)),rectangle(image,a,b,color,width))[1])
    def no_location(*args):raise AssertionError('Retained box must not calculate a new GPS')
    monkeypatch.setattr('uav.runtime.locate',no_location)
    rt.last_encode_at=0
    rt._process(frame,{'connected':False},.2,1720000000.2,[{**d,'bbox':[700,700,800,800],'confidence':.2}])
    held=rt.snapshot()['detections']
    assert len(held)==1 and held[0]['retained'] and not held[0]['location']['valid']
    assert store.count()==1 and boxes==[((300,300),(500,500),(0,180,255))]
    rt._process(frame,{'connected':False},.4,1720000000.4,[])
    assert rt.snapshot()['detections']==[] and store.count()==1
    store.close()
