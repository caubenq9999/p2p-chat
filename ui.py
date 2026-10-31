"""Giao diện Tkinter cho ứng dụng LAN Chat.

Module chỉ gọi API công khai của các service và nhận sự kiện qua queue an toàn
luồng. UI không gọi trực tiếp ``socket.send()`` hoặc ``socket.recv()``, nhờ đó
các thread mạng hoạt động độc lập với main thread của Tkinter.
"""

from __future__ import annotations

import hashlib
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


PINK = "#F75FA2"
PINK_DARK = "#E84B91"
PINK_LIGHT = "#FFE3F0"
PINK_PALE = "#FFF0F7"
TEXT = "#302331"
MUTED = "#938595"
GREEN = "#10CF6B"
RED = "#FB3555"
WHITE = "#FFFFFF"


@dataclass
class PeerView:
    peer_id: str
    name: str
    ip: str
    tcp_port: int = 0
    unread: int = 0
    online: bool = True


class LANChatUI:
    """UI desktop có thể nối với các service discovery, chat và truyền file."""

    def __init__(
        self,
        event_queue: Optional["queue.Queue[Any]"] = None,
        *,
        discovery: Any = None,
        chat: Any = None,
        file_transfer: Any = None,
        local_name: str = "Hoàng",
        local_ip: str = "127.0.0.1",
        assets_dir: Optional[str] = None,
        demo_mode: bool = False,
        root: Optional[tk.Tk] = None,
    ) -> None:
        self.root = root or tk.Tk()
        self.event_queue: "queue.Queue[Any]" = event_queue or queue.Queue()
        self.discovery = discovery
        self.chat = chat
        self.file_transfer = file_transfer
        self.local_name = local_name
        self.local_ip = local_ip
        self.demo_mode = demo_mode

        base = Path(__file__).resolve().parent
        self.assets_dir = Path(assets_dir) if assets_dir else base / "LANChat_UI_Assets" / "assets"
        self._avatar_files = sorted((self.assets_dir / "avatars").glob("*.png"))
        self._photos: Dict[tuple, tk.PhotoImage] = {}

        self.peers: Dict[str, PeerView] = {}
        self.conversations: Dict[str, List[dict]] = {}
        self.transfer_messages: Dict[str, dict] = {}
        self.selected_peer_id: Optional[str] = None
        self._closed = False

        self._configure_root()
        self._configure_styles()
        self._build_layout()
        if demo_mode:
            self._load_demo_state()
        else:
            self._render_peers()
            self._render_conversation()
        self.root.after(75, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # API công khai của UI
    # ------------------------------------------------------------------
    def show_peers(self, peers: Iterable[Any]) -> None:
        """Thay danh sách peer online hiện đang hiển thị ở sidebar."""
        snapshot: Dict[str, PeerView] = {}
        for peer in peers:
            view = self._normalise_peer(peer)
            if view.peer_id and view.online:
                previous = self.peers.get(view.peer_id)
                if previous:
                    view.unread = previous.unread
                snapshot[view.peer_id] = view
        self.peers = snapshot
        if self.selected_peer_id not in self.peers:
            self.selected_peer_id = next(iter(self.peers), None)
        self._render_peers()
        self._render_conversation()

    def show_message(self, peer_id: str, text: str, *, outgoing: bool = False) -> None:
        """Thêm tin nhắn văn bản vào hội thoại đang được lưu trong RAM."""
        message = {
            "type": "text",
            "from": "me" if outgoing else "peer",
            "text": text,
            "time": self._now(),
            "status": "sent" if outgoing else "received",
        }
        self.conversations.setdefault(peer_id, []).append(message)
        if not outgoing and peer_id != self.selected_peer_id and peer_id in self.peers:
            self.peers[peer_id].unread += 1
            self._render_peers()
        if peer_id == self.selected_peer_id:
            self._render_conversation(scroll_to_end=True)

    def show_progress(self, transfer_id: str, percent: float) -> None:
        """Cập nhật phần trăm tiến độ cho một file đang được truyền."""

        message = self.transfer_messages.get(transfer_id)
        if message is None:
            return
        message["progress"] = max(0.0, min(100.0, float(percent)))
        if message["progress"] >= 100 and message.get("status") not in {"failed", "rejected"}:
            message["status"] = "verifying"
        if message.get("peer_id") == self.selected_peer_id:
            self._render_conversation()

    def handle_event(self, event: Any) -> None:
        """Xử lý một Event do discovery, chat hoặc file-transfer phát ra."""
        event_type = str(self._field(event, "type", ""))
        peer_id = str(self._field(event, "peer_id", ""))
        data = self._field(event, "data", {}) or {}
        if not isinstance(data, dict):
            data = vars(data) if hasattr(data, "__dict__") else {}

        if event_type in {"PEER_ONLINE", "PEER_UPDATED"}:
            source = data.get("peer", data)
            view = self._normalise_peer(source, fallback_id=peer_id)
            if view.peer_id:
                old = self.peers.get(view.peer_id)
                view.unread = old.unread if old else 0
                self.peers[view.peer_id] = view
                if self.selected_peer_id is None:
                    self.selected_peer_id = view.peer_id
                self._render_peers()
                self._render_conversation()
            return

        if event_type == "PEER_OFFLINE":
            self.peers.pop(peer_id, None)
            if self.selected_peer_id == peer_id:
                self.selected_peer_id = next(iter(self.peers), None)
            self._render_peers()
            self._render_conversation()
            return

        if event_type == "CHAT_RECEIVED":
            self.show_message(peer_id, str(data.get("text", "")))
            return

        if event_type == "FILE_REQUEST":
            self._handle_file_request(peer_id, data)
            return

        if event_type == "FILE_PROGRESS":
            self._handle_file_progress(peer_id, data)
            return

        if event_type in {"FILE_COMPLETED", "FILE_FAILED"}:
            self._handle_file_finished(peer_id, data, event_type == "FILE_COMPLETED")
            return

        if event_type in {"CONNECTION_ERROR", "PEER_DISCONNECTED"} and peer_id:
            if peer_id == self.selected_peer_id:
                self.connection_var.set("Mất kết nối TCP")

    def run(self) -> None:
        """Khởi chạy vòng lặp giao diện chính của Tkinter."""

        self.root.mainloop()

    # ------------------------------------------------------------------
    # Cửa sổ và bố cục
    # ------------------------------------------------------------------
    def _configure_root(self) -> None:
        self.root.title("LAN Chat")
        self.root.geometry("1280x760")
        self.root.minsize(900, 600)
        self.root.configure(bg=PINK_PALE)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Pink.Horizontal.TProgressbar",
            troughcolor="#EFCFDE",
            background=PINK,
            bordercolor="#EFCFDE",
            lightcolor=PINK,
            darkcolor=PINK,
            thickness=10,
        )

    def _build_layout(self) -> None:
        shell = tk.Frame(self.root, bg=PINK_PALE)
        shell.pack(fill="both", expand=True, padx=18, pady=18)
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_columnconfigure(0, weight=73)
        shell.grid_columnconfigure(1, weight=27, minsize=270)

        left = tk.Frame(shell, bg=WHITE, highlightthickness=1, highlightbackground="#F8DCE8")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 11))
        left.grid_rowconfigure(2, weight=1)
        left.grid_columnconfigure(0, weight=1)

        right = tk.Frame(shell, bg="#FFF9FC", highlightthickness=1, highlightbackground="#F8DCE8")
        right.grid(row=0, column=1, sticky="nsew", padx=(11, 0))
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self._build_brand(left)
        self._build_chat_header(left)
        self._build_messages(left)
        self._build_composer(left)
        self._build_profile(right)
        self._build_peer_list(right)

    def _build_brand(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg="#F77FB4", height=92)
        frame.grid(row=0, column=0, sticky="ew")
        frame.grid_propagate(False)
        logo = self._icon("logo-planet.png", 58)
        tk.Label(frame, image=logo, bg="#F77FB4").pack(side="left", padx=(28, 14))
        copy = tk.Frame(frame, bg="#F77FB4")
        copy.pack(side="left", pady=13)
        tk.Label(copy, text="LAN Chat", bg="#F77FB4", fg=WHITE,
                 font=("Segoe UI", 27, "bold")).pack(anchor="w")
        tk.Label(copy, text="Chat directly. Nearby only.", bg="#F77FB4", fg="#FFF0F7",
                 font=("Segoe UI", 10)).pack(anchor="w")

    def _build_chat_header(self, parent: tk.Widget) -> None:
        self.chat_header = tk.Frame(parent, bg="#FFF5F9", height=90)
        self.chat_header.grid(row=1, column=0, sticky="ew")
        self.chat_header.grid_propagate(False)
        self.header_avatar = tk.Label(self.chat_header, bg="#FFF5F9")
        self.header_avatar.pack(side="left", padx=(26, 15), pady=12)
        copy = tk.Frame(self.chat_header, bg="#FFF5F9")
        copy.pack(side="left", pady=18)
        self.peer_name_var = tk.StringVar(value="Chưa chọn người dùng")
        self.connection_var = tk.StringVar(value="Đang tìm người dùng trong LAN…")
        tk.Label(copy, textvariable=self.peer_name_var, bg="#FFF5F9", fg=TEXT,
                 font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(copy, textvariable=self.connection_var, bg="#FFF5F9", fg=MUTED,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(3, 0))

    def _build_messages(self, parent: tk.Widget) -> None:
        holder = tk.Frame(parent, bg=WHITE)
        holder.grid(row=2, column=0, sticky="nsew")
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.message_canvas = tk.Canvas(holder, bg=WHITE, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.message_canvas.yview)
        self.message_canvas.configure(yscrollcommand=scroll.set)
        self.message_canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.message_frame = tk.Frame(self.message_canvas, bg=WHITE)
        self.message_window = self.message_canvas.create_window(
            (0, 0), window=self.message_frame, anchor="nw"
        )
        self.message_frame.bind(
            "<Configure>",
            lambda _e: self.message_canvas.configure(scrollregion=self.message_canvas.bbox("all")),
        )
        self.message_canvas.bind(
            "<Configure>",
            lambda e: self.message_canvas.itemconfigure(self.message_window, width=e.width),
        )

    def _build_composer(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg=WHITE, height=82)
        frame.grid(row=3, column=0, sticky="ew")
        frame.grid_propagate(False)
        attach_icon = self._icon("paperclip.png", 27)
        send_icon = self._icon("send.png", 28)
        self.attach_button = tk.Button(
            frame, image=attach_icon, command=self._on_attach, bd=0, cursor="hand2",
            bg=PINK_LIGHT, activebackground="#FFD5E7", width=52, height=52,
        )
        self.attach_button.pack(side="left", padx=(20, 12), pady=14)
        self.message_entry = tk.Entry(
            frame, bd=1, relief="solid", highlightthickness=2,
            highlightbackground="#F7C9DE", highlightcolor=PINK,
            fg=TEXT, bg="#FFFAFD", insertbackground=TEXT,
            font=("Segoe UI", 12),
        )
        self.message_entry.pack(side="left", fill="x", expand=True, ipady=12, pady=15)
        self.message_entry.bind("<Return>", self._on_send)
        self.send_button = tk.Button(
            frame, image=send_icon, command=self._on_send, bd=0, cursor="hand2",
            bg=PINK, activebackground=PINK_DARK, width=54, height=54,
        )
        self.send_button.pack(side="left", padx=14, pady=14)

    def _build_profile(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg="#FFDCE9", height=220)
        frame.grid(row=0, column=0, sticky="ew")
        frame.grid_propagate(False)
        avatar = self._avatar("local-profile", 105)
        tk.Label(frame, image=avatar, bg="#FFDCE9").pack(pady=(18, 3))
        tk.Label(frame, text=self.local_name, bg="#FFDCE9", fg=TEXT,
                 font=("Segoe UI", 18, "bold")).pack()
        tk.Label(frame, text=self.local_ip, bg="#FFDCE9", fg=MUTED,
                 font=("Segoe UI", 10)).pack(pady=(2, 0))

    def _build_peer_list(self, parent: tk.Widget) -> None:
        frame = tk.Frame(parent, bg="#FFF9FC")
        frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=16)
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        heading = tk.Frame(frame, bg="#FFF9FC")
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        radar = self._icon("radar.png", 25)
        tk.Label(heading, image=radar, bg="#FFF9FC").pack(side="left")
        tk.Label(heading, text="Nearby Friends", bg="#FFF9FC", fg=PINK,
                 font=("Segoe UI", 15, "bold")).pack(side="left", padx=8)

        canvas = tk.Canvas(frame, bg="#FFF9FC", highlightthickness=0)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.peer_frame = tk.Frame(canvas, bg="#FFF9FC")
        window = canvas.create_window((0, 0), window=self.peer_frame, anchor="nw")
        self.peer_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

    # ------------------------------------------------------------------
    # Hiển thị dữ liệu trạng thái lên giao diện
    # ------------------------------------------------------------------
    def _render_peers(self) -> None:
        for child in self.peer_frame.winfo_children():
            child.destroy()
        online = [peer for peer in self.peers.values() if peer.online]
        if not online:
            tk.Label(
                self.peer_frame,
                text="Không tìm thấy người dùng\nđang online trong mạng LAN.",
                bg="#FFF9FC", fg=MUTED, justify="center", font=("Segoe UI", 10),
            ).pack(pady=45)
            return

        for peer in online:
            selected = peer.peer_id == self.selected_peer_id
            bg = "#EDFFF5" if selected else "#FFF0F6"
            border = GREEN if selected else "#FFF0F6"
            row = tk.Frame(
                self.peer_frame, bg=bg, highlightthickness=2,
                highlightbackground=border, cursor="hand2", height=76,
            )
            row.pack(fill="x", pady=5)
            row.pack_propagate(False)
            avatar = self._avatar(peer.peer_id, 52)
            avatar_label = tk.Label(row, image=avatar, bg=bg)
            avatar_label.pack(side="left", padx=(10, 9))
            copy = tk.Frame(row, bg=bg)
            copy.pack(side="left", fill="both", expand=True, pady=13)
            name = tk.Label(copy, text=peer.name, bg=bg, fg=TEXT, anchor="w",
                            font=("Segoe UI", 12, "bold"))
            name.pack(fill="x")
            ip = tk.Label(copy, text=peer.ip, bg=bg, fg=MUTED, anchor="w",
                          font=("Segoe UI", 9))
            ip.pack(fill="x")
            if peer.unread:
                badge = tk.Label(row, text=str(peer.unread), bg=RED, fg=WHITE,
                                 font=("Segoe UI", 10, "bold"), padx=8, pady=4)
                badge.pack(side="right", padx=10)
            dot = tk.Label(row, text="●", bg=bg, fg=GREEN, font=("Segoe UI", 13))
            dot.place(x=52, y=45)
            for widget in (row, avatar_label, copy, name, ip, dot):
                widget.bind("<Button-1>", lambda _e, pid=peer.peer_id: self._select_peer(pid))

    def _render_conversation(self, *, scroll_to_end: bool = False) -> None:
        peer = self.peers.get(self.selected_peer_id or "")
        if peer is None:
            self.peer_name_var.set("Chưa chọn người dùng")
            self.connection_var.set("Đang tìm người dùng trong LAN…")
            self.header_avatar.configure(image="")
            self._set_composer_enabled(False)
        else:
            avatar = self._avatar(peer.peer_id, 62)
            self.header_avatar.configure(image=avatar)
            self.header_avatar.image = avatar
            self.peer_name_var.set(peer.name)
            port = f":{peer.tcp_port}" if peer.tcp_port else ""
            self.connection_var.set(f"Online • {peer.ip}{port}")
            self._set_composer_enabled(True)

        for child in self.message_frame.winfo_children():
            child.destroy()
        if peer is None:
            tk.Label(self.message_frame, text="Chọn một người dùng để bắt đầu trò chuyện.",
                     bg=WHITE, fg=MUTED, font=("Segoe UI", 11)).pack(pady=80)
            return

        messages = self.conversations.get(peer.peer_id, [])
        if not messages:
            tk.Label(self.message_frame, text="Chưa có tin nhắn.", bg=WHITE, fg=MUTED,
                     font=("Segoe UI", 10)).pack(pady=70)
        for message in messages:
            self._render_message(peer, message)
        self.root.update_idletasks()
        if scroll_to_end:
            self.message_canvas.yview_moveto(1.0)

    def _render_message(self, peer: PeerView, message: dict) -> None:
        outgoing = message.get("from") == "me"
        row = tk.Frame(self.message_frame, bg=WHITE)
        row.pack(fill="x", padx=18, pady=6)
        side = "right" if outgoing else "left"
        avatar = self._avatar("local-profile" if outgoing else peer.peer_id, 42)
        avatar_label = tk.Label(row, image=avatar, bg=WHITE)
        avatar_label.pack(side=side, padx=7, anchor="n")
        body = tk.Frame(row, bg=WHITE)
        body.pack(side=side, anchor="e" if outgoing else "w")

        if message.get("type") == "file":
            self._render_file_card(body, message)
        else:
            bg = "#FFDFEC" if outgoing else "#FFEAF3"
            tk.Label(
                body, text=message.get("text", ""), bg=bg, fg=TEXT,
                font=("Segoe UI", 11), padx=16, pady=10,
                wraplength=470, justify="left",
            ).pack(anchor="e" if outgoing else "w")
        footer = message.get("time", "")
        if message.get("status") == "failed":
            footer += "  • Không gửi được"
        tk.Label(body, text=footer, bg=WHITE,
                 fg=RED if message.get("status") == "failed" else MUTED,
                 font=("Segoe UI", 8)).pack(anchor="e" if outgoing else "w", pady=(3, 0))

    def _render_file_card(self, parent: tk.Widget, message: dict) -> None:
        card = tk.Frame(parent, bg="#FFE6F0", width=385, height=76)
        card.pack()
        card.pack_propagate(False)
        icon = self._icon("file.png", 38)
        tk.Label(card, image=icon, bg="#FFE6F0").pack(side="left", padx=(15, 12))
        copy = tk.Frame(card, bg="#FFE6F0")
        copy.pack(side="left", fill="y", pady=13)
        tk.Label(copy, text=message.get("filename", "File"), bg="#FFE6F0", fg=TEXT,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(anchor="w")
        tk.Label(copy, text=message.get("size_text", ""), bg="#FFE6F0", fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(3, 0))
        progress = float(message.get("progress", 0))
        status = str(message.get("status", ""))
        progress_box = tk.Frame(card, bg="#FFE6F0", width=130)
        progress_box.pack(side="right", fill="y", padx=14, pady=12)
        progress_box.pack_propagate(False)
        if status == "completed":
            label = "✓ Hoàn tất"
            color = GREEN
        elif status in {"failed", "rejected"}:
            label = "✕ Thất bại"
            color = RED
        elif status == "pending":
            label = "Chờ xác nhận"
            color = PINK
        elif status == "preparing":
            label = "Đang chuẩn bị…"
            color = PINK
        elif status == "verifying":
            label = "Đang kiểm tra…"
            color = PINK
        else:
            label = f"{progress:.0f}%"
            color = PINK
        tk.Label(progress_box, text=label, bg="#FFE6F0", fg=color,
                 font=("Segoe UI", 9, "bold")).pack(anchor="e")
        ttk.Progressbar(progress_box, style="Pink.Horizontal.TProgressbar",
                        maximum=100, value=progress).pack(fill="x", pady=(7, 0))

    # ------------------------------------------------------------------
    # Thao tác của người dùng
    # ------------------------------------------------------------------
    def _select_peer(self, peer_id: str) -> None:
        if peer_id not in self.peers:
            return
        self.selected_peer_id = peer_id
        self.peers[peer_id].unread = 0
        self._render_peers()
        self._render_conversation(scroll_to_end=True)
        if self.chat is not None and hasattr(self.chat, "connect"):
            peer = self.peers[peer_id]
            threading.Thread(target=self._connect_peer, args=(peer,), daemon=True).start()

    def _connect_peer(self, peer: PeerView) -> None:
        try:
            self.chat.connect(peer)
        except Exception as exc:
            if not self._closed:
                self.root.after(0, lambda: self.connection_var.set(f"Không thể kết nối: {exc}"))

    def _on_send(self, _event: Any = None) -> str:
        peer_id = self.selected_peer_id
        text = self.message_entry.get().strip()
        if not peer_id or not text:
            return "break"
        self.message_entry.delete(0, "end")
        message = {
            "type": "text", "from": "me", "text": text,
            "time": self._now(), "status": "sending",
        }
        self.conversations.setdefault(peer_id, []).append(message)
        self._render_conversation(scroll_to_end=True)

        if self.demo_mode and self.chat is None:
            message["status"] = "sent"
            self._render_conversation(scroll_to_end=True)
        elif self.chat is None or not hasattr(self.chat, "send_message"):
            message["status"] = "failed"
            self._render_conversation(scroll_to_end=True)
        else:
            threading.Thread(
                target=self._send_text_worker,
                args=(peer_id, text, message),
                daemon=True,
            ).start()
        return "break"

    def _send_text_worker(self, peer_id: str, text: str, message: dict) -> None:
        try:
            ok = bool(self.chat.send_message(peer_id, text))
        except Exception:
            ok = False
        message["status"] = "sent" if ok else "failed"
        if not self._closed:
            self.root.after(0, lambda: self._render_conversation(scroll_to_end=True))

    def _on_attach(self) -> None:
        peer_id = self.selected_peer_id
        if not peer_id:
            return
        if self.file_transfer is None:
            messagebox.showinfo("LAN Chat", "File Transfer chưa được kết nối.", parent=self.root)
            return
        path = filedialog.askopenfilename(title="Chọn file để gửi", parent=self.root)
        if not path:
            return
        try:
            transfer_id = self.file_transfer.send_file(peer_id, path)
        except Exception as exc:
            messagebox.showerror("Không thể gửi file", str(exc), parent=self.root)
            return
        file_path = Path(path)
        message = {
            "type": "file", "from": "me", "peer_id": peer_id,
            "transfer_id": transfer_id, "filename": file_path.name,
            "size_text": self._format_size(file_path.stat().st_size),
            "progress": 0.0, "status": "preparing", "time": self._now(),
        }
        self.transfer_messages[transfer_id] = message
        self.conversations.setdefault(peer_id, []).append(message)
        self._render_conversation(scroll_to_end=True)

    # ------------------------------------------------------------------
    # Các hàm xử lý theo từng loại Event
    # ------------------------------------------------------------------
    def _handle_file_request(self, peer_id: str, data: dict) -> None:
        transfer_id = str(data.get("transfer_id", ""))
        if not transfer_id or self.file_transfer is None:
            return
        message = {
            "type": "file", "from": "peer", "peer_id": peer_id,
            "transfer_id": transfer_id,
            "filename": str(data.get("filename", "File")),
            "size_text": self._format_size(int(data.get("size", 0))),
            "progress": 0.0, "status": "pending", "time": self._now(),
        }
        self.transfer_messages[transfer_id] = message
        self.conversations.setdefault(peer_id, []).append(message)
        if peer_id != self.selected_peer_id and peer_id in self.peers:
            self.peers[peer_id].unread += 1
            self._render_peers()
        if peer_id == self.selected_peer_id:
            self._render_conversation(scroll_to_end=True)

        peer_name = self.peers.get(peer_id, PeerView(peer_id, peer_id, "")).name
        accepted = messagebox.askyesno(
            "Nhận file",
            f"{peer_name} muốn gửi {message['filename']} ({message['size_text']}).\nBạn có muốn nhận không?",
            parent=self.root,
        )
        if not accepted:
            self.file_transfer.reject_file(transfer_id)
            message["status"] = "rejected"
            self._render_conversation()
            return
        path = filedialog.asksaveasfilename(
            title="Lưu file nhận được",
            initialfile=message["filename"],
            parent=self.root,
        )
        if not path:
            self.file_transfer.reject_file(transfer_id)
            message["status"] = "rejected"
        elif self.file_transfer.accept_file(transfer_id, path):
            message["status"] = "receiving"
        else:
            message["status"] = "failed"
        self._render_conversation()

    def _handle_file_progress(self, peer_id: str, data: dict) -> None:
        transfer_id = str(data.get("transfer_id", ""))
        if not transfer_id:
            return
        message = self.transfer_messages.get(transfer_id)
        if message is None:
            direction = str(data.get("direction", "incoming"))
            message = {
                "type": "file",
                "from": "me" if direction == "outgoing" else "peer",
                "peer_id": peer_id,
                "transfer_id": transfer_id,
                "filename": str(data.get("filename", "File")),
                "size_text": self._format_size(int(data.get("size", 0))),
                "time": self._now(),
            }
            self.transfer_messages[transfer_id] = message
            self.conversations.setdefault(peer_id, []).append(message)
        message["progress"] = float(data.get("progress", 0.0))
        message["status"] = str(data.get("status", message.get("status", "sending")))
        if peer_id == self.selected_peer_id:
            self._render_conversation()

    def _handle_file_finished(self, peer_id: str, data: dict, ok: bool) -> None:
        transfer_id = str(data.get("transfer_id", ""))
        message = self.transfer_messages.get(transfer_id)
        if message is None:
            self._handle_file_progress(peer_id, data)
            message = self.transfer_messages.get(transfer_id)
        if message is None:
            return
        message["progress"] = 100.0 if ok else float(data.get("progress", 0.0))
        message["status"] = "completed" if ok else "failed"
        message["reason"] = str(data.get("reason", ""))
        if peer_id == self.selected_peer_id:
            self._render_conversation(scroll_to_end=True)

    # ------------------------------------------------------------------
    # Queue, asset và các hàm hỗ trợ nhỏ
    # ------------------------------------------------------------------
    def _poll_events(self) -> None:
        if self._closed:
            return
        for _ in range(100):
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle_event(event)
            finally:
                task_done = getattr(self.event_queue, "task_done", None)
                if callable(task_done):
                    task_done()
        self.root.after(75, self._poll_events)

    def _normalise_peer(self, peer: Any, fallback_id: str = "") -> PeerView:
        if isinstance(peer, PeerView):
            return peer
        getter = peer.get if isinstance(peer, dict) else lambda key, default=None: getattr(peer, key, default)
        peer_id = str(getter("peer_id", fallback_id) or fallback_id)
        return PeerView(
            peer_id=peer_id,
            name=str(getter("name", peer_id) or peer_id),
            ip=str(getter("ip", "") or ""),
            tcp_port=int(getter("tcp_port", 0) or 0),
            unread=int(getter("unread", 0) or 0),
            online=bool(getter("online", True)),
        )

    def _avatar(self, identity: str, target: int) -> tk.PhotoImage:
        if not self._avatar_files:
            return self._blank_photo(target)
        digest = hashlib.sha256(identity.encode("utf-8")).digest()
        path = self._avatar_files[int.from_bytes(digest[:4], "big") % len(self._avatar_files)]
        return self._photo(path, target)

    def _icon(self, name: str, target: int) -> tk.PhotoImage:
        path = self.assets_dir / "icons" / name
        return self._photo(path, target) if path.exists() else self._blank_photo(target)

    def _photo(self, path: Path, target: int) -> tk.PhotoImage:
        key = (str(path), target)
        if key in self._photos:
            return self._photos[key]
        try:
            original = tk.PhotoImage(file=str(path))
            factor = max(1, max(original.width(), original.height()) // target)
            image = original.subsample(factor, factor)
        except tk.TclError:
            image = self._blank_photo(target)
        self._photos[key] = image
        return image

    def _blank_photo(self, target: int) -> tk.PhotoImage:
        key = ("blank", target)
        if key not in self._photos:
            image = tk.PhotoImage(width=target, height=target)
            image.put(PINK_LIGHT, to=(0, 0, target, target))
            self._photos[key] = image
        return self._photos[key]

    def _set_composer_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.message_entry.configure(state=state)
        self.attach_button.configure(state=state)
        self.send_button.configure(state=state)

    def _load_demo_state(self) -> None:
        self.show_peers([
            {"peer_id": "huy", "name": "Huy", "ip": "192.168.1.15"},
            {"peer_id": "nguyen", "name": "Nguyên", "ip": "192.168.1.22", "unread": 2},
            {"peer_id": "linh", "name": "Linh", "ip": "192.168.1.27"},
        ])
        self.conversations["huy"] = [
            {"type": "text", "from": "peer", "text": "Hii", "time": "10:24", "status": "received"},
            {"type": "text", "from": "me", "text": "hii", "time": "10:24", "status": "sent"},
            {"type": "text", "from": "peer", "text": "gửi file nè", "time": "10:25", "status": "received"},
            {"type": "file", "from": "peer", "peer_id": "huy", "transfer_id": "demo-file",
             "filename": "report.pdf", "size_text": "24.8 MB", "progress": 68,
             "status": "receiving", "time": "10:25"},
        ]
        self.transfer_messages["demo-file"] = self.conversations["huy"][-1]
        self.selected_peer_id = "huy"
        self._render_peers()
        self._render_conversation(scroll_to_end=True)

    def _on_close(self) -> None:
        self._closed = True
        for service, method in (
            (self.file_transfer, "shutdown"),
            (self.discovery, "stop_discovery"),
        ):
            callback = getattr(service, method, None)
            if callable(callback):
                try:
                    callback()
                except Exception:
                    pass
        self.root.destroy()

    @staticmethod
    def _field(source: Any, name: str, default: Any = None) -> Any:
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%H:%M")

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(max(0, size))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"


def main() -> None:
    """Mở bản demo giao diện khi chạy trực tiếp file ``ui.py``."""
    LANChatUI(demo_mode=True, local_ip="192.168.1.8").run()


if __name__ == "__main__":
    main()


__all__ = ["LANChatUI", "PeerView"]
