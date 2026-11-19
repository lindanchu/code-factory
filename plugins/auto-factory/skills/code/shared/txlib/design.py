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
import unicodedata
import zipfile

from . import core
from .core import TxError

INDEX = "autotest-states.json"
READER = 2  # how a Spec is read; an index made by another reading has its coverage read again (complete_index)
STATE_CODE = re.compile(r"^[A-Z]{1,5}-\d{1,3}$")
# A state code cited inside a sentence ("AD-7", "`AD-7`"), not part of a longer key such as a ticket's C2P3-225.
CITED_CODE = re.compile(r"(?<![A-Za-z0-9-])([A-Z]{1,5}-\d{1,3})(?![0-9A-Za-z])")
OPEN_ISSUE = re.compile(r"(?<![A-Za-z0-9])OI(?:-\d+)?(?![A-Za-z0-9])")  # "OI": an issue the design has not settled
DESIGN_LACKS = re.compile(r"\b(chua co design|design chua co)\b")  # the same, said in words (on the folded answer)
NOT_APPLICABLE = re.compile(r"^(không áp dụng|khong ap dung|not applicable|n/a)(?![\w])\W*(.*)$", re.I | re.S)
UNANSWERED = re.compile(r"^(\?+|-+|—+|–+|\.{3,}|…+|tbd|todo|chua( co| ro| tra loi)?)?$")  # on the folded answer
# The thirteen groups of the Spec's edge questions (design-qa 2.25 and later): (code, name, how a group's cell starts).
# design-qa reads a group by the start of its cell, with its marks ("Nút chính", "File (mẫu)" are the groups Nút and
# File; "Dòng trống" is not Đóng), so this does too. The answers of those that show something on the screen cite a
# drawn state.
EDGE_GROUPS = [("limits", "Giới hạn", r"giới hạn|limits?"), ("counts", "Số đếm", r"số đếm|counts?"),
               ("entry", "Điểm vào", r"điểm vào|entry|ways? in"), ("rights", "Quyền", r"quyền|permissions?|rights?"),
               ("loading", "Tải dữ liệu", r"tải dữ liệu|data loading|loading"),
               ("derived", "Dữ liệu suy ra", r"dữ liệu suy ra|derived"), ("required", "Bắt buộc", r"bắt buộc|required"),
               ("files", "File", r"files?"), ("edges", "Biên", r"biên|edges?"),
               ("close", "Đóng / huỷ", r"đóng|close|cancel"), ("text", "Chữ và tooltip", r"chữ|texts?"),
               ("buttons", "Nút", r"nút|buttons?"), ("filters", "Lọc", r"lọc|filters?")]
SHOWN_GROUPS = {"limits", "loading", "rights", "edges", "entry", "filters"}
# The kinds of state design-qa asks a screen for, by what it holds (its state matrix, Ma trận trạng thái), folded.
STATE_KINDS = {"mac dinh", "mat ket noi", "thieu quyen", "dang tai", "rong", "loi tai", "dang loc", "khong khop",
               "nhieu trang", "loi o", "dang luu", "luu xong", "loi may chu", "chua luu", "roi trang khi chua luu",
               "khong tim thay", "chi xem", "xac nhan xoa", "dang xoa", "xoa bi chan", "dang doc file", "sai loai file",
               "file qua lon", "vuot so dong", "file hong", "file co loi", "file hop le", "dang xuat", "xuat loi"}
# The headings of the three sections of the Spec's own coverage. design-qa knows each by its columns alone; a table
# with the same columns under another heading counts when its rows speak the section's words (below), so a table of a
# component's kinds and states is not one of them.
SECTIONS = {"edges": re.compile(r"^(cau hoi bien|edge questions?)\b"),
            "matrix": re.compile(r"^(ma tran trang thai|state matrix)\b"),
            "flows": re.compile(r"^(luong|flows?)\b")}
# The first line of a package's CHANGES.md: "# <name> — gói v1.2 (18.11.2026)".
PACKAGE_VERSION = re.compile(r"(?:gói|goi|package)\s+v(\d+(?:\.\d+){1,3})(?:\s*\(\s*([\d./-]{1,20})\s*\))?", re.I)
_COVERAGE = {}  # (index path, sha256) -> the coverage read from the Spec of a package indexed by an older tx
_REFRESHED = {}  # (index path, sha256) -> the index made again by this reading (None: its files changed since)


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
    return complete_index(d, core.read_json(os.path.join(d, INDEX)), write=True)


