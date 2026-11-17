"""desktop.py: a desktop app in code and review (fx imports it, next to itself).

A desktop app here is a Tauri 2 app: a web front end (the same one the project may ship as a website) in a window of
the system's web view, with a Rust core the front end calls through Tauri's IPC (`invoke`, events, channels). Its UI
check runs in two layers:

1. In a browser, on any machine (this file and factory-desktop.js): the front end, served by the repository's own
   `app.up` (its dev server), opens in Playwright's Chromium with Tauri's IPC answered by mocks: the project's mocks
   file, the check script's own handlers, and a few of Tauri's plugins built in. Every call is logged, and a command
   with no answer fails the check. On Windows the web view is WebView2, a Chromium, so this is close to the real
   window for everything the front end draws.
2. The real app, built for Windows and driven in WebView2: not yet (the factory runs in WSL and the app on Windows,
   like the Android emulator of a mobile app).

Standard library only.
"""
import json
import os
import re

NAME = "code-factory"   # the factory's folders: ~/.cache/<NAME>, %LOCALAPPDATA%\<NAME> on Windows
FRAMEWORKS = ("tauri",)
CONF_NAMES = ("tauri.conf.json", "tauri.conf.json5", "Tauri.toml")
# Tauri's own default when tauri.conf.json gives the window no size
DEFAULT_WINDOW = (800, 600)


def _read(path, limit=400000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def conf_path(root, dir_=None):
    """The Tauri configuration of a repository: in `dir_` when given (desktop.dir), else src-tauri/, else the root."""
    for d in ([dir_] if dir_ else ["src-tauri", "."]):
        for name in CONF_NAMES:
            p = os.path.join(root, d, name)
            if os.path.isfile(p):
                return p
    return None


def app_kind(root):
    """What kind of desktop app a repository is, from its files: tauri, or None (a web, an API, a mobile app)."""
    if not root or not os.path.isdir(root):
        return None
    if conf_path(root):
        return "tauri"
    try:
        pkg = json.loads(_read(os.path.join(root, "package.json")) or "{}")
    except ValueError:
        pkg = {}
    if isinstance(pkg, dict):
        names = set()
        for k in ("dependencies", "devDependencies"):
            if isinstance(pkg.get(k), dict):
                names |= set(pkg[k])
        if "@tauri-apps/cli" in names:   # the CLI builds an app; @tauri-apps/api alone can be a library's
            return "tauri"
    return None


_BARE = re.compile(r"[A-Za-z_$][\w$]*")


def _json5(text):
    """Enough of JSON5 for a tauri.conf.json5: comments, trailing commas, single-quoted strings and bare keys."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j, buf = i + 1, []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2] if text[j + 1] != "'" else "'")
                    j += 2
                    continue
                buf.append('\\"' if text[j] == '"' else text[j])
                j += 1
            out.append('"%s"' % "".join(buf))
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            m = _BARE.match(text, i)   # at i, without copying the rest of the text
            if m and m.group(0) not in ("true", "false", "null"):
                k = m.end()
                while k < n and text[k] in " \t\r\n":
                    k += 1
                out.append('"%s"' % m.group(0) if k < n and text[k] == ":" else m.group(0))
                i = m.end()
                continue
            out.append(c)
            i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def _load(path):
    text = _read(path)
    if path.endswith(".toml"):
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            return {}
        try:
            return tomllib.loads(text)
        except ValueError:
            return {}
    try:
        return json.loads(text) if path.endswith(".json") else _json5(text)
    except ValueError:
        return {}


def read_conf(root, dir_=None):
    """What the factory needs of a Tauri app's configuration (v2, and v1's names where they differ): its name, version
    and identifier, the main window's size, where its dev server is, and the CSP the window applies. Missing values
    are None; the window falls back to Tauri's default size."""
    path = conf_path(root, dir_)
    c = _load(path) if path else {}
    c = c if isinstance(c, dict) else {}
    pkg = c.get("package") if isinstance(c.get("package"), dict) else {}            # v1
    tv1 = c.get("tauri") if isinstance(c.get("tauri"), dict) else {}                 # v1
    app = c.get("app") if isinstance(c.get("app"), dict) else tv1
    build = c.get("build") if isinstance(c.get("build"), dict) else {}
    version = c.get("version") or pkg.get("version")
    if isinstance(version, str) and version.endswith(".json") and path:
        # v2 can take the version from a package.json, by its path from the configuration's folder
        try:
            version = json.loads(_read(os.path.join(os.path.dirname(path), version)) or "{}").get("version")
        except (ValueError, AttributeError):
            version = None
    wins = [w for w in (app.get("windows") or []) if isinstance(w, dict)]
    main = next((w for w in wins if w.get("label") in (None, "main")), wins[0] if wins else {})

    def size(k, default):
        v = main.get(k)
        return int(v) if isinstance(v, (int, float)) and v > 0 else default
    sec = app.get("security") if isinstance(app.get("security"), dict) else {}
    bundle = c.get("bundle") if isinstance(c.get("bundle"), dict) else (tv1.get("bundle") or {})
    return {
        "conf": os.path.relpath(path, root) if path else None,
        "name": c.get("productName") or pkg.get("productName"),
        "version": version if isinstance(version, str) else None,
        "identifier": c.get("identifier") or (bundle.get("identifier") if isinstance(bundle, dict) else None),
        "window": [size("width", DEFAULT_WINDOW[0]), size("height", DEFAULT_WINDOW[1])],
        "min_window": [size("minWidth", 0), size("minHeight", 0)],
        "title": main.get("title"),
        "dev_url": build.get("devUrl") or build.get("devPath"),
        "before_dev": build.get("beforeDevCommand") if isinstance(build.get("beforeDevCommand"), str) else None,
        "frontend_dist": build.get("frontendDist") or build.get("distDir"),
        "csp": sec.get("csp") if isinstance(sec.get("csp"), (str, dict)) else None,
    }


