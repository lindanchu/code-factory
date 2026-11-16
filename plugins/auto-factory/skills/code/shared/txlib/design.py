"""Design packages: the only design source of the UI check is the HTML package (runnable pages) and its Spec.

A package is taken as delivered: its files are never changed and nothing is read from inside the design tool. tx reads
the Spec's state tables (code -> input that opens the state from outside) and writes an index next to the package for
the comparer. "The design must be testable" is a gate: a screen whose state has no code, or whose page cannot take the
web's data, is not compared, and the report says why.
"""

import hashlib
import json
import os
import re
import shutil
import zipfile

from . import core
from .core import TxError

INDEX = "autotest-states.json"
STATE_CODE = re.compile(r"^[A-Z]{1,5}-\d{1,3}$")
PAGE_IN_HEADING = re.compile(r"`([^`]+?\.html)`")
DATA_IN_HEADING = re.compile(r"`([^`]+?\.data\.json)`")


def design_root(repo):
    return repo.path(repo.c("design.dir") or "design-src")


def package_dir(repo, version=None):
    version = version or repo.c("design.current")
    if not version:
        return None
    return os.path.join(design_root(repo), version)


def load_index(repo, version=None):
    d = package_dir(repo, version)
    if not d:
        return None
    return core.read_json(os.path.join(d, INDEX))


def cells(line):
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def is_rule(line):
    return bool(re.match(r"^\s*\|?\s*:?-{2,}", line.replace("|", " | ").strip().lstrip("|").strip()))


def tables(md):
    """Every table of a markdown text: (nearest headings above it, header cells, rows)."""
    out, heads = [], []
    lines = md.split("\n")  # only \n: a cell may hold characters str.splitlines would split on
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            level = len(m.group(1))
            heads = [h for h in heads if h[0] < level] + [(level, m.group(2))]
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = cells(line)
            rows = []
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            out.append(([h[1] for h in heads], header, rows))
            continue
        i += 1
    return out


def col(header, *names):
    low = [h.lower() for h in header]
    for n in names:
        if n.lower() in low:
            return low.index(n.lower())
    return -1


def parse_spec(md, pages):
    """States (code -> page, input, label), data keys per page and the ticket cover table, from a Spec.md."""
    states, data_keys, cover, problems = {}, {}, [], []
    for heads, header, rows in tables(md):
        ci, ii = col(header, "Mã", "Code"), col(header, "Đầu vào", "Input")
        if ci >= 0 and ii >= 0:
            # the nearest heading that holds a page's file name decides whose states these are
            page = None
            for h in reversed(heads):
                m = PAGE_IN_HEADING.search(h)
                if m and m.group(1) in pages:
                    page = m.group(1)
                    break
            if page is None and len(pages) == 1:
                page = pages[0]
            si, li = col(header, "Màn", "Screen"), col(header, "Trạng thái", "State")
            for r in rows:
                if len(r) <= max(ci, ii):
                    continue
                code = r[ci].strip("` ")
                if not STATE_CODE.match(code):
                    continue
                raw = r[ii].strip()
                m = re.match(r"^`(.*)`$", raw)
                try:
                    inp = json.loads(m.group(1) if m else raw)
                except ValueError:
                    problems.append("state %s: its input is not JSON" % code)
                    continue
                if not isinstance(inp, dict):
                    problems.append("state %s: its input must be a JSON object" % code)
                    continue
                if page is None:
                    problems.append("state %s: no heading above its table names the page file (`<Page>.html`)" % code)
                    continue
                if code in states:
                    problems.append("state %s is declared twice" % code)
                    continue
                states[code] = {"page": page, "input": inp, "screen": r[si] if 0 <= si < len(r) else "",
                                "label": r[li] if 0 <= li < len(r) else ""}
            continue
        ki = col(header, "Khoá", "Key")
        if ki >= 0 and col(header, "Hiện ở đâu", "Shown") >= 0:
            wi = col(header, "Hiện ở đâu", "Shown")
            page = None
            for h in reversed(heads):
                m = DATA_IN_HEADING.search(h)
                if m:
                    page = m.group(1)[:-len(".data.json")] + ".html"
                    break
            for r in rows:
                if len(r) > max(ki, wi):
                    data_keys.setdefault(page or "", []).append({"key": r[ki].strip("` "), "shown": r[wi]})
            continue
        ti, mi = col(header, "Ticket"), col(header, "Mã", "Code")
        if ti >= 0 and mi >= 0 and col(header, "Tiêu chí", "Criterion") >= 0:
            qi, vi = col(header, "Tiêu chí", "Criterion"), col(header, "Thấy trên màn", "Seen")
            for r in rows:
                if len(r) > max(ti, mi, qi):
                    cover.append({"ticket": r[ti], "criterion": r[qi], "state": r[mi].strip("` "),
                                  "seen": r[vi].strip("` ") if 0 <= vi < len(r) else ""})
    return states, data_keys, cover, problems


