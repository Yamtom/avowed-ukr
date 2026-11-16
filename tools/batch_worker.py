#!/usr/bin/env python3
"""Avowed l10n: resumable work queue for parallel translators (built on tools/batch_io.py, stdlib only).

State lives entirely in the DB (strings.status / claim_id / claimed_at / attempts, table batches), so a broken session
is resumed by simply starting workers again: stale claims (> 1 h) are released automatically.

  claim  --worker W --groups g1,g2 [--table-prefix P] [--fix] [--max-rows N --max-chars C]
         atomically reserves the next rows (priority = order of --groups; a worker keeps working on the table it
         touched last; small tables of one group are merged; a table with a live claim of another batch is skipped)
         and writes scratch/batches/batch_<id>.json; prints ONE json line {"batch","file",...} or {"batch":null}
  done   --batch ID            release rows of a batch the translator could not finish
  plan   [--scope story|ui]    remaining work per group (<= 15 lines)
  peek   --prefix P --n 40     evenly spaced sample lines of tables (for voice profiles)
  tables --prefix P --depth D  table counts by path prefix

Groups: conv:07_prologue quests chatter codex conv:companions conv:03_dawnshore conv:01_emerald_stair conv:04_shatterscarp
        conv:02_galawains_tusks conv:06_dawnshore_redux conv:05_garden (+ UI groups, see batch_io.py)
Batch file (JSON): {batch, group, tables:[...], glossary:[[en,uk,uf,ux,note]], voices:[...], ctx:{table_idx:[[en,uk]..]},
                    rows:[{id,en,en_f,en_x,pl,pl_f,pl_x,ru,need,t}]}  -> answer: scratch/batches/out/batch_<id>.jsonl
Answer line: {"id","uk","uf","ux"} | {"id","keep":1}; then  python tools/batch_io.py import <answer file>
"""
import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_io as B  # noqa: E402

SCRATCH = B.REPO / "scratch" / "batches"
TTL = 3600
LIMITS = {"codex": (35, 18000)}
DEFAULT_LIMIT = (200, 22000)
SMALL_TABLE = 60


def limits(g, a):
    r, c = LIMITS.get(g, DEFAULT_LIMIT)
    return (a.max_rows or r, a.max_chars or c)


def load_voices():
    p = B.DOCS / "voices.md"
    secs, cur = [], None
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                cur = {"name": line[3:].strip(), "match": [], "text": []}
                secs.append(cur)
            elif cur is not None and line.strip():
                if line.lower().startswith("match:"):
                    cur["match"] = [m.strip().lower() for m in line[6:].split("|") if m.strip()]
                else:
                    cur["text"].append(line.strip().lstrip("-• ").strip())
    return secs


def voices_for(tables, secs):
    out, size = [], 0
    # general sections (match: *) first, then the characters whose name occurs in the table paths of this batch
    ordered = [s for s in secs if "*" in s["match"]] + [s for s in secs if "*" not in s["match"]]
    for s in ordered:
        if "*" in s["match"] or any(m in t.lower() for t in tables for m in s["match"]):
            txt = f"{s['name']}: " + " ".join(s["text"])
            if size + len(txt) > 4800:
                continue
            out.append(txt)
            size += len(txt)
    return out


def prev_ctx(con, r):
    rows = con.execute("SELECT en_default, uk_default, status FROM strings WHERE source_file=? AND table_name=? AND string_id<? "
                       "ORDER BY string_id DESC LIMIT 5", (r["source_file"], r["table_name"], r["string_id"])).fetchall()
    return [[x[0][:200], x[1][:200] if x[2] in ("draft", "reviewed") else ""] for x in reversed(rows)]


