"""FiveM fishing helper with automatic circle discovery and fast pixel tracking."""
import ctypes
from ctypes import wintypes as w
import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import math
import statistics
import sys
from pathlib import Path
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk,filedialog
import cv2
import numpy as np
from fishing_features import OutcomeMonitor, SessionStats
from fishing_supplies import SupplySession, NEED_MAX_AGE, INVENTORY_MAX_AGE, ROD_PRIORITY, parse_rod_slots
from fishing_paths import ASSET_DIR, data_dir, settings_path
from fishing_ui import build_dashboard, duration, age, phase
from fishing_discord import DISCORD_DEFAULTS, DiscordNotifier, FishingAlerts, webhook_url, protect_url, unprotect_url
from fishing_calculator import CALCULATOR_DEFAULTS, parse_calculator_field, calculator_settings, estimate_run, estimate_time
from fishing_session import RunJournal,SessionStore,RUN_DEFAULTS,parse_target,export_history,export_support
from fishing_safety import OvernightSafety
from fishing_reliability import RELIABILITY_DEFAULTS,parse_reserve,parse_health_minutes,HudReadError,RecoveryBudget,FailureRecorder
from fishing_remote import REMOTE_DEFAULTS,DiscordControls,RemoteReply,remote_settings,protect_token,unprotect_token
from fishing_updates import UPDATE_DEFAULTS,UpdateManager,feed_url,channel_feed,preferences as update_preferences,acknowledge_startup

TOGGLE_KEY = 0x5A
STOP_KEY = 0x78
POLL_SECONDS = .002
KEY_HOLD_SECONDS = .045
MIN_RING_RADIUS = 28
END_CONFIRM_SECONDS = .7
DEFAULT_DELAY = 1.5
APP_VERSION = '4.5'
DEFAULT_SETTINGS = dict(catch_delay=DEFAULT_DELAY, needs_enabled=True,
                        hunger_threshold=20, thirst_threshold=20, item_wait=8,overlay_enabled=False,
                        wait_limit=180,rod_slots=ROD_PRIORITY,overnight_safety=True)
DEFAULT_SETTINGS.update(DISCORD_DEFAULTS)
DEFAULT_SETTINGS.update(CALCULATOR_DEFAULTS)
DEFAULT_SETTINGS.update(RUN_DEFAULTS)
DEFAULT_SETTINGS.update(RELIABILITY_DEFAULTS)
DEFAULT_SETTINGS.update(REMOTE_DEFAULTS)
DEFAULT_SETTINGS.update(UPDATE_DEFAULTS)
LOGGER = logging.getLogger('fishing_helper')
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
kernel32.CreateMutexW.restype = w.HANDLE
try:
    ctypes.WinDLL('shcore').SetProcessDpiAwareness(2)
except OSError:
    user32.SetProcessDPIAware()

user32.GetDC.argtypes = [w.HWND]
user32.GetDC.restype = w.HDC
user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
user32.GetForegroundWindow.restype = w.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
user32.FindWindowW.restype = w.HWND
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.ShowWindow.restype = w.BOOL
gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.SelectObject.argtypes = [w.HDC, w.HANDLE]
gdi32.SelectObject.restype = w.HANDLE
gdi32.DeleteObject.argtypes = [w.HANDLE]
gdi32.DeleteDC.argtypes = [w.HDC]
gdi32.BitBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD]


class BitmapHeader(ctypes.Structure):
    _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG),
                ('planes', w.WORD), ('bits', w.WORD), ('compression', w.DWORD),
                ('image_size', w.DWORD), ('xppm', w.LONG), ('yppm', w.LONG),
                ('colors_used', w.DWORD), ('colors_important', w.DWORD)]


gdi32.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BitmapHeader), w.UINT,
                                  ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
gdi32.CreateDIBSection.restype = w.HBITMAP


class MouseInput(ctypes.Structure):
    _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('data', w.DWORD),
                ('flags', w.DWORD), ('time', w.DWORD), ('extra', ctypes.c_size_t)]


