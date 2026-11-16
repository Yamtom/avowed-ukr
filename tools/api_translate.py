#!/usr/bin/env python3
"""Avowed l10n: API translator on top of the shared DB queue (tools/batch_worker.py claim -> tools/batch_io.py import).

API mode and agent mode use ONE queue: rows are owned through strings.claim_id (atomic BEGIN IMMEDIATE claim), import
refuses rows claimed by another batch, and unfinished rows are released with `batch_worker.py done` / `reap`.

  direct        --groups G [--workers 4] [--max-batches N] --budget-usd X
                parallel Messages API calls: claim -> translate -> import -> <=2 fix rounds (_fix1/_fix2) -> release
  prepare-batch --groups G --n N --budget-usd X
                claim N batches and submit them as ONE Message Batch (50 % price); claims get a 25 h lease
  collect-batch [--wait]   import the results of every submitted Message Batch (resumable; failed rows stay needs_fix,
                errored/expired requests are released back to the queue)
  dry-run       --groups G  claim ONE batch on a temporary COPY of the DB, print the prompt size and test JSONL parsing,
                import, error detection and recovery (no API call, the real DB is not touched)
  stats         API spend and runs (table api_runs) + open Message Batches (table api_jobs)

Needs the `anthropic` SDK and a credential (ANTHROPIC_API_KEY or an `ant auth login` profile) for direct/prepare/collect.
The key is never printed. Default model claude-sonnet-5-5 (story dialogue: use a model that passed QA, never Haiku
without a QA test). Answer format = JSONL {"id","uk","uf","ux"} | {"id","keep":1} (same as the agent workers).
"""
import argparse
import contextlib
import io
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_io as B  # noqa: E402
import batch_worker as W  # noqa: E402

PRICES = {  # USD per 1M tokens: input, output, cache read, cache write (5 min)
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
STYLE_SECTIONS = ("1", "3", "4", "5", "6", "8", "9")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
LOCK = threading.Lock()

RULES = """You are a professional English->Ukrainian game localizer (RPG Avowed, world of Eora).
INPUT (user message, JSON): "glossary" = mandatory equivalents [en, uk, uk_female, uk_extra, note] (decline them, keep the stem);
"voices" = speaker profiles (keep each voice and one form of address ти/ви per NPC through a scene);
"ctx" = READ-ONLY preceding lines {table index: [[en, uk-if-translated], ...]} - never translate them;
"rows" = what you translate: id, en (source of MEANING; if empty use pl), en_f / en_x (female / neutral English),
pl / pl_f / pl_x (Polish: only for gender and ти/ви), ru (hint only, never copy wording), need ("F" -> "uf" required,
"X" -> "ux" required), t (table index), and in fix rounds uk/uf/ux/note = previous attempt and why it was rejected.
VARIANTS: uk = masculine/base, uf = feminine Envoy, ux = neutral they/them Envoy (3rd person -> plural, 2nd person ->
rephrase; never "зробив(ла)"). When need is empty the row has ONE text for every Envoy: never put a gendered past
verb/adjective on the Envoy ("я прийшов", "ти готовий") - rephrase (present tense, impersonal, noun phrase).
Do not invent a gender for unnamed NPCs (follow pl). Female speakers (pl feminine forms) speak in the feminine.
NEVER change tags (<i>, </>, <img .../> ...), {variables}, ICU |plural(one=..,few=..,many=..) keys - translate only text.
Keep line breaks (\\n, \\r\\n) and every vocative ("Envoy" -> "Посланцю"). No russisms/calques: спробуй (не постарайся),
радше (не скоріше), понад (не більш ніж), ходімо (не давай підемо), треба/мусимо (не маємо + inf.), no -ючий/-ачий participles.
OUTPUT: ONLY JSON Lines, one object per input row, same ids, no Markdown, no comments:
{"id":123,"uk":"...","uf":"...","ux":"..."}  ("uf"/"ux" only when need has F/X or a previous attempt had them)
{"id":123,"keep":1}  for names, codes and sounds that stay unchanged."""


# ---------------------------------------------------------------- prompt
def system_prompt():
    txt = (B.DOCS / "style_uk.md").read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=## )", txt)
    keep = [p.strip() for p in parts if re.match(r"## (\d+)\.", p) and re.match(r"## (\d+)\.", p).group(1) in STYLE_SECTIONS]
    return RULES + "\n\nSTYLE GUIDE (binding):\n" + "\n\n".join(keep)


def user_payload(batch):
    return json.dumps({"glossary": batch.get("glossary", []), "voices": batch.get("voices", []),
                       "ctx": batch.get("ctx", {}), "rows": batch["rows"]}, ensure_ascii=False, separators=(",", ":"))


def est_tokens(s):
    return len(s) // 3 + 1          # Ukrainian/English mix; conservative for budgeting


def parse_answer(text, ids):
    """JSONL -> {id: obj}; tolerant to code fences and stray prose. Returns (answers, problems)."""
    ans, probs = {}, []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
            i = int(o["id"])
        except Exception as e:  # noqa: BLE001
            probs.append(f"bad line: {str(e)[:50]}")
            continue
        if i not in ids:
            probs.append(f"{i}: unknown id")
            continue
        o["id"] = i
        ans[i] = {k: v for k, v in o.items() if k in ("id", "uk", "uf", "ux", "keep")}
    missing = [i for i in ids if i not in ans]
    if missing:
        probs.append(f"missing {len(missing)} ids")
    return ans, probs


def write_jsonl(path, answers):
    path.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in answers), encoding="utf-8")


