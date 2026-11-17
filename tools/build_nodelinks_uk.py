#!/usr/bin/env python3
"""Avowed l10n: restore NodeLinks (lore/glossary highlight spans) for the Ukrainian text.

Every FOEIStringTable has three maps key -> [(key, node_id, start, length, 0)] (Default / Female / Extra text of that string):
character spans in the localized text that the game highlights as lore/glossary links. The originals exist for EN (15 365)
and PL (19 953) over only ~192 distinct node_ids, so the problem is closed: node_id -> Ukrainian term -> spans in uk text.

  python tools/build_nodelinks_uk.py nodes          node dictionary summary (EN forms -> glossary -> Ukrainian patterns)
  python tools/build_nodelinks_uk.py build          spans for every uk variant -> logs/nodelinks_uk.json + logs/nodelinks_miss.tsv
  python tools/build_nodelinks_uk.py verify         structural check of the cache against the DB texts
  python tools/build_nodelinks_uk.py review-export  node dictionary + real examples for the reviewers -> logs/nodelinks_review/

Rules: reference set per (table, variant, key) = EN spans UNION PL spans (max occurrences per node); a span is the term as found
in the Ukrainian word forms (bounded case endings), starts at a token start, includes trailing sentence punctuation like the
originals (`animancer,`), is measured in UTF-16 code units; unknown/ambiguous matches are NOT guessed: they go to
logs/nodelinks_miss.tsv. Run it AFTER all text fixes: the cache stores a hash of the text each span list belongs to.
Reviewed corrections of the node dictionary live in tools/data/nodelinks_nodes_uk.json ({node: {"terms": [...], "skip": bool}}).
"""
import argparse
import collections
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import repack_uasset as rp  # noqa: E402

RAW = REPO / "raw_assets"
LOGS = REPO / "logs"
CACHE = LOGS / "nodelinks_uk.json"
MISS = LOGS / "nodelinks_miss.tsv"
NODES_TSV = LOGS / "nodelinks_nodes.tsv"
OVERRIDES = REPO / "tools" / "data" / "nodelinks_nodes_uk.json"
TRAIL = ".,;:!?\u2026"


def open_db(a):
    """read-only connection to --db (default db/avowed_l10n.db); a release is built from a named snapshot of the DB"""
    return sqlite3.connect(f"file:{Path(a.db).as_posix()}?mode=ro", uri=True)


def text_hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def u16(s, i):
    return len(s[:i].encode("utf-16-le")) // 2


def norm_form(s):
    s = s.strip(" \t\r\n\"\u201c\u201d\u201e\u00ab\u00bb()[]{}")
    s = re.sub(r"[.,;:!?\u2026]+$", "", s)
    s = re.sub(r"(?i)^the\s+", "", s)
    s = re.sub(r"(?i)['\u2019]s$", "", s)
    return re.sub(r"\s+", " ", s).lower().strip()


def stem_uk(w):
    """Stem for the case endings. Words of up to 5 letters lose only a final vowel (\u0415\u043e\u0440\u0430 -> \u0435\u043e\u0440, \u0411\u0435\u0440\u0430\u0442 stays whole): a short
    stem like \u00ab\u0431\u0435\u0440\u00bb of \u0411\u0435\u0440\u0430\u0442 would also match \u0431\u0435\u0440\u0435\u0448/\u0431\u0435\u0440\u0435\u0433."""
    w = w.lower().replace("\u2019", "'")
    n = len(w)
    if n >= 9:
        return w[:n - 3]
    if n >= 6:
        return w[:n - 2]
    return w[:-1] if n > 3 and w[-1] in "\u0430\u044f\u043e\u0435\u0454\u0438\u0456\u0457\u044e\u0443\u044c" else w


def tok_pattern(t):
    """stem + at most (letters cut from the base form) + 3 more letters (2 for terms of up to 4 letters)."""
    st = stem_uk(t)
    bound = (len(t) - len(st)) + (2 if len(t) <= 4 else 3)
    return re.escape(st) + r"\w{0," + str(bound) + r"}"


