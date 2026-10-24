# prs-agent frontend

## What is built and how to run it

A responsive, local frontend in `frontend/`, using HTML, CSS, and browser JavaScript modules. There are no runtime dependencies, external fonts, analytics, cookies, browser storage, credentials, upload endpoints, or calls into the agent. The existing agent and harness are unchanged.

From the repository root, with Node 20 or later:

```sh
npm start
```

Open **http://127.0.0.1:8000**. No install or build step is required. Set `PORT` to use another port. The server binds only to loopback, serves an explicit list of frontend assets, rejects methods other than GET/HEAD, and does not expose repository files. Its content security policy blocks application network connections and external assets. The static frontend also works with a local static server serving `frontend/`; use the supplied server for its additional response headers and file allowlist.

When running inside a VM, keep `npm start` running on the VM and open an SSH tunnel from a terminal on your own computer:

```sh
ssh -N -L 8000:127.0.0.1:8000 <user>@<vm-host>
```

Replace `<user>` and `<vm-host>`, including the angle brackets, with your VM login and host address. Leave the tunnel open, then visit **http://localhost:8000** in your computer's browser. The preview server remains bound to the VM's loopback interface.

This stack keeps the privacy boundary easy to inspect and the interface usable without reference data or a development toolchain. The Node server only serves assets; it is not an agent backend.

Included screens and interactions:

- A homepage that explains what prs-agent is, who it is for, what a person needs, and the intended process in three steps. It introduces polygenic risk scores and percentiles in plain language, gives equal space to a score and an explained refusal, and states clearly that this is a preview with no connected DNA analysis.
- Homepage navigation links to **How it works**, **What you need**, and **FAQs**. **Try the preview** opens file selection, **See an example** opens the labeled refusal example, and a saved-report link opens the local viewer. The brand and **About** return to the homepage.
- Local file selection and drag/drop, metadata, removal, privacy explanation, and a file-review screen with absent findings left unavailable. Expandable help explains raw DNA files; file review explains the genome build as the version of the DNA reference map.
- A free-text trait question and optional ancestry self-description, including “I don’t know.”
- An ancestry disagreement screen, with separate self-description and genetic similarity, continuation, and voluntary exit.
- Model reasoning, candidate tradeoffs in the agent’s order, explicitly estimated coverage, acknowledgment for a transparency gate, and a free-text response for a value decision. The page explains that a scoring model is a research formula, separate from the AI assistant, and introduces coverage in everyday language. No candidate is recommended without a supplied artifact.
- Six-stage progress with plain-language stage descriptions, questions and replies, event history, reselection, a reference-wait state, and a separate data stop. Example events advance only when deliberately selected.
- A scored report renderer and an unpopulated report preview. A valid score requires recorded interpretation, a usable reference, and measured coverage; the frontend computes none of them.
- A full refusal page rendered directly from `report.refusal_envelope`, with a plainly labeled, prose-only example available without data. Missing delivery stays missing rather than being reconstructed from technical reasons.
- Optional explanations within score and refusal reports, labeled **From this report** or **General explanation**. Example explanations keep an explicit example label. The original report, refusal envelope, and personalized limits stay visible above the exploration area.
- A finite set of optional next explanations, with a clear stopping point once they have been opened. These are supplied report passages and static explanations, with no generated agent replies.
- A **My questions** notebook: keep a suggested question, write your own, edit it, or remove it. Download a standalone plain-text appointment note, or save the questions with the report and restore them by explicitly importing that JSON file.
- Appointment-conversation preparation, explicit report downloads, and exit that clears the tab’s application state.
- FAQs covering what scores can tell a person, refusal versus a data stop, suitable questions, preview capabilities, and unresolved provider access. Privacy copy distinguishes what this preview does from disclosures a future connected version would need.
- Connection-option comparison and **Open a saved report**, with a plain explanation of the downloaded JSON report format and separate technical details for reviewing backend output shapes.

The footer’s collapsed **Preview tools** contains the screen selector and artifact viewer on desktop and mobile. No DNA is needed to explore. The result preview contains no fabricated score, percentile, cohort, distribution, or clinical metric. The refusal, stop, and progress examples are explicitly labeled; they describe no real person or selected file. Contract-test fixtures contain explicitly labeled synthetic values and are never bundled or served by the app. Explicitly imported bundles with `example: true` retain their example label in the viewer and every report download.

