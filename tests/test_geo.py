"""Known-axis projection and stale/incomplete telemetry rejection."""
import math
import pytest
from uav.config import load_settings
from uav.geo import locate
from uav.geo import to_twd97


@pytest.fixture
def geo():
    c=load_settings()['geolocation']
    c.update(calibrated=True,fx=1000,fy=1000,ground_altitude_relative_m=0,orientation_source='fixed',
             estimated_focal_ratio=None,assume_ground_at_takeoff=False)
    return c


@pytest.fixture
def flight():
    return dict(connected=True,latitude=25,longitude=121,roll=0,pitch=0,yaw=0,relative_altitude_m=60,fix_type=6,
                ages_s={'GLOBAL_POSITION_INT':.1,'GPS_RAW_INT':.1,'ATTITUDE':.1})


def test_nadir_center_matches_aircraft(geo,flight):
    g=locate((960,540),(1920,1080),flight,geo)
    assert g['valid']
    assert g['latitude']==pytest.approx(25)
    assert g['longitude']==pytest.approx(121)


def test_right_pixel_points_east_then_south_after_yaw(geo,flight):
    g=locate((1060,540),(1920,1080),flight,geo)
    assert g['east_m']==pytest.approx(6)
    assert g['north_m']==pytest.approx(0,abs=1e-10)
    flight['yaw']=math.pi/2
    g=locate((1060,540),(1920,1080),flight,geo)
    assert g['north_m']==pytest.approx(-6)
    assert g['east_m']==pytest.approx(0,abs=1e-10)


@pytest.mark.parametrize('kind',['GLOBAL_POSITION_INT','GPS_RAW_INT','ATTITUDE'])
def test_reject_stale_and_future_data(geo,flight,kind):
    for age in (3,-.1):
        flight['ages_s'][kind]=age
        assert not locate((960,540),(1920,1080),flight,geo)['valid']


def test_missing_calibration_and_height_and_fix(geo,flight):
    geo['calibrated']=False
    assert locate((960,540),(1920,1080),flight,geo)['reason']=='camera_not_calibrated'
    geo['calibrated']=True;geo['ground_altitude_relative_m']=None
    assert locate((960,540),(1920,1080),flight,geo)['reason']=='no_ground_height'
    flight['fix_type']=2
    assert locate((960,540),(1920,1080),flight,geo)['reason']=='gps_fix_below_gate'


@pytest.mark.parametrize('fix,label', [(3,'GPS 3D'),(4,'DGPS'),(5,'RTK Float'),(6,'RTK Fixed')])
def test_standard_gps_and_rtk_both_project_targets(geo,flight,fix,label):
    flight['fix_type']=fix
    target=locate((1060,540),(1920,1080),flight,geo)
    assert target['valid'] and target['gps_quality']==label
    assert target['east_m']==pytest.approx(6)
    assert target['longitude']>flight['longitude']


def test_uncalibrated_estimate_matches_geometry_and_labels_assumptions(flight):
    config=load_settings()['geolocation']
    flight['fix_type']=3
    target=locate((1397.5,443),(1920,1080),flight,config)
    assert target['valid'] and not config['calibrated']
    assert target['method']=='estimated_ground_plane'
    assert target['height_source']=='assumed_takeoff_ground'
    assert target['east_m']==pytest.approx(60*437.5/(1920*.9))
    assert target['north_m']==pytest.approx(60*97/(1920*.9))
    assert target['assumptions']==['estimated_focal_length','ground_at_takeoff_altitude','nadir_heading_follows_aircraft']
    scaled=locate((698.75,221.5),(960,540),flight,config)
    assert scaled['latitude']==pytest.approx(target['latitude'])
    assert scaled['longitude']==pytest.approx(target['longitude'])


@pytest.mark.parametrize('height', [None,0,-1,float('nan')])
def test_estimate_still_requires_positive_height(flight,height):
    flight['relative_altitude_m']=height
    assert locate((960,540),(1920,1080),flight,load_settings()['geolocation'])['reason']=='no_ground_height'


