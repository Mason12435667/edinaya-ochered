"""Preserve outgoing attachments before the delivery queue removes temporary files."""
import os
import shutil
from pathlib import Path
from queue_core import log_suppressed
from ticketing import TicketStore


class AttachmentArchive:
    def __init__(self, store: TicketStore, media_dir: Path, outbound_media_dir: Path, upload_dir: Path):
        self.store = store
        self.media_dir = media_dir
        self.outbound_media_dir = outbound_media_dir
        self.upload_dir = upload_dir

    def preserve(self, message_id: int) -> dict:
        """Keep a local copy of a successfully sent attachment for chat history.

        The WhatsApp queue deletes its temporary outbound file after a successful send.
        Without this preserved copy the remote WhatsApp message is correct, but the
        local bubble can later contain only the caption.  Preserve first, then let the
        queue clean the temporary file.
        """
        with self.store.connection() as db:
            row = db.execute(
                'SELECT media_path,media_mime,media_name FROM outbound_messages WHERE id=?',
                (message_id,),
            ).fetchone()
        if not row:
            return {}

        destination = self.media_dir / ('outbound-' + str(message_id) + '.bin')
        destination.parent.mkdir(parents=True, exist_ok=True)
        mime = str(row['media_mime'] or 'application/octet-stream')
        name = str(row['media_name'] or 'Вложение')

        # Idempotent result callbacks must keep returning the already-preserved copy.
        if destination.is_file():
            return {
                'media_path': str(destination), 'media_mime': mime, 'media_name': name,
                'type': 'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'document',
            }

        if not row['media_path']: return {}
        source = Path(str(row['media_path'])).resolve()
        allowed_roots = [self.outbound_media_dir.resolve()]
        try:
            allowed_roots.append(Path(self.upload_dir).resolve())
        except (OSError, TypeError):
            log_suppressed("queue_attachment_archive.preserve")
        if not any(root == source.parent or root in source.parents for root in allowed_roots):
            return {}
        if not source.is_file():
            return {}

        try:
            # Same data volume in the normal install: zero-copy and safe.
            os.link(source, destination)
        except OSError:
            # Cross-filesystem fallback.  Keep the source until the queue marks the
            # send complete, instead of moving it out from under the sender.
            shutil.copy2(source, destination)

        return {
            'media_path': str(destination), 'media_mime': mime, 'media_name': name,
            'type': 'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'document',
        }
