import test from 'node:test';
import assert from 'node:assert/strict';
import { referenceChart, referenceChartData } from './result-chart.js';

const result = (raw_scores = [-1, 0, 1], score = 0.25) => ({
  kind: 'score', score, percentileRange: [55, 64],
  reference: { source: 'Recorded reference', version_string: 'v1', n_participants: raw_scores.length,
    distribution: { raw_scores, mean: 0, sd: 1, n: raw_scores.length } },
});
const figures = html => [...html.matchAll(/<svg class="comparison-person" /g)].length;
const cellRelations = html => [...html.matchAll(/<span class="comparison-cell comparison-(lower|you|equal|higher)"/g)].map(match => match[1]);
function assertGrid(html, lower, equal, higher) {
  assert.equal([...html.matchAll(/class="comparison-grid"/g)].length, 1);
  assert.deepEqual(cellRelations(html), [
    ...Array(lower).fill('lower'), 'you', ...Array(equal).fill('equal'), ...Array(higher).fill('higher'),
  ]);
  assert.equal(figures(html), lower + equal + higher + 1);
}
const counts = (n, n_below, n_equal, n_above) => ({ n, n_below, n_equal, n_above });
const assertUnavailable = (input, recordedCounts = []) => {
  assert.equal(referenceChartData(input, recordedCounts), null);
  const html = referenceChart(input, recordedCounts);
  assert.match(html, /comparison chart is unavailable/i);
  assert.equal(figures(html), 0);
  assert.doesNotMatch(html, /<svg|scored lower/);
};

test('every comparison person has a figure and the highlighted person is additional', () => {
  const input = result();
  const chart = referenceChartData(input);
  const html = referenceChart(input);
  assert.equal(chart.kind, 'people');
  assert.equal(chart.total, 3);
  assert.equal(figures(html), 4);
  assert.match(html, /Out of 3 people in your comparison group, <strong>2 scored lower than you\./);
  assert.match(html, /0 scored the same\. 1 scored higher\./);
  assert.match(html, /blue figure marks you and is not part of the group/);
  assert.match(html, /class="comparison-cell comparison-you"><svg class="comparison-person"/);
  assert.match(html, /<small>You<\/small>/);
  assertGrid(html, 2, 0, 1);
  assert.doesNotMatch(html, /similar ancestry|same ancestry/);
});

test('recorded values and the full percentile range survive without changing the input or counts', () => {
  const input = result([-1, 0, 1], 0.25000000000000006);
  input.percentileRange = [60.1234567890123, 70.5];
  const recorded = [counts(3, 2, 0, 1), { ...counts(3, 2, 0, 1), tool: 'rank_range.py' }];
  const before = structuredClone({ input, recorded });
  const html = referenceChart(input, recorded);
  assert.match(html, /0\.25000000000000006/);
  assert.match(html, /60\.1234567890123–70\.5<\/dd>/);
  assert.match(html, /does not cover every source of uncertainty/);
  assert.match(html, /<details[^>]*><summary id="comparison-details-toggle">Show comparison details/);
  assert.deepEqual({ input, recorded }, before);
});

test('exact lower, equal and higher counts stay separate from the uncertainty range', () => {
  const input = result([-1, 0, 0, 0, 1], 0);
  input.percentileRange = [11.123, 88.876];
  const chart = referenceChartData(input, [counts(5, 1, 3, 1)]);
  const html = referenceChart(input, [counts(5, 1, 3, 1)]);
  assert.deepEqual(chart.entries, [{ value: -1, count: 1 }, { value: 0, count: 3 }, { value: 1, count: 1 }]);
  assert.equal(figures(html), 6);
  assertGrid(html, 1, 3, 1);
  assert.match(html, /Out of 5 people[^<]+<strong>1 scored lower than you\./);
  assert.match(html, /3 scored the same\. 1 scored higher\./);
  assert.match(html, /11\.123–88\.876/);
  assert.match(html, /Your recorded score is 0\./);
  assert.doesNotMatch(html, /×3/);
});

test('a single comparison person is neither expanded to 100 nor replaced by you', () => {
  const html = referenceChart(result([1], 0));
  assert.equal(figures(html), 2);
  assertGrid(html, 0, 0, 1);
  assert.match(html, /Out of 1 person in your comparison group/);
  assert.match(html, /0 scored lower than you/);
  assert.match(html, /0 scored the same\. 1 scored higher/);
});

test('all lower, all higher and all tied cohorts retain exact counts and every person', () => {
  for (const [scores, score, expected] of [
    [[-8, -4, -1], -10, counts(3, 0, 0, 3)],
    [[1, 2, 3], 5, counts(3, 3, 0, 0)],
    [[0, 0, 0], 0, counts(3, 0, 3, 0)],
  ]) {
    const input = result(scores, score);
    const chart = referenceChartData(input, [expected]);
    for (const key of Object.keys(expected)) assert.equal(chart[key], expected[key]);
    assertGrid(referenceChart(input, [expected]), expected.n_below, expected.n_equal, expected.n_above);
  }
});

