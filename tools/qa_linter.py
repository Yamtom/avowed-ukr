#!/usr/bin/env python3
"""Avowed l10n: full QA linter over db/avowed_l10n.db (stdlib only, read-only diagnostics).

Checks every translated row and every filled Ukrainian form (uk_default / uk_female / uk_extra) against its English /
Polish source. It DIAGNOSES; fixes go through tools/batch_io.py (fix batches / imports). Severities:
  P0  breaks tags, variables, ICU, line breaks, control chars, empty text      -> must be 0
  P1  missing mandatory gender/neutral form                                      -> must be 0
  P2  confirmed language/terminology error (high precision rule)                 -> fix or document
  P3  warning, suspicious or ambiguous case (needs context)                      -> never auto-fixed

Usage
  python tools/qa_linter.py [--groups g1,g2] [--out logs/qa/lint_latest.tsv]     full run, <=30 summary lines
  python tools/qa_linter.py --show CODE [--n 8]                                  sample of one code from the last report
  python tools/qa_linter.py mark --codes A,B [--note TEXT]                       rows of these codes -> needs_fix (qa_note 'lint:CODE ...')
Outputs: logs/qa/lint_latest.tsv (sev, code, id, group, table, string_id, field, message), logs/qa/lint_summary.json,
         logs/qa/charset_uk.tsv (every non-ASCII character used by the Ukrainian text), logs/qa/tyvy_tables.tsv.
"""
import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

TAG = r"</?[A-Za-z][^<>]*?/?>|</>"
VAR = r"\{[^{}]*\}"
ICU = r"\|(?:plural|ordinal|gender|select)\([^()]*\)"
TAG_RE, VAR_RE = re.compile(TAG), re.compile(VAR)
ICU_ARGS_RE = re.compile(r"\|(\w+)\(([^()]*)\)")
PROT = re.compile("(" + "|".join((TAG, VAR, ICU)) + ")")
CYR = re.compile("[А-Яа-яІіЇїЄєҐґ]")
RU_ONLY = re.compile("[ыэъёЫЭЪЁ]")
PL_CHARS = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
CTRL = re.compile("[\x00-\x08\x0c\x0e-\x1f\x7f  ]")
BAD_APOS = re.compile("[ʼ’‘´`]")
STRAIGHT_Q = re.compile("[\"“”„‟]")
HYPHEN_DASH = re.compile(r"(?<=\S) (?:-|--|–) (?=\S)")
LATIN = re.compile(r"[A-Za-z]{3,}")
FORMAL = re.compile(r"(?<![\w'])(?:[Вв]и|[Вв]ас|[Вв]ам|[Вв]ами|[Вв]аш\w*)(?![\w'])")
FORMAL_CAP_MID = re.compile(r"(?<=[а-яіїєґ,;:] )(?:Ви|Вас|Вам|Вами|Ваш\w*)(?![\w'])")
INFORMAL = re.compile(r"(?<![\w'])(?:[Тт]и|[Тт]обі|[Тт]ебе|[Тт]обою|[Тт]вій|[Тт]воя|[Тт]воє|[Тт]вої|[Тт]воїх|[Тт]вого|[Тт]вому)(?![\w'])")
RUSISM_STRICT = re.compile(r"(?<![\w'])(?:призива\w*|являєтьс\w*|слідуюч\w*|прийма\w* участь|на протязі|в залежності від|відноситьс\w*|"
                           r"стараюс\w*|стараєш\w*|скоріше|прямо як|берегись|проявляєтьс\w*)", re.I)
