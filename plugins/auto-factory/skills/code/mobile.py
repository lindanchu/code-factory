"""Virtual phones for the factory: what code, review and the auto test run a mobile app on, instead of a browser.

Only the way of running changes. A job, a review and an auto test round go through the same steps as on the web; where
those start a browser, a mobile project starts this:

  devices   iOS simulators (macOS only) and Android emulators the factory makes for itself and keeps ("factory ..."), at
            the screen sizes of a preset the project's owner chose; never a phone of the user's own, which is left as it
            is (a device that was already running stays running).
  build     the app built from the code under test with the project's own commands: an Android App Bundle (the build
            that ships; installed the way Play installs it, through bundletool) and an APK, and an iOS simulator build.
  appium    one Appium server per step, on a port of the step's own, which the tests drive through WebdriverIO.

On Windows the factory runs inside WSL and the Android emulator runs on Windows: every Android and Appium command is
then a Windows program (adb.exe, emulator.exe, node.exe), called from WSL, and the files they read are copied to a
folder of Windows first. iOS needs macOS.

Nothing here is fixed to one machine: the SDK, Xcode, Java and the free ports are looked up when a step runs, and how
many devices run at once follows the room the machine's line gives the step (fx's ledger).
"""
import contextlib
import glob
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

try:
    import fcntl
except ImportError:  # Windows without WSL: fx itself refuses to run there
    fcntl = None

# Versions installed for the factory (fx install), pinned so that every machine drives the phones the same way. They
# are bumped here, with the skill's VERSION.
APPIUM_VERSION = "3.9.0"
UIAUTOMATOR2_VERSION = "8.7.3"
XCUITEST_VERSION = "12.22.8"
WEBDRIVERIO_VERSION = "9.32.0"
BUNDLETOOL_VERSION = "1.18.3"
BUNDLETOOL_URL = "https://github.com/google/bundletool/releases/download/%s/bundletool-all-%s.jar"

PLATFORMS = ("android", "ios")
# The screen classes a preset is made of. iOS: chosen from the iPhones this machine's Xcode offers, by their size in
# points. Android: an emulator is given exactly this size (dp) and density.
ANDROID_SIZES = {"small": (360, 640, 320), "standard": (411, 914, 420), "large": (448, 998, 480)}
PRESETS = {
    # code and review: the two ends of each platform (where a layout breaks first)
    "ends": {"ios": ("small", "large"), "android": ("small", "large")},
    # the auto test, recommended: the two ends and the size most people hold
    "standard": {"ios": ("small", "standard", "large"), "android": ("small", "standard", "large")},
    # every size class, the 375x812 frame most phone designs are drawn in too
    "full": {"ios": ("small", "frame", "standard", "large"), "android": ("small", "standard", "large")},
}
PRESET_INFO = {
    "ends": "the smallest and the largest phone of each platform",
    "standard": "small, common and large phone of each platform",
    "full": "every size class, with the 375x812 frame designs are drawn in",
}
NAME_PREFIX = "factory"   # simulators "factory ios-375x667", emulators factory_android-360x640
BOOT_TIMEOUT_SEC = 420
ANIMATIONS = ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")


