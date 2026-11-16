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
  glossary-import FILE         JSONL {en,uk,uf,ux,kind,note[,lore,source]} -> table glossary (absent lore/source keep the stored value)
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
if not DB.exists():   # git worktree (.claude/worktrees/<name>): the DB is git-ignored and lives only in the main checkout
    DB = next((p / "db" / "avowed_l10n.db" for p in REPO.parents if (p / "db" / "avowed_l10n.db").exists()), DB)
LOGS = REPO / "logs"
DOCS = REPO / "docs"
SCOPE_UI = "(source_file='D11' OR (category LIKE 'game/%' AND category!='game/codex'))"
SCOPE_STORY = "(category IN ('conversations','chatter','quests','game/codex') AND table_name NOT LIKE 'conversations/test%')"
SCOPE = f"({SCOPE_UI} OR {SCOPE_STORY})"
STORY_CATS = ("conversations", "chatter", "quests", "game/codex")
STORY_PRIORITY = ["conv:07_prologue", "quests", "chatter", "codex", "conv:companions", "conv:03_dawnshore",
                  "conv:01_emerald_stair", "conv:04_shatterscarp", "conv:02_galawains_tusks", "conv:06_dawnshore_redux",
                  "conv:05_garden"]


def scope_sql(name):
    return {"ui": SCOPE_UI, "story": SCOPE_STORY, "all": SCOPE}[name]


def group_where(g):
    """SQL condition selecting one priority group."""
    if g == "D11":
        return "source_file='D11'"
    if g.startswith("conv:"):
        return f"(category='conversations' AND table_name LIKE 'conversations/{g[5:]}%')"
    if g in ("quests", "chatter"):
        return f"category='{g}'"
    if g == "codex":
        return "category='game/codex'"
    return f"(category='{g}' AND source_file!='D11')"
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
CTRL = re.compile("[\x00-\x08\x0c\x0e-\x1f\x7f  ]")      # \x0b (vertical tab) is a real line break in some sources
FORMAL_YOU = re.compile(r"\b(?:[Вв]и|[Вв]ас|[Вв]ам|[Вв]ами|[Вв]аш\w*)\b")
MASC_PAST = re.compile(r"\b[Тт]и\s+(?:не\s+|вже\s+|ще\s+|лише\s+|одразу\s+)?\w*(?:[аеиіоуяє]в|шов|ів)\b")
RUSISM = re.compile(r"призива|являєтьс|слідуюч|прийма\w* участь|на протязі|в залежності від|відноситьс|стараюс|стараєш|скоріше|прямо\s+як|берегись|проявляєтьс", re.I)
EN_STOP = re.compile(r"\b(?:the|you|your|and|to|of|is|are|was|it|this|that|with|for|have|we|not|what|my)\b", re.I)
PROPER_KINDS = ("place", "person", "deity", "faction", "creature", "title")


def proper_glossary(con):
    gl = [g for g in glossary_rows(con) if len(g["en"]) >= 4 and g["kind"] in PROPER_KINDS]
    for g in gl:
        g["_re"] = re.compile(r"(?<![A-Za-z])" + re.escape(g["en"]) + r"(?:'s|s)?(?![A-Za-z])")
        g["_low"] = g["en"].lower()
        g["_w"] = [w for w in re.split(r"[\s/:,]+", g["uk"].lower()) if len(w) >= 3]
    return gl


