"""A mobile app in code and review: the virtual phones (skills/code/mobile.py), and the app side of the UI check
(runtime/uicheck/app.js, which the code skill shares with the auto test). What tx does differently for a mobile project
is in test_autotest_mobile.py.

Standard library only; the JavaScript parts run with Node when it is there. No phone is started here: what needs a
running emulator or simulator is the real run on a project.
    python3 -I -m unittest discover -s tests -p test_mobile.py
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = os.path.join(ROOT, "plugins", "auto-factory", "skills", "code")
# the comparer and the app runtime: shipped inside the code skill (shared/) where there is no auto test, else the auto test's
TEST = next(d for d in (os.path.join(CODE, "shared"), os.path.join(ROOT, "plugins", "auto-factory", "skills", "test"))
            if os.path.isdir(os.path.join(d, "runtime", "uicheck")))
sys.path.insert(0, CODE)

import mobile as M  # noqa: E402

NODE = shutil.which("node")

ANDROID_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy index="0" class="hierarchy" rotation="0" width="720" height="1280">
  <android.widget.FrameLayout index="0" package="com.example" class="android.widget.FrameLayout" text="" content-desc=""
      clickable="false" enabled="true" displayed="true" bounds="[0,0][720,1280]">
    <android.view.View index="0" class="android.view.View" text="" content-desc="Welcome!" clickable="false" enabled="true"
        displayed="true" bounds="[40,200][680,260]" />
    <android.widget.Button index="1" class="android.widget.Button" text="" content-desc="Next" clickable="true" enabled="true"
        displayed="true" bounds="[40,1100][680,1200]" />
    <android.widget.EditText index="2" class="android.widget.EditText" text="a@b.test" hint="Email" clickable="true"
        enabled="true" password="false" displayed="true" bounds="[40,400][680,500]" />
    <android.view.View index="3" class="android.view.View" text="" content-desc="" clickable="true" enabled="true"
        displayed="true" bounds="[600,40][660,80]" />
    <android.view.View index="4" class="android.view.View" text="" content-desc="Hidden" clickable="false"
        displayed="false" bounds="[0,0][10,10]" />
  </android.widget.FrameLayout>
</hierarchy>"""

IOS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AppiumAUT>
  <XCUIElementTypeApplication type="XCUIElementTypeApplication" name="C2P" label="C2P" enabled="true" visible="true"
      x="0" y="0" width="375" height="667">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Welcome!" name="Welcome!" label="Welcome!"
        enabled="true" visible="true" x="20" y="100" width="200" height="30"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="Next" label="Next" enabled="true" visible="true"
        x="16" y="600" width="343" height="50"/>
    <XCUIElementTypeSecureTextField type="XCUIElementTypeSecureTextField" value="••••" label="Password"
        placeholderValue="Password" enabled="true" visible="true" x="16" y="300" width="343" height="44"/>
    <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" name="Remember" label="Remember" value="1" enabled="true"
        visible="true" x="300" y="360" width="51" height="31"/>
  </XCUIElementTypeApplication>
