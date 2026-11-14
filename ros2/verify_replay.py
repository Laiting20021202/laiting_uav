#!/usr/bin/env python3
"""Replay a downloaded REAL bag on isolated domain 91; subscribe on host + Docker."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ros2'))
from uav_ros.bags import validate


def main():
    evidence=ROOT/'data/ros2/evidence'
    bag=evidence/'unpacked-current/bag'
    validation=validate(bag)
    (evidence/'downloaded-bag-validation.json').write_text(json.dumps(validation,indent=2))
    with (evidence/'downloaded-bag-info.txt').open('w') as f:
        subprocess.run(['ros2','bag','info',str(bag)],stdout=f,check=True)
    env=dict(os.environ,ROS_DOMAIN_ID='91',ROS_LOCALHOST_ONLY='0',RMW_IMPLEMENTATION='rmw_fastrtps_cpp',
             FASTRTPS_DEFAULT_PROFILES_FILE=str(ROOT/'ros2/config/fastdds.xml'))
    env['PYTHONPATH']=str(ROOT/'ros2')+':'+env.get('PYTHONPATH','')
    host=subprocess.Popen([sys.executable,'-m','uav_ros.probe','--seconds','40','--output',str(evidence/'replay-host.json')],env=env)
    container=subprocess.Popen(['docker','compose','-f',str(ROOT/'ros2/compose.yaml'),'exec','-T','gateway','bash','-c',
        'source /opt/ros/humble/setup.bash; ROS_DOMAIN_ID=91 python3 -m uav_ros.probe --seconds 40 --output /data/replay-container.json'])
    try:
        time.sleep(3)
        with (evidence/'replay-player.txt').open('w') as log:
            subprocess.run(['ros2','bag','play',str(bag),'--clock','--rate','0.5','--wait-for-all-acked','10','--delay','2','--disable-keyboard-controls',
                '--qos-profile-overrides-path',str(ROOT/'ros2/config/qos.yaml')],env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=55)
        assert host.wait(timeout=25)==0
        assert container.wait(timeout=25)==0
    finally:
        for proc in (host,container):
            if proc.poll() is None:
                proc.terminate();proc.wait(timeout=5)
    results={}
    for label,p in [('host',evidence/'replay-host.json'),('container',ROOT/'data/ros2/storage/replay-container.json')]:
        data=json.loads(p.read_text())
        results[label]={t:dict(expected=validation['topics'][t]['count'],received=r['count'],
                             invalid_count=r['invalid_count']) for t,r in data['topics'].items()}
        assert all(r['received']>0 for r in results[label].values()),results[label]
    (evidence/'replay-result.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results,indent=2))


if __name__=='__main__':main()
