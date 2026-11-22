# 3.9.0: chạy thử trên máy Windows

Ghi chú bàn giao cho nhánh `fix/review-fixes` (3.9.0). Đọc hết trước khi làm. **Xoá file này trước khi merge vào
main.**

## Cách làm việc (giữ nguyên trên máy Windows)

- Báo tiến độ bằng tiếng Việt, xong bước nào báo bước đó.
- Việc nào cần người dùng quyết thì giải thích trước, hỏi **từng câu một**, không tự chọn.
- Sửa xong hết mới chạy bộ test **một lần** ở cuối, không chạy sau từng mục.
- Chỉ commit hay push khi người dùng bảo.
- Cài gì vào máy (Lima, bản WSL mới, gh...) đều phải hỏi trước.
- Không đăng issue thật lên GitHub để thử `fx skill-note`: repo này public. Test dùng một `gh` giả.

## Trạng thái khi bàn giao

- Trên macOS: cả bộ test qua (1811 test, 18 bỏ qua vì cần VM sandbox thật).
- Test với VM sandbox thật trên macOS (`FX_SANDBOX_REAL=1`, `test_isolation.py`): đang chạy lúc đẩy nhánh lên; kết quả
  sẽ được ghi thêm vào đây.
- Chưa chạy lần nào trên Windows/WSL.

## 3.9.0 đổi những gì (phần có thể chạy khác trên Windows)

1. **Gate đọc factory từ một git worktree** ở commit nó bắt đầu (`<work>/_factory/<sha12>`, chung cho các gate cùng
   commit, xoá sau 2 ngày). `fx checks` cũng đọc từ đó, nên lần pass của builder được gate dùng lại.
   - Worktree được tạo với `core.autocrlf=false` và `core.eol=lf`: byte đúng như đã commit.
   - Trong worktree, fx tạo **symlink** tới `local/` và tới thư mục của các lane (`lanes[].paths`); file danh sách
     baselines (`baselines.manifest`) và `baseline.json` được copy vào.
   - Script tìm factory qua `$FACTORY_DIR`. File cài đặt sửa dở chưa commit bị bỏ qua và ghi vào `notes`; commit chưa
     push vẫn bị từ chối; code của project ghi vào worktree trong lúc gate chạy thì round bị gạt ra (void).
2. **App trong box** được khởi chạy kiểu mới: `setsid bash -c 'bash -lc <cmd>; echo $? > /run/fx-<key>.exit'`. fx đọc
   file `.exit` để dừng sớm khi `app.up` thoát với mã khác 0.
3. **`fx skill-note`**: thiếu `gh` thì fx tự cài (trong WSL là tải bản phát hành vào thư mục tools của fx, không cần
   sudo); chưa đăng nhập thì trả `ask_user` giải thích lý do và giữ ghi chú lại.
4. **`fx tidy`**: tách file knowledge dài theo `##` (tên file con bỏ dấu tiếng Việt), sửa link, chuyển lịch sử và điểm
   chưa chốt của logic ra `history.md` / `open.md`.

## Việc cần làm trên Windows

Trong Ubuntu của WSL, ở thư mục repo (`git fetch && git checkout fix/review-fixes && git pull`).

1. **Cả bộ test, factory trên ổ của WSL** (thư mục test mặc định là `/tmp`, trên ổ của WSL):
   `python3 -I -m unittest discover -s tests`
2. **Test sandbox thật** (cần VM Lima `fx-sandbox` trong WSL đang chạy; `limactl list` để xem):
   `FX_SANDBOX_REAL=1 python3 -I -m unittest discover -s tests -p "test_isolation.py"`
3. **Factory nằm trên ổ Windows** (`/mnt/c/...`), chỗ symlink dễ hỏng nhất. Bộ test không thử trường hợp này, nên thử
   tay trên một bản sao factory thật, không dùng bản đang làm việc:
   - `git clone <factory> /mnt/c/tmp/factory-copy`, rồi chạy một gate thử
     (`python3 <repo>/plugins/auto-factory/skills/code/fx gate <KEY> --dry-run`) trên một job có sẵn.
   - Xem `<work>/_factory/<sha12>/` có được tạo không, `local` trong đó có phải symlink không, và gate có đọc đúng
     `checks/` không.
   - Nếu WSL không tạo được symlink trên `/mnt/c`: báo người dùng trước khi sửa. Hướng sửa có thể là copy thay cho
     symlink khi `os.symlink` báo lỗi, nhưng `local/` có key nên phải hỏi.
4. **CRLF**: trong bản sao ở bước 3, bật `git config core.autocrlf true` rồi checkout lại một script trong `checks/`;
   gate dry-run phải chạy được script đó (không lỗi `$'\r': command not found`).
5. **`gh` trong WSL** (chỉ khi máy chưa có `gh`, và hỏi người dùng trước): chạy
   `python3 -I -m unittest tests.test_review_fixes.SkillNoteTest` (dùng `gh` giả), rồi xem `fx install` có cài được
   `gh` thật vào thư mục tools không. Không gọi `fx skill-note` thật.

## Khi xong

- Báo người dùng kết quả từng bước. Test nào hỏng: dán output, nói nguyên nhân, sửa trên nhánh này, chạy lại cả bộ
  **một lần**.
- Commit và push chỉ khi người dùng bảo. Trước khi merge vào main: xoá file này.
