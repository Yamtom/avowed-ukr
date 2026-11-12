#!/usr/bin/env python3
"""Avowed l10n: batch export / validated import / stats / QA for Ukrainian translations (stdlib only).

Translators (sub-agents) NEVER touch SQLite directly: they read `export`ed JSONL batches and write results with
`import`, which validates every row and serializes writes (BEGIN IMMEDIATE + busy_timeout, WAL) so any number of
agents can import in parallel.

Scope = D11 (all) + game/* except game/codex.  Row status: new -> draft (imported, auto-checks passed)
-> reviewed (QA ok) | needs_fix (check failed, reason in qa_note) | skipped (nothing to translate).

Commands
  migrate                      add qa_note + glossary table, recompute has_gender_variant, auto-skip empty rows
  export [--fix] [--statuses]  write batches  logs/batches/in/NNN_<cat>.jsonl (+ manifest.json)
  import FILE [--force]        validated import of a JSONL file written by a translator
  validate                     re-run all checks over every translated row
  stats                        per-category table (<= 25 lines)
  sample --pct 2.5             stratified QA sample -> logs/qa/in/QNN.jsonl
  review FILE                  apply QA verdicts
  candidates                   glossary candidate terms -> logs/glossary_candidates.tsv
  glossary-import FILE         JSONL {en,uk,uf,ux,kind,note} -> table glossary
  glossary-md                  docs/glossary_uk.md from the table
  apply-glossary               rows whose English text IS a glossary term are filled from the glossary
  audit-glossary [--mark]      translated rows that lack the stem of a glossary term present in the English text

Translator I/O (one JSON object per line)
  in : {"id","en","en_f","en_x","pl","pl_f","pl_x","ru","need":"FX"}  (+ "uk","uf","ux","note" in --fix batches)
  out: {"id","uk","uf","ux"}   or   {"id","keep":1}  (proper names / codes: copy the source unchanged)
"""
import argparse
import collections
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "db" / "avowed_l10n.db"
LOGS = REPO / "logs"
DOCS = REPO / "docs"
SCOPE = "(source_file='D11' OR (category LIKE 'game/%' AND category!='game/codex'))"
PRIORITY = ["D11", "game/gui", "game/inputbindings", "game/tooltips", "game/tutorial", "game/characterstats",
            "game/abilities", "game/statuseffects", "game/deities", "game/locations", "game/waypoints",
            "game/characters", "game/bestiary", "game/achievements", "game/lore", "game/oeiunreal",
            "game/oeiphotomode", "game/loadingtips", "game/items", "game/credits"]

TAG = r"</?[A-Za-z][^<>]*?/?>|</>"
VAR = r"\{[^{}]*\}"
ICU = r"\|(?:plural|ordinal|gender|select)\([^()]*\)"
PROT = re.compile("(" + "|".join((TAG, VAR, ICU)) + ")")
ICU_KEYS = {"zero", "one", "two", "few", "many", "other"}
CYR = re.compile("[А-Яа-яІіЇїЄєҐґ]")
RU_ONLY = re.compile("[ыэъёЫЭЪЁ]")
PL_CHARS = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")

SCHEMA_GLOSSARY = """
CREATE TABLE IF NOT EXISTS glossary (
    en        TEXT PRIMARY KEY COLLATE NOCASE,
    uk        TEXT NOT NULL,
    uk_female TEXT NOT NULL DEFAULT '',
    uk_extra  TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT 'term',
    note      TEXT NOT NULL DEFAULT '',
    pl        TEXT NOT NULL DEFAULT '',
    ru        TEXT NOT NULL DEFAULT '',
    freq      INTEGER NOT NULL DEFAULT 0
)"""

SEED = [  # mandated by the project owner: (en, uk, uk_female, uk_extra, kind, note)
    ("Living Lands", "Живі Землі", "", "", "place", ""),
    ("Aedyr", "Едір", "", "", "place", "прикметник Aedyran — едірський"),
    ("Envoy", "Посланець", "Посланниця", "Посланець/Посланниця", "title", "титул гравця"),
    ("Godlike", "Богоподібний", "Богоподібна", "Богоподібний/Богоподібна", "term", "раса/ознака"),
    ("Adra", "Адра", "", "", "term", "кристалічна/магічна речовина; не змінювати"),
    ("Essence", "Есенція", "", "", "term", ""),
    ("Dreamscourge", "Сновидна Моровиця", "", "", "term", "хвороба"),
    ("Paradis", "Парадіс", "", "", "place", ""),
    ("Woedica", "Воедіка", "", "", "deity", ""),
    ("Sapadal", "Сападал", "", "", "deity", ""),
    ("Kith", "Кіти", "", "", "term", "народи Eora; множина; однина — кіт (з малої в загальному значенні не вживати)"),
    ("Orlan", "Орлан", "", "", "term", "раса"),
    ("AuMaua", "АуМауа", "", "", "term", "раса; також Aumaua"),
]