</AppiumAUT>"""


def node(script, *args, cwd=None):
    p = subprocess.run([NODE, "-e", script] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       timeout=120, cwd=cwd or TEST)
    if p.returncode != 0:
        raise AssertionError("node failed: %s" % p.stderr[-2000:])
    return json.loads(p.stdout)


class PhonesTest(unittest.TestCase):
    """mobile.py: which phones a preset is, on any machine."""

    TYPES = [  # what `simctl list devicetypes` gives on a machine, read for its screen
        {"id": "se", "name": "iPhone SE (3rd generation)", "w": 375, "h": 667, "scale": 2, "min": "15.0", "max": "99"},
        {"id": "mini", "name": "iPhone 13 mini", "w": 375, "h": 812, "scale": 3, "min": "15.0", "max": "99"},
        {"id": "16", "name": "iPhone 16", "w": 393, "h": 852, "scale": 3, "min": "18.0", "max": "99"},
        {"id": "15", "name": "iPhone 15", "w": 393, "h": 852, "scale": 3, "min": "17.0", "max": "99"},
        {"id": "pm", "name": "iPhone 17 Pro Max", "w": 440, "h": 956, "scale": 3, "min": "26.0", "max": "99"},
        {"id": "old", "name": "iPhone 8", "w": 375, "h": 667, "scale": 2, "min": "11.0", "max": "16.4"},
    ]

    def test_each_size_class_is_one_iphone_of_its_own(self):
        got = M.pick_ios(self.TYPES, M.PRESETS["full"]["ios"], "26.5")
        self.assertEqual([t["name"] for t in got],
                         ["iPhone SE (3rd generation)", "iPhone 13 mini", "iPhone 16", "iPhone 17 Pro Max"])
        self.assertEqual([t["size_class"] for t in got], ["small", "frame", "standard", "large"])

    def test_a_runtime_too_old_for_a_type_leaves_it_out(self):
        got = M.pick_ios(self.TYPES, ("large",), "17.5")
        self.assertEqual(got[0]["name"], "iPhone 15")  # the Pro Max needs iOS 26, the 16 needs 18

    def test_code_and_review_take_the_two_ends_of_each_platform(self):
        devs = [{"key": "ios-375x667", "platform": "ios", "size": [375, 667]},
                {"key": "ios-393x852", "platform": "ios", "size": [393, 852]},
                {"key": "ios-440x956", "platform": "ios", "size": [440, 956]},
                {"key": "android-360x640", "platform": "android", "size": [360, 640]},
                {"key": "android-411x914", "platform": "android", "size": [411, 914]}]
        self.assertEqual([d["key"] for d in M.ends_of(devs)],
                         ["android-360x640", "android-411x914", "ios-375x667", "ios-440x956"])
        self.assertEqual([d["key"] for d in M.ends_of(devs[:1])], ["ios-375x667"])

    def plan_android(self, conf, matrix="cross", apis=(33, 35)):
        with tempfile.TemporaryDirectory() as sdk, tempfile.TemporaryDirectory() as cache:
            os.makedirs(os.path.join(sdk, "platform-tools"))
            for a in apis:
                os.makedirs(os.path.join(sdk, "system-images", "android-%d" % a, "google_apis", M.host_abi()))
            old = dict(os.environ)
            os.environ.update(ANDROID_HOME=sdk, FACTORY_MOBILE_DIR=cache)
            try:
                host = M.Host(sdk)
                host.mac = False  # no iOS here, whatever this machine is
                return M.plan_devices(host, "standard", ["android", "ios"], conf, matrix=matrix), list(M.plan_devices.notes)
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_every_size_runs_on_every_version(self):
        devs, notes = self.plan_android({"min_os": {"android": 24}}, apis=(24, 29, 30, 33, 36))
        self.assertEqual([d["key"] for d in devs], [
            "android-360x640-api24", "android-411x914-api24", "android-448x998-api24",
            "android-360x640-api30", "android-411x914-api30", "android-448x998-api30",
            "android-360x640-api36", "android-411x914-api36", "android-448x998-api36"])
        self.assertEqual(devs[0]["lcd"], [720, 1280, 320])
        self.assertEqual((devs[0]["os"], devs[-1]["os"]), ("Android 7.0", "Android 16"))
        self.assertEqual(notes, [])

    def test_code_and_review_run_small_on_the_oldest_and_large_on_the_newest(self):
        devs, _ = self.plan_android({"min_os": {"android": 26}}, matrix="ends", apis=(24, 26, 30, 36))
        self.assertEqual([d["key"] for d in devs], ["android-360x640-api26", "android-448x998-api36"])

    def test_versions_the_project_names_are_taken_as_they_are(self):
        devs, notes = self.plan_android({"versions": {"android": [33, 34]}}, apis=(33, 35))
        self.assertEqual(sorted({d["version"] for d in devs}), ["api33"])
        self.assertIn("API 34", notes[0])

    def test_without_the_oldest_supported_version_it_says_so(self):
        devs, notes = self.plan_android({}, apis=(33, 35))
        self.assertEqual(sorted({d["version"] for d in devs}), ["api33", "api35"])
        self.assertIn("min_os.android", notes[0])

    def test_the_oldest_the_newest_and_the_middle(self):
        self.assertEqual(M.pick_versions([21, 24, 26, 29, 30, 33, 36], 24), [1, 4, 6])
        self.assertEqual(M.pick_versions([(17, 5), (18, 6), (26, 5), (27, 0)], (15, 0)), [0, 1, 3])  # a tie: the older
        self.assertEqual(M.pick_versions([(26, 5), (27, 0)], (15, 0)), [0, 1])
        self.assertEqual(M.pick_versions([35], 24), [0])
        self.assertEqual(M.pick_versions([21, 22], 24), [])
        self.assertEqual(M.pick_versions([24, 30, 36], 24, count=2), [0, 2])

    def test_ends_of_a_matrix(self):
        devs = [{"key": "a-s-old", "platform": "android", "size": [360, 640], "ver_rank": 0},
                {"key": "a-l-old", "platform": "android", "size": [448, 998], "ver_rank": 0},
                {"key": "a-s-new", "platform": "android", "size": [360, 640], "ver_rank": 2},
                {"key": "a-l-new", "platform": "android", "size": [448, 998], "ver_rank": 2}]
        self.assertEqual([d["key"] for d in M.ends_of(devs)], ["a-s-old", "a-l-new"])

    def test_an_unknown_preset_is_refused(self):
        with self.assertRaises(M.MobileError):
            M.plan_devices(M.Host(tempfile.gettempdir()), "everything", ["android"])

    def test_an_emulator_reaches_this_machine_at_its_own_address(self):
        self.assertEqual(M.api_url_for("android", "http://127.0.0.1:8001/api"), "http://10.0.2.2:8001/api")
        self.assertEqual(M.api_url_for("android", "http://localhost/api"), "http://10.0.2.2/api")
        self.assertEqual(M.api_url_for("ios", "http://127.0.0.1:8001/api"), "http://127.0.0.1:8001/api")
        self.assertEqual(M.api_url_for("android", "https://api.example.test/api"), "https://api.example.test/api")

    def test_an_ini_keeps_its_other_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write("hw.lcd.width = 1080\nimage.sysdir.1 = x\n")
        try:
            M.set_ini(f.name, {"hw.lcd.width": 720, "hw.keyboard": "yes"})
            text = open(f.name).read()
        finally:
            os.remove(f.name)
        self.assertIn("hw.lcd.width = 720", text)
        self.assertIn("image.sysdir.1 = x", text)
        self.assertIn("hw.keyboard = yes", text)

    def test_closing_stops_only_the_webdriveragent_of_its_own_simulator(self):
        host = M.Host(tempfile.gettempdir())
        host.mac = True
        ps = "\n".join([
            "101 /Applications/Xcode.app/usr/bin/xcodebuild test -project %s/WebDriverAgent.xcodeproj -destination id=U-1" % host.appium_home,
            "102 /Applications/Xcode.app/usr/bin/xcodebuild test -project /elsewhere/WebDriverAgent.xcodeproj -destination id=U-1",
            "103 /Applications/Xcode.app/usr/bin/xcodebuild test -project %s/WebDriverAgent.xcodeproj -destination id=U-2" % host.appium_home])
        killed = []
        real_run, real_kill = M._run, M.os.kill
        M._run = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=ps)
        M.os.kill = lambda pid, sig: killed.append((pid, sig)) if sig else (_ for _ in ()).throw(OSError())
        try:
            M.stop_wda(host, "U-1")
        finally:
            M._run, M.os.kill = real_run, real_kill
        self.assertEqual(killed, [(101, 15)])

    def test_phones_side_by_side_each_have_their_own_agent_port(self):
        a = M.capabilities({"platform": "android", "serial": "emulator-5554"}, {"id": "com.x"})
        b = M.capabilities({"platform": "android", "serial": "emulator-5556"}, {"id": "com.x"})
        self.assertEqual((a["appium:systemPort"], b["appium:systemPort"]), (8200, 8201))
        host = M.Host(tempfile.gettempdir())
        i = M.capabilities({"platform": "ios", "udid": "U-1", "agent_port": 8150}, {"id": "c2p.app"}, host)
        j = M.capabilities({"platform": "ios", "udid": "U-2", "agent_port": 8151}, {"id": "c2p.app"}, host)
        self.assertEqual((i["appium:wdaLocalPort"], j["appium:wdaLocalPort"]), (8150, 8151))
        self.assertNotEqual(i["appium:derivedDataPath"], j["appium:derivedDataPath"])

    def test_a_half_removed_ui_agent_is_removed_whole(self):
        host = type("H", (), {"adb": "adb"})()

        def phone(listed, kept):
            calls = []

            def run(cmd, **k):
                calls.append(cmd[3:])
                out = kept if cmd[-1] == "-u" else listed
                return subprocess.CompletedProcess(cmd, 0, stdout="".join("package:%s\n" % x for x in out))
            real = M._run
            M._run = run
            try:
                did = M.repair_agent(host, "emulator-5556")
            finally:
                M._run = real
            return did, [c for c in calls if c[0] == "uninstall"]

        both, half = list(M.UIA2), [M.UIA2[1]]
        self.assertEqual(phone(both, both), (False, []))
        self.assertEqual(phone([], []), (False, []))
        did, removed = phone(half, both)
        self.assertTrue(did)
        self.assertEqual(sorted(r[1] for r in removed), sorted(M.UIA2))

    def test_capabilities_start_nothing_of_an_earlier_test(self):
        ios = M.capabilities({"platform": "ios", "udid": "U"}, {"id": "c2p.app"})
        self.assertEqual((ios["appium:bundleId"], ios["appium:udid"], ios["appium:automationName"]), ("c2p.app", "U", "XCUITest"))
        andr = M.capabilities({"platform": "android", "serial": "emulator-5554"}, {"id": "com.x", "activity": ".Main"})
        self.assertEqual(andr["appium:appPackage"], "com.x")
        self.assertEqual(andr["appium:appActivity"], ".Main")
        self.assertTrue(andr["appium:autoGrantPermissions"])


class BuildTest(unittest.TestCase):
    """mobile.builds_for: the project's own commands, once per commit and address."""

    def test_a_build_is_made_once_per_commit_and_address(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as out:
            conf = {"build": {"android": "mkdir -p build && echo \"$FACTORY_API_URL {api_url}\" > build/app.aab && "
                                         "echo x >> count", "ios": ""},
                    "artifact": {"android": "build/*.aab"}, "app_id": {"android": "com.x"}}
            apps, notes = M.builds_for(conf, ["android", "ios"], code, dict(os.environ), "http://127.0.0.1:9000/api",
                                       "abc", out)
            self.assertEqual(sorted(apps), ["android"])
            self.assertIn("no build command for ios", notes[0])
            with open(apps["android"]["file"]) as f:
                self.assertEqual(f.read().strip(), "http://10.0.2.2:9000/api http://10.0.2.2:9000/api")
            again, _ = M.builds_for(conf, ["android"], code, dict(os.environ), "http://127.0.0.1:9000/api", "abc", out)
            self.assertTrue(again["android"]["reused"])
            M.builds_for(conf, ["android"], code, dict(os.environ), "http://127.0.0.1:9000/api", "def", out)
            with open(os.path.join(code, "count")) as f:
                self.assertEqual(len(f.read().split()), 2)

    def test_a_build_that_fails_says_why(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as out:
            conf = {"build": {"android": "echo nope; exit 3"}, "artifact": {"android": "x.aab"}}
            with self.assertRaises(M.MobileError) as e:
                M.builds_for(conf, ["android"], code, dict(os.environ), "", "abc", out)
            self.assertIn("nope", str(e.exception))


@unittest.skipUnless(NODE, "node is not installed")
class AppScreenTest(unittest.TestCase):
    """uicheck/app.js: an app's screen read into the nodes the comparer takes."""

    def nodes(self, xml, platform, scale, vp):
        return node("const A=require('./runtime/uicheck/app.js');"
                    "process.stdout.write(JSON.stringify(A.nodesOf(process.argv[1], process.argv[2], +process.argv[3],"
                    " JSON.parse(process.argv[4]), null)))", xml, platform, str(scale), json.dumps(vp))

    def test_android_in_dp_with_roles_texts_and_labels(self):
        ns = self.nodes(ANDROID_XML, "android", 2, {"width": 360, "height": 640})
        texts = [n for n in ns if n["type"] == "text"]
        els = {n.get("name"): n for n in ns if n["type"] == "el" and n.get("role")}
        self.assertEqual([t["text"] for t in texts], ["Welcome!", "Next"])  # a field's value is its state, as on the web
        self.assertEqual(texts[0]["box"], {"x": 20, "y": 100, "w": 320, "h": 30})
        self.assertEqual(els["Next"]["role"], "button")
        self.assertEqual(els["Email"]["role"], "textbox")
        self.assertEqual(els["Email"]["state"]["value"], "a@b.test")
        self.assertIn("", els)  # a control nobody can name is kept, for the accessibility check
        self.assertNotIn("Hidden", [t["text"] for t in texts])
        self.assertTrue(ns[0]["page"])

    def test_ios_in_points(self):
        ns = self.nodes(IOS_XML, "ios", 2, {"width": 375, "height": 667})
        roles = {n["name"]: n for n in ns if n["type"] == "el" and n.get("role")}
        self.assertEqual(roles["Next"]["box"], {"x": 16, "y": 600, "w": 343, "h": 50})
        self.assertEqual(roles["Password"]["role"], "textbox")
        self.assertEqual(roles["Remember"]["state"]["checked"], True)
        self.assertIn("Welcome!", [n["text"] for n in ns if n["type"] == "text"])

    def test_what_an_app_cannot_tell_is_taken_from_the_design(self):
        got = node("""
const A=require('./runtime/uicheck/app.js');
const D={nodes:[{id:1,type:'text',text:'Next',box:{x:10,y:10,w:40,h:20},font:{size:16,weight:'600',family:'Inter'},lines:1,
  color:[0,0,0,1],under:[255,255,255,1],align:'center'},{id:2,type:'el',role:'button',name:'Next',box:{x:0,y:0,w:100,h:40},
  paint:{bg:[0,0,255,1],radius:[8,8,8,8],border:[{w:1,c:[0,0,0,1]}]}}]};
const R={nodes:[{id:5,type:'text',text:'Next',box:{x:30,y:14,w:30,h:12},font:null,color:[90,90,90,1],unmeasured:['font','lines','box']},
  {id:6,type:'el',role:'button',name:'Next',box:{x:0,y:0,w:100,h:40},paint:{bg:[0,0,200,1]},unmeasured:['radius','border','shadow']}]};
A.fillUnmeasured(D,R,{m:new Map([[1,5],[2,6]]),rev:new Map([[5,1],[6,2]])});
process.stdout.write(JSON.stringify(R.nodes));""")
        t, b = got
        self.assertEqual(t["font"], {"size": 16, "weight": "600", "family": "Inter"})
        self.assertEqual(t["box"], {"x": 25, "y": 10, "w": 40, "h": 20})  # the design's size, around the ink's centre
        self.assertEqual(t["color"], [90, 90, 90, 1])  # measured, and really another colour: kept
        self.assertEqual(b["paint"]["radius"], [8, 8, 8, 8])
        self.assertEqual(b["paint"]["bg"], [0, 0, 200, 1])

    def test_accessibility_of_an_app_screen(self):
        got = node("""
const A=require('./runtime/uicheck/app.js');
process.stdout.write(JSON.stringify(A.a11yOf([{type:'el',role:'button',name:'',tag:'View',box:{x:0,y:0,w:30,h:30}},
  {type:'el',role:'button',name:'Ok',box:{x:0,y:0,w:48,h:48}}],'android')));""")
        self.assertEqual(sorted(v["id"] for v in got), ["control-name", "target-size"])
        self.assertEqual(next(v for v in got if v["id"] == "control-name")["impact"], "serious")

    def test_an_app_that_dies_on_start_says_what_it_died_of(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        body = {"procPath": "/Users/u/Library/Developer/CoreSimulator/Devices/UDID-1/data/Runner.app/Runner",
                "exception": {"type": "EXC_BREAKPOINT", "signal": "SIGTRAP"}, "faultingThread": 0,
                "threads": [{"frames": [{"imageIndex": 0, "symbol": "NoSceneLifecycle_block"}]}],
                "usedImages": [{"name": "UIKitCore"}]}
        for name, bundle, udid in (("a.ips", "c2p.app", "UDID-1"), ("b.ips", "other.app", "UDID-1"),
                                   ("c.ips", "c2p.app", "UDID-2")):
            with open(os.path.join(d, name), "w") as f:
                f.write(json.dumps({"bundleID": bundle}) + "\n" + json.dumps(dict(body, procPath=body["procPath"].replace("UDID-1", udid))))
        got = node("const A=require('./runtime/app.js');process.stdout.write(JSON.stringify(A.iosCrashes("
                   "{platform:'ios',key:'k',app:{id:'c2p.app'},capabilities:{'appium:udid':'UDID-1'}},0,process.argv[1])))", d)
        self.assertEqual([g["text"] for g in got], ["EXC_BREAKPOINT (SIGTRAP) in UIKitCore NoSceneLifecycle_block"])

    def test_what_a_flutter_screen_shows_is_read_as_a_user_sees_it(self):
        # an image with a label is an image (not a text); its box is the painted tile around the mark it labels; an
        # iOS field shows its label as the hint; an icon button's label is not a text on the screen; a box that only
        # paints is not "missing" on an app
        got = node("""
const A=require('./runtime/uicheck/app.js');
const W=100,H=100,img={width:W,height:H,data:Buffer.alloc(W*H*4)};
const put=(x,y,c)=>{const o=(y*W+x)*4;img.data[o]=c[0];img.data[o+1]=c[1];img.data[o+2]=c[2];img.data[o+3]=255;};
for(let y=0;y<H;y++)for(let x=0;x<W;x++)put(x,y,[245,245,240]);
for(let y=10;y<50;y++)for(let x=10;x<50;x++)put(x,y,[20,20,30]);      // the tile
for(let y=24;y<36;y++)for(let x=22;x<38;x++)put(x,y,[250,250,250]);   // the mark inside
for(let y=70;y<90;y++)for(let x=60;x<96;x++)put(x,y,[255,255,255]);   // a field
for(let y=78;y<82;y++)for(let x=88;x<92;x++)put(x,y,[60,60,60]);      // an eye icon in it
const andr='<hierarchy><android.widget.ImageView class="android.widget.ImageView" content-desc="Logo" bounds="[22,24][38,36]" displayed="true" enabled="true" clickable="false"/></hierarchy>';
const ios='<AppiumAUT><XCUIElementTypeTextField type="XCUIElementTypeTextField" label="ten@qa.test" name="ten@qa.test" x="60" y="70" width="36" height="20" visible="true" enabled="true"/><XCUIElementTypeButton type="XCUIElementTypeButton" label="Hien mat khau" name="Hien mat khau" x="86" y="74" width="8" height="12" visible="true" enabled="true"/></AppiumAUT>';
const a=A.nodesOf(andr,'android',1,{width:W,height:H},img).filter(n=>n.id>1);
const i=A.nodesOf(ios,'ios',1,{width:W,height:H},img).filter(n=>n.id>1);
const D={nodes:[{id:1,type:'el',page:true,box:{x:0,y:0,w:W,h:H}},{id:2,type:'el',paint:{bg:[1,1,1,1]},box:{x:0,y:0,w:9,h:9}},{id:3,type:'el',role:'button',box:{x:0,y:0,w:9,h:9}}]};
D.nodes.push({id:4,type:'el',paint:{bg:[1,1,1,1]},box:{x:0,y:0,w:9,h:9}},{id:5,type:'el',parent:4,graphic:{kind:'svg'},box:{x:0,y:0,w:4,h:4}},{id:6,type:'el',paint:{bg:[1,1,1,1]},box:{x:0,y:0,w:9,h:9}},{id:7,type:'el',parent:6,graphic:{kind:'svg'},box:{x:0,y:0,w:4,h:4}});
const kept=A.dropUnseen([{prop:'missing',level:'MINOR',inside:0,dNode:2},{prop:'missing',level:'MINOR',inside:0,dNode:3},{prop:'missing',level:'BAD',inside:2,dNode:2},
  {prop:'missing',level:'MINOR',inside:1,dNode:4},{prop:'missing',level:'MINOR',inside:0,dNode:6}],D,{m:new Map([[7,9]])});
process.stdout.write(JSON.stringify({a:a.map(n=>({type:n.type,img:!!n.graphic,box:n.box,text:n.text})),
  i:i.map(n=>({type:n.type,role:n.role,text:n.text,ph:n.state&&n.state.placeholder})),kept:kept.length}));""")
        self.assertEqual([(n["type"], n["img"]) for n in got["a"]], [("el", True)])
        self.assertEqual(got["a"][0]["box"], {"x": 10, "y": 10, "w": 40, "h": 40})
        field = next(n for n in got["i"] if n["role"] == "textbox")
        self.assertEqual(field["ph"], "ten@qa.test")
        self.assertNotIn("Hien mat khau", [n["text"] for n in got["i"] if n["type"] == "text"])
        self.assertEqual(got["kept"], 3)   # + the box whose icon is missing with it; not the one whose icon is there

    def test_colours_within_the_smoothing_of_edges_are_the_designs(self):
        got = node("""
const A=require('./runtime/uicheck/app.js');
const D={nodes:[{id:1,type:'text',color:[27,31,42,1],box:{x:0,y:0,w:9,h:9}},{id:2,type:'text',color:[47,95,196,1],box:{x:0,y:0,w:9,h:9}}]};
const R={nodes:[{id:1,type:'text',color:[28,30,44,1],unmeasured:['font'],box:{x:0,y:0,w:9,h:9}},{id:2,type:'text',color:[27,31,42,1],unmeasured:['font'],box:{x:0,y:0,w:9,h:9}}]};
A.fillUnmeasured(D,R,{m:new Map([[1,1],[2,2]])});
process.stdout.write(JSON.stringify(R.nodes.map(n=>n.color)));""")
        self.assertEqual(got[0], [27, 31, 42, 1])   # 1-2 steps off: the design's colour
        self.assertEqual(got[1], [27, 31, 42, 1])   # really another colour: kept as it is

    def test_a_colour_near_another_token_is_still_a_difference(self):
        got = node("""
const E=require('./runtime/uicheck/engine.js');const A=require('./runtime/uicheck/app.js');
E.setTokens({css:':root{--g-50:#F9FAFB;--g-100:#F3F4F6;--ink:#1B1F2A;}'});
const D={nodes:[{id:1,type:'text',color:[243,244,246,1],box:{x:0,y:0,w:9,h:9}},{id:2,type:'text',color:[27,31,42,1],box:{x:0,y:0,w:9,h:9}}]};
const R={nodes:[{id:1,type:'text',color:[249,250,251,1],unmeasured:['font'],box:{x:0,y:0,w:9,h:9}},{id:2,type:'text',color:[34,38,50,1],unmeasured:['font'],box:{x:0,y:0,w:9,h:9}}]};
A.fillUnmeasured(D,R,{m:new Map([[1,1],[2,2]])});
process.stdout.write(JSON.stringify(R.nodes.map(n=>n.color)));""")
        self.assertEqual(got[0], [249, 250, 251, 1])   # #F9FAFB is a token of its own: the wrong one, kept
        self.assertEqual(got[1], [27, 31, 42, 1])      # 22 steps off, nearest to the design's own token: that token

    def test_colours_and_ink_are_read_from_a_screenshot(self):
        got = node("""
const { encode } = require('./runtime/uicheck/annotate.js');
const A=require('./runtime/uicheck/app.js');
const w=40,h=20,data=Buffer.alloc(w*h*4,255);
for(let y=8;y<12;y++)for(let x=10;x<30;x++){const o=(y*w+x)*4;data[o]=20;data[o+1]=40;data[o+2]=200;}
const {decode}=require('./runtime/uicheck/annotate.js');
const img={width:w,height:h,data};
process.stdout.write(JSON.stringify({ink:A.inkOf(img,{x:0,y:0,w:20,h:10},2),bg:A.bgOf(img,{x:0,y:0,w:20,h:10},2)}));""")
        self.assertEqual(got["bg"], [255, 255, 255])
        self.assertEqual(got["ink"]["color"], [20, 40, 200, 1])
        self.assertEqual(got["ink"]["box"], {"x": 5, "y": 4, "w": 10, "h": 2})


def load_fx():
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("fx_mobile_under_test", os.path.join(CODE, "fx"))
    spec = importlib.util.spec_from_loader("fx_mobile_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FxMobileTest(unittest.TestCase):
    """fx mobile, as tx and a person call it; the UI check of a mobile app in the gate."""

    class Ctx:
        def __init__(self, cfg):
            self.cfg = cfg

        def c(self, dotted, default=None):
            v = self.cfg
            for k in dotted.split("."):
                if not isinstance(v, dict) or k not in v:
                    return default
                v = v[k]
            return v

    def test_the_phones_reports_become_one_named_by_phone(self):
        fx = load_fx()
        merged = {"steps": [], "shots": [], "console": [], "requests": [], "a11y": [], "design": [], "error": None,
                  "devices": [], "notes": []}
        dinfo = {"why": None, "packages": [], "thresholds": None}
        fx.mobile_merge(merged, {"steps": [{"name": "AC1", "ok": True}], "shots": [{"name": "s", "file": "a.png"}],
                                 "console": [{"text": "FATAL EXCEPTION: main"}],
                                 "a11y": [{"id": "control-name", "impact": "serious", "page": "s"}],
                                 "design": [{"code": "IN-1", "level": "BAD", "diffs": []}]},
                        {"key": "android-360x640", "name": "Android 360x640", "screen": "360x640 dp"}, dinfo)
        fx.mobile_merge(merged, {"steps": [{"name": "AC1", "ok": False, "error": "không thấy"}], "error": "boom"},
                        {"key": "ios-375x667", "name": "iPhone SE", "screen": "375x667 pt"}, dinfo)
        self.assertEqual([s["name"] for s in merged["steps"]], ["[android-360x640] AC1", "[ios-375x667] AC1"])
        self.assertEqual(merged["shots"][0]["viewport"], "android-360x640 360x640 dp")
        self.assertEqual(merged["design"][0]["device"], "android-360x640")
        self.assertIn("[ios-375x667] boom", merged["error"])
        rep = fx.ui_judge(self.Ctx({}), merged)
        joined = "\n".join(rep["blocking"])
        self.assertIn("UI step failed: [ios-375x667] AC1", joined)
        self.assertIn("FATAL EXCEPTION", joined)  # a crash of the app is a console error
        self.assertIn("control-name", joined)
        self.assertEqual(rep["design_bad"], ["IN-1"])  # heavy, but the UI reviewer decides it

    def test_a_repository_that_became_an_app_stops_and_asks_for_its_set_up(self):
        fx = load_fx()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fx.mobile_missing(self.Ctx({}), d)   # a web: nothing to ask
        os.makedirs(os.path.join(d, "android"))
        with open(os.path.join(d, "pubspec.yaml"), "w") as f:
            f.write("name: x\ndependencies:\n  flutter:\n    sdk: flutter\n")
        with self.assertRaises(fx.FxError) as e:
            fx.mobile_missing(self.Ctx({"mobile": {"app": "later"}}), d)
        self.assertEqual(e.exception.extra["next"], "setup-mobile")
        fx.mobile_missing(self.Ctx({"mobile": {"app": "no"}}), d)   # init was told this repository is not the app

    def test_what_kind_of_app_a_repository_is(self):
        def repo(files):
            d = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, d, True)
            for name, body in files.items():
                os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
                if body is not None:
                    with open(os.path.join(d, name), "w") as f:
                        f.write(body)
            return d
        flutter = "dependencies:\n  flutter:\n    sdk: flutter\n"
        self.assertEqual(M.app_kind(repo({"pubspec.yaml": flutter, "ios/x": None})), "flutter")
        self.assertIsNone(M.app_kind(repo({"pubspec.yaml": flutter})))   # a Flutter package, no app
        self.assertEqual(M.app_kind(repo({"package.json": '{"dependencies": {"react-native": "0.80"}}'})), "react-native")
        self.assertIsNone(M.app_kind(repo({"package.json": '{"dependencies": {"@capacitor/core": "7"}}',
                                           "android/app/build.gradle": "apply plugin: 'com.android.application'"})))
        self.assertEqual(M.app_kind(repo({"app/build.gradle.kts": 'plugins { id("com.android.application") }'})), "android")
        self.assertIsNone(M.app_kind(repo({"index.html": "<p>"})))
        self.assertIsNone(M.app_kind(repo({"package.json": "[]"})))
        self.assertIsNone(M.app_kind(repo({"Mac.xcodeproj/project.pbxproj": "SDKROOT = macosx;"})))   # a Mac app
        self.assertEqual(M.app_kind(repo({"ios/Runner.xcodeproj/project.pbxproj": "SDKROOT = iphoneos;"})), "ios")

    def test_appium_is_stopped_by_its_port_only_when_it_is_this_steps(self):
        calls = []
        host = type("H", (), {"wsl": True})()
        real = M._run
        M._run = lambda cmd, **k: calls.append(cmd[0]) or subprocess.CompletedProcess(cmd, 0, stdout="")
        try:
            M.appium_stop(None, host, 4723)                  # it never answered here: another step's may be there
            self.assertEqual(calls, [])
            M.appium_stop(None, host, 4723, by_port=True)    # this step's own
            self.assertEqual(calls, ["powershell.exe"])
        finally:
            M._run = real

    def test_a_command_that_hangs_is_a_clear_error(self):
        with self.assertRaises(M.MobileError) as e:
            M._run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        self.assertIn("did not answer", str(e.exception))

    def test_the_phones_open_a_window_only_when_asked(self):
        host = type("H", (), {"emulator": "emulator"})()
        self.assertIn("-no-window", M.emulator_args(host, "a", 5554))
        self.assertNotIn("-no-window", M.emulator_args(host, "a", 5554, window=True))
        a = load_fx().build_parser().parse_args(["new", "C2P3-6", "--base", "main", "--phones", "window"])
        self.assertEqual(a.phones, "window")
        a = load_fx().build_parser().parse_args(["mobile", "session", "--setup", "s.json", "--window"])
        self.assertTrue(a.window)

    def test_a_web_machine_is_not_offered_the_phones_tools(self):
        fx = load_fx()
        names = [t["name"] for t in fx.machine_status(None)["tools"]]
        self.assertNotIn("appium", names)
        self.assertNotIn("bundletool", names)

    def test_a_reviewed_repository_is_an_app_by_its_own_settings(self):
        fx = load_fx()
        ctx = self.Ctx({"mobile": {"platforms": ["android"], "app": "this"}})
        web = fx.ReviewBoxCtx(ctx, {"primary": False, "name": "fe"})
        app = fx.ReviewBoxCtx(ctx, {"primary": False, "name": "app", "mobile": {"platforms": ["ios"]}})
        self.assertIsNone(fx.mobile_conf(web))
        self.assertEqual(fx.mobile_conf(app)["platforms"], ["ios"])
        self.assertEqual(fx.mobile_conf(fx.ReviewBoxCtx(ctx, {"primary": True}))["platforms"], ["android"])
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "package.json"), "w") as f:
            f.write('{"dependencies": {"react-native": "0.80"}}')
        with self.assertRaises(fx.FxError) as e:
            fx.mobile_missing(web, d)
        self.assertEqual(e.exception.extra["where"], "review.repos[].mobile")

    def test_a_web_project_has_no_mobile_block(self):
        fx = load_fx()
        self.assertIsNone(fx.mobile_conf(self.Ctx({"mobile": {"platforms": []}})))
        self.assertIsNone(fx.mobile_conf(self.Ctx({})))
        self.assertTrue(fx.mobile_conf(self.Ctx({"mobile": {"platforms": ["android"]}})))

    def test_doctor_answers_on_any_machine(self):
        p = subprocess.run([sys.executable, os.path.join(CODE, "fx"), "mobile", "doctor", "--platforms", "android"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300,
                           cwd=tempfile.gettempdir())
        res = json.loads(p.stdout)
        self.assertTrue(res["ok"])
        self.assertEqual(sorted(res["presets"]), ["ends", "full", "standard"])
        self.assertIn("android-sdk", [t["name"] for t in res["tools"]])

    def test_options_after_the_action_are_read(self):
        # an action's options stand after it (a REMAINDER argument once swallowed them)
        p = subprocess.run([sys.executable, os.path.join(CODE, "fx"), "mobile", "devices", "--preset", "nonsense"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300,
                           cwd=tempfile.gettempdir())
        res = json.loads(p.stdout)
        self.assertFalse(res["ok"])
        self.assertIn("nonsense", res["error"])



if __name__ == "__main__":
    unittest.main()
