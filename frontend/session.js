/**
 * Pure projection of the harness's append-only live-run events.
 *
 * The harness owns time, stage judgments and completion. This module never
 * advances a stage on a timer or interprets a lack of updates as a refusal.
 * `blocked` / `stage_blocked` are explicit adapter extensions: today's ledger
 * tailer does not publish the reference adapter's blocked entries.
 */
const STAGE_IDS = Object.freeze([
  'read-and-reason', 'ancestry', 'select', 'score', 'interpret', 'report',
]);

const normalizeStage = (stage) => stage === 'door' ? 'read-and-reason' : stage;
const eventId = (event) => event.event_id ?? event.id;
const hasId = (event) => eventId(event) !== undefined && eventId(event) !== null;
const resultStatus = (outcome) => outcome === 'proceed' || outcome === 'score' ? 'complete'
  : ['refuse', 'stop', 'ask', 'blocked', 'session-ended-by-person'].includes(outcome) ? outcome : 'unknown';
const hasSettledResult = (stage) => ['complete', 'refuse', 'stop', 'unknown', 'session-ended-by-person'].includes(stage.status)
  || ['ask', 'blocked'].includes(stage.status) && [...stage.history].reverse().find(event =>
    !['stage_started', 'stage_progress'].includes(event.kind))?.kind === 'stage_completed';
const ARTIFACT_KEYS = {
  'read-and-reason': 'classification', ancestry: 'ancestry', select: 'model_choice',
  score: 'score', interpret: 'interpret', report: 'report',
};

function createStage(id) {
  return { id, status: 'pending', outcome: null, attempt: 0, history: [] };
}

function startedAttempt(stage) {
  const boundary = [...stage.history].reverse().find((event) =>
    event.kind === 'stage_started' || event.kind === 'stage_completed');
  return boundary?.kind === 'stage_completed' ? stage.attempt + 1 : stage.attempt || 1;
}

export function createSessionState() {
  return {
    events: [],
    stages: STAGE_IDS.map(createStage),
    currentStage: null,
    status: 'idle',
    pendingQuestion: null,
    messages: [],
    lastEventAt: null,
  };
}

function finishStages(state, bundle = {}) {
  return {
    ...state,
    stages: state.stages.map((stage) => {
      const outcome = bundle[ARTIFACT_KEYS[stage.id]]?.outcome;
      if (typeof outcome === 'string') return { ...stage, outcome, status: resultStatus(outcome) };
      // Ending a run does not supply a missing judgment or imply success.
      return ['running', 'waiting-agent'].includes(stage.status)
        ? { ...stage, status: 'unknown' } : stage;
    }),
  };
}

/** Terminal artifacts retain judgments that may have fallen out of the progress window. */
export function projectRunSession(run = {}) {
  const events = run.events ?? run.progress;
  const state = (Array.isArray(events) ? events : []).reduce(applySessionEvent, createSessionState());
  return ['completed', 'failed'].includes(run.status)
    ? finishStages(state, run.report?.bundle ?? {}) : state;
}

function updateStage(state, event, update) {
  const id = normalizeStage(event.stage);
  if (!STAGE_IDS.includes(id)) return state;
  const known = state.stages.some((stage) => stage.id === id);
  const stages = known ? state.stages : [...state.stages, createStage(id)];
  return {
    ...state,
    currentStage: id,
    stages: stages.map((stage) => stage.id === id ? {
      ...stage,
      ...update(stage),
      history: [...stage.history, event],
    } : stage),
  };
}

function appendMessage(state, event, role, text) {
  return {
    ...state,
    messages: [...state.messages, {
      role,
      kind: event.kind,
      text: typeof text === 'string' ? text : '',
      questionId: event.question_id ?? null,
      ...(event.ts !== undefined ? { ts: event.ts } : {}),
      ...(event.why_it_matters !== undefined ? { whyItMatters: event.why_it_matters } : {}),
      ...(event.hard_blocker !== undefined ? { hardBlocker: event.hard_blocker } : {}),
      ...(event.options !== undefined ? { options: event.options } : {}),
      ...(event.skipped !== undefined ? { skipped: event.skipped } : {}),
    }],
  };
}