class MobileError(Exception):
    def __init__(self, msg, code=3, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


# --------------------------------------------------------------------------- small helpers

def _run(cmd, timeout=600, env=None, cwd=None, check=False):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                           timeout=timeout, env=env, cwd=cwd, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise MobileError("%s is not installed (%s)" % (cmd[0], e)) from None
    except subprocess.TimeoutExpired:
        raise MobileError("%s did not answer within %d s" % (" ".join(map(str, cmd[:3])), timeout)) from None
    except OSError as e:
        raise MobileError("%s could not be started (%s)" % (cmd[0], e)) from None
    if check and p.returncode != 0:
        raise MobileError("%s failed (%d): %s" % (" ".join(map(str, cmd[:3])), p.returncode, (p.stdout or "")[-600:]))
    return p


def cache_dir():
    d = os.path.expanduser(os.environ.get("FACTORY_MOBILE_DIR") or "~/.cache/auto-code-factory/mobile")
    os.makedirs(d, exist_ok=True)
    return d


def _write_json(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def machine_lock(name, wait_sec=900):
    """One step of this machine at a time for `name` (choosing ports and starting what binds them)."""
    if not fcntl:
        yield
        return
    os.makedirs(cache_dir(), exist_ok=True)
    f = open(os.path.join(cache_dir(), "lock-%s" % name), "a+")
    t0 = time.time()
    try:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() - t0 > wait_sec:
                    raise MobileError("another step held %s for %d s" % (name, wait_sec), code=75)
                time.sleep(1)
        yield
    finally:
        f.close()


def windows_listening(host):
    """The ports something listens on, on the Windows side (WSL without mirrored networking: a Linux port that is free
    can be taken there). Empty elsewhere, or when Windows cannot be asked."""
    if not getattr(host, "wsl", False):
        return set()
    try:
        out = _run(["netstat.exe", "-an", "-p", "TCP"], timeout=30).stdout or ""
    except MobileError:
        return set()
    return {int(m) for m in re.findall(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING", out, re.M)}


def pick_port(host, start=4723):
    """A port free here and, on WSL, on the Windows side too."""
    return free_port(start, avoid=windows_listening(host))


def free_port(start=4723, avoid=()):
    for p in range(start, start + 400):
        if p in avoid:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise MobileError("no free port from %d" % start)


def is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


# --------------------------------------------------------------------------- where the tools are on this machine

class Host:
    """The programs this machine drives the phones with. On WSL the Android and Appium side is Windows."""

    def __init__(self, tools_dir):
        self.tools_dir = tools_dir
        self.wsl = is_wsl()
        self.mac = platform.system() == "Darwin"
        self.exe = ".exe" if self.wsl else ""
        self.sdk = self._sdk()
        self.java_home = self._java_home()

    # -- Android SDK
    def _sdk(self):
        cands = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
        if self.wsl:
            for home in sorted(glob.glob("/mnt/c/Users/*/AppData/Local/Android/Sdk")):
                cands.append(home)
        cands += [os.path.expanduser("~/Library/Android/sdk"), os.path.expanduser("~/Android/Sdk"),
                  os.path.expanduser("~/Android/sdk")]
        for c in cands:
            if c and os.path.isdir(os.path.join(c, "platform-tools")):
                return c
        return None

    def sdk_tool(self, rel):
        """A program of the SDK (platform-tools/adb, emulator/emulator, cmdline-tools/latest/bin/avdmanager)."""
        if not self.sdk:
            return None
        if rel.startswith("cmdline-tools/"):
            name, top = rel.split("/")[-1], os.path.join(self.sdk, "cmdline-tools")
            subs = ["latest"] + sorted((d for d in os.listdir(top) if d != "latest"), reverse=True) if os.path.isdir(top) else []
            for d in subs:
                p = os.path.join(top, d, "bin", name + (".bat" if self.wsl else ""))
                if os.path.isfile(p):
                    return p
            return None
        p = os.path.join(self.sdk, rel + self.exe)
        return p if os.path.isfile(p) else None

    @property
    def adb(self):
        return self.sdk_tool("platform-tools/adb")

    @property
    def emulator(self):
        return self.sdk_tool("emulator/emulator")

    # -- Java (bundletool, avdmanager): JAVA_HOME, else the one Android Studio carries
    def _java_home(self):
        cands = [os.environ.get("JAVA_HOME")]
        if self.wsl:
            cands += glob.glob("/mnt/c/Program Files/Android/Android Studio*/jbr")
        cands += ["/Applications/Android Studio.app/Contents/jbr/Contents/Home",
                  os.path.expanduser("~/Applications/Android Studio.app/Contents/jbr/Contents/Home"),
                  os.path.expanduser("~/android-studio/jbr"), "/opt/android-studio/jbr"]
        for c in cands:
            if c and os.path.isfile(os.path.join(c, "bin", "java" + self.exe)):
                return c
        if not self.wsl and shutil.which("java"):
            p = _run(["java", "-version"], timeout=30)
            if p.returncode == 0:  # macOS has a java that only says no Java is installed
                return os.path.dirname(os.path.dirname(os.path.realpath(shutil.which("java"))))
        return None

    @property
    def java(self):
        return os.path.join(self.java_home, "bin", "java" + self.exe) if self.java_home else None

    def java_env(self, env=None):
        env = dict(os.environ if env is None else env)
        if self.java_home and not self.wsl:
            env["JAVA_HOME"] = self.java_home
        if self.sdk and not self.wsl:
            env.setdefault("ANDROID_HOME", self.sdk)
            env.setdefault("ANDROID_SDK_ROOT", self.sdk)
        return env

    # -- the factory's own tools
    @property
    def bundletool(self):
        p = os.path.join(self.tools_dir, "bundletool-%s.jar" % BUNDLETOOL_VERSION)
        return p if os.path.isfile(p) else None

    @property
    def appium_home(self):
        return os.path.join(self.tools_dir, "appium-home")

    @property
    def appium(self):
        p = os.path.join(self.tools_dir, "node_modules", ".bin", "appium")
        return p if os.path.isfile(p) else None

    # -- files a Windows program reads (WSL)
    def win_var(self, name):
        out = _run(["cmd.exe", "/c", "echo %%%s%%" % name], timeout=30).stdout.strip().splitlines()
        val = out[-1].strip() if out else ""
        if not val or "%" in val:
            raise MobileError("cannot read %%%s%% of Windows from WSL" % name)
        return _run(["wslpath", "-u", val], timeout=30).stdout.strip()

    def win_dir(self):
        """A folder on Windows for the files Windows programs read (WSL only)."""
        d = os.path.join(self.win_var("LOCALAPPDATA"), "auto-code-factory", "mobile")
        os.makedirs(d, exist_ok=True)
        return d

    def winpath(self, path):
        return _run(["wslpath", "-w", path], timeout=30).stdout.strip()

    def for_tool(self, path):
        """The path a tool of this host reads `path` at: the same path; on WSL its Windows name, after a copy into a
        folder of Windows when it lives on the Linux side."""
        if not self.wsl:
            return path
        if os.path.abspath(path).startswith("/mnt/"):
            return self.winpath(path)
        dst = os.path.join(self.win_dir(), hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:10] + "-"
                           + os.path.basename(path.rstrip("/")))
        if os.path.isdir(path):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(path, dst)
        else:
            shutil.copy2(path, dst)
        return self.winpath(dst)

    def avd_home(self):
        if os.environ.get("ANDROID_AVD_HOME"):
            return os.environ["ANDROID_AVD_HOME"]
        if self.wsl:
            return os.path.join(self.win_var("USERPROFILE"), ".android", "avd")
        return os.path.expanduser("~/.android/avd")

    def connect_host(self):
        """Where Appium on this host answers: 127.0.0.1, or on WSL without mirrored networking the Windows side."""
        if not self.wsl or wsl_mirrored():
            return "127.0.0.1"
        r = _run(["ip", "route", "show", "default"], timeout=10).stdout.split()
        return r[2] if len(r) > 2 else "127.0.0.1"

    # -- what is there
    def status(self, platforms=PLATFORMS):
        """One row per thing a mobile project needs, for fx install: name, ok, detail, how to get it."""
        rows = []
        if "android" in platforms:
            rows.append({"name": "android-sdk", "ok": bool(self.adb and self.emulator),
                         "detail": self.sdk or "not found",
                         "manual": [] if self.adb and self.emulator else [
                             "install Android Studio%s and, in its SDK Manager, the Android SDK Platform-Tools, the "
                             "Android Emulator, the Android SDK Command-line Tools and one Google APIs system image"
                             % (" on Windows" if self.wsl else "")]})
            rows.append({"name": "android-system-image", "ok": bool(self.system_images()),
                         "detail": ", ".join(self.system_images()[:3]) or "none installed",
                         "manual": [] if self.system_images() else [
                             "%s \"system-images;android-35;google_apis;%s\"" % (
                                 self.sdk_tool("cmdline-tools/latest/bin/sdkmanager") or "sdkmanager", host_abi())]})
            rows.append({"name": "java", "ok": bool(self.java), "detail": self.java_home or "not found",
                         "manual": [] if self.java else ["install Android Studio (it carries a Java runtime) or a JDK 17+"]})
            rows.append({"name": "bundletool", "ok": bool(self.bundletool), "detail": self.bundletool or "not installed",
                         "auto": None if self.bundletool else "download"})
        if "ios" in platforms:
            ok = self.mac and _run(["xcrun", "simctl", "help"], timeout=60).returncode == 0 if self.mac else False
            rows.append({"name": "xcode-simulators", "ok": ok,
                         "detail": "" if ok else ("iOS simulators need macOS" if not self.mac else "Xcode not found"),
                         "manual": [] if ok or not self.mac else ["install Xcode from the App Store, open it once, then "
                                                                  "xcode-select -s /Applications/Xcode.app"]})
        return rows

    # -- Android system images installed
    def system_images(self):
        if not self.sdk:
            return []
        out = []
        for d in sorted(glob.glob(os.path.join(self.sdk, "system-images", "android-*", "*", "*")), reverse=True):
            api, tag, abi = d.split(os.sep)[-3:]
            if os.path.isfile(os.path.join(d, "system.img")) or os.path.isdir(d):
                out.append("system-images;%s;%s;%s" % (api, tag, abi))
        # newest API first, Google APIs (no Play Store: it can be driven and has WebView updates) before the others
        out.sort(key=lambda s: (-int(re.sub(r"\D", "", s.split(";")[1]) or 0), "google_apis" != s.split(";")[2],
                                "playstore" in s))
        return [s for s in out if s.split(";")[3] == host_abi()] or out


def host_abi():
    m = platform.machine().lower()
    return "arm64-v8a" if m in ("arm64", "aarch64") else "x86_64"


def wsl_mirrored():
    try:
        p = _run(["wslinfo", "--networking-mode"], timeout=10)
        return p.returncode == 0 and "mirrored" in p.stdout.lower()
    except MobileError:
        return False


# --------------------------------------------------------------------------- the devices of a preset

def ios_device_types():
    """The iPhones this Xcode offers: [{id, name, w, h (points), scale}] from each type's own screen description."""
    p = _run(["xcrun", "simctl", "list", "devicetypes", "-j"], timeout=120)
    if p.returncode != 0:
        return []
    out = []
    for t in json.loads(p.stdout).get("devicetypes", []):
        if t.get("productFamily") != "iPhone":
            continue
        cap = os.path.join(t.get("bundlePath", ""), "Contents", "Resources", "capabilities.plist")
        try:
            with open(cap, "rb") as f:
                sd = (plistlib.load(f).get("capabilities") or {}).get("ScreenDimensionsCapability") or {}
            scale = float(sd["main-screen-scale"])
            w, h = int(round(sd["main-screen-width"] / scale)), int(round(sd["main-screen-height"] / scale))
        except (OSError, KeyError, ValueError, TypeError, plistlib.InvalidFileException):
            continue
        out.append({"id": t["identifier"], "name": t["name"], "w": w, "h": h, "scale": scale,
                    "min": t.get("minRuntimeVersionString") or "0", "max": t.get("maxRuntimeVersionString") or "999"})
    return out


def _vt(s):
    return tuple(int(x) for x in re.findall(r"\d+", str(s))[:3])


def pick_ios(types, classes, runtime_version=None):
    """The iPhone of each size class among `types`: small = the smallest screen, large = the largest, standard = the
    commonest width (closest to 393 pt), frame = closest to the 375x812 frame designs are drawn in. Types the runtime
    cannot run are left out; one type is used once."""
    if runtime_version:
        rv = _vt(runtime_version)
        types = [t for t in types if _vt(t["min"]) <= rv <= _vt(t["max"])]
    if not types:
        return []
    area = lambda t: t["w"] * t["h"]
    # among types of the same size, the newest name wins (a higher number in the name)
    newest = lambda t: _vt(t["name"])
    want = {"small": lambda t: (area(t), [-x for x in newest(t)]),
            "large": lambda t: (-area(t), [-x for x in newest(t)]),
            "standard": lambda t: (abs(t["w"] - 393) + abs(t["h"] - 852) / 4, [-x for x in newest(t)]),
            "frame": lambda t: (abs(t["w"] - 375) + abs(t["h"] - 812), [-x for x in newest(t)])}
    out, used = [], set()
    for c in classes:
        pool = [t for t in types if (t["w"], t["h"]) not in used]
        if not pool:
            break
        t = min(pool, key=want[c])
        used.add((t["w"], t["h"]))
        out.append(dict(t, size_class=c))
    return out


def android_api(v):
    """24 from "24", "android-24", "system-images;android-24;google_apis;arm64-v8a" or "Android 7"."""
    m = re.search(r"android-(\d+)", str(v)) or re.match(r"\s*(\d+)\s*$", str(v))
    return int(m.group(1)) if m else None


ANDROID_RELEASE = {21: "5.0", 22: "5.1", 23: "6", 24: "7.0", 25: "7.1", 26: "8.0", 27: "8.1", 28: "9", 29: "10",
                   30: "11", 31: "12", 32: "12L", 33: "13", 34: "14", 35: "15", 36: "16", 37: "17"}


def android_images(host, installable=True):
    """The Google APIs system images of this machine's ABI: installed ones, and (installable) the ones the SDK manager
    offers, read once a day. [{api, package, installed}], oldest first."""
    have = {android_api(s): s for s in host.system_images() if s.split(";")[2] == "google_apis"}
    offered = {}
    if installable:
        cache = os.path.join(cache_dir(), "android-images.json")
        got = _read_json(cache, {})
        if not got or time.time() - got.get("at", 0) > 86400:
            sdkm = host.sdk_tool("cmdline-tools/latest/bin/sdkmanager")
            pk = []
            if sdkm and host.java_home:
                try:
                    p = _run([sdkm, "--list"], timeout=300, env=host.java_env())
                    pk = sorted(set(re.findall(r"system-images;android-\d+;google_apis;%s" % re.escape(host_abi()), p.stdout)))
                except MobileError:
                    pk = []   # offline: only what is installed; asked again in an hour, not at every step
            got = {"at": time.time() - (0 if pk else 86400 - 3600), "packages": pk}
            _write_json(cache, got)
        offered = {android_api(s): s for s in got.get("packages") or []}
    apis = sorted(set(have) | set(offered))
    return [{"api": a, "package": have.get(a) or offered[a], "installed": a in have} for a in apis if a]


def ios_runtimes():
    """The iOS runtimes this Xcode has installed: [{id, name, version}], oldest first."""
    p = _run(["xcrun", "simctl", "list", "runtimes", "-j"], timeout=120)
    if p.returncode != 0:
        return []
    rts = [r for r in json.loads(p.stdout).get("runtimes", []) if r.get("isAvailable") and
           (r.get("platform") == "iOS" or str(r.get("identifier", "")).startswith("com.apple.CoreSimulator.SimRuntime.iOS"))]
    out = [{"id": r["identifier"], "name": r.get("name") or "iOS " + r.get("version", ""), "version": r.get("version", "")}
           for r in rts]
    return sorted(out, key=lambda r: _vt(r["version"]))


def pick_versions(avail, low=None, count=3):
    """From versions (oldest first, each a tuple or number) the oldest at or above `low`, the newest and, when count is 3,
    the one nearest the middle between them. Returns their indexes in `avail`, oldest first."""
    key = lambda v: v if isinstance(v, tuple) else (v,)
    ok = [i for i, v in enumerate(avail) if low is None or key(v) >= key(low)]
    if not ok:
        return []
    old, new = ok[0], ok[-1]
    picked = [old, new] if old != new else [old]
    if count >= 3 and len(ok) > 2:
        mid_val = (key(avail[old])[0] + key(avail[new])[0]) / 2
        mid = min((i for i in ok if i not in (old, new)), key=lambda i: abs(key(avail[i])[0] - mid_val))
        picked.insert(1, mid)
    return sorted(set(picked))


def versions_for(host, platform_, conf, count=3):
    """The OS versions a project runs on: the ones its mobile block names (versions.<platform>), else the oldest the
    app supports (min_os.<platform>) that this machine can run, the newest, and one in the middle. Returns
    ([{version, label, os, ...}], notes), oldest first."""
    conf = conf or {}
    want = (conf.get("versions") or {}).get(platform_)
    low = (conf.get("min_os") or {}).get(platform_)
    notes = []
    if platform_ == "android":
        imgs = android_images(host)
        if want:
            apis = {android_api(v) for v in want}
            chosen = [i for i in imgs if i["api"] in apis]
            missing = sorted(apis - {i["api"] for i in chosen})
            if missing:
                notes.append("android: no system image offered for API %s" % ", ".join(map(str, missing)))
        else:
            idx = pick_versions([i["api"] for i in imgs], android_api(low) if low else None, count)
            chosen = [imgs[i] for i in idx]
            if not low:
                notes.append("android: the oldest version the app supports is not set (mobile.min_os.android): the "
                             "oldest image offered is taken")
        return [{"version": "api%d" % i["api"], "api": i["api"], "image": i["package"], "installed": i["installed"],
                 "os": "Android %s" % ANDROID_RELEASE.get(i["api"], "API %d" % i["api"])} for i in chosen], notes
    rts = ios_runtimes()
    if want:
        chosen = [r for r in rts if any(r["version"] == str(w) or r["version"].startswith(str(w) + ".") for w in want)]
        missing = [w for w in want if not any(r["version"] == str(w) or r["version"].startswith(str(w) + ".") for r in rts)]
        if missing:
            notes.append("ios: the simulator runtime of iOS %s is not installed: download it in Xcode (Settings > "
                         "Components) or with `xcodebuild -downloadPlatform iOS -buildVersion <version>`" % ", ".join(map(str, missing)))
    else:
        idx = pick_versions([_vt(r["version"]) for r in rts], _vt(low) if low else None, count)
        chosen = [rts[i] for i in idx]
        if low and rts and _vt(rts[0]["version"]) > _vt(low):
            notes.append("ios: the app supports iOS %s, the oldest simulator runtime here is %s: download an older one in "
                         "Xcode (Settings > Components) to test the old end" % (low, rts[0]["version"]))
        if len(chosen) < count:
            notes.append("ios: only %d iOS version(s) installed (%s)" % (len(chosen), ", ".join(r["version"] for r in chosen) or "none"))
    return [{"version": r["version"], "runtime": r["id"], "os": r["name"], "installed": True} for r in chosen], notes


def plan_devices(host, preset, platforms, conf=None, matrix="cross"):
    """The phones of `preset` on this machine: [{key, platform, name, os, screen, version, ...how to make it}].
    matrix "cross": every size class on every version (the auto test). "ends": the smallest phone on the oldest version
    and the largest on the newest, per platform (code and review)."""
    if preset not in PRESETS:
        raise MobileError("unknown device preset %r (%s)" % (preset, ", ".join(PRESETS)), code=2)
    devs, notes = [], []
    for plat in [p for p in PLATFORMS if p in platforms]:
        if plat == "ios" and not host.mac:
            continue
        vers, more = versions_for(host, plat, conf)
        notes += more
        classes = PRESETS[preset][plat]
        pairs = ([(v, c) for v in vers for c in classes] if matrix == "cross" else
                 ([(vers[0], classes[0])] + ([(vers[-1], classes[-1])] if len(vers) > 1 or len(classes) > 1 else []))
                 if vers else [])
        for rank, (v, c) in ((vers.index(v), (v, c)) for v, c in pairs):
            if plat == "ios":
                t = pick_ios(ios_device_types(), (c,), v["version"])
                if not t:
                    continue
                t = t[0]
                devs.append({"key": "ios-%dx%d-%s" % (t["w"], t["h"], v["version"]), "platform": "ios", "name": t["name"],
                             "type": t["id"], "runtime": v["runtime"], "os": v["os"], "version": v["version"],
                             "screen": "%dx%d pt" % (t["w"], t["h"]), "size": [t["w"], t["h"]], "size_class": c,
                             "ver_rank": rank})
            else:
                w, h, dpi = ANDROID_SIZES[c]
                devs.append({"key": "android-%dx%d-%s" % (w, h, v["version"]), "platform": "android",
                             "name": "Android %dx%d" % (w, h), "image": v["image"], "image_installed": v["installed"],
                             "lcd": [int(round(w * dpi / 160)), int(round(h * dpi / 160)), dpi], "os": v["os"],
                             "version": v["version"], "screen": "%dx%d dp" % (w, h), "size": [w, h], "size_class": c,
                             "ver_rank": rank})
    seen, out = set(), []
    for d in devs:
        if d["key"] not in seen:
            seen.add(d["key"])
            out.append(d)
    plan_devices.notes = notes
    return out


plan_devices.notes = []


def ends_of(devices):
    """Per platform, the smallest phone of the oldest version and the largest of the newest (what code and review run
    on, out of a whole matrix)."""
    out = []
    area = lambda d: (d.get("size") or [0, 0])[0] * (d.get("size") or [0, 0])[1]
    for p in PLATFORMS:
        ds = [d for d in devices if d.get("platform") == p]
        if not ds:
            continue
        lo_r, hi_r = min(d.get("ver_rank", 0) for d in ds), max(d.get("ver_rank", 0) for d in ds)
        lo = min((d for d in ds if d.get("ver_rank", 0) == lo_r), key=area)
        hi = max((d for d in ds if d.get("ver_rank", 0) == hi_r), key=area)
        out += [lo] if lo is hi else [lo, hi]
    return out


def install_image(host, package):
    """An Android system image installed with the SDK manager (no administrator rights; about 1-2 GB). Its licence is
    accepted as the user agreed when they chose to install it."""
    sdkm = host.sdk_tool("cmdline-tools/latest/bin/sdkmanager")
    if not sdkm or not host.java_home:
        raise MobileError("the Android SDK Command-line Tools and Java are needed to install %s" % package)
    p = subprocess.run([sdkm, "--install", package], input="y\n" * 20, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace", timeout=3600, env=host.java_env())
    if p.returncode != 0:
        raise MobileError("could not install %s: %s" % (package, (p.stdout or "")[-500:]))
    return package


# --------------------------------------------------------------------------- making and starting a device

def sim_name(dev):
    return "%s %s" % (NAME_PREFIX, dev["key"])


def avd_name(dev):
    return "%s_%s" % (NAME_PREFIX, re.sub(r"[^A-Za-z0-9_.-]", "_", dev["key"]))


def ios_find(dev):
    """{udid, state} of the factory's simulator of this device, or None."""
    p = _run(["xcrun", "simctl", "list", "devices", "-j"], timeout=120)
    if p.returncode != 0:
        return None
    for rt, ds in json.loads(p.stdout).get("devices", {}).items():
        for d in ds:
            if d.get("name") == sim_name(dev) and d.get("isAvailable", True) and (not dev.get("runtime") or rt == dev["runtime"]):
                return {"udid": d["udid"], "state": d.get("state")}
    return None


def ensure_device(host, dev, install_images=False):
    """Make the factory's own simulator or emulator of `dev` when it is not there yet. Returns what was done. An
    Android version whose system image is not installed is installed only when allowed (it is a large download)."""
    if dev["platform"] == "ios":
        if not host.mac:
            raise MobileError("%s is an iOS simulator: it needs macOS" % dev["key"])
        if ios_find(dev):
            return "kept"
        _run(["xcrun", "simctl", "create", sim_name(dev), dev["type"], dev["runtime"]], timeout=300, check=True)
        return "made"
    avdm = host.sdk_tool("cmdline-tools/latest/bin/avdmanager")
    ini = os.path.join(host.avd_home(), avd_name(dev) + ".avd", "config.ini")
    if os.path.isfile(ini):
        return "kept"
    if not avdm:
        raise MobileError("the Android SDK Command-line Tools are missing (avdmanager): install them in Android "
                          "Studio's SDK Manager")
    if dev["image"] not in host.system_images():
        if not install_images:
            raise MobileError("the Android system image %s (%s) is not installed" % (dev["image"], dev.get("os")),
                              code=2, missing_image=dev["image"])
        install_image(host, dev["image"])
    p = subprocess.run([avdm, "create", "avd", "-n", avd_name(dev), "-k", dev["image"], "-d", "pixel", "--force"],
                       input="no\n", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                       timeout=300, env=host.java_env())
    if p.returncode != 0:
        raise MobileError("could not make the emulator %s: %s" % (avd_name(dev), (p.stdout or "")[-500:]))
    if os.path.isfile(ini):
        w, h, dpi = dev["lcd"]
        set_ini(ini, {"hw.lcd.width": w, "hw.lcd.height": h, "hw.lcd.density": dpi, "hw.keyboard": "yes",
                      "hw.ramSize": 2048, "disk.dataPartition.size": "6G", "showDeviceFrame": "no",
                      "hw.initialOrientation": "portrait"})
    return "made"


def set_ini(path, values):
    lines, seen = [], set()
    with open(path) as f:
        for line in f.read().splitlines():
            k = line.split("=", 1)[0].strip()
            if k in values:
                lines.append("%s = %s" % (k, values[k]))
                seen.add(k)
            else:
                lines.append(line)
    lines += ["%s = %s" % (k, v) for k, v in values.items() if k not in seen]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def adb_devices(host):
    p = _run([host.adb, "devices"], timeout=60)
    return [ln.split()[0] for ln in p.stdout.splitlines()[1:] if ln.strip().endswith("device")]


def adb_avd_of(host, serial):
    p = _run([host.adb, "-s", serial, "emu", "avd", "name"], timeout=30)
    return (p.stdout.splitlines() or [""])[0].strip() if p.returncode == 0 else ""


@contextlib.contextmanager
def device_lock(dev, wait_sec=3600):
    """One step at a time on a device: two jobs installing and clearing one app on one phone would test each other."""
    if not fcntl:
        yield
        return
    path = os.path.join(cache_dir(), "lock-%s" % re.sub(r"[^A-Za-z0-9_.-]", "_", dev["key"]))
    f = open(path, "a+")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() - t0 > wait_sec:
                f.close()
                raise MobileError("the device %s is used by another step for over %d minutes" % (dev["key"], wait_sec // 60),
                                  code=75)
            time.sleep(2)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def boot(host, dev, log_dir, window=False):
    """Start the device (or take it as it runs). Returns its handle: {key, platform, udid | serial, booted}. A device
    this step started is stopped by shutdown(); one that was running before is left running.
    window: the user asked to watch it (asked for each ticket): the emulator gets its window, the simulator is shown
    in Simulator. Otherwise nothing opens on the screen (command line only)."""
    os.makedirs(log_dir, exist_ok=True)
    if dev["platform"] == "ios":
        found = ios_find(dev)
        if not found:
            raise MobileError("the simulator %s is missing: run fx mobile devices --make" % sim_name(dev))
        booted = found["state"] != "Booted"
        try:
            if booted:
                _run(["xcrun", "simctl", "boot", found["udid"]], timeout=300)
            p = _run(["xcrun", "simctl", "bootstatus", found["udid"], "-b"], timeout=BOOT_TIMEOUT_SEC)
            if p.returncode != 0:
                raise MobileError("the simulator %s did not start: %s" % (sim_name(dev), p.stdout[-400:]))
        except BaseException:
            if booted:   # started by this step: not left running
                try:
                    _run(["xcrun", "simctl", "shutdown", found["udid"]], timeout=180)
                except MobileError:
                    pass
            raise
        if window and host.mac:
            # Simulator shows the booted device; it is left open afterwards (the user may use it), the device is shut
            # down as always
            _run(["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", found["udid"]], timeout=60)
        return {"key": dev["key"], "platform": "ios", "udid": found["udid"], "booted": booted, "name": sim_name(dev)}
    name = avd_name(dev)
    for serial in adb_devices(host):
        if serial.startswith("emulator-") and adb_avd_of(host, serial) == name:
            settle_android(host, serial)
            return {"key": dev["key"], "platform": "android", "serial": serial, "booted": False, "name": name}
    log = open(os.path.join(log_dir, "emulator-%s.log" % dev["key"]), "ab")
    # the console port is chosen and taken under one lock of the machine (two steps never start on one port), and the
    # phone on it is this step's only when it is this AVD
    with machine_lock("emulator-port"):
        taken = {int(s.split("-")[1]) for s in adb_devices(host) if s.startswith("emulator-")}
        busy = windows_listening(host)
        port = next(p for p in range(5554, 5682, 2) if p not in taken and p + 1 not in taken and p not in busy
                    and p + 1 not in busy and _port_free(p) and _port_free(p + 1))
        proc = subprocess.Popen(emulator_args(host, name, port, window), stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=host.java_env())
        serial = "emulator-%d" % port
        t0 = time.time()
        while time.time() - t0 < 90 and proc.poll() is None and serial not in adb_devices(host):
            time.sleep(1)
    try:
        t0 = time.time()
        while time.time() - t0 < BOOT_TIMEOUT_SEC:
            if proc.poll() is not None:
                raise MobileError("the emulator %s stopped while starting; see %s" % (name, log.name))
            p = _run([host.adb, "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=30)
            if p.stdout.strip() == "1":
                if adb_avd_of(host, serial) not in (name, ""):
                    raise MobileError("%s is another emulator than %s: not this step's" % (serial, name))
                break
            time.sleep(3)
        else:
            raise MobileError("the emulator %s did not start within %d s; see %s" % (name, BOOT_TIMEOUT_SEC, log.name))
        settle_android(host, serial)
    except BaseException:
        # started by this step: stopped, through its own process (never by a serial another step may hold); on WSL
        # the emulator is a Windows program, so its console is told too, when the phone there is this AVD
        try:
            if serial in adb_devices(host) and adb_avd_of(host, serial) == name:
                _run([host.adb, "-s", serial, "emu", "kill"], timeout=30)
        except MobileError:
            pass
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    return {"key": dev["key"], "platform": "android", "serial": serial, "booted": True, "name": name, "pid": proc.pid}


def emulator_args(host, name, port, window=False):
    """The emulator's command: no window unless the user asked to watch it."""
    return [host.emulator, "-avd", name, "-port", str(port)] + ([] if window else ["-no-window"]) + [
        "-no-audio", "-no-boot-anim", "-no-snapshot-save", "-gpu", "auto"]


def _port_free(p):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) != 0


def settle_android(host, serial):
    """No animations (a screenshot taken mid-animation is not the screen), and the screen stays on."""
    for k in ANIMATIONS:
        _run([host.adb, "-s", serial, "shell", "settings", "put", "global", k, "0"], timeout=30)
    _run([host.adb, "-s", serial, "shell", "svc", "power", "stayon", "true"], timeout=30)
    repair_agent(host, serial)


UIA2 = ("io.appium.uiautomator2.server", "io.appium.uiautomator2.server.test")


def repair_agent(host, serial):
    """Appium's UiAutomator2 server is two packages. A session stopped while they were being set up can leave one of
    them half removed (listed only with its data kept): Appium sees its version and does not install it again, and
    every session on that phone then fails ("Unable to find instrumentation target package"). Both are removed so the
    next session installs them whole. Returns True when it did."""
    p = _run([host.adb, "-s", serial, "shell", "pm", "list", "packages"], timeout=60)
    have = {line.split(":", 1)[1].strip() for line in (p.stdout or "").splitlines() if line.startswith("package:")}
    present = [x for x in UIA2 if x in have]
    if len(present) == len(UIA2):
        return False
    q = _run([host.adb, "-s", serial, "shell", "pm", "list", "packages", "-u"], timeout=60)
    kept = {line.split(":", 1)[1].strip() for line in (q.stdout or "").splitlines() if line.startswith("package:")}
    if not present and not any(x in kept for x in UIA2):
        return False   # never installed on this phone: Appium installs it
    for x in UIA2:
        _run([host.adb, "-s", serial, "uninstall", x], timeout=120)
    return True


def stop_wda(host, udid):
    """The WebDriverAgent build Appium started for a simulator (xcodebuild ... -destination id=<udid>, of the factory's
    own Appium): it runs in its own process group, so stopping Appium leaves it, and it boots the simulator again.
    Only that one is stopped: a WebDriverAgent of anything else on this machine is left."""
    if not host.mac or not udid:
        return 0
    p = _run(["ps", "-axo", "pid=,command="], timeout=30)
    mine = [int(line.split(None, 1)[0]) for line in (p.stdout or "").splitlines()
            if "xcodebuild" in line and "id=" + udid in line and host.appium_home in line]
    for pid in mine:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    t0 = time.time()
    while mine and time.time() - t0 < 20:
        mine = [pid for pid in mine if _alive(pid)]
        time.sleep(0.5)
    for pid in mine:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    return len(mine)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def shutdown(host, h):
    """Stop a device this step started; leave one that was running before."""
    if not h.get("booted"):
        return False
    if h["platform"] == "ios":
        _run(["xcrun", "simctl", "shutdown", h["udid"]], timeout=180)
    else:
        _run([host.adb, "-s", h["serial"], "emu", "kill"], timeout=60)
        t0 = time.time()
        while time.time() - t0 < 60 and h["serial"] in adb_devices(host):
            time.sleep(1)
    return True


# --------------------------------------------------------------------------- the app under test

def build(cmd, cwd, env, artifact, out_dir, key, timeout=3600):
    """Build one artifact with the project's own command, once per `key` (the commit and what the app is pointed at).
    `artifact` is a path or a glob relative to `cwd`. Returns the kept copy's path."""
    os.makedirs(out_dir, exist_ok=True)
    keep = os.path.join(out_dir, hashlib.sha1(key.encode()).hexdigest()[:12])
    done = _read_json(os.path.join(keep, "build.json"), {})
    if done.get("key") == key and os.path.exists(done.get("file", "")):
        return done["file"], True
    shutil.rmtree(keep, ignore_errors=True)
    os.makedirs(keep)
    log = os.path.join(keep, "build.log")
    with open(log, "wb") as fh:
        try:
            p = subprocess.run(["bash", "-lc", cmd], cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise MobileError("the build did not finish within %d s; see %s" % (timeout, log), log=log) from None
    if p.returncode != 0:
        with open(log, errors="replace") as f:
            tail = "\n".join(f.read().splitlines()[-25:])
        raise MobileError("the build failed (%d); last lines of %s:\n%s" % (p.returncode, log, tail), log=log)
    hits = sorted(glob.glob(os.path.join(cwd, artifact)), key=os.path.getmtime)
    if not hits:
        raise MobileError("the build finished but %s is not there" % artifact, log=log)
    src = hits[-1]
    dst = os.path.join(keep, os.path.basename(src.rstrip("/")))
    (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
    _write_json(os.path.join(keep, "build.json"), {"key": key, "file": dst, "built": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return dst, False


def debug_keystore(host):
    """The debug key bundletool signs the installable APKs with (an AAB is not signed for one phone): the one Android
    tooling keeps in ~/.android (of Windows, on WSL), made when missing."""
    ks = os.path.join(os.path.dirname(host.avd_home()), "debug.keystore")
    if not os.path.isfile(ks):
        keytool = os.path.join(host.java_home, "bin", "keytool" + host.exe) if host.java_home else None
        if not keytool or not os.path.isfile(keytool):
            raise MobileError("no Java keytool to make the debug key with (install Android Studio or a JDK)")
        os.makedirs(os.path.dirname(ks), exist_ok=True)
        _run([keytool, "-genkeypair", "-keystore", host.for_tool(ks), "-storepass", "android",
              "-alias", "androiddebugkey", "-keypass", "android", "-keyalg", "RSA", "-validity", "10000",
              "-dname", "CN=Android Debug,O=Android,C=US"], timeout=120, check=True)
    return ks


def install(host, h, artifact, app_id, work_dir):
    """Install the app on a started device, replacing an earlier install. An .aab goes through bundletool, for this
    phone exactly (its ABI, density and language), the way Play installs it."""
    if h["platform"] == "ios":
        _run(["xcrun", "simctl", "uninstall", h["udid"], app_id], timeout=180)
        p = _run(["xcrun", "simctl", "install", h["udid"], artifact], timeout=600)
        if p.returncode != 0:
            raise MobileError("could not install the app on %s: %s" % (h["key"], p.stdout[-400:]))
        return "installed"
    if app_id:
        _run([host.adb, "-s", h["serial"], "uninstall", app_id], timeout=180)
    if artifact.endswith(".aab"):
        if not host.bundletool or not host.java:
            raise MobileError("an .aab is installed with bundletool and Java: run /auto-factory:code install")
        apks = os.path.join(host.win_dir() if host.wsl else work_dir, "%s.apks" % h["key"])
        os.makedirs(os.path.dirname(apks), exist_ok=True)
        ks = debug_keystore(host)
        T = host.for_tool
        p = _run([host.java, "-jar", T(host.bundletool), "build-apks", "--bundle", T(artifact), "--output",
                  T(apks) if not host.wsl else host.winpath(apks), "--overwrite", "--connected-device",
                  "--device-id", h["serial"], "--adb", T(host.adb), "--ks", T(ks), "--ks-pass", "pass:android",
                  "--ks-key-alias", "androiddebugkey", "--key-pass", "pass:android"], timeout=900, env=host.java_env())
        if p.returncode != 0:
            raise MobileError("bundletool could not make the APKs of the bundle for %s: %s" % (h["key"], p.stdout[-600:]))
        p = _run([host.java, "-jar", T(host.bundletool), "install-apks", "--apks", T(apks), "--device-id",
                  h["serial"], "--adb", T(host.adb)], timeout=900, env=host.java_env())
        if p.returncode != 0:
            raise MobileError("bundletool could not install the bundle on %s: %s" % (h["key"], p.stdout[-600:]))
        # the bundle's installer grants no runtime permission (`adb install -g` does): each test grants them when it
        # starts the app with its data cleared (the runtime)
        return "installed from the bundle"
    p = _run([host.adb, "-s", h["serial"], "install", "-r", "-g", host.for_tool(artifact)], timeout=900)
    if p.returncode != 0 or "Failure" in p.stdout:
        raise MobileError("could not install the APK on %s: %s" % (h["key"], p.stdout[-400:]))
    return "installed"


# --------------------------------------------------------------------------- Appium

def appium_start(host, port, log_path):
    """One Appium server for this step on its own port. Returns (process, url)."""
    if not host.appium and not host.wsl:
        raise MobileError("Appium is not installed for the factory: run /auto-factory:code install")
    env = host.java_env(dict(os.environ, APPIUM_HOME=host.appium_home))
    # on WSL without mirrored networking, the address of Windows on WSL's own network: not every network of the machine
    addr = host.connect_host()
    args = ["--port", str(port), "--address", addr, "--base-path", "/", "--log-no-colors", "--log-timestamp",
            "--allow-insecure", "uiautomator2:adb_shell,xcuitest:record_audio"]
    if host.wsl:
        home = _run(["wslpath", "-w", os.path.join(host.win_dir(), "appium-home")], timeout=30).stdout.strip()
        cmd = ["cmd.exe", "/c", "set APPIUM_HOME=%s&& set ANDROID_HOME=%s&& set JAVA_HOME=%s&& appium %s" % (
            home, _run(["wslpath", "-w", host.sdk], timeout=30).stdout.strip(),
            _run(["wslpath", "-w", host.java_home], timeout=30).stdout.strip() if host.java_home else "",
            " ".join(args))]
    else:
        cmd = [host.appium] + args
    log = open(log_path, "ab")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                            start_new_session=True)
    url = "http://%s:%d" % (host.connect_host(), port)
    t0 = time.time()
    while time.time() - t0 < 120:
        if proc.poll() is not None:
            # it stopped by itself (a port taken after all): nothing of ours is on that port, nothing is stopped there
            raise MobileError("Appium stopped while starting; see %s" % log_path)
        try:
            with urllib.request.urlopen(url + "/status", timeout=3) as r:
                if r.status == 200:
                    return proc, url
        except Exception:  # noqa: BLE001
            time.sleep(1)
    appium_stop(proc, host, port, by_port=True)   # ours: started on a port nothing listened on (pick_port)
    raise MobileError("Appium did not answer on %s within 120 s; see %s" % (url, log_path))


def appium_stop(proc, host=None, port=None, by_port=False):
    """Stop this step's Appium. On WSL it runs on Windows, out of reach of the process handle: it is stopped by its port
    only when it is this step's (by_port: it was started and answered there), never a server another step runs."""
    if by_port and host is not None and host.wsl and port:
        # the server runs on Windows: stop the one on this step's port, and nothing else
        _run(["powershell.exe", "-NoProfile", "-Command",
              "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*appium*--port %d *' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % int(port)], timeout=60)
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, 15)
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()


def device_port(h):
    """The port this machine talks to the device's test agent on (UiAutomator2's server, WebDriverAgent). Phones that
    run side by side each need their own, or the second one's session cannot start. An emulator's is taken from its
    console port, which no other running emulator has; a simulator's from its id, then the next free one."""
    if h["platform"] == "android":
        try:
            return 8200 + (int(str(h["serial"]).rsplit("-", 1)[1]) - 5554) // 2
        except (KeyError, ValueError, IndexError):
            return free_port(8200)
    return free_port(8100 + int(hashlib.sha1(h["udid"].encode()).hexdigest(), 16) % 90)


def capabilities(h, app, host=None):
    """What a WebdriverIO session on this device asks Appium for. The app is installed already: every test starts it
    with its data cleared (the runtime does that), so nothing of an earlier test is left in it."""
    port = h.get("agent_port") or device_port(h)
    if h["platform"] == "ios":
        caps = {"platformName": "iOS", "appium:automationName": "XCUITest", "appium:udid": h["udid"],
                "appium:bundleId": app["id"], "appium:noReset": True, "appium:newCommandTimeout": 600,
                "appium:wdaLaunchTimeout": 240000, "appium:wdaConnectionTimeout": 240000,
                "appium:reduceMotion": True, "appium:shouldTerminateApp": True, "appium:wdaLocalPort": port}
        if host is not None:
            # WebDriverAgent built once per simulator and kept: simulators side by side do not build over each other
            caps["appium:derivedDataPath"] = os.path.join(host.tools_dir, "wda", h["udid"])
        return caps
    caps = {"platformName": "Android", "appium:automationName": "UiAutomator2", "appium:udid": h["serial"],
            "appium:appPackage": app["id"], "appium:noReset": True, "appium:newCommandTimeout": 600,
            "appium:autoGrantPermissions": True, "appium:disableWindowAnimation": True,
            "appium:uiautomator2ServerInstallTimeout": 240000, "appium:adbExecTimeout": 120000,
            "appium:systemPort": port}
    if app.get("activity"):
        caps["appium:appActivity"] = app["activity"]
    return caps


# --------------------------------------------------------------------------- a whole session of a step

class Session:
    """The devices of a step, started, with the app installed and Appium up. Used by fx (code, review) and through
    `fx mobile session` by the auto test. close() stops what it started, and only that."""

    def __init__(self, tools_dir, devices, apps, out_dir, port=None, install_images=False, window=False):
        self.host = Host(tools_dir)
        self.window = window
        self.install_images = install_images
        self.devices, self.apps, self.out_dir = devices, apps, out_dir
        self.port = port
        self.handles, self.appium, self.url, self.locks = [], None, None, contextlib.ExitStack()
        self.notes = []

    def open(self):
        os.makedirs(self.out_dir, exist_ok=True)
        try:
            for dev in self.devices:
                if dev["platform"] not in self.apps:
                    self.notes.append("%s: no %s build of the app, not run" % (dev["key"], dev["platform"]))
                    continue
                self.locks.enter_context(device_lock(dev))
                ensure_device(self.host, dev, self.install_images)
                h = boot(self.host, dev, self.out_dir, self.window)
                self.handles.append(h)
                app = self.apps[dev["platform"]]
                h["install"] = install(self.host, h, app["file"], app["id"], os.path.join(self.out_dir, "apks"))
                h.update(device=dev)
            if not self.handles:
                raise MobileError("no device could run the app: %s" % "; ".join(self.notes))
            for h in self.handles:
                app = self.apps[h["platform"]]
                h["app_file"] = self.host.for_tool(app["file"])
                h["agent_port"] = device_port(h)
            # the port is chosen and Appium bound to it under one lock of the machine: two steps never take one port
            with machine_lock("appium-port"):
                self.port = self.port or pick_port(self.host)
                self.appium, self.url = appium_start(self.host, self.port, os.path.join(self.out_dir, "appium.log"))
        except BaseException:
            self.close()
            raise
        return self

    def describe(self):
        """What the JavaScript side gets (FACTORY_UI_MOBILE, AUTOTEST_MOBILE): Appium's address and each device."""
        return {"appium": self.url, "notes": self.notes,
                "devices": [{"key": h["key"], "platform": h["platform"], "name": h["device"].get("name"),
                             "os": h["device"].get("os"), "screen": h["device"].get("screen"),
                             "size": h["device"].get("size"),
                             "app": {k: v for k, v in self.apps[h["platform"]].items() if k != "file"},
                             "app_file": h.get("app_file"),
                             "capabilities": capabilities(h, self.apps[h["platform"]], self.host)} for h in self.handles]}

    def close(self):
        appium_stop(self.appium, self.host, self.port, by_port=self.appium is not None)
        self.appium = None
        for h in self.handles:
            try:
                if h["platform"] == "ios":
                    stop_wda(self.host, h.get("udid"))
                shutdown(self.host, h)
            except MobileError as e:
                self.notes.append("%s did not stop: %s" % (h["key"], e))
        self.handles = []
        self.locks.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


def app_kind(root):
    """What kind of mobile app a repository is, from its files: flutter, react-native, android or ios; None for
    anything else (a web, an API, a web wrapped by Capacitor or Cordova: those are checked in a browser)."""
    def has(*parts):
        return os.path.exists(os.path.join(root, *parts))

    def text(*parts):
        try:
            with open(os.path.join(root, *parts), encoding="utf-8", errors="replace") as f:
                return f.read(200000)
        except OSError:
            return ""
    if has("pubspec.yaml") and re.search(r"(?m)^\s+sdk:\s*flutter\b", text("pubspec.yaml")) and (has("android") or has("ios")):
        return "flutter"
    pkg = text("package.json")
    if pkg:
        try:
            deps = json.loads(pkg)
        except ValueError:
            deps = {}
        if not isinstance(deps, dict):
            deps = {}
        names = set((deps.get("dependencies") or {}).keys()) | set((deps.get("devDependencies") or {}).keys())
        if "react-native" in names or "expo" in names:
            return "react-native"
        return None   # a JavaScript project with an android/ or ios/ folder is a wrapped web
    for gradle in (("app", "build.gradle"), ("app", "build.gradle.kts"), ("android", "app", "build.gradle"),
                   ("android", "app", "build.gradle.kts")):
        if "com.android.application" in text(*gradle):
            return "android"
    for d in (root, os.path.join(root, "ios")):
        # an Xcode project that builds for the iPhone (a Mac app's builds for macosx)
        for proj in glob.glob(os.path.join(d, "*.xcodeproj", "project.pbxproj")):
            if re.search(r"SDKROOT = iphoneos|IPHONEOS_DEPLOYMENT_TARGET", text(os.path.relpath(proj, root))):
                return "ios"
    return None


def api_url_for(platform_, url):
    """The address a virtual device reaches a service of this machine at: an Android emulator sees this machine as
    10.0.2.2; an iOS simulator shares its network."""
    if platform_ == "android":
        return re.sub(r"//(127\.0\.0\.1|localhost)([:/]|$)", r"//10.0.2.2\2", url)
    return url


def fill(cmd, values):
    for k, v in values.items():
        cmd = cmd.replace("{%s}" % k, shlex.quote(str(v)) if k.endswith("_q") else str(v))
    return cmd


def builds_for(conf, platforms, cwd, base_env, api_url, commit, out_dir, apk_too=False):
    """Build what the devices of `platforms` install, from the code in `cwd` at `commit`, pointed at `api_url` (the API
    of the job, the review's sandbox, or the auto test's). Returns {platform: {id, file, activity?}} and notes.
    conf is the project's mobile block: build.<platform> (command), artifact.<platform> (path or glob), app_id."""
    apps, notes = {}, []
    want = [p for p in platforms] + (["android_apk"] if apk_too and "android" in platforms else [])
    for p in want:
        plat = "android" if p.startswith("android") else "ios"
        cmd = ((conf.get("build") or {}).get(p) or "").strip()
        art = ((conf.get("artifact") or {}).get(p) or "").strip()
        if not cmd or not art:
            notes.append("no build command for %s in the project's mobile settings" % p)
            continue
        url = api_url_for(plat, api_url) if api_url else ""
        env = dict(base_env, FACTORY_API_URL=url, FACTORY_MOBILE_PLATFORM=plat)
        f, reused = build(fill(cmd, {"api_url": url}), cwd, env, art, os.path.join(out_dir, p),
                          "%s|%s|%s|%s" % (commit, p, url, cmd))
        app_id = (conf.get("app_id") or {}).get(plat) or ""
        apps[p] = {"id": app_id, "file": f, "reused": reused}
        if (conf.get("activity") or {}).get(plat):
            apps[p]["activity"] = conf["activity"][plat]
    return apps, notes