PARTICIPLE = re.compile(r"(?<![\w'])[а-яіїєґ']{3,}(?:учий|ючий|ачий|ячий|учого|ючого|ачого|ячого|учі|ючі|ачі|ячі)(?![\w'])", re.I)
MASC_PAST = re.compile(r"(?<![\w'])[Тт]и\s+(?:не\s+|вже\s+|ще\s+|лише\s+|одразу\s+)?\w*(?:[аеиіоуяє]в|шов|ів)(?![\w'])")
ENVOY_MASC = re.compile(r"Посланц|Посланець|Посланец")
ENVOY_FEM = re.compile(r"Посланниц|Посланнице")
PLAIN_WS2 = re.compile(r"(?<=\S)  +(?=\S)")
SPACE_PUNCT = re.compile(r"\s[,.;:!?](?!\.)")
DOUBLE_PUNCT = re.compile(r"(?<![.…])\.\.(?!\.)|,,|;;|!{3,}|\?{3,}")
KNOWN_LATIN_OK = {"Avowed", "Obsidian", "Xbox", "Steam", "NVIDIA", "DLSS", "DLAA", "XeSS", "AMD", "FSR", "HDR", "Intel", "Windows",
                  "PlayStation", "Epic", "Unreal", "Engine", "OEI", "Alt", "Cmd", "Ctrl", "Shift", "Tab", "Esc", "Enter", "LORE",
                  "Lorem", "ipsum", "FPS", "GPU", "CPU", "VSync", "TAA", "TSR", "SSAO", "HUD", "DLC", "NPC", "DevOps"}
PARGRUN = ("bleste", "viate", "dukte dragu", "corba", "nimdut", "saka", "fuitu", "usundit", "nekai", "camaii", "amacul",
           "obsomolu vointu", "dragu")

GROUP_UI_LEN = ("game/gui", "game/inputbindings")
SYSTEM_CATS = {"quests", "game/gui", "game/tooltips", "game/tutorial", "game/inputbindings", "game/characterstats",
               "game/abilities", "game/statuseffects", "game/oeiunreal", "game/oeiphotomode", "game/loadingtips",
               "game/achievements", "game/items", "game/deities", "game/locations", "game/waypoints", "game/bestiary"}

SEV = {
    "TAG_SET": "P0", "TAG_BALANCE": "P0", "VAR_SET": "P0", "ICU_FUNC": "P0", "ICU_SYNTAX": "P0", "NEWLINE": "P0",
    "CTRL_CHAR": "P0", "EMPTY_UK": "P0", "BAD_ENCODING": "P0",
    "MISSING_FEMALE": "P1", "MISSING_EXTRA": "P1",
    "RU_LETTER": "P2", "PL_LETTER": "P2", "BAD_APOSTROPHE": "P2", "STRAIGHT_QUOTES": "P2", "HYPHEN_DASH": "P2",
    "OLD_GLOSSARY": "P2", "LATIN_LEFTOVER": "P2", "PARGRUN_LATIN": "P2", "EXTRA_ENVOY_SINGLE": "P2",
    "FORMAL_CAPITAL": "P2", "RUSISM": "P2", "UNTRANSLATED": "P2",
    "GENDER_SAME": "P3", "EXTRA_SAME": "P3", "FORMAL_YOU": "P3", "PARTICIPLE": "P3", "MASC_PAST_PLAYER": "P3", "GLOSSARY_MISS": "P3",
    "UI_LENGTH": "P3", "DOUBLE_SPACE": "P3", "SPACE_PUNCT": "P3", "DOUBLE_PUNCT": "P3", "NUMERO_SIGN": "P3", "LATIN_UNCHANGED": "P3",
    "ICU_NOFEW": "P3", "FEMALE_HAS_ENVOY_MASC": "P3", "DEFAULT_ENVOY_FEM": "P3", "ELLIPSIS_STYLE": "P3",
    "ARQUEBUS_GENDER": "P2", "GIATTO_FORM": "P2", "KOSMIN_FORM": "P2", "GLOSSARY_NAMED": "P2",
}
# terms the project owner named as violated: a glossary miss on them is a confirmed P2, not a warning
NAMED_TERMS = {"dreamthrall", "deadfire", "strangleroot", "fiorian", "swordfish", "lödwyn", "lodwyn", "ukaizo"}
# (code, regex on the Ukrainian text, regex on the English source or None, message)
KNOWN = [
    # masculine agreement only: «аркебуз» is also the correct genitive plural of «аркебуза»
    ("ARQUEBUS_GENDER", re.compile(r"(?:ий|ій|ого|ому|им|цей|мій|твій|наш|ваш|один)\s+[Аа]ркебуз(?![\w'])"), None, "аркебуза — жіночого роду (аркебуза, аркебузу), не «аркебуз»"),
    ("GIATTO_FORM", re.compile(r"Джіат|Джат"), None, "Ґіатта: кличний «Ґіатто», не «Джіатто»"),
    ("KOSMIN_FORM", re.compile(r"Космина"), re.compile(r"Kosmin|Cosmin(?!a)"), "Космін — чоловік (Косміна), не «Космина»"),
]


def plain(s):
    return "".join(PROT.split(s)[0::2])


def depth_ok(tags):
    d = 0
    for t in tags:
        if t.startswith("</"):
            d -= 1
            if d < 0:
                return False
        elif not t.endswith("/>"):
            d += 1
    return d == 0


def stem(w):
    """Case-tolerant word stem: long words lose 3 letters so that vowel alternations (посланець/посланця) still match."""
    return w[:max(3, len(w) - 3)] if len(w) >= 6 else w[:max(3, len(w) - 2)]


def load_glossary(con):
    gl, old_forms = [], []
    try:
        gl = list(con.execute("SELECT en, uk, kind FROM glossary"))
    except sqlite3.Error:
        pass
    p = REPO / "logs" / "glossary_changes.json"
    kinds = {en.lower(): kind for en, _uk, kind in gl}
    if p.exists():
        for c in json.loads(p.read_text(encoding="utf-8")):
            # only proper names: common-word stems of corrected stat labels ("вбит", "минул") would flag half of the game
            if kinds.get(c["en"].lower()) not in ("place", "person", "deity", "faction", "creature", "title"):
                continue
            o = [w for w in re.split(r"[\s/:,\-]+", c["old"].lower()) if len(w) >= 5]
            n = [w for w in re.split(r"[\s/:,\-]+", c["new"].lower()) if len(w) >= 4]
            for w in o:
                if not any(w[:max(4, len(w) - 2)] == x[:max(4, len(x) - 2)] for x in n):   # a genuinely different old spelling
                    old_forms.append((w[:max(4, len(w) - 2)], c["en"], c["new"],
                                      re.compile(r"(?<![A-Za-z])" + re.escape(c["en"]) + r"(?:'s|s)?(?![A-Za-z])", re.I)))
    uk_all = " ".join(uk.lower() for _en, uk, _k in gl)
    old_forms = [o for o in old_forms if o[0] not in uk_all]       # an "old" stem still used by a CURRENT term (спорлінг) is not old
    return gl, old_forms


def lint(con, groups=None):
    issues = []
    stats = collections.Counter()
    charset = collections.Counter()
    tyvy = collections.defaultdict(lambda: [0, 0])
    gl, old_forms = load_glossary(con)
    def stems_of(en, uk):
        st = [stem(w) for w in re.split(r"[\s/:,\-]+", uk.lower()) if len(w) >= 3]
        # one English word with a multiword Ukrainian name (Dreamthrall -> Сновидний Невільник): the glossary lets the text
        # shorten it to the distinctive first word, so only that word is required
        return st[:1] if len(en.split()) == 1 and len(st) > 1 else st
    proper_re = [(re.compile(r"(?<![A-Za-z])" + re.escape(en) + r"(?:'s|s)?(?![A-Za-z])"), en, en.lower(), uk, stems_of(en, uk))
                 for en, uk, kind in gl if kind in ("place", "person", "deity", "faction", "creature", "title") and len(en) >= 4]
    glossary_en = {en.lower() for en, _uk, _k in gl}
    kept_tokens = set()
    for (t,) in con.execute("SELECT uk_default FROM strings WHERE qa_note='kept as source'"):
        kept_tokens.update(LATIN.findall(plain(t)))
    ok_latin = KNOWN_LATIN_OK | kept_tokens

    q = ("SELECT id, source_file, category, table_name, string_id, status, qa_note, has_gender_variant, "
         "en_default, en_female, en_extra, pl_default, pl_female, pl_extra, ru_female, ru_extra, "
         "uk_default, uk_female, uk_extra FROM strings WHERE status IN ('draft','reviewed')")
    for r in con.execute(q):
        (rid, src, cat, tab, sid, status, note, hasg, en_d, en_f, en_x, pl_d, pl_f, pl_x, ru_f, ru_x, uk_d, uk_f, uk_x) = r
        grp = "D11" if src == "D11" else ("conv:" + tab.split("/")[1] if cat == "conversations" else cat)
        if groups and grp not in groups:
            continue
        stats["rows"] += 1
        kept = note == "kept as source"
        ref_d = en_d or pl_d
        # structure (tags/variables/breaks) follows the ENGLISH source first; Polish variants only when English is empty
        refs = {"uk_default": ref_d, "uk_female": en_f or en_d or pl_f or pl_d, "uk_extra": en_x or en_d or pl_x or pl_d}
        if not ref_d.strip():                       # whitespace-only source (spacer rows): nothing to translate
            continue
        texts = {"uk_default": uk_d, "uk_female": uk_f, "uk_extra": uk_x}
        need_f = bool(en_f or pl_f or ru_f)
        need_x = bool(en_x or pl_x or ru_x)
        short_tab = tab[-34:]

        def add(code, field, msg):
            issues.append((SEV[code], code, rid, grp, short_tab, sid, field, msg[:150].replace("\t", " ").replace("\n", "\\n")))

        # ---- P0 / P1: completeness
        if not uk_d.strip():
            add("EMPTY_UK", "uk_default", "empty translation of a translated row")
            continue
        if need_f and not uk_f.strip():
            add("MISSING_FEMALE", "uk_female", "source has a female variant (en/pl/ru) but uk_female is empty")
        if need_x and not uk_x.strip():
            add("MISSING_EXTRA", "uk_extra", "source has an extra variant (en/pl/ru) but uk_extra is empty")

        for field, uk in texts.items():
            if not uk:
                continue
            stats["forms"] += 1
            ref = refs[field]
            pu = plain(uk)
            for ch in set(uk):
                if ord(ch) > 127:
                    charset[ch] += 1
            # ---- P0: structure
            if CTRL.search(uk):
                add("CTRL_CHAR", field, f"control character U+{ord(CTRL.search(uk).group(0)):04X}")
            try:
                uk.encode("utf-16-le")
            except UnicodeEncodeError:
                add("BAD_ENCODING", field, "unpaired surrogate / not encodable as UTF-16")
            tu, tr = TAG_RE.findall(uk), TAG_RE.findall(ref)
            if collections.Counter(tu) != collections.Counter(tr):
                a, b = collections.Counter(tr), collections.Counter(tu)
                add("TAG_SET", field, f"missing {list((a - b).elements())[:3]} extra {list((b - a).elements())[:3]}")
            elif depth_ok(tr) and not depth_ok(tu):
                add("TAG_BALANCE", field, "tags present but not balanced/nested like the source")
            vu, vr = collections.Counter(VAR_RE.findall(uk)), collections.Counter(VAR_RE.findall(ref))
            if vu != vr:
                add("VAR_SET", field, f"missing {list((vr - vu).elements())[:3]} extra {list((vu - vr).elements())[:3]}")
            if (uk.count("\n") + uk.count("\x0b"), uk.count("\r")) != (ref.count("\n") + ref.count("\x0b"), ref.count("\r")):
                add("NEWLINE", field, f"line breaks {uk.count(chr(10))}/{uk.count(chr(13))} != source {ref.count(chr(10))}/{ref.count(chr(13))}")
            ie, iu = ICU_ARGS_RE.findall(ref), ICU_ARGS_RE.findall(uk)
            if ie or iu or "|plural" in uk or "|ordinal" in uk:
                if sorted(n for n, _ in ie) != sorted(n for n, _ in iu):
                    add("ICU_FUNC", field, f"functions {[n for n, _ in ie]} vs {[n for n, _ in iu]}")
                elif uk.count("(") != uk.count(")"):
                    add("ICU_SYNTAX", field, "unbalanced parentheses")
                else:
                    for name, args in iu:
                        keys = re.findall(r"(?:^|,\s*)(\w+)=", args)
                        if not keys or not set(keys) <= {"zero", "one", "two", "few", "many", "other"}:
                            add("ICU_SYNTAX", field, f"bad keys {keys}")
                        elif name == "plural" and not ("one" in keys and ("many" in keys or "other" in keys)):
                            add("ICU_SYNTAX", field, f"plural keys {keys}: need one + many/other")
                        elif name == "plural" and "many" in keys and "few" not in keys:
                            add("ICU_NOFEW", field, "plural has many but no few (2-4 forms)")
            if kept:
                continue
            # ---- P2: confirmed language errors
            if RU_ONLY.search(uk):
                add("RU_LETTER", field, f"Russian-only letter {RU_ONLY.search(uk).group(0)!r}")
            if PL_CHARS.search(pu):
                add("PL_LETTER", field, f"Polish letter {PL_CHARS.search(pu).group(0)!r}")
            if BAD_APOS.search(uk):
                add("BAD_APOSTROPHE", field, f"non-U+0027 apostrophe U+{ord(BAD_APOS.search(uk).group(0)):04X}")
            if STRAIGHT_Q.search(pu):
                add("STRAIGHT_QUOTES", field, "straight/curly double quotes, style requires «»")
            if HYPHEN_DASH.search(pu):
                add("HYPHEN_DASH", field, "hyphen/en dash/-- with spaces, style requires —")
            if re.search(r"[A-Za-z]{2,}", plain(ref)) and not CYR.search(pu):
                add("UNTRANSLATED", field, "no Cyrillic in a translatable row")
            lat = [t for t in LATIN.findall(pu) if t not in ok_latin]
            if lat:
                low = pu.lower()
                if any(pg in low for pg in PARGRUN):
                    add("PARGRUN_LATIN", field, "invented Pargrun word left in Latin: " + ", ".join(lat[:3]))
                elif any(t.lower() in glossary_en for t in lat):
                    add("LATIN_LEFTOVER", field, "glossary term left in Latin: " + ", ".join(t for t in lat if t.lower() in glossary_en)[:60])
                elif any(t in ref for t in lat):
                    add("LATIN_UNCHANGED", field, "Latin word(s) identical to the source: " + ", ".join(lat[:4]))
                else:
                    add("LATIN_LEFTOVER", field, "Latin text not present in the source: " + ", ".join(lat[:4]))
            low = uk.lower()
            for st, en, new, en_rx in old_forms:
                if st in low and en_rx.search(ref_d):          # the English term must really occur in this row
                    add("OLD_GLOSSARY", field, f"old spelling '{st}…' of {en} (now {new})")
                    break
            for kcode, krx, ken, kmsg in KNOWN:
                if krx.search(pu) and (ken is None or ken.search(ref)):
                    add(kcode, field, kmsg)
            if RUSISM_STRICT.search(pu):
                add("RUSISM", field, "russism/calque: " + RUSISM_STRICT.search(pu).group(0))
            if cat in SYSTEM_CATS and FORMAL_CAP_MID.search(pu):
                add("FORMAL_CAPITAL", field, "capitalised formal «Ви» in a system text")
            elif cat in SYSTEM_CATS and FORMAL.search(pu):
                add("FORMAL_YOU", field, "formal address in a system category (check context)")
            if field == "uk_extra" and need_x and ENVOY_MASC.search(uk) and not ENVOY_FEM.search(uk):
                add("EXTRA_ENVOY_SINGLE", field, "uk_extra names the Envoy only in the masculine form")
            if field == "uk_default" and re.search(r"Посланниц", uk) and not ENVOY_MASC.search(uk):
                add("DEFAULT_ENVOY_FEM", field, "uk_default uses the feminine Envoy form")
            if field == "uk_female" and ENVOY_MASC.search(uk) and not ENVOY_FEM.search(uk):
                add("FEMALE_HAS_ENVOY_MASC", field, "uk_female has masculine Envoy form (other person?)")
            # ---- P3
            if PARTICIPLE.search(pu):
                add("PARTICIPLE", field, "active participle: " + PARTICIPLE.search(pu).group(0))
            if cat == "quests" and not hasg and MASC_PAST.search(pu):
                add("MASC_PAST_PLAYER", field, "gendered past tense about the player in a row without female variant")
            if PLAIN_WS2.search(pu):
                add("DOUBLE_SPACE", field, "double space")
            if SPACE_PUNCT.search(pu):
                add("SPACE_PUNCT", field, "space before punctuation")
            if DOUBLE_PUNCT.search(pu):
                add("DOUBLE_PUNCT", field, "doubled punctuation")
            if "№" in uk:
                add("NUMERO_SIGN", field, "№ is missing in EspinosaNova (font fallback)")
            if ("…" in uk) != ("…" in ref) and ("..." in ref or "…" in ref):
                add("ELLIPSIS_STYLE", field, "ellipsis character differs from the source (… vs ...)")
        if kept:
            continue
        # ---- gender / extra consistency
        if (pl_f and pl_f != pl_d or en_f and en_f != en_d) and uk_f and uk_f == uk_d:
            add("GENDER_SAME", "uk_female", "source female differs from default but uk_female == uk_default")
        if (pl_x and pl_x != pl_d or en_x and en_x != en_d) and uk_x and uk_x == uk_d:
            add("EXTRA_SAME", "uk_extra", "source extra differs from default but uk_extra == uk_default")
        if cat in GROUP_UI_LEN and len(en_d) >= 6 and len(plain(uk_d)) > 1.4 * len(plain(en_d)):
            add("UI_LENGTH", "uk_default", f"{len(plain(uk_d))} chars vs EN {len(plain(en_d))} (>140%)")
        # ---- glossary (proper names only; stems tolerate case endings)
        en_pl = plain(en_d)
        low_en, low_uk = en_pl.lower(), plain(uk_d).lower()
        for rx, en, en_low, uk_term, stems in proper_re:
            if en_low in low_en and rx.search(en_pl) and not all(s in low_uk for s in stems):
                add("GLOSSARY_NAMED" if en_low in NAMED_TERMS else "GLOSSARY_MISS", "uk_default", f"{en} -> {uk_term}")
                break
        if cat == "conversations":                     # ти/ви by table, informational only
            tyvy[tab][0] += len(INFORMAL.findall(plain(uk_d)))
            tyvy[tab][1] += len(FORMAL.findall(plain(uk_d)))
    return issues, stats, charset, tyvy


def write_reports(issues, stats, charset, tyvy, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    issues.sort(key=lambda x: (order[x[0]], x[1], x[2]))
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("sev\tcode\tid\tgroup\ttable\tstring_id\tfield\tmessage\n")
        for it in issues:
            fh.write("\t".join(str(x) for x in it) + "\n")
    with open(out.parent / "charset_uk.tsv", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("char\tcodepoint\tcount\n")
        for ch, n in sorted(charset.items(), key=lambda kv: -kv[1]):
            fh.write(f"{ch}\tU+{ord(ch):04X}\t{n}\n")
    rows = [(t, inf, form) for t, (inf, form) in tyvy.items() if inf >= 3 and form >= 3 and min(inf, form) / (inf + form) >= 0.2]
    with open(out.parent / "tyvy_tables.tsv", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("table\tinformal\tformal\n")
        for t, inf, form in sorted(rows, key=lambda x: -(x[1] + x[2])):
            fh.write(f"{t}\t{inf}\t{form}\n")
    by_code = collections.Counter((i[0], i[1]) for i in issues)
    ids = collections.defaultdict(set)
    for i in issues:
        ids[i[0]].add(i[2])
    summary = {"rows": stats["rows"], "forms": stats["forms"], "by_sev_rows": {s: len(v) for s, v in ids.items()},
               "by_code": {f"{s}:{c}": n for (s, c), n in sorted(by_code.items())}, "tyvy_mixed_tables": len(rows)}
    (out.parent / "lint_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return by_code, ids, len(rows)


def cmd_run(a):
    con = sqlite3.connect(f"file:{Path(a.db).as_posix()}?mode=ro", uri=True)
    groups = set(a.groups.split(",")) if a.groups else None
    issues, stats, charset, tyvy = lint(con, groups)
    by_code, ids, ntyvy = write_reports(issues, stats, charset, tyvy, a.out)
    print(f"qa_linter: {stats['rows']} rows, {stats['forms']} uk forms checked; {len(issues)} findings -> {a.out}")
    print("sev  rows   | code (findings)")
    for s in ("P0", "P1", "P2", "P3"):
        codes = sorted(((c, n) for (sv, c), n in by_code.items() if sv == s), key=lambda x: -x[1])
        print(f"{s}  {len(ids.get(s, ())):>5}  | " + (", ".join(f"{c} {n}" for c, n in codes) or "-"))
    odd = sorted(((ch, n) for ch, n in charset.items() if not (CYR.match(ch) or ch in "«»—… –")), key=lambda x: -x[1])[:10]
    print("non-Cyrillic non-ASCII chars used: " + (" ".join(f"{ch}(U+{ord(ch):04X})x{n}" for ch, n in odd) or "-"))
    print(f"ти/ви mixed tables: {ntyvy} (logs/qa/tyvy_tables.tsv); full reports in {Path(a.out).parent}")
    return 1 if ids.get("P0") or ids.get("P1") else 0


def cmd_show(a):
    n = 0
    for ln in Path(a.out).read_text(encoding="utf-8").splitlines()[1:]:
        f = ln.split("\t")
        if len(f) >= 8 and f[1] == a.show:
            print("\t".join(f[2:]))
            n += 1
            if n >= a.n:
                break


def cmd_mark(a):
    codes = set(a.codes.split(","))
    todo = {}
    for ln in Path(a.out).read_text(encoding="utf-8").splitlines()[1:]:
        f = ln.split("\t")
        if len(f) >= 8 and f[1] in codes:
            todo.setdefault(int(f[2]), []).append(f"{f[1]}({f[6]}): {f[7]}")
    import batch_io as B
    con = B.connect()
    con.execute("BEGIN IMMEDIATE")
    n = 0
    for rid, msgs in todo.items():
        n += con.execute("UPDATE strings SET status='needs_fix', qa_note=? WHERE id=? AND status IN ('draft','reviewed')",
                         ("lint:" + "; ".join(msgs)[:380] + (" | " + a.note if a.note else ""), rid)).rowcount
    con.execute("COMMIT")
    print(f"mark: {n} rows -> needs_fix for codes {sorted(codes)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="run", choices=("run", "mark"))
    ap.add_argument("--db", default=str(REPO / "db" / "avowed_l10n.db"))
    ap.add_argument("--out", default=str(REPO / "logs" / "qa" / "lint_latest.tsv"))
    ap.add_argument("--groups")
    ap.add_argument("--show", help="print findings of one code from the last report")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--codes", help="mark: comma separated codes")
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.show:
        cmd_show(a)
        return 0
    return {"run": cmd_run, "mark": cmd_mark}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())

