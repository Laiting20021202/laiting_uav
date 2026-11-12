"""Candidate scores stay aligned with live boxes and become independent records."""
import csv
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
import torch
from fastapi.testclient import TestClient
from PIL import Image
from ultralytics.models.yolo.detect import DetectionPredictor

from uav.auth import password_hash
from uav.config import load_settings, model_spec
from uav.detection import Detector
from uav.prediction import CandidatePredictor
from uav.runtime import Runtime
from uav.storage import EventStore
from uav.web import create_app


NAMES = dict(enumerate(model_spec('president-cup-v5')['classes']))


def predictor(cls, *, classes=None, conf=.1):
    instance = cls(overrides={'conf':conf, 'iou':.55, 'max_det':100, 'classes':classes})
    instance.model = SimpleNamespace(names=NAMES, end2end=False)
    instance.batch = (['test.jpg'],)
    return instance


@pytest.mark.parametrize('classes', [None, [0, 1, 3]])
def test_candidate_predictor_preserves_nms_boxes_and_exact_score_alignment(classes):
    raw = torch.zeros((1, 9, 8))
    raw[0, :4] = torch.tensor([
        [50, 50, 40, 40], [150, 150, 40, 40], [50, 50, 40, 40],
        [250, 250, 40, 40], [320, 320, 40, 40], [20, 300, 20, 20],
        [20, 200, 20, 20], [320, 20, 20, 20],
    ]).T
    score_rows = torch.tensor([
        [.8,.2,.03,.001,.0001], [.1,.7,.05,.002,.0002], [.9,.3,.04,.003,.0003],
        [.2,.1,.02,.6,.0004], [.1,.2,.03,.004,.95], [.01,.01,.01,.01,.01],
        [0,0,0,0,0], [.02,.02,.02,.02,.02],
    ])
    raw[0, 4:] = score_rows.T
    img = torch.zeros((1, 3, 400, 400))
    originals = [np.zeros((400,400,3), np.uint8)]
    expected = predictor(DetectionPredictor, classes=classes).postprocess(raw.clone(), img, originals)[0]
    result = predictor(CandidatePredictor, classes=classes).postprocess((raw.clone(), []), img, originals)[0]
    torch.testing.assert_close(result.boxes.data, expected.boxes.data)
    selected = [4, 2, 1, 3] if classes is None else [2, 1, 3]
    np.testing.assert_allclose(result.class_scores, score_rows[selected].tolist())


def test_empty_and_end_to_end_results_keep_normal_behavior():
    img = torch.zeros((1,3,400,400)); originals = [np.zeros((400,400,3), np.uint8)]
    result = predictor(CandidatePredictor).postprocess(torch.zeros((1,9,8)), img, originals)[0]
    assert len(result.boxes) == 0 and result.class_scores == []
    raw = torch.tensor([[[10,20,30,40,.9,0], [50,60,70,80,.01,1]]])
    expected = predictor(DetectionPredictor).postprocess(raw.clone(), img, originals)[0]
    actual = predictor(CandidatePredictor).postprocess(raw.clone(), img, originals)[0]
    torch.testing.assert_close(actual.boxes.data, expected.boxes.data)
    assert getattr(actual, 'class_scores', None) is None


@pytest.mark.parametrize('scores,allowed,expected', [
    ([.9,.00001,.02,.005,.001], [0,1,2,3,4], [0,2,3,4,1]),
    ([.9,0,.02,0,.001], [0,1,2,3,4], [0,2,4]),
    ([.9,.00001,.02,.005,.001], [0,1,4], [0,4,1]),
])
def test_detector_collects_positive_scores_below_tracking_floor(scores, allowed, expected):
    detector = Detector.__new__(Detector)
    detector.config = load_settings()['detection']; detector.kwargs = {}; detector.class_ids = allowed
    result = SimpleNamespace(names=NAMES, class_scores=[scores], boxes=SimpleNamespace(
        data=SimpleNamespace(cpu=lambda:SimpleNamespace(tolist=lambda:[[80,80,240,240,.9,0]]))))
    detector.model = SimpleNamespace(predict=lambda frame, **kwargs:[result])
    detected = detector.detect(np.zeros((360,640,3),np.uint8))
    assert len(detected) == 1
    assert detected[0]['class_name'] == NAMES[0] and detected[0]['confidence'] == .9
    assert [c['class_id'] for c in detected[0]['class_candidates']] == expected
    assert [c['confidence'] for c in detected[0]['class_candidates']] == [scores[i] for i in expected]


def candidates():
    scores = [.9,.3,.05,.001,.00001]
    return dict(bbox=[80,80,240,240], pixel=[160,160], target=True,
                confidence=.9, class_id=0, class_name=NAMES[0],
                class_candidates=[dict(class_id=i,class_name=NAMES[i],confidence=score) for i,score in enumerate(scores)])