def complete_index(folder, idx, write=False):
    """An index made by another reading of the Spec (an older tx: `reader` says which) is made again from the same
    files, keeping what was not read from them, and written when `write`; so every command, and fx, read one table.
    When the files are no longer the ones it was made from, it is kept, with its coverage and the package's version
    read again in memory."""
    if not isinstance(idx, dict) or not isinstance(idx.get("states"), dict):
        return idx
    stale = idx.get("reader") != READER
    key = (os.path.join(folder, INDEX), idx.get("sha256"))
    if stale and key not in _REFRESHED:
        _REFRESHED[key] = refreshed(folder, idx)
    if stale and _REFRESHED[key] is not None:
        if write:
            core.write_json(key[0], _REFRESHED[key])
        return json.loads(json.dumps(_REFRESHED[key]))  # each caller its own: one may mark a page in it
    if stale or "coverage" not in idx:
        if key not in _COVERAGE:
            _COVERAGE[key] = read_coverage(folder, idx)
        idx["coverage"] = _COVERAGE[key]
    if stale or "package_version" not in idx:
        idx.update(package_version(folder))
    return idx


def refreshed(folder, old):
    """The index of a package made again by this reading, or None when its files are not the ones `old` was made
    from. What a trial found out about a page, the archive it came from and what it replaced are kept."""
    try:
        if package_sha(folder) != old.get("sha256"):
            return None
    except OSError:
        return None
    marks = {p: i.get("why_no_data") for p, i in (old.get("pages") or {}).items()
             if isinstance(i, dict) and i.get("marked_no_data")}
    idx, _problems = build_index(folder, str(old.get("version") or os.path.basename(folder.rstrip(os.sep))), marks)
    for p in marks:
        if p in idx["pages"]:
            idx["pages"][p]["marked_no_data"] = True
    for k in ("archive_sha256", "replaces"):
        if k in old:
            idx[k] = old[k]
    return idx


def package_version(folder):
    """A package's own version and date, as the first line of its CHANGES.md gives them ("# <name> — gói v1.2
    (18.11.2026)"); empty when it has none. A ticket names the package it was written from by this version. The file
    comes from outside: whatever its bytes are, reading it never stops a command."""
    try:
        with open(os.path.join(folder, "CHANGES.md"), "rb") as f:
            raw = f.read(4096)
    except OSError:
        raw = b""
    text = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    first = unicodedata.normalize("NFC", text.lstrip("\ufeff").split("\n")[0])
    m = PACKAGE_VERSION.search(first)
    return {"package_version": m.group(1) if m else "", "package_date": (m.group(2) or "").strip() if m else ""}


def version_words(idx):
    """How a prompt names the package's own version."""
    if not (idx or {}).get("package_version"):
        return "no version of its own found (the first line of its CHANGES.md gives it)"
    return "its own version v%s%s" % (idx["package_version"], " (%s)" % idx["package_date"] if idx.get("package_date") else "")


def coverage_words(idx):
    """How a prompt says what the Spec's own coverage holds, or None when the Spec has none."""
    n = coverage_counts(idx)
    if not (n["edge_questions"] or n["state_matrix"] or n["flows"]):
        return None
    return ("%d answers to its edge questions (Câu hỏi biên), %d rows of its state matrix (Ma trận trạng thái: the "
            "states each screen must have) and %d flows (Luồng: what each button leads to); %d row(s) are not ready "
            "(`open`: an OI, no answer, no drawn state): the design is not finished there" % (
                n["edge_questions"], n["state_matrix"], n["flows"], n["not_ready"]))


def read_coverage(folder, idx):
    """The Spec's own coverage of a package already indexed (parse_coverage over its Spec files)."""
    coverage = {"edges": [], "matrix": [], "flows": []}
    pages = sorted((idx.get("pages") or {}).keys())
    for s in sorted(f for f in os.listdir(folder) if f.endswith(".md") and "spec" in f.lower()):
        cov, _problems = parse_coverage(core.read(os.path.join(folder, s), "") or "", pages, idx["states"])
        for k in coverage:
            coverage[k] += cov[k]
    return coverage


def cells(line):
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def is_rule(line):
    return bool(re.match(r"^\s*\|?\s*:?-{2,}", line.replace("|", " | ").strip().lstrip("|").strip()))


