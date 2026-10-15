from pathlib import Path
base=Path(SPECPATH)
assets=base/'app'
data=[(str(assets/name),'.') for name in (
 'fishing-ocr.ps1','fishing-update.ps1','fishing-hud-ocr.cjs','fishing-added-header.png',
 'fishing-hunger-icon.png','fishing-water-icon.png','fishing-slot-tags.png',
 'fishing-logo.png','fishing-logo-small.png','fishing-logo.ico')]
data.append((str(assets/'hud-ocr'),'hud-ocr'))
data.append((str(assets/'licenses'),'licenses'))
data.append((__import__('shutil').which('node'),'node-runtime'))

a=Analysis([str(assets/'fishing_helper.py')],pathex=[str(assets)],binaries=[],
           datas=data,hiddenimports=['fishing_selftest'],
           hookspath=[],hooksconfig={},runtime_hooks=[],excludes=[],noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='Fishing Helper',debug=False,
        bootloader_ignore_signals=False,strip=False,upx=False,console=False,
        disable_windowed_traceback=False,icon=str(assets/'fishing-logo.ico'),
        version=str(base/'fishing_version.txt'))
