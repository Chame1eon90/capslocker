# -*- coding: utf-8 -*-
"""WoW Переводчик — графический интерфейс."""
import ctypes
import json
import os
import queue
import sys
import threading
import time
import traceback

import customtkinter as ctk
from tkinter import messagebox

import core
import updater
from engine import Engine as CapsEngine
from core import Engine, detect_direction, load_settings, save_settings, log, WOW_CHAT_LIMIT

APP_NAME = "Капслокер"

# ---- палитра
BG = "#14161c"
CARD = "#1d2029"
CARD_2 = "#252935"
BORDER = "#2e3342"
TEXT = "#e9e6dc"
MUTED = "#8b8f9c"
GOLD = "#e0b545"
GOLD_HOVER = "#f0c75a"
GREEN = "#4cc38a"
RED = "#e5605a"
BLUE = "#5aa9e6"
ORANGE = "#e69a4c"

FONT = "Segoe UI"


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def nice_key(hk):
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in str(hk).split("+"))


# Скан-коды клавиш: работают при любой раскладке (русской тоже)
SC_CTRL, SC_A, SC_C, SC_V, SC_ENTER = 29, 30, 46, 47, 28
MODIFIER_NAMES = {"ctrl", "left ctrl", "right ctrl", "alt", "left alt", "right alt", "alt gr",
                  "shift", "left shift", "right shift", "windows", "left windows", "right windows"}
# Windows keycode -> действие для поля ввода (не зависит от раскладки)
EDIT_KEYCODES = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "select_all"}


class Card(ctk.CTkFrame):
    def __init__(self, master, title, badge=None, **kw):
        super().__init__(master, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER, **kw)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(head, text=title, font=(FONT, 15, "bold"), text_color=TEXT).pack(side="left")
        if badge:
            ctk.CTkLabel(head, text=f" {badge} ", font=(FONT, 11, "bold"), text_color=BG,
                         fg_color=GOLD, corner_radius=6).pack(side="left", padx=8)
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=16, pady=(0, 14))


