import queue
import unittest
import tkinter as tk

from models import Event, Peer
from ui import LANChatUI


class UITests(unittest.TestCase):
    def test_event_render_preserves_composer_and_worker_result(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        events = queue.Queue()
        ui = LANChatUI(events, root=root)
        self.addCleanup(ui._on_close)
        ui.show_peers([Peer("b", "Bob", "127.0.0.1", 5002, 0)])
        ui.message_entry.insert(0, "đang nhập")
        events.put(Event("CHAT_RECEIVED", "b", {"text": "hello", "message_id": "1"}))
        ui._poll_events()
        self.assertEqual(ui.message_entry.get(), "đang nhập")
        self.assertEqual(ui.conversations["b"][-1]["text"], "hello")
        message = {"status": "sending"}
        events.put({"type": "UI_SEND_RESULT", "peer_id": "b",
                    "data": {"message": message, "ok": True}})
        ui._poll_events()
        self.assertEqual(message["status"], "sent")