The homepage and beginner explanations are static local HTML templates and CSS within the existing frontend. They add no services, assets, requests, or scientific computations. Supplied report text and refusal envelopes remain directly rendered rather than rewritten by the introductory copy.

## Screen flow and specification sources

The implemented flow adds an explanatory homepage before the originally proposed analysis flow:

```text
Homepage: what it is, what you need, examples, and FAQs
    → privacy disclosure → local file selection → file findings
    → trait question + optional self-description
    → ancestry context / disagreement, before model selection
    → recommended model, evidence, cost, and appropriate gate
    → stage progress ↔ questions and replies
         ↳ measured coverage may return to model selection
         ↳ missing provider → waiting, no agent judgment
         ↳ valid placement → score report → follow-up or exit
         ↳ scientific refusal → refusal report → follow-up or exit
         ↳ broken / unclassifiable data → stop explanation
```

All stage screens are previewable, but their navigation is not an execution schedule. Real classification and ancestry work must complete before selection. A loaded ancestry disagreement is surfaced before entering model review. A person may end the session at any gate; it is recorded separately from agent outcomes.

| Screen | Source | Behavior carried into the frontend |
| --- | --- | --- |
| Homepage and beginner introduction | All six skills; especially `agent/.claude/skills/report/SKILL.md`, FRAME-1/FRAME-2/REP-3 and refusal delivery | Explain the product before asking for a file. Define PRS and percentile without invented results, distinguish score from probability and diagnosis, introduce refusal as a valid result, and explain the current preview boundary. Show required inputs, a three-step overview, FAQs, and local-file privacy before the intake CTA. |
| File selection and findings | `agent/.claude/skills/read-and-reason/SKILL.md` | Accept text and archives without inferring semantics from extensions. Show assay, build evidence, absence semantics, and confidence only when supplied. Classification uncertainty can stop the run. A checksum is not external verification. |
| Trait question | `agent/.claude/skills/select/SKILL.md`, SEL-1/SEL-3 | Preserve the person’s wording. Ontology mapping belongs to the agent. A diagnosis/severity ambiguity requires conversation, not a string-match shortcut. |
| Ancestry context and disagreement | `agent/.claude/skills/ancestry/SKILL.md`, ANC-1–ANC-6 | Self-report is optional and never drives computation. Identity and reference-genome similarity are not competing measurements of the same thing. Show disagreement before choosing a model; allow exit without an ancestry-choice control. No invented PC plot or ancestry percentages. |
| Model recommendation | `agent/.claude/skills/select/SKILL.md`, SEL-2/SEL-4/SEL-6–SEL-12 | Keep the agent’s evidence ranking and each candidate’s cost. Distinguish acknowledgment from a value decision. Coverage is an estimate and a floor, not a ranking criterion. A refusal cannot be overridden with “score anyway.” |
| Progress, conversation, and reselection | All six skills; especially `score/SKILL.md` and `select/SKILL.md` | Event-driven state, no timed stage advancement, no percentage or made-up ETA. A reply waits for agent judgment. A second selection attempt retains history. |
| Reference waiting | `agent/.claude/skills/interpret/SKILL.md`, INT-1/INT-2 | No provider response means blocked availability, not stop/refuse. A provider’s inability to build a usable reference is grounds for an explicit interpretation refusal. |
| Score report | `agent/.claude/skills/report/SKILL.md`, REP-1–REP-7; `interpret/SKILL.md` | Percentile is explained as relative position, never probability; it may move. Include personalized limits, measured weight coverage, source/release/cohort rule/assay/parity, all provider warnings and interpretation caveats, and support. Do not show a raw score without valid placement. |
| Refusal report | `agent/.claude/skills/report/SKILL.md`, refusal-envelope section | Direct rendering of headline, detail, what was tried, why it is honest, what would change it. `is_about_the_person: false` adds the explicit science/data/assay limitation message. Clinician handoff is separate; it has no envelope field. |
| Report exploration and follow-up | `agent/.claude/skills/report/SKILL.md`, REP-8–REP-14 | Optional, source-labeled explanations stay scoped to the result. Score topics cover percentile meaning, the chosen model’s recorded reasoning and tradeoff, supplied limits, and support. Refusal topics use the envelope directly for why and what could change; the science/data scope topic requires `is_about_the_person: false`. A finite next-explanation prompt ends when all topics have been opened. Health decisions belong with a clinician; no agent response is simulated. |
| My questions and appointment notes | `agent/.claude/skills/report/SKILL.md`, REP-8/REP-12–REP-14 | Keep, edit, and remove local questions; download a plain-text note for a clinician conversation. A suggested appointment request helps with the contact step without giving medical advice. Explicit report export/import can carry the question list. |

