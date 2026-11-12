from pathlib import Path
import av, cv2, csv, json, time, torch
from fractions import Fraction
from collections import Counter
from ultralytics import YOLO
ROOT=Path.cwd(); OUT=ROOT/'models/president_cup_v5/video_validation'; SRC=ROOT/'testing/實驗室走動.MOV'
paths={'president_cup_v5':ROOT/'models/president_cup_v5/best.pt','competition_5class_v1-3':ROOT/'models/competition_5class_v1-3/weights/best.pt'}
models={k:YOLO(str(p)) for k,p in paths.items()}
src=av.open(str(SRC)); vs=src.streams.video[0]
summary={'source':str(SRC),'settings':{'imgsz':1536,'conf':0.25,'iou':0.6,'max_det':200,'device':'cuda:0','tracking':False,'audio':'not included'},'models':{}}
outputs={}; csvfiles={}; writers={}
for k,m in models.items():
 summary['models'][k]={'path':str(paths[k]),'classes':m.names,'parameters':sum(p.numel() for p in m.model.parameters()),'weight_bytes':paths[k].stat().st_size,'checkpoint_validation_metrics':m.ckpt.get('train_metrics'),'detections':Counter(),'frames_by_class':Counter(),'frames_with_detection':0,'confidence_sum':Counter(),'inference_ms':0}
 csvfiles[k]=(OUT/(k+'.csv')).open('w',newline=''); writers[k]=csv.writer(csvfiles[k]); writers[k].writerow(['frame','timestamp_seconds','class_id','class_name','confidence','x1','y1','x2','y2'])
for k in [*models,'comparison_side_by_side']:
 c=av.open(str(OUT/(k+'.mp4')),'w'); s=c.add_stream('libx264',rate=vs.average_rate); s.width=1920; s.height=1080 if k!='comparison_side_by_side' else 540;s.pix_fmt='yuv420p';s.options={'crf':'20','preset':'fast'}; outputs[k]=(c,s)
def encode(k,img,t):
 c,s=outputs[k]; f=av.VideoFrame.from_ndarray(img,format='bgr24');f.pts=round(t*60000);f.time_base=Fraction(1,60000)
 for p in s.encode(f):c.mux(p)
n=0; start=time.monotonic(); batch=[]; samples={0,156,312,468,623}
def process(batch):
 global n
 images=[f.to_ndarray(format='bgr24') for f in batch]; annotated={}
 for k,m in models.items():
  results=m.predict(images,imgsz=1536,conf=.25,iou=.6,max_det=200,device=0,verbose=False)
  annotated[k]=[];stat=summary['models'][k]
  for offset,(f,r) in enumerate(zip(batch,results)):
   t=float(f.pts*f.time_base); names=[]
   for b in r.boxes:
    cls=int(b.cls.item());name=m.names[cls];conf=float(b.conf.item());xy=b.xyxy[0].tolist();names.append(name)
    stat['detections'][name]+=1;stat['confidence_sum'][name]+=conf
    writers[k].writerow([n+offset,round(t,6),cls,name,round(conf,6),*[round(v,2) for v in xy]])
   stat['frames_with_detection']+=bool(names);stat['frames_by_class'].update(set(names));stat['inference_ms']+=r.speed['inference']
   img=r.plot();cv2.rectangle(img,(0,0),(1920,48),(20,20,20),-1);cv2.putText(img,f'{k} | t={t:.2f}s | conf=0.25',(12,32),cv2.FONT_HERSHEY_SIMPLEX,.8,(255,255,255),2)
   annotated[k].append(img);encode(k,img,t)
 for j,f in enumerate(batch):
  pair=cv2.hconcat([cv2.resize(annotated[k][j],(960,540)) for k in models]);encode('comparison_side_by_side',pair,float(f.pts*f.time_base))
  if n+j in samples:cv2.imwrite(str(OUT/f'comparison_frame_{n+j:04d}.jpg'),pair)
 n+=len(batch)
 print(f'{n}/624 frames, elapsed {time.monotonic()-start:.1f}s',flush=True)
for f in src.decode(vs):
 batch.append(f)
 if len(batch)==8:process(batch);batch=[]
if batch:process(batch)
for c,s in outputs.values():
 for p in s.encode():c.mux(p)
 c.close()
for f in csvfiles.values():f.close()
summary['frames']=n;summary['duration_seconds']=float(vs.duration*vs.time_base);summary['processing_seconds']=time.monotonic()-start
for stat in summary['models'].values():
 stat['mean_confidence_by_class']={k:stat['confidence_sum'][k]/v for k,v in stat['detections'].items()};del stat['confidence_sum'];stat['average_inference_ms']=stat['inference_ms']/n
(OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