test('large groups keep one figure per supplied person instead of sampling or replacing them with bins', () => {
  const scores = Array.from({ length: 20_003 }, (_, i) => ((i % 999) - 499) / 1000);
  const input = result(scores);
  const chart = referenceChartData(input);
  assert.equal(chart.kind, 'people');
  assert.equal(chart.entries.reduce((sum, entry) => sum + entry.count, 0), scores.length);
  assert.equal(chart.n_below + chart.n_equal + chart.n_above, scores.length);
  assert.equal(chart.entries[0].value, Math.min(...scores));
  assert.equal(chart.entries.at(-1).value, Math.max(...scores));
  const html = referenceChart(input);
  assert.equal(figures(html), scores.length + 1);
  assertGrid(html, chart.n_below, chart.n_equal, chart.n_above);
  assert.match(html, /comparison-scroll/);
  assert.match(html, /tabindex="0" role="region" aria-label="Comparison figures\. Scroll to see every person\./);
  assert.doesNotMatch(html, /data-chart-kind="distribution"|<rect/);
});

test('every recorded count source must agree with the scores and with the other source', () => {
  const input = result();
  const correct = counts(3, 2, 0, 1);
  assert.equal(referenceChartData(input, [null, undefined, correct]).kind, 'people');
  for (const supplied of [
    counts(4, 2, 0, 2), counts(3, 1, 0, 2), counts(3, 2, 1, 0), counts(3, 2, 0, -1),
    counts(3, 1.5, 0, 1.5), counts('3', 2, 0, 1), counts(3, 2, NaN, 1),
    { n: 3, n_below: 2, n_above: 1 }, {},
  ]) {
    assertUnavailable(input, [supplied]);
    assertUnavailable(input, [correct, supplied]);
    assertUnavailable(input, [supplied, correct]);
  }
});

test('counts use the recorded comparison score when the delivered score has been rounded', () => {
  const input = result([0.2499, 0.25, 0.25000000000000006, 0.2501], 0.25);
  const comparisonScore = 0.25000000000000006;
  const recorded = [counts(4, 2, 1, 1)];
  const before = structuredClone({ input, recorded });
  const chart = referenceChartData(input, recorded, comparisonScore);
  assert.equal(chart.score, 0.25);
  assert.equal(chart.n_below, 2);
  assert.equal(chart.n_equal, 1);
  assert.equal(chart.n_above, 1);
  const html = referenceChart(input, recorded, comparisonScore);
  assert.equal(figures(html), 5);
  assertGrid(html, 2, 1, 1);
  assert.match(html, /2 scored lower than you/);
  assert.match(html, /1 scored the same\. 1 scored higher\./);
  assert.match(html, /Your recorded score is 0\.25\./);
  assert.match(html, /<th scope="row">0\.25000000000000006<\/th>/);
  assert.deepEqual({ input, recorded }, before);
  assertUnavailable(input, recorded);
});

test('an invalid explicit comparison score cannot authorize a person chart', () => {
  const input = result();
  for (const comparisonScore of [null, '0.25', NaN, Infinity]) {
    assert.equal(referenceChartData(input, [], comparisonScore), null);
    assert.match(referenceChart(input, [], comparisonScore), /comparison chart is unavailable/i);
  }
});

test('a chart cannot silently contradict the recorded participant or distribution count', () => {
  for (const change of [
    input => { input.reference.n_participants = 4; },
    input => { input.reference.n_participants = '3'; },
    input => { delete input.reference.n_participants; },
    input => { input.reference.distribution.n = 4; },
    input => { input.reference.distribution.n = 2.5; },
    input => { delete input.reference.distribution.n; },
  ]) {
    const input = result(); change(input); assertUnavailable(input);
  }
});

test('invalid supplied ranges never turn into an apparently exact placement', () => {
  for (const range of [[], [50], [0, 50, 100], [60, 20], [-1, 50], [20, 101], ['20', 50], [NaN, 50], [20, Infinity]]) {
    const input = result(); input.percentileRange = range; assertUnavailable(input);
  }
  for (const range of [[0, 100], [0, 0], [100, 100], [49.999, 50.001]]) {
    const input = result(); input.percentileRange = range;
    assert.equal(referenceChartData(input).kind, 'people');
    assert.ok(referenceChart(input).includes(`${range[0]}–${range[1]}`));
  }
});

test('an older comparison can show exact counts without inventing absent range bounds', () => {
  for (const range of [undefined, null]) {
    const input = result(); input.percentileRange = range;
    const before = structuredClone(input);
    const chart = referenceChartData(input);
    assert.equal(chart.kind, 'people');
    assert.equal(chart.n_below, 2);
    const html = referenceChart(input);
    assertGrid(html, 2, 0, 1);
    assert.match(html, /2 scored lower than you/);
    assert.doesNotMatch(html, /<dt>Percentile range<\/dt>|The reported range allows|55–64|undefined|null/);
    assert.deepEqual(input, before);
  }
});

test('extreme and tiny finite scores keep exact counts without chart geometry calculations', () => {
  for (const [scores, score] of [
    [[-Number.MAX_VALUE, Number.MAX_VALUE], 0],
    [[Number.MIN_VALUE, 2 * Number.MIN_VALUE], Number.MIN_VALUE],
    [Array(500).fill(Number.MAX_VALUE), -Number.MAX_VALUE],
    [Array(500).fill(0), 0],
  ]) {
    const input = result(scores, score);
    const html = referenceChart(input);
    assert.doesNotMatch(html, /NaN|Infinity|undefined/);
    assert.ok(html.includes(`Your recorded score is ${String(score)}`));
    assert.equal(figures(html), scores.length + 1);
    const chart = referenceChartData(input);
    assert.equal(chart.entries.reduce((sum, entry) => sum + entry.count, 0), scores.length);
    for (const match of html.matchAll(/\b(?:cx|cy|x|y|width|height)="([\d.-]+)"/g)) assert.ok(Number.isFinite(Number(match[1])));
  }
});

test('signed zero remains recorded while both signed zeros count as ties', () => {
  const input = result([-0, 0, 1], -0);
  const chart = referenceChartData(input);
  assert.equal(chart.n_equal, 2);
  const html = referenceChart(input);
  assert.equal(figures(html), 4);
  assert.match(html, /Your recorded score is -0\./);
  assert.match(html, /<th scope="row">-0<\/th>/);
  assert.match(html, /<th scope="row">0<\/th>/);
});

test('quantile-only references show recorded summaries without synthesizing people or lower counts', () => {
  const input = result([]);
  input.reference.n_participants = 900;
  input.reference.distribution = { mean: 0, sd: 1, n: 900,
    p01: -2.3, p05: -1.6, p10: -1.2, p25: -0.6, p50: 0, p75: 0.6, p90: 1.2, p95: 1.6, p99: 2.3 };
  const chart = referenceChartData(input);
  assert.equal(chart.kind, 'summary');
  assert.equal(chart.entries.length, 9);
  const html = referenceChart(input);
  assert.match(html, /data-chart-kind="summary"/);
  assert.match(html, /Individual scores were not supplied/);
  assert.doesNotMatch(html, /<svg|scored lower|scored the same|scored higher/);
  assert.equal(figures(html), 0);
  for (const entry of chart.entries) assert.ok(html.includes(String(entry.value)));
});

test('malformed distributions are unavailable and non-score outcomes never get a chart', () => {
  for (const input of [undefined, { kind: 'refuse' }, { kind: 'pending' }]) assert.equal(referenceChart(input), '');
  for (const distribution of [undefined, {}, { raw_scores: [] }, { raw_scores: [0, NaN] }, { raw_scores: [0, '1'] },
    { p01: 9, p05: 8, p10: 7, p25: 6, p50: 5, p75: 4, p90: 3, p95: 2, p99: 1 }]) {
    const input = result(); input.reference.distribution = distribution; assertUnavailable(input);
  }
  for (const score of [undefined, '0.25', NaN, Infinity]) {
    const input = result(); input.score = score; assertUnavailable(input);
  }
});

test('screen readers receive complete counts while repeated figure graphics are decorative', () => {
  const html = referenceChart(result([-1, 0, 0, 1], 0));
  assert.match(html, /<figcaption>[\s\S]*Out of 4 people[\s\S]*1 scored lower than you/);
  assert.match(html, /<p class="comparison-text">2 scored the same\. 1 scored higher\./);
  assert.match(html, /55–64/);
  const icons = [...html.matchAll(/<svg class="comparison-person"[^>]*>/g)].map(match => match[0]);
  assert.equal(icons.length, 5);
  for (const icon of icons) assert.match(icon, /aria-hidden="true"/);
  assert.doesNotMatch(html, /<svg[^>]*tabindex=/);
  assert.match(html, /spacing does not measure a difference in scores/);
});

test('the chart never shows the reference\'s own record of where it comes from, raw or escaped', () => {
  const input = result();
  input.reference.source = '<img src=x onerror="alert(1)"> ref_1kg.vcf.gz from refdata/ancestry-ref';
  input.reference.version_string = '<script>alert(1)</script> ##source=1000GenomesPhase3Pipeline';
  const html = referenceChart(input);
  assert.doesNotMatch(html, /<img|<script|&lt;img|&lt;script|ref_1kg|refdata|##source|Comparison source|Reference version/);
});
