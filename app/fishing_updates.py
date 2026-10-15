"""Opt-in HTTPS release feed, bounded downloads and verified restart handoff."""
import hashlib,json,os,re,shutil,subprocess,threading,urllib.parse,urllib.request,uuid
from pathlib import Path

DEFAULT_FEED='https://github.com/Bray12541/fishing-helper/releases/latest/download/latest.json'
UPDATE_DEFAULTS=dict(update_feed_url=DEFAULT_FEED,update_check_startup=True)
MAX_DOWNLOAD=500_000_000

def feed_url(value):
    if not isinstance(value,str):raise ValueError('Update feed: enter an HTTPS URL.')
    value=value.strip()
    if not value:return ''
    parsed=urllib.parse.urlsplit(value)
    if (len(value)>2048 or parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('Update feed: use an HTTPS URL without credentials or a fragment.')
    return value

def version(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,4}(?:\.\d{1,4}){1,3}',value):raise ValueError('Invalid release version.')
    parts=tuple(int(v) for v in value.split('.'));return parts+(0,)*(4-len(parts))

class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        feed_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def release(data,current,folder,online=False):
    if not isinstance(data,dict) or data.get('product')!='FishingHelper' or data.get('schema')!=1:raise ValueError('Invalid update feed.')
    if version(data.get('version'))<=version(current):return None
    digest=data.get('sha256');size=data.get('size')
    if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',digest):raise ValueError('Release checksum is missing.')
    if type(size) is not int or not 512<=size<=MAX_DOWNLOAD:raise ValueError('Invalid release size.')
    url=feed_url(data.get('url',''))
    filename=data.get('filename','')
    if not url:
        if online:raise ValueError('Online release needs an HTTPS download URL.')
        if not isinstance(filename,str) or not re.fullmatch(r'[\w .()-]{1,100}\.exe',filename,re.I):raise ValueError('Invalid local release filename.')
        path=(Path(folder)/filename).resolve()
        if path.parent!=Path(folder).resolve():raise ValueError('Release path is outside its folder.')
    else:path=None
    notes=data.get('notes','')
    if not isinstance(notes,str):notes=''
    return dict(version=data['version'],sha256=digest.lower(),size=size,url=url,path=str(path) if path else None,notes=notes[:1000])

def validate_exe(path,expected):
    digest=hashlib.sha256();count=0
    with Path(path).open('rb') as stream:
        header=stream.read(4096)
        if len(header)<64 or header[:2]!=b'MZ':raise ValueError('Update is not a Windows executable.')
        offset=int.from_bytes(header[60:64],'little')
        if offset<64 or offset>len(header)-4 or header[offset:offset+4]!=b'PE\0\0':raise ValueError('Update executable header is invalid.')
        digest.update(header);count=len(header)
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk);count+=len(chunk)
            if count>expected['size']:raise ValueError('Update size mismatch.')
    if count!=expected['size'] or digest.hexdigest()!=expected['sha256']:raise ValueError('Update checksum or size does not match the feed.')

class UpdateManager:
    def __init__(self,current,folder,staging,opener=None):
        self.current=current;self.folder=Path(folder);self.staging=Path(staging)
        self.opener=opener or urllib.request.build_opener(SecureRedirect())
        self.lock=threading.RLock();self.thread=None;self.cancel=threading.Event();self.generation=0
        self.available=None;self.ready=None;self.installing=False;self.state='Check for an update or enter your HTTPS feed URL.'
        self.source='';self.handed_off=False
    def snapshot(self):
        with self.lock:return self.state,self.available,self.installing,self.ready
    def check(self,url=''):
        url=feed_url(url)
        with self.lock:
            if self.thread and self.thread.is_alive():return False
            self.generation+=1;generation=self.generation;self.source=url
            self.cancel.clear();self.available=None;self.ready=None;self.state='Checking for updates…'
        def job():
            try:
                if url:
                    request=urllib.request.Request(url,headers={'User-Agent':'FishingHelper-Updater','Accept':'application/json'})
                    with self.opener.open(request,timeout=10) as response:
                        raw=response.read(65537)
                    if len(raw)>65536:raise ValueError('Update feed is too large.')
                else:
                    manifest=self.folder/'fishing-release.json'
                    if not manifest.is_file():
                        self.result(generation,'Online updates need a feed URL. No newer local release was found.');return
                    if manifest.stat().st_size>65536:raise ValueError('Update feed is too large.')
                    raw=manifest.read_bytes()
                item=release(json.loads(raw.decode('utf-8-sig')),self.current,self.folder,bool(url))
                self.result(generation,('Update available: '+item['version']+'. '+item['notes']) if item else 'You have the latest published version.',item)
            except Exception:self.result(generation,'Could not check updates. Verify the HTTPS feed and try again.')
        return self.start(job)
    def result(self,generation,state,item=None,ready=None):
        with self.lock:
            if generation!=self.generation or self.cancel.is_set():return
            self.state=state
            if item is not None:self.available=item
            if ready is not None:self.ready=ready
            self.installing=False if ready is not None or state.startswith(('Could not','Update failed')) else self.installing
    def start(self,job):
        self.thread=threading.Thread(target=job,daemon=True,name='fishing-updates')
        try:self.thread.start();return True
        except RuntimeError:self.state='Could not start the updater.';self.installing=False;return False
    def prepare(self):
        with self.lock:
            if not self.available or (self.thread and self.thread.is_alive()):return False
            item=dict(self.available);generation=self.generation;self.installing=True;self.ready=None;self.state='Preparing and verifying update…'
        def job():
            stage=self.staging/uuid.uuid4().hex
            try:
                stage.mkdir(parents=True,exist_ok=False);target=stage/'new.exe'
                if item['url']:
                    request=urllib.request.Request(item['url'],headers={'User-Agent':'FishingHelper-Updater'})
                    source=self.opener.open(request,timeout=10)
                else:source=Path(item['path']).open('rb')
                with source as stream,target.open('wb') as output:
                    count=0
                    while True:
                        if self.cancel.is_set():raise ValueError('Cancelled')
                        chunk=stream.read(1024*1024)
                        if not chunk:break
                        count+=len(chunk)
                        if count>item['size']:raise ValueError('Download exceeds expected size.')
                        output.write(chunk)
                validate_exe(target,item)
                self.result(generation,'Update verified. Closing the app to install…',item,str(target))
            except Exception:
                try:(stage/'new.exe').unlink(missing_ok=True)
                except OSError:pass
                try:stage.rmdir()
                except OSError:pass
                self.result(generation,'Update failed. Current app was kept. Check the feed/download and try again.')
        return self.start(job)
    def handoff(self,target,script):
        with self.lock:
            if not self.ready or self.handed_off:raise ValueError('No verified update is ready.')
            staged=Path(self.ready);item=dict(self.available)
        validate_exe(staged,item)
        target=Path(target).resolve()
        if target.suffix.lower()!='.exe' or not target.is_file():raise ValueError('The installed EXE could not be found.')
        # Copy the helper out of PyInstaller's temporary bundle before exiting.
        helper=staged.parent/'apply-update.ps1';shutil.copyfile(script,helper)
        plan=staged.parent/'plan.json'
        plan.write_text(json.dumps(dict(pid=os.getpid(),target=str(target),source=str(staged),sha256=item['sha256'],size=item['size'])),encoding='utf-8')
        subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(helper),'-PlanPath',str(plan)],
            creationflags=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.handed_off=True
    def close(self):
        if not self.handed_off:self.cancel.set()
