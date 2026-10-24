/** In-memory, opt-in alerts. Question text never leaves the page in a notification. */
export function createQuestionAlerts({ onChange = () => {}, documentRef = globalThis.document,
  NotificationImpl = globalThis.Notification, AudioContextImpl = globalThis.AudioContext || globalThis.webkitAudioContext,
  focusWindow = () => globalThis.window?.focus() } = {}) {
  let enabled = false, context = null, activeRun = null, message = '', disposed = false;
  let lastRun = null;
  let lastQuestionKey = null;
  const seen = new Set(), notifications = new Set();
  const state = () => ({ enabled, message });
  const closeNotifications = () => {
    for (const notification of notifications) notification.close();
    notifications.clear();
  };
  function sound() {
    if (!context || context.state !== 'running') return;
    const oscillator = context.createOscillator(), gain = context.createGain();
    oscillator.type = 'sine'; oscillator.frequency.setValueAtTime(660, context.currentTime);
    gain.gain.setValueAtTime(0, context.currentTime);
    gain.gain.linearRampToValueAtTime(.035, context.currentTime + .04);
    gain.gain.exponentialRampToValueAtTime(.001, context.currentTime + .4);
    oscillator.connect(gain); gain.connect(context.destination);
    oscillator.start(); oscillator.stop(context.currentTime + .45);
  }
  function observe(run) {
    if (disposed) return;
    lastRun = run;
    const question = ['running','waiting'].includes(run?.status) ? run.question : null;
    const questionKey = question?.id ? `${run?.run_id}:${question.id}` : null;
    if (questionKey !== lastQuestionKey) closeNotifications();
    lastQuestionKey = questionKey;
    if (run?.run_id !== activeRun) { seen.clear(); activeRun = run?.run_id; }
    if (documentRef) documentRef.title = question ? 'Answer needed · prs-agent' : run?.status === 'completed' ? 'Your result · prs-agent' : run?.status === 'failed' ? 'Analysis stopped · prs-agent' : ['running','waiting'].includes(run?.status) ? 'Analysis in progress · prs-agent' : 'Your workspace · prs-agent';
    const key = question?.id;
    if (!enabled || !key || seen.has(key) || disposed) return;
    seen.add(key); sound();
    if (documentRef?.hidden && NotificationImpl?.permission === 'granted') {
      try {
        const notification = new NotificationImpl('Your answer is needed', { body: 'Open prs-agent to continue your analysis.', tag: 'prs-question' });
        notifications.add(notification);
        notification.onclick = () => { focusWindow(); notification.close(); notifications.delete(notification); };
      } catch { /* Title and sound work where direct browser notifications are unavailable. */ }
    }
  }
  async function toggle() {
    if (disposed) return state();
    if (enabled) { enabled = false; closeNotifications(); message = 'Sound and notifications are off. The tab title still flags questions.'; }
    else {
      if (AudioContextImpl) {
        try { context ||= new AudioContextImpl(); await context.resume(); } catch { /* Explain unavailable sound below. */ }
      }
      if (disposed) return state();
      if (NotificationImpl?.permission === 'default') {
        try { await NotificationImpl.requestPermission(); } catch { /* Browser controls permission. */ }
      }
      if (disposed) return state();
      enabled = true;
      const soundOn = context?.state === 'running';
      const notificationsOn = NotificationImpl?.permission === 'granted';
      message = notificationsOn ? `${soundOn ? 'Sound on. ' : ''}Notifications allowed when this tab is in the background.`
        : `${soundOn ? 'Sound on. ' : ''}Notifications unavailable or not allowed. Watch the tab title for questions.`;
      observe(lastRun);
    }
    onChange(state()); return state();
  }
  function dispose() {
    disposed = true; enabled = false; seen.clear(); lastRun = null;
    closeNotifications(); context?.close().catch(() => {});
  }
  return { state, toggle, observe, dispose };
}
