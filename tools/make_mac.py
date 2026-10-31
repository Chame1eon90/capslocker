import re
s = open('windows/app.py', encoding='utf-8').read()

def rep(a, b, cnt=1):
    global s
    assert a in s, ("MISSING", a[:90])
    s = s.replace(a, b, cnt)

def method(name, new):
    """Заменить метод класса App целиком."""
    global s
    m = re.search(r'\n    def ' + re.escape(name) + r'\(.*?(?=\n    def |\n\n\ndef |\nclass )', s, re.S)
    assert m, name
    s = s[:m.start()] + '\n' + new.rstrip('\n') + '\n' + s[m.end():]

rep('from engine import Engine as CapsEngine\n', 'import mac_input\nfrom mac_input import MacInput, hotkey_label\n')
rep('FONT = "Segoe UI"', 'FONT = "Helvetica Neue"')
rep('''        try:
            self.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass''', '''        try:
            from tkinter import PhotoImage
            self._icon_img = PhotoImage(file=resource_path("icon.png"))
            self.iconphoto(True, self._icon_img)
        except Exception:
            pass
        self.main_calls = queue.Queue()
        self.after(15, self._main_pump)
        # Dock: клик по значку — показать окно; ⌘Q — выйти
        try:
            self.createcommand("::tk::mac::ReopenApplication", self.show_window)
            self.createcommand("::tk::mac::Quit", self.on_close)
        except Exception:
            pass''')
rep('''        self._setup_hotkey()
        self.refresh_models()''', '''        self.refresh_models()''')
rep('''        self.caps_engine = CapsEngine(self.caps_settings, on_event=lambda k, t: self.ui("capsev", k, t))
        self.caps_engine.start()
        self._start_tray()''', '''        self.caps_engine = MacInput(self.caps_settings, self.call_main,
                                    on_event=lambda k, t: self.ui("capsev", k, t))
        self.caps_engine.on_hotkey = lambda slot: threading.Thread(target=self._hotkey_work, args=(slot,),
                                                                   daemon=True).start()
        self._setup_hotkey()
        self.after(400, self._start_input)''')
# окно: ⌘ вместо Ctrl в подсказках, горячая клавиша по умолчанию

rep('''        self.entry.bind("<Control-KeyPress>", self._entry_ctrl_key)
        self.entry.bind("<Shift-Insert>", self._entry_paste)
''', '')
s = s.replace('(Ctrl+A, Ctrl+C)', '(⌘A, ⌘C)').replace('Ctrl+V', '⌘V').replace('(Ctrl+C)', '(⌘C)')
# качественная модель (Tencent) — только Windows: прячем
rep('''        self.engine_map = {"Быстрая": "fast", "Качественная": "quality"}''', '''        self.engine_map = {"Быстрая": "fast"}
        self.engine.offline_engine = "fast"''')
rep('''                                ("quality", "Качественная · Tencent HY-MT", "~1,1 ГБ, точнее, медленнее"),
''', '')
rep('''        ready = {"fast": self.engine.fast_ready(), "quality": self.engine.hymt.is_installed(),''',
    '''        ready = {"fast": self.engine.fast_ready(),''')
# вкладка раскладки: подписи
rep('("Держать Caps", "Обычный Caps Lock (ЗАГЛАВНЫЕ)")', '("Держать Caps", "Обычный Caps Lock (ЗАГЛАВНЫЕ)")')
rep('''        ctk.CTkLabel(st.body, text="Не работать в программах (через запятую, например: game.exe):",''',
    '''        ctk.CTkLabel(st.body, text="Не работать в программах (названия через запятую, например: World of Warcraft):",''')
rep('''"Запускать Капслокер вместе с Windows (в трее)"''', '''"Запускать Капслокер при входе в систему"''')
rep('''        self.caps_status = ctk.CTkLabel(P, text="Работает. Закрытие окна сворачивает Капслокер в трей.",''',
    '''        self.caps_status = ctk.CTkLabel(P, text="Закрытие окна не выключает программу. Выход — ⌘Q.",''')
rep('''    def on_caps_enabled(self):
        self.caps_settings["enabled"] = bool(self.caps_sw.get())
        save_settings(self.settings)''', '''    def on_caps_enabled(self):
        self.caps_settings["enabled"] = bool(self.caps_sw.get())
        save_settings(self.settings)
        threading.Thread(target=self.caps_engine.apply_caps_remap, daemon=True).start()''')

