"""Rod backups and passive hunger/thirst readings from the supplied HUD."""
from pathlib import Path
import math
import re
import threading
import time

import cv2
import numpy as np

NEED_MAX_AGE = 12
ROD_CHECK_ATTEMPTS = 4
ROD_PRIORITY = ('3','1','2')


def parse_rod_slots(value):
    if (not isinstance(value,(list,tuple)) or not value or
            any(type(slot) is not str or slot not in ROD_PRIORITY for slot in value)
            or len(set(value)) != len(value)):
        raise ValueError('Enable at least one rod slot (1, 2 or 3) on Supplies.')
    return tuple(slot for slot in ROD_PRIORITY if slot in value)


def choose_percentage(readings):
    candidates = []
    for reading in readings:
        text = reading.get('text','').strip()
        value = percentage([text]) if re.fullmatch(r'\d{1,3}\s*%',text) else None
        confidence = reading.get('confidence',0)
        if value is not None and confidence>=50:
            candidates.append((value,confidence))
    if not candidates: return None,False
    # Choosing the highest plausible value avoids a dropped digit turning 87%
    # into 17% and wasting supplies. Low-use authorization needs stronger votes.
    value = max(value for value,_ in candidates)
    strong = sum(candidate==value and confidence>=75 for candidate,confidence in candidates)>=2
    return value,strong


def percentage(lines):
    values = set()
    for line in lines:
        match = re.fullmatch(r'\s*(\d{1,3})\s*[%eEsS.]*\s*', line)
        if match and 0 <= int(match[1]) <= 100:
            values.add(int(match[1]))
    return values.pop() if len(values) == 1 else None


