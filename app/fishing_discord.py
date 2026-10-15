"""Optional Discord cards. Network delivery never runs on the fishing thread."""
import base64
from collections import OrderedDict,deque
import ctypes
from ctypes import wintypes
from datetime import datetime,timezone,timedelta
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from fishing_session import fingerprint

DISCORD_DEFAULTS=dict(discord_enabled=False,discord_url='',discord_catches=True,
                     discord_supplies=True,discord_rods=True,discord_errors=True,discord_live=True,discord_complete=True,discord_health=True)


def webhook_url(value):
    if not isinstance(value,str):raise ValueError('Paste a Discord webhook URL in Settings.')
    value=value.strip()
    if not value:return ''
    try:
        parsed=urllib.parse.urlsplit(value)
        query=urllib.parse.parse_qs(parsed.query,strict_parsing=True)
        valid=(parsed.scheme=='https' and parsed.netloc.lower() in ('discord.com','discordapp.com')
               and not parsed.fragment and
               re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+',parsed.path)
               and not set(query)-{'thread_id','wait'} and
               all(len(values)==1 for values in query.values()) and
               ('thread_id' not in query or re.fullmatch(r'\d+',query['thread_id'][0])) and
               ('wait' not in query or query['wait'][0] in ('true','false')))
    except ValueError:valid=False
    if not valid:raise ValueError('Use an HTTPS Discord webhook URL copied from Discord Integrations.')
    query={'thread_id':query['thread_id'][0]} if 'thread_id' in query else {}
    return urllib.parse.urlunsplit(('https','discord.com',parsed.path,urllib.parse.urlencode(query),''))


class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data,protect):
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    result=Blob()
    function=crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,
                       ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    function.restype=wintypes.BOOL
    if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(result)):
        raise OSError('Windows could not protect/read the saved Discord webhook. Paste it again.')
    try:return ctypes.string_at(result.data,result.size)
    finally:kernel.LocalFree(result.data)


def protect_url(value):
    return 'dpapi:'+base64.b64encode(_crypt(webhook_url(value).encode('utf-8'),True)).decode('ascii')


def unprotect_url(value):
    if not isinstance(value,str) or not value.startswith('dpapi:'):raise ValueError('Invalid saved webhook.')
    return webhook_url(_crypt(base64.b64decode(value[6:],validate=True),False).decode('utf-8'))


def items_from_notice(text):
    text=' '.join(text.lower().split())
    if re.search(r'[-−]\s*\d|\b(?:removed|lost|sold|gave|no|zero|none)\b',text):return []
    text=re.sub(r'^added\s+[il]\s*x\b','added 1x',text)
    suffix=re.fullmatch(r'(.+?)\s+(added|received)',text)
    if suffix:text=suffix[2]+' '+suffix[1]
    patterns=(r'^(?:received|added|obtained)\s*:?\s+(\d+)\s*x?\s+([a-z][a-z0-9 ]{0,60})[.!]?$',
              r'^([a-z][a-z ]{0,60})\s*\+\s*(\d+)(?:\s*x)?$',
              r'^\+\s*(\d+)\s*x?\s+([a-z][a-z ]{0,60})$',
              r'^(?:you (?:have )?)?(?:caught|successfully caught|reeled in|received|added|obtained)\s+(?:(\d+)\s*x?\s+|a |an )?([a-z][a-z ]{0,60})[.!]?$',
              r'^(?:received|added|obtained)\s*:?\s+([a-z][a-z ]{0,60})\s+x?\s*(\d+)[.!]?$')
    for index,pattern in enumerate(patterns):
        match=re.fullmatch(pattern,text)
        if not match:continue
        quantity,name=match.groups() if index not in (1,4) else match.groups()[::-1]
        name=name.strip()
        quantity=int(quantity) if quantity else None
        if quantity is not None and not 1<=quantity<=9999:return []
        if name in ('fish','fish added','successfully caught a fish','artificial bait','water','sandwich') or name.endswith('rod'):return []
        return [(name.title(),quantity)]
    return []


