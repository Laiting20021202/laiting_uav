"""Recording boundaries, GPS gaps, persistence and authenticated track API."""
import copy
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from uav.auth import password_hash
from uav.config import load_settings
from uav.flight_tracks import FlightTracks, valid_position
from uav.runtime import Runtime
from uav.web import create_app


@pytest.fixture
def recorder(tmp_path):
    flight = Runtime._demo_flight()
    tracks = FlightTracks(tmp_path / "tracks.sqlite3", lambda: copy.deepcopy(flight), min_free_mb=0)
    yield tracks, flight
    tracks.close()


def test_opt_in_stop_freezes_track_and_next_start_is_separate(recorder):
    tracks, flight = recorder
    tracks.sample()
    assert tracks.snapshot()["track"] is None
    started = tracks.start_recording()
    first = started["active"]["id"]
    assert started["active"]["point_count"] == 1
    assert tracks.start_recording()["active"]["id"] == first
    flight["latitude"] += .001
    tracks.sample()
    completed = tracks.stop_recording(first)
    assert completed["active"] is None
    assert 110 < completed["latest"]["distance_m"] < 112
    assert completed["latest"]["point_count"] == 3
    flight["latitude"] += 1
    tracks.sample()
    assert tracks.completed(first)["track"] == completed["latest"]
    second = tracks.start_recording()["active"]["id"]
    assert second != first
    stale_stop = tracks.stop_recording(first)
    assert stale_stop["active"]["id"] == second
    assert stale_stop["latest"]["id"] == first
    assert stale_stop["active"]["point_count"] == 1
    assert stale_stop["active"]["distance_m"] == 0


@pytest.mark.parametrize("change", [
    {"connected":False}, {"fix_type":2}, {"latitude":None}, {"longitude":float("nan")},
    {"latitude":91}, {"longitude":181}, {"latitude":True}, {"fix_type":"3"},
    {"ages_s":{"GLOBAL_POSITION_INT":0}},
    {"ages_s":{"GLOBAL_POSITION_INT":4,"GPS_RAW_INT":0}},
    {"ages_s":{"GLOBAL_POSITION_INT":0,"GPS_RAW_INT":-1}},
])
def test_invalid_gps_is_rejected_and_recovery_starts_new_segment(recorder, change):
    tracks, flight = recorder
    original = copy.deepcopy(flight)
    tracks.start_recording()
    flight.update(change)
    assert not valid_position(flight, 3)
    tracks.sample()
    assert tracks.snapshot()["track"]["point_count"] == 1
    flight.clear()
    flight.update(original)
    flight["latitude"] += .1
    tracks.sample()
    result = tracks.snapshot()
    assert [p["segment"] for p in result["points"]] == [0, 1]
    assert result["track"]["distance_m"] == 0


def test_waiting_for_first_fix_and_equator_coordinates(recorder):
    tracks, flight = recorder
    flight["connected"] = False
    tracks.start_recording()
    assert tracks.snapshot()["active"]["point_count"] == 0
    flight.update(connected=True, latitude=0, longitude=0)
    tracks.sample()
    assert tracks.snapshot()["points"][0]["latitude"] == 0


def test_connection_change_and_sampling_delay_break_the_line(recorder):
    tracks, flight = recorder
    tracks.start_recording()
    flight.update(connections=2, longitude=122)
    tracks.sample()
    tracks.previous["at"] -= 5
    flight["longitude"] = 123
    tracks.sample()
    result = tracks.snapshot()
    assert [p["segment"] for p in result["points"]] == [0, 1, 2]
    assert result["track"]["distance_m"] == 0


def test_crash_recovery_keeps_points_but_does_not_resume(tmp_path):
    path = tmp_path / "tracks.sqlite3"
    tracks = FlightTracks(path, Runtime._demo_flight, min_free_mb=0)
    track_id = tracks.start_recording()["active"]["id"]
    tracks.conn.close()  # Simulate termination before graceful close.
    restored = FlightTracks(path, Runtime._demo_flight, min_free_mb=0)
    try:
        state = restored.snapshot()
        assert state["active"] is None
        assert state["latest"]["id"] == track_id
        assert state["latest"]["end_reason"] == "service_restart"
        assert len(state["points"]) == 1
        restored.sample()
        assert restored.snapshot() == state
    finally:
        restored.close()


def test_storage_failure_is_reported_and_does_not_bridge_missing_points(recorder, monkeypatch):
    tracks, flight = recorder
    tracks.start_recording()
    tracks.min_free = 10
    with monkeypatch.context() as patch:
        patch.setattr("uav.flight_tracks.shutil.disk_usage", lambda path: SimpleNamespace(free=0))
        flight["latitude"] += 1
        tracks.sample()
        assert tracks.snapshot()["error"]
        assert tracks.snapshot()["track"]["point_count"] == 1
    tracks.sample()
    state = tracks.snapshot()
    assert state["error"] is None
    assert state["points"][-1]["segment"] == 1
    assert state["track"]["distance_m"] == 0


