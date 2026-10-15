"""Session history, learned rates and background persistence. No game input."""
from collections import OrderedDict
import csv
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid
import zipfile
from fishing_calculator import CALCULATOR_DEFAULTS,estimate_run,estimate_time

RUN_DEFAULTS=dict(stop_bait=0,stop_catches=0,stop_minutes=0,require_supplies=False,learn_rates=True)

def parse_target(key,value):
    limit=10080 if key=='stop_minutes' else 1_000_000
    if isinstance(value,bool) or not re.fullmatch(r'[0-9]+',str(value).strip()):
        raise ValueError('Stop targets must be whole numbers; use 0 for no limit.')
    result=int(value)
    if not 0<=result<=limit:raise ValueError(f'{key.replace("stop_","").title()} target must be from 0 to {limit}.')
    return result

def fingerprint(url):return hashlib.sha256(url.encode('utf-8')).hexdigest() if url else ''

def clean_text(value):
    value=re.sub(r'https?://(?:\w+\.)?discord(?:app)?\.com/[^\s"<>]+','[webhook removed]',str(value),flags=re.I)
    return re.sub(r'dpapi:[A-Za-z0-9+/=]+','[encrypted secret removed]',value)

def safe_settings(settings):
    return {key:value for key,value in settings.items() if not key.startswith('_') and
            not any(word in key.lower() for word in ('url','secret','token','password'))}

def utc_time():return datetime.now(timezone.utc).isoformat(timespec='seconds')

