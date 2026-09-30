import assert from 'node:assert/strict';

const modal = { hidden: true };
const title = { textContent: '' };
const body = { innerHTML: '' };
const nodes = { modal, 'modal-title': title, 'modal-body': body };

globalThis.document = { getElementById: (id) => nodes[id] };
globalThis.location = { search: '' };
globalThis.localStorage = { getItem: () => null };
globalThis.fetch = async () => ({
  status: 200,
  ok: true,
  text: async () => JSON.stringify({
    total: 51,
    profile: 'uhd-remux',
    candidates: Array.from({ length: 51 }, (_, index) => ({
      name: `Release ${index + 1}`,
      accepted: true,
      score: 1000,
      rejections: [],
      breakdown: {},
      size_bytes: 1000,
      seeders: 5,
      indexer: 'Test',
    })),
  }),
});

const { default: library } = await import('../src/conduit/static/assets/js/views/library.js');
await library.onAction('preview', { dataset: { id: '1', title: 'Silo' } }, {});

assert.equal(modal.hidden, false);
assert.match(body.innerHTML, /Showing 1–50 of 51/);
assert.match(body.innerHTML, /Release 50/);
assert.doesNotMatch(body.innerHTML, /Release 51/);

await library.onAction('preview-page', { dataset: { page: '1' } }, {});
assert.match(body.innerHTML, /Showing 51–51 of 51/);
assert.match(body.innerHTML, /Release 51/);
