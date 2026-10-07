const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app/static/operational_queue.js', 'utf8');
const eventStart = source.indexOf("  document.addEventListener('click'");
assert(eventStart > 0);
const requests = [];
const context = {
  localStorage: { getItem: () => 'test-token' },
  document: { getElementById: () => null },
  confirm: () => true, alert: () => {},
  fetch: async (url, options) => {
    requests.push({url, options});
    return { ok: true, status: 200, json: async () => ({message: 'OK'}) };
  },
};
vm.createContext(context);
vm.runInContext(source.slice(0, eventStart) + '\n loadQueue = async () => {}; openProject = async () => {}; globalThis.ui = {artifactChecklist, projectActions, reviewAsset, reviewProject};\n})();', context);
const task = {
  id: 'task-1', video_url: '/video.mp4', can_review_assets: true, can_approve_anyway: true,
  artifact_checklist: { items: [{key: 'narration', label: 'Narração', status: 'partial', duration_sec: 97, target_sec: 60}] },
};
let html = context.ui.artifactChecklist(task);
assert.match(html, /data-asset="narration" data-action="verify"/);
assert.match(html, /data-asset="narration" data-action="approve"/);
assert.match(html, /1:37 \/ meta 1:00/);
assert.match(context.ui.projectActions(task), /Aprovar vídeo mesmo assim/);
task.can_review_assets = false;
assert.doesNotMatch(context.ui.artifactChecklist(task), /class="btn btn-ghost oq-asset"/);
task.artifact_checklist.items[0] = {key: 'narration', status: 'approved', manual_approval: true, summary: 'Aprovado manualmente'};
html = context.ui.artifactChecklist(task);
assert.match(html, /Aprovado por você/);
assert.match(html, /Aprovado manualmente/);
assert.doesNotMatch(html, /Verificar/);
(async () => {
  await context.ui.reviewAsset('task-1', 'narration', 'verify', {textContent: 'Verificar'});
  assert.equal(requests[0].url, '/youtube/cinematic/queue/task-1/assets/narration');
  assert.equal(JSON.parse(requests[0].options.body).action, 'verify');
  await context.ui.reviewProject('approve-anyway', 'task-1', {textContent: 'Aprovar'});
  assert.equal(requests[1].url, '/youtube/cinematic/queue/task-1/approve');
  assert.equal(JSON.parse(requests[1].options.body).approve_anyway, true);
  assert.equal(requests.length, 2);
  console.log('Asset review UI: actionable warnings, approval labels, scoped requests and manual approval passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