class RunJournal:
    def __init__(self,saved=None):
        self.lock=threading.RLock()
        self.session_id=uuid.uuid4().hex
        self.entries=OrderedDict()
        self.completed_attempts=0
        self.bait_used=0
        self.stock={}
        self.capacity_reading=None
        self.baseline=None
        self.last_inventory_sample=-float('inf')
        self.bait_seconds=0.;self.learning_bait=0
        self.uses={'food':0,'water':0}
        self.learning_seconds=0.
        self.last_elapsed=0.
        self.completed_goal=None
        self.revision=0
        if isinstance(saved,dict):
            identifier=saved.get('session_id')
            if isinstance(identifier,str) and re.fullmatch(r'[0-9a-f]{32}',identifier):self.session_id=identifier
            for key in ('completed_attempts','bait_used','learning_bait'):
                value=saved.get(key,0)
                if type(value) is int and 0<=value<=1_000_000_000:setattr(self,key,value)
            for key in ('bait_seconds','learning_seconds','last_elapsed'):
                value=saved.get(key,0)
                if type(value) in (int,float) and math.isfinite(value) and 0<=value<=1e9:setattr(self,key,float(value))
            for kind in self.uses:
                value=saved.get('uses',{}).get(kind,0) if isinstance(saved.get('uses',{}),dict) else 0
                if type(value) is int and 0<=value<=1e9:self.uses[kind]=value
            for row in saved.get('entries',[])[:10000] if isinstance(saved.get('entries'),list) else []:
                if not isinstance(row,dict) or type(row.get('cycle')) is not int or not 1<=row['cycle']<=1e9:continue
                items=row.get('items',{})
                items={str(name)[:80]:quantity for name,quantity in items.items()
                       if quantity is None or (type(quantity) is int and 1<=quantity<=9999)} if isinstance(items,dict) else {}
                self.entries[row['cycle']]=dict(cycle=row['cycle'],time=str(row.get('time',''))[:40],
                    result=row.get('result') if row.get('result') in ('catch','failure','unknown') else None,
                    items=items,rod=str(row.get('rod','—'))[:8])
            if isinstance(saved.get('stock'),dict):
                for kind,value in saved['stock'].items():
                    if kind in ('bait','food','water') and isinstance(value,dict) and type(value.get('count')) is int and value['count']>=0:
                        self.stock[kind]=dict(count=value['count'],checked=str(value.get('checked',''))[:40],estimated=True)

    def reset(self):
        fresh=RunJournal()
        with self.lock:
            stock=self.stock;revision=self.revision+1
            for key,value in vars(fresh).items():
                if key!='lock':setattr(self,key,value)
            self.stock=stock;self.revision=revision

    def started(self,cycle,rod):
        with self.lock:
            self.entries.setdefault(cycle,dict(cycle=cycle,time=utc_time(),result=None,items={},rod=rod or '—'))
            if rod is not None:self.entries[cycle]['rod']=rod
            while len(self.entries)>10000:self.entries.popitem(last=False)
            self.revision+=1

    def observe(self,cycle,text,sampled_at=None):
        from fishing_discord import items_from_notice
        with self.lock:
            if cycle not in self.entries:return
            row=self.entries[cycle]
            for name,quantity in items_from_notice(text):
                row['items'][name]=max(row['items'].get(name) or 0,quantity or 0) or None
            if sampled_at is not None:
                age=max(0,time.monotonic()-sampled_at)
                row['time']=datetime.fromtimestamp(time.time()-age,timezone.utc).isoformat(timespec='seconds')
            self.revision+=1

    def sync(self,stats):
        snapshot=stats.snapshot()
        with stats.lock:results={cycle:attempt['result'] for cycle,attempt in stats.attempts.items()}
        with self.lock:
            self.learning_seconds=max(self.learning_seconds,float(snapshot['elapsed']))
            self.last_elapsed=float(snapshot['elapsed'])
            for cycle,result in results.items():
                if result is None:continue
                if cycle not in self.entries:self.started(cycle,None)
                if self.entries[cycle]['result']!=result:
                    self.entries[cycle]['result']=result;self.revision+=1

    def completed(self):
        with self.lock:
            self.completed_attempts+=1;self.revision+=1
            if 'bait' in self.stock:
                self.stock['bait']['count']=max(0,self.stock['bait']['count']-1)
                self.stock['bait']['estimated']=True

    def inventory(self,snapshot,elapsed):
        if not isinstance(snapshot,tuple) or len(snapshot)!=2 or not isinstance(snapshot[1],dict):return
        sampled,values=snapshot
        with self.lock:
            # Reject duplicate scans. After restart, fresh inventory starts a
            # new baseline; stored stock never authorizes learning or item use.
            if sampled<=self.last_inventory_sample:return
            self.last_inventory_sample=sampled
            bait=values.get('bait')
            if type(bait) is int and bait>=0:
                if self.baseline:
                    _,old,old_elapsed,old_attempts=self.baseline
                    used=old-bait;seconds=elapsed-old_elapsed
                    attempts=self.completed_attempts-old_attempts
                    if attempts>0 and 0<used<=max(4,attempts*3) and 0<seconds<=3600:
                        self.bait_used+=used;self.learning_bait+=used;self.bait_seconds+=seconds
                self.baseline=(sampled,bait,elapsed,self.completed_attempts)
            for kind,count in values.items():
                if kind in ('bait','food','water') and type(count) is int and count>=0:
                    self.stock[kind]=dict(count=count,checked=utc_time(),estimated=False,sampled=sampled)
            for kind in self.stock:
                if kind not in values:self.stock[kind]['estimated']=True
            self.revision+=1

    def consumed(self,kind,sent_at=None):
        kind='food' if kind=='hunger' else 'water'
        with self.lock:
            self.uses[kind]+=1
            if kind in self.stock and (sent_at is None or self.stock[kind].get('sampled',-float('inf'))<=sent_at):
                self.stock[kind]['count']=max(0,self.stock[kind]['count']-1)
                self.stock[kind]['estimated']=True
            self.revision+=1

    def rates(self):
        with self.lock:
            rates={}
            if self.learning_bait>=20 and self.bait_seconds>=300:
                rates['calc_bait_per_hour']=min(10000,max(1,self.learning_bait*3600/self.bait_seconds))
            if self.learning_seconds>=1800:
                for kind in ('food','water'):
                    if self.uses[kind]>=2:rates['calc_'+kind+'_per_hour']=min(1000,max(.01,self.uses[kind]*3600/self.learning_seconds))
            return rates

    def plan(self,settings,bait=None):
        values={key:settings.get(key,default) for key,default in CALCULATOR_DEFAULTS.items()}
        if settings.get('learn_rates',True):values.update(self.rates())
        if bait is not None:values['calc_bait']=max(1,int(bait))
        return estimate_run(values)

    def preflight(self,settings,supplies):
        with self.lock:
            goal=settings.get('stop_bait') or settings.get('calc_bait',1000)
            remaining=max(0,goal-max(self.completed_attempts,self.bait_used))
        plan=self.plan(settings,remaining)
        required={'bait':remaining,'food':plan['pack_food'] if remaining else 0,
                  'water':plan['pack_water'] if remaining else 0}
        with self.lock:
            checks=[];short=False;unknown=False
            for kind,needed in required.items():
                stock=self.stock.get(kind)
                if needed and (not stock or stock['estimated']):
                    checks.append(f'{kind.title()}: check pending');unknown=True
                else:
                    count=stock['count'] if stock else 0;short|=count<needed
                    checks.append(f'{kind.title()}: {count}/{needed}'+(' · short' if count<needed else ''))
        available=len(supplies['rod_slots'])-len(supplies['broken'])
        if supplies.get('rod_checked',False):checks.append(f'Rods: {available}/{len(supplies["rod_slots"])} available')
        else:checks.append('Rods: check pending');unknown=True
        return ' · '.join(checks),short,unknown

    def target_reason(self,settings,stats):
        snapshot=stats.snapshot()
        with self.lock:
            used=max(self.completed_attempts,self.bait_used)
        if settings.get('stop_bait',0) and used>=settings['stop_bait']:return 'Bait/attempt target reached.'
        if settings.get('stop_catches',0) and snapshot['catches']>=settings['stop_catches']:return 'Catch target reached.'
        if settings.get('stop_minutes',0) and snapshot['elapsed']>=settings['stop_minutes']*60:return 'Fishing time target reached.'
        return None

    def record_capacity(self,snapshot):
        if not isinstance(snapshot,tuple) or len(snapshot)!=2:return
        sampled,value=snapshot
        if (type(sampled) not in (int,float) or not math.isfinite(sampled) or
                not isinstance(value,(tuple,list)) or len(value)!=2 or
                not all(type(v) in (int,float) and math.isfinite(v) for v in value) or
                not 0<=value[0]<=100000 or not 0<value[1]<=100000):return
        with self.lock:self.capacity_reading=(sampled,tuple(value))

    def overnight_check(self,settings,supplies,now=None):
        detail,short,unknown=self.preflight(settings,supplies)
        now=time.monotonic() if now is None else now
        with self.lock:capacity=self.capacity_reading
        if not settings.get('inventory_space_check',False):
            detail+=' · Inventory space: not enforced'
        elif not capacity or not 0<=now-capacity[0]<=15:
            detail+=' · Inventory space: check pending';unknown=True
        else:
            used,total=capacity[1];free=max(0,total-used);reserve=settings.get('inventory_reserve_kg',5)
            short|=free<reserve or used>=total
            detail+=f' · Inventory space: {free:g} kg free / {total:g} kg (minimum {reserve:g})'
        targets=[f'{settings[key]} {label}' for key,label in (('stop_bait','bait/attempts'),('stop_catches','catches'),('stop_minutes','active minutes')) if settings.get(key,0)]
        detail+=' · Stop targets: '+(', '.join(targets) if targets else 'none; continuous run')
        return detail,short,unknown

    def progress(self,settings,stats):
        data=stats.snapshot()
        with self.lock:
            used=max(self.completed_attempts,self.bait_used)
            stock=self.stock.get('bait')
            remaining=str(stock['count'])+(' (estimated)' if stock['estimated'] else ' (last check)') if stock else '—'
        goal=settings.get('stop_bait',0) or settings.get('calc_bait',1000)
        left=max(0,goal-used);percent=min(100,used/goal*100)
        minutes=0 if not left else self.plan(settings,left)['minutes']
        summary=f'{used}/{goal} used/attempts'
        if not settings.get('stop_bait',0) and settings.get('stop_catches',0):
            goal=settings['stop_catches'];left=max(0,goal-data['catches'])
            pace=data['catches']*3600/data['elapsed'] if data['catches']>=3 and data['elapsed']>=60 else None
            minutes=math.ceil(left*60/pace) if pace else (0 if not left else self.plan(settings,left)['minutes'])
            percent=min(100,data['catches']/goal*100);summary=f'{data["catches"]}/{goal} confirmed catches'
        elif not settings.get('stop_bait',0) and settings.get('stop_minutes',0):
            goal=settings['stop_minutes']*60
            minutes=math.ceil(max(0,goal-data['elapsed'])/60)
            percent=min(100,data['elapsed']/goal*100);summary=f'{data["elapsed"]//60}/{settings["stop_minutes"]} active minutes'
        eta=datetime.fromtimestamp(time.time()+minutes*60).astimezone().strftime('%I:%M %p') if minutes else 'Complete'
        text=f'Bait left: {remaining} · {summary} · {percent:.0f}%\n'
        text+=f'Estimated time left: {estimate_time(minutes)} · Finish around {eta} while fishing runs'
        if self.target_reason(settings,stats):text+='\nStop target reached. Reset the session or raise the target before restarting.'
        return text,percent

    def history(self):
        with self.lock:return [dict(row,items=dict(row['items'])) for row in self.entries.values() if row['result'] is not None]

    def export_state(self):
        with self.lock:
            return dict(session_id=self.session_id,completed_attempts=self.completed_attempts,bait_used=self.bait_used,
                entries=[dict(row,items=dict(row['items'])) for row in self.entries.values()],stock={key:dict(value) for key,value in self.stock.items()},
                uses=dict(self.uses),learning_bait=self.learning_bait,bait_seconds=self.bait_seconds,
                learning_seconds=self.learning_seconds,last_elapsed=self.last_elapsed)

