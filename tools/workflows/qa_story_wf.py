# Antigravity run_workflow port of qa_story_wf.js: semantic QA of story translations by strong-model editors.
# 1) python tools/batch_io.py sample --scope story --groups G --pct P --flagged --out logs/qa/<dir>/in   (done here)
# 2) one editor per Q*.jsonl -> logs/qa/<dir>/out/Q*.jsonl -> batch_io.py review (corrections re-validated)
# args: {dir:"conv_companions_1", groups:"conv:companions", pct:10, size:110, model:"pro", seed:11,
#        tablesFile:"" (100% review of listed tables instead of a sample), root:"D:/Xbox/avowed-ukr"}
import glob
import json
import os
import subprocess

A = args
ROOT = A.get("root", "D:/Xbox/avowed-ukr")
D = A["dir"]
SCHEMA = {"type": "object", "properties": {"batch": {"type": "string"}, "reviewed": {"type": "number"},
          "ok": {"type": "number"}, "fixed": {"type": "number"}, "unfixed": {"type": "number"},
          "issues": {"type": "string"}}, "required": ["batch", "reviewed", "ok"]}

indir = f"{ROOT}/logs/qa/{D}/in"
os.makedirs(f"{ROOT}/logs/qa/{D}/out", exist_ok=True)
if not glob.glob(f"{indir}/Q*.jsonl"):
    cmd = ["python", f"{ROOT}/tools/batch_io.py", "sample", "--scope", "story", "--groups", A["groups"],
           "--pct", str(A.get("pct", 10)), "--size", str(A.get("size", 110)), "--seed", str(A.get("seed", 11)),
           "--flagged", "--out", indir]
    if A.get("tablesFile"):
        cmd += ["--tables-file", A["tablesFile"]]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    log((r.stdout or r.stderr).strip()[:300])
names = sorted(os.path.basename(p)[:-6] for p in glob.glob(f"{indir}/Q*.jsonl")
               if not os.path.exists(f"{ROOT}/logs/qa/{D}/out/{os.path.basename(p)}"))


def prompt(q):
    return f"""Avowed QA editor {D}/{q}. You are a senior English->Ukrainian game-localization editor reviewing story text of the RPG Avowed (dialogues, quests, chatter barks, codex lore). Repo root: {ROOT} (absolute paths only).
1. Read {ROOT}/docs/style_uk.md (binding; sections 4, 8 and 9 matter most: ти/ви, quests without "ви", gender/neutral variants of the Envoy, chatter, codex genres, rules learned from QA).
2. Read {ROOT}/logs/qa/{D}/in/{q}.jsonl. Line 1 = header: "glossary" ([en, uk, uk_female, uk_extra, note] mandatory terms) and "voices" (speaker profiles of these scenes). Other lines = one row each: id, t (table tail), en (source), en_f/en_x, pl/pl_f/pl_x (Polish, for meaning and gender), ru (hint only), need (F/X), prev = [EN, UK] of the preceding line of the same table, and the CURRENT translation uk / uf / ux; "note" may carry a soft flag ("flag:glossary:X", "flag:formal-you", "flag:rusism", "flag:masc-past") - check exactly that, but a flag alone is not a defect (verify in context).
3. Judge every row: faithful meaning (EN first, PL for disambiguation), natural spoken/literary Ukrainian (no russisms, no calques), consistency with prev and with the character voice, correct ти/ви, glossary terms, correct feminine uf and neutral ux about the Envoy (not another person), preserved tags/variables/ICU, brevity of short barks/quest titles. Taste differences are NOT defects; wrong meaning, wrong term, wrong address form, wrong gender form, stilted or russianised phrasing, broken voice are.
4. Write {ROOT}/logs/qa/{D}/out/{q}.jsonl with your file-writing tool (UTF-8, valid JSON per line, every row exactly once): {{"id":<id>,"ok":true}} or {{"id":<id>,"ok":false,"issue":"<short reason>","uk":"<full corrected default>","uf":"<corrected female if the row has one>","ux":"<corrected neutral if the row has one>"}}.
5. Run: python {ROOT}/tools/batch_io.py review {ROOT}/logs/qa/{D}/out/{q}.jsonl  (re-validates corrections; failed ones become needs_fix). Touch nothing else.
6. Finish with JSON: batch, reviewed, ok, fixed, unfixed, issues (<=3 most frequent recurring problems, one line - they become new style rules)."""


async def run(q, sem):
    import asyncio
    async with sem:
        for attempt in range(A.get("retries", 3)):
            try:
                return await agent(prompt(q), schema=SCHEMA, role=f"QA {D}/{q}", model=A.get("model", "pro"))
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:200]
                if ("429" in msg or "RESOURCE_EXHAUSTED" in msg or "UNAVAILABLE" in msg) and attempt + 1 < A.get("retries", 3):
                    await asyncio.sleep(45 * (attempt + 1))
                    continue
                return {"batch": q, "reviewed": 0, "ok": 0, "error": msg}

phase("QA")
import asyncio
sem = asyncio.Semaphore(A.get("concurrency", 4))
res = await parallel(names, lambda q: run(q, sem)) if names else []
tot = {k: sum(int(r.get(k) or 0) for r in res) for k in ("reviewed", "ok", "fixed", "unfixed")}
print(json.dumps({"dir": D, "files": len(names), **tot,
                  "issues": [f"{r['batch']}: {r.get('error') or r.get('issues', '')}"[:220] for r in res
                             if r.get("issues") or r.get("error")][:12]}, ensure_ascii=False))