def card(title,description,fields=(),color=0x5BD1E7):
    embed=dict(description=description[:3000],color=color,
        fields=[dict(name=name,value=str(value)[:900],inline=True) for name,value in fields],
        footer={'text':'Fishing Helper'},timestamp=datetime.now(timezone.utc).isoformat())
    if title:embed['title']=title[:256]
    return dict(username='Fishing Helper',allowed_mentions={'parse':[]},embeds=[embed])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def number(value,default=1):
    try:
        value=float(value)
        return max(0,value) if math.isfinite(value) else default
    except (TypeError,ValueError,OverflowError):return default


class DiscordNotifier:
    def __init__(self,opener=None):
        self.condition=threading.Condition()
        self.stop=threading.Event()
        self.queue=deque()
        self.seen=OrderedDict()
        self.thread=None
        self.opener=opener or urllib.request.build_opener(NoRedirect())
        self.settings=dict(DISCORD_DEFAULTS)
        self.generation=0
        self.destination_generation=0
        self.sent=self.failed=self.dropped=0
        self.status='Discord notifications are off.'
        self.live_id=None
        self.health_id=None
        self.uncertain_live_posts=OrderedDict()

    def configure(self,settings):
        config={key:settings.get(key,default) for key,default in DISCORD_DEFAULTS.items()}
        config['discord_url']=webhook_url(config['discord_url'])
        if config['discord_enabled'] and not config['discord_url']:
            raise ValueError('Paste a Discord webhook URL before enabling notifications.')
        with self.condition:
            if config!=self.settings:
                self.generation+=1
                self.dropped+=len(self.queue);self.queue.clear();self.seen.clear()
                if config['discord_url']!=self.settings['discord_url']:
                    self.live_id=None
                    self.health_id=None
                    self.uncertain_live_posts.clear()
                    self.destination_generation+=1
                self.settings=config
                self.status='Discord ready.' if config['discord_enabled'] else 'Discord notifications are off.'
                self.condition.notify_all()

    def wants(self,kind):
        with self.condition:
            return bool(self.settings['discord_enabled'] and self.settings['discord_url'] and
                        self.settings.get('discord_'+kind,False) and not self.stop.is_set())

    def snapshot(self):
        with self.condition:
            return f'{self.status} Sent {self.sent} · Failed/unconfirmed {self.failed} · Dropped {self.dropped} · Queued {len(self.queue)}'

    def saved_live_card(self):
        with self.condition:
            return dict(id=self.live_id,health_id=self.health_id,destination=fingerprint(self.settings['discord_url']))

    def restore_live_card(self,value):
        with self.condition:
            if (isinstance(value,dict) and self.settings['discord_url'] and value.get('destination')==fingerprint(self.settings['discord_url'])):
                for key,attribute in (('id','live_id'),('health_id','health_id')):
                    identifier=value.get(key)
                    if isinstance(identifier,str) and re.fullmatch(r'\d{1,30}',identifier):setattr(self,attribute,identifier)

    def enqueue(self,kind,key,payload,force=False):
        with self.condition:
            if self.stop.is_set() or not self.settings['discord_url']:return False
            if not force and not self.wants(kind):return False
            if key in self.seen:return False
            live=kind=='health' or (kind=='catches' and key[0]=='catch' and self.settings['discord_live'])
            if live and self.supplemental_blocked(key):
                self.dropped+=1
                return False
            if live:
                for previous in list(self.queue):
                    if previous[-1] and previous[0]==kind:self.queue.remove(previous);self.dropped+=1
            if len(self.queue)>=128:
                victim=next((item for item in self.queue if item[0]=='health'),None)
                if victim is None:victim=next((item for item in self.queue if item[0]=='catches'),None)
                if victim is None:
                    self.dropped+=1;self.status='Notification queue full.';return False
                self.queue.remove(victim);self.dropped+=1
            self.seen[key]=True
            while len(self.seen)>1024:self.seen.popitem(last=False)
            event=(kind,key,payload,self.generation,self.settings['discord_url'],live)
            if kind=='health' or (kind=='catches' and key[0]=='catch'):self.queue.append(event)
            else:self.queue.appendleft(event)
            self.status='Notification queued.'
            if self.thread is None:
                self.thread=threading.Thread(target=self.run,name='fishing-discord',daemon=True)
                try:self.thread.start()
                except RuntimeError:
                    for queued in self.queue:self.seen.pop(queued[1],None)
                    self.failed+=len(self.queue);self.queue.clear();self.thread=None
                    self.status='Notification sender could not start.'
                    return False
            self.condition.notify_all()
            return True

    @staticmethod
    def live_cycle(key):
        if len(key)==2 and key[0]=='catch' and type(key[1]) is int:return key[1]
        if len(key)>=3 and key[:2]==('catch','items') and type(key[2]) is int:return key[2]
        return None

    def supplemental_blocked(self,key):
        # Called under the sender condition, both when queued and before HTTP.
        # Display text can change after a test/alert and is not delivery evidence.
        return (self.live_id is None and key[:2]==('catch','items') and
                self.live_cycle(key) in self.uncertain_live_posts)

    def test(self):
        return self.enqueue('errors',('test',time.monotonic()),card('🐟 Webhook test',
            'Fishing Helper is connected. Catch and supply alerts will appear here.'),force=True)

    def reset_session(self,payload):
        """Invalidate old session deliveries, retaining this destination's live card."""
        with self.condition:
            self.generation+=1
            self.dropped+=len(self.queue)
            self.queue.clear();self.seen.clear()
            self.uncertain_live_posts.clear()
            self.condition.notify_all()
            # Honor the master switch. An existing live card can still be reset
            # when automatic catch notifications have been switched off.
            if (self.stop.is_set() or not self.settings['discord_enabled'] or
                    not self.settings['discord_url'] or
                    not (self.settings['discord_catches'] or self.live_id)):
                return False
            return self.enqueue('catches',('catch','reset',self.generation),payload,force=True)

    def close(self):
        self.stop.set()
        with self.condition:
            self.dropped+=len(self.queue);self.queue.clear();self.condition.notify_all()

    def run(self):
        not_before=0
        rate_url=None
        while not self.stop.is_set():
            with self.condition:
                while not self.queue and not self.stop.is_set():self.condition.wait(.5)
                if self.stop.is_set():return
                event=self.queue.popleft()
            kind,key,payload,generation,url,live=event
            if url!=rate_url:rate_url,not_before=url,0
            for attempt in range(3):
                with self.condition:
                    while (not self.stop.is_set() and generation==self.generation and
                           time.monotonic()<not_before):
                        self.condition.wait(min(.5,not_before-time.monotonic()))
                    if self.stop.is_set():return
                    if generation!=self.generation:
                        self.dropped+=1;break
                    destination_generation=self.destination_generation
                    if live and self.supplemental_blocked(key):
                        self.dropped+=1
                        break
                    editing_id=(self.health_id if kind=='health' else self.live_id) if live else None
                query=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query));query['wait']='true'
                endpoint=url.split('?')[0]+'?'+urllib.parse.urlencode(query)
                editing=bool(editing_id)
                if editing:
                    query.pop('wait',None)
                    endpoint=url.split('?')[0]+'/messages/'+editing_id
                    if query:endpoint+='?'+urllib.parse.urlencode(query)
                request=urllib.request.Request(endpoint,data=json.dumps(payload).encode('utf-8'),
                    headers={'Content-Type':'application/json','User-Agent':'FishingHelper'},method='PATCH' if editing else 'POST')
                delay=None
                try:
                    with self.opener.open(request,timeout=6) as response:
                        # A new live card needs the ID returned by wait=true.
                        # 204 succeeds for standalone sends/edits, but cannot
                        # authorize reposting a live card whose ID is unknown.
                        status=response.status
                        if status not in (200,204):raise RuntimeError('Unexpected Discord response.')
                        body=response.read(16384)
                        if live and not editing:
                            if status!=200:raise RuntimeError('No live message ID returned.')
                            identifier=str(json.loads(body).get('id',''))
                            if not re.fullmatch(r'\d+',identifier):raise RuntimeError('No live message ID returned.')
                            with self.condition:
                                # A reset can arrive during the first POST. Keep
                                # its returned ID so the reset edits that card.
                                if destination_generation==self.destination_generation:
                                    if kind=='health':self.health_id=identifier
                                    else:self.live_id=identifier
                        if response.headers.get('X-RateLimit-Remaining')=='0':
                            not_before=time.monotonic()+number(response.headers.get('X-RateLimit-Reset-After'))
                    with self.condition:
                        self.sent+=1
                        if generation==self.generation:self.status='Last notification delivered.'
                    break
                except urllib.error.HTTPError as error:
                    if error.code==429:
                        try:delay=number(json.loads(error.read(16384)).get('retry_after'))
                        except (ValueError,TypeError,AttributeError):delay=number(error.headers.get('Retry-After'))
                    elif editing and error.code==404:
                        with self.condition:
                            attribute='health_id' if kind=='health' else 'live_id'
                            if destination_generation==self.destination_generation and getattr(self,attribute)==editing_id:setattr(self,attribute,None)
                        delay=0
                    elif 500<=error.code<600:delay=2**attempt
                    message=('Webhook invalid or deleted. Paste a new URL.' if error.code in (401,403,404)
                             else f'Discord rejected notification (HTTP {error.code}).')
                    error.close()
                except Exception:
                    # A timeout can occur after Discord accepts the POST. Do
                    # not replay an uncertain request and create duplicate cards.
                    message='Delivery unconfirmed; check Discord and your connection.'
                    cycle=self.live_cycle(key)
                    if live and not editing and cycle is not None:
                        with self.condition:
                            if destination_generation==self.destination_generation:
                                self.uncertain_live_posts[cycle]=True
                                while len(self.uncertain_live_posts)>64:self.uncertain_live_posts.popitem(last=False)
                if delay is not None:
                    not_before=time.monotonic()+delay
                if delay is not None and attempt<2:
                    with self.condition:self.status='Discord busy; retrying in the background.'
                    continue
                with self.condition:
                    self.failed+=1
                    if generation==self.generation:self.status=message
                break


