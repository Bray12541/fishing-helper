"""Opt-in Discord bot controls. No arbitrary commands, capture or input here."""
import asyncio,base64,concurrent.futures,io,queue,re,threading,time
from collections import OrderedDict
from dataclasses import dataclass,field
from fishing_discord import _crypt

REMOTE_DEFAULTS=dict(remote_enabled=False,remote_token='',remote_user_id='',remote_guild_id='',
                     remote_channel_id='',remote_focus=False,remote_screenshots=True)
ACTIONS={'status':'Show fishing status and the latest pause reason',
         'pause':'Pause fishing and cancel any scheduled start',
         'resume':'Retry fishing using the normal focus and supply checks',
         'screen':'Show a private snapshot of the foreground FiveM window'}

def remote_settings(settings):
    values={key:settings.get(key,default) for key,default in REMOTE_DEFAULTS.items()}
    for key in ('remote_enabled','remote_focus','remote_screenshots'):
        if type(values[key]) is not bool:raise ValueError('Discord controls: use checkbox values.')
    for key in ('remote_user_id','remote_guild_id','remote_channel_id'):
        value=values[key]
        if not isinstance(value,str):raise ValueError('Discord controls: paste numeric user, server and channel IDs.')
        values[key]=value.strip()
        if values['remote_enabled'] and (not re.fullmatch(r'[1-9]\d{14,19}',values[key]) or int(values[key])>2**64-1):
            raise ValueError('Discord controls: paste your user ID, server ID and channel ID before enabling.')
        if len(values[key])>20:raise ValueError('Discord controls: invalid ID length.')
    token=values['remote_token']
    if not isinstance(token,str):raise ValueError('Discord controls: paste a bot token in the app.')
    values['remote_token']=token.strip()
    if values['remote_enabled'] and not re.fullmatch(r'[A-Za-z0-9._-]{20,300}',values['remote_token']):
        raise ValueError('Discord controls: paste a bot token, rather than a webhook URL.')
    if len(token)>300:raise ValueError('Discord controls: invalid bot token length.')
    return values

def protect_token(token):
    return 'dpapi:'+base64.b64encode(_crypt(token.encode('utf-8'),True)).decode('ascii')

def unprotect_token(value):
    if not isinstance(value,str) or not value.startswith('dpapi:'):raise ValueError('Invalid saved bot credential.')
    return _crypt(base64.b64decode(value[6:],validate=True),False).decode('utf-8')

@dataclass
class RemoteReply:
    text:str
    image:bytes|None=None

@dataclass
class RemoteRequest:
    action:str
    user:str
    guild:str
    channel:str
    identifier:str
    generation:int
    created:float=field(default_factory=time.monotonic)
    future:concurrent.futures.Future=field(default_factory=concurrent.futures.Future)

