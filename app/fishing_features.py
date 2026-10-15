"""Local outcome recognition and session bookkeeping, off the timing thread."""
import base64
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time

import cv2
import numpy as np
from fishing_supplies import NeedsHud, RodInventory, InventoryAmounts, choose_percentage, ROD_PRIORITY, parse_rod_slots, inventory_quantity
from fishing_paths import node_path

FISH = (r'(?:(?:atlantic|pacific|bluefin|yellowfin|sea|red|black|white|great white|rainbow|brown|brook|lake|'
        r'chinook|coho|pink|sockeye|king|tiger|hammerhead|bull|largemouth|smallmouth) )?'
        r'(?:fish|salmon|trout|bass|carp|tuna|shark|pike|perch|cod|catfish|sturgeon|mackerel|snapper|'
        r'sardine|mahi|barracuda|tilapia|eel|swordfish|flounder|halibut|lobster|crab|anchovy|herring|'
        r'grouper|piranha|pollock|walleye|bluefish|whitefish|pufferfish|bluegill|blue gill|mullet|mussel|oyster|kingfish)s?')
ROD = r'(?:(?:fishing|titanium) )?rod'
REWARD_GRACE = 8


def normalize_notice(text):
    text = re.sub(r"[^a-z0-9 ]", '', text.lower())
    return ' '.join(text.split())

def inventory_header(text):
    text = normalize_notice(text)
    # In this compact bold font Windows OCR confuses the numeral 1 with I/L.
    text = re.sub(r'^added\s+[il]\s*x\b', 'added 1x', text)
    return re.sub(r'^(added|removed)\s+(\d+)\s*x\b',r'\1 \2x',text)


def bonus_item_label(text):
    """Only used beneath a spatially aligned ADDED inventory-card header."""
    name=normalize_notice(text)
    if not re.fullmatch(r'[a-z][a-z0-9]*(?: [a-z0-9]+){0,5}',name) or len(name)>60:return None
    if (name in ('water','sandwich','food','bait','artificial bait','added','removed') or
            re.search(r'\b(?:rod|added|removed|received|caught|lost|sold|no|zero|unlocked|youve|level|experience|congratulations)\b',name)):
        return None
    return name


def classify_notice(text):
    raw = text.lower().strip()
    text = inventory_header(text)
    if re.match(r'^(?:(?:your|the|you) )?'+ROD+r' (?:has |is |was )?(?:broken|broke|snapped|destroyed)\b', text):
        return 'broken'
    if re.match(r'^you (?:have )?(?:broke|broken|destroyed) (?:your |the )?'+ROD+r'\b', text):
        return 'broken'
    if re.fullmatch(r'removed\s+[1-9]\d*\s*x?\s+'+ROD, text):
        return 'broken'
    # Require a direct notification; usernames, timestamps and chat prefixes
    # must not turn someone else's catch into this player's result.
    if (re.match(r'^you (?:have )?(?:caught|successfully caught|reeled in)\b', text)
            and not re.search(r'\b(?:no|zero|0)\b', text)
            and re.search(r'\b' + FISH + r'\b', text)):
        return 'catch'
    if re.match(r"^you (?:didnt|did not|dont|do not) have enough strength to catch", text):
        return 'failure'
    if re.match(r'^you (?:failed to catch|lost (?:the|your) fish|didnt catch|did not catch)\b', text):
        return 'failure'
    if re.match(r'^(?:the fish (?:escaped|got away)|fishing failed)\b', text):
        return 'failure'
    if re.match(r'^(?:you (?:need|must) (?:to equip (?:a |the |your )?(?:fishing )?rod|a fishing rod)|no fishing rod|fishing rod (?:is )?not equipped)\b', text):
        return 'rod'
    if re.match(r'^(?:you (?:have no|dont have any|do not have any|need more) bait|no bait|inventory (?:is )?full|your inventory is full)\b', text):
        return 'blocked'
    # Item gains are weaker evidence than a catch notice. The worker accepts
    # them only after an E attempt and a disappearing fishing challenge.
    # Inspect signs before normalizing: Fish -1 must never become Fish +1.
    if not re.search(r'[-−]\s*\d|\b(?:removed|lost|sold|gave|no|zero|0)\b', raw):
        item = r'(?:(?:a|an|[1-9]\d*\s*x?)\s+)?' + FISH + r'(?:\s+x?\s*[1-9]\d*\s*x?)?'
        if re.fullmatch(r'(?:you (?:have )?)?(?:received|added|obtained)\s+(?:item\s+)?' + item, text):
            return 'gain'
        if re.fullmatch(item + r'\s+(?:added|received)', text):
            return 'gain'
        if (re.fullmatch(FISH + r'\s*\+\s*[1-9]\d*(?:\s*x)?', raw)
                or re.fullmatch(r'\+\s*[1-9]\d*\s*x?\s*' + FISH, raw)):
            return 'gain'
    return None


