"""Shared TCP transport. UI consumes events; only this module reads sockets."""
import queue
import socket
import threading
from dataclasses import dataclass, field

from models import Event
from protocol import ProtocolError, encode_packet, make_packet, recv_packet


@dataclass(eq=False)
class _Connection:
    sock: socket.socket
    outgoing: bool
    write_lock: threading.Lock = field(default_factory=threading.Lock)


class Chat:
    def __init__(self, peer_id, name, event_queue=None, *, host="0.0.0.0", timeout=5):
        if not isinstance(peer_id, str) or not peer_id or not isinstance(name, str):
            raise ValueError("peer_id must be nonempty and name must be a string")
        self.peer_id, self.name = peer_id, name
        self.events = event_queue if event_queue is not None else queue.Queue()
        self.host, self.timeout = host, timeout
        self.port = None
        self._lock = threading.RLock()
        self._connections = {}
        self._sockets = set()
        self._threads = set()
        self._listener = None
        self._stopped = False

    def _emit(self, kind, peer_id, **data):
        self.events.put(Event(kind, peer_id, data))

    def _spawn(self, target, *args):
        def run():
            try:
                target(*args)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())
        with self._lock:
            if self._stopped:
                return
            thread = threading.Thread(target=run, daemon=True)
            self._threads.add(thread)
            thread.start()

    @staticmethod
    def _close(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def start_server(self, port):
        with self._lock:
            if self._stopped or self._listener is not None:
                raise RuntimeError("Chat stopped or server already started")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind((self.host, port))
                listener.listen()
                listener.settimeout(0.2)
            except OSError as exc:
                listener.close()
                self._emit("CONNECTION_ERROR", "", reason=str(exc))
                raise
            self._listener = listener
            self.port = listener.getsockname()[1]
            self._spawn(self._accept, listener)

    def _accept(self, listener):
        while not self._stopped:
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self._stopped:
                    self._close(sock)
                    break
                self._sockets.add(sock)
                self._spawn(self._establish, sock, False, None)

    def connect(self, peer):
        with self._lock:
            if self._stopped or peer.peer_id == self.peer_id:
                return False
            if peer.peer_id in self._connections:
                return True
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sockets.add(sock)
        try:
            sock.settimeout(self.timeout)
            sock.connect((peer.ip, peer.tcp_port))
        except OSError as exc:
            self._emit("CONNECTION_ERROR", peer.peer_id, reason=str(exc))
            self._release(sock)
            return False
        return self._establish(sock, True, peer.peer_id)

    def _release(self, sock):
        self._close(sock)
        with self._lock:
            self._sockets.discard(sock)

    def _establish(self, sock, outgoing, expected):
        remote_id = expected or ""
        try:
            sock.settimeout(self.timeout)
            sock.sendall(encode_packet(make_packet("HELLO", self.peer_id,
                         {"peer_id": self.peer_id, "name": self.name})))
            hello = recv_packet(sock)
            if hello["type"] != "HELLO":
                raise ProtocolError("First packet must be HELLO")
            remote_id = hello["sender_id"]
            if remote_id == self.peer_id or (expected and remote_id != expected):
                raise ProtocolError("Unexpected peer identity")
            sock.settimeout(None)
            connection = _Connection(sock, outgoing)
            with self._lock:
                if self._stopped:
                    self._release(sock)
                    return False
                old = self._connections.get(remote_id)
                # Both peers prefer the socket initiated by the smaller peer_id.
                preferred = outgoing == (self.peer_id < remote_id)
                if old and (not preferred or old.outgoing == outgoing):
                    self._release(sock)
                    return True
                self._connections[remote_id] = connection
                if old:
                    self._release(old.sock)
                else:
                    self._emit("PEER_CONNECTED", remote_id, name=hello["payload"]["name"])
                self._spawn(self._receive, remote_id, connection)
            return True
        except (OSError, EOFError, ProtocolError) as exc:
            self._emit("CONNECTION_ERROR", remote_id, reason=str(exc))
            self._release(sock)
            return False

    def _receive(self, peer_id, connection):
        try:
            while True:
                packet = recv_packet(connection.sock)
                if packet["sender_id"] != peer_id or packet["type"] == "HELLO":
                    raise ProtocolError("Invalid connection identity or repeated HELLO")
                if packet["type"] == "CHAT":
                    self._emit("CHAT_RECEIVED", peer_id, text=packet["payload"]["text"],
                               message_id=packet["id"])
                else:
                    self._emit("PACKET_RECEIVED", peer_id, packet=packet)
        except EOFError:
            pass
        except (OSError, ProtocolError) as exc:
            with self._lock:
                if self._connections.get(peer_id) is connection:
                    self._emit("CONNECTION_ERROR", peer_id, reason=str(exc))
        finally:
            self._drop(peer_id, connection)

    def _drop(self, peer_id, connection):
        with self._lock:
            if self._connections.get(peer_id) is connection:
                del self._connections[peer_id]
                self._emit("PEER_DISCONNECTED", peer_id)
            self._release(connection.sock)

    def send_message(self, peer_id, text):
        return self.send_packet(peer_id, make_packet("CHAT", self.peer_id, {"text": text}))

    def send_packet(self, peer_id, packet):
        try:
            frame = encode_packet(packet)
            if packet["sender_id"] != self.peer_id or packet["type"] == "HELLO":
                raise ProtocolError("Invalid sender or reserved HELLO packet")
        except ProtocolError as exc:
            self._emit("CONNECTION_ERROR", peer_id, reason=str(exc))
            return False
        with self._lock:
            connection = self._connections.get(peer_id)
        if connection is None:
            self._emit("CONNECTION_ERROR", peer_id, reason="Peer is not connected")
            return False
        try:
            with connection.write_lock:
                connection.sock.sendall(frame)
            return True
        except OSError as exc:
            self._emit("CONNECTION_ERROR", peer_id, reason=str(exc))
            self._drop(peer_id, connection)
            return False

    def disconnect(self, peer_id):
        with self._lock:
            connection = self._connections.get(peer_id)
            if connection:
                self._drop(peer_id, connection)

    def get_connections(self):
        with self._lock:
            return list(self._connections)

    def stop_server(self):
        """Terminal shutdown: close listener, connections and pending handshakes."""
        with self._lock:
            self._stopped = True
            if self._listener:
                self._close(self._listener)
                self._listener = None
            for peer_id, connection in list(self._connections.items()):
                self._drop(peer_id, connection)
            for sock in list(self._sockets):
                self._release(sock)
            threads = list(self._threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=self.timeout + 1)