class DiscordControls:
    def __init__(self):
        self.lock=threading.RLock();self.config=dict(REMOTE_DEFAULTS);self.generation=0
        self.queue=queue.Queue(maxsize=16);self.seen=OrderedDict();self.cooldowns={}
        self.thread=None;self.loop=None;self.client=None;self.stop=threading.Event()
        self.state='Discord controls are off.';self.last_action='';self.closed=False

    def snapshot(self):
        with self.lock:return self.state

    def configure(self,settings):
        config=remote_settings(settings)
        with self.lock:
            if config==self.config:return
            self.config=config;self.generation+=1;self.seen.clear();self.cooldowns.clear()
            self.stop.set();self.reject_pending('Discord controls settings changed; send a new command.')
            self.state='Ready to connect.' if config['remote_enabled'] else 'Discord controls are off.'

    def authorized(self,user,guild,channel,generation=None):
        with self.lock:
            return (not self.closed and self.config['remote_enabled'] and
                    (generation is None or generation==self.generation) and
                    str(user)==self.config['remote_user_id'] and str(guild)==self.config['remote_guild_id'] and
                    str(channel)==self.config['remote_channel_id'])

    def submit(self,action,user,guild,channel,identifier,issued_at=None,generation=None):
        with self.lock:
            if not self.authorized(user,guild,channel,generation):return RemoteReply('This account, server or channel is not authorized.')
            if action not in ACTIONS:return RemoteReply('Unsupported fishing command.')
            if type(issued_at) not in (int,float) or not -5<=time.time()-issued_at<=30:return RemoteReply('Command expired. Send a fresh command.')
            if not isinstance(identifier,str) or not identifier.isdigit() or len(identifier)>30:return RemoteReply('Invalid command ID.')
            if identifier in self.seen:return RemoteReply('This command was already received; it will not run twice.')
            if action=='screen' and not self.config['remote_screenshots']:return RemoteReply('Screenshot uploads are disabled in the helper.')
            now=time.monotonic();interval=10 if action=='screen' else 3 if action=='resume' else 1
            if now-self.cooldowns.get(action,-float('inf'))<interval:return RemoteReply('Please wait a moment before repeating that command.')
            request=RemoteRequest(action,str(user),str(guild),str(channel),identifier,self.generation)
            try:self.queue.put_nowait(request)
            except queue.Full:return RemoteReply('Control queue is busy. Try again shortly.')
            self.seen[identifier]=True
            while len(self.seen)>256:self.seen.popitem(last=False)
            self.cooldowns[action]=now
            return request

    def drain(self,handler):
        for _ in range(4):
            try:request=self.queue.get_nowait()
            except queue.Empty:return
            try:
                if not request.future.set_running_or_notify_cancel():continue
                if not self.authorized(request.user,request.guild,request.channel,request.generation):
                    result=RemoteReply('Discord controls were disabled or changed. No action was taken.')
                elif time.monotonic()-request.created>10:result=RemoteReply('Command expired while the app was busy. No action was taken.')
                elif request.action=='screen' and not self.config['remote_screenshots']:
                    result=RemoteReply('Screenshot uploads are disabled.')
                else:
                    try:result=handler(request.action)
                    except Exception:result=RemoteReply('The helper could not complete that command. Check /fishing status.')
                    self.last_action=request.action
                request.future.set_result(result)
            finally:self.queue.task_done()

    def reject_pending(self,reason):
        while True:
            try:request=self.queue.get_nowait()
            except queue.Empty:return
            if not request.future.done():
                try:request.future.set_result(RemoteReply(reason))
                except concurrent.futures.InvalidStateError:pass
            self.queue.task_done()

    def connect(self):
        with self.lock:
            if self.closed or not self.config['remote_enabled']:return False
            if self.thread and self.thread.is_alive():
                self.state='Previous Discord connection is closing. Click Connect again shortly.';return False
            self.stop=threading.Event();config=dict(self.config);generation=self.generation;stop=self.stop
            self.state='Connecting Discord controls…'
            self.thread=threading.Thread(target=self.run,args=(config,generation,stop),daemon=True,name='fishing-remote')
            try:self.thread.start()
            except RuntimeError:self.thread=None;self.state='Could not start Discord controls.';return False
            return True

    def set_state(self,text,generation):
        with self.lock:
            if generation==self.generation and not self.closed:self.state=text

    def disconnect(self):
        with self.lock:
            self.generation+=1;self.stop.set();self.reject_pending('Discord controls disconnected. No action was taken.')
            self.state='Discord controls disconnected.'

    def close(self):
        self.disconnect();self.closed=True

    def run(self,config,generation,stop):
        try:asyncio.run(self.session(config,generation,stop))
        except Exception:self.set_state('Discord connection failed. Check the bot token, installation and server/channel IDs.',generation)

    async def session(self,config,generation,stop):
        client=make_client(self,config,generation)
        self.loop=asyncio.get_running_loop();self.client=client
        async def stopping():
            while not stop.is_set():await asyncio.sleep(.1)
        if stop.is_set():await client.close();return
        start=asyncio.create_task(client.start(config['remote_token'],reconnect=True));watch=asyncio.create_task(stopping())
        try:
            done,_=await asyncio.wait((start,watch),return_when=asyncio.FIRST_COMPLETED)
            if start in done:
                await start
                self.set_state('Discord controls disconnected. Click Connect to retry.',generation)
        finally:
            start.cancel();watch.cancel()
            await asyncio.gather(start,watch,return_exceptions=True)
            try:await asyncio.wait_for(client.close(),8)
            finally:self.client=None;self.loop=None

    async def interaction(self,interaction,action,generation):
        # Gateway authentication comes from discord.py; do not trust message text.
        if not self.authorized(interaction.user.id,interaction.guild_id,interaction.channel_id,generation):
            await interaction.response.send_message('Not authorized for this helper.',ephemeral=True);return
        await interaction.response.defer(ephemeral=True,thinking=True)
        created=interaction.created_at.timestamp()
        request=self.submit(action,interaction.user.id,interaction.guild_id,interaction.channel_id,str(interaction.id),created,generation)
        if isinstance(request,RemoteReply):reply=request
        else:
            try:reply=await asyncio.wait_for(asyncio.wrap_future(request.future),12)
            except asyncio.TimeoutError:
                request.future.cancel();reply=RemoteReply('Helper response timed out. Check status before sending another action.')
        import discord
        files=[discord.File(io.BytesIO(reply.image),filename='fivem.jpg')] if reply.image else []
        await interaction.followup.send(reply.text[:1900],files=files,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

def make_client(controller,config,generation):
    import discord
    from discord import app_commands
    class FishingClient(discord.Client):
        async def setup_hook(self):
            self.tree=app_commands.CommandTree(self)
            group=app_commands.Group(name='fishing',description='Your Fishing Helper controls',guild_only=True)
            def callback_for(action):
                async def callback(interaction:discord.Interaction):
                    await controller.interaction(interaction,action,generation)
                return callback
            for action,description in ACTIONS.items():
                group.add_command(app_commands.Command(name=action,description=description,callback=callback_for(action)))
            guild=discord.Object(id=int(config['remote_guild_id']))
            self.tree.add_command(group,guild=guild)
            self.tree.on_error=self.command_error
            if controller.authorized(config['remote_user_id'],config['remote_guild_id'],config['remote_channel_id'],generation):
                # Upsert only our command. Bulk sync would delete other guild
                # commands when the user reuses a bot with existing commands.
                await self.http.upsert_guild_command(self.application_id,guild.id,group.to_dict(self.tree))
        async def command_error(self,interaction,error):
            # No exceptions/tokens are copied into Discord or application logs.
            try:
                if interaction.response.is_done():await interaction.followup.send('Command failed. Check the helper connection and try status.',ephemeral=True)
                else:await interaction.response.send_message('Command failed.',ephemeral=True)
            except Exception:pass
        async def on_ready(self):controller.set_state('Connected · /fishing status, pause, resume, screen',generation)
        async def on_disconnect(self):controller.set_state('Discord disconnected; reconnecting when possible…',generation)
        async def on_error(self,*args,**kwargs):controller.set_state('Discord event failed; check connection and retry status.',generation)
    return FishingClient(intents=discord.Intents.none(),allowed_mentions=discord.AllowedMentions.none(),max_messages=None)
