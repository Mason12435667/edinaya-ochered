"""AvatarCache в отдельной папке: что сохраняется, что отклоняется и на что ссылается страница."""
import base64
import tempfile
import unittest
from pathlib import Path

from queue_avatars import AvatarCache

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
CHAT = "77010000007@c.us"


class AvatarCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "avatars"
        self.cache = AvatarCache(self.root)

    def save(self, data: bytes, chat: str = CHAT, **extra):
        self.cache.save({"chat_id": chat, "image_base64": base64.b64encode(data).decode(), **extra})

    def test_saved_image_gets_a_versioned_url_inside_the_cache_directory(self):
        self.assertEqual(self.cache.url(CHAT), "")
        self.save(PNG)
        self.assertEqual(self.cache.path(CHAT).parent, self.root)
        self.assertEqual(self.cache.path(CHAT).read_bytes(), PNG)
        self.assertTrue(self.cache.url(CHAT).startswith("/api/avatar?chat_id=77010000007%40c.us&v="))

    def test_unknown_chat_and_non_image_are_rejected(self):
        with self.assertRaises(ValueError):
            self.save(PNG, chat="not-a-chat")
        with self.assertRaises(ValueError):
            self.save(b"plain text, not an image")
        self.assertFalse(self.root.exists())

    def test_missing_photo_keeps_the_cached_one(self):
        self.save(PNG)
        self.cache.save({"chat_id": CHAT, "missing": True})
        self.assertEqual(self.cache.path(CHAT).read_bytes(), PNG)


if __name__ == "__main__":
    unittest.main()
