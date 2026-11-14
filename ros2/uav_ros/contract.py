"""Fixed topic contract and validation, shared by online and offline checks."""
import math
from geometry_msgs.msg import QuaternionStamped, Vector3Stamped
from sensor_msgs.msg import Image, Imu, NavSatFix, BatteryState, MagneticField, FluidPressure
from std_msgs.msg import Float64

CONTRACT = {
    '/uav/camera/image_raw': Image,
    '/uav/imu/data_raw': Imu,
    '/uav/attitude': QuaternionStamped,
    '/uav/gps/fix': NavSatFix,
    '/uav/altitude/relative_home': Float64,
    '/uav/altitude/msl': Float64,
    '/uav/velocity/enu': Vector3Stamped,
    '/uav/battery/state': BatteryState,
    '/uav/imu/mag': MagneticField,
    '/uav/barometer/pressure': FluidPressure,
}
TYPES = {k: v.__module__.split('.')[0]+'/msg/'+v.__name__ for k, v in CONTRACT.items()}


def qos_for(topic):
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    # Full 1080p BGR frames exceed default UDP receive buffers on this host.
    # Reliable image readers allow fragment retransmission; small sensors keep
    # standard sensor QoS. Recorder and probes use exactly the same policies.
    return QoSProfile(depth=5, durability=DurabilityPolicy.VOLATILE,
        reliability=ReliabilityPolicy.RELIABLE if topic.endswith('image_raw') else ReliabilityPolicy.BEST_EFFORT)


def xyz(v):
    return [v.x, v.y, v.z]


def inspect(topic, m):
    """Return content validity separately from arrival. Unknown values remain NaN."""
    if topic.endswith('image_raw'):
        v = dict(width=m.width, height=m.height, encoding=m.encoding, bytes=len(m.data))
        ok = m.width > 0 and m.height > 0 and m.encoding == 'bgr8' and len(m.data) == m.height*m.step and m.step == m.width*3
        return ok, '' if ok else 'invalid image layout', v
    if topic.endswith('data_raw'):
        v = dict(acceleration_m_s2=xyz(m.linear_acceleration), angular_velocity_rad_s=xyz(m.angular_velocity),
                 orientation_available=bool(m.orientation_covariance[0] != -1))
        values = v['acceleration_m_s2']+v['angular_velocity_rad_s']
        ok = m.linear_acceleration_covariance[0] != -1 and m.angular_velocity_covariance[0] != -1
    elif topic == '/uav/attitude':
        values = [m.quaternion.x, m.quaternion.y, m.quaternion.z, m.quaternion.w]
        v = dict(quaternion_xyzw=values, frame=m.header.frame_id)
        ok = abs(sum(x*x for x in values)-1) < .01
    elif topic.endswith('fix'):
        values = [m.latitude, m.longitude, m.altitude]
        v = dict(latitude=m.latitude, longitude=m.longitude, ellipsoid_m=m.altitude, fix_status=m.status.status)
        ok = m.status.status >= 0 and -90 <= m.latitude <= 90 and -180 <= m.longitude <= 180
        reasons = []
        if not ok:
            reasons.append('no valid 3D GPS fix or coordinates')
        if not math.isfinite(m.altitude):
            reasons.append('WGS84 ellipsoid height unavailable (MSL is not substituted)')
        if reasons:
            return False, '; '.join(reasons), v
    elif '/altitude/' in topic:
        values = [m.data]
        v = dict(m=m.data)
        ok = True
    elif topic.endswith('/enu'):
        values = xyz(m.vector)
        v = dict(east_north_up_m_s=values)
        ok = m.header.frame_id == 'map_enu'
    elif topic.endswith('/state'):
        values = [m.voltage]
        v = dict(voltage_v=m.voltage, current_a=m.current, fraction=m.percentage,
                 temperature_c=m.temperature, present=m.present)
        ok = m.present and m.voltage > 0
        missing = [k for k in ('current_a', 'fraction') if not math.isfinite(v[k])]
        if missing:
            return False, 'battery fields unavailable: '+', '.join(missing), v
    elif topic.endswith('/mag'):
        values = xyz(m.magnetic_field)
        v = dict(tesla=values)
        ok = any(x != 0 for x in values)
    else:
        values = [m.fluid_pressure]
        v = dict(pa=m.fluid_pressure)
        ok = m.fluid_pressure > 0
    valid = bool(ok and all(math.isfinite(x) for x in values))
    return valid, '' if valid else 'invalid or unavailable measurement', v


def clean(value):
    if hasattr(value, 'item'):
        return clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value