def cmd_claim(a):
    con = B.connect()
    now = int(time.time())
    status = "needs_fix" if a.fix else "new"
    maxtry = a.max_attempts or (6 if a.fix else 3)
    pfx = (a.table_prefix or "").replace("'", "''")
    extra = f" AND table_name LIKE '{pfx}%'" if pfx else ""
    con.execute("BEGIN IMMEDIATE")
    picked, group = [], None
    try:
        con.execute("UPDATE strings SET claim_id='', claimed_at=0 WHERE claim_id!='' AND claimed_at<?", (now - TTL,))
        busy = {r[0] for r in con.execute("SELECT DISTINCT table_name FROM strings WHERE claim_id!=''")}
        last = con.execute("SELECT tbl, grp FROM batches WHERE worker=? ORDER BY id DESC LIMIT 1", (a.worker,)).fetchone()
        for g in [x for x in a.groups.split(",") if x]:
            max_rows, max_chars = limits(g, a)
            tabs = [(r[0], r[1]) for r in con.execute(
                f"SELECT table_name, COUNT(*) FROM strings WHERE {B.group_where(g)}{extra} AND status=? AND claim_id='' "
                f"AND attempts<? GROUP BY table_name ORDER BY table_name", (status, maxtry))]
            if g.startswith("conv:"):             # dialogue parts of one table stay sequential; codex/quests/chatter rows are independent
                tabs = [t for t in tabs if t[0] not in busy]
            if not tabs:
                continue
            if last and last["grp"] == g:
                tabs.sort(key=lambda t: t[0] != last["tbl"])     # stable: keep the worker's current table first
            chars, full = 0, False
            for tname, cnt in tabs:
                if picked and cnt > SMALL_TABLE:
                    continue                                      # only small tables are merged into an open batch
                for r in con.execute("SELECT * FROM strings WHERE table_name=? AND status=? AND claim_id='' AND attempts<? "
                                     "AND source_file=(SELECT source_file FROM strings WHERE table_name=? LIMIT 1) ORDER BY string_id",
                                     (tname, status, maxtry, tname)):
                    w = len(r["en_default"]) + len(r["en_female"]) + len(r["en_extra"]) + 30
                    if picked and (len(picked) >= max_rows or chars + w > max_chars):
                        full = True
                        break
                    picked.append(r)
                    chars += w
                if full or len(picked) >= max_rows or cnt > SMALL_TABLE:
                    break
            if picked:
                group = g
                break
        if not picked:
            con.execute("COMMIT")
            print(json.dumps({"batch": None, "msg": "queue empty for " + a.groups + (" prefix " + a.table_prefix if a.table_prefix else "")}))
            return
        cur = con.execute("INSERT INTO batches(worker, grp, tbl, n_rows, created_at) VALUES (?,?,?,?,?)",
                          (a.worker, group, picked[0]["table_name"], len(picked), now))
        bid = f"b{cur.lastrowid:05d}"
        ids = [r["id"] for r in picked]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            con.execute(f"UPDATE strings SET claim_id=?, claimed_at=?, attempts=attempts+1 WHERE id IN ({','.join('?' * len(part))})",
                        [bid, now] + part)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    tables = list(dict.fromkeys(r["table_name"] for r in picked))
    tix = {t: i for i, t in enumerate(tables)}
    rows = []
    for r in picked:
        o = B.fmt_row(r, with_prev=a.fix)
        if len(tables) > 1:
            o["t"] = tix[r["table_name"]]
        rows.append(o)
    ctx = {}
    for t in tables:
        first = next(r for r in picked if r["table_name"] == t)
        c = prev_ctx(con, first)
        if c:
            ctx[str(tix[t])] = c
    texts = [x for r in picked for x in (r["en_default"], r["en_female"], r["en_extra"])]
    batch = {"batch": bid, "group": group, "tables": tables, "glossary": B.glossary_subset(B.glossary_rows(con), texts),
             "voices": voices_for(tables, load_voices()), "ctx": ctx, "rows": rows}
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "out").mkdir(exist_ok=True)
    f = SCRATCH / f"batch_{bid}.json"
    # valid JSON, but one row per line so that the translator can Read it in chunks
    head = json.dumps({k: v for k, v in batch.items() if k != "rows"}, ensure_ascii=False, separators=(",", ":"))
    body = ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows)
    f.write_text(head[:-1] + ',"rows":[\n' + body + "\n]}", encoding="utf-8")
    left = con.execute(f"SELECT COUNT(*) FROM strings WHERE {B.group_where(group)}{extra} AND status=? AND claim_id=''", (status,)).fetchone()[0]
    print(json.dumps({"batch": bid, "file": str(f), "out": str(SCRATCH / "out" / f"batch_{bid}.jsonl"), "group": group,
                      "rows": len(rows), "tables": len(tables), "left_in_group": left}))


def cmd_done(a):
    con = B.connect()
    con.execute("BEGIN IMMEDIATE")
    n = con.execute("UPDATE strings SET claim_id='', claimed_at=0 WHERE claim_id=?", (a.batch,)).rowcount
    con.execute("COMMIT")
    print(f"released {n} unfinished rows of {a.batch}")


