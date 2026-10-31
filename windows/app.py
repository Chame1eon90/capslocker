# -*- coding: utf-8 -*-
"""Капслокер для Windows: горячие клавиши через keyboard, значок в трее, автозапуск через реестр."""
import ctypes
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

from tkinter import messagebox  # noqa: E402

import core  # noqa: E402
import engine as caps_engine_mod  # noqa: E402
from engine import Engine as CapsEngine  # noqa: E402
from core import save_settings, log  # noqa: E402
from app_base import (AppBase, APP_NAME, GREEN, RED, BLUE, ORANGE, resource_path, run)  # noqa: E402,F401

def nice_key(hk):
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in str(hk).split("+"))


# Скан-коды клавиш: работают при любой раскладке (русской тоже)
SC_CTRL, SC_A, SC_C, SC_V, SC_ENTER = 29, 30, 46, 47, 28
MODIFIER_NAMES = {"ctrl", "left ctrl", "right ctrl", "alt", "left alt", "right alt", "alt gr",
                  "shift", "left shift", "right shift", "windows", "left windows", "right windows"}
# Windows keycode -> действие для поля ввода (не зависит от раскладки)
EDIT_KEYCODES = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "select_all"}


class App(AppBase):
    def _platform_init(self):
        try:
            self.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

    def _start_input(self):
        self.caps_engine = CapsEngine(self.caps_settings, on_event=lambda k, t: self.ui("capsev", k, t))
        self.caps_engine.start()
        self._start_tray()

    def _bind_entry_keys(self, entry):
        entry.bind("<Control-KeyPress>", self._entry_ctrl_key)
        entry.bind("<Shift-Insert>", self._entry_paste)

    def _clip_quiet_until(self):
        return caps_engine_mod.CLIP_QUIET_UNTIL

    def _platform_close(self):
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

    def _hk_label(self, slot):
        key, lab, default = self.HOTKEY_SLOTS[slot]
        return self.settings.get(lab) or nice_key(self.settings.get(key) or default)

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

    def _front_app_name(self):
        try:
            import engine as _caps
            return _caps.process_name(_caps.foreground())
        except Exception:
            return ""

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
                if how == "без перевода":
                    # фраза не похожа на русскую — покажем, что именно скопировалось из чата
                    log(f"hotkey: перевод пропущен, из чата скопировано {text[:80]!r}")
                    self.set_status(f"Не перевёл: из чата скопировалось «{text.strip()[:40]}» — это не русский текст",
                                    ORANGE)
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

    def _copy(self, text):
        import pyperclip
        self.own_clip.add(text)
        if len(self.own_clip) > 60:
            self.own_clip = {text}
        pyperclip.copy(text)

    def _clip(self):
        import pyperclip
        return pyperclip.paste()

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


def main():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Capslocker_single_instance_mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:
        messagebox.showinfo(APP_NAME, "Капслокер уже запущен — ищи значок в трее рядом с часами.")
        return
    run(App)


if __name__ == "__main__":
    main()
