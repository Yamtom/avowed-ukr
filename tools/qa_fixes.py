#!/usr/bin/env python3
"""Avowed l10n: deterministic, rule-based fixes for qa_linter findings. Nothing is written to SQLite directly:
the corrected rows are exported as JSONL and imported through `tools/batch_io.py import --force` (full validation).

  python tools/qa_fixes.py [--codes NEWLINE,PARGRUN_LATIN] [--dry]      (reads logs/qa/lint_latest.tsv)

Fixers
  NEWLINE        line breaks of the translation take the break types of the source in order (the source may mix \r\n and \n)
  PARGRUN_LATIN  invented Pargrun words stay Cyrillic (docs/style_uk.md §9): viate -> віате, bleste -> блесте, ...
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PARGRUN = {  # Latin Pargrun word -> Cyrillic (user-approved list + the forms found in the DB)
    "dukte dragu": "дукте драґу", "obsomolu vointu": "обсомолу воінту", "nimduten": "німдутен", "nimdut": "німдут",
    "viate": "віате", "bleste": "блесте", "nekai": "некай", "camaii": "камаї", "amacul": "амакуль", "corba": "корба",
    "saka": "сака", "fuitu": "фуїту", "usundit": "усундіт", "meu": "меу",
}
SPELL = {"фуіту": "фуїту"}   # one canonical Cyrillic spelling per Pargrun word (as in the majority of the DB)
SPELL_RE = re.compile(r"(?<![\w'])(" + "|".join(SPELL) + r")(?![\w'])", re.I)
PARGRUN_RE = re.compile(r"(?<![\w'])(" + "|".join(sorted((re.escape(k) for k in PARGRUN), key=len, reverse=True)) + r")(?![\w'])", re.I)


def fix_breaks(uk, ref):
    tokens = re.findall(r"\r\n|\n", ref)
    parts = uk.replace("\r\n", "\n").split("\n")
    if len(parts) - 1 != len(tokens):
        return None                                   # different number of breaks: needs an editor
    out = parts[0]
    for tok, part in zip(tokens, parts[1:]):
        out += tok + part
    return out


def fix_pargrun(uk):
    def sub(m):
        cyr = PARGRUN[m.group(1).lower()]
        w = m.group(1)
        return cyr.upper() if w.isupper() and len(w) > 1 else (cyr[0].upper() + cyr[1:] if w[0].isupper() else cyr)
    return PARGRUN_RE.sub(sub, uk)


def fix_spell(uk):
    def sub(m):
        cyr = SPELL[m.group(1).lower()]
        w = m.group(1)
        return cyr.upper() if w.isupper() and len(w) > 1 else (cyr[0].upper() + cyr[1:] if w[0].isupper() else cyr)
    return SPELL_RE.sub(sub, uk)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codes", default="NEWLINE,PARGRUN_LATIN")
    ap.add_argument("--lint", default=str(REPO / "logs" / "qa" / "lint_latest.tsv"))
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    codes = set(a.codes.split(","))
    ids = {}
    for ln in Path(a.lint).read_text(encoding="utf-8").splitlines()[1:]:
        f = ln.split("\t")
        if len(f) >= 8 and f[1] in codes:
            ids.setdefault(int(f[2]), set()).add(f[1])
    con = sqlite3.connect(f"file:{(REPO / 'db' / 'avowed_l10n.db').as_posix()}?mode=ro", uri=True)
    out, skipped = [], []
    for rid, cs in ids.items():
        (en_d, en_f, en_x, pl_d, pl_f, pl_x, uk_d, uk_f, uk_x) = con.execute(
            "SELECT en_default, en_female, en_extra, pl_default, pl_female, pl_extra, uk_default, uk_female, uk_extra "
            "FROM strings WHERE id=?", (rid,)).fetchone()
        refs = (en_d or pl_d, en_f or en_d or pl_f or pl_d, en_x or en_d or pl_x or pl_d)
        new = [uk_d, uk_f, uk_x]
        for i, (txt, ref) in enumerate(zip(new, refs)):
            if not txt:
                continue
            if "NEWLINE" in cs:
                fixed = fix_breaks(txt, ref)
                if fixed is not None:
                    txt = fixed
            if "PARGRUN_LATIN" in cs:
                txt = fix_pargrun(txt)
            if "PARGRUN_SPELL" in cs:
                txt = fix_spell(txt)
            new[i] = txt
        if new == [uk_d, uk_f, uk_x]:
            skipped.append(rid)
        else:
            out.append({"id": rid, "uk": new[0], "uf": new[1], "ux": new[2]})
    print(f"qa_fixes: {len(ids)} rows in report for {sorted(codes)}; {len(out)} fixable mechanically, {len(skipped)} left for an editor {skipped[:8]}")
    if a.dry or not out:
        return 0
    f = REPO / "scratch" / "fixes"
    f.mkdir(parents=True, exist_ok=True)
    fp = f / f"mech_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    fp.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in out) + "\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "tools" / "batch_io.py"), "import", "--force", str(fp)], capture_output=True, text=True, encoding="utf-8")
    print(r.stdout.strip()[:1500])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())