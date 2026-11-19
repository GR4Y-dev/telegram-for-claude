"""Persistent, ordered Telegram copies. One worker per connector/data volume.

Telegram sends and disk commits cannot be one transaction. The pending journal
reconciles uncertain sends against recent target history before attempting them
again. It is a best-effort recovery heuristic, not an exactly-once guarantee.
"""
import asyncio
import base64
import copy
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from telethon import errors, utils
from telethon.extensions import BinaryReader, html
from telethon.tl import types

from transfers import MB, Transfer, TransferError
import video_tools

log = logging.getLogger("telegram-copy-jobs")
ACTIVE = {"running", "waiting"}
# Telegram's Premium upload cap; try_over_limit never attempts more than this.
TRY_LIMIT = 4000 * MB
# Telethon names known RPC errors by class (FilePartsInvalidError); unknown ones keep the raw code.
REFUSED_SIZE = ("FilePart", "TooBig", "FILE_PART", "TOO_BIG")
FLOODS = (errors.FloodWaitError, errors.FloodPremiumWaitError)
TRANSIENT = (ConnectionError, TimeoutError, errors.TimedOutError, errors.ServerError,
             errors.FileReferenceExpiredError, errors.FileReferenceInvalidError,
             errors.FileReferenceEmptyError)
MEDIA_ERRORS = (errors.ChatForwardsRestrictedError, errors.MediaInvalidError,
                errors.MediaEmptyError, errors.DocumentInvalidError, errors.PhotoInvalidError)
ALBUM_ERRORS = (*MEDIA_ERRORS, errors.GroupedMediaInvalidError,
                errors.MediaGroupedInvalidError, TypeError)
BACKOFF = (10, 30, 90, 300)
LINK = re.compile(r"(?<![\w/.-])https?://(?:www\.)?(?:t\.me|telegram\.me)/"
                  r"(?P<chat>c/\d+|[a-zA-Z][\w]*)/(?P<id>\d+)(?![\w/])", re.I)


class PauseCopy(Exception):
    def __init__(self, source_id, reason):
        self.source_id, self.reason = source_id, reason


def _positive(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) < 1:
        raise ValueError("Message ids must be positive whole numbers.")
    return int(value)


def _link_base(chat_id, username):
    if chat_id >= -10**12:
        return None  # Private conversations and basic groups have no post permalinks.
    return "https://t.me/" + (username or f"c/{abs(chat_id) - 10**12}")


def rewrite_html(job, value):
    """Only change links to this source; HTML parsing later recalculates UTF-16 offsets."""
    target = _link_base(job["to_chat_id"], job.get("to_username"))
    source_paths = {f"c/{abs(job['from_chat_id']) - 10**12}"}
    if job.get("from_username"):
        source_paths.add(job["from_username"].lower())
    unresolved = set(job.get("links_left_unchanged", []))

    def replace(match):
        if match["chat"].lower() not in source_paths:
            return match[0]
        source_id = int(match["id"])
        mapped = job["map"].get(str(source_id))
        skipped = any(s["source_id"] == source_id for s in job["skipped"])
        missing = (job["from_message_id"] <= source_id < job["next_source_id"] or
                   any(a <= source_id <= b for a, b in job.get("deleted_ranges", [])))
        if not mapped and (skipped or missing):
            later = [int(n) for n in job["map"]
                     if max(source_id + 1, job["from_message_id"]) <= int(n) <= job["end_source_id"]]
            if later:
                mapped = job["map"][str(min(later))]
        if mapped and target:
            return f"{target}/{mapped[0]}"
        unresolved.add(source_id)
        return match[0]

    result = LINK.sub(replace, value)
    job["links_left_unchanged"] = sorted(unresolved)
    return result


def _text(job, message):
    text, entities = message.message or "", getattr(message, "entities", None) or []
    rendered = None
    if job["options"]["rewrite_links"]:
        before = html.unparse(text, entities)
        after = rewrite_html(job, before)
        if after != before:
            rendered = after
            text, entities = html.parse(after)
    # Persist the precise outgoing text/entities, including separate captions.
    # TL entity bytes preserve entity kinds that HTML cannot represent.
    return _pack_text(text, entities, rendered)


def _pack_text(text, entities, rendered=None):
    return {"text": text, "html": rendered,
            "entities": [base64.b64encode(bytes(e)).decode("ascii") for e in entities]}


def _entities(item):
    return [BinaryReader(base64.b64decode(e)).tgread_object() for e in item["entities"]]


def _text_args(item):
    if item["html"] is not None:
        return item["html"], {"parse_mode": "html"}
    return item["text"], {"formatting_entities": _entities(item), "parse_mode": None}


