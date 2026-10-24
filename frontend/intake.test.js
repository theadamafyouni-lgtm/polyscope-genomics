import test from 'node:test';
import assert from 'node:assert/strict';
import { createIntakeDraft, encodeIntake, intakeView, consentView } from './intake.js';

// A stand-in for the rendered form: the fields by id, the radios by name.
function form() {
  const field = (id, extra = {}) => ({ id, value: '', checked: false, ...extra });
  const fields = ['intake-condition', 'intake-reason', 'intake-ancestry', 'intake-doctor-note', 'intake-consent'].map(id => field(id));
  const radios = [['intake-sex', ['female', 'male', '']], ['intake-diagnosed', ['no', 'yes', 'not-sure']]]
    .flatMap(([name, values]) => values.map(value => field('', { name, value })));
  return { fields, radios, querySelector: selector => fields.find(f => `#${f.id}` === selector) || null,
    querySelectorAll: selector => radios.filter(r => selector === `input[name="${r.name}"]`) };
}

test('the upload form asks the routine questions, only the condition and the agreement required', () => {
  const html = intakeView();
  assert.match(html, /<label for="intake-condition">Which condition or trait would you like looked at\?<\/label><input id="intake-condition" name="condition" type="text" maxlength="200" required/);
  for (const [id, limit] of [['intake-reason', 600], ['intake-ancestry', 300]]) {
    assert.match(html, new RegExp(`id="${id}"[^>]*maxlength="${limit}"`));
    assert.doesNotMatch(html, new RegExp(`id="${id}"[^>]*required`));
  }
  assert.match(html, /What made you want to look into it\? <span class="intake-optional">Optional<\/span>/);
  assert.match(html, /How would you describe your family’s ancestry\? <span class="intake-optional">Optional<\/span>/);
  assert.match(html, /<legend>Sex <span class="intake-optional">Optional<\/span><\/legend>/);
  for (const value of ['female', 'male', '']) assert.match(html, new RegExp(`type="radio" name="intake-sex" value="${value}"`));
  assert.match(html, /Prefer not to say/);
  assert.match(html, /<legend>Has a doctor diagnosed you with it, or are you treated for it\? <span class="intake-optional">Optional<\/span><\/legend>/);
  for (const value of ['no', 'yes', 'not-sure']) assert.match(html, new RegExp(`type="radio" name="intake-diagnosed" value="${value}"`));
  assert.match(html, /type="checkbox" id="intake-doctor-note"[^>]*>\s*<span>Add a short note I can take to my doctor<\/span>/);
  assert.doesNotMatch(html, /required[^>]*name="intake-(sex|diagnosed)"|name="intake-(sex|diagnosed)"[^>]*required/);
  const consent = consentView();
  assert.match(consent, /type="checkbox" id="intake-consent" name="consent" required/);
  assert.match(consent, /research tool, not a diagnosis or medical advice/);
  assert.match(intakeView(true), /<fieldset class="intake-questions" disabled>/);
  assert.match(consentView(true), /id="intake-consent"[^>]* disabled/);
});

test('the answers survive a redraw and go as the person gave them, blanks left out', () => {
  const draft = createIntakeDraft();
  const first = form();
  const set = (id, value) => { const f = first.querySelector(`#${id}`); f.value = value; assert.ok(draft.update(f)); };
  assert.deepEqual(draft.problem(), { field: 'intake-condition', message: 'Say which condition or trait you would like looked at.' });
  set('intake-condition', '  Condition X ');
  set('intake-reason', '   ');
  set('intake-ancestry', ' Mixed heritage ');
  assert.ok(draft.update(first.radios.find(r => r.name === 'intake-sex' && r.value === 'female')));
  assert.ok(draft.update(first.radios.find(r => r.name === 'intake-diagnosed' && r.value === 'no')));
  assert.ok(draft.update({ id: 'intake-doctor-note', checked: true }));
  assert.equal(draft.update({ id: 'genome-file', value: 'x' }), false);
  assert.deepEqual(draft.problem(), { field: 'intake-consent', message: 'Tick the box to agree before starting.' });
  assert.ok(draft.update({ id: 'intake-consent', checked: true }));
  assert.equal(draft.problem(), null);
  assert.deepEqual(draft.payload(), { condition: 'Condition X', ancestry: 'Mixed heritage', sex: 'female',
    diagnosed: 'no', doctor_note: true, consent: true });
  // Redrawn: every answer back where the person left it.
  const again = form();
  draft.restore(again);
  assert.equal(again.querySelector('#intake-condition').value, '  Condition X ');
  assert.equal(again.querySelector('#intake-ancestry').value, ' Mixed heritage ');
  assert.deepEqual(again.radios.filter(r => r.checked).map(r => `${r.name}=${r.value}`), ['intake-sex=female', 'intake-diagnosed=no']);
  assert.ok(again.querySelector('#intake-doctor-note').checked && again.querySelector('#intake-consent').checked);
  // Prefer not to say stays chosen, and says nothing.
  assert.ok(draft.update(again.radios.find(r => r.name === 'intake-sex' && r.value === '')));
  assert.equal('sex' in draft.payload(), false);
  const third = form();
  draft.restore(third);
  assert.deepEqual(third.radios.filter(r => r.checked).map(r => `${r.name}=${r.value}`), ['intake-sex=', 'intake-diagnosed=no']);
  draft.reset();
  assert.deepEqual(draft.payload(), { condition: '', consent: false });
});

test('the answers go in one header, as UTF-8 JSON in base64', () => {
  const answers = { condition: 'Hautveränderung X', ancestry: '家族のルーツはさまざま', consent: true };
  assert.deepEqual(JSON.parse(Buffer.from(encodeIntake(answers), 'base64').toString('utf8')), answers);
  assert.match(encodeIntake(answers), /^[A-Za-z0-9+/]+=*$/);
});
