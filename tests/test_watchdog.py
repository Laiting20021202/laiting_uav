"""Only progressing capture/inference/telemetry loops feed the service watchdog."""
import time
from types import SimpleNamespace
from uav.config import load_settings
from uav.runtime import Runtime
from uav.storage import EventStore


def test_offline_devices_can_be_healthy_but_stalled_workers_cannot(tmp_path,monkeypatch):
    store=EventStore(tmp_path/'events.sqlite3',min_free_mb=0)
    runtime=Runtime(load_settings(),store)
    thread=SimpleNamespace(is_alive=lambda:True)
    runtime.thread=runtime.camera.thread=runtime.telemetry.thread=thread
    notifications=[];monkeypatch.setattr('uav.runtime.notify_watchdog',lambda:notifications.append(True))
    # The on-demand G3P control worker is normally stopped while RTSP runs.
    runtime.watchdog();assert len(notifications)==1
    runtime.camera.control.thread=thread
    runtime.watchdog();assert len(notifications)==2
    runtime.camera.progress_at=time.monotonic()-40
    runtime.watchdog();assert len(notifications)==2
    runtime.camera.progress_at=time.monotonic();runtime.progress_at=time.monotonic()-70
    runtime.watchdog();assert len(notifications)==2
    store.close()
