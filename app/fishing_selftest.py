"""Packaged-app checks against saved fixtures. Never sends keyboard input."""
import json
import sys
import threading
import time
import traceback
import tempfile
import subprocess
import base64
from pathlib import Path

import cv2
import numpy as np
from fishing_features import OutcomeMonitor,SessionStats,NoticeEdges
from fishing_paths import ASSET_DIR,node_path,data_dir
from fishing_supplies import SupplySession,inventory_quantity,InventoryAmounts


def run(report_path):
    result={'ok':False,'checks':[],'errors':[]}
    monitor=None
    original_input=None
    try:
        from fishing_helper import CircleLocator,RingDetector,App,APP_VERSION,FishingWorker,user32,StageGate,PressConfirmation,press_stalled
        original_input=user32.SendInput
        def blocked_input(*args):raise AssertionError('Self-test must never send Windows input')
        user32.SendInput=blocked_input
        from fishing_session import RunJournal,SessionStore,export_history,export_support
        result['version']=APP_VERSION
        fixtures=ASSET_DIR/'test-fixtures'
        if '--fixtures' in sys.argv:fixtures=Path(sys.argv[sys.argv.index('--fixtures')+1]).resolve()
        if not getattr(sys,'frozen',False) and not fixtures.is_dir():
            fixtures=ASSET_DIR.parent/'work'
        circle=cv2.imread(str(fixtures/'fishing-reference.png'))
        assert circle is not None,'Circle fixture missing'
        circle=cv2.cvtColor(circle,cv2.COLOR_BGR2BGRA)
        match=CircleLocator().locate(circle.tobytes(),circle.shape[1],circle.shape[0])
        assert match,'Packaged circle discovery failed'
        (x,y),radius=match
        detector=RingDetector(radius)
        crop=circle[y-detector.half:y+detector.half+1,x-detector.half:x+detector.half+1]
        ring_crop=crop.copy()
        assert all(detector.detect(crop.tobytes())) and detector.is_present(crop.tobytes())
        result['checks'].append('Circle discovery, blue arc and white marker')
        gate=StageGate();target=(0,40,100)
        for step in range(4):gate.observe(-.09+step*.03,target,((330+step*10)%360,4,20),True)
        gate.record_press(0,target,(0,4,20))
        assert not gate.observe(.03,target,(55,4,20),True)
        assert gate.observe(.1,target,(78,4,20),True),'Small same-target reset was missed'
        gate=StageGate();gate.record_press(0,target,(0,4,20))
        pending=PressConfirmation();pending.sent(0,(0,4,20));pending.attempts=2;pending.travel=360
        gate.observe(3,(90,40,100),(90,4,20),True)
        assert not press_stalled(pending,gate,3),'New stage raced the stall guard'
        assert gate.observe(3.03,(90,40,100),(100,4,20),True)
        gate.motion_speeds=[540]*7
        assert not gate.press_window_current((0,30,100),(0,4,20),100,100.04)
        result['checks'].append('Same-arc stage reset, transition/stall ordering and stale-frame input guard')
        from fishing_safety import OvernightSafety
        safety_stats=SessionStats();safety=OvernightSafety();safety.start(safety_stats,100)
        for ended in (110,120,130):
            cycle=safety_stats.begin(ended-3);safety_stats.observe(cycle,challenge=ended-2,sent=ended-1)
            safety_stats.end(cycle,ended);safety_stats.finish('unknown',cycle)
        reason,grace=safety.assess(safety_stats,131)
        assert 'unconfirmed' in reason and grace==7
        safety_stats.finish('catch',cycle,confirmed_at=132)
        assert safety.assess(safety_stats,138)[0] is None
        assert '15 minutes' in safety.assess(safety_stats,1032)[0]
        for ended in range(1040,2300,30):
            failed=safety_stats.begin(ended-3);safety_stats.observe(failed,challenge=ended-2,sent=ended-1)
            safety_stats.end(failed,ended);safety_stats.finish('failure',failed)
            assert safety.assess(safety_stats,ended)[0] is None
        result['checks'].append('Overnight safety: bounded unconfirmed attempts, late reward grace and inactivity pause')
        speck=np.zeros((detector.size,detector.size,4),np.uint8)
        speck[detector.half,detector.half+round(radius),:3]=255
        assert detector.detect(speck.tobytes())[1] is None,'White speck accepted as marker'
        result['checks'].append('Rejected white-speck frame does not crash the marker filter')
        assert Path(node_path()).is_file()
        result['checks'].append('Bundled Node runtime')
        # Exercise the actual DLLs, Tk, and window icon without starting a run.
        from unittest.mock import MagicMock,patch
        import fishing_helper as helper
        with patch.object(helper,'load_settings',return_value=dict(helper.DEFAULT_SETTINGS)):
            app=App()
        # Disable every control callback before pumping layout events.
        for callback in app.root.tk.call('after','info'):app.root.after_cancel(callback)
        # Exercise the actual calculator widgets without saving test values to
        # the user's settings or invoking any game controls.
        from fishing_calculator import CALCULATOR_DEFAULTS
        saved_planner={key:var.get() for key,var in app.calculator_vars.items()}
        saved_queue_save=app.queue_settings_save
        app.queue_settings_save=lambda *_:None
        for key,value in CALCULATOR_DEFAULTS.items():app.calculator_vars[key].set(str(value))
        assert app.calculator_results['time'].get()=='6 hr 15 min'
        assert app.calculator_results['food'].get()=='10' and app.calculator_results['water'].get()=='15'
        app.calculator_vars['calc_bait'].set('2000')
        assert app.calculator_results['time'].get()=='12 hr 30 min'
        app.calculator_vars['calc_bait'].set('')
        assert all(var.get()=='—' for var in app.calculator_results.values())
        for key,value in saved_planner.items():app.calculator_vars[key].set(value)
        app.queue_settings_save=saved_queue_save
        app.toggle_section('calculator');app.root.update_idletasks()
        assert app.sections['calculator'].winfo_manager(),'Calculator panel did not open'
        assert app.root.winfo_height()<=app.root.winfo_screenheight()-100,'Calculator exceeds the screen'
        app.toggle_section('calculator')
        result['checks'].append('Bait calculator: time, food/water reserves, live edits, invalid input and Planner page; no settings saved')
        app.root.update()
        body=app.root.winfo_children()[0]
        result['requested_window']=[body.winfo_reqwidth()+48,body.winfo_reqheight()+32]
        result['window']=[app.root.winfo_width(),app.root.winfo_height()]
        assert body.winfo_reqwidth()+48<=app.root.winfo_width(),str(result['requested_window'])
        for child in body.winfo_children():
            assert child.winfo_y()+child.winfo_height()<=body.winfo_height(),'Clipped dashboard panel'
        assert not app.tracking_card.winfo_manager(),'Idle tracking should be hidden'
        assert str(app.reset_session_button.cget('state'))=='normal','Idle session reset unavailable'
        assert not app.sections['settings'].winfo_manager() and not app.sections['diagnostics'].winfo_manager()
        for name in app.sections:
            app.show_page(name);app.root.update_idletasks()
            assert [key for key,panel in app.sections.items() if panel.winfo_manager()]==[name]
            assert app.content.winfo_width()<=app.viewport.winfo_width(),'Page overflows horizontally'
        app.show_page('overview')
        from unittest.mock import MagicMock,patch
        stopped_worker=MagicMock();stopped_worker.stop=threading.Event();stopped_worker.stop.set()
        app.worker=stopped_worker
        with patch.object(user32,'GetAsyncKeyState',return_value=0):
            app.messages.put('Completed: Catch target reached.')
            app.poll_controls()
            assert app.phase_text.get()=='Run complete','Completed status discarded after stop'
            app.messages.put('Running: stale frame');app.poll_controls()
            assert app.status.get().startswith('Completed:'),'Stale progress replaced completion'
        app.worker=None;app.notifier.close()
        result['checks'].append('Stopped worker completion reaches native UI; stale running status remains rejected')
        assert not app.remote_vars['remote_enabled'].get() and app.remote.thread is None
        assert app.remote_token_entry.cget('show')=='•'
        from fishing_remote import DiscordControls,RemoteReply,RemoteRequest,make_client,protect_token,unprotect_token
        remote_config={'remote_enabled':True,'remote_token':'SELFTEST_FAKE_BOT_TOKEN_123456789',
            'remote_user_id':'11111111111111111','remote_guild_id':'22222222222222222','remote_channel_id':'33333333333333333'}
        controls=DiscordControls();controls.configure(remote_config)
        rejected=controls.submit('screen','44444444444444444',remote_config['remote_guild_id'],remote_config['remote_channel_id'],'1',time.time())
        assert isinstance(rejected,RemoteReply) and controls.queue.empty()
        request=controls.submit('pause',remote_config['remote_user_id'],remote_config['remote_guild_id'],remote_config['remote_channel_id'],'2',time.time())
        assert isinstance(request,RemoteRequest)
        controls.drain(lambda action:RemoteReply('paused offline'));assert request.future.result().text=='paused offline'
        assert unprotect_token(protect_token(remote_config['remote_token']))==remote_config['remote_token']
        result['checks'].append('Discord controls default off, masked/encrypted bot credential, owner/channel gate and offline action queue')
        import asyncio
        from unittest.mock import AsyncMock
        async def sdk_check():
            client=make_client(controls,remote_config,controls.generation)
            client.http.upsert_guild_command=AsyncMock(return_value={})
            try:
                await client.setup_hook()
                command=client.http.upsert_guild_command.call_args.args[2]
                assert command['name']=='fishing' and {v['name'] for v in command['options']}=={'status','pause','resume','screen'}
                assert client.intents.value==0
            finally:await client.close()
        asyncio.run(sdk_check());controls.close()
        result['checks'].append('Bundled Discord SDK registers one guild command with four actions and no privileged intents; HTTP mock, no login')
        capture=MagicMock();pixels=np.zeros((300,400,4),np.uint8);pixels[40:80,40:80,:3]=255;capture.read.return_value=pixels.tobytes()
        with patch.object(helper,'is_game',return_value=True),patch.object(helper,'process_id',return_value=42),patch.object(helper,'game_bounds',return_value=(0,0,400,300)),patch.object(user32,'GetForegroundWindow',return_value=101),patch.object(helper,'Capture',return_value=capture):
            image_reply=app.remote_screen()
        assert cv2.imdecode(np.frombuffer(image_reply.image,np.uint8),cv2.IMREAD_COLOR).shape==(300,400,3)
        capture.close.assert_called_once();app.remote.close()
        result['checks'].append('Requested game-area JPEG, focus/process checks and capture cleanup; synthetic pixels only, no real screen capture')
        result['window']=[app.root.winfo_width(),app.root.winfo_height()]
        app.root.destroy()
        result['checks'].append('Compact six-page navigation, bounded window, rounded panels, Tk runtime and fish icon')
        # Replay delayed consumption entirely offline, before any OCR threads
        # exist. The minimum increase is checked by the real worker wait loop.
        import fishing_helper as helper
        clock=[100.]
        recovery=FishingWorker.__new__(FishingWorker)
        recovery.stop=threading.Event()
        recovery.settings={'item_wait':8};recovery.supplies=SupplySession();recovery.alerts=None
        recovery.monitor=MagicMock(state='Local OCR active',needs_error='')
        recovery.monitor.needs_debug={'icons_found':True}
        recovery.monitor.needs_snapshot=lambda:({'hunger':(clock[0],3 if clock[0]<110 else 35)},{'hunger':True})
        recovery.check=lambda:None;recovery.publish=lambda **kw:None;recovery.log=lambda *a,**kw:None
        recovery.delay=lambda seconds:clock.__setitem__(0,clock[0]+seconds)
        recovery.supplies.used('hunger',100,3)
        with patch.object(helper.time,'monotonic',side_effect=lambda:clock[0]):
            recovery.verify_supply_use('hunger',100,3)
        assert clock[0]>=110 and not recovery.supplies.snapshot()['pending_uses']
        result['checks'].append('Delayed food recovery waits for a fresh increase without another item input; offline clock/HUD replay')
        frame=np.zeros((480,1000,4),np.uint8)
        for name,x,y in [('fishing-rods-reference.png',300,20),('fishing-needs-reference.png',20,380)]:
            source=cv2.imread(str(fixtures/name))
            assert source is not None,name+' missing'
            frame[y:y+source.shape[0],x:x+source.shape[1],:3]=source
        captures=[]
        class Capture:
            def __init__(self,*args):
                self.closed=False
                captures.append(self)
            def read(self):return frame.tobytes()
            def close(self):self.closed=True
        monitor=OutcomeMonitor(1,Capture,lambda hwnd:(0,0,1000,480),lambda:True,needs_enabled=True,capacity_enabled=True)
        monitor.start()
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if (monitor.inventory and monitor.inventory[1]=={'1':True,'2':True,'3':True}
                    and monitor.needs_debug.get('inventory_open') is True):break
            if monitor.state.startswith('OCR unavailable'):raise AssertionError(monitor.state)
            time.sleep(.1)
        else:raise AssertionError(('OCR fixture timeout',monitor.state,monitor.needs_debug,monitor.inventory))
        assert monitor.needs_snapshot()==({},{}),'Inventory view retained HUD authorizations'
        result['checks'].append('Offline Windows OCR: rods 1–3; obscured HUD readings suppressed during inventory')
        deadline=time.monotonic()+20
        while not monitor.inventory_capacity and time.monotonic()<deadline:time.sleep(.05)
        assert monitor.inventory_capacity and monitor.inventory_capacity[1]==(65.52,100),('Capacity OCR',monitor.inventory_capacity)
        result['checks'].append('Two native weight readings: 65.52/100kg with literal slash/unit; normal inventory headroom')
        frame.fill(0)
        hud=cv2.imread(str(fixtures/'fishing-needs-reference.png'))
        frame[380:380+hud.shape[0],20:20+hud.shape[1],:3]=hud
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            needs,trust=monitor.needs_snapshot()
            if (not monitor.inventory_visible and needs.get('hunger',(-1,-1))[1]==3
                    and needs.get('thirst',(-1,-1))[1]==60 and trust.get('hunger') is True):break
            if monitor.state.startswith('OCR unavailable'):raise AssertionError(monitor.state)
            time.sleep(.1)
        else:raise AssertionError(('HUD after inventory closure timeout',monitor.needs_debug))
        result['checks'].append('Tesseract: fresh hunger 3%, thirst 60% reacquired after inventory closes')
        frame.fill(0)
        source=cv2.imread(str(fixtures/'fishing-empty-slot-reference.png'))
        assert source is not None,'Empty-slot fixture missing'
        frame[20:20+source.shape[0],300:300+source.shape[1],:3]=source
        hud=cv2.imread(str(fixtures/'fishing-needs-reference.png'))
        frame[380:380+hud.shape[0],20:20+hud.shape[1],:3]=hud
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if monitor.inventory and monitor.inventory[1]=={'1':True,'2':True,'3':False}:break
            if monitor.state.startswith('OCR unavailable'):raise AssertionError(monitor.state)
            time.sleep(.1)
        else:raise AssertionError(('Empty-slot inventory timeout',monitor.inventory,monitor.state))
        supplies=SupplySession()
        assert supplies.inventory(monitor.inventory[1])=='1','Empty third slot did not select backup 1'
        result['checks'].append('Actual missing-number empty slot 3: rods in 1/2 recognized and backup 1 selected')
        frame.fill(0)
        source=cv2.imread(str(fixtures/'fishing-nonrod-reference.png'))
        assert source is not None,'Non-rod fixture missing'
        frame[20:20+source.shape[0],300:300+source.shape[1],:3]=source
        frame[380:380+hud.shape[0],20:20+hud.shape[1],:3]=hud
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if monitor.inventory and monitor.inventory[1]=={'1':False,'2':True,'3':True}:break
            if monitor.state.startswith('OCR unavailable'):raise AssertionError(monitor.state)
            time.sleep(.1)
        else:raise AssertionError(('Non-rod inventory timeout',monitor.inventory,monitor.state))
        supplies=SupplySession()
        assert supplies.inventory(monitor.inventory[1])=='3','Valid rod in 3 not retained'
        assert supplies.inventory({'1':False,'2':True,'3':False})=='2','Rubber slot was not skipped'
        result['checks'].append('Actual Rubber slot 1 skipped, rods 2/3 recognized and backup 2 selected when 3 is unavailable')
        supplies=SupplySession(('2',))
        assert supplies.inventory({'1':True,'2':True,'3':True})=='2','Disabled stocked slots selected'
        assert supplies.break_rod(time.monotonic()) and supplies.rod is None,'Disabled backup used after rod broke'
        result['checks'].append('Enabled-slot selection and break rotation exclude unchecked stocked slots')
        frame.fill(0)
        source=cv2.imread(str(fixtures/'fishing-bright-inventory-reference.png'))
        assert source is not None,'Bright inventory fixture missing'
        frame[20:20+source.shape[0],300:300+source.shape[1],:3]=source
        changed=time.monotonic()
        deadline=changed+25
        while time.monotonic()<deadline:
            if (monitor.inventory and monitor.inventory[0]>=changed and
                    monitor.inventory[1]=={'1':False,'2':True,'3':True}):break
            if monitor.state.startswith('OCR unavailable'):raise AssertionError(monitor.state)
            time.sleep(.1)
        else:raise AssertionError(('Bright inventory timeout',monitor.inventory,monitor.state))
        result['checks'].append('Actual daylight inventory: merged badges found, rods 2/3 recognized')
        frame.fill(0)
        source=cv2.imread(str(fixtures/'fishing-bait-row-reference.png'))
        assert source is not None,'Bait row fixture missing'
        frame[20:20+source.shape[0],300:300+source.shape[1],:3]=source
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            amounts=monitor.inventory_amounts
            if amounts and amounts[1]=={'bait':132,'food':9,'water':6}:break
            if monitor.state.startswith('OCR unavailable'):raise AssertionError(monitor.state)
            time.sleep(.1)
        else:raise AssertionError(('Inventory quantities timeout',monitor.inventory_amounts,monitor.state))
        result['checks'].append('Supplied bait placed in test row: bait 132, food 9, water 6 confirmed through real OCR')
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if monitor.inventory_capacity and monitor.inventory_capacity[1]==(132.78,100):break
            time.sleep(.05)
        else:raise AssertionError(('Overweight capacity OCR',monitor.inventory_capacity))
        preflight=RunJournal();preflight.record_capacity(monitor.inventory_capacity)
        preflight.inventory(monitor.inventory_amounts,0)
        detail,short,_=preflight.overnight_check({'calc_bait':1,'inventory_reserve_kg':5,'inventory_space_check':True},dict(rod_slots=('3','2'),broken=set(),rod_checked=True))
        assert short and '0 kg free' in detail
        result['checks'].append('Native/Tesseract inventory weight 132.78/100kg confirmed twice; overweight preflight rejected without game input')
        detail,short,unknown=preflight.overnight_check({'calc_bait':1,'inventory_space_check':False},dict(rod_slots=('3','2'),broken=set(),rod_checked=True))
        assert not short and not unknown and 'not enforced' in detail
        result['checks'].append('Unlimited-inventory setting bypasses weight shortage while retaining rod/supply preflight')
        from fishing_discord import DiscordNotifier,FishingAlerts,DISCORD_DEFAULTS,protect_url,unprotect_url
        test_url='https://discord.com/api/webhooks/123456/SELFTEST_ONLY'
        assert unprotect_url(protect_url(test_url))==test_url,'Windows webhook encryption failed'
        requests=[]
        class Reply:
            status=200
            headers={}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"id":"123987"}'
        class LocalMock:
            def open(self,request,timeout):requests.append(request);return Reply()
        notifier=DiscordNotifier(LocalMock())
        try:
            notifier.configure({**DISCORD_DEFAULTS,'discord_enabled':True,'discord_url':test_url})
            # Exercise the same notice/counter/alert path as a real run. This
            # worker has no Windows handle and its input methods are never used.
            worker=FishingWorker.__new__(FishingWorker)
            worker.settings={'debug':False};worker.stats=SessionStats();worker.monitor=monitor
            worker.stop=threading.Event()
            worker.journal=RunJournal()
            worker.reward_notices=[];worker.ocr_candidate='';worker.outcome=None
            worker.alerts=FishingAlerts(worker.stats,notifier)
            worker.alerts.journal=worker.journal
            worker.alerts.inventory_amounts(monitor.inventory_amounts)
            source=cv2.imread(str(fixtures/'fishing-reward-reference.png'))
            assert source is not None,'Catch reward fixture missing'
            for number in (1,2):
                frame.fill(0)
                scanned=monitor.reward_scans
                deadline=time.monotonic()+8
                while (monitor.reward_scans<=scanned or monitor.inventory_visible) and time.monotonic()<deadline:time.sleep(.02)
                assert not monitor.inventory_visible,'Inventory did not close before reward test'
                time.sleep(1.1) # Separate identical reward popups.
                monitor.drain()
                now=time.monotonic();worker.cycle_id=worker.stats.begin(now-.1);worker.outcome=None
                worker.stats.observe(worker.cycle_id,challenge=now-.09,sent=now-.08,absent=now-.01)
                worker.alerts.started(worker.cycle_id,'3')
                worker.journal.started(worker.cycle_id,'3')
                monitor.prioritize_rewards()
                frame[100:100+source.shape[0],100:100+source.shape[1],:3]=source
                deadline=time.monotonic()+8
                while worker.stats.snapshot()['catches']<number and time.monotonic()<deadline:
                    worker.collect_notices();time.sleep(.02)
                assert worker.stats.snapshot()['catches']==number,('Reward not counted',monitor.state,monitor.candidate)
                worker.stats.end(worker.cycle_id,time.monotonic())
                worker.journal.completed();worker.journal.sync(worker.stats)
                worker.alerts.flush()
                deadline=time.monotonic()+12
                while notifier.sent<number and time.monotonic()<deadline:
                    worker.collect_notices();time.sleep(.02)
            assert [request.method for request in requests]==['POST','PATCH'],'Live card did not update'
            assert '/messages/123987' in requests[1].full_url
            description=json.loads(requests[1].data)['embeds'][0]['description']
            assert 'Last catch: 1× Anchovy' in description and 'Session catches: 2' in description
            # Separate card text/geometry is replayed after native fish OCR.
            # A delayed reply must enrich that catch without adding another.
            sampled=worker.stats.attempts[worker.cycle_id]['end']+.01
            worker.reward_notices.extend(NoticeEdges().update(['ADDED 2X','RUBBER'],sampled,
                [[20,20,70,10],[20,110,70,10]]))
            worker.collect_rewards();worker.journal.sync(worker.stats)
            deadline=time.monotonic()+3
            while notifier.sent<3 and time.monotonic()<deadline:time.sleep(.02)
            assert len(requests)==3 and requests[-1].method=='PATCH','Extra item did not enrich the existing card'
            description=json.loads(requests[-1].data)['embeds'][0]['description']
            assert '1× Anchovy' in description and '2× Rubber' in description and 'Session catches: 2' in description
            assert worker.stats.snapshot()['catches']==2
            # Replay the user's actual 1,023 bait header through Windows OCR.
            source=cv2.imread(str(fixtures/'fishing-bait-thousands-reference.png'))
            assert source is not None,'Grouped bait-count fixture missing'
            header=source[5:round(source.shape[1]*.26),21:source.shape[1]-2]
            quantity_proc=subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-File',str(ASSET_DIR/'fishing-ocr.ps1')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
            worker.alerts.inventory_amounts((time.monotonic(),{'bait':25}))
            try:
                assert json.loads(quantity_proc.stdout.readline()).get('ready')
                counts=[];quantity_times=[]
                for height in (72,96,144):
                    zoom=height/header.shape[0]
                    crop=cv2.resize(header,None,fx=zoom,fy=zoom,interpolation=cv2.INTER_CUBIC)
                    ok,png=cv2.imencode('.png',crop);assert ok
                    quantity_proc.stdin.write(base64.b64encode(png).decode()+'\n');quantity_proc.stdin.flush()
                    reply=json.loads(quantity_proc.stdout.readline())
                    counts.append(inventory_quantity(' '.join(reply.get('lines',[]))))
                    quantity_times.append(time.monotonic())
                assert counts==[1023,1023,1023],('Grouped quantity OCR failed',counts)
            finally:
                quantity_proc.stdin.close();quantity_proc.wait(timeout=10);quantity_proc.stdout.close()
            amounts_reader=InventoryAmounts()
            assert amounts_reader.update({'bait':counts[0]},quantity_times[0]) is None
            confirmed=amounts_reader.update({'bait':counts[1]},quantity_times[1])
            worker.alerts.inventory_amounts(confirmed)
            assert worker.alerts.amount('bait')=='1023 (last inventory check)'
            assert worker.journal.stock['bait']['count']==1023
            assert len(worker.journal.history())==2
            assert all(row['items'].get('Anchovy')==1 for row in worker.journal.history())
            with tempfile.TemporaryDirectory(dir=Path(report_path).parent,prefix='fishing-session-check-') as folder:
                folder=Path(folder);store=SessionStore(folder/'session.json')
                store.submit(dict(format=1,stats=worker.stats.snapshot(),journal=worker.journal.export_state(),discord=notifier.saved_live_card()))
                store.close()
                reader=SessionStore(folder/'session.json')
                try:saved=reader.load()
                finally:reader.close()
                restored=RunJournal(saved['journal'])
                assert len(restored.history())==2 and saved['stats']['catches']==2
                restored_notifier=DiscordNotifier(LocalMock())
                restored_notifier.configure(notifier.settings);restored_notifier.restore_live_card(saved['discord'])
                assert restored_notifier.live_id=='123987','Saved Discord card was not restored'
                restored_notifier.close()
                export_history(folder/'history.csv',restored)
                export_support(folder/'support.zip',APP_VERSION,{'discord_url':test_url},worker.stats,restored,notifier,folder/'no-log')
                assert (folder/'history.csv').is_file() and (folder/'support.zip').is_file()
            worker.stats.reset()
            worker.journal.reset()
            assert worker.alerts.reset_session(),'Session reset was not queued'
            deadline=time.monotonic()+3
            while notifier.sent<4 and time.monotonic()<deadline:time.sleep(.02)
            assert len(requests)==4 and requests[-1].method=='PATCH','Session reset did not edit the live card'
            assert '/messages/123987' in requests[-1].full_url,'Reset changed the live message'
            description=json.loads(requests[-1].data)['embeds'][0]['description']
            assert 'Session catches: 0' in description and 'Last catch: —' in description
            assert 'Bait left: 1023' in description and 'Food left: 9' in description
            assert worker.stats.snapshot()['catches']==0 and not worker.alerts.records
        finally:
            notifier.close()
            if notifier.thread:notifier.thread.join(2)
        result['checks'].append('Actual Anchovy popup through native OCR → worker catch counter → automatic live POST/PATCH after item collection; local HTTP mock only')
        result['checks'].append('Separate extra-item card text/geometry → same catch/history → live PATCH with both items and unchanged catch count; local HTTP mock only')
        result['checks'].append('Session reset clears counters and edits the same live message to zero, preserving supply amounts; local HTTP mock only')
        result['checks'].append('Native catch history → atomic saved session → restored history/live-card ID → CSV and troubleshooting ZIP; isolated files')
        result['checks'].append('Actual 1,023 bait screenshot at three OCR sizes → whole grouped quantity → two matching reads → old 25 replaced → same live card PATCH; local HTTP mock only')
        result['checks'].append('Windows webhook encryption')
        health_requests=[]
        class HealthReply(Reply):
            def __init__(self,identifier):self.identifier=identifier
            def read(self,*args):return json.dumps({'id':self.identifier}).encode()
        class HealthMock:
            def open(self,request,timeout):
                health_requests.append(request);return HealthReply(str(800+len(health_requests)))
        health_notifier=DiscordNotifier(HealthMock())
        try:
            health_notifier.configure({**DISCORD_DEFAULTS,'discord_enabled':True,'discord_url':test_url})
            health_notifier.enqueue('catches',('catch',1),{'content':'fixture catch'})
            deadline=time.monotonic()+3
            while health_notifier.sent<1 and time.monotonic()<deadline:time.sleep(.01)
            health=FishingAlerts(SessionStats(),health_notifier);health.set_health_state('Waiting for fish');health.update_health()
            deadline=time.monotonic()+3
            while health_notifier.sent<2 and time.monotonic()<deadline:time.sleep(.01)
            health.health_stopped('Fixture stop reason')
            deadline=time.monotonic()+3
            while health_notifier.sent<3 and time.monotonic()<deadline:time.sleep(.01)
            assert [r.method for r in health_requests]==['POST','POST','PATCH']
            assert '/messages/802' in health_requests[-1].full_url and health_notifier.live_id=='801'
            assert 'Fixture stop reason' in json.loads(health_requests[-1].data)['embeds'][0]['description']
        finally:
            health_notifier.close()
            if health_notifier.thread:health_notifier.thread.join(2)
        result['checks'].append('Separate live health card posts/edits exact pause reason without replacing catch card; local HTTP mock only')
        from fishing_reliability import FailureRecorder,RecoveryBudget
        with tempfile.TemporaryDirectory(prefix='fishing-snapshot-check-') as temporary:
            recorder=FailureRecorder(Path(temporary)/'snapshots')
            recorder.observe(ring_crop.tobytes(),ring_crop.shape[0],100,target=[0,40],marker=[50,4],last_press=99.9)
            assert recorder.snapshot('fixture failure');recorder.close()
            metadata=json.loads(Path(recorder.last_path).read_text())
            snapshot=cv2.imread(str(Path(recorder.last_path).with_suffix('.png')),cv2.IMREAD_UNCHANGED)
            assert snapshot.shape==(ring_crop.shape[0],ring_crop.shape[1],3) and metadata['reason']=='fixture failure'
        budget=RecoveryBudget()
        assert budget.claim('ocr',100) and budget.claim('ocr',110) and not budget.claim('ocr',120)
        assert budget.claim('hud',120) and budget.claim('ocr',700)
        result['checks'].append('Local ring-only PNG/JSON failure bundle, opaque pixels and two-per-type recovery budget')
        monitor.close()
        assert not monitor.thread.is_alive() and not monitor.hud_thread.is_alive(),'OCR threads did not stop'
        assert monitor.proc.poll() is not None and monitor.hud_proc.poll() is not None
        assert all(capture.closed for capture in captures)
        result['checks'].append('Both OCR processes, threads and captures close cleanly')
        result['data_directory']=str(data_dir())
        result['ok']=True
    except Exception:
        result['errors'].append(traceback.format_exc())
    finally:
        if original_input is not None:user32.SendInput=original_input
        if monitor:monitor.close()
        Path(report_path).write_text(json.dumps(result,indent=2),encoding='utf-8')
    return 0 if result['ok'] else 1
