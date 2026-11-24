import socket
import struct
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from chat import Chat
from models import Peer
from protocol import MAX_PAYLOAD, ProtocolError, encode_packet, make_packet, recv_packet


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out")


def event(chat, kind):
    deadline = time.monotonic() + 5
    while True:
        item = chat.events.get(timeout=max(0.001, deadline - time.monotonic()))
        if item.type == kind:
            return item


class FramingTests(unittest.TestCase):
    def test_fragmented_and_adjacent_large_unicode_frames(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.settimeout(5)
        packets = [make_packet("CHAT", "a", {"text": "Xin chào 🌍" * 10000}),
                   make_packet("CHAT", "a", {"text": "next"})]
        raw = b"".join(map(encode_packet, packets))
        def send():
            for offset in range(0, len(raw), 7):
                a.sendall(raw[offset:offset + 7])
        thread = threading.Thread(target=send)
        thread.start()
        self.assertEqual(recv_packet(b), packets[0])
        self.assertEqual(recv_packet(b), packets[1])
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_invalid_lengths_json_type_and_truncated_body(self):
        bad = [struct.pack("!I", 0), struct.pack("!I", MAX_PAYLOAD + 1),
               struct.pack("!I", 1) + b"{",
               struct.pack("!I", 2) + b"[]"]
        for raw in bad:
            with self.subTest(raw=raw):
                a, b = socket.socketpair()
                try:
                    a.sendall(raw)
                    a.shutdown(socket.SHUT_WR)
                    with self.assertRaises(ProtocolError):
                        recv_packet(b)
                finally:
                    a.close()
                    b.close()
        a, b = socket.socketpair()
        try:
            a.sendall(struct.pack("!I", 10) + b"abc")
            a.close()
            with self.assertRaises(EOFError):
                recv_packet(b)
        finally:
            a.close()
            b.close()


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.chats = [Chat(name, name, host="127.0.0.1", timeout=1) for name in "abc"]
        for chat in self.chats:
            self.addCleanup(chat.stop_server)
            chat.start_server(0)
        self.a, self.b, self.c = self.chats

    def peer(self, chat):
        return Peer(chat.peer_id, chat.name, "127.0.0.1", chat.port, time.time())

    def test_multiple_peers_concurrent_writes_and_file_dispatch(self):
        for remote in (self.b, self.c):
            self.assertTrue(self.a.connect(self.peer(remote)))
        wait_until(lambda: self.b.get_connections() == ["a"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: self.a.send_message("b", str(n)), range(100)))
        self.assertTrue(all(results))
        received = {event(self.b, "CHAT_RECEIVED").data["text"] for _ in range(100)}
        self.assertEqual(received, set(map(str, range(100))))
        self.assertTrue(self.c.send_message("a", "Xin chào"))
        self.assertEqual(event(self.a, "CHAT_RECEIVED").data["text"], "Xin chào")
        packet = make_packet("FILE_CHUNK", "a", {"transfer_id": "t", "seq": 0, "data": "aGk="})
        self.assertTrue(self.a.send_packet("b", packet))
        self.assertEqual(event(self.b, "PACKET_RECEIVED").data["packet"], packet)

    def test_simultaneous_connect_and_disconnect(self):
        barrier = threading.Barrier(2)
        def connect(pair):
            local, remote = pair
            barrier.wait()
            return local.connect(self.peer(remote))
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertTrue(all(pool.map(connect, [(self.a, self.b), (self.b, self.a)])))
        wait_until(lambda: self.a.get_connections() == ["b"] and self.b.get_connections() == ["a"])
        self.assertTrue(self.a.send_message("b", "hello"))
        self.assertEqual(event(self.b, "CHAT_RECEIVED").data["text"], "hello")
        self.b.stop_server()
        event(self.a, "PEER_DISCONNECTED")
        self.assertEqual(self.a.get_connections(), [])
        self.assertFalse(self.a.send_message("b", "offline"))

    def test_bad_peer_does_not_break_server(self):
        with socket.create_connection(("127.0.0.1", self.a.port), timeout=2) as raw:
            recv_packet(raw)
            raw.sendall(struct.pack("!I", 0))
        event(self.a, "CONNECTION_ERROR")
        self.assertTrue(self.b.connect(self.peer(self.a)))
        self.assertTrue(self.b.send_message("a", "still alive"))
        self.assertEqual(event(self.a, "CHAT_RECEIVED").data["text"], "still alive")

    def test_stop_closes_pending_handshake_and_threads(self):
        with socket.create_connection(("127.0.0.1", self.a.port), timeout=2) as raw:
            recv_packet(raw)
            self.a.stop_server()
            self.assertEqual(raw.recv(1), b"")
        self.assertFalse(self.a._threads)
        self.assertFalse(self.a._sockets)


if __name__ == "__main__":
    unittest.main()
