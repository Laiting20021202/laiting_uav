"""Actual subscription evidence for a container, host or remote ROS receiver."""
import argparse
import json
import os
import socket
import time
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from .contract import CONTRACT, TYPES, inspect, clean, qos_for


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--seconds',type=float,default=20)
    p.add_argument('--output',default='-')
    p.add_argument('--require-all-valid',action='store_true')
    a=p.parse_args()
    rclpy.init()
    node=Node('uav_acceptance_'+str(os.getpid()))
    rows={t:dict(type=TYPES[t],count=0,valid_count=0,invalid_count=0,first=None,last=None,
                 first_stamp_ns=None,last_stamp_ns=None,distinct_stamps=0,last_sample=None,reasons={}) for t in CONTRACT}
    def receive(t,m):
        r=rows[t];now=time.monotonic()
        valid,reason,sample=inspect(t,m)
        r['count']+=1;r['valid_count' if valid else 'invalid_count']+=1
        r['first']=now if r['first'] is None else r['first'];r['last']=now
        if hasattr(m,'header'):
            stamp=m.header.stamp.sec*10**9+m.header.stamp.nanosec
            if r['last_stamp_ns'] != stamp:r['distinct_stamps']+=1
            if r['first_stamp_ns'] is None:r['first_stamp_ns']=stamp
            r['last_stamp_ns']=stamp
        r['last_sample']=sample
        if reason:r['reasons'][reason]=r['reasons'].get(reason,0)+1
    subs=[node.create_subscription(cls,t,lambda m,t=t:receive(t,m),qos_for(t)) for t,cls in CONTRACT.items()]
    started=time.time();end=time.monotonic()+a.seconds
    while time.monotonic()<end and rclpy.ok():rclpy.spin_once(node,timeout_sec=.1)
    for r in rows.values():
        r['hz']=(r['count']-1)/(r['last']-r['first']) if r['count']>1 else 0
        r['continuous']=r['count']>=3 and r['last']-r['first']>=a.seconds*.5
        r.pop('first');r.pop('last')
    result=clean(dict(hostname=socket.gethostname(),domain_id=os.getenv('ROS_DOMAIN_ID','0'),
        rmw=os.getenv('RMW_IMPLEMENTATION','default'),qos='image reliable; other sensors best_effort; volatile',
        started_at=started,stopped_at=time.time(),topics=rows))
    text=json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)
    if a.output=='-':print(text)
    else:
        from pathlib import Path
        Path(a.output).write_text(text+'\n')
    node.destroy_node();rclpy.shutdown()
    if a.require_all_valid and any(not r['continuous'] or not r['valid_count'] or r['invalid_count'] for r in rows.values()):
        raise SystemExit(2)


if __name__=='__main__':main()