def phrase_pattern(term):
    """'Сталева Гарота' -> стал\\w{0,4}[\\s-]+гарот\\w{0,4} with token-start / token-end guards."""
    toks = [t for t in re.split(r"[\s\-]+", term.strip()) if t]
    if not toks:
        return None
    return r"(?<![\w'])" + r"[\s\-]+".join(tok_pattern(t) for t in toks) + r"(?![\w'])"


def load_tables():
    return {lg: {t: (links, entries) for t, links, entries in rp.read_tables((RAW / f"BaseGame_Text_{lg}.uasset").read_bytes())}
            for lg in ("enus", "plpl")}


def glossary(con):
    return {en.lower(): uk for en, uk in con.execute("SELECT en, uk FROM glossary")}


def gl_lookup(gl, form):
    cands = [form, re.sub(r"^the ", "", form)]
    if form.endswith("s"):
        cands += [form[:-1], form[:-2] if form.endswith("es") else form[:-1]]
    for c in cands:
        if c in gl:
            return gl[c]
    return None


def load_uk(con):
    rows = {}
    for t, k, d, f, x in con.execute("SELECT table_name, string_id, uk_default, uk_female, uk_extra FROM strings "
                                     "WHERE source_file='BaseGame' AND status IN ('draft','reviewed')"):
        rows[(t, k)] = (d, f, x)
    return rows


def build_nodes(con, tabs, uk_rows):
    gl = glossary(con)
    nodes = collections.defaultdict(lambda: {"forms": collections.Counter(), "pl_forms": collections.Counter(), "rows": set(),
                                             "terms": [], "aligned": [], "skip": False})
    for lg in ("enus", "plpl"):
        for t, (links, entries) in tabs[lg].items():
            for p, od in links.items():
                for k, els in od.items():
                    text = entries.get(k, ("", "", ""))[p - 4]
                    for e in els:
                        n = nodes[e[1]]
                        (n["forms"] if lg == "enus" else n["pl_forms"])[norm_form(text[e[2]:e[2] + e[3]])] += 1
                        n["rows"].add((t, k))
    df_all, n_all = collections.Counter(), 0
    for (t, k), v in uk_rows.items():
        n_all += 1
        df_all.update({stem_uk(w) for w in re.findall(r"[\u0400-\u04ff']{4,}", v[0].lower())})
    ov = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    for nid, n in nodes.items():
        for form, _c in n["forms"].most_common():
            uk = gl_lookup(gl, form)
            if uk:
                for alt in uk.split("/"):
                    if alt.strip() and alt.strip() not in n["terms"]:
                        n["terms"].append(alt.strip())
        rows = [uk_rows[r][0] for r in n["rows"] if r in uk_rows]
        if rows:
            dfn = collections.Counter()
            for txt in rows:
                dfn.update({stem_uk(w) for w in re.findall(r"[\u0400-\u04ff']{4,}", txt.lower())})
            cand = []
            for st, c in dfn.items():
                cover = c / len(rows)
                lift = cover / max(df_all[st] / n_all, 1e-9)
                if cover >= 0.5 and lift >= 8 and c >= 2:
                    cand.append((cover * min(lift, 200), st, round(cover, 2)))
            n["aligned"] = [(st, cv) for _s, st, cv in sorted(cand, reverse=True)[:3]]
        o = ov.get(str(nid))
        if o and o.get("terms") is not None:
            n["terms"] = list(o["terms"])
            n["source"] = "override"
        n["skip"] = bool(o and o.get("skip"))
        pats = [phrase_pattern(t) for t in n["terms"]]
        n["patterns"] = [p for p in pats if p] + list((o or {}).get("patterns", []))     # reviewed raw regexes (alternations like вог-)
        if o and (o.get("terms") is not None or o.get("patterns")):
            n["source"] = "override"
        if not o or (o.get("terms") is None and not o.get("patterns")):
            # statistical co-occurring stems (n["aligned"]) prove co-occurrence, not identity of the term: they never become a
            # pattern (a lone stem highlights "Високій" instead of "Високій Траві"); they are only exported for the reviewers
            n["source"] = "glossary" if n["patterns"] else "none"
        n["rx"] = re.compile("|".join(sorted(n["patterns"], key=len, reverse=True)), re.I) if n["patterns"] and not n["skip"] else None
    return nodes


