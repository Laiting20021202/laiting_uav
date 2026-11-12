#!/usr/bin/env python3
"""Estimate camera intrinsics from chessboard images; apply only with --apply."""
import argparse
import json
import sys
from pathlib import Path
import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from uav.config import atomic_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images',type=Path,help='Directory containing calibration JPG/PNG images')
    parser.add_argument('--columns',type=int,default=9,help='Internal chessboard corners per row')
    parser.add_argument('--rows',type=int,default=6,help='Internal chessboard corners per column')
    parser.add_argument('--square-mm',type=float,required=True)
    parser.add_argument('--apply',action='store_true',help='Write fx/fy/cx/cy/distortion into settings.json')
    args=parser.parse_args()
    if args.columns<3 or args.rows<3 or not np.isfinite(args.square_mm) or args.square_mm<=0:
        parser.error('Invalid board dimensions')
    board=(args.columns,args.rows)
    object_points=np.zeros((args.columns*args.rows,3),np.float32)
    object_points[:,:2]=np.mgrid[0:args.columns,0:args.rows].T.reshape(-1,2)*args.square_mm/1000
    objects,images=[],[]
    size=None
    for path in sorted(args.images.iterdir()):
        if path.suffix.lower() not in ('.jpg','.jpeg','.png'):
            continue
        gray=cv2.imread(str(path),cv2.IMREAD_GRAYSCALE)
        if gray is None:
            continue
        current=gray.shape[::-1]
        if size is not None and current!=size:
            parser.error('All calibration images must have the same dimensions')
        size=current
        found,corners=cv2.findChessboardCornersSB(gray,board)
        if found:
            objects.append(object_points)
            images.append(corners)
    if len(images)<12:
        parser.error(f'Found only {len(images)} usable boards; capture at least 12 diverse views')
    rms,matrix,distortion,_,_=cv2.calibrateCamera(objects,images,size,None,None)
    result=dict(width=size[0],height=size[1],fx=float(matrix[0,0]),fy=float(matrix[1,1]),cx=float(matrix[0,2]),cy=float(matrix[1,2]),distortion=distortion.ravel().tolist())
    print(json.dumps({'rms_pixels':rms,'images_used':len(images),**result},indent=2))
    if not np.isfinite(rms) or rms>2:
        parser.error('Reprojection error exceeds 2 pixels; improve calibration captures before applying')
    if args.apply:
        path=ROOT/'config/settings.json'
        config=json.loads(path.read_text())
        config['geolocation'].update(result,calibrated=True)
        atomic_json(path,config,0o644)
        print('Intrinsics saved. Restart uav-v2 to apply. Mount/yaw and ground height still require field verification.')


if __name__=='__main__':
    main()