class NoticeEdges:
    """A sticky toast yields one event, and never carries into the next cycle."""
    def __init__(self):
        self.visible = set()
        self.last_seen = {}

    def update(self, lines, sampled_at, boxes=None):
        # Joining adjacent lines handles wrapped toasts without searching arbitrary
        # substrings inside chat. Every candidate must start with a notice phrase.
        texts = []
        bonuses=set()
        for index, text in enumerate(lines):
            if classify_notice(text):
                texts.append(text)
            elif (index + 1 < len(lines) and not re.search(r'[.!?]\s*$', text)
                  and (boxes is None or self.neighbors(boxes[index], boxes[index+1]))):
                texts.append(text + ' ' + lines[index + 1])
            # Inventory cards put an item picture between ADDED 1X and ANCHOVY.
            # Pair the header with an aligned fish label across that picture;
            # arbitrary neighboring chat lines cannot supply the item name.
            if re.fullmatch(r'added\s+[1-9]\d*\s*x?', inventory_header(text)):
                for other in range(index+1, len(lines) if boxes is not None else min(len(lines), index+2)):
                    if (re.fullmatch(FISH, normalize_notice(lines[other])) and
                            (boxes is None or self.inventory_neighbors(boxes[index], boxes[other]))):
                        texts.append(inventory_header(text) + ' ' + lines[other])
                        break
                    if (boxes is not None and self.inventory_neighbors(boxes[index],boxes[other])
                            and bonus_item_label(lines[other]) is not None):
                        bonuses.add(('bonus',inventory_header(text)+' '+normalize_notice(lines[other])))
                        break
            if re.fullmatch(r'removed\s+[1-9]\d*\s*x?', normalize_notice(text)):
                for other in range(index+1, len(lines) if boxes is not None else min(len(lines), index+2)):
                    if (re.fullmatch(ROD, normalize_notice(lines[other])) and
                            (boxes is None or self.inventory_neighbors(boxes[index],boxes[other]))):
                        texts.append(text+' '+lines[other])
                        break
        current = {(kind, inventory_header(text)) for text in texts
                   if (kind := classify_notice(text))}
        current.update(bonuses)
        # Keep notices through brief OCR dropouts. Punctuation changes are not
        # new catches, and a single unreadable scan must not clear a sticky toast.
        events = sorted(key for key in current
                        if key not in self.visible and
                        (key not in self.last_seen or sampled_at-self.last_seen[key] >= 1))
        self.last_seen = {key: seen for key, seen in self.last_seen.items() if sampled_at-seen < 30}
        self.last_seen.update({key: sampled_at for key in current})
        if len(self.last_seen) > 64:
            self.last_seen = dict(sorted(self.last_seen.items(), key=lambda pair: pair[1])[-64:])
        self.visible = current
        # Wrapped and unwrapped forms of the same notice count once per scan.
        by_kind = {}
        gains=[]
        for kind, text in events:
            if kind in ('gain','bonus'):gains.append((sampled_at,kind,text))
            else:by_kind.setdefault(kind, (sampled_at, kind, text))
        return list(by_kind.values())+gains

    @staticmethod
    def neighbors(first, second):
        x, y, width, height = first
        nx, ny, nw, nh = second
        # Wrapped notifications share a column and are at most a line apart.
        return (abs(ny-y) <= max(height, nh)*2.5 and
                nx <= x+width and x <= nx+nw)

    @staticmethod
    def inventory_neighbors(first, second):
        x, y, width, height = first
        nx, ny, nw, nh = second
        overlap = max(0, min(x+width, nx+nw)-max(x, nx))
        return (max(height, nh)*6 <= ny-y <= max(height, nh)*14 and
                overlap >= min(width, nw)*.6 and
                abs((x+width/2)-(nx+nw/2)) <= max(width,nw)*.4)