SLASH_JOIN = re.compile(r"/|\s/\s")


def find_spans(text, rx):
    out = []
    for m in rx.finditer(text):
        e = m.end()
        while e < len(text) and text[e] in TRAIL:
            e += 1
        # the gender-neutral variants write both forms of a term with a slash (Посланцю/Посланнице): one highlight over the pair
        if out and SLASH_JOIN.fullmatch(text[out[-1][1]:m.start()] or "\0"):
            out[-1] = (out[-1][0], e)
            continue
        out.append((m.start(), e))
    return out


def references(tabs):
    ref = {}
    for lg in ("enus", "plpl"):
        for t, (links, entries) in tabs[lg].items():
            for p, od in links.items():
                for k, els in od.items():
                    text = entries.get(k, ("", "", ""))[p - 4]
                    if len(text.encode("utf-16-le", "surrogatepass")) != 2 * len(text):
                        # original spans are UTF-16 offsets and are sliced here as Python indexes: only valid inside the BMP
                        raise SystemExit(f"non-BMP character in a linked source text ({t}, prop {p}, key {k}): extend references() to UTF-16 slicing")
                    cnt = collections.Counter(e[1] for e in els)
                    r = ref.setdefault((t, p, k), {"nodes": collections.Counter(), "pos": collections.defaultdict(list), "en": ""})
                    for nid, c in cnt.items():
                        r["nodes"][nid] = max(r["nodes"][nid], c)
                    if lg == "enus" or not r["pos"]:
                        r["en"] = text
                        for e in els:
                            r["pos"][e[1]].append((e[2] / max(1, len(text)), text[e[2]:e[2] + e[3]], e[2]))
    for r in ref.values():
        r["n_ref"] = sum(r["nodes"].values())
    return ref


def resolve_overlaps(found):
    """Spans of different nodes must not overlap. Rule: the longer span (the more specific term) wins; two different nodes
    on equally long overlapping spans are a real conflict and BOTH are dropped (no order-dependent guess).
    Returns (kept, dropped); every dropped span is reported to nodelinks_miss.tsv by the caller."""
    found = sorted(found, key=lambda x: (x[1], -(x[2] - x[1]), x[0]))
    kept, dropped = [], []
    for nid, s, e in found:
        if kept and s < kept[-1][2]:
            pn, ps, pe = kept[-1]
            if (pn, ps, pe) == (nid, s, e):
                continue                                    # the very same span found twice
            if e - s > pe - ps:
                dropped.append(kept.pop())
                kept.append((nid, s, e))
            elif e - s < pe - ps:
                dropped.append((nid, s, e))
            elif pn == nid:
                continue                                    # same node, same length: one highlight
            else:
                dropped.append(kept.pop())
                dropped.append((nid, s, e))
            continue
        kept.append((nid, s, e))
    return kept, dropped


def ordinal_pick(r, nid, spans):
    """The translation has more matches than the source has linked spans. Not guessed by proximity: only when the source text
    itself contains the linked surface form exactly len(spans) times (so occurrence i of the source <-> occurrence i of the
    translation) and every linked source span is one of those occurrences, the linked ordinals are taken. Otherwise None."""
    pos = r["pos"].get(nid) or []
    forms = {t.rstrip(TRAIL + " ").lower() for _q, t, _a in pos if t.strip()}
    if not forms or len(pos) != r["nodes"][nid]:
        return None
    low = r["en"].lower()
    occ = sorted({m.start() for f in forms for m in re.finditer(r"(?<![\w'])" + re.escape(f) + r"(?![\w'])", low)})
    linked = sorted(a for _q, _t, a in pos)
    if len(occ) != len(spans) or not all(a in occ for a in linked):
        return None
    return [spans[occ.index(a)] for a in linked]


