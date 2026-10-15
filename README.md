# Fishing Helper

Windows desktop helper with automatic circle recognition, session history, supplies/planning, Discord notification cards, optional account-restricted Discord controls, and verified in-app updates.

Download `FishingHelper.exe` from the latest GitHub release. Configure the helper for your game before leaving it unattended. Discord commands require your own bot; see `Discord Remote Setup.md`. No credentials are included.

## In-app updates

In Settings > Advanced timing & recovery > App updates, enter:

```
https://github.com/Bray12541/fishing-helper/releases/latest/download/latest.json
```

This address is configured by default. Startup checks show a header notice when a newer release is available. Use Check for updates, then Update and restart while fishing is paused. Downloads must match the published size and SHA-256 checksum; the Windows helper waits for the app to exit, keeps the prior executable as `.previous`, replaces the app, and restarts it. Only trust a feed controlled by the app publisher.

Confirmed catch failures are counted and the helper keeps fishing after the normal delay. Unknown results, stalled stages, unreadable supplies and lost focus still have separate checks. Recent confirmed failed casts count as activity for the 15-minute inactivity check.

## Build on Windows

Install Python 3.14 and Node.js 24, then:

```powershell
python -m pip install -r requirements.txt
npm ci --prefix app/hud-ocr
python -m PyInstaller --noconfirm fishing.spec
```

The EXE is in `dist`. Saved settings, bot tokens, webhook credentials, logs, session history and private OCR screenshots are excluded from the repository and binary. The packaged test mode can accept an external private fixture directory with `--self-test --fixtures PATH --report REPORT.json`; those fixtures are not public release assets.

## Publish an update

Update `APP_VERSION` in `app/fishing_helper.py` and `fishing_version.txt`, commit the change, then push a matching tag such as `v4.5`. The release workflow builds the Windows app, generates its checksum feed, and publishes `FishingHelper.exe` and `latest.json` as release assets. The workflow uses the repository's GitHub Actions token; no Discord token or personal GitHub token belongs in repository files.

The updater checks the latest published version; it does not invent or build new fixes. The publisher must release a new version before users see an update. Automated/native offline checks do not guarantee uninterrupted live gameplay.