def export_history(path,journal):
    with Path(path).open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.writer(stream);writer.writerow(('Time (UTC)','Attempt','Result','Rod slot','Item','Quantity'))
        for row in journal.history():
            for name,quantity in (row['items'] or {'Item unreadable':None}).items():
                # Keep spreadsheet exports from evaluating OCR text as formulas.
                values=(row['time'],row['cycle'],row['result'],row['rod'],name,quantity if quantity is not None else '')
                writer.writerow(tuple("'"+value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value for value in values))

def export_support(path,version,settings,stats,journal,notifier,log_path):
    try:log=Path(log_path).read_text(encoding='utf-8',errors='replace')[-60000:]
    except OSError:log='No local log available.'
    summary=dict(version=version,created=utc_time(),settings=safe_settings(settings),session=stats.snapshot(),
                 discord=notifier.snapshot(),completed_attempts=journal.completed_attempts,learned_rates=journal.rates())
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('report.json',clean_text(json.dumps(summary,indent=2)))
        archive.writestr('recent-log.txt',clean_text(log))

class SessionStore:
    def __init__(self,path):
        self.path=Path(path);self.condition=threading.Condition();self.pending=None
        self.closed=False;self.error='';self.thread=threading.Thread(target=self.run,daemon=True,name='fishing-save')
        self.thread.start()

    def load(self):
        try:
            if self.path.stat().st_size>12_000_000:raise ValueError('Saved session too large')
            result=json.loads(self.path.read_text(encoding='utf-8-sig'))
            return result if isinstance(result,dict) and result.get('format')==1 else {}
        except FileNotFoundError:return {}
        except (OSError,ValueError):self.error='Saved session could not be read; started a fresh session.';return {}

    def submit(self,value):
        with self.condition:
            if not self.closed:self.pending=value;self.condition.notify_all()

    def run(self):
        while True:
            with self.condition:
                while self.pending is None and not self.closed:self.condition.wait()
                if self.pending is None and self.closed:return
                value=self.pending;self.pending=None
            temporary=self.path.with_suffix('.json.tmp')
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
                temporary.replace(self.path);self.error=''
            except OSError:self.error='Session could not be saved. Check free disk space and folder access.'

    def close(self):
        with self.condition:self.closed=True;self.condition.notify_all()
        self.thread.join(3)
