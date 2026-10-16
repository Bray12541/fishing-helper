"""Offline update checks. No installers are executed and no EXEs are replaced."""
import hashlib,io,json,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import fishing_updates as u

def binary():
    data=bytearray(512);data[:2]=b'MZ';data[60:64]=(128).to_bytes(4,'little');data[128:132]=b'PE\0\0';return bytes(data)
def manifest(data=None):
    data=binary() if data is None else data
    return dict(product='FishingHelper',schema=1,version='4.5',filename='new.exe',url='https://example.com/new.exe',size=len(data),sha256=hashlib.sha256(data).hexdigest(),notes='Test update')
class Reply(io.BytesIO):pass
class Transport:
    def __init__(self,*values):self.values=list(values);self.calls=[]
    def open(self,request,timeout):self.calls.append(request.full_url);return Reply(self.values.pop(0))
class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);self.c=u.UpdateManager('4.4',self.folder,self.folder/'staging');self.addCleanup(self.c.close)
    def wait(self):
        self.c.thread.join(3);self.assertFalse(self.c.thread.is_alive())
    def test_version_comparison_normalizes_trailing_zeroes(self):
        self.assertEqual(u.version('4.4'),u.version('4.4.0'));self.assertLess(u.version('4.9'),u.version('4.10'))
        self.assertIsNone(u.release({**manifest(),'version':'4.4.0'},'4.4',self.folder))
        self.assertIsNone(u.release({**manifest(),'version':'4.3'},'4.4',self.folder))
    def test_feed_requires_https_without_user_credentials(self):
        for value in ['http://example.com/feed','file:///c:/feed','https://me:secret@example.com/feed','https://example.com/feed#token']:
            with self.assertRaises(ValueError):u.feed_url(value)
        self.assertEqual(u.feed_url('https://example.com/feed.json'),'https://example.com/feed.json')
    def test_redirect_cannot_downgrade_https(self):
        with self.assertRaises(ValueError):u.SecureRedirect().redirect_request(None,None,302,'',{},'http://example.com/app.exe')
    def test_online_release_needs_url_checksum_and_bounded_size(self):
        for change in [dict(sha256='wrong'),dict(size=True),dict(size=0),dict(size=u.MAX_DOWNLOAD+1),dict(url='')]:
            with self.assertRaises(ValueError):u.release({**manifest(),**change},'4.4',self.folder,True)
    def test_local_filename_must_stay_in_release_folder(self):
        for filename in ['../new.exe','C:\\new.exe','sub/new.exe']:
            with self.assertRaises(ValueError):u.release({**manifest(), 'url':'','filename':filename},'4.4',self.folder)
    def test_no_hosting_reports_configuration_needed_without_network(self):
        self.c.opener=MagicMock();self.c.check();self.wait();self.c.opener.open.assert_not_called()
        self.assertIn('need a feed URL',self.c.state)
    def test_https_check_finds_new_version_without_downloading_binary(self):
        transport=Transport(json.dumps(manifest()).encode());self.c.opener=transport
        self.c.check('https://example.com/feed.json');self.wait()
        self.assertEqual(self.c.available['version'],'4.5');self.assertEqual(len(transport.calls),1)
        self.assertFalse((self.folder/'staging').exists())
    def test_invalid_or_oversized_feed_is_not_accepted(self):
        for data in [b'not json',b'x'*65537]:
            self.c.opener=Transport(data);self.c.check('https://example.com/feed');self.wait()
            self.assertIsNone(self.c.available);self.assertIn('Could not check',self.c.state)
    def test_download_is_verified_and_prepared_without_installing(self):
        self.c.opener=Transport(json.dumps(manifest()).encode(),binary())
        self.c.check('https://example.com/feed');self.wait();self.assertTrue(self.c.prepare());self.wait()
        self.assertIsNotNone(self.c.ready);self.assertEqual(Path(self.c.ready).read_bytes(),binary());self.assertFalse(self.c.handed_off)
    def test_bad_checksum_keeps_installed_file(self):
        current=self.folder/'installed.exe';current.write_bytes(b'old app')
        self.c.opener=Transport(json.dumps(manifest()).encode(),b'x'*512)
        self.c.check('https://example.com/feed');self.wait();self.c.prepare();self.wait()
        self.assertIsNone(self.c.ready);self.assertEqual(current.read_bytes(),b'old app');self.assertIn('kept',self.c.state)
    def test_download_cannot_exceed_declared_size(self):
        self.c.opener=Transport(json.dumps(manifest()).encode(),binary()+b'extra')
        self.c.check('https://example.com/feed');self.wait();self.c.prepare();self.wait()
        self.assertIsNone(self.c.ready);self.assertIn('failed',self.c.state)
    def test_local_release_uses_same_validation(self):
        (self.folder/'new.exe').write_bytes(binary());(self.folder/'fishing-release.json').write_text(json.dumps({**manifest(),'url':''}))
        self.c.check();self.wait();self.c.prepare();self.wait();self.assertIsNotNone(self.c.ready)
    def test_not_an_executable_is_rejected_even_with_matching_hash(self):
        raw=b'x'*512;path=self.folder/'bad.exe';path.write_bytes(raw)
        with self.assertRaises(ValueError):u.validate_exe(path,manifest(raw))
    def test_handoff_writes_strict_plan_without_running_installer_in_test(self):
        self.c.opener=Transport(json.dumps(manifest()).encode(),binary());self.c.check('https://example.com/feed');self.wait();self.c.prepare();self.wait()
        target=self.folder/'installed.exe';target.write_bytes(b'old app');script=self.folder/'helper.ps1';script.write_text('# fixture')
        with patch.object(u.subprocess,'Popen') as launch,patch.object(u.subprocess,'CREATE_NO_WINDOW',0,create=True):self.c.handoff(target,script)
        self.assertTrue(self.c.handed_off);self.assertEqual(target.read_bytes(),b'old app')
        args=launch.call_args.args[0];self.assertIn('-PlanPath',args)
        plan=json.loads(Path(args[-1]).read_text());self.assertEqual(plan['target'],str(target.resolve()))
        self.assertEqual(plan['sha256'],manifest()['sha256'])

