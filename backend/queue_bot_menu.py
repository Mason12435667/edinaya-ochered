"""Запросы к меню бота и тексты на нужном языке, построенные из одного снимка настроек."""
from __future__ import annotations

import queue_user_locale
from queue_language import parse_menu_number
from queue_menu_config import MenuConfig
from queue_user_locale import tr


class BotMenu:
    def __init__(self, config: MenuConfig):
        self.config = config

    @staticmethod
    def _numbered(profile):
        return sorted((r for r in profile["categories"] if r.get("enabled", True)),
                      key=lambda r: int(r["number"]))

    @staticmethod
    def _label(row):
        return str(row["label_kz"] if queue_user_locale.is_kz() else row["label_ru"])

    def configured_menu_category(self, choice: str) -> str:
        number = str(parse_menu_number(choice))
        return next((str(r["key"]) for r in self.config.rows() if str(r["number"]) == number), "")

    def user_menu_label(self, category: str, fallback: str = "") -> str:
        row = next((r for r in self.config.profile["categories"] if r["key"] == category), None)
        return self._label(row) if row else fallback or str(category)

    def menu_payload(self) -> list[dict[str, str]]:
        result = [{"number": str(r["number"]), "label": self._label(r)}
                  for r in self._numbered(self.config.profile)]
        result.append({"number": "8", "label": tr("Мои активные заявки", "Менің белсенді өтінімдерім")})
        return result

    def main_menu_text(self, error: bool = False) -> str:
        profile = self.config.profile
        greeting = str(profile["greeting_kz"] if queue_user_locale.is_kz() else profile["greeting_ru"])
        entries = "\n".join(f"{r['number']}. {self._label(r)}" for r in self._numbered(profile))
        active = tr("8. Мои активные заявки", "8. Менің белсенді өтінімдерім")
        prefix = tr("Выберите категорию обращения:", "Өтінім санатын таңдаңыз:")
        tail = tr("00 — сменить язык", "00 — тілді өзгерту")
        if error:
            prefix = tr("Выберите действительный номер категории:\n", "Санаттың дұрыс нөмірін таңдаңыз:\n") + prefix
        return f"{greeting}\n\n{prefix}\n{entries}\n{active}\n\n{tail}"

    start_menu_prompt = main_menu_text

    def configured_category_prompt(self, category: str) -> str:
        row = next((r for r in self.config.profile["categories"] if r["key"] == category), {})
        return str(row.get("prompt_kz" if queue_user_locale.is_kz() else "prompt_ru", "")) or tr("Опишите ваш вопрос", "Сұрағыңызды жазыңыз")

    def configured_category_required_fields(self, category: str) -> list[str]:
        row = next((r for r in self.config.profile["categories"] if r["key"] == category), {})
        return list(row.get("required_fields", ["problem"]))

    def active_ticket_category_items(self) -> list[tuple[str, str]]:
        return [(str(r["key"]), str(r["label_ru"])) for r in self.config.rows() if r.get("action", "ticket") == "ticket"]

    def enabled_menu_options(self) -> list[dict]:
        return [r for r in self.config.menu_options() if r["enabled"]]

    def configured_category_action(self, category: str) -> str:
        row = next((r for r in self.config.menu_options() if r["key"] == category), {})
        action = str(row.get("action", "ticket"))
        return action if action in {"ticket", "instruction"} else "ticket"
