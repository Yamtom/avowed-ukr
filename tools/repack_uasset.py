#!/usr/bin/env python3
"""Avowed l10n: byte-exact re-serializer for Text_<lang>.uasset (UStringTableBundleSet, Zen package).

Takes an original (template) asset from raw_assets/, copies the Zen header and the table metadata verbatim
(Name, 3x TMap<FString,FIntSet>, entry ids/keys) and re-serializes the DefaultText / FemaleText / ExtraText strings of
every `Entries` record. The three TMap<int32,FNodeLinkArray> ("NodeLinks") are NOT text independent: every element is
(string key, node id, start, length) = a character span INSIDE that string's Default/Female/Extra text. They are
therefore rebuilt per string according to where the emitted text came from:
    template language text -> the template's own links (byte-identical when nothing changes)
    other game language    -> that language's links (en/pl/ru assets in raw_assets/)
    Ukrainian text         -> no links (spans of another language would point at the wrong characters)
Then ExportMap[0].CookedSerialSize in the Zen header is fixed. The original 12-byte trailer is kept as is.

Sub-commands
  roundtrip   rebuild all 6 raw assets from the DB (en/pl/ru columns) and require byte equality   [exit 1 on any diff]
  build       build one asset from the DB:  --source BaseGame --template plpl --text uk --fallback en --out file

Binary spec: .agent/skills/avowed-l10n/SKILL.md
"""
import argparse
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_to_sqlite import (REPO, PKG_TAG_TAIL, ASSETS, parse_names, mapped_name, read_header,  # noqa: E402
                               skip_str_intset_map)

TEXT_LANGS = ("en", "pl", "ru", "uk")
FIELDS = ("default", "female", "extra")
LANG_CODE = {"en": "enus", "pl": "plpl", "ru": "ruru"}
TRAILER_LEN = 12


