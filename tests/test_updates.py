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

if __name__=='__main__':unittest.main()