class FishingAlerts:
    def __init__(self,stats,notifier):
        self.stats,self.notifier=stats,notifier
        self.records=OrderedDict()
        self.next_flush=0
        self.amounts={}
        self.last_inventory_sample=-float('inf')
        self.empty_alerted=set()
        self.journal=None
        self.health_state=None;self.health_reason='';self.health_next=0;self.health_minutes=15;self.health_serial=0

    def set_health_state(self,text,minutes=15):
        self.health_state=str(text)[:350];self.health_reason='';self.health_minutes=max(5,min(120,int(minutes)))

    def health_stopped(self,reason,state='Paused'):
        if self.health_state is None:return False
        self.health_state=state;self.health_reason=str(reason)[:500]
        return self.update_health(force=True)

    def update_health(self,force=False):
        if self.health_state is None or not self.notifier.wants('health'):return False
        now=time.monotonic()
        if not force and now<self.health_next:return False
        stats=self.stats.snapshot();last=None
        if self.journal:
            self.journal.sync(self.stats)
            last=next((row for row in reversed(self.journal.history()) if row['result']=='catch'),None)
        caught=last['time'] if last else 'Not confirmed this session'
        age=stats.get('last_catch_age');age_text=f'{age//60} min {age%60}s ago' if age is not None else '—'
        description=(f'**Status:** {self.health_state}\nLast successful catch: {caught}\n'
                     f'Since last catch: {age_text}\nSession catches: {stats["catches"]}\n'
                     f'Active time: {stats["elapsed"]//60} minutes\nBait left: {self.amount("bait")}\n'
                     f'Food left: {self.amount("food")}\nWater left: {self.amount("water")}\n'
                     f'Stop reason: {self.health_reason or "—"}')
        self.health_serial+=1
        queued=self.notifier.enqueue('health',('health',self.health_serial,now),card('🐟 Fishing health',description))
        if queued:self.health_next=now+self.health_minutes*60
        return queued

    def reset_session(self):
        self.records.clear()
        self.next_flush=0
        description=(f'🐟 **Last catch: —**\nBait left: {self.amount("bait")}\n'
                     f'Food left: {self.amount("food")}\nSession catches: 0\nTime caught: —')
        queued=self.notifier.reset_session(card('',description))
        health_id=getattr(self.notifier,'health_id',None)
        if self.health_state is not None or isinstance(health_id,str):
            self.health_state='Session reset';self.health_reason='';self.health_next=0
            queued=self.update_health(force=True) or queued
        return queued

    def inventory_amounts(self,snapshot):
        if not isinstance(snapshot,tuple) or len(snapshot)!=2:return
        sampled,values=snapshot
        if (type(sampled) not in (int,float) or not math.isfinite(sampled) or
                not isinstance(values,dict) or sampled<=self.last_inventory_sample):return
        self.last_inventory_sample=sampled
        if self.journal:self.journal.inventory(snapshot,self.stats.snapshot()['elapsed'])
        for kind,(previous_sample,count,estimated) in list(self.amounts.items()):
            if type(values.get(kind)) is not int or values[kind]<0:
                self.amounts[kind]=(previous_sample,count,True)
        for kind,count in values.items():
            if type(count) is int and count>=0 and (kind not in self.amounts or sampled>self.amounts[kind][0]):
                self.amounts[kind]=(sampled,count,False)
                if count>0:self.empty_alerted.discard(kind)
                if kind in ('food','water') and count==0 and kind not in self.empty_alerted:
                    slot='4' if kind=='food' else '5'
                    queued=self.notifier.enqueue('supplies',('empty',kind,sampled),card(
                        f'{kind.title()} ran out',f'Inventory confirms slot {slot} is empty. Refill it before {"hunger" if kind=="food" else "thirst"} gets low.',color=0xEFC879))
                    if queued:self.empty_alerted.add(kind)

    def consumed(self,kind,sent_at=None):
        if self.journal:self.journal.consumed(kind,sent_at)
        kind='food' if kind=='hunger' else 'water'
        if kind in self.amounts:
            sampled,count,_=self.amounts[kind]
            if sent_at is not None and sampled>sent_at:return
            self.amounts[kind]=(sampled,max(0,count-1),True)

    def amount(self,kind):
        if kind not in self.amounts:return '—'
        _,count,estimated=self.amounts[kind]
        return f'{count} ({"estimated" if estimated else "last inventory check"})'

    def started(self,cycle,slot):
        if not self.notifier.wants('catches'):return
        self.records.setdefault(cycle,dict(slot=slot,items={},deadline=None,notified=False,corrected=False,caught_at=None))
        while len(self.records)>64:self.records.popitem(last=False)

    def observe(self,cycle,text,sampled_at=None):
        if cycle not in self.records or not self.notifier.wants('catches'):return
        record=self.records[cycle]
        changed=False
        for name,quantity in items_from_notice(text):
            previous=record['items'].get(name)
            merged=max(previous or 0,quantity or 0) or None
            changed|=name not in record['items'] or merged!=previous
            record['items'][name]=merged
        if record['deadline'] is None:record['deadline']=time.monotonic()+8
        if record['caught_at'] is None:
            age=max(0,time.monotonic()-sampled_at) if sampled_at is not None else 0
            record['caught_at']=datetime.now().astimezone()-timedelta(seconds=age)
        if changed and record['notified'] and self.notifier.settings.get('discord_live',True):
            with self.stats.lock:results={cid:a['result'] for cid,a in self.stats.attempts.items()}
            latest=next((cid for cid,entry in reversed(list(self.records.items()))
                         if entry['notified'] and results.get(cid)=='catch'),None)
            if latest==cycle:
                self.notifier.enqueue('catches',('catch','items',cycle,tuple(sorted(record['items'].items()))),self.catch_payload(record))

    def catch_payload(self,record=None):
        descriptions=[f'**{quantity}× {name}**' if quantity is not None else f'**{name}** (quantity unreadable)'
                      for name,quantity in sorted(record['items'].items())] if record else []
        item=' · '.join(descriptions) if descriptions else ('**Item unreadable**' if record else '—')
        caught=(record['caught_at'] or datetime.now().astimezone()).strftime('%I:%M:%S %p %Z') if record else '—'
        description=(f'🐟 **Last catch: {item.replace("**","")}**\n'
                     f'Bait left: {self.amount("bait")}\nFood left: {self.amount("food")}\n'
                     f'Session catches: {self.stats.snapshot()["catches"]}\nTime caught: {caught}')
        return card('',description)

    def flush(self,force=False):
        now=time.monotonic()
        if not force and now<self.next_flush:return
        self.next_flush=now+.2
        self.update_health()
        if not self.notifier.wants('catches'):
            self.records.clear();return
        with self.stats.lock:
            results={cycle:(a['result'],a['end']) for cycle,a in self.stats.attempts.items()}
        for cycle,record in list(self.records.items()):
            result,ended=results.get(cycle,(None,None))
            if result=='failure' and record['notified'] and not record['corrected']:
                record['corrected']=self.notifier.enqueue('catches',('correction',cycle),card(
                    'Catch correction',f'Catch #{cycle} was later recognized as a failure.',color=0xE87373))
                if record['corrected'] and self.notifier.settings.get('discord_live',True):
                    previous=next((entry for cid,entry in reversed(list(self.records.items()))
                                   if results.get(cid,(None,None))[0]=='catch' and entry['notified']),None)
                    self.notifier.enqueue('catches',('catch','correction',cycle),self.catch_payload(previous))
            if (result!='catch' or record['notified'] or ended is None or
                    (not force and record['deadline'] is not None and now<record['deadline'])):continue
            record['notified']=self.notifier.enqueue('catches',('catch',cycle),self.catch_payload(record))

    def rod(self,old,new,confirmed_break=False):
        title='Rod broke' if confirmed_break else 'Rod unavailable'
        action=f'Switching to slot {new}.' if new else 'No enabled backup remains; fishing will pause.'
        self.notifier.enqueue('rods',('rod',old,time.monotonic()),card(title,
            f'Slot {old} no longer has a usable fishing rod. {action}',color=0xEFC879))

    def pause(self,reason):
        self.health_stopped(reason)
        lowered=reason.lower()
        if lowered.startswith('pre-run supply check:'):kind,title='supplies','Pre-run check — fishing paused'
        elif 'bait' in lowered:kind,title='supplies','Bait needs attention — fishing paused'
        elif 'slot 4' in lowered or 'restore hunger' in lowered:kind,title='supplies','Food needs attention — fishing paused'
        elif 'slot 5' in lowered or 'restore thirst' in lowered:kind,title='supplies','Water needs attention — fishing paused'
        elif 'no fishing rods' in lowered or 'no rod remains' in lowered:kind,title='rods','No usable rods — fishing paused'
        elif 'inventory is full' in lowered or 'inventory full' in lowered:kind,title='supplies','Inventory full — fishing paused'
        else:kind,title='errors','Fishing paused — needs attention'
        if ('no bait' in lowered or 'have no bait' in lowered or 'dont have any bait' in lowered
                or 'bait slot 1 is empty' in lowered):
            title='Out of bait — fishing paused'
        self.notifier.enqueue(kind,('pause',reason,time.monotonic()),card(title,reason,color=0xE87373))

    def complete(self,reason,stats):
        self.health_stopped(reason,state='Completed')
        session=self.journal.session_id if self.journal else 'current'
        self.notifier.enqueue('complete',('complete',session,reason,stats['cycles'],stats['elapsed']),card('🐟 Fishing run complete',
            f'{reason}\nSession catches: {stats["catches"]}\nActive time: {stats["elapsed"]//60} minutes.\nFishing has stopped.',color=0x77DDBA))
