# Discord setup for Fishing Helper 4.4

Remote commands and private game snapshots use a separate bot. See **Discord Remote Setup.md**; the webhook below continues handling notifications.

1. In Discord, open Server Settings > Integrations > Create Webhook. Choose the text channel and copy its Webhook URL. These are [Discord's documented steps](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks).
2. In Fishing Helper, open Settings and use the Discord section. Paste the URL into the masked field. It saves encrypted for this Windows account; no token is written into the debug log.
3. Enable Discord notifications. Keep Update one live catch card checked. Select the catch, supply, rod and error alerts you want.
4. Click Send test and check the channel. The test button sends a separate test message; the live catch card appears after the first confirmed catch. No webhook has been configured by Codex and no real messages were sent during development.
5. Set the channel notification override to All and enable mobile push for phone alerts. Make sure the server/channel is not muted. See [Discord notification settings](https://support.discord.com/hc/en-us/articles/215253258-Notifications-Settings-101). Live edits do not act as fresh-message notifications; supply and rod alerts are new messages.

Version 4.2 labels combined preflight shortages as **Pre-run check**, instead of incorrectly calling every summary containing the word bait a bait problem. Inventory-space enforcement is separately optional and defaults off.

Version 4.1 adds **Periodic live health card** in Settings. This edits a separate message every 15 minutes by default, with current state, last successful catch, supplies and exact stop reason. Pauses/completion update it immediately. The interval is configurable under Advanced timing & recovery. It uses the same webhook and has its own persisted message ID; resetting the session updates both cards. Live health edits do not create fresh phone alerts.

Version 4.0 uses a five-consecutive-confirmed-failure limit for Overnight safety. Its pause alert remains a separate message. Level-up text is excluded from bonus items; readable extra items such as Copper Coil still join the fish card.

The live card uses this layout:

> 🐟 **Last catch: 1× Anchovy**
> Bait left: 132 (last inventory check)
> Food left: 9 (last inventory check)
> Session catches: 42
> Time caught: 12:34:00 AM EDT

Your slots: Artificial Bait in 1, rods in 2/3, food in 4, water in 5. Rod priority is 3 then 2. Counts refresh during the existing startup/four-attempt checks. The helper does not open inventory after every catch. Verified food/water use reduces the cached count by one and labels it estimated until the next check. Unknown quantities show a dash. The supplied bait example reads 132; that number is a test result, not hardcoded stock.

Catch cards wait up to eight seconds for reward OCR. Separate readable fish and extra-item ADDED cards join the same catch: for example, Last catch: 1× Anchovy · 2× Rubber. The example is illustrative. An extra item alone does not confirm a fish or increment Session catches. An associated late item edits the latest catch card; it cannot replace a newer catch. If the first POST has uncertain delivery and no message ID, a late item does not silently retry that POST. Version 3.9 also protects an update already queued while that POST is in flight, and the guard survives unrelated test/alert status changes. A live POST without a returned ID stays unconfirmed. A newer catch or an explicit reset may create a new card; the uncertain request itself is not replayed. They include recognized species and quantities associated with a confirmed catch; unreadable names or quantities are labelled. Inventory-based silent rod loss is reported as unavailable; an explicit break notice is reported as a break. Food/water emptiness produces an alert when inventory confirms it, once per empty state; a positive refill rearms the alert. Quantities omitted from a newer confirmation retain their cached values labelled estimated. Failure to restore a need is reported as needs attention, rather than asserted to be an empty slot. Empty bait or a recognized no-bait message pauses fishing. A late failure correction gets a separate correction message.

One live catch message is maintained per saved session and destination in version 3.8. The session saves its message ID and a hash of the destination; restarting restores the same card and counters paused. Older releases did not save this ID and cannot transfer their current card automatically. Changing alert preferences preserves the live card. A deleted live card is recreated on the next catch. Changing destinations discards queued messages for the old destination. Pending notifications are not replayed after restart; later catches update the restored card with current totals.

Enable **Run completion alerts** to receive a separate message when a bait/attempt, catch or time target is reached. Targets are configured in Planner and use session totals. The current attempt finishes before stopping. Supply-check warnings appear in Planner; enabling the mandatory pre-run supply check pauses and sends the applicable attention alert when stock is short or cannot be confirmed.

Pause fishing, wait for cleanup, then use **Reset session** on Overview. It clears the local session counters and time, removes pending old-session updates, and edits the same live card to **Session catches: 0**, **Last catch: —**, and **Time caught: —**. Cached bait/food quantities are retained. The next catch updates that same message with a count of 1. If no live card exists, reset creates one; separate-message mode posts a new reset card. Enable notifications for the Discord update. A reset can finish locally while Discord delivery is pending or unconfirmed; check delivery status in Settings. An initial catch POST already in flight can finish, after which reset edits its returned message ID. Existing rate limits still apply.

Keep the PC awake, online, and the helper running. Only item/status text is posted; no screenshots or general game chat are uploaded. Delivery counters appear below the URL. Network errors stay off the fishing thread. A timeout may leave delivery unconfirmed; uncertain requests are not replayed. Queues are bounded, newer live updates replace older queued ones, and alerts have priority. Closing cancels queued deliveries; an in-flight request may finish.

Tests use local mock responses for POST/PATCH, rate limits, deletion/recreation, queue priority, token redaction and encrypted persistence. Actual Discord delivery requires your URL and Send test. Live overnight server behavior remains unverified.

If Send test works but catches do not appear, check the recognition line below the webhook field. Confirmed catches must increase; the live card is sent after the attempt ends and allows up to eight seconds to collect reward items. A zero confirmed count means no catch evidence reached the counter. Version 2.8 separates hunger/thirst OCR from reward capture and checks reward cards more frequently after E presses. Diagnostics also log per-attempt results and reward scan/card/event totals, without the webhook URL.

Version 2.9 closes a positively confirmed catch even if the old circle persists, so the live card can send. Supply counts are copied before inventory closes instead of disappearing before reporting. More fish labels and popup sizes are supported; unreadable cards receive a contrast retry. The `reward_ocr` log entry contains only local cropped-popup text, never the URL. An unconfirmed attempt still does not create a catch notification. Changing destinations interrupts waits for the old destination; rate limits remain respected for the same destination.

API references: [Webhook execution and editing](https://docs.discord.com/developers/resources/webhook), [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits).
