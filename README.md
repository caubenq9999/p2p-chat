# P2P Chat — Sprint 1

Python 3.10+, raw TCP socket. UI dùng `resvg-py` để đọc các SVG gốc trong `assets/`.

```powershell
python -m pip install -r requirements.txt
```

## Trạng thái

- TCP Chat: `chat.py` triển khai transport, chat nhiều peer và event queue.
- Shared: `models.py`, `protocol.py` theo Contract Sprint 1; cả nhóm cần review khi tích hợp.
- UDP discovery: merge từ `origin/Peer-discovery`, cập nhật shared peer_id và event queue.
- File transfer và Tkinter UI: merge từ `origin/Hoang`.
- `main.py`: ghép discovery, TCP, file dispatcher và queue cho UI.

## Chạy ứng dụng tích hợp

### Bản web (chat và file thật)

Dừng `python -m http.server` cũ bằng Ctrl+C. Mở hai terminal:

```powershell
python web_app.py --name Huy --port 5001 --web-port 8000
python web_app.py --name Hoang --port 5002 --web-port 8001
```

Mở http://127.0.0.1:8000 và http://127.0.0.1:8001. Nếu TCP port đang bị app
cũ chiếm, đổi thành 5011/5012. Mỗi server Python là một peer; hai tab cùng URL
là cùng một peer. Web và Tkinter có thể chat/truyền file cho nhau.

Trên hai máy LAN: mỗi máy chạy web_app.py rồi mở localhost của chính máy đó.
HTTP chỉ bind loopback; UDP/TCP vẫn giao tiếp qua LAN. Browser dùng HTTP polling
600 ms tới Python, peer-to-peer vẫn raw TCP/UDP, không dùng WebSocket.

Web hỗ trợ upload tối đa 16 MiB/file. Người nhận bấm Nhận file/Từ chối;
sau khi kiểm tra hash hoàn tất, ảnh PNG/JPEG/GIF/WebP hiện trong hội thoại;
bấm ảnh để xem lớn hoặc Tải về máy để lưu với tên và đuôi gốc (kể cả tiếng Việt).
Có thể Hủy truyền khi đang chờ/đang gửi; việc upload không khóa nút chat.
Hội thoại và file đã nhận vẫn truy cập được khi peer offline trong phiên web.
File gửi/nhận được lưu tại
`.web-data/<peer_id>/` (không đưa vào Git); dọn thư mục khi đã tắt app nếu không
cần giữ. Lịch sử chat ở RAM trong phiên server. CSS lấy từ branch Hoang.

Quy ước tích hợp bổ sung: `FILE_RESULT {ok: false, reason}` chấm dứt phiên
ở mọi trạng thái đang hoạt động, từ cả phía gửi và nhận (hủy/lỗi giữa chừng).
`ok: true` chỉ được chấp nhận khi bên gửi đang chờ kết quả sau FILE_END.
Hai phía cần cùng bản cập nhật để phản hồi hủy đầy đủ.

### Bản desktop

Mở ba terminal trong `p2p-chat` (hoặc chạy mỗi lệnh trên một máy cùng LAN):

```powershell
python main.py --name Huy --port 5001
python main.py --name Nguyen --port 5002
python main.py --name Hoang --port 5003
```

Chọn người dùng trong danh sách để chat, nút File để gửi file. Người nhận chọn
chấp nhận/từ chối và nơi lưu. Mỗi instance tạo UUID chung cho UDP/TCP trong phiên.
UDP mặc định 9999; các instance phải dùng cùng UDP port, TCP port khác nhau nếu
cùng máy. Windows cần cho phép Python qua firewall trên mạng LAN để khám phá/kết nối.
Chạy `python ui.py` chỉ mở giao diện demo, không có mạng.

Nhánh tích hợp: `integration/sprint1`. Hai nhánh nguồn được merge giữ lịch sử.
Discovery hiện phát JSON PRESENCE theo contract; bản cũ chỉ gửi
`HELLO|name|port|status` không có peer_id sẽ bị bỏ qua. Các máy nên chạy cùng bản tích hợp.
API start/stop cũ vẫn có, bổ sung start_discovery/stop_discovery/broadcast_presence,
get_peer và get_online_peers trả shared Peer. Status được giữ trong event discovery.

Controller chạy riêng, chuyển FILE_* tới FileTransfer và event hiển thị sang UI;
không để hai consumer tranh nhau đọc một queue. Mất TCP làm thất bại các phiên
file chưa hoàn tất và dọn file tạm. Gửi chat/file tự kết nối tới peer từ discovery.
Assets PNG/SVG từ branch Hoang được dùng chung cho web và Tkinter.

## Kiểm thử

Chạy từ thư mục `p2p-chat`:

```powershell
python -m unittest discover -s tests -v
```

Test kiểm tra frame dài/Unicode, chia nhỏ, frame liên tiếp, dữ liệu sai,
EOF giữa frame, nhiều peer, gửi đồng thời, dispatch packet file, kết nối chéo
và shutdown. Test tích hợp chạy ba instance với UDP broadcast/TCP thật trên một
máy, kiểm tra chat, file 256 KiB/hash, refresh/timeout và mất TCP. Test Tkinter
kiểm tra nhận tin vẫn giữ nội dung đang nhập (skip nếu môi trường không có Tk).
Chưa kiểm thử LAN ba máy vật lý hay thao tác hộp thoại nhận file bằng tay.

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
