import test from 'node:test';
import assert from 'node:assert/strict';
import { createQuestionAnswer, questionView } from './question.js';
import { createDemoClient } from './demo-app.js';

const question = { id: 'example-question', text: 'May I go ahead with this model?',
  why_it_matters: 'It decides which score you get and how well it fits you.',
  options: ['Yes, go ahead', 'No, stop here'], other: true };

test('choices use accessible native radios with exact escaped labels and an optional own-words answer', () => {
  const html = questionView({ ...question, options: ['Yes & continue', '<No>'] });
  assert.match(html, /<fieldset class="question-choices"><legend>Choose an answer<\/legend>/);
  assert.equal((html.match(/type="radio"/g) || []).length, 3);
  assert.match(html, /Yes &amp; continue/);
  assert.match(html, /&lt;No&gt;/);
  assert.match(html, /id="answer-choice-other" value="other" required/);
  assert.match(html, /<label for="answer">Your own words<\/label>/);
  assert.match(html, /id="answer-submit"[^>]+disabled/);
  assert.doesNotMatch(questionView({ ...question, other: false }), /textarea|answer-choice-other/);
  const free = questionView({ id: 'free', text: 'Tell us in your own words.' });
  assert.doesNotMatch(free, /type="radio"/);
  assert.match(free, /<textarea[^>]+ required/);
  assert.match(questionView(question, true), /<fieldset class="question-choices" disabled>/);
  assert.notEqual(questionView(question), questionView({ ...question, id: 'a-new-question-with-the-same-words' }),
    'a new id triggers redraw even when its question and choices are identical');
});

test('options send their exact original text and cannot collide with the Other control', () => {
  const answer = createQuestionAnswer();
  answer.sync({ ...question, options: ['  Yes, go ahead  ', 'other', '__other__'] });
  assert.equal(answer.payload(), null);
  answer.choose('0');
  assert.deepEqual(answer.payload(), { question_id: question.id, text: '  Yes, go ahead  ' });
  answer.choose('1');
  assert.equal(answer.payload().text, 'other');
  answer.choose('2');
  assert.equal(answer.payload().text, '__other__');
  answer.choose('other');
  assert.equal(answer.payload(), null);
  answer.type('My own answer');
  assert.equal(answer.payload().text, 'My own answer');
});

test('options-only questions reject typed, absent and invalid selections', () => {
  const answer = createQuestionAnswer();
  answer.sync({ ...question, other: false });
  answer.type('An answer the question did not offer.');
  assert.equal(answer.payload(), null);
  for (const invalid of ['other', '-1', '2', '0.5', '', null, undefined]) {
    answer.choose(invalid);
    assert.equal(answer.payload(), null);
  }
  answer.choose('1');
  assert.equal(answer.payload().text, question.options[1]);
});

test('Other text and choices survive redraws, but new questions erase the previous answer', () => {
  const answer = createQuestionAnswer();
  answer.sync(question);
  answer.type('  Here is my answer.  ');
  answer.sync(structuredClone(question));
  assert.equal(answer.payload().text, 'Here is my answer.');
  answer.choose('1');
  answer.sync(structuredClone(question));
  assert.equal(answer.payload().text, 'No, stop here');
  answer.choose('other');
  assert.equal(answer.payload().text, 'Here is my answer.');
  answer.sync({ ...question, id: 'next-question' });
  assert.equal(answer.payload(), null);
  answer.choose('other');
  assert.equal(answer.payload(), null);
  answer.type('A different answer');
  answer.sync(null);
  answer.sync(question);
  assert.equal(answer.payload(), null);
});

test('changed options cannot silently submit an answer that is no longer offered', () => {
  const answer = createQuestionAnswer();
  answer.sync(question);
  answer.choose('0');
  answer.sync({ ...question, options: [...question.options].reverse() });
  assert.equal(answer.payload().text, question.options[0]);
  answer.sync({ ...question, options: ['Different first answer', 'Different second answer'] });
  assert.equal(answer.payload(), null);
  answer.type('Own words');
  answer.sync({ ...question, other: false });
  assert.equal(answer.payload(), null);
});

test('restore recovers the selected control, draft, conditional requirement and button state', () => {
  const answer = createQuestionAnswer();
  const radios = [{ value: '0' }, { value: '1' }, { value: 'other' }];
  const textarea = { value: '' }, button = { disabled: true };
  const root = {
    querySelectorAll: () => radios,
    querySelector: selector => selector === '#answer' ? textarea : button,
  };
  answer.sync(question);
  answer.type('My answer');
  answer.restore(root);
  assert.deepEqual(radios.map(radio => radio.checked), [false, false, true]);
  assert.deepEqual(textarea, { value: 'My answer', required: true });
  assert.equal(button.disabled, false);
  answer.choose('0');
  answer.restore(root, true);
  assert.deepEqual(radios.map(radio => radio.checked), [true, false, false]);
  assert.equal(textarea.required, false);
  assert.equal(button.disabled, true);
  answer.sync({ id: 'free', text: 'Tell us more.' });
  answer.restore(root);
  assert.equal(textarea.value, '');
  assert.equal(textarea.required, true);
  assert.equal(button.disabled, true);
  answer.type('A free answer');
  assert.deepEqual(answer.payload(), { question_id: 'free', text: 'A free answer' });
  answer.type('x'.repeat(8001));
  assert.equal(answer.payload(), null);
});

test('a selected choice is sent once with the question id and exact text; a failed send preserves it', async () => {
  const calls = [];
  let finish;
  const response = (body, status = 200) => ({ ok: status === 200, status, json: async () => body, headers: new Headers() });
  const client = createDemoClient({
    now: () => 1000, setTimer: () => 0, clearTimer: () => {},
    fetchImpl: async (path, options) => {
      if (path === '/api/session') return response({ authenticated: true, csrf: 'example-token', expires_at: 3600 });
      if (path === '/api/run') return response({ status: 'waiting', owned: true, question });
      calls.push({ path, options });
      return new Promise(resolve => { finish = resolve; });
    },
  });
  const answer = createQuestionAnswer();
  await client.start();
  answer.sync(client.state.run.question);
  answer.choose('1');
  const payload = answer.payload();
  const pending = client.answer(payload.question_id, payload.text);
  assert.equal(await client.answer(payload.question_id, payload.text), false);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/answer');
  assert.deepEqual(JSON.parse(calls[0].options.body), { question_id: question.id, text: question.options[1] });
  finish(response({}, 500));
  assert.equal(await pending, false);
  answer.sync(client.state.run.question);
  assert.deepEqual(answer.payload(), payload);
  client.clear();
});
