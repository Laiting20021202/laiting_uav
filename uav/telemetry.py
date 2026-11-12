"""Reconnectable MAVLink reader with companion heartbeat and telemetry requests."""
import copy
import math
import logging
import os
import threading
import time
from pathlib import Path
from serial.tools import list_ports

# Message 285 requires MAVLink 2. Keep NVIDIA/ROS's system Python unchanged.
os.environ["MAVLINK20"] = "1"
from pymavlink.dialects.v20 import ardupilotmega as mav2

log = logging.getLogger(__name__)

MESSAGE_RATES = {"GLOBAL_POSITION_INT": 10, "GPS_RAW_INT": 5, "ATTITUDE": 20,
                 "VFR_HUD": 2, "SYS_STATUS": 2, "DISTANCE_SENSOR": 5,
                 "GIMBAL_DEVICE_ATTITUDE_STATUS": 10}
REQUIRED_MESSAGES = tuple(MESSAGE_RATES)[:5]


def resolve_connection(config):
    """Re-resolve the same USB identity on every attempt; never guess an unrelated port."""
    connection = config["connection"]
    if not connection.startswith("/") or Path(connection).exists():
        return connection
    serial = config.get("usb_serial")
    if serial:
        matches = [p.device for p in list_ports.comports() if p.serial_number == serial
                   and (config.get("usb_vid") is None or p.vid == config["usb_vid"])
                   and (config.get("usb_pid") is None or p.pid == config["usb_pid"])]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError("Multiple USB interfaces match; configure the exact by-id path")
    raise FileNotFoundError("Flight-controller USB is not present; waiting for reconnect")


class ReconnectRequested(Exception):
    pass


def is_autopilot_heartbeat(msg, config, mavutil):
    if msg is None or msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID:
        return False
    if msg.type == mavutil.mavlink.MAV_TYPE_GCS:
        return False
    if config["system_id"] and msg.get_srcSystem() != config["system_id"]:
        return False
    if config.get("component_id", 1) and msg.get_srcComponent() != config.get("component_id", 1):
        return False
    return True


