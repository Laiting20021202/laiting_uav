"""FFmpeg capture with a single latest-frame slot, read deadline and reconnect."""
import json
import logging
import os
import select
import subprocess
import threading
import time
from collections import deque
from functools import lru_cache
from concurrent.futures import TimeoutError as FutureTimeout
from .g3p import G3PControl, G3PCommandError
from urllib.parse import urlparse
import numpy as np

log = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def rtsp_timeout_flag(binary):
    """FFmpeg 4.x uses stimeout; newer builds use timeout (rw_timeout fails here)."""
    result = subprocess.run([binary, "-hide_banner", "-h", "demuxer=rtsp"],
                            capture_output=True, text=True, timeout=5, check=True)
    return "-stimeout" if "-stimeout " in result.stdout + result.stderr else "-timeout"


class ReconnectRequested(Exception):
    pass


class PlannedReconnect(Exception):
    pass


class Camera:
    def __init__(self, config, telemetry):
        self.config = config
        self.telemetry = telemetry
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.reconnect_event = threading.Event()
        self.latest = None
        self.sequence = 0
        self.error = None
        self.thread = None
        self.process = None
        self.phase = "waiting"
        self.attempts = 0
        self.connections = 0
        self.refreshes = 0
        self.last_connected = None
        self.last_frame_at = None
        self.frame_times = deque(maxlen=30)
        self.epoch = 0
        self.progress_at = time.monotonic()
        self.last_failure = None
        self.control_error = None
        self.recovery_failures = 0
        self.busy_failures = 0
        self.next_control_at = 0
        self.power_cycle_required = False
        self.control = G3PControl(urlparse(config["source"]).hostname,
                                  settings=config.get("g3p_settings")) if config["g3p_start"] and config["source"].startswith("rtsp://") else None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="camera-reader", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.request_reconnect()
        if self.control:
            self.control.stop()
        if self.thread:
            self.thread.join(timeout=20)

    def request_reconnect(self):
        self.reconnect_event.set()
        with self.lock:
            self.latest = None
            self.epoch += 1
            self.phase = "reconnecting"
            self.next_control_at = 0
            process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            age = now-self.latest["at"] if self.latest else None
            span = self.frame_times[-1]-self.frame_times[0] if len(self.frame_times)>1 else 0
            fps = (len(self.frame_times)-1)/span if span > 0 else 0
            return {"phase": self.phase, "connected": age is not None and age < 3,
                    "age_s": round(age, 3) if age is not None else None, "fps": round(fps, 1) if age is not None and age < 3 else 0,
                    "attempts": self.attempts, "connections": self.connections,
                    "refreshes": self.refreshes,
                    "last_connected": self.last_connected, "last_frame": self.last_frame_at,
                    "error": self.error, "last_error": self.last_failure, "control_error": self.control_error,
                    "recovery_failures": self.recovery_failures,
                    "recovery_retry_in_s": round(max(0, self.next_control_at-now), 1) if self.error or not self.connections else 0,
                    "power_cycle_required": self.power_cycle_required,
                    "control": self.control.snapshot() if self.control else None}

    def is_current(self, sample):
        with self.lock:
            return sample.get("epoch") == self.epoch and self.latest is not None

    def _check_cancelled(self):
        self.progress_at = time.monotonic()
        if self.stop_event.is_set() or self.reconnect_event.is_set():
            raise ReconnectRequested()

    @staticmethod
    def _terminate(process):
        try:
            if process.poll() is None:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

    def get(self):
        with self.lock:
            return self.latest

    def _control_failed(self, exc):
        with self.lock:
            self.recovery_failures += 1
            self.busy_failures = self.busy_failures + 1 if isinstance(exc, G3PCommandError) and exc.code == -21 else 0
            self.power_cycle_required = self.busy_failures >= 3
            # Give a busy encoder time to settle. RTSP attempts continue so a
            # power-cycled camera can return before the control cooldown ends.
            delay = min(300, 30 * 2 ** min(self.recovery_failures-1, 4))
            self.next_control_at = time.monotonic() + delay
            self.control_error = (type(exc).__name__ + ": " + str(exc))[:200]
        log.warning("G3P encoder control unavailable: %s; retry in %ds", self.control_error, delay)

    def _command(self):
        c = self.config
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
        source = c["source"]
        if source.startswith("rtsp://"):
            # Match the older fielded reader: discard damaged/late packets and
            # let our outer frame deadline own reconnects instead of libav's
            # RTSP socket timeout.
            cmd += ["-rtsp_transport", "tcp", "-fflags", "nobuffer+discardcorrupt",
                    "-flags", "low_delay", "-avioflags", "direct", "-probesize", "32",
                    "-analyzeduration", "0", "-rtbufsize", "100k"]
        elif source.startswith("/dev/video"):
            cmd += ["-f", "v4l2", "-framerate", str(c["fps"]), "-video_size", f'{c["width"]}x{c["height"]}']
        else:
            cmd += ["-re"]
        # Keep decoded camera pixels for inference. Preview resizing/encoding
        # belongs to Runtime._process, after detection and local record capture.
        cmd += ["-i", source, "-an", "-sn", "-dn",
                "-vf", f'fps={c["fps"]}',
                "-vsync", "drop", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        return cmd

    def _probe(self):
        c = self.config
        cmd = ["ffprobe", "-v", "error"]
        if c["source"].startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp", rtsp_timeout_flag("ffprobe"), str(int(c["timeout_s"]*1000000))]
        if c["source"].startswith("/dev/video"):
            cmd += ["-f", "v4l2", "-video_size", f'{c["width"]}x{c["height"]}']
        cmd += ["-i", c["source"], "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json"]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with self.lock:
            self.process = process
        try:
            deadline = time.monotonic()+c["timeout_s"]+2
            while True:
                self._check_cancelled()
                if time.monotonic() > deadline:
                    raise TimeoutError("Camera probe timeout")
                try:
                    output, errors = process.communicate(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                self._check_cancelled()
                raise RuntimeError(errors.strip()[-300:].replace(c["source"], "camera"))
        finally:
            self._terminate(process)
            process.stdout.close()
            process.stderr.close()
            with self.lock:
                self.process = None
        streams = json.loads(output).get("streams", [])
        if not streams:
            raise RuntimeError("No video stream from camera")
        stream = streams[0]
        width, height = stream.get("width"), stream.get("height")
        if not all(type(v) is int and v > 0 for v in (width, height)):
            raise RuntimeError("Camera stream has no decodable video dimensions; waiting for encoder recovery")
        if abs(width/height - c["width"]/c["height"]) > 0.01:
            raise ValueError("CameraAspectRatioMismatch")
        if (width, height) != (c["width"], c["height"]):
            raise ValueError(f"CameraResolutionMismatch: source {width}x{height}, required {c['width']}x{c['height']}; refusing to resize inference frames")

    def _run(self):
        c = self.config
        while not self.stop_event.is_set():
            process = None
            stderr_thread = None
            planned = False
            errors = deque(maxlen=5)
            try:
                self.reconnect_event.clear()
                with self.lock:
                    self.attempts += 1
                    self.phase = "connecting"
                self._check_cancelled()
                # A new TCP session alone cannot recover this camera's stalled encoder.
                if (self.connections == 0 or self.error) and self.control and time.monotonic() >= self.next_control_at:
                    with self.lock:
                        self.phase = "recovering_encoder"
                    log.warning("Recovering G3P encoder: reset viewfinder, then wait 12 seconds")
                    try:
                        # The older application used the control port only for
                        # startup. Keep it out of the RTSP session lifetime.
                        self.control.start()
                        future = self.control.request_recovery()
                        deadline = time.monotonic()+35
                        try:
                            while True:
                                self._check_cancelled()
                                if time.monotonic() > deadline:
                                    raise TimeoutError("G3P encoder recovery timeout")
                                try:
                                    code = future.result(timeout=.25)
                                    break
                                except FutureTimeout:
                                    if future.done():
                                        raise
                        except BaseException:
                            future.cancel()
                            raise
                        if code != 0:
                            raise G3PCommandError('viewfinder reset', code)
                        with self.lock:
                            self.control_error = None
                            self.next_control_at = time.monotonic() + 30
                    except (OSError, RuntimeError, ValueError) as exc:
                        self._control_failed(exc)
                    finally:
                        self.control.stop()
                with self.lock:
                    self.phase = "connecting"
                # Check each new session: a restarted encoder can change its
                # dimensions; rawvideo has no framing metadata to catch that.
                self._probe()
                self._check_cancelled()
                process = subprocess.Popen(self._command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                with self.lock:
                    self.process = process
                    self.phase = "waiting_frame"
                def drain_errors(pipe):
                    for line in iter(pipe.readline, b""):
                        errors.append(line.decode(errors="replace").strip()[-300:].replace(c["source"], "camera"))
                stderr_thread = threading.Thread(target=drain_errors, args=(process.stderr,), daemon=True)
                stderr_thread.start()
                size = int(c["width"] * c["height"] * 3)
                first_frame = True
                refresh_at = None
                while not self.stop_event.is_set():
                    data = bytearray()
                    deadline = time.monotonic() + c["timeout_s"]
                    while len(data) < size and not self.stop_event.is_set():
                        self._check_cancelled()
                        if time.monotonic() > deadline:
                            raise TimeoutError("Camera frame timeout")
                        if not select.select([process.stdout], [], [], 0.25)[0]:
                            continue
                        block = os.read(process.stdout.fileno(), min(size - len(data), 1048576))
                        if not block:
                            raise EOFError("Camera stream ended: " + " | ".join(errors))
                        data.extend(block)
                    if self.stop_event.is_set():
                        break
                    at = time.monotonic()
                    frame = np.frombuffer(data, dtype=np.uint8).reshape(c["height"], c["width"], 3)
                    sample = {"frame": frame, "at": at, "timestamp": time.time(), "flight": self.telemetry.snapshot(at)}
                    with self.lock:
                        if self.reconnect_event.is_set():
                            raise ReconnectRequested()
                        if first_frame:
                            self.connections += 1
                            self.last_connected = time.time()
                            self.frame_times.clear()
                            log.info("Camera connected (%dx%d), session %d", c["width"], c["height"], self.connections)
                            first_frame = False
                            refresh_at = at + c.get("refresh_s", 0) if c.get("refresh_s", 0) else None
                        self.sequence += 1
                        self.latest = {**sample, "sequence": self.sequence, "epoch": self.epoch}
                        self.last_frame_at = sample["timestamp"]
                        self.frame_times.append(at)
                        self.phase = "streaming"
                        self.error = None
                        self.control_error = None
                        self.recovery_failures = self.busy_failures = 0
                        self.power_cycle_required = False
                    if refresh_at is not None and at >= refresh_at:
                        raise PlannedReconnect()
            except PlannedReconnect:
                planned = True
                with self.lock:
                    self.refreshes += 1
                log.info("Refreshing RTSP session before camera firmware timeout")
            except ReconnectRequested:
                pass
            except Exception as exc:
                with self.lock:
                    message = (type(exc).__name__ + ": " + str(exc))[:400]
                    if message != self.error:
                        log.warning("Camera unavailable: %s; retrying", message)
                    self.error = self.last_failure = message
            finally:
                with self.lock:
                    if not planned:
                        self.latest = None
                    self.epoch += 1
                    self.phase = "connecting" if planned else "retry_wait"
                if process:
                    self._terminate(process)
                    process.stdout.close()
                    if stderr_thread:
                        stderr_thread.join(timeout=2)
                    process.stderr.close()
                with self.lock:
                    self.process = None
            if not self.stop_event.is_set() and not planned:
                self.reconnect_event.wait(1)
            self.progress_at = time.monotonic()
        with self.lock:
            self.phase = "stopped"
