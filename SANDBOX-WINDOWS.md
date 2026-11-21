# Sandbox cho review: làm tiếp và thử thật trên máy Windows

Ghi chú bàn giao cho nhánh `fix/review-sandbox-isolation` (3.7.0). Đọc hết trước khi làm. **Xoá file này trước khi
merge vào main.**

## Cách làm việc (giữ nguyên trên máy Windows)

- Báo tiến độ bằng tiếng Việt, xong bước nào báo bước đó.
- Việc nào cần người dùng quyết thì giải thích trước, hỏi **từng câu một**, không tự chọn.
- Sửa xong hết mới chạy bộ test **một lần** ở cuối, không chạy sau từng mục.
- Chỉ commit hay push khi người dùng bảo.
- Không đụng dữ liệu riêng, không commit key hay mật khẩu.
- Cài gì vào máy (Lima, QEMU, bản WSL mới, Sysbox...) đều phải hỏi trước.

## Đã làm trong 3.7.0 (đã commit trên nhánh này)

Nguồn gốc: một bản review bảo mật chỉ ra rằng code của MR mà fx chạy lúc review đọc được `~/.aws`, `~/.ssh` và key S3
của factory, lại có mạng.

Các điểm đã chốt với người dùng:

1. **Phạm vi:** vòng này làm luồng review MR (setup, review.checks, app.up của code chưa review). Các gate của job làm
   ở vòng sau, dùng cùng cơ chế.
2. **Hai lớp:**
   - Mỗi máy có một máy ảo Linux `fx-sandbox` (Lima). Máy ảo không mount gì của máy thật và có Docker riêng.
   - Trong máy ảo, mỗi review là một box Sysbox riêng, có Docker, mạng, database và volume riêng.
   - Box bị xoá khi review xong. Không có cache ghi được nào dùng chung giữa các review.
3. **Web:** build, chạy app, Playwright/Chromium và ZAP đều chạy trong box. Chỉ kết quả (log, ảnh, báo cáo) được copy ra
   ngoài.
4. **Android và iOS:** build và chạy thẳng trên máy thật. API của app chạy trong box và chỉ mở cổng ra 127.0.0.1. Báo
   cáo ghi rõ là không cách ly.
5. **Desktop:**
   - Bản chạy giả (cửa sổ app trong Chromium, các lệnh gọi xuống phần native trả bằng dữ liệu giả) chạy trong box.
   - Bản thật do **Windows** build và chạy.
   - Phần web và API của app chạy trong box.
   - Báo cáo ghi rõ là bản thật không cách ly.
6. **Mạng:**
   - Lúc cài thư viện: chỉ đi qua proxy có danh sách cho phép (registry npm, packagist, PyPI, Maven, Docker Hub...).
     Thêm tên miền qua `sandbox.allow` trong factory.json.
   - Sau đó box không còn internet.
7. **Không có sandbox:**
   - Review dừng trước khi chạy bất cứ thứ gì của MR (`ask-no-sandbox`).
   - Người review có thể chọn `--skip` (không chạy), hoặc `--host` (chạy trên máy thật, không cách ly). Cả hai đều phải
     ghi lý do và nhập mã xác nhận, và lý do được ghi vào Jira.
8. **Bash của agent:** chỉ dùng deny rule của Claude Code. fx init ghi các rule này vào `.claude/settings.local.json`,
   gồm `Read(~/.aws/**)`, `Read(~/.ssh/**)`... Đây là cách "làm hết sức", vẫn lách được.
9. **Giảm thiệt hại:** fx doctor cảnh báo khi profile AWS mặc định dùng key dài hạn (AKIA). README khuyên dùng key S3
   hẹp quyền, ngắn hạn.

Các chỗ chính:

- Trong `plugins/auto-factory/skills/code/fx`:
  - Phần "Sandbox": `sandbox_state`, `vm_run`, `box_create`, `box_cut`, `box_destroy`, `box_put`, `box_get`,
    `box_exec`, `box_spawn`, `cmd_sandbox`.
  - `review_sandbox`, `review_host_waiver`, `review_host_builds`.
- `plugins/auto-factory/skills/code/sandbox/fx-sandbox.yaml`: template Lima. Gồm Ubuntu 24.04, Docker, Sysbox 0.6.7,
  image `fx-proxy` (tinyproxy) và image `fx-box` (Node 20, Playwright, Chromium, socat).
- Lệnh: `fx sandbox status | install | reset | clean`, `fx review sandbox <id> --host`.
- Test: `tests/test_isolation.py`. Class `RealSandboxTest` chỉ chạy khi có `FX_SANDBOX_REAL=1` và máy ảo thật.

Tình trạng test trên Mac (giả lập `limactl`, chưa có máy ảo thật):

- Cả bộ đều qua.
- `test_isolation` và `test_review_sandbox` từng lỗi khi chạy song song với file khác (trùng cổng). Chạy riêng thì qua.
- Test errno-37 trong `test_desktop` treo trên macOS, lỗi này có sẵn từ main.

## Việc còn lại: review app desktop trên máy Windows

**Vấn đề:**

- Bản thật của app desktop chỉ build được khi factory chạy trong WSL trên Windows.
- Sandbox hiện chỉ làm bằng Lima. Trong WSL, nếu không có Lima thì review dừng ở `ask-no-sandbox`.
- Vì vậy người review chỉ còn cách chạy `--host` (không cách ly), hoặc bỏ qua bước chạy.