def test_worker_records_without_browser_or_camera(recorder):
    tracks, flight = recorder
    tracks.start_worker()
    tracks.start_recording()
    flight["longitude"] += .001
    deadline = time.monotonic() + 4
    while tracks.snapshot()["track"]["point_count"] < 2 and time.monotonic() < deadline:
        time.sleep(.05)
    assert tracks.snapshot()["track"]["point_count"] >= 2


def test_pagination_and_track_cursor_reset(recorder):
    tracks, _ = recorder
    track_id = tracks.start_recording()["active"]["id"]
    with tracks.conn:
        tracks.conn.executemany("INSERT INTO track_points VALUES (?,?,?,?,?,?)",
                                [(track_id, i, "2026-10-02T00:00:00+00:00", 25, 121, 0) for i in range(2, 2006)])
        tracks.conn.execute("UPDATE tracks SET point_count=2005 WHERE id=?", (track_id,))
    first = tracks.snapshot()
    assert len(first["points"]) == 2000 and first["has_more"]
    last = tracks.snapshot(track_id, 2000)
    assert len(last["points"]) == 5 and not last["has_more"]
    assert not tracks.snapshot(track_id, 2005)["points"]
    tracks.stop_recording(track_id)
    new = tracks.start_recording()["active"]["id"]
    reset = tracks.snapshot(track_id, 2005)
    assert reset["track"]["id"] == new
    assert reset["points"][0]["seq"] == 1
    assert tracks.completed(track_id, 2000)["points"][-1]["seq"] == 2006


def test_api_login_csrf_and_completed_download_data(tmp_path):
    users = tmp_path / "users.json"
    users.write_text(json.dumps({"test": password_hash("test-password")}))
    config = load_settings(demo=True)
    config["flight_tracks"]["enabled"] = True
    app = create_app(config, data_dir=tmp_path / "data", users_path=users, start_workers=False)
    origin = config["web"]["origin"]
    with TestClient(app, base_url=origin) as client:
        for path in ("/api/flight-tracks", "/api/flight-tracks/1"):
            assert client.get(path).status_code == 401
        for action in ("start", "stop"):
            assert client.post(f"/api/flight-tracks/{action}", json={"track_id":1}, headers={"Origin":origin}).status_code == 401
        assert client.post("/api/login", json={"username":"test", "password":"test-password"}, headers={"Origin":origin}).status_code == 200
        csrf = client.get("/api/me").json()["csrf"]
        assert client.post("/api/flight-tracks/start", headers={"Origin":origin}).status_code == 403
        headers = {"Origin":origin, "X-CSRF-Token":csrf}
        started = client.post("/api/flight-tracks/start", headers=headers).json()
        track_id = started["active"]["id"]
        assert client.get(f"/api/flight-tracks/{track_id}").status_code == 409
        assert client.get("/api/status").json()["flight_position_valid"]
        assert client.post("/api/flight-tracks/stop", json={"track_id":track_id}, headers={"Origin":origin}).status_code == 403
        assert client.post("/api/flight-tracks/stop", json={"track_id":track_id}, headers=headers).status_code == 200
        completed = client.get(f"/api/flight-tracks/{track_id}").json()
        assert len(completed["points"]) == 2 and completed["track"]["demo"]
        assert client.get("/api/flight-tracks/999").status_code == 404
        assert client.post("/api/flight-tracks/stop", json={"track_id":999}, headers=headers).status_code == 404
        assert client.get("/assets/vendor/leaflet/leaflet.js").status_code == 200
        assert client.get("/assets/vendor/leaflet/unknown").status_code == 404


def test_disabled_tracks_never_open_database_or_create_worker_on_restarts(tmp_path, monkeypatch):
    users = tmp_path / "users.json"
    users.write_text(json.dumps({"test": password_hash("test-password")}))
    directory = tmp_path / "data"
    directory.mkdir()
    path = directory / "flight-tracks.sqlite3"
    # Old route storage, even unreadable/corrupt, must not affect live telemetry.
    path.write_bytes(b"old unreadable route database")
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled route storage was opened")
    monkeypatch.setattr('uav.web.FlightTracks', forbidden)
    config = load_settings(demo=True)
    origin = config['web']['origin']
    for _ in range(2):
        app = create_app(config, data_dir=directory, users_path=users, start_workers=False)
        assert app.state.tracks is None
        with TestClient(app, base_url=origin) as client:
            assert client.get('/api/flight-tracks').status_code == 401
            client.post('/api/login', json={'username':'test', 'password':'test-password'},
                        headers={'Origin':origin})
            csrf = client.get('/api/me').json()['csrf']
            headers = {'Origin':origin, 'X-CSRF-Token':csrf}
            state = client.get('/api/status').json()
            assert not state['flight_tracks_enabled'] and state['flight_position_valid']
            assert client.get('/api/flight-tracks').json()['enabled'] is False
            assert client.post('/api/flight-tracks/start', headers=headers).status_code == 409
            assert client.post('/api/flight-tracks/stop', json={'track_id':1}, headers=headers).status_code == 409
            assert client.get('/api/flight-tracks/1').status_code == 409
    assert path.read_bytes() == b"old unreadable route database"
    assert not list(directory.glob('flight-tracks.sqlite3-*'))