def token_prefix(css):
    counts = {}
    for name in re.findall(r"(--[a-z][a-z0-9]*-)[a-z0-9-]+\s*:", css or ""):
        counts[name] = counts.get(name, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def package_sha(folder):
    h = hashlib.sha256()
    for name in sorted(os.listdir(folder)):
        full = os.path.join(folder, name)
        if name == INDEX or name.startswith(".") or not os.path.isfile(full):
            continue
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(core.sha256_file(full).encode())
        h.update(b"\0")
    return h.hexdigest()


def verify_version_file(folder):
    """VERSION.txt lists size and sha256 of every file: a mismatch means a file changed after the package was made."""
    body = core.read(os.path.join(folder, "VERSION.txt"))
    if body is None:
        return None, []
    wrong, label = [], None
    for line in body.split("\n"):
        m = re.match(r"^(?:dsVersion|version)\s*:\s*(\S+)", line)
        if m:
            label = m.group(1)
        m = re.match(r"^(.+?)\s{2,}(\d+)\s{2,}sha256:([0-9a-f]{64})\s*$", line)
        if not m:
            continue
        full = os.path.join(folder, m.group(1))
        if not os.path.isfile(full):
            wrong.append("%s is listed but missing" % m.group(1))
        elif core.sha256_file(full) != m.group(3):
            wrong.append("%s does not match its listed hash" % m.group(1))
    return label, wrong


def zip_name(info):
    """A member's name as its maker wrote it. Without the UTF-8 flag, zipfile reads the bytes as CP437; many tools (the
    macOS archiver among them) write UTF-8 without the flag, so "Quản trị" would come out as "Quß║ún trß╗ï"."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def unzip(archive, dest):
    """Extract a package. An archive is untrusted data: no path may leave the folder, nothing is run."""
    with zipfile.ZipFile(archive) as z:
        members = {zip_name(i): i for i in z.infolist()}
        names = list(members)
        for n in names:
            if n.startswith("/") or ".." in n.replace("\\", "/").split("/"):
                raise TxError("the archive holds a path that leaves its folder (%s): not extracted" % n, code=core.EXIT_HUMAN)
        os.makedirs(dest, exist_ok=True)
        top = {n.split("/")[0] for n in names if n.strip("/")}
        strip = (top.pop() + "/") if len(top) == 1 and all("/" in n for n in names if n.strip("/")) else ""
        base = os.path.realpath(dest)
        for n in names:
            if n.endswith("/"):
                continue
            rel = n[len(strip):] if strip and n.startswith(strip) else n
            if not rel or rel.startswith("__MACOSX/"):
                continue
            parts = [x for x in rel.replace("\\", "/").split("/") if x not in ("", ".")]
            target = os.path.realpath(os.path.join(base, *parts)) if parts else base
            # "pkg//etc/x" loses its top folder and becomes an absolute path: every name must stay inside
            if not parts or ".." in parts or not target.startswith(base + os.sep):
                raise TxError("the archive holds a path that leaves its folder (%s): not extracted" % n, code=core.EXIT_HUMAN)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(members[n]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def build_index(folder, version, no_data=None):
    files = sorted(os.listdir(folder))
    pages = [f for f in files if f.endswith(".html") and not f.startswith("Spec ")]
    specs = [f for f in files if f.endswith(".md") and "spec" in f.lower()]
    states, data_keys, cover, problems = {}, {}, [], []
    for s in specs:
        st, dk, cv, pr = parse_spec(core.read(os.path.join(folder, s), "") or "", pages)
        for code, v in st.items():
            if code in states:
                problems.append("state %s is declared in more than one Spec file" % code)
            states[code] = v
        for k, v in dk.items():
            data_keys.setdefault(k, []).extend(v)
        cover += cv
        problems += pr
    no_data = dict(no_data or {})
    page_info = {}
    for p in pages:
        data = p[:-len(".html")] + ".data.json"
        has = os.path.isfile(os.path.join(folder, data))
        info = {"data": data if has else "", "takes_data": has and p not in no_data}
        if p in no_data:
            info["why_no_data"] = no_data[p]
        elif not has:
            info["why_no_data"] = "gói không có file dữ liệu mẫu cho trang này (%s)" % data
        page_info[p] = info
    css = core.read(os.path.join(folder, "tokens.css"))
    label, wrong = verify_version_file(folder)
    idx = {"version": version, "label": label or "", "sha256": package_sha(folder),
           "tokens": "tokens.css" if css is not None else "", "token_prefix": token_prefix(css),
           "pages": page_info, "states": states, "data_keys": data_keys, "cover": cover, "indexed": core.today()}
    if not specs:
        problems.append("the package has no Spec.md")
    if not states:
        problems.append("no state with a code and an input was found in the Spec (a table with the columns Mã and Đầu vào)")
    if css is None:
        problems.append("the package has no tokens.css")
    problems += ["VERSION.txt: " + w for w in wrong]
    return idx, problems


def cmd_add(repo, source, version=None, make_current=True):
    """Register a design package: a folder already under the design folder, another folder, or a .zip."""
    source = os.path.abspath(os.path.expanduser(source))
    root = design_root(repo)
    if os.path.isfile(source) and source.lower().endswith(".zip"):
        version = version or re.sub(r"\.zip$", "", os.path.basename(source), flags=re.I)
        dest = os.path.join(root, core.safe_name(version))
        if os.path.isdir(dest) and os.listdir(dest):
            raise TxError("%s already holds a package: give another --version" % os.path.relpath(dest, repo.root), code=core.EXIT_USAGE)
        unzip(source, dest)
        archive_sha = core.sha256_file(source)
    elif os.path.isdir(source):
        version = version or os.path.basename(source.rstrip("/"))
        dest = os.path.join(root, core.safe_name(version))
        if os.path.realpath(dest) != os.path.realpath(source):  # a folder already in its place is indexed again
            if os.path.isdir(dest) and os.listdir(dest):
                raise TxError("%s already holds a package: give another --version" % os.path.relpath(dest, repo.root), code=core.EXIT_USAGE)
            shutil.copytree(source, dest, dirs_exist_ok=True)
        archive_sha = ""
    else:
        raise TxError("%s is neither a folder nor a .zip" % source, code=core.EXIT_USAGE)
    version = core.safe_name(version)
    old = core.read_json(os.path.join(dest, INDEX), {}) or {}
    no_data = {p: i.get("why_no_data") for p, i in (old.get("pages") or {}).items() if i.get("marked_no_data")}
    idx, problems = build_index(dest, version, no_data)
    for p in no_data:
        if p in idx["pages"]:
            idx["pages"][p]["marked_no_data"] = True
    if archive_sha:
        idx["archive_sha256"] = archive_sha
    core.write_json(os.path.join(dest, INDEX), idx)
    if make_current:
        repo.save({"design": {"current": version}})
    repo.journal("design-add", version=version, sha=idx["sha256"], states=len(idx["states"]))
    return {"ok": True, "version": version, "folder": os.path.relpath(dest, repo.root), "sha256": idx["sha256"],
            "pages": idx["pages"], "states": len(idx["states"]), "cover_rows": len(idx["cover"]),
            "token_prefix": idx["token_prefix"], "problems": problems, "current": repo.c("design.current"),
            "next": "design-check"}


def cmd_page(repo, page, no_data=None, takes_data=False, version=None):
    """Record what a trial showed about a page: it does not draw the data it is given (so its screens cannot be compared
    with the web), or it does after all."""
    d = package_dir(repo, version)
    idx = load_index(repo, version)
    if not idx:
        raise TxError("no design package is registered yet (tx design add)", code=core.EXIT_USAGE)
    if page not in idx["pages"]:
        raise TxError("the package has no page %r" % page, code=core.EXIT_USAGE, pages=sorted(idx["pages"]))
    info = idx["pages"][page]
    if takes_data:
        info.pop("marked_no_data", None)
        info.pop("why_no_data", None)
        info["takes_data"] = bool(info.get("data"))
    else:
        if not (no_data or "").strip():
            raise TxError("say why the page cannot take the web's data (--no-data \"...\")", code=core.EXIT_USAGE)
        info.update(takes_data=False, why_no_data=no_data.strip(), marked_no_data=True)
    core.write_json(os.path.join(d, INDEX), idx)
    return {"ok": True, "page": page, "info": info}


def comparable(idx, code):
    """(True, "") when a state can be compared with the web, else (False, why)."""
    st = (idx or {}).get("states", {}).get(code)
    if not st:
        return False, "gói design không có trạng thái %s" % code
    page = idx["pages"].get(st["page"]) or {}
    if not page.get("takes_data"):
        return False, page.get("why_no_data") or "trang design chưa nhận dữ liệu từ ngoài"
    return True, ""


def cmd_check(repo, cases, feature=None):
    """The gate "the design must be testable", per feature: which screens of the cases can be compared, which cannot
    and why. A screen that cannot be compared is tested for what it does only; nothing is compared by guessing."""
    idx = load_index(repo)
    if not idx:
        return {"ok": True, "package": None, "say": "no design package registered: every case is tested for what it "
                "does only, no UI check", "features": {}}
    feats, used = {}, set()
    for key, case in cases.items():
        f = key.split("-")[1]
        if feature and f != feature:
            continue
        row = feats.setdefault(f, {"cases_with_ui": 0, "screens": 0, "comparable": 0, "not_comparable": {}, "missing": []})
        ui = case.get("ui") or []
        if ui:
            row["cases_with_ui"] += 1
        for u in ui:
            code = u.get("state")
            row["screens"] += 1
            used.add(code)
            ok, why = comparable(idx, code)
            if ok:
                row["comparable"] += 1
            elif code not in idx["states"]:
                if code not in row["missing"]:
                    row["missing"].append(code)
            else:
                row["not_comparable"].setdefault(why, [])
                if code not in row["not_comparable"][why]:
                    row["not_comparable"][why].append(code)
    unused = sorted(set(idx["states"]) - used)
    return {"ok": True, "package": idx["version"], "sha256": idx["sha256"], "states": len(idx["states"]),
            "features": feats, "states_no_case_goes_through": unused[:80], "states_no_case_total": len(unused),
            "pages": idx["pages"]}
