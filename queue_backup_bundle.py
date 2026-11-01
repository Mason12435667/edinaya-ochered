"""Database snapshot plus application media; no credentials or WhatsApp session."""
from __future__ import annotations
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def create_bundle(database_path: Path, data_dir: Path, backup_dir: Path) -> Path:
    data_dir = data_dir.resolve()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    target = backup_dir / f'tickets-{stamp}.db'
    archive = target.with_suffix('.tar.gz')
    # Staging on the same filesystem makes final renames atomic.
    with tempfile.TemporaryDirectory(prefix='.backup-', dir=backup_dir) as work:
        stage = Path(work)
        snapshot = stage / 'tickets.db'
        with sqlite3.connect(database_path, timeout=15) as source:
            destination = sqlite3.connect(snapshot)
            try:
                source.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Database integrity check failed')
                destination.commit()
            finally:
                destination.close()
        paths = {}
        for folder in ('chat-media', 'outbound-media', 'avatars'):
            root = data_dir / folder
            if root.exists():
                for path in root.rglob('*'):
                    if path.is_symlink():
                        raise RuntimeError('Symbolic link in media directory')
                    if path.is_file() and path.suffix != '.part':
                        paths[str(path.relative_to(data_dir))] = path
        flag = data_dir / 'auto-reply.enabled'
        if flag.is_file(): paths['auto-reply.enabled'] = flag
        # Every referenced file must be included, or this is not a full backup.
        with sqlite3.connect(snapshot) as db:
            for table in ('whatsapp_chat_messages', 'outbound_messages'):
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                for (raw,) in db.execute(f"SELECT DISTINCT media_path FROM {table} WHERE media_path<>''"):
                    path = Path(raw)
                    if not path.is_absolute(): path = data_dir / path
                    resolved = path.resolve()
                    if not resolved.is_relative_to(data_dir) or path.is_symlink():
                        raise RuntimeError('Referenced media is outside data directory')
                    if not path.is_file():
                        # Sent queue entries retain an obsolete temporary path;
                        # their authoritative history copy is in chat-media.
                        if table == 'outbound_messages':
                            rows = db.execute('SELECT id,status FROM outbound_messages WHERE media_path=?', (raw,)).fetchall()
                            if rows and all(status == 'sent' and (data_dir/'chat-media'/f'outbound-{mid}.bin').is_file() for mid,status in rows):
                                continue
                        raise RuntimeError('Referenced attachment missing; backup not published')
                    paths[str(resolved.relative_to(data_dir))] = path
        paths['tickets.db'] = snapshot
        manifest = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'data_dir': str(data_dir), 'files': {}}
        staged_archive = stage / 'bundle.tar.gz'
        with tarfile.open(staged_archive, 'w:gz') as tar:
            for name, path in sorted(paths.items()):
                if path.is_symlink(): raise RuntimeError('Symbolic link rejected')
                with path.open('rb') as stream:
                    before = os.fstat(stream.fileno())
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib,'file_digest') else _digest(stream)
                    stream.seek(0)
                    info = tar.gettarinfo(str(path), arcname=name)
                    info.mode = 0o600
                    tar.addfile(info, stream)
                    after = os.fstat(stream.fileno())
                    if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                        raise RuntimeError('Attachment changed during backup')
                    manifest['files'][name] = digest
            payload = json.dumps(manifest,ensure_ascii=False,indent=2).encode()
            info=tarfile.TarInfo('manifest.json'); info.size=len(payload); info.mode=0o600
            tar.addfile(info,io.BytesIO(payload))
        # Read every archived byte back, detecting truncation/corruption.
        with tarfile.open(staged_archive,'r:gz') as tar:
            for name, expected in manifest['files'].items():
                with tar.extractfile(name) as stream:
                    if _digest(stream) != expected: raise RuntimeError('Archive checksum mismatch')
        restore_result = verify_restore(staged_archive, backup_dir)
        os.chmod(snapshot,0o600);os.chmod(staged_archive,0o600)
        staged_archive.replace(archive)
        try:
            snapshot.replace(target)  # completion marker used by existing UI
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
    restore_result['archive'] = archive.name
    report=backup_dir/'restore-check.json'
    report_tmp=report.with_suffix('.tmp')
    report_tmp.write_text(json.dumps(restore_result,ensure_ascii=False),encoding='utf-8')
    report_tmp.replace(report)
    return target


def _digest(stream):
    digest=hashlib.sha256()
    while True:
        block=stream.read(1024*1024)
        if not block: break
        digest.update(block)
    return digest.hexdigest()


def verify_restore(archive: Path, parent: Path | None = None) -> dict:
    """Restore in isolation, then check database and every media reference."""
    with tempfile.TemporaryDirectory(prefix='.restore-check-',dir=parent) as work:
        root=Path(work)
        with tarfile.open(archive,'r:gz') as tar:
            member=tar.getmember('manifest.json')
            if member.size>16*1024*1024:raise RuntimeError('Manifest too large')
            manifest=json.load(tar.extractfile(member))
            files=manifest.get('files',{})
            if 'tickets.db' not in files:raise RuntimeError('Database absent from manifest')
            for name,expected in files.items():
                relative=Path(name)
                if relative.is_absolute() or '..' in relative.parts:raise RuntimeError('Unsafe archive path')
                member=tar.getmember(name)
                if not member.isfile():raise RuntimeError('Only regular files can be restored')
                destination=root/relative;destination.parent.mkdir(parents=True,exist_ok=True)
                digest=hashlib.sha256()
                with tar.extractfile(member) as src,destination.open('wb') as dst:
                    while True:
                        block=src.read(1024*1024)
                        if not block:break
                        dst.write(block);digest.update(block)
                if digest.hexdigest()!=expected:raise RuntimeError('Restored checksum mismatch')
        with sqlite3.connect(root/'tickets.db') as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Restored database corrupt')
            count=db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]
            original=Path(manifest['data_dir'])
            for table in ('whatsapp_chat_messages','outbound_messages'):
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
                for raw, in db.execute(f"SELECT DISTINCT media_path FROM {table} WHERE media_path<>''"):
                    path=Path(raw);relative=path.relative_to(original) if path.is_absolute() else path
                    if '..' in relative.parts:raise RuntimeError('Unsafe media reference')
                    if (root/relative).is_file():continue
                    if table=='outbound_messages':
                        rows=db.execute('SELECT id,status FROM outbound_messages WHERE media_path=?',(raw,)).fetchall()
                        if rows and all(status=='sent' and (root/'chat-media'/f'outbound-{mid}.bin').is_file() for mid,status in rows):continue
                    raise RuntimeError('Restored media reference missing')
        return {'ok':True,'checked_at':datetime.now(timezone.utc).isoformat(),'tickets':count,'files':len(files),'archive':archive.name}