class SessionStats:
    def __init__(self, resume=None):
        self.lock = threading.RLock()
        self.values = dict(catches=0, failures=0, unknown=0, cycles=0,
                           inputs=0, stages=0, retries=0, recoveries=0)
        self.last_result = 'No results yet'
        self.attempts = {}
        self.next_attempt = 0
        self.active_since = None
        self.active_seconds = 0
        self.last_catch_at = None
        if resume is not None:
            for key in self.values:
                value = resume.get(key, 0)
                if type(value) is not int or value < 0:
                    raise ValueError('Invalid session counter: ' + key)
                self.values[key] = value
            self.values['cycles'] = sum(self.values[key] for key in ('catches', 'failures', 'unknown'))
            self.last_result = str(resume.get('last_result', 'Restored session counters'))[:80]

    def reset(self):
        """Clear a stopped session, preserving unique attempt IDs across resets."""
        with self.lock:
            for key in self.values:self.values[key] = 0
            self.last_result = 'No results yet'
            self.attempts.clear()
            self.active_since = None
            self.active_seconds = 0
            self.last_catch_at = None

    def begin(self, started):
        with self.lock:
            self.next_attempt += 1
            cycle = self.next_attempt
            self.attempts[cycle] = dict(start=started, challenge=None, input=None,
                                       absent=None, end=None, result=None, pending_stage=False,
                                       confirmed_at=None,final_stage_counted=False)
            if len(self.attempts) > 64:
                del self.attempts[next(iter(self.attempts))]
            return cycle

    def observe(self, cycle, *, challenge=None, sent=None, absent=None):
        with self.lock:
            attempt = self.attempts[cycle]
            if challenge is not None and attempt['challenge'] is None:
                attempt['challenge'] = challenge
            if sent is not None:
                attempt['input'] = sent
            attempt['absent'] = absent

    def end(self, cycle, ended, pending_stage=False):
        with self.lock:
            if self.attempts[cycle]['end'] is None:
                self.attempts[cycle]['end'] = ended
                self.attempts[cycle]['pending_stage'] = pending_stage
                if pending_stage and self.attempts[cycle]['result'] == 'catch':
                    self.values['stages'] += 1
                    self.attempts[cycle]['pending_stage'] = False
                    self.attempts[cycle]['final_stage_counted'] = True

    def notice_cycle(self, sampled_at, kind):
        with self.lock:
            attempts = list(self.attempts.items())
            containing = [(cycle, attempt) for cycle, attempt in attempts
                          if attempt['start'] <= sampled_at and
                          (attempt['end'] is None or sampled_at <= attempt['end'])]
            cycle, attempt = containing[-1] if containing else (None, None)
            # A reward can appear after re-equipping, before the next challenge.
            # It belongs to the preceding attempt, including an already counted
            # one: duplicate wording must not manufacture a second catch.
            if kind in ('catch', 'gain', 'bonus', 'failure') and (attempt is None or attempt['challenge'] is None
                    or sampled_at < attempt['challenge']):
                previous = [(cid, a) for cid, a in attempts if a['end'] is not None
                            and a['challenge'] is not None
                            and 0 <= sampled_at-a['end'] <= REWARD_GRACE]
                if previous:
                    cycle, attempt = previous[-1]
            if kind in ('gain','bonus'):
                if (attempt is None or attempt['input'] is None
                        or sampled_at < attempt['input']
                        or (attempt['absent'] is None and attempt['end'] is None)):
                    return None
                if attempt['absent'] is not None and sampled_at < attempt['absent']-.5:
                    return None
            return cycle

    def add(self, **counts):
        with self.lock:
            for key, value in counts.items():
                self.values[key] += value

    def finish(self, result, cycle=None, confirmed_at=None):
        with self.lock:
            old = self.attempts[cycle]['result'] if cycle is not None else None
            if old == result or old == 'failure' or (old == 'catch' and result == 'unknown'):
                return False
            key = {'catch': 'catches', 'failure': 'failures'}.get(result, 'unknown')
            if old is not None:
                old_key = {'catch': 'catches', 'failure': 'failures'}.get(old, 'unknown')
                self.values[old_key] -= 1
            else:
                self.values['cycles'] += 1
            self.values[key] += 1
            if result == 'catch':
                confirmed_at = time.monotonic() if confirmed_at is None else confirmed_at
                self.last_catch_at = max(self.last_catch_at or confirmed_at,confirmed_at)
            if cycle is not None:
                if old == 'catch' and result == 'failure' and self.attempts[cycle]['final_stage_counted']:
                    self.values['stages'] -= 1
                    self.attempts[cycle]['final_stage_counted'] = False
                previous_time = self.attempts[cycle]['confirmed_at']
                self.attempts[cycle]['result'] = result
                self.attempts[cycle]['confirmed_at'] = confirmed_at if result == 'catch' else None
                if old == 'catch' and result != 'catch' and previous_time == self.last_catch_at:
                    self.last_catch_at = max((a['confirmed_at'] for a in self.attempts.values()
                        if a['result'] == 'catch' and a['confirmed_at'] is not None),default=None)
                if result == 'catch' and self.attempts[cycle]['pending_stage']:
                    self.values['stages'] += 1
                    self.attempts[cycle]['pending_stage'] = False
                    self.attempts[cycle]['final_stage_counted'] = True
            self.last_result = {'catch': 'Catch confirmed', 'failure': 'Failure confirmed'}.get(result, 'Unknown result')
            return True

    def start_timer(self, now):
        with self.lock:
            if self.active_since is None:self.active_since = now

    def pause_timer(self, now):
        with self.lock:
            if self.active_since is not None:
                self.active_seconds += max(0,now-self.active_since)
                self.active_since = None

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            data = dict(self.values, last_result=self.last_result)
            elapsed = self.active_seconds+(max(0,now-self.active_since) if self.active_since is not None else 0)
            data['elapsed'] = int(elapsed)
            data['catches_per_hour'] = f"{self.values['catches']*3600/elapsed:.1f}" if elapsed>=1 else '—'
            data['last_catch_age'] = int(max(0,now-self.last_catch_at)) if self.last_catch_at is not None else None
        known = data['catches'] + data['failures']
        data['rate'] = f"{data['catches'] / known:.0%}" if known else '—'
        return data


class InventoryCards:
    """Find the supplied ADDED heading and magnify its small inventory card."""
    def __init__(self):
        template = cv2.imread(str(Path(__file__).with_name('fishing-added-header.png')), cv2.IMREAD_GRAYSCALE)
        self.templates = []
        if template is not None:
            for scale in (.75,1,1.25,1.5,1.75,2,2.25,2.5,2.75,3):
                resized = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                self.templates.append((scale, resized))

    def regions(self, image):
        gray = cv2.cvtColor(image[:,:,:3], cv2.COLOR_BGR2GRAY)
        matches = []
        height, width = gray.shape
        for scale, template in self.templates:
            if template.shape[0] >= height or template.shape[1] >= width:
                continue
            scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
            for _ in range(3):
                _, score, _, (x,y) = cv2.minMaxLoc(scores)
                if score < .82:
                    break
                matches.append((score, x, y, scale))
                scores[max(0,y-template.shape[0]):y+template.shape[0],
                       max(0,x-template.shape[1]):x+template.shape[1]] = 0
        regions = []
        for _, x, y, scale in sorted(matches, reverse=True):
            left, top = max(0,round(x-26*scale)), max(0,round(y-10*scale))
            right, bottom = min(width,round(x+78*scale)), min(height,round(y+105*scale))
            if any(abs(left-old[0]) < 50*scale and abs(top-old[1]) < 50*scale for old in regions):
                continue
            regions.append((left,top,right,bottom))
            if len(regions) == 4:
                break
        return regions


