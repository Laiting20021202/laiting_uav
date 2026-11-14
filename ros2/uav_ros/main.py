import json
import array
import os
import signal
import threading
import time
from pathlib import Path
import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from .source import get
from .contract import CONTRACT, qos_for
from .convert import convert, header
from .monitor import Monitor
from .bags import Bags
from .server import serve


def main():
    config = json.loads(Path(os.getenv('UAV_ROS_CONFIG','/app/config/settings.json')).read_text())
    if set(config['topics']) != set(CONTRACT):
        raise ValueError('settings must contain exactly the ten contract topics')
    for cfg in config['topics'].values():
        if cfg['expected_hz'] <= 0 or cfg['timeout_s'] <= 0:
            raise ValueError('topic frequency and timeout must be positive')
    # ROS log directories are auxiliary; bound old logs independently of bags.
    logroot = Path(config['data_dir'])/'logs'
    logroot.mkdir(parents=True, exist_ok=True)
    logfiles = sorted((p for p in logroot.rglob('*') if p.is_file()),key=lambda p:p.stat().st_mtime,reverse=True)
    used = 0
    for p in logfiles:
        used += p.stat().st_size
        if used > 50*1024**2:
            p.unlink()
    rclpy.init()
    node = Node('uav_live_gateway')
    qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT,
                     durability=DurabilityPolicy.VOLATILE, history=HistoryPolicy.KEEP_LAST)
    pubs = {t:node.create_publisher(cls,t,qos_for(t)) for t,cls in CONTRACT.items()}
    monitor = Monitor(config)
    monitor.heartbeat = time.monotonic()
    def receive(topic, msg):
        monitor.receive(topic,msg)
        if topic == '/uav/camera/image_raw':
            frame = np.frombuffer(msg.data,dtype=np.uint8).reshape(msg.height,msg.width,3)
            preview = cv2.resize(frame,(640,round(640*msg.height/msg.width)))
            ok,jpeg = cv2.imencode('.jpg',preview,[cv2.IMWRITE_JPEG_QUALITY,65])
            if ok:
                with monitor.lock:
                    monitor.preview = jpeg.tobytes()
    subs = [node.create_subscription(cls,t,lambda msg,t=t:receive(t,msg),qos_for(t)) for t,cls in CONTRACT.items()]
    stop = threading.Event()
    seq = 0
    boot = None
    def events():
        nonlocal seq,boot
        while not stop.is_set():
            try:
                _,_,body = get(config['source_socket'],'/events?after='+str(seq))
                data = json.loads(body)
                if boot != data['boot_id']:
                    seq,boot = 0,data['boot_id']
                    continue
                with monitor.lock:
                    monitor.sources.update(bridge=True, flight=data['flight'], camera=data['camera'],
                                           error=None, source_boot_id=boot)
                    monitor.sources['dropped_events'] += max(0,data['oldest']-seq-1) if seq else 0
                for event in data['events']:
                    # Skip stale backlog after a restart or pause.
                    if -.5 <= (time.time_ns()-event['received_ns'])/1e9 <= 2:
                        for topic,msg in convert(event,config).items():
                            pubs[topic].publish(msg)
                    seq = event['seq']
            except Exception as exc:
                with monitor.lock:
                    monitor.sources.update(bridge=False,error=type(exc).__name__+': '+str(exc))
            stop.wait(.04)
    def camera():
        previous_boot = None
        sequence = 0
        while not stop.is_set():
            try:
                with monitor.lock:
                    current_boot = monitor.sources.get('source_boot_id')
                if current_boot != previous_boot:
                    sequence,previous_boot = 0,current_boot
                status,headers,body = get(config['source_socket'],'/frame?after='+str(sequence))
                if status == 200:
                    sequence = int(headers['X-Sequence'])
                    width,height = int(headers['X-Width']),int(headers['X-Height'])
                    ns = int(headers['X-TimestampNs'])
                    if len(body) != width*height*3 or not -.5 <= (time.time_ns()-ns)/1e9 <= 2:
                        raise ValueError('invalid or stale source frame')
                    m = header(CONTRACT['/uav/camera/image_raw'](),ns,'camera_optical_frame')
                    m.width,m.height,m.step,m.encoding = width,height,width*3,'bgr8'
                    # Avoid generated Python setter checking six million bytes
                    # twice per frame; the typed array takes the validated path.
                    m.data = array.array('B', body)
                    pubs['/uav/camera/image_raw'].publish(m)
                    with monitor.lock:
                        monitor.sources['camera_bridge_error'] = None
            except Exception as exc:
                with monitor.lock:
                    monitor.sources['camera_bridge_error'] = str(exc)
            stop.wait(1/config['camera_poll_hz'])
    threads = [threading.Thread(target=f,daemon=True,name=f.__name__) for f in (events,camera)]
    bags = Bags(config,monitor)
    servers = serve(config,monitor,bags)
    for th in threads:
        th.start()
    def tick():
        monitor.heartbeat = time.monotonic()
        bags.tick()
    node.create_timer(1,tick)
    exiting = threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig, lambda *_:exiting.set())
    try:
        while not exiting.is_set() and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=.2)
    finally:
        # Keep subscriptions alive while the recorder drains its final buffers.
        bags.close()
        stop.set()
        for th in threads:
            th.join(timeout=5)
        for s in servers:
            s.shutdown()
            s.server_close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