`agent/JUDGMENT-CORE.md`, `agent/AGENT.md`, and `agent/interfaces/reference-distribution.md` were also read. They clarify final outcomes, stage aliases, source provenance, provider fields, and session ending.

## Authentication: comparison, not a decision

Neither route is implemented or selected. There is intentionally no usable API-key input and no pretend provider sign-in button. The connection screen presents both options.

| Consideration | Pasted API key | Provider sign-in / delegated session |
| --- | --- | --- |
| Credential responsibility | The app receives a secret. Browser code and any server proxy that handles it become part of the trust boundary. | The app still handles sensitive authorization tokens or sessions, even if the person never pastes a secret. |
| Architecture needed | Decide browser-direct versus a backend broker; minimize scope/lifetime, avoid persistence, redact logs/errors/telemetry, prevent exposure to other scripts, and support revocation. | A provider-supported delegation flow for this actual use, with scopes, consent, token exchange, secure session storage, expiry, refresh, and revocation. A CLI login does not establish this. |
| “Never store it” | Only defensible after inspecting the complete architecture, including proxies, tracing, crash reports, and browser persistence. Memory-only is not the same as impossible to expose. | Requires equally specific retention and session-handling statements. Avoiding a pasted key does not eliminate credential custody. |
| Subscription / billing | A consumer subscription must not be assumed to include API usage. Verify eligibility and charges before offering a run. | Verify that delegation is supported and permitted, and that the intended subscription actually covers the agent usage. |
| User experience | Explicit but technical; users need clear scope and revocation instructions. | Potentially simpler, if supported; permissions and disconnect controls still need to be visible. |

The existing `harness/harness/auth.py` supports Claude CLI authentication. It does not implement browser provider authorization or an OpenAI backend. It also documents historical credential exposure through manifests and subsequent redaction: a blanket “never stored” claim would not follow from this repository. No credential files were read for this frontend work.

Before either option launches, the product also needs an explicit genomic-data flow: execution host, provider-visible prompts/tool output, any reference-service transfers, retention of originals and derived data, deletion behavior, and consent bound to that particular plan. The local file chooser is not permission for a future upload.

## Artifact boundary and backend work still required

`frontend/contracts.js` is the read-only artifact boundary. `frontend/session.js` projects ordered events. `frontend/exploration.js` builds source-labeled explanation topics from accepted reports, preserving delivered limits, model reasoning, and refusal-envelope prose. `frontend/notebook.js` validates question imports and handles immutable add/edit operations. `frontend/app.js` owns presentation, removal, downloads, and local state. `frontend/examples.js` is labeled illustrative copy and contains no calculated score.

Use **Open a saved report** on the homepage or file screen, or **Preview tools → Open a saved report**, to select a JSON object. The viewer explains that this is a saved report rather than a DNA data file. It can contain any subset of these keys:

| Bundle key | Original artifact |
| --- | --- |
| `classification` | `door/classification.json` |
| `ancestry` | `ancestry/ancestry.json` |
| `model_choice` | `select/model_choice.json` |
| `score` | `score/score.json`, or the mechanical `score/raw_score.json` shape |
| `coverage` | **Measured** `score/coverage_<PGS>.json`, with `tool: "weight_coverage.py measure"` |
| `interpret` | `interpret/interpret.json` |
| `reference_response` | `interpret/reference_response.json`, including the provider’s `reference` and `warnings_for_report` |
| `report` | `report/report.json` |
| `events` | Optional ordered array of `session.jsonl` event objects |
| `frontend_notes` | Optional frontend-only `{ "schema_version": 1, "questions": [string] }` object, written by explicit report downloads; this is not an agent judgment |

