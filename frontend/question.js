import { escapeHTML } from './html.js';
const arrow = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>';

const questionOptions = question => Array.isArray(question?.options)
  ? question.options.filter(option => typeof option === 'string' && option.trim()) : [];

/** A draft belongs to one question. Option text, including its whitespace, is sent exactly. */
export function createQuestionAnswer() {
  let question = null, choice = null, draft = '';
  function reset() { question = null; choice = null; draft = ''; }
  function sync(next) {
    if (!next || next.id !== question?.id) { choice = null; draft = ''; }
    question = next || null;
    const options = questionOptions(question);
    if (choice?.kind === 'option' && !options.includes(choice.text)) choice = null;
    if (choice?.kind === 'other' && question?.other !== true) choice = null;
  }
  function choose(value) {
    const options = questionOptions(question);
    if (value === 'other') {
      choice = options.length && question?.other === true ? { kind: 'other' } : null;
    } else {
      const index = Number(value);
      choice = Number.isInteger(index) && String(index) === String(value) && options[index] !== undefined
        ? { kind: 'option', text: options[index] } : null;
    }
  }
  function type(value) {
    const options = questionOptions(question);
    if (options.length && question?.other !== true) return;
    draft = String(value);
    if (options.length) choice = { kind: 'other' };
  }
  function payload() {
    if (typeof question?.id !== 'string' || !question.id) return null;
    const options = questionOptions(question);
    if (options.length && choice?.kind === 'option' && options.includes(choice.text)) {
      return { question_id: question.id, text: choice.text };
    }
    if (options.length && (question.other !== true || choice?.kind !== 'other')) return null;
    const text = draft.trim();
    return text && text.length <= 8000 ? { question_id: question.id, text } : null;
  }
  function restore(root, disabled = false) {
    const options = questionOptions(question);
    const value = choice?.kind === 'other' ? 'other' : choice?.kind === 'option' ? String(options.indexOf(choice.text)) : null;
    for (const radio of root.querySelectorAll('input[name="answer-choice"]')) radio.checked = radio.value === value;
    const answer = root.querySelector('#answer');
    if (answer) {
      if (answer.value !== draft) answer.value = draft;
      answer.required = !options.length || choice?.kind === 'other';
    }
    const submit = root.querySelector('#answer-submit');
    if (submit) submit.disabled = disabled || !payload();
  }
  return { sync, choose, type, payload, restore, reset };
}

export function questionView(question, disabled = false) {
  const options = questionOptions(question);
  const off = disabled ? ' disabled' : '';
  const freeText = !options.length || question.other === true;
  const choice = (value, label) => `<label class="question-choice"><input class="question-choice-input" type="radio" name="answer-choice" id="answer-choice-${value}" value="${value}" required${off}><span class="question-choice-face"><span>${escapeHTML(label)}</span><span class="question-choice-mark" aria-hidden="true"></span></span></label>`;
  const choices = options.length ? `<fieldset class="question-choices"${off}><legend>Choose an answer</legend>${options.map((option, index) => choice(String(index), option)).join('')}${question.other === true ? choice('other', 'Something else') : ''}</fieldset>` : '';
  const answer = freeText ? `<div class="question-written-answer"><label for="answer">${options.length ? 'Your own words' : 'Your answer'}</label><textarea id="answer" name="answer" maxlength="8000" aria-describedby="answer-help"${options.length ? '' : ' required'}${off}></textarea></div>` : '';
  const help = options.length ? question.other === true ? 'Choose an answer, or write your own.' : 'Choose one answer to continue.' : 'Use your own words. We’ll use your answer to continue.';
  return `<section class="question-panel" aria-labelledby="live-question"><div class="question-copy"><p class="section-label">Your answer is needed</p><h1 id="live-question">${escapeHTML(question.text)}</h1>${question.why_it_matters ? `<p class="question-why">${escapeHTML(question.why_it_matters)}</p>` : ''}</div><form id="answer-form" data-question-id="${escapeHTML(question.id)}" aria-labelledby="live-question">${choices}${answer}<p id="answer-help" class="field-help">${help}</p><button id="answer-submit" type="submit" class="button" disabled>${disabled ? 'Sending…' : 'Send answer'} ${arrow}</button></form></section>`;
}