def quiet(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*args)
    return r, buf.getvalue()


def do_import(path, bid):
    (ok, nfail), _ = quiet(B.cmd_import, argparse.Namespace(file=str(path), batch=bid, force=False), str(path))
    bad = {}
    f = B.LOGS / "import" / f"{path.stem}.fail.txt"
    for line in (f.read_text(encoding="utf-8").splitlines() if f.exists() else []):
        m = re.match(r"(\d+): (.*)", line)
        if m and "claimed by another batch" not in m.group(2):
            bad[int(m.group(1))] = m.group(2)
    return ok, bad


# ---------------------------------------------------------------- DB bookkeeping (state lives in the DB)
def ensure_tables(con):
    con.execute("CREATE TABLE IF NOT EXISTS api_runs (id INTEGER PRIMARY KEY, ts INTEGER, mode TEXT, batch TEXT, model TEXT, "
                "in_tok INTEGER, out_tok INTEGER, cache_r INTEGER, cache_w INTEGER, usd REAL, ok INTEGER, fixes INTEGER, note TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS api_jobs (msg_batch TEXT, batch TEXT PRIMARY KEY, model TEXT, status TEXT, "
                "created_at INTEGER, collected_at INTEGER)")


def cost(model, u, batch_api=False):
    pi, po, pr, pw = PRICES.get(model, PRICES["claude-opus-5-5"])
    usd = (u["in"] * pi + u["out"] * po + u["cr"] * pr + u["cw"] * pw) / 1e6
    return usd * (0.5 if batch_api else 1.0)


def spent(con):
    return con.execute("SELECT COALESCE(SUM(usd),0) FROM api_runs").fetchone()[0]