# --------------------------------------------------------------------------- FString
def ser_fstring(s: str) -> bytes:
    """UE FString: pure ASCII -> int32 len+1, bytes, 00 ; otherwise UTF-16LE -> int32 -(units+1), data, 00 00.
    Empty string -> int32 0 (the original files never mask text properties in the zero-mask, only e_id)."""
    if s == "":
        return b"\0\0\0\0"
    if "\0" in s:
        raise ValueError("NUL inside string")
    try:
        a = s.encode("ascii")
    except UnicodeEncodeError:
        u = s.encode("utf-16-le")
        return struct.pack("<i", -(len(u) // 2 + 1)) + u + b"\0\0"
    return struct.pack("<i", len(a) + 1) + a + b"\0"


def read_fstr_strict(b, pos):
    """Like extract_to_sqlite.read_fstr but refuses anything the writer could not reproduce."""
    (n,) = struct.unpack_from("<i", b, pos)
    pos += 4
    if n == 0:
        return "", pos
    if n > 0:
        raw = b[pos:pos + n]
        if raw[-1] != 0 or any(c >= 0x80 for c in raw):
            raise ValueError(f"non-ASCII / unterminated ANSI FString at {pos}")
        return raw[:-1].decode("ascii"), pos + n
    n = -n
    raw = b[pos:pos + 2 * n]
    if raw[-2:] != b"\0\0":
        raise ValueError(f"unterminated UTF-16 FString at {pos}")
    s = raw[:-2].decode("utf-16-le")
    if s.isascii():
        raise ValueError(f"pure-ASCII UTF-16 FString at {pos}: writer would emit ANSI")
    return s, pos + 2 * n


def entry_header(has_id):
    """Canonical FOEIStringEntry unversioned header: 1 fragment, 4 values {0:id,1:Default,2:Female,3:Extra};
    the three strings are always serialized, only a zero e_id is masked (zero-mask byte present then)."""
    zmask = 0 if has_id else 1
    v = (4 << 9) | (1 << 8) | (0x80 if zmask else 0)
    return struct.pack("<H", v) + (bytes([zmask]) if zmask else b"")


# --------------------------------------------------------------------------- NodeLinks
def ser_element(v):
    """FNodeLink = 5 int32 properties (key, node id, start, length, 0); zero values are masked."""
    zm = sum(1 << i for i in range(5) if v[i] == 0)
    h = (5 << 9) | (1 << 8) | (0x80 if zm else 0)
    return struct.pack("<H", h) + (bytes([zm]) if zm else b"") + b"".join(struct.pack("<i", x) for x in v if x)


def ser_links(od):
    out = [struct.pack("<ii", 0, len(od))]
    for key, els in od.items():
        out.append(struct.pack("<i", key) + b"\x00\x03" + struct.pack("<i", len(els)))
        out.extend(ser_element(e) for e in els)
    return b"".join(out)


def parse_links(b, pos):
    """TMap<int32,FNodeLinkArray> -> ({key: [5-tuples]}, new_pos). Refuses a non-canonical encoding."""
    start = pos
    rem, cnt = struct.unpack_from("<ii", b, pos)
    pos += 8
    if rem:
        raise ValueError("TMap with removals")
    od = {}
    for _ in range(cnt):
        (key,) = struct.unpack_from("<i", b, pos)
        pos += 4
        pos, pres = read_header(b, pos)
        els = []
        if 0 in pres:
            (acnt,) = struct.unpack_from("<i", b, pos)
            pos += 4
            for _ in range(acnt):
                pos, ep = read_header(b, pos)
                vals = []
                for i in range(5):
                    if i in ep:
                        vals.append(struct.unpack_from("<i", b, pos)[0])
                        pos += 4
                    else:
                        vals.append(0)
                els.append(tuple(vals))
        od[key] = els
    if ser_links(od) != bytes(b[start:pos]):
        raise ValueError(f"non-canonical NodeLinks map at {start}")
    return od, pos


def read_tables(b):
    """Full read of an asset -> [(table_name, {prop4/5/6: {key: [links]}}, {key: (default, female, extra)})]."""
    names = parse_names(b)
    (hs,) = struct.unpack_from("<I", b, 4)
    pos, present = read_header(b, hs)
    res = []
    if 0 in present:
        _rem, tcnt = struct.unpack_from("<ii", b, pos)
        pos += 8
        for _ in range(tcnt):
            kidx, knum = struct.unpack_from("<II", b, pos)
            pos += 8
            tname = mapped_name(names, kidx, knum)
            pos, pres = read_header(b, pos)
            links, entries = {}, {}
            for p in range(8):
                if p not in pres:
                    continue
                if p == 0:
                    pos += 8
                elif p <= 3:
                    pos = skip_str_intset_map(b, pos)
                elif p <= 6:
                    links[p], pos = parse_links(b, pos)
                else:
                    _erem, ecnt = struct.unpack_from("<ii", b, pos)
                    pos += 8
                    for _ in range(ecnt):
                        (e_key,) = struct.unpack_from("<i", b, pos)
                        pos += 4
                        pos, ep = read_header(b, pos)
                        if 0 in ep:
                            pos += 4
                        vals = []
                        for _p2 in (1, 2, 3):
                            s, pos = read_fstr_strict(b, pos)
                            vals.append(s)
                        entries[e_key] = tuple(vals)
            res.append((tname, links, entries))
    if pos != len(b) - TRAILER_LEN:
        raise ValueError("unparsed bytes before trailer")
    return res


def extract_links(b):
    """-> {(table_name, prop, key): [links]} for all three NodeLinks maps."""
    return {(t, p, k): els for t, links, _e in read_tables(b) for p, od in links.items() for k, els in od.items()}


def check_links(data):
    """Every link must be (key, node, start, length) with the span inside the matching text variant.
    -> (elements, out_of_range, key_mismatch)."""
    n = bad = badkey = 0
    for _t, links, entries in read_tables(data):
        for p, od in links.items():
            for k, els in od.items():
                text = entries.get(k, ("", "", ""))[p - 4]
                for e in els:
                    n += 1
                    badkey += e[0] != k
                    bad += not (e[2] >= 0 and e[3] >= 0 and e[2] + e[3] <= len(text))
    return n, bad, badkey


def make_links_loader(raw_dir, source):
    """links_of(lang) -> {(table, prop, key): [links]} from raw_assets/<source>_Text_<lang>.uasset (cached)."""
    cache = {}

    def links_of(lang):
        if lang not in cache:
            cache[lang] = extract_links(template_path(raw_dir, source, LANG_CODE[lang]).read_bytes())
        return cache[lang]
    return links_of


def compose_links(tname, p, tmpl_od, new_by_key, template_lang, links_of, stats):
    """NodeLinks map for property p (4=Default, 5=Female, 6=Extra) of one table, see module docstring."""
    v = p - 4
    res = {}
    for k in list(tmpl_od) + [k for k in new_by_key if k not in tmpl_od]:
        nb = new_by_key.get(k)
        if nb is None or nb[3] in ("template", template_lang):
            if k in tmpl_od:
                res[k] = tmpl_od[k]
                stats["links_kept"] = stats.get("links_kept", 0) + len(tmpl_od[k])
            continue
        text, src = nb[v], nb[3]
        if text and src != "uk":
            els = links_of(src).get((tname, p, k))
            if els:
                res[k] = els
                stats["links_from_" + src] = stats.get("links_from_" + src, 0) + len(els)
        if k in tmpl_od and k not in res:
            stats["links_dropped"] = stats.get("links_dropped", 0) + len(tmpl_od[k])
    return res


# --------------------------------------------------------------------------- core
def rebuild_asset(template: bytes, resolver, template_lang="pl", links_of=None, stats=None):
    """resolver(table_name, key, (default, female, extra)) -> (default, female, extra, source) with
    source in {'template', 'en', 'pl', 'ru', 'uk'}.  Returns the new asset bytes."""
    b = template
    stats = stats if stats is not None else {}
    if b[-4:] != PKG_TAG_TAIL:
        raise ValueError("template: missing package tag footer")
    names = parse_names(b)
    (hs,) = struct.unpack_from("<I", b, 4)
    exp_off, bun_off = struct.unpack_from("<2I", b, 0x20)       # ExportMapOffset, ExportBundleEntriesOffset
    if bun_off - exp_off != 72:
        raise ValueError("template has != 1 export")
    old_off, old_size = struct.unpack_from("<QQ", b, exp_off)    # ExportMap[0].CookedSerialOffset / CookedSerialSize
    if old_off != 0 or old_size != len(b) - hs - 4:
        raise ValueError(f"unexpected ExportMap[0] offset/size {old_off}/{old_size}")

    out, seg = [], hs                # `seg` = start of the not-yet-copied verbatim span
    pos, present = read_header(b, hs)
    if 0 in present:
        _rem, tcnt = struct.unpack_from("<ii", b, pos)
        pos += 8
        for _ in range(tcnt):
            kidx, knum = struct.unpack_from("<II", b, pos)
            pos += 8
            tname = mapped_name(names, kidx, knum)
            pos, pres = read_header(b, pos)
            tmpl_links, placeholder, new_by_key = {}, {}, {}
            for p in range(8):
                if p not in pres:
                    continue
                if p == 0:
                    pos += 8
                elif p <= 3:
                    pos = skip_str_intset_map(b, pos)
                elif p <= 6:
                    m0 = pos
                    tmpl_links[p], pos = parse_links(b, pos)
                    out.append(b[seg:m0])
                    placeholder[p] = len(out)
                    out.append(b"")                       # filled in once the table's texts are known
                    seg = pos
                else:
                    _erem, ecnt = struct.unpack_from("<ii", b, pos)
                    pos += 8
                    for _ in range(ecnt):
                        (e_key,) = struct.unpack_from("<i", b, pos)
                        pos += 4
                        h0 = pos
                        pos, ep = read_header(b, pos)
                        if b[h0:pos] != entry_header(0 in ep):
                            raise ValueError(f"non-canonical entry header in {tname}/{e_key} at {h0}")
                        id_bytes = b""
                        if 0 in ep:
                            id_bytes = b[pos:pos + 4]
                            pos += 4
                        old = []
                        for _p2 in (1, 2, 3):
                            s, pos = read_fstr_strict(b, pos)
                            old.append(s)
                        new = resolver(tname, e_key, tuple(old))
                        new_by_key[e_key] = new
                        out.append(b[seg:h0])
                        out.append(entry_header(0 in ep))
                        out.append(id_bytes)
                        out.extend(ser_fstring(s) for s in new[:3])
                        seg = pos
            for p, idx in placeholder.items():
                res = compose_links(tname, p, tmpl_links[p], new_by_key, template_lang, links_of, stats)
                out[idx] = ser_links(res)
    if len(b) - TRAILER_LEN != pos:
        raise ValueError(f"template: {len(b) - TRAILER_LEN - pos} unparsed bytes before trailer")
    out.append(b[seg:])             # remaining verbatim bytes, includes the original 12-byte trailer
    body = b"".join(out)

    header = bytearray(b[:hs])
    struct.pack_into("<Q", header, exp_off + 8, len(body) - 4)   # CookedSerialSize excludes the 4-byte package tag
    return bytes(header) + body


# --------------------------------------------------------------------------- DB access
def load_rows(db_path, source):
    """-> {(table_name, string_id): 12-tuple (en|pl|ru|uk) x (default, female, extra)}"""
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    # Ukrainian text ships only for rows that passed validation (draft/reviewed); needs_fix / new rows must never reach the game
    cols = ", ".join((f"CASE WHEN status IN ('draft','reviewed') THEN {lg}_{f} ELSE '' END" if lg == "uk" else f"{lg}_{f}")
                     for lg in TEXT_LANGS for f in FIELDS)
    rows = {(t, k): tuple(v) for t, k, *v in
            con.execute(f"SELECT table_name, string_id, {cols} FROM strings WHERE source_file=?", (source,))}
    con.close()
    return rows


def lang_of(rows_val, lang):
    i = TEXT_LANGS.index(lang) * 3
    return rows_val[i:i + 3]


def make_resolver(rows, text="uk", fallback="en", counters=None):
    """text='pl' etc. -> always that language (used by the round-trip test).
    text='uk' -> uk_* where uk_default is non-empty, otherwise the `fallback` language as a whole
    (en|pl|ru) or the untouched template text (fallback='template'). Rows missing in the DB keep the template text.
    Returns (default, female, extra, source)."""
    counters = counters if counters is not None else {}

    def resolve(table, key, orig):
        r = rows.get((table, key))
        if r is None:
            counters["missing_in_db"] = counters.get("missing_in_db", 0) + 1
            return (*orig, "template")
        if text != "uk":
            counters["used_" + text] = counters.get("used_" + text, 0) + 1
            return (*lang_of(r, text), text)
        if r[9]:
            counters["used_uk"] = counters.get("used_uk", 0) + 1
            return (*lang_of(r, "uk"), "uk")
        counters["fallback"] = counters.get("fallback", 0) + 1
        if fallback == "template":
            return (*orig, "template")
        return (*lang_of(r, fallback), fallback)
    return resolve


def verify_asset(data, template, resolver):
    """Re-read a built asset with the strict reader and check (1) every entry equals what the resolver must have
    produced for the template entry, (2) CookedSerialSize, trailer, (3) every NodeLinks span lies inside its text.
    Returns (entries, links, links_out_of_range)."""
    (hs,) = struct.unpack_from("<I", data, 4)
    exp_off = struct.unpack_from("<I", data, 0x20)[0]
    assert struct.unpack_from("<Q", data, exp_off + 8)[0] == len(data) - hs - 4, "CookedSerialSize"
    assert data[-4:] == PKG_TAG_TAIL and data[-12:-8] == template[-12:-8] and data[-8:-4] == b"\0\0\0\0", "trailer"
    assert data[:exp_off + 8] == template[:exp_off + 8] and data[exp_off + 16:hs] == template[exp_off + 16:hs], \
        "Zen header differs outside CookedSerialSize"
    got, tmpl = read_tables(data), read_tables(template)
    assert [t[0] for t in got] == [t[0] for t in tmpl], "table list"
    n = 0
    for (tname, _l, ents), (_t2, _l2, tents) in zip(got, tmpl):
        assert list(ents) == list(tents), f"entry keys of {tname}"
        for k, orig in tents.items():
            want = tuple(resolver(tname, k, orig)[:3])
            assert ents[k] == want, f"{tname}/{k} differs"
            n += 1
    links, bad, badkey = check_links(data)
    assert badkey == 0, "links with a foreign key"
    return n, links, bad


def template_path(raw_dir, source, lang):
    return Path(raw_dir) / f"{source}_Text_{lang}.uasset"


# --------------------------------------------------------------------------- CLI
def cmd_roundtrip(args):
    ok = True
    for (src, lang) in ASSETS:
        tmpl = template_path(args.raw_dir, src, lang).read_bytes()
        lg = lang[:2]
        rows = load_rows(args.db, src)
        out = rebuild_asset(tmpl, make_resolver(rows, text=lg), template_lang=lg)
        same = out == tmpl
        ok &= same
        links, bad, badkey = check_links(tmpl)
        if same:
            print(f"[OK  ] {src}_Text_{lang}: {len(out):,} B identical to template; "
                  f"links {links}, out-of-range {bad}, foreign-key {badkey}")
        else:
            first = next((i for i, (x, y) in enumerate(zip(out, tmpl)) if x != y), min(len(out), len(tmpl)))
            print(f"[FAIL] {src}_Text_{lang}: len {len(out)} vs {len(tmpl)}, first diff at {first}")
        ok &= bad == 0 and badkey == 0
    return 0 if ok else 1


def build_asset(raw_dir, db, source, template, text, fallback):
    """-> (bytes, template_bytes, stats). Used by build_iostore_patch.py."""
    rows = load_rows(db, source)
    tmpl = template_path(raw_dir, source, template).read_bytes()
    stats = {}
    resolver = make_resolver(rows, text, fallback, stats)
    data = rebuild_asset(tmpl, resolver, template_lang=template[:2], links_of=make_links_loader(raw_dir, source),
                         stats=stats)
    n, links, bad = verify_asset(data, tmpl, make_resolver(rows, text, fallback))
    assert bad == 0, f"{bad} NodeLinks spans outside their text"
    stats.update(entries_verified=n, links=links)
    return data, tmpl, stats


def cmd_build(args):
    data, tmpl, stats = build_asset(args.raw_dir, args.db, args.source, args.template, args.text, args.fallback)
    Path(args.out).write_bytes(data)
    print(f"built {args.out}: {len(data):,} B (template {len(tmpl):,} B), {stats}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", default=str(REPO / "raw_assets"))
    ap.add_argument("--db", default=str(REPO / "db" / "avowed_l10n.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("roundtrip")
    b = sub.add_parser("build")
    b.add_argument("--source", choices=("BaseGame", "D11"), required=True)
    b.add_argument("--template", choices=("enus", "plpl", "ruru"), default="plpl")
    b.add_argument("--text", choices=TEXT_LANGS, default="uk")
    b.add_argument("--fallback", choices=("en", "pl", "ru", "template"), default="en")
    b.add_argument("--out", required=True)
    args = ap.parse_args()
    sys.exit({"roundtrip": cmd_roundtrip, "build": cmd_build}[args.cmd](args))


if __name__ == "__main__":
    main()
