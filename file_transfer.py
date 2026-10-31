"""Module truyền file của dự án LAN Chat.

Module này không tự sở hữu hay đọc/ghi socket. Nó sử dụng tầng TCP dùng chung
do ``chat.py`` cung cấp thông qua ``transport.send_packet(peer_id, packet)``
và đẩy các sự kiện an toàn cho UI vào ``queue.Queue``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional

try:
    # Dùng Event tạm thời cho đến khi models.py chung của nhóm được bổ sung.
    from models import Event  # type: ignore
except ImportError:
    @dataclass(frozen=True)
    class Event:  # type: ignore[no-redef]
        type: str
        peer_id: str
        data: dict


class TransferDirection(str, Enum):
    """Hướng truyền của một phiên gửi/nhận file."""

    OUTGOING = "outgoing"
    INCOMING = "incoming"


class TransferStatus(str, Enum):
    """Các trạng thái trong vòng đời của một phiên truyền file."""

    PREPARING = "preparing"
    OFFERED = "offered"
    PENDING = "pending"
    SENDING = "sending"
    RECEIVING = "receiving"
    WAITING_RESULT = "waiting_result"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class TransferRecord:
    """Thông tin và trạng thái đang được theo dõi của một phiên truyền file."""

    transfer_id: str
    peer_id: str
    filename: str
    size: int
    sha256: str
    direction: TransferDirection
    status: TransferStatus
    source_path: Optional[Path] = None
    save_path: Optional[Path] = None
    temp_path: Optional[Path] = None
    bytes_transferred: int = 0
    next_seq: int = 0
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    file_handle: Optional[BinaryIO] = field(default=None, repr=False)
    worker_started: bool = field(default=False, repr=False)
    last_reported_percent: float = field(default=-1.0, repr=False)

    @property
    def progress(self) -> float:
        if self.size == 0:
            return 100.0 if self.status == TransferStatus.COMPLETED else 0.0
        return max(0.0, min(100.0, self.bytes_transferred * 100.0 / self.size))

    def public_data(self) -> dict:
        return {
            "transfer_id": self.transfer_id,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "direction": self.direction.value,
            "status": self.status.value,
            "progress": round(self.progress, 2),
            "bytes_transferred": self.bytes_transferred,
            "path": str(self.save_path or self.source_path or ""),
            "reason": self.reason,
        }


class FileTransferManager:
    """Gửi và nhận file qua tầng TCP có framing dùng chung với chat."""

    PACKET_TYPES = {
        "FILE_OFFER",
        "FILE_ACCEPT",
        "FILE_REJECT",
        "FILE_CHUNK",
        "FILE_END",
        "FILE_RESULT",
    }

    def __init__(
        self,
        transport: Any,
        event_queue: "queue.Queue[Event]",
        *,
        local_peer_id: str = "",
        chunk_size: int = 64 * 1024,
        max_file_size: Optional[int] = None,
    ) -> None:
        if not 1024 <= chunk_size <= 512 * 1024:
            raise ValueError("chunk_size must be between 1 KiB and 512 KiB")
        self.transport = transport
        self.event_queue = event_queue
        self.local_peer_id = local_peer_id
        self.chunk_size = chunk_size
        self.max_file_size = max_file_size
        self._transfers: Dict[str, TransferRecord] = {}
        self._lock = threading.RLock()
        self._stopping = threading.Event()

    # ------------------------------------------------------------------
    # API công khai theo Contract_Sprint1.docx
    # ------------------------------------------------------------------
    def send_file(self, peer_id: str, path: str) -> str:
        """Chuẩn bị lời mời gửi file và trả về ``transfer_id`` ngay lập tức.

        SHA-256 được tính trong thread nền để việc chọn file lớn không làm treo
        giao diện.
        """
        source = Path(path).expanduser().resolve()
        if not peer_id:
            raise ValueError("peer_id is required")
        if not source.is_file():
            raise FileNotFoundError(source)
        size = source.stat().st_size
        if self.max_file_size is not None and size > self.max_file_size:
            raise ValueError("file exceeds the configured size limit")

        transfer_id = uuid.uuid4().hex
        record = TransferRecord(
            transfer_id=transfer_id,
            peer_id=peer_id,
            filename=source.name,
            size=size,
            sha256="",
            direction=TransferDirection.OUTGOING,
            status=TransferStatus.PREPARING,
            source_path=source,
        )
        with self._lock:
            self._transfers[transfer_id] = record
        self._emit("FILE_PROGRESS", record)
        threading.Thread(
            target=self._prepare_offer,
            args=(transfer_id,),
            name=f"file-offer-{transfer_id[:8]}",
            daemon=True,
        ).start()
        return transfer_id

    def accept_file(self, transfer_id: str, save_path: str) -> bool:
        """Chấp nhận file đến và ghi dữ liệu vào đường dẫn người dùng chọn."""

        destination = Path(save_path).expanduser().resolve()
        with self._lock:
            record = self._transfers.get(transfer_id)
            if (
                record is None
                or record.direction != TransferDirection.INCOMING
                or record.status != TransferStatus.PENDING
            ):
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._available_temp_path(destination, transfer_id)
            try:
                handle = open(temp_path, "xb")
            except OSError as exc:
                self._fail_locked(record, f"cannot create destination: {exc}")
                self._emit("FILE_FAILED", record)
                return False
            record.save_path = destination
            record.temp_path = temp_path
            record.file_handle = handle
            record.status = TransferStatus.RECEIVING

        if not self._send(record.peer_id, "FILE_ACCEPT", {"transfer_id": transfer_id}):
            self._fail(transfer_id, "could not send file acceptance")
            return False
        self._emit("FILE_PROGRESS", record)
        return True

    def reject_file(self, transfer_id: str) -> bool:
        """Từ chối một lời mời nhận file đang chờ xử lý."""

        with self._lock:
            record = self._transfers.get(transfer_id)
            if (
                record is None
                or record.direction != TransferDirection.INCOMING
                or record.status != TransferStatus.PENDING
            ):
                return False
            record.status = TransferStatus.REJECTED
            record.reason = "rejected by user"
        sent = self._send(record.peer_id, "FILE_REJECT", {"transfer_id": transfer_id})
        self._emit("FILE_FAILED", record)
        return sent

    def handle_packet(self, packet: dict) -> None:
        """Phân phối một packet ``FILE_*`` đã được ``chat.py`` giải mã."""
        if not isinstance(packet, dict):
            return
        packet_type = packet.get("type")
        if packet_type not in self.PACKET_TYPES:
            return
        payload = packet.get("payload")
        if not isinstance(payload, dict):
            self._emit_raw("FILE_FAILED", str(packet.get("sender_id", "")), {
                "reason": "malformed file packet payload"
            })
            return
        peer_id = str(packet.get("sender_id") or packet.get("peer_id") or "")
        handlers = {
            "FILE_OFFER": self._handle_offer,
            "FILE_ACCEPT": self._handle_accept,
            "FILE_REJECT": self._handle_reject,
            "FILE_CHUNK": self._handle_chunk,
            "FILE_END": self._handle_end,
            "FILE_RESULT": self._handle_result,
        }
        try:
            handlers[packet_type](peer_id, payload)
        except Exception as exc:  # Dữ liệu remote lỗi không được làm dừng receive loop.
            transfer_id = str(payload.get("transfer_id", ""))
            if transfer_id and transfer_id in self._transfers:
                self._fail(transfer_id, f"invalid {packet_type}: {exc}", notify_peer=True)
            else:
                self._emit_raw("FILE_FAILED", peer_id, {
                    "transfer_id": transfer_id,
                    "reason": f"invalid {packet_type}: {exc}",
                })

    def get_progress(self, transfer_id: str) -> float:
        """Trả về tiến độ hiện tại trong khoảng từ 0 đến 100 phần trăm."""

        with self._lock:
            record = self._transfers.get(transfer_id)
            return record.progress if record else 0.0

    def get_transfer(self, transfer_id: str) -> Optional[dict]:
        """Lấy bản sao dữ liệu công khai của một phiên truyền file."""

        with self._lock:
            record = self._transfers.get(transfer_id)
            return record.public_data() if record else None

    def shutdown(self) -> None:
        """Dừng worker và xóa các file tạm chưa truyền xong."""
        self._stopping.set()
        with self._lock:
            active_ids = [
                transfer_id
                for transfer_id, record in self._transfers.items()
                if record.status not in {
                    TransferStatus.COMPLETED,
                    TransferStatus.REJECTED,
                    TransferStatus.FAILED,
                }
            ]
        for transfer_id in active_ids:
            self._fail(transfer_id, "application stopped")

    # ------------------------------------------------------------------
    # Luồng gửi file
    # ------------------------------------------------------------------
    def _prepare_offer(self, transfer_id: str) -> None:
        with self._lock:
            record = self._transfers.get(transfer_id)
            if record is None or record.source_path is None:
                return
            source = record.source_path
        try:
            digest = self._sha256_file(source)
            current_size = source.stat().st_size
            if current_size != record.size:
                raise OSError("file size changed while preparing transfer")
        except OSError as exc:
            self._fail(transfer_id, f"cannot read source file: {exc}")
            return

        with self._lock:
            if self._stopping.is_set() or record.status != TransferStatus.PREPARING:
                return
            record.sha256 = digest
            record.status = TransferStatus.OFFERED
        payload = {
            "transfer_id": record.transfer_id,
            "filename": record.filename,
            "size": record.size,
            "sha256": record.sha256,
        }
        if not self._send(record.peer_id, "FILE_OFFER", payload):
            self._fail(transfer_id, "could not send file offer")
            return
        self._emit("FILE_PROGRESS", record)

    def _handle_accept(self, peer_id: str, payload: dict) -> None:
        transfer_id = self._required_id(payload)
        with self._lock:
            record = self._transfers.get(transfer_id)
            if record is None or record.direction != TransferDirection.OUTGOING:
                return
            self._check_peer(record, peer_id)
            if record.status != TransferStatus.OFFERED or record.worker_started:
                return
            record.status = TransferStatus.SENDING
            record.worker_started = True
        threading.Thread(
            target=self._send_worker,
            args=(transfer_id,),
            name=f"file-send-{transfer_id[:8]}",
            daemon=True,
        ).start()

    def _send_worker(self, transfer_id: str) -> None:
        with self._lock:
            record = self._transfers.get(transfer_id)
            if record is None or record.source_path is None:
                return
            source = record.source_path
        try:
            with open(source, "rb") as handle:
                seq = 0
                while not self._stopping.is_set():
                    chunk = handle.read(self.chunk_size)
                    if not chunk:
                        break
                    payload = {
                        "transfer_id": transfer_id,
                        "seq": seq,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    }
                    if not self._send(record.peer_id, "FILE_CHUNK", payload):
                        raise ConnectionError("connection lost while sending file")
                    with self._lock:
                        if record.status != TransferStatus.SENDING:
                            return
                        record.bytes_transferred += len(chunk)
                        record.next_seq = seq + 1
                    self._emit_progress(record)
                    seq += 1
                if self._stopping.is_set():
                    raise ConnectionError("application stopped")
            with self._lock:
                if record.status == TransferStatus.SENDING:
                    record.status = TransferStatus.WAITING_RESULT
            # Chuyển sang WAITING_RESULT trước khi gửi FILE_END. Với transport
            # cục bộ nhanh, bên nhận có thể kiểm tra và trả FILE_RESULT từ một
            # thread khác trước khi send_packet(FILE_END) kịp trả về.
            if not self._send(record.peer_id, "FILE_END", {"transfer_id": transfer_id}):
                raise ConnectionError("could not send end-of-file packet")
            self._emit_progress(record, force=True)
        except (OSError, ConnectionError) as exc:
            self._fail(transfer_id, str(exc))

    def _handle_reject(self, peer_id: str, payload: dict) -> None:
        transfer_id = self._required_id(payload)
        with self._lock:
            record = self._transfers.get(transfer_id)
            if record is None or record.direction != TransferDirection.OUTGOING:
                return
            self._check_peer(record, peer_id)
            if record.status not in {TransferStatus.OFFERED, TransferStatus.PREPARING}:
                return
            record.status = TransferStatus.REJECTED
            record.reason = str(payload.get("reason") or "rejected by peer")
        self._emit("FILE_FAILED", record)

    def _handle_result(self, peer_id: str, payload: dict) -> None:
        transfer_id = self._required_id(payload)
        with self._lock:
            record = self._transfers.get(transfer_id)
            if record is None or record.direction != TransferDirection.OUTGOING:
                return
            self._check_peer(record, peer_id)
            if record.status != TransferStatus.WAITING_RESULT:
                return
            if bool(payload.get("ok")):
                record.status = TransferStatus.COMPLETED
                record.bytes_transferred = record.size
                event_type = "FILE_COMPLETED"
            else:
                record.status = TransferStatus.FAILED
                record.reason = str(payload.get("reason") or "receiver rejected file result")
                event_type = "FILE_FAILED"
        self._emit(event_type, record)

    # ------------------------------------------------------------------
    # Luồng nhận file
    # ------------------------------------------------------------------
    def _handle_offer(self, peer_id: str, payload: dict) -> None:
        transfer_id = self._required_id(payload)
        filename = Path(str(payload.get("filename", ""))).name
        size = int(payload.get("size", -1))
        digest = str(payload.get("sha256", "")).lower()
        if not peer_id or not filename or filename in {".", ".."}:
            raise ValueError("invalid sender or filename")
        if size < 0 or (self.max_file_size is not None and size > self.max_file_size):
            raise ValueError("invalid file size")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("invalid SHA-256 digest")
        with self._lock:
            if transfer_id in self._transfers:
                return
            record = TransferRecord(
                transfer_id=transfer_id,
                peer_id=peer_id,
                filename=filename,
                size=size,
                sha256=digest,
                direction=TransferDirection.INCOMING,
                status=TransferStatus.PENDING,
            )
            self._transfers[transfer_id] = record
        self._emit("FILE_REQUEST", record)

    def _handle_chunk(self, peer_id: str, payload: dict) -> None:
        transfer_id = self._required_id(payload)
        seq = int(payload.get("seq", -1))
        encoded = payload.get("data")
        if not isinstance(encoded, str):
            raise ValueError("chunk data must be base64 text")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 chunk") from exc
        if len(chunk) > self.chunk_size:
            raise ValueError("chunk exceeds negotiated size")

        with self._lock:
            record = self._transfers.get(transfer_id)
            if record is None or record.direction != TransferDirection.INCOMING:
                return
            self._check_peer(record, peer_id)
            if record.status != TransferStatus.RECEIVING or record.file_handle is None:
                raise ValueError("file has not been accepted")
            if seq != record.next_seq:
                raise ValueError(f"expected chunk {record.next_seq}, got {seq}")
            if record.bytes_transferred + len(chunk) > record.size:
                raise ValueError("received more bytes than declared")
            try:
                record.file_handle.write(chunk)
            except OSError as exc:
                raise OSError(f"could not write file: {exc}") from exc
            record.bytes_transferred += len(chunk)
            record.next_seq += 1
        self._emit_progress(record)

    def _handle_end(self, peer_id: str, payload: dict) -> None:
        transfer_id = self._required_id(payload)
        with self._lock:
            record = self._transfers.get(transfer_id)
            if record is None or record.direction != TransferDirection.INCOMING:
                return
            self._check_peer(record, peer_id)
            if record.status != TransferStatus.RECEIVING:
                return
            if record.file_handle is not None:
                record.file_handle.flush()
                os.fsync(record.file_handle.fileno())
                record.file_handle.close()
                record.file_handle = None
            if record.bytes_transferred != record.size:
                reason = f"size mismatch: expected {record.size}, got {record.bytes_transferred}"
                self._fail_locked(record, reason)
                failed = True
            else:
                failed = False
            temp_path = record.temp_path

        if failed:
            self._send_result(record, False, record.reason)
            self._emit("FILE_FAILED", record)
            return
        if temp_path is None:
            self._fail(transfer_id, "missing partial file", notify_peer=True)
            return
        try:
            actual_hash = self._sha256_file(temp_path)
            if actual_hash != record.sha256:
                raise ValueError("SHA-256 mismatch")
            if record.save_path is None:
                raise OSError("missing destination path")
            os.replace(temp_path, record.save_path)
        except (OSError, ValueError) as exc:
            self._fail(transfer_id, str(exc), notify_peer=True)
            return

        with self._lock:
            record.temp_path = None
            record.status = TransferStatus.COMPLETED
            record.bytes_transferred = record.size
        self._send_result(record, True, "")
        self._emit("FILE_COMPLETED", record)

    # ------------------------------------------------------------------
    # Hàm hỗ trợ nội bộ
    # ------------------------------------------------------------------
    def _send(self, peer_id: str, packet_type: str, payload: dict) -> bool:
        packet = {
            "v": 1,
            "type": packet_type,
            "id": uuid.uuid4().hex,
            "sender_id": self.local_peer_id,
            "payload": payload,
        }
        try:
            return bool(self.transport.send_packet(peer_id, packet))
        except Exception:
            return False

    def _send_result(self, record: TransferRecord, ok: bool, reason: str) -> None:
        self._send(record.peer_id, "FILE_RESULT", {
            "transfer_id": record.transfer_id,
            "ok": ok,
            "reason": reason,
        })

    def _emit(self, event_type: str, record: TransferRecord) -> None:
        self._emit_raw(event_type, record.peer_id, record.public_data())

    def _emit_raw(self, event_type: str, peer_id: str, data: dict) -> None:
        self.event_queue.put(Event(type=event_type, peer_id=peer_id, data=data))

    def _emit_progress(self, record: TransferRecord, *, force: bool = False) -> None:
        progress = record.progress
        # Tránh làm đầy queue UI khi file lớn được chia thành quá nhiều chunk nhỏ.
        if force or progress >= 100 or progress - record.last_reported_percent >= 0.5:
            record.last_reported_percent = progress
            self._emit("FILE_PROGRESS", record)

    def _fail(self, transfer_id: str, reason: str, *, notify_peer: bool = False) -> None:
        with self._lock:
            record = self._transfers.get(transfer_id)
            if record is None or record.status in {
                TransferStatus.COMPLETED,
                TransferStatus.FAILED,
                TransferStatus.REJECTED,
            }:
                return
            self._fail_locked(record, reason)
        if notify_peer:
            self._send_result(record, False, reason)
        self._emit("FILE_FAILED", record)

    def _fail_locked(self, record: TransferRecord, reason: str) -> None:
        record.status = TransferStatus.FAILED
        record.reason = reason
        if record.file_handle is not None:
            try:
                record.file_handle.close()
            except OSError:
                pass
            record.file_handle = None
        if record.temp_path is not None:
            try:
                record.temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            record.temp_path = None

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)

    @staticmethod
    def _required_id(payload: dict) -> str:
        transfer_id = str(payload.get("transfer_id", ""))
        if not transfer_id:
            raise ValueError("missing transfer_id")
        return transfer_id

    @staticmethod
    def _check_peer(record: TransferRecord, peer_id: str) -> None:
        if peer_id and record.peer_id != peer_id:
            raise ValueError("packet sender does not own this transfer")

    @staticmethod
    def _available_temp_path(destination: Path, transfer_id: str) -> Path:
        base = destination.with_name(f".{destination.name}.{transfer_id[:8]}.part")
        candidate = base
        counter = 1
        while candidate.exists():
            candidate = base.with_name(f"{base.name}.{counter}")
            counter += 1
        return candidate


# Tên rút gọn cho nơi tích hợp muốn khởi tạo bằng ``FileTransfer(...)``.
FileTransfer = FileTransferManager


__all__ = [
    "Event",
    "FileTransfer",
    "FileTransferManager",
    "TransferDirection",
    "TransferRecord",
    "TransferStatus",
]