def log_run(mode, bid, model, u, usd, ok, fixes, note=""):
    con = B.connect()
    ensure_tables(con)
    con.execute("INSERT INTO api_runs(ts,mode,batch,model,in_tok,out_tok,cache_r,cache_w,usd,ok,fixes,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), mode, bid, model, u["in"], u["out"], u["cr"], u["cw"], usd, ok, fixes, note[:200]))


# ---------------------------------------------------------------- API
def client():
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic SDK not installed (pip install anthropic) - use agent mode (Sonnet workers) instead")
    try:
        return anthropic, anthropic.Anthropic(max_retries=6)        # SDK: exponential backoff on 408/409/429/5xx
    except Exception as e:  # noqa: BLE001  (missing credential)
        sys.exit(f"no API credential ({type(e).__name__}) - set ANTHROPIC_API_KEY or `ant auth login`; or use agent mode")


def request_params(a, sysp, payload):
    out_budget = min(64000, max(16000, est_tokens(payload) * 2))      # Ukrainian output + adaptive thinking
    return {"model": a.model, "max_tokens": out_budget,
            "system": [{"type": "text", "text": sysp, "cache_control": {"type": "ephemeral"}}],   # static part first = cached
            "messages": [{"role": "user", "content": payload}],
            "output_config": {"effort": a.effort}}


def call(cl, a, sysp, payload):
    p = request_params(a, sysp, payload)
    with cl.beta.messages.stream(betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}, **p) as s:
        m = s.get_final_message()
    u = m.usage
    use = {"in": u.input_tokens, "out": u.output_tokens, "cr": u.cache_read_input_tokens or 0, "cw": u.cache_creation_input_tokens or 0}
    if m.stop_reason == "refusal":
        return "", use, "refusal"
    text = "".join(b.text for b in m.content if b.type == "text")
    return text, use, m.stop_reason


def claim(a, worker, fix=False):
    ns = argparse.Namespace(worker=worker, groups=a.groups, table_prefix=a.table_prefix, fix=fix, max_rows=a.max_rows,
                            max_chars=a.max_chars, max_attempts=None)
    _, out = quiet(W.cmd_claim, ns)
    return json.loads(out.strip().splitlines()[-1])


def fix_rows(batch, bad, answers):
    rows = []
    for r in batch["rows"]:
        if r["id"] in bad:
            o = dict(r)
            prev = answers.get(r["id"], {})
            for k in ("uk", "uf", "ux"):
                if prev.get(k):
                    o[k] = prev[k]
            o["note"] = bad[r["id"]]
            rows.append(o)
    return rows


def run_batch(cl, anthropic, a, sysp, info, worker):
    bid, out = info["batch"], W.SCRATCH / "out"
    out.mkdir(parents=True, exist_ok=True)
    batch = json.loads(Path(info["file"]).read_text(encoding="utf-8"))
    tot = {"in": 0, "out": 0, "cr": 0, "cw": 0}
    answers, ok, rnd, rows = {}, 0, 0, batch["rows"]
    try:
        while rows and rnd <= 2:                  # first pass + max 2 fix rounds
            sub = dict(batch, rows=rows)
            text, u, stop = call(cl, a, sysp, user_payload(sub))
            for k in tot:
                tot[k] += u[k]
            ans, probs = parse_answer(text, {r["id"] for r in rows})
            if not ans:
                break
            answers.update(ans)
            f = out / (f"batch_{bid}.jsonl" if rnd == 0 else f"batch_{bid}_fix{rnd}.jsonl")
            write_jsonl(f, ans.values())
            o, bad = do_import(f, bid)
            ok += o
            for i in {r["id"] for r in rows} - set(ans):          # lost rows: retry them in the fix round
                bad.setdefault(i, "row missing in the answer")
            rows = fix_rows(batch, bad, answers)
            rnd += 1
    except anthropic.APIStatusError as e:
        print(f"[{worker}] {bid}: API {e.status_code}; releasing", flush=True)
    finally:
        _, msg = quiet(W.cmd_done, argparse.Namespace(batch=bid))     # leftovers back to the queue (needs_fix stay)
    usd = cost(a.model, tot)
    log_run("direct", bid, a.model, tot, usd, ok, max(rnd - 1, 0), f"left {len(rows)}")
    print(f"[{worker}] {bid}: rows {len(batch['rows'])} ok {ok} needs_fix {len(rows)} rounds {rnd} "
          f"tok in {tot['in']}+cache {tot['cr']}/{tot['cw']} out {tot['out']} ${usd:.3f}", flush=True)
    return usd


def cmd_direct(a):
    anthropic, cl = client()
    sysp = system_prompt()
    con = B.connect()
    ensure_tables(con)
    state = {"n": 0, "spent": spent(con)}

    def worker(w):
        while True:
            with LOCK:
                if state["spent"] >= a.budget_usd or (a.max_batches and state["n"] >= a.max_batches):
                    return
                state["n"] += 1
            info = claim(a, w)
            if not info.get("batch"):
                return
            usd = run_batch(cl, anthropic, a, sysp, info, w)
            with LOCK:
                state["spent"] += usd

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(worker, [f"api{i + 1:02d}" for i in range(a.workers)]))
    print(f"direct: {state['n']} claims, total API spend ${state['spent']:.2f} (budget ${a.budget_usd})")


# ---------------------------------------------------------------- Message Batches API
def cmd_prepare(a):
    anthropic, cl = client()
    sysp = system_prompt()
    con = B.connect()
    ensure_tables(con)
    reqs, bids, est = [], [], 0.0
    for _ in range(a.n):
        info = claim(a, "apibatch")
        if not info.get("batch"):
            break
        batch = json.loads(Path(info["file"]).read_text(encoding="utf-8"))
        p = request_params(a, sysp, user_payload(batch))
        est += cost(a.model, {"in": est_tokens(p["messages"][0]["content"]) + est_tokens(sysp), "out": p["max_tokens"] // 2,
                              "cr": 0, "cw": 0}, batch_api=True)
        reqs.append({"custom_id": info["batch"], "params": p})
        bids.append(info["batch"])
    if not reqs:
        print("queue empty")
        return
    if spent(con) + est > a.budget_usd:
        for b in bids:
            quiet(W.cmd_done, argparse.Namespace(batch=b))
        sys.exit(f"estimated ${est:.2f} would exceed the budget ${a.budget_usd} (spent ${spent(con):.2f}); claims released")
    lease = int(time.time()) + 25 * 3600                   # Message Batches may take up to 24 h: keep the claims alive
    con.execute("BEGIN IMMEDIATE")
    for b in bids:
        con.execute("UPDATE strings SET claimed_at=? WHERE claim_id=?", (lease, b))
    con.execute("COMMIT")
    try:
        mb = cl.messages.batches.create(requests=reqs)
    except Exception:
        for b in bids:
            quiet(W.cmd_done, argparse.Namespace(batch=b))
        raise
    now = int(time.time())
    con.executemany("INSERT OR REPLACE INTO api_jobs VALUES (?,?,?,?,?,0)", [(mb.id, b, a.model, "submitted", now) for b in bids])
    print(f"submitted {mb.id}: {len(bids)} batches ({bids[0]}..{bids[-1]}), est ${est:.2f}")


