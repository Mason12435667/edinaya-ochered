"""Вложения и сообщения чата: сохранение файлов, удаление у всех, восстановление своего вложения и подготовка сообщений к показу."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote

import queue_features
import queue_message_identity
import queue_performance
import queue_uploads
from queue_attachment_archive import AttachmentArchive
from queue_avatars import AvatarCache
from queue_core import log_suppressed, media_extension, normalize_message, valid_conversation_id
from queue_work_context import cached_snapshot_value
from ticketing import TicketStore


def whatsapp_message_stanza_id(message_id: str) -> str:
    value = normalize_message(str(message_id or ""))[:160]
    if not value:
        return ""
    return value.rsplit("_", 1)[-1] if "_" in value else value


def same_whatsapp_message_identity(left: str, right: str) -> bool:
    left_value = normalize_message(str(left or ""))[:160]
    right_value = normalize_message(str(right or ""))[:160]
    if not left_value or not right_value:
        return False
    if left_value == right_value:
        return True
    left_stanza = whatsapp_message_stanza_id(left_value)
    right_stanza = whatsapp_message_stanza_id(right_value)
    return bool(left_stanza and right_stanza and left_stanza == right_stanza)


def mention_only_message(message: dict[str, object]) -> bool:
    """Return True only when the message contains mentions and no real text/media."""
    if not isinstance(message, dict):
        return False
    if str(message.get("media_path", "") or "").strip() or str(message.get("media_name", "") or "").strip():
        return False
    if str(message.get("quoted_message_key", "") or "").strip():
        return False
    body = str(message.get("body", "") or "").strip()
    if not body:
        return False

    rest = body
    raw_mentions = message.get("mentions", [])
    if isinstance(raw_mentions, list):
        exact_tokens: list[str] = []
        for mention in raw_mentions:
            if not isinstance(mention, dict):
                continue
            for key in ("name", "id", "resolved_id"):
                value = str(mention.get(key, "") or "").strip()
                if not value:
                    continue
                if "@" in value and not value.startswith("@"):
                    value = value.split("@", 1)[0]
                value = value.lstrip("@").strip()
                if value:
                    exact_tokens.append("@" + value)
        for token in sorted(set(exact_tokens), key=len, reverse=True):
            rest = re.sub(re.escape(token), "", rest, flags=re.IGNORECASE)

    # WhatsApp stores mention bodies using a compact @identifier. Keep the
    # fallback deliberately strict so normal text containing @ is never blocked.
    rest = re.sub(r"@[\w.+:-]+", "", rest, flags=re.UNICODE)
    return not rest.strip() and "@" in body


_MEDIA_PLACEHOLDER_BODIES = {
    "[фото]", "фото", "[image]", "image",
    "[видео]", "видео", "[video]", "video",
    "[аудио]", "аудио", "[audio]", "audio",
    "[документ]", "документ", "[document]", "document",
    "[вложение]", "вложение", "[attachment]", "attachment",
    "[стикер]", "стикер", "[sticker]", "sticker",
}


def _observed_outbound_body(body: object, message_type: object = "") -> str:
    value = normalize_message(str(body or ""))[:1000]
    safe_type = normalize_message(str(message_type or "")).casefold()
    if safe_type in {"image", "photo", "video", "audio", "ptt", "document", "sticker"}:
        if value.casefold() in _MEDIA_PLACEHOLDER_BODIES:
            return ""
    return value


def _outbound_media_matches_observation(
    row: object,
    observed_body: object = "",
    observed_reply_to: object = "",
    observed_type: object = "",
) -> bool:
    """Return True only when an outbound media row belongs to this WhatsApp echo.

    An old uncertain attachment could be rebound to the next plain
    text/reply in the same chat because an empty queued caption was treated as
    a wildcard. Body and reply context are now strict identity signals.
    """
    try:
        queued_body = normalize_message(str(row["body"] or ""))[:1000]
    except (KeyError, IndexError):
        log_suppressed("queue_chat_media._outbound_media_matches_observation")
        queued_body = ""
    current_body = _observed_outbound_body(observed_body, observed_type)
    if queued_body != current_body:
        return False

    try:
        queued_reply = normalize_message(str(row["reply_to_key"] or ""))[:160]
    except (KeyError, IndexError):
        log_suppressed("queue_chat_media._outbound_media_matches_observation")
        queued_reply = ""
    current_reply = normalize_message(str(observed_reply_to or ""))[:160]
    if bool(queued_reply) != bool(current_reply):
        return False
    if queued_reply and not same_whatsapp_message_identity(queued_reply, current_reply):
        return False
    return True


class ChatMedia:
    """Файлы вложений и сообщения чата для одной базы; app.py создаёт один экземпляр.

    `chat_state` меняется на месте под `chat_lock`, `employees` — тот же список, что у остальных частей
    приложения. `display_names` возвращает текущий справочник имён (ChatViews.display_names).
    """

    def __init__(
        self, store: TicketStore, attachments: AttachmentArchive, avatars: AvatarCache, *,
        database_path: Path, media_dir: Path, outbound_media_dir: Path, max_media_bytes: int,
        chat_lock: Lock, chat_state: dict[str, object], employees: list[str], display_names: Callable[[], Any],
    ) -> None:
        self.store = store
        self.attachments = attachments
        self.avatars = avatars
        self.database_path = database_path
        self.media_dir = media_dir
        self.outbound_media_dir = outbound_media_dir
        self.max_media_bytes = max_media_bytes
        self.chat_lock = chat_lock
        self.chat_state = chat_state
        self.employees = employees
        self.display_names = display_names

    def save_media_payload(
        self,
        chat_id: str, message_id: str, media_base64: str, mimetype: str, filename: str
    ) -> str:
        if not media_base64 or not chat_id or not message_id:
            return ""
        try:
            with queue_performance.media_slot():
                try:
                    raw = base64.b64decode(media_base64, validate=True)
                except (ValueError, binascii.Error):
                    return ""
                if not raw or len(raw) > self.max_media_bytes:
                    return ""
                digest = hashlib.sha256(f"{chat_id}|{message_id}".encode("utf-8")).hexdigest()
                path = self.media_dir / f"{digest}{media_extension(mimetype, filename)}"
                try:
                    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
                    try:
                        temporary.write_bytes(raw)
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
                    try:
                        path.chmod(0o640)
                    except OSError:
                        pass
                except OSError:
                    return ""
                return str(path)
        except TimeoutError:
            return ""

    def scrub_deleted_whatsapp_message(
        self,
        chat_id: str,
        message_id: str,
        media_path: str = "",
        message_timestamp: int = 0,
        from_me: bool | None = None,
    ) -> tuple[str, int]:
        """Scrub the original row after WhatsApp 'delete for everyone'.

        WhatsApp may report the same direct chat as @lid while our DB already knows it
        as @c.us. Matching only an exact (chat_id, message_key) pair could leave the
        original row intact and insert a second tombstone. Here we reconcile
        by the stable WhatsApp stanza id and direction/time, scrub the original row(s),
        and return the original key so the caller does not create a duplicate bubble.
        """
        safe_chat_id = valid_conversation_id(chat_id)
        safe_message_id = normalize_message(message_id)[:160]
        if not safe_chat_id or not safe_message_id:
            return safe_message_id, 0

        resolved_key = safe_message_id
        matched_rows: list[tuple[int, str, str, str, int, int]] = []
        media_paths: set[str] = set()
        if media_path:
            media_paths.add(str(media_path))

        try:
            with sqlite3.connect(self.database_path, timeout=5.0) as connection:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(whatsapp_chat_messages)")}
                required = {"id", "chat_id", "message_key"}
                if required.issubset(columns):
                    select_media = "media_path" if "media_path" in columns else "''"
                    select_ts = "message_timestamp" if "message_timestamp" in columns else "0"
                    select_from = "from_me" if "from_me" in columns else "0"
                    rows = connection.execute(
                        f"SELECT id, chat_id, message_key, {select_media}, {select_ts}, {select_from} "
                        "FROM whatsapp_chat_messages WHERE message_key=? ORDER BY id DESC",
                        (safe_message_id,),
                    ).fetchall()

                    if not rows:
                        stanza = whatsapp_message_stanza_id(safe_message_id)
                        if stanza:
                            # Keep the query bounded to recent candidates when a timestamp is
                            # available; the stanza id is otherwise already highly specific.
                            if "message_timestamp" in columns and message_timestamp > 0:
                                rows = connection.execute(
                                    f"SELECT id, chat_id, message_key, {select_media}, {select_ts}, {select_from} "
                                    "FROM whatsapp_chat_messages "
                                    "WHERE message_timestamp BETWEEN ? AND ? ORDER BY id DESC LIMIT 250",
                                    (max(0, int(message_timestamp) - 300), int(message_timestamp) + 300),
                                ).fetchall()
                            else:
                                rows = connection.execute(
                                    f"SELECT id, chat_id, message_key, {select_media}, {select_ts}, {select_from} "
                                    "FROM whatsapp_chat_messages ORDER BY id DESC LIMIT 1200"
                                ).fetchall()
                            rows = [row for row in rows if whatsapp_message_stanza_id(str(row[2] or "")) == stanza]

                    for row in rows:
                        row_from_me = bool(int(row[5] or 0))
                        if from_me is not None and row_from_me != bool(from_me):
                            continue
                        row_ts = int(row[4] or 0)
                        if message_timestamp > 0 and row_ts > 0 and abs(row_ts - int(message_timestamp)) > 300:
                            continue
                        matched_rows.append((int(row[0]), str(row[1] or ""), str(row[2] or ""), str(row[3] or ""), row_ts, int(row[5] or 0)))

                    if matched_rows:
                        # Prefer the row whose chat id already canonicalizes to the current
                        # conversation, otherwise the newest exact/stanza match is safe.
                        canonical = self.store.canonical_whatsapp_chat_id(safe_chat_id) or safe_chat_id
                        preferred = matched_rows[0]
                        for candidate in matched_rows:
                            candidate_chat = self.store.canonical_whatsapp_chat_id(candidate[1]) or candidate[1]
                            if candidate_chat == canonical:
                                preferred = candidate
                                break
                        resolved_key = preferred[2] or safe_message_id
                        assignments: list[str] = []
                        for column in (
                            "body", "media_path", "media_url", "media_mime", "media_name",
                            "transcript", "quoted_body", "quoted_sender", "reactions_json"
                        ):
                            if column in columns:
                                assignments.append(f"{column}=''")
                        if "deleted" in columns:
                            assignments.append("deleted=1")
                        if assignments:
                            ids = [row[0] for row in matched_rows]
                            placeholders = ",".join("?" for _ in ids)
                            connection.execute(
                                f"UPDATE whatsapp_chat_messages SET {', '.join(assignments)} WHERE id IN ({placeholders})",
                                ids,
                            )
                            connection.commit()
                        for row in matched_rows:
                            if row[3]:
                                media_paths.add(row[3])
        except Exception as exc:
            print(f"WhatsApp remote-delete DB reconcile warning: {safe_message_id}: {exc}", flush=True)

        media_root = self.media_dir.resolve()
        for value in media_paths:
            try:
                candidate = Path(value).resolve()
                if media_root in candidate.parents and candidate.is_file():
                    candidate.unlink()
            except OSError as exc:
                print(f"WhatsApp remote-delete media cleanup warning: {safe_message_id}: {exc}", flush=True)

        return resolved_key, len(matched_rows)

    def save_outbound_media(self, media_base64: str, mimetype: str, filename: str) -> str:
        try:
            with queue_performance.media_slot():
                try:
                    raw = base64.b64decode(media_base64, validate=True)
                except (ValueError, binascii.Error):
                    return ""
                if not raw or len(raw) > self.max_media_bytes:
                    return ""
                digest = hashlib.sha256(raw + secrets.token_bytes(8)).hexdigest()
                path = self.outbound_media_dir / f"{digest}{media_extension(mimetype, filename)}"
                try:
                    path.write_bytes(raw)
                    path.chmod(0o640)
                except OSError:
                    return ""
                return str(path)
        except TimeoutError:
            return ""

    def save_outbound_media_stream(self, handler, content_length: int, mimetype: str, filename: str) -> str:
        """Save one raw upload directly to outbound-media without base64/RAM expansion."""
        if content_length <= 0 or content_length > self.max_media_bytes:
            return ""
        try:
            with queue_performance.media_slot():
                extension = media_extension(mimetype, filename)
                final_path = self.outbound_media_dir / f"{secrets.token_hex(24)}{extension}"
                temporary = final_path.with_name(final_path.name + "." + secrets.token_hex(6) + ".tmp")
                remaining = content_length
                # A small body may already have been read (CSRF check); replay it.
                source = io.BytesIO(handler.read_body(content_length) or b"") if handler.body_already_read() else handler.rfile
                try:
                    with temporary.open("wb") as stream:
                        while remaining > 0:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise OSError("upload ended before Content-Length")
                            stream.write(chunk)
                            remaining -= len(chunk)
                    if remaining != 0 or temporary.stat().st_size != content_length:
                        raise OSError("upload size mismatch")
                    temporary.replace(final_path)
                    final_path.chmod(0o640)
                    return str(final_path)
                except OSError:
                    temporary.unlink(missing_ok=True)
                    final_path.unlink(missing_ok=True)
                    return ""
        except TimeoutError:
            return ""

    # Recover the preserved local attachment for a message that was sent from the
    # Queue UI.  whatsapp-web.js can report the same outgoing message a moment later
    # as text/caption only, even though WhatsApp itself received the image/file.
    def preserved_system_outgoing_media(
        self,
        message_key: str,
        observed_body: str = "",
        observed_reply_to: str = "",
        observed_type: str = "",
    ) -> dict[str, str]:
        safe_key = normalize_message(str(message_key or ""))[:180]
        if not safe_key:
            return {}

        def same_provider_id(left: object, right: object) -> bool:
            a = str(left or "").strip()
            b = str(right or "").strip()
            if not a or not b:
                return False
            if a == b or a.endswith(b) or b.endswith(a):
                return True
            try:
                da, sa = queue_message_identity.parts(a)
                db, sb = queue_message_identity.parts(b)
                return bool(sa and sb and sa == sb and (not da or not db or da == db))
            except (TypeError, ValueError):
                log_suppressed("queue_chat_media.same_provider_id")
                return False

        candidates = []
        try:
            with self.store.connection() as connection:
                exact = connection.execute(
                    "SELECT id, provider_id, body, reply_to_key, media_mime, media_name FROM outbound_messages "
                    "WHERE status='sent' AND provider_id=? ORDER BY id DESC LIMIT 1",
                    (safe_key,),
                ).fetchone()
                if exact:
                    candidates.append(exact)
                else:
                    # Small bounded fallback for provider-id formatting differences.
                    candidates.extend(connection.execute(
                        "SELECT id, provider_id, body, reply_to_key, media_mime, media_name FROM outbound_messages "
                        "WHERE status='sent' AND provider_id<>'' ORDER BY id DESC LIMIT 80"
                    ).fetchall())
        except sqlite3.Error:
            log_suppressed("queue_chat_media.preserved_system_outgoing_media")
            return {}

        for row in candidates:
            if not same_provider_id(row["provider_id"], safe_key):
                continue
            if not _outbound_media_matches_observation(
                row, observed_body, observed_reply_to, observed_type
            ):
                continue
            try:
                media_path = self.media_dir / f"outbound-{int(row['id'])}.bin"
            except (TypeError, ValueError):
                continue
            if not media_path.is_file():
                continue
            return {
                "media_path": str(media_path),
                "media_mime": normalize_message(str(row["media_mime"] or ""))[:120],
                "media_name": normalize_message(str(row["media_name"] or ""))[:180],
            }
        return {}

    # whatsapp-web.js can occasionally return an empty send result even though the
    # media was actually accepted by WhatsApp.  In that case the outbound row is
    # intentionally left as "uncertain", while the fallback history sync observes
    # the real outgoing provider id a moment later.  Reconcile only a media row
    # from the same canonical chat and a tight timestamp window, then use the local
    # preserved outbound copy instead of trying to download our own image back from
    # WhatsApp.
    def reconcile_uncertain_system_outgoing_media(
        self,
        chat_id: str,
        message_key: str,
        message_timestamp: int,
        body: str = "",
        message_type: str = "",
        quoted_message_key: str = "",
    ) -> dict[str, str]:
        safe_chat = valid_conversation_id(str(chat_id or ""))
        safe_key = normalize_message(str(message_key or ""))[:180]
        if not safe_chat or not safe_key:
            return {}
        try:
            timestamp = int(message_timestamp or 0)
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp <= 0:
            return {}

        safe_type = normalize_message(str(message_type or "")).casefold()
        expected_prefix = {
            "image": "image/",
            "video": "video/",
            "audio": "audio/",
            "ptt": "audio/",
            "sticker": "image/",
        }.get(safe_type, "")
        observed_body = normalize_message(str(body or ""))[:1000]

        candidates: list[tuple[int, int, sqlite3.Row]] = []
        try:
            with self.store.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, chat_id, body, status, created_at, media_path,
                           media_mime, media_name, provider_id, reply_to_key
                      FROM outbound_messages
                     WHERE chat_id = ?
                       AND status IN ('uncertain','sending','processing')
                       AND (media_path <> '' OR media_mime <> '' OR media_name <> '')
                     ORDER BY id ASC
                     LIMIT 30
                    """,
                    (safe_chat,),
                ).fetchall()
        except sqlite3.Error:
            log_suppressed("queue_chat_media.reconcile_uncertain_system_outgoing_media")
            return {}

        for row in rows:
            provider_id = normalize_message(str(row["provider_id"] or ""))[:180]
            if provider_id and provider_id != safe_key:
                continue
            row_mime = normalize_message(str(row["media_mime"] or ""))[:120]
            if expected_prefix and row_mime and not row_mime.casefold().startswith(expected_prefix):
                continue
            if not _outbound_media_matches_observation(
                row, observed_body, quoted_message_key, message_type
            ):
                continue
            try:
                created_raw = str(row["created_at"] or "").strip()
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                created_epoch = int(created_dt.timestamp())
            except (TypeError, ValueError, OverflowError):
                continue
            delta = abs(timestamp - created_epoch)
            # A WhatsApp echo normally appears within a few seconds.  Keep the
            # window deliberately narrow to avoid binding an unrelated attachment.
            if delta > 30:
                continue
            try:
                row_id = int(row["id"])
            except (TypeError, ValueError):
                continue
            candidates.append((delta, row_id, row))

        if not candidates:
            return {}
        _, row_id, row = min(candidates, key=lambda value: (value[0], value[1]))

        media: dict[str, str] = {}
        preserved_path = self.media_dir / f"outbound-{row_id}.bin"
        if preserved_path.is_file():
            media = {
                "media_path": str(preserved_path),
                "media_mime": normalize_message(str(row["media_mime"] or ""))[:120],
                "media_name": normalize_message(str(row["media_name"] or ""))[:180],
            }
        else:
            try:
                preserved = self.attachments.preserve(row_id)
            except (OSError, sqlite3.Error):
                log_suppressed("queue_chat_media.reconcile_uncertain_system_outgoing_media")
                preserved = {}
            if preserved:
                media = {
                    "media_path": str(preserved.get("media_path", "") or ""),
                    "media_mime": normalize_message(str(preserved.get("media_mime", "") or row["media_mime"] or ""))[:120],
                    "media_name": normalize_message(str(preserved.get("media_name", "") or row["media_name"] or ""))[:180],
                }

        if not media.get("media_path") or not Path(str(media["media_path"])).is_file():
            return {}

        try:
            updated = self.store.complete_outbound_message(row_id, True, "", safe_key)
        except Exception as error:
            print(f"Не удалось связать локальное вложение #{row_id} с {safe_key}: {error}", flush=True)
            return {}
        if not updated:
            return {}
        print(
            f"Локальное исходящее вложение восстановлено без скачивания из WhatsApp: "
            f"queue={row_id} provider={safe_key}",
            flush=True,
        )
        return media

    def decorate_chat_messages(self, chat_id: str, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        # Для групп подменяем технические WhatsApp LID в @упоминаниях на понятные
        # имена из уже загруженного списка участников. Одновременно используем
        # sender_id сообщения, чтобы заменить «Участник группы» на реальное имя и
        # показать номер даже если имя/номер догрузились уже после самого сообщения.
        mention_names: dict[str, str] = {}
        participant_by_id: dict[str, dict[str, object]] = {}
        if str(chat_id).endswith("@g.us"):
            try:
                with self.chat_lock:
                    participants_map = self.chat_state.get("group_participants", {})
                    participants = list(participants_map.get(chat_id, [])) if isinstance(participants_map, dict) else []
                for participant in participants:
                    if not isinstance(participant, dict):
                        continue
                    mention_id = str(participant.get("mention_id", "")).strip()
                    resolved_id = str(participant.get("resolved_id", "")).strip()
                    name = str(participant.get("name", "")).strip()
                    phone = str(participant.get("phone", "")).strip()
                    for participant_id in (mention_id, resolved_id):
                        if participant_id:
                            participant_by_id[participant_id] = participant
                    if mention_id and name:
                        mention_names[mention_id.split("@")[0]] = name
                    if resolved_id and name:
                        mention_names[resolved_id.split("@")[0]] = name
                    if phone:
                        phone_digits = re.sub(r"\D", "", phone)
                        if phone_digits:
                            participant_by_id[f"{phone_digits}@c.us"] = participant
            except (AttributeError, TypeError, ValueError):
                log_suppressed("queue_chat_media.decorate_chat_messages")
                mention_names = {}
                participant_by_id = {}

        manual_names = {row["chat_id"]: row["name"] for row in cached_snapshot_value("manual_contacts", 2.5, self.store.list_manual_whatsapp_contacts)}
        quote_cache = {}
        result: list[dict[str, object]] = []
        generic_group_names = {"", "участник", "участник группы", "пользователь whatsapp"}
        for raw in messages:
            item = dict(raw)
            # Recover only a proven provider ID, never a caption/time guess.
            sent_row = None
            if item.get("from_me") and not item.get("deleted") and str(item.get("id", "")):
                with self.store.connection() as db:
                    sent_row = db.execute(
                        "SELECT id, body, reply_to_key, media_mime, media_name FROM outbound_messages "
                        "WHERE chat_id=? AND provider_id=? AND status='sent' LIMIT 1",
                        (chat_id, str(item.get("id", ""))),
                    ).fetchone()

            # Repair old poisoned bindings created by the former fuzzy
            # uncertain-media matcher. A plain text/reply must never inherit a
            # previous screenshot merely because it was sent in the same time window.
            if (
                item.get("from_me")
                and not item.get("deleted")
                and item.get("media_path")
                and sent_row
                and not _outbound_media_matches_observation(
                    sent_row,
                    str(item.get("body", "")),
                    str(item.get("quoted_message_key", "")),
                    str(item.get("message_type", item.get("type", ""))),
                )
            ):
                item["media_path"] = ""
                item["media_mime"] = ""
                item["media_name"] = ""
                try:
                    with self.store.connection() as db:
                        db.execute(
                            "UPDATE whatsapp_chat_messages SET media_path='', media_mime='', media_name='' "
                            "WHERE chat_id=? AND message_key=?",
                            (chat_id, str(item.get("id", ""))),
                        )
                        db.commit()
                except sqlite3.Error:
                    log_suppressed("queue_chat_media.decorate_chat_messages")

            if item.get("from_me") and not item.get("deleted") and not item.get("media_path"):
                media = {}
                if sent_row and _outbound_media_matches_observation(
                    sent_row,
                    str(item.get("body", "")),
                    str(item.get("quoted_message_key", "")),
                    str(item.get("message_type", item.get("type", ""))),
                ):
                    media = self.attachments.preserve(sent_row["id"])
                if not media:
                    media = self.reconcile_uncertain_system_outgoing_media(
                        chat_id,
                        str(item.get("id", "")),
                        int(item.get("timestamp", item.get("message_timestamp", 0)) or 0),
                        str(item.get("body", "")),
                        str(item.get("message_type", item.get("type", ""))),
                        str(item.get("quoted_message_key", "")),
                    )
                if not media:
                    # Repair already saved
                    # [Фото] rows by matching the still-present clipboard upload to
                    # this exact chat and WhatsApp timestamp.
                    media = queue_uploads.recover_clipboard_attachment(
                        self.store,
                        self.attachments,
                        chat_id,
                        str(item.get("id", "")),
                        int(item.get("timestamp", item.get("message_timestamp", 0)) or 0),
                        str(item.get("body", "")),
                        str(item.get("message_type", item.get("type", ""))),
                    )
                if media:
                    item.update(media)
                    self.store.save_whatsapp_chat_messages(chat_id, [item])
            own_ids = {key for key, participant in participant_by_id.items() if participant.get("is_me")}
            item["highlight_mention"] = queue_features.mention_match(item, self.employees, own_ids)
            if str(chat_id).endswith("@g.us"):
                sender_id = str(item.get("sender_id", "")).strip()
                participant = participant_by_id.get(sender_id) if participant_by_id else None
                if participant:
                    participant_name = str(participant.get("name", "")).strip()
                    participant_phone = str(participant.get("phone", "")).strip()
                    current_sender = str(item.get("sender", "")).strip()
                    current_folded = current_sender.casefold()
                    if participant_name and (
                        current_folded in generic_group_names
                        or re.fullmatch(r"[+\d\s().-]+", current_sender or "") is not None
                    ):
                        item["sender"] = participant_name
                    if participant_phone and not str(item.get("sender_phone", "")).strip():
                        item["sender_phone"] = participant_phone

                avatar_candidates: list[str] = []
                if participant:
                    avatar_candidates.extend([
                        str(participant.get("resolved_id", "") or "").strip(),
                        str(participant.get("mention_id", "") or "").strip(),
                    ])
                avatar_candidates.append(sender_id)
                item["sender_avatar_url"] = ""
                for avatar_id in avatar_candidates:
                    if not avatar_id:
                        continue
                    avatar_url = self.avatars.url(avatar_id)
                    if avatar_url:
                        item["sender_avatar_url"] = avatar_url
                        break
            if mention_names and item.get("body"):
                body = str(item.get("body", ""))
                for technical_id, display_name in mention_names.items():
                    body = re.sub(rf"@{re.escape(technical_id)}(?!\d)", f"@{display_name}", body)
                item["body"] = body
            if not item.get("from_me"):
                admin_id = str(item.get("sender_id", "")) if str(chat_id).endswith("@g.us") else chat_id
                item["sender"] = self.display_names().name(admin_id, item.get("sender", ""), item.get("sender_phone", ""))
            qid = str(item.get("quoted_message_key", ""))
            if qid:
                if qid not in quote_cache: quote_cache[qid] = self.store.resolve_whatsapp_message(chat_id, qid)
                original = quote_cache[qid]
                if not original or original.get("deleted"):
                    item["quoted_body"] = "Сообщение недоступно"
                    item["quote_unavailable"] = True
                else:
                    media = bool(original.get("media_path") or original.get("media_mime") or original.get("message_type") in {"image", "video", "audio", "ptt", "document", "sticker"})
                    item["quoted_body"] = queue_message_identity.media_label(original) if media else str(original.get("body", ""))[:1200]
                    sender_id = str(original.get("sender_id", "")) if str(chat_id).endswith("@g.us") else chat_id
                    item["quoted_sender"] = "Вы" if original.get("from_me") else self.display_names().name(sender_id, original.get("sender", ""), original.get("sender_phone", ""))
                    item["quoted_message_key"] = original["message_key"]
                    if str(original.get("media_mime", "")).startswith("image/") and original.get("media_path"):
                        item["quote_preview_url"] = f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(original['message_key'])}"
            message_id = str(item.get("id", ""))
            media_path = str(item.pop("media_path", "") or "")
            if media_path and message_id and not bool(item.get("deleted")):
                item["media_url"] = (
                    f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(message_id)}"
                )
            else:
                item["media_url"] = ""
            result.append(item)
        return result
