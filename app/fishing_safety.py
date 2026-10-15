"""Conservative unattended-run limits. No capture, keyboard or network access."""
UNKNOWN_LIMIT=3
RESULT_GRACE_SECONDS=8
NO_RESULT_SECONDS=15*60

class OvernightSafety:
    def __init__(self):
        self.started_at=None
        self.first_attempt=0

    def start(self,stats,now):
        with stats.lock:self.first_attempt=stats.next_attempt
        self.started_at=now

    def assess(self,stats,now):
        """Return a pause reason and remaining grace for late reward OCR."""
        if self.started_at is None:return None,0
        with stats.lock:
            rows=[dict(row) for cycle,row in stats.attempts.items() if cycle>self.first_attempt
                  and (row['challenge'] is not None or row['result'] in ('catch','failure'))
                  and row['end'] is not None and row['result'] is not None]
            last_catch=stats.last_catch_at
        tail=rows[-1]['result'] if rows else None
        streak=0
        for row in reversed(rows):
            if row['result']!=tail:break
            streak+=1
        if tail=='unknown' and streak>=UNKNOWN_LIMIT:
            grace=max(0,rows[-1]['end']+RESULT_GRACE_SECONDS-now)
            return 'Overnight safety: three consecutive unconfirmed attempts. Check catch recognition before pressing Z.',grace
        # A confirmed failed cast is still evidence that fishing is progressing.
        # Unknown outcomes and mere cancel prompts do not reset this timer.
        last_failure=max((row['end'] for row in rows if row['result']=='failure' and row['end']<=now),default=self.started_at)
        recent=max(self.started_at,last_catch if last_catch is not None else self.started_at,last_failure)
        if now-recent>=NO_RESULT_SECONDS:
            return 'Overnight safety: no confirmed fishing result for 15 minutes. Check fishing and recognition before pressing Z.',0
        return None,0