def _text_parts(value):
    """Long rewritten links can push even a valid source past Telegram's limit."""
    text = value["text"]
    if len(text.encode("utf-16-le")) // 2 <= 4096:
        yield value
        return
    entities, start, offset = _entities(value), 0, 0
    while start < len(text):
        end, length = start, 0
        while end < len(text):
            units = 2 if ord(text[end]) > 0xffff else 1
            if length + units > 4096:
                break
            length += units
            end += 1
        clipped = []
        for entity in entities:
            a, b = max(offset, entity.offset), min(offset + length, entity.offset + entity.length)
            if a < b:
                part = copy.copy(entity)
                part.offset, part.length = a - offset, b - a
                clipped.append(part)
        yield _pack_text(text[start:end], clipped)
        start, offset = end, offset + length


def _file_info(message):
    file = getattr(message, "file", None)
    document = getattr(message, "document", None)
    attributes = getattr(document, "attributes", None) or []
    name = next((a.file_name for a in attributes if isinstance(a, types.DocumentAttributeFilename)), None)
    name = name or getattr(file, "name", None)
    return name, int(getattr(file, "size", 0) or 0), attributes


def _kind(message):
    if isinstance(message, (types.MessageService, types.MessageEmpty)) or getattr(message, "action", None):
        return "skip", "Service or empty message."
    media = getattr(message, "media", None)
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return ("text", None) if message.message else ("skip", "Empty message or link preview without text.")
    if isinstance(media, (types.MessageMediaDocument, types.MessageMediaPhoto)) and getattr(message, "file", None):
        return "file", None
    if isinstance(media, (types.MessageMediaContact, types.MessageMediaGeo,
                          types.MessageMediaGeoLive, types.MessageMediaVenue, types.MessageMediaDice)):
        return "reference", None
    return "skip", "Unsupported media (poll, game, invoice, story or unavailable attachment)."


async def _can_post(client, target):
    if isinstance(target, types.User) and not target.deleted:
        return
    if not isinstance(target, (types.Chat, types.Channel)) or getattr(target, "left", False):
        raise ValueError("The target must be a chat this account can post in.")
    try:
        permission = await client.get_permissions(target, "me")
    except errors.UserNotParticipantError:
        raise ValueError("This account is not a member of the target chat.") from None
    if permission.has_left:
        raise ValueError("This account is not a member of the target chat.")
    if isinstance(target, types.Channel) and target.broadcast:
        allowed = permission.is_creator or (permission.is_admin and permission.post_messages)
    else:
        allowed = not getattr(permission.participant, "left", False)
        bans = [getattr(permission.participant, "banned_rights", None)]
        if not permission.is_admin:
            bans.append(getattr(target, "default_banned_rights", None))
        allowed = allowed and not any(getattr(b, flag, False) for b in bans
                                      for flag in ("view_messages", "send_messages", "send_plain"))
    if not allowed:
        raise ValueError("This account does not have permission to post in the target chat.")


