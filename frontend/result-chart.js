/** Presentation only: recorded scores and reported ranges are never recalculated. */
const finite = value => typeof value === 'number' && Number.isFinite(value);
const escapeHTML = value => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
const exact = value => Object.is(value, -0) ? '-0' : String(value);
const quantileKeys = ['p01', 'p05', 'p10', 'p25', 'p50', 'p75', 'p90', 'p95', 'p99'];
const validRange = range => Array.isArray(range) && range.length === 2 && range.every(value => finite(value) && value >= 0 && value <= 100) && range[0] <= range[1];

/** Count the supplied people for display, checking every recorded count source. */
export function referenceChartData(result, recordedCounts = [], comparisonScore = result?.score) {
  if (result?.kind !== 'score' || !finite(result.score) || !finite(comparisonScore) || (result.percentileRange != null && !validRange(result.percentileRange))) return null;
  const reference = result.reference, distribution = reference?.distribution;
  if (!distribution || typeof distribution !== 'object') return null;
  const scores = distribution.raw_scores;
  if (scores !== undefined && !Array.isArray(scores)) return null;
  if (Array.isArray(scores) && scores.length) {
    if (!scores.every(finite) || !Number.isSafeInteger(reference.n_participants)
      || reference.n_participants !== scores.length || distribution.n !== scores.length) return null;
    const counts = { n: scores.length, n_below: 0, n_equal: 0, n_above: 0 };
    const entries = new Map();
    for (const value of scores) {
      counts[value < comparisonScore ? 'n_below' : value > comparisonScore ? 'n_above' : 'n_equal']++;
      const key = exact(value), entry = entries.get(key) || { value, count: 0 };
      entry.count++;
      entries.set(key, entry);
    }
    for (const supplied of recordedCounts.filter(value => value != null)) {
      if (Object.keys(counts).some(key => !Number.isSafeInteger(supplied[key]) || supplied[key] !== counts[key])) return null;
    }
    return { kind: 'people', total: scores.length, score: result.score, ...counts,
      entries: [...entries.values()].sort((a, b) => a.value < b.value ? -1 : a.value > b.value ? 1 : 0) };
  }
  // Older summaries cannot identify individual people's scores. Never turn a
  // quantile into a person or estimate how many people lie below the score.
  const entries = quantileKeys.map(key => ({ key, value: distribution[key] }));
  if (!entries.every(entry => finite(entry.value))
    || entries.some((entry, index) => index > 0 && entry.value < entries[index - 1].value)) return null;
  return { kind: 'summary', score: result.score, entries };
}

const person = '<svg class="comparison-person" viewBox="0 0 24 38" aria-hidden="true"><use href="#comparison-person-shape"/></svg>';
const personShape = '<svg class="comparison-definitions" aria-hidden="true" width="0" height="0"><defs><g id="comparison-person-shape" fill="inherit" stroke="currentColor" stroke-width="inherit" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="3"/><path fill="none" d="M12 12v12m-7-6 7-6 7 6M12 24 6 34m6-10 6 10"/></g></defs></svg>';
function people(count, relation) {
  return `<span class="comparison-cell comparison-${relation}">${person}</span>`.repeat(count);
}
function peopleView(chart) {
  const legend = `<div class="comparison-legend" aria-hidden="true"><span><i class="legend-lower"></i>${chart.n_below} lower</span><span><i class="legend-you"></i>You</span>${chart.n_equal ? `<span><i class="legend-equal"></i>${chart.n_equal} the same</span>` : ''}<span><i class="legend-higher"></i>${chart.n_above} higher</span></div>`;
  return `${personShape}${legend}<div id="comparison-figures" class="comparison-pictogram${chart.total > 200 ? ' comparison-scroll' : ''}" ${chart.total > 200 ? 'tabindex="0" role="region" aria-label="Comparison figures. Scroll to see every person."' : ''}><div class="comparison-grid" aria-hidden="true">${people(chart.n_below, 'lower')}<span class="comparison-cell comparison-you">${person}<small>You</small></span>${people(chart.n_equal, 'equal')}${people(chart.n_above, 'higher')}</div></div>`;
}
function dataTable(chart) {
  if (chart.kind === 'summary') return `<table><caption>Group summaries recorded by the analysis</caption><thead><tr><th scope="col">Percentile</th><th scope="col">Score</th></tr></thead><tbody>${chart.entries.map(entry => `<tr><th scope="row">${Number(entry.key.slice(1))}</th><td>${exact(entry.value)}</td></tr>`).join('')}</tbody></table>`;
  return `<table><caption>All comparison scores. Repeated values are counted together here; each person has their own figure above.</caption><thead><tr><th scope="col">Score</th><th scope="col">People</th></tr></thead><tbody>${chart.entries.map(entry => `<tr><th scope="row">${exact(entry.value)}</th><td>${entry.count}</td></tr>`).join('')}</tbody></table>`;
}

