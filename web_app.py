"""Local browser UI bridge; peer traffic still uses the shared raw TCP/UDP transport."""
import argparse
import base64
import binascii
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import queue
import re
import threading
import time
from urllib.parse import urlparse, parse_qs, quote
import uuid

from main import Application

BASE = Path(__file__).resolve().parent
MAX_FILE = 16 * 1024 * 1024


def image_type(path):
    """Only raster images may be displayed inline, never remote HTML/SVG."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(16)
    except OSError:
        return None
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'image/webp'
    return None


class WebBridge:
    def __init__(self, app, storage=None):
        self.app = app
        self.lock = threading.RLock()
        self.messages = {}
        self.transfers = {}
        self.token = uuid.uuid4().hex
        self.storage = Path(storage or BASE / ".web-data" / app.chat.peer_id)
        self.storage.mkdir(parents=True, exist_ok=True)

    def snapshot(self):
        with self.lock:
            while True:
                try:
                    event = self.app.ui_events.get_nowait()
                except queue.Empty:
                    break
                data = event.data
                if event.type == "CHAT_RECEIVED":
                    self.add_message(event.peer_id, data["text"], "peer")
                elif event.type.startswith("FILE_") and data.get("transfer_id"):
                    item = self.transfers.setdefault(data["transfer_id"], {"created_at": time.time()})
                    item.update(data, peer_id=event.peer_id)
                    if item.get("status") == "completed":
                        item["previewable"] = bool(image_type(item.get("path", "")))
                self.app.ui_events.task_done()
            return json.loads(json.dumps({
                "me": {"name": self.app.chat.name, "peer_id": self.app.chat.peer_id,
                       "ip": self.app.discovery.local_ip, "port": self.app.chat.port},
                "peers": [asdict(p) for p in self.app.discovery.get_online_peers()],
                "connections": self.app.chat.get_connections(),
                "messages": self.messages, "transfers": list(self.transfers.values()),
                "token": self.token,
            }))

    def add_message(self, peer_id, text, direction):
        with self.lock:
            self.messages.setdefault(peer_id, []).append({
                "id": uuid.uuid4().hex, "text": text, "from": direction, "time": time.time()})

    def action(self, path, data):
        if path == "/api/send":
            peer, text = data.get("peer_id"), data.get("text")
            if not isinstance(peer, str) or not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise ValueError("Tin nhắn không hợp lệ")
            if not self.app.transport.send_message(peer, text):
                raise ValueError("Không gửi được: peer mất kết nối")
            self.add_message(peer, text, "me")
            return {"ok": True}
        if path == "/api/refresh":
            self.app.discovery.broadcast_presence()
            return {"ok": True}
        if path == "/api/cancel":
            tid = data.get("transfer_id")
            if not isinstance(tid, str) or not self.app.files.cancel_transfer(tid):
                raise ValueError("Phiên truyền đã kết thúc")
            return {"ok": True}
        if path == "/api/upload":
            peer = data.get("peer_id")
            if not isinstance(peer, str) or not self.app.discovery.get_peer(peer):
                raise ValueError("Peer không online")
            name = Path(str(data.get("filename", "file"))).name
            if name in {"", ".", ".."} or any(c in name for c in ':<>|?*'):
                raise ValueError("Tên file không hợp lệ")
            content = base64.b64decode(data.get("content", ""), validate=True)
            if len(content) > MAX_FILE:
                raise ValueError("Bản web hỗ trợ file tối đa 16 MiB")
            folder = self.storage / uuid.uuid4().hex
            folder.mkdir()
            source = folder / name
            source.write_bytes(content)
            transfer_id = self.app.files.send_file(peer, str(source))
            return {"transfer_id": transfer_id}
        if path in {"/api/accept", "/api/reject"}:
            transfer_id = data.get("transfer_id")
            if not isinstance(transfer_id, str):
                raise ValueError("Transfer ID không hợp lệ")
            record = self.app.files.get_transfer(transfer_id)
            if not record or record["direction"] != "incoming" or record["status"] != "pending":
                raise ValueError("Lời mời đã hết hạn")
            if path == "/api/reject":
                ok = self.app.files.reject_file(transfer_id)
            else:
                # Destination is generated locally, never from a remote path/transfer ID.
                folder = self.storage / uuid.uuid4().hex
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', record["filename"]).rstrip(' .') or 'file'
                destination = folder / name
                ok = self.app.files.accept_file(transfer_id, str(destination))
            if not ok:
                raise ValueError("Không xử lý được lời mời")
            return {"ok": True}
        raise ValueError("API không tồn tại")


def create_server(bridge, host="127.0.0.1", port=8000):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body, content_type="application/json"):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                return self.reply(200, bridge.snapshot())
            if parsed.path in {"/api/download", "/api/preview"}:
                tid = parse_qs(parsed.query).get("id", [""])[0]
                record = bridge.app.files.get_transfer(tid)
                if record and record["status"] == "completed":
                    preview = parsed.path == "/api/preview"
                    content_type = image_type(record["path"]) if preview else "application/octet-stream"
                    if not content_type:
                        return self.reply(415, {"error": "File này không hỗ trợ xem ảnh"})
                    try:
                        handle = open(record["path"], "rb")
                    except OSError:
                        return self.reply(404, {"error": "File không còn trên máy"})
                    with handle:
                        self.send_response(200)
                        self.send_header("Content-Type", content_type)
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Content-Length", str(record["size"]))
                        disposition = "inline" if preview else "attachment"
                        self.send_header("Content-Disposition", disposition + "; filename*=UTF-8''" + quote(record["filename"], safe=""))
                        self.end_headers()
                        try:
                            while chunk := handle.read(64 * 1024):
                                self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    return
                return self.reply(404, {"error": "File chưa hoàn tất"})
            path = parsed.path
            if path == "/":
                file = BASE / "index.html"
            elif path in {"/app.js", "/styles.css"}:
                file = BASE / path[1:]
            elif path.startswith("/assets/") and Path(path).name == path.removeprefix("/assets/"):
                file = BASE / "assets" / Path(path).name
            else:
                return self.reply(404, {"error": "Not found"})
            if not file.is_file():
                return self.reply(404, {"error": "Not found"})
            self.reply(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")

        def do_POST(self):
            if self.headers.get("X-Chat-Token") != bridge.token:
                return self.reply(403, {"error": "Mở lại trang để kết nối"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_FILE * 4 // 3 + 8192:
                    return self.reply(413, {"error": "File tối đa 16 MiB"})
                self.connection.settimeout(30)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Invalid request")
                self.reply(200, bridge.action(self.path, data))
            except (ValueError, TypeError, OSError, binascii.Error) as exc:
                self.reply(400, {"error": str(exc)})

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="P2P Chat browser UI")
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=int, default=5001, help="TCP peer port")
    parser.add_argument("--web-port", type=int, default=8000)
    parser.add_argument("--udp-port", type=int, default=9999)
    args = parser.parse_args()
    app = Application(args.name, args.port, udp_port=args.udp_port)
    server = None
    try:
        server = create_server(WebBridge(app), port=args.web_port)
        app.start()
        print(f"LAN Chat: http://127.0.0.1:{server.server_port} — TCP {app.chat.port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"Không mở được server: {exc}. Kiểm tra --port và --web-port.")
    finally:
        if server:
            server.server_close()
        app.stop()


if __name__ == "__main__":
    main()
