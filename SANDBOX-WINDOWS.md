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
- `plugins/auto-factory/skills/code/sandbox/fx-sandbox.yaml`: template Lima. Gồm Ubuntu 24.04, Docker, Sysbox 0.7.1,
  image `fx-proxy` (tinyproxy) và image `fx-box` (Node 20, Playwright, Chromium, socat).
- Lệnh: `fx sandbox status | install | reset | clean`, `fx review sandbox <id> --host`.
- Test: `tests/test_isolation.py`. Các class `Real*` (`RealSandboxTest`, `RealJobBoxTest`, `RealJobFlowTest`,
  `RealReviewFlowTest`) chỉ chạy khi có `FX_SANDBOX_REAL=1` và máy ảo thật.

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

Việc này **làm trên máy Windows**, không làm từ Mac: người dùng muốn phần của Windows do máy Windows tự làm và tự thử.
Người dùng đã đồng ý hướng này. Các chi tiết bên dưới là đề xuất: chỗ nào phải chọn thì hỏi người dùng từng câu một.

**Trước khi sửa `fx`:** chạy `git pull`. Từ Mac vẫn đang làm tiếp các phần khác trên cùng nhánh này, và `fx` là một file
lớn. Nên giữ phần WSL thành một khối riêng, đặt cạnh phần "Sandbox" của Lima, để ít đụng nhau.

Các link đã kiểm tra là tải được (ngày 2026-11-21):

- Ubuntu cho WSL:
  - `https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz`
  - Bản arm64 cùng chỗ. Có file `SHA256SUMS` để kiểm tra.
- Sysbox **0.7.1** (không dùng 0.6.7: với Docker 29 và containerd 2 nó báo `namespace {"time" ""} does not exist`,
  đã gặp trên Mac): `https://github.com/nestybox/sysbox/releases/download/v0.7.1/sysbox-ce_0.7.1.linux_amd64.deb`,
  sha256 `9d6d5484f980d0a17f86c492c1262015c2afb66280bdb97215b79fde6a0261c5`. Template Lima cũng dùng link và checksum
  này.

Dùng `--import` cho bản rootfs, không dùng `wsl --install --name`: bản cài kiểu `--install` hỏi tạo user ở lần mở đầu,
fx không chạy tự động được.

- Cách tạo: `wsl.exe --import fx-sandbox <thư mục> <rootfs Ubuntu> --version 2`, gọi từ WSL qua `wsl.exe`.
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
  - **Tắt interop:** sau khi đặt `[interop] enabled=false` trong `fx-sandbox`, WSL chính có còn gọi được
    `cmd.exe /c ver` không? Interop dựa trên `binfmt_misc` của nhân dùng chung, và nếu WSL chính mất interop thì cả
    luồng desktop (build và chạy .exe) hỏng. `fx sandbox install` nên tự kiểm tra điểm này rồi báo.
  - **Sysbox trên nhân WSL2:** Sysbox không ghi là hỗ trợ WSL2. Thử một box `docker run --runtime=sysbox-runc` có
    Docker bên trong. Không chạy được thì báo người dùng và để người dùng chọn hướng khác.
  - **Cổng:** chung namespace mạng thì cổng box mở trên 127.0.0.1 của `fx-sandbox` hiện thẳng trên 127.0.0.1 của WSL
    chính, không cần chuyển tiếp. Kiểm tra lại cho chắc.
  - Nếu những chỗ này không cách ly được, báo người dùng mức rủi ro rồi để người dùng chọn. Đừng tự quyết.

Làm xong thì sửa `install.md` (bước 7) và README (phần sandbox), để máy Windows được hướng dẫn cài bản WSL thay cho
Lima. Bump version nếu người dùng muốn.

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

### Bước 4: thử thật một job (3.8: mỗi job một box)

Từ 3.8, mỗi job (và mỗi bản build thử best-of-N, mỗi gói việc chạy song song) có box riêng trong cùng máy ảo, dùng
suốt đời job. Các quyết định đã chốt với người dùng:

