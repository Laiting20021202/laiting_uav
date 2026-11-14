"""Offline conversion/validity tests; fixtures are not published as live data."""
import json
import math
import sys
from pathlib import Path
import time
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ros2'))
from uav_ros.convert import convert, attitude
from uav_ros.contract import inspect
from uav_ros.monitor import Monitor

@pytest.fixture
def config():
    return json.loads((Path(__file__).resolve().parents[1]/'ros2/config/settings.json').read_text())

def event(kind, length=30, **fields):
    return dict(received_ns=time.time_ns(),payload_length=length,message=dict(mavpackettype=kind,**fields))

def test_ned_frd_attitude_to_enu_flu():
    w,x,y,z=attitude(0,0,0)
    assert abs(w)==pytest.approx(2**-.5)
    assert abs(z)==pytest.approx(2**-.5)
    assert x==pytest.approx(0) and y==pytest.approx(0)
    assert 2*math.atan2(z,w) % (2*math.pi)==pytest.approx(math.pi/2)

def test_flight_units_and_velocity(config):
    out=convert(event('GLOBAL_POSITION_INT',relative_alt=1234,alt=54321,vx=123,vy=-456,vz=78),config)
    assert out['/uav/altitude/relative_home'].data==1.234
    assert out['/uav/altitude/msl'].data==54.321
    v=out['/uav/velocity/enu'].vector
    assert [v.x,v.y,v.z]==[ -4.56,1.23,-.78]

@pytest.mark.parametrize('length,alt,valid',[(30,0,False),(34,0,False),(34,12345,True)])
def test_gps_never_substitutes_msl(config,length,alt,valid):
    out=convert(event('GPS_RAW_INT',length,fix_type=3,lat=250000000,lon=1210000000,alt=67890,alt_ellipsoid=alt),config)
    m=out['/uav/gps/fix']
    assert math.isfinite(m.altitude)==valid
    assert inspect('/uav/gps/fix',m)[0]==valid
    if valid:assert m.altitude==12.345

def test_no_gps_fix_is_received_but_invalid(config):
    m=convert(event('GPS_RAW_INT',34,fix_type=1,lat=0,lon=0,alt=0,alt_ellipsoid=0),config)['/uav/gps/fix']
    monitor=Monitor(config)
    monitor.sources.update(bridge=True,flight={'connected':True})
    monitor.receive('/uav/gps/fix',m)
    r=next(r for r in monitor.snapshot()['topics'] if r['name'].endswith('/fix'))
    assert r['count']==1 and r['invalid_count']==1 and r['state']=='invalid'
    assert math.isnan(m.latitude) and m.status.status==-1

def test_scaled_imu_has_no_fake_orientation(config):
    m=convert(event('SCALED_IMU',xacc=1000,yacc=2000,zacc=-1000,xgyro=1000,ygyro=2000,zgyro=-3000,xmag=100,ymag=-200,zmag=300),config)
    imu=m['/uav/imu/data_raw']
    assert imu.orientation_covariance[0]==-1
    assert [imu.orientation.x,imu.orientation.y,imu.orientation.z,imu.orientation.w]==[0,0,0,0]
    assert [imu.linear_acceleration.x,imu.linear_acceleration.y,imu.linear_acceleration.z]==[9.80665,-19.6133,9.80665]
    assert [imu.angular_velocity.x,imu.angular_velocity.y,imu.angular_velocity.z]==[1,-2,3]
    mag=m['/uav/imu/mag'].magnetic_field
    assert [mag.x,mag.y,mag.z]==pytest.approx([1e-5,2e-5,-3e-5])

def test_raw_imu_without_units_is_not_published(config):
    assert convert(event('RAW_IMU',xacc=1),config)=={}

def test_highres_updated_fields_required(config):
    config.update(imu_source='HIGHRES_IMU',mag_source='HIGHRES_IMU',pressure_source='HIGHRES_IMU')
    assert convert(event('HIGHRES_IMU',fields_updated=0),config)=={}