class Update45Tests(unittest.TestCase):
    setUp=UpdateTests.setUp
    wait=UpdateTests.wait
    def test_stable_rejects_beta_and_beta_uses_dedicated_feed(self):
        with self.assertRaises(ValueError):u.release({**manifest(),'channel':'beta'},'4.4',self.folder,True,'stable')
        self.assertEqual(u.release({**manifest(),'channel':'beta'},'4.4',self.folder,True,'beta')['channel'],'beta')
        self.assertEqual(u.channel_feed(u.DEFAULT_FEED,'beta'),u.BETA_FEED)
        self.assertEqual(u.channel_feed(u.BETA_FEED,'stable'),u.DEFAULT_FEED)
        self.assertEqual(u.channel_feed('https://example.com/feed','beta'),'https://example.com/feed')
    def test_preferences_validate_channel_pin_and_preserve_deferral(self):
        values=u.preferences(dict(update_channel='invalid',update_skip='stable:4.5',update_remind_after=123))
        self.assertEqual(values['update_channel'],'stable');self.assertEqual(values['update_skip'],'stable:4.5');self.assertEqual(values['update_remind_after'],123)
        with self.assertRaises(ValueError):u.preferences(dict(update_publisher='bad'))
    def test_retry_connection_error_then_download_succeeds(self):
        transport=MagicMock();transport.open.side_effect=[OSError('dropped'),Reply(binary())]
        self.c.available=u.release(manifest(),'4.4',self.folder);self.c.opener=transport
        self.assertTrue(self.c.prepare());self.wait()
        self.assertEqual(transport.open.call_count,2);self.assertIsNotNone(self.c.ready)
        self.assertEqual(self.c.details()['progress'],100);self.assertGreater(self.c.details()['speed'],0)
    def test_exhausted_retries_keep_current_app_and_allow_retry(self):
        self.c.available=u.release(manifest(),'4.4',self.folder);self.c.opener=MagicMock();self.c.opener.open.side_effect=OSError('offline')
        self.c.prepare();self.wait();self.assertEqual(self.c.opener.open.call_count,3);self.assertIsNone(self.c.ready)
        self.assertEqual(self.c.phase,'failed');self.assertFalse(self.c.installing)
    def test_cancel_mid_stream_never_exposes_ready_file(self):
        manager=self.c
        class CancelReply(Reply):
            def read(self,count=-1):
                manager.cancel_download();return super().read(count)
        self.c.available=u.release(manifest(),'4.4',self.folder);self.c.opener=MagicMock();self.c.opener.open.return_value=CancelReply(binary())
        self.c.prepare();self.wait();self.assertIsNone(self.c.ready);self.assertEqual(self.c.phase,'cancelled');self.assertFalse(self.c.installing)
        self.assertFalse(list((self.folder/'staging').rglob('new.exe')))
    def test_cancel_ready_download_cleans_staging(self):
        self.c.available=u.release(manifest(),'4.4',self.folder);self.c.opener=Transport(binary());self.c.prepare();self.wait()
        path=Path(self.c.ready);self.assertTrue(self.c.cancel_download());self.assertFalse(path.exists());self.assertIsNone(self.c.ready)
    def test_required_signature_blocks_unsigned_and_wrong_publisher(self):
        pin='A'*40
        with patch.object(u.subprocess,'run',return_value=MagicMock(returncode=0,stdout=json.dumps(dict(status='NotSigned',thumbprint='')))):
            with self.assertRaises(ValueError):u.verify_signature(self.folder/'new.exe',required=True)
        with patch.object(u.subprocess,'run',return_value=MagicMock(returncode=0,stdout=json.dumps(dict(status='Valid',thumbprint='B'*40)))):
            with self.assertRaises(ValueError):u.verify_signature(self.folder/'new.exe',pin)
        with patch.object(u.subprocess,'run',return_value=MagicMock(returncode=0,stdout=json.dumps(dict(status='Valid',thumbprint=pin)))):
            self.assertIn('verified',u.verify_signature(self.folder/'new.exe',pin))
    def test_signature_failure_does_not_offer_install(self):
        self.c.available=u.release(manifest(),'4.4',self.folder);self.c.opener=Transport(binary());self.c.require_signed=True
        with patch.object(u,'verify_signature',side_effect=ValueError('Untrusted')):self.c.prepare();self.wait()
        self.assertIsNone(self.c.ready);self.assertEqual(self.c.phase,'failed')
    def test_startup_ack_requires_correct_token_path_and_version(self):
        path=self.folder/'startup-health.json';token='a'*64
        (self.folder/'plan.json').write_text(json.dumps(dict(health_path=str(path),health_token=token,version='4.5')))
        for wrong_token,wrong_version in [('b'*64,'4.5'),(token,'4.6')]:
            with self.assertRaises(ValueError):u.acknowledge_startup(path,wrong_token,wrong_version)
        self.assertFalse(path.exists());u.acknowledge_startup(path,token,'4.5')
        self.assertEqual(json.loads(path.read_text())['token'],token)
    def test_handoff_contains_health_token_and_unique_stage_path(self):
        self.c.available=u.release(manifest(),'4.4',self.folder);self.c.opener=Transport(binary());self.c.prepare();self.wait()
        target=self.folder/'installed.exe';target.write_bytes(b'old');script=self.folder/'helper.ps1';script.write_text('# mock')
        with patch.object(u.subprocess,'Popen'):self.c.handoff(target,script)
        plan=json.loads((Path(self.c.ready).parent/'plan.json').read_text());self.assertEqual(len(plan['health_token']),64)
        self.assertEqual(Path(plan['health_path']).parent,Path(self.c.ready).parent);self.assertEqual(plan['version'],'4.5')

if __name__=='__main__':unittest.main()