# =============================================================================================== DB
def connect():
    con = sqlite3.connect(DB, timeout=120, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=120000")
    return con


def group_of(r):
    return "D11" if r["source_file"] == "D11" else r["category"]


def prio(r):
    g = group_of(r)
    return PRIORITY.index(g) if g in PRIORITY else len(PRIORITY)


def need_flags(r):
    return (bool(r["en_female"] or r["pl_female"] or r["ru_female"]), bool(r["en_extra"] or r["pl_extra"] or r["ru_extra"]))


def ref_text(r, variant):
    """Source text a translation variant (default/female/extra) is checked against."""
    v = {"d": "default", "f": "female", "x": "extra"}[variant]
    for lg in ("en", "pl"):
        for x in (v, "default"):
            if r[f"{lg}_{x}"]:
                return r[f"{lg}_{x}"]
    return ""


# =============================================================================================== validation
def normalize(uk, ref):
    s = uk
    for ch in "’ʼ‘´`":
        s = s.replace(ch, "'")
    s = s.replace("\r\n", "\n")
    parts = PROT.split(s)
    for i in range(0, len(parts), 2):
        p = parts[i]
        for bad in (" -- ", " - ", " – "):
            p = p.replace(bad, " — ")
        parts[i] = p
    s = "".join(parts)
    lead = ref[:len(ref) - len(ref.lstrip())]
    trail = ref[len(ref.rstrip()):]
    s = lead.replace("\r\n", "\n") + s.strip() + trail.replace("\r\n", "\n")
    if "\r\n" in ref:
        s = s.replace("\n", "\r\n")
    return s


def plain_of(s):
    return "".join(PROT.split(s)[0::2])


def check_variant(uk, ref, label):
    p = []
    if not uk.strip():
        return [f"{label}: empty"]
    if collections.Counter(re.findall(TAG, uk)) != collections.Counter(re.findall(TAG, ref)):
        a, b = collections.Counter(re.findall(TAG, ref)), collections.Counter(re.findall(TAG, uk))
        p.append(f"{label}: tags differ, missing {list((a - b).elements())[:3]} extra {list((b - a).elements())[:3]}")
    if collections.Counter(re.findall(VAR, uk)) != collections.Counter(re.findall(VAR, ref)):
        a, b = collections.Counter(re.findall(VAR, ref)), collections.Counter(re.findall(VAR, uk))
        p.append(f"{label}: variables differ, missing {list((a - b).elements())[:3]} extra {list((b - a).elements())[:3]}")
    ie, iu = re.findall(r"\|(\w+)\(([^()]*)\)", ref), re.findall(r"\|(\w+)\(([^()]*)\)", uk)
    if sorted(n for n, _ in ie) != sorted(n for n, _ in iu) or uk.count("(") != uk.count(")") and "|" in uk:
        p.append(f"{label}: ICU construct differs ({[n for n, _ in ie]} vs {[n for n, _ in iu]})")
    else:
        for name, args in iu:
            keys = re.findall(r"(?:^|,\s*)(\w+)=", args)
            if not keys or not set(keys) <= ICU_KEYS or (name == "plural" and not ("one" in keys and ("many" in keys or "other" in keys))):
                p.append(f"{label}: bad ICU keys {keys} (plural needs one + many/other)")
    if RU_ONLY.search(uk):
        p.append(f"{label}: Russian-only letter {RU_ONLY.search(uk).group(0)!r}")
    if PL_CHARS.search(uk):
        p.append(f"{label}: Polish letter {PL_CHARS.search(uk).group(0)!r}")
    pl_uk = plain_of(uk)
    if re.search(r"[\"“”„]", pl_uk):
        p.append(f"{label}: use «» instead of straight/curly quotes")
    if uk.count("\n") != ref.count("\n"):
        p.append(f"{label}: line breaks {uk.count(chr(10))} != source {ref.count(chr(10))}")
    if re.search(r"[A-Za-z]{2,}", plain_of(ref)) and not CYR.search(pl_uk):
        p.append(f"{label}: untranslated (no Cyrillic)")
    return p


def check_row(r, uk, uf, ux, keep=False):
    """-> (normalized (uk, uf, ux), [problems])"""
    refs = {v: ref_text(r, v) for v in "dfx"}
    if keep:
        return (r["en_default"] or r["pl_default"], r["en_female"], r["en_extra"]), []
    out, probs = [], []
    for v, s in (("d", uk), ("f", uf), ("x", ux)):
        s = normalize(s, refs[v]) if s and s.strip() else ""
        out.append(s)
        if s:
            probs += check_variant(s, refs[v], {"d": "uk", "f": "uk_female", "x": "uk_extra"}[v])
    needf, needx = need_flags(r)
    if not out[0]:
        probs.append("uk: empty")
    if needf and not out[1]:
        probs.append("uk_female: required (source has a female variant)")
    if needx and not out[2]:
        probs.append("uk_extra: required (source has an extra variant)")
    return tuple(out), probs


# =============================================================================================== glossary helpers
def glossary_rows(con):
    return [dict(r) for r in con.execute("SELECT * FROM glossary ORDER BY freq DESC, en")]


def term_regex(en):
    return re.compile(r"(?<![A-Za-z])" + re.escape(en) + r"(?:'s|s|es)?(?![A-Za-z])", re.I)


def glossary_subset(gl, texts, limit=80):
    blob = "\n".join(texts)
    hits = [g for g in gl if term_regex(g["en"]).search(blob)]
    return [[g["en"], g["uk"], g["uk_female"], g["uk_extra"], g["note"]] for g in hits[:limit]]


# =============================================================================================== commands
def cmd_migrate(a):
    con = connect()
    con.execute("PRAGMA journal_mode=WAL")
    cols = {r[1] for r in con.execute("PRAGMA table_info(strings)")}
    if "qa_note" not in cols:
        con.execute("ALTER TABLE strings ADD COLUMN qa_note TEXT NOT NULL DEFAULT ''")
    con.execute(SCHEMA_GLOSSARY)
    con.execute("BEGIN IMMEDIATE")
    # gender = a female variant in ANY language; ExtraText (third/neutral variant) is deliberately not counted
    con.execute("UPDATE strings SET has_gender_variant = (en_female!='' OR pl_female!='' OR ru_female!='')")
    # empty English text (other languages hold only debug placeholders like "[plpl 306 - gui]"): nothing to translate
    n = con.execute(f"""UPDATE strings SET status='skipped', qa_note='empty English source', uk_default='', uk_female='',
                        uk_extra='' WHERE {SCOPE} AND en_default='' AND status IN ('new','draft','needs_fix')""").rowcount
    for t, u, f, x, kind, note in SEED:
        con.execute("INSERT OR IGNORE INTO glossary(en, uk, uk_female, uk_extra, kind, note) VALUES (?,?,?,?,?,?)",
                    (t, u, f, x, kind, note))
    con.execute("COMMIT")
    g = con.execute("SELECT sum(has_gender_variant) FROM strings").fetchone()[0]
    print(f"migrated: qa_note+glossary ok; has_gender_variant=1 on {g} rows (female in en|pl|ru); auto-skipped {n} empty rows")


def fmt_row(r, with_prev=False):
    o = {"id": r["id"], "en": r["en_default"]}
    for k, v in (("en_f", r["en_female"]), ("en_x", r["en_extra"]), ("pl", r["pl_default"]), ("pl_f", r["pl_female"]),
                 ("pl_x", r["pl_extra"])):
        if v:
            o[k] = v
    if r["ru_default"] and len(r["ru_default"]) <= 160:
        o["ru"] = r["ru_default"]
    nf, nx = need_flags(r)
    need = ("F" if nf else "") + ("X" if nx else "")
    if need:
        o["need"] = need
    if with_prev:
        for k, v in (("uk", r["uk_default"]), ("uf", r["uk_female"]), ("ux", r["uk_extra"]), ("note", r["qa_note"])):
            if v:
                o[k] = v
    return o


def write_batches(rows, out_dir, prefix, max_rows, max_chars, with_prev, gl, mix=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    gof = (lambda r: "audit") if mix else group_of
    rows.sort(key=lambda r: (prio(r), r["table_name"], r["string_id"]))
    batches, cur, chars = [], [], 0
    for r in rows:
        w = sum(len(r[c]) for c in ("en_default", "en_female", "en_extra", "pl_default")) + 40
        if cur and (len(cur) >= max_rows or chars + w > max_chars or gof(cur[0]) != gof(r)):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(r)
        chars += w
    if cur:
        batches.append(cur)
    man = []
    for i, b in enumerate(batches, 1):
        g = gof(b[0])
        name = f"{prefix}{i:03d}_{g.replace('/', '-')}"
        texts = [t for r in b for t in (r["en_default"], r["en_female"], r["en_extra"])]
        head = {"batch": name, "category": g, "rows": len(b), "glossary": glossary_subset(gl, texts)}
        lines = [json.dumps(head, ensure_ascii=False)] + [json.dumps(fmt_row(r, with_prev), ensure_ascii=False) for r in b]
        (out_dir / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        man.append({"batch": name, "category": g, "rows": len(b), "chars": sum(len(x) for x in lines)})
    (out_dir / f"{prefix}manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=0), encoding="utf-8")
    return man


def cmd_export(a):
    con = connect()
    st = tuple(a.statuses.split(","))
    if a.ids_file:          # explicit worklist: TSV  id[<TAB>term<TAB>uk]  -> rows with an audit note, previous translation included
        notes = {}
        for ln in Path(a.ids_file).read_text(encoding="utf-8").splitlines():
            p = ln.split("\t")
            if p and p[0].isdigit():
                notes[int(p[0])] = f"glossary: {p[1]} -> {p[2]}" if len(p) >= 3 else "check"
        ids = list(notes)
        rows = []
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows += [dict(r, qa_note=notes[r["id"]]) for r in
                     con.execute(f"SELECT * FROM strings WHERE id IN ({','.join('?' * len(part))})", part)]
        man = write_batches(rows, Path(a.out), "aud", a.max_rows, a.max_chars, True, glossary_rows(con), mix=True)
        print(f"exported {len(rows)} audit rows in {len(man)} batches -> {a.out}")
        return
    rows = [r for r in con.execute(f"SELECT * FROM strings WHERE {SCOPE} AND status IN ({','.join('?' * len(st))})", st)]
    if a.category:
        rows = [r for r in rows if group_of(r) == a.category]
    man = write_batches(rows, Path(a.out), "fix" if a.fix else "", a.max_rows, a.max_chars, a.fix, glossary_rows(con))
    c = collections.Counter(m["category"] for m in man)
    print(f"exported {len(rows)} rows in {len(man)} batches -> {a.out} ; per category: {dict(c)}")


def cmd_import(a, path=None, verdict=False):
    path = Path(path or a.file)
    recs, bad = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            o["id"] = int(o["id"])
            recs.append(o)
        except Exception as e:  # noqa: BLE001
            bad.append(f"line {n}: invalid JSON ({str(e)[:60]})")
    con = connect()
    rows = {}
    ids = [o["id"] for o in recs]
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in con.execute(f"SELECT * FROM strings WHERE {SCOPE} AND id IN ({','.join('?' * len(part))})", part):
            rows[r["id"]] = r
    upd, fails, ok, skipped, skipped_rows = [], list(bad), 0, 0, 0
    for o in recs:
        r = rows.get(o["id"])
        if r is None:
            fails.append(f"{o['id']}: unknown id / out of scope")
            continue
        if r["status"] == "reviewed" and not getattr(a, "force", False) and not verdict:
            skipped += 1
            continue
        if r["status"] == "skipped":
            skipped_rows += 1                # nothing to translate: ignored silently
            continue
        (uk, uf, ux), probs = check_row(r, str(o.get("uk", "")), str(o.get("uf", "")), str(o.get("ux", "")), keep=bool(o.get("keep")))
        if probs:
            upd.append((uk, uf, ux, "needs_fix", "; ".join(probs)[:400], o["id"]))
            fails.append(f"{o['id']}: " + "; ".join(probs)[:230])
        else:
            upd.append((uk, uf, ux, "reviewed" if verdict else "draft", "kept as source" if o.get("keep") else "", o["id"]))
            ok += 1
    con.execute("BEGIN IMMEDIATE")
    con.executemany("UPDATE strings SET uk_default=?, uk_female=?, uk_extra=?, status=?, qa_note=? WHERE id=?", upd)
    con.execute("COMMIT")
    (LOGS / "import").mkdir(parents=True, exist_ok=True)
    (LOGS / "import" / f"{path.stem}.fail.txt").write_text("\n".join(fails), encoding="utf-8")
    print(f"{path.name}: {len(recs)} lines -> ok {ok}, needs_fix {len(upd) - ok}, rejected {len(fails) - (len(upd) - ok)}, "
          f"kept-reviewed {skipped}, ignored-empty-source {skipped_rows}")
    for f in fails[:14]:
        print("  FIX", f)
    if len(fails) > 14:
        print(f"  ... {len(fails) - 14} more in logs/import/{path.stem}.fail.txt")
    return ok, len(fails)


def cmd_validate(a):
    con = connect()
    upd, nfix = [], 0
    for r in con.execute(f"SELECT * FROM strings WHERE {SCOPE} AND uk_default!='' AND status!='skipped'"):
        if r["qa_note"] == "kept as source":
            continue
        (uk, uf, ux), probs = check_row(r, r["uk_default"], r["uk_female"], r["uk_extra"])
        if probs:
            nfix += 1
            upd.append((uk, uf, ux, "needs_fix", "; ".join(probs)[:400], r["id"]))
        elif r["status"] == "needs_fix":
            upd.append((uk, uf, ux, "draft", "", r["id"]))
        elif (uk, uf, ux) != (r["uk_default"], r["uk_female"], r["uk_extra"]):
            upd.append((uk, uf, ux, r["status"], r["qa_note"], r["id"]))
    con.execute("BEGIN IMMEDIATE")
    con.executemany("UPDATE strings SET uk_default=?, uk_female=?, uk_extra=?, status=?, qa_note=? WHERE id=?", upd)
    con.execute("COMMIT")
    print(f"validate: {len(upd)} rows updated, {nfix} failing")


def cmd_stats(a):
    con = connect()
    agg = collections.defaultdict(collections.Counter)
    for r in con.execute(f"SELECT source_file, category, status, uk_default FROM strings WHERE {SCOPE}"):
        g = "D11" if r["source_file"] == "D11" else r["category"]
        agg[g][r["status"]] += 1
    print(f"{'category':<22}{'total':>6}{'draft':>7}{'review':>7}{'needsfx':>8}{'skipped':>8}{'new':>6}")
    tot = collections.Counter()
    for g in PRIORITY:
        c = agg.get(g)
        if not c:
            continue
        tot.update(c)
        print(f"{g:<22}{sum(c.values()):>6}{c['draft']:>7}{c['reviewed']:>7}{c['needs_fix']:>8}{c['skipped']:>8}{c['new']:>6}")
    n = sum(tot.values())
    done = tot["draft"] + tot["reviewed"]
    print(f"{'TOTAL':<22}{n:>6}{tot['draft']:>7}{tot['reviewed']:>7}{tot['needs_fix']:>8}{tot['skipped']:>8}{tot['new']:>6}"
          f"   translated {done}/{n - tot['skipped']} = {100 * done / max(1, n - tot['skipped']):.1f}%")


def cmd_sample(a):
    con = connect()
    rnd = random.Random(a.seed)
    by = collections.defaultdict(list)
    excl = set()
    if a.exclude_file:
        excl = {int(x.split("\t")[0]) for x in Path(a.exclude_file).read_text(encoding="utf-8").splitlines() if x.split("\t")[0].isdigit()}
    for r in con.execute(f"SELECT * FROM strings WHERE {SCOPE} AND status='draft' AND qa_note!='kept as source'"):
        if r["id"] not in excl:
            by[group_of(r)].append(r)
    pick = []
    for g, rs in by.items():
        k = min(len(rs), max(3, round(len(rs) * a.pct / 100)))
        pick += rnd.sample(rs, k)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    gl = glossary_rows(con)
    pick.sort(key=lambda r: (prio(r), r["id"]))
    n = 0
    for i in range(0, len(pick), a.size):
        b = pick[i:i + a.size]
        texts = [t for r in b for t in (r["en_default"], r["en_female"], r["en_extra"])]
        head = {"batch": f"Q{n + 1:02d}", "rows": len(b), "glossary": glossary_subset(gl, texts)}
        lines = [json.dumps(head, ensure_ascii=False)] + [json.dumps(fmt_row(r, True), ensure_ascii=False) for r in b]
        (out / f"Q{n + 1:02d}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        n += 1
    print(f"QA sample: {len(pick)} rows ({100 * len(pick) / max(1, sum(len(v) for v in by.values())):.1f}% of draft) in {n} files -> {out}")


def cmd_review(a):
    recs = []
    for line in Path(a.file).read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            try:
                recs.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    ok_ids = [int(o["id"]) for o in recs if o.get("ok")]
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    con.executemany("UPDATE strings SET status='reviewed', qa_note='' WHERE id=? AND status='draft'", [(i,) for i in ok_ids])
    bad = []
    for o in recs:
        if o.get("ok"):
            continue
        r = con.execute("SELECT * FROM strings WHERE id=?", (int(o["id"]),)).fetchone()
        if r is None:
            continue
        note = ("QA: " + str(o.get("issue", ""))[:300]) if o.get("issue") else "QA: rejected"
        if o.get("uk"):
            (uk, uf, ux), probs = check_row(r, o["uk"], o.get("uf", r["uk_female"]), o.get("ux", r["uk_extra"]))
            if not probs:
                con.execute("UPDATE strings SET uk_default=?, uk_female=?, uk_extra=?, status='reviewed', qa_note='' WHERE id=?",
                            (uk, uf, ux, r["id"]))
                continue
            note += " | fix rejected: " + "; ".join(probs)[:150]
        con.execute("UPDATE strings SET status='needs_fix', qa_note=? WHERE id=?", (note, r["id"]))
        bad.append(int(o["id"]))
    con.execute("COMMIT")
    print(f"review {Path(a.file).name}: reviewed ok {len(ok_ids)}, rejected {len(bad)}")


# ---------------------------------------------------------------------------------------------- glossary
def cmd_candidates(a):
    con = connect()
    rows = [dict(r) for r in con.execute("SELECT * FROM strings WHERE category!='game/codex'")]
    cats = {"game/characters", "game/locations", "game/deities", "game/abilities", "game/statuseffects",
            "game/characterstats", "game/bestiary", "game/waypoints"}
    cand = {}
    for r in rows:
        en = r["en_default"].strip()
        if r["category"] in cats and en and len(en) <= 40 and len(en.split()) <= 5 and "<" not in en and "{" not in en:
            cand.setdefault(en, {"pl": r["pl_default"], "ru": r["ru_default"], "cat": r["category"]})
    tok = re.compile(r"[A-Za-z][A-Za-z'\-]*")
    cnt_exact = collections.Counter()
    cap = collections.Counter()
    ex = {}
    capre = re.compile(r"(?<![.!?\"“]\s)(?<!^)\b((?:[A-Z][a-z'\-]+)(?:\s+(?:of\s+|the\s+|de\s+)?[A-Z][a-z'\-]+){0,3})")
    stop = {"I", "The", "A", "An", "It", "He", "She", "They", "We", "You", "My", "Your", "His", "Her", "That", "This", "What",
            "Why", "How", "When", "Where", "Who", "And", "But", "Or", "If", "So", "No", "Yes", "Oh", "Well", "Now", "Then",
            "There", "Here", "In", "On", "At", "To", "For", "Of", "As", "Be", "Do", "Let", "Not", "Just", "All", "Our", "Their",
            "Is", "Are", "Was", "Were", "Can", "Will", "Would", "Could", "Should", "Don", "Won", "Ve", "Ll", "Re", "Dont", "Im"}
    for r in rows:
        for fld in ("en_default",):
            t = r[fld]
            if not t:
                continue
            toks = tok.findall(t)
            for n in range(1, 6):
                for i in range(len(toks) - n + 1):
                    g = " ".join(toks[i:i + n])
                    if g in cand:
                        cnt_exact[g] += 1
            if r["category"] == "game/credits":
                continue
            for m in capre.findall(t):
                if m.split()[0] in stop and len(m.split()) == 1 or m in stop:
                    continue
                cap[m] += 1
                ex.setdefault(m, (t[:110], r["ru_default"][:110], r["category"]))
    out = {}
    for en, d in cand.items():
        if d["cat"] in ("game/deities", "game/locations", "game/bestiary", "game/statuseffects", "game/characterstats",
                        "game/waypoints") or cnt_exact[en] >= 3:
            out[en] = (cnt_exact[en], d["cat"], d["pl"], d["ru"], "")
    for en, c in cap.items():
        if c >= 6 and en not in out:
            sentence, ru, cat = ex[en]
            out[en] = (c, "freq", "", "", f"{sentence} || {ru}")
    LOGS.mkdir(parents=True, exist_ok=True)
    p = LOGS / "glossary_candidates.tsv"
    items = sorted(out.items(), key=lambda kv: (-kv[1][0], kv[0]))
    p.write_text("en\tcount\tsource\tpl\tru\tcontext(en||ru)\n" + "\n".join(
        f"{k}\t{v[0]}\t{v[1]}\t{v[2]}\t{v[3]}\t{v[4]}".replace("\n", " ") for k, v in items), encoding="utf-8")
    print(f"candidates: {len(items)} terms -> {p}  (names from categories: {sum(1 for v in out.values() if v[1] != 'freq')}, frequent capitalized: {sum(1 for v in out.values() if v[1] == 'freq')})")


def cmd_glossary_import(a):
    con = connect()
    n = bad = 0
    con.execute("BEGIN IMMEDIATE")
    for line in Path(a.file).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
            en, uk = o["en"].strip(), o["uk"].strip()
            assert en and uk and not RU_ONLY.search(uk)
        except Exception:  # noqa: BLE001
            bad += 1
            continue
        old = con.execute("SELECT uk FROM glossary WHERE en=?", (en,)).fetchone()
        if old and o.get("seed_lock", True) and en in {s[0] for s in SEED}:
            continue  # mandated entries are never overwritten
        for ch in "’ʼ":
            uk = uk.replace(ch, "'")
        con.execute("INSERT OR REPLACE INTO glossary(en, uk, uk_female, uk_extra, kind, note, pl, ru, freq) VALUES (?,?,?,?,?,?,?,?,?)",
                    (en, uk, o.get("uf", ""), o.get("ux", ""), o.get("kind", "term"), o.get("note", ""), o.get("pl", ""),
                     o.get("ru", ""), int(o.get("freq", 0))))
        n += 1
    con.execute("COMMIT")
    print(f"glossary-import: {n} upserted, {bad} rejected; table size {con.execute('select count(*) from glossary').fetchone()[0]}")


def cmd_glossary_md(a):
    con = connect()
    DOCS.mkdir(parents=True, exist_ok=True)
    kinds = collections.defaultdict(list)
    for g in glossary_rows(con):
        kinds[g["kind"]].append(g)
    lines = ["# Глосарій Avowed (Eora) — українські відповідники", "",
             "Джерело істини — таблиця `glossary` в `db/avowed_l10n.db`; цей файл генерує `python tools/batch_io.py glossary-md`.",
             "Один термін = один переклад в усіх категоріях. Форми: чол. / жін. / нейтр. (`uk_extra`) — лише для термінів з гендерними варіантами.", ""]
    for k in sorted(kinds):
        lines += [f"## {k}", "", "| EN | UK | жін. | нейтр. | примітка |", "|---|---|---|---|---|"]
        for g in sorted(kinds[k], key=lambda x: x["en"].lower()):
            lines.append(f"| {g['en']} | {g['uk']} | {g['uk_female']} | {g['uk_extra']} | {g['note']} |")
        lines.append("")
    (DOCS / "glossary_uk.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"glossary-md: {sum(len(v) for v in kinds.values())} terms in {len(kinds)} groups -> docs/glossary_uk.md")


def cmd_apply_glossary(a):
    con = connect()
    gl = {g["en"].lower(): g for g in glossary_rows(con)}
    upd = []
    for r in con.execute(f"SELECT * FROM strings WHERE {SCOPE} AND status='new' AND uk_default=''"):
        g = gl.get(r["en_default"].strip().lower())
        if not g:
            continue
        nf, nx = need_flags(r)
        uf, ux = g["uk_female"], g["uk_extra"]
        if (nf and not uf) or (nx and not ux):
            continue
        (uk, uf2, ux2), probs = check_row(r, g["uk"], uf if nf or uf else "", ux if nx or ux else "")
        if not probs:
            upd.append((uk, uf2, ux2, "draft", "glossary", r["id"]))
    con.execute("BEGIN IMMEDIATE")
    con.executemany("UPDATE strings SET uk_default=?, uk_female=?, uk_extra=?, status=?, qa_note=? WHERE id=?", upd)
    con.execute("COMMIT")
    print(f"apply-glossary: {len(upd)} rows filled directly from the glossary")


def cmd_audit(a):
    con = connect()
    # only proper names: common nouns decline with vowel alternation (здібність -> здібностей) and give false hits
    gl = [g for g in glossary_rows(con) if len(g["en"]) >= 4 and g["kind"] in ("place", "person", "deity", "faction", "creature", "title")]
    for g in gl:
        g["_re"] = re.compile(r"(?<![A-Za-z])" + re.escape(g["en"]) + r"(?:'s|s)?(?![A-Za-z])")
        g["_low"] = g["en"].lower()
    res = collections.Counter()
    marks = []
    det = []
    for r in con.execute(f"SELECT * FROM strings WHERE {SCOPE} AND status IN ('draft','reviewed') AND uk_default!=''"):
        if r["qa_note"] in ("kept as source", "glossary"):
            continue
        en_plain = plain_of(r["en_default"])           # ignore tags / {variables} / ICU
        en_low = en_plain.lower()
        for g in gl:
            if g["_low"] not in en_low:          # cheap prefilter before the regex
                continue
            m = g["_re"].search(en_plain)
            if not m:
                continue
            words = [w for w in re.split(r"[\s/:,]+", g["uk"].lower()) if len(w) >= 3]
            low = plain_of(r["uk_default"]).lower()
            if not all(w[:max(3, len(w) - 2)] in low for w in words):
                res[g["en"]] += 1
                det.append(f"{r['id']}\t{g['en']}\t{g['uk']}\t{r['uk_default'][:90]}")
                marks.append((f"glossary: {g['en']} -> {g['uk']}", r["id"]))
                break
    (LOGS / "glossary_audit.tsv").write_text("\n".join(det), encoding="utf-8")
    if a.mark and marks:
        con.execute("BEGIN IMMEDIATE")
        con.executemany("UPDATE strings SET status='needs_fix', qa_note=? WHERE id=? AND status IN ('draft','reviewed')", marks)
        con.execute("COMMIT")
    print(f"audit-glossary: {len(marks)} rows without the glossary term{' (marked needs_fix)' if a.mark else ''}; top: {res.most_common(10)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    e = sub.add_parser("export")
    e.add_argument("--fix", action="store_true")
    e.add_argument("--statuses", default="new")
    e.add_argument("--category")
    e.add_argument("--ids-file", help="TSV worklist (id, term, uk): export those rows with previous translation + note")
    e.add_argument("--out", default=str(LOGS / "batches" / "in"))
    e.add_argument("--max-rows", type=int, default=220)
    e.add_argument("--max-chars", type=int, default=24000)
    i = sub.add_parser("import")
    i.add_argument("file")
    i.add_argument("--force", action="store_true")
    sub.add_parser("validate")
    sub.add_parser("stats")
    s = sub.add_parser("sample")
    s.add_argument("--pct", type=float, default=2.5)
    s.add_argument("--size", type=int, default=120)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--exclude-file", help="TSV whose first column holds ids to leave out of the sample")
    s.add_argument("--out", default=str(LOGS / "qa" / "in"))
    rv = sub.add_parser("review")
    rv.add_argument("file")
    sub.add_parser("candidates")
    gi = sub.add_parser("glossary-import")
    gi.add_argument("file")
    sub.add_parser("glossary-md")
    sub.add_parser("apply-glossary")
    au = sub.add_parser("audit-glossary")
    au.add_argument("--mark", action="store_true")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    {"migrate": cmd_migrate, "export": cmd_export, "import": cmd_import, "validate": cmd_validate, "stats": cmd_stats,
     "sample": cmd_sample, "review": cmd_review, "candidates": cmd_candidates, "glossary-import": cmd_glossary_import,
     "glossary-md": cmd_glossary_md, "apply-glossary": cmd_apply_glossary, "audit-glossary": cmd_audit}[a.cmd](a)


if __name__ == "__main__":
    main()