def tables(md):
    """Every table of a markdown text: (headings above it as (level, text), nearest last; header cells; rows)."""
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
            out.append((list(heads), header, rows))
            continue
        i += 1
    return out


def clean(cell):
    """A header cell, a name or a code as design-qa reads it: no backticks or asterisks, in one Unicode form."""
    return unicodedata.normalize("NFC", re.sub(r"[`*]", "", str(cell or ""))).strip()


def col(header, *names):
    low = [clean(h).lower() for h in header]
    for n in names:
        if clean(n).lower() in low:
            return low.index(clean(n).lower())
    return -1


def fold(text):
    """Lower case, without Vietnamese marks and with one space between words: a heading, a kind of state or an
    answer reads the same whichever way it was typed ("xoá" and "xóa", "Chưa rõ" and "chua ro")."""
    t = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "D")
    return re.sub(r"\s+", " ", "".join(c for c in t if not unicodedata.combining(c))).strip().lower()


def group_code(name):
    """Which of the thirteen groups a cell names, or None: by how it starts, with its marks ("Nút chính" is the group
    Nút, "Dòng trống" is not Đóng), or by its whole name typed without them ("Gioi han")."""
    key = re.sub(r"^[\W\d_]+", "", clean(name).lower())
    for code, _name, starts in EDGE_GROUPS:
        if re.match(r"(?:%s)\b" % starts, key):
            return code
    bare = re.sub(r"[^a-z0-9]", "", fold(name))
    for code, vi, _starts in EDGE_GROUPS:
        if bare == re.sub(r"[^a-z0-9]", "", fold(vi)):
            return code
    return None


def pages_named(heading, pages):
    """The pages a heading names by their file ("<Page>.html" or "<Page>.data.json"), in the order it names them. In
    backticks, the name is the whole of what they hold. Outside them, a longer name is looked for first and taken out
    ("Levels Form.html" does not name the page "Form.html"), and a name inside a longer word names nothing ("QA.html"
    is not the page "A.html"). A heading with only the screen's name, without the file's ending, names none."""
    h = unicodedata.normalize("NFC", str(heading or ""))
    names, found = {}, {}
    for p in pages:
        base = unicodedata.normalize("NFC", p[:-len(".html")] if p.endswith(".html") else p)
        names[base + ".data.json"] = names[base + ".html"] = p
    for m in re.finditer(r"`([^`]+)`", h):
        if m.group(1).strip() in names:
            found.setdefault(names[m.group(1).strip()], m.start())
    # the rest of the heading, each character kept in its place
    rest = re.sub(r"`[^`]*`", lambda m: "\0" * len(m.group(0)), h).replace("*", "\0")
    for name in sorted(names, key=lambda x: (-len(x), x)):
        m = re.search(r"(?<![\w.-])" + re.escape(name), rest)
        if m:
            found.setdefault(names[name], m.start())
            rest = rest.replace(name, "\0" * len(name))
    return sorted(found, key=found.get)


def page_of(heads, pages):
    """The page a table of the Spec is about: the nearest heading above it that names a page's file, or the only page
    of the package."""
    for _level, text in reversed(heads):
        hit = pages_named(text, pages)
        if hit:
            return hit[0]
    return pages[0] if len(pages) == 1 else None


def answer_state(text):
    """(why it does not apply, why it is not ready) of an answer of the Spec: a real answer is ("", "")."""
    t = clean(text).strip("_ ").strip()
    if UNANSWERED.match(fold(t)):
        return "", "no answer"
    m = NOT_APPLICABLE.match(t)
    if m:
        why = m.group(2).strip()
        return (why, "") if len(why) >= 5 else ("", "\"not applicable\" without a reason")
    return "", ""


def speaks(kind, rows, first, answer, states):
    """Whether a table with a section's columns, under a heading that does not name the section, is that section all
    the same, by what its rows say: three of the thirteen groups; three of the kinds of state, one with a drawn state;
    or results that mostly lead somewhere the section would say (a drawn state, "not applicable" with its reason, an
    OI). A table of a component's kinds, or of what each key does, says none of these."""
    rows = [r for r in rows if len(r) > max(first, answer) and any(c.strip() for c in r)]
    cites = [r for r in rows if any(c in states for c in CITED_CODE.findall(r[answer]))]
    if kind == "edges":
        return len({group_code(r[first]) for r in rows} - {None}) >= 3
    if kind == "matrix":
        kinds = {fold(clean(k)) for r in rows for k in re.split(r"[,·;]", r[first])}
        return len(kinds & STATE_KINDS) >= 3 and bool(cites)
    said = [r for r in rows if r in cites or answer_state(r[answer])[0] or OPEN_ISSUE.search(r[answer])]
    return bool(cites) and 2 * len(said) >= len(rows)


