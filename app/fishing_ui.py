"""Compact dashboard and readable fishing status descriptions."""
import tkinter as tk
import math
from tkinter import ttk
from fishing_paths import ASSET_DIR
from fishing_discord import DISCORD_DEFAULTS
from fishing_calculator import CALCULATOR_DEFAULTS
from fishing_remote import REMOTE_DEFAULTS

BG,SURFACE,BORDER = '#10171e','#18232e','#2c3a47'
TEXT,MUTED,ACCENT = '#ecf3f8','#a0b0bf','#2caac2'


def duration(seconds):
    seconds=max(0,int(seconds))
    hours,remainder=divmod(seconds,3600)
    minutes,seconds=divmod(remainder,60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def age(seconds):
    if seconds is None:return '—'
    if seconds<60:return f'{seconds}s ago'
    if seconds<3600:return f'{seconds//60}m ago'
    return f'{seconds//3600}h {(seconds%3600)//60}m ago'


def phase(text,running=False):
    lowered=text.lower()
    if lowered.startswith('completed:'):
        return 'Run complete',text.partition(':')[2].strip()+'\nReset the session or raise the stop target to begin again.'
    if lowered.startswith('paused:'):
        reason=text.partition(':')[2].strip()
        if reason.lower().startswith('pre-run supply check:'):
            action='Review the shortages and planned bait amount in Planner, then retry.'
        elif 'icons are visible' in lowered:
            action='Press Z to retry percentage recognition.'
        elif 'hud' in lowered or 'uncertain' in lowered:
            action='Keep the hunger/thirst HUD visible, then retry.'
        elif 'restore hunger' in lowered or 'restore thirst' in lowered:
            action='Check food in slot 4 and water in slot 5, then retry.'
        elif ('rod slots are empty' in lowered or 'no rod remains' in lowered
              or 'no fishing rods in slots' in lowered):
            action='Refill the enabled rod slots, then retry.'
        elif 'inventory' in lowered:
            action='Check the numbered slots in Tab inventory, then retry.'
        elif 'bait' in lowered:
            action='Refill your bait or free inventory space, then retry.'
        elif 'threshold' in lowered or 'settings' in lowered:
            action='Correct the values in Settings, then retry.'
        else:
            action='Resolve the issue, focus FiveM, then retry.'
        return 'Needs attention',reason+'\n'+action
    if lowered.startswith('paused'):
        return 'Paused',('Focus FiveM and press Z to resume.' if 'focus' in lowered
                         else 'Press Z in FiveM to resume.')
    if not running:return 'Ready',text.replace('\n',' ')
    if lowered.startswith(('recovering local','retrying temporary')):return 'Recovering recognition',''
    if 'inventory' in lowered or 'checking rod' in lowered:return 'Checking rods',''
    if 'low hunger' in lowered:return 'Restoring food',''
    if 'low thirst' in lowered:return 'Restoring water',''
    if 'reading hunger' in lowered:return 'Reading supplies',''
    if 'waiting for circle' in lowered or 'waiting for the fishing circle' in lowered or 'waiting for /' in lowered or 'waiting for circle' in lowered or 'equipping rod' in lowered:
        return 'Waiting for bite',''
    if 'circle found' in lowered or 'e sent' in lowered or 'stage' in lowered or 'tracking' in lowered:
        return 'Matching circle',''
    if 'local outcome' in lowered or 'starting local' in lowered:return 'Preparing fishing',''
    if 'switching' in lowered:return 'Switching rod',''
    if 'next cycle' in lowered or 'next attempt' in lowered:return 'Preparing next attempt',''
    return 'Fishing',''


class ThinMeter(tk.Canvas):
    """Flat meter, without the platform theme's raised progress-bar border."""
    def __init__(self,parent,maximum=100,value=0,style=None,**options):
        self.maximum=float(maximum);self.value=float(value)
        super().__init__(parent,height=5,bg=BORDER,highlightthickness=0,bd=0,**options)
        self.bind('<Configure>',self.draw)

    def configure(self,cnf=None,**options):
        if cnf is not None:
            if not isinstance(cnf,dict):return super().configure(cnf,**options)
            options={**cnf,**options}
        if not options:return super().configure()
        for key in ('maximum','value'):
            if key in options:
                number=float(options.pop(key))
                if not math.isfinite(number) or (key=='maximum' and number<=0):raise ValueError('Invalid meter value')
                setattr(self,key,number)
        if options:super().configure(**options)
        self.draw()
    config=configure

    def cget(self,key):
        if key in ('value','maximum'):return getattr(self,key)
        return super().cget(key)

    def draw(self,event=None):
        self.delete('fill')
        fraction=max(0,min(1,self.value/self.maximum))
        if fraction:self.create_rectangle(0,0,round(self.winfo_width()*fraction),5,fill=ACCENT,outline='',tags='fill')


class RoundedPanel(tk.Frame):
    """Rounded native surface; children remain ordinary accessible Tk widgets."""
    def __init__(self,parent,**options):
        super().__init__(parent,bg=BG,**options)
        self.backdrop=tk.Canvas(self,bg=BG,highlightthickness=0,bd=0)
        self.backdrop.place(x=0,y=0,relwidth=1,relheight=1)
        tk.Misc.lower(self.backdrop)
        self.backdrop.bind('<Configure>',self.draw)

    def draw(self,event):
        width,height=event.width-1,event.height-1
        radius=min(12,width/2,height/2)
        if radius<=0:return
        self.backdrop.delete('surface')
        points=(radius,1,width-radius,1,width,1,width,radius,width,height-radius,
                width,height,width-radius,height,radius,height,1,height,1,height-radius,1,radius,1,1)
        self.backdrop.create_polygon(points,smooth=True,splinesteps=20,fill=SURFACE,
                                     outline=BORDER,width=1,tags='surface')


def build_dashboard(app,version,draw_tracking):
    root=app.root;root.configure(bg=BG);root.option_add('*Font',('Segoe UI',10))
    style=ttk.Style(root);style.theme_use('clam')
    for name,bg,fg in [('Primary',ACCENT,'#082c36'),('Secondary',SURFACE,TEXT),('Quiet',BG,MUTED),
                       ('Nav',BG,MUTED),('SelectedNav',SURFACE,TEXT)]:
        style.configure(name+'.TButton',background=bg,foreground=fg,font=('Segoe UI',10),
                        padding=(12,8),borderwidth=0,focuscolor=ACCENT)
        style.map(name+'.TButton',background=[('disabled',BG),('pressed',BORDER),('active',SURFACE if name!='Primary' else '#49bad0')],
                  foreground=[('disabled','#667787')])
    style.configure('Timing.TEntry',fieldbackground=BG,foreground=TEXT,insertcolor=TEXT,
                    bordercolor=BORDER,lightcolor=BORDER,darkcolor=BORDER,padding=7)
    style.configure('Supply.Horizontal.TProgressbar',background=ACCENT,troughcolor=BORDER,
                    borderwidth=0,thickness=5)
    style.configure('Fishing.Treeview',background=BG,fieldbackground=BG,foreground=TEXT,rowheight=34,
                    font=('Segoe UI',10),borderwidth=0)
    style.configure('Fishing.Treeview.Heading',background=SURFACE,foreground=MUTED,font=('Segoe UI',10),padding=9)
    style.map('Fishing.Treeview',background=[('selected','#244454')],foreground=[('selected',TEXT)])
    style.configure('Vertical.TScrollbar',background=BORDER,troughcolor=BG,arrowcolor=MUTED,
                    bordercolor=BG,lightcolor=BG,darkcolor=BG,borderwidth=0,width=10)
    style.map('Vertical.TScrollbar',background=[('active','#415563'),('pressed',ACCENT)])
    style.configure('Disclosure.TButton',background=SURFACE,foreground=MUTED,anchor='w',
                    font=('Segoe UI',10),padding=(8,6),borderwidth=0)
    style.map('Disclosure.TButton',background=[('active',SURFACE)],foreground=[('active',TEXT)])
    app.ui_wrap_labels=[]
    def label(parent,text=None,var=None,muted=False,font=None,bg=None,wrap=False,**options):
        widget=tk.Label(parent,text=text,textvariable=var,bg=bg or SURFACE,fg=options.pop('fg',MUTED if muted else TEXT),
                        font=font or ('Segoe UI',10),anchor='w',justify='left',**options)
        if wrap:
            widget.configure(wraplength=350)
            app.ui_wrap_labels.append(widget)
        return widget
    def text(parent,message=None,var=None,muted=True,pady=(0,10)):
        widget=label(parent,message,var,muted,wrap=True);widget.pack(fill='x',padx=18,pady=pady);return widget
    def card(parent,title=None):
        panel=RoundedPanel(parent)
        if title:label(panel,title,font=('Segoe UI',11,'bold')).pack(fill='x',padx=18,pady=(16,8))
        return panel
    def row(parent,pady=(0,12)):
        result=tk.Frame(parent,bg=SURFACE);result.pack(fill='x',padx=18,pady=pady);return result
    def checkbox(parent,caption,var,command=None):
        return tk.Checkbutton(parent,text=caption,variable=var,command=command,bg=SURFACE,fg=TEXT,
            selectcolor=BG,activebackground=SURFACE,activeforeground=TEXT,font=('Segoe UI',10),
            bd=0,highlightthickness=0,anchor='w',justify='left')
    def field(parent,caption,var,width=9,unit=''):
        result=tk.Frame(parent,bg=SURFACE);result.pack(fill='x',pady=5)
        label(result,caption,muted=True).pack(side='left')
        if unit:label(result,unit,muted=True,width=5).pack(side='right',padx=(7,0))
        entry=ttk.Entry(result,textvariable=var,width=width,style='Timing.TEntry',justify='center')
        entry.pack(side='right');return entry
    def column_field(parent,column,caption,var):
        parent.columnconfigure(column,weight=1,uniform='fields')
        group=tk.Frame(parent,bg=SURFACE);group.grid(row=0,column=column,sticky='ew',padx=(0,12))
        label(group,caption,muted=True).pack(anchor='w',pady=(0,7))
        entry=ttk.Entry(group,textvariable=var,width=9,style='Timing.TEntry',justify='center')
        entry.pack(fill='x');return entry
    def details(parent,title,name):
        group=card(parent);group.pack(fill='x',pady=(0,10))
        content=tk.Frame(group,bg=SURFACE);opened=tk.BooleanVar(master=root,value=False)
        def toggle():
            opened.set(not opened.get());button.configure(text=('▾ ' if opened.get() else '▸ ')+title)
            if opened.get():content.pack(fill='x',padx=18,pady=(0,16))
            else:content.pack_forget()
            app.resize_content()
        button=ttk.Button(group,text='▸ '+title,style='Disclosure.TButton',command=toggle)
        button.pack(fill='x',padx=12,pady=4)
        app.detail_groups[name]=(group,content,button,opened)
        return content

    app.body=tk.Frame(root,bg=BG);app.body.pack(fill='both',expand=True,padx=24,pady=16)
    header=tk.Frame(app.body,bg=BG);header.pack(fill='x',pady=(0,14))
    app.logo=tk.PhotoImage(master=root,file=str(ASSET_DIR/'fishing-logo-small.png'))
    app.logo=app.logo.zoom(2).subsample(3)
    app.app_icon=tk.PhotoImage(master=root,file=str(ASSET_DIR/'fishing-logo.png'));root.iconphoto(True,app.app_icon)
    label(header,image=app.logo,bg=BG).pack(side='left',padx=(0,10))
    label(header,'Fishing Helper',font=('Segoe UI',16,'bold'),bg=BG).pack(side='left')
    app.version_text=tk.StringVar(master=root,value='Version '+version)
    version_label=label(header,var=app.version_text,muted=True,bg=BG,font=('Segoe UI',9));version_label.pack(side='right')
    version_label.configure(cursor='hand2');version_label.bind('<Button-1>',lambda event:app.show_updates())
    app.nav=tk.Frame(app.body,bg=BG);app.nav.pack(fill='x',pady=(0,16))
    app.page_titles={'overview':('Overview','Your session, supplies and next catch at a glance.'),
                     'supplies':('Supplies','Inventory slots and automatic food & water.'),
                     'calculator':('Planner','Estimate supplies and choose when your run should stop.'),
                     'history':('Catch history','Confirmed items and results from this session.'),
                     'settings':('Settings','Safety, Discord and advanced preferences.'),
                     'diagnostics':('Diagnostics','Recognition, stage tracking and local reports.')}
    app.page_buttons={}
    for column,(name,(title,_)) in enumerate(app.page_titles.items()):
        button=ttk.Button(app.nav,text=title,style='Nav.TButton',command=lambda name=name:app.show_page(name))
        button.grid(row=0,column=column,sticky='ew',padx=(0,5));app.nav.columnconfigure(column,weight=1)
        app.page_buttons[name]=button
        setattr(app,name+'_button',button)
    app.fold_controls=app.nav
    app.footer=tk.Frame(app.body,bg=BG);app.footer.pack(side='bottom',fill='x',pady=(12,0))
    label(app.footer,'Z  Start / pause    ·    F9  Emergency stop',bg=BG,muted=True,font=('Segoe UI',9)).pack(side='left')
    label(app.footer,'FiveM must stay focused',bg=BG,muted=True,font=('Segoe UI',9)).pack(side='right')
    app.page_header=tk.Frame(app.body,bg=BG);app.page_header.pack(fill='x',pady=(0,14))
    heading=tk.Frame(app.page_header,bg=BG);heading.pack(side='left',fill='x',expand=True)
    app.page_title=tk.StringVar(master=root,value='Overview');app.page_subtitle=tk.StringVar(master=root)
    label(heading,var=app.page_title,font=('Segoe UI',22,'bold'),bg=BG).pack(anchor='w')
    label(heading,var=app.page_subtitle,muted=True,bg=BG,font=('Segoe UI',9)).pack(anchor='w',pady=(3,0))
    status=tk.Frame(app.page_header,bg=BG);status.pack(side='right',padx=(8,0))
    app.badge=label(status,'●',bg=BG,fg=ACCENT);app.badge.pack(side='left',padx=(0,6))
    app.phase_text=tk.StringVar(master=root,value='Ready');label(status,var=app.phase_text,bg=BG).pack(side='left')
    app.recovery_text=tk.StringVar(master=root)
    app.recovery_label=label(app.body,var=app.recovery_text,muted=True,bg=BG,font=('Segoe UI',9),wrap=True)
    app.scroll_host=tk.Frame(app.body,bg=BG);app.scroll_host.pack(fill='both',expand=True)
    app.viewport=tk.Canvas(app.scroll_host,bg=BG,highlightthickness=0,width=850,height=600)
    app.scrollbar=ttk.Scrollbar(app.scroll_host,orient='vertical',command=app.viewport.yview)
    app.viewport.configure(yscrollcommand=app.scrollbar.set);app.viewport.pack(side='left',fill='both',expand=True)
    app.content=tk.Frame(app.viewport,bg=BG)
    app.content_window=app.viewport.create_window((0,0),window=app.content,anchor='nw')
    app.viewport.bind('<Configure>',app.resize_viewport);app.content.bind('<Configure>',app.resize_content)
    root.bind('<MouseWheel>',app.scroll_dashboard,add='+')
    app.sections={name:tk.Frame(app.content,bg=BG) for name in app.page_titles}
    app.section_open={name:False for name in app.sections};app.current_page=None
    app.detail_groups={};app.export_feedback=tk.StringVar(master=root)
    app.stat_vars={key:tk.StringVar(master=root,value='—' if key=='rate' else '0')
                   for key in ('catches','rate','failures','unknown','stages')}
    app.active_time_var=tk.StringVar(master=root,value='00:00')
    app.failure_caption=tk.StringVar(master=root,value='0 confirmed failures')
    app.stock_vars={kind:tk.StringVar(master=root,value='—') for kind in ('bait','food','water')}
    app.rod_text=tk.StringVar(master=root,value='Slot '+app.supplies.rod if app.supplies.rod else 'Unavailable')
    app.need_vars={kind:tk.StringVar(master=root,value='—') for kind in ('hunger','thirst')};app.need_bars={}
    app.rod_check_text=tk.StringVar(master=root,value='Next inventory check: startup')
    app.settings_feedback=tk.StringVar(master=root,value='Settings save automatically.')
    app.session_feedback=tk.StringVar(master=root,value='Session persistence starts when launched normally.')
    app.run_controls=[];app.supply_entries=[];app.rod_slot_checks=[];app.discord_controls=[]

    overview=app.sections['overview']
    run=card(overview);run.pack(fill='x',pady=(0,14))
    controls=row(run,(14,14));copy=tk.Frame(controls,bg=SURFACE);copy.pack(side='left',fill='x',expand=True)
    app.run_title=tk.StringVar(master=root,value='Ready for your next catch')
    app.run_detail=tk.StringVar(master=root,value='Focus FiveM, then press Z to start.')
    label(copy,var=app.run_title,font=('Segoe UI',11,'bold')).pack(anchor='w')
    label(copy,var=app.run_detail,muted=True,font=('Segoe UI',9),wrap=True).pack(fill='x',pady=(4,0))
    app.pause_button=ttk.Button(controls,text='Pause',style='Secondary.TButton',command=app.pause)
    app.pause_button.pack(side='right',padx=(8,0))
    app.start_button=ttk.Button(controls,text='Start fishing',style='Primary.TButton',command=app.schedule_start)
    app.start_button.pack(side='right',padx=(12,0))
    metrics=tk.Frame(overview,bg=BG);metrics.pack(fill='x',pady=(0,14))
    top=tk.Frame(metrics,bg=BG);top.pack(fill='x',pady=(0,12))
    label(top,'This session',bg=BG,font=('Segoe UI',11,'bold')).pack(side='left')
    app.reset_session_button=ttk.Button(top,text='Reset session',style='Quiet.TButton',command=app.reset_session)
    app.reset_session_button.pack(side='right')
    grid=tk.Frame(metrics,bg=BG);grid.pack(fill='x')
    for column,(title,var) in enumerate((('Confirmed catches',app.stat_vars['catches']),('Success rate',app.stat_vars['rate']),('Active time',app.active_time_var))):
        grid.columnconfigure(column,weight=1,uniform='metric')
        group=tk.Frame(grid,bg=BG);group.grid(row=0,column=column,sticky='new',padx=(0,18))
        label(group,title,bg=BG,muted=True,font=('Segoe UI',9)).pack(anchor='w')
        label(group,var=var,bg=BG,font=('Segoe UI',25,'bold')).pack(anchor='w',pady=(4,0))
        if column==1:label(group,var=app.failure_caption,bg=BG,muted=True,font=('Segoe UI',9)).pack(anchor='w')
    app.session_detail=tk.StringVar(master=root,value='— catches/hr · Last catch —')
    label(metrics,var=app.session_detail,bg=BG,muted=True,font=('Segoe UI',9)).pack(fill='x',pady=(9,0))
    pair=tk.Frame(overview,bg=BG);pair.pack(fill='x',pady=(0,14))
    for col in range(2):pair.columnconfigure(col,weight=1,uniform='overview')
    progress=card(pair,'Run progress');progress.grid(row=0,column=0,sticky='nsew',padx=(0,8))
    initial_progress,initial_percent=app.journal.progress(app.settings,app.stats)
    app.progress_text=tk.StringVar(master=root,value=initial_progress)
    text(progress,var=app.progress_text,pady=(4,12))
    app.progress_bar=ThinMeter(progress,maximum=100,value=initial_percent)
    app.progress_bar.pack(fill='x',padx=18,pady=(0,12))
    text(progress,'Stock checked at startup and every 4 attempts.',pady=(0,16))
    latest=card(pair,'Last confirmed catch');latest.grid(row=0,column=1,sticky='nsew',padx=(8,0))
    app.last_catch_text=tk.StringVar(master=root,value='No catches yet');app.last_catch_details=tk.StringVar(master=root,value='')
    text(latest,var=app.last_catch_text,muted=False,pady=(5,2)).configure(font=('Segoe UI',16,'bold'))
    text(latest,var=app.last_catch_details,pady=(0,12))
    app.discord_summary=tk.StringVar(master=root,value='Discord notifications off')
    app.storage_summary=tk.StringVar(master=root,value='Session in memory')
    text(latest,var=app.discord_summary,pady=(0,5));text(latest,var=app.storage_summary,pady=(0,16))
    top=tk.Frame(overview,bg=BG);top.pack(fill='x',pady=(0,8))
    label(top,'Current supplies',bg=BG,font=('Segoe UI',11,'bold')).pack(side='left')
    ttk.Button(top,text='Manage supplies ↗',style='Quiet.TButton',command=lambda:app.show_page('supplies')).pack(side='right')
    supplies_summary=card(overview);supplies_summary.pack(fill='x',pady=(0,10))
    grid=row(supplies_summary,(14,10))
    for column,(title,var,kind) in enumerate((('Active rod',app.rod_text,None),('Food · slot 4',app.need_vars['hunger'],'hunger'),('Water · slot 5',app.need_vars['thirst'],'thirst'))):
        grid.columnconfigure(column,weight=1,uniform='needs')
        group=tk.Frame(grid,bg=SURFACE);group.grid(row=0,column=column,sticky='ew',padx=(0,18))
        label(group,title,muted=True,font=('Segoe UI',9)).pack(anchor='w')
        label(group,var=var,font=('Segoe UI',17,'bold')).pack(anchor='w',pady=(4,6))
        if kind:
            bar=ThinMeter(group,maximum=100);bar.pack(fill='x');app.need_bars[kind]=bar
    text(supplies_summary,var=app.rod_check_text,pady=(0,14))

    supplies=app.sections['supplies'];inventory=card(supplies,'Inventory slots');inventory.pack(fill='x',pady=(0,14))
    head=row(inventory,(0,10));label(head,'Choose which slots the helper can use for rods.',muted=True).pack(side='left')
    app.refill_button=ttk.Button(head,text='Recheck rods',style='Secondary.TButton',command=app.refill_rods);app.refill_button.pack(side='right')
    app.rod_slot_vars={slot:tk.BooleanVar(master=root,value=slot in app.settings['rod_slots']) for slot in ('1','2','3')}
    app.rod_availability_vars={slot:tk.StringVar(master=root,value='Check pending') for slot in ('1','2','3')}
    app.slot1_contents=tk.StringVar(master=root,value='Check pending')
    grid=row(inventory)
    for column in range(5):grid.columnconfigure(column,weight=1,uniform='slots')
    for column in range(5):
        slot=str(column+1);group=tk.Frame(grid,bg=SURFACE);group.grid(row=0,column=column,sticky='nw',padx=(0,12))
        label(group,'SLOT '+slot,muted=True,font=('Segoe UI',9)).pack(anchor='w',pady=(0,8))
        label(group,{'1':'Bait / rod','2':'Rod slot','3':'Rod slot','4':'Food','5':'Water'}[slot],font=('Segoe UI',11,'bold')).pack(anchor='w')
        if slot in app.rod_slot_vars:
            label(group,var=app.slot1_contents if slot=='1' else app.rod_availability_vars[slot],muted=True,font=('Segoe UI',9),wraplength=110).pack(anchor='w',pady=(5,4))
            check=checkbox(group,'Use for rod',app.rod_slot_vars[slot]);check.pack(anchor='w');app.rod_slot_checks.append(check)
        else:label(group,var=app.stock_vars['food' if slot=='4' else 'water'],muted=True,font=('Segoe UI',9),wraplength=110).pack(anchor='w',pady=(5,0))
    text(inventory,var=app.rod_check_text,pady=(0,14))
    text(inventory,'Unchecked slots are skipped. Rod priority is 3 → 1 → 2.',pady=(0,16))
    needs=card(supplies,'Food & water');needs.pack(fill='x',pady=(0,14))
    app.needs_enabled=tk.BooleanVar(master=root,value=app.settings['needs_enabled'])
    app.needs_check=checkbox(needs,'Eat and drink automatically when low',app.needs_enabled);app.needs_check.pack(anchor='w',padx=16,pady=(0,8))
    app.hunger_threshold=tk.StringVar(master=root,value=f"{app.settings['hunger_threshold']:g}")
    app.thirst_threshold=tk.StringVar(master=root,value=f"{app.settings['thirst_threshold']:g}")
    grid=row(needs)
    app.supply_entries.extend((field(grid,'Eat at or below',app.hunger_threshold,unit='%'),field(grid,'Drink at or below',app.thirst_threshold,unit='%')))
    text(needs,'Requires fresh HUD readings. Items are used between fishing attempts.',pady=(0,16))

    planner=app.sections['calculator'];plan=card(planner,'Plan your run');plan.pack(fill='x',pady=(0,14))
    app.calculator_vars={key:tk.StringVar(master=root,value=str(app.settings.get(key,default))) for key,default in CALCULATOR_DEFAULTS.items()}
    app.learn_rates=tk.BooleanVar(master=root,value=app.settings.get('learn_rates',True))
    grid=row(plan);app.calculator_bait_entry=column_field(grid,0,'Bait to use',app.calculator_vars['calc_bait'])
    column_field(grid,1,'Extra supplies · %',app.calculator_vars['calc_reserve'])
    grid=row(plan);app.calculator_results={}
    for column,(key,title) in enumerate((('time','Fishing time'),('food','Pack food · slot 4'),('water','Pack water · slot 5'))):
        grid.columnconfigure(column,weight=1,uniform='plan')
        group=tk.Frame(grid,bg=SURFACE);group.grid(row=0,column=column,sticky='ew')
        var=tk.StringVar(master=root,value='—');app.calculator_results[key]=var
        label(group,title,muted=True,font=('Segoe UI',9)).pack(anchor='w');label(group,var=var,font=('Segoe UI',18,'bold')).pack(anchor='w',pady=(5,0))
    app.calculator_detail=tk.StringVar(master=root);text(plan,var=app.calculator_detail,pady=(0,16))
    rates=details(planner,'Consumption rates','rates')
    for key,title,unit in (('calc_bait_per_hour','Bait used per hour','bait/hr'),('calc_food_per_hour','Food per hour','items/hr'),('calc_water_per_hour','Water per hour','items/hr')):
        field(rates,title,app.calculator_vars[key],unit=unit)
    checkbox(rates,'Use learned rates when enough data is available',app.learn_rates).pack(anchor='w',pady=(10,0))
    label(rates,'Assumes one bait per attempt, including failures. Pauses extend the run.',muted=True,wrap=True).pack(fill='x',pady=(10,0))
    targets=card(planner,'Stop targets');targets.pack(fill='x',pady=(0,14))
    text(targets,'Finish the current attempt, then stop at the first target reached.')
    app.target_vars={key:tk.StringVar(master=root,value=str(app.settings.get(key,0))) for key in ('stop_bait','stop_catches','stop_minutes')}
    grid=row(targets)
    for column,(key,title) in enumerate((('stop_bait','Bait / attempts'),('stop_catches','Confirmed catches'),('stop_minutes','Active minutes'))):
        app.run_controls.append(column_field(grid,column,title,app.target_vars[key]))
    app.require_supplies=tk.BooleanVar(master=root,value=app.settings.get('require_supplies',False))
    check=checkbox(targets,'Require enough supplies for the plan before starting',app.require_supplies);check.pack(anchor='w',padx=16,pady=(0,10));app.run_controls.append(check)
    app.overnight_preflight=tk.BooleanVar(master=root,value=app.settings.get('overnight_preflight',True))
    check=checkbox(targets,'Overnight preflight: supplies, rods & stop targets',app.overnight_preflight)
    check.pack(anchor='w',padx=16,pady=(0,10));app.run_controls.append(check)
    app.inventory_space_check=tk.BooleanVar(master=root,value=app.settings.get('inventory_space_check',False))
    check=checkbox(targets,'Check inventory space (servers with a weight limit)',app.inventory_space_check)
    check.pack(anchor='w',padx=16,pady=(0,10));app.run_controls.append(check)
    text(targets,'0 means no limit. Targets use session totals.',pady=(0,16))
    app.preflight_text=tk.StringVar(master=root,value='Supply check pending.');label(planner,var=app.preflight_text,bg=BG,muted=True,wrap=True).pack(fill='x',pady=(0,10))
    app.update_calculator()

    history=app.sections['history'];table=card(history,'Recent catches');table.pack(fill='x',pady=(0,14))
    grid=row(table)
    app.history_tree=ttk.Treeview(grid,columns=('time','result','rod','items'),show='headings',height=9,style='Fishing.Treeview')
    for key,title,width in (('time','Time · UTC',170),('result','Result',105),('rod','Rod slot',70),('items','Items received',310)):
        app.history_tree.heading(key,text=title);app.history_tree.column(key,width=width,minwidth=40,stretch=key=='items')
    scrollbar=ttk.Scrollbar(grid,orient='vertical',command=app.history_tree.yview)
    app.history_tree.configure(yscrollcommand=scrollbar.set);app.history_tree.pack(side='left',fill='both',expand=True);scrollbar.pack(side='right',fill='y')
    app.history_summary=tk.StringVar(master=root,value='No results yet.');text(table,var=app.history_summary)
    actions=row(table,(0,16));ttk.Button(actions,text='Export CSV',style='Secondary.TButton',command=app.export_catches).pack(side='left')
    label(history,var=app.export_feedback,muted=True,bg=BG,wrap=True).pack(fill='x')

    settings=app.sections['settings'];safety=card(settings,'Overnight safety');safety.pack(fill='x',pady=(0,14))
    app.overnight_safety=tk.BooleanVar(master=root,value=app.settings.get('overnight_safety',True))
    check=checkbox(safety,'Pause when fishing becomes unreliable',app.overnight_safety);check.pack(anchor='w',padx=16,pady=(0,8));app.run_controls.append(check)
    text(safety,'Confirmed catch failures keep fishing. Pause after 3 unconfirmed attempts\nor 15 minutes without a confirmed result. Late rewards get 8 seconds.',pady=(0,16))
    discord=card(settings,'Discord notifications');discord.pack(fill='x',pady=(0,14))
    text(discord,'One live catch card, with separate attention alerts.')
    app.discord_vars={key:tk.BooleanVar(master=root,value=app.settings.get(key,default)) for key,default in DISCORD_DEFAULTS.items() if key!='discord_url'}
    check=checkbox(discord,'Enable Discord notifications',app.discord_vars['discord_enabled']);check.pack(anchor='w',padx=16,pady=(0,8));app.discord_controls.append(check)
    app.discord_url=tk.StringVar(master=root,value=app.settings.get('discord_url',''))
    label(discord,'Webhook URL',muted=True).pack(anchor='w',padx=18,pady=(3,6))
    controls=row(discord)
    app.discord_entry=ttk.Entry(controls,textvariable=app.discord_url,show='•',style='Timing.TEntry');app.discord_entry.pack(side='left',fill='x',expand=True,padx=(0,8))
    app.discord_show=tk.BooleanVar(master=root,value=False)
    checkbox(controls,'Show',app.discord_show,app.reveal_discord).pack(side='left',padx=(0,8))
    app.discord_test=ttk.Button(controls,text='Send test',style='Secondary.TButton',command=app.test_discord);app.discord_test.pack(side='left')
    app.discord_controls.extend((app.discord_entry,app.discord_test))
    grid=row(discord)
    for column in range(2):grid.columnconfigure(column,weight=1)
    for index,(key,caption) in enumerate((('discord_live','Update one live catch card'),('discord_catches','Catches & item details'),('discord_supplies','Supplies & full inventory'),('discord_rods','Rod-slot availability'),('discord_errors','Errors & safety pauses'),('discord_complete','Run completion'),('discord_health','Periodic live health card'))):
        check=checkbox(grid,caption,app.discord_vars[key]);check.grid(row=index//2,column=index%2,sticky='w',pady=3);app.discord_controls.append(check)
    app.discord_status=tk.StringVar(master=root,value=app.notifier.snapshot());text(discord,var=app.discord_status,pady=(0,10))
    text(discord,'Encrypted for your Windows account. Live card edits do not create new phone notifications.',pady=(0,16))
    remote=details(settings,'Discord remote controls','remote')
    app.remote_vars={key:(tk.BooleanVar(master=root,value=app.settings.get(key,default)) if type(default) is bool
                        else tk.StringVar(master=root,value=app.settings.get(key,default))) for key,default in REMOTE_DEFAULTS.items()}
    check=checkbox(remote,'Enable Discord controls (bot required)',app.remote_vars['remote_enabled']);check.pack(anchor='w',pady=(0,8));app.discord_controls.append(check)
    label(remote,'Bot token · encrypted for your Windows account',muted=True).pack(anchor='w',pady=(4,6))
    app.remote_token_entry=ttk.Entry(remote,textvariable=app.remote_vars['remote_token'],show='•',style='Timing.TEntry')
    app.remote_token_entry.pack(fill='x',pady=(0,10));app.discord_controls.append(app.remote_token_entry)
    for key,caption in (('remote_user_id','Your Discord user ID'),('remote_guild_id','Server ID'),('remote_channel_id','Control channel ID')):
        app.discord_controls.append(field(remote,caption,app.remote_vars[key],width=23))
    for key,caption in (('remote_focus','Allow remote resume to focus a previously verified FiveM window'),('remote_screenshots','Allow /fishing screen to send requested FiveM snapshots')):
        check=checkbox(remote,caption,app.remote_vars[key]);check.pack(anchor='w',pady=(8,0));app.discord_controls.append(check)
    controls=tk.Frame(remote,bg=SURFACE);controls.pack(fill='x',pady=(12,8))
    ttk.Button(controls,text='Connect bot',style='Secondary.TButton',command=app.connect_remote).pack(side='left')
    ttk.Button(controls,text='Disconnect',style='Secondary.TButton',command=app.remote.disconnect).pack(side='left',padx=(8,0))
    app.remote_status=tk.StringVar(master=root,value=app.remote.snapshot());label(remote,var=app.remote_status,muted=True,wrap=True).pack(fill='x',pady=(0,8))
    label(remote,'Commands: /fishing status · pause · resume · screen\nOnly the configured account/server/channel can use them. Screen is a requested snapshot, not video. Setup: Discord Remote Setup.md.',muted=True,wrap=True).pack(fill='x',pady=(0,8))
    advanced=details(settings,'Advanced timing & recovery','advanced')
    app.item_wait=tk.StringVar(master=root,value=f"{app.settings['item_wait']:g}")
    app.catch_delay=tk.StringVar(master=root,value=f"{app.settings['catch_delay']:g}")
    app.wait_limit=tk.StringVar(master=root,value=f"{app.settings['wait_limit']:g}")
    app.supply_entries.append(field(advanced,'Item animation wait',app.item_wait,unit='sec'))
    app.timing_entry=field(advanced,'Delay after minigame',app.catch_delay,unit='sec')
    app.supply_entries.append(field(advanced,'Wait recovery after',app.wait_limit,unit='sec'))
    checkbox(advanced,'Show tracking overlay in game',app.overlay_enabled,app.toggle_overlay).pack(anchor='w',pady=(10,0))
    app.reliability_vars={}
    for key,caption in (('transient_recovery','Retry temporary OCR/HUD failures within fixed limits'),('failure_snapshots','Save local circle-only failure snapshots')):
        var=tk.BooleanVar(master=root,value=app.settings.get(key,True));app.reliability_vars[key]=var
        check=checkbox(advanced,caption,var);check.pack(anchor='w',pady=(8,0));app.run_controls.append(check)
    app.inventory_reserve=tk.StringVar(master=root,value=f"{app.settings.get('inventory_reserve_kg',5):g}")
    app.health_minutes=tk.StringVar(master=root,value=str(app.settings.get('health_minutes',15)))
    app.run_controls.append(field(advanced,'Minimum inventory headroom',app.inventory_reserve,unit='kg'))
    app.run_controls.append(field(advanced,'Health update interval',app.health_minutes,unit='min'))
    label(advanced,'OCR/HUD recovery is limited to two attempts of each type per ten minutes.\nHeadroom checks cannot predict the weight of future catches.',muted=True,wrap=True).pack(fill='x',pady=(8,0))
    label(advanced,'App updates',font=('Segoe UI',11,'bold')).pack(anchor='w',pady=(16,8))
    app.update_feed=tk.StringVar(master=root,value=app.settings.get('update_feed_url',''))
    app.update_startup=tk.BooleanVar(master=root,value=app.settings.get('update_check_startup',True))
    label(advanced,'HTTPS release-feed URL',muted=True).pack(anchor='w',pady=(0,6))
    ttk.Entry(advanced,textvariable=app.update_feed,style='Timing.TEntry').pack(fill='x',pady=(0,8))
    checkbox(advanced,'Check for updates when the app opens',app.update_startup).pack(anchor='w')
    controls=tk.Frame(advanced,bg=SURFACE);controls.pack(fill='x',pady=(10,8))
    ttk.Button(controls,text='Check for updates',style='Secondary.TButton',command=app.check_updates).pack(side='left')
    app.update_install_button=ttk.Button(controls,text='Update and restart',style='Secondary.TButton',command=app.install_update,state='disabled')
    app.update_install_button.pack(side='left',padx=(8,0))
    app.update_feedback=tk.StringVar(master=root,value=app.updater.state)
    label(advanced,var=app.update_feedback,muted=True,wrap=True).pack(fill='x',pady=(0,8))
    label(settings,var=app.settings_feedback,muted=True,bg=BG,wrap=True).pack(fill='x',pady=(0,10))

    diagnostic=app.sections['diagnostics']
    app.tracking_card=card(diagnostic,'Circle tracking')
    app.debug_canvas=tk.Canvas(app.tracking_card,width=145,height=136,bg=SURFACE,highlightthickness=0)
    app.debug_canvas.pack(side='left',padx=(12,16),pady=14)
    label(app.tracking_card,'Blue arc → target\nWhite marker → timing\nE is timed automatically.',muted=True).pack(side='left',pady=20)
    draw_tracking(app.debug_canvas,72,68,47,None,None)
    app.diagnostic_summary=card(diagnostic,'Recognition & session');app.diagnostic_summary.pack(fill='x',pady=(0,14))
    grid=row(app.diagnostic_summary)
    for column,(key,title) in enumerate((('failures','Failures'),('unknown','Unconfirmed'),('stages','Confirmed stages'))):
        grid.columnconfigure(column,weight=1,uniform='diagnostic')
        group=tk.Frame(grid,bg=SURFACE);group.grid(row=0,column=column,sticky='ew')
        label(group,title,muted=True,font=('Segoe UI',9)).pack(anchor='w');label(group,var=app.stat_vars[key],font=('Segoe UI',20,'bold')).pack(anchor='w',pady=(5,0))
    app.stats_detail=tk.StringVar(master=root);text(app.diagnostic_summary,var=app.stats_detail)
    app.debug_text=tk.StringVar(master=root,value='No active circle.');text(app.diagnostic_summary,var=app.debug_text)
    text(app.diagnostic_summary,var=app.session_feedback,pady=(0,16))
    report=card(diagnostic,'Troubleshooting');report.pack(fill='x',pady=(0,14))
    text(report,'Reports stay local and remove webhook secrets.')
    controls=row(report,(0,16));ttk.Button(controls,text='Export report',style='Secondary.TButton',command=app.export_diagnostics).pack(side='left')
    text(report,var=app.export_feedback,pady=(0,16))
    app.snapshot_feedback=tk.StringVar(master=root,value='Failure snapshots save locally; images are never sent to Discord.')
    text(report,var=app.snapshot_feedback,pady=(0,8))
    controls=row(report,(0,16));ttk.Button(controls,text='Open failure snapshots',style='Secondary.TButton',command=app.open_snapshots).pack(side='left')
    app.tracking_visible=False
    app.show_page('overview');app.fit_dashboard()
