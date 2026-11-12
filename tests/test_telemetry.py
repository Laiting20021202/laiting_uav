"""MAVLink source isolation, freshness, and the outbound-command allowlist."""
import time
import socket
import os
import pty
from types import SimpleNamespace
from pymavlink import mavutil
from uav.config import load_settings
from uav.telemetry import Telemetry
from uav.telemetry import resolve_connection
from uav.telemetry import is_autopilot_heartbeat
import pytest


def message(kind, system=1, component=1, **data):
    return SimpleNamespace(get_type=lambda:kind,get_srcSystem=lambda:system,get_srcComponent=lambda:component,**data)


def test_source_filter_and_stale_heartbeat():
    t=Telemetry(load_settings()['telemetry']);t.system=1;t.component=1
    t.consume(message('GLOBAL_POSITION_INT',system=2,lat=250000000,lon=1210000000),mavutil)
    assert 'latitude' not in t.snapshot()
    t.data['connected']=True;t.times['HEARTBEAT']=time.monotonic()-10
    assert not t.snapshot()['connected']


def test_other_rangefinder_does_not_refresh_downward_sample():
    t=Telemetry(load_settings()['telemetry']);t.system=1;t.component=1
    t.times['DISTANCE_SENSOR']=0;t.data['rangefinder_m']=60
    t.consume(message('DISTANCE_SENSOR',orientation=0,current_distance=200,min_distance=10,max_distance=300),mavutil)
    assert t.times['DISTANCE_SENSOR']==0
    t.consume(message('DISTANCE_SENSOR',orientation=25,current_distance=300,min_distance=10,max_distance=300),mavutil)
    assert t.data['rangefinder_m'] is None


def test_only_telemetry_interval_commands_are_sent():
    commands=[]
    conn=SimpleNamespace(target_system=1,target_component=1,mav=SimpleNamespace(command_long_send=lambda *args:commands.append(args)))
    Telemetry.request_rates(conn,mavutil)
    assert len(commands)==7
    assert {args[2] for args in commands}=={mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL}


def test_gimbal_source_pinning():
    t=Telemetry(load_settings()['telemetry']);t.system=1;t.component=1
    d=dict(q=[1,0,0,0],flags=64,failure_flags=0,gimbal_device_id=0)
    t.consume(message('GIMBAL_DEVICE_ATTITUDE_STATUS',component=154,**d),mavutil)
    assert t.snapshot()['gimbal']['component_id']==154
    t.consume(message('GIMBAL_DEVICE_ATTITUDE_STATUS',component=155,**d),mavutil)
    assert t.snapshot()['gimbal']['component_id']==154


@pytest.mark.parametrize('close_error', [False, True])
def test_real_udp_mavlink_reader_and_heartbeat_expiry(monkeypatch, close_error):
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    config=load_settings()['telemetry'];config.update(connection=f'udpin:127.0.0.1:{port}',heartbeat_timeout_s=2)
    original_open=mavutil.mavlink_connection
    failed_close=[]
    def open_connection(connection, **kwargs):
        conn=original_open(connection, **kwargs)
        if connection.startswith('udpin:') and close_error:
            original_close=conn.close
            def close():
                original_close()
                if not failed_close:
                    failed_close.append(True)
                    raise OSError('Disconnected transport failed during close')
            conn.close=close
        return conn
    monkeypatch.setattr(mavutil, 'mavlink_connection', open_connection)
    reader=Telemetry(config);reader.start()
    sender=mavutil.mavlink_connection(f'udpout:127.0.0.1:{port}',source_system=1,source_component=1)
    try:
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            sender.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_QUADROTOR,mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,0,0,3)
            sender.mav.global_position_int_send(1000,250000000,1210000000,60000,60000,0,0,0,0)
            if reader.snapshot().get('latitude')==25:
                break
            time.sleep(.1)
        state=reader.snapshot()
        assert state['connected'] and state['latitude']==25 and state['longitude']==121
        deadline=time.monotonic()+4
        while reader.snapshot()['connected'] and time.monotonic()<deadline:
            time.sleep(.1)
        assert not reader.snapshot()['connected']
        old_count=reader.connections
        deadline=time.monotonic()+7
        while time.monotonic()<deadline:
            sender.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_QUADROTOR,mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,0,0,3)
            sender.mav.global_position_int_send(2000,260000000,1220000000,70000,70000,0,0,0,0)
            if reader.snapshot().get('latitude')==26:
                break
            time.sleep(.1)
        assert reader.snapshot()['connected'] and reader.snapshot()['latitude']==26
        assert reader.connections==old_count+1
        reader.request_reconnect()
        assert not reader.snapshot()['connected'] and not reader.snapshot()['ages_s']
    finally:
        sender.close();reader.stop()
    assert not reader.thread.is_alive()
    if close_error:
        assert failed_close