def soft_flags(r, variants, gl):
    """Non-blocking hints for QA (never reject on these alone): formal «Ви» outside dialogue, russisms, glossary misses."""
    out = []
    txt = " ".join(plain_of(s) for s in variants if s)
    if r["category"] not in ("conversations", "chatter", "game/codex") and FORMAL_YOU.search(txt):
        out.append("formal-you")
    if r["category"] == "quests" and not r["has_gender_variant"] and MASC_PAST.search(txt):
        out.append("masc-past")             # quest journal has no female variant: no gendered past tense about the player
    if RUSISM.search(txt):
        out.append("rusism")
    en = plain_of(r["en_default"])
    low, ul = en.lower(), txt.lower()
    miss = [g["en"] for g in gl if g["_low"] in low and g["_re"].search(en) and not all(w[:max(3, len(w) - 2)] in ul for w in g["_w"])]
    if miss:
        out.append("glossary:" + "/".join(miss[:3]))
    return out

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
    freq      INTEGER NOT NULL DEFAULT 0,
    lore      TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT ''
)"""
# Field semantics (the review asked to stop mixing them in one column):
#   kind   = entity type (deity, person, place, creature, faction, title, term, ...)  -> section of docs/glossary_uk.md
#   note   = translation_note: declension, gender, spelling, cross-references. ONLY this goes to translator agents (glossary_subset).
#   lore   = what the entity means in Eora; reference for humans/QA, never a translation rule
#   source = where lore/usage was checked (game table, codex, conversation)

SEED = [  # mandated by the project owner: (en, uk, uk_female, uk_extra, kind, note)
    ("Living Lands", "Живі Землі", "", "", "place", ""),
    ("Aedyr", "Едір", "", "", "place", "прикметник Aedyran — едірський"),
    ("Envoy", "Посланець", "Посланниця", "Посланець/Посланниця", "title", "титул гравця"),
    ("Godlike", "Богоподібний", "Богоподібна", "Богоподібний/Богоподібна", "term", "ознака людей будь-якого народу, не окрема раса"),
    ("Adra", "Адра", "", "", "term", "не змінювати; адровий стовп, Адраган"),
    ("Essence", "Есенція", "", "", "term", ""),
    ("Dreamscourge", "Сновидна Моровиця", "", "", "term", "хвороба"),
    ("Paradis", "Парадіс", "", "", "place", ""),
    ("Woedica", "Воедіка", "", "", "deity", ""),
    ("Sapadal", "Сападал", "", "", "deity", ""),
    ("Kith", "Розумні істоти", "", "", "term", "народи Eora (PL istoty rozumne); однина — розумна істота; не «кіт» (омонім)"),
    ("Orlan", "Орлан", "", "", "term", "раса"),
    ("Aumaua", "Аумауа", "", "", "term", "раса; регістр як в оригіналі (у суцільному тексті — аумауа)"),
]


# =============================================================================================== DB
def connect():
    con = sqlite3.connect(DB, timeout=120, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=120000")
    return con


def group_of(r):
    if r["source_file"] == "D11":
        return "D11"
    c = r["category"]
    if c == "conversations":
        return "conv:" + r["table_name"].split("/")[1]
    return {"quests": "quests", "chatter": "chatter", "game/codex": "codex"}.get(c, c)


ALL_PRIORITY = PRIORITY + STORY_PRIORITY


def prio(r):
    g = group_of(r)
    return ALL_PRIORITY.index(g) if g in ALL_PRIORITY else len(ALL_PRIORITY)


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
    if CTRL.search(uk):
        p.append(f"{label}: control character U+{ord(CTRL.search(uk).group(0)):04X}")
    if RU_ONLY.search(uk):
        p.append(f"{label}: Russian-only letter {RU_ONLY.search(uk).group(0)!r}")
    if PL_CHARS.search(uk):
        p.append(f"{label}: Polish letter {PL_CHARS.search(uk).group(0)!r}")
    pl_uk = plain_of(uk)
    if re.search(r"[\"“”„]", pl_uk):
        p.append(f"{label}: use «» instead of straight/curly quotes")
    if uk.count("\n") + uk.count("\x0b") != ref.count("\n") + ref.count("\x0b"):
        p.append(f"{label}: line breaks {uk.count(chr(10)) + uk.count(chr(11))} != source {ref.count(chr(10)) + ref.count(chr(11))}")
    if re.search(r"[A-Za-z]{2,}", plain_of(ref)) and not CYR.search(pl_uk):
        p.append(f"{label}: untranslated (no Cyrillic)")
    return p


def check_row(r, uk, uf, ux, keep=False):
    """-> (normalized (uk, uf, ux), [problems])"""
    refs = {v: ref_text(r, v) for v in "dfx"}
    if keep:
        kept = (r["en_default"] or r["pl_default"], r["en_female"], r["en_extra"])
        if len(EN_STOP.findall(plain_of(kept[0]))) >= 2:          # names/codes/sounds only, never English prose
            return kept, ["keep: source is English prose - translate it"]
        return kept, []
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


def glossary_subset(gl, texts, limit=170):
    """Glossary terms occurring in the texts, most frequent in THIS batch first (a low global frequency must not drop a name)."""
    blob = "\n".join(texts)
    hits = []
    for g in gl:
        if g["en"].lower() not in blob.lower():
            continue
        n = len(term_regex(g["en"]).findall(blob))
        if n:
            hits.append((-n, -g["freq"], g))
    hits.sort(key=lambda x: (x[0], x[1], x[2]["en"]))
    return [[g["en"], g["uk"], g["uk_female"], g["uk_extra"], g["note"]] for _, _, g in hits[:limit]]


# =============================================================================================== commands
def cmd_migrate(a):
    con = connect()
    con.execute("PRAGMA journal_mode=WAL")
    cols = {r[1] for r in con.execute("PRAGMA table_info(strings)")}
    for col, ddl in (("qa_note", "TEXT NOT NULL DEFAULT ''"), ("claim_id", "TEXT NOT NULL DEFAULT ''"),
                     ("claimed_at", "INTEGER NOT NULL DEFAULT 0"), ("attempts", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols:
            con.execute(f"ALTER TABLE strings ADD COLUMN {col} {ddl}")
    con.execute(SCHEMA_GLOSSARY)
    con.execute("CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY AUTOINCREMENT, worker TEXT, grp TEXT, tbl TEXT, "
                "n_rows INTEGER, created_at INTEGER)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_strings_queue ON strings(category, status, claim_id)")
    gcols = {r[1] for r in con.execute("PRAGMA table_info(glossary)")}
    for c in ("lore", "source"):
        if c not in gcols:
            con.execute(f"ALTER TABLE glossary ADD COLUMN {c} TEXT NOT NULL DEFAULT ''")
    con.execute("BEGIN IMMEDIATE")
    # gender = a female variant in ANY language; ExtraText (third/neutral variant) is deliberately not counted
    con.execute("UPDATE strings SET has_gender_variant = (en_female!='' OR pl_female!='' OR ru_female!='')")
    # empty English text (other languages hold only debug placeholders like "[plpl 306 - gui]"): nothing to translate
    n = con.execute(f"""UPDATE strings SET status='skipped', qa_note='empty English source', uk_default='', uk_female='',
                        uk_extra='' WHERE {SCOPE} AND en_default='' AND status IN ('new','draft','needs_fix')""").rowcount
    tests = con.execute("""UPDATE strings SET status='skipped', qa_note='test conversation' WHERE table_name LIKE 'conversations/test%'
                           AND status='new'""").rowcount
    for t, u, f, x, kind, note in SEED:
        con.execute("INSERT OR IGNORE INTO glossary(en, uk, uk_female, uk_extra, kind, note) VALUES (?,?,?,?,?,?)",
                    (t, u, f, x, kind, note))
    con.execute("COMMIT")
    g = con.execute("SELECT sum(has_gender_variant) FROM strings").fetchone()[0]
    print(f"migrated: columns+glossary+batches ok; has_gender_variant=1 on {g} rows (female in en|pl|ru); "
          f"auto-skipped {n} empty-English rows and {tests} test-conversation rows")


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
    rows = [r for r in con.execute(f"SELECT * FROM strings WHERE {scope_sql(a.scope)} AND status IN ({','.join('?' * len(st))})", st)]
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
    m = re.match(r"batch_(b\d+)", path.stem)
    bid = getattr(a, "batch", None) or (m.group(1) if m else "")
    gl = proper_glossary(con)
    foreign = kept_draft = 0
    for o in recs:
        r = rows.get(o["id"])
        if r is None:
            fails.append(f"{o['id']}: unknown id / out of scope")
            continue
        if r["claim_id"] and r["claim_id"] != bid:      # one row is edited by one batch only
            foreign += 1
            fails.append(f"{o['id']}: claimed by another batch ({r['claim_id']}); not imported")
            continue
        if r["status"] == "reviewed" and not getattr(a, "force", False) and not verdict:
            skipped += 1
            continue
        if (r["status"] == "draft" and r["category"] in STORY_CATS and r["claim_id"] != bid and bid
                and not getattr(a, "force", False)):    # finished story rows are not overwritten by a stray re-import
            kept_draft += 1
            continue
        if r["status"] == "skipped":
            skipped_rows += 1                # nothing to translate: ignored silently
            continue
        (uk, uf, ux), probs = check_row(r, str(o.get("uk", "")), str(o.get("uf", "")), str(o.get("ux", "")), keep=bool(o.get("keep")))
        if probs:
            upd.append((uk, uf, ux, "needs_fix", "; ".join(probs)[:400], o["id"]))
            fails.append(f"{o['id']}: " + "; ".join(probs)[:230])
        else:
            fixed_flag = r["status"] == "needs_fix" and r["qa_note"].startswith("flag:")   # fixer has looked at the flag
            flags = [] if (o.get("keep") or fixed_flag) else soft_flags(r, (uk, uf, ux), gl)
            note = "kept as source" if o.get("keep") else ("flag:" + ",".join(flags) if flags else "")
            upd.append((uk, uf, ux, "reviewed" if verdict else "draft", note, o["id"]))
            ok += 1
    con.execute("BEGIN IMMEDIATE")
    con.executemany("UPDATE strings SET uk_default=?, uk_female=?, uk_extra=?, status=?, qa_note=?, claim_id='', claimed_at=0 WHERE id=?", upd)
    con.execute("COMMIT")
    (LOGS / "import").mkdir(parents=True, exist_ok=True)
    (LOGS / "import" / f"{path.stem}.fail.txt").write_text("\n".join(fails), encoding="utf-8")
    print(f"{path.name}: {len(recs)} lines -> ok {ok}, needs_fix {len(upd) - ok}, rejected {len(fails) - (len(upd) - ok)}, "
          f"kept-reviewed {skipped}, kept-draft {kept_draft}, ignored-empty-source {skipped_rows}, foreign-claim {foreign}")
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
    flagged = collections.Counter()
    for r in con.execute(f"SELECT source_file, category, table_name, status, qa_note FROM strings WHERE {scope_sql(a.scope)}"):
        g = group_of(r)
        agg[g][r["status"]] += 1
        flagged[g] += r["qa_note"].startswith("flag:")
    print(f"{'group':<26}{'total':>6}{'draft':>7}{'review':>7}{'needsfx':>8}{'skipped':>8}{'new':>6}{'flagged':>8}")
    tot = collections.Counter()
    order = [g for g in ALL_PRIORITY if g in agg] + sorted(g for g in agg if g not in ALL_PRIORITY)
    for g in order:
        c = agg[g]
        tot.update(c)
        print(f"{g:<26}{sum(c.values()):>6}{c['draft']:>7}{c['reviewed']:>7}{c['needs_fix']:>8}{c['skipped']:>8}{c['new']:>6}{flagged[g]:>8}")
    n = sum(tot.values())
    done = tot["draft"] + tot["reviewed"]
    print(f"{'TOTAL':<26}{n:>6}{tot['draft']:>7}{tot['reviewed']:>7}{tot['needs_fix']:>8}{tot['skipped']:>8}{tot['new']:>6}"
          f"   translated {done}/{n - tot['skipped']} = {100 * done / max(1, n - tot['skipped']):.1f}%")


def cmd_sample(a):
    con = connect()
    rnd = random.Random(a.seed)
    by = collections.defaultdict(list)
    excl = set()
    if a.exclude_file:
        excl = {int(x.split("\t")[0]) for x in Path(a.exclude_file).read_text(encoding="utf-8").splitlines() if x.split("\t")[0].isdigit()}
    want = set(a.groups.split(",")) if a.groups else None
    for r in con.execute(f"SELECT * FROM strings WHERE {scope_sql(a.scope)} AND status='draft' AND qa_note!='kept as source'"):
        if r["id"] not in excl and (want is None or group_of(r) in want):
            by[group_of(r)].append(r)
    pick = []
    if a.tables_file:                                    # 100% review of the listed (problem) tables
        tabs = {x.strip() for x in Path(a.tables_file).read_text(encoding="utf-8").splitlines() if x.strip()}
        by = {g: [r for r in rs if r["table_name"] in tabs] for g, rs in by.items()}
        pick = [r for rs in by.values() for r in rs]
        by = {}
    for g, rs in by.items():
        k = min(len(rs), max(3, round(len(rs) * a.pct / 100)))
        fl = [r for r in rs if r["qa_note"].startswith("flag:")]
        if a.flagged and fl:                      # soft-flagged rows first (up to half of the quota)
            first = rnd.sample(fl, min(len(fl), k // 2))
            ids = {r["id"] for r in first}
            pick += first + rnd.sample([r for r in rs if r["id"] not in ids], k - len(first))
        else:
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
        if a.scope != "ui":                       # voice profiles of the scenes in this file (docs/voices.md)
            import batch_worker as W
            head["voices"] = W.voices_for(sorted({r["table_name"] for r in b}), W.load_voices())
        rows_out = []
        for r in b:
            o = fmt_row(r, True)
            if a.scope != "ui":                   # preceding line of the same table for dialogue/quest context (read-only)
                pv = con.execute("SELECT en_default, uk_default FROM strings WHERE source_file=? AND table_name=? AND string_id<? "
                                 "ORDER BY string_id DESC LIMIT 1", (r["source_file"], r["table_name"], r["string_id"])).fetchone()
                if pv:
                    o["prev"] = [pv[0][:160], pv[1][:160]]
                o["t"] = r["table_name"].split("/", 1)[-1][-48:]
            rows_out.append(json.dumps(o, ensure_ascii=False))
        lines = [json.dumps(head, ensure_ascii=False)] + rows_out
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
            if not probs:            # keep the defect class in qa_note for QA statistics (critical/semantic/stylistic/technical)
                con.execute("UPDATE strings SET uk_default=?, uk_female=?, uk_extra=?, status='reviewed', qa_note=? WHERE id=?",
                            (uk, uf, ux, f"QA-fixed[{o.get('cls', '?')}]: {str(o.get('issue', ''))[:200]}", r["id"]))
                continue
            note += " | fix rejected: " + "; ".join(probs)[:150]
        con.execute("UPDATE strings SET status='needs_fix', qa_note=? WHERE id=?", (note, r["id"]))
        bad.append(int(o["id"]))
    con.execute("COMMIT")
    rej_ids = [int(o["id"]) for o in recs if not o.get("ok")]
    allv = {i: 1 for i in ok_ids}
    allv.update({i: 0 for i in rej_ids})
    log = []
    for i in range(0, len(allv), 500):
        part = list(allv)[i:i + 500]
        for r in con.execute(f"SELECT id, source_file, category, table_name FROM strings WHERE id IN ({','.join('?' * len(part))})", part):
            log.append(f"{r['id']}\t{group_of(r)}\t{r['table_name']}\t{allv[r['id']]}")
    (LOGS / "qa").mkdir(parents=True, exist_ok=True)
    with open(LOGS / "qa" / "verdicts.tsv", "a", encoding="utf-8") as fh:
        fh.write("\n".join(log) + ("\n" if log else ""))
    print(f"review {Path(a.file).name}: reviewed ok {len(ok_ids)}, rejected {len(rej_ids)} (not fixable: {len(bad)})")


def cmd_reflag(a):
    """Recompute soft flags of all draft rows (after the flag rules or the glossary changed)."""
    con = connect()
    gl = proper_glossary(con)
    upd = []
    for r in con.execute(f"SELECT * FROM strings WHERE {scope_sql(a.scope)} AND status='draft' AND uk_default!=''"):
        if r["qa_note"] in ("kept as source", "glossary"):
            continue
        fl = soft_flags(r, (r["uk_default"], r["uk_female"], r["uk_extra"]), gl)
        note = ("flag:" + ",".join(fl)) if fl else ""
        if note != r["qa_note"]:
            upd.append((note, r["id"]))
    con.execute("BEGIN IMMEDIATE")
    con.executemany("UPDATE strings SET qa_note=? WHERE id=?", upd)
    con.execute("COMMIT")
    c = collections.Counter(k.split(":")[0] for (n, _) in upd for k in n[5:].split(",") if n)
    print(f"reflag: {len(upd)} rows changed; flags set: {dict(c)}")


def cmd_flag_to_fix(a):
    """draft rows carrying the given soft flags -> needs_fix (note keeps the flag) so that `batch_worker claim --fix` serves them."""
    con = connect()
    kinds = tuple(a.kinds.split(","))
    want = set(a.groups.split(",")) if a.groups else None
    star = a.kinds == "*"                         # every draft row of the groups (systematically defective tables)
    cond = "" if star else " AND qa_note LIKE 'flag:%'"
    ids = [r["id"] for r in con.execute(f"SELECT * FROM strings WHERE {scope_sql(a.scope)} AND status='draft'{cond}")
           if (want is None or group_of(r) in want) and r["qa_note"] != "kept as source"
           and (star or any(k in r["qa_note"] for k in kinds))]
    con.execute("BEGIN IMMEDIATE")
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        if a.note:
            con.execute(f"UPDATE strings SET status='needs_fix', qa_note=? WHERE id IN ({','.join('?' * len(part))})", [a.note] + part)
        else:
            con.execute(f"UPDATE strings SET status='needs_fix' WHERE id IN ({','.join('?' * len(part))})", part)
    con.execute("COMMIT")
    print(f"flag-to-fix: {len(ids)} rows marked needs_fix (kinds {kinds})")


def cmd_qa_report(a):
    """Defect rate per group and per table from logs/qa/verdicts.tsv; tables above the threshold -> logs/qa/problem_tables.txt."""
    p = LOGS / "qa" / "verdicts.tsv"
    seen = {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        f = ln.split("\t")
        if len(f) == 4:
            seen[int(f[0])] = (f[1], f[2], int(f[3]))          # last verdict per id wins
    g, t = collections.defaultdict(lambda: [0, 0]), collections.defaultdict(lambda: [0, 0])
    for grp, tab, ok in seen.values():
        g[grp][0] += 1
        g[grp][1] += 1 - ok
        t[tab][0] += 1
        t[tab][1] += 1 - ok
    print(f"{'group':<26}{'reviewed':>9}{'rejected':>9}{'rate':>7}")
    tot = [0, 0]
    for grp in [x for x in ALL_PRIORITY if x in g] + sorted(x for x in g if x not in ALL_PRIORITY):
        n, bad = g[grp]
        tot[0] += n
        tot[1] += bad
        print(f"{grp:<26}{n:>9}{bad:>9}{100 * bad / n:>6.1f}%")
    print(f"{'TOTAL':<26}{tot[0]:>9}{tot[1]:>9}{100 * tot[1] / max(1, tot[0]):>6.1f}%")
    prob = sorted(tab for tab, (n, bad) in t.items() if n >= a.min_n and bad / n >= a.min_rate)
    (LOGS / "qa" / "problem_tables.txt").write_text("\n".join(prob), encoding="utf-8")
    print(f"problem tables (>= {a.min_n} reviewed rows, defect rate >= {a.min_rate:.0%}): {len(prob)} -> logs/qa/problem_tables.txt")


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
        old = con.execute("SELECT uk, lore, source FROM glossary WHERE en=?", (en,)).fetchone()
        if old and o.get("seed_lock", True) and en in {s[0] for s in SEED}:
            continue  # mandated entries are never overwritten
        for ch in "’ʼ":
            uk = uk.replace(ch, "'")
        lore = o.get("lore", old["lore"] if old else "")
        source = o.get("source", old["source"] if old else "")
        con.execute("INSERT OR REPLACE INTO glossary(en, uk, uk_female, uk_extra, kind, note, pl, ru, freq, lore, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (en, uk, o.get("uf", ""), o.get("ux", ""), o.get("kind", "term"), o.get("note", ""), o.get("pl", ""),
                     o.get("ru", ""), int(o.get("freq", 0)), lore, source))
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
             "Один термін = один переклад в усіх категоріях. Форми: чол. / жін. / нейтр. (`uk_extra`) — лише для термінів з гендерними варіантами.",
             "Принципи вибору відповідників (переклад значущих назв, транслітерація вигаданих мов, питомі слова) — `docs/style_uk.md` §8.",
             "Заголовок розділу = тип сутності (`kind`). «Правило» (`note`) — відмінювання, рід, написання, перехресні посилання: єдине, що отримують перекладачі. «Лор» (`lore`) — що це в світі Eora, довідка для людей і QA, не правило. «Джерело» (`source`) — де це перевірено.", ""]
    for k in sorted(kinds):
        lines += [f"## {k}", "", "| EN | UK | жін. | нейтр. | правило | лор | джерело |", "|---|---|---|---|---|---|---|"]
        for g in sorted(kinds[k], key=lambda x: x["en"].lower()):
            lines.append(f"| {g['en']} | {g['uk']} | {g['uk_female']} | {g['uk_extra']} | {g['note']} | {g['lore']} | {g['source']} |")
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


def cmd_audit_changed(a):
    """Glossary entries were corrected after rows had been translated: JSON [{en, old, new}] (old/new = uk) ->
    rows whose English text has the term but whose Ukrainian lacks the NEW form go back to needs_fix with the change as note."""
    ch = json.loads(Path(a.file).read_text(encoding="utf-8"))
    terms = []
    for c in ch:
        words = [w for w in re.split(r"[\s/:,\-]+", c["new"].lower()) if len(w) >= 3]
        olds = [w for w in re.split(r"[\s/:,\-]+", c["old"].lower()) if len(w) >= 3]
        if words and [w[:max(3, len(w) - 2)] for w in words] != [w[:max(3, len(w) - 2)] for w in olds]:   # capitalisation-only changes are ignored
            terms.append((c["en"], c["old"], c["new"], [w[:max(3, len(w) - 2)] for w in words],
                          re.compile(r"(?<![A-Za-z])" + re.escape(c["en"]) + r"(?:'s|s)?(?![A-Za-z])")))   # case-sensitive: "Lies" is not "lies"
    con = connect()
    marks, per = [], collections.Counter()
    for r in con.execute(f"SELECT * FROM strings WHERE {scope_sql(a.scope)} AND status IN ('draft','reviewed') AND uk_default!=''"):
        if r["qa_note"] == "kept as source":
            continue
        en = plain_of(r["en_default"])
        low = plain_of(r["uk_default"]).lower()
        hit = [(t[0], t[1], t[2]) for t in terms if t[4].search(en) and not all(w in low for w in t[3])]
        if hit:
            for h in hit:
                per[h[0]] += 1
            marks.append(("glossary changed: " + "; ".join(f"{e}: {o} -> {n}" for e, o, n in hit[:3]), r["id"]))
    if a.mark and marks:
        con.execute("BEGIN IMMEDIATE")
        con.executemany("UPDATE strings SET status='needs_fix', qa_note=? WHERE id=? AND status IN ('draft','reviewed')", marks)
        con.execute("COMMIT")
    print(f"audit-changed: {len(terms)} changed terms checked, {len(marks)} rows{' marked needs_fix' if a.mark else ' to fix'}; top: {per.most_common(8)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    ac = sub.add_parser("audit-changed")
    ac.add_argument("--file", default=str(LOGS / "glossary_changes.json"))
    ac.add_argument("--scope", choices=("ui", "story", "all"), default="all")
    ac.add_argument("--mark", action="store_true")
    e = sub.add_parser("export")
    e.add_argument("--fix", action="store_true")
    e.add_argument("--statuses", default="new")
    e.add_argument("--category")
    e.add_argument("--ids-file", help="TSV worklist (id, term, uk): export those rows with previous translation + note")
    e.add_argument("--scope", choices=("ui", "story", "all"), default="ui")
    e.add_argument("--out", default=str(LOGS / "batches" / "in"))
    e.add_argument("--max-rows", type=int, default=220)
    e.add_argument("--max-chars", type=int, default=24000)
    i = sub.add_parser("import")
    i.add_argument("file")
    i.add_argument("--force", action="store_true")
    i.add_argument("--batch", help="batch id (default: from file name batch_<id>.jsonl)")
    sub.add_parser("validate")
    st_ = sub.add_parser("stats")
    st_.add_argument("--scope", choices=("ui", "story", "all"), default="story")
    s = sub.add_parser("sample")
    s.add_argument("--pct", type=float, default=2.5)
    s.add_argument("--size", type=int, default=120)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--exclude-file", help="TSV whose first column holds ids to leave out of the sample")
    s.add_argument("--scope", choices=("ui", "story", "all"), default="ui")
    s.add_argument("--groups", help="comma separated groups (D11, game/gui, quests, chatter, codex, conv:companions ...)")
    s.add_argument("--flagged", action="store_true", help="soft-flagged rows take up to half of the quota")
    s.add_argument("--tables-file", help="review ALL draft rows of the tables listed in this file (problem tables)")
    rf = sub.add_parser("reflag")
    rf.add_argument("--scope", choices=("ui", "story", "all"), default="all")
    ff = sub.add_parser("flag-to-fix")
    ff.add_argument("--kinds", required=True, help="comma separated: glossary,masc-past,formal-you,rusism")
    ff.add_argument("--groups")
    ff.add_argument("--note", help="qa_note stored with the rows (shown to the fixer); kinds '*' = all draft rows of the groups")
    ff.add_argument("--scope", choices=("ui", "story", "all"), default="story")
    qr = sub.add_parser("qa-report")
    qr.add_argument("--min-n", type=int, default=3)
    qr.add_argument("--min-rate", type=float, default=0.25)
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
     "glossary-md": cmd_glossary_md, "apply-glossary": cmd_apply_glossary, "audit-glossary": cmd_audit,
     "qa-report": cmd_qa_report, "reflag": cmd_reflag, "flag-to-fix": cmd_flag_to_fix,
     "audit-changed": cmd_audit_changed}[a.cmd](a)


if __name__ == "__main__":
    main()