def cmd_collect(a):
    anthropic, cl = client()
    con = B.connect()
    ensure_tables(con)
    jobs = con.execute("SELECT msg_batch, batch, model FROM api_jobs WHERE status='submitted'").fetchall()
    for mid in sorted({j[0] for j in jobs}):
        mb = cl.messages.batches.retrieve(mid)
        while a.wait and mb.processing_status != "ended":
            time.sleep(60)
            mb = cl.messages.batches.retrieve(mid)
        if mb.processing_status != "ended":
            print(f"{mid}: {mb.processing_status} (processing {mb.request_counts.processing})")
            continue
        model = next(j[2] for j in jobs if j[0] == mid)
        for res in cl.messages.batches.results(mid):              # results come in any order: key by custom_id
            bid = res.custom_id
            ok, left, u = 0, 0, {"in": 0, "out": 0, "cr": 0, "cw": 0}
            if res.result.type == "succeeded":
                m = res.result.message
                u = {"in": m.usage.input_tokens, "out": m.usage.output_tokens,
                     "cr": m.usage.cache_read_input_tokens or 0, "cw": m.usage.cache_creation_input_tokens or 0}
                batch = json.loads((W.SCRATCH / f"batch_{bid}.json").read_text(encoding="utf-8"))
                ans, _ = parse_answer("".join(b.text for b in m.content if b.type == "text"), {r["id"] for r in batch["rows"]})
                if ans:
                    f = W.SCRATCH / "out" / f"batch_{bid}.jsonl"
                    write_jsonl(f, ans.values())
                    ok, bad = do_import(f, bid)
                    left = len(bad)              # stay needs_fix -> `batch_worker.py claim --fix` (agents or direct)
            quiet(W.cmd_done, argparse.Namespace(batch=bid))
            log_run("batch", bid, model, u, cost(model, u, batch_api=True), ok, 0, f"{res.result.type}; needs_fix {left}")
            con.execute("UPDATE api_jobs SET status=?, collected_at=? WHERE batch=?", (res.result.type, int(time.time()), bid))
            print(f"{bid}: {res.result.type} ok {ok} needs_fix {left}")


