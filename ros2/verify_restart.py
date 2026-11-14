#!/usr/bin/env python3
"""Fault only the new Docker service; retain original hardware services and data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ros2'))
from uav_ros.source import UnixHTTP
COMPOSE=['docker','compose','-f',str(ROOT/'ros2/compose.yaml')]


def request(route='/api/status',method='GET'):
    c=UnixHTTP(str(ROOT/'data/ros2/control/http.sock'),timeout=5)
    try:
        c.request(method,route);r=c.getresponse();return r.status,json.loads(r.read())
    finally:c.close()


def ready():
    deadline=time.monotonic()+65
    while time.monotonic()<deadline:
        try:
            code,d=request()
            if code==200:return d
        except (OSError,ValueError):pass
        time.sleep(1)
    raise TimeoutError('gateway did not recover')


def main():
    out=ROOT/'data/ros2/evidence/restart-report.json'
    before=ready()
    assert before['recording']['current']['state'] not in ('starting','recording','stopping','validating','packaging')
    history={j['name']:j['zip_sha256'] for j in before['recording']['history'] if j['state']=='ready'}
    code,started=request('/api/start','POST');assert code==200
    interrupted=started['current']['name']
    time.sleep(5)
    subprocess.run(COMPOSE+['kill','-s','SIGKILL','gateway'],check=True)
    recovered=ready()
    deadline=time.monotonic()+65
    while not any(j['name']==interrupted and j['state']=='failed' for j in recovered['recording']['history']):
        if time.monotonic()>deadline:raise TimeoutError('interrupted job not marked failed')
        time.sleep(1);recovered=ready()
    denied,_=request('/api/download/'+interrupted);assert denied==409
    subprocess.run(['systemctl','--user','stop','uav-ros2.service'],check=True)
    subprocess.run(COMPOSE+['rm','-f','gateway'],check=True)
    subprocess.run(['systemctl','--user','start','uav-ros2.service'],check=True)
    after=ready()
    after_jobs={j['name']:j for j in after['recording']['history']}
    for name,digest in history.items():
        path=ROOT/'data/ros2/storage/recordings'/name/(name+'.zip')
        assert after_jobs[name]['state']=='ready'
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
    report=dict(interrupted_recording=interrupted,interrupted_state=after_jobs[interrupted]['state'],
        incomplete_download_http=denied,removed_and_recreated_container=True,
        preserved_ready_archives=list(history),all_archive_hashes_unchanged=True,
        systemd_enabled=subprocess.check_output(['systemctl','--user','is-enabled','uav-ros2'],text=True).strip(),
        docker_boot_enabled=subprocess.check_output(['systemctl','is-enabled','docker'],text=True).strip(),
        linger=subprocess.check_output(['loginctl','show-user','hrc','-p','Linger','--value'],text=True).strip(),
        original_service_active=subprocess.check_output(['systemctl','--user','is-active','uav-v2'],text=True).strip(),
        full_ubuntu_reboot_tested=False,completed_at=time.time())
    out.write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))


if __name__=='__main__':main()