def test_battery_unknowns_are_nan_and_current_is_discharge(config):
    m=convert(event('SYS_STATUS',voltage_battery=24000,current_battery=123,battery_remaining=-1),config)['/uav/battery/state']
    assert m.voltage==24 and m.current==pytest.approx(-1.23)
    assert math.isnan(m.percentage) and math.isnan(m.temperature)
    assert not inspect('/uav/battery/state',m)[0]

def test_pressure_hpa_to_pa(config):
    m=convert(event('SCALED_PRESSURE',press_abs=1013.25),config)['/uav/barometer/pressure']
    assert m.fluid_pressure==101325

def test_monitor_wait_timeout_disconnect_are_distinct(config):
    from std_msgs.msg import Float64
    t='/uav/altitude/msl';monitor=Monitor(config)
    monitor.sources.update(bridge=True,flight={'connected':True})
    row=lambda:next(r for r in monitor.snapshot()['topics'] if r['name']==t)
    assert row()['state']=='waiting'
    monitor.receive(t,Float64(data=10.0))
    assert row()['state']=='normal'
    monitor.rows[t]['last']-=10
    assert row()['state']=='timeout' and not row()['live']
    monitor.sources['flight']['connected']=False
    assert row()['state']=='disconnected'

def test_monitor_snapshot_is_strict_json(config):
    from uav_ros.contract import clean
    m=convert(event('SCALED_IMU',xacc=1000,yacc=0,zacc=0,xgyro=0,ygyro=0,zgyro=0,xmag=1,ymag=1,zmag=1),config)
    monitor=Monitor(config)
    for t,value in m.items():monitor.receive(t,value)
    json.dumps(clean(monitor.snapshot()),allow_nan=False)


def make_bag(path, count=1, metadata_count=None, typ='std_msgs/msg/Float64'):
    import sqlite3
    import yaml
    from rclpy.serialization import serialize_message
    from std_msgs.msg import Float64
    path.mkdir()
    with sqlite3.connect(path/'test.db3') as conn:
        conn.execute('CREATE TABLE topics(id INTEGER,name TEXT,type TEXT)')
        conn.execute('CREATE TABLE messages(topic_id INTEGER,timestamp INTEGER,data BLOB)')
        conn.execute('INSERT INTO topics VALUES(1,?,?)',('/uav/altitude/msl',typ))
        for _ in range(count):conn.execute('INSERT INTO messages VALUES(1,1000000000,?)',(serialize_message(Float64(data=12.3)),))
    metadata_count=count if metadata_count is None else metadata_count
    meta={'rosbag2_bagfile_information':dict(relative_file_paths=['test.db3'],
        topics_with_message_count=[dict(topic_metadata=dict(name='/uav/altitude/msl',type=typ),message_count=metadata_count)],
        message_count=metadata_count,starting_time={'nanoseconds_since_epoch':1000000000},duration={'nanoseconds':0})}
    (path/'metadata.yaml').write_text(yaml.safe_dump(meta))
    return path


def test_bag_validation_counts_and_missing(tmp_path):
    from uav_ros.bags import validate
    result=validate(make_bag(tmp_path/'bag'))
    assert result['message_count']==1 and result['topic_count']==1
    assert result['integrity']=='partial' and len(result['missing'])==9

@pytest.mark.parametrize('kwargs,reason',[
    ({'metadata_count':2},'count mismatch'),
    ({'typ':'std_msgs/msg/String'},'type mismatch'),
    ({'count':0},'empty bag')])
def test_bag_rejects_invalid_metadata(tmp_path,kwargs,reason):
    from uav_ros.bags import validate
    with pytest.raises(ValueError,match=reason):validate(make_bag(tmp_path/'bag',**kwargs))


def test_bag_empty_disk_and_interrupted_jobs_are_not_ready(tmp_path,config):
    from uav_ros.bags import Bags,save
    config['data_dir']=str(tmp_path)
    folder=tmp_path/'recordings'/'interrupted';folder.mkdir(parents=True)
    save(folder/'job.json',dict(name='interrupted',state='recording'))
    bags=Bags(config,Monitor(config))
    try:
        assert bags.history()[0]['state']=='failed'
        with pytest.raises(ValueError,match='not a validated'):bags.download('interrupted')
        bags.disk=lambda:dict(free_mb=0)
        with pytest.raises(ValueError,match='insufficient'):bags.start()
        assert bags.job is None
    finally:bags.close()