def test_real_serial_device_disappears_and_returns_with_new_tty(tmp_path):
    """Exercise pySerial with real PTYs, including removal and changed device names."""
    link=tmp_path/'flight-controller'
    config=load_settings()['telemetry']
    config.update(connection=str(link), usb_serial=None, heartbeat_timeout_s=2)
    reader=Telemetry(config)
    master=slave=None
    def send_position(latitude):
        heartbeat=mavutil.mavlink.MAVLink_heartbeat_message(
            mavutil.mavlink.MAV_TYPE_QUADROTOR,
            mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, 3, 3)
        position=mavutil.mavlink.MAVLink_global_position_int_message(
            1000, latitude*10000000, 1210000000, 60000, 60000, 0, 0, 0, 0)
        encoder=mavutil.mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
        os.write(master, heartbeat.pack(encoder)+position.pack(encoder))
    def await_position(latitude):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            send_position(latitude)
            state=reader.snapshot()
            if state['connected'] and state.get('latitude')==latitude:
                return
            time.sleep(.05)
        pytest.fail(str(reader.snapshot()))
    try:
        master,slave=pty.openpty()
        link.symlink_to(os.ttyname(slave))
        reader.start()
        await_position(25)
        # A stalled outgoing interval request must not hang the reconnect worker.
        assert 0 < reader.conn.port.write_timeout <= 1
        for cycle in range(3):
            old_path=os.ttyname(slave)
            link.unlink()
            os.close(master);master=None
            deadline=time.monotonic()+3
            while reader.snapshot()['connected'] and time.monotonic()<deadline:
                time.sleep(.02)
            state=reader.snapshot()
            assert not state['connected'] and not state['ages_s']
            assert 'latitude' not in state and reader.thread.is_alive()
            old_slave=slave
            master,slave=pty.openpty()
            os.close(old_slave)
            assert os.ttyname(slave)!=old_path
            link.symlink_to(os.ttyname(slave))
            await_position(26+cycle)
            assert reader.connections==cycle+2
    finally:
        reader.stop()
        for fd in (master,slave):
            if fd is not None:
                os.close(fd)
    assert not reader.thread.is_alive()


def test_interval_write_failure_is_not_silently_accepted():
    conn=SimpleNamespace(target_system=1, target_component=1, portdead=False)
    def fail_write(*args):
        # pymavlink's serial writer records the error instead of raising it.
        conn.portdead=True
    conn.mav=SimpleNamespace(command_long_send=fail_write)
    with pytest.raises(ConnectionError, match='write failed'):
        Telemetry.request_rates(conn, mavutil)


def test_usb_identity_survives_device_number_change(tmp_path,monkeypatch):
    c=load_settings()['telemetry'];c['connection']=str(tmp_path/'missing-by-id')
    c.update(usb_vid=1, usb_pid=2)
    monkeypatch.setattr('uav.telemetry.list_ports.comports',lambda:[SimpleNamespace(device='/dev/ttyACM1',serial_number=c['usb_serial'],vid=1,pid=2)])
    assert resolve_connection(c)=='/dev/ttyACM1'
    monkeypatch.setattr('uav.telemetry.list_ports.comports',lambda:[SimpleNamespace(device='/dev/ttyACM0',serial_number='unrelated',vid=1,pid=2)])
    with pytest.raises(FileNotFoundError):resolve_connection(c)
    monkeypatch.setattr('uav.telemetry.list_ports.comports',lambda:[])
    with pytest.raises(FileNotFoundError):resolve_connection(c)


def test_routed_gcs_heartbeat_never_selected_as_flight_controller():
    c=load_settings()['telemetry']
    gcs=message('HEARTBEAT',system=255,component=235,autopilot=3,type=mavutil.mavlink.MAV_TYPE_GCS)
    assert not is_autopilot_heartbeat(gcs,c,mavutil)
    fc=message('HEARTBEAT',system=1,component=1,autopilot=3,type=mavutil.mavlink.MAV_TYPE_QUADROTOR)
    assert is_autopilot_heartbeat(fc,c,mavutil)


