# P2P Chat — Sprint 1

Python 3.10+, thư viện chuẩn, raw TCP socket.

## Trạng thái

- TCP Chat: `chat.py` triển khai transport, chat nhiều peer và event queue.
- Shared: `models.py`, `protocol.py` theo Contract Sprint 1; cả nhóm cần review khi tích hợp.
- UDP discovery: broadcaster, listener, online list, timeout — Nguyên phụ trách, chưa triển khai.
- File transfer và UI — Hoàng phụ trách, chưa triển khai.
- `main.py` tích hợp chưa triển khai.

## Kiểm thử

Chạy từ thư mục `p2p-chat`:

```powershell
python -m unittest discover -s tests -v
```

Test kiểm tra frame dài/Unicode, chia nhỏ, frame liên tiếp, dữ liệu sai,
EOF giữa frame, nhiều peer, gửi đồng thời, dispatch packet file, kết nối chéo
và shutdown. Chưa thay thế test LAN ba máy, discovery hay truyền file đủ/hash.

## Tích hợp

```python
from queue import Queue
from chat import Chat
from models import Peer

events = Queue()  # Dùng queue không giới hạn, chung với controller/UI
chat = Chat("peer-a", "Alice", events)
chat.start_server(5001)  # Listener chạy nền
try:
    # Peer thực tế lấy từ discovery.get_peer(...).
    peer = Peer("peer-b", "Bob", "192.168.1.12", 5002, 0.0)
    if chat.connect(peer):
        chat.send_message(peer.peer_id, "Xin chào!")
    print(chat.get_connections())
finally:
    chat.stop_server()
```

API contract được cung cấp dưới dạng method của `Chat`: `start_server(port)`,
`connect(peer)`, `send_message(peer_id, text)`, `send_packet(peer_id, packet)`,
`disconnect(peer_id)`, `get_connections()`.

`connect()` và hàm gửi đồng bộ: UI nên gọi qua worker để không chặn nhập liệu.
Receive loop chạy riêng. `sendall()` có thể chờ khi remote không đọc;
`disconnect()`/`stop_server()` đóng socket để ngắt thao tác. Gửi trả True chỉ
xác nhận socket cục bộ gửi thành công, không phải ACK từ ứng dụng bên nhận.

### Event queue

Controller lấy Event và chuyển tới UI/FileTransfer. UI dùng polling/schedule
phù hợp, không gọi queue.get() blocking trên luồng giao diện.

| Event.type | Event.data |
| --- | --- |
| CHAT_RECEIVED | text, message_id |
| PEER_CONNECTED | name |
| PEER_DISCONNECTED | dict rỗng |
| PACKET_RECEIVED | packet: toàn bộ envelope |
| CONNECTION_ERROR | reason: mô tả lỗi để UI/log xử lý |

Event luôn có peer_id; ID có thể rỗng nếu lỗi trước nhận dạng hoặc lỗi listener.
Controller chuyển `event.data["packet"]` tới FileTransfer.handle_packet().
FileTransfer dùng `protocol.make_packet(type, chat.peer_id, payload)` và
`chat.send_packet(peer_id, packet)`, không tự đọc/ghi socket.
PACKET_RECEIVED còn bao gồm ERROR, PING/PONG; controller xử lý nếu cần.
TCP disconnect không đồng nghĩa UDP offline.

## Protocol và các lựa chọn bổ sung

- Envelope `{v, type, id, sender_id, payload}`, v=1; id tạo bằng UUID.
- Frame: 4 byte unsigned big-endian + JSON UTF-8, tối đa 1 MiB payload.
- Hai bên gửi HELLO rồi nhận HELLO; peer_id phải khớp Peer khi connect.
- Mỗi socket một receive loop và write lock cho toàn bộ frame.
- Kết nối chéo: ưu tiên socket do peer có ID nhỏ hơn theo thứ tự chuỗi khởi tạo.
  Thay socket ban đầu có thể làm lần gửi đang chạy thất bại; transport không
  tự retry tin nhắn.
- Packet sai/type lạ/sai sender: CONNECTION_ERROR và đóng kết nối đó, process
  tiếp tục. Controller cần ghi log/hiển thị sự kiện lỗi. FileTransfer tự kiểm tra
  schema file, sequence, size và SHA-256.
- Constructor bổ sung: `Chat(peer_id, name, event_queue=None, *, host="0.0.0.0", timeout=5)`.
  Timeout áp dụng connect và các thao tác handshake.
- `stop_server()` bổ sung để đóng listener, connections, handshake và join thread.
  Gọi nhiều lần được; tạo Chat mới để khởi động lại.
- `start_server(0)` dùng cho test; port thực nằm ở `chat.port`. Discovery phải
  quảng bá đúng port và dùng chung peer_id với Chat.
- Bind/listen lỗi phát CONNECTION_ERROR và raise OSError cho main xử lý.

HELLO chưa có xác thực/mã hóa; triển khai dành cho bài tập LAN tin cậy.