def check_setup(conf, root):
    """The setup factory-desktop.js reads (FACTORY_UI_DESKTOP): which ways the script runs (`web` too when the front end
    is also shipped as a website), the window size, the app's name and version for the built-in answers, and the
    project's mocks file. Returns (setup, notes)."""
    notes = []
    info = read_conf(root, conf.get("dir"))
    if not info["conf"]:
        notes.append("no tauri.conf.json found%s: the app's name, version and window size are Tauri's defaults"
                     % (" in %s" % conf["dir"] if conf.get("dir") else ""))
    mocks = conf.get("mocks")
    mocks_abs = None
    if mocks:
        p = os.path.realpath(os.path.join(root, mocks))
        if not (p == os.path.realpath(root) or p.startswith(os.path.realpath(root) + os.sep)):
            notes.append("desktop.mocks (%s) is outside the repository: not read" % mocks)
        elif os.path.isfile(p):
            mocks_abs = p
        else:
            notes.append("desktop.mocks (%s) does not exist yet: only the check script's own handlers and the "
                         "built-in ones answer" % mocks)
    vps = conf.get("viewports")
    if not (isinstance(vps, list) and vps and all(isinstance(v, list) and len(v) == 2 for v in vps)):
        vps = [info["window"]]
    variants = ["web", "desktop"] if conf.get("web", True) else ["desktop"]
    setup = {"framework": "tauri", "variants": variants, "viewports": vps, "mocks": mocks_abs,
             "mocks_name": mocks or None, "os": "windows",
             "app": {"name": info["name"] or "app", "version": info["version"] or "0.0.0",
                     "identifier": info["identifier"], "title": info["title"] or info["name"] or "app"},
             "conf": info["conf"]}
    return setup, notes


