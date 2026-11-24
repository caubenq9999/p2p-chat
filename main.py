"""Application wiring: discovery -> TCP -> file dispatcher -> UI queue."""
import argparse
import logging
import queue
import threading
import uuid

from chat import Chat
from discovery import UDPPeerDiscovery
from file_transfer import FileTransferManager
from models import Event


class ConnectedTransport:
    """Connect on demand, including when UI auto-selects the first online peer."""
    def __init__(self, chat, discovery):
        self.chat, self.discovery = chat, discovery
        self._connect_lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.chat, name)

    def _ensure(self, peer_id):
        with self._connect_lock:
            if peer_id in self.chat.get_connections():
                return True
            peer = self.discovery.get_peer(peer_id)
            return peer is not None and self.chat.connect(peer)

    def send_message(self, peer_id, text):
        return self._ensure(peer_id) and self.chat.send_message(peer_id, text)

    def send_packet(self, peer_id, packet):
        # Only a new offer may establish a connection. Never resume a broken file
        # session by silently opening a fresh socket for a chunk/result.
        if packet.get("type") == "FILE_OFFER" and not self._ensure(peer_id):
            return False
        return self.chat.send_packet(peer_id, packet)


class Application:
    def __init__(self, name, port=5001, *, peer_id=None, udp_port=9999):
        self.events = queue.Queue()
        self.ui_events = queue.Queue()
        self.chat = Chat(peer_id or uuid.uuid4().hex, name, self.events)
        self.discovery = UDPPeerDiscovery(name, port, udp_port, peer_id=self.chat.peer_id,
                                         event_queue=self.events)
        self.transport = ConnectedTransport(self.chat, self.discovery)
        self.files = FileTransferManager(self.transport, self.events, local_peer_id=self.chat.peer_id)
        self.port = port
        self._router = None
        self._stopped = False

    def start(self):
        self.chat.start_server(self.port)
        self.discovery.tcp_port = self.chat.port
        self._router = threading.Thread(target=self._route, daemon=True, name="event-router")
        self._router.start()
        try:
            self.discovery.start_discovery()
        except Exception:
            self.stop()
            raise

    def _route(self):
        while True:
            event = self.events.get()
            try:
                if event is None:
                    return
                if event.type == "PACKET_RECEIVED":
                    self.files.handle_packet(event.data["packet"])
                else:
                    if event.type == "PEER_DISCONNECTED":
                        self.files.handle_disconnect(event.peer_id)
                    if event.type == "CONNECTION_ERROR":
                        logging.warning("TCP %s: %s", event.peer_id, event.data.get("reason"))
                    self.ui_events.put(event)
            except Exception as exc:
                logging.exception("Event dispatch failed")
                self.ui_events.put(Event("CONNECTION_ERROR", event.peer_id, {"reason": str(exc)}))
            finally:
                self.events.task_done()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.discovery.stop_discovery()
        self.chat.stop_server()
        self.files.shutdown()
        if self._router:
            self.events.put(None)
            self._router.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description="P2P LAN Chat")
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--udp-port", type=int, default=9999)
    args = parser.parse_args()
    from ui import LANChatUI
    app = Application(args.name, args.port, udp_port=args.udp_port)
    try:
        app.start()
        ui = LANChatUI(app.ui_events, chat=app.transport, discovery=app.discovery,
                       file_transfer=app.files, local_name=args.name,
                       local_ip=app.discovery.local_ip)
        ui.run()
    finally:
        app.stop()


if __name__ == "__main__":
    main()