class KeyboardInput(ctypes.Structure):
    _fields_ = [('vk', w.WORD), ('scan', w.WORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('extra', ctypes.c_size_t)]


class InputUnion(ctypes.Union):
    _fields_ = [('mouse', MouseInput), ('keyboard', KeyboardInput)]


class Input(ctypes.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', w.DWORD), ('data', InputUnion)]


user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]
user32.SendInput.restype = w.UINT


def parse_delay(value):
    if isinstance(value, bool):
        raise ValueError('Enter a delay from 0.1 to 15 seconds.')
    delay = float(value)
    if not math.isfinite(delay) or not .1 <= delay <= 15:
        raise ValueError('Enter a delay from 0.1 to 15 seconds.')
    return delay


def load_settings(path):
    settings = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(data, dict):
            for key in ('stop_bait','stop_catches','stop_minutes'):
                try:settings[key]=parse_target(key,data.get(key,settings[key]))
                except (ValueError,TypeError,OverflowError):pass
            for key in ('require_supplies','learn_rates','overnight_safety','transient_recovery','failure_snapshots','overnight_preflight','inventory_space_check'):
                if type(data.get(key)) is bool:settings[key]=data[key]
            for key in CALCULATOR_DEFAULTS:
                try:settings[key]=parse_calculator_field(key,data.get(key,settings[key]))
                except (ValueError,TypeError,OverflowError):pass
            for key, parser in [('catch_delay',parse_delay),('hunger_threshold',parse_threshold),
                                ('thirst_threshold',parse_threshold),('item_wait',parse_item_wait),
                                ('wait_limit',parse_wait_limit),('rod_slots',parse_rod_slots)]:
                try: settings[key] = parser(data.get(key, settings[key]))
                except (ValueError,TypeError,OverflowError): pass
            for key,parser in (('inventory_reserve_kg',parse_reserve),('health_minutes',parse_health_minutes)):
                try:settings[key]=parser(data.get(key,settings[key]))
                except (ValueError,TypeError,OverflowError):pass
            if type(data.get('needs_enabled')) is bool:
                settings['needs_enabled'] = data['needs_enabled']
            if type(data.get('overlay_enabled')) is bool:
                settings['overlay_enabled'] = data['overlay_enabled']
            for key in DISCORD_DEFAULTS:
                if key!='discord_url' and type(data.get(key)) is bool:settings[key]=data[key]
            if data.get('discord_secret'):
                try:settings['discord_url']=unprotect_url(data['discord_secret'])
                except (OSError,ValueError,UnicodeError):settings['discord_enabled']=False
            if not settings['discord_url']:settings['discord_enabled']=False
            for key,default in REMOTE_DEFAULTS.items():
                if key=='remote_token':continue
                if type(data.get(key)) is type(default):settings[key]=data[key]
            if data.get('remote_secret'):
                try:settings['remote_token']=unprotect_token(data['remote_secret'])
                except (OSError,ValueError,UnicodeError):settings['remote_enabled']=False
            try:settings.update(remote_settings(settings))
            except ValueError:settings['remote_enabled']=False
            try:settings.update(update_preferences(data))
            except ValueError:pass
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    return settings


def parse_threshold(value):
    if type(value) is bool: raise ValueError('Use a needs threshold from 1 to 50%.')
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= 50:
        raise ValueError('Use a needs threshold from 1 to 50%.')
    return number


def parse_item_wait(value):
    if type(value) is bool: raise ValueError('Use an item wait from 1 to 30 seconds.')
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= 30:
        raise ValueError('Use an item wait from 1 to 30 seconds.')
    return number


def save_settings(path, settings):
    temporary = path.with_suffix('.json.tmp')
    try:
        stored=dict(settings)
        url=stored.pop('discord_url','')
        if url:stored['discord_secret']=protect_url(url)
        token=stored.pop('remote_token','')
        if token:stored['remote_secret']=protect_token(token)
        temporary.write_text(json.dumps(stored, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_wait_limit(value):
    if type(value) is bool:raise ValueError('Use a wait recovery limit from 60 to 900 seconds.')
    number=float(value)
    if not math.isfinite(number) or not 60<=number<=900:
        raise ValueError('Use a wait recovery limit from 60 to 900 seconds.')
    return number


class BoundedLogHandler(QueueHandler):
    def enqueue(self,record):
        try:self.queue.put_nowait(record)
        except queue.Full:
            try:self.queue.get_nowait()
            except queue.Empty:pass
            try:self.queue.put_nowait(record)
            except queue.Full:pass


class BoundedLogListener(QueueListener):
    def enqueue_sentinel(self):
        while True:
            try:self.queue.put_nowait(self._sentinel);return
            except queue.Full:
                try:self.queue.get_nowait()
                except queue.Empty:pass


def configure_diagnostics():
    # Disk writes run off the timing thread and the files have a fixed size limit.
    try:
        handler = RotatingFileHandler(data_dir()/'fishing-debug.log',
                                      maxBytes=1_000_000, backupCount=1, encoding='utf-8')
    except OSError:
        return None
    handler.setFormatter(logging.Formatter('%(message)s'))
    records = queue.Queue(maxsize=4096)
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(BoundedLogHandler(records))
    listener = BoundedLogListener(records, handler)
    listener.start()
    return listener


def process_id(hwnd):
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_game(hwnd):
    # Validate the executable, so a browser/document mentioning FiveM cannot receive keys.
    handle = kernel32.OpenProcess(0x1000, False, process_id(hwnd))
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = w.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        name = Path(buf.value).name.lower()
        return (name.startswith('fivem') and name.endswith('.exe')) or name in ('gta5.exe', 'gta5_enhanced.exe')
    finally:
        kernel32.CloseHandle(handle)


def angle_distance(a, b):
    return abs((a - b + 180) % 360 - 180)


def blue_mask(b, g, r):
    # Bright saturated UI blue; exclude the less saturated blue of daytime water.
    return (b>=130) & (g>=45) & (b>g*1.4) & (b>r*2)


def game_bounds(hwnd):
    rect, point = w.RECT(), w.POINT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)) or not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        raise OSError('Could not find game window bounds.')
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 100 or height < 100:
        raise OSError('The game window is minimized or too small.')
    return point.x, point.y, width, height


def strongest_run(counts, minimum, max_width, close_gap=0):
    active = [n >= minimum for n in counts]
    if not any(active) or all(active):
        return None
    if close_gap:
        original = list(active)
        for i in range(len(active)):
            if not original[i] or original[(i + 1) % len(active)]:
                continue
            for gap in range(1, close_gap + 1):
                if original[(i + gap + 1) % len(active)]:
                    for step in range(1, gap + 1):
                        active[(i + step) % len(active)] = True
                    break
        if all(active):
            return None
    # Start immediately after an empty bin to handle arcs crossing zero degrees.
    start = next(i for i, flag in enumerate(active) if not flag) + 1
    runs, current = [], []
    for offset in range(len(active)):
        i = (start + offset) % len(active)
        if active[i]:
            current.append(i)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    candidates = [r for r in runs if 2 <= len(r) * 2 <= max_width]
    if not candidates:
        return None
    best = max(candidates, key=lambda r: sum(counts[i] for i in r))
    weight = sum(counts[i] for i in best)
    # The white marker hides some blue pixels. Use the arc's geometric midpoint
    # so its center does not move away from the marker as they overlap.
    return (best[0] * 2 + len(best)) % 360, len(best) * 2, weight


class RingDetector:
    """Track blue target / white marker in the automatically located annulus."""
    def __init__(self, radius):
        self.radius = radius
        self.half = math.ceil(radius * 1.24)
        self.size = self.half * 2 + 1
        self.samples = []
        for y in range(self.size):
            for x in range(self.size):
                dx, dy = x - self.half, y - self.half
                if radius * .77 <= math.hypot(dx, dy) <= radius * 1.18:
                    angle = math.degrees(math.atan2(dy, dx)) % 360
                    self.samples.append(((y * self.size + x) * 4, int(angle / 2)))
        self.offsets = np.array([offset // 4 for offset, _ in self.samples], dtype=np.intp)
        self.buckets = np.array([bucket for _, bucket in self.samples], dtype=np.intp)
        self.sample_x=self.offsets%self.size-self.half
        self.sample_y=self.offsets//self.size-self.half
        self.glyph_checker = CircleLocator()

    def is_present(self, bgra):
        # The central E stays visible between stages even if the blue arc blinks.
        image = np.frombuffer(bgra, np.uint8).reshape(self.size, self.size, 4)
        return self.glyph_checker.central_e(image, self.half, self.half, self.radius) is not None

    def detect(self, bgra):
        pixels = np.frombuffer(bgra, np.uint8).reshape(-1, 4)[self.offsets, :3].astype(np.int16)
        b, g, r = pixels.T
        blue_pixels = blue_mask(b,g,r)
        white_pixels = (pixels.min(axis=1) >= 185) & (pixels.max(axis=1) - pixels.min(axis=1) <= 55)
        blue = np.bincount(self.buckets[blue_pixels], minlength=180).tolist()
        white = np.bincount(self.buckets[white_pixels], minlength=180).tolist()
        # The white marker can cover a few degrees of the blue target.
        target = strongest_run(blue, max(2, int(self.radius / 24)), 150, close_gap=3)
        marker = self.radial_marker(white_pixels,white)
        if target and (target[1] < 8 or target[2] < self.radius * .65):
            target = None
        return target, marker

    def radial_marker(self,white_pixels,counts):
        counts=list(counts)
        bin_angles=np.arange(len(counts))*2
        for _ in range(12):
            candidate=strongest_run(counts,1,22)
            if candidate is None:return None
            center,width,weight=candidate
            distance=np.abs((self.buckets*2-center+180)%360-180)
            selected=white_pixels & (distance<=width/2+1)
            angle=math.radians(center)
            radial=self.sample_x[selected]*math.cos(angle)+self.sample_y[selected]*math.sin(angle)
            tangent=-self.sample_x[selected]*math.sin(angle)+self.sample_y[selected]*math.cos(angle)
            if (len(radial)>=max(5,int(self.radius*.15)) and
                    float(np.ptp(radial))>=max(4,self.radius*.13) and
                    float(np.std(tangent))<=max(1.3,self.radius*.045)):
                return candidate
            bin_distance=np.abs((bin_angles-center+180)%360-180)
            for index in np.flatnonzero(bin_distance<=width/2+1):counts[int(index)]=0
        return None


class CircleLocator:
    """Search near blue components, then validate a circular ring and central E."""
    def __init__(self):
        self.glyph = np.array([[int(c == '#') for c in row] for row in
            ['########'] * 2 + ['###.....'] * 3 + ['#######.'] * 2 +
            ['###.....'] * 3 + ['########'] * 2], np.uint8)

    def central_e(self, image, cx, cy, radius):
        extent = max(10, round(radius * .28))
        x0, y0 = round(cx) - extent, round(cy) - extent
        if x0 < 0 or y0 < 0 or x0 + extent * 2 >= image.shape[1] or y0 + extent * 2 >= image.shape[0]:
            return None
        patch = image[y0:y0 + extent * 2, x0:x0 + extent * 2, :3].astype(np.int16)
        mask = ((patch.min(axis=2) > 130) & (patch.max(axis=2) - patch.min(axis=2) < 60)).astype(np.uint8)
        count, labels, stats, centers = cv2.connectedComponentsWithStats(mask)
        for i in range(1, count):
            x, y, width, height, area = stats[i]
            if not (.08 * radius <= width <= .30 * radius and .14 * radius <= height <= .45 * radius):
                continue
            if math.dist(centers[i], (extent, extent)) > radius * .13:
                continue
            shape = (labels[y:y + height, x:x + width] == i).astype(np.uint8)
            shape = cv2.resize(shape, (8, 12), interpolation=cv2.INTER_NEAREST)
            overlap = np.logical_and(shape, self.glyph).sum() / max(1, np.logical_or(shape, self.glyph).sum())
            if overlap > .65:
                return x0 + x + (width - 1) / 2, y0 + y + (height - 1) / 2
        return None

    def outline_score(self, image, cx, cy, radius):
        angles = np.arange(0, 360, 5) * math.pi/180
        distances = radius*np.array([.91, .96, 1, 1.04, 1.09])
        xx = np.rint(cx+np.cos(angles[:, None])*distances).astype(np.intp)
        yy = np.rint(cy+np.sin(angles[:, None])*distances).astype(np.intp)
        if xx.min()<0 or yy.min()<0 or xx.max()>=image.shape[1] or yy.max()>=image.shape[0]:
            return 0
        pixels = image[yy,xx,:3].astype(np.int16)
        gray = (pixels.min(axis=2)>=12) & (pixels.max(axis=2)<=120) & (np.ptp(pixels,axis=2)<=16)
        return float(gray.any(axis=1).mean())

    def validate_ring(self, image, cx, cy, radius):
        if radius < MIN_RING_RADIUS or self.outline_score(image,cx,cy,radius)<.55:
            return None
        detector = RingDetector(radius)
        cx, cy = round(cx), round(cy)
        left, top = cx-detector.half, cy-detector.half
        if left<0 or top<0 or left+detector.size>image.shape[1] or top+detector.size>image.shape[0]:
            return None
        crop = image[top:top+detector.size,left:left+detector.size]
        if not detector.is_present(crop.tobytes()):
            return None
        target, marker = detector.detect(crop.tobytes())
        if target and marker:
            return (cx,cy), radius
        return None

    def locate_from_glyph(self, image):
        # Find E first. The blue arc can join a huge water-colored component,
        # so isolated blue blobs are not a reliable discovery prerequisite.
        pixels = image[:,:,:3].astype(np.int16)
        mask = ((pixels.min(axis=2)>145) & (np.ptp(pixels,axis=2)<35)).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        for index in range(1,count):
            x,y,width,height,area = stats[index]
            if not (5<=width<=48 and 8<=height<=64 and .4<=width/height<=.95):
                continue
            shape = (labels[y:y+height,x:x+width]==index).astype(np.uint8)
            shape = cv2.resize(shape,(8,12),interpolation=cv2.INTER_NEAREST)
            overlap = np.logical_and(shape,self.glyph).sum()/max(1,np.logical_or(shape,self.glyph).sum())
            if overlap <= .65:
                continue
            estimated = height/.24
            radii = np.linspace(estimated*.78,estimated*1.22,13)
            candidates = []
            for radius in radii:
                cx = x+(width-1)/2+radius*.01
                cy = y+(height-1)/2+radius*.03
                score = self.outline_score(image,cx,cy,radius)
                if score>=.55:
                    candidates.append((score,-abs(radius-estimated),cx,cy,float(radius)))
            for _,_,cx,cy,radius in sorted(candidates,reverse=True):
                match = self.validate_ring(image,cx,cy,radius)
                if match:
                    return match
        return None

    def locate(self, bgra, width, height):
        image = np.frombuffer(bgra, np.uint8).reshape(height, width, 4)
        # Try the usual center first, then cover other circle locations.
        side = min(400,width,height)
        x0,y0 = (width-side)//2,(height-side)//2
        centered = self.locate_from_glyph(image[y0:y0+side,x0:x0+side])
        if centered:
            (cx,cy),radius = centered
            return (cx+x0,cy+y0),radius
        glyph_match = self.locate_from_glyph(image)
        if glyph_match:
            return glyph_match
        b, g, r = [image[:, :, i].astype(np.int16) for i in range(3)]
        mask = blue_mask(b,g,r).astype(np.uint8)
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        components = [stats[i] for i in range(1, count) if 25 <= stats[i, 4] <= 50000
                      and max(stats[i, 2], stats[i, 3]) <= 450]
        components.sort(key=lambda item: item[4], reverse=True)
        for x, y, box_width, box_height, area in components[:12]:
            span = max(box_width, box_height)
            margin = max(100, round(span * 2.5))
            x0, y0 = max(0, x - margin), max(0, y - margin)
            x1, y1 = min(width, x + box_width + margin), min(height, y + box_height + margin)
            tile = image[y0:y1, x0:x1, :3]
            gray = cv2.GaussianBlur(cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY), (5, 5), 0)
            circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, 1, 25,
                param1=50, param2=18, minRadius=MIN_RING_RADIUS, maxRadius=min(240, max(80, int(span * 3))))
            if circles is None:
                continue
            for tx, ty, outer_radius in circles[0]:
                cx, cy, radius = float(tx + x0), float(ty + y0), float(outer_radius * .93)
                center = self.central_e(image, cx, cy, radius)
                if center is None:
                    continue
                # Hough estimates can drift on small rings. Align the glyph's
                # optical center using the font offset measured in the reference,
                # retaining the ring estimate when the two centers agree closely.
                optical = center[0] + radius * .01, center[1] + radius * .03
                if math.dist((cx, cy), optical) > radius * .04:
                    cx, cy = optical
                cx, cy = round(cx), round(cy)
                match = self.validate_ring(image,cx,cy,radius)
                if match:
                    return match
        return None


class Capture:
    def __init__(self, x, y, size, height=None):
        self.x, self.y, self.size = x, y, size
        self.height = height if height is not None else size
        self.screen = self.dc = self.bitmap = self.previous = None
        self.closed = False
        self.bits = ctypes.c_void_p()
        if size <= 0 or self.height <= 0:
            raise ValueError('Capture dimensions must be positive.')
        try:
            self.screen = user32.GetDC(None)
            if not self.screen:
                raise OSError('Could not access the desktop capture device.')
            self.dc = gdi32.CreateCompatibleDC(self.screen)
            if not self.dc:
                raise OSError('Could not create a capture device.')
            header = BitmapHeader()
            header.size, header.width, header.height = ctypes.sizeof(header), size, -self.height
            header.planes, header.bits = 1, 32
            self.bitmap = gdi32.CreateDIBSection(self.screen, ctypes.byref(header), 0,
                                                ctypes.byref(self.bits), None, 0)
            if not self.bitmap or not self.bits.value:
                raise OSError('Could not create screen capture.')
            previous = gdi32.SelectObject(self.dc, self.bitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                raise OSError('Could not select the capture bitmap.')
            self.previous = previous
        except Exception:
            self.close()
            raise

    def read(self):
        if self.closed:
            raise RuntimeError('Capture is already closed.')
        if not gdi32.BitBlt(self.dc, 0, 0, self.size, self.height, self.screen,
                           self.x, self.y, 0x00CC0020 | 0x40000000):
            raise OSError('Screen capture failed')
        return ctypes.string_at(self.bits, self.size * self.height * 4)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.previous:
            gdi32.SelectObject(self.dc, self.previous)
        if self.bitmap:
            gdi32.DeleteObject(self.bitmap)
        if self.dc:
            gdi32.DeleteDC(self.dc)
        if self.screen:
            user32.ReleaseDC(None, self.screen)
        self.screen = self.dc = self.bitmap = self.previous = None


class StageGate:
    """Allow one press per stage, then wait for evidence of a new stage."""
    def __init__(self):
        self.armed = True
        self.last_press = -math.inf
        self.last_target = None
        self.previous_marker = self.previous_time = None
        self.changed_since = self.gap_since = None
        self.changed_target = None
        self.reset_seen = self.gap_seen = False
        self.reset_since = self.reset_origin = None
        self.reset_time = self.reset_speed = None
        self.confirmation = None
        self.motion_marker = self.motion_time = None
        self.motion_speeds = []

    def record_press(self, now, target, marker, sampled_at=None):
        self.armed = False
        self.last_press, self.last_target = now, target
        self.previous_marker, self.previous_time = marker[0], now
        self.changed_since = self.gap_since = None
        self.changed_target = None
        self.reset_seen = self.gap_seen = False
        self.reset_since = self.reset_origin = None
        self.reset_time = self.reset_speed = None
        self.confirmation = None
        self.motion_marker, self.motion_time = marker[0], now if sampled_at is None else sampled_at

    def confirmation_pending(self):
        return self.changed_since is not None or self.reset_since is not None or self.gap_since is not None

    def press_window_current(self,target,marker,sampled_at,now):
        elapsed=now-sampled_at
        if not 0<=elapsed<=.05:return False
        if len(self.motion_speeds)<3:return elapsed<=.02
        projected=marker[0]+statistics.median(self.motion_speeds)*elapsed
        return angle_distance(target[0],projected)<=max(2,target[1]/2-2)

    def observe(self, now, target, marker, present):
        angle = marker[0] if present and marker else None
        # A fast ordinary rotation can move >80 degrees between game frames.
        # Call it a reset only when it also disagrees with observed rotation.
        jump = False
        reference = None
        if angle is not None and self.motion_marker is not None:
            delta = (angle-self.motion_marker+180) % 360-180
            elapsed = now-self.motion_time
            if abs(delta) >= 2 and elapsed > 0:
                if elapsed <= .15 and len(self.motion_speeds) >= 3:
                    speed = statistics.median(self.motion_speeds)
                    predicted = self.motion_marker+speed*elapsed
                    # Small resets can start another stage with the same arc.
                    # Require a persistent departure from measured rotation,
                    # rather than an arbitrary 80-degree jump threshold.
                    jump = abs(delta) >= 24 and angle_distance(angle, predicted) > max(24,abs(speed*elapsed)*.6+10)
                    if jump:reference=(self.motion_marker,self.motion_time,speed)
                if not jump and self.reset_since is None and elapsed <= .15:
                    self.motion_speeds.append(delta/elapsed)
                    self.motion_speeds = self.motion_speeds[-7:]
                self.motion_marker, self.motion_time = angle, now
        elif angle is not None:
            self.motion_marker, self.motion_time = angle, now
        else:
            self.motion_marker = self.motion_time = None
            self.motion_speeds = []
        canceled_reset = False
        if self.reset_since is not None and angle is not None:
            expected = self.reset_origin+self.reset_speed*(now-self.reset_time)
            if angle_distance(angle, expected) < 24:
                self.reset_since = self.reset_origin = None
                self.reset_time = self.reset_speed = None
                self.reset_seen = False
                canceled_reset = True
            elif now - self.reset_since >= .025:
                self.reset_seen = True
        if jump and self.reset_since is None and not canceled_reset:
            self.reset_since = now
            self.reset_origin,self.reset_time,self.reset_speed = reference
        self.previous_marker, self.previous_time = angle, now
        if self.armed:
            return True
        if not present or (not target and not marker):
            self.gap_since = self.gap_since if self.gap_since is not None else now
            self.gap_seen |= now - self.gap_since >= .06
            self.changed_since = None
            self.changed_target = None
            return False
        if not marker:
            # A marker hidden by overlap/frame dropout is not a stage change.
            self.gap_since = None
            self.changed_since = None
            self.changed_target = None
            return False
        self.gap_since = None
        changed = target and self.last_target and (
            angle_distance(target[0], self.last_target[0]) > 7
            or abs(target[1] - self.last_target[1]) >= 6)
        if changed:
            consistent = self.changed_target and angle_distance(target[0],self.changed_target[0])<=4 and abs(target[1]-self.changed_target[1])<=4
            if not consistent:self.changed_since,self.changed_target = now,target
        else:
            self.changed_since = None
            self.changed_target = None
        if now - self.last_press < .08:
            return False
        stable_change = self.changed_since is not None and now - self.changed_since >= .025
        if stable_change or self.reset_seen or self.gap_seen:
            self.armed = True
            self.confirmation = ('New blue target' if stable_change else
                                 'Marker reset' if self.reset_seen else 'Stage returned after gap')
        return self.armed


def press_stalled(pending,gate,now):
    # Give a just-observed transition its normal confirmation interval before
    # declaring the previous E unsuccessful. The 60-second guard still applies.
    return pending.stalled(now) and not gate.confirmation_pending()


class RunComplete(Exception):
    pass

class RetryCycle(Exception):
    """A visible failure/unequipped notice allows another equip attempt."""


class ActiveChallenge(Exception):
    def __init__(self,ring):
        super().__init__('Circle appeared during supply reading.')
        self.ring=ring


class PressConfirmation:
    """Track an outstanding input, with one retry after a full observed turn."""
    def __init__(self):
        self.pending = False
        self.attempts = 0
        self.travel = 0
        self.previous = None
        self.pressed_at = 0

    def sent(self, now, marker):
        self.pending = True
        self.attempts += 1
        self.travel = 0
        self.previous = marker[0]
        self.pressed_at = now

    def observe(self, marker):
        if marker is None:
            self.previous = None
            return
        if self.previous is not None:
            delta = (marker[0] - self.previous + 180) % 360 - 180
            if abs(delta) < 80:
                self.travel += delta
        self.previous = marker[0]

    def can_retry(self, now):
        return self.pending and self.attempts == 1 and abs(self.travel) >= 330 and now - self.pressed_at >= .7

    def stalled(self, now):
        return self.pending and self.attempts >= 2 and abs(self.travel) >= 360 and now - self.pressed_at >= 2

    def accept(self):
        accepted = self.pending
        self.pending = False
        self.attempts = 0
        self.travel = 0
        self.previous = None
        return accepted


class FishingWorker:
    def __init__(self, settings, messages):
        self.settings, self.messages = settings, messages
        self.stop = threading.Event()
        self.hwnd = user32.GetForegroundWindow()
        self.pid = process_id(self.hwnd)
        self.game_verified = is_game(self.hwnd)
        self.capture_bounds = None
        self.pressed_scan = None
        self.last_key_sent = None
        self.stats = settings.get('_stats') or SessionStats()
        self.telemetry = settings.get('_telemetry')
        self.monitor = None
        self.cycle_started = 0
        self.cycle_id = None
        self.reward_notices = []
        self.ocr_candidate = ''
        self.reward_debug_signature = ''
        self.outcome = None
        self.supplies = settings.get('_supplies') or SupplySession()
        if 'rod_slots' in settings:self.supplies.configure_rod_slots(settings['rod_slots'])
        self.rod_changed = False
        self.rod_inventory_sample = 0
        self.had_challenge = False
        self.alerts=settings.get('_alerts')
        self.journal=settings.get('_journal')
        self.preflight_done=False
        self.overnight=OvernightSafety()
        self.recovery_budget=RecoveryBudget();self.recognition_blocked=False;self.recognition_generation=0
        self.recorder=FailureRecorder(data_dir()/'failure-snapshots',settings.get('failure_snapshots',False))
        self.snapshot_cycles=set()

    def ensure_recognition(self,active=False):
        if self.monitor is None or self.stop.is_set():return
        issue=None;health=getattr(self.monitor,'health_issue',None)
        if callable(health):issue=health(time.monotonic())
        if not isinstance(issue,str):
            state=getattr(self.monitor,'state','');needs=getattr(self.monitor,'needs_error','')
            issue=state if isinstance(state,str) and state.startswith('OCR unavailable') else (
                'Hunger/thirst OCR unavailable: '+needs if isinstance(needs,str) and needs else None)
        if not issue:return
        self.recognition_blocked=True
        if not self.settings.get('transient_recovery',False):raise RuntimeError(issue)
        if not self.recovery_budget.claim('ocr',time.monotonic()):raise RuntimeError(issue+' Automatic OCR recovery limit reached; press Z after checking the game.')
        self.release_key();old=self.monitor
        inventory_requested=getattr(old,'inventory_requested',False) is True
        needs_requested=getattr(old,'needs_requested',False) is True
        self.status('Recovering local recognition. Waiting for fresh scans; no keys are being sent…')
        self.log('ocr_recovery',reason=issue)
        old.close()
        for name in ('thread','hud_thread'):
            thread=getattr(old,name,None)
            if thread is not None and thread.ident is not None and thread.is_alive():
                raise RuntimeError('Previous OCR thread did not close. No keys were sent; check recognition before restarting.')
        queued=old.drain()
        monitor=OutcomeMonitor(self.hwnd,Capture,game_bounds,
            lambda:user32.GetForegroundWindow()==self.hwnd and process_id(self.hwnd)==self.pid,
            needs_enabled=self.settings.get('needs_enabled',False),rod_slots=self.supplies.snapshot()['rod_slots'],capacity_enabled=self.settings.get('overnight_preflight',False) and self.settings.get('inventory_space_check',False))
        monitor.inventory_requested=inventory_requested;monitor.needs_requested=needs_requested
        monitor.events.extend(queued[-32:]);self.monitor=monitor
        restarted_at=time.monotonic();monitor.start();deadline=restarted_at+12
        while not monitor.initialized.is_set() and time.monotonic()<deadline:self.delay(.05)
        self.check()
        if not monitor.initialized.is_set():raise RuntimeError('OCR recovery timed out. No keys were sent; press Z to retry.')
        issue=monitor.health_issue(time.monotonic())
        if issue:raise RuntimeError('OCR recovery failed: '+issue)
        if active and monitor.inventory_visible:
            raise RuntimeError('Recognition recovered, but inventory is open during fishing. Close it and check the game before restarting.')
        # A process reporting "ready" is not enough. Require a fresh game cue
        # before unlocking any keys, without probing inventory with Tab.
        deadline=restarted_at+20;last_ring_check=-math.inf
        while True:
            self.check();now=time.monotonic()
            issue=monitor.health_issue(now)
            if issue:raise RuntimeError('OCR recovery failed: '+issue)
            inventory=monitor.inventory
            inventory_fresh=(isinstance(inventory,tuple) and len(inventory)==2 and inventory[0]>=restarted_at
                             and all(type(inventory[1].get(slot)) is bool for slot in self.supplies.snapshot()['rod_slots']))
            needs,_=monitor.needs_snapshot()
            needs_fresh=all(kind in needs and needs[kind][0]>=restarted_at and 0<=now-needs[kind][0]<=NEED_MAX_AGE
                            for kind in ('hunger','thirst'))
            prompt_fresh=monitor.waiting_prompt is True and monitor.prompt_sampled_at>=restarted_at
            if inventory_fresh or needs_fresh or prompt_fresh:break
            if self.settings.get('outcome_ocr',False) and now-last_ring_check>=.25:
                last_ring_check=now;ring=self.relocate_ring()
                if ring:
                    ring[1].close();break
            if now>=deadline:raise RuntimeError('Recognition restarted, but a fresh inventory, HUD, fishing prompt or circle could not be verified. No keys were sent.')
            self.delay(.05)
        with self.supplies.lock:self.supplies.rod_inventory_checked=False
        self.preflight_done=False;self.recognition_generation+=1;self.recognition_blocked=False
        self.status('Local recognition recovered from fresh scans. Rechecking state before the next attempt.')

    def failure_snapshot(self,reason,cycle=None):
        recorder=getattr(self,'recorder',None)
        if recorder is None:return
        cycle=self.cycle_id if cycle is None else cycle
        key=(cycle,str(reason))
        if key in self.snapshot_cycles:return
        self.snapshot_cycles.add(key)
        if len(self.snapshot_cycles)>64:self.snapshot_cycles=set(sorted(self.snapshot_cycles,key=lambda k:k[0] or 0)[-64:])
        recorder.snapshot(reason,reported_cycle=cycle,stats=self.stats.snapshot(),last_key_sent=getattr(self,'last_key_sent',None))

    def protect_overnight(self):
        if not self.settings.get('overnight_safety',False):return
        while True:
            self.check()
            reason,grace=self.overnight.assess(self.stats,time.monotonic())
            if reason is None:return
            if grace<=0:raise RuntimeError(reason)
            # A late reward can repair Unknown. Do not re-equip during that
            # bounded collection window; no extra fishing/supply key is sent.
            self.status('Overnight safety: waiting for late catch confirmation…')
            self.delay(min(.1,grace))
            self.collect_notices()

    def discord_result(self,cycle,text,sampled_at=None):
        if getattr(self,'journal',None) and cycle is not None:self.journal.observe(cycle,text,sampled_at)
        if self.alerts and cycle is not None:self.alerts.observe(cycle,text,sampled_at)

    def publish(self, **data):
        if self.telemetry is None:
            return
        data['ocr'] = self.monitor.state if self.monitor else 'Outcome recognition inactive'
        if self.monitor is not None and self.settings.get('needs_enabled',False):
            data['needs'] = dict(getattr(self.monitor,'needs',{}))
        # A single latest snapshot prevents old debug frames delaying the UI.
        try:
            self.telemetry.put_nowait(data)
        except queue.Full:
            try: self.telemetry.get_nowait()
            except queue.Empty: pass
            try: self.telemetry.put_nowait(data)
            except queue.Full: pass

    def collect_notices(self, waiting=False, active=False):
        if self.monitor is None:
            return
        # Frame timestamps identify the attempt, even when OCR finishes later.
        notices = self.monitor.drain()
        if self.alerts:
            self.alerts.inventory_amounts(getattr(self.monitor,'inventory_amounts',None))
        candidate = getattr(self.monitor, 'candidate', '')
        if isinstance(candidate, str) and candidate and candidate != self.ocr_candidate:
            self.ocr_candidate = candidate
            self.log('ocr_candidate', text=candidate)
        reward_debug=getattr(self.monitor,'reward_debug',None)
        if isinstance(reward_debug,list) and reward_debug:
            signature=json.dumps(reward_debug)
            if signature!=getattr(self,'reward_debug_signature',''):
                self.reward_debug_signature=signature
                self.log('reward_ocr',views=reward_debug)
        recover = None
        blocked = None
        for sampled_at, kind, text in notices:
            self.log('notice', kind=kind, text=text)
            if kind == 'blocked':
                blocked = text
                continue
            if kind == 'broken' and self.settings.get('rod_backups',False):
                old=self.supplies.rod
                if self.supplies.break_rod(sampled_at):
                    if self.alerts:self.alerts.rod(old,self.supplies.rod,confirmed_break=True)
                    self.rod_changed = True
                    if waiting or active: recover = text
                continue
            if kind in ('gain','bonus'):
                self.reward_notices.append((sampled_at, kind, text))
                continue
            cycle = self.stats.notice_cycle(sampled_at, kind) if self.cycle_id is not None else None
            if self.cycle_id is not None and cycle != self.cycle_id:
                if cycle is not None and kind in ('catch', 'failure'):
                    self.stats.finish(kind, cycle,confirmed_at=sampled_at)
                    if kind=='failure':self.failure_snapshot(text,cycle)
                    if kind=='catch':self.discord_result(cycle,text,sampled_at)
                    self.log('late_result', cycle=cycle, kind=kind)
                continue
            if kind == 'rod':
                if waiting and self.outcome not in ('catch', 'failure'):
                    self.outcome = 'rod'
                    recover = text
            elif kind == 'failure':
                self.outcome = 'failure'
                if waiting: recover = text
            elif kind == 'catch' and self.outcome != 'failure':
                self.outcome = 'catch'
                if waiting: recover = text
            if kind in ('catch', 'failure') and self.cycle_id is not None:
                self.stats.finish(self.outcome, self.cycle_id,confirmed_at=sampled_at)
                if kind=='catch':self.discord_result(self.cycle_id,text,sampled_at)
                else:self.failure_snapshot(text,self.cycle_id)
        self.collect_rewards()
        if self.alerts:self.alerts.flush()
        if blocked is not None:raise RuntimeError(f'Fishing needs attention: {blocked}')
        if not self.stop.is_set():self.ensure_recognition(active=waiting or active)
        if recover is not None:
            raise RetryCycle(recover)

    def verify_rod(self):
        if not self.settings.get('rod_backups',False) or self.monitor is None:
            return
        if self.monitor.state.startswith('OCR unavailable'):
            raise RuntimeError('Rod verification needs local OCR. Check the game display, then press Z.')
        inventory = getattr(self.monitor,'inventory',None)
        if not isinstance(inventory,tuple) or len(inventory)!=2:
            return
        sampled,states = inventory
        if sampled <= self.rod_inventory_sample:
            return
        self.rod_inventory_sample = sampled
        equipped = self.supplies.equipped_at
        if sampled < equipped+.5 or time.monotonic()-sampled > INVENTORY_MAX_AGE:
            return
        old = self.supplies.rod
        self.supplies.inventory(states)
        if states.get(old) is False:
            if self.alerts:self.alerts.rod(old,self.supplies.rod)
            self.rod_changed = True
            self.log('rod_slot_unusable',slot=old,next_slot=self.supplies.rod)
            raise RetryCycle('Inventory confirms the selected slot contains no fishing rod.')

    def inspect_inventory(self, count_recovery=True):
        for attempt in range(2):
            try:return self._inspect_inventory_once(count_recovery)
            except RuntimeError as error:
                if (attempt or not str(error).startswith('Could not read enabled rod slots') or
                        not getattr(self,'_last_inventory_opened',False)):raise
                self.log('inventory_retry',diagnostic=getattr(self.monitor,'inventory_debug',{}))
                self.status('Inventory text was unclear. Retrying once with focused label recognition…')
                self.delay(.5)
                if self.settings.get('outcome_ocr',False):
                    ring=self.relocate_ring()
                    if ring:raise ActiveChallenge(ring)

    def _inspect_inventory_once(self, count_recovery=True):
        """Read actual rod cells between attempts, without using prompt absence."""
        if not self.settings.get('rod_backups',False): return
        if self.monitor is None:
            raise RuntimeError('Rod inventory checks need local OCR.')
        slots = tuple(self.supplies.snapshot()['rod_slots'])
        slot_names = ', '.join(slots)
        self.status(f'Checking enabled rod slots {slot_names} in inventory…')
        requested = time.monotonic()
        already_open = getattr(self.monitor,'inventory_visible',False) is True
        self._last_inventory_opened=already_open
        states = None
        amounts = None
        capacity = None
        self.monitor.inventory_requested=True
        try:
            if not already_open:self.key('TAB')
            deadline = time.monotonic()+18
            while time.monotonic()<deadline:
                self.check()
                self.ensure_recognition()
                if getattr(self.monitor,'inventory_visible',False) is True:self._last_inventory_opened=True
                inventory = getattr(self.monitor,'inventory',None)
                if (isinstance(inventory,tuple) and len(inventory)==2 and
                        inventory[0]>=requested-(3 if already_open else 0) and
                        0 <= time.monotonic()-inventory[0] <= INVENTORY_MAX_AGE):
                    candidate = inventory[1]
                    if all(type(candidate.get(slot)) is bool for slot in slots):
                        states = {slot:candidate[slot] for slot in slots}
                        amounts=getattr(self.monitor,'inventory_amounts',None)
                        capacity=getattr(self.monitor,'inventory_capacity',None)
                        if isinstance(amounts,tuple) and len(amounts)==2 and isinstance(amounts[1],dict):
                            amounts=(amounts[0],dict(amounts[1]))
                        if self.settings.get('overnight_preflight',False) and not self.preflight_done:
                            fresh_capacity=(isinstance(capacity,tuple) and len(capacity)==2 and capacity[0]>=requested-(3 if already_open else 0))
                            fresh_amounts=(isinstance(amounts,tuple) and len(amounts)==2 and amounts[0]>=requested-(3 if already_open else 0)
                                           and all(type(amounts[1].get(kind)) is int for kind in ('bait','food','water')))
                            if not (fresh_amounts and (fresh_capacity or not self.settings.get('inventory_space_check',False))):
                                states=None;self.delay(.05);continue
                        break
                if str(self.monitor.state).startswith('OCR unavailable'):
                    raise RuntimeError('Inventory OCR is unavailable. Rod slots were not marked empty.')
                self.publish(mode='Checking inventory rod slots',present=False)
                self.delay(.05)
        finally:
            self.monitor.inventory_requested=False
            # Escape only closes a positively identified inventory. If Tab did
            # not open it, don't accidentally open the game's pause menu.
            if getattr(self.monitor,'inventory_visible',False) is True and not self.stop.is_set():
                closed_at = time.monotonic()
                self.key('ESC')
                deadline = time.monotonic()+8
                while time.monotonic()<deadline:
                    self.check()
                    if (getattr(self.monitor,'inventory_visible',True) is False and
                            getattr(self.monitor,'prompt_sampled_at',0)>=closed_at):
                        break
                    self.delay(.05)
                else:
                    raise RuntimeError('Could not verify inventory closed. No rod key was sent.')
        if states is None:
            if self.settings.get('overnight_preflight',False) and not self.preflight_done:
                required='enabled rods, bait and food/water'+(' and inventory weight' if self.settings.get('inventory_space_check',False) else '')
                raise RuntimeError('Overnight preflight could not confirm '+required+'. Keep the complete inventory visible, then retry.')
            raise RuntimeError(f'Could not read enabled rod slots {slot_names} in inventory. Check Tab opens inventory, then press Z. No rods were marked empty.')
        old = self.supplies.rod
        self.supplies.inventory(states)
        if old!=self.supplies.rod and self.supplies.equipped_at!=-float('inf') and self.alerts:
            self.alerts.rod(old,self.supplies.rod)
        self.log('rod_inventory',slots=states,selected=self.supplies.rod)
        if all(states[slot] is False for slot in slots):
            raise RuntimeError(f'Inventory confirms no fishing rods in slots {slot_names} (enabled). Refill them, then press Z.')
        if self.alerts:self.alerts.inventory_amounts(amounts)
        if self.journal:
            self.journal.inventory(amounts,self.stats.snapshot()['elapsed'])
            self.journal.record_capacity(capacity)
            detail,short,unknown=self.journal.preflight(self.settings,{**self.supplies.snapshot(),'rod_checked':True})
            if self.settings.get('overnight_preflight',False):
                detail,short,unknown=self.journal.overnight_check(self.settings,{**self.supplies.snapshot(),'rod_checked':True})
            self.log('preflight',detail=detail,short=short,unknown=unknown)
            if (self.settings.get('require_supplies',False) or self.settings.get('overnight_preflight',False)) and not self.preflight_done and (short or unknown):
                raise RuntimeError('Pre-run supply check: '+detail+'. Refill supplies or adjust the plan in Planner.')
            if (self.settings.get('overnight_preflight',False) and self.settings.get('inventory_space_check',False) and isinstance(capacity,tuple) and len(capacity)==2
                    and 0<=time.monotonic()-capacity[0]<=INVENTORY_MAX_AGE and
                    (capacity[1][1]-capacity[1][0]<self.settings.get('inventory_reserve_kg',5) or capacity[1][0]>=capacity[1][1])):
                raise RuntimeError('Inventory space is below the configured headroom. Clear inventory before continuing.')
        if (isinstance(amounts,tuple) and len(amounts)==2 and isinstance(amounts[1],dict) and
                0<=time.monotonic()-amounts[0]<=INVENTORY_MAX_AGE and amounts[0]>=requested-(3 if already_open else 0) and
                '1' not in slots and amounts[1].get('bait')==0):
            raise RuntimeError('Bait slot 1 is empty. Refill Artificial Bait, then press Z.')
        self.supplies.rod_inventory_verified()
        self.preflight_done=True
        if self.supplies.rod != old and count_recovery:
            self.stats.add(recoveries=1)
        return states

    def recover_long_wait(self):
        # Never infer a broken rod from a slow bite. Recheck the visible circle
        # before touching inventory, then read the actual numbered rod cells.
        visible=self.relocate_ring()
        if visible:return visible
        if not self.settings.get('rod_backups',False) or self.monitor is None:
            raise RuntimeError('No fishing circle appeared within the wait limit. Check fishing, then press Z.')
        old=self.supplies.rod
        self.log('wait_recovery',slot=old,limit=self.settings.get('wait_limit',180))
        try:self.inspect_inventory(count_recovery=False)
        except ActiveChallenge as challenge:return challenge.ring
        self.collect_notices(waiting=True)
        if self.supplies.rod!=old:
            self.rod_changed=True
            raise RetryCycle('Inventory confirms the selected slot contains no fishing rod. Switching to a stocked slot.')
        visible=self.relocate_ring()
        if visible:return visible
        sampled=getattr(self.monitor,'prompt_sampled_at',0)
        if (self.monitor.waiting_prompt is True and
                0<=time.monotonic()-sampled<=NEED_MAX_AGE):
            self.status('Fishing is still active. Waiting for the circle…')
            return None
        raise RuntimeError('Rod is present, but fishing activity could not be confirmed after the wait limit. Check fishing, then press Z.')

    def needs_readings(self, after=0, kind=None, confirm_low=False,allow_circle=False,minimum=None):
        if self.monitor is not None:self.monitor.needs_requested=True
        try:
            options={}
            if allow_circle:options['allow_circle']=True
            if minimum is not None:options['minimum']=minimum
            for attempt in range(2):
                try:return self._needs_readings(after,kind,confirm_low,**options)
                except HudReadError:
                    reset=getattr(self.monitor,'reset_needs',None)
                    if (attempt or not self.settings.get('transient_recovery',False) or not callable(reset)
                            or not self.recovery_budget.claim('hud',time.monotonic())):raise
                    reset();self.status('Retrying temporary HUD recognition with fresh readings. No item key was sent.')
                    self.log('hud_recovery');self.delay(.5)
        finally:
            if self.monitor is not None:self.monitor.needs_requested=False

    def _needs_readings(self, after=0, kind=None, confirm_low=False,allow_circle=False,minimum=None):
        deadline = time.monotonic()+(30 if allow_circle else NEED_MAX_AGE)
        previous_diagnostic = None
        last_circle_check=-float('inf')
        while True:
            self.check()
            self.ensure_recognition()
            now=time.monotonic()
            if allow_circle and self.settings.get('outcome_ocr',False) and now-last_circle_check>=.25:
                last_circle_check=now
                ring=self.relocate_ring()
                if ring:raise ActiveChallenge(ring)
            state=getattr(self.monitor,'state','')
            if isinstance(state,str) and state.startswith('OCR unavailable'):raise RuntimeError(state)
            needs_error=getattr(self.monitor,'needs_error','')
            if isinstance(needs_error,str) and needs_error:
                raise RuntimeError('Hunger/thirst OCR unavailable: '+needs_error)
            snapshot = getattr(self.monitor,'needs_snapshot',None)
            pair = snapshot() if callable(snapshot) else None
            if isinstance(pair,tuple) and len(pair)==2:
                readings,trust = pair
            else:
                readings = getattr(self.monitor,'needs',{}) if self.monitor is not None else {}
                trust = getattr(self.monitor,'needs_low_confirmed',{}) if self.monitor is not None else {}
            names = (kind,) if kind is not None else ('hunger','thirst')
            now = time.monotonic()
            fresh = all(name in readings and readings[name][0] >= after and
                        0 <= now-readings[name][0] <= NEED_MAX_AGE for name in names)
            uncertain = fresh and confirm_low and any(
                readings[name][1] <= self.settings.get(name+'_threshold',20) and
                trust.get(name) is not True for name in names)
            recovery_pending=fresh and minimum is not None and any(readings[name][1]<minimum for name in names)
            if fresh and not uncertain and not recovery_pending: return readings
            diagnostic = getattr(self.monitor,'needs_debug',None) if self.monitor is not None else None
            if isinstance(diagnostic,dict) and diagnostic and diagnostic != previous_diagnostic:
                self.log('needs_scan',**diagnostic)
                previous_diagnostic = dict(diagnostic)
            if now >= deadline:
                if recovery_pending:
                    slot='4' if kind=='hunger' else '5'
                    raise RuntimeError(f'Slot {slot} did not restore {kind}. Refill/check that slot, then press Z.')
                if uncertain:
                    raise HudReadError(f'Low {kind or "hunger/thirst"} reading is uncertain. No item was used; press Z to retry.')
                if isinstance(diagnostic,dict) and diagnostic.get('icons_found') is True:
                    missing=', '.join(name for name in names if name not in readings or now-readings[name][0]>NEED_MAX_AGE)
                    raise HudReadError(f'HUD icons are visible, but {missing or "percentage"} text is unreadable. No food or water was used.')
                raise HudReadError('Hunger/thirst HUD could not be read. Keep both indicators visible, then press Z.')
            self.publish(mode=f'Waiting for {kind} recovery' if minimum is not None else 'Reading hunger / thirst',present=False)
            self.delay(.05)

    def maintain_supplies(self):
        if not self.settings.get('needs_enabled',False): return
        # A Z pause or input-release error may interrupt an item animation.
        # Verify the previous use before sending another supply or rod key.
        for kind,(sent,before) in self.supplies.snapshot()['pending_uses'].items():
            self.verify_supply_use(kind,sent,before)
        readings = self.needs_readings(allow_circle=True)
        thresholds = {kind:self.settings.get(kind+'_threshold',20) for kind in ('hunger','thirst')}
        for kind in self.supplies.due(readings,thresholds,time.monotonic()):
            readings = self.needs_readings(kind=kind,confirm_low=True,allow_circle=True)
            before = readings[kind][1]
            if before > thresholds[kind]: continue
            slot = '4' if kind == 'hunger' else '5'
            self.status(f"Low {kind} ({before}%). Using {'food' if slot=='4' else 'water'} from slot {slot}…")
            sent = time.monotonic()
            self.last_key_sent = None
            try:
                self.key(slot)
            finally:
                if self.last_key_sent is not None and self.last_key_sent[0] == slot:
                    self.supplies.used(kind,sent,before)
            self.supplies.used(kind,sent,before)
            self.verify_supply_use(kind,sent,before)

    def verify_supply_use(self, kind, sent, before):
        wait = self.settings.get('item_wait',8)
        self.delay(max(0,sent+wait-time.monotonic()))
        after = self.needs_readings(after=sent+wait,kind=kind,minimum=before+2)[kind][1]
        slot = '4' if kind == 'hunger' else '5'
        if after < before+2:
            raise RuntimeError(f'Slot {slot} did not restore {kind}. Refill/check that slot, then press Z.')
        self.supplies.restored(kind)
        if self.alerts:self.alerts.consumed(kind,sent)
        self.log('supply_used',kind=kind,slot=slot,before=before,after=after)

    def collect_rewards(self):
        remaining = []
        now = time.monotonic()
        for sampled_at, kind, text in self.reward_notices[-32:]:
            cycle = self.stats.notice_cycle(sampled_at, kind)
            if cycle is None:
                if now-sampled_at <= 8:
                    remaining.append((sampled_at, kind, text))
                continue
            if kind=='bonus':
                with self.stats.lock:result=self.stats.attempts[cycle]['result']
                if result=='catch':self.discord_result(cycle,text)
                elif result!='failure' and now-sampled_at<=8:remaining.append((sampled_at,kind,text))
                continue
            if cycle == self.cycle_id and self.outcome != 'failure':
                self.outcome = 'catch'
            if self.stats.finish('catch', cycle,confirmed_at=sampled_at):
                self.log('inventory_reward', cycle=cycle, text=text)
            self.discord_result(cycle,text,sampled_at)
        self.reward_notices = remaining

    def log(self, event, **details):
        if event in ('error','stage_stalled'):self.failure_snapshot(details.get('reason',event))
        if self.settings.get('debug') or event in ('error','stage_stalled','paused','completed','key_release_error','ocr_close_error','final_notice_error'):
            LOGGER.info(json.dumps({'time': round(time.monotonic(), 4), 'event': event, **details}))

    def press_match(self, target, marker, retry, pending, gate, sampled_at=None):
        started = time.monotonic()
        if sampled_at is not None and not gate.press_window_current(target,marker,sampled_at,started):
            self.log('stale_match_skipped',target=target,marker=marker,age=started-sampled_at)
            return False
        self.last_key_sent = None
        sent = False
        try:
            self.key('E')
            sent = True
        finally:
            # Z/focus loss can interrupt the hold after Windows accepted E.
            # Preserve that real input for a reward received during shutdown.
            if sent or (self.last_key_sent is not None and self.last_key_sent[0] == 'E'):
                prioritize=getattr(self.monitor,'prioritize_rewards',None)
                if callable(prioritize):prioritize()
                self.stats.add(inputs=1,retries=int(retry))
                self.stats.observe(self.cycle_id,sent=started)
                pressed_at = self.last_key_sent[1] if self.last_key_sent is not None and self.last_key_sent[0]=='E' else started
                pending.sent(pressed_at,marker)
                gate.record_press(pressed_at,target,marker,sampled_at=sampled_at if sampled_at is not None else started)
                self.log('matched_press',target=target,marker=marker,retry=retry,
                         sampled_at=sampled_at,pressed_at=pressed_at,hold_finished=time.monotonic())
        return True

    def status(self, text):
        alerts=getattr(self,'alerts',None)
        health=getattr(alerts,'set_health_state',None) if alerts else None
        if self.settings.get('discord_health',False) and callable(health):health(text,self.settings.get('health_minutes',15))
        try:self.messages.put_nowait(text)
        except queue.Full:
            try:self.messages.get_nowait()
            except queue.Empty:pass
            try:self.messages.put_nowait(text)
            except queue.Full:pass

    def check(self):
        if self.stop.is_set():
            raise InterruptedError()
        foreground = user32.GetForegroundWindow()
        if not self.game_verified or foreground != self.hwnd or process_id(foreground) != self.pid:
            raise InterruptedError('Paused because the game lost focus.')
        if user32.GetAsyncKeyState(STOP_KEY) & 0x8000:
            raise InterruptedError('Paused with F9.')

    def delay(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            self.stop.wait(min(.015, max(0, until - time.monotonic())))

    def key(self, character):
        if getattr(self,'recognition_blocked',False):raise RuntimeError('Recognition recovery has not verified fresh state. No key was sent.')
        self.check()
        self.log('key', key=character)
        scans = {'E':0x12,'1':0x02,'2':0x03,'3':0x04,'4':0x05,'5':0x06,'TAB':0x0f,'ESC':0x01}
        if character not in scans:
            raise ValueError('Only E, slots 1–5, Tab and Escape are supported.')
        scan = scans[character]
        down = Input(type=1, keyboard=KeyboardInput(0, scan, 0x0008, 0, 0))
        if user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(Input)) != 1:
            raise OSError('Windows blocked keyboard input.')
        self.last_key_sent = (character,time.monotonic())
        self.pressed_scan = scan
        try:
            self.delay(KEY_HOLD_SECONDS if character == 'E' else .06)
        finally:
            self.release_key()

    def release_key(self):
        if self.pressed_scan is None:
            return
        up = Input(type=1, keyboard=KeyboardInput(0, self.pressed_scan, 0x0008 | 0x0002, 0, 0))
        for _ in range(3):
            if user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(Input)) == 1:
                self.pressed_scan = None
                return
        raise OSError('Windows blocked key release. Press and release the last E / number key manually.')

    def find_ring(self):
        self.status('Rod equipped. Waiting for the fishing circle to appear…')
        bounds = game_bounds(self.hwnd)
        left, top, width, height = bounds
        full = Capture(left, top, width, height)
        locator = CircleLocator()
        waiting_since=time.monotonic()
        try:
            while True:
                self.check()
                self.collect_notices(waiting=True)
                self.protect_overnight()
                current = game_bounds(self.hwnd)
                if current != bounds:
                    full.close()
                    bounds = current
                    left, top, width, height = bounds
                    full = Capture(left, top, width, height)
                match = locator.locate(full.read(), width, height)
                self.check()
                if match:
                    center, radius = match
                    self.log('circle_found', center=center, radius=radius)
                    detector = RingDetector(radius)
                    capture = Capture(left + center[0] - detector.half,
                                      top + center[1] - detector.half, detector.size)
                    self.status('Circle found automatically. Tracking the moving marker.')
                    self.capture_bounds = bounds
                    return detector, capture
                self.verify_rod()
                if time.monotonic()-waiting_since>=self.settings.get('wait_limit',180):
                    recovered=self.recover_long_wait()
                    if recovered:return recovered
                    waiting_since=time.monotonic()
                self.publish(mode='Waiting for circle', present=False)
                self.delay(.02)
        finally:
            full.close()

    def relocate_ring(self):
        """Recheck the entire client before assuming a lost circle has ended."""
        self.check()
        bounds = game_bounds(self.hwnd)
        left, top, width, height = bounds
        full = Capture(left, top, width, height)
        try:
            match = CircleLocator().locate(full.read(), width, height)
            self.check()
            if match:
                center, radius = match
                detector = RingDetector(radius)
                capture = Capture(left+center[0]-detector.half, top+center[1]-detector.half, detector.size)
                self.capture_bounds = bounds
                return detector, capture
        finally:
            full.close()

    def run(self):
        capture = None
        existing = None
        pending = None
        first_cycle = True
        try:
            if (self.supplies.snapshot()['pending_uses'] and
                    not self.settings.get('needs_enabled',False)):
                raise RuntimeError('Enable Eat and drink when low on Supplies to verify the pending food/water use before restarting.')
            self.stats.start_timer(time.monotonic())
            self.overnight.start(self.stats,time.monotonic())
            if self.settings.get('outcome_ocr', False):
                # Adopt a visible challenge before waiting for OCR or toggling 3.
                existing = self.relocate_ring()
                if existing:
                    _,capture = existing
                self.monitor = OutcomeMonitor(self.hwnd, Capture, game_bounds,
                    lambda: user32.GetForegroundWindow() == self.hwnd and process_id(self.hwnd) == self.pid,
                    needs_enabled=self.settings.get('needs_enabled',False),
                    rod_slots=self.supplies.snapshot()['rod_slots'],capacity_enabled=self.settings.get('overnight_preflight',False) and self.settings.get('inventory_space_check',False))
                self.monitor.start()
                self.status('Starting local outcome recognition…')
                deadline = time.monotonic()+12
                while not existing and not self.monitor.initialized.is_set() and time.monotonic() < deadline:
                    self.delay(.02)
                if not existing and not self.monitor.initialized.is_set():
                    raise RuntimeError('Local OCR is still starting. No rod key was sent; press Z to retry.')
                if not existing and self.monitor.state.startswith('OCR unavailable'):self.ensure_recognition()
            while True:
                self.check()
                self.collect_notices()
                self.protect_overnight()
                if (not existing and self.monitor is not None and self.settings.get('outcome_ocr',False)
                        and self.monitor.inventory_visible is not True):
                    existing=self.relocate_ring()
                if self.journal:
                    self.journal.sync(self.stats)
                    reason=self.journal.target_reason(self.settings,self.stats)
                    if reason:raise RunComplete(reason)
                pending = None
                self.cycle_started = time.monotonic()
                self.cycle_id = self.stats.begin(self.cycle_started)
                if self.journal:self.journal.started(self.cycle_id,None)
                self.outcome = None
                self.rod_changed = False
                self.had_challenge = False
                already_waiting = (first_cycle and self.monitor is not None and
                                   getattr(self.monitor,'waiting_prompt',False) is True)
                if existing or already_waiting:
                    self.status('Fishing already active. Waiting for / matching the circle.')
                    self.supplies.equipped(self.cycle_started)
                else:
                    self.collect_notices()
                    if (self.supplies.rod_check_due() or (self.journal and not self.preflight_done) or
                            (self.monitor is not None and self.monitor.inventory_visible is True)):
                        try:self.inspect_inventory()
                        except ActiveChallenge as challenge:existing=challenge.ring
                    if self.supplies.rod is None:
                        raise RuntimeError('No enabled rod slot was confirmed usable. Check the enabled slots on Supplies and in inventory, then press Z.')
                    if existing is None:
                        try:self.maintain_supplies()
                        except ActiveChallenge as challenge:
                            existing=challenge.ring
                            self.status('Circle appeared while reading supplies. Matching it now.')
                    if existing is None:
                        if self.journal:
                            reason=self.journal.target_reason(self.settings,self.stats)
                            if reason:raise RunComplete(reason)
                        self.status(f'Running: equipping rod from slot {self.supplies.rod}; waiting for circle.')
                        self.key(self.supplies.rod)
                        self.supplies.equipped(time.monotonic())
                first_cycle = False
                if self.alerts:self.alerts.started(self.cycle_id,None if existing or already_waiting else self.supplies.rod)
                if self.journal:self.journal.started(self.cycle_id,self.supplies.rod)
                hits = 0
                pending = PressConfirmation()
                finished_while_waiting = False
                try:
                    if existing:
                        detector,capture = existing
                        existing = None
                    else:
                        detector, capture = self.find_ring()
                    self.stats.observe(self.cycle_id, challenge=time.monotonic())
                    self.had_challenge = True
                    gate = StageGate()
                    recognition_generation=self.recognition_generation
                    stage_started=time.monotonic()
                    absent_since = None
                    last_trace = geometry_checked = last_debug = last_relocate = -100
                    while True:
                        self.check()
                        now = time.monotonic()
                        self.collect_notices(active=True)
                        if recognition_generation!=self.recognition_generation:
                            recognition_generation=self.recognition_generation
                            gate.motion_marker=gate.motion_time=gate.previous_marker=gate.previous_time=None
                            gate.motion_speeds=[];gate.changed_since=gate.changed_target=gate.gap_since=None
                            gate.reset_since=gate.reset_origin=gate.reset_time=gate.reset_speed=None
                            gate.reset_seen=gate.gap_seen=False
                            pending.previous=None;pending.travel=0
                        if self.outcome in ('catch','failure'):
                            # Positive terminal evidence ends this attempt even
                            # if the server leaves the old circle visible.
                            if self.outcome=='catch' and pending.accept():self.stats.add(stages=1)
                            finished_while_waiting=True
                            break
                        if self.capture_bounds is not None and now - geometry_checked >= .25:
                            current = game_bounds(self.hwnd)
                            if current[2:] != self.capture_bounds[2:]:
                                raise RuntimeError('Game size changed. Press Z to find the circle again.')
                            if current != self.capture_bounds:
                                capture.x += current[0] - self.capture_bounds[0]
                                capture.y += current[1] - self.capture_bounds[1]
                                self.capture_bounds = current
                            geometry_checked = now
                        sampled_at = time.monotonic()
                        frame = capture.read()
                        target, marker = detector.detect(frame)
                        now = time.monotonic()
                        present = detector.is_present(frame)
                        self.recorder.observe(frame,getattr(detector,'size',0),sampled_at,cycle=self.cycle_id,
                            target=target,marker=marker,present=present,attempts=pending.attempts,
                            frame_processing_seconds=max(0,now-sampled_at),
                            last_press=gate.last_press if math.isfinite(gate.last_press) else None,
                            motion_degrees_per_second=statistics.median(gate.motion_speeds) if gate.motion_speeds else None)
                        if present:
                            absent_since = None
                        elif absent_since is None:
                            absent_since = now
                        self.stats.observe(self.cycle_id, absent=absent_since)
                        self.collect_rewards()
                        if (absent_since is not None and self.capture_bounds is not None
                                and now - absent_since >= .5 and now - last_relocate >= .5):
                            last_relocate = now
                            relocated = self.relocate_ring()
                            if relocated:
                                capture.close()
                                detector, capture = relocated
                                absent_since = None
                                gate.gap_since = None
                                gate.gap_seen = False
                                gate.previous_marker = gate.previous_time = None
                                pending.previous = None
                                self.stats.add(recoveries=1)
                                self.status('Recovered circle tracking. Resuming the current minigame…')
                                continue
                        if now - last_trace >= .25:
                            self.log('tracking', present=present, target=target, marker=marker,
                                     armed=gate.armed, inputs=hits, pending=pending.pending)
                            last_trace = now
                        if now - last_debug >= .05:
                            self.publish(mode='Verifying result' if not present else
                                         'Waiting for stage confirmation' if pending.pending else 'Tracking',
                                         present=present, target=target, marker=marker,
                                         pending=pending.pending, attempts=pending.attempts,
                                         center=(getattr(capture, 'x', 0) + getattr(detector, 'half', 0),
                                                 getattr(capture, 'y', 0) + getattr(detector, 'half', 0)),
                                         radius=getattr(detector, 'radius', 50))
                            last_debug = now
                        if absent_since is not None and now - absent_since > END_CONFIRM_SECONDS + self.settings['catch_delay']:
                            # Only a catch notification confirms the final stage.
                            if self.outcome == 'catch' and pending.accept():
                                self.stats.add(stages=1)
                            break
                        armed = gate.observe(sampled_at, target, marker, present)
                        if armed and gate.confirmation and self.outcome != 'failure' and pending.accept():
                            stage_started=now
                            self.stats.add(stages=1)
                            self.log('stage_confirmed', evidence=gate.confirmation)
                            self.status('Stage transition confirmed. Matching the next circle…')
                        pending.observe(marker if present else None)
                        if present and self.outcome is None and now-stage_started>=60:
                            raise RuntimeError('No confirmed circle-stage progress for 60 seconds. Check the minigame, then press Z.')
                        if self.outcome is None and press_stalled(pending,gate,now):
                            self.log('stage_stalled',target=target,marker=marker,attempts=pending.attempts,
                                     travel=pending.travel,last_press=gate.last_press,
                                     speeds=gate.motion_speeds,evidence=gate.confirmation)
                            raise RuntimeError('No stage change after two E attempts. Check the minigame, then press Z to restart.')
                        retry = (not armed and not gate.confirmation_pending() and pending.can_retry(now) and self.outcome is None)
                        if (armed or retry) and present and target and marker and self.outcome is None:
                            tolerance = max(2, min(10, target[1] * .20))
                            if angle_distance(target[0], marker[0]) <= tolerance:
                                if self.press_match(target,marker,retry,pending,gate,sampled_at=sampled_at):
                                    hits += 1
                                    self.status('E sent; waiting for a visible stage transition.' if not retry else
                                                'No stage transition after a full turn. Retried E once.')
                        self.stop.wait(POLL_SECONDS)
                except RetryCycle as error:
                    self.log('recovery', reason=str(error))
                    finished_while_waiting = True
                finally:
                    if capture:
                        capture.close()
                        capture = None
                result = self.outcome or 'unknown'
                if self.had_challenge and result in ('failure','unknown'):self.failure_snapshot(result)
                self.stats.end(self.cycle_id, time.monotonic(), pending_stage=pending.pending)
                if self.had_challenge:
                    self.supplies.completed_attempt()
                    if self.journal:self.journal.completed()
                if result != 'rod' and not (self.rod_changed and not self.had_challenge):
                    self.stats.finish(result, self.cycle_id)
                self.collect_rewards()
                if self.journal:self.journal.sync(self.stats)
                self.log('cycle_result',cycle=self.cycle_id,result=result,
                         catches=self.stats.snapshot()['catches'],unknown=self.stats.snapshot()['unknown'],
                         reward_scans=getattr(self.monitor,'reward_scans',None),
                         reward_cards=getattr(self.monitor,'reward_cards',None),
                         reward_events=getattr(self.monitor,'reward_events',None))
                if self.rod_changed:
                    if self.supplies.rod is None:
                        raise RuntimeError('No rod remains selected. Press Z to recheck the enabled rod slots in inventory.')
                    self.status(f'Rod unavailable. Switching to slot {self.supplies.rod}…')
                    self.delay(max(1,self.settings['catch_delay']))
                    self.stats.add(recoveries=1)
                    continue
                if result in ('failure', 'rod'):
                    self.status('Catch failed. Continuing after the normal delay…' if result=='failure' else 'Fishing interrupted. Waiting before re-equipping the rod…')
                    self.delay(max(1, self.settings['catch_delay']))
                    self.stats.add(recoveries=1)
                else:
                    self.status(f"{self.stats.snapshot()['last_result']}. Starting the next cycle…")
                    if finished_while_waiting:
                        self.delay(max(1, self.settings['catch_delay']))
        except RunComplete as error:
            self.stop.set()
            self.log('completed',reason=str(error))
            self.status('Completed: '+str(error))
            if self.alerts:self.alerts.complete(str(error),self.stats.snapshot())
        except InterruptedError as error:
            self.stop.set()
            self.log('paused', reason=str(error) or 'Stopped')
            self.status(str(error) or 'Paused.')
            if self.alerts and 'focus' in str(error).lower():self.alerts.pause(str(error))
            elif self.alerts and self.settings.get('discord_health',False):
                health=getattr(self.alerts,'health_stopped',None)
                if callable(health):health(str(error) or 'Paused by user.')
        except Exception as error:
            self.stop.set()
            self.log('error', reason=str(error),traceback=traceback.format_exc())
            self.status(f'Paused: {error}')
            if self.alerts:self.alerts.pause(str(error))
        finally:
            self.stats.pause_timer(time.monotonic())
            self.publish(mode='Paused', present=False)
            try:
                self.release_key()
            except OSError as error:
                self.status(f'Paused: {error}')
                self.log('key_release_error', reason=str(error))
            finally:
                if self.cycle_id is not None:
                    self.stats.end(self.cycle_id,time.monotonic(),
                                   pending_stage=bool(pending and pending.pending))
                try:
                    try:
                        try:
                            if existing and existing[1] is not capture:existing[1].close()
                        finally:
                            if capture: capture.close()
                    finally:
                        if self.monitor:
                            try: self.monitor.close()
                            except Exception as error: self.log('ocr_close_error', reason=str(error))
                            # Process replies already queued before stop. Confirmed
                            # results still count when Z interrupts the end delay.
                            if self.cycle_id is not None:
                                try: self.collect_notices()
                                except Exception as error: self.log('final_notice_error', reason=str(error))
                finally:
                    if self.cycle_id is not None:
                        self.stats.end(self.cycle_id, time.monotonic())
                    if self.alerts:self.alerts.flush(force=True)
                    if self.journal:self.journal.sync(self.stats)
                    self.recorder.close()
                    self.stop.set()


def draw_tracking(canvas, cx, cy, radius, target, marker, overlay=False):
    canvas.delete('all')
    ring = radius * (1.28 if overlay else 1)
    canvas.create_oval(cx-ring, cy-ring, cx+ring, cy+ring, outline='#35465d', width=2)
    if target:
        rr = radius * (1.34 if overlay else 1)
        # Canvas angles run counterclockwise, detector angles run clockwise.
        canvas.create_arc(cx-rr, cy-rr, cx+rr, cy+rr, start=-(target[0]+target[1]/2),
                          extent=target[1], style='arc', outline='#55b7ef', width=5)
        tolerance = max(2, min(10, target[1] * .20))
        rr = radius * (1.53 if overlay else .8)
        canvas.create_arc(cx-rr, cy-rr, cx+rr, cy+rr, start=-(target[0]+tolerance),
                          extent=tolerance*2, style='arc', outline='#77ddba', width=4)
    if marker:
        angle = math.radians(marker[0])
        rr = radius * (1.43 if overlay else 1.05)
        x, y = cx+rr*math.cos(angle), cy+rr*math.sin(angle)
        canvas.create_oval(x-4, y-4, x+4, y+4, fill='#edf3fa', outline='')
    if not overlay:
        canvas.create_text(cx, cy, text='E', fill='#91a3bb', font=('Segoe UI', 16, 'bold'))


class TrackingOverlay:
    """Passive visual marks outside the sampled ring; no input or focus."""
    def __init__(self, root):
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg='#010203')
        self.window.wm_attributes('-transparentcolor', '#010203')
        self.canvas = tk.Canvas(self.window, bg='#010203', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.window.update_idletasks()
        user32.GetParent.argtypes = [w.HWND]
        user32.GetParent.restype = w.HWND
        self.hwnd = user32.GetParent(self.window.winfo_id()) or self.window.winfo_id()
        get_long = user32.GetWindowLongPtrW
        set_long = user32.SetWindowLongPtrW
        get_long.argtypes, get_long.restype = [w.HWND, ctypes.c_int], ctypes.c_ssize_t
        set_long.argtypes, set_long.restype = [w.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
        set_long(self.hwnd, -20, get_long(self.hwnd, -20) | 0x08000000 | 0x20 | 0x80)
        user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, w.UINT]
        user32.SetWindowPos.restype = w.BOOL

    def hide(self):
        self.window.withdraw()

    def show(self, data):
        radius = data['radius']
        half = math.ceil(radius * 1.7)
        center = data['center']
        size = half * 2 + 1
        # Negative Tk geometry offsets anchor to the right/bottom monitor edge.
        # Windows coordinates are absolute, including monitors left of primary.
        self.window.geometry(f'{size}x{size}')
        draw_tracking(self.canvas, half, half, radius, data.get('target'), data.get('marker'), True)
        self.window.deiconify()
        if not user32.SetWindowPos(self.hwnd, ctypes.c_void_p(-1).value,
                                  round(center[0]-half), round(center[1]-half), size, size, 0x10 | 0x40):
            raise OSError('Could not position the tracking overlay.')


class App:
    def __init__(self, resume_stats=None,session_path=None):
        self.root = tk.Tk()
        self.root.title('Fishing helper')
        self.root.geometry('760x600')
        self.root.resizable(True, True)
        self.root.configure(bg='#0c1422')
        self.path = settings_path()
        self.settings = load_settings(self.path)
        self.session_store=SessionStore(session_path) if session_path is not None else None
        saved=self.session_store.load() if self.session_store else {}
        saved_stats=saved.get('stats',{}) if isinstance(saved.get('stats',{}),dict) else {}
        self.journal=RunJournal(saved.get('journal'))
        self.messages, self.worker = queue.Queue(maxsize=128), None
        if any(type(saved_stats.get(key,0)) is not int or not 0<=saved_stats.get(key,0)<=1e9
               for key in ('catches','failures','unknown','cycles','inputs','stages','retries','recoveries')):
            saved={};saved_stats={};self.journal=RunJournal()
        try:self.stats = SessionStats(resume_stats if resume_stats is not None else saved_stats or None)
        except ValueError:
            self.stats=SessionStats();self.journal=RunJournal();saved={};saved_stats={}
        for key in ('elapsed','next_attempt'):
            value=saved_stats.get(key,0)
            if type(value) is int and 0<=value<=1e9:
                if key=='elapsed':self.stats.active_seconds=value
                else:self.stats.next_attempt=value
        self.stats.next_attempt=max(self.stats.next_attempt,max(self.journal.entries,default=0))
        saved_age=saved_stats.get('last_catch_age')
        saved_at=saved.get('saved_at')
        if (self.stats.values['catches'] and type(saved_age) in (int,float) and math.isfinite(saved_age) and saved_age>=0
                and type(saved_at) in (int,float) and math.isfinite(saved_at)):
            self.stats.last_catch_at=time.monotonic()-saved_age-max(0,time.time()-saved_at)
        self.notifier=DiscordNotifier()
        self.remote=DiscordControls();self.remote.configure(self.settings)
        self.updater=UpdateManager(APP_VERSION,Path(sys.executable).parent if getattr(sys,'frozen',False) else ASSET_DIR,data_dir()/'updates')
        self.updating=False
        self.notifier.configure(self.settings)
        self.notifier.restore_live_card(saved.get('discord'))
        self.alerts=FishingAlerts(self.stats,self.notifier)
        self.alerts.journal=self.journal
        self.alerts.amounts={kind:(0,value['count'],True) for kind,value in self.journal.stock.items()}
        self.supplies = SupplySession(self.settings['rod_slots'])
        self.supplies.restore(saved.get('supplies'))
        self.next_session_save=0
        self.next_history_update=0
        self.history_revision=-1
        self.telemetry = queue.Queue(maxsize=1)
        self.latest_debug = {}
        self.overlay = None
        self.overlay_enabled = tk.BooleanVar(master=self.root,value=self.settings['overlay_enabled'])
        self.stats_cache = None
        self.debug_cache = None
        self.overlay_frame = None
        self.pending_start = False
        self.start_after = None
        self.closing = False
        self.ui_state_cache = None
        self.save_after = None
        self.settings_dirty = False
        self.countdown_until = None
        self.last_needs = {}
        self.previous_keys = {key for key in (TOGGLE_KEY, STOP_KEY) if user32.GetAsyncKeyState(key) & 0x8000}
        self.status = tk.StringVar(master=self.root,value='Focus FiveM and press Z to begin.')
        self.build_ui()
        for variable in (*self.target_vars.values(),self.require_supplies,self.overnight_safety):variable.trace_add('write',self.queue_settings_save)
        for variable in (*self.reliability_vars.values(),self.overnight_preflight,self.inventory_space_check,self.inventory_reserve,self.health_minutes):variable.trace_add('write',self.queue_settings_save)
        self.learn_rates.trace_add('write',self.calculator_changed)
        for variable in self.calculator_vars.values():
            variable.trace_add('write',self.calculator_changed)
        for variable in (self.needs_enabled,self.hunger_threshold,self.thirst_threshold,
                         self.item_wait,self.catch_delay,self.overlay_enabled,self.wait_limit,
                         *self.rod_slot_vars.values()):
            variable.trace_add('write',self.queue_settings_save)
        for variable in (*self.discord_vars.values(),self.discord_url):
            variable.trace_add('write',self.queue_settings_save)
        for variable in self.remote_vars.values():variable.trace_add('write',self.queue_settings_save)
        for variable in (self.update_feed,self.update_startup,self.update_channel,self.update_when_idle,self.update_require_signed,self.update_publisher):variable.trace_add('write',self.queue_settings_save)
        self.status.trace_add('write', self.refresh_ui)
        self.refresh_ui()
        if saved:self.status.set('Paused. Saved session restored; press Z in FiveM to resume.')
        elif self.session_store and self.session_store.error:self.status.set(self.session_store.error)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_after = self.root.after(25, self.poll)
        if self.settings.get('remote_enabled',False):self.remote.connect()
        if self.settings.get('update_check_startup',True):self.root.after(1500,lambda:self.check_updates(manual=False))

    def build_ui(self):
        build_dashboard(self,APP_VERSION,draw_tracking)

    def resize_viewport(self, event):
        self.viewport.itemconfigure(self.content_window,width=event.width)
        self.resize_content()

    def resize_content(self, *_):
        for label in getattr(self,'ui_wrap_labels',[]):
            width=label.master.winfo_width()
            if width>1:
                wrap=max(120,width-36)
                if int(label.cget('wraplength'))!=wrap:label.configure(wraplength=wrap)
        self.viewport.configure(scrollregion=self.viewport.bbox('all'))
        overflow=self.content.winfo_reqheight()>self.viewport.winfo_height()+2
        if overflow and not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side='right',fill='y')
        elif not overflow and self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()

    def scroll_dashboard(self, event):
        if isinstance(event.widget,ttk.Treeview):return
        if self.content.winfo_reqheight()>self.viewport.winfo_height():
            self.viewport.yview_scroll(-int(event.delta/120),'units')

    def fit_dashboard(self):
        self.root.update_idletasks()
        if not getattr(self,'layout_fitted',False):
            height=min(940,max(420,self.root.winfo_screenheight()-100))
            width=min(980,max(760,self.root.winfo_screenwidth()-100))
            self.root.minsize(760,min(580,height))
            self.root.geometry(f'{width}x{height}')
            self.layout_fitted=True
        self.root.update_idletasks()
        self.resize_content()

    def show_page(self,name):
        if name not in self.sections:raise ValueError('Unknown app page.')
        for key,panel in self.sections.items():
            panel.pack_forget()
            self.section_open[key]=key==name
            self.page_buttons[key].configure(style='SelectedNav.TButton' if key==name else 'Nav.TButton')
        self.current_page=name
        self.sections[name].pack(fill='x')
        self.page_title.set(self.page_titles[name][0]);self.page_subtitle.set(self.page_titles[name][1])
        self.viewport.yview_moveto(0)
        self.root.update_idletasks();self.resize_content()

    def toggle_section(self, name):
        self.show_page('overview' if self.current_page==name else name)

    def update_calculator(self):
        try:
            values=calculator_settings({key:var.get() for key,var in self.calculator_vars.items()})
            learned=self.journal.rates() if self.learn_rates.get() else {}
            values.update(learned)
            result=estimate_run(values)
        except (ValueError,TypeError,OverflowError) as error:
            for variable in self.calculator_results.values():variable.set('—')
            self.calculator_detail.set(str(error))
            return False
        self.calculator_results['time'].set(estimate_time(result['minutes']))
        self.calculator_results['food'].set(str(result['pack_food']))
        self.calculator_results['water'].set(str(result['pack_water']))
        self.calculator_detail.set(f"Estimated use: {result['food']} food · {result['water']} water. "
            f"Pack totals above include {values['calc_reserve']:g}% extra supplies.\n"+
            ('Learned: '+', '.join(key.replace('calc_','').replace('_',' ')+f' {value:.2f}' for key,value in learned.items())
             if learned else 'Using starting rates; measured rates appear after enough fishing data.'))
        return True

    def calculator_changed(self, *_):
        if self.update_calculator():self.queue_settings_save()

    def read_ui_settings(self):
        try:calculator=calculator_settings({key:var.get() for key,var in self.calculator_vars.items()})
        except (ValueError,TypeError,OverflowError):
            # An incomplete planner entry must not block fishing or saving its
            # safety settings. Keep the last valid saved planner values.
            calculator={key:self.settings.get(key,default) for key,default in CALCULATOR_DEFAULTS.items()}
        discord={key:var.get() for key,var in self.discord_vars.items()}
        discord['discord_url']=webhook_url(self.discord_url.get())
        if discord['discord_enabled'] and not discord['discord_url']:
            raise ValueError('Paste a Discord webhook URL before enabling notifications.')
        return dict(catch_delay=parse_delay(self.catch_delay.get()),needs_enabled=self.needs_enabled.get(),
            hunger_threshold=parse_threshold(self.hunger_threshold.get()),
            thirst_threshold=parse_threshold(self.thirst_threshold.get()),
            item_wait=parse_item_wait(self.item_wait.get()),overlay_enabled=self.overlay_enabled.get(),
            wait_limit=parse_wait_limit(self.wait_limit.get()),
            rod_slots=parse_rod_slots([slot for slot,var in self.rod_slot_vars.items() if var.get()]),
            **discord,**calculator,**{key:parse_target(key,var.get()) for key,var in self.target_vars.items()},
            require_supplies=self.require_supplies.get(),learn_rates=self.learn_rates.get(),
            overnight_safety=self.overnight_safety.get(),overnight_preflight=self.overnight_preflight.get(),inventory_space_check=self.inventory_space_check.get(),
            inventory_reserve_kg=parse_reserve(self.inventory_reserve.get()),health_minutes=parse_health_minutes(self.health_minutes.get()),
            **{key:var.get() for key,var in self.reliability_vars.items()},
            **remote_settings({key:var.get() for key,var in self.remote_vars.items()}),
            **update_preferences(dict(update_feed_url=self.update_feed.get(),update_check_startup=self.update_startup.get(),
                update_channel=self.update_channel.get().lower(),update_when_idle=self.update_when_idle.get(),
                update_require_signed=self.update_require_signed.get(),update_publisher=self.update_publisher.get(),
                update_skip=self.settings.get('update_skip',''),update_remind_after=self.settings.get('update_remind_after',0))))

    def queue_settings_save(self, *_):
        if self.closing:return
        self.settings_dirty=True
        if self.save_after is not None:self.root.after_cancel(self.save_after)
        self.save_after=self.root.after(400,self.persist_settings)

    def persist_settings(self):
        if self.save_after is not None:self.root.after_cancel(self.save_after)
        self.save_after=None
        try:
            settings=self.read_ui_settings()
        except (ValueError,TypeError,OverflowError) as error:
            self.settings_feedback.set(str(error))
            return False
        self.settings=settings
        self.notifier.configure(settings)
        self.remote.configure(settings)
        if (not (self.worker and not self.worker.stop.is_set()) and
                not (getattr(self,'thread',None) and self.thread.is_alive())):
            self.supplies.configure_rod_slots(settings['rod_slots'])
        try:
            save_settings(self.path,settings)
        except OSError as error:
            self.settings_feedback.set('Could not save settings. Current values still work for this run.')
            LOGGER.info(json.dumps({'event':'settings_not_saved','reason':str(error)}))
            return False
        self.settings_dirty=False
        self.settings_feedback.set('Saved automatically. Rod order: '+' → '.join(settings['rod_slots'])+'.')
        return True

    def test_discord(self):
        try:
            settings={**self.settings,**{key:var.get() for key,var in self.discord_vars.items()},
                      'discord_url':webhook_url(self.discord_url.get())}
            if not settings['discord_url']:raise ValueError('Paste your Discord webhook URL first.')
            self.notifier.configure(settings)
            self.notifier.test()
            self.discord_status.set(self.notifier.snapshot())
        except (ValueError,TypeError,OSError):
            with self.notifier.condition:
                self.notifier.status='Paste a valid Discord webhook URL, then test again.'
            self.discord_status.set(self.notifier.snapshot())

    def reveal_discord(self):
        self.discord_entry.configure(show='' if self.discord_show.get() else '•')

    def toggle_overlay(self):
        if not self.overlay_enabled.get() and self.overlay:
            self.overlay.hide()

    def refill_rods(self):
        if self.worker is not None and not self.worker.stop.is_set(): return
        if getattr(self,'thread',None) is not None and self.thread.is_alive(): return
        try:self.supplies.configure_rod_slots(self.read_ui_settings()['rod_slots'])
        except (ValueError,TypeError,OverflowError) as error:
            self.status.set(f'Paused: {error}')
            return
        self.supplies.reset(preserve_supplies=True)
        self.status.set('Enabled rod slots reset to '+' → '.join(self.supplies.rod_slots)+'. Focus FiveM and press Z.')

    def update_tracking(self):
        now=time.monotonic()
        self.journal.sync(self.stats)
        if self.worker is not None:
            self.journal.record_capacity(getattr(getattr(self.worker,'monitor',None),'inventory_capacity',None))
            recorder=getattr(self.worker,'recorder',None)
            if isinstance(recorder,FailureRecorder):
                self.snapshot_feedback.set(recorder.error or ('Latest snapshot: '+Path(recorder.last_path).name if recorder.last_path else
                    'Failure snapshots save locally; images are never sent to Discord.'))
        if now>=self.next_history_update:
            self.next_history_update=now+1
            self.update_calculator()
            progress,percent=self.journal.progress(self.settings,self.stats)
            self.progress_text.set(progress.replace(' while fishing runs',''));self.progress_bar.configure(value=percent)
            detail,short,unknown=self.journal.preflight(self.settings,self.supplies.snapshot())
            if self.settings.get('overnight_preflight',False):detail,short,unknown=self.journal.overnight_check(self.settings,self.supplies.snapshot(),now)
            self.preflight_text.set(('Supplies need attention: ' if short else 'Supply check pending: ' if unknown else 'Supplies ready: ')+detail)
            self.update_history()
            with self.journal.lock:stock={kind:dict(value) for kind,value in self.journal.stock.items()}
            for kind,var in self.stock_vars.items():
                entry=stock.get(kind)
                value=f'{entry["count"]:,}'+(' (estimated)' if entry['estimated'] else ' (last check)') if entry else '—'
                if var.get()!=value:var.set(value)
        if self.session_store and now>=self.next_session_save:
            self.next_session_save=now+5
            self.save_session()
        if self.session_store:self.session_feedback.set(self.session_store.error or 'Session saves automatically every 5 seconds. Restarting restores it paused.')
        self.storage_summary.set((self.session_store.error or 'Session saved locally') if self.session_store else 'Session in memory')
        with self.notifier.condition:
            summary='Discord: '+self.notifier.status if self.notifier.settings['discord_enabled'] else 'Discord notifications off'
        if self.discord_summary.get()!=summary:self.discord_summary.set(summary)
        recognition=self.stats.snapshot()
        discord_status=(self.notifier.snapshot()+f"\nCatch recognition: {recognition['catches']} confirmed · "
                        f"{recognition['unknown']} unconfirmed. Only confirmed catches update Discord.")
        if self.discord_status.get()!=discord_status:self.discord_status.set(discord_status)
        snapshot=self.stats.snapshot(now)
        if snapshot!=self.stats_cache:
            for key,var in self.stat_vars.items():var.set(str(snapshot[key]))
            self.active_time_var.set(duration(snapshot['elapsed']))
            self.failure_caption.set(f"{snapshot['failures']} confirmed failures")
            self.stats_detail.set(f"{snapshot['inputs']} inputs · {snapshot['retries']} retries · "
                f"{snapshot['recoveries']} recoveries · Success rate uses confirmed results")
            detail=f"{snapshot['catches_per_hour']} catches/hr · Last catch {age(snapshot['last_catch_age'])}"
            if self.session_detail.get()!=detail:self.session_detail.set(detail)
            self.stats_cache=snapshot
        while not self.telemetry.empty():self.latest_debug=self.telemetry.get_nowait()
        data=self.latest_debug
        supplies=self.supplies.snapshot()
        for slot,var in self.rod_availability_vars.items():
            state=('Not enabled' if slot not in supplies['rod_slots'] else 'Check pending' if not supplies['rod_checked']
                   else 'Unavailable' if slot in supplies['broken'] else 'Rod confirmed')
            if var.get()!=state:var.set(state)
        slot1=self.rod_availability_vars['1'].get() if '1' in supplies['rod_slots'] else self.stock_vars['bait'].get()
        if self.slot1_contents.get()!=slot1:self.slot1_contents.set(slot1)
        rod='Slot '+supplies['rod'] if supplies['rod'] else 'Unavailable'
        if self.rod_text.get()!=rod:self.rod_text.set(rod)
        progress=('Next inventory check: startup' if not supplies['rod_checked'] else
                  f"Next inventory check: {min(4,supplies['rod_check_attempts'])}/4 attempts")
        progress='Enabled rods: '+' → '.join(supplies['rod_slots'])+' · '+progress
        if self.rod_check_text.get()!=progress:self.rod_check_text.set(progress)
        needs=data.get('needs',{})
        if isinstance(needs,dict) and 'needs' in data:self.last_needs=dict(needs)
        for kind,var in self.need_vars.items():
            reading=self.last_needs.get(kind)
            value=reading[1] if reading and 0<=now-reading[0]<=NEED_MAX_AGE else None
            text=f'{value:g}%' if value is not None else '—'
            if var.get()!=text:
                var.set(text)
                self.need_bars[kind].configure(value=value if value is not None else 0)
        target,marker=data.get('target'),data.get('marker')
        if data!=self.debug_cache:
            draw_tracking(self.debug_canvas,72,68,47,target,marker)
            angles=(f"Target {target[0]:.0f}° · Marker {marker[0]:.0f}° · Offset {angle_distance(target[0],marker[0]):.0f}°"
                    if target and marker else 'Blue target · White marker · Green press zone')
            self.debug_text.set(f"{data.get('mode','Waiting for the next run.')}\n{angles}\n{data.get('ocr','Recognition starts with fishing')}")
            self.debug_cache=dict(data)
        running=self.worker is not None and not self.worker.stop.is_set()
        visible=bool(running and data.get('present'))
        if visible!=self.tracking_visible:
            self.tracking_visible=visible
            if visible:self.tracking_card.pack(fill='x',before=self.diagnostic_summary,pady=(0,12))
            else:self.tracking_card.pack_forget()
            self.fit_dashboard()
        if self.overlay_enabled.get() and running and data.get('present') and 'center' in data:
            if user32.GetForegroundWindow()==self.worker.hwnd:
                if self.overlay is None:self.overlay=TrackingOverlay(self.root)
                if data!=self.overlay_frame:
                    self.overlay.show(data);self.overlay_frame=dict(data)
            elif self.overlay:
                self.overlay.hide();self.overlay_frame=None
        elif self.overlay:
            self.overlay.hide();self.overlay_frame=None

    def refresh_ui(self, *_):
        text=self.status.get()
        running=self.worker is not None and not self.worker.stop.is_set()
        busy=bool(not self.closing and getattr(self,'thread',None) and self.thread.is_alive() and not running)
        reached=bool(self.journal.target_reason(self.settings,self.stats))
        title,hint=phase(text,running)
        if running and self.latest_debug.get('present'):title='Matching circle'
        countdown=max(1,math.ceil((self.countdown_until or time.monotonic())-time.monotonic()))
        color='#5bd1e7'
        if self.closing:title,hint,color='Stopping','Releasing keys and closing recognition…','#efc879'
        elif self.pending_start:title,hint,color='Switch to FiveM','', '#efc879'
        elif running:color='#77ddba'
        elif text.lower().startswith('paused:'):color='#efc879'
        elif text.lower().startswith('paused'):color='#a7b9d0'
        if self.phase_text.get()!=title:self.phase_text.set(title)
        if self.recovery_text.get()!=hint:
            self.recovery_text.set(hint)
            notice=bool(hint and (text.lower().startswith(('paused:','completed:')) or 'saved session restored' in text.lower()))
            if notice and not self.recovery_label.winfo_manager():self.recovery_label.pack(fill='x',before=self.scroll_host,pady=(0,12))
            elif not notice and self.recovery_label.winfo_manager():self.recovery_label.pack_forget()
            self.fit_dashboard()
        state=title,color,running,self.pending_start,self.closing,busy,countdown,reached
        self.run_title.set(title if running or self.pending_start or text.lower().startswith(('paused:','completed:')) else 'Ready for your next catch')
        self.run_detail.set('Switch to FiveM to begin.' if self.pending_start else
                            'Watch the game; press Z to pause.' if running else 'Focus FiveM, then press Z to start.')
        if state!=self.ui_state_cache:
            self.badge.configure(fg=color)
            disabled=running or self.pending_start or self.closing or busy
            caption=(f'Starting in {countdown}s' if self.pending_start else 'Fishing in progress' if running
                     else 'Target reached' if reached else 'Retry fishing' if text.lower().startswith('paused:') else 'Start fishing')
            self.start_button.configure(state='disabled' if disabled or reached else 'normal',text=caption)
            self.pause_button.configure(state='normal' if running or self.pending_start else 'disabled',
                                        text='Cancel' if self.pending_start else 'Pause')
            if running or self.pending_start:
                if not self.pause_button.winfo_manager():self.pause_button.pack(side='right',before=self.start_button,padx=(8,0))
            else:self.pause_button.pack_forget()
            self.timing_entry.configure(state='disabled' if disabled else 'normal')
            self.needs_check.configure(state='disabled' if disabled else 'normal')
            self.refill_button.configure(state='disabled' if disabled else 'normal')
            self.reset_session_button.configure(state='disabled' if disabled else 'normal')
            for control in self.run_controls:control.configure(state='disabled' if disabled else 'normal')
            for checkbox in self.rod_slot_checks:checkbox.configure(state='disabled' if disabled else 'normal')
            for control in self.discord_controls:control.configure(state='disabled' if disabled else 'normal')
            for entry in self.supply_entries:entry.configure(state='disabled' if disabled else 'normal')
            self.ui_state_cache=state

    def reset_session(self):
        if (self.closing or self.pending_start or
                (self.worker is not None and not self.worker.stop.is_set()) or
                (getattr(self,'thread',None) and self.thread.is_alive())):
            return False
        if self.settings_dirty:self.persist_settings()
        self.stats.reset()
        self.journal.reset()
        self.history_revision=-1;self.next_history_update=0
        queued=self.alerts.reset_session()
        self.worker=None
        while not self.messages.empty():self.messages.get_nowait()
        while not self.telemetry.empty():self.telemetry.get_nowait()
        self.latest_debug={}
        self.stats_cache=None
        if self.overlay:self.overlay.hide()
        self.status.set('Session reset. '+('Discord card update queued.' if queued else
                        'Discord card was not queued; check notification settings and status.'))
        self.update_tracking()
        self.save_session()
        LOGGER.info(json.dumps({'event':'session_reset','discord_queued':queued}))
        return True

    def pause(self):
        self.remote.reject_pending('Fishing was paused. Send a fresh command if still needed.')
        if self.start_after is not None:
            self.root.after_cancel(self.start_after)
            self.start_after = None
        self.pending_start = False
        self.countdown_until = None
        self.stats.pause_timer(time.monotonic())
        if self.worker:
            self.worker.stop.set()
        while not self.messages.empty():
            self.messages.get_nowait()
        self.status.set('Paused.')
        self.latest_debug = {}
        if self.overlay: self.overlay.hide()
        while not self.telemetry.empty():
            self.telemetry.get_nowait()

    def schedule_start(self):
        if self.closing or self.updating:
            return
        self.pause()
        self.pending_start = True
        self.countdown_until = time.monotonic()+3
        self.status.set('Switch to FiveM to begin.')
        self.start_after = self.root.after(3000, self.delayed_start)

    def delayed_start(self):
        self.start_after = None
        if self.pending_start:
            self.pending_start = False
            self.start()

    def start(self):
        if self.closing or self.updating:
            return
        if self.pending_start:
            self.pause()
            return
        if self.worker and not self.worker.stop.is_set():
            self.pause()
            return
        if self.worker and getattr(self, 'thread', None) and self.thread.is_alive():
            self.status.set('Previous run is stopping. Press Z again in a moment.')
            return
        if not is_game(user32.GetForegroundWindow()):
            self.status.set('Focus the FiveM / GTA window, then press Z.')
            return
        try:
            self.settings=self.read_ui_settings()
            if self.supplies.snapshot()['pending_uses'] and not self.settings['needs_enabled']:
                raise ValueError('Enable Eat and drink when low to verify the pending food/water use before restarting.')
        except (ValueError,TypeError,OverflowError) as error:
            self.status.set(f'Paused: Correct Settings: {error}')
            return
        self.persist_settings()
        reason=self.journal.target_reason(self.settings,self.stats)
        if reason:
            self.status.set('Completed: '+reason+' Reset the session or raise the target.');return
        # Each run gets its own queue so late messages from an older run cannot
        # overwrite this run's status.
        self.messages = queue.Queue(maxsize=128)
        self.telemetry = queue.Queue(maxsize=1)
        self.latest_debug = {}
        self.last_needs = {}
        self.worker = FishingWorker({**self.settings, 'debug': True, 'outcome_ocr': True,
                                      '_stats': self.stats, '_telemetry': self.telemetry,
                                      'rod_backups':True,'_supplies':self.supplies,'_alerts':self.alerts,'_journal':self.journal}, self.messages)
        self.thread = threading.Thread(target=self.worker.run, daemon=False)
        try:
            self.thread.start()
        except RuntimeError as error:
            self.worker.stop.set()
            self.status.set(f'Paused: could not start fishing: {error}')
        self.refresh_ui()

    def poll(self):
        self.poll_after = None
        if self.closing:
            return
        try:
            self.poll_controls()
            self.health_poll_passes=getattr(self,'health_poll_passes',0)+1;self.health_last_poll=time.monotonic()
        except Exception as error:
            self.pause()
            self.status.set(f'Paused: control update failed: {error}')
            self.health_control_failed=True
            LOGGER.exception('control_update_failed')
        finally:
            if not self.closing:
                self.poll_after = self.root.after(25, self.poll)

    def poll_controls(self):
        keys = {key for key in (TOGGLE_KEY, STOP_KEY) if user32.GetAsyncKeyState(key) & 0x8000}
        fresh = keys - self.previous_keys
        self.previous_keys = keys
        if STOP_KEY in keys:
            if STOP_KEY in fresh:
                self.pause()
        elif TOGGLE_KEY in fresh:
            self.start()
        if fresh:self.remote.reject_pending('Local controls were used. Send a fresh Discord command if still needed.')
        self.remote.drain(self.remote_action)
        self.poll_updates()
        if self.closing:return
        remote_state=self.remote.snapshot()
        if self.remote_status.get()!=remote_state:self.remote_status.set(remote_state)
        while not self.messages.empty():
            message = self.messages.get_nowait()
            if self.worker is None or not self.worker.stop.is_set() or message.lower().startswith(('paused','completed:')):
                self.status.set(message)
        self.refresh_ui()
        try:
            self.update_tracking()
        except (tk.TclError, OSError) as error:
            self.overlay_enabled.set(False)
            if self.overlay:
                try: self.overlay.hide()
                except tk.TclError: pass
            self.overlay_frame = None
            self.debug_text.set(f'Tracking display unavailable: {error}')
            LOGGER.info(json.dumps({'event': 'tracking_display_error', 'reason': str(error)}))

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.remote.close()
        self.updater.close()
        if self.save_after is not None:
            self.root.after_cancel(self.save_after)
            self.save_after=None
        if self.settings_dirty:self.persist_settings()
        if self.poll_after is not None:
            self.root.after_cancel(self.poll_after)
            self.poll_after = None
        self.pause()
        self.status.set('Stopping and releasing keys…')
        self.finish_close()

    def finish_close(self):
        if getattr(self, 'thread', None) is not None and self.thread.is_alive():
            self.root.after(25, self.finish_close)
            return
        if self.remote.thread and self.remote.thread.is_alive():
            self.root.after(50,self.finish_close);return
        self.notifier.close()
        if self.notifier.thread and self.notifier.thread.is_alive():
            self.root.after(50,self.finish_close);return
        self.save_session()
        if self.session_store:self.session_store.close()
        self.root.destroy()

    def save_session(self):
        if not self.session_store:return
        with self.stats.lock:
            self.journal.sync(self.stats)
            stats=self.stats.snapshot();stats['next_attempt']=self.stats.next_attempt
            journal=self.journal.export_state()
        self.session_store.submit(dict(format=1,saved_at=time.time(),stats=stats,journal=journal,
                                       supplies=self.supplies.snapshot(),discord=self.notifier.saved_live_card()))

    def update_history(self):
        if self.history_revision==self.journal.revision:return
        self.history_revision=self.journal.revision
        rows=self.journal.history()
        last=next((row for row in reversed(rows) if row['result']=='catch'),None)
        if last:
            names=' · '.join((f'{count} × ' if count is not None else '')+name for name,count in last['items'].items())
            self.last_catch_text.set(names or 'Item unreadable')
            self.last_catch_details.set(f'Rod slot {last["rod"]} · '+last['time'].replace('T',' ')[:19]+' UTC')
        else:
            self.last_catch_text.set('No catches yet');self.last_catch_details.set('')
        self.history_tree.delete(*self.history_tree.get_children())
        totals={}
        for row in rows:
            if row['result']=='catch':
                for name,count in row['items'].items():
                    if count is not None:totals[name]=totals.get(name,0)+count
        for row in reversed(rows[-200:]):
            items=' · '.join((f'{count}× ' if count is not None else '')+name for name,count in row['items'].items()) or 'Item unreadable'
            self.history_tree.insert('', 'end',values=(row['time'].replace('T',' ')[:19],row['result'],row['rod'],items))
        self.history_summary.set(('Known item totals: '+', '.join(f'{count}× {name}' for name,count in sorted(totals.items()))
                                 if totals else 'No item quantities confirmed yet.')+'\nShowing the latest 200 results. Export includes up to 10,000 stored results; timestamps are UTC.')

    def export_catches(self,path=None):
        self.journal.sync(self.stats)
        if path is None:path=filedialog.asksaveasfilename(parent=self.root,title='Export catch history',
            defaultextension='.csv',initialfile='Fishing catches.csv',filetypes=[('CSV','*.csv')])
        if not path:return False
        try:export_history(path,self.journal)
        except OSError as error:self.export_feedback.set('Export failed: '+str(error));return False
        self.export_feedback.set('Catch history exported.');return True

    def export_diagnostics(self,path=None):
        if path is None:path=filedialog.asksaveasfilename(parent=self.root,title='Export troubleshooting report',
            defaultextension='.zip',initialfile='Fishing troubleshooting.zip',filetypes=[('ZIP','*.zip')])
        if not path:return False
        try:export_support(path,APP_VERSION,self.settings,self.stats,self.journal,self.notifier,data_dir()/'fishing-debug.log')
        except OSError as error:self.export_feedback.set('Export failed: '+str(error));return False
        self.export_feedback.set('Troubleshooting report exported; webhook secrets removed.');return True

    def open_snapshots(self):
        import os
        folder=data_dir()/'failure-snapshots'
        try:folder.mkdir(parents=True,exist_ok=True);os.startfile(str(folder))
        except OSError:self.snapshot_feedback.set('Could not open the failure snapshot folder.')

    def connect_remote(self):
        if self.persist_settings():
            if not self.settings.get('remote_enabled',False):self.remote_status.set('Enable Discord controls and fill the bot fields first.');return
            self.remote.connect();self.remote_status.set(self.remote.snapshot())

    def show_updates(self):
        self.show_page('settings')
        _,_,button,opened=self.detail_groups['advanced']
        if not opened.get():button.invoke()
        self.root.after_idle(lambda:self.viewport.yview_moveto(1))

    def acknowledge_update(self,path,token):
        if (self.closing or getattr(self,'health_control_failed',False) or getattr(self,'health_poll_passes',0)<10
                or time.monotonic()-getattr(self,'health_last_poll',0)>1):return False
        acknowledge_startup(path,token,APP_VERSION);return True

    def check_updates(self,manual=True):
        if self.closing or self.updating:return
        if manual:
            self.settings['update_skip']='';self.settings['update_remind_after']=0;self.queue_settings_save()
        try:self.updater.check(channel_feed(self.update_feed.get(),self.update_channel.get().lower()),self.update_channel.get().lower())
        except ValueError as error:self.update_feedback.set(str(error))

    def update_busy(self):
        return bool(self.pending_start or (self.worker and not self.worker.stop.is_set()) or (getattr(self,'thread',None) and self.thread.is_alive()))

    def download_update(self):
        if self.closing or self.updating:return
        self.updater.require_signed=self.update_require_signed.get()
        self.updater.publisher=self.update_publisher.get().strip().upper().replace(' ','')
        try:source=channel_feed(self.update_feed.get(),self.update_channel.get().lower())
        except ValueError as error:self.update_feedback.set(str(error));return
        if source!=self.updater.source or self.update_channel.get().lower()!=self.updater.channel:self.check_updates();return
        self.updater.prepare()

    def cancel_update(self):
        self.install_requested=False;self.update_when_idle.set(False);self.updater.cancel_download()

    def defer_update(self,skip=False):
        item=self.updater.snapshot()[1]
        if not item:return
        self.settings['update_skip']=(self.update_channel.get().lower()+':'+item['version']) if skip else ''
        self.settings['update_remind_after']=0 if skip else int(time.time()+86400)
        self.install_requested=False;self.update_when_idle.set(False);self.updater.cancel_download()
        self.queue_settings_save()

    def install_update(self):
        if self.closing or self.updating:return
        if not getattr(sys,'frozen',False):
            self.update_feedback.set('Automatic replacement is available in the EXE. Source installs can use the release files.');return
        if self.update_busy():
            self.update_feedback.set('Pause fishing and wait for cleanup before updating. Use Download update to prepare it in the background.');return
        self.install_requested=True
        if not self.updater.ready:self.download_update()

    def poll_updates(self):
        state,item,installing,ready=self.updater.snapshot()
        detail=self.updater.details()
        if self.update_feedback.get()!=state:self.update_feedback.set(state)
        suppressed=bool(item and (self.settings.get('update_skip')==self.update_channel.get().lower()+':'+item['version'] or time.time()<self.settings.get('update_remind_after',0)))
        title=('Update '+item['version']+' available · Settings') if item and not suppressed else 'Version '+APP_VERSION
        if self.version_text.get()!=title:self.version_text.set(title)
        desired='normal' if item and not self.updating else 'disabled'
        if str(self.update_install_button.cget('state'))!=desired:self.update_install_button.configure(state=desired)
        self.update_download_button.configure(state='normal' if item and not installing and not ready else 'disabled')
        self.update_cancel_button.configure(state='normal' if installing or ready else 'disabled')
        self.update_progress['value']=detail['progress']
        text=('Version '+item['version']+' · '+item.get('channel','stable').title()+'\n'+item.get('notes','No release notes supplied.')) if item else 'No newer release found. Check for updates to refresh.'
        if suppressed:text+='\nNotice hidden. Check for updates to show it again.'
        if ready:text+='\n'+detail['signature']
        if self.update_release_text.get()!=text:self.update_release_text.set(text)
        try:current_feed=channel_feed(self.update_feed.get(),self.update_channel.get().lower())
        except ValueError:current_feed=None
        if ready and (self.updater.channel!=self.update_channel.get().lower() or self.updater.source!=current_feed or self.updater.require_signed!=self.update_require_signed.get() or self.updater.publisher!=self.update_publisher.get().strip().upper().replace(' ','')):
            self.cancel_update();self.updater.state='Update preferences changed. Download again to verify with the new preferences.';return
        if ready and (getattr(self,'install_requested',False) or self.update_when_idle.get()) and not self.update_busy():
            if not getattr(sys,'frozen',False):
                self.install_requested=False;self.update_when_idle.set(False);self.updater.state='Update ready. Source installs must replace files manually.';return
            self.updating=True
            try:self.updater.handoff(sys.executable,ASSET_DIR/'fishing-update.ps1')
            except (OSError,ValueError):
                self.updating=False;self.install_requested=False;self.updater.ready=None
                self.updater.state='Update could not start. Current app was kept; close and retry.';return
            self.close()
        elif getattr(self,'install_requested',False) and not ready and detail['phase'] in ('failed','cancelled'):
            self.install_requested=False

    def remote_action(self,action):
        if self.closing:return RemoteReply('The helper is closing. No action was taken.')
        if action=='pause':
            self.pause();return RemoteReply('Fishing paused. Keys are being released; use /fishing status to check cleanup.')
        if action=='status':
            stats=self.stats.snapshot();running=bool(self.worker and not self.worker.stop.is_set())
            stock=' · '.join(f'{kind.title()}: {self.alerts.amount(kind)}' for kind in ('bait','food','water'))
            text=(f'Fishing: {"running" if running else "paused"}\n{self.status.get()}\n'
                  f'Catches: {stats["catches"]} · Failures: {stats["failures"]} · Unconfirmed: {stats["unknown"]}\n'
                  f'{stock}\n'+('FiveM is focused.' if is_game(user32.GetForegroundWindow()) else 'FiveM is not focused.'))
            return RemoteReply(text)
        if action=='screen':return self.remote_screen()
        if action!='resume':return RemoteReply('Unsupported fishing command.')
        if user32.GetAsyncKeyState(STOP_KEY)&0x8000:return RemoteReply('F9 is held. Remote resume was blocked.')
        if self.pending_start:return RemoteReply('A local start countdown is already pending.')
        if self.worker and not self.worker.stop.is_set():return RemoteReply('Fishing is already running. Nothing was restarted.')
        if getattr(self,'thread',None) and self.thread.is_alive():return RemoteReply('Previous run is still cleaning up. Retry in a moment.')
        foreground=user32.GetForegroundWindow()
        if not is_game(foreground):
            if not self.remote.config['remote_focus']:return RemoteReply('FiveM is not focused. Enable Allow remote resume to focus FiveM in the helper, or focus it locally.')
            previous=self.worker
            if previous is None or not is_game(previous.hwnd) or process_id(previous.hwnd)!=previous.pid:
                return RemoteReply('No previously verified FiveM window is available. Open/focus the game locally first.')
            user32.ShowWindow(previous.hwnd,9);user32.SetForegroundWindow(previous.hwnd)
            if user32.GetForegroundWindow()!=previous.hwnd or not is_game(previous.hwnd):
                return RemoteReply('Windows did not allow FiveM to take focus. No fishing key was sent.')
        self.start()
        if self.worker and not self.worker.stop.is_set():
            return RemoteReply('Resume requested. Normal circle, focus, supply and preflight checks are running; check /fishing status for the result.')
        return RemoteReply('Could not resume: '+self.status.get())

    def remote_screen(self):
        if not self.remote.config['remote_screenshots']:return RemoteReply('Screenshot uploads are disabled.')
        hwnd=user32.GetForegroundWindow()
        if not is_game(hwnd):return RemoteReply('FiveM must be foreground to request its screenshot. No desktop image was sent.')
        pid=process_id(hwnd);bounds=game_bounds(hwnd);x,y,width,height=bounds
        if width*height>16_000_000:return RemoteReply('FiveM resolution is too large for a remote snapshot.')
        capture=Capture(x,y,width,height)
        try:frame=capture.read()
        finally:capture.close()
        if (user32.GetForegroundWindow()!=hwnd or process_id(hwnd)!=pid or not is_game(hwnd) or game_bounds(hwnd)!=bounds):
            return RemoteReply('FiveM focus or window position changed. The snapshot was discarded.')
        image=np.frombuffer(frame,np.uint8).reshape(height,width,4)[:,:,:3]
        scale=min(1,1920/width,1080/height)
        if scale<1:image=cv2.resize(image,None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA)
        ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,80])
        if not ok or jpeg.nbytes>4_000_000:return RemoteReply('Could not encode a bounded FiveM screenshot.')
        return RemoteReply('FiveM snapshot requested by you. Visible game overlays may appear. This is not a live feed.',jpeg.tobytes())