def parse_coverage(md, pages, states):
    """The Spec's own coverage, when it has it (design-qa 2.25 and later): its edge questions (Nhóm | Câu hỏi | Trả
    lời), its state matrix (Loại | Trạng thái | Ghi chú) and its flows (Hành động | Kết quả), one table per screen.
    It is read the way design-qa checks it, so a package that passes there is ready here: a table is known by its
    columns, a group by how its cell starts, a screen by the nearest heading that names its file. Each row keeps the
    drawn states it cites and, when the design is not finished there, why (`open`: an OI, no answer, "not applicable"
    with no reason, no drawn state, or a code the state tables do not have). Returns (coverage, problems)."""
    cov, problems = {"edges": [], "matrix": [], "flows": []}, []
    prefixes = {c.split("-")[0] for c in states}
    asked = {}  # page (or the heading, when no page is named) -> the groups its edge questions answer
    for heads, header, rows in tables(md):
        if col(header, "Mã", "Code") >= 0 and col(header, "Đầu vào", "Input") >= 0:
            continue  # a state table
        gi, qi, ai = col(header, "Nhóm", "Group"), col(header, "Câu hỏi", "Question"), col(header, "Trả lời", "Answer")
        ki, si, ni = col(header, "Loại", "Kind"), col(header, "Trạng thái", "State"), col(header, "Ghi chú", "Note")
        hi, ri = col(header, "Hành động", "Action"), col(header, "Kết quả", "Result")
        if gi >= 0 and ai >= 0:
            kind, first, answer = "edges", gi, ai
        elif ki >= 0 and si >= 0:
            kind, first, answer = "matrix", ki, si
        elif hi >= 0 and ri >= 0:
            kind, first, answer = "flows", hi, ri
        else:
            continue
        titles = [t for _level, t in heads]
        if not any(SECTIONS[kind].match(re.sub(r"^[\W\d_]+", "", fold(t))) for t in titles) \
                and not speaks(kind, rows, first, answer, states):
            continue
        need = max(first, answer)
        page = page_of(heads, pages) or ""
        where = titles[-1] if titles else ""
        groups = asked.setdefault(page or where, set()) if kind == "edges" else None
        for r in rows:
            if len(r) <= need or not any(c.strip() for c in r):
                continue
            # the states a row leads to are in its answer; a note or a question may name a ticket of the same prefix
            cited = list(dict.fromkeys(CITED_CODE.findall(r[answer])))
            codes = [c for c in cited if c in states]
            unknown = [c for c in cited if c not in states and c.split("-")[0] in prefixes]
            na, why = answer_state(r[answer])
            # an OI is the answer's own: one named in a note or a question says where the answer came from
            if not why and (OPEN_ISSUE.search(r[answer]) or DESIGN_LACKS.search(fold(r[answer]))):
                why = "an OI: the design has not settled it"
            if not why and unknown:
                why = "cites %s, which the package's state tables do not have" % ", ".join(unknown)
            group = group_code(r[gi]) if kind == "edges" else None
            if group:
                groups.add(group)
            if not why and not na and not codes and (kind != "edges" or group in SHOWN_GROUPS):
                why = "names no drawn state"
            row = {"page": page, "where": where, "codes": codes}
            beside = " ".join(c for i, c in enumerate(r) if i != answer)
            if not why and OPEN_ISSUE.search(beside):
                # design-qa calls the row ready; whether the note says "settled" or "still open" is for a reader
                row["mentions"] = sorted(set(OPEN_ISSUE.findall(beside)))
            if kind == "edges":
                row.update(group=clean(r[gi]), question=r[qi] if 0 <= qi < len(r) else "", answer=r[ai])
            elif kind == "matrix":
                row.update(kinds=[clean(k) for k in re.split(r"[,·;]", r[ki]) if clean(k)], state=r[si],
                           note=r[ni] if 0 <= ni < len(r) else "")
            else:
                row.update(action=r[hi], result=r[ri])
            if na:
                row["na"] = na
            if why:
                row["open"] = why
            cov[kind].append(row)
    # as design-qa counts them: the thirteen groups once for each page file, over every table of that page
    for label, groups in asked.items():
        missing = [name for code, name, _starts in EDGE_GROUPS if code not in groups]
        if missing:
            problems.append("the edge questions of %s have no row for: %s" % (label or "the Spec", ", ".join(missing)))
    for r in open_rows(cov):
        problems.append("not ready in the design: %s: %s" % (row_label(r), r["open"]))
    return cov, problems


