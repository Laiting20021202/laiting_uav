"""MAVLink SI conversion. Header stamps are host receive UTC, never boot time."""
import math
from .contract import CONTRACT

NAN = float('nan')


def multiply(a, b):
    w,x,y,z = a; W,X,Y,Z = b
    return [w*W-x*X-y*Y-z*Z, w*X+x*W+y*Z-z*Y,
            w*Y-x*Z+y*W+z*X, w*Z+x*Y-y*X+z*W]


def attitude(r, p, y):
    cr,sr = math.cos(r/2), math.sin(r/2)
    cp,sp = math.cos(p/2), math.sin(p/2)
    cy,sy = math.cos(y/2), math.sin(y/2)
    q = [cr*cp*cy+sr*sp*sy, sr*cp*cy-cr*sp*sy,
         cr*sp*cy+sr*cp*sy, cr*cp*sy-sr*sp*cy]
    # R_ENU<-NED * R_NED<-FRD * R_FRD<-FLU (REP-103).
    return multiply(multiply([0, 2**-.5, 2**-.5, 0], q), [0,1,0,0])


def header(m, ns, frame):
    m.header.stamp.sec, m.header.stamp.nanosec = divmod(int(ns), 10**9)
    m.header.frame_id = frame
    return m


def vector(v, values):
    v.x, v.y, v.z = map(float, values)


def convert(event, config):
    d = event['message']; kind = d['mavpackettype']; ns = event['received_ns']
    out = {}
    def new(topic, frame='base_link'):
        m = CONTRACT[topic]()
        if hasattr(m, 'header'):
            header(m, ns, frame)
        out[topic] = m
        return m
    if kind == 'ATTITUDE':
        q = attitude(d['roll'], d['pitch'], d['yaw'])
        m = new('/uav/attitude', 'map_enu')
        m.quaternion.w, m.quaternion.x, m.quaternion.y, m.quaternion.z = q
    elif kind == 'GLOBAL_POSITION_INT':
        new('/uav/altitude/relative_home').data = d['relative_alt']/1000.
        new('/uav/altitude/msl').data = d['alt']/1000.
        vector(new('/uav/velocity/enu', 'map_enu').vector, [d['vy']/100., d['vx']/100., -d['vz']/100.])
    elif kind == 'GPS_RAW_INT':
        m = new('/uav/gps/fix', 'gps_link')
        fix = d['fix_type'] >= 3 and -900000000 <= d['lat'] <= 900000000 and -1800000000 <= d['lon'] <= 1800000000
        m.status.status = (2 if d['fix_type'] in (5,6) else 0) if fix else -1
        # GPS_RAW_INT does not prove which GNSS constellations are enabled.
        m.status.service = 0
        m.latitude, m.longitude = (d['lat']/1e7, d['lon']/1e7) if fix else (NAN, NAN)
        # MAVLink 2 zero-fills absent extensions. Payload evidence or explicit
        # firmware confirmation is required; never copy GPS.alt (MSL) here.
        has_ellipsoid = config['gps_ellipsoid_confirmed'] or (
            event['payload_length'] >= 34 and d.get('alt_ellipsoid', 0) != 0)
        m.altitude = d.get('alt_ellipsoid', NAN)/1000. if fix and has_ellipsoid else NAN
        m.position_covariance_type = 0
    elif kind == 'SYS_STATUS':
        m = new('/uav/battery/state')
        m.voltage = d['voltage_battery']/1000. if d['voltage_battery'] != 65535 else NAN
        # MAVLink positive current means discharge; ROS positive means charging.
        m.current = -d['current_battery']/100. if d['current_battery'] != -1 else NAN
        m.percentage = d['battery_remaining']/100. if 0 <= d['battery_remaining'] <= 100 else NAN
        m.temperature = m.charge = m.capacity = m.design_capacity = NAN
        m.present = math.isfinite(m.voltage) and m.voltage > 0
        # Charge status/chemistry are UNKNOWN; these enums are not measurements.
    if kind in ('SCALED_IMU', 'HIGHRES_IMU'):
        high = kind == 'HIGHRES_IMU'
        flags = d.get('fields_updated', 0)
        def fresh(mask):
            return not high or flags & mask == mask
        if kind == config['imu_source'] and fresh(0x3f):
            m = new('/uav/imu/data_raw')
            # ROS requires -1 covariance when orientation is unavailable.
            m.orientation_covariance[0] = -1.
            m.orientation.w = 0.
            a, g = (1., 1.) if high else (9.80665/1000., .001)
            vector(m.linear_acceleration, [d['xacc']*a, -d['yacc']*a, -d['zacc']*a])
            vector(m.angular_velocity, [d['xgyro']*g, -d['ygyro']*g, -d['zgyro']*g])
        if kind == config['mag_source'] and fresh(0x1c0):
            m = new('/uav/imu/mag')
            scale = 1e-4 if high else 1e-7
            vector(m.magnetic_field, [d['xmag']*scale, -d['ymag']*scale, -d['zmag']*scale])
        if kind == config['pressure_source'] and fresh(0x200):
            new('/uav/barometer/pressure', 'barometer_link').fluid_pressure = d['abs_pressure']*100.
    elif kind == config['pressure_source'] == 'SCALED_PRESSURE':
        new('/uav/barometer/pressure', 'barometer_link').fluid_pressure = d['press_abs']*100.
    return out