def compute(con):
    tabs = load_tables()
    uk_rows = load_uk(con)
    nodes = build_nodes(con, tabs, uk_rows)
    ref = references(tabs)
    cache, miss = {}, []
    tot = collections.Counter()
    per_node = collections.defaultdict(lambda: [0, 0])
    examples = collections.defaultdict(list)
    miss_ex = collections.defaultdict(list)
    for (t, p, k), r in sorted(ref.items()):
        v = uk_rows.get((t, k))
        if not v or not v[p - 4]:
            continue
        text = v[p - 4]
        tot["ref"] += r["n_ref"]
        found = []
        for nid, need in r["nodes"].items():
            node = nodes.get(nid)
            per_node[nid][1] += need
            if not node or node["rx"] is None:
                miss.append((t, k, p, nid, "no Ukrainian pattern for this node" if node and not node["skip"] else "node skipped by review"))
                if len(miss_ex[nid]) < 3:
                    miss_ex[nid].append((r["pos"][nid][0][1] if r["pos"].get(nid) else "", text[:110]))
                continue
            spans = find_spans(text, node["rx"])
            if not spans:
                miss.append((t, k, p, nid, "term not found in the Ukrainian text"))
                if len(miss_ex[nid]) < 3:
                    miss_ex[nid].append((r["pos"][nid][0][1] if r["pos"].get(nid) else "", text[:110]))
                continue
            if len(spans) > need:
                picked = ordinal_pick(r, nid, spans)
                if picked is None:
                    tot["ambiguous"] += need
                    miss.append((t, k, p, nid, f"ambiguous: {len(spans)} occurrences for {need} source span(s), not guessed"))
                    if len(miss_ex[nid]) < 3:
                        miss_ex[nid].append((r["pos"][nid][0][1] if r["pos"].get(nid) else "", text[:110]))
                    continue
                tot["ordinal"] += need
                spans = picked
            found += [(nid, s, e) for s, e in spans[:need]]
            if len(spans) < need:
                miss.append((t, k, p, nid, f"only {len(spans)} of {need} occurrences found"))
        found, dropped = resolve_overlaps(found)
        for nid, s, e in dropped:
            tot["overlap"] += 1
            miss.append((t, k, p, nid, f"overlap with another node's span, not guessed: {text[s:e][:30]}"))
        for nid, s, e in found:
            per_node[nid][0] += 1
            if len(examples[nid]) < 3 and (tot["restored"] + len(examples[nid])) % 7 == 0 or len(examples[nid]) < 1:
                src = r["pos"].get(nid, [(0, "")])[0][1]
                examples[nid].append((src, text[s:e], text[max(0, s - 18):e + 14].replace("\n", " ")))
        if found:
            cache[f"{t}\x01{p}\x01{k}"] = [text_hash(text), [[k, nid, u16(text, s), u16(text, e) - u16(text, s)] for nid, s, e in found]]
            tot["restored"] += len(found)
    return tabs, nodes, cache, miss, tot, per_node, examples, miss_ex