def open_rows(cov):
    return [r for kind in ("edges", "matrix", "flows") for r in (cov or {}).get(kind) or [] if r.get("open")]


def row_label(r):
    """A row of the Spec's coverage as a person finds it again: its section, its words, its screen."""
    if "question" in r:
        what = "the edge question %s · %s" % (r.get("group") or "", r.get("question") or "")
    elif "kinds" in r:
        what = "the state matrix row %s" % ", ".join(r.get("kinds") or [])
    else:
        what = "the flow %s" % (r.get("action") or "")
    return "%s (%s)" % (what, r.get("where") or r.get("page") or "Spec")


def coverage_rows(idx):
    """Every row of the Spec's own coverage by what it is about, (section, page, its words, which of them in turn) ->
    row: the same row of two packages has the same key, whatever its answer. Two rows about the same thing (the same
    kind of state on two screens of one page) are told apart by their order."""
    out, seen = {}, {}
    for kind in ("edges", "matrix", "flows"):
        for r in ((idx or {}).get("coverage") or {}).get(kind) or []:
            if kind == "edges":
                words = "%s · %s" % (fold(r.get("group")), fold(r.get("question")))
            elif kind == "matrix":
                words = ", ".join(sorted(fold(k) for k in r.get("kinds") or []))
            else:
                words = fold(r.get("action"))
            key = (kind, r.get("page") or "", words)
            seen[key] = seen.get(key, 0) + 1
            out[key + (seen[key],)] = r
    return out


def package_changes(old, new):
    """What a package changes against the one that was current before it, for whoever reads the rules and designs the
    cases: the states that came, went or are opened another way; the rows of the Spec's own coverage that came, went,
    say something else, or are ready now and were not; and the pages all of that is on. With no package before it
    (`from` is empty), everything it has is new. None when it is the same package."""
    if not isinstance(old, dict) or not isinstance(old.get("states"), dict):
        old = {"states": {}}
    elif old.get("sha256") == new.get("sha256"):
        return None
    so, sn = old["states"], new["states"]
    added, removed = sorted(set(sn) - set(so)), sorted(set(so) - set(sn))
    reopened = sorted(c for c in set(so) & set(sn)
                      if (so[c].get("page"), so[c].get("input")) != (sn[c].get("page"), sn[c].get("input")))
    ro, rn = coverage_rows(old), coverage_rows(new)

    def said(r):
        return r.get("answer"), r.get("state"), r.get("result"), r.get("open")
    ready = sorted(k for k in set(ro) & set(rn) if ro[k].get("open") and not rn[k].get("open"))
    other = sorted(k for k in set(ro) & set(rn) if k not in ready and said(ro[k]) != said(rn[k]))
    came, went = sorted(set(rn) - set(ro)), sorted(set(ro) - set(rn))
    pages = {(sn.get(c) or so.get(c) or {}).get("page") for c in added + removed + reopened}
    pages |= {k[1] for k in ready + other + came + went}
    return {"from": old.get("version") or "", "from_package_version": old.get("package_version") or "",
            "states_added": added, "states_removed": removed, "states_reopened": reopened,
            "rows_now_ready": [row_label(rn[k]) for k in ready], "rows_changed": [row_label(rn[k]) for k in other],
            "rows_added": [row_label(rn[k]) for k in came], "rows_removed": [row_label(ro[k]) for k in went],
            "pages": sorted(x for x in pages if x)}


def features_touched(changes, cases, states):
    """The features whose cases compare a screen a new package changed: a state that went or is opened another way,
    or any state of a page whose states or rows of the Spec changed. With no package before it, every feature."""
    from . import cases as cases_mod
    if not changes:
        return []
    if not changes["from"]:  # the first package: every feature's cases get their UI check now
        return sorted({cases_mod.feature_of(key) for key in cases})
    gone, pages = set(changes["states_removed"]) | set(changes["states_reopened"]), set(changes["pages"])
    return sorted({cases_mod.feature_of(key) for key, case in cases.items() for u in case.get("ui") or []
                   if u.get("state") in gone or (states.get(u.get("state")) or {}).get("page") in pages})


