#!/usr/bin/env python3
"""Install/rollback opt-in user systemd services; never rebuild at boot."""
import argparse
import json
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
UNITS=Path.home()/'.config/systemd/user'
DROP=UNITS/'uav-v2.service.d/ros2.conf'
SERVICE=UNITS/'uav-ros2.service'


def run(*args):
    subprocess.run(args,cwd=ROOT,check=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['install','rollback'])
    a=p.parse_args()
    if a.action=='rollback':
        run('systemctl','--user','disable','--now','uav-ros2.service')
        DROP.unlink(missing_ok=True);SERVICE.unlink(missing_ok=True)
        run('systemctl','--user','daemon-reload')
        run('systemctl','--user','restart','uav-v2.service')
        print('Original hardware service restored. Recordings retained in data/ros2/.')
        return
    run('docker','image','inspect','--format','{{.Id}}','laiting-uav-ros2:humble-v1')
    run('docker','compose','-f','ros2/compose.yaml','config','--quiet')
    linger=subprocess.check_output(['loginctl','show-user',str(os.getuid()),'-p','Linger','--value'],text=True).strip()
    if linger!='yes':
        raise SystemExit('Enable linger for this user (loginctl enable-linger) before installation.')
    for folder in ('source','control','storage'):
        path=ROOT/'data/ros2'/folder;path.mkdir(parents=True,exist_ok=True);path.chmod(0o700)
    if ' ' in str(ROOT):
        raise SystemExit('systemd path with spaces needs explicit escaping; use a path without spaces')
    DROP.parent.mkdir(parents=True,exist_ok=True);SERVICE.parent.mkdir(parents=True,exist_ok=True)
    if not DROP.exists():
        DROP.write_text('[Service]\nEnvironment=UAV_ROS_BRIDGE='+str(ROOT/'data/ros2/source/source.sock')+'\nEnvironment=UAV_ROS_CONTROL='+str(ROOT/'data/ros2/control/http.sock')+'\n')
    SERVICE.write_text((ROOT/'ros2/systemd/uav-ros2.service.in').read_text().replace('@ROOT@',str(ROOT)))
    run('systemctl','--user','daemon-reload')
    run('systemctl','--user','restart','uav-v2.service')
    run('systemctl','--user','enable','--now','uav-ros2.service')
    print('Dashboard: existing website /ros2/ or LAN http://HOST:8090 (existing user credentials).')


if __name__=='__main__':main()
