
from dataclasses import dataclass, asdict
import json
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple



DEFAULT_UDP_PORT = 9999
BROADCAST_INTERVAL = 3.0  
PEER_TIMEOUT = 10.0       
CLEANUP_INTERVAL = 1.0    
BUFFER_SIZE = 4096
VALID_STATUSES = ("online", "away")


@dataclass
class PeerInfo:
    """Cấu trúc thông tin của một Peer đang online trong mạng LAN."""
    name: str
    ip: str
    tcp_port: int
    status: str
    last_seen: float

    @property
    def peer_key(self) -> str:
        return f"{self.ip}:{self.tcp_port}"

    def to_dict(self) -> dict:
        """Chuyển đổi sang dict phục vụ tích hợp với module TCP Chat và UI."""
        return asdict(self)


def get_local_lan_ip() -> str:
    """
    Lấy địa chỉ IP LAN thực tế của máy hiện tại (ví dụ: 192.168.1.29).
    Sử dụng UDP socket kết nối giả lập tới 8.8.8.8 (không thực sự gửi gói tin ra Internet).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class UDPPeerDiscovery:
    """
    Quản lý tiến trình khám phá ngang hàng (Peer Discovery) và trạng thái (Online Status) qua UDP.
    Chạy ngầm các daemon threads theo đúng Thread Model ở Mục 5.1:
    - UDP broadcaster: mỗi 3 giây gửi gói HELLO|<name>|<tcp_port>|<status>
    - UDP listener   : nhận gói tin UDP trên cổng 9999 và cập nhật online-users list
    - Timeout checker: loại bỏ peer không phản hồi sau 10 giây
    """

    def __init__(
        self,
        name: str,
        tcp_port: int,
        udp_port: int = DEFAULT_UDP_PORT,
        status: str = "online",
        on_peer_online: Optional[Callable[[PeerInfo], None]] = None,
        on_peer_offline: Optional[Callable[[PeerInfo], None]] = None,
        on_peer_status_change: Optional[Callable[[PeerInfo, str], None]] = None,
    ):
        self.name = name.strip()
        self.tcp_port = int(tcp_port)
        self.udp_port = int(udp_port)
        self.status = status.strip().lower() if status else "online"
        self.local_ip = get_local_lan_ip()

        self._peers: Dict[str, PeerInfo] = {}
        self._lock = threading.RLock()
        self._running = threading.Event()

        self.on_peer_online = on_peer_online or self._default_on_online
        self.on_peer_offline = on_peer_offline or self._default_on_offline
        self.on_peer_status_change = on_peer_status_change or self._default_on_status_change

        self._send_sock: Optional[socket.socket] = None
        self._recv_sock: Optional[socket.socket] = None
        self._broadcast_thread: Optional[threading.Thread] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._cleanup_thread: Optional[threading.Thread] = None

    
    @staticmethod
    def _default_on_online(peer: PeerInfo) -> None:
        print(f"\r\033[K[*] {peer.name} ({peer.ip}:{peer.tcp_port}) is now {peer.status}\n> ", end="", flush=True)

    @staticmethod
    def _default_on_offline(peer: PeerInfo) -> None:
        print(f"\r\033[K[*] {peer.name} ({peer.ip}:{peer.tcp_port}) went offline\n> ", end="", flush=True)

    @staticmethod
    def _default_on_status_change(peer: PeerInfo, old_status: str) -> None:
        print(
            f"\r\033[K[*] {peer.name} ({peer.ip}:{peer.tcp_port}) changed status: "
            f"{old_status} -> {peer.status}\n> ",
            end="",
            flush=True,
        )

   
    def _setup_sockets(self) -> None:
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self._recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass

        self._recv_sock.bind(("", self.udp_port))
        self._recv_sock.settimeout(1.0)

   
    def _build_presence_packet(self, msg_type: str = "HELLO") -> bytes:
        """
        Tạo gói tin UDP theo đúng Mục 5.2 của đề bài:
        HELLO|<name>|<tcp_port>|<status>
        Ví dụ: HELLO|Nguyen|5001|online
        """
        if msg_type == "BYE":
            text = f"BYE|{self.name}|{self.tcp_port}"
        else:
            text = f"HELLO|{self.name}|{self.tcp_port}|{self.status}"
        return text.encode("utf-8")

    @staticmethod
    def _parse_packet(raw_bytes: bytes) -> Optional[dict]:
        """
        Giải mã gói tin UDP nhận được.
        Hỗ trợ cả định dạng chuẩn Mục 5.2 (HELLO|name|tcp_port|status) và JSON.
        """
        try:
            text = raw_bytes.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None

        if not text:
            return None

        if "|" in text and not text.startswith("{"):
            parts = text.split("|")
            cmd = parts[0].strip().upper()
            if cmd in ("HELLO", "PRESENCE") and len(parts) >= 4:
                try:
                    return {
                        "type": "HELLO",
                        "name": parts[1].strip(),
                        "tcp_port": int(parts[2].strip()),
                        "status": parts[3].strip().lower(),
                    }
                except ValueError:
                    return None
            elif cmd in ("BYE", "OFFLINE") and len(parts) >= 3:
                try:
                    return {
                        "type": "BYE",
                        "name": parts[1].strip(),
                        "tcp_port": int(parts[2].strip()),
                        "status": "offline",
                    }
                except ValueError:
                    return None
            return None

        try:
            data = json.loads(text)
            if isinstance(data, dict) and "name" in data and "tcp_port" in data:
                raw_type = str(data.get("type", "HELLO")).upper()
                return {
                    "type": "BYE" if raw_type in ("BYE", "OFFLINE") else "HELLO",
                    "name": str(data["name"]).strip(),
                    "tcp_port": int(data["tcp_port"]),
                    "status": str(data.get("status", "online")).strip().lower(),
                    "ip": data.get("ip"),
                }
        except (ValueError, json.JSONDecodeError):
            pass

        return None

    def send_presence_once(self, msg_type: str = "HELLO") -> None:
        """
        Phát gói tin UDP Broadcast.
        Gửi tới cả <broadcast> (cho các máy khác trong LAN) và 127.255.255.255 (cho trường hợp
        chạy nhiều cửa sổ trên cùng 1 PC theo Mục 9b của đề bài).
        """
        if not self._send_sock:
            return
        packet = self._build_presence_packet(msg_type=msg_type)
        sent_addresses = set()
        for target_addr in ("<broadcast>", "127.255.255.255"):
            if target_addr in sent_addresses:
                continue
            try:
                self._send_sock.sendto(packet, (target_addr, self.udp_port))
                sent_addresses.add(target_addr)
            except OSError:
                pass

   
    def _broadcast_loop(self) -> None:
        """Thread 1 (UDP broadcaster): Gửi gói presence mỗi 3 giây."""
        while self._running.is_set():
            self.send_presence_once(msg_type="HELLO")
            for _ in range(int(BROADCAST_INTERVAL * 10)):
                if not self._running.is_set():
                    break
                time.sleep(0.1)

    def _is_self_packet(self, parsed: dict, sender_ip: str) -> bool:
        """
        Kiểm tra gói tin có phải do chính tiến trình này gửi ra hay không.
        Nếu cùng máy (sender_ip là IP của máy mình hoặc 127.0.0.1) VÀ trùng tcp_port -> Bỏ qua.
        Nhờ so sánh cả tcp_port nên bạn mở 2-3 cửa sổ khác port (5001, 5002, 5003) trên 1 máy vẫn thấy nhau!
        """
        local_ips = {self.local_ip, "127.0.0.1", "0.0.0.0"}
        if sender_ip in local_ips and parsed.get("tcp_port") == self.tcp_port:
            return True
        return False

    def _listen_loop(self) -> None:
        """Thread 2 (UDP listener): Nhận gói tin UDP và cập nhật danh sách online."""
        while self._running.is_set():
            if not self._recv_sock:
                break
            try:
                raw_bytes, (sender_ip, _) = self._recv_sock.recvfrom(BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break

            parsed = self._parse_packet(raw_bytes)
            if not parsed:
                continue

   
            if self._is_self_packet(parsed, sender_ip):
                continue

            msg_type = parsed["type"]
            name = parsed["name"]
            tcp_port = parsed["tcp_port"]
            status = parsed["status"]

            if not (1 <= tcp_port <= 65535):
                continue

         
            peer_ip = self.local_ip if sender_ip in ("127.0.0.1", "0.0.0.0") else sender_ip
            peer_key = f"{peer_ip}:{tcp_port}"
            now = time.time()

            if msg_type == "BYE":
                removed_peer: Optional[PeerInfo] = None
                with self._lock:
                    if peer_key in self._peers:
                        removed_peer = self._peers.pop(peer_key)
                if removed_peer:
                    self.on_peer_offline(removed_peer)
                continue

            new_peer_event: Optional[PeerInfo] = None
            status_changed_event: Optional[Tuple[PeerInfo, str]] = None

            with self._lock:
                existing = self._peers.get(peer_key)
                if existing is None:
                    peer_obj = PeerInfo(
                        name=name,
                        ip=peer_ip,
                        tcp_port=tcp_port,
                        status=status,
                        last_seen=now,
                    )
                    self._peers[peer_key] = peer_obj
                    new_peer_event = peer_obj
                else:
                    old_status = existing.status
                    existing.name = name
                    existing.status = status
                    existing.last_seen = now
                    if old_status != status:
                        status_changed_event = (existing, old_status)

            if new_peer_event:
                self.on_peer_online(new_peer_event)
            elif status_changed_event:
                self.on_peer_status_change(*status_changed_event)

    def _cleanup_loop(self) -> None:
        """Thread 3 (Timeout checker): Xóa peer không gửi gói tin sau 10 giây."""
        while self._running.is_set():
            now = time.time()
            expired_peers: List[PeerInfo] = []

            with self._lock:
                keys_to_remove = [
                    key
                    for key, peer in self._peers.items()
                    if (now - peer.last_seen) > PEER_TIMEOUT
                ]
                for key in keys_to_remove:
                    expired_peers.append(self._peers.pop(key))

            for peer in expired_peers:
                self.on_peer_offline(peer)

            time.sleep(CLEANUP_INTERVAL)

   
    def start(self) -> None:
        """Khởi động dịch vụ UDP Peer Discovery."""
        if self._running.is_set():
            return
        self._setup_sockets()
        self._running.set()

        self._listen_thread = threading.Thread(
            target=self._listen_loop, name="UDP-Listener", daemon=True
        )
        self._broadcast_thread = threading.Thread(
            target=self._broadcast_loop, name="UDP-Broadcaster", daemon=True
        )
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, name="UDP-TimeoutChecker", daemon=True
        )

        self._listen_thread.start()
        self._broadcast_thread.start()
        self._cleanup_thread.start()

    def stop(self) -> None:
        """Dừng dịch vụ UDP Peer Discovery và gửi gói BYE."""
        if not self._running.is_set():
            return
        self._running.clear()
        self.send_presence_once(msg_type="BYE")

        for sock in (self._send_sock, self._recv_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        self._send_sock = None
        self._recv_sock = None

    def set_status(self, new_status: str) -> bool:
        """
        Đổi trạng thái ('online' hoặc 'away' theo Mục 3.2 & 4.1) và phát UDP broadcast ngay.
        Trả về True nếu hợp lệ, False nếu trạng thái không nằm trong ('online', 'away').
        """
        cleaned = new_status.strip().lower()
        if cleaned not in VALID_STATUSES:
            return False
        self.status = cleaned
        if self._running.is_set():
            self.send_presence_once(msg_type="HELLO")
        return True

    def get_online_peers(self) -> List[Dict]:
        """Trả về danh sách các peer đang online (name, ip, tcp_port, status, last_seen)."""
        with self._lock:
            return [peer.to_dict() for peer in self._peers.values()]

    def get_peer_by_name(self, name: str) -> Optional[Dict]:
        """Tìm peer theo tên (không phân biệt hoa thường)."""
        target = name.strip().lower()
        with self._lock:
            for peer in self._peers.values():
                if peer.name.lower() == target:
                    return peer.to_dict()
        return None

    def resolve_peer(self, name_or_number: str) -> Optional[Dict]:
        """
        Hỗ trợ lệnh `/chat <name|number>` ở Mục 4.1:
        Cho phép lấy thông tin peer theo số thứ tự trong `/list` (1, 2, 3...) hoặc theo tên.
        """
        query = name_or_number.strip()
        peers = self.get_online_peers()
        if query.isdigit():
            idx = int(query) - 1
            if 0 <= idx < len(peers):
                return peers[idx]
        return self.get_peer_by_name(query)

    def format_online_list(self) -> str:
        """
        Định dạng danh sách online theo đúng mẫu lệnh `/list` ở Mục 4.2 của đề bài:
          1. Minh  (192.168.1.12:6000) online
          2. Lan   (192.168.1.15:6000) away
        """
        peers = self.get_online_peers()
        if not peers:
            return "[*] No other users are currently online."
        lines = []
        for idx, p in enumerate(peers, start=1):
            addr = f"({p['ip']}:{p['tcp_port']})"
            lines.append(f"  {idx}. {p['name']:<10} {addr:<22} {p['status']}")
        return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LAN Chat - UDP Peer Discovery Module (Nguyen)")
    parser.add_argument("--name", type=str, required=True, help="Tên hiển thị của bạn (VD: Nguyen)")
    parser.add_argument(
        "--port", "--tcp-port",
        dest="tcp_port",
        type=int,
        default=5000,
        help="Cổng TCP của tiến trình (VD: --port 5001)",
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=DEFAULT_UDP_PORT,
        help="Cổng UDP broadcast (mặc định: 9999)",
    )
    args = parser.parse_args()

    discovery = UDPPeerDiscovery(
        name=args.name,
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
    )
    discovery.start()

    print(f"[*] User: {discovery.name} ({discovery.local_ip}:{discovery.tcp_port})")
    print(f"[*] Broadcasting presence on UDP port {discovery.udp_port} (every {int(BROADCAST_INTERVAL)}s)")
    print("[*] Commands: /list, /status <online|away>, /help, /quit")

    try:
        while True:
            cmd = input("> ").strip()
            if not cmd:
                continue

            normalized = cmd if cmd.startswith("/") else f"/{cmd}"
            parts = normalized.split(maxsplit=1)
            action = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if action in ("/quit", "/exit"):
                break
            elif action == "/list":
                print(discovery.format_online_list())
            elif action == "/status":
                if discovery.set_status(arg):
                    print(f"[*] Status changed to: {discovery.status}")
                else:
                    print("[!] Usage: /status <online|away>")
            elif action == "/help":
                print("Available commands (UDP Discovery & Status):")
                print("  /list                  Show online users with number, name, IP and status")
                print("  /status <online|away>  Change your status (sent via UDP)")
                print("  /help                  Show all commands")
                print("  /quit                  Exit the program")
            else:
                print("[!] Unknown command. Type /help for available commands.")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        print("\n[*] Stopping UDP Discovery...")
        discovery.stop()
