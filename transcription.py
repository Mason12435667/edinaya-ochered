from __future__ import annotations

import base64
import binascii
import os
import tempfile
import threading
from pathlib import Path


_MODEL = None
_MODEL_LOCK = threading.Lock()
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def _technical_detail(error: BaseException) -> str:
    detail = str(error).replace("\n", " ").strip()
    return detail[:220]


def friendly_transcription_error(error: BaseException) -> str:
    detail = _technical_detail(error)
    lowered = detail.casefold()
    if isinstance(error, MemoryError) or any(token in lowered for token in ("out of memory", "cannot allocate memory", "bad_alloc")):
        reason = "На сервере не хватает оперативной памяти для модели Whisper. Закройте лишние процессы или используйте модель tiny/base"
    elif isinstance(error, PermissionError) or "permission denied" in lowered:
        reason = "Нет прав на чтение аудио или запись кэша модели. Проверьте права пользователя queueapp на /var/lib/edinaya-ochered"
    elif isinstance(error, FileNotFoundError):
        reason = "Нужный аудиофайл или файл модели не найден. Возможно, вложение уже удалено или кэш модели повреждён"
    elif "no space left on device" in lowered:
        reason = "На диске закончилось свободное место. Освободите место и повторите распознавание"
    elif any(token in lowered for token in ("huggingface", "couldn't connect", "connection error", "temporary failure in name resolution", "name or service not known", "network is unreachable", "connection timed out")):
        reason = "Сервер не смог скачать модель Whisper. Проверьте интернет/DNS и доступ к Hugging Face, затем повторите"
    elif any(token in lowered for token in ("401", "403", "unauthorized", "forbidden")):
        reason = "Серверу отказано в доступе при загрузке модели. Проверьте сетевые ограничения, прокси или доступ к Hugging Face"
    elif any(token in lowered for token in ("cuda", "cudnn", "cublas")):
        reason = "Whisper пытается использовать GPU, но CUDA настроена неправильно. Для этого сервера поставьте QUEUE_WHISPER_DEVICE=cpu и QUEUE_WHISPER_COMPUTE=int8"
    elif any(token in lowered for token in ("invalid data", "invalid argument", "decoder", "unsupported", "averror", "format")):
        reason = "Не удалось декодировать голосовое. Файл может быть повреждён или иметь неподдерживаемый формат"
    else:
        reason = "Не удалось выполнить распознавание голосового"
    if detail:
        return f"{reason}. Техническая причина: {detail}"
    return reason


def _audio_suffix(mimetype: str) -> str:
    value = (mimetype or "").lower()
    if "ogg" in value or "opus" in value:
        return ".ogg"
    if "mpeg" in value or "mp3" in value:
        return ".mp3"
    if "mp4" in value or "m4a" in value:
        return ".m4a"
    if "wav" in value:
        return ".wav"
    return ".audio"


def _whisper_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel

            _MODEL = WhisperModel(
                os.getenv("QUEUE_WHISPER_MODEL", "base"),
                device=os.getenv("QUEUE_WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("QUEUE_WHISPER_COMPUTE", "int8"),
            )
    return _MODEL


def transcribe_audio_payload(encoded_audio: str, mimetype: str = "") -> dict[str, object]:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return {
            "available": False,
            "text": "",
            "reason": "Модуль faster-whisper не установлен",
        }

    try:
        audio = base64.b64decode(encoded_audio, validate=True)
    except (ValueError, binascii.Error):
        return {"available": True, "text": "", "reason": "Некорректные аудиоданные"}
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        return {"available": True, "text": "", "reason": "Аудио пустое или слишком большое"}

    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=_audio_suffix(mimetype),
            delete=False,
        ) as temporary:
            temporary.write(audio)
            temporary_path = temporary.name
        segments, _ = _whisper_model().transcribe(
            temporary_path,
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return {"available": True, "text": text[:4000], "reason": ""}
    except Exception as error:
        return {
            "available": True,
            "text": "",
            "reason": friendly_transcription_error(error),
        }
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)