class NeedsHud:
    def __init__(self):
        self.templates = {}
        for kind, name in [('hunger','fishing-hunger-icon.png'),('thirst','fishing-water-icon.png')]:
            source = cv2.imread(str(Path(__file__).with_name(name)), cv2.IMREAD_GRAYSCALE)
            if source is not None:
                self.templates[kind] = [(scale,cv2.resize(source,None,fx=scale,fy=scale,
                    interpolation=cv2.INTER_CUBIC)) for scale in (.75,1,1.25,1.5,2,2.5,3)]
        self.previous = {}
        self.readings = {}
        self.previous_trust = {}
        self.low_confirmed = {}

    def clear(self):
        self.previous.clear()
        self.readings.clear()
        self.previous_trust.clear()
        self.low_confirmed.clear()

    def regions(self, image):
        gray = cv2.cvtColor(image[:,:,:3], cv2.COLOR_BGR2GRAY)
        height, width = gray.shape
        matches = {}
        for kind, templates in self.templates.items():
            best = None
            for scale, template in templates:
                if template.shape[0] >= height or template.shape[1] >= width:
                    continue
                scores = cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED)
                _, score, _, (x,y) = cv2.minMaxLoc(scores)
                if score >= .83 and (best is None or score > best[0]):
                    best = (score,x,y,scale,template.shape[1],template.shape[0])
            if best is not None:
                matches[kind] = best
        if len(matches) != 2:
            return {}
        _,hx,hy,hs,hw,hh = matches['hunger']
        _,wx,wy,ws,ww,wh = matches['thirst']
        # Both familiar icons must share the HUD row and be adjacent. A random
        # droplet elsewhere cannot be read as this player's thirst.
        if (not 20*hs <= wx-hx <= 100*hs or abs(wy-hy) > 12*hs or
                not .7 <= ws/hs <= 1.4):
            return {}
        regions = {}
        for kind, (_,x,y,scale,w,h) in matches.items():
            left = x+math.ceil(w/(2*scale)-18)*scale
            region = (round(left),round(y+32*scale),round(left+48*scale),round(y+50*scale))
            if not (0 <= region[0] < region[2] <= width and
                    0 <= region[1] < region[3] <= height):
                return {}  # Clipped labels cannot be interpreted as percentages.
            regions[kind] = region
        return regions

    @staticmethod
    def variants(image, region):
        left,top,right,bottom = region
        pixels = image[top:bottom,left:right,:3]
        gray = cv2.cvtColor(pixels,cv2.COLOR_BGR2GRAY)
        neutral = np.where(pixels.max(axis=2)-pixels.min(axis=2)<45,pixels.min(axis=2),0).astype(np.uint8)
        # Preserve original strokes as well as binary views. Thresholding and
        # shrinking can erase a faint leading digit or the percent sign.
        for source in (gray,255-gray):
            zoom=cv2.resize(source,None,fx=6,fy=6,interpolation=cv2.INTER_CUBIC)
            yield cv2.copyMakeBorder(zoom,24,24,24,24,cv2.BORDER_CONSTANT,value=int(np.median(source)))
        sharp=cv2.addWeighted(gray,2,cv2.GaussianBlur(gray,(0,0),1),-1,0)
        zoom=cv2.resize(sharp,None,fx=6,fy=6,interpolation=cv2.INTER_CUBIC)
        yield cv2.copyMakeBorder(zoom,24,24,24,24,cv2.BORDER_CONSTANT,value=int(np.median(gray)))
        for source in (neutral,gray):
            source = cv2.resize(source,(48,18),interpolation=cv2.INTER_AREA)
            for threshold in (120,150,180,170):
                mask = cv2.threshold(source,threshold,255,cv2.THRESH_BINARY)[1]
                zoom = cv2.resize(mask,None,fx=4,fy=4,interpolation=cv2.INTER_CUBIC)
                yield cv2.copyMakeBorder(zoom,20,20,20,20,cv2.BORDER_CONSTANT,value=0)

        # Isolate the text band and remove slow-changing floor highlights. The
        # live wooden-floor fixture needs these views to preserve the 8 in 87%.
        tight = image[top+round((bottom-top)*4/18):top+round((bottom-top)*17/18),
                      left:left+round((right-left)*36/48),:3]
        tight = cv2.resize(cv2.cvtColor(tight,cv2.COLOR_BGR2GRAY),(36,13),interpolation=cv2.INTER_AREA)
        for size,threshold in ((3,40),(5,40),(7,None)):
            hat = cv2.morphologyEx(tight,cv2.MORPH_TOPHAT,np.ones((size,size),np.uint8))
            if threshold is not None: hat=cv2.threshold(hat,threshold,255,cv2.THRESH_BINARY)[1]
            zoom=cv2.resize(hat,None,fx=8,fy=8,interpolation=cv2.INTER_CUBIC)
            yield cv2.copyMakeBorder(zoom,20,20,20,20,cv2.BORDER_CONSTANT,value=0)

    def update(self, values, sampled_at, trusted=None):
        trusted = trusted if trusted is not None else {kind:True for kind in values}
        for kind in ('hunger','thirst'):
            value = values.get(kind)
            previous = self.previous.get(kind)
            if value is not None and previous is not None and 0 <= sampled_at-previous[0] <= NEED_MAX_AGE:
                if abs(value-previous[1]) <= 3:
                    self.readings[kind] = (sampled_at,max(value,previous[1]))
                    self.low_confirmed[kind] = bool(trusted.get(kind) and self.previous_trust.get(kind))
                else:
                    # A contradictory fresh value invalidates the old low
                    # authorization until the new value is confirmed.
                    self.readings.pop(kind,None)
                    self.low_confirmed.pop(kind,None)
            if value is not None:
                self.previous[kind] = (sampled_at,value)
                self.previous_trust[kind] = trusted.get(kind,False)
            elif previous is not None and sampled_at-previous[0] > NEED_MAX_AGE:
                self.previous.pop(kind,None)
                self.previous_trust.pop(kind,None)
        return dict(self.readings)


INVENTORY_MAX_AGE = 15

