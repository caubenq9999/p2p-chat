# Hướng dẫn sử dụng giao diện LAN Chat

Tài liệu này dành cho các thành viên phụ trách **Peer Discovery** và **Chat/File Transfer** khi cần lấy giao diện từ branch `Hoang` về để chạy thử hoặc tích hợp logic.

## 1. Lấy giao diện về máy

### Trường hợp đã clone repository

Đứng tại thư mục `p2p-chat` và chạy:

```bash
git fetch origin
git switch Hoang
git pull origin Hoang
```

Nếu máy chưa có branch `Hoang` ở local:

```bash
git fetch origin
git switch --track origin/Hoang
```

### Trường hợp chưa clone repository

```bash
git clone https://github.com/caubenq9999/p2p-chat.git
cd p2p-chat
git switch Hoang
```

## 2. Chạy bản UI demo

UI hiện tại dùng HTML, CSS và JavaScript thuần, không cần cài package.

Cách nhanh nhất là mở file `index.html` bằng trình duyệt. Nếu dùng VS Code, có thể cài extension **Live Server**, nhấp chuột phải vào `index.html` và chọn **Open with Live Server**.

Cũng có thể chạy server có sẵn của Python:

```bash
python -m http.server 5500
```

Sau đó mở:

```text
http://localhost:5500
```

Nhấn `Ctrl + C` trong terminal để tắt server.

## 3. Cấu trúc phần giao diện

```text
p2p-chat/
├── index.html       # Khung giao diện
├── styles.css       # Màu sắc, bố cục, responsive và animation
├── app.js           # Dữ liệu demo và tương tác trên giao diện
└── assets/          # Avatar, icon và ảnh trang trí
```

Không đổi tên hoặc di chuyển thư mục `assets` nếu chưa cập nhật lại các đường dẫn `./assets/...` trong HTML, CSS và JavaScript.

## 4. Trạng thái hiện tại

Đây là **UI demo**. Các thao tác chọn người dùng, nhập tin nhắn, chọn file, badge chưa đọc, refresh và animation đã hiển thị được, nhưng chưa kết nối với mạng P2P thật.

Dữ liệu mẫu đang nằm trong hai biến `peers` và `messages` ở đầu file `app.js`.

## 5. Phần dành cho từng role

### Role Peer Discovery

Khi module discovery tìm thấy hoặc loại bỏ peer, hãy chuyển danh sách đó sang dạng:

```js
{
  id: "peer-id-unique",
  name: "Tên người dùng",
  ip: "192.168.1.15",
  avatar: "Chiikawa",
  online: true,
  unread: 0
}
```

Sau khi cập nhật dữ liệu, gọi lại hàm `peerList()` để render danh sách. Nút có id `refreshPeers` là vị trí để nối chức năng quét lại peer thật.

### Role Chat và File Transfer

Form có id `composer` là nơi bắt sự kiện gửi tin nhắn. Input file có id `fileInput` là nơi bắt sự kiện chọn file.

Tin nhắn hiển thị theo dạng:

```js
// Tin nhắn chữ
{ from: "peer", type: "text", text: "Xin chào", time: "10:24" }

// Tin nhắn file
{
  from: "me",
  type: "file",
  filename: "report.pdf",
  size: "24.8 MB",
  progress: 68,
  time: "10:25"
}
```

Các vị trí cần nối logic thật đã được đánh dấu bằng `TODO` trong `app.js`:

- Gửi tin nhắn từ sự kiện submit của `composer`.
- Gửi file từ sự kiện change của `fileInput`.
- Cập nhật `progress` bằng tiến độ truyền file thật.
- Đưa tin nhắn nhận được vào đúng mảng hội thoại rồi gọi `render()`.

## 6. Cách tích hợp mà ít conflict

Mỗi role nên làm trên branch riêng và lấy UI từ branch `Hoang` về branch của mình:

```bash
git switch <ten-branch-cua-ban>
git fetch origin
git merge origin/Hoang
```

Trước khi merge, hãy commit hoặc stash phần code đang làm dở. Không sửa trực tiếp `styles.css` hoặc cấu trúc HTML nếu chỉ đang nối logic; ưu tiên thay phần dữ liệu demo và các đoạn `TODO` trong `app.js`.

Nếu gặp conflict, giữ bố cục từ branch `Hoang` và ghép phần logic mạng của branch role vào các điểm tích hợp nói trên.

## 7. Kiểm tra trước khi push

- Mở `http://localhost:5500` và kiểm tra không có ảnh/icon bị mất.
- Thử chọn peer, gửi tin nhắn demo, chọn file và nhấn refresh.
- Mở DevTools (`F12`) và kiểm tra tab Console không có lỗi.
- Không commit file tạm, file nhận qua mạng hoặc cấu hình riêng của máy.

