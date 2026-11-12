"""Frame budgets include recording work and skip obsolete camera samples."""
from types import SimpleNamespace

import pytest

from uav.config import load_settings
from uav.runtime import Runtime
from uav.storage import EventStore


@pytest.mark.parametrize("inference_s,record_s,starts,sequences", [
    (.01, .02, [100, 100.25, 100.5], [1, 2, 3]),
    (.20, .02, [100, 100.27, 100.54], [1, 2, 3]),
    (.40, .15, [100, 100.60, 101.20], [1, 3, 5]),
])
def test_processing_yields_and_always_uses_latest_frame(
        tmp_path, monkeypatch, inference_s, record_s, starts, sequences):
    config = load_settings()
    store = EventStore(tmp_path / "events.sqlite3", min_free_mb=0)
    rt = Runtime(config, store)
    rt.pending_model = None
    now = [100.0]
    calls, processed, waits = [], [], []

    class StopEvent:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            if not self.stopped:
                waits.append(seconds)
                now[0] += seconds
                # Bound the loop even when Runtime catches an unexpected error.
                if len(waits) > 10:
                    self.stopped = True

    def latest():
        sequence = int((now[0] - 100) * 4) + 1
        return dict(frame=sequence, sequence=sequence, epoch=0,
                    at=100 + (sequence - 1) / 4, timestamp=1720000000,
                    flight={})

    def detect(frame):
        calls.append(now[0])
        now[0] += inference_s
        return []

    def process(frame, flight, at, timestamp, detections):
        processed.append(frame)
        now[0] += record_s
        if len(processed) == 3:
            rt.stop_event.stopped = True

    rt.stop_event = StopEvent()
    rt.detector = SimpleNamespace(detect=detect)
    monkeypatch.setattr("uav.runtime.time", SimpleNamespace(
        monotonic=lambda: now[0], time=lambda: 1720000000))
    monkeypatch.setattr(rt.camera, "get", latest)
    monkeypatch.setattr(rt.camera, "is_current", lambda sample: True)
    monkeypatch.setattr(rt, "_process", process)
    try:
        rt._run()
        assert rt.state["model_error"] is None
        assert calls == pytest.approx(starts)
        assert processed == sequences
        assert len(waits) == 2
        assert min(waits) >= .05
    finally:
        store.close()