class Telemetry:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.reconnect_event = threading.Event()
        self.thread = None
        self.conn = None
        self.data = {"connected": False, "error": None}
        self.times = {}
        self.system = None
        self.component = None
        self.gimbal_source = None
        self.phase = "waiting"
        self.attempts = 0
        self.connections = 0
        self.last_connected = None
        self.last_message_at = None
        self.active_connection = None
        self.last_failure = None
        self.progress_at = time.monotonic()
        self.rate_queue = []
        self.rate_sent_at = {}
        self.next_rate_at = 0
        self.rate_requests = 0
        self.next_heartbeat_at = 0
        self.last_transport = {}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="mavlink-reader")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.request_reconnect()
        if self.thread:
            self.thread.join(timeout=12)

    def request_reconnect(self):
        self.reconnect_event.set()
        with self.lock:
            self.data = {"connected": False, "error": None}
            self.times.clear()
            self.phase = "reconnecting"

    def _check_cancelled(self):
        self.progress_at = time.monotonic()
        if self.stop_event.is_set() or self.reconnect_event.is_set():
            raise ReconnectRequested()

    def snapshot(self, at=None):
        now = time.monotonic() if at is None else at
        with self.lock:
            d = copy.deepcopy(self.data)
            d["ages_s"] = {key: round(now - value, 3) for key, value in self.times.items()}
            d.update(phase=self.phase, attempts=self.attempts, connections=self.connections,
                     last_connected=self.last_connected, last_message_at=self.last_message_at,
                     connection=self.active_connection, last_error=self.last_failure,
                     rate_requests=self.rate_requests, transport=self._transport_stats(self.conn))
        d["connected"] = bool(d["connected"] and 0 <= d["ages_s"].get("HEARTBEAT", math.inf) < self.config["heartbeat_timeout_s"])
        d["missing_messages"] = [name for name in REQUIRED_MESSAGES if not d["connected"]
                                 or not 0 <= d["ages_s"].get(name, math.inf) <= self.config["stale_s"]]
        d["complete"] = d["connected"] and not d["missing_messages"]
        return d

    def _transport_stats(self, conn):
        if conn is None:
            return dict(self.last_transport)
        return {name: getattr(conn.mav, attribute, 0) for name, attribute in (
            ("bytes_received", "total_bytes_received"), ("packets_received", "total_packets_received"),
            ("receive_errors", "total_receive_errors"), ("packets_sent", "total_packets_sent"))}

    def _heartbeat_alive(self):
        # The receive loop must not deep-copy all telemetry for every packet.
        with self.lock:
            age = time.monotonic() - self.times.get("HEARTBEAT", -math.inf)
            return self.data["connected"] and 0 <= age < self.config["heartbeat_timeout_s"]

    def consume(self, msg, mavutil):
        if msg.get_type() == "GIMBAL_DEVICE_ATTITUDE_STATUS":
            self.consume_gimbal(msg)
            return
        if msg.get_srcSystem() != self.system or msg.get_srcComponent() != self.component:
            return
        kind = msg.get_type()
        if kind == "DISTANCE_SENSOR" and msg.orientation != 25:
            return
        with self.lock:
            d = self.data
            self.times[kind] = time.monotonic()
            self.last_message_at = time.time()
            if kind == "HEARTBEAT":
                d.update(connected=True, error=None, system_id=self.system,
                         armed=bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED),
                         flight_mode=mavutil.mode_string_v10(msg))
            elif kind == "GLOBAL_POSITION_INT":
                d.update(latitude=msg.lat / 1e7, longitude=msg.lon / 1e7,
                         altitude_m=msg.alt / 1000, relative_altitude_m=msg.relative_alt / 1000,
                         vx=msg.vx / 100, vy=msg.vy / 100, vz=msg.vz / 100,
                         heading_deg=None if msg.hdg == 65535 else msg.hdg / 100)
            elif kind == "GPS_RAW_INT":
                d.update(fix_type=msg.fix_type, satellites=None if msg.satellites_visible == 255 else msg.satellites_visible,
                         hdop=None if msg.eph == 65535 else msg.eph / 100)
            elif kind == "ATTITUDE":
                d.update({k: float(getattr(msg, k)) if math.isfinite(getattr(msg, k)) else None for k in ("roll", "pitch", "yaw")})
            elif kind == "VFR_HUD":
                d.update({k: float(getattr(msg, k)) if math.isfinite(getattr(msg, k)) else None for k in ("groundspeed", "airspeed", "climb", "throttle")})
            elif kind == "SYS_STATUS":
                d.update(voltage_v=None if msg.voltage_battery == 65535 else msg.voltage_battery / 1000,
                         current_a=None if msg.current_battery == -1 else msg.current_battery / 100,
                         battery_pct=None if msg.battery_remaining == -1 else msg.battery_remaining)
            elif kind == "DISTANCE_SENSOR" and msg.orientation == 25:
                valid = msg.min_distance <= msg.current_distance < msg.max_distance and msg.current_distance > 0
                d["rangefinder_m"] = msg.current_distance / 100 if valid else None
            elif kind == "STATUSTEXT":
                d["message"] = str(msg.text)[:200]

    def consume_gimbal(self, msg):
        component = msg.get_srcComponent()
        device = getattr(msg, "gimbal_device_id", 0)
        source = (msg.get_srcSystem(), component, device)
        if source[0] != self.system:
            return
        if self.config["gimbal_component_id"] and component != self.config["gimbal_component_id"]:
            return
        if self.config["gimbal_device_id"] is not None and device != self.config["gimbal_device_id"]:
            return
        with self.lock:
            if self.gimbal_source is not None and source != self.gimbal_source:
                return
            self.gimbal_source = source
            self.times["GIMBAL_DEVICE_ATTITUDE_STATUS"] = time.monotonic()
            delta = getattr(msg, "delta_yaw", float("nan"))
            self.data["gimbal"] = {"q": [float(v) if math.isfinite(v) else None for v in msg.q],
                                   "flags": int(msg.flags), "failure_flags": int(msg.failure_flags),
                                   "delta_yaw": float(delta) if math.isfinite(delta) else None,
                                   "component_id": component, "device_id": device}

    @staticmethod
    def request_rate(conn, mavutil, name):
        conn.mav.command_long_send(conn.target_system, conn.target_component,
            mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            getattr(mav2, "MAVLINK_MSG_ID_" + name), 1000000 // MESSAGE_RATES[name],
            0, 0, 0, 0, 0)
        # mavserial.write swallows serial exceptions and sets portdead.
        if getattr(conn, "portdead", False):
            raise ConnectionError("Flight-controller transport write failed")

    @staticmethod
    def request_rates(conn, mavutil):
        for name in MESSAGE_RATES:
            Telemetry.request_rate(conn, mavutil, name)

    def _service_rates(self, conn, mavutil):
        """Send one request at a time; repair missing streams without reopening USB."""
        now = time.monotonic()
        if now < self.next_rate_at:
            return
        with self.lock:
            if self.rate_queue:
                name = self.rate_queue.pop(0)
            else:
                # Optional sensors may not exist; don't keep requesting them.
                name = next((key for key in REQUIRED_MESSAGES
                             if now - self.times.get(key, -math.inf) > self.config["stale_s"]
                             and now - self.rate_sent_at.get(key, -math.inf)
                             >= self.config.get("rate_retry_s", 5)), None)
            if name is None:
                return
        self._check_cancelled()
        self.request_rate(conn, mavutil, name)
        with self.lock:
            self.rate_sent_at[name] = now
            self.rate_requests += 1
        self.next_rate_at = time.monotonic() + .2

    def _service_heartbeat(self, conn, mavutil):
        """Advertise this receiving companion, from the healthy reader itself."""
        now = time.monotonic()
        if now < self.next_heartbeat_at:
            return
        conn.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
                               mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0,
                               mavutil.mavlink.MAV_STATE_ACTIVE)
        if getattr(conn, "portdead", False):
            raise ConnectionError("Flight-controller transport write failed")
        self.next_heartbeat_at = time.monotonic() + 1

    def _run(self):
        from pymavlink import mavutil
        mavutil.set_dialect("ardupilotmega")
        while not self.stop_event.is_set():
            conn = None
            try:
                self.reconnect_event.clear()
                self._check_cancelled()
                with self.lock:
                    self.attempts += 1
                    self.last_transport = {}
                    self.phase = "waiting_usb" if self.config["connection"].startswith("/") else "connecting"
                connection = resolve_connection(self.config)
                conn = mavutil.mavlink_connection(connection, baud=self.config["baud"],
                                                   source_system=245, source_component=191,
                                                   autoreconnect=False)
                self.conn = conn
                if isinstance(conn, mavutil.mavserial):
                    # pySerial writes block forever by default, even though reads
                    # are nonblocking. Bound interval requests after USB failure.
                    conn.port.write_timeout = 1
                    conn.port.exclusive = True
                with self.lock:
                    self.active_connection = connection
                    self.phase = "waiting_heartbeat"
                deadline = time.monotonic() + self.config["heartbeat_timeout_s"]
                self.next_heartbeat_at = 0
                while not self.stop_event.is_set():
                    self._check_cancelled()
                    if time.monotonic() > deadline:
                        raise TimeoutError("No flight-controller heartbeat")
                    self._service_heartbeat(conn, mavutil)
                    msg = conn.recv_match(type="HEARTBEAT", blocking=True, timeout=.25)
                    self._check_cancelled()
                    if not is_autopilot_heartbeat(msg, self.config, mavutil):
                        continue
                    self.system, self.component = msg.get_srcSystem(), msg.get_srcComponent()
                    conn.target_system, conn.target_component = self.system, self.component
                    with self.lock:
                        self.data = {"connected": True, "error": None}
                        self.times = {}
                        self.gimbal_source = None
                        self.rate_queue = list(MESSAGE_RATES)
                        self.rate_sent_at.clear()
                        self.next_rate_at = 0
                        self.rate_requests = 0
                        self.last_failure = None
                        self.connections += 1
                        self.last_connected = time.time()
                        self.phase = "connected"
                    self.consume(msg, mavutil)
                    log.info("Flight controller connected: %s system=%s component=%s, session %d", connection, self.system, self.component, self.connections)
                    break
                if self.stop_event.is_set():
                    break
                integrity_at = 0
                integrity_previous = None
                integrity_logged_at = 0
                while not self.stop_event.is_set():
                    self._check_cancelled()
                    if getattr(conn, "portdead", False):
                        raise ConnectionError("Flight-controller transport is dead")
                    if not self._heartbeat_alive():
                        raise TimeoutError("Flight-controller heartbeat expired")
                    self._service_heartbeat(conn, mavutil)
                    self._service_rates(conn, mavutil)
                    msg = conn.recv_match(blocking=True, timeout=0.25)
                    self._check_cancelled()
                    # Expire the previous session before accepting a late heartbeat;
                    # otherwise a just-returned packet can revive stale session data.
                    if not self._heartbeat_alive():
                        raise TimeoutError("Flight-controller heartbeat expired")
                    if msg is not None and msg.get_type() != "BAD_DATA":
                        self.consume(msg, mavutil)
                    now = time.monotonic()
                    if now >= integrity_at:
                        state = self.snapshot()
                        missing = tuple(state["missing_messages"])
                        if missing != integrity_previous or now - integrity_logged_at >= 30:
                            log.info("Flight telemetry integrity: session=%d complete=%s missing=%s ages_s=%s",
                                     self.connections, state["complete"], ",".join(missing) or "none",
                                     {name: state["ages_s"].get(name) for name in ("HEARTBEAT", *REQUIRED_MESSAGES)})
                            integrity_previous, integrity_logged_at = missing, now
                        integrity_at = now + 2
            except ReconnectRequested:
                pass
            except Exception as exc:
                with self.lock:
                    message = type(exc).__name__ + ": " + str(exc)
                    if message != self.last_failure:
                        log.warning("Flight controller unavailable: %s; transport=%s; retrying",
                                    message, self._transport_stats(conn))
                    self.last_failure = message
                    self.data = {"connected": False, "error": message}
                    self.times.clear()
                    self.phase = "waiting_usb" if isinstance(exc, FileNotFoundError) else "retry_wait"
            finally:
                with self.lock:
                    self.last_transport = self._transport_stats(conn)
                    self.data = {"connected": False, "error": self.data.get("error")}
                    self.times.clear()
                    self.system = self.component = self.gimbal_source = None
                    self.active_connection = None
                try:
                    if conn:
                        conn.close()
                except Exception:
                    # A failed device must not kill the only reconnect worker.
                    log.warning("Failed to close flight-controller transport; continuing recovery", exc_info=True)
                finally:
                    self.conn = None
            if not self.stop_event.is_set():
                self.reconnect_event.wait(self.config.get("retry_s", 1))
            self.progress_at = time.monotonic()
        with self.lock:
            self.phase = "stopped"