- **J1:** setup, lệnh của builder (`fx run --job`), checks, e2e, seed, đăng nhập, bảng quyền và app đều chạy trong box.
- **J2:** chỉ lệnh cài (`fx setup`, `fx run --job <job> --install -- ...`) được ra registry; mọi lệnh khác không có mạng.
- **J3:** job chờ người (needs-info, approve, pr, handed-off) thì box tạm dừng; xoá clone của job thì xoá box.
- **J4:** không có sandbox thì job dừng trước setup và hỏi. Muốn chạy trên máy thật thì dùng
  `fx sandbox host --job <job> --reason ... --confirm <mã>`, mỗi job hỏi một lần, và PR ghi rõ.
- **J5:** bản build mobile và bản desktop thật vẫn chạy trên máy thật; PR ghi rõ.
- **J6:** chỉ mang về các file git thấy được. Không mang về file bị ignore, symlink, `.git/`, `.claude/`, `.vscode/`,
  `.idea/`, `.devcontainer/`, `.envrc`.

Thử trên máy thật:

1. Chạy `/auto-factory:code init` lại để có deny rule mới. Builder gọi thẳng `npm`/`composer`... phải bị chặn.
2. Chạy một job nhỏ từ đầu đến PR. Xem:
   - `fx setup` tạo box và cài thư viện (`setup-box-*.log` nếu là job cũ).
   - Builder chạy test qua `fx run --job`; lockfile mới về clone sau `--install`.
   - Gate pass. App trong box trả lời trên `127.0.0.1:<port>` của máy thật.
   - Đặt job vào needs-info thì box dừng (`limactl shell fx-sandbox sudo docker ps -a`).
3. Đo RAM của máy ảo khi chạy 2–3 job cùng lúc. Trên Mac đã đo với job web nhỏ (Postgres, app Node, Chromium): mỗi
   box khoảng 130 MB lúc nghỉ, 220 MB lúc chạy Chromium, 3 box cùng lúc đỉnh 1,4 GB trên 6 GB. Trên Windows nên đo
   với một dự án thật (build nặng hơn). Nếu thiếu thì báo người dùng, rồi hỏi họ có muốn nâng `memory` không.
4. Thời gian mỗi lệnh `fx run`: mỗi lệnh gọi `limactl shell` 4 lần (kiểm tra box, đưa file vào, chạy, mang file về).
   Nếu chậm quá thì báo lại.

Một lỗi đã sửa nhưng chưa thử thật: trước đây fx dò cổng của app trước khi bật nó, mà Lima giữ sẵn cổng của box nên fx
báo "port already answers" sai. Giờ app chạy trong box thì fx bỏ bước dò này.

## Những chỗ chưa kiểm chứng

- **Đã chạy thật trên Mac** (Apple Silicon, Lima 2.3.0, máy ảo dựng lại từ đầu bằng template):
  - Các test `Real*` đều qua, gồm cả luồng review và luồng job đầy đủ qua fx: setup cài thư viện thật qua proxy, lệnh
    builder không có mạng và không thấy home, `--install`, app trả lời trên máy thật, gate chạy checks trong box, dừng
    và bật lại box, xoá box khi dọn job.
  - Chưa chạy trên Windows/WSL, và chưa chạy trên máy amd64.
- Box kéo image Docker qua proxy lúc `app.up`, nên danh sách cho phép đang có Docker Hub và kho apt.
- Deny rule của agent chỉ là "làm hết sức".
- Mobile, iOS và bản desktop thật chạy trên máy thật, không cách ly. Đây là quyết định của người dùng.

## Chạy test

Trong WSL, ở thư mục `tests/`:

- Trên máy ảo thật (sau `fx sandbox install`): `FX_SANDBOX_REAL=1 python3 -m unittest test_isolation` chạy cả các
  class `Real*`.

- Từng file: `python3 -m unittest test_isolation`.
- Cả bộ: chạy từng file, rồi lọc dòng `FAIL:`/`ERROR:` trong log. Nếu chạy song song mà một file lỗi vì trùng cổng thì
  chạy riêng file đó lại.