def cmd_reap(a):
    """After a broken session: import every answer file (batch_<id>.jsonl, then _fix1.._fixN) of batches that still hold
    claims, then (--release) free the claims of dead batches so their rows return to the queue (attempt not counted).
    Run only when no translator is working (or with --older-than SECONDS to touch only old claims)."""
    import contextlib
    import io
    con = B.connect()
    lim = int(time.time()) - (a.older_than or 0)
    bids = sorted(r[0] for r in con.execute("SELECT DISTINCT claim_id FROM strings WHERE claim_id!='' AND claimed_at<=?", (lim,)))
    if a.worker:                       # only the batches of one (crashed) worker
        mine = {f"b{r[0]:05d}" for r in con.execute("SELECT id FROM batches WHERE worker=?", (a.worker,))}
        bids = [b for b in bids if b in mine]
    files = ok = fails = 0
    for bid in bids:
        fs = sorted((SCRATCH / "out").glob(f"batch_{bid}*.jsonl"), key=lambda p: (len(p.stem), p.stem))
        for f in fs:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                o, n = B.cmd_import(argparse.Namespace(file=str(f), batch=bid, force=False), str(f))
            files += 1
            ok += o
            fails += n
    released = 0
    if a.release and bids:
        con.execute("BEGIN IMMEDIATE")
        for i in range(0, len(bids), 400):
            part = bids[i:i + 400]
            released += con.execute(f"UPDATE strings SET claim_id='', claimed_at=0, attempts=MAX(attempts-1,0) "
                                    f"WHERE claim_id IN ({','.join('?' * len(part))})", part).rowcount
        con.execute("COMMIT")
    print(f"reap: {len(bids)} claimed batches, {files} answer files imported (ok {ok}, rejected/needs_fix {fails}), "
          f"released {released} rows")


def cmd_plan(a):
    con = B.connect()
    groups = B.STORY_PRIORITY if a.scope == "story" else B.PRIORITY
    print(f"{'group':<26}{'new':>7}{'claimed':>8}{'needsfx':>8}{'draft+rev':>10}{'chars(new)':>11}{'batches~':>9}")
    tn = 0
    for g in groups:
        w = B.group_where(g)
        q = lambda cond: con.execute(f"SELECT COUNT(*), COALESCE(SUM(length(en_default)),0) FROM strings WHERE {w} AND {cond}").fetchone()  # noqa: E731
        new, ch = q("status='new' AND claim_id=''")
        claimed = q("claim_id!=''")[0]
        fx = q("status='needs_fix'")[0]
        done = q("status IN ('draft','reviewed')")[0]
        mr, mc = LIMITS.get(g, DEFAULT_LIMIT)
        est = max(math.ceil(new / mr), math.ceil(ch * 1.3 / mc)) if new else 0
        tn += est
        print(f"{g:<26}{new:>7}{claimed:>8}{fx:>8}{done:>10}{ch:>11}{est:>9}")
    print(f"{'estimated batches':<26}{tn:>53}")


def cmd_peek(a):
    con = B.connect()
    rows = con.execute("SELECT id, table_name, en_default, pl_default, pl_female FROM strings WHERE table_name LIKE ? AND en_default!='' "
                       "ORDER BY table_name, string_id", (a.prefix + "%",)).fetchall()
    if not rows:
        print("no rows")
        return
    step = max(1, len(rows) // a.n)
    print(f"{len(rows)} lines under {a.prefix}; showing {min(a.n, len(rows))} evenly spaced (id | table-tail | EN | PL)")
    for r in rows[::step][:a.n]:
        print(f"{r['id']} | {r['table_name'][-26:]} | {r['en_default'][:a.chars].replace(chr(10), ' ')} | {r['pl_default'][:a.chars - 30].replace(chr(10), ' ')}")


def cmd_tables(a):
    con = B.connect()
    agg = {}
    for t, n in con.execute("SELECT table_name, COUNT(*) FROM strings WHERE table_name LIKE ? GROUP BY table_name", (a.prefix + "%",)):
        k = "/".join(t.split("/")[:a.depth])
        c = agg.setdefault(k, [0, 0])
        c[0] += 1
        c[1] += n
    for k, (nt, nr) in sorted(agg.items(), key=lambda kv: -kv[1][1])[:a.limit]:
        print(f"{k:<70}{nt:>5} tables{nr:>7} rows")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--worker", required=True)
    c.add_argument("--groups", required=True)
    c.add_argument("--table-prefix")
    c.add_argument("--fix", action="store_true")
    c.add_argument("--max-rows", type=int)
    c.add_argument("--max-chars", type=int)
    c.add_argument("--max-attempts", type=int)
    d = sub.add_parser("done")
    d.add_argument("--batch", required=True)
    rp = sub.add_parser("reap")
    rp.add_argument("--release", action="store_true")
    rp.add_argument("--older-than", type=int, default=0)
    rp.add_argument("--worker")
    p = sub.add_parser("plan")
    p.add_argument("--scope", choices=("story", "ui"), default="story")
    k = sub.add_parser("peek")
    k.add_argument("--prefix", required=True)
    k.add_argument("--n", type=int, default=40)
    k.add_argument("--chars", type=int, default=150)
    t = sub.add_parser("tables")
    t.add_argument("--prefix", default="")
    t.add_argument("--depth", type=int, default=2)
    t.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    {"claim": cmd_claim, "done": cmd_done, "reap": cmd_reap, "plan": cmd_plan, "peek": cmd_peek, "tables": cmd_tables}[a.cmd](a)


if __name__ == "__main__":
    main()