"""Адреса чата для сайта: история, файлы вложений, отправка вложений и пересылка сообщений.

Подмешивается в queue_whatsapp_handlers.WhatsAppHandlerMixin, откуда берёт `whatsapp`
(WhatsAppDependencies) и методы чтения запроса.
"""
from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path

import queue_performance
import queue_realtime
import queue_reliability
import queue_uploads
from queue_chat_state import dedupe_whatsapp_messages
from queue_core import content_disposition_header, normalize_message, valid_conversation_id, valid_group_id
from queue_work_context import work_actor


class WhatsAppChatRoutes:
    def handle_chat_history(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        cursor = normalize_message(query.get("cursor", [""])[0])[:80]
        if not chat_id:
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Не выбран чат"}, HTTPStatus.BAD_REQUEST)
            return
        page = self.whatsapp.store.list_saved_whatsapp_messages_page(chat_id, 50, cursor)
        messages = self.whatsapp.decorate_chat_messages(chat_id, dedupe_whatsapp_messages(list(page.get("messages", []))))
        self.json_response({
            "messages": messages,
            "has_more": bool(page.get("has_more")),
            "cursor": str(page.get("cursor", "")),
        })

    def handle_chat_media(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        message_id = normalize_message(query.get("message_id", [""])[0])[:160]
        message = self.whatsapp.store.get_whatsapp_message(chat_id, message_id) if chat_id and message_id else None
        if not message or bool(message.get("deleted")):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        media_path = str(message.get("media_path", "") or "")
        if not media_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            path = Path(media_path).resolve()
            media_root = self.whatsapp.media_dir.resolve()
            if media_root not in path.parents or not path.is_file():
                raise ValueError
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = str(message.get("media_mime", "") or "application/octet-stream")
        download = query.get("download", [""])[0] == "1"
        media_name = str(message.get("media_name", "") or "Вложение")
        # Active documents must not execute in the application's origin.
        mime = queue_uploads.safe_mime(content_type)
        previewable = mime.startswith(('audio/','video/')) or mime in {
            'image/jpeg','image/png','image/webp','image/gif','application/pdf','text/plain'
        }
        disposition = content_disposition_header("attachment" if download or not previewable else "inline", media_name)
        queue_uploads.serve_range(self, path, content_type, disposition)

    def handle_chat_media_send(self) -> None:
        payload = self.read_json_body(18_000_000)
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        if not self.claim_conversation_write(chat_id):
            return
        body = normalize_message(str(payload.get("message", "")))[:1000]
        reply_to = normalize_message(str(payload.get("reply_to", "")))[:160]
        mimetype = normalize_message(str(payload.get("mimetype", "")))[:120]
        filename = normalize_message(str(payload.get("filename", "")))[:180] or "Вложение"
        media_base64 = str(payload.get("media_base64", "") or "")
        if not chat_id or not media_base64:
            self.json_response({"queued": False, "error": "Не выбран чат или файл"}, HTTPStatus.BAD_REQUEST)
            return
        valid_upload, upload_error = queue_reliability.upload_validation(filename, mimetype, len(media_base64), self.whatsapp.max_media_bytes)
        if not valid_upload:
            self.json_response({"queued": False, "error": upload_error}, HTTPStatus.BAD_REQUEST)
            return
        media_path = self.whatsapp.save_outbound_media(media_base64, mimetype, filename)
        if not media_path:
            self.json_response({"queued": False, "error": queue_performance.friendly_error(f"Не удалось сохранить вложение. Проверьте файл и лимит {self.whatsapp.max_media_bytes // (1024*1024)} МБ")}, HTTPStatus.BAD_REQUEST)
            return
        mentions = payload.get("mentions", [])
        if not isinstance(mentions, list):
            mentions = []
        message_id = self.whatsapp.store.queue_direct_message(
            chat_id, body, work_actor(), [str(x) for x in mentions[:100]],
            reply_to_key=reply_to, media_path=media_path, media_mime=mimetype, media_name=filename,
        )
        if not message_id:
            try: Path(media_path).unlink()
            except OSError: pass
        else:
            queue_realtime.notify_outbound()
        self.json_response({"queued": bool(message_id), "message_id": message_id}, HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST)

    def handle_chat_media_send_raw(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        if not self.claim_conversation_write(chat_id):
            return
        body = normalize_message(query.get("message", [""])[0])[:1000]
        reply_to = normalize_message(query.get("reply_to", [""])[0])[:160]
        mimetype = normalize_message(query.get("mimetype", [""])[0])[:120] or normalize_message(self.headers.get("Content-Type", ""))[:120]
        filename = normalize_message(query.get("filename", [""])[0])[:180] or "Вложение"
        raw_mentions = query.get("mentions", [""])[0]
        mentions = [
            item.strip() for item in raw_mentions.split(",")
            if valid_conversation_id(item.strip()) or valid_group_id(item.strip())
        ][:100]
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if not chat_id or content_length <= 0:
            self.json_response({"queued": False, "error": "Не выбран чат или файл"}, HTTPStatus.BAD_REQUEST)
            return
        if content_length > self.whatsapp.max_media_bytes:
            self.json_response(
                {"queued": False, "error": f"Файл превышает лимит {self.whatsapp.max_media_bytes // (1024*1024)} МБ"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        # Existing validation expects the encoded payload size. Keep the same
        # safety check while streaming the actual bytes directly to disk.
        encoded_length = ((content_length + 2) // 3) * 4
        valid_upload, upload_error = queue_reliability.upload_validation(filename, mimetype, encoded_length, self.whatsapp.max_media_bytes)
        if not valid_upload:
            self.json_response({"queued": False, "error": upload_error}, HTTPStatus.BAD_REQUEST)
            return
        media_path = self.whatsapp.save_outbound_media_stream(self, content_length, mimetype, filename)
        if not media_path:
            self.json_response({"queued": False, "error": "Не удалось сохранить вложение"}, HTTPStatus.BAD_REQUEST)
            return
        message_id = self.whatsapp.store.queue_direct_message(
            chat_id, body, work_actor(), mentions,
            reply_to_key=reply_to, media_path=media_path, media_mime=mimetype, media_name=filename,
        )
        if not message_id:
            try:
                Path(media_path).unlink()
            except OSError:
                pass
        else:
            queue_realtime.notify_outbound()
        self.json_response(
            {"queued": bool(message_id), "message_id": message_id},
            HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
        )

    def handle_chat_forward(self) -> None:
        payload = self.read_json_body(250_000)
        if payload is None:
            return
        source_chat = valid_conversation_id(str(payload.get("source_chat_id", "")))
        target_chat = valid_conversation_id(str(payload.get("target_chat_id", "")))
        if target_chat and not self.claim_conversation_write(target_chat):
            return
        raw_ids = payload.get("message_ids", [])
        if not source_chat or not target_chat or not isinstance(raw_ids, list):
            self.json_response({"queued": False, "error": "Некорректные данные пересылки"}, HTTPStatus.BAD_REQUEST)
            return
        ids: list[str] = []
        for value in raw_ids[:20]:
            mid = normalize_message(str(value))[:160]
            if mid and mid not in ids and self.whatsapp.store.get_whatsapp_message(source_chat, mid):
                ids.append(mid)
        queued = []
        action_ids: list[int] = []
        action_message_ids: list[str] = []
        blocked_mentions = 0
        for mid in ids:
            message = self.whatsapp.store.get_whatsapp_message(source_chat, mid) or {}
            if self.whatsapp.mention_only_message(message):
                blocked_mentions += 1
                continue
            payload = json.dumps(
                {
                    "target_chat_id": target_chat,
                    "body": str(message.get("body", "") or ""),
                    "timestamp": int(message.get("message_timestamp", 0) or 0),
                    "media_name": str(message.get("media_name", "") or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            action_id = self.whatsapp.store.queue_whatsapp_action("forward", source_chat, mid, payload, work_actor())
            if action_id:
                queued.append(action_id)
                action_ids.append(int(action_id))
                action_message_ids.append(mid)
        if queued:
            queue_realtime.notify_outbound()
        if not queued and blocked_mentions:
            self.json_response(
                {"queued": False, "count": 0, "blocked_count": blocked_mentions, "error": "Нельзя пересылать сообщение, состоящее только из тега пользователя"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        self.json_response(
            {
                "queued": bool(queued), "count": len(queued), "blocked_count": blocked_mentions,
                "action_ids": action_ids, "action_message_ids": action_message_ids,
            },
            HTTPStatus.OK if queued else HTTPStatus.BAD_REQUEST,
        )
