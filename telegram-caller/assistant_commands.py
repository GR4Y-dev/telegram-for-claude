"""Explicit memory and reminder requests, handled without a model or API calls.

Only call this for a direct message from the owner, never forwarded message contents.
An unrecognized request returns None for the normal conversation handler.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


REMINDER_HELP = (
    "Tell me when and what to remind you about. Examples:\n"
    "Remind me in 20 minutes to take a break\n"
    "Remind me tomorrow at 8am to study\n"
    "Remind me every weekday at 08:00 to study\n"
    "/remind 2027-12-10 09:00 | Bring my passport\n"
    "Times without am/pm use the 24-hour clock in your configured timezone."
)
COMMAND_HELP = (
    "Memory: /remember <fact>, /memories, /forget <memory ID>\n"
    "Reminders: /remind in 20 minutes | Take a break\n"
    "Repeating: /remind weekdays 08:00 | Study\n"
    "Manage: /reminders, /snooze <ID> <minutes>, /done <ID>\n"
    "You can also write 'Remember that ...' or 'Remind me tomorrow at 8am to ...'."
)
WEEKDAYS = {name: i for i, name in enumerate(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}


def _content(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Please include the text you want me to save.")
    if len(value) > 2000:
        raise ValueError("Please keep each saved fact or reminder under 2,000 characters.")
    return value


def _clock(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value.strip(), re.I)
    if not match:
        raise ValueError("Please give a time such as 08:00, 8am or 8:30pm.")
    hour, minute = int(match[1]), int(match[2] or 0)
    suffix = (match[3] or "").lower()
    if minute > 59 or (suffix and not 1 <= hour <= 12) or (not suffix and hour > 23):
        raise ValueError("That time is not valid. Try 08:00, 8am or 8:30pm.")
    if suffix:
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    return hour, minute


def _local(day, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    """Reject wall times the clock skips or repeats instead of choosing silently."""
    naive = datetime(day.year, day.month, day.day, hour, minute)
    candidates = {}
    for fold in (0, 1):
        local = naive.replace(tzinfo=tz, fold=fold)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            candidates[utc] = local
    if not candidates:
        raise ValueError("That local time is skipped by the daylight-saving clock change. Choose another time.")
    if len(candidates) > 1:
        raise ValueError("That local time occurs twice during the daylight-saving clock change. Choose another time.")
    return next(iter(candidates.values()))


def _schedule(spec: str, tz: ZoneInfo, now: datetime) -> tuple[datetime, str]:
    spec = spec.strip().lower()
    utc_now = now.astimezone(timezone.utc)
    local_now = now.astimezone(tz)
    relative = re.fullmatch(r"in\s+(\d+)\s+(minutes?|hours?|days?)", spec)
    if relative:
        count = int(relative[1])
        seconds = count * {"minute": 60, "hour": 3600, "day": 86400}[relative[2].rstrip("s")]
        if not 0 < seconds <= 366 * 86400:
            raise ValueError("Choose a delay between one minute and 366 days.")
        return (utc_now + timedelta(seconds=seconds)).astimezone(tz), "once"

    repeating = re.fullmatch(
        r"(daily|weekdays|weekly|every day|every weekday|every week|every "
        + "|every ".join(WEEKDAYS) + r")\s+(?:at\s+)?(.+)", spec)
    if repeating:
        kind, clock = repeating.groups()
        hour, minute = _clock(clock)
        target_weekday = None
        if kind in ("daily", "every day"):
            repeat = "daily"
        elif kind in ("weekdays", "every weekday"):
            repeat = "weekdays"
        else:
            repeat = "weekly"
            target_weekday = WEEKDAYS.get(kind.removeprefix("every "), local_now.weekday())
        for offset in range(8):
            day = local_now.date() + timedelta(days=offset)
            if repeat == "weekdays" and day.weekday() >= 5:
                continue
            if target_weekday is not None and day.weekday() != target_weekday:
                continue
            candidate = _local(day, hour, minute, tz)
            if candidate.astimezone(timezone.utc) > utc_now:
                return candidate, repeat

    tomorrow = re.fullmatch(r"(tomorrow|today)\s+at\s+(.+)", spec)
    absolute = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(?:at\s+)?(.+)", spec)
    if tomorrow:
        day = local_now.date() + timedelta(days=tomorrow[1] == "tomorrow")
        hour, minute = _clock(tomorrow[2])
    elif absolute:
        try:
            day = datetime.strptime(absolute[1], "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("That date is not valid. Use YYYY-MM-DD, for example 2027-12-10.") from exc
        hour, minute = _clock(absolute[2])
    else:
        raise ValueError(REMINDER_HELP)
    due = _local(day, hour, minute, tz)
    if due.astimezone(timezone.utc) <= utc_now:
        raise ValueError("That time has already passed. Please give a future date and time.")
    return due, "once"


def _when(due: datetime, tz: ZoneInfo) -> str:
    return due.astimezone(tz).strftime("%a %Y-%m-%d %H:%M") + f" ({tz.key})"


def _handle(store, owner: int, text: str, tz: ZoneInfo, now: datetime) -> str | None:
    clean = text.strip()
    normalized = clean.lower().rstrip("?.!").strip()
    if normalized in ("/help", "help with reminders", "help with memory"):
        return COMMAND_HELP
    if normalized in ("/memories", "what do you remember about me", "show my memories"):
        memories = store.memories(owner)
        if not memories:
            return "You haven't asked me to save any facts yet. Try: Remember that my exam is December 10."
        return "Saved memories:\n" + "\n".join(f"{m['id']}. {m['text']}" for m in memories) + "\nTo remove one: /forget <ID>"
    remember = re.match(r"^(?:/remember|remember)(?:\s+(.*))?$", clean, re.I | re.S)
    if remember:
        if not clean.startswith("/") and (clean.endswith("?") or re.match(r"remember\s+(when|how|why|who|where)\b", clean, re.I)):
            return None
        fact = re.sub(r"^that(?:\s+|$)", "", remember[1] or "", flags=re.I)
        ident = store.remember(owner, _content(fact))
        return f"Saved memory #{ident}: {fact.strip()}\nI'll keep it after restarts. Remove it with /forget {ident}."
    forget = re.fullmatch(r"(?:/forget|forget memory)\s+#?(\d+)", clean, re.I)
    if forget:
        ident = int(forget[1])
        if not store.forget(owner, ident):
            return f"I couldn't find memory #{ident}. Send /memories to see your saved facts."
        # A resumed model session could still contain the forgotten fact. Start fresh.
        return f"Forgot memory #{ident} and started a fresh chat. This doesn't delete old Telegram messages or saved conversation files."
    if re.match(r"^(?:/forget|forget memory)(?:\s|$)", clean, re.I):
        return "Choose the memory ID from /memories, then send /forget <ID>."

    if normalized in ("/reminders", "/tasks", "what are my reminders", "what are my tasks",
                      "show my reminders", "show my tasks", "list my reminders", "list my tasks"):
        reminders = store.reminders(owner)
        if not reminders:
            return "You have no unfinished reminders. Try: Remind me in 20 minutes to take a break."
        lines = []
        for r in reminders:
            due = r["next_due"]
            when = (_when(datetime.fromtimestamp(due, timezone.utc), ZoneInfo(r["timezone"]))
                    if due is not None else "already notified; awaiting Done")
            repeat = "" if r["repeat"] == "once" else f"; repeats {r['repeat']}"
            lines.append(f"#{r['id']}: {r['text']} — {when}{repeat}")
        return "Your reminders:\n" + "\n".join(lines) + "\n/done <ID> finishes it (and stops repeats). /snooze <ID> <minutes> postpones it."

    done = re.fullmatch(r"(?:/done|done)\s+#?(\d+)", clean, re.I)
    if done:
        ident = int(done[1])
        return (f"Reminder #{ident} is done. It won't send any more notifications."
                if store.complete(owner, ident) else f"No unfinished reminder #{ident}. Send /reminders to see the IDs.")
    if re.match(r"^/done(?:\s|$)", clean, re.I) or normalized == "done":
        return "Which reminder is done? Send /done <ID>. Find its ID with /reminders."

    snooze = re.fullmatch(r"(?:/snooze|snooze)\s+#?(\d+)\s+(?:for\s+)?(\d+)(?:\s+minutes?)?", clean, re.I)
    if snooze:
        ident, minutes = int(snooze[1]), int(snooze[2])
        if not 1 <= minutes <= 10080:
            raise ValueError("Snooze must be between 1 and 10,080 minutes (one week).")
        if store.snooze(owner, ident, minutes, now):
            due = now.astimezone(timezone.utc) + timedelta(minutes=minutes)
            return f"Snoozed reminder #{ident} until {_when(due, tz)}."
        return f"No unfinished reminder #{ident}. Send /reminders to see the IDs."
    if re.match(r"^(?:/snooze|snooze)(?:\s|$)", clean, re.I):
        return "Send /snooze <ID> <minutes>, for example /snooze 2 20. Find IDs with /reminders."

    reminder = re.match(r"^(/remind|remind me)(?:\s+(.*))?$", clean, re.I | re.S)
    if reminder:
        body = (reminder[2] or "").strip()
        if "|" in body:
            spec, task = body.split("|", 1)
        else:
            split = re.split(r"\s+to\s+", body, maxsplit=1, flags=re.I)
            if len(split) != 2:
                raise ValueError(REMINDER_HELP)
            spec, task = split
        task = _content(task)
        due, repeat = _schedule(spec, tz, now)
        ident = store.add_reminder(owner, task, due, tz.key, repeat=repeat)
        repeating = "" if repeat == "once" else f" Repeats {repeat} at {due.astimezone(tz):%H:%M}."
        return (f"Reminder #{ident} saved for {_when(due, tz)}: {task}.{repeating}\n"
                f"Snooze: /snooze {ident} 20. Finish or stop: /done {ident}. "
                "Saved reminders don't need Claude to send; your assistant service must be running.")
    return None


def handle_command(store, owner: int, text: str, tz: ZoneInfo, now: datetime) -> str | None:
    """Apply one explicit owner command; return its response, or None for ordinary chat."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    try:
        return _handle(store, owner, text, tz, now)
    except (ValueError, OverflowError) as exc:
        return str(exc)