The validator also accepts stage-keyed artifacts under `stages` (`door` or `read-and-reason`, `ancestry`, `select`, `score`, `interpret`, `report`) and `measured_coverage` as an alias. This means full judgment objects, not the smaller `result.json` stage summaries. The viewer reads only explicitly selected JSON, up to 5 MiB. It never opens referenced filesystem paths or follows evidence URLs.

Report downloads include `frontend_notes: { "schema_version": 1, "questions": [...] }`. Use **Open a saved report** on the homepage or file screen to import the downloaded JSON and restore valid questions to **My questions**. The list allows up to 30 questions of 2,000 characters each. Outer whitespace is trimmed; wording and internal line breaks are preserved. Duplicate detection ignores case and repeated whitespace. A malformed notes object, any invalid question, or an oversized list causes the entire notes block to be ignored, with a visible notice; it does not invalidate otherwise usable report artifacts. A separate **Download my questions** action saves a `.txt` appointment note containing the questions and report context. Example context is labeled in both download formats, and questions retain their example status across screen navigation.

Questions, drafts, and opened explanation topics exist only in memory in the current tab. Closing or refreshing the tab clears them unless the person has chosen to download a copy; restoring downloaded questions requires an explicit import. The frontend has no automatic persistence, notifications, background evidence updates, or live agent follow-up responses. Saving a question never sends it anywhere or supplies an answer.

Missing facts are not filled. Invalid or inconsistent scored artifacts suppress all numeric result display. A refusal does not require a reference or score, but it does require the person-facing envelope and recorded judgment. The validator checks structure and consistency; it cannot authenticate the source, establish a patient’s identity, validate scientific claims, or attest that a local file belongs to an imported run. Selecting a new genome clears imported findings; importing a run releases the selected genome reference and intake answers.

The report skill leaves the types inside `delivered.score` and `delivered.placement` unspecified. The bridge accepts a finite numeric score (or `{ "value": number }`) and a numeric placement (or `{ "percentile": number }`). A recorded `score.raw_score` can supply the score; `interpret.percentile` supplies placement. Conflicting supplied values are rejected. It never extracts numbers from prose. This is an explicit integration convention, not an assertion that the skill already defines those shapes.

For progress, the reducer accepts the current harness’s `run_started`, `stage_started`, `stage_progress`, `stage_completed`, `question`, `answer`, `session_end`, `refusal`, `run_completed`, and `run_failed` kinds. `door` maps to `read-and-reason`. A question carries `question_id`, `stage`, `question`, `why_it_matters`, `hard_blocker`, and optional `options`; an answer carries `question_id`, `text`, and `skipped`. Only the matching reply clears the pending question. `blocked` / `stage_blocked` are explicit adapter extensions because the current ledger tailer does not publish the reference adapter’s blocked state. Supplied event IDs allow replay deduplication. Timestamps are preserved only when supplied.

When there are no session events, available stage judgments can populate progress with `source_artifact` provenance and no invented times. An `ask` judgment without the corresponding question event cannot supply an answer form; the backend must deliver the actual question. `proceed` and an intermediate `score` mean continuation, not a finished report. Transport errors and voluntary ending remain separate from scientific outcomes.

The missing backend must supply:

1. A chosen, verified authentication and execution architecture, with an explicit pre-transfer consent record. No current UI action sends credentials, DNA, answers, or a run request.
2. Real genotype intake, provenance, hygiene checks, normalization, ancestry projection, catalog evidence, and model judgments. This frontend performs none of those scientific decisions.
3. Durable jobs independent of the browser and the agent interaction lifetime, stable run/event IDs, ordered replay, status retrieval, reconnect support, and explicit cancellation/session-ending semantics. Ending the conversation must still allow the harness to record its evidence-based judgment and final result.
4. A question/answer bridge with acknowledged delivery and stale-question handling, gate acknowledgments and value decisions, plus changed recommendations on reselection. Local replies currently wait; they never pretend to have reached the agent.
5. Actual scoring inputs and reference data, measured coverage and completion checks, and a usable provider distribution with all INT-1 metadata. An unavailable provider must not be transformed into a scientific refusal.
6. A complete person-facing report and follow-up conversation. Medical-intent handling and clinician handoff need the live agent; the preview does not classify free-text medical questions or offer treatment advice.
7. Tested access control, token redaction, run isolation, artifact ownership, retention, deletion, and downloads bound to the right run. No security guarantee for a future connected deployment is implied by the local preview.