def test_companion_heartbeat_is_bounded_and_not_a_flight_command(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('uav.telemetry.time.monotonic', lambda: now[0])
    packets = []
    conn = SimpleNamespace(portdead=False,
                           mav=SimpleNamespace(heartbeat_send=lambda *args: packets.append(args)))
    reader = Telemetry(load_settings()['telemetry'])
    reader._service_heartbeat(conn, mavutil)
    reader._service_heartbeat(conn, mavutil)
    now[0] += 1
    reader._service_heartbeat(conn, mavutil)
    assert packets == [(mavutil.mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
                        mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0,
                        mavutil.mavlink.MAV_STATE_ACTIVE)] * 2
    now[0] += 1
    conn.portdead = True
    with pytest.raises(ConnectionError):
        reader._service_heartbeat(conn, mavutil)


def test_missing_stream_is_retried_without_usb_reopen(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('uav.telemetry.time.monotonic', lambda: now[0])
    from uav.telemetry import REQUIRED_MESSAGES
    from pymavlink.dialects.v20 import ardupilotmega as mav2
    commands = []
    conn = SimpleNamespace(target_system=1, target_component=1,
                           mav=SimpleNamespace(command_long_send=lambda *args: commands.append(args)))
    reader = Telemetry(load_settings()['telemetry'])
    reader.data['connected'] = True
    reader.times = {name: now[0] for name in (*REQUIRED_MESSAGES, 'HEARTBEAT')}
    reader.times.pop('SYS_STATUS')
    assert reader.snapshot()['missing_messages'] == ['SYS_STATUS']
    reader._service_rates(conn, mavutil)
    assert len(commands) == 1 and commands[0][4] == mav2.MAVLINK_MSG_ID_SYS_STATUS
    now[0] += 1
    reader._service_rates(conn, mavutil)
    assert len(commands) == 1
    now[0] += 5
    reader.times.update({name: now[0] for name in (*REQUIRED_MESSAGES, 'HEARTBEAT') if name != 'SYS_STATUS'})
    reader._service_rates(conn, mavutil)
    assert len(commands) == 2
    reader.times['SYS_STATUS'] = now[0]
    assert reader.snapshot()['complete']
    now[0] += .3
    reader._service_rates(conn, mavutil)
    assert len(commands) == 2  # Optional sensors do not create endless retries.


def test_initial_rate_requests_are_spaced_between_receives(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('uav.telemetry.time.monotonic', lambda: now[0])
    from uav.telemetry import MESSAGE_RATES
    commands = []
    conn = SimpleNamespace(target_system=1, target_component=1,
                           mav=SimpleNamespace(command_long_send=lambda *args: commands.append(args)))
    reader = Telemetry(load_settings()['telemetry'])
    reader.rate_queue = list(MESSAGE_RATES)
    reader._service_rates(conn, mavutil)
    reader._service_rates(conn, mavutil)
    assert len(commands) == 1
    now[0] += .25
    reader._service_rates(conn, mavutil)
    assert len(commands) == 2


def test_usb_can_appear_after_offline_boot(tmp_path):
    link = tmp_path / 'late-controller'
    config = load_settings()['telemetry']
    config.update(connection=str(link), usb_serial=None, heartbeat_timeout_s=2, retry_s=.1)
    reader = Telemetry(config)
    master = slave = None
    reader.start()
    try:
        deadline = time.monotonic() + 2
        while reader.attempts < 2 and time.monotonic() < deadline:
            time.sleep(.02)
        assert reader.thread.is_alive() and reader.snapshot()['phase'] == 'waiting_usb'
        master, slave = pty.openpty()
        link.symlink_to(os.ttyname(slave))
        encoder = mavutil.mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
        heartbeat = mavutil.mavlink.MAVLink_heartbeat_message(
            mavutil.mavlink.MAV_TYPE_QUADROTOR,
            mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, 3, 3)
        deadline = time.monotonic() + 3
        while not reader.snapshot()['connected'] and time.monotonic() < deadline:
            os.write(master, heartbeat.pack(encoder))
            time.sleep(.03)
        assert reader.snapshot()['connected'] and reader.connections == 1
    finally:
        reader.stop()
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