def changes_words(idx, limit=12):
    """How a prompt says what the package changed against the one before it, or None. State codes and counts only:
    the words of a row are the package's own text, and stay in the index (`replaces`), where they are read as data."""
    ch = (idx or {}).get("replaces")
    if not ch or not ch.get("from"):
        return None

    def some(items):
        return ", ".join(items[:limit]) + (" and %d more" % (len(items) - limit) if len(items) > limit else "")
    parts = [("states that are new: %s" % some(ch["states_added"]), ch["states_added"]),
             ("states it no longer has: %s" % some(ch["states_removed"]), ch["states_removed"]),
             ("states opened another way: %s" % some(ch["states_reopened"]), ch["states_reopened"]),
             ("%d row(s) of the Spec's coverage ready now that were not" % len(ch["rows_now_ready"]), ch["rows_now_ready"]),
             ("%d row(s) that say something else" % len(ch["rows_changed"]), ch["rows_changed"]),
             ("%d new row(s)" % len(ch["rows_added"]), ch["rows_added"]),
             ("%d row(s) it no longer has" % len(ch["rows_removed"]), ch["rows_removed"])]
    said = "; ".join(text for text, items in parts if items)
    return "It replaced %s%s: %s" % (ch["from"], " (v%s)" % ch["from_package_version"] if ch.get("from_package_version") else "",
                                     said or "its states and the rows of its Spec are the same; only how its pages draw may differ")


def parse_spec(md, pages):
    """States (code -> page, input, label), data keys per page and the ticket cover table, from a Spec.md."""
    states, data_keys, cover, problems = {}, {}, [], []
    for heads, header, rows in tables(md):
        ci, ii = col(header, "Mã", "Code"), col(header, "Đầu vào", "Input")
        if ci >= 0 and ii >= 0:
            # the nearest heading that holds a page's file name decides whose states these are
            page = page_of(heads, pages)
            si, li = col(header, "Màn", "Screen"), col(header, "Trạng thái", "State")
            for r in rows:
                if len(r) <= max(ci, ii):
                    continue
                code = clean(r[ci])
                if not STATE_CODE.match(code):
                    continue
                raw = re.sub(r"^`+|`+$", "", r[ii].strip()).replace("\\|", "|").strip()  # a | inside a cell is written \|
                try:
                    inp = json.loads(raw)
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
        ki = col(header, "Khoá", "Khóa", "Key")
        if ki >= 0 and col(header, "Hiện ở đâu", "Shown") >= 0:
            wi = col(header, "Hiện ở đâu", "Shown")
            page = page_of(heads, pages)
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
    texts = {s: core.read(os.path.join(folder, s), "") or "" for s in specs}
    for s in specs:
        st, dk, cv, pr = parse_spec(texts[s], pages)
        for code, v in st.items():
            if code in states:
                problems.append("state %s is declared in more than one Spec file" % code)
            states[code] = v
        for k, v in dk.items():
            data_keys.setdefault(k, []).extend(v)
        cover += cv
        problems += pr
    coverage = {"edges": [], "matrix": [], "flows": []}
    for s in specs:  # once every state is known: a row may cite a state another Spec file declares
        cov, pr = parse_coverage(texts[s], pages, states)
        for k in coverage:
            coverage[k] += cov[k]
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
           "pages": page_info, "states": states, "data_keys": data_keys, "cover": cover, "coverage": coverage,
           "indexed": core.today()}
    idx.update(package_version(folder), reader=READER)
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
    try:
        before = load_index(repo)  # the package that is current now: what this one replaces
    except TxError:
        before = None  # its index cannot be read: nothing to compare with
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
    changed = package_changes(before, idx)
    if changed and changed["from"]:
        idx["replaces"] = changed
    elif old.get("replaces") and old.get("sha256") == idx["sha256"]:
        idx["replaces"] = old["replaces"]  # the same package, indexed again
    core.write_json(os.path.join(dest, INDEX), idx)
    if make_current:
        repo.save({"design": {"current": version}})
    repo.journal("design-add", version=version, sha=idx["sha256"], states=len(idx["states"]))
    return {"ok": True, "version": version, "folder": os.path.relpath(dest, repo.root), "sha256": idx["sha256"],
            "pages": idx["pages"], "states": len(idx["states"]), "cover_rows": len(idx["cover"]),
            "coverage": coverage_counts(idx), "package_version": idx["package_version"],
            "package_date": idx["package_date"], "token_prefix": idx["token_prefix"], "problems": problems,
            "changed": changed, "current": repo.c("design.current"), "next": "design-check"}