def cmd_build(a):
    con = open_db(a)
    tabs, nodes, cache, miss, tot, per_node, _ex, _mx = compute(con)
    LOGS.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"version": 2, "links": cache}, ensure_ascii=False), encoding="utf-8")
    with open(MISS, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("table\tkey\tprop\tnode\treason\n")
        for m in miss:
            fh.write("\t".join(str(x) for x in m) + "\n")
    with open(NODES_TSV, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("node\trefs\trestored\trecall\tsource\ten_forms\tuk_terms\taligned\n")
        for nid, n in sorted(nodes.items(), key=lambda kv: -per_node[kv[0]][1]):
            got, need = per_node[nid]
            fh.write(f"{nid}\t{need}\t{got}\t{(100 * got / need if need else 0):.0f}%\t{n['source']}\t"
                     f"{'|'.join(f for f, _c in n['forms'].most_common(4))}\t{'|'.join(n['terms'][:4])}\t{' '.join(f'{s}({c})' for s, c in n['aligned'])}\n")
    en_total = sum(len(els) for t, (links, e) in tabs["enus"].items() for od in links.values() for els in od.values())
    pl_total = sum(len(els) for t, (links, e) in tabs["plpl"].items() for od in links.values() for els in od.values())
    low = sorted(((100 * g / n if n else 0, nid, n) for nid, (g, n) in per_node.items() if n >= 5))[:6]
    reasons = collections.Counter(m[4].split(":")[0][:34] for m in miss)
    print(f"nodelinks build: reference spans (EN u PL, translated rows) {tot['ref']}, restored {tot['restored']} = {100 * tot['restored'] / max(1, tot['ref']):.1f}% "
          f"| of PL originals {pl_total}: {100 * tot['restored'] / pl_total:.1f}% | EN originals {en_total}")
    print(f"  nodes {len(nodes)}: " + ", ".join(f"{s} {sum(1 for n in nodes.values() if n['source'] == s)}" for s in ("glossary", "aligned", "override", "none"))
          + f"; misses {len(miss)} {dict(reasons.most_common(5))} -> {MISS.name}")
    print(f"  not guessed: ambiguous {tot['ambiguous']} spans, overlap conflicts {tot['overlap']}; resolved by verified ordinal rule {tot['ordinal']}")
    print("  lowest-recall nodes (>=5 refs): " + ", ".join(f"{nid}:{r:.0f}%/{n}" for r, nid, n in low))
    return 0


def cmd_review_export(a):
    con = open_db(a)
    tabs, nodes, cache, miss, tot, per_node, examples, miss_ex = compute(con)
    out = LOGS / "nodelinks_review"
    out.mkdir(parents=True, exist_ok=True)
    order = sorted(nodes, key=lambda nid: -per_node[nid][1])
    half = (len(order) + 1) // 2
    for name, part in (("A", order[:half]), ("B", order[half:])):
        with open(out / f"nodes_{name}.jsonl", "w", encoding="utf-8", newline="\n") as fh:
            for nid in part:
                n = nodes[nid]
                got, need = per_node[nid]
                fh.write(json.dumps({"node": nid, "refs": need, "restored": got, "source": n["source"],
                                     "en_forms": n["forms"].most_common(6), "pl_forms": n["pl_forms"].most_common(4),
                                     "uk_terms": n["terms"], "aligned": n["aligned"],
                                     "matched_examples": examples.get(nid, [])[:3], "missed_examples": miss_ex.get(nid, [])[:3]},
                                    ensure_ascii=False) + "\n")
    print(f"review-export: {len(order)} nodes -> {out} (nodes_A.jsonl {half}, nodes_B.jsonl {len(order) - half})")
    return 0


def cmd_verify(a):
    con = open_db(a)
    cache = json.loads(CACHE.read_text(encoding="utf-8"))["links"]
    uk_rows = load_uk(con)
    bad = collections.Counter()
    n = 0
    for key, (h, els) in cache.items():
        t, p, k = key.split("\x01")
        p, k = int(p), int(k)
        v = uk_rows.get((t, k))
        if not v or text_hash(v[p - 4]) != h:
            bad["stale_text_hash"] += len(els)
            continue
        text16 = v[p - 4].encode("utf-16-le")
        prev_end = -1
        for kk, nid, s, ln in els:
            n += 1
            if kk != k:
                bad["foreign_key"] += 1
            if s < 0 or ln <= 0 or (s + ln) * 2 > len(text16):
                bad["out_of_range"] += 1
            try:
                span = text16[s * 2:(s + ln) * 2].decode("utf-16-le")      # strict: a cut surrogate pair is an error, not ignored
            except UnicodeDecodeError:
                bad["utf16_split"] += 1
                continue
            if not span or span[0].isspace() or span[-1].isspace():
                bad["span_edge_space"] += 1
            if s < prev_end:
                bad["overlap"] += 1
            prev_end = max(prev_end, s + ln)
    print(f"nodelinks verify: {n} spans in {len(cache)} variants; problems: {dict(bad) or 'none'}")
    return 1 if bad else 0


def cmd_nodes(a):
    con = open_db(a)
    nodes = build_nodes(con, load_tables(), load_uk(con))
    print(f"nodes: {len(nodes)}; pattern source {dict(collections.Counter(n['source'] for n in nodes.values()))}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("nodes", "build", "verify", "review-export"))
    ap.add_argument("--db", default=str(REPO / "db" / "avowed_l10n.db"))
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return {"nodes": cmd_nodes, "build": cmd_build, "verify": cmd_verify, "review-export": cmd_review_export}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
