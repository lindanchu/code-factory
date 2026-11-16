# Kho sách — Spec v0.1

Gói design bịa ra cho fixture của auto test (`tests/autotest-fixture`). Không phải gói của một sản phẩm thật.

## File trong gói

| File | Nội dung |
|---|---|
| `Kho sách · Quản lý.html` | Màn Kho sách (danh sách, menu Sắp xếp, hộp thoại Thêm sách, hộp thoại Chi tiết sách) và màn Trợ giúp |
| `Kho sách · Quản lý.data.json` | Dữ liệu mẫu của màn Kho sách |
| `Báo cáo tồn kho.html` | Màn Báo cáo tồn kho; chưa nhận dữ liệu từ ngoài |
| `tokens.css` | Token (tiền tố `--ks-`) |
| `autotest-states.json` | Bảng trạng thái cho auto test |

## Mở trang với trạng thái và dữ liệu khác

Mỗi màn nhận đầu vào lúc mở: `<Trang>.html#dc=<JSON đã encodeURIComponent>`. JSON là một đối tượng, mỗi khoá là một prop của màn trong bảng trạng thái (cột Đầu vào) hoặc `data` (cùng dạng với `<Trang>.data.json`). Không truyền gì thì màn mở với dữ liệu mẫu ở trạng thái mặc định. Trang không dùng được đầu vào thì hiện "Không mở được trang: …".

## Trạng thái

### `Kho sách · Quản lý.html`

Đối tượng được chọn theo vị trí trong dữ liệu, không theo tên: hộp thoại Chi tiết sách vẽ đầu sách đầu tiên của `books`.

| Mã | Đầu vào | Màn | Trạng thái | Mô tả |
|---|---|---|---|---|
| HP-1 | `{"screen":"help","variant":"default"}` | help | Trợ giúp | Trang chữ, không hiện dữ liệu. |
| BK-1 | `{"screen":"books","variant":"default"}` | books | Danh sách | Bảng đầu sách, dòng "Tổng số: n đầu sách". |
| BK-2 | `{"screen":"books","variant":"add"}` | books | Thêm sách | Hộp thoại ba ô; giá trị đã gõ lấy từ `demo.newBook`. |
| BK-3 | `{"screen":"books","variant":"detail"}` | books | Chi tiết sách | Hộp thoại của đầu sách đầu tiên trong `books`. |
| BK-4 | `{"screen":"books","variant":"sort"}` | books | Menu sắp xếp | Menu mở dưới nút Sắp xếp: Theo mã, Theo tên sách, Theo số lượng. |

### `Báo cáo tồn kho.html`

| Mã | Đầu vào | Màn | Trạng thái | Mô tả |
|---|---|---|---|---|
| RP-1 | `{}` | report | Báo cáo tồn kho | Số liệu vẽ sẵn trong trang, chưa nhận `data`. |

## Dữ liệu

### `Kho sách · Quản lý.data.json`

| Khoá | Kiểu | Hiện ở đâu | Ví dụ |
|---|---|---|---|
| `books[].id` | chuỗi | Không hiện; khoá của đầu sách | `"b1"` |
| `books[].code` | chuỗi | Bảng: cột Mã; hộp thoại Chi tiết: dòng Mã sách | `"KS-101"` |
| `books[].name` | chuỗi | Bảng: cột Tên sách; tiêu đề hộp thoại Chi tiết | `"Chí Phèo"` |
| `books[].author` | chuỗi | Bảng: cột Tác giả; hộp thoại Chi tiết | `"Nam Cao"` |
| `books[].qty` | số | Bảng: cột Số lượng; hộp thoại Chi tiết | `5` |
| `books[].status` | chuỗi | Cột Trạng thái: "in" → "Còn hàng", "out" → "Hết hàng" | `"in"` |
| `demo.newBook.code` | chuỗi | Hộp thoại Thêm sách: ô Mã sách | `"KS-104"` |
| `demo.newBook.name` | chuỗi | Hộp thoại Thêm sách: ô Tên sách | `"Bỉ vỏ"` |
| `demo.newBook.author` | chuỗi | Hộp thoại Thêm sách: ô Tác giả | `"Nguyên Hồng"` |

## Chỗ gói vẽ khác sản phẩm

- Thanh đầu trang chỉ là thanh giữ chỗ ("giữ như sản phẩm").
- Chân bảng chỉ có dòng "Tổng số: n đầu sách", chưa có dòng "Cập nhật lúc …".
