# Discord remote controls — Fishing Helper 4.3

Your webhook keeps its catch card, health card and alerts. Remote commands use a separate bot running inside Fishing Helper. Controls stay off until you configure and connect it.

## Create and install the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application, such as Fishing Remote. A dedicated bot is easiest. See [Discord's app setup guide](https://docs.discord.com/developers/quick-start/getting-started).
2. On the **Bot** page, generate/copy its token. Paste it directly into the helper's masked bot-token field. The helper encrypts it for your Windows account. Do not paste it into chat or a channel.
3. Choose **Guild Install**, with **bot** and **applications.commands** scopes. Give the bot **View Channel**, **Send Messages** and **Attach Files** in the control channel. Administrator permission and privileged/message-content intents are not needed.
4. Use the installation link to add it to your server. Leave the app's **Interactions Endpoint URL** empty: this helper receives interactions over Discord's Gateway. If an existing bot uses an endpoint or another service, create a dedicated bot instead.
5. In Discord, enable **User Settings > Advanced > Developer Mode**. Use the Copy ID options to copy **your account's user ID**, the **server ID**, and the **control channel ID**. See [Discord's ID instructions](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID). Use numeric IDs rather than names; the user ID must be yours, not the bot's.

## Connect Fishing Helper

Close the older helper, open **Fishing Helper 4.3.exe**, and expand **Settings > Discord remote controls**.

- Paste the token and the three IDs.
- Check **Enable Discord controls**, then click **Connect bot**.
- Wait for **Connected**. Commands register in that server. The helper updates only its `/fishing` command, preserving other commands belonging to a reused bot.
- Keep **Allow /fishing screen** checked if you want snapshots.
- Optionally enable **Allow remote resume to focus a previously verified FiveM window**. Run fishing locally once so the helper knows that window before you leave. Windows can refuse focus; no fishing key is sent if foreground verification fails.

Enabled controls reconnect when the app starts. **Disconnect** closes the current connection; uncheck **Enable Discord controls** to keep them off across restarts. Fishing runs independently of the bot connection.

No bot credentials were provided during development. No real bot login, command registration or screenshot upload was performed. Complete this setup before expecting the commands to appear.

## Commands from your phone

Use the configured server/channel while signed in as the configured account. Responses are private to you.

| Command | Result |
| --- | --- |
| `/fishing status` | Running/paused state, latest local status/error, catches, failures, unconfirmed results, supply counts and whether FiveM is focused. |
| `/fishing pause` | Uses normal pause, cancels the start countdown and requests key release/worker cleanup. |
| `/fishing resume` | Uses normal start checks. Already running sessions remain running; previous cleanup, F9, focus, supplies and preflight are respected. |
| `/fishing screen` | Sends a current JPEG of the foreground FiveM client area as a private response. Visible overlays may appear. This is a requested snapshot, not live video. |

When an error alert arrives, request **status**, inspect **screen** if needed, then **resume** to retry. Persistent supply or readability errors can pause again. Commands do not refill inventory, change safety/Planner settings, run arbitrary PC commands, restart a crashed game or reopen a closed helper.

## Screenshots

Snapshots are requested manually, stay in memory until sent, and are not written to screenshot files. The app requires foreground FiveM and checks its focus, process and bounds again after capture. Changed views are discarded. Images are limited to 1920×1080 and 4 MB, with a ten-second cooldown. A background/minimized game does not authorize a desktop screenshot. Live video still needs Discord screen sharing. Discord receives the attachment; a private response is not end-to-end encrypted.

## Troubleshooting

- **No commands:** check Connected, the installed server, applications.commands scope, exact IDs, Use Application Commands permission, and the bot's channel permissions. Commands are for the configured server channel, not DMs.
- **Not authorized:** all three IDs must match. Server administrator status does not bypass the account check.
- **Connection failed:** check the bot token, installation, server ID and connectivity; click Connect again. Gateway disconnections reconnect when possible.
- **Expired/timed out:** check status before sending another action. Old, cancelled, duplicate and stale-connection requests do not silently replay.
- **FiveM not focused:** enable the optional verified-window focus setting before leaving, or focus it locally. Other applications do not receive fishing keys.
- **Capture unavailable/blank:** try FiveM in borderless/windowed mode. Results depend on Windows and the game's rendering.
- **No response at all:** the PC, network and helper must remain running. Commands cannot recover a powered-off PC or a helper that has completely crashed.

The bot uses [Discord application commands](https://docs.discord.com/developers/interactions/application-commands) and [discord.py](https://discordpy.readthedocs.io/en/stable/interactions/api.html). The EXE bundles the library. Source users can install `fishing-remote-requirements.txt`; dependency licenses are included under licenses.
