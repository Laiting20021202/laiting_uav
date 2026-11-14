"""Opt-in, local-only, read-only tap. Hardware remains owned by Runtime."""
import json
import os
import socketserver
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class SourceBridge:
    def __init__(self, runtime, path):
        self.runtime, self.path = runtime, Path(path)
        self.lock = threading.Lock()
        self.events = deque(maxlen=4096)
        self.sequence = 0
        self.server = None

    def consume(self, msg, received_ns):
        # Called only after the existing autopilot system/component filter.
        payload = msg.to_dict()
        if payload['mavpackettype'] not in {
            'HEARTBEAT', 'GLOBAL_POSITION_INT', 'GPS_RAW_INT', 'ATTITUDE',
            'ATTITUDE_QUATERNION', 'SYS_STATUS', 'SCALED_IMU', 'HIGHRES_IMU',
            'SCALED_PRESSURE', 'COMMAND_ACK'}:
            return
        wire = msg.get_msgbuf()
        with self.lock:
            self.sequence += 1
            self.events.append(dict(seq=self.sequence, received_ns=received_ns,
                                    payload_length=int(wire[1]) if wire else 0,
                                    message=payload))

    def start(self):
        bridge = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.connection.settimeout(3)
                parsed = urlsplit(self.path)
                if parsed.path == '/credentials':
                    # Private socket only: read current hashes after atomic password
                    # changes; a Docker file bind would retain the old inode.
                    from .config import ROOT
                    body = (ROOT/'config/users.json').read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                elif parsed.path == '/events':
                    after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                    with bridge.lock:
                        rows = [e for e in bridge.events if e['seq'] > after]
                        oldest = bridge.events[0]['seq'] if bridge.events else 0
                    body = json.dumps(dict(boot_id=bridge.runtime.boot_id, events=rows,
                        oldest=oldest, latest=bridge.sequence, now_ns=time.time_ns(),
                        flight=bridge.runtime.telemetry.snapshot(),
                        camera=bridge.runtime.camera.snapshot()), allow_nan=True).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                elif parsed.path == '/frame':
                    sample = bridge.runtime.camera.get()
                    after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                    if (not sample or sample['sequence'] <= after
                            or time.monotonic()-sample['at'] > 2):
                        self.send_response(204)
                        self.end_headers()
                        return
                    # Lossless BGR transport of the existing unannotated decoded frame.
                    frame = sample['frame']
                    body = frame.tobytes()
                    self.send_response(200)
                    for k, v in dict(Sequence=sample['sequence'], Width=frame.shape[1],
                                     Height=frame.shape[0], TimestampNs=int(sample['timestamp']*1e9)).items():
                        self.send_header('X-'+k, str(v))
                    self.send_header('Content-Type', 'application/octet-stream')
                else:
                    self.send_error(404)
                    return
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        self.server = Server(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self.runtime.telemetry.raw_observer = self.consume
        threading.Thread(target=self.server.serve_forever, daemon=True, name='ros-source-tap').start()

    def stop(self):
        self.runtime.telemetry.raw_observer = None
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.path.unlink(missing_ok=True)