def inventory_quantity(text):
    """Read the entire count, never the suffix after a thousands separator."""
    if not isinstance(text,str) or len(text)>128:return None
    text=re.sub(r'\s+','',text).lower()
    quantity=r'(?:0|[1-9][0-9]{0,6}|[1-9][0-9]{0,2}(?:,[0-9]{3}){1,2})'
    match=re.fullmatch(r'(?:[0-9]+(?:[.,][0-9]+)?(?:kg|g))?('+quantity+r')[x×]',text)
    if not match:return None
    value=int(match[1].replace(',',''))
    return value if value<=1_000_000 else None

class RodInventory:
    """Read usable rods: True=rod, False=empty/known non-rod, None=unreadable."""
    NON_ROD_LABELS = frozenset(('rubber','sandwich','water','artificial bait'))
    def __init__(self):
        strip = cv2.imread(str(Path(__file__).with_name('fishing-slot-tags.png')),cv2.IMREAD_GRAYSCALE)
        self.tags = {} if strip is None else {str(index+1):strip[:,index*14:index*14+width]
                                              for index,width in enumerate((14,14,13,14,13))}
        self.previous = None
        self.confirmed = None

    def row(self, image,allow_fallback=True):
        bgr = image[:,:,:3]
        bright = ((bgr.min(axis=2)>160)&(bgr.max(axis=2)-bgr.min(axis=2)<45)).astype(np.uint8)
        _,_,components,_ = cv2.connectedComponentsWithStats(bright)
        gray = cv2.cvtColor(bgr,cv2.COLOR_BGR2GRAY)
        matches = []
        for x,y,w,h,area in components[1:]:
            if not (8 <= w <= 50 and 10 <= h <= 60 and .65 <= w/h <= 1.1 and area >= w*h*.65):
                continue
            patch = gray[y:y+h,x:x+w]
            best = max(((float(cv2.matchTemplate(patch,cv2.resize(template,(w,h)),
                              cv2.TM_CCOEFF_NORMED)[0,0]),slot) for slot,template in self.tags.items()),
                       default=(0,None))
            if best[0]>=.83:
                matches.append((best[1],int(x),int(y),int(w),int(h),best[0]))
        missing=set(self.tags)-{match[0] for match in matches}
        if missing and allow_fallback:
            # Daylight inventory backgrounds join the light number badges into
            # one large connected component. Match the actual badge glyphs
            # directly; the regular numbered-row fit below still validates them.
            for slot,template in self.tags.items():
                if slot not in missing:continue
                for scale in (.75,1,1.25,1.5,2,2.5,3):
                    resized=cv2.resize(template,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
                    h,w=resized.shape
                    if h>=gray.shape[0] or w>=gray.shape[1]:continue
                    scores=cv2.matchTemplate(gray,resized,cv2.TM_CCOEFF_NORMED)
                    for _ in range(3):
                        _,score,_,(x,y)=cv2.minMaxLoc(scores)
                        if score<.90:break
                        matches.append((slot,x,y,w,h,score))
                        scores[max(0,y-h):y+h,max(0,x-w):x+w]=-1
        # Empty cells do not display a number badge. Fit the numbered row
        # from the remaining occupied cells, then verify any missing cells.
        candidates = []
        for first in matches:
            slot,x,y,w,h,_ = first
            for second in matches:
                distance = int(second[0])-int(slot)
                if distance<=0 or abs(second[2]-y)>h*.4:continue
                stride = (second[1]-x)/distance
                if not w*4<=stride<=w*12:continue
                origin = x-(int(slot)-1)*stride
                row = {}
                for index in range(1,6):
                    fitting = [m for m in matches if m[0]==str(index) and abs(m[2]-y)<=h*.4
                               and abs(m[1]-(origin+(index-1)*stride))<=stride*.08
                               and .7<=m[4]/h<=1.4]
                    if fitting:row[str(index)] = max(fitting,key=lambda m:m[-1])
                # Two tags alone are accepted only for the known 4/5 supply
                # cells, with all three missing rod cards visibly empty.
                if len(row)<3 and set(row)!={'4','5'}:continue
                positions = np.array([int(s)-1 for s in row],dtype=float)
                fitted_stride,origin = np.polyfit(positions,[row[s][1] for s in row],1)
                error = sum(abs(row[s][1]-(origin+(int(s)-1)*fitted_stride)) for s in row)
                candidates.append((len(row),-error,row,float(fitted_stride),float(origin)))
        for _,_,row,stride,origin in sorted(candidates,key=lambda item:item[:2],reverse=True):
            anchor = next(iter(row.values()))
            completed = dict(row)
            for index in range(1,6):
                slot = str(index)
                if slot in completed:continue
                inferred = (slot,round(origin+(index-1)*stride),anchor[2],anchor[3],anchor[4],0)
                if not self.empty_cell(image,inferred,stride,require_outline=True):
                    # An occupied slot can have an obscured number. Three real
                    # numbered anchors plus both card borders locate the slot;
                    # its contents still require positive OCR, never guessed.
                    if len(row)<3 or not self.card_outline(image,inferred,stride):break
                completed[slot] = inferred
            if len(completed)==5:return completed,stride
        return None

    @staticmethod
    def card_outline(image,tag,stride):
        _,x,y,w,h,_=tag
        left,top=x-1,y-2;right,bottom=round(left+stride-2),round(top+stride)
        height,width=image.shape[:2]
        if left<0 or top<0 or right>width or bottom>height:return False
        gray=cv2.cvtColor(image[:,:,:3],cv2.COLOR_BGR2GRAY)
        inside=gray[top+h+5:bottom-8,left+8:right-8]
        if not inside.size:return False
        interior=float(np.median(inside))
        for edge in (left,right):
            strip=gray[top+h+5:bottom-8,max(0,edge-5):min(width,edge+6)]
            if not strip.size:return False
            contrast=max(float(np.median(strip.max(axis=1)))-interior,
                         interior-float(np.median(strip.min(axis=1))))
            if contrast<12:return False
        return True

    @staticmethod
    def empty_cell(image, tag, stride, require_outline=False):
        _,x,y,w,h,_ = tag
        left,top = x-1,y-2
        right,bottom = round(left+stride-2),round(top+stride)
        height,width = image.shape[:2]
        if left<0 or top<0 or right>width or bottom>height:return False
        cell = image[top+3:bottom-3,left+3:right-3,:3].copy()
        if not cell.size:return False
        cell[:h+2,:w+2] = cell[-1,-1]
        gray = cv2.cvtColor(cell,cv2.COLOR_BGR2GRAY)
        if float(gray.std())>=6 or float(gray.max())>=100:return False
        if require_outline:
            # The inferred slot must also have both real card borders. Two
            # floating numbers over dark scenery are not an inventory row.
            middle_top,middle_bottom=top+h+5,bottom-8
            if middle_bottom<=middle_top:return False
            gray_image=cv2.cvtColor(image[:,:,:3],cv2.COLOR_BGR2GRAY)
            interior=float(np.median(gray))
            for edge in (left,right):
                strip=gray_image[middle_top:middle_bottom,max(0,edge-5):min(width,edge+6)]
                if not strip.size or float(np.median(strip.max(axis=1)))<interior+12:return False
        return True

    def states(self, image, row, words):
        tags,stride = row
        height,width = image.shape[:2]
        result = {}
        for slot in ('1','2','3'):
            _,x,y,w,h,_ = tags[slot]
            left,top = x-1,y-2
            right,bottom = round(left+stride-2),round(top+stride)
            if left<0 or top<0 or right>width or bottom>height:
                result[slot] = None
                continue
            label_words = sorted((word for word in words if
                      left <= word['box'][0]+word['box'][2]/2 < right and
                      top+stride*.65 <= word['box'][1]+word['box'][3]/2 < bottom),
                      key=lambda word:word['box'][0])
            label = ' '.join(word.get('text','').strip().lower() for word in label_words)
            rod = any(re.fullmatch(r'r[o0]d',word.get('text','').strip().lower())
                      for word in label_words)
            compact=re.sub(r'[^a-z0-9]','',label)
            if rod or compact in ('fishingrod','fishingr0d','titaniumrod','titaniumr0d'):
                result[slot] = True
                continue
            # Match an entire known item label, never arbitrary OCR fragments.
            # ROD evidence above wins; partial TITANIUM/FISHING stays unknown.
            if label in self.NON_ROD_LABELS or compact in {name.replace(' ','') for name in self.NON_ROD_LABELS}:
                result[slot] = False
                continue
            # An occupied card may have unreadable text. To call it empty,
            # require a flat, dark cell with no text/item pixels, excluding its
            # number badge and border. Pictures and quantity text veto emptiness.
            result[slot] = False if self.empty_cell(image,tags[slot],stride) else None
        return result

    def update(self, states, sampled_at):
        self.confirmed = None
        if states is None:
            self.previous = None
        else:
            if (self.previous is not None and 0 <= sampled_at-self.previous[0]<=INVENTORY_MAX_AGE
                    and self.previous[1]==states):
                self.confirmed = (sampled_at,dict(states))
            self.previous = (sampled_at,dict(states))
        return self.confirmed

    def amounts(self,image,row,words,headers=None):
        tags,stride=row
        result={}
        self.quantity_crops={}
        for slot,kind,expected in (('1','bait','artificial bait'),('4','food','sandwich'),('5','water','water')):
            _,x,y,w,h,_=tags[slot]
            left,top=x-1,y-2;right,bottom=round(left+stride-2),round(top+stride)
            if self.empty_cell(image,tags[slot],stride):
                result[kind]=0;continue
            labels=sorted((word for word in words if
                left<=word['box'][0]+word['box'][2]/2<right and
                top+stride*.65<=word['box'][1]+word['box'][3]/2<bottom),key=lambda word:word['box'][0])
            label=' '.join(word.get('text','').strip().lower() for word in labels)
            if re.sub(r'[^a-z0-9]','',label)!=expected.replace(' ',''):
                result[kind]=None;continue
            header=sorted((word for word in words if
                left+stride*.3<=word['box'][0]+word['box'][2]/2<right and
                top<=word['box'][1]+word['box'][3]/2<top+stride*.25),key=lambda word:word['box'][0])
            text=''.join(word.get('text','').lower().replace(' ','') for word in header)
            if headers and kind in headers:text=headers[kind].lower().replace(' ','')
            result[kind]=inventory_quantity(text)
            if result[kind] is None:
                # Include the whole header beside the badge so retries cannot
                # lose the leading group of a four-/five-digit item count.
                self.quantity_crops[kind]=(round(x+w+1),y,round(x+stride-3),round(y+stride*.23))
        return result


class InventoryAmounts:
    def __init__(self):
        self.previous={}

    def update(self,values,sampled_at):
        confirmed={}
        for kind in ('bait','food','water'):
            value=values.get(kind)
            previous=self.previous.get(kind)
            if type(value) is not int or value<0:
                self.previous.pop(kind,None);continue
            if previous and previous[1]==value and 0<=sampled_at-previous[0]<=INVENTORY_MAX_AGE:
                confirmed[kind]=value
            self.previous[kind]=(sampled_at,value)
        return (sampled_at,confirmed) if confirmed else None


class SupplySession:
    def __init__(self, rod_slots=ROD_PRIORITY):
        self.lock = threading.Lock()
        self.rod_slots = parse_rod_slots(rod_slots)
        self.reset()

    def configure_rod_slots(self, slots):
        slots = parse_rod_slots(slots)
        with self.lock:
            if slots == self.rod_slots:return False
            self.rod_slots = slots
            self.broken.intersection_update(slots)
            if self.rod not in slots:
                self.rod = next((slot for slot in slots if slot not in self.broken),None)
                self.equipped_at = -float('inf')
                self.equipped_slot = None
            self.rod_inventory_checked = False
            return True

    def restore(self,saved):
        if not isinstance(saved,dict):return
        with self.lock:
            broken=saved.get('broken',[])
            if isinstance(broken,list):self.broken={slot for slot in broken if slot in self.rod_slots}
            self.rod=next((slot for slot in self.rod_slots if slot not in self.broken),None)
            pending=saved.get('pending_uses',{})
            if isinstance(pending,dict):
                for kind,value in pending.items():
                    if (kind in ('hunger','thirst') and isinstance(value,(list,tuple)) and len(value)==2 and
                            type(value[1]) in (int,float) and math.isfinite(value[1]) and 0<=value[1]<=100):
                        self.pending_uses[kind]=(time.monotonic(),value[1])
                        self.last_use[kind]=time.monotonic()
            # Every restart verifies current stock. Stored OCR is never fresh.
            self.rod_inventory_checked=False

    def reset(self, preserve_supplies=False):
        with self.lock:
            self.rod = self.rod_slots[0]
            self.broken = set()
            self.equipped_at = -float('inf')
            self.equipped_slot = None
            self.last_break = -float('inf')
            if not preserve_supplies:
                self.last_use = {}
                self.pending_uses = {}
            self.rod_inventory_checked = False
            self.attempts_since_rod_check = 0

    def rod_check_due(self):
        with self.lock:
            return (not self.rod_inventory_checked or self.rod is None or
                    self.attempts_since_rod_check >= ROD_CHECK_ATTEMPTS)

    def rod_inventory_verified(self):
        # Reset only after a complete inventory read and verified closure.
        with self.lock:
            self.rod_inventory_checked = True
            self.attempts_since_rod_check = 0

    def completed_attempt(self):
        with self.lock:
            self.attempts_since_rod_check += 1

    def equipped(self, now):
        with self.lock:
            self.equipped_at = now
            self.equipped_slot = self.rod

    def break_rod(self, sampled_at):
        with self.lock:
            # Delayed replies and briefly unreadable sticky popups must not
            # consume a second backup immediately after the first switch.
            if (self.rod is None or sampled_at < self.equipped_at or sampled_at-self.last_break < 5
                    or (self.equipped_slot is not None and self.equipped_slot!=self.rod)):
                return False
            self.broken.add(self.rod)
            self.last_break = sampled_at
            self.rod = next((slot for slot in self.rod_slots if slot not in self.broken), None)
            return True

    def snapshot(self):
        with self.lock:
            return dict(rod=self.rod,broken=sorted(self.broken),last_use=dict(self.last_use),
                        pending_uses=dict(self.pending_uses),rod_checked=self.rod_inventory_checked,
                        rod_check_attempts=self.attempts_since_rod_check,rod_slots=list(self.rod_slots))

    def inventory(self, states):
        with self.lock:
            for slot,present in states.items():
                if slot not in self.rod_slots:continue
                if present is True: self.broken.discard(slot)
                elif present is False: self.broken.add(slot)
            if self.rod is None or states.get(self.rod) is False:
                self.rod = next((slot for slot in self.rod_slots if states.get(slot) is True),None)
            return self.rod

    def due(self, readings, thresholds, now):
        with self.lock:
            return [kind for kind in ('hunger','thirst') if kind in readings and
                    0 <= now-readings[kind][0] <= NEED_MAX_AGE and
                    readings[kind][1] <= thresholds[kind] and
                    now-self.last_use.get(kind,-float('inf')) >= 30]

    def used(self, kind, now, before=None):
        with self.lock:
            self.last_use[kind] = now
            if before is not None:
                self.pending_uses[kind] = (now,before)

    def restored(self, kind):
        with self.lock:
            self.pending_uses.pop(kind,None)