## Product findings that affected the screens

- **A first visit needs an explanation before intake.** FRAME-1 assumes no knowledge of genetics. The homepage defines the product, expected inputs, PRS, percentile, and possible outcomes before asking for a file. Intake explanations distinguish a raw DNA file from a summary report, a DNA reference map from an ancestry claim, and a research scoring formula from the AI assistant. These definitions explain the process without inventing personal findings.
- **Six user-facing capabilities, mixed internal vocabulary.** The prompt names six stages; `AGENT.md` calls intake a door and numbers only select through report. Skill examples use `proceed` alongside the core’s four outcomes. The frontend keeps six visible capabilities and normalizes only the presentation meaning, preserving original outcomes in history.
- **Refusal cannot depend on a successful interpretation.** The report skill says it is blocked until INT-1, yet explicitly requires delivery of selection and interpretation refusals. The refusal renderer therefore does not require score/reference artifacts.
- **Delivery is not a ledger entry.** Some harness paths can emit an upstream refusal or a terminal `refusal` event even for `stop`, without a refusal envelope. The frontend preserves the stated outcome and waits for proper person-facing delivery. It never rewrites technical reasons into an invented report.
- **The score schema is incomplete.** The report’s example has null score/placement values, and published performance/assay-specific limitations are primarily prose rather than typed fields. The renderer can carry the supplied personalized explanation, but cannot manufacture a missing published figure or verify it from the current report shape.
- **“Cost” is an evidence tradeoff.** It is not a quoted monetary price. The model page labels it as a tradeoff and shows the supplied explanation, while API billing and runtime remain unquoted until there is evidence.
- **Decision gates do not define a structured options contract.** `gate_type` exists, but the skill does not identify a machine-readable set of eligible options or a live decision prompt. The frontend provides a free-text value response; it does not offer every candidate as an admissible choice or ask the person to approve technical validity.
- **Estimated and measured coverage share a field name.** Using `resolved_weight_fraction` alone is unsafe. The result requires the measured tool provenance and checks consistency with the report; selection estimates remain explicitly labeled.
- **No principled frontend threshold for reselection.** “Materially worse” belongs to the agent. The interface preserves attempts and explanations instead of implementing its own numerical policy.
- **The wait is longer than the headline suggests.** README timing mainly concerns a run ending after selection. The score skill describes roughly 15–25 minutes per chromosome and hours overall. No fabricated timer or prediction is used. The current process-lifetime caveat also makes durable background execution a real backend requirement.
- **Ancestry disagreement is not a correction to identity.** Genetic distances and self-described race/ethnicity are different quantities. There is no posterior probability, fine-resolution ancestry chart, or control to choose which ancestry the math uses. The projection’s uncertain precision belongs in the agent’s explanation.
- **A missing provider differs from an unusable reference.** The former leaves interpretation unmade. The latter can justify INT-2 refusal. Provider minimum-cohort thresholds and discrete-versus-distance grouping rules remain explicit, not invented frontend defaults.
- **Reference matching remains a scientific limitation.** The interface contract notes the All of Us versus 1000 Genomes conflict, assay mismatch, and full-model versus resolved-subset parity. The score report preserves every supplied warning and shows source, version, cohort rule, and assay/parity details. An audit trail does not guarantee reproducibility.
- **Session ending and clearing are different actions.** An imported record that says the person left does not itself clear that record. The UI labels the recorded ending separately; the explicit exit action releases the preview’s in-memory data. A connected backend must also finalize its judgment without reclassifying the person’s choice as refusal or stop.
- **Exploration must not dilute the delivered report.** Required refusal copy and personalized limits remain visible. Optional topics identify whether their wording comes from the supplied artifacts or a general explanation; missing model-selection reasoning is stated as unavailable. Suggested questions are notes to keep, not prompts that claim a live agent will answer. No new score, reference, or personal medical conclusion is generated to create another interaction.

