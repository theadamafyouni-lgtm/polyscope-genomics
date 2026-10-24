// The routine questions, answered on the upload form before the run starts, so that nobody waits
// for a first question. Only the condition and the agreement are required; demo_site/intake.py
// checks the same rules on the server, which is the one that decides.
import { escapeHTML } from './html.js';
const optional = '<span class="intake-optional">Optional</span>';
const SEX = [['female', 'Female'], ['male', 'Male'], ['', 'Prefer not to say']];
const DIAGNOSED = [['no', 'No'], ['yes', 'Yes'], ['not-sure', 'Not sure']];
const TEXT = [['intake-condition', 'condition'], ['intake-reason', 'reason'], ['intake-ancestry', 'ancestry']];

const option = (name, [value, label]) => `<label class="intake-option"><input type="radio" name="${name}" value="${value}"><span>${escapeHTML(label)}</span></label>`;

/** The questions, inside the upload form and before the file. */
export function intakeView(disabled = false) {
  return `<fieldset class="intake-questions"${disabled ? ' disabled' : ''}><legend>Your question</legend>`
    + '<div class="intake-field"><label for="intake-condition">Which condition or trait would you like looked at?</label><input id="intake-condition" name="condition" type="text" maxlength="200" required autocomplete="off" aria-describedby="intake-condition-help"><p id="intake-condition-help" class="field-help">For example: type 2 diabetes, high blood pressure, an underactive thyroid.</p></div>'
    + `<div class="intake-field"><label for="intake-reason">What made you want to look into it? ${optional}</label><textarea id="intake-reason" name="reason" maxlength="600" rows="3"></textarea></div>`
    + `<div class="intake-field"><label for="intake-ancestry">How would you describe your family’s ancestry? ${optional}</label><input id="intake-ancestry" name="ancestry" type="text" maxlength="300" autocomplete="off" aria-describedby="intake-ancestry-help"><p id="intake-ancestry-help" class="field-help">For example, where your grandparents’ families came from. The calculation uses your DNA; this lets us tell you if your DNA shows something different.</p></div>`
    + `<fieldset class="intake-choice"><legend>Sex ${optional}</legend><div class="intake-options">${SEX.map(choice => option('intake-sex', choice)).join('')}</div><p class="field-help">Used only for a condition tied to one sex.</p></fieldset>`
    + `<fieldset class="intake-choice"><legend>Has a doctor diagnosed you with it, or are you treated for it? ${optional}</legend><div class="intake-options">${DIAGNOSED.map(choice => option('intake-diagnosed', choice)).join('')}</div></fieldset>`
    + '<label class="intake-check"><input type="checkbox" id="intake-doctor-note" name="doctor_note"> <span>Add a short note I can take to my doctor</span></label>'
    + '</fieldset>';
}

/** The agreement, after the file and before the button. */
export function consentView(disabled = false) {
  return `<label class="intake-check intake-consent"><input type="checkbox" id="intake-consent" name="consent" required${disabled ? ' disabled' : ''}> <span>I agree to have my file analysed for this condition: a scoring method is picked for me, my score is compared with people whose DNA is like mine, and I’m shown the result. It is a research tool, not a diagnosis or medical advice.</span></label>`;
}

/** What the person has answered so far: kept across the page's redraws, sent with the file. */
export function createIntakeDraft() {
  const blank = () => ({ condition: '', reason: '', ancestry: '', sex: null, diagnosed: null, doctor_note: false, consent: false });
  let draft = blank();
  function update(target) {
    const text = TEXT.find(([id]) => id === target?.id);
    if (text) draft[text[1]] = String(target.value);
    else if (target?.name === 'intake-sex') draft.sex = String(target.value);
    else if (target?.name === 'intake-diagnosed') draft.diagnosed = String(target.value);
    else if (target?.id === 'intake-doctor-note') draft.doctor_note = !!target.checked;
    else if (target?.id === 'intake-consent') draft.consent = !!target.checked;
    else return false;
    return true;
  }
  function restore(root) {
    for (const [id, key] of TEXT) {
      const field = root.querySelector(`#${id}`);
      if (field && field.value !== draft[key]) field.value = draft[key];
    }
    for (const radio of root.querySelectorAll('input[name="intake-sex"]')) radio.checked = radio.value === draft.sex;
    for (const radio of root.querySelectorAll('input[name="intake-diagnosed"]')) radio.checked = radio.value === draft.diagnosed;
    const note = root.querySelector('#intake-doctor-note');
    if (note) note.checked = draft.doctor_note;
    const consent = root.querySelector('#intake-consent');
    if (consent) consent.checked = draft.consent;
  }
  function payload() {
    const answers = { condition: draft.condition.trim() };
    for (const key of ['reason', 'ancestry']) if (draft[key].trim()) answers[key] = draft[key].trim();
    if (draft.sex === 'female' || draft.sex === 'male') answers.sex = draft.sex;
    if (DIAGNOSED.some(([value]) => value === draft.diagnosed)) answers.diagnosed = draft.diagnosed;
    if (draft.doctor_note) answers.doctor_note = true;
    answers.consent = draft.consent;
    return answers;
  }
  /** The first required answer missing, in the order the form asks, or null. */
  function problem() {
    if (!draft.condition.trim()) return { field: 'intake-condition', message: 'Say which condition or trait you would like looked at.' };
    if (!draft.consent) return { field: 'intake-consent', message: 'Tick the box to agree before starting.' };
    return null;
  }
  return { update, restore, payload, problem, reset: () => { draft = blank(); } };
}

/** The answers as the upload's X-PRS-Intake header: UTF-8 JSON in base64. */
export function encodeIntake(answers) {
  const bytes = new TextEncoder().encode(JSON.stringify(answers));
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
