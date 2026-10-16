import argparse,hashlib,json,re
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--repository',required=True);p.add_argument('--exe',default='dist/FishingHelper.exe');p.add_argument('--output',default='dist/latest.json');p.add_argument('--tag',required=True)
a=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',a.repository):raise ValueError('Invalid repository')
code=Path('app/fishing_helper.py').read_text(encoding='utf-8')
v=re.search(r"APP_VERSION = '([0-9.]+)'",code)[1]
channel='beta' if a.tag.endswith('-beta') else 'stable'
if a.tag!='v'+v+('-beta' if channel=='beta' else ''):raise ValueError('Tag must match APP_VERSION')
exe=Path(a.exe);digest=hashlib.sha256()
with exe.open('rb') as stream:
    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
feed=dict(schema=1,product='FishingHelper',version=v,url=f'https://github.com/{a.repository}/releases/download/{a.tag}/FishingHelper.exe',filename='FishingHelper.exe',size=exe.stat().st_size,sha256=digest.hexdigest(),notes=(Path('RELEASE_NOTES.md').read_text(encoding='utf-8')[:4000] if Path('RELEASE_NOTES.md').is_file() else f'Fishing Helper {v}'),channel=channel,publisher=__import__('os').environ.get('FISHING_SIGNER',''))
Path(a.output).write_text(json.dumps(feed,indent=2),encoding='utf-8')
