#!/usr/bin/env python3
"""Build bounded browser replay assets from recorded MCAP RGB and depth."""
import argparse
import bisect
import json
from pathlib import Path
import shutil
import time

import cv2
import numpy as np
import rosbag2_py
from rclpy.serialization import deserialize_message
from sensor_msgs.msg import CompressedImage, Image
import yaml


def reader(path, topic):
    r=rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(path),storage_id='mcap'),rosbag2_py.ConverterOptions('',''))
    r.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    return r


def stamp(msg):
    return msg.header.stamp.sec+msg.header.stamp.nanosec*1e-9


def main():
    parser=argparse.ArgumentParser();parser.add_argument('dataset',type=Path);args=parser.parse_args()
    root=Path('/home/nvidia/perception_domain_nx');out=args.dataset/'browser_replay'
    out.mkdir(exist_ok=True)
    calib=yaml.safe_load((args.dataset/'calib.yaml').read_text())['cam_0']
    # Vendor FishPoly world2cam: output rectified pinhole rays -> raw pixels.
    yy,xx=np.mgrid[:648,:800].astype(float);yy=(yy+.5)*2-.5;xx=(xx+.5)*2-.5
    yn=(yy-calib['v0'])/calib['A22'];xn=(xx-calib['u0'])/calib['A11']-yn*calib['A12']/calib['A11']
    radius=np.hypot(xn,yn);theta=np.arctan(radius);distorted=theta.copy()
    for power in range(2,8):distorted+=calib['k'+str(power)]*theta**power
    scale=np.divide(distorted,radius,out=np.ones_like(radius),where=radius>1e-8)
    mx=(xn*scale*calib['A11']+yn*scale*calib['A12']+calib['u0']).astype(np.float32)
    my=(yn*scale*calib['A22']+calib['v0']).astype(np.float32)
    rgb=[];r=reader(args.dataset/'bag','/odin1/image/compressed')
    while r.has_next():
        _,data,_=r.read_next();msg=deserialize_message(data,CompressedImage);rgb.append((stamp(msg),bytes(msg.data)))
    rgb.sort(key=lambda x:x[0]);times=[x[0] for x in rgb]
    manifest=[];r=reader(args.dataset/'bag','/odin1/diagnostics/depth_registered')
    while r.has_next():
        _,data,_=r.read_next();msg=deserialize_message(data,Image);ts=stamp(msg)
        index=bisect.bisect_left(times,ts);candidates=[i for i in [index-1,index] if 0<=i<len(rgb)]
        closest=min(candidates,key=lambda i:abs(times[i]-ts));dt=abs(times[closest]-ts)
        if msg.encoding!='32FC1' or msg.width!=1600 or msg.height!=1296:raise ValueError('Unexpected depth format')
        raw=np.ndarray((msg.height,msg.width),dtype='>f4' if msg.is_bigendian else '<f4',buffer=bytes(msg.data),strides=(msg.step,4))
        valid=np.isfinite(raw)&(raw>0)
        # Display reduction only: nearest real depth in each 2x2 block. Do not
        # average across invalid pixels or invent depth in unmeasured blocks.
        depth=np.where(valid,raw,np.inf).reshape(648,2,800,2).min(axis=(1,3))
        good=np.isfinite(depth);indices=np.zeros(depth.shape,np.uint8)
        indices[good]=np.rint(np.clip((depth[good]-.2)/2.8,0,1)*255).astype(np.uint8)
        colored=cv2.applyColorMap(indices,cv2.COLORMAP_TURBO);colored[~good]=0
        image=np.zeros((648,800,3),np.uint8)
        if dt<=.020:
            original=cv2.imdecode(np.frombuffer(rgb[closest][1],np.uint8),cv2.IMREAD_COLOR)
            image=cv2.remap(original,mx,my,cv2.INTER_LINEAR)
        else:cv2.putText(image,'No paired RGB within 20 ms',(30,50),0,.8,(255,255,255),2)
        combined=np.hstack((image,colored));name=f'{len(manifest):04d}.jpg'
        cv2.imwrite(str(out/name),combined,[cv2.IMWRITE_JPEG_QUALITY,90])
        manifest.append(dict(file=name,stamp=ts,rgb_dt_ms=dt*1000,rgb_valid=dt<=.020))
        if len(manifest)%100==0:print('Prepared frames:',len(manifest),flush=True)
    if not manifest:raise ValueError('No recorded depth frames')
    start=manifest[0]['stamp']
    for item in manifest:item['time']=item['stamp']-start
    (out/'manifest.json').write_text(json.dumps(dict(dataset=args.dataset.name,frames=manifest,duration=manifest[-1]['time']),ensure_ascii=False))
    shutil.copy2(root/'tools/odin1/depth_viewer/replay.html',out/'index.html')
    print(json.dumps(dict(directory=str(out),frames=len(manifest),duration=manifest[-1]['time'])),flush=True)


if __name__=='__main__':main()