function recordedMetadata(result) {
  const reference = result.reference;
  const distribution = reference.distribution;
  const fields = [
    ['Your recorded score', exact(result.score)],
    ['Percentile range', Array.isArray(result.percentileRange) && result.percentileRange.length === 2 && result.percentileRange.every(finite)
      ? `${exact(result.percentileRange[0])}–${exact(result.percentileRange[1])}` : null],
    ['Comparison participants', finite(reference.n_participants) ? exact(reference.n_participants) : null],
    ['Recorded distribution size', finite(distribution.n) ? exact(distribution.n) : null],
    ['Recorded mean', finite(distribution.mean) ? exact(distribution.mean) : null],
    ['Recorded standard deviation', finite(distribution.sd) ? exact(distribution.sd) : null],
  ];
  return `<dl class="reference-chart-metadata">${fields.filter(([, value]) => value !== null).map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHTML(value)}</dd></div>`).join('')}</dl>`;
}

/** Native text and disclosure provide the complete screen-reader version. */
export function referenceChart(result, recordedCounts = [], comparisonScore = result?.score) {
  if (result?.kind !== 'score') return '';
  const chart = referenceChartData(result, recordedCounts, comparisonScore);
  if (!chart) return '<p class="reference-chart-unavailable">The comparison chart is unavailable. The recorded scores and group counts could not be checked.</p>';
  const isPeople = chart.kind === 'people';
  const caption = isPeople
    ? `Out of ${chart.total} ${chart.total === 1 ? 'person' : 'people'} in your comparison group, <strong>${chart.n_below} scored lower than you.</strong>`
    : 'Individual scores were not supplied, so people cannot be shown here.';
  const textVersion = isPeople ? `<p class="comparison-text">${chart.n_equal} scored the same. ${chart.n_above} scored higher. Each figure is one person. The blue figure marks you and is not part of the group.</p>` : '';
  return `<figure class="reference-chart" data-chart-kind="${chart.kind}"><figcaption><h2>Your comparison group</h2><p>${caption}</p></figcaption>${textVersion}${isPeople ? peopleView(chart) : ''}<p class="reference-chart-note">${validRange(result.percentileRange) ? 'The reported range allows for the size of this sample. It does not cover every source of uncertainty or show your chance of developing a condition.' : 'These are the recorded comparison scores. No range was supplied for this result. Your place in this group is not your chance of developing a condition.'}</p><details id="comparison-values" class="reference-chart-details"><summary id="comparison-details-toggle">Show comparison details</summary><p>Your recorded score is ${exact(chart.score)}. ${isPeople ? 'Every comparison person is shown, including people with the same score. The figures are grouped by whether their score is lower, the same or higher; spacing does not measure a difference in scores.' : 'Only the group summaries recorded by the analysis are shown below.'}</p>${recordedMetadata(result)}${dataTable(chart)}</details></figure>`;
}