Người dùng đã đồng ý hướng **"phần của Windows thì để Windows chạy"**. Thử theo thứ tự dưới đây. Mỗi bước có quyết
định thì hỏi người dùng trước.

### Bước 1: thử nguyên đường Lima trong WSL (không phải sửa code)

WSL2 trên Windows 11 thường có ảo hoá lồng (`/dev/kvm`). Nếu có thì Lima chạy được bằng QEMU ngay trong WSL. Đây là
một máy ảo thật, cách ly bằng đúng cơ chế đang dùng trên Mac.

1. Trong WSL: chạy `ls -l /dev/kvm`, rồi `cat /proc/cpuinfo | grep -c -E 'vmx|svm'`.
2. Nếu có `/dev/kvm`:
   - Hỏi người dùng trước. Nếu đồng ý thì cài `qemu-system-x86` (cùng `qemu-utils`) và Lima
     (https://lima-vm.io/docs/installation/).
   - Sau đó chạy `fx sandbox install`, rồi `fx sandbox status`.
3. Nếu không có `/dev/kvm`: thường do `nestedVirtualization` trong `%UserProfile%\.wslconfig` bị tắt, hoặc CPU/Windows
   không hỗ trợ. Báo người dùng, rồi chuyển sang bước 2.

Cần xem kỹ:

- **Lima chuyển cổng:** cổng mà box mở trên 127.0.0.1 của máy ảo phải hiện ra trên 127.0.0.1 của WSL.
- **File .exe trên Windows gọi tới API trong box:**
  - Ở `networkingMode=mirrored`: gọi thẳng.
  - Ở NAT: đi qua `node.exe` (xem `WinHost.runner` trong `desktop.py`).
- **`test -e ~` trong máy ảo phải trả về lỗi:** máy ảo không được thấy home của WSL.
- **Tốc độ:** QEMU lồng trong WSL có thể chậm. Đo thời gian `fx sandbox install` và một lần review.

### Bước 2 (nếu bước 1 không được): một bản WSL2 riêng làm máy ảo

Đây là phương án đã đề xuất với người dùng nhưng **chưa làm**. Phải chốt chi tiết với người dùng trước khi code.

- Cách tạo: `wsl.exe --import fx-sandbox <thư mục> <rootfs Ubuntu>`, gọi từ WSL qua `wsl.exe`.
- `/etc/wsl.conf` của bản này:
  - `[automount] enabled=false`: không gắn ổ C:.
  - `[interop] enabled=false, appendWindowsPath=false`: không gọi được sang Windows.
  - `[boot] systemd=true`.
- Bên trong: Docker, Sysbox, `fx-proxy`, `fx-box` như file `fx-sandbox.yaml`.
- Code:
  - Thêm một "backend" cho sandbox. `vm_run` gọi `wsl.exe -d fx-sandbox -u root -- ...` thay cho `limactl shell`.
  - `sandbox_state` kiểm tra bản WSL đang có, đã sẵn sàng, và không thấy `/mnt/c` cũng như home của WSL chính.
  - `cmd_sandbox install/reset` tạo hoặc xoá bản này.
  - Test giả lập `wsl.exe` giống cách đang giả lập `limactl` trong `tests/test_security.py`.
- **Phải kiểm tra trên máy thật trước khi chốt:**
  - Các bản WSL2 dùng chung một nhân Linux và (theo mặc định) chung namespace mạng. Vậy iptables của box và
    `docker0` trong `fx-sandbox` có đụng vào WSL chính hoặc Docker Desktop không?
  - `/mnt/wsl` là vùng dùng chung giữa các bản WSL: box có thấy gì ở đó không?
  - Nếu những chỗ này không cách ly được, báo người dùng mức rủi ro rồi để người dùng chọn. Đừng tự quyết.

### Bước 3: thử thật một review app desktop

1. Chạy `FX_SANDBOX_REAL=1 python3 -m unittest test_isolation.RealSandboxTest` (trong `tests/`). Test này kiểm tra:
   - Box không đọc được key.
   - Lúc cài thư viện chỉ ra được registry, sau đó không có mạng.
   - Một `postinstall` độc không lấy được gì.
2. Chạy `/auto-factory:review` trên một MR thật của app desktop. Xem:
   - setup, app và checks có chạy trong box không (log của fx).
   - Bản thật có build và chạy được trên Windows không, và có gọi tới API trong box được không.
   - Báo cáo và comment Jira có ghi "bản thật build trên Windows, không cách ly" không.
   - Box có bị xoá sau review không (`fx sandbox status`).
3. Chạy `fx doctor`: có dòng về deny rule, sandbox và key AWS không.

## Những chỗ chưa kiểm chứng

- Chưa từng chạy trên máy ảo thật, kể cả trên Mac.
- Sysbox `0.6.7`: đường tải `downloads.nestybox.com/sysbox/releases/v0.6.7/...` và image
  `nestybox/ubuntu-jammy-systemd-docker` chưa thử.
- Box kéo image Docker qua proxy lúc `app.up`, nên danh sách cho phép đang có Docker Hub và kho apt.
- Deny rule của agent chỉ là "làm hết sức".
- Mobile, iOS và bản desktop thật chạy trên máy thật, không cách ly. Đây là quyết định của người dùng.

## Chạy test

Trong WSL, ở thư mục `tests/`:

- Từng file: `python3 -m unittest test_isolation`.
- Cả bộ: chạy từng file, rồi lọc dòng `FAIL:`/`ERROR:` trong log. Nếu chạy song song mà một file lỗi vì trùng cổng thì
  chạy riêng file đó lại.
