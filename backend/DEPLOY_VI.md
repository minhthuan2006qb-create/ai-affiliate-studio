# Đưa Facebook Hub lên máy chủ (hướng dẫn cho điện thoại)

Tài liệu này mô tả cách chạy **API + website công khai + worker 24/7** trên một máy chủ Linux,
thao tác hoàn toàn qua trình duyệt trên điện thoại.

> Trạng thái: các file `docker-compose.yml`, `Caddyfile`, `app/hub_runner.py` đã có test tự động,
> nhưng **chưa được chạy thử trên máy chủ thật**. Lần triển khai đầu tiên cần kiểm tra từng bước.

## 1. Chọn nơi chạy

Hệ thống lưu dữ liệu trong file SQLite, nên máy chủ phải **giữ được ổ đĩa** và **chạy liên tục**.

| Lựa chọn | Chi phí | Ghi chú |
|---|---|---|
| Máy ảo Oracle Cloud "Always Free" | Miễn phí | Cần thẻ để xác minh khi đăng ký. Hay hết máy trống ("Out of capacity"). Hạn mức miễn phí có thể đã giảm, cần xem trang Oracle. |
| VPS trả phí nhỏ (1 GB RAM) | Khoảng vài USD/tháng | Ổn định, ít rủi ro nhất. |
| Render / Koyeb gói miễn phí | Miễn phí | **Không dùng được**: ổ đĩa bị xóa khi khởi động lại, máy tự ngủ nên worker không chạy 24/7. |

Bạn cũng cần **một tên miền** (hoặc tên miền phụ miễn phí) trỏ về IP máy chủ, để có HTTPS.
Meta bắt buộc HTTPS cho đăng nhập Facebook.

## 2. Mở cửa sổ dòng lệnh trên máy chủ

- Oracle Cloud: trong trang quản lý máy ảo, dùng nút kết nối/Cloud Shell trên trình duyệt.
- VPS khác: hầu hết có "Web console" trong trang quản lý.

Trên Oracle, mở cổng 80 và 443 ở **hai chỗ**: Security List của mạng (trong trang web Oracle)
và tường lửa bên trong máy:

```bash
sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
```

```bash
sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
```

```bash
sudo netfilter-persistent save
```

## 3. Cài Docker và lấy mã

```bash
curl -fsSL https://get.docker.com | sudo sh
```

```bash
git clone -b feature/facebook-hub-v1 https://github.com/minhthuan2006qb-create/ai-affiliate-studio.git
```

Nếu repo để riêng tư, GitHub sẽ hỏi mật khẩu: dùng Personal Access Token (quyền chỉ đọc), không dùng mật khẩu tài khoản.

```bash
cd ai-affiliate-studio/backend && cp .env.example .env
```

## 4. Điền cấu hình

Tạo khóa ngẫu nhiên (chạy lệnh, sao chép kết quả):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(40))"
```

Mở file cấu hình:

```bash
nano .env
```

Điền tối thiểu:

- `API_KEY=` khóa vừa tạo (đây là mật khẩu vào ứng dụng quản lý; không chia sẻ).
- `SITE_DOMAIN=` tên miền của bạn, ví dụ `tin.example.com`.
- `ALLOWED_ORIGINS=` địa chỉ trang bạn mở ứng dụng quản lý `facebook-hub.html`.
- `PUBLIC_SITE_NAME=` tên website.
- Giữ `META_PUBLISHING_ENABLED=false` cho tới khi thử OAuth Meta xong.

Lưu: bấm Ctrl+O, Enter, rồi Ctrl+X (bàn phím điện thoại: dùng phím Ctrl của ứng dụng terminal).

## 5. Chạy

```bash
sudo docker compose --profile https up -d --build
```

Kiểm tra:

```bash
sudo docker compose ps
```

```bash
sudo docker compose logs --tail 30 hub-worker
```

Mở trên điện thoại:
- `https://TÊN-MIỀN/health`: phải thấy trạng thái ok.
- `https://TÊN-MIỀN/site/`: website công khai.
- Ứng dụng quản lý: nhập `https://TÊN-MIỀN` và `API_KEY` ở ô "Kết nối backend".
  Mục "Vấn đề của Page" sẽ báo nếu worker dừng.

## 6. Sao lưu và cập nhật

Toàn bộ dữ liệu nằm trong thư mục `backend/data/`. Sao lưu định kỳ:

```bash
tar czf ~/hub-backup-$(date +%F).tgz data
```

Cập nhật mã mới:

```bash
git pull && sudo docker compose --profile https up -d --build
```

## Ghi chú an toàn

- Không đưa file `.env` lên GitHub (đã có trong `.gitignore`).
- `META_TOKEN_ENCRYPTION_KEY` phải giữ nguyên và sao lưu; đổi khóa sẽ làm mất token Page đã lưu.
- Cổng 8000 của API chỉ mở cho Caddy trong máy; người ngoài vào qua HTTPS cổng 443.
