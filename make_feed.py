import argparse,hashlib,json,re
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--repository',required=True);p.add_argument('--exe',default='dist/FishingHelper.exe');p.add_argument('--output',default='dist/latest.json');p.add_argument('--tag',required=True)
a=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',a.repository):raise ValueError('Invalid repository')
code=Path('app/fishing_helper.py').read_text(encoding='utf-8')
v=re.search(r"APP_VERSION = '([0-9.]+)'",code)[1]
if a.tag!='v'+v:raise ValueError('Tag must match APP_VERSION')
exe=Path(a.exe);digest=hashlib.sha256()
with exe.open('rb') as stream:
    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
feed=dict(schema=1,product='FishingHelper',version=v,url=f'https://github.com/{a.repository}/releases/download/{a.tag}/FishingHelper.exe',filename='FishingHelper.exe',size=exe.stat().st_size,sha256=digest.hexdigest(),notes=f'Fishing Helper {v}')
Path(a.output).write_text(json.dumps(feed,indent=2),encoding='utf-8')
