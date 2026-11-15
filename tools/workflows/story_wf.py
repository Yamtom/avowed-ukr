# Antigravity run_workflow port of story_wf.js: DB-queue translators (tools/batch_worker.py claim -> translate ->
# tools/batch_io.py import -> fix rounds), repeated in waves until the queue of the groups is empty or max_waves.
# args: {groups:"conv:companions", workers:6, batchesPer:3, model:"flash", tag:"cA", fix:false, tablePrefix:"",
#        maxWaves:4, root:"D:/Xbox/avowed-ukr"}
import json
import subprocess

A = args
ROOT = A.get("root", "D:/Xbox/avowed-ukr")
model = A.get("model", "flash")
per = A.get("batchesPer", 3)
fix = A.get("fix", False)
flags = (" --fix" if fix else "") + (f" --table-prefix {A['tablePrefix']}" if A.get("tablePrefix") else "") + \
        (f" --max-attempts {A['maxAttempts']}" if A.get("maxAttempts") else "")
SCHEMA = {"type": "object", "properties": {"worker": {"type": "string"}, "batches": {"type": "number"},
          "rows": {"type": "number"}, "ok": {"type": "number"}, "needs_fix": {"type": "number"},
          "note": {"type": "string"}}, "required": ["worker", "batches", "ok"]}


def prompt(w):
    fixtxt = ("This is a FIX batch: rows also carry the previous attempt uk/uf/ux and \"note\" = why it was rejected or "
              "flagged - correct exactly that problem and keep what was right. ") if fix else ""
    return f"""Avowed EN->UK translator {w}. You are a professional English->Ukrainian game localizer (RPG Avowed, world of Eora). Worker id: {w}. Repo root: {ROOT} (use absolute paths). You work through a DB-backed queue; never read or write the database any other way, never print batch contents to the terminal.

First read {ROOT}/docs/style_uk.md - the binding style guide (sections 4 and 8: gender variants, dialogue / quests / chatter / codex rules; section 9: rules learned from QA).

Then repeat the following cycle up to {per} times:
1. Claim: run  python {ROOT}/tools/batch_worker.py claim --worker {w} --groups {A['groups']}{flags}   -> prints ONE json line. If "batch" is null the queue is empty: stop and answer. Otherwise it gives "file" (batch to read), "out" (answer file to write) and the batch id.
2. Read the batch file (JSON, one row per line; read all of it, in chunks if needed). Fields: "glossary" = mandatory Ukrainian equivalents [en, uk, uk_female, uk_extra, note] (decline them, keep the stem); "voices" = speaker profiles for these scenes - keep each character's voice and the ти/ви forms consistent; "ctx" = READ-ONLY lines that precede the first row of each table ({{table index: [[en, uk-if-already-translated], ...]}}) for continuity only, never translate them; "rows" = what you translate: id, en (source - if empty use pl), en_f / en_x (female / neutral English variants), pl / pl_f / pl_x (Polish: use only to resolve meaning and gender), ru (hint only, never copy its wording), need ("F" = uf required, "X" = ux required), t (table index when the batch has several tables). Rows are in dialogue order within a table. {fixtxt}
3. Translate EVERY row. Write the "out" file with your file-writing tool (UTF-8, one JSON object per line, valid JSON, no shell redirection): {{"id":<id>,"uk":"...","uf":"...","ux":"..."}} - "uf" only when need contains F, "ux" only when need contains X (FIX batches: include uf/ux whenever the previous attempt had them). For proper names, codes or sounds that must stay unchanged answer {{"id":<id>,"keep":1}}. Escape " as \\" and line breaks as \\n (\\r\\n when the source uses \\r\\n). Never change tags (<i>, </>, <img .../>, <imginputbind .../> ...), {{variables}} or ICU |plural(...) constructs - translate only the text around/inside them. Female / neutral variants concern ONLY the player character (the Envoy / Посланець / Посланниця): see style guide sections 4 and 8. Natural spoken Ukrainian for dialogue, no russisms, no calques from English/Polish, apostrophe is ASCII '.
4. Import: python {ROOT}/tools/batch_io.py import <the out file>. It prints "FIX <id>: <reason>" for rejected rows (already stored as needs_fix). Put ONLY the corrected rows into <out file without .jsonl>_fix1.jsonl (then _fix2, _fix3; keep the batch_<id> prefix of the name) and import again. At most 3 rounds.
5. Release leftovers: python {ROOT}/tools/batch_worker.py done --batch <batch id>.
Then start the next cycle.

Finish with the JSON summary: worker, batches (cycles done), rows (rows claimed), ok (rows accepted), needs_fix (rows still rejected), note (<=1 line, mention any tool error)."""


def reap_worker(w):
    # import whatever a crashed worker wrote, then return its claimed rows to the queue
    r = subprocess.run(["python", f"{ROOT}/tools/batch_worker.py", "reap", "--release", "--worker", w],
                       capture_output=True, text=True, encoding="utf-8")
    return (r.stdout or r.stderr).strip()[:200]


async def run(w, delay=0):
    import asyncio
    await asyncio.sleep(delay)                      # staggered start: avoids a burst of parallel model calls (429)
    last = ""
    for attempt in range(A.get("retries", 4)):
        try:
            return await agent(prompt(w), schema=SCHEMA, role=f"Translator {w}", model=model)
        except Exception as e:  # noqa: BLE001
            last = str(e)[:200]
            log(f"{w}: attempt {attempt + 1} failed: {last[:120]} | {reap_worker(w)}")
            if "429" in last or "RESOURCE_EXHAUSTED" in last or "UNAVAILABLE" in last:
                await asyncio.sleep(90 * (attempt + 1))
                continue
            break
    return {"worker": w, "batches": 0, "ok": 0, "error": last}


def left():
    out = subprocess.run(["python", f"{ROOT}/tools/batch_worker.py", "plan"], capture_output=True, text=True,
                         encoding="utf-8").stdout
    n = 0
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] in A["groups"].split(","):
            n += int(parts[3]) if fix else int(parts[1])
    return n


tot = {"batches": 0, "rows": 0, "ok": 0, "needs_fix": 0}
notes = []
for wave in range(A.get("maxWaves", 4)):
    rem = left()
    log(f"wave {wave + 1}: {rem} rows left in {A['groups']}")
    if rem == 0:
        break
    phase(f"Wave {wave + 1}")
    nw = max(1, min(A.get("workers", 6), -(-rem // 150)))
    ws = [f"{A.get('tag', 'w')}{wave + 1}{i + 1:02d}" for i in range(nw)]
    res = await parallel(list(enumerate(ws)), lambda iw: run(iw[1], iw[0] * A.get("stagger", 40)))
    for r in res:
        for k in tot:
            tot[k] += int(r.get(k) or 0)
        if r.get("error") or r.get("note"):
            notes.append(f"{r['worker']}: {r.get('error') or r.get('note')}"[:160])
    log(f"wave {wave + 1} done: " + json.dumps({k: tot[k] for k in tot}))
print(json.dumps({"groups": A["groups"], "left": left(), **tot, "notes": notes[:10]}, ensure_ascii=False))