def test_each_candidate_has_own_jpeg_without_changing_live_or_repeating_track(tmp_path):
    store = EventStore(tmp_path/'events.sqlite3', min_free_mb=0)
    rt = Runtime(load_settings(), store); frame = np.full((360,640,3), 80, np.uint8)
    d = candidates()
    try:
        rt._process(frame, {'connected':False}, 0, 1720000000, [d])
        events = store.list()
        assert len(events) == 5 and {e['detection']['class_name'] for e in events} == set(NAMES.values())
        live = rt.snapshot()['detections']
        assert len(live) == 1 and live[0]['class_name'] == NAMES[0] and live[0]['confidence'] == .9
        assert 'candidate_rank' not in live[0]
        for e in events:
            candidate = e['detection'];rank = candidate['candidate_rank']
            assert candidate['track_id'] == live[0]['track_id'] and candidate['candidate_count'] == 5
            assert candidate['confidence'] == d['class_candidates'][rank-1]['confidence']
            assert candidate['bbox'] == d['bbox'] and candidate['location'] == live[0]['location']
            assert e['target_gps'] == live[0]['location'] and e['timestamp'] == events[0]['timestamp']
            photo = Image.open(io.BytesIO(store.image(e['id'])))
            assert photo.width == 640 and photo.height > 360
            assert Image.open(io.BytesIO(store.image(e['id'], thumbnail=True))).width <= 480
            description = photo.getexif()[270]
            assert f"Type: {candidate['class_name']}" in description and f'Candidate: {rank}/5' in description
            assert f'Most likely: {NAMES[0]} (90.0%)' in description
            if rank == 5:
                assert 'Confidence: <0.1%' in description
        rt._process(frame, {'connected':False}, .1, 1720000000.1, [d])
        rt._process(frame, {'connected':False}, .2, 1720000000.2, [])
        assert rt.snapshot()['detections'][0]['retained'] and store.count() == 5
        rt._process(frame, {'connected':False}, .3, 1720000000.3, [d])
        assert store.count() == 5
        rt._process(frame, {'connected':False}, .7, 1720000000.7, [d])
        assert store.count() == 10
    finally:
        store.close()


def test_batch_failure_rolls_back_and_real_observation_retries_once(tmp_path):
    store = EventStore(tmp_path/'events.sqlite3', min_free_mb=0)
    rt = Runtime(load_settings(), store);frame = np.zeros((360,640,3), np.uint8);d = candidates()
    # Fail after two inserts to verify rollback also removes their images.
    store.conn.execute("CREATE TRIGGER fail_candidate BEFORE INSERT ON events WHEN "
                       "json_extract(NEW.payload,'$.detection.candidate_rank')=3 "
                       "BEGIN SELECT RAISE(ABORT,'test storage failure'); END")
    store.conn.commit()
    try:
        rt._process(frame, {'connected':False}, 0, 1720000000, [d])
        assert store.count() == 0 and rt.state['storage_error']
        assert store.conn.execute('SELECT COUNT(*) FROM event_images').fetchone()[0] == 0
        assert rt.tracker.tracks[1]['last_record'] is None
        store.conn.execute('DROP TRIGGER fail_candidate');store.conn.commit()
        rt._process(frame, {'connected':False}, .1, 1720000000.1, [])
        assert store.count() == 0  # HOLD never writes, even after a failure.
        rt._process(frame, {'connected':False}, .2, 1720000000.2, [d])
        assert store.count() == 5 and rt.state['storage_error'] is None
        rt._process(frame, {'connected':False}, .3, 1720000000.3, [d])
        assert store.count() == 5
    finally:
        store.close()


def test_batch_validation_and_combined_disk_budget_before_writes(tmp_path, monkeypatch):
    store = EventStore(tmp_path/'events.sqlite3', min_free_mb=0)
    try:
        with pytest.raises(ValueError):
            store.add_many([({'test':1}, b'photo', b'thumb'), ({'test':2}, b'photo', None)])
        assert store.count() == 0
        with pytest.raises(ValueError):
            store.add_many([({'test':1}, None, None), ({'test':float('nan')}, None, None)])
        assert store.count() == 0
        monkeypatch.setattr('uav.storage.shutil.disk_usage', lambda _:SimpleNamespace(free=15))
        with pytest.raises(OSError):
            store.add_many([({'test':1}, b'photo', b'thumb'), ({'test':2}, b'photo', b'thumb')])
        assert store.count() == 0
    finally:
        store.close()


def test_candidate_records_download_as_jpg_and_csv_and_survive_restart(tmp_path):
    config = load_settings();origin = config['web']['origin']
    users = tmp_path/'users.json';users.write_text(json.dumps({'test':password_hash('test-password')}))
    app = create_app(config, data_dir=tmp_path/'data', users_path=users, start_workers=False)
    with TestClient(app, base_url=origin) as client:
        rt = app.state.runtime;store = app.state.store
        rt._process(np.zeros((360,640,3),np.uint8), {'connected':False}, 0, 1720000000, [candidates()])
        assert client.post('/api/login',json={'username':'test','password':'test-password'},headers={'Origin':origin}).status_code == 200
        events = client.get('/api/events').json()['events']
        assert len(events) == 5
        for event in events:
            response = client.get(f"/api/events/{event['id']}/image.jpg?download=true")
            assert response.status_code == 200 and 'attachment;' in response.headers['content-disposition']
            assert f"Type: {event['detection']['class_name']}" in Image.open(io.BytesIO(response.content)).getexif()[270]
        rows = list(csv.DictReader(io.StringIO(client.get('/api/events.csv').text)))
        assert len(rows) == 5 and {r['class_name'] for r in rows} == set(NAMES.values())
        assert {float(r['confidence']) for r in rows} == {c['confidence'] for c in candidates()['class_candidates']}
        path = store.path
    reopened = EventStore(path,min_free_mb=0)
    try:
        assert reopened.count() == 5 and all(e['has_image'] for e in reopened.list())
    finally:
        reopened.close()
