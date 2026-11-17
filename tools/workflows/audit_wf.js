export const meta = {
  name: 'audit-qa',
  description: 'Audit workers: (mode fix) queue workers repair needs_fix rows; (mode review) editors review QA sample files. All writes go through tools/batch_io.py.',
  phases: [{ title: 'Work', detail: 'parallel agents following tools/workflows/agent_worker.md / agent_qa.md' }],
}
// args: { root, mode: "fix" | "review", model: "sonnet",
//         fix:    { groups: "g1,g2", workers: 4, cycles: 2, maxRows: 120, maxChars: 14000, tag: "af" }
//         review: { dir: "a1", names: ["Q01","Q02"] }   files: <root>/logs/qa/<dir>/in/<name>.jsonl -> out/<name>.jsonl }
const A = args
const R = A.root
const model = A.model || 'sonnet'
const WSCHEMA = { type: 'object', properties: { worker: { type: 'string' }, batches: { type: 'number' }, rows: { type: 'number' }, ok: { type: 'number' }, needs_fix: { type: 'number' }, note: { type: 'string' } }, required: ['worker', 'ok'] }
const QSCHEMA = { type: 'object', properties: { q: { type: 'string' }, reviewed: { type: 'number' }, ok: { type: 'number' }, fixed: { type: 'number' }, unfixed: { type: 'number' }, critical: { type: 'number' }, semantic: { type: 'number' }, stylistic: { type: 'number' }, technical: { type: 'number' }, issues: { type: 'string' } }, required: ['q', 'reviewed', 'ok'] }

phase('Work')
let res
if (A.mode === 'fix') {
  const F = A.fix
  const flags = `--fix --max-rows ${F.maxRows || 120} --max-chars ${F.maxChars || 14000} --max-attempts 12`
  const ws = Array.from({ length: F.workers }, (_, i) => `${F.tag || 'af'}${String(i + 1).padStart(2, '0')}`)
  res = await parallel(ws.map(w => () => agent(
    `You are a translator worker of the Avowed localization queue. Read ${R}/tools/workflows/agent_worker.md and follow it exactly.
WORKER = ${w}
GROUPS = ${F.groups}
CYCLES = ${F.cycles || 1}
FLAGS = ${flags}
R = ${R}   (use this absolute root for every path; never the base checkout D:/Xbox/avowed-ukr)
These are FIX batches of rows flagged by the automatic linter / QA: the row's "note" says exactly what is wrong (e.g. "lint:EXTRA_ENVOY_SINGLE(uk_extra): ...", "GLOSSARY_NAMED: Dreamthrall -> Сновидний Невільник", "RUSISM: Призиває", "ARQUEBUS_GENDER"). Fix exactly that problem in every variant of the row (uk, uf, ux), keep everything that was right, and follow docs/style_uk.md sections 4, 8, 9 and the glossary subset of the batch. If a flag is a false positive (the existing text is already correct), return the SAME text unchanged. In "uk_extra" the Envoy is addressed/named with both forms (Посланець/Посланниця, vocative Посланцю/Посланнице, correct case endings).
Answer ONLY with the JSON summary (worker, batches, rows, ok, needs_fix, note).`,
    { label: w, phase: 'Work', model, schema: WSCHEMA })))
} else {
  const V = A.review
  res = await parallel(V.names.map(q => () => agent(
    `You are a senior QA editor. Read ${R}/tools/workflows/agent_qa.md and follow it exactly, with these overrides:
R = ${R}   (use this absolute root for every path; never the base checkout D:/Xbox/avowed-ukr)
Q = ${q}
The sample file of THIS wave is ${R}/logs/qa/${V.dir}/in/${q}.jsonl and your answer file is ${R}/logs/qa/${V.dir}/out/${q}.jsonl (not logs/qa/in|out); review command: python ${R}/tools/batch_io.py review ${R}/logs/qa/${V.dir}/out/${q}.jsonl
${V.focus ? 'FOCUS OF THIS WAVE: ' + V.focus : ''}
Answer ONLY with the JSON summary (q, reviewed, ok, fixed, unfixed, critical, semantic, stylistic, technical, issues = the up to 3 most frequent recurring problems, one line).`,
    { label: 'qa:' + V.dir + ':' + q, phase: 'Work', model, schema: QSCHEMA })))
}
const done = res.filter(Boolean)
if (A.mode === 'fix') {
  return { mode: 'fix', agents: res.length, returned: done.length, ok: done.reduce((s, r) => s + (r.ok || 0), 0), needs_fix: done.reduce((s, r) => s + (r.needs_fix || 0), 0), notes: done.filter(r => r.note).map(r => r.worker + ': ' + r.note).slice(0, 8) }
}
return {
  mode: 'review', files: res.length, returned: done.length,
  reviewed: done.reduce((s, r) => s + (r.reviewed || 0), 0), ok: done.reduce((s, r) => s + (r.ok || 0), 0),
  fixed: done.reduce((s, r) => s + (r.fixed || 0), 0), unfixed: done.reduce((s, r) => s + (r.unfixed || 0), 0),
  critical: done.reduce((s, r) => s + (r.critical || 0), 0), semantic: done.reduce((s, r) => s + (r.semantic || 0), 0),
  stylistic: done.reduce((s, r) => s + (r.stylistic || 0), 0), technical: done.reduce((s, r) => s + (r.technical || 0), 0),
  issues: done.filter(r => r.issues).map(r => r.q + ': ' + r.issues),
}