def main(resume_stats=None):
    if getattr(sys,'frozen',False):
        # External Node/PowerShell must use their system DLL search path.
        kernel32.SetDllDirectoryW.argtypes=[w.LPCWSTR]
        kernel32.SetDllDirectoryW(None)
    if '--self-test' in sys.argv:
        from fishing_selftest import run
        report=(sys.argv[sys.argv.index('--report')+1] if '--report' in sys.argv
                else str(data_dir()/'self-test.json'))
        return run(report)
    # Prevent two windows listening to Z and sending duplicate E/3 presses.
    ctypes.set_last_error(0)
    mutex = kernel32.CreateMutexW(None, False, 'Local\\CodexFishingHelper')
    if not mutex:
        raise OSError('Could not create the fishing helper instance lock.')
    duplicate = ctypes.get_last_error() == 183
    if duplicate:
        existing = user32.FindWindowW(None, 'Fishing helper')
        if existing:
            user32.ShowWindow(existing, 9)  # Restore a minimized existing app.
            user32.SetForegroundWindow(existing)
        kernel32.CloseHandle(mutex)
        return 0
    diagnostics = configure_diagnostics()
    LOGGER.info(json.dumps({'event': 'app_started', 'version': APP_VERSION}))
    app = None
    try:
        app = App(resume_stats,session_path=data_dir()/'fishing-session.json')
        if '--update-health' in sys.argv and '--update-token' in sys.argv:
            health_path=sys.argv[sys.argv.index('--update-health')+1];health_token=sys.argv[sys.argv.index('--update-token')+1]
            app.root.after(8000,lambda:app.acknowledge_update(health_path,health_token))
        app.root.mainloop()
    finally:
        if app is not None:
            if app.worker:
                app.worker.stop.set()
            if getattr(app, 'thread', None) is not None:
                app.thread.join()
        if diagnostics:
            diagnostics.stop()
        logging.shutdown()
        kernel32.CloseHandle(mutex)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
