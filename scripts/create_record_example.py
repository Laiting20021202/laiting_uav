#!/usr/bin/env python3
"""Add one clearly labelled format example; clearing records never recreates it."""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uav.config import load_settings
from uav.geo import locate
from uav.records import render_record
from uav.storage import EventStore


def make_example():
    frame = np.full((1080, 1920, 3), (45, 52, 55), np.uint8)
    # A synthetic camera scene, deliberately distinct from an operational image.
    for x in range(0,1920,120):
        cv2.line(frame,(x,0),(x,1080),(58,66,69),1)
    for y in range(0,1080,120):
        cv2.line(frame,(0,y),(1920,y),(58,66,69),1)
    cv2.rectangle(frame,(0,380),(1919,760),(74,78,82),-1)
    for x in range(0,1920,180):
        cv2.rectangle(frame,(x,565),(x+90,577),(180,184,186),-1)
    cv2.rectangle(frame,(1000,405),(1240,525),(220,226,232),-1)
    cv2.rectangle(frame,(1040,413),(1085,517),(60,80,90),-1)
    cv2.rectangle(frame,(1160,413),(1200,517),(60,80,90),-1)
    for x in (1020,1190):
        for y in (399,515):cv2.rectangle(frame,(x,y),(x+30,y+16),(15,18,21),-1)
    cv2.putText(frame,"SYNTHETIC CAMERA VIEW / FORMAT EXAMPLE",(55,80),cv2.FONT_HERSHEY_SIMPLEX,1.2,(120,210,255),2)
    d=dict(bbox=[995,395,1245,540],pixel=[1120,467.5],class_name='car',confidence=.95,track_id=1,visible_s=2.0,confirmed=True,target=True)
    config=load_settings()['geolocation']
    config.update(calibrated=True,fx=1000,fy=1000,ground_altitude_relative_m=0)
    flight=dict(connected=True,latitude=25.033964,longitude=121.564468,roll=0,pitch=0,yaw=0.3,
                relative_altitude_m=60,fix_type=6,ages_s={k:0 for k in ('GLOBAL_POSITION_INT','GPS_RAW_INT','ATTITUDE')})
    location=locate(d['pixel'],(1920,1080),flight,config)
    event=dict(timestamp=datetime.now(timezone.utc).isoformat(),example=True,demo=True,model='format-example',
               detection=d,target_gps=location,aircraft=flight,calibration=config)
    return event, *render_record(frame,event)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    store=EventStore(args.database)
    try:
        existing=store.example()
        if existing:
            event=existing;jpeg=store.image(event['id'])
        else:
            event,jpeg,thumb=make_example();event=store.add(event,jpeg,thumb)
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_bytes(jpeg)
        print(f"Example record #{event['id']}: simulated image and coordinates, not a live detection.")
    finally:
        store.close()


if __name__=='__main__':main()