# ---------------------------------------------------------------- dry run (temp DB copy, no API)
def cmd_dry(a):
    tmp = Path(tempfile.mkdtemp(prefix="avowed_dry_"))
    real_db, real_scratch = B.DB, W.SCRATCH
    try:
        src = sqlite3.connect(real_db)
        dst = sqlite3.connect(tmp / "copy.db")
        src.backup(dst)                                     # consistent snapshot even with live writers (WAL)
        src.close()
        dst.close()
        B.DB, W.SCRATCH, B.LOGS = tmp / "copy.db", tmp / "batches", tmp / "logs"
        info = claim(a, "dryrun")
        if not info.get("batch"):
            print("queue empty:", info.get("msg"))
            return
        bid = info["batch"]
        batch = json.loads(Path(info["file"]).read_text(encoding="utf-8"))
        sysp, pay = system_prompt(), user_payload(batch)
        p = request_params(a, sysp, pay)
        need = sum(1 for r in batch["rows"] if r.get("need"))
        print(f"[1] claim on copy: {bid} group {info['group']} rows {len(batch['rows'])} tables {len(batch['tables'])} "
              f"(gender rows {need}), glossary {len(batch['glossary'])}, voices {len(batch['voices'])}, ctx {len(batch['ctx'])}")
        print(f"[2] prompt: system {len(sysp)} ch ~{est_tokens(sysp)} tok (cached), user {len(pay)} ch ~{est_tokens(pay)} tok, "
              f"max_tokens {p['max_tokens']}; est. cost {a.model} ${cost(a.model, {'in': est_tokens(pay), 'out': est_tokens(pay), 'cr': est_tokens(sysp), 'cw': 0}):.3f}"
              f" direct / ${cost(a.model, {'in': est_tokens(pay) + est_tokens(sysp), 'out': est_tokens(pay), 'cr': 0, 'cw': 0}, True):.3f} batch")
        print("    voices:", "; ".join(v.split(":")[0] for v in batch["voices"])[:200])
        # [3] synthetic model answer: keep-all rows + 3 injected defects + prose/fence noise
        ids = [r["id"] for r in batch["rows"]]
        lines = ["```jsonl", "Here is the translation:"] + [json.dumps({"id": i, "keep": 1}) for i in ids[3:]]
        tagged = next((r for r in batch["rows"] if re.search(B.TAG, r["en"]) or re.search(B.VAR, r["en"])), None)
        lines += [json.dumps({"id": ids[0], "uk": "Привіт"}),            # ok
                  json.dumps({"id": 99999999, "uk": "x"}),               # unknown id
                  '{"id": %d, "uk": "broken' % ids[1]]                   # invalid JSON -> id[1] missing
        if tagged and tagged["id"] not in ids[:2]:
            lines.append(json.dumps({"id": tagged["id"], "uk": "тег втрачено"}))
        ans, probs = parse_answer("\n".join(lines + ["```"]), set(ids))
        print(f"[3] parse: {len(ans)} answers, detected: {'; '.join(probs)}")
        f = W.SCRATCH / "out" / f"batch_{bid}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(f, ans.values())
        ok, bad = do_import(f, bid)
        con = B.connect()
        st = dict(con.execute(f"SELECT status, COUNT(*) FROM strings WHERE id IN ({','.join(map(str, ids))}) GROUP BY status").fetchall())
        print(f"[4] import on copy: ok {ok}, flagged {len(bad)} {list(bad.items())[:2]}; statuses {st}")
        print(f"[5] fix round would resend {len(fix_rows(batch, bad, ans)) + (len(ids) - len(ans))} rows -> batch_{bid}_fix1.jsonl")
        held = con.execute("SELECT COUNT(*) FROM strings WHERE claim_id=?", (bid,)).fetchone()[0]
        con.execute("UPDATE strings SET claimed_at=claimed_at-7200 WHERE claim_id=?", (bid,))     # simulate a crash 2 h ago
        _, rep = quiet(W.cmd_reap, argparse.Namespace(release=True, older_than=3600, worker=None))
        left = con.execute("SELECT COUNT(*) FROM strings WHERE claim_id=?", (bid,)).fetchone()[0]
        print(f"[6] interruption: {held} rows still claimed -> {rep.strip()} -> claimed now {left}")
        two = claim(a, "dryrun2")
        print(f"[7] next claim after recovery: {two.get('batch')} rows {two.get('rows')} (real DB untouched: {real_db})")
    finally:
        B.DB, W.SCRATCH = real_db, real_scratch
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_stats(a):
    con = B.connect()
    ensure_tables(con)
    for r in con.execute("SELECT mode, model, COUNT(*), SUM(ok), SUM(in_tok), SUM(cache_r), SUM(out_tok), SUM(usd) FROM api_runs GROUP BY mode, model"):
        print(f"{r[0]:<7}{r[1]:<20} batches {r[2]:>4} rows ok {r[3] or 0:>6} in {r[4] or 0:>9} cache {r[5] or 0:>9} out {r[6] or 0:>8} ${r[7] or 0:.2f}")
    for r in con.execute("SELECT msg_batch, status, COUNT(*) FROM api_jobs GROUP BY msg_batch, status ORDER BY MAX(created_at) DESC LIMIT 8"):
        print(f"msg batch {r[0]} {r[1]} x{r[2]}")
    print(f"total API spend ${spent(con):.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("direct", "prepare-batch", "dry-run"):
        p = sub.add_parser(name)
        p.add_argument("--groups", required=True)
        p.add_argument("--table-prefix")
        p.add_argument("--max-rows", type=int, default=150)
        p.add_argument("--max-chars", type=int, default=18000)
        p.add_argument("--model", default="claude-sonnet-5-5")
        p.add_argument("--effort", default="medium", choices=("low", "medium", "high"))
        p.add_argument("--budget-usd", type=float, default=0.0, help="hard cap on the total API spend recorded in api_runs")
        if name == "direct":
            p.add_argument("--workers", type=int, default=4, choices=range(1, 7))
            p.add_argument("--max-batches", type=int, default=0)
        if name == "prepare-batch":
            p.add_argument("--n", type=int, default=10)
    c = sub.add_parser("collect-batch")
    c.add_argument("--wait", action="store_true")
    sub.add_parser("stats")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd in ("direct", "prepare-batch") and a.budget_usd <= 0:
        sys.exit("--budget-usd is required for paid modes")
    {"direct": cmd_direct, "prepare-batch": cmd_prepare, "collect-batch": cmd_collect, "dry-run": cmd_dry,
     "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    main()