class HistoryItem(ctk.CTkFrame):
    def __init__(self, master, original, translated, direction, how, on_copy):
        super().__init__(master, fg_color=CARD_2, corner_radius=10)
        arrow = ("RU → EN" if direction == ("ru", "en") else "EN → RU" if direction == ("en", "ru")
                 else "Исправлены ошибки")
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(top, text=f"{arrow}  ·  {how}  ·  {time.strftime('%H:%M')}",
                     font=(FONT, 10), text_color=MUTED).pack(side="left")
        ctk.CTkButton(top, text="Копировать", width=86, height=22, font=(FONT, 11),
                      fg_color="transparent", hover_color=BORDER, text_color=GOLD,
                      command=lambda: on_copy(translated)).pack(side="right")
        ctk.CTkLabel(self, text=original, font=(FONT, 12), text_color=MUTED, wraplength=430,
                     justify="left", anchor="w").pack(fill="x", padx=10, pady=(2, 0))
        ctk.CTkLabel(self, text=translated, font=(FONT, 14), text_color=TEXT, wraplength=430,
                     justify="left", anchor="w").pack(fill="x", padx=10, pady=(0, 9))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.settings = load_settings()
        self.engine = Engine()
        self.engine.mode = self.settings["mode"]
        core.set_glossary(self.settings.get("glossary") or core.DEFAULT_GLOSSARY)
        self.engine.offline_engine = self.settings.get("offline_engine", "fast")
        if self.settings.get("online_service") not in ("google", "microsoft", "yandex", "mymemory"):
            self.settings["online_service"] = "google"
        self.settings.pop("deepl_key", None)
        self.engine.online.preferred = self.settings["online_service"]
        self.engine.online.fallback = bool(self.settings.get("online_fallback", True))
        caps_defaults = {"enabled": True, "hold_ms": 450, "selection": True, "exclude": "", "scope": "phrase",
                         "auto": False, "auto_skip": [], "auto_only": ""}
        if "caps" not in self.settings:
            try:  # перенос настроек из отдельного CapsFix
                with open(os.path.join(core.OLD_CAPS_DIR, "settings.json"), encoding="utf-8") as f:
                    old = json.load(f)
                caps_defaults.update({k: old[k] for k in caps_defaults if k in old})
            except Exception:
                pass
            self.settings["caps"] = caps_defaults
        else:
            for k, v in caps_defaults.items():
                self.settings["caps"].setdefault(k, v)
        self.caps_settings = self.settings["caps"]
        self.settings.setdefault("autostart", False)
        self.settings.setdefault("first_run", True)
        self.settings.setdefault("tab", "tr")
        self.tray = None
        self.ui_queue = queue.Queue()
        self.busy = False
        self.downloading = False
        self.binding = False
        self.kb = None
        self.hotkey_handles = {}
        self.settings.setdefault("spell_before_translate", False)
        self.own_clip = set()
        self.last_clip = None
        self.history_items = []

        self.title(APP_NAME)
        self._fit_geometry(540, 940)
        self.configure(fg_color=BG)
        try:
            self.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass
        self.attributes("-topmost", self.settings["keep_on_top"])
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self._build()
        self._apply_dimming()
        self.after(100, self._poll_queue)
        self._setup_hotkey()
        self.refresh_models()
        threading.Thread(target=self._warm_up, daemon=True).start()
        threading.Thread(target=self._clipboard_watcher, daemon=True).start()
        threading.Thread(target=self._idle_watcher, daemon=True).start()
        self.caps_engine = CapsEngine(self.caps_settings, on_event=lambda k, t: self.ui("capsev", k, t))
        self.caps_engine.start()
        self._start_tray()
        # автоматическое исправление раскладки (по умолчанию выключено; словари грузятся, только если включено)
        self.auto_layout = core.AutoLayout()
        self.auto_layout.skip.update(self.caps_settings.get("auto_skip", []))
        self.caps_engine.auto = self.auto_layout
        self.caps_engine.on_auto_skip = lambda w: self.ui("auto_skip", w)
        if self.caps_settings.get("auto"):
            self.auto_layout.load_async()

    # ================================================================ UI
    def _build(self):
        # ---- шапка
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 8))
        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(left, text="Капслокер", font=(FONT, 22, "bold"), text_color=GOLD).pack(anchor="w")
        ctk.CTkLabel(left, text="Перевод и раскладка · Русский ⇄ English" +
                     (f" · сборка {updater.BUILD}" if updater.BUILD else ""), font=(FONT, 12),
                     text_color=MUTED).pack(anchor="w")
        self.pin_switch = ctk.CTkSwitch(header, text="Поверх окон", font=(FONT, 12), text_color=MUTED,
                                        progress_color=GOLD, command=self.on_topmost)
        self.pin_switch.pack(side="right")
        self.update_btn = ctk.CTkButton(header, text="Обновить", width=86, height=28, font=(FONT, 12),
                                        fg_color="transparent", hover_color=BORDER, border_width=1,
                                        border_color=BORDER, text_color=GOLD, command=self.on_update)
        self.update_btn.pack(side="right", padx=(0, 10))
        if self.settings["keep_on_top"]:
            self.pin_switch.select()

        # ---- вкладки
        self.tab_map = {"Перевод": "tr", "Ошибки": "spell", "Раскладка": "caps", "Настройки": "set"}
        self.tab_seg = ctk.CTkSegmentedButton(
            self, values=list(self.tab_map), command=self.on_tab, font=(FONT, 14, "bold"), height=38,
            fg_color=CARD, selected_color=GOLD, selected_hover_color=GOLD_HOVER,
            unselected_color=CARD, unselected_hover_color=CARD_2, text_color=TEXT)
        self.tab_seg.pack(fill="x", padx=18, pady=(0, 8))
        # строка состояния — внизу окна, видна на любой вкладке
        self.status = ctk.CTkLabel(self, text="Готово", font=(FONT, 11), text_color=GREEN, anchor="w",
                                   justify="left", wraplength=480)
        self.status.pack(side="bottom", fill="x", padx=22, pady=(4, 10))
        # прокрутка: на ноутбуках с масштабом 125–150% окно может не влезть по высоте
        mk = lambda: ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color=BORDER)
        self.tab_tr, self.tab_spell, self.tab_caps, self.tab_set = mk(), mk(), mk(), mk()
        self.tabs = {"tr": self.tab_tr, "spell": self.tab_spell, "caps": self.tab_caps, "set": self.tab_set}
        # Настройки: общие
        gen = Card(self.tab_set, "Общие")
        gen.pack(fill="x", padx=18, pady=6)
        self._g_set_general = gen.body
        self._build_caps(self.tab_caps)

        # ---- режим
        def feature_bar(parent, title, hint, key):
            bar = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12, border_width=1, border_color=BORDER)
            bar.pack(fill="x", padx=18, pady=(0, 8))
            sw = ctk.CTkSwitch(bar, text=title, font=(FONT, 14, "bold"), text_color=TEXT, progress_color=GOLD)
            sw.configure(command=lambda: self.on_feature(key, sw))
            sw.pack(side="left", padx=(14, 10), pady=10)
            ctk.CTkLabel(bar, text=hint, font=(FONT, 10), text_color=MUTED, justify="right").pack(side="right", padx=12)
            return sw

        self.tr_sw = feature_bar(self.tab_tr, "Перевод включён", "выключен — клавиша свободна", "tr_enabled")
        self.spell_en_sw = feature_bar(self.tab_spell, "Проверка ошибок включена",
                                       "выключена — клавиша свободна", "spell_enabled")
        spell_card = self._g_spell = Card(self.tab_spell, "Исправление ошибок", "RU · EN")
        spell_card.pack(fill="x", padx=18, pady=6)
        sq = self._g_spell_quick = Card(self.tab_spell, "Проверить текст", "RU · EN")
        sq.pack(fill="x", padx=18, pady=6)
        sq_row = ctk.CTkFrame(sq.body, fg_color="transparent")
        sq_row.pack(fill="x")
        self.spell_entry = ctk.CTkEntry(sq_row, placeholder_text="Вставь или напиши текст…", height=38,
                                        font=(FONT, 13), fg_color=CARD_2, border_color=BORDER, text_color=TEXT)
        self.spell_entry.pack(side="left", fill="x", expand=True)
        self.spell_entry.bind("<Return>", lambda e: self.on_manual_spell())
        self._spell_manual_btn = ctk.CTkButton(sq_row, text="Исправить", width=100, height=38,
                                               font=(FONT, 13, "bold"), fg_color=GOLD, hover_color=GOLD_HOVER,
                                               text_color=BG, command=self.on_manual_spell)
        self._spell_manual_btn.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(sq.body, text="Результат скопируется и попадёт в историю на вкладке «Перевод»",
                     font=(FONT, 10), text_color=MUTED).pack(anchor="w", pady=(6, 0))
        if self.settings.get("tr_enabled", True):
            self.tr_sw.select()
        if self.settings.get("spell_enabled", True):
            self.spell_en_sw.select()
        mode_row = ctk.CTkFrame(self.tab_tr, fg_color="transparent")
        mode_row.pack(fill="x", padx=18, pady=(4, 10))
        self._g_mode = mode_row
        self.mode_map = {"Авто": "auto", "Онлайн": "online", "Офлайн": "offline"}
        rev = {v: k for k, v in self.mode_map.items()}
        self.mode_seg = ctk.CTkSegmentedButton(
            mode_row, values=list(self.mode_map), command=self.on_mode, font=(FONT, 13, "bold"), height=34,
            fg_color=CARD, selected_color=GOLD, selected_hover_color=GOLD_HOVER,
            unselected_color=CARD, unselected_hover_color=CARD_2, text_color=TEXT)
        self.mode_seg.pack(fill="x")
        self.mode_seg.set(rev.get(self.settings["mode"], "Авто"))
        self.mode_hint = ctk.CTkLabel(mode_row, text="", font=(FONT, 11), text_color=MUTED)
        self.mode_hint.pack(anchor="w", pady=(4, 0))
        self._update_mode_hint()

        srv = ctk.CTkFrame(mode_row, fg_color="transparent")
        srv.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(srv, text="Онлайн через", font=(FONT, 12), text_color=TEXT).pack(side="left")
        self.service_names = {"google": "Google", "microsoft": "Microsoft (Bing)", "yandex": "Яндекс",
                              "mymemory": "MyMemory"}
        self.service_menu = ctk.CTkOptionMenu(
            srv, values=list(self.service_names.values()), command=self.on_service, width=170, height=28,
            font=(FONT, 12), dropdown_font=(FONT, 12), fg_color=CARD_2, button_color=CARD_2,
            button_hover_color=BORDER, dropdown_fg_color=CARD, dropdown_hover_color=CARD_2, text_color=TEXT)
        self.service_menu.pack(side="left", padx=8)
        self.service_menu.set(self.service_names.get(self.engine.online.preferred, "Google"))
        self.test_btn = ctk.CTkButton(srv, text="Проверить", width=86, height=28, font=(FONT, 12),
                                      fg_color="transparent", hover_color=BORDER, border_width=1,
                                      border_color=BORDER, text_color=GOLD, command=self.on_test_services)
        self.test_btn.pack(side="left")
        self.fallback_switch = ctk.CTkSwitch(srv, text="Запасные", font=(FONT, 12), text_color=MUTED,
                                             progress_color=GOLD, width=40, command=self.on_fallback)
        self.fallback_switch.pack(side="right")
        if self.engine.online.fallback:
            self.fallback_switch.select()

        # ---- мои сообщения
        mine = self._g_mine = Card(self.tab_tr, "Мои сообщения", "RU → EN")
        mine.pack(fill="x", padx=18, pady=6)
        row = ctk.CTkFrame(mine.body, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="Напиши фразу в чате игры и нажми", font=(FONT, 12),
                     text_color=MUTED).pack(side="left")
        self.key_btn = ctk.CTkButton(row, text=self._hk_label("tr"), width=70, height=30,
                                     font=(FONT, 13, "bold"), fg_color=CARD_2, hover_color=BORDER,
                                     border_width=1, border_color=GOLD, text_color=GOLD,
                                     command=lambda: self.on_rebind("tr"))
        self.key_btn.pack(side="left", padx=8)
        sb = spell_card.body
        rows = ctk.CTkFrame(sb, fg_color="transparent")
        rows.pack(fill="x")
        ctk.CTkLabel(rows, text="Напиши фразу в чате и нажми", font=(FONT, 12), text_color=MUTED).pack(side="left")
        self.spell_btn = ctk.CTkButton(rows, text=self._hk_label("spell"), width=70, height=30,
                                       font=(FONT, 13, "bold"), fg_color=CARD_2, hover_color=BORDER,
                                       border_width=1, border_color=GOLD, text_color=GOLD,
                                       command=lambda: self.on_rebind("spell"))
        self.spell_btn.pack(side="left", padx=8)
        ctk.CTkLabel(sb, text="Выделишь часть текста — проверится только она", font=(FONT, 10),
                     text_color=MUTED).pack(anchor="w", pady=(4, 0))
        self.spell_sw = ctk.CTkSwitch(sb, text="Исправлять ошибки и перед переводом", font=(FONT, 12),
                                      text_color=TEXT, progress_color=GOLD, command=self.on_spell_before)
        self.spell_sw.pack(anchor="w", pady=(8, 0))
        if self.settings.get("spell_before_translate", False):
            self.spell_sw.select()
        self.punct_sw = ctk.CTkSwitch(sb, text="Проверять и пунктуацию (запятые, знаки)", font=(FONT, 12),
                                      text_color=TEXT, progress_color=GOLD, command=self.on_punct)
        self.punct_sw.pack(anchor="w", pady=(6, 0))
        if self.settings.get("spell_punct", False):
            self.punct_sw.select()
        self._punct_lbl = ctk.CTkLabel(sb, text="Пунктуация — только в программах (например: telegram):",
                                       font=(FONT, 10), text_color=MUTED)
        self._punct_lbl.pack(anchor="w", pady=(6, 2))
        pr = ctk.CTkFrame(sb, fg_color="transparent")
        pr.pack(fill="x")
        self.punct_ex = ctk.CTkEntry(pr, height=28, font=(FONT, 12), fg_color=CARD_2, border_color=BORDER,
                                     text_color=TEXT, placeholder_text="пусто — во всех программах")
        self.punct_ex.pack(side="left", fill="x", expand=True)
        self._punct_pick = ctk.CTkButton(pr, text="Выбрать…", width=84, height=28, font=(FONT, 12),
                                         fg_color="transparent", hover_color=BORDER, border_width=1,
                                         border_color=BORDER, text_color=GOLD, command=self.on_pick_punct_app)
        self._punct_pick.pack(side="left", padx=(6, 0))
        if self.settings.get("punct_only"):
            self.punct_ex.insert(0, self.settings["punct_only"])
        self.punct_ex.bind("<FocusOut>", lambda e: self.on_punct_exclude())
        self.punct_ex.bind("<Return>", lambda e: self.on_punct_exclude())
        glc = Card(self.tab_set, "Словарь игровых слов")
        glc.pack(fill="x", padx=18, pady=6)
        gl = ctk.CTkFrame(glc.body, fg_color="transparent")
        gl.pack(fill="x")
        ctk.CTkLabel(gl, text="хил, дд, ЛФГ, герои Overwatch… — для перевода и проверки", font=(FONT, 12),
                     text_color=MUTED).pack(side="left")
        ctk.CTkButton(gl, text="Словарь…", width=96, height=28, font=(FONT, 12), fg_color="transparent",
                      hover_color=BORDER, border_width=1, border_color=BORDER, text_color=GOLD,
                      command=self.on_glossary).pack(side="right")
        row2 = ctk.CTkFrame(mine.body, fg_color="transparent")
        row2.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(row2, text="Нажми на клавишу в рамке, чтобы поменять её", font=(FONT, 10),
                     text_color=MUTED).pack(side="left")
        self.send_switch = ctk.CTkSwitch(row2, text="Сразу отправлять", font=(FONT, 12), text_color=TEXT,
                                         progress_color=GOLD, command=self.on_autosend)
        self.send_switch.pack(side="right")
        if self.settings["auto_send"]:
            self.send_switch.select()
        # режим «без нажатий»: программа ничего не нажимает, только меняет буфер обмена
        self.clipmode_sw = ctk.CTkSwitch(mine.body, text="Без нажатий: скопировал русский — в буфере уже английский",
                                         font=(FONT, 12), text_color=TEXT, progress_color=GOLD,
                                         command=self.on_clipmode)
        self.clipmode_sw.pack(anchor="w", pady=(10, 0))
        if self.settings.get("clip_ru2en"):
            self.clipmode_sw.select()
        ctk.CTkLabel(mine.body, text="Сам выдели фразу и скопируй (Ctrl+A, Ctrl+C), потом вставь перевод (Ctrl+V).\n"
                                     "В игре программа ничего не нажимает — как будто ты пользуешься переводчиком.",
                     font=(FONT, 10), text_color=MUTED, justify="left").pack(anchor="w", padx=(46, 0))
        self._clipmode_lbl = ctk.CTkLabel(mine.body, text="Только в программах (например: wow, overwatch):",
                                          font=(FONT, 10), text_color=MUTED)
        self._clipmode_lbl.pack(anchor="w", padx=(46, 0), pady=(4, 0))
        cr = ctk.CTkFrame(mine.body, fg_color="transparent")
        cr.pack(fill="x", padx=(46, 0))
        self.clipmode_ex = ctk.CTkEntry(cr, height=28, font=(FONT, 12), fg_color=CARD_2, border_color=BORDER,
                                        text_color=TEXT, placeholder_text="пусто — во всех программах")
        self.clipmode_ex.pack(side="left", fill="x", expand=True)
        self._clipmode_pick = ctk.CTkButton(cr, text="Выбрать…", width=84, height=28, font=(FONT, 12),
                                            fg_color="transparent", hover_color=BORDER, border_width=1,
                                            border_color=BORDER, text_color=GOLD,
                                            command=lambda: self._pick_app_into(self.clipmode_ex, self.on_clipmode_apps,
                                                                                "Режим без нажатий работает в"))
        self._clipmode_pick.pack(side="left", padx=(6, 0))
        if self.settings.get("clip_ru2en_only"):
            self.clipmode_ex.insert(0, self.settings["clip_ru2en_only"])
        self.clipmode_ex.bind("<FocusOut>", lambda e: self.on_clipmode_apps())
        self.clipmode_ex.bind("<Return>", lambda e: self.on_clipmode_apps())

        # ---- ручной перевод
        manual = self._g_manual = Card(self.tab_tr, "Быстрый перевод", "RU ⇄ EN")
        manual.pack(fill="x", padx=18, pady=6)
        in_row = ctk.CTkFrame(manual.body, fg_color="transparent")
        in_row.pack(fill="x")
        self.entry = ctk.CTkEntry(in_row, placeholder_text="Пиши на русском или английском…", height=38,
                                  font=(FONT, 13), fg_color=CARD_2, border_color=BORDER, text_color=TEXT)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.on_manual())
        self.entry.bind("<Control-KeyPress>", self._entry_ctrl_key)
        self.entry.bind("<Shift-Insert>", self._entry_paste)
        self._tr_manual_btn = ctk.CTkButton(in_row, text="Перевести", width=100, height=38, font=(FONT, 13, "bold"),
                                            fg_color=GOLD, hover_color=GOLD_HOVER, text_color=BG,
                                            command=self.on_manual)
        self._tr_manual_btn.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(manual.body, text="Перевод сразу скопируется — останется нажать Ctrl+V",
                     font=(FONT, 10), text_color=MUTED).pack(anchor="w", pady=(6, 0))

        # ---- офлайн-модели + статус
        foot = ctk.CTkFrame(self.tab_set, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
        foot.pack(fill="x", padx=18, pady=(6, 16))
        er = ctk.CTkFrame(foot, fg_color="transparent")
        er.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(er, text="Офлайн-модель", font=(FONT, 13, "bold"), text_color=TEXT).pack(side="left")
        self.engine_map = {"Быстрая": "fast", "Качественная": "quality"}
        self.engine_seg = ctk.CTkSegmentedButton(
            er, values=list(self.engine_map), command=self.on_engine, font=(FONT, 12, "bold"), height=28,
            fg_color=CARD_2, selected_color=GOLD, selected_hover_color=GOLD_HOVER,
            unselected_color=CARD_2, unselected_hover_color=BORDER, text_color=TEXT)
        self.engine_seg.pack(side="right")
        self.engine_seg.set({v: k for k, v in self.engine_map.items()}[self.engine.offline_engine])

        self.model_rows = {}
        for key, title, sub in (("fast", "Быстрая · Argos", "~200 МБ, мгновенно"),
                                ("quality", "Качественная · Tencent HY-MT", "~1,1 ГБ, точнее, медленнее"),
                                ("sage", "Проверка ошибок · SAGE (Сбер)", "~100 МБ, русский: опечатки и запятые")):
            r = ctk.CTkFrame(foot, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=2)
            dot = ctk.CTkLabel(r, text="●", font=(FONT, 14), text_color=RED, width=14)
            dot.pack(side="left")
            txt = ctk.CTkFrame(r, fg_color="transparent")
            txt.pack(side="left", padx=6)
            ctk.CTkLabel(txt, text=title, font=(FONT, 12), text_color=TEXT, height=16).pack(anchor="w")
            lab = ctk.CTkLabel(txt, text=sub, font=(FONT, 10), text_color=MUTED, height=14)
            lab.pack(anchor="w")
            btn = ctk.CTkButton(r, text="Скачать", width=96, height=28, font=(FONT, 12, "bold"),
                                fg_color=CARD_2, hover_color=BORDER, border_width=1, border_color=GOLD,
                                text_color=GOLD, command=lambda k=key: self.on_download(k))
            btn.pack(side="right")
            fbtn = ctk.CTkButton(r, text="Из файла", width=80, height=28, font=(FONT, 12),
                                 fg_color="transparent", hover_color=BORDER, text_color=MUTED,
                                 command=self.on_install_file)
            fbtn.pack(side="right", padx=(0, 6))
            self.model_rows[key] = (dot, lab, btn, sub)

        ur = ctk.CTkFrame(foot, fg_color="transparent")
        ur.pack(fill="x", padx=16, pady=(6, 0))
        ctk.CTkLabel(ur, text="Выгружать из памяти, если не нужна", font=(FONT, 11),
                     text_color=MUTED).pack(side="left")
        self.unload_map = {"5 мин": 5, "10 мин": 10, "30 мин": 30, "1 час": 60, "Никогда": 0}
        self.unload_menu = ctk.CTkOptionMenu(
            ur, values=list(self.unload_map), command=self.on_unload_minutes, width=100, height=24,
            font=(FONT, 11), dropdown_font=(FONT, 11), fg_color=CARD_2, button_color=CARD_2,
            button_hover_color=BORDER, dropdown_fg_color=CARD, dropdown_hover_color=CARD_2, text_color=TEXT)
        self.unload_menu.pack(side="right")
        dr = ctk.CTkFrame(foot, fg_color="transparent")
        dr.pack(fill="x", padx=16, pady=(6, 0))
        ctk.CTkLabel(dr, text="Модели, настройки и журналы хранятся отдельно от программы", font=(FONT, 11),
                     text_color=MUTED).pack(side="left")
        ctk.CTkButton(dr, text="Папка с данными", width=120, height=24, font=(FONT, 11),
                      fg_color="transparent", hover_color=BORDER, border_width=1, border_color=BORDER,
                      text_color=GOLD, command=self.on_open_data).pack(side="right")
        cur = self.settings.get("unload_minutes", 10)
        self.unload_menu.set(next((k for k, v in self.unload_map.items() if v == cur), "10 мин"))
        self.progress = ctk.CTkProgressBar(foot, height=6, progress_color=GOLD, fg_color=CARD_2)
        self.progress.set(0)
        self._foot_end = ctk.CTkFrame(foot, fg_color="transparent", height=8, width=8)
        self._foot_end.pack()

        # ---- история / входящие
        hist = self._g_hist = Card(self.tab_tr, "История и входящие", "EN → RU")
        hist.pack(fill="both", expand=True, padx=18, pady=6)
        top = ctk.CTkFrame(hist.body, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        self.watch_switch = ctk.CTkSwitch(top, text="Переводить английский, который я копирую",
                                          font=(FONT, 12), text_color=TEXT, progress_color=GOLD,
                                          command=self.on_watch)
        self.watch_switch.pack(side="left")
        if self.settings["watch_clipboard"]:
            self.watch_switch.select()
        tr2 = ctk.CTkFrame(hist.body, fg_color="transparent")
        tr2.pack(fill="x", pady=(0, 6))
        self.toast_switch = ctk.CTkSwitch(tr2, text="Показывать перевод поверх окон (плашкой)", font=(FONT, 12),
                                          text_color=TEXT, progress_color=GOLD, command=self.on_toast_toggle)
        self.toast_switch.pack(side="left")
        if self.settings.get("toast", True):
            self.toast_switch.select()
        self.toast_pos_map = {"Снизу справа": "br", "Снизу слева": "bl", "Снизу по центру": "bc",
                              "Сверху справа": "tr", "Сверху слева": "tl", "Сверху по центру": "tc"}
        tp = ctk.CTkOptionMenu(tr2, values=list(self.toast_pos_map), command=self.on_toast_pos, width=150,
                               height=26, font=(FONT, 11), dropdown_font=(FONT, 11), fg_color=CARD_2,
                               button_color=CARD_2, button_hover_color=BORDER, dropdown_fg_color=CARD,
                               dropdown_hover_color=CARD_2, text_color=TEXT)
        tp.pack(side="right")
        tp.set(next((k for k, v in self.toast_pos_map.items() if v == self.settings.get("toast_pos", "br")),
                    "Снизу справа"))
        ctk.CTkLabel(hist.body, text="Плашка — только в программах (через запятую, например: Wow, Overwatch):",
                     font=(FONT, 10), text_color=MUTED).pack(anchor="w", pady=(0, 2))
        tr3 = ctk.CTkFrame(hist.body, fg_color="transparent")
        tr3.pack(fill="x", pady=(0, 6))
        self.toast_ex = ctk.CTkEntry(tr3, height=28, font=(FONT, 12), fg_color=CARD_2, border_color=BORDER,
                                     text_color=TEXT, placeholder_text="пусто — во всех программах")
        self.toast_ex.pack(side="left", fill="x", expand=True)
        if self.settings.get("toast_only"):
            self.toast_ex.insert(0, self.settings["toast_only"])
        self.toast_ex.bind("<FocusOut>", lambda e: self.on_toast_apps())
        self.toast_ex.bind("<Return>", lambda e: self.on_toast_apps())
        ctk.CTkButton(tr3, text="Выбрать…", width=84, height=28, font=(FONT, 12), fg_color="transparent",
                      hover_color=BORDER, border_width=1, border_color=BORDER, text_color=GOLD,
                      command=self.on_pick_toast_app).pack(side="left", padx=(6, 0))
        ctk.CTkButton(top, text="Очистить", width=70, height=24, font=(FONT, 11), fg_color="transparent",
                      hover_color=BORDER, text_color=MUTED, command=self.clear_history).pack(side="right")
        # обычный список: вся вкладка и так прокручивается
        self.history = ctk.CTkFrame(hist.body, fg_color="transparent")
        self.history.pack(fill="x")
        self.empty_label = ctk.CTkLabel(
            self.history, text="Пока пусто.\nСкопируй английский текст (Ctrl+C) —\nперевод появится здесь.",
            font=(FONT, 12), text_color=MUTED, justify="center")
        self.empty_label.pack(pady=30)


    # ================================================================ очередь UI
    def _fit_geometry(self, w, h):
        """Не даём окну быть выше экрана (с учётом масштаба Windows)."""
        try:
            scale = ctk.ScalingTracker.get_window_scaling(self)
            max_h = int((self.winfo_screenheight() - 90) / max(scale, 1.0))
            h = max(560, min(h, max_h))
        except Exception:
            pass
        self.geometry(f"{w}x{h}")
        self.minsize(480, min(h, 600))

    def on_update(self):
        if getattr(self, "_updating", False):
            return
        self._updating = True
        self.update_btn.configure(text="Проверяю…", state="disabled")

        def work():
            try:
                info = updater.latest()
                if not info:
                    self.ui("info", "На GitHub пока нет сборки для этой системы.")
                    return
                num, url, size = info
                if num <= updater.BUILD:
                    self.ui("info", f"У тебя последняя версия (сборка {updater.BUILD}).")
                    return
                if not updater.can_self_update():
                    self.ui("info", f"Есть сборка {num}. Скачай её со страницы Releases на GitHub.")
                    return
                self.ui("ask_update", num, url, size)
            except Exception as e:
                log("update check: " + traceback.format_exc())
                self.ui("error", f"Не удалось проверить обновления:\n{e}")
            finally:
                self.ui("update_btn")

        threading.Thread(target=work, daemon=True).start()

    def _do_update(self, num, url, size):
        cur = f"сборка {updater.BUILD}" if updater.BUILD else "собрана вручную"
        if not messagebox.askyesno(APP_NAME, f"Доступна новая версия — сборка {num} (у тебя {cur}).\n\n"
                                             f"Скачать (~{max(1, size // 1048576)} МБ) и перезапустить программу?"):
            return
        self._updating = True
        self.update_btn.configure(text="Скачиваю…", state="disabled")

        def work():
            try:
                import tempfile
                dest = os.path.join(tempfile.gettempdir(), updater.asset_name())

                def prog(pct, msg):
                    self.set_status(f"{msg} ({pct}%)", BLUE)
                    self.ui("update_pct", pct)
                log(f"update: качаю сборку {num}")
                updater.download(url, dest, prog)
                log(f"update: скачано {os.path.getsize(dest)} байт, запускаю замену")
                self.set_status("Устанавливаю обновление — программа перезапустится…", BLUE)
                updater.install_and_restart(dest)
                log("update: выхожу, чтобы заменить файл")
                time.sleep(0.5)
                self.ui("quit")
            except Exception as e:
                log("update failed: " + traceback.format_exc())
                self.ui("error", f"Не удалось обновить:\n{e}")
                self.ui("update_btn")

        threading.Thread(target=work, daemon=True).start()

    def on_tab(self, label):
        key = self.tab_map[label]
        self.settings["tab"] = key
        save_settings(self.settings)
        for t in self.tabs.values():
            t.pack_forget()
        self.tabs.get(key, self.tab_tr).pack(fill="both", expand=True)

    # ================================================================ вкладка «Раскладка»
    def _build_caps(self, P):
        how = self._g_caps_how = Card(P, "Исправление раскладки по CapsLock")
        how.pack(fill="x", padx=18, pady=6)
        top = ctk.CTkFrame(how.body, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(top, text="Набрал не в той раскладке — нажми CapsLock", font=(FONT, 12),
                     text_color=MUTED).pack(side="left")
        self.caps_sw = ctk.CTkSwitch(top, text="Включено", font=(FONT, 12, "bold"), text_color=TEXT,
                                     progress_color=GOLD, command=self.on_caps_enabled)
        self.caps_sw.pack(side="right")
        if self.caps_settings["enabled"]:
            self.caps_sw.select()
        for key, txt in (("Caps", "Исправить набранное и сменить язык («ghbdtn rfr ltkf» → «привет как дела»)."
                                  " Если ничего не набрано — просто сменить язык"),
                         ("Caps ещё раз", "Отменить исправление"),
                         ("Держать Caps", "Обычный Caps Lock (ЗАГЛАВНЫЕ)"),
                         ("Shift + Caps", "Переделать выделенный текст")):
            r = ctk.CTkFrame(how.body, fg_color="transparent")
            r.pack(fill="x", pady=3)
            ctk.CTkLabel(r, text=key, width=112, height=26, font=(FONT, 12, "bold"), text_color=GOLD,
                         fg_color=CARD_2, corner_radius=6).pack(side="left", anchor="n")
            ctk.CTkLabel(r, text=txt, font=(FONT, 12), text_color=TEXT, justify="left", anchor="w",
                         wraplength=330).pack(side="left", padx=10, fill="x")

        tr = self._g_caps_try = Card(P, "Попробуй здесь")
        tr.pack(fill="x", padx=18, pady=6)
        ctk.CTkLabel(tr.body, text="Набери «ghbdtn rfr ltkf» в английской раскладке и нажми CapsLock",
                     font=(FONT, 11), text_color=MUTED).pack(anchor="w")
        ctk.CTkEntry(tr.body, height=36, font=(FONT, 14), fg_color=CARD_2, border_color=BORDER,
                     text_color=TEXT).pack(fill="x", pady=(6, 0))

        st = self._g_caps_set = Card(P, "Настройки")
        st.pack(fill="x", padx=18, pady=6)
        sr = ctk.CTkFrame(st.body, fg_color="transparent")
        sr.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(sr, text="CapsLock исправляет:", font=(FONT, 12), text_color=TEXT).pack(side="left")
        self.scope_map = {"Всю фразу": "phrase", "Последнее слово": "word"}
        seg = ctk.CTkSegmentedButton(
            sr, values=list(self.scope_map), command=self.on_scope, font=(FONT, 12, "bold"), height=28,
            fg_color=CARD_2, selected_color=GOLD, selected_hover_color=GOLD_HOVER,
            unselected_color=CARD_2, unselected_hover_color=BORDER, text_color=TEXT)
        seg.pack(side="right")
        seg.set({v: k for k, v in self.scope_map.items()}[self.caps_settings["scope"]])
        self.sel_sw = ctk.CTkSwitch(st.body, text="Shift + Caps переделывает выделенный текст", font=(FONT, 12),
                                    text_color=TEXT, progress_color=GOLD, command=self.on_caps_selection)
        self.sel_sw.pack(anchor="w", pady=3)
        if self.caps_settings["selection"]:
            self.sel_sw.select()
        self.autofix_sw = ctk.CTkSwitch(st.body, text="Исправлять автоматически, без CapsLock", font=(FONT, 12),
                                        text_color=TEXT, progress_color=GOLD, command=self.on_autofix)
        self.autofix_sw.pack(anchor="w", pady=(3, 0))
        if self.caps_settings.get("auto"):
            self.autofix_sw.select()
        ctk.CTkLabel(st.body, text="После пробела слово «ghbdtn» само станет «привет». Исправил зря — нажми\n"
                                   "CapsLock: слово вернётся, и больше программа его трогать не будет.",
                     font=(FONT, 10), text_color=MUTED, justify="left").pack(anchor="w", padx=(46, 0), pady=(0, 3))
        self._auto_lbl = ctk.CTkLabel(st.body, text="Автоматически — только в программах (например: telegram, discord):",
                                      font=(FONT, 10), text_color=MUTED)
        self._auto_lbl.pack(anchor="w", padx=(46, 0))
        ar = ctk.CTkFrame(st.body, fg_color="transparent")
        ar.pack(fill="x", padx=(46, 0), pady=(0, 4))
        self.auto_ex = ctk.CTkEntry(ar, height=28, font=(FONT, 12), fg_color=CARD_2, border_color=BORDER,
                                    text_color=TEXT, placeholder_text="пусто — во всех программах")
        self.auto_ex.pack(side="left", fill="x", expand=True)
        self._auto_pick = ctk.CTkButton(ar, text="Выбрать…", width=84, height=28, font=(FONT, 12),
                                        fg_color="transparent", hover_color=BORDER, border_width=1,
                                        border_color=BORDER, text_color=GOLD,
                                        command=lambda: self._pick_app_into(self.auto_ex, self.on_auto_apps,
                                                                            "Автоисправление будет работать в"))
        self._auto_pick.pack(side="left", padx=(6, 0))
        if self.caps_settings.get("auto_only"):
            self.auto_ex.insert(0, self.caps_settings["auto_only"])
        self.auto_ex.bind("<FocusOut>", lambda e: self.on_auto_apps())
        self.auto_ex.bind("<Return>", lambda e: self.on_auto_apps())
        self.auto_sw = ctk.CTkSwitch(self._g_set_general, text="Запускать Капслокер вместе с Windows (в трее)", font=(FONT, 12),
                                     text_color=TEXT, progress_color=GOLD, command=self.on_autostart)
        self.auto_sw.pack(anchor="w", pady=3)
        if self.settings["autostart"]:
            self.auto_sw.select()
        hr = ctk.CTkFrame(st.body, fg_color="transparent")
        hr.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(hr, text="Удержание для Caps Lock:", font=(FONT, 12), text_color=TEXT).pack(side="left")
        self.hold_lbl = ctk.CTkLabel(hr, text="", font=(FONT, 12, "bold"), text_color=GOLD, width=60)
        self.hold_lbl.pack(side="right")
        sl = ctk.CTkSlider(st.body, from_=250, to=900, number_of_steps=13, button_color=GOLD,
                           button_hover_color=GOLD_HOVER, progress_color=GOLD, command=self.on_hold)
        sl.pack(fill="x", pady=(2, 6))
        sl.set(self.caps_settings["hold_ms"])
        self.on_hold(self.caps_settings["hold_ms"], save=False)
        ctk.CTkLabel(st.body, text="Не работать в программах (через запятую, например: game.exe):",
                     font=(FONT, 11), text_color=MUTED).pack(anchor="w", pady=(4, 2))
        self.ex_entry = ctk.CTkEntry(st.body, height=32, font=(FONT, 12), fg_color=CARD_2, border_color=BORDER,
                                     text_color=TEXT, placeholder_text="пусто — работает везде")
        self.ex_entry.pack(fill="x")
        if self.caps_settings["exclude"]:
            self.ex_entry.insert(0, self.caps_settings["exclude"])
        self.ex_entry.bind("<FocusOut>", lambda e: self.on_exclude())
        self.ex_entry.bind("<Return>", lambda e: self.on_exclude())

        self.caps_status = ctk.CTkLabel(P, text="Работает. Закрытие окна сворачивает Капслокер в трей.",
                                        font=(FONT, 11), text_color=GREEN, anchor="w")
        self.caps_status.pack(fill="x", padx=22, pady=(6, 12))

    def caps_status_set(self, text, color=MUTED):
        self.caps_status.configure(text=text, text_color=color)

    def on_caps_enabled(self):
        self.caps_settings["enabled"] = bool(self.caps_sw.get())
        save_settings(self.settings)
        self._apply_dimming()
        self.caps_status_set("Включено" if self.caps_settings["enabled"]
                             else "Выключено — CapsLock работает как обычно",
                             GREEN if self.caps_settings["enabled"] else MUTED)
        self._update_tray()

    def on_scope(self, label):
        self.caps_settings["scope"] = self.scope_map[label]
        save_settings(self.settings)

    def on_clipmode(self):
        on = bool(self.clipmode_sw.get())
        self.settings["clip_ru2en"] = on
        save_settings(self.settings)
        self._apply_dimming()
        self.set_status("Без нажатий: скопируй русскую фразу — перевод будет в буфере" if on
                        else "Режим без нажатий выключен", GREEN if on else MUTED)

    def on_clipmode_apps(self):
        self.settings["clip_ru2en_only"] = self.clipmode_ex.get().strip()
        save_settings(self.settings)

    def on_auto_apps(self):
        self.caps_settings["auto_only"] = self.auto_ex.get().strip()
        save_settings(self.settings)

    def on_autofix(self):
        on = bool(self.autofix_sw.get())
        self.caps_settings["auto"] = on
        save_settings(self.settings)
        if on:
            self.auto_layout.load_async()
        self.caps_status_set("Автоисправление раскладки включено" if on else "Автоисправление выключено",
                             GREEN if on else MUTED)
        self._apply_dimming()

    def on_caps_selection(self):
        self.caps_settings["selection"] = bool(self.sel_sw.get())
        save_settings(self.settings)

    def on_hold(self, val, save=True):
        ms = int(round(float(val) / 50) * 50)
        self.hold_lbl.configure(text=f"{ms / 1000:.2f} с")
        self.caps_settings["hold_ms"] = ms
        if save:
            save_settings(self.settings)

    def on_exclude(self):
        self.caps_settings["exclude"] = self.ex_entry.get().strip()
        save_settings(self.settings)

    def on_autostart(self):
        on = bool(self.auto_sw.get())
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                 winreg.KEY_SET_VALUE)
            try:
                if on:
                    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
                    winreg.SetValueEx(key, "Capslocker", 0, winreg.REG_SZ, f'"{exe}" --tray')
                else:
                    try:
                        winreg.DeleteValue(key, "Capslocker")
                    except FileNotFoundError:
                        pass
                for oldname in ("CapsFix",):   # старый отдельный CapsFix больше не нужен
                    try:
                        winreg.DeleteValue(key, oldname)
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(key)
            self.settings["autostart"] = on
            save_settings(self.settings)
            self.caps_status_set("Капслокер будет запускаться вместе с Windows" if on else "Автозапуск выключен", GREEN)
        except Exception as e:
            log("autostart: " + traceback.format_exc())
            self.caps_status_set(f"Не удалось: {e}", RED)

    def _on_caps_event(self, kind, text):
        if kind == "fix":
            self.caps_status_set(f"Исправлено: {text}", GREEN)
        elif kind == "auto":
            self.caps_status_set(f"Исправлено автоматически: {text}", GREEN)
        elif kind == "undo":
            self.caps_status_set(f"Отменено: {text}", BLUE)
        elif kind == "switch":
            self.caps_status_set("Язык переключён", MUTED)
        elif kind == "selection":
            self.caps_status_set(f"Выделение переделано: {text}", GREEN)
        elif kind == "caps":
            self.caps_status_set(text, BLUE)
        elif kind == "error":
            self.caps_status_set(text, RED)

    # ================================================================ трей
    def _tray_image(self):
        from PIL import Image
        return Image.open(resource_path("tray_on.png" if self.caps_settings["enabled"] else "tray_off.png"))

    def _start_tray(self):
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Открыть Капслокер", lambda i, it: self.ui("show"), default=True),
                pystray.MenuItem("Исправление раскладки", lambda i, it: self.ui("caps_toggle"),
                                 checked=lambda it: self.caps_settings["enabled"]),
                pystray.MenuItem("Перевод", lambda i, it: self.ui("feat_toggle", "tr_enabled"),
                                 checked=lambda it: self.settings.get("tr_enabled", True)),
                pystray.MenuItem("Проверка ошибок", lambda i, it: self.ui("feat_toggle", "spell_enabled"),
                                 checked=lambda it: self.settings.get("spell_enabled", True)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda i, it: self.ui("quit")),
            )
            self.tray = pystray.Icon("Capslocker", self._tray_image(), "Капслокер — перевод и раскладка", menu)
            self.tray.run_detached()
        except Exception:
            log("tray: " + traceback.format_exc())
            self.tray = None

    def _update_tray(self):
        if self.tray:
            try:
                self.tray.icon = self._tray_image()
                self.tray.update_menu()
            except Exception:
                pass

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide_to_tray(self):
        try:
            self.on_exclude()
            self.on_punct_exclude()
            self.on_toast_apps()
        except Exception:
            pass
        if self.tray is None:
            self.on_close()
            return
        if self.settings.get("first_run"):
            self.settings["first_run"] = False
            save_settings(self.settings)
            try:
                self.tray.notify("Капслокер работает в трее. Открыть — двойной клик по значку.", "Капслокер")
            except Exception:
                pass
        self.withdraw()

    def ui(self, *item):
        self.ui_queue.put(item)

    def set_status(self, text, color=MUTED):
        self.ui("status", text, color)

    def _poll_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status.configure(text=item[1], text_color=item[2])
                elif kind == "history":
                    self._add_history(*item[1:])
                elif kind == "models":
                    self.refresh_models()
                elif kind == "progress":
                    val = item[1]
                    if val is None:
                        self.progress.pack_forget()
                    else:
                        if not self.progress.winfo_ismapped():
                            self.progress.pack(fill="x", padx=16, pady=(2, 6), before=self._foot_end)
                        self.progress.set(val)
                elif kind == "key":
                    self.key_btn.configure(text=item[1])
                elif kind == "spellkey":
                    self.spell_btn.configure(text=item[1])
                elif kind == "error":
                    messagebox.showerror(APP_NAME, item[1])
                elif kind == "ask_update":
                    self._do_update(*item[1:])
                elif kind == "update_btn":
                    self._updating = False
                    self.update_btn.configure(text="Обновить", state="normal")
                elif kind == "update_pct":
                    self.update_btn.configure(text=f"{item[1]}%")
                elif kind == "toast":
                    self._show_toast(item[1], item[2])
                elif kind == "feat_toggle":
                    sw = self.tr_sw if item[1] == "tr_enabled" else self.spell_en_sw
                    sw.deselect() if self.settings.get(item[1], True) else sw.select()
                    self.on_feature(item[1], sw)
                elif kind == "capsev":
                    self._on_caps_event(item[1], item[2])
                elif kind == "auto_skip":
                    skip = self.caps_settings.setdefault("auto_skip", [])
                    if item[1] not in skip:
                        skip.append(item[1])
                        del skip[:-500]
                        save_settings(self.settings)
                    self.caps_status_set(f"Отменено — «{item[1]}» больше не исправляю автоматически", BLUE)
                elif kind == "show":
                    self.show_window()
                elif kind == "quit":
                    self.on_close()
                    return
                elif kind == "caps_toggle":
                    if self.caps_settings["enabled"]:
                        self.caps_sw.deselect()
                    else:
                        self.caps_sw.select()
                    self.on_caps_enabled()
                elif kind == "test_done":
                    self.test_btn.configure(state="normal", text="Проверить")
                elif kind == "info":
                    messagebox.showinfo(APP_NAME, item[1])
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _add_history(self, original, translated, direction, how):
        self.empty_label.pack_forget()
        item = HistoryItem(self.history, original, translated, direction, how, self.copy_from_history)
        if self.history_items:
            item.pack(fill="x", pady=4, before=self.history_items[-1])
        else:
            item.pack(fill="x", pady=4)
        self.history_items.append(item)
        if len(self.history_items) > 15:
            self.history_items.pop(0).destroy()
        try:
            self.history._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    def clear_history(self):
        for it in self.history_items:
            it.destroy()
        self.history_items.clear()
        self.empty_label.pack(pady=30)

    def copy_from_history(self, text):
        self._copy(text)
        self.set_status("Скопировано — Ctrl+V", GREEN)

    def refresh_models(self):
        ready = {"fast": self.engine.fast_ready(), "quality": self.engine.hymt.is_installed(),
                 "sage": self.engine.offline_speller.sage_installed()}
        for key, (dot, lab, btn, sub) in self.model_rows.items():
            active = key == self.engine.offline_engine or key == "sage"
            if ready[key]:
                dot.configure(text_color=GREEN if active else MUTED)
                lab.configure(text=("установлена · используется" if active else "установлена"))
                btn.configure(text="Обновить", state="disabled" if self.downloading else "normal")
            else:
                dot.configure(text_color=RED if active else MUTED)
                lab.configure(text=sub + (" · не скачана" if active else ""))
                btn.configure(text="Скачать", state="disabled" if self.downloading else "normal")

    def _update_mode_hint(self):
        hints = {
            "auto": "Через интернет, а без сети — офлайн",
            "online": "Всегда через интернет — лучшее качество",
            "offline": "Всё на твоём компьютере, интернет не нужен",
        }
        self.mode_hint.configure(text=hints[self.engine.mode])

    # ================================================================ обработчики
    def on_mode(self, label):
        mode = self.mode_map[label]
        self.engine.mode = mode
        self.settings["mode"] = mode
        save_settings(self.settings)
        self._update_mode_hint()
        if mode == "offline" and not self.engine.offline_ready():
            self.set_status("Для офлайна скачай модель — кнопка внизу", ORANGE)
        self._warm_async()

    def on_service(self, label):
        key = {v: k for k, v in self.service_names.items()}[label]
        self.engine.online.preferred = key
        self.settings["online_service"] = key
        save_settings(self.settings)
        self.set_status(f"Онлайн-перевод: {label}", GREEN)

    def on_fallback(self):
        self.engine.online.fallback = bool(self.fallback_switch.get())
        self.settings["online_fallback"] = self.engine.online.fallback
        save_settings(self.settings)

    def on_test_services(self):
        self.test_btn.configure(state="disabled", text="Проверяю…")
        self.set_status("Проверяю онлайн-сервисы…", BLUE)

        def work():
            try:
                res = self.engine.online.test_all()
                parts, lines = [], []
                for key, (ok, info, sec) in res.items():
                    name = self.engine.online.services[key].name
                    if ok is None:
                        parts.append(f"{name} —")
                        lines.append(f"—  {name}: {info}")
                    elif ok:
                        parts.append(f"{name} ✔")
                        lines.append(f"✔  {name} ({sec:.1f} с): {info}")
                    else:
                        parts.append(f"{name} ✖")
                        lines.append(f"✖  {name}: {info}")
                good = [k for k, v in res.items() if v[0]]
                self.set_status("  ".join(parts), GREEN if good else RED)
                self.ui("info", "Проверка онлайн-сервисов\n(фраза «Привет, нужен танк в подземелье»)\n\n"
                        + "\n\n".join(lines))
            except Exception as e:
                log("test failed: " + traceback.format_exc())
                self.set_status(str(e)[:80], RED)
            finally:
                self.ui("test_done")

        threading.Thread(target=work, daemon=True).start()

    def on_engine(self, label):
        eng = self.engine_map[label]
        self.engine.offline_engine = eng
        self.settings["offline_engine"] = eng
        save_settings(self.settings)
        self.refresh_models()
        if eng != "quality":
            threading.Thread(target=self.engine.hymt.stop, daemon=True).start()
        if not self.engine.offline_ready():
            self.set_status("Эта модель ещё не скачана — нажми «Скачать»", ORANGE)
        else:
            self.set_status("Офлайн-модель: " + label.lower(), GREEN)
        self._warm_async()

    def _warm_async(self):
        threading.Thread(target=self._warm_up, daemon=True).start()

    def on_topmost(self):
        self.settings["keep_on_top"] = bool(self.pin_switch.get())
        self.attributes("-topmost", self.settings["keep_on_top"])
        save_settings(self.settings)

    def on_autosend(self):
        self.settings["auto_send"] = bool(self.send_switch.get())
        save_settings(self.settings)

    def on_watch(self):
        self.settings["watch_clipboard"] = bool(self.watch_switch.get())
        save_settings(self.settings)

    HOTKEY_SLOTS = {"tr": ("hotkey", "hotkey_label", "f9"), "spell": ("spell_hotkey", "spell_hotkey_label", "f8")}

    def _hk_label(self, slot):
        key, lab, default = self.HOTKEY_SLOTS[slot]
        return self.settings.get(lab) or nice_key(self.settings.get(key) or default)

    def on_spell_before(self):
        self.settings["spell_before_translate"] = bool(self.spell_sw.get())
        save_settings(self.settings)
        self.set_status("Перед переводом ошибки будут исправляться" if self.settings["spell_before_translate"]
                        else "Перед переводом ошибки не исправляются", GREEN)

    def on_punct(self):
        self.settings["spell_punct"] = bool(self.punct_sw.get())
        save_settings(self.settings)
        self._apply_dimming()
        self.set_status("Пунктуация тоже проверяется" if self.settings["spell_punct"]
                        else "Проверяется только орфография", GREEN)

    def on_punct_exclude(self):
        self.settings["punct_only"] = self.punct_ex.get().strip()
        save_settings(self.settings)

    def _pick_app_into(self, entry, save_cb, what):
        from tkinter import filedialog
        if sys.platform == "darwin":
            self._pick_running_app(entry, save_cb, what)
            return
        if sys.platform == "win32":
            path = filedialog.askopenfilename(title="Выбери программу (например, Wow.exe)",
                                              filetypes=[("Программы", "*.exe"), ("Все файлы", "*.*")])
        else:
            path = filedialog.askdirectory(title="Выбери программу в «Программах»", initialdir="/Applications")
        if not path:
            return
        name = os.path.basename(path.rstrip("/\\"))
        name = name[:-4] if name.lower().endswith((".exe", ".app")) else name
        cur = [x.strip() for x in entry.get().replace(";", ",").split(",") if x.strip()]
        if name.lower() not in [c.lower() for c in cur]:
            cur.append(name)
        entry.delete(0, "end")
        entry.insert(0, ", ".join(cur))
        save_cb()
        self.set_status(f"{what}: {', '.join(cur)}", GREEN)

    def _add_app_name(self, entry, save_cb, what, name):
        cur = [x.strip() for x in entry.get().replace(";", ",").split(",") if x.strip()]
        if name.lower() not in [c.lower() for c in cur]:
            cur.append(name)
        entry.delete(0, "end")
        entry.insert(0, ", ".join(cur))
        save_cb()
        self.set_status(f"{what}: {', '.join(cur)}", GREEN)

    def _pick_running_app(self, entry, save_cb, what):
        """Mac: выбрать из открытых программ (в окне выбора файлов программы в «Программах» не выбираются)."""
        import mac_input
        w = ctk.CTkToplevel(self)
        w.title("Выбери программу")
        w.geometry("340x460")
        w.configure(fg_color=BG)
        w.attributes("-topmost", True)
        ctk.CTkLabel(w, text="Открытые программы", font=(FONT, 15, "bold"), text_color=GOLD).pack(
            anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(w, text="Нужной нет — открой её и нажми «Выбрать…» ещё раз", font=(FONT, 10),
                     text_color=MUTED).pack(anchor="w", padx=14)
        box = ctk.CTkScrollableFrame(w, fg_color=CARD, scrollbar_button_color=BORDER)
        box.pack(fill="both", expand=True, padx=14, pady=10)

        def choose(name):
            w.destroy()
            self._add_app_name(entry, save_cb, what, name)
        for name in mac_input.running_apps():
            ctk.CTkButton(box, text=name, anchor="w", height=30, font=(FONT, 12), fg_color="transparent",
                          hover_color=CARD_2, text_color=TEXT, command=lambda n=name: choose(n)).pack(fill="x")
        ctk.CTkButton(w, text="Отмена", width=100, height=30, font=(FONT, 12), fg_color="transparent",
                      hover_color=BORDER, border_width=1, border_color=BORDER, text_color=MUTED,
                      command=w.destroy).pack(pady=(0, 12))

    def _front_in_list(self, key, label):
        """Активная программа есть в списке настроек key? Пустой список — значит, везде."""
        only = [x.strip().lower() for x in self.settings.get(key, "").replace(";", ",").split(",") if x.strip()]
        if not only:
            return True
        raw = self._front_app_name()
        name = raw.lower().replace(".exe", "").replace(".app", "")
        ok = any(e.replace(".exe", "").replace(".app", "") in name for e in only if e) if name else False
        log(f"{label}: активная программа «{raw or 'не определена'}» → {'да' if ok else 'нет'}")
        return ok

    def on_pick_toast_app(self):
        self._pick_app_into(self.toast_ex, self.on_toast_apps, "Плашка будет появляться в")

    def on_toast_apps(self):
        self.settings["toast_only"] = self.toast_ex.get().strip()
        save_settings(self.settings)

    def on_pick_punct_app(self):
        from tkinter import filedialog
        if sys.platform == "win32":
            path = filedialog.askopenfilename(title="Выбери программу (например, Telegram.exe)",
                                              filetypes=[("Программы", "*.exe"), ("Все файлы", "*.*")])
        else:
            path = filedialog.askdirectory(title="Выбери программу в «Программах»", initialdir="/Applications")
        if not path:
            return
        name = os.path.basename(path.rstrip("/\\"))
        name = name[:-4] if name.lower().endswith((".exe", ".app")) else name
        cur = [x.strip() for x in self.punct_ex.get().replace(";", ",").split(",") if x.strip()]
        if name.lower() not in [c.lower() for c in cur]:
            cur.append(name)
        self.punct_ex.delete(0, "end")
        self.punct_ex.insert(0, ", ".join(cur))
        self.on_punct_exclude()
        self.set_status(f"Пунктуация будет проверяться в: {', '.join(cur)}", GREEN)

    def _front_app_name(self):
        try:
            import engine as _caps
            return _caps.process_name(_caps.foreground())
        except Exception:
            return ""

    def _punct_here(self):
        """Проверять ли пунктуацию в программе, где сейчас печатают."""
        if not self.settings.get("spell_punct", False):
            return False
        only = [x.strip().lower() for x in self.settings.get("punct_only", "").replace(";", ",").split(",")
                if x.strip()]
        if not only:
            return True  # список пуст — во всех программах
        raw = self._front_app_name()
        name = raw.lower().replace(".exe", "").replace(".app", "")
        ok = any(e.replace(".exe", "").replace(".app", "") in name for e in only if e) if name else False
        log(f"пунктуация: активная программа «{raw or 'не определена'}» → {'проверяю' if ok else 'не проверяю'}")
        return ok

    def on_glossary(self):
        if getattr(self, "_gl_win", None) and self._gl_win.winfo_exists():
            self._gl_win.lift()
            return
        w = ctk.CTkToplevel(self)
        self._gl_win = w
        w.title("Словарь игровых слов")
        w.geometry("560x620")
        w.configure(fg_color=BG)
        w.attributes("-topmost", True)
        ctk.CTkLabel(w, text="Словарь игровых слов", font=(FONT, 18, "bold"), text_color=GOLD).pack(
            anchor="w", padx=16, pady=(14, 2))
        ctk.CTkLabel(w, text="Каждая строка: русское = english. Варианты — через «/».\n"
                             "Эти слова корректор не трогает, а переводчик переводит именно так.\n"
                             "Русские окончания учитываются сами: «хил» найдёт и «хила», и «хилом».",
                     font=(FONT, 11), text_color=MUTED, justify="left").pack(anchor="w", padx=16)
        box = ctk.CTkTextbox(w, font=("Consolas" if sys.platform == "win32" else "Menlo", 12),
                             fg_color=CARD_2, text_color=TEXT, border_color=BORDER, border_width=1)
        box.pack(fill="both", expand=True, padx=16, pady=10)
        box.insert("1.0", self.settings.get("glossary") or core.DEFAULT_GLOSSARY)
        br = ctk.CTkFrame(w, fg_color="transparent")
        br.pack(fill="x", padx=16, pady=(0, 14))

        def save():
            text = box.get("1.0", "end").rstrip() + "\n"
            self.settings["glossary"] = text
            save_settings(self.settings)
            core.set_glossary(text)
            self.set_status("Словарь сохранён", GREEN)
            w.destroy()

        def reset():
            if messagebox.askyesno(APP_NAME, "Вернуть стандартный словарь? Твои изменения пропадут.", parent=w):
                box.delete("1.0", "end")
                box.insert("1.0", core.DEFAULT_GLOSSARY)

        ctk.CTkButton(br, text="Сохранить", width=120, height=32, font=(FONT, 13, "bold"), fg_color=GOLD,
                      hover_color=GOLD_HOVER, text_color=BG, command=save).pack(side="right")
        ctk.CTkButton(br, text="Стандартный словарь", width=160, height=32, font=(FONT, 12),
                      fg_color="transparent", hover_color=BORDER, border_width=1, border_color=BORDER,
                      text_color=MUTED, command=reset).pack(side="left")

        def add_new():
            new = core.glossary_missing(box.get("1.0", "end"))
            if not new:
                self.set_status("Новых слов в стандартном словаре нет", GREEN)
                return
            box.insert("end", "\n# ===== Добавлено из стандартного словаря =====\n" + "\n".join(new) + "\n")
            box.see("end")
            self.set_status(f"Добавлено строк: {len(new)} — нажми «Сохранить»", GREEN)

        ctk.CTkButton(br, text="Добавить новые", width=130, height=32, font=(FONT, 12),
                      fg_color="transparent", hover_color=BORDER, border_width=1, border_color=GOLD,
                      text_color=GOLD, command=add_new).pack(side="left", padx=(8, 0))

    def on_toast_toggle(self):
        self.settings["toast"] = bool(self.toast_switch.get())
        save_settings(self.settings)
        if self.settings["toast"]:
            self._show_toast("Hi! LF1M healer for raid", "Привет! Ищем ещё одного хила в рейд")

    def on_toast_pos(self, label):
        self.settings["toast_pos"] = self.toast_pos_map[label]
        save_settings(self.settings)
        self._show_toast("This is where translations will appear", "Здесь будут появляться переводы")

    def _show_toast(self, original, translated):
        """Плашка с переводом поверх всех окон на несколько секунд. Клик — закрыть.
        Обычное окно tkinter (без пересчёта масштаба customtkinter): размеры и место — в
        настоящих пикселях экрана, поэтому при масштабе Windows 125–150% ничего не съезжает."""
        import tkinter as tk
        try:
            old = getattr(self, "_toast", None)
            if old is not None and old.winfo_exists():
                old.destroy()
            t = tk.Toplevel(self)
            self._toast = t
            t.withdraw()                      # сначала строим невидимой, потом показываем на месте
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            try:
                t.attributes("-alpha", 0.95)
            except Exception:
                pass
            t.configure(bg=GOLD)
            box = tk.Frame(t, bg=CARD)
            box.pack(fill="both", expand=True, padx=2, pady=2)
            wrap = int(t.winfo_fpixels("1i") * 4.6)   # ~4,6 дюйма — одинаково при любом масштабе
            tk.Label(box, text=translated, font=(FONT, 13, "bold"), fg=TEXT, bg=CARD, wraplength=wrap,
                     justify="left", anchor="w").pack(fill="x", padx=12, pady=(8, 1))
            tk.Label(box, text=original, font=(FONT, 9), fg=MUTED, bg=CARD, wraplength=wrap,
                     justify="left", anchor="w").pack(fill="x", padx=12, pady=(0, 8))
            for wdg in (t, box) + tuple(box.winfo_children()):
                wdg.bind("<Button-1>", lambda e: t.destroy())
            t.update_idletasks()
            w, h = t.winfo_reqwidth(), t.winfo_reqheight()
            margin = int(t.winfo_fpixels("0.25i"))
            left, top, sw, sh = 0, 0, t.winfo_screenwidth(), t.winfo_screenheight()
            bottom = int(t.winfo_fpixels("0.8i"))   # над панелью задач / Dock
            if sys.platform == "win32":
                # рабочая область основного экрана в настоящих пикселях (без панели задач);
                # winfo_screenwidth при масштабе 125–150% даёт «уменьшенные» размеры
                try:
                    import ctypes
                    from ctypes import wintypes
                    rc = wintypes.RECT()
                    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rc), 0):
                        left, top, sw, sh = rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top
                        bottom = margin
                except Exception:
                    pass
            pos = self.settings.get("toast_pos", "br")
            if pos in ("br", "tr"):
                x = left + sw - w - margin
            elif pos in ("bl", "tl"):
                x = left + margin
            else:
                x = left + (sw - w) // 2
            y = top + margin if pos in ("tr", "tl", "tc") else top + sh - h - bottom
            tk.Wm.geometry(t, f"{w}x{h}+{max(0, x)}+{max(0, y)}")
            t.deiconify()
            t.lift()
            t.after(min(14000, 4500 + 70 * len(translated)), lambda: t.winfo_exists() and t.destroy())
        except Exception:
            log("toast: " + traceback.format_exc())
    # ---------- затемнение выключенных функций
    DIM = "#5a5e6b"

    DIM_PROPS = {
        "CTkLabel": ("text_color", "fg_color"),
        "CTkButton": ("text_color", "border_color", "fg_color"),
        "CTkSwitch": ("progress_color", "text_color"),
        "CTkSegmentedButton": ("selected_color", "selected_hover_color", "text_color"),
        "CTkEntry": ("text_color", "border_color"),
        "CTkSlider": ("progress_color", "button_color"),
        "CTkOptionMenu": ("text_color",),
    }

    def _dim(self, w, on, keep=()):
        """Сделать виджет (и всё внутри рамок) активным/серым. keep — что не трогать."""
        if w in keep:
            return
        kind = type(w).__name__
        if kind in self.DIM_PROPS or isinstance(w, ctk.CTkLabel):
            props = self.DIM_PROPS.get(kind, self.DIM_PROPS["CTkLabel"])
            if not hasattr(w, "_orig_colors"):
                w._orig_colors = {}
                for p in props:
                    try:
                        w._orig_colors[p] = w.cget(p)
                    except Exception:
                        pass
            for p, orig in w._orig_colors.items():
                if on:
                    val = orig
                elif p == "fg_color":
                    val = orig if orig == "transparent" else "#30343f"
                elif p == "text_color" and kind == "CTkButton" and w._orig_colors.get("fg_color") not in (None, "transparent"):
                    val = "#7d818d"
                else:
                    val = self.DIM
                try:
                    w.configure(**{p: val})
                except Exception:
                    pass
            if not isinstance(w, ctk.CTkLabel):
                try:
                    w.configure(state="normal" if on else "disabled")
                except Exception:
                    pass
            return
        if isinstance(w, ctk.CTkFrame):          # рамки и карточки — идём внутрь
            for ch in w.winfo_children():
                self._dim(ch, on, keep)
            return
        try:
            w.configure(state="normal" if on else "disabled")
        except Exception:
            pass
    def _apply_dimming(self):
        tr = self.settings.get("tr_enabled", True)
        sp = self.settings.get("spell_enabled", True)
        punct_ws = [self._punct_lbl, self.punct_ex, self._punct_pick]
        # вкладка «Перевод»: всё, кроме переключателя
        for card in (self._g_mode, self._g_mine, self._g_manual, self._g_hist):
            self._dim(card, tr)
        # вкладка «Ошибки»
        for card in (self._g_spell, self._g_spell_quick):
            self._dim(card, sp)
        # «исправлять перед переводом» имеет смысл, только если включено и то и другое
        self._dim(self.spell_sw, tr and sp)
        # список программ для пунктуации — только когда пунктуация включена
        for wdg in punct_ws:
            self._dim(wdg, sp and self.settings.get("spell_punct", False))
        # вкладка «Раскладка»: всё, кроме переключателя «Включено»
        caps = self.caps_settings.get("enabled", True)
        for card in (self._g_caps_how, self._g_caps_try, self._g_caps_set):
            self._dim(card, caps, keep=(self.caps_sw,))
        # режим «без нажатий»: список программ — только когда режим включён
        self._dim(self.clipmode_sw, tr)
        for wdg in (self._clipmode_lbl, self.clipmode_ex, self._clipmode_pick):
            self._dim(wdg, tr and self.settings.get("clip_ru2en", False))
        # список программ для автоисправления — только когда оно включено
        for wdg in (self._auto_lbl, self.auto_ex, self._auto_pick):
            self._dim(wdg, caps and self.caps_settings.get("auto", False))

    def on_feature(self, key, sw):
        on = bool(sw.get())
        self.settings[key] = on
        save_settings(self.settings)
        self._setup_hotkey()      # выключенная функция отпускает клавишу
        try:
            self._update_tray()
        except Exception:
            pass
        self._apply_dimming()
        name = "Перевод" if key == "tr_enabled" else "Проверка ошибок"
        self.set_status(f"{name}: {'включено' if on else 'выключено'}", GREEN if on else MUTED)

    def on_manual_spell(self):
        text = self.spell_entry.get().strip()
        if not text:
            return
        self.spell_entry.delete(0, "end")

        def work():
            try:
                self.set_status("Проверяю ошибки…", BLUE)
                fixed, n, how = self.engine.spell(text, punctuation=self.settings.get("spell_punct", False))
                self._copy(fixed)
                if n:
                    self.ui("history", text, fixed, "spell", how)
                    self.set_status(f"Исправлено ошибок: {n} — скопировано, можно Ctrl+V", GREEN)
                else:
                    self.set_status("Ошибок не нашёл", GREEN)
            except Exception as e:
                log("manual spell failed: " + traceback.format_exc())
                self.set_status(str(e)[:80], RED)

        threading.Thread(target=work, daemon=True).start()

    def on_rebind(self, slot="tr"):
        if self.binding or not self.kb:
            return
        self.binding = True
        btn = self.key_btn if slot == "tr" else self.spell_btn
        btn.configure(text="Жми…")
        self.set_status("Нажми новую клавишу или сочетание (Esc — отмена)", BLUE)
        key_name, label_name, _ = self.HOTKEY_SLOTS[slot]

        def work():
            kb = self.kb
            try:
                for h in list(self.hotkey_handles.values()):
                    try:
                        kb.remove_hotkey(h)
                    except Exception:
                        pass
                self.hotkey_handles = {}
                time.sleep(0.3)
                pressed = {}          # scan_code -> имя
                order = []
                while True:
                    ev = kb.read_event(suppress=False)
                    if ev.event_type == "down":
                        if ev.scan_code not in pressed:
                            pressed[ev.scan_code] = (ev.name or "").lower()
                            order.append(ev.scan_code)
                    elif ev.event_type == "up" and pressed:
                        break
                names = [pressed[sc] for sc in order]
                if names == ["esc"]:
                    return
                order.sort(key=lambda sc: 0 if pressed[sc] in MODIFIER_NAMES else 1)
                if all(pressed[sc] in MODIFIER_NAMES for sc in order):
                    self.set_status("Нужна обычная клавиша, не только Ctrl/Alt/Shift", ORANGE)
                    return
                label = "+".join(
                    (pressed[sc].replace("left ", "").replace("right ", "") or f"#{sc}").upper()
                    if len(pressed[sc]) <= 1 else
                    (pressed[sc].replace("left ", "").replace("right ", "") or f"#{sc}").capitalize()
                    for sc in order)
                other = "spell" if slot == "tr" else "tr"
                if label == self._hk_label(other):
                    self.set_status("Эта клавиша уже занята другой функцией", ORANGE)
                    return
                self.settings[key_name] = order
                self.settings[label_name] = label
                save_settings(self.settings)
            except Exception:
                log("rebind failed: " + traceback.format_exc())
            finally:
                self.binding = False
                self._setup_hotkey()

        threading.Thread(target=work, daemon=True).start()

    def _entry_ctrl_key(self, event):
        """Ctrl+V/C/X/A по коду клавиши — работает и при русской раскладке
        (и когда текст вставляют программы диктовки вроде Handy)."""
        action = EDIT_KEYCODES.get(event.keycode)
        if not action:
            return None
        if event.keysym.lower() in ("v", "c", "x", "a"):
            if action != "select_all":
                return None  # английская раскладка — Tk справится сам
        if action == "select_all":
            self.entry.select_range(0, "end")
            self.entry.icursor("end")
        elif action == "<<Paste>>":
            return self._entry_paste()
        else:
            self.entry._entry.event_generate(action)
        return "break"

    def _entry_paste(self, _event=None):
        try:
            text = self.clipboard_get()
        except Exception:
            try:
                text = self._clip()
            except Exception:
                return "break"
        text = text.replace("\r", " ").replace("\n", " ")
        w = self.entry._entry
        try:
            if w.selection_present():
                w.delete("sel.first", "sel.last")
        except Exception:
            pass
        w.insert("insert", text)
        return "break"

    def on_install_file(self):
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            title="Выбери файлы моделей (.argosmodel или .gguf)",
            filetypes=[("Офлайн-модель", "*.argosmodel *.zip *.gguf"), ("Все файлы", "*.*")])
        if not paths:
            return

        def work():
            done = []
            for p in paths:
                name = os.path.basename(p).lower()
                if "sage" in name and name.endswith(".zip"):
                    try:
                        self.set_status("Устанавливаю модель проверки ошибок…", BLUE)
                        self.engine.offline_speller.install_from_file(p)
                        done.append("SAGE")
                    except Exception as e:
                        log("install sage failed: " + traceback.format_exc())
                        self.ui("error", f"Не удалось установить {os.path.basename(p)}:\n{e}")
                    continue
                if name.endswith(".gguf"):
                    try:
                        def prog(pct, msg):
                            self.ui("progress", pct / 100 if pct >= 0 else None)
                            self.set_status(msg, BLUE)
                        self.engine.hymt.install_from_file(p, prog)
                        done.append("HY-MT")
                    except Exception as e:
                        log("install gguf failed: " + traceback.format_exc())
                        self.ui("error", f"Не удалось установить {os.path.basename(p)}:\n{e}")
                    finally:
                        self.ui("progress", None)
                    continue
                key = ("en", "ru") if "en_ru" in name else ("ru", "en") if "ru_en" in name else None
                if key is None:
                    self.ui("error", f"Не понял, что за файл: {os.path.basename(p)}\n"
                                     "В названии должно быть ru_en или en_ru.")
                    continue
                try:
                    self.set_status(f"Устанавливаю {os.path.basename(p)}…", BLUE)
                    self.engine.offline[key].install_from_file(p)
                    done.append("RU→EN" if key == ("ru", "en") else "EN→RU")
                except Exception as e:
                    log("install file failed: " + traceback.format_exc())
                    self.ui("error", f"Не удалось установить {os.path.basename(p)}:\n{e}")
            if done:
                self.set_status("Установлено: " + ", ".join(done), GREEN)
            self.ui("models")
            self._warm_async()

        threading.Thread(target=work, daemon=True).start()

    def on_download(self, which="fast"):
        if self.downloading:
            return
        self.downloading = True
        self.refresh_models()
        self.ui("progress", 0)

        def work():
            try:
                if which == "sage":
                    def prog(pct, msg):
                        if pct >= 0:
                            self.ui("progress", pct / 100)
                        self.set_status(msg + (f" ({pct}%)" if pct >= 0 else ""), BLUE)
                    self.engine.offline_speller.download(prog, "Модель SAGE")
                    self.set_status("Офлайн-проверка ошибок готова ✔", GREEN)
                elif which == "quality":
                    def prog(pct, msg):
                        if pct >= 0:
                            self.ui("progress", pct / 100)
                        self.set_status(msg + (f" ({pct}%)" if pct >= 0 else ""), BLUE)
                    self.engine.hymt.download(prog, "Модель HY-MT")
                    self.set_status("Качественная модель готова ✔", GREEN)
                else:
                    labels = {("ru", "en"): "Модель RU→EN", ("en", "ru"): "Модель EN→RU"}
                    items = list(self.engine.offline.items())
                    for i, (key, model) in enumerate(items):
                        def prog(pct, msg, i=i):
                            if pct >= 0:
                                self.ui("progress", (i + pct / 100) / len(items))
                            self.set_status(msg + (f" ({pct}%)" if pct >= 0 else ""), BLUE)
                        model.download(prog, labels[key])
                    self.set_status("Быстрые модели готовы ✔", GREEN)
            except Exception as e:
                log("download failed: " + traceback.format_exc())
                self.set_status("Не удалось скачать модель", RED)
                if which == "sage":
                    self.ui("error", "Не удалось скачать модель проверки ошибок:\n%s\n\n"
                                     "Попробуй ещё раз позже или скачай файл sage-ru-ct2-int8.zip вручную:\n"
                                     "github.com/Chame1eon90/capslocker/releases/tag/models\n"
                                     "и нажми «Из файла»." % e)
                elif which == "quality":
                    self.ui("error", "Не удалось скачать модель:\n%s\n\n"
                                     "Варианты:\n"
                                     "• включи VPN и нажми «Скачать» ещё раз (докачает с того же места);\n"
                                     "• или скачай файл через браузер и нажми «Из файла»:\n"
                                     "  huggingface.co/tencent/HY-MT1.5-1.8B-GGUF\n"
                                     "  файл HY-MT1.5-1.8B-Q4_K_M.gguf" % e)
                else:
                    self.ui("error", "Не удалось скачать модели:\n%s\n\n"
                                     "Сервер моделей может не открываться из России. Варианты:\n"
                                     "• включи VPN и нажми «Скачать» ещё раз;\n"
                                     "• или скачай 2 файла через браузер и нажми «Из файла»:\n"
                                     "  argos-net.com/v1/translate-ru_en-1_9.argosmodel\n"
                                     "  argos-net.com/v1/translate-en_ru-1_9.argosmodel" % e)
            finally:
                self.downloading = False
                self.ui("progress", None)
                self.ui("models")
                self._warm_async()

        threading.Thread(target=work, daemon=True).start()

    def on_manual(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")

        def work():
            try:
                self.set_status("Перевожу…", BLUE)
                direction = detect_direction(text)
                result, how = self.engine.translate(text)
                self._copy(result)
                if direction:
                    self.ui("history", text, result, direction, how)
                self.set_status(f"Скопировано ({how}) — можно Ctrl+V", GREEN)
                if direction == ("ru", "en"):
                    self._warn_length(result)
            except Exception as e:
                log("manual failed: " + traceback.format_exc())
                self.set_status(str(e)[:80], RED)

        threading.Thread(target=work, daemon=True).start()

    # ================================================================ буфер обмена
    def _copy(self, text):
        import pyperclip
        self.own_clip.add(text)
        if len(self.own_clip) > 60:
            self.own_clip = {text}
        pyperclip.copy(text)

    def _clip(self):
        import pyperclip
        return pyperclip.paste()

    def _warn_length(self, text):
        if len(text) > WOW_CHAT_LIMIT:
            self.set_status(f"Длинно: {len(text)} симв., чат WoW примет только {WOW_CHAT_LIMIT}", ORANGE)

    def _clipboard_watcher(self):
        try:
            self.last_clip = self._clip()
        except Exception:
            pass
        while True:
            time.sleep(0.5)
            if not (self.settings.get("watch_clipboard") or self.settings.get("clip_ru2en")) \
                    or not self.settings.get("tr_enabled", True) or self.busy:
                continue
            try:
                text = self._clip()
            except Exception:
                continue
            if not text or text == self.last_clip:
                continue
            self.last_clip = text
            import engine as _caps
            if time.time() < _caps.CLIP_QUIET_UNTIL:
                continue  # это раскладка взяла текст, а не пользователь скопировал
            if text in self.own_clip or len(text) > 2000:
                continue
            direction = detect_direction(text)
            if direction == ("ru", "en") and self.settings.get("clip_ru2en"):
                self._clip_ru2en(text)
                continue
            if direction != ("en", "ru") or not self.settings.get("watch_clipboard"):
                continue
            toast_here = self.settings.get("toast", True) and self._front_in_list("toast_only", "плашка")
            try:
                self.set_status("Перевожу входящее…", BLUE)
                result, how = self.engine.translate(text, ("en", "ru"))
                self.ui("history", text.strip(), result, ("en", "ru"), how)
                if toast_here:
                    self.ui("toast", text.strip(), result)
                self.set_status(f"Входящее переведено ({how})", GREEN)
            except Exception as e:
                log("incoming failed: " + traceback.format_exc())
                self.set_status(str(e)[:80], RED)

    def _clip_ru2en(self, text):
        """Режим «без нажатий»: скопированную русскую фразу заменить в буфере английским переводом."""
        if not self._front_in_list("clip_ru2en_only", "без нажатий"):
            return
        try:
            self.set_status("Перевожу скопированное…", BLUE)
            src_text = text
            if self.settings.get("spell_before_translate", False) and self.settings.get("spell_enabled", True):
                try:
                    src_text, _, _ = self.engine.spell(text, punctuation=self._punct_here())
                except Exception:
                    src_text = text
            result, how = self.engine.translate(src_text, ("ru", "en"))
            if self._clip() != text:
                return  # пока переводили, скопировали что-то другое
            self._copy(result)
            self.last_clip = result
            self.ui("history", text.strip(), result.strip(), ("ru", "en"), how)
            if self.settings.get("toast", True):
                self.ui("toast", "Перевод в буфере — нажми Ctrl+V", result.strip())
            self.set_status(f"Перевод в буфере — нажми Ctrl+V ({how})", GREEN)
            self._warn_length(result)
        except Exception as e:
            log("clip ru2en failed: " + traceback.format_exc())
            self.set_status(str(e)[:80], RED)

    # ================================================================ горячая клавиша
    def _setup_hotkey(self):
        try:
            import keyboard
            self.kb = keyboard
        except Exception:
            self.set_status("Горячая клавиша недоступна", RED)
            return
        for h in list(self.hotkey_handles.values()):
            try:
                self.kb.remove_hotkey(h)
            except Exception:
                pass
        self.hotkey_handles = {}
        for slot, (key_name, label_name, default) in self.HOTKEY_SLOTS.items():
            if not self.settings.get("tr_enabled" if slot == "tr" else "spell_enabled", True):
                continue      # функция выключена — клавишу не занимаем
            hk = self.settings.get(key_name) or default
            if isinstance(hk, list):
                hk = tuple(hk)   # сочетание скан-кодов; список библиотека не принимает
            try:
                self.hotkey_handles[slot] = self.kb.add_hotkey(hk, lambda s=slot: self._on_hotkey(s), suppress=True)
            except Exception as e:
                log("hotkey %s failed: %r (%r), back to %s" % (slot, e, hk, default))
                self.settings[key_name] = default
                self.settings.pop(label_name, None)
                save_settings(self.settings)
                try:
                    self.hotkey_handles[slot] = self.kb.add_hotkey(default, lambda s=slot: self._on_hotkey(s),
                                                                   suppress=True)
                except Exception:
                    self.set_status("Не удалось назначить клавишу", RED)
        self.ui("key", self._hk_label("tr"))
        self.ui("spellkey", self._hk_label("spell"))
        self.set_status(f"Готово. Перевод — {self._hk_label('tr')}, ошибки — {self._hk_label('spell')}", GREEN)

    def _on_hotkey(self, slot="tr"):
        if self.busy or self.binding:
            return
        threading.Thread(target=self._hotkey_work, args=(slot,), daemon=True).start()

    def _hotkey_work(self, slot="tr"):
        kb = self.kb
        self.busy = True
        try:
            for mod in ("ctrl", "alt", "shift"):
                deadline = time.time() + 1
                while kb.is_pressed(mod) and time.time() < deadline:
                    time.sleep(0.02)
            old_clip = ""
            try:
                old_clip = self._clip()
            except Exception:
                pass
            punct = self._punct_here()
            marker = "⁣wowtr⁣"
            selection = False
            text = marker
            if slot == "spell":
                # сначала пробуем выделенный текст: проверим только его
                self._copy(marker)
                kb.send([SC_CTRL, SC_C])
                for _ in range(12):
                    time.sleep(0.03)
                    text = self._clip()
                    if text != marker:
                        break
                selection = bool(text and text != marker and text.strip())
            if not selection:
                self._copy(marker)
                kb.send([SC_CTRL, SC_A])
                time.sleep(0.05)
                kb.send([SC_CTRL, SC_C])
                text = marker
                for _ in range(20):
                    time.sleep(0.03)
                    text = self._clip()
                    if text != marker:
                        break
            if text == marker or not text.strip():
                self._copy(old_clip)
                self.set_status("Не нашёл текст. Открой чат и напиши фразу", ORANGE)
                return
            if slot == "spell":
                self.set_status("Проверяю ошибки…", BLUE)
                result, n, how = self.engine.spell(text, punctuation=punct)
                if not n:
                    self._copy(old_clip)
                    self.set_status("Ошибок не нашёл", GREEN)
                    return
            else:
                src_text = text
                if self.settings.get("spell_before_translate", False) and core.CYR.search(text):
                    try:
                        src_text, _, _ = self.engine.spell(text, punctuation=punct)
                    except Exception:
                        src_text = text  # проверка недоступна — переводим как есть
                self.set_status("Перевожу…", BLUE)
                result, how = self.engine.translate(src_text, ("ru", "en"))
            if selection and text != text.strip():
                # сохраняем пробелы по краям выделения
                lead = text[:len(text) - len(text.lstrip())]
                trail = text[len(text.rstrip()):]
                result = lead + result + trail
            self._copy(result)   # исправленный текст остаётся в буфере обмена
            if not selection:
                kb.send([SC_CTRL, SC_A])
                time.sleep(0.03)
            kb.send([SC_CTRL, SC_V])
            time.sleep(0.05)
            if self.settings.get("auto_send") and slot != "spell":
                kb.send(SC_ENTER)
            if result.strip() != text.strip():
                self.ui("history", text.strip(), result.strip(), "spell" if slot == "spell" else ("ru", "en"), how)
            if slot == "spell":
                self.set_status(f"Исправлено ошибок: {n} ({how})", GREEN)
            else:
                self.set_status(f"Вставлено в чат ({how})", GREEN)
                self._warn_length(result)
        except Exception as e:
            log("hotkey failed: " + traceback.format_exc())
            self.set_status(str(e)[:80], RED)
        finally:
            try:
                self.last_clip = self._clip()
            except Exception:
                pass
            self.busy = False

    def _warm_up(self):
        """Заранее модели грузим только в режиме «Офлайн».
        В «Авто» модель загрузится сама, когда пропадёт интернет."""
        try:
            if self.engine.mode != "offline":
                if self.engine.unload_models():
                    self.set_status("Офлайн-модель выгружена — загрузится, если пропадёт интернет", MUTED)
                return
            self.engine.last_offline_use = time.time()
            if self.engine.offline_engine == "quality":
                for m in self.engine.offline.values():
                    m.unload()
                if self.engine.hymt.is_installed():
                    self.set_status("Загружаю качественную модель…", BLUE)
                    self.engine.hymt.start()
                    self.set_status("Качественная модель загружена", GREEN)
            else:
                self.engine.hymt.unload()
                for m in self.engine.offline.values():
                    m.warm_up()
        except Exception as e:
            log("warm_up failed: " + traceback.format_exc())
            self.set_status(str(e)[:80], RED)

    def _idle_watcher(self):
        """Выгружает офлайн-модели, если ими не пользовались заданное время."""
        while True:
            time.sleep(20)
            try:
                mins = int(self.settings.get("unload_minutes", 10) or 0)
                if mins <= 0 or not self.engine.models_loaded():
                    continue
                if time.time() - self.engine.last_offline_use >= mins * 60:
                    if self.engine.unload_models():
                        self.set_status(f"Офлайн-модель выгружена: не использовалась {mins} мин", MUTED)
            except Exception:
                log("idle watcher: " + traceback.format_exc())

    def on_open_data(self):
        try:
            if sys.platform == "win32":
                os.startfile(core.DATA_DIR)
            else:
                import subprocess
                subprocess.run(["open", core.DATA_DIR])
        except Exception as e:
            self.set_status(f"Не удалось открыть папку: {e}", RED)

    def on_unload_minutes(self, label):
        self.settings["unload_minutes"] = self.unload_map[label]
        save_settings(self.settings)
        self.set_status(("Не выгружать модель" if not self.unload_map[label]
                         else f"Модель выгрузится после {label} простоя"), GREEN)

    def on_close(self):
        try:
            self.engine.hymt.stop()
        except Exception:
            pass
        try:
            self.caps_engine.stop()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            if self.kb:
                self.kb.unhook_all()
        except Exception:
            pass
        self.destroy()
        os._exit(0)


def main():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Capslocker_single_instance_mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:
        messagebox.showinfo(APP_NAME, "Капслокер уже запущен — ищи значок в трее рядом с часами.")
        return
    try:
        app = App()
        app.on_tab({v: k for k, v in app.tab_map.items()}[app.settings.get("tab", "tr")])
        app.tab_seg.set({v: k for k, v in app.tab_map.items()}[app.settings.get("tab", "tr")])
        if "--tray" in sys.argv and not app.settings.get("first_run"):
            app.withdraw()
        app.mainloop()
    except Exception:
        err = traceback.format_exc()
        log("fatal: " + err)
        try:
            messagebox.showerror(APP_NAME, "Программа упала с ошибкой:\n\n" + err[-800:])
        except Exception:
            print(err, file=sys.stderr)


if __name__ == "__main__":
    main()