## Verification

Run the dependency-free checks:

```sh
npm test
node frontend/contracts.test.js
node frontend/exploration.test.js
node frontend/notebook.test.js
node frontend/session.test.js
```

All **51 unit tests pass**: 22 artifact-contract tests, 10 exploration tests, 10 notebook tests, and 9 event-reducer tests. The direct commands show the individual assertions (some Node environments summarize `--test` at the file level). Tests cover missing references, estimate-versus-measured provenance, inconsistent values and model identities, malformed shapes, stale reports conflicting with events, direct refusal delivery, outcome separation, questions and matching replies, reselection, event replay, and voluntary exit. Exploration checks cover direct source preservation, missing model reasoning, explicit example labels, separation of score and refusal topics, and the clinical handoff boundary. Notebook checks cover whitespace and duplicates, immutable edits, length and count limits, strict malformed imports, and preserving untrusted text for the rendering layer to escape. Syntax is checked with `node --check frontend/app.js`.

The visual identity uses a forest-green header, a larger lime DNA mark and wordmark, warm off-white surfaces, and a restrained lime accent across navigation and upload controls. A two-column welcome screen puts the introduction beside the upload card on laptops. Content expands to 1,040 px (1,160 px on the welcome screen), with responsive single-column layouts for smaller screens. The interface uses a rem-based type scale: 17 px base text, 16–20 px explanatory text and controls, and 14 px supporting metadata at the default browser font size. Main buttons are at least 52 px tall. Important privacy and result-limit text grows along with the headings; longer report paragraphs have a comfortable reading width. Keyboard focus stays visible against the dark header. Four navigation sections group the same screen flow; all six analysis stages remain in progress. Refusals lead directly with the supplied person-facing headline and retain every required envelope field.

Chromium checks pass on the current layout at desktop (1440 px) and mobile (390 px) widths. All 12 screens offered by the preview selector, including the homepage, render without horizontal overflow. Checks cover malformed imports across seven screens, both model gates, scored-report and question export/reimport, suppression without a reference, direct refusal wording, source labels, keyboard tabs, preserving unfinished question drafts while exploring, adding/editing/removing questions, duplicate rejection, escaped markup, standalone text downloads, clearing notes when the source changes or the session ends, and malformed note imports. Score exploration checks both supplied and absent model reasoning and carries the recorded warnings. No page errors, outbound requests, cookies, or browser storage were observed in these flows. Desktop and mobile screenshots were also inspected.

HTTP checks verified that the supplied server returns 404 for repository files and 405 for POST, and serves the frontend with the stated content security policy and no-store headers.

Six additional Chromium lifecycle checks pass: delayed imports cannot restore data after exit, a new genome selection, a newer import, or clearing; a stale read failure stays silent after exit; questions saved from an example retain their example label and filename after navigating to an empty result screen. Import generation checks invalidate stale reads before they can change application state.

The current layout was checked at 1,366×768, 1,440×900, 1,024×768, 683×768, 390×844, and 320×740. All 12 preview-selector screens fit without horizontal overflow. Privacy, percentile explanations, and refusal text retain the larger type. The file screen retains the 52 px file chooser. The report-exploration browser suite also passes after the homepage and copy changes, including keyboard tabs, question editing, downloads, reimport, and clearing. Laptop and mobile screenshots were inspected.

First-visit checks pass at all six widths: the homepage introduces the product before displaying a file input; site links reach their sections; FAQs open with the keyboard; the example route stays explicitly labeled; and returning home preserves the selected file when the person reopens the preview. On the tested laptops, both the homepage start action and the file screen’s chooser are visible without scrolling. The homepage adds no account connection, upload, or analysis service.

The local browser QA tooling, when present, lives under ignored `frontend/.qa/`; it is not a runtime dependency and is not served by the preview. All added application, test, documentation, and QA files are within the supplied repository directory.