method('on_autostart', '''    def on_autostart(self):
        on = bool(self.auto_sw.get())
        plist = os.path.expanduser("~/Library/LaunchAgents/com.capslocker.app.plist")
        try:
            if on:
                exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
                os.makedirs(os.path.dirname(plist), exist_ok=True)
                with open(plist, "w", encoding="utf-8") as f:
                    f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.capslocker.app</string>
<key>ProgramArguments</key><array><string>{exe}</string><string>--tray</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
""")
            elif os.path.exists(plist):
                os.remove(plist)
            self.settings["autostart"] = on
            save_settings(self.settings)
            self.caps_status_set("Будет запускаться при входе в систему" if on else "Автозапуск выключен", GREEN)
        except Exception as e:
            log("autostart: " + traceback.format_exc())
            self.caps_status_set(f"Не удалось: {e}", RED)
''')
for name in ('_tray_image', '_start_tray', '_entry_ctrl_key', '_entry_paste'):
    method(name, '')
method('_update_tray', '''    def _update_tray(self):
        pass
''')
method('hide_to_tray', '''    def hide_to_tray(self):
        try:
            self.on_exclude()
        except Exception:
            pass
        if self.settings.get("first_run"):
            self.settings["first_run"] = False
            save_settings(self.settings)
            messagebox.showinfo(APP_NAME, "Капслокер продолжает работать. Окно вернётся по клику на значок в Dock.\\n"
                                          "Выйти совсем — ⌘Q.")
        self.withdraw()

    # ---------- главный поток (раскладки macOS можно трогать только из него)
    def call_main(self, fn, timeout=2.0):
        if threading.current_thread() is threading.main_thread():
            return fn()
        done = threading.Event()
        box = {}
        self.main_calls.put((fn, done, box))
        done.wait(timeout)
        return box.get("result")

    def _main_pump(self):
        try:
            while True:
                fn, done, box = self.main_calls.get_nowait()
                try:
                    box["result"] = fn()
                except Exception:
                    log("main call: " + traceback.format_exc())
                done.set()
        except queue.Empty:
            pass
        self.after(15, self._main_pump)

    # ---------- разрешения и запуск перехвата
    def _start_input(self):
        def work():
            if self.caps_engine.start():
                self.ui("perm", True)
                return
            mac_input.has_permissions(prompt=True)
            self.ui("perm", False)
            while not self.caps_engine.start():
                time.sleep(3)
            self.ui("perm", True)
        threading.Thread(target=work, daemon=True).start()

    def _show_perm_banner(self, ok):
        if ok:
            if getattr(self, "perm_banner", None):
                self.perm_banner.destroy()
                self.perm_banner = None
            self.set_status(f"Готово. Перевод — {self._hk_label('tr')}, ошибки — {self._hk_label('spell')}", GREEN)
            self.caps_status_set("Работает. Закрытие окна не выключает программу. Выход — ⌘Q.", GREEN)
            return
        if getattr(self, "perm_banner", None):
            return
        b = ctk.CTkFrame(self, fg_color="#3a2a12", corner_radius=12, border_width=1, border_color=GOLD)
        b.pack(fill="x", padx=18, pady=(0, 8), after=self.tab_seg)
        ctk.CTkLabel(b, text="Нужны разрешения macOS", font=(FONT, 13, "bold"), text_color=GOLD).pack(
            anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(b, text="Системные настройки → Конфиденциальность и безопасность:\\n"
                             "включи Капслокер в «Универсальный доступ» и «Мониторинг ввода».\\n"
                             "Программа сама заметит разрешение через пару секунд.",
                     font=(FONT, 11), text_color=TEXT, justify="left").pack(anchor="w", padx=14)
        r = ctk.CTkFrame(b, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=(6, 10))
        ctk.CTkButton(r, text="Универсальный доступ", height=28, font=(FONT, 12), fg_color=GOLD,
                      hover_color=GOLD_HOVER, text_color=BG,
                      command=lambda: mac_input.open_privacy_settings("Accessibility")).pack(side="left")
        ctk.CTkButton(r, text="Мониторинг ввода", height=28, font=(FONT, 12), fg_color=CARD_2,
                      hover_color=BORDER, text_color=GOLD,
                      command=lambda: mac_input.open_privacy_settings("ListenEvent")).pack(side="left", padx=8)
        self.perm_banner = b
''')
rep('''                elif kind == "capsev":''', '''                elif kind == "perm":
                    self._show_perm_banner(item[1])
                elif kind == "capsev":''')

