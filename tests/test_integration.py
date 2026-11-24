import hashlib
import queue
import socket
import tempfile
import time
import unittest
from pathlib import Path

from main import Application
from discovery import PEER_TIMEOUT


def await_condition(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Integration timeout")


def next_event(app, kind):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        event = app.ui_events.get(timeout=max(0.01, deadline - time.monotonic()))
        if event.type == kind:
            return event
    raise AssertionError(kind)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("", 0))
            udp_port = sock.getsockname()[1]
        self.apps = [Application(name, 0, peer_id=name, udp_port=udp_port) for name in ("a", "b", "c")]
        for app in self.apps:
            self.addCleanup(app.stop)
            app.start()
        self.a, self.b, self.c = self.apps
        # Actual UDP broadcast + listeners, not a mocked discovery list.
        for app in self.apps:
            app.discovery.broadcast_presence()
        await_condition(lambda: all(len(app.discovery.get_online_peers()) == 2 for app in self.apps))

    def test_discovery_chat_file_hash_and_disconnect(self):
        self.assertTrue(self.a.transport.send_message("b", "Xin chào 🌍"))
        self.assertEqual(next_event(self.b, "CHAT_RECEIVED").data["text"], "Xin chào 🌍")
        self.assertTrue(self.c.transport.send_message("a", "third peer"))
        self.assertEqual(next_event(self.a, "CHAT_RECEIVED").peer_id, "c")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.bin"
            destination = Path(folder) / "received.bin"
            source.write_bytes(bytes(range(256)) * 1024)
            transfer_id = self.a.files.send_file("b", str(source))
            request = next_event(self.b, "FILE_REQUEST")
            self.assertEqual(request.data["transfer_id"], transfer_id)
            self.assertTrue(self.b.files.accept_file(transfer_id, str(destination)))
            next_event(self.b, "FILE_COMPLETED")
            next_event(self.a, "FILE_COMPLETED")
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),
                             hashlib.sha256(destination.read_bytes()).digest())
            # Pending file must fail when TCP disappears, independently of UDP online.
            pending = self.a.files.send_file("b", str(source))
            next_event(self.b, "FILE_REQUEST")
            self.a.chat.disconnect("b")
            await_condition(lambda: self.b.files.get_transfer(pending)["status"] == "failed")
            self.assertIsNotNone(self.a.discovery.get_peer("b"))

    def test_refresh_update_timeout_and_self_filter(self):
        discovery = self.a.discovery
        self.assertIsNone(discovery.get_peer("a"))
        before = discovery.get_peer("b").last_seen
        self.b.discovery.name = "Bob updated"
        self.b.discovery.broadcast_presence()
        await_condition(lambda: discovery.get_peer("b").name == "Bob updated")
        self.assertGreater(discovery.get_peer("b").last_seen, before)
        with discovery._lock:
            discovery._peers["b"].last_seen = time.time() - PEER_TIMEOUT - 1
        discovery.check_timeouts()
        self.assertIsNone(discovery.get_peer("b"))


if __name__ == "__main__":
    unittest.main()