def coverage_counts(idx):
    """How much of the Spec's own coverage a package has: rows of its edge questions, its state matrix and its flows,
    and how many of them are not ready."""
    cov = (idx or {}).get("coverage") or {}
    return {"edge_questions": len(cov.get("edges") or []), "state_matrix": len(cov.get("matrix") or []),
            "flows": len(cov.get("flows") or []), "not_ready": len(open_rows(cov))}


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
    from . import cases as cases_mod
    covered = {u.get("state") for c in cases.values() if cases_mod.is_active(c) for u in c.get("ui") or []}
    gaps = {}
    for f in sorted(set(feats) | ({feature} if feature else set())):
        fc = feature_coverage(repo, idx, cases, f, covered)
        if fc is None:
            continue
        feats.setdefault(f, {"cases_with_ui": 0, "screens": 0, "comparable": 0, "not_comparable": {}, "missing": []})
        feats[f]["design_coverage"] = fc
        if fc["uncovered"]:
            gaps[f] = len(fc["uncovered"])
    res = {"ok": not gaps, "package": idx["version"], "sha256": idx["sha256"], "states": len(idx["states"]),
           "features": feats, "states_no_case_goes_through": unused[:80], "states_no_case_total": len(unused),
           "pages": idx["pages"]}
    if gaps:
        raise TxError("the Spec names drawn states that no case reaches, for screens of %s: %s. Give each a case whose "
                      "`ui` compares it, or say in the matrix file's `states_na` why it is not this feature's" % (
                          ", ".join(sorted(gaps)), "; ".join("%s: %d" % kv for kv in sorted(gaps.items()))),
                      code=core.EXIT_GATE, next="design", **res)
    return res


def feature_coverage(repo, idx, cases, feature, covered):
    """What the Spec's own coverage asks of one feature, or None when the Spec has none. The screens a feature works
    are those its cases compare (page and screen of each state in their `ui`). Each drawn state that a row of the Spec
    cites for those screens (a state of the matrix, a result of a flow, an answer of an edge question) is reached by a
    case of any feature, or the feature's matrix says why not (`states_na`). Rows of those screens that are not ready
    are listed: each is a question for the design side."""
    from . import cases as cases_mod
    rows = [r for k in ("edges", "matrix", "flows") for r in (idx.get("coverage") or {}).get(k) or []]
    if not rows:
        return None
    states = idx["states"]

    def screen(code):
        st = states.get(code) or {}
        return st.get("page"), st.get("screen") or ""
    mine = {u.get("state") for k, c in cases.items() if cases_mod.feature_of(k) == feature and cases_mod.is_active(c)
            for u in c.get("ui") or []}
    touched = {screen(c) for c in mine if c in states}
    pages = {p for p, _s in touched}
    m = core.read_json(cases_mod.matrix_path(repo, feature), {}) or {}
    said = (m.get("states_na") if isinstance(m, dict) else None) or {}
    na = {k for k, v in (said.items() if isinstance(said, dict) else []) if len(str(v or "").strip()) >= 8}
    required, not_ready = {}, []
    for r in rows:
        on_screen = [c for c in r["codes"] if screen(c) in touched]
        if r.get("open") and (on_screen or (not r["codes"] and r["page"] in pages)):
            not_ready.append({"row": row_label(r), "why": r["open"]})
        for c in on_screen:  # a drawn state is drawn, even in a row the design has not settled yet
            required.setdefault(c, row_label(r))
    uncovered, cannot = [], []
    for c in sorted(required):
        if c not in covered and c not in na:
            (uncovered if comparable(idx, c)[0] else cannot).append(c)
    return {"cited": len(required), "reached": sum(1 for c in required if c in covered),
            "explained": sorted(c for c in required if c in na and c not in covered),
            "uncovered": [{"state": c, "row": required[c]} for c in uncovered],
            "cited_not_comparable": cannot, "not_ready": not_ready}