# =========================================================================== the real app on Windows (layer 2)
#
# The factory runs in WSL; the app is a Windows program. Its build runs on Windows (Rust's MSVC toolchain), from a copy
# of the job's code in a folder of Windows that keeps node_modules and Rust's target between builds; each build is kept
# by commit. A check run starts the .exe with WebView2's DevTools port open (WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS) and
# its own WebView2 data folder, and Playwright, in WSL, connects to it over CDP: straight to 127.0.0.1 when WSL shares
# Windows' network (networkingMode=mirrored); otherwise through a tunnel (factory-desktop.js) that carries each
# connection over the stdin and stdout of a small node.exe on Windows, since WSL's NAT network cannot reach a port
# Windows only opens on 127.0.0.1 and Windows' firewall blocks a listener on its side of that network. Windows reaches
# WSL's localhost in both modes, so the app calls the job's own API at http://127.0.0.1:<port>.
# A Windows program started from WSL outlives the WSL process that started it: the app is stopped by the path of its
# .exe, a copy of the build made for this run alone, and by its WebView2 data folder, never by name.

import base64
import glob
import hashlib
import shutil
import socket
import subprocess
import time
import uuid

try:
    import fcntl
except ImportError:  # Windows without WSL: fx itself refuses to run there
    fcntl = None

# wry's own WebView2 arguments, kept when the variable replaces them
WRY_ARGS = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection"
KEEP_DIRS = ("node_modules", "target", ".svelte-kit", ".next", ".nuxt", ".turbo")   # rebuilt slowly: kept between builds


class DesktopError(Exception):
    """kind: machine (a tool or a setting of this machine is missing: never the job's fault) or build (the app did not
    build from the code under check: the job's)."""

    def __init__(self, msg, kind="machine", **extra):
        super().__init__(msg)
        self.kind = kind
        self.extra = extra


def _run(cmd, timeout=600, env=None, cwd=None):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                              timeout=timeout, env=env, cwd=cwd, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise DesktopError("%s not found: %s" % (cmd[0], e))
    except subprocess.TimeoutExpired:
        raise DesktopError("%s did not answer within %ss" % (os.path.basename(cmd[0]), timeout))


def is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def ps_encoded(script):
    """A PowerShell script as -EncodedCommand takes it: no quoting of the WSL shell, cmd.exe or PowerShell in the way.
    Its progress records are silenced: with its output redirected, PowerShell writes them as CLIXML among the answer."""
    return base64.b64encode(("$ProgressPreference = 'SilentlyContinue'\n" + script).encode("utf-16-le")).decode("ascii")


def ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def stop_script(exe_win, data_win):
    """Stops this run's app and its WebView2 processes: by the .exe made for this run and by the run's WebView2 data
    folder, nothing else."""
    return ("$exe = %s; $data = %s\n"
            "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $exe -or "
            "($_.CommandLine -and $_.CommandLine.Contains($data)) } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n" % (
                ps_quote(exe_win), ps_quote(data_win)))


def cmd_set(name, value):
    """A `set` line of a .cmd file: quoted as a whole, so & | < > ^ stay literal; % doubled."""
    return 'set "%s=%s"' % (name, str(value).replace("%", "%%").replace('"', ""))


def files_to_copy(root, extra=()):
    """What a build of the clone needs, relative to it: what git tracks, what is new and not ignored (a builder checks
    before committing), and the files copied into every clone (copy_into_worktree, such as .env)."""
    p = _run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root, timeout=300)
    if p.returncode != 0:
        raise DesktopError("git ls-files failed in %s: %s" % (root, p.stdout[-300:]))
    files = [f for f in p.stdout.split("\0") if f]
    for pattern in extra or ():
        for f in glob.glob(os.path.join(root, pattern), recursive=True):
            if os.path.isfile(f):
                files.append(os.path.relpath(f, root))
    keep = set()
    for f in files:
        f = f.replace("\\", "/")
        if f.startswith("../") or f.startswith("/") or "/../" in f or not os.path.isfile(os.path.join(root, f)):
            continue
        keep.add(f)
    return sorted(keep)


