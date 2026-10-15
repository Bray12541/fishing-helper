"""Bounded recovery budgets and local circle-only failure diagnostics."""
from collections import deque
from datetime import datetime,timezone
import json,math,queue,threading,time,uuid
from pathlib import Path
import cv2,numpy as np

RELIABILITY_DEFAULTS=dict(transient_recovery=True,failure_snapshots=True,overnight_preflight=True,
                          inventory_space_check=False,inventory_reserve_kg=5,health_minutes=15)

def parse_reserve(value):
    if isinstance(value,bool):raise ValueError('Inventory headroom: use 0 to 50 kg.')
    try:value=float(value)
    except (ValueError,TypeError,OverflowError):raise ValueError('Inventory headroom: use 0 to 50 kg.') from None
    if not math.isfinite(value) or not 0<=value<=50:raise ValueError('Inventory headroom: use 0 to 50 kg.')
    return value

def parse_health_minutes(value):
    if isinstance(value,bool) or not str(value).strip().isdigit():raise ValueError('Health update interval: use 5 to 120 whole minutes.')
    value=int(value)
    if not 5<=value<=120:raise ValueError('Health update interval: use 5 to 120 whole minutes.')
    return value

class HudReadError(RuntimeError):pass

class RecoveryBudget:
    """At most two attempts of each recovery type in any ten-minute window."""
    def __init__(self):self.attempts={}
    def claim(self,kind,now):
        rows=self.attempts.setdefault(kind,deque())
        while rows and now-rows[0]>=600:rows.popleft()
        if len(rows)>=2:return False
        rows.append(now);return True

class FailureRecorder:
    """One latest cropped frame, five seconds of measurements, bounded disk/queue."""
    def __init__(self,folder,enabled=True):
        self.folder=Path(folder);self.enabled=enabled;self.samples=deque(maxlen=64)
        self.latest=None;self.last_sample=-math.inf;self.queue=queue.Queue(maxsize=8)
        self.thread=None;self.closed=False;self.last_path='';self.error='';self.dropped=0
    def observe(self,frame,size,now,**details):
        if not self.enabled or now-self.last_sample<.1:return
        if not isinstance(frame,(bytes,bytearray)) or not 1<=size<=650 or len(frame)!=size*size*4:return
        self.last_sample=now;self.latest=(bytes(frame),size)
        self.samples.append(dict(sampled_at=now,**details))
        while self.samples and now-self.samples[0]['sampled_at']>5:self.samples.popleft()
    def snapshot(self,reason,**context):
        if not self.enabled or self.closed or self.latest is None:return False
        event=(self.latest,dict(reason=str(reason)[:500],created=datetime.now(timezone.utc).isoformat(),
                                samples=list(self.samples),**context))
        try:self.queue.put_nowait(event)
        except queue.Full:self.dropped+=1;return False
        if self.thread is None:
            self.thread=threading.Thread(target=self.run,daemon=True,name='fishing-snapshots')
            try:self.thread.start()
            except RuntimeError:self.thread=None;self.error='Could not start snapshot writer.';return False
        return True
    def run(self):
        while True:
            try:event=self.queue.get(timeout=.1)
            except queue.Empty:
                if self.closed:return
                continue
            try:self.write(*event)
            except (OSError,ValueError,TypeError,cv2.error):self.error='Could not save failure snapshot. Check folder access and disk space.'
            finally:self.queue.task_done()
    def write(self,frame,metadata):
        self.folder.mkdir(parents=True,exist_ok=True)
        pixels,size=frame;image=np.frombuffer(pixels,np.uint8).reshape(size,size,4)[:,:,:3]
        ok,png=cv2.imencode('.png',image)
        if not ok:raise ValueError('Snapshot image could not be encoded')
        encoded=json.dumps(metadata,indent=2,allow_nan=False)
        stem='failure-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        image_path=self.folder/(stem+'.png');json_path=self.folder/(stem+'.json')
        try:
            image_path.write_bytes(png.tobytes())
            json_path.write_text(encoded,encoding='utf-8')
        except OSError:
            image_path.unlink(missing_ok=True);json_path.unlink(missing_ok=True)
            raise
        self.last_path=str(json_path);self.error=''
        # Only remove our named files directly within the dedicated folder.
        root=self.folder.resolve();groups={}
        for path in self.folder.glob('failure-*'):
            if path.suffix not in ('.png','.json') or not path.is_file() or path.resolve().parent!=root:continue
            groups.setdefault(path.stem,[]).append(path)
        ordered=sorted(groups.values(),key=lambda paths:min(p.stat().st_mtime for p in paths))
        total=sum(p.stat().st_size for paths in ordered for p in paths)
        while ordered and (len(ordered)>20 or total>20_000_000):
            for path in ordered.pop(0):
                total-=path.stat().st_size;path.unlink(missing_ok=True)
    def close(self):
        self.closed=True
        if self.thread:self.thread.join(2)
