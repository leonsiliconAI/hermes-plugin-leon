"""Mirror Muse's trigger-only schedule runs to Slack, with a durable outbox."""

import re

TITLE_PREFIX = "Hermes handoff: "
TRIGGER_PROMPT = "Acknowledge this scheduled trigger with the exact text 'Hermes handoff due'. Use no tools and take no other action."
SCHEDULE_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}")
RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


def schedule_request(title, when=None, schedule_id=None):
    title = " ".join(title.split()) if isinstance(title, str) else ""
    if not title or len(title) > 200:
        raise ValueError("Use a short schedule title (1 to 200 characters).")
    timing = " ".join(when.split()) if isinstance(when, str) else ""
    if schedule_id is not None:
        if not isinstance(schedule_id, str) or not SCHEDULE_ID.fullmatch(schedule_id) or timing:
            raise ValueError("To update a trigger, pass its Muse schedule ID without a new time.")
        request = (f'I want to update my existing scheduled task with ID "{schedule_id}" in place. '
                   "Keep its ID, timing, time zone, enabled state, and recurrence exactly as they are.")
    elif not timing or len(timing) > 300:
        raise ValueError("Pass the schedule's time, time zone, and recurrence if any.")
    else:
        request = f"I want to create a scheduled task that fires {timing}."
    return (f'{request}\nSet its title to "{TITLE_PREFIX}{title}". Replace the entire scheduled prompt '
            f'with exactly this text:\n\n{TRIGGER_PROMPT}\n\n'
            "Set up the trigger only. Do not run it now. Do not add any work instructions or actions "
            "to the schedule. Return the schedule ID, title, prompt and timing so I can verify them.")


class ScheduleRuns:
    def __init__(self, connect):
        self.connect = connect
        with connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS muse_schedules (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL, since REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS muse_schedule_runs (
                    schedule_id TEXT NOT NULL, run_id TEXT NOT NULL, due REAL NOT NULL,
                    text TEXT NOT NULL, receipt TEXT, PRIMARY KEY (schedule_id, run_id)
                );
            """)

    def record(self, schedules, runs, now):
        with self.connect() as db:
            for schedule in schedules:
                if not isinstance(schedule, dict):
                    continue
                job = schedule.get("id")
                if not isinstance(job, str) or not SCHEDULE_ID.fullmatch(job):
                    continue
                title = schedule.get("title")
                if (schedule.get("is_heartbeat") or schedule.get("mode") == "heartbeat"
                        or not isinstance(title, str) or not title.startswith(TITLE_PREFIX)):
                    db.execute("DELETE FROM muse_schedules WHERE id=?", (job,))
                    continue
                title = " ".join(title[len(TITLE_PREFIX):].split())[:200]
                if not title:
                    continue
                cadence = schedule.get("cadence")
                key = schedule.get("schedule_key")
                run_once = ((isinstance(cadence, dict) and cadence.get("kind") == "runonce")
                            or (isinstance(key, str) and key.startswith("runonce@")))
                kind = "reminder" if run_once else "routine"
                db.execute("""INSERT INTO muse_schedules VALUES (?, ?, ?, ?)
                              ON CONFLICT(id) DO UPDATE SET title=excluded.title, kind=excluded.kind""",
                           (job, title, kind, now))
            known = {row["id"]: row for row in db.execute("SELECT * FROM muse_schedules")}
            for run in runs:
                if not isinstance(run, dict) or not isinstance(run.get("job_id"), str):
                    continue
                schedule = known.get(run["job_id"])
                run_id, due = run.get("run_id"), run.get("scheduled_for_utc")
                reason = run.get("trigger_reason")
                if (not schedule or not isinstance(reason, str) or not reason or reason in ("retry", "dependency_probe")
                        or not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)
                        or not isinstance(due, (int, float)) or not schedule["since"] <= due <= now):
                    continue
                job = schedule["id"]
                text = f"Muse {schedule['kind']} due: {schedule['title']}\nMuse schedule: {job}\nMuse run: {run_id}"
                db.execute("INSERT OR IGNORE INTO muse_schedule_runs VALUES (?, ?, ?, ?, NULL)",
                           (job, run_id, due, text))

    def pending(self):
        with self.connect() as db:
            return db.execute("SELECT * FROM muse_schedule_runs WHERE receipt IS NULL ORDER BY due").fetchall()

    def delivered(self, run, ts):
        with self.connect() as db:
            db.execute("UPDATE muse_schedule_runs SET receipt=? WHERE schedule_id=? AND run_id=?",
                       (ts, run["schedule_id"], run["run_id"]))
