/** Local question notes only. Text is never interpreted or sent to an agent. */
export const MAX_QUESTIONS = 30;
export const MAX_LENGTH = 2000;

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Keep the person's wording and line breaks; remove only outer whitespace. */
export function normalizeQuestion(value) {
  if (typeof value !== 'string') return '';
  const trimmed = value.trim();
  return trimmed.length <= MAX_LENGTH ? trimmed : '';
}

const comparisonKey = value => normalizeQuestion(value).replace(/\s+/gu, ' ').toLowerCase();

/**
 * Explicitly imported notebook format. Reject an invalid notebook as a whole,
 * so importing never silently turns incomplete notes into a successful restore.
 */
export function readNotebook(value) {
  if (!object(value) || value.schema_version !== 1 || !Array.isArray(value.questions)
    || value.questions.length > MAX_QUESTIONS) return [];
  const questions = [];
  const seen = new Set();
  for (const valueQuestion of value.questions) {
    const question = normalizeQuestion(valueQuestion);
    if (!question) return [];
    const key = comparisonKey(question);
    if (!seen.has(key)) {
      seen.add(key);
      questions.push(question);
    }
  }
  return questions;
}

/**
 * Update an in-memory list without mutating it. Imported lists should first
 * pass through readNotebook. Rejected updates leave all existing notes intact.
 */
export function updateQuestion(questions, text, index = null) {
  const next = Array.isArray(questions) ? questions.slice() : [];
  const result = status => ({ questions: next, status });
  if (index !== null && (!Number.isInteger(index) || index < 0 || index >= next.length)) {
    return result('invalid-index');
  }
  if (typeof text !== 'string' || !text.trim()) return result('empty');
  const question = text.trim();
  if (question.length > MAX_LENGTH) return result('too-long');
  const key = comparisonKey(question);
  if (next.some((existing, existingIndex) => existingIndex !== index && comparisonKey(existing) === key)) {
    return result('duplicate');
  }
  if (index === null) {
    if (next.length >= MAX_QUESTIONS) return result('full');
    next.push(question);
    return result('added');
  }
  next[index] = question;
  return result('updated');
}
