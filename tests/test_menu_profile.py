"""Bot menu: one source of truth in the database, edited on /admin/categories."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import support
from tests.support import WhatsAppUser, admin_cookie, get, post_form
import queue_generic_profile as qgp
from ticketing import TicketStore

app = support.app


def form_for(profile: dict, **changes) -> dict:
    """The /admin/categories form as the page submits it, with optional field changes."""
    form = {"greeting_ru": profile["greeting_ru"], "greeting_kz": profile["greeting_kz"]}
    for index, row in enumerate(profile["categories"], start=1):
        form.update({
            f"key_{index}": row["key"], f"number_{index}": str(row["number"]),
            f"label_ru_{index}": row["label_ru"], f"label_kz_{index}": row["label_kz"],
            f"prompt_ru_{index}": row["prompt_ru"], f"prompt_kz_{index}": row["prompt_kz"],
        })
        for name in row["required_fields"]:
            form[f"req_{name}_{index}"] = "1"
    form.update(changes)
    return form


class StoredProfileTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="eo-profile-"))
        self.store = TicketStore(self.folder / "tickets.db")
        self.config = self.folder / "company.json"
        self.config.write_text(json.dumps({"categories": [
            {"number": 1, "key": "general", "label_ru": "Из файла", "label_kz": "Файлдан"}]}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_first_start_seeds_the_database_then_the_file_is_not_read(self):
        with mock.patch.dict("os.environ", {"QUEUE_GENERIC_CONFIG": str(self.config)}):
            first = qgp.load_profile(self.store, self.folder)
            self.assertEqual(first["categories"][0]["label_ru"], "Из файла")
            self.assertTrue(self.store.get_setting(qgp.PROFILE_SETTING, ""))
            self.config.write_text(json.dumps({"categories": [
                {"number": 1, "key": "general", "label_ru": "Изменили файл", "label_kz": "x"}]}), encoding="utf-8")
            self.assertEqual(qgp.load_profile(self.store, self.folder)["categories"][0]["label_ru"], "Из файла")

    def test_form_cannot_inject_unknown_keys(self):
        current = qgp.validate_profile(qgp._DEFAULT)
        with self.assertRaises(ValueError):
            qgp.profile_from_form({"key_1": "custom_evil", "number_1": "1", "label_ru_1": "x"}, current)

    def test_form_adds_and_deletes_categories(self):
        current = qgp.validate_profile(qgp._DEFAULT)
        form = form_for(current, number_new="3", label_ru_new="Принтеры", req_reference_new="1")
        profile = qgp.validate_profile(qgp.profile_from_form(form, current))
        self.assertEqual([row["label_ru"] for row in profile["categories"]], ["Общее обращение", "Принтеры"])
        added = profile["categories"][1]
        self.assertTrue(added["key"].startswith("custom_c"))
        self.assertEqual(added["label_kz"], "Принтеры", "KZ label falls back to RU")
        self.assertEqual(added["required_fields"], ["reference"])
        removed = qgp.profile_from_form(form_for(profile, delete_2="1"), profile)
        self.assertEqual(len(removed["categories"]), 1)


class AdminCategoriesTests(unittest.TestCase):
    def setUp(self):
        self.original = qgp.load_profile(app.STORE, app.ROOT)
        self.cookie = admin_cookie()

    def tearDown(self):  # other tests rely on the configured test menu
        app.apply_bot_profile(qgp.save_profile(app.STORE, self.original))

    def test_page_shows_the_stored_profile(self):
        self.assertEqual(get("/admin/categories", self.cookie).status, 200)
        data = get("/api/admin-page?page=categories", self.cookie).text
        for row in self.original["categories"]:
            self.assertIn(row["label_ru"], data)

    def test_edit_changes_the_bot_menu_immediately(self):
        changes = {"label_ru_2": "Принтеры и сканеры", "greeting_ru": "Здравствуйте, это ИТ-отдел"}
        response = post_form("/admin/categories", form_for(self.original, **changes), self.cookie)
        self.assertIn("Сохранено", response.location and __import__("urllib.parse").parse.unquote(response.location))
        menu = WhatsAppUser().register_profile()
        self.assertIn("2. Принтеры и сканеры", menu["reply"])
        self.assertTrue(menu["reply"].startswith("Здравствуйте, это ИТ-отдел"))
        self.assertEqual(json.loads(app.STORE.get_setting(qgp.PROFILE_SETTING))["categories"][1]["label_ru"], "Принтеры и сканеры")

    def test_saving_keeps_the_order_the_bot_asks_fields_in(self):
        printers = self.original["categories"][1]
        self.assertEqual(printers["required_fields"], ["reference", "problem"])  # as in company.json
        post_form("/admin/categories", form_for(self.original, label_ru_2="Принтеры (ред.)"), self.cookie)
        saved = qgp.load_profile(app.STORE, app.ROOT)["categories"][1]
        self.assertEqual(saved["required_fields"], ["reference", "problem"])

    def test_invalid_edit_is_rejected_and_nothing_changes(self):
        response = post_form("/admin/categories", form_for(self.original, number_2="1"), self.cookie)
        self.assertIn("/admin/categories?notice=", response.location)
        self.assertEqual(qgp.load_profile(app.STORE, app.ROOT), self.original)
        self.assertIn("2. Принтеры", WhatsAppUser().register_profile()["reply"])

    def test_number_eight_is_reserved(self):
        post_form("/admin/categories", form_for(self.original, number_2="8"), self.cookie)
        self.assertEqual(qgp.load_profile(app.STORE, app.ROOT), self.original)

    def test_removed_category_keeps_its_label_on_old_tickets(self):
        user = WhatsAppUser(); user.register_profile(); user.say("2"); user.say("PR-17 не печатает"); user.say("Готово")
        key = self.original["categories"][1]["key"]
        post_form("/admin/categories", form_for(self.original, delete_2="1"), self.cookie)
        self.assertNotIn("2. Принтеры", WhatsAppUser().register_profile()["reply"])
        self.assertEqual(app.CATEGORIES[key], self.original["categories"][1]["label_ru"])

    def test_new_category_from_the_page_reaches_the_bot(self):
        # field names are taken from the frontend, not from the test helper
        template = get("/static/templates/category_editor_row.html").text
        for name in ("label_ru_{{ suffix }}", "number_{{ suffix }}"):
            self.assertIn(f'name="{name}"', template)
        self.assertIn('name="req_${name}_', (Path(__file__).parents[1] / "frontend/static/pages.js").read_text(encoding="utf-8"))
        form = form_for(self.original, number_new="3", label_ru_new="Доступ к 1С", label_kz_new="1С қолжетімділігі")
        post_form("/admin/categories", form, self.cookie)
        self.assertIn("3. Доступ к 1С", WhatsAppUser().register_profile()["reply"])

    def test_old_menu_editor_endpoint_is_gone(self):
        self.assertEqual(post_form("/admin/menu", {"action": "save"}, self.cookie).status, 404)


if __name__ == "__main__":
    unittest.main()