method('_hk_label', """    MAC_HK = {"tr": "mac_hotkey", "spell": "mac_spell_hotkey"}
    MAC_HK_DEFAULT = {"tr": {"key": 17, "mods": ["ctrl", "alt"]}, "spell": {"key": 15, "mods": ["ctrl", "alt"]}}

    def _hk_label(self, slot):
        hk = self.settings.get(self.MAC_HK[slot])
        return hotkey_label(hk if isinstance(hk, dict) else self.MAC_HK_DEFAULT[slot])
""")
method('on_rebind', """    def on_rebind(self, slot="tr"):
        if self.binding:
            return
        self.binding = True
        btn = self.key_btn if slot == "tr" else self.spell_btn
        btn.configure(text="Жми…")
        self.set_status("Нажми новое сочетание (лучше с ⌃ или ⌥), Esc — отмена", BLUE)

        def done(hk):
            if hk:
                other = "spell" if slot == "tr" else "tr"
                if hk == self.caps_engine.hotkeys.get(other):
                    self.set_status("Это сочетание уже занято другой функцией", ORANGE)
                else:
                    self.caps_engine.hotkeys[slot] = hk
                    self.settings[self.MAC_HK[slot]] = hk
                    save_settings(self.settings)
            self.binding = False
            self.ui("key", self._hk_label("tr"))
            self.ui("spellkey", self._hk_label("spell"))
            self.set_status(f"Перевод — {self._hk_label('tr')}, ошибки — {self._hk_label('spell')}", GREEN)
        self.caps_engine.capture_cb = done
""")
method('_front_app_name', """    def _front_app_name(self):
        return mac_input.frontmost_app()[0]
""")
method('_setup_hotkey', """    def _setup_hotkey(self):
        eng = getattr(self, "caps_engine", None)
        if eng is None:
            return
        for slot in ("tr", "spell"):
            on = self.settings.get("tr_enabled" if slot == "tr" else "spell_enabled", True)
            hk = self.settings.get(self.MAC_HK[slot])
            eng.hotkeys[slot] = (hk if isinstance(hk, dict) else self.MAC_HK_DEFAULT[slot]) if on else None
""")
method('_on_hotkey', """    def _on_hotkey(self, slot="tr"):
        pass
""")
method('_hotkey_work', """    def _hotkey_work(self, slot="tr"):
        if self.busy:
            return
        self.busy = True
        mi = mac_input
        try:
            punct = self._punct_here()
            mi.clip_quiet(4)
            mi.wait_mods_released()
            old_clip = self._clip() or ""
            text, selection = None, False
            if slot == "spell":
                text = mi.copy_selection(0.5)          # сначала — выделенный текст
                selection = bool(text and text.strip())
            if not selection:
                mi.cmd(mi.VK_A)
                time.sleep(0.05)
                text = mi.copy_selection()
            if not (text or "").strip():
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
                        src_text = text
                self.set_status("Перевожу…", BLUE)
                result, how = self.engine.translate(src_text, ("ru", "en"))
            if selection and text != text.strip():
                lead = text[:len(text) - len(text.lstrip())]
                trail = text[len(text.rstrip()):]
                result = lead + result + trail
            mi.clip_quiet(4)
            self._copy(result)   # результат остаётся в буфере обмена
            if not selection:
                mi.cmd(mi.VK_A)
                time.sleep(0.03)
            mi.cmd(mi.VK_V)
            time.sleep(0.05)
            if self.settings.get("auto_send") and slot != "spell":
                mi.key(mi.VK_RETURN)
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
""")
rep('''            import engine as _caps
            if time.time() < _caps.CLIP_QUIET_UNTIL:''', '''            if time.time() < mac_input.CLIP_QUIET_UNTIL:''')
# закрытие
rep('''        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            if self.kb:
                self.kb.unhook_all()
        except Exception:
            pass
        self.destroy()''', '''        self.destroy()''')
rep('''    ctypes.windll.kernel32.CreateMutexW(None, False, "Capslocker_single_instance_mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:
        messagebox.showinfo(APP_NAME, "Капслокер уже запущен — ищи значок в трее рядом с часами.")
        return''', '''    import fcntl
    global _LOCK
    _LOCK = open(os.path.join(core.DATA_DIR, ".lock"), "w")
    try:
        fcntl.flock(_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        messagebox.showinfo(APP_NAME, "Капслокер уже запущен — нажми на его значок в Dock.")
        return''')
# подсказки, где говорится про F9 и трей
s = s.replace('"Готово. Клавиша F9 работает"', '"Готово"')
m = re.search(r'    def _copy\(self, text\):\n.*?\n    def _clip\(self\):\n.*?\n\n', s, re.S)
s = s[:m.start()] + """    def _copy(self, text):
        self.own_clip.add(text)
        if len(self.own_clip) > 60:
            self.own_clip = {text}
        mac_input.set_clipboard(text)

    def _clip(self):
        return mac_input.get_clipboard()

""" + s[m.end():]
s = s.replace('filetypes=[("Офлайн-модель", "*.argosmodel *.zip *.gguf"), ("Все файлы", "*.*")])',
              'filetypes=[("Офлайн-модель", "*.argosmodel *.zip"), ("Все файлы", "*.*")])')
open('mac/app_mac.py', 'w', encoding='utf-8').write(s)
print("ok", len(s))