class OutcomeMonitor:
    """Windows OCR in a hidden local process; never block marker tracking."""
    def __init__(self, hwnd, capture_factory, bounds, focused, needs_enabled=False,rod_slots=ROD_PRIORITY,capacity_enabled=False):
        self.hwnd, self.capture_factory = hwnd, capture_factory
        self.bounds, self.focused = bounds, focused
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.events = []
        self.state = 'Starting local OCR…'
        self.proc = None
        self.hud_proc = None
        self.hud_io_lock=threading.Lock()
        self.edges = NoticeEdges()
        self.thread = threading.Thread(target=self.run, name='fishing-outcomes', daemon=True)
        self.initialized = threading.Event()
        self.waiting_prompt = False
        self.prompt_sampled_at = 0
        self.candidate = ''
        self.cards = InventoryCards()
        self.needs_hud = NeedsHud() if needs_enabled else None
        self.needs = {}
        self.needs_low_confirmed = {}
        self.needs_debug = {}
        self.inventory_reader = RodInventory()
        self.amounts_reader = InventoryAmounts()
        self.inventory_amounts = None
        self.rod_slots = parse_rod_slots(rod_slots)
        self.inventory = None
        self.inventory_debug = {}
        self.inventory_visible = False
        self.inventory_requested = False
        self.fishing_activity_at = 0
        self.reward_until = 0
        self.needs_requested = False
        self.last_full_scan = -float('inf')
        self.reward_scans = 0
        self.reward_cards = 0
        self.reward_events = 0
        self.hud_frame = None
        self.hud_thread = None
        self.inventory_generation = 0
        self.needs_error = ''
        self.inventory_misses = 0
        self.reward_debug = []
        self.inventory_region=None
        self.inventory_capacity=None;self.capacity_previous=None
        self.capacity_enabled=capacity_enabled
        self.last_progress_at=time.monotonic()

    def health_issue(self,now):
        if self.state.startswith('OCR unavailable'):return self.state
        if self.needs_error:return 'Hunger/thirst OCR unavailable: '+self.needs_error
        if self.initialized.is_set() and now-self.last_progress_at>30:return 'OCR watchdog: no completed scan for 30 seconds.'
        return None

    def reset_needs(self):
        with self.lock:
            self.inventory_generation+=1;self.hud_frame=None
            if self.needs_hud:self.needs_hud.clear()
            self.needs={};self.needs_low_confirmed={}

    @staticmethod
    def capacity_value(words,row):
        value=None
        if row:
            tags,stride=row;left=tags['3'][1];right=tags['5'][1]+stride;y=tags['1'][2]
            header=sorted((w for w in words if left<=w['box'][0]+w['box'][2]/2<=right
                           and y-stride*.65<=w['box'][1]+w['box'][3]/2<y),key=lambda w:w['box'][0])
            raw=''.join(w.get('text','') for w in header).lower().replace(' ','')
            raw=re.sub(r'(?<=/)(\d[\do]*)(?=(?:kg|g)$)',lambda m:m[0].replace('o','0'),raw)
            match=re.fullmatch(r'(\d+(?:[.,]\d{1,3})?)/(\d+(?:[.,]\d{1,3})?)(kg|g)',raw)
            if match:
                used,total=(float(v.replace(',','.')) for v in match.groups()[:2])
                if match[3]=='g':used/=1000;total/=1000
                if 0<=used<=100000 and 0<total<=100000:value=(used,total)
        return value

    def update_capacity(self,words,row,sampled_at):
        value=self.capacity_value(words,row)
        self.inventory_capacity=None
        previous=self.capacity_previous
        if value is not None and previous and 0<=sampled_at-previous[0]<=15 and all(abs(a-b)<=.1 for a,b in zip(value,previous[1])):
            self.inventory_capacity=(sampled_at,(max(value[0],previous[1][0]),min(value[1],previous[1][1])))
        self.capacity_previous=(sampled_at,value) if value is not None else None
        return self.inventory_capacity

    def retry_capacity(self,image,row,words,proc):
        if self.capacity_value(words,row) is not None:return words
        tags,stride=row
        # The weight header is above the last slots. Read the literal slash and
        # unit; missing punctuation is not inferred from two unrelated numbers.
        left=max(0,round(tags['5'][1]-stride*.4));right=min(image.shape[1],round(tags['5'][1]+stride+4))
        top=max(0,round(tags['1'][2]-stride*.4));bottom=max(top+1,tags['1'][2]-3)
        crop=image[top:bottom,left:right,:3]
        result=words
        if not crop.size:return result
        for height in (96,):
            if self.stop.is_set():break
            zoom=min(height/crop.shape[0],2400/crop.shape[1])
            scaled=cv2.resize(crop,None,fx=zoom,fy=zoom,interpolation=cv2.INTER_CUBIC)
            ok,png=cv2.imencode('.png',scaled)
            if not ok:continue
            reply=self.read_reply(proc,base64.b64encode(png).decode('ascii'))
            found=[dict(text=w['text'],box=[left+w['box'][0]/zoom,top+w['box'][1]/zoom,w['box'][2]/zoom,w['box'][3]/zoom]) for w in reply.get('words',[])]
            raw=''.join(w['text'] for w in sorted(found,key=lambda w:w['box'][0])).lower().replace(' ','')
            raw=re.sub(r'(?<=/)(\d[\do]*)(?=(?:kg|g)$)',lambda m:m[0].replace('o','0'),raw)
            if re.fullmatch(r'\d+(?:[.,]\d{1,3})?/\d+(?:[.,]\d{1,3})?(?:kg|g)',raw):return [dict(text=raw,box=[left,top,right-left,bottom-top])]
        if self.hud_proc is not None and not self.stop.is_set():
            images=[]
            # Exclude the colored weight bar below the white header glyphs.
            bottom=max(top+1,round(tags['1'][2]-stride*.15))
            crop=image[top:bottom,left:right,:3]
            gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
            mask=(gray>=230).astype(np.uint8)*255
            points=cv2.findNonZero(mask)
            if points is None:return result
            x,y,w,h=cv2.boundingRect(points)
            variant=255-mask[y:y+h,x:x+w]
            variant=cv2.copyMakeBorder(variant,3,3,3,3,cv2.BORDER_CONSTANT,value=255)
            for zoom in (2,4):
                scaled=cv2.resize(variant,None,fx=zoom,fy=zoom,interpolation=cv2.INTER_CUBIC)
                ok,png=cv2.imencode('.png',scaled)
                if ok:images.append(base64.b64encode(png).decode('ascii'))
            with self.hud_io_lock:
                readings=self.read_reply(self.hud_proc,json.dumps({'mode':'capacity','images':images})).get('readings',[])
            values=[r.get('text','').lower().replace(' ','') for r in readings if r.get('confidence',0)>=80]
            if len(values)==2 and values[0]==values[1] and re.fullmatch(r'\d+(?:[.,]\d{1,3})?/\d+(?:[.,]\d{1,3})?(?:kg|g)',values[0]):
                return [dict(text=values[0],box=[left,top,right-left,bottom-top])]
        return result

    def locate_inventory(self,image,allow_fallback=True):
        """Use the last region as a search hint, validating new pixels each time."""
        shape=image.shape[:2]
        hint=self.inventory_region
        row=None
        if hint is not None and hint[0]==shape:
            left,top,right,bottom=hint[1]
            local=self.inventory_reader.row(image[top:bottom,left:right],allow_fallback=allow_fallback)
            if local is not None:
                tags,stride=local
                row=({slot:(tag[0],tag[1]+left,tag[2]+top,*tag[3:]) for slot,tag in tags.items()},stride)
        if row is None:row=self.inventory_reader.row(image,allow_fallback=allow_fallback)
        if row is not None:
            tags,stride=row
            bounds=(max(0,tags['1'][1]-16),max(0,tags['1'][2]-16),
                    min(shape[1],round(tags['5'][1]+stride+16)),min(shape[0],round(tags['1'][2]+stride+16)))
            self.inventory_region=(shape,bounds)
        return row

    def read_reward_card(self,image,region,proc,sampled_at):
        left,top,right,bottom=region
        lines,boxes,debug=[],[],[]
        for variant in ('color','contrast'):
            if self.stop.is_set():break
            if variant=='contrast':
                # Retry an unreadable toast with a larger, neutral text view.
                # Keep actual heading/quantity evidence; never assume an item.
                margin=round((right-left)*.12)
                left=max(0,region[0]-margin);right=min(image.shape[1],region[2]+margin)
                bottom=min(image.shape[0],region[3]+margin)
            crop=image[top:bottom,left:right,:3]
            if variant=='contrast':
                neutral=np.where(np.ptp(crop.astype(np.int16),axis=2)<60,crop.min(axis=2),0).astype(np.uint8)
                crop=cv2.threshold(neutral,120,255,cv2.THRESH_BINARY)[1]
            zoom=3 if variant=='color' else 4
            crop=cv2.resize(crop,None,fx=zoom,fy=zoom,interpolation=cv2.INTER_CUBIC)
            border=16
            crop=cv2.copyMakeBorder(crop,border,border,border,border,cv2.BORDER_CONSTANT,value=0)
            ok,png=cv2.imencode('.png',crop)
            if not ok:continue
            reply=self.read_reply(proc,base64.b64encode(png).decode('ascii'))
            found=reply.get('lines',[]);positions=reply.get('boxes',[])
            debug.append({'view':variant,'lines':[str(line)[:90] for line in found[:6]]})
            if len(found)!=len(positions):continue
            for line,(x,y,w,h) in zip(found,positions):
                lines.append(line);boxes.append([left+(x-border)/zoom,top+(y-border)/zoom,w/zoom,h/zoom])
            if any(event[1] in ('gain','bonus') for event in NoticeEdges().update(lines,sampled_at,boxes)):break
        return lines,boxes,debug

    def prioritize_rewards(self):
        self.reward_until=time.monotonic()+8

    def read_inventory_quantities(self,image,proc,counts):
        """Retry unreadable headers together, keeping each slot spatially separate."""
        pending=dict(self.inventory_reader.quantity_crops)
        for target_height in (72,96,144):
            if self.stop.is_set() or not pending:break
            crops=[];top=16;width=0
            for kind,(left,y,right,bottom) in pending.items():
                header=image[y:bottom,left:right,:3]
                if not header.size:continue
                zoom=min(target_height/header.shape[0],2400/header.shape[1])
                crop=cv2.resize(header,None,fx=zoom,fy=zoom,interpolation=cv2.INTER_CUBIC)
                crops.append((kind,top,crop));top+=crop.shape[0]+32
                width=max(width,crop.shape[1])
            if not crops:break
            sheet=np.zeros((top,width+32,3),np.uint8)
            for _,y,crop in crops:sheet[y:y+crop.shape[0],16:16+crop.shape[1]]=crop
            ok,png=cv2.imencode('.png',sheet)
            if not ok:break
            reply=self.read_reply(proc,base64.b64encode(png).decode('ascii'))
            lines,boxes=reply.get('lines',[]),reply.get('boxes',[])
            if len(lines)!=len(boxes):continue
            for kind,y,crop in crops:
                parts=[(box[1],box[0],line) for line,box in zip(lines,boxes)
                       if len(box)==4 and 16<=box[0]+box[2]/2<=16+crop.shape[1]
                       and y<=box[1] and box[1]+box[3]<=y+crop.shape[0]+1]
                value=inventory_quantity(' '.join(line for _,_,line in sorted(parts)))
                if value is not None:counts[kind]=value;pending.pop(kind,None)
        return counts

    def scan_mode(self,now):
        fast=(now<self.reward_until and not self.needs_requested and
              not self.inventory_requested and not self.inventory_visible)
        return fast,fast and now-self.last_full_scan<2

    def publish_notices(self,lines,boxes,sampled_at,baseline,full_scan=True):
        if full_scan:
            self.waiting_prompt=any(re.fullmatch(r'(?:press )?e\s*(?:to )?cancel(?: fishing)?',normalize_notice(line)) for line in lines)
            self.prompt_sampled_at=sampled_at
            if self.waiting_prompt or any(re.match(r'^a fish has bitten\b',normalize_notice(line)) for line in lines):
                self.fishing_activity_at=sampled_at
        candidates=[line[:160] for line in lines if re.search(r'\b(?:'+FISH+r'|rod)\b',normalize_notice(line))]
        if candidates:self.candidate=' | '.join(candidates[:3])
        events=self.edges.update(lines,sampled_at,boxes)
        accepted=events if not baseline else [event for event in events if event[1]=='blocked']
        with self.lock:
            self.events.extend(accepted)
            self.events=self.events[-32:]
            self.reward_events+=sum(event[1] in ('catch','gain','bonus') for event in accepted)

    def scan_needs(self,image,sampled_at,generation):
        values, trusted, details = {}, {}, {}
        regions = self.needs_hud.regions(image)
        for kind, region in regions.items():
            images = []
            for variant in self.needs_hud.variants(image,region):
                if self.stop.is_set(): return
                ok, png = cv2.imencode('.png',variant)
                if ok: images.append(base64.b64encode(png).decode('ascii'))
            with self.hud_io_lock:
                reply = self.read_reply(self.hud_proc,json.dumps({'images':images}))
            readings = reply.get('readings',[])
            value,strong = choose_percentage(readings)
            details[kind] = {'value':value,'strong':strong,
                'candidates':[{k:r[k] for k in ('text','confidence')} for r in readings if r.get('text')]}
            if value is not None:
                values[kind],trusted[kind] = value,strong
        with self.lock:
            if (self.stop.is_set() or self.inventory_visible or
                    generation!=self.inventory_generation): return
            self.needs = self.needs_hud.update(values,sampled_at,trusted)
            self.needs_low_confirmed = dict(self.needs_hud.low_confirmed)
            self.needs_debug = {'icons_found':bool(regions),'sampled_at':sampled_at,'readings':details}

    def run_needs(self):
        # Consume only the newest frame. A slow percentage scan must never
        # prevent the capture thread from seeing a brief catch popup.
        try:
            while not self.stop.is_set():
                with self.lock:
                    frame,self.hud_frame = self.hud_frame,None
                if frame is None:
                    self.stop.wait(.05)
                    continue
                self.scan_needs(*frame)
        except Exception as error:
            if not self.stop.is_set():
                with self.lock:
                    self.needs_error=str(error)[:120]
                    self.needs_hud.clear()
                    self.needs,self.needs_low_confirmed = {},{}
                    self.needs_debug = {'error':str(error)[:120]}

    def read_reply(self, proc, payload=None):
        # Terminating a stalled child also unblocks a full stdin pipe/readline.
        expired = threading.Event()
        def timeout():
            expired.set()
            try: proc.terminate()
            except OSError: pass
        timer = threading.Timer(8, timeout)
        timer.daemon = True
        timer.start()
        try:
            if payload is not None:
                proc.stdin.write(payload+'\n')
                proc.stdin.flush()
            line = proc.stdout.readline()
            if expired.is_set(): raise TimeoutError('Local OCR timed out.')
            if not line: raise RuntimeError('Local OCR exited without a response.')
            response = json.loads(line)
            if not isinstance(response, dict): raise RuntimeError('Invalid OCR response.')
            if 'error' in response: raise RuntimeError(response['error'])
            return response
        finally:
            timer.cancel()

    def start(self):
        self.thread.start()

    def drain(self, since=None):
        with self.lock:
            events, self.events = self.events, []
        return events if since is None else [item for item in events if item[0] >= since]

    def needs_snapshot(self):
        with self.lock:
            return dict(self.needs),dict(self.needs_low_confirmed)

    def update_inventory(self, states, sampled_at):
        # Unchecked cells cannot invalidate two-frame confirmation merely
        # because their labels alternate between readable and unreadable.
        selected = None if states is None else {slot:states.get(slot) for slot in self.rod_slots}
        self.inventory = self.inventory_reader.update(selected,sampled_at)
        return self.inventory

    def update_inventory_visibility(self,found,full_scan=True):
        with self.lock:
            before=self.inventory_visible
            if found:
                self.inventory_misses=0
                self.inventory_visible=True
            elif full_scan:
                self.inventory_misses=min(2,self.inventory_misses+1)
                if self.inventory_misses>=2:self.inventory_visible=False
            if self.inventory_visible!=before:
                self.inventory_generation+=1
                self.capacity_previous=None;self.inventory_capacity=None

    def close(self):
        self.stop.set()
        with self.lock:
            processes = [self.proc,self.hud_proc]
        for proc in processes:
            if proc and proc.poll() is None:
                try: proc.terminate()
                except OSError: pass
        if self.thread.ident:
            self.thread.join(timeout=2)
        if self.hud_thread and self.hud_thread.ident:
            self.hud_thread.join(timeout=2)
        # Process termination is asynchronous on Windows. Confirm the known
        # children exited rather than returning with a live OCR subprocess.
        for proc in processes:
            if proc is None:continue
            try:proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait(timeout=2)

    def run(self):
        proc = hud_proc = capture = None
        try:
            proc = subprocess.Popen(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                 str(Path(__file__).with_name('fishing-ocr.ps1'))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
            with self.lock:
                self.proc = proc
                stopped = self.stop.is_set()
            if stopped:
                proc.terminate()
                return
            ready = self.read_reply(proc)
            if not ready.get('ready'):
                raise RuntimeError(ready.get('error', 'OCR did not start.'))
            self.state = 'Local OCR active'
            if self.needs_hud is not None or self.capacity_enabled:
                node = node_path()
                hud_proc = subprocess.Popen([node,str(Path(__file__).with_name('fishing-hud-ocr.cjs'))],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                    text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
                with self.lock:
                    self.hud_proc = hud_proc
                    stopped = self.stop.is_set()
                if stopped:
                    hud_proc.terminate()
                    return
                if not self.read_reply(hud_proc).get('ready'):
                    raise RuntimeError('Percentage OCR did not start.')
                if self.needs_hud is not None:
                    self.hud_thread=threading.Thread(target=self.run_needs,name='fishing-needs',daemon=True)
                    self.hud_thread.start()
            previous = None
            baseline = True
            while not self.stop.is_set():
                if not self.focused():
                    if self.stop.wait(.2): break
                    continue
                current = self.bounds(self.hwnd)
                if current != previous:
                    if capture: capture.close()
                    capture = self.capture_factory(*current)
                    previous = current
                sampled_at = time.monotonic()
                pixels = capture.read()
                if not self.focused(): continue
                image = np.frombuffer(pixels, np.uint8).reshape(current[3], current[2], 4)
                # OCR's limit is 2600 px; preserve small toast text on 1080p/1440p.
                if max(image.shape[:2]) > 2500:
                    scale = 2500 / max(image.shape[:2])
                    image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                # Inventory is located from the current pixels, never cached
                # coordinates. Once visible, OCR only its row instead of first
                # reading the entire game and then reading that same row again.
                inventory_row=None
                row_checked=self.inventory_requested or self.inventory_visible
                if row_checked:inventory_row=self.locate_inventory(image,allow_fallback=True)
                inventory_only=inventory_row is not None and row_checked
                fast,quick=self.scan_mode(sampled_at)
                if quick or inventory_only:response={'lines':[],'boxes':[],'words':[]}
                else:
                    ok, png = cv2.imencode('.png', image[:, :, :3])
                    if not ok: raise RuntimeError('Could not encode OCR frame.')
                    response = self.read_reply(proc, base64.b64encode(png).decode('ascii'))
                    self.last_full_scan=sampled_at
                lines = response.get('lines', [])
                boxes = response.get('boxes')
                words = response.get('words',[])
                if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
                    raise RuntimeError('Invalid OCR lines.')
                if (not isinstance(boxes, list) or len(boxes) != len(lines)
                        or not all(isinstance(box, list) and len(box) == 4 and
                                   all(isinstance(value, (int, float)) for value in box)
                                   for box in boxes)):
                    boxes = None
                likely_inventory=(self.inventory_requested or self.inventory_visible or
                    any(re.fullmatch(r'r[o0]d|water|sandwich|rubber|bait',word.get('text','').lower()) for word in words))
                if not row_checked:
                    inventory_row = self.locate_inventory(image,allow_fallback=likely_inventory and not quick)
                inventory_states = None
                self.update_inventory_visibility(inventory_row is not None,full_scan=not quick)
                if inventory_row is not None:
                    original_words=words
                    tags,stride = inventory_row
                    left = max(0,tags['1'][1]-4)
                    top = max(0,tags['1'][2]-round(stride*.6))
                    right = min(image.shape[1],round(tags['5'][1]+stride))
                    bottom = min(image.shape[0],round(tags['1'][2]+stride+4))
                    zoom = min(2,2400/max(1,right-left))
                    crop = cv2.resize(image[top:bottom,left:right,:3],None,fx=zoom,fy=zoom,
                                      interpolation=cv2.INTER_CUBIC)
                    ok,inventory_png = cv2.imencode('.png',crop)
                    if ok and not self.stop.is_set():
                        card = self.read_reply(proc,base64.b64encode(inventory_png).decode('ascii'))
                        words = [dict(text=word['text'],box=[left+word['box'][0]/zoom,
                                 top+word['box'][1]/zoom,word['box'][2]/zoom,word['box'][3]/zoom])
                                 for word in card.get('words',[])]
                    inventory_states = self.inventory_reader.states(image,inventory_row,words)
                    original_states=self.inventory_reader.states(image,inventory_row,original_words)
                    for slot in self.rod_slots:
                        if inventory_states.get(slot) is None and type(original_states.get(slot)) is bool:
                            inventory_states[slot]=original_states[slot]
                        if inventory_states.get(slot) is not None or not (self.inventory_requested or inventory_only):continue
                        _,x,y,_,_,_=tags[slot]
                        left=max(0,x-1);top=max(0,round(y+stride*.64))
                        right=min(image.shape[1],round(x+stride-3));bottom=min(image.shape[0],round(y+stride-2))
                        label=image[top:bottom,left:right,:3]
                        if not label.size:continue
                        gray=cv2.cvtColor(label,cv2.COLOR_BGR2GRAY)
                        for source in (label,cv2.cvtColor(255-gray,cv2.COLOR_GRAY2BGR)):
                            if self.stop.is_set():break
                            crop=cv2.resize(source,None,fx=4,fy=4,interpolation=cv2.INTER_CUBIC)
                            ok,png=cv2.imencode('.png',crop)
                            if not ok:continue
                            reply=self.read_reply(proc,base64.b64encode(png).decode('ascii'))
                            focused=[dict(text=word['text'],box=[left+word['box'][0]/4,top+word['box'][1]/4,
                                     word['box'][2]/4,word['box'][3]/4]) for word in reply.get('words',[])]
                            reread=self.inventory_reader.states(image,inventory_row,focused).get(slot)
                            if type(reread) is bool:inventory_states[slot]=reread;break
                self.inventory_debug=dict(row_found=inventory_row is not None,slots=inventory_states,
                                          sampled_at=sampled_at)
                capacity_words=words
                if self.capacity_enabled and inventory_row:
                    capacity_words=self.retry_capacity(image,inventory_row,words,proc)
                self.update_capacity(capacity_words,inventory_row,sampled_at)
                counts=self.inventory_reader.amounts(image,inventory_row,words) if inventory_row else {}
                if inventory_row:self.read_inventory_quantities(image,proc,counts)
                if '1' in self.rod_slots and counts.get('bait')==0:counts.pop('bait')
                self.inventory_amounts=self.amounts_reader.update(counts,sampled_at)
                self.update_inventory(inventory_states,sampled_at)
                # Full-frame OCR misses the tiny inventory heading/label. Find
                # cards anywhere in the frame and OCR their magnified pixels.
                # Coordinates are mapped back before joining wrapped notices.
                reward_regions=self.cards.regions(image)
                self.reward_scans+=1
                self.reward_cards+=len(reward_regions)
                card_debug=[]
                for region in reward_regions:
                    if self.stop.is_set(): break
                    card_lines,card_boxes,details=self.read_reward_card(image,region,proc,sampled_at)
                    card_debug.extend(details)
                    if boxes is None:lines,boxes=[],[]
                    lines.extend(card_lines);boxes.extend(card_boxes)
                if card_debug:self.reward_debug=card_debug
                # Deliver captured reward evidence immediately. Percentage OCR
                # can take seconds and must not hold a catch event behind it.
                self.publish_notices(lines,boxes,sampled_at,baseline,full_scan=not quick and not inventory_only)
                baseline=False
                if (self.needs_hud is not None and not self.inventory_visible and
                        not self.inventory_requested and not fast):
                    with self.lock:
                        self.hud_frame=(image.copy(),sampled_at,self.inventory_generation)
                elif self.needs_hud is not None and (self.inventory_visible or self.inventory_requested):
                    # Inventory obscures/dims the HUD. Do not authorize item
                    # use or delay rod scans using percentages from that view.
                    with self.lock:
                        self.hud_frame=None
                        self.needs_hud.clear()
                        self.needs = {}
                        self.needs_low_confirmed = {}
                        self.needs_debug = {'inventory_open':True,'sampled_at':sampled_at,'readings':{}}
                self.initialized.set()
                self.last_progress_at=time.monotonic()
                self.stop.wait(.1 if fast else .35)
        except Exception as error:
            if not self.stop.is_set():
                self.state = f'OCR unavailable: {str(error)[:120]}'
        finally:
            self.stop.set()
            self.initialized.set()
            try:
                if capture: capture.close()
            finally:
                for proc in (proc,hud_proc):
                    if proc is None: continue
                    if proc.poll() is None:
                        try: proc.terminate()
                        except OSError: pass
                    try: proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                    for pipe in (proc.stdin, proc.stdout):
                        if pipe: pipe.close()
                if self.hud_thread and self.hud_thread.ident:
                    self.hud_thread.join(timeout=2)
