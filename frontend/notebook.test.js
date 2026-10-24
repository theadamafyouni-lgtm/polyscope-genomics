import test from 'node:test';
import assert from 'node:assert/strict';
import { MAX_QUESTIONS, MAX_LENGTH, normalizeQuestion, readNotebook, updateQuestion } from './notebook.js';

test('normalization trims only outer whitespace and preserves wording and newlines', () => {
  assert.equal(normalizeQuestion('  First line?\n  Second line?\n '), 'First line?\n  Second line?');
  for (const value of ['', ' \n\t ', '\u00a0', null, undefined, 42, {}, [], { toString: () => 'Question' }]) {
    assert.equal(normalizeQuestion(value), '');
  }
  assert.equal(normalizeQuestion('a'.repeat(MAX_LENGTH)).length, MAX_LENGTH);
  assert.equal(normalizeQuestion(`  ${'a'.repeat(MAX_LENGTH)}  `).length, MAX_LENGTH);
  assert.equal(normalizeQuestion('a'.repeat(MAX_LENGTH + 1)), '');
});

test('explicit notebook import trims and deduplicates case and whitespace without changing saved wording', () => {
  const notebook = { schema_version: 1, questions: ['  Why this model? ', 'WHY\n  THIS MODEL?', 'A second\n  question?', 'a second question?'] };
  const snapshot = structuredClone(notebook);
  assert.deepEqual(readNotebook(notebook), ['Why this model?', 'A second\n  question?']);
  assert.deepEqual(notebook, snapshot);
});

test('malformed imports are rejected whole without string coercion or partial recovery', () => {
  const poison = { toString() { throw new Error('must not coerce'); }, valueOf() { throw new Error('must not coerce'); } };
  for (const value of [null, undefined, [], 'notes', {}, { schema_version: '1', questions: ['Question?'] },
    { schema_version: 2, questions: ['Question?'] }, { schema_version: 1, questions: 'Question?' },
    { schema_version: 1, questions: ['Valid?', null] }, { schema_version: 1, questions: ['Valid?', '  '] },
    { schema_version: 1, questions: ['Valid?', poison] }, { schema_version: poison, questions: ['Valid?'] },
    { schema_version: 1, questions: ['Valid?', 'x'.repeat(MAX_LENGTH + 1)] }]) {
    assert.deepEqual(readNotebook(value), []);
  }
});

test('notebook import accepts the boundary but does not silently truncate an oversized input', () => {
  const questions = Array.from({ length: MAX_QUESTIONS }, (_, index) => `Question ${index}?`);
  assert.deepEqual(readNotebook({ schema_version: 1, questions }), questions);
  assert.deepEqual(readNotebook({ schema_version: 1, questions: [...questions, 'Extra question?'] }), []);
  assert.deepEqual(readNotebook({ schema_version: 1, questions: Array(MAX_QUESTIONS + 1).fill('Repeated?') }), []);
  assert.deepEqual(readNotebook({ schema_version: 1, questions: [] }), []);
});

test('adding and editing questions is immutable and preserves internal newlines', () => {
  const questions = Object.freeze(['Original question?']);
  const added = updateQuestion(questions, ' New question?\n  With context. ');
  assert.deepEqual(added, { status: 'added', questions: ['Original question?', 'New question?\n  With context.'] });
  assert.deepEqual(questions, ['Original question?']);
  const updated = updateQuestion(questions, ' Revised\nquestion? ', 0);
  assert.deepEqual(updated, { status: 'updated', questions: ['Revised\nquestion?'] });
  assert.deepEqual(questions, ['Original question?']);
  assert.notEqual(updated.questions, questions);
});

test('duplicate checks ignore case and repeated whitespace while allowing edits of the same note', () => {
  const questions = ['Why this model?', 'What are the limits?'];
  assert.equal(updateQuestion(questions, '  WHY\n this\t model? ').status, 'duplicate');
  assert.equal(updateQuestion(questions, 'WHAT ARE\nTHE LIMITS?', 0).status, 'duplicate');
  assert.deepEqual(updateQuestion(questions, 'WHY\nTHIS MODEL?', 0), { status: 'updated', questions: ['WHY\nTHIS MODEL?', 'What are the limits?'] });
  assert.equal(updateQuestion(questions, 'Why this model?', 0).status, 'updated');
});

test('empty, overlong and duplicate edits leave the previous note untouched', () => {
  const questions = ['Original?', 'Another?'];
  const poison = { toString() { throw new Error('must not coerce'); } };
  for (const [value, status] of [[' \n ', 'empty'], [null, 'empty'], [poison, 'empty'], ['x'.repeat(MAX_LENGTH + 1), 'too-long'], ['another?', 'duplicate']]) {
    const result = updateQuestion(questions, value, 0);
    assert.equal(result.status, status);
    assert.deepEqual(result.questions, questions);
    assert.notEqual(result.questions, questions);
  }
  assert.equal(updateQuestion(questions, 'x'.repeat(MAX_LENGTH), 0).status, 'updated');
});

test('invalid edit indexes are rejected without adding or replacing a note', () => {
  const questions = ['Original?'];
  for (const index of [-1, 1, 0.5, '0', NaN, Infinity, {}, false]) {
    assert.deepEqual(updateQuestion(questions, 'Replacement?', index), { status: 'invalid-index', questions });
  }
  assert.deepEqual(updateQuestion([], 'Question?', 0), { status: 'invalid-index', questions: [] });
});

test('a full notebook still permits edits and recognizes duplicates before reporting its limit', () => {
  const questions = Array.from({ length: MAX_QUESTIONS }, (_, index) => `Question ${index}?`);
  assert.deepEqual(updateQuestion(questions, 'One more?'), { status: 'full', questions });
  assert.equal(updateQuestion(questions, 'QUESTION 0?').status, 'duplicate');
  const updated = updateQuestion(questions, 'Replacement?', MAX_QUESTIONS - 1);
  assert.equal(updated.status, 'updated');
  assert.equal(updated.questions.length, MAX_QUESTIONS);
  assert.equal(updated.questions.at(-1), 'Replacement?');
  assert.equal(updateQuestion(questions.slice(1), 'One more?').status, 'added');
});

test('markup and script-like note text stay plain strings for the rendering layer to escape', () => {
  const text = '<img src=x onerror="alert(1)">\n<script>doSomething()</script>';
  assert.equal(normalizeQuestion(text), text);
  assert.deepEqual(readNotebook({ schema_version: 1, questions: [text] }), [text]);
  assert.deepEqual(updateQuestion([], text), { status: 'added', questions: [text] });
});