def test_only_one_start_or_stop_can_run(tmp_path,config,monkeypatch):
    from uav_ros.bags import Bags
    from types import SimpleNamespace
    config['data_dir']=str(tmp_path)
    calls=[]
    def spawn(*a,**kw):
        calls.append(a)
        return SimpleNamespace(pid=123456789,poll=lambda:None)
    monkeypatch.setattr('uav_ros.bags.subprocess.Popen',spawn)
    bags=Bags(config,Monitor(config))
    try:
        bags.start()
        with pytest.raises(ValueError,match='already in progress'):bags.start()
        assert len(calls)==1
        bags.job['state']='packaging'
        with pytest.raises(ValueError,match='already in progress'):bags.stop()
        bags.job['state']='failed'
    finally:
        if bags.logfile:bags.logfile.close()
        bags.close()


@pytest.mark.parametrize('state',['failed','ready','validating','packaging'])
def test_inactive_recording_without_stop_time_never_keeps_timing(state):
    from uav_ros.bags import elapsed_seconds
    job=dict(state=state,started_at=1000)
    assert elapsed_seconds(job,now=2000) is None
    assert elapsed_seconds(job,now=200000) is None


@pytest.mark.parametrize('state',['starting','recording','stopping'])
def test_only_active_recorders_have_a_growing_elapsed_time(state):
    from uav_ros.bags import elapsed_seconds
    job=dict(state=state,started_at=1000)
    assert elapsed_seconds(job,now=1005)==5
    assert elapsed_seconds(job,now=1006)==6


def test_known_stop_time_stays_fixed_including_zero_duration():
    from uav_ros.bags import elapsed_seconds
    job=dict(state='failed',started_at=1000,stopped_at=1005)
    assert elapsed_seconds(job,now=2000)==5
    assert elapsed_seconds(job,now=200000)==5
    job['stopped_at']=1000
    assert elapsed_seconds(job,now=200000)==0


def test_recovery_does_not_count_downtime_as_recording(tmp_path,config,monkeypatch):
    from uav_ros.bags import Bags,save
    config['data_dir']=str(tmp_path)
    folder=tmp_path/'recordings'/'interrupted';folder.mkdir(parents=True)
    save(folder/'job.json',dict(name='interrupted',state='recording',started_at=1000))
    monkeypatch.setattr('uav_ros.bags.time.time',lambda:200000)
    bags=Bags(config,Monitor(config))
    try:
        row=bags.history()[0]
        assert row['state']=='failed' and row['elapsed_s'] is None
        assert row['interrupted_detected_at']==200000
        assert row.get('stopped_at') is None
        monkeypatch.setattr('uav_ros.bags.time.time',lambda:300000)
        assert bags.history()[0]['elapsed_s'] is None
        bags.job=row
        assert bags.status()['current']['elapsed_s'] is None
    finally:bags.close()


def test_finalization_failure_does_not_extend_recording_time(tmp_path,config,monkeypatch):
    import io
    from types import SimpleNamespace
    from uav_ros.bags import Bags,elapsed_seconds
    config['data_dir']=str(tmp_path)
    clock=[100]
    monkeypatch.setattr('uav_ros.bags.time.time',lambda:clock[0])
    monkeypatch.setattr('uav_ros.bags.os.killpg',lambda *args:None)
    def fail_validation(path):
        clock[0]=200
        raise ValueError('invalid metadata')
    monkeypatch.setattr('uav_ros.bags.validate',fail_validation)
    bags=Bags(config,Monitor(config))
    try:
        (bags.root/'test').mkdir()
        bags.job=dict(name='test',state='stopping',started_at=10)
        bags.process=SimpleNamespace(pid=123456789,poll=lambda:None,wait=lambda **kwargs:0)
        bags.logfile=io.BytesIO()
        bags._finish()
        assert bags.job['state']=='failed'
        assert bags.job['stopped_at']==100
        assert elapsed_seconds(bags.job,now=200000)==90
    finally:bags.close()