/**
 * Preserve file order, raw outcomes and raw events. `proceed` completes a stage;
 * it never establishes a scored result. A browser/adapter may attach event_id
 * for replay deduplication; timestamp alone is deliberately not an identifier.
 */
export function applySessionEvent(previous, event) {
  if (!event || typeof event !== 'object' || Array.isArray(event)
      || typeof event.kind !== 'string') return previous;
  if (hasId(event) && previous.events.some((seen) => hasId(seen)
      && eventId(seen) === eventId(event))) return previous;

  let state = {
    ...previous,
    events: [...previous.events, event],
    lastEventAt: event.ts ?? previous.lastEventAt,
  };

  switch (event.kind) {
    case 'run_started': {
      if (Array.isArray(event.stages)) {
        const planned = [...new Set(event.stages.map(normalizeStage))]
          .filter((id) => STAGE_IDS.includes(id));
        state.stages = planned.map((id) => previous.stages.find((stage) => stage.id === id)
          ?? createStage(id));
      }
      state.status = state.pendingQuestion ? 'ask' : 'running';
      break;
    }
    case 'stage_started':
      state = updateStage(state, event, (stage) => ({
        // The tailer also emits starts for bookkeeping after a judgment.
        // Only a new judgment or explicit question/block changes that result.
        ...(!hasSettledResult(stage) ? { status: 'running', outcome: null } : {}),
        attempt: startedAttempt(stage),
      }));
      state.status = state.pendingQuestion ? 'ask' : 'running';
      break;
    case 'stage_progress':
      state = updateStage(state, event, (stage) => ({
        ...(!hasSettledResult(stage) ? { status: state.pendingQuestion ? 'ask' : 'running' } : {}),
        attempt: stage.attempt || 1,
      }));
      state.status = state.pendingQuestion ? 'ask' : 'running';
      break;
    case 'stage_completed': {
      const outcome = event.outcome ?? null;
      const status = resultStatus(outcome);
      state = updateStage(state, event, (stage) => ({
        status, outcome, attempt: stage.attempt || 1,
      }));
      state.status = state.pendingQuestion ? 'ask'
        : ['refuse', 'stop', 'ask', 'blocked'].includes(outcome) ? outcome : 'running';
      break;
    }
    case 'question':
      state = updateStage(state, event, (stage) => ({
        status: 'ask', attempt: stage.attempt || 1,
      }));
      state = appendMessage(state, event, 'agent', event.question);
      state.pendingQuestion = event;
      state.status = 'ask';
      break;
    case 'answer':
      state = appendMessage(state, event, 'person', event.text);
      if (state.pendingQuestion && event.question_id === state.pendingQuestion.question_id) {
        const stage = state.pendingQuestion.stage;
        state = updateStage(state, { ...event, stage }, () => ({ status: 'waiting-agent' }));
        state.pendingQuestion = null;
        state.status = 'waiting-agent';
      }
      break;
    case 'blocked':
    case 'stage_blocked':
      state = updateStage(state, event, (stage) => ({
        status: 'blocked', attempt: stage.attempt || 1,
      }));
      state.status = 'blocked';
      break;
    case 'session_end':
      state.status = 'session-ended-by-person';
      state.pendingQuestion = null;
      break;
    case 'refusal': {
      // The current harness uses kind=refusal for stop too. Keep the distinction.
      const outcome = event.outcome === 'stop' ? 'stop' : 'refuse';
      state = updateStage(state, event, (stage) => ({
        status: outcome, outcome, attempt: stage.attempt || 1,
      }));
      state.status = outcome;
      state.pendingQuestion = null;
      break;
    }
    case 'run_completed':
      state = finishStages(state);
      state.status = ['score', 'refuse', 'stop', 'session-ended-by-person'].includes(event.outcome)
        ? event.outcome : 'unknown';
      state.pendingQuestion = null;
      break;
    case 'run_failed':
      state = finishStages(state);
      state.status = 'error';
      state.pendingQuestion = null;
      break;
    default:
      // An unknown event remains inspectable without inventing its meaning.
      break;
  }

  // A later harness termination must not recast a person's decision as a stop.
  if (previous.status === 'session-ended-by-person') {
    state.status = previous.status;
    state.pendingQuestion = null;
  }
  return state;
}