def sync_tree(root, dest, files):
    """dest becomes the files of root: copied when their size or time differ, and what is no longer in root removed,
    except the folders a build makes slowly (KEEP_DIRS). Returns how many files were copied."""
    os.makedirs(dest, exist_ok=True)
    want = set(files)
    copied = 0
    for f in files:
        s, d = os.path.join(root, f), os.path.join(dest, f)
        try:
            st = os.stat(s)
            dt = os.stat(d)
            if dt.st_size == st.st_size and int(dt.st_mtime) == int(st.st_mtime):
                continue
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)
        copied += 1
    for top, dirs, names in os.walk(dest):
        dirs[:] = [x for x in dirs if x not in KEEP_DIRS and x != ".git"]
        for n in names:
            rel = os.path.relpath(os.path.join(top, n), dest).replace(os.sep, "/")
            if rel not in want and not rel.startswith(".factory-"):
                try:
                    os.remove(os.path.join(top, n))
                except OSError:
                    pass
    return copied


def lock_dir():
    """Where the locks of the builds are: on WSL's own disk (flock on a folder of Windows is not to be relied on)."""
    return os.path.join(os.path.expanduser(os.environ.get("FACTORY_STATE_DIR") or "~/.cache/" + NAME),
                        "desktop-locks")


class _Lock:
    """One build of a repository at a time on this machine (its copy and its target folder are shared). wait_sec 0:
    taken now or DesktopError."""

    def __init__(self, path, wait_sec=3600):
        self.path, self.wait_sec, self.fh = path, wait_sec, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a")
        if fcntl is None:
            return self
        t0 = time.time()
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:   # held by another build; any other error is not a turn to wait for
                if time.time() - t0 >= self.wait_sec:
                    self.fh.close()
                    raise DesktopError("another build of this app is running" if not self.wait_sec else
                                       "another build of this app has held the line for %d min" % (self.wait_sec // 60))
                time.sleep(2)
            except OSError as e:
                self.fh.close()
                raise DesktopError("cannot lock %s: %s" % (self.path, e))

    def __exit__(self, *exc):
        if fcntl is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


class WinHost:
    """Windows, seen from WSL: where the factory keeps the app's builds, and what the machine has to build and run it."""

    def __init__(self):
        self.wsl = is_wsl()
        self._vars = {}

    # -- Windows' side
    def win_var(self, name):
        if name not in self._vars:
            p = _run(["cmd.exe", "/c", "echo %%%s%%" % name], timeout=30, cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)
            lines = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
            val = lines[-1] if lines else ""
            if not val or "%" in val:
                raise DesktopError("cannot read %%%s%% of Windows from WSL" % name)
            self._vars[name] = val
        return self._vars[name]

    def to_wsl(self, win):
        return _run(["wslpath", "-u", win], timeout=30).stdout.strip()

    def winpath(self, path):
        return _run(["wslpath", "-w", path], timeout=30).stdout.strip()

    def home(self):
        """The factory's desktop folder on Windows (a WSL path to it)."""
        d = os.path.join(self.to_wsl(self.win_var("LOCALAPPDATA")), NAME, "desktop")
        os.makedirs(d, exist_ok=True)
        return d

    def ps(self, script, timeout=120):
        return _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", ps_encoded(script)],
                    timeout=timeout, cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)

    def mirrored(self):
        try:
            p = _run(["wslinfo", "--networking-mode"], timeout=10)
            return p.returncode == 0 and "mirrored" in p.stdout.lower()
        except DesktopError:
            return False

    # -- what is there
    def node_exe(self):
        return shutil.which("node.exe")

    def cargo(self):
        try:
            p = os.path.join(self.to_wsl(self.win_var("USERPROFILE")), ".cargo", "bin", "cargo.exe")
        except DesktopError:
            return None
        return p if os.path.isfile(p) else shutil.which("cargo.exe")

    def msvc(self):
        vswhere = "/mnt/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe"
        if not os.path.isfile(vswhere):
            return None
        p = _run([vswhere, "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                  "-property", "installationPath"], timeout=60, cwd="/mnt/c")
        return (p.stdout or "").strip().splitlines()[-1].strip() if p.returncode == 0 and (p.stdout or "").strip() else None

    def webview2(self):
        for top in ("/mnt/c/Program Files (x86)/Microsoft/EdgeWebView/Application",
                    "/mnt/c/Program Files/Microsoft/EdgeWebView/Application"):
            vs = sorted(glob.glob(os.path.join(top, "*", "msedgewebview2.exe")))
            if vs:
                return os.path.basename(os.path.dirname(vs[-1]))
        return None

    def status(self):
        """One row per thing the real app needs on this machine, for fx install and fx desktop doctor."""
        if not self.wsl:
            return [{"name": "windows", "ok": False, "detail": "the real app runs on Windows: run the factory in WSL there",
                     "manual": []}]
        rows = []
        cargo, msvc, wv, node = self.cargo(), self.msvc(), self.webview2(), self.node_exe()
        rows.append({"name": "rust-windows", "ok": bool(cargo), "detail": self.winpath(cargo) if cargo else "not found",
                     "manual": [] if cargo else ["in PowerShell on Windows: winget install --id Rustlang.Rustup -e, then "
                                                 "rustup default stable-msvc"]})
        rows.append({"name": "msvc-build-tools", "ok": bool(msvc), "detail": msvc or "not found",
                     "manual": [] if msvc else [
                         "in PowerShell on Windows (as administrator): winget install --id "
                         "Microsoft.VisualStudio.2022.BuildTools -e --override \"--quiet --wait --add "
                         "Microsoft.VisualStudio.Workload.VCTools --includeRecommended\""]})
        rows.append({"name": "webview2", "ok": bool(wv), "detail": wv or "not found",
                     "manual": [] if wv else ["install the WebView2 Runtime (Evergreen) from "
                                              "https://developer.microsoft.com/microsoft-edge/webview2/"]})
        rows.append({"name": "node-windows", "ok": bool(node), "detail": self.winpath(node) if node else "not found",
                     "manual": [] if node else ["in PowerShell on Windows: winget install --id OpenJS.NodeJS.LTS -e "
                                                "(the app's front end is built on Windows, and with WSL's NAT network "
                                                "the check reaches the app through it)"]})
        rows.append({"name": "wsl-network", "ok": True, "detail": "mirrored: the check reaches the app straight" if
                     self.mirrored() else "NAT: the check reaches the app through Windows' node.exe (networkingMode="
                     "mirrored in %UserProfile%\\.wslconfig reaches it straight)", "manual": []})
        return rows

    def runner(self):
        """How a check reaches the app's DevTools port: ('direct', None), or ('tunnel', why-not or None) through
        node.exe while WSL's network is NAT."""
        if self.mirrored():
            return "direct", None
        if not self.node_exe():
            return "tunnel", "Windows has no Node.js (node.exe), which carries the check to the app while WSL's network is NAT"
        return "tunnel", None

    # -- ports
    def windows_listening(self):
        try:
            out = _run(["netstat.exe", "-an", "-p", "TCP"], timeout=30, cwd="/mnt/c").stdout or ""
        except DesktopError:
            return set()
        ports = set()
        for line in out.splitlines():
            m = re.search(r"[\d.]+:(\d+)\s+\S+\s+LISTENING", line)
            if m:
                ports.add(int(m.group(1)))
        return ports

    def free_port(self, start=9300, span=400):
        """A port free here and on Windows: the app's DevTools port."""
        busy = self.windows_listening()
        for p in range(start, start + span):
            if p in busy:
                continue
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    continue
            return p
        raise DesktopError("no free port from %d on this machine and Windows" % start)

    # -- the build
    def repo_key(self, name):
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:60]

    def build(self, conf, root, key, commit, env_vars, log_path, timeout=3600, extra_files=()):
        """The app built from root (the job's clone) on Windows, kept by commit. Returns the .exe (a WSL path)."""
        command = ((conf.get("build") or {}).get("windows") or "").strip()
        if not command:
            raise DesktopError("desktop.build.windows is not set: the project's command that builds the app on Windows")
        home = self.home()
        builds = os.path.join(home, "builds", key)
        cached = os.path.join(builds, re.sub(r"[^A-Za-z0-9+._-]", "_", commit) + ".exe")
        if os.path.isfile(cached):
            return cached, True
        with _Lock(os.path.join(lock_dir(), key + ".lock")):
            if os.path.isfile(cached):
                return cached, True
            work = os.path.join(home, "src", key)
            copied = sync_tree(root, work, files_to_copy(root, extra_files))
            # rustup's folder first: a Rust installed after this WSL session began is not on the PATH it hands Windows
            script = ["@echo off", "cd /d \"%s\"" % self.winpath(work),
                      'if exist "%USERPROFILE%\\.cargo\\bin" set "PATH=%USERPROFILE%\\.cargo\\bin;%PATH%"']
            script += [cmd_set(k, v) for k, v in sorted(env_vars.items())]
            script += [command, "exit /b %ERRORLEVEL%"]
            bat = os.path.join(work, ".factory-build.cmd")   # kept by sync_tree (.factory-*), gone with the copy
            with open(bat, "w", newline="\r\n") as f:
                f.write("\n".join(script) + "\n")
            t0 = time.time()
            p = _run(["cmd.exe", "/c", self.winpath(bat)], timeout=timeout, cwd="/mnt/c")
            with open(log_path, "w") as f:
                f.write("copied %d files to %s\n%s\nexit %s after %ds\n" % (
                    copied, self.winpath(work), p.stdout or "", p.returncode, time.time() - t0))
            if p.returncode != 0:
                raise DesktopError("the app's build exited with %d (see %s)" % (p.returncode, log_path), kind="build")
            exe = find_artifact(work, conf)
            if not exe:
                raise DesktopError("the build made no .exe where desktop.artifact.windows (%s) says (see %s)" % (
                    (conf.get("artifact") or {}).get("windows") or "src-tauri/target/{debug,release}/*.exe", log_path),
                    kind="build")
            os.makedirs(builds, exist_ok=True)
            shutil.copy2(exe, cached + ".part")
            os.replace(cached + ".part", cached)
            for old in sorted(glob.glob(os.path.join(builds, "*.exe")), key=os.path.getmtime)[:-5]:
                try:
                    os.remove(old)   # five builds of a repository are kept
                except OSError:
                    pass
            return cached, False

    def new_run(self, exe):
        """A folder of Windows for one run: the .exe copied there (so this run's app is found and stopped by its own
        path) and its WebView2 data folder."""
        d = os.path.join(self.home(), "runs", uuid.uuid4().hex[:12])
        os.makedirs(os.path.join(d, "webview"), exist_ok=True)
        dst = os.path.join(d, os.path.basename(exe).split("+")[0].replace(".exe", "") + "-app.exe")
        shutil.copy2(exe, dst)
        return {"dir": d, "exe": dst, "exe_win": self.winpath(dst), "data_win": self.winpath(os.path.join(d, "webview"))}

    def end_run(self, run):
        self.ps(stop_script(run["exe_win"], run["data_win"]), timeout=60)
        for _ in range(10):
            try:
                shutil.rmtree(run["dir"])
                return
            except OSError:
                time.sleep(1)   # Windows lets go of the files a moment after the processes end


def find_artifact(work, conf):
    pattern = (conf.get("artifact") or {}).get("windows")
    if pattern:
        cands = glob.glob(os.path.join(work, pattern), recursive=True)
    else:
        top = conf.get("dir") or "src-tauri"
        cands = []
        for profile in ("debug", "release"):
            cands += glob.glob(os.path.join(work, top, "target", profile, "*.exe"))
    cands = [c for c in cands if os.path.isfile(c) and c.lower().endswith(".exe")]
    return max(cands, key=os.path.getmtime) if cands else None