class CopyJobs:
    def __init__(self, directory, *, get_client, resolve_chat, transfers, upload_limit,
                 video_upload, notify=None, sends_per_hour=60, clock=time.time, sleep=asyncio.sleep):
        self.directory = Path(directory) / "copy-jobs"
        self.get_client, self.resolve_chat = get_client, resolve_chat
        self.transfers, self.upload_limit, self.video_upload = transfers, upload_limit, video_upload
        self.notify, self.sends_per_hour = notify, max(1, int(sends_per_hour))
        self.clock, self.sleep = clock, sleep
        self.jobs, self.tasks = {}, {}
        self.lock = asyncio.Lock()
        self.started = False

    def _save(self, job):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        job["updated"] = self.clock()
        fd, name = tempfile.mkstemp(prefix=job["job_id"] + "-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.chmod(name, 0o600)
                json.dump(job, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / (job["job_id"] + ".json"))
            if os.name == "posix":
                folder = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
        finally:
            Path(name).unlink(missing_ok=True)

    def startup(self):
        """No Telegram await here: safe from both tg() and the ASGI startup hook."""
        if self.started:
            return
        self.started = True
        for path in sorted(self.directory.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["job_id"] != path.stem or not re.fullmatch(r"[a-f0-9]{12}", path.stem):
                    raise ValueError("Invalid job id")
                self.jobs[job["job_id"]] = job
            except (ValueError, KeyError, OSError):
                log.error("Could not load a copy job state file; inspect copy-jobs storage.")
        active = sorted((j for j in self.jobs.values() if j["status"] in ACTIVE), key=lambda j: j["created"])
        for job in active[1:]:
            job.update(status="paused", pause_reason="Another copy job was resumed first.",
                       paused_at_source_id=job["next_source_id"])
            self._save(job)
            self.tasks[job["job_id"]] = asyncio.create_task(self._notify(job))
        if active:
            self._launch(active[0])

    def _launch(self, job):
        # Rates count only time spent running since this launch, not time spent paused or failed.
        job["stats"]["run_started_at"] = self.clock()
        self.tasks[job["job_id"]] = asyncio.create_task(self._run(job), name="copy-" + job["job_id"])

    def _check_idle(self, except_id=None):
        if any(j["job_id"] != except_id and j["status"] in ACTIVE for j in self.jobs.values()):
            raise ValueError("Another copy job is running. Pause or cancel it first.")

    def _merge_links(self, job, links):
        pairs = {str(_positive(k)): [_positive(v)] for k, v in (links or {}).items()}
        for source_id, targets in pairs.items():
            if source_id in job["map"] and job["map"][source_id][0] != targets[0]:
                raise ValueError(f"Message {source_id} already has a different target in the map.")
            pending = job.get("pending")
            if pending and int(source_id) in pending["source_ids"] and source_id not in job["map"]:
                raise ValueError("That message has an unfinished send. Resume it before adding its link.")
        for source_id, targets in pairs.items():
            job["map"].setdefault(source_id, targets)

    async def start(self, from_chat, to_chat, from_message_id, to_message_id=None,
                    playable_videos=True, rewrite_links=True, link_map=None, pace_seconds=3):
        self.startup()
        first = _positive(from_message_id)
        if to_message_id is not None and _positive(to_message_id) < first:
            raise ValueError("The last message id must be at least the first message id.")
        async with self.lock:
            self._check_idle()
            client = await self.get_client()
            source = await self.resolve_chat(client, from_chat)
            target = await self.resolve_chat(client, to_chat)
            await _can_post(client, target)
            if to_message_id is None:
                newest = await client.get_messages(source, limit=1)
                last = newest[0].id if newest else 0
            else:
                last = _positive(to_message_id)
            if last < first:
                raise ValueError("There are no source messages at or after that starting id.")
            now = self.clock()
            job = dict(job_id=secrets.token_hex(6), from_chat_id=utils.get_peer_id(source),
                       from_title=utils.get_display_name(source), to_chat_id=utils.get_peer_id(target),
                       to_title=utils.get_display_name(target), from_username=getattr(source, "username", None),
                       to_username=getattr(target, "username", None), created=now, updated=now,
                       from_message_id=first, next_source_id=first, end_source_id=last,
                       status="running", pause_reason=None, last_error=None, paused_at_source_id=None,
                       map={}, skipped=[], pending=None, deleted_ranges=[], links_left_unchanged=[],
                       sends=[], wait_until=None, retry={},
                       stats=dict(copied_messages=0, copied_files=0, bytes=0, started_at=now,
                                  last_copied_at=None, flood_wait_seconds_total=0, current=None),
                       options=dict(playable_videos=bool(playable_videos), rewrite_links=bool(rewrite_links),
                                    pace_seconds=max(0, float(pace_seconds))))
            self._merge_links(job, link_map)
            self._save(job)
            self.jobs[job["job_id"]] = job
            self._launch(job)
            return {"job_id": job["job_id"], "from_chat": job["from_title"], "to_chat": job["to_title"],
                    "from_chat_id": job["from_chat_id"], "to_chat_id": job["to_chat_id"],
                    "from_message_id": first, "to_message_id": last,
                    "approximate_messages": last - first + 1,
                    "note": "The count includes deleted ids. Watch with copy_status(job_id); use control_copy to pause or cancel."}

    async def control(self, job_id, action, link_map=None):
        self.startup()
        if action not in {"pause", "resume", "skip_current", "cancel", "add_links", "try_over_limit"}:
            raise ValueError("Use pause, resume, skip_current, cancel, add_links or try_over_limit.")
        async with self.lock:
            job = self._get(job_id)
            if action in {"resume", "skip_current", "try_over_limit"}:
                self._check_idle(job_id)
                if job["status"] in {"done", "cancelled"}:
                    raise ValueError("That job is finished or cancelled. Start a new copy for another range.")
            if action == "skip_current" and (job["status"] != "paused" or not job["paused_at_source_id"]):
                raise ValueError("Pause the job at the message you want to skip first.")
            if action == "try_over_limit":
                if job["status"] != "paused":
                    raise ValueError("try_over_limit resumes a paused job; this one is not paused.")
                # Let Telegram decide on files over the account limit; its refusal pauses again.
                job["options"]["try_over_limit"] = True
            self._merge_links(job, link_map)
            if action in {"pause", "cancel", "skip_current"}:
                if job["status"] in {"done", "cancelled"}:
                    raise ValueError("That job is already finished or cancelled.")
                job["status"] = "cancelled" if action == "cancel" else "paused"
                job["pause_reason"] = "Cancelled by the user." if action == "cancel" else "Paused by the user."
                job["paused_at_source_id"] = job["paused_at_source_id"] or (
                    (job["stats"]["current"] or {}).get("source_id", job["next_source_id"]))
                self._save(job)
                task = self.tasks.get(job_id)
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if action == "skip_current":
                self._skip(job, job["paused_at_source_id"], "skipped by the user")
            if action in {"resume", "skip_current", "try_over_limit"} and job["status"] not in ACTIVE:
                job.update(status="running", pause_reason=None, last_error=None,
                           paused_at_source_id=None, retry={})
                # Keep a server-requested flood deadline even across manual resume.
                self._save(job)
                self._launch(job)
            else:
                self._save(job)
            return self.status(job_id)

    async def shutdown(self):
        tasks = [t for t in self.tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _get(self, job_id):
        if job_id not in self.jobs:
            raise ValueError("No copy job with that id. Use copy_status('all') to list jobs.")
        return self.jobs[job_id]

    def status(self, job_id=None):
        self.startup()
        ordered = sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)
        if job_id == "all":
            return {"jobs": [{k: j[k] for k in ("job_id", "status", "from_title", "to_title",
                                                "next_source_id", "end_source_id", "pause_reason")}
                             for j in ordered[:20]], "total_jobs": len(ordered)}
        if not job_id and not ordered:
            return {"jobs": []}
        job = self._get(job_id) if job_id else next((j for j in ordered if j["status"] in ACTIVE), ordered[0])
        stats = job["stats"]
        since = max(stats["started_at"], stats.get("run_started_at") or 0, self.clock() - 3600)
        elapsed = max(1, self.clock() - since)
        recent = [s for s in job["sends"] if s[0] >= since]
        rate = len(recent) * 3600 / elapsed
        remaining = max(0, job["end_source_id"] - job["next_source_id"] + 1)
        entries = sorted(job["map"].items(), key=lambda p: int(p[0]))[-10:]
        return {k: job[k] for k in ("job_id", "status", "from_title", "to_title", "pause_reason",
                                    "last_error", "paused_at_source_id", "next_source_id", "end_source_id")} | {
            "stats": dict(job["stats"]), "mb_per_minute": round(sum(s[1] for s in recent) / MB * 60 / elapsed, 2),
            "messages_per_hour": round(rate, 1), "eta_seconds_estimate": round(remaining * 3600 / rate) if rate else None,
            "skipped": job["skipped"][-20:], "skipped_count": len(job["skipped"]),
            "links_left_unchanged_count": len(job["links_left_unchanged"]),
            "links_left_unchanged": job["links_left_unchanged"][-20:], "last_map_entries": dict(entries)}

    def _current(self, job, source_id, phase, **extra):
        job["stats"]["current"] = dict(source_id=source_id, phase=phase, **extra)

    def _skip(self, job, source_id, reason):
        if not any(s["source_id"] == source_id for s in job["skipped"]):
            job["skipped"].append(dict(source_id=source_id, reason=reason))

    async def _notify(self, job):
        if self.notify:
            message = (f"Copy job {job['job_id']} {job['status']} at source message "
                       f"{job['paused_at_source_id'] or job['next_source_id']}. "
                       f"{job['pause_reason'] or job['last_error'] or 'The requested range is complete.'}")
            if job["status"] in {"paused", "failed"}:
                message += f" Use control_copy('{job['job_id']}', 'resume') to retry, or 'skip_current' when paused to skip it."
            try:
                await self.notify(message)
            except Exception:
                log.warning("Copy job %s notification failed.", job["job_id"])

    async def _wait(self, job, seconds, phase):
        source_id = (job["stats"]["current"] or {}).get("source_id", job["next_source_id"])
        self._current(job, source_id, phase)
        job.update(status="waiting", wait_until=self.clock() + max(0, seconds))
        self._save(job)
        await self.sleep(max(0, seconds))
        job.update(status="running", wait_until=None)
        self._save(job)

    async def _budget(self, job, count):
        while True:
            recent = sorted(s[0] for j in self.jobs.values() for s in j["sends"] if s[0] > self.clock() - 3600)
            if len(recent) + count <= self.sends_per_hour:
                break
            await self._wait(job, recent[len(recent) + count - self.sends_per_hour - 1] + 3600 - self.clock() + .01,
                             "waiting_send_budget")
        latest = max((s[0] for j in self.jobs.values() for s in j["sends"]), default=0)
        delay = latest + job["options"]["pace_seconds"] - self.clock()
        if delay > 0:
            await self.sleep(delay)

    async def _groups(self, job, source):
        client = await self.get_client()
        messages = [m async for m in client.iter_messages(source, reverse=True,
                    min_id=job["next_source_id"] - 1, max_id=job["end_source_id"] + 1, limit=100)]
        full = len(messages) == 100
        # Look beyond the page boundary so a ten-item album is never split there.
        if full and getattr(messages[-1], "grouped_id", None):
            last = messages[-1]
            client = await self.get_client()
            async for m in client.iter_messages(source, reverse=True, min_id=last.id,
                                                max_id=job["end_source_id"] + 1, limit=10):
                if getattr(m, "grouped_id", None) != last.grouped_id:
                    break
                messages.append(m)
        previous = job["next_source_id"] - 1
        for m in messages:
            if m.id > previous + 1:
                gap = [previous + 1, m.id - 1]
                if gap not in job["deleted_ranges"]:
                    job["deleted_ranges"].append(gap)
            previous = m.id
        if not full and previous < job["end_source_id"]:
            gap = [previous + 1, job["end_source_id"]]
            if gap not in job["deleted_ranges"]:
                job["deleted_ranges"].append(gap)
        groups = []
        for m in messages:
            if (groups and getattr(m, "grouped_id", None) and
                    getattr(groups[-1][-1], "grouped_id", None) == m.grouped_id and len(groups[-1]) < 10):
                groups[-1].append(m)
            else:
                groups.append([m])
        return [[m.id for m in group] for group in groups]

    async def _plan(self, job, source, ids):
        client = await self.get_client()
        messages = await client.get_messages(source, ids=ids)
        me = await client.get_me()
        caption_limit = 2048 if getattr(me, "premium", False) else 1024
        limit = await self._limit(job, client)
        items = []
        skipped = {s["source_id"] for s in job["skipped"]}
        for source_id, m in zip(ids, messages):
            if source_id in skipped or str(source_id) in job["map"]:
                continue
            if m is None:
                continue
            kind, reason = _kind(m)
            if kind == "skip":
                self._skip(job, source_id, reason)
                continue
            name, size, _ = _file_info(m)
            if kind == "file" and size > limit:
                raise PauseCopy(source_id, f"Message {source_id} ({name or 'file'}, {size / MB:.1f} MB) "
                                f"is bigger than the {limit // MB} MB this account can send.")
            text = _text(job, m)
            reply = getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)
            base = dict(source_id=source_id, started_at=None, target_id=None, attempted=False,
                        file_name=name, size=size, reply_source_id=reply,
                        media_type=type(getattr(m, "media", None)).__name__,
                        link_preview=bool(getattr(m, "web_preview", False)))
            split = kind != "text" and (len(text["text"].encode("utf-16-le")) // 2 > caption_limit or
                                        bool(getattr(m, "video_note", False)) and bool(text["text"]))
            first_text = {"text": "", "entities": [], "html": None} if split else text
            for part in _text_parts(first_text) if kind == "text" else [first_text]:
                items.append(base | part | dict(kind=kind, text_prefix=part["text"][:100]))
            if split:
                for part in _text_parts(text):
                    items.append(base | part | dict(kind="text", file_name=None, size=0, text_prefix=part["text"][:100]))
        if not items:
            job["next_source_id"] = max(ids) + 1
            self._save(job)
            return
        job["pending"] = {k: items[0][k] for k in ("source_id", "kind", "file_name", "size", "text_prefix")}
        job["pending"].update(started_at=self.clock(), source_ids=ids, items=items,
                              album=len(items) > 1 and all(p["kind"] == "file" and
                                  p["reply_source_id"] == items[0]["reply_source_id"] for p in items) and
                                  items[0]["reply_source_id"] not in ids)
        self._save(job)

    async def _limit(self, job, client):
        limit = await self.upload_limit(client)
        return max(limit, TRY_LIMIT) if job["options"].get("try_over_limit") else limit

    def _record(self, job, item, target_id):
        item["target_id"] = target_id
        targets = job["map"].setdefault(str(item["source_id"]), [])
        if target_id not in targets:
            targets.append(target_id)
        now = self.clock()
        job["sends"] = [s for s in job["sends"] if s[0] > now - 3600] + [[now, item["size"]]]
        self._save(job)

    async def _recover(self, job, target):
        pending = job["pending"]
        uncertain = [p for p in pending["items"] if p["attempted"] and not p["target_id"]]
        if not uncertain:
            return
        client = await self.get_client()
        recent = [m async for m in client.iter_messages(target, limit=20)]
        recent.sort(key=lambda m: m.id)
        used = {n for ids in job["map"].values() for n in ids}
        for item in uncertain:
            for m in recent:
                if (not m.out or m.id in used or m.id <= item["last_target_id"] or
                        not m.date or m.date.timestamp() < item["started_at"] - 60):
                    continue
                name, size, _ = _file_info(m)
                if item["kind"] == "file":
                    matches = name == item["file_name"] and size == item["size"] and getattr(m, "file", None)
                else:
                    matches = not getattr(m, "file", None) and (m.message or "")[:100] == item["text_prefix"]
                    if item["kind"] == "reference":
                        matches = matches and type(m.media).__name__ == item["media_type"]
                    else:
                        matches = matches and (m.media is None or isinstance(m.media, types.MessageMediaWebPage))
                if matches:
                    self._record(job, item, m.id)
                    used.add(m.id)
                    break

    def _attempt(self, job, items):
        floor = max((n for ids in job["map"].values() for n in ids), default=0)
        for item in items:
            item.update(attempted=True, started_at=self.clock(), last_target_id=floor)
        job["pending"].update({k: items[0][k] for k in ("source_id", "started_at", "kind", "file_name", "size", "text_prefix")})
        self._current(job, items[0]["source_id"], "sending")
        self._save(job)

    def _progress(self, job, source_id, phase):
        def update(done, total):
            self._current(job, source_id, phase, bytes_done=done, size=total)
        return update

    async def _thumbnail(self, job, message, item):
        if not getattr(getattr(message, "document", None), "thumbs", None):
            return None
        with tempfile.TemporaryDirectory(prefix="thumb-", dir=item.folder) as folder:
            thumb = Transfer(folder, 199_999, self.transfers.reserve)
            try:
                client = await self.get_client()
                await client.download_media(message, file=thumb, thumb=-1)
                if not thumb.size:
                    return None
                thumb.finish("thumb.jpg")
                client = await self.get_client()
                return await client.upload_file(str(thumb.path), file_size=thumb.size, file_name="thumb.jpg")
            except (TransferError, errors.PhotoInvalidError):
                return None
            finally:
                thumb.close()

    async def _upload(self, job, message, pending_item):
        name, size, attributes = _file_info(message)
        client = await self.get_client()
        limit = await self._limit(job, client)
        if size > limit:
            raise PauseCopy(message.id, f"Message {message.id} ({name or 'file'}, {size / MB:.1f} MB) "
                            f"is bigger than the {limit // MB} MB this account can send.")
        async with self.transfers.file(limit, size or None) as item:
            self._current(job, message.id, "downloading", bytes_done=0, size=size)
            client = await self.get_client()
            await client.download_media(message, file=item,
                progress_callback=self._progress(job, message.id, "downloading"))
            item.finish(name or ("photo.jpg" if getattr(message, "photo", None) else "file" + (message.file.ext or "")))
            attributes = list(attributes)
            promote = (job["options"]["playable_videos"] and video_tools.is_video(message) and
                       not any(getattr(message, flag, None) for flag in ("video", "video_note", "gif", "sticker")))
            options = {}
            if promote:
                # Separate derived paths from the original filename (which may be thumb.jpg).
                with tempfile.TemporaryDirectory(prefix="video-", dir=item.folder) as folder:
                    try:
                        item.check_disk(20_000)
                        options = await self.video_upload(str(item.path), folder)
                        attributes = [a for a in attributes if not isinstance(a, types.DocumentAttributeVideo)] + options["attributes"]
                        generated_thumb = options.get("thumb")
                        if generated_thumb and Path(generated_thumb).stat().st_size < 200_000:
                            item.check_disk()
                            client = await self.get_client()
                            options["thumb_handle"] = await client.upload_file(generated_thumb)
                    except video_tools.VideoBusy:
                        raise
                    except video_tools.VideoError:
                        pass  # Keep the original type; never transcode.
            client = await self.get_client()
            self._current(job, message.id, "uploading", bytes_done=0, size=item.size)
            handle = await client.upload_file(str(item.path), file_size=item.size,
                file_name=name or item.path.name, progress_callback=self._progress(job, message.id, "uploading"))
            pending_item["size"] = item.size
            if getattr(message, "photo", None):
                return types.InputMediaUploadedPhoto(handle)
            pending_item["file_name"] = name or item.path.name
            if not any(isinstance(a, types.DocumentAttributeFilename) for a in attributes):
                attributes.append(types.DocumentAttributeFilename(name or item.path.name))
            thumb = await self._thumbnail(job, message, item) or options.get("thumb_handle")
            typed = any(isinstance(a, (types.DocumentAttributeVideo, types.DocumentAttributeAudio,
                                      types.DocumentAttributeSticker, types.DocumentAttributeAnimated)) for a in attributes)
            return types.InputMediaUploadedDocument(handle,
                mime_type=options.get("mime_type") or message.file.mime_type or "application/octet-stream",
                attributes=attributes, thumb=thumb, force_file=not typed,
                nosound_video=not bool(getattr(message, "gif", False)))

    async def _media(self, job, source, item, *, force_upload=False):
        self._current(job, item["source_id"], "sending")
        client = await self.get_client()
        m = await client.get_messages(source, ids=item["source_id"])
        if m is None or isinstance(m, types.MessageEmpty):
            self._skip(job, item["source_id"], "Message was deleted before copying.")
            return None
        if _kind(m)[0] != item["kind"]:
            raise PauseCopy(m.id, f"Message {m.id} changed its media type during the copy. Skip it or start a new range for it.")
        if item["kind"] == "file":
            name, size, _ = _file_info(m)
            client = await self.get_client()
            limit = await self._limit(job, client)
            if size > limit:
                raise PauseCopy(m.id, f"Message {m.id} ({name or 'file'}, {size / MB:.1f} MB) "
                                f"is bigger than the {limit // MB} MB this account can send.")
            item.update(file_name=name, size=size)
        protected = getattr(source, "noforwards", False) or getattr(m, "noforwards", False)
        promote = (job["options"]["playable_videos"] and video_tools.is_video(m) and
                   not any(getattr(m, flag, None) for flag in ("video", "video_note", "gif", "sticker")))
        if item["kind"] == "file" and (protected or promote or force_upload):
            return await self._upload(job, m, item)
        return m.media

    def _reply(self, job, item):
        ids = job["map"].get(str(item["reply_source_id"]))
        return ids[0] if ids else None

    async def _send_one(self, job, source, target, item, prepared=None):
        text, formatting = _text_args(item)
        self._current(job, item["source_id"], "sending")
        await self._budget(job, 1)
        media = None
        if item["kind"] != "text":
            media = prepared if prepared is not None else await self._media(job, source, item)
            if media is None:
                return
        await self._budget(job, 1)
        client = await self.get_client()
        self._attempt(job, [item])
        if item["kind"] == "text":
            sent = await client.send_message(target, text, reply_to=self._reply(job, item),
                                             link_preview=item["link_preview"], **formatting)
        else:
            try:
                sent = await client.send_file(target, media, caption=text, reply_to=self._reply(job, item), **formatting)
            except (*MEDIA_ERRORS, TypeError):
                if item["kind"] == "reference":
                    self._skip(job, item["source_id"], "Telegram could not copy this contact, location or dice.")
                    return
                if isinstance(media, (types.InputMediaUploadedDocument, types.InputMediaUploadedPhoto)):
                    raise
                media = await self._media(job, source, item, force_upload=True)
                if media is None:
                    return
                await self._budget(job, 1)
                client = await self.get_client()
                self._attempt(job, [item])
                sent = await client.send_file(target, media, caption=text, reply_to=self._reply(job, item), **formatting)
        self._record(job, item, sent.id)

    async def _copy_pending(self, job, source, target):
        pending = job["pending"]
        skipped = {s["source_id"] for s in job["skipped"]}
        items = [p for p in pending["items"] if not p["target_id"] and p["source_id"] not in skipped]
        prepared = {}
        if pending["album"] and 1 < len(items) <= self.sends_per_hour:
            self._current(job, items[0]["source_id"], "sending")
            await self._budget(job, len(items))
            media = [await self._media(job, source, p) for p in items]
            prepared = {p["source_id"]: m for p, m in zip(items, media)}
            if all(m is not None for m in media):
                await self._budget(job, len(items))
                client = await self.get_client()
                self._attempt(job, items)
                try:
                    sent = await client.send_file(target, media, caption=[p["text"] for p in items],
                        formatting_entities=[_entities(p) for p in items], parse_mode=None,
                        reply_to=self._reply(job, items[0]))
                except ALBUM_ERRORS:
                    pending["album"] = False
                    self._save(job)
                else:
                    if not isinstance(sent, (list, tuple)) or len(sent) != len(items) or any(m is None for m in sent):
                        raise ConnectionError("Incomplete album acknowledgement")
                    for item, message in zip(items, sent):
                        self._record(job, item, message.id)
                    items = []
        for item in items:
            if item["source_id"] not in {s["source_id"] for s in job["skipped"]}:
                await self._send_one(job, source, target, item, prepared.get(item["source_id"]))
        # The map has already been saved, with each acknowledged/recovered part.
        completed = {p["source_id"] for p in pending["items"] if p["target_id"]}
        files = [p for p in pending["items"] if p["target_id"] and p["kind"] == "file"]
        stats = job["stats"]
        stats["copied_messages"] += len(completed)
        stats["copied_files"] += len(files)
        stats["bytes"] += sum(p["size"] for p in files)
        if completed:
            stats["last_copied_at"] = self.clock()
        job.update(next_source_id=max(pending["source_ids"]) + 1, pending=None, retry={}, last_error=None)
        self._save(job)

    async def _run(self, job):
        groups, recover = [], True
        try:
            while job["status"] in ACTIVE:
                try:
                    if job["wait_until"] and job["wait_until"] > self.clock():
                        await self._wait(job, job["wait_until"] - self.clock(),
                                         (job["stats"]["current"] or {}).get("phase", "waiting_flood"))
                    elif job["status"] == "waiting":
                        job.update(status="running", wait_until=None)
                    client = await self.get_client()
                    source = await self.resolve_chat(client, str(job["from_chat_id"]))
                    target = await self.resolve_chat(client, str(job["to_chat_id"]))
                    if job["pending"]:
                        if recover:
                            await self._recover(job, target)
                            recover = False
                        await self._copy_pending(job, source, target)
                        continue
                    if job["next_source_id"] > job["end_source_id"]:
                        break
                    if not groups:
                        groups = await self._groups(job, source)
                    if not groups:
                        job["next_source_id"] = job["end_source_id"] + 1
                        break
                    ids = groups.pop(0)
                    self._current(job, ids[0], "sending")
                    await self._plan(job, source, ids)
                except PauseCopy as exc:
                    job.update(status="paused", pause_reason=exc.reason, paused_at_source_id=exc.source_id)
                except Exception as exc:
                    source_id = (job["stats"]["current"] or {}).get("source_id", job["next_source_id"])
                    retry = job["retry"]
                    if retry.get("source_id") != source_id:
                        retry = job["retry"] = dict(source_id=source_id, floods=0, transient=0)
                    if isinstance(exc, FLOODS):
                        seconds = max(0, exc.seconds) + 5
                        retry["floods"] += 1
                        if retry["floods"] <= 5:
                            job["stats"]["flood_wait_seconds_total"] += seconds
                            await self._wait(job, seconds, "waiting_flood")
                        else:
                            job.update(status="paused", pause_reason="Telegram kept asking this copy to wait after five retries.",
                                       paused_at_source_id=source_id, wait_until=self.clock() + seconds)
                    elif isinstance(exc, video_tools.VideoBusy) or isinstance(exc, TransferError) and "busy" in str(exc):
                        await self._wait(job, 3, "downloading")
                    elif isinstance(exc, TRANSIENT) or isinstance(exc, TransferError) and "took too long" in str(exc):
                        job["last_error"] = "The Telegram connection or file reference failed. Retrying this message."
                        if retry["transient"] < len(BACKOFF):
                            delay = BACKOFF[retry["transient"]]
                            retry["transient"] += 1
                            await self._wait(job, delay, "sending")
                        else:
                            job.update(status="paused", pause_reason="The message still could not be copied after four retries.",
                                       paused_at_source_id=source_id)
                    elif (isinstance(exc, errors.RPCError) and job["options"].get("try_over_limit")
                          and any(k in f"{type(exc).__name__} {exc.message}" for k in REFUSED_SIZE)):
                        job["options"]["try_over_limit"] = False
                        reason = (f"Telegram refused message {source_id} over the size limit "
                                  f"({type(exc).__name__}: {str(exc).split(' (caused')[0]}). "
                                  "It stays paused; nothing was skipped.")
                        job.update(status="paused", pause_reason=reason, last_error=reason, paused_at_source_id=source_id)
                    elif isinstance(exc, (errors.RPCError, TransferError, ValueError, OSError)):
                        reason = str(exc) if isinstance(exc, TransferError) else "Telegram could not copy this message. Check access, media and free disk space."
                        job.update(status="paused", pause_reason=reason, last_error=reason, paused_at_source_id=source_id)
                    else:
                        job.update(status="failed", last_error="The copy stopped because of an unexpected connector error.",
                                   paused_at_source_id=source_id)
                    log.warning("Copy job %s at source %s: %s", job["job_id"], source_id, type(exc).__name__)
                    groups, recover = [], True
            if job["status"] in ACTIVE:
                job.update(status="done", wait_until=None)
            job["stats"]["current"] = None
            self._save(job)
            await self._notify(job)
        except asyncio.CancelledError:
            # A send may already have reached Telegram. Leave the pending journal
            # and running/waiting status intact on shutdown for startup recovery.
            raise
        except Exception:
            # A storage failure must stop sends, never continue without a journal.
            job.update(status="failed", last_error="Could not save copy job state. Check copy-jobs storage before resuming.")
            log.error("Copy job %s stopped: state storage or worker failure.", job["job_id"])
            await self._notify(job)
