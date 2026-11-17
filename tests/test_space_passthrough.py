import ctypes
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import patch


if sys.platform == "win32":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "windows"))
    import engine


@unittest.skipUnless(sys.platform == "win32", "Windows keyboard hook")
class SpaceHookTests(unittest.TestCase):
    def setUp(self):
        # No hooks, worker threads, clipboard access or real key injection.
        self.e = engine.Engine.__new__(engine.Engine)
        # Old saved settings must not re-enable automatic correction.
        self.e.settings = {"enabled": True, "auto": True, "auto_only": ""}
        self.e.q = queue.Queue()
        self.e.buf = []
        self.e.buf_hwnd = None
        self.e.caps_on = False
        self.e.last_fix = None
        for target, replacement in (("foreground", lambda: 123), ("_down", lambda vk: False)):
            p = patch.object(engine, target, replacement)
            p.start()
            self.addCleanup(p.stop)
        for name in ("_send", "type_text", "tap", "switch_layout", "process_name"):
            p = patch.object(engine, name)
            mock = p.start()
            self.addCleanup(p.stop)
            self.addCleanup(mock.assert_not_called)
        p = patch.object(engine.user32, "CallNextHookEx", return_value=42)
        self.next_hook = p.start()
        self.addCleanup(p.stop)

    def event(self, vk, scan, down):
        kb = engine.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan)
        event = engine.WM_KEYDOWN if down else engine.WM_KEYUP
        return self.e._hook_proc(0, event, ctypes.addressof(kb))

    def space(self, down):
        return self.event(engine.VK_SPACE, engine.SPACE_SCAN, down)

    def test_double_space_passes_through(self):
        self.assertEqual([self.space(down) for down in (True, False, True, False)], [42] * 4)
        self.assertEqual(self.next_hook.call_count, 4)
        self.assertEqual([self.e.q.get_nowait()[0] for _ in range(2)], ["key", "key"])
        self.assertTrue(self.e.q.empty())

    def test_held_space_and_autorepeat_pass_through(self):
        self.assertEqual([self.space(down) for down in (True, True, True, False)], [42] * 4)

    def test_ctrl_space_passes_through(self):
        with patch.object(engine, "_down", lambda vk: vk == engine.VK_CONTROL):
            self.assertEqual([self.space(down) for down in (True, False, True, False)], [42] * 4)

    def test_letters_and_spaces_pass_through_without_checking_the_process(self):
        for vk, scan in ((0x54, 0x14), (0x4F, 0x18), (0x54, 0x14), (engine.VK_SPACE, engine.SPACE_SCAN)):
            self.assertEqual(self.event(vk, scan, True), 42)
            self.assertEqual(self.event(vk, scan, False), 42)

    def test_manual_phrase_buffer_keeps_space_boundaries_without_sending_keys(self):
        for _ in range(2):
            self.space(True)
            ev = self.e.q.get_nowait()
            self.e._on_key(*ev[1:])
        self.assertEqual(engine.render(self.e.buf, "en"), "  ")

    def test_capslock_still_dispatches_manual_action(self):
        self.assertEqual(self.event(engine.VK_CAPITAL, 0x3A, True), 1)
        self.assertEqual(self.e.q.get_nowait(), ("caps", True, False))


if __name__ == "__main__":
    unittest.main()