def test_measured_height_and_calibration_override_estimates(geo,flight):
    geo.update(estimated_focal_ratio=.9,assume_ground_at_takeoff=True)
    flight.update(rangefinder_m=40)
    flight['ages_s']['DISTANCE_SENSOR']=.1
    target=locate((1060,540),(1920,1080),flight,geo)
    assert target['east_m']==pytest.approx(4)
    assert target['intrinsics_source']=='calibrated'
    assert target['height_source']=='downward_rangefinder'
    assert target['assumptions']==[]


def test_zero_coordinates_valid_and_nan_rejected(geo,flight):
    flight.update(latitude=0,longitude=0)
    assert locate((960,540),(1920,1080),flight,geo)['valid']
    flight['roll']=float('nan')
    assert not locate((960,540),(1920,1080),flight,geo)['valid']


def test_upward_ray_and_wrong_aspect_rejected(geo,flight):
    geo['mount_rpy_deg']=[0,90,0]
    assert not locate((960,540),(1920,1080),flight,geo)['valid']
    assert locate((480,360),(960,720),flight,geo)['reason']=='calibration_aspect_mismatch'


def test_scaled_image_and_rangefinder(geo,flight):
    geo['ground_altitude_relative_m']=None
    flight.update(rangefinder_m=40)
    flight['ages_s']['DISTANCE_SENSOR']=.1
    g=locate((530,270),(960,540),flight,geo)
    assert g['valid'] and g['east_m']==pytest.approx(4)
    flight['ages_s']['DISTANCE_SENSOR']=5
    assert not locate((530,270),(960,540),flight,geo)['valid']


def test_stabilized_gimbal_does_not_double_apply_aircraft_attitude(geo,flight):
    geo['orientation_source']='mavlink_gimbal'
    flight.update(roll=.3,pitch=.2,yaw=1.0,gimbal={'q':[math.sqrt(.5),0,-math.sqrt(.5),0],'flags':64,'failure_flags':0})
    assert locate((960,540),(1920,1080),flight,geo)['reason']=='missing_or_stale_gimbal_attitude'
    flight['ages_s']['GIMBAL_DEVICE_ATTITUDE_STATUS']=.1
    g=locate((960,540),(1920,1080),flight,geo)
    assert g['valid'] and g['north_m']==pytest.approx(0,abs=1e-10) and g['east_m']==pytest.approx(0,abs=1e-10)


def test_gimbal_vehicle_heading_frame(geo,flight):
    geo['orientation_source']='mavlink_gimbal'
    flight.update(yaw=math.pi/2,gimbal={'q':[math.sqrt(.5),0,-math.sqrt(.5),0],'flags':32,'failure_flags':0})
    flight['ages_s']['GIMBAL_DEVICE_ATTITUDE_STATUS']=.1
    g=locate((1060,540),(1920,1080),flight,geo)
    assert g['valid'] and g['north_m']==pytest.approx(-6)
    flight['gimbal']['failure_flags']=1
    assert locate((1060,540),(1920,1080),flight,geo)['reason']=='gimbal_unhealthy'


def test_user_requested_nadir_assumption(geo,flight):
    geo['orientation_source']='assumed_nadir'
    flight.update(roll=.3,pitch=.2)
    g=locate((960,540),(1920,1080),flight,geo)
    assert g['valid'] and g['quality']=='assumed_nadir_ground_plane'
    assert g['north_m']==pytest.approx(0,abs=1e-10)


def test_twd97_uses_projected_target_position_not_aircraft_gps(geo,flight):
    base=to_twd97(25,121)
    assert base['epsg']==3826 and base['easting_m']==pytest.approx(250000,abs=.001)
    assert base['northing_m']==pytest.approx(2765777.56,abs=.01)
    target=locate((1060,540),(1920,1080),flight,geo)
    assert target['twd97']['easting_m']-base['easting_m']==pytest.approx(5.9994,abs=.01)
    geo['calibrated']=False
    assert 'twd97' not in locate((1060,540),(1920,1080),flight,geo)
