const $ = (q, el = document) => el.querySelector(q);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const paths = {
  log: "M3 5h18M3 12h18M3 19h12",
  coverage: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  digest: "M4 4h16v16H4zM7 8h10M7 12h10M7 16h6",
  review: "m5 12 4 4L19 6M4 21h16",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2",
  search: "m21 21-5-5M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14",
  arrow: "M4 12h16m-6-6 6 6-6 6",
  link: "M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2",
  refresh:
    "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2",
  close: "m6 6 12 12M6 18 18 6",
  check: "m5 12 4 4L19 6",
  clock: "M12 8v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18",
  plus: "M12 4v16M4 12h16",
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.log}"/></svg>`;
const label = (c) =>
  ({
    timeline: "Timeline",
    registry_date: "Registry dates",
    enrollment: "Enrollment",
    development_status: "Development",
    trial_design: "Trial design",
    partnership: "Partnership",
    milestone: "Milestone",
    results: "Results",
  })[c] || c;
const fmt = (v) =>
  v
    ? new Date(v).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : "Not yet checked";
const state = {
  page: "log",
  demo: false,
  category: "",
  followed: false,
  q: "",
  me: null,
  companies: [],
  studies: [],
  follows: [],
  changes: [],
  overview: {},
  tab: "companies",
  syncing: false,
};
const demo = [
  {
    id: "demo-1",
    company: "Meridian Therapeutics",
    ticker: "MRDN",
    drug: "MDN-201 · solid tumors",
    category: "timeline",
    title: "Readout moves to the second half of 2026",
    before_value: "H1 2026",
    after_value: "H2 2026",
    source: "company",
    detected_at: "2026-09-24",
    review_status: "approved",
    copy: "Updated company guidance shifts the expected readout window. The registry’s primary-completion date is a separate milestone.",
    reason: "No reason stated in the reviewed excerpt.",
  },
  {
    id: "demo-2",
    company: "Northstar Oncology",
    ticker: "NSTR",
    drug: "NS-104 · advanced melanoma",
    category: "enrollment",
    title: "Enrollment target increases by 60 participants",
    before_value: "180 participants (estimated)",
    after_value: "240 participants (estimated)",
    source: "registry",
    detected_at: "2026-09-24",
    review_status: "approved",
    copy: "The latest registry version expands planned enrollment. This change alone does not establish a later readout.",
  },
  {
    id: "demo-3",
    company: "Linden Bio",
    ticker: "LNDB",
    drug: "LDB-30 · hematologic malignancies",
    category: "development_status",
    title: "Phase 2 study is now recruiting",
    before_value: "Not yet recruiting",
    after_value: "Recruiting",
    source: "registry",
    detected_at: "2026-09-23",
    review_status: "approved",
    copy: "Recruitment status changed in the trial registry. No new company readout guidance is included in this example.",
  },
];
async function api(path, options = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-TrialDelta-Client": "web",
      ...options.headers,
    },
  });
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      data = { detail: "The server could not complete this request." };
    }
    throw new Error(
      Array.isArray(data.detail)
        ? data.detail.map((x) => x.msg).join("; ")
        : data.detail || "Request failed",
    );
  }
  return response.json();
}
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(
    () => $("#toast").classList.remove("show"),
    5000,
  );
}
async function load() {
  [
    state.overview,
    state.companies,
    state.studies,
    state.follows,
    state.changes,
  ] = await Promise.all(
    ["/overview", "/companies", "/studies", "/follows", "/changes"].map((p) =>
      api(p),
    ),
  );
}
function empty(title, copy, button = "") {
  return `<div class="empty">${icon("log")}<h3>${title}</h3><p>${copy}</p>${button}</div>`;
}
function heading(title, subtitle, actions = "") {
  return `<div class="page-heading"><div><p class="eyebrow">Oncology research workspace</p><h1>${title}</h1><p class="subtitle">${subtitle}</p></div><div class="actions">${actions}</div></div>`;
}
function shell() {
  const pages = [
    ["log", "Change log"],
    ["coverage", "Coverage"],
    ["digest", "Daily digest"],
    ...(state.me.is_admin
      ? [
          ["inbox", "Source inbox"],
          ["review", "Review queue"],
        ]
      : []),
    ["settings", "Settings"],
  ];
  $("#app").innerHTML =
    `<div class="shell"><aside class="sidebar"><div class="brand"><img src="/static/favicon.svg" alt=""><span>Trial<span>Delta</span></span></div><div class="workspace"><span>Oncology workspace</span><small>01</small></div><div class="nav-label">Research</div><nav class="nav" aria-label="Main navigation">${pages.map(([p, t]) => `<button data-page="${p}" class="${state.page === p ? "active" : ""}" ${state.page === p ? 'aria-current="page"' : ""}>${icon(p)}${t}${p === "review" ? `<span class="count">${state.overview.pending || 0}</span>` : ""}</button>`).join("")}</nav><div class="side-bottom"><strong>Evidence over noise.</strong><br>Every change, with its source.<div class="side-rule"></div>${state.me.mode === "local" ? "Local research workspace" : "Private research workspace"}<br><span>Prospective history · v0.1</span></div></aside><div class="workspace-main"><header class="topbar"><div class="breadcrumb">Workspace &nbsp;/&nbsp; <b>${pages.find((x) => x[0] === state.page)?.[1] || "Change log"}</b></div><div class="top-right"><div class="search">${icon("search")}<input id="search" aria-label="Search companies, programs, or changes" placeholder="Search companies, programs…" value="${esc(state.q)}"></div><div class="avatar" title="${esc(state.me.email)}">${state.me.mode === "local" ? "JD" : esc(state.me.email.slice(0, 2).toUpperCase())}</div></div></header><main id="main" class="content"></main></div></div>`;
  renderPage();
  $("#search").addEventListener("input", (e) => {
    state.q = e.target.value;
    clearTimeout(window.searchTimer);
    window.searchTimer = setTimeout(renderPage, 150);
  });
}
function card(c) {
  return `<article class="change-card"><div class="card-meta"><span class="ticker">${esc(c.ticker)}</span><span>${esc(c.company)}</span><span>·</span><span>${esc(c.drug || c.study_id || "Company guidance")}</span></div><div class="card-meta"><span class="badge ${c.category === "timeline" ? "amber" : c.category === "enrollment" ? "blue" : ""}">${label(c.category)}</span></div><h3 class="change-title">${esc(c.title)}</h3>${c.copy ? `<p class="change-copy">${esc(c.copy)}</p>` : ""}<div class="comparison"><div><label>Previous</label><strong>${esc(c.before_value)}</strong></div><span>→</span><div class="after"><label>Current</label><strong>${esc(c.after_value)}</strong></div></div><div class="card-footer"><span class="source-meta">${icon("check")} Reviewed &nbsp;·&nbsp; ${c.source === "registry" ? "2 registry snapshots" : "2 company sources"}</span><button class="text-button" data-change="${c.id}">View evidence &nbsp;↗</button></div></article>`;
}
function logPage() {
  let changes = state.demo ? demo : state.changes;
  if (state.category)
    changes = changes.filter((c) => c.category === state.category);
  if (state.q)
    changes = changes.filter((c) =>
      JSON.stringify(c).toLowerCase().includes(state.q.toLowerCase()),
    );
  if (state.followed && !state.demo)
    changes = changes.filter((c) => matchFollow(c));
  const o = state.overview;
  return `${heading("The change log.", "What changed, what it means, and where it came from.", `${state.me.is_admin ? `<button data-action="evidence">${icon("plus")} Add evidence</button><button class="primary" data-action="sync" ${state.syncing ? "disabled" : ""}>${icon("refresh")} ${state.syncing ? "Refreshing…" : "Refresh sources"}</button>` : ""}`)}<div class="stats"><div class="stat"><label>Companies in coverage</label><div class="stat-number">${o.companies || 0}<span class="stat-accent"> &nbsp; Oncology</span></div><small>Curated sponsor directory</small></div><div class="stat"><label>Trials captured</label><div class="stat-number">${o.studies || 0}</div><small>Primary-source registry records</small></div><div class="stat"><label>Reviewed changes</label><div class="stat-number">${o.changes || 0}</div><small>Evidence checked before publication</small></div><div class="stat"><label>Your follows</label><div class="stat-number">${o.follows || 0}</div><small>Companies, programs & research themes</small></div></div><div class="columns"><section><div class="section-head"><h2>Meaningful changes</h2><div class="segmented"><button data-mode="live" class="${!state.demo ? "selected" : ""}">Live workspace</button><button data-mode="demo" class="${state.demo ? "selected" : ""}">Explore demo</button></div></div>${state.demo ? '<div class="demo-banner"><b>Illustrative demo.</b> These companies, changes, and evidence excerpts are fictional. Live data and your digest remain separate.</div>' : ""}<div class="filters"><button data-filter="" class="${!state.category ? "selected" : ""}">All changes</button>${["timeline", "development_status", "enrollment", "trial_design", "registry_date"].map((c) => `<button data-filter="${c}" class="${state.category === c ? "selected" : ""}">${label(c)}</button>`).join("")}<span class="separator"></span><button data-action="followed" aria-pressed="${state.followed}" class="${state.followed ? "selected" : ""}">Following only</button></div><div class="feed-date"><span>${state.demo ? "Sample research brief" : fmt(new Date().toISOString())}</span><span>${changes.length} ${changes.length === 1 ? "change" : "changes"}</span></div>${changes.length ? changes.map(card).join("") : empty(state.q || state.category || state.followed ? "No matching changes" : "History starts with a baseline.", state.q || state.category || state.followed ? "Adjust your filters or follow more companies to broaden this view." : `${o.studies || 0} trials are currently captured. Future changes will appear here after review. Company guidance can be added with its source evidence.`, `<button data-page="${state.q || state.category || state.followed ? "coverage" : "coverage"}">Explore covered trials &nbsp;→</button>`)}${
    !changes.length && !state.demo && state.studies.length
      ? `<div class="section-head"><h2>Recently captured</h2><button class="text-button" data-action="programs">All trials &nbsp;→</button></div>${state.studies
          .slice(0, 3)
          .map(
            (s) =>
              `<article class="change-card"><div class="card-meta"><span class="ticker">${esc(s.ticker)}</span><span>${esc(s.id)}</span><span class="badge">Baseline</span></div><h3>${esc(s.title)}</h3><div class="card-footer"><span class="source-meta">${esc(s.phase.replaceAll("PHASE", "Phase "))} · ${esc(s.status.replaceAll("_", " ").toLowerCase())}</span><button class="text-button" data-study="${esc(s.id)}">Open trial &nbsp;↗</button></div></article>`,
          )
          .join("")}`
      : ""
  }</section><aside class="rail"><div class="rail-card"><div class="rail-head"><h2>Coverage pulse</h2>${icon("coverage")}</div>${state.companies
    .filter((c) => c.followed)
    .concat(state.companies.filter((c) => !c.followed))
    .slice(0, 5)
    .map(
      (c) =>
        `<div class="coverage-row"><span class="company-mark">${esc(c.ticker.slice(0, 2))}</span><div class="company-label"><b>${esc(c.name)}</b><small>${esc(c.ticker)} · ${c.study_count} trials</small></div><button class="text-button" data-company="${c.id}" aria-label="View ${esc(c.name)} trials">↗</button></div>`,
    )
    .join(
      "",
    )}<button class="text-button subtle-link" data-page="coverage">Manage your coverage &nbsp;→</button><p class="rail-note">${o.last_job?.finished_at ? `Last refresh ${fmt(o.last_job.finished_at)}. ${o.last_job.status === "partial" ? "Some sources need attention." : "Lead-sponsor matches only."}` : "Refresh sources to start collecting trial snapshots."}</p></div><div class="rail-card digest-rail"><p class="eyebrow">Your daily research brief</p><h2>Only the changes<br>worth your attention.</h2><p class="rail-note">Reviewed updates for the companies and programs you follow, with evidence attached.</p><button data-page="digest">Open daily digest &nbsp;→</button></div><div class="rail-card"><h2>Dates mean different things.</h2><p class="rail-note">A trial’s primary completion is not its expected readout. We preserve both, and say when the timing is uncertain.</p></div></aside></div><p class="footer-note">Primary sources. Preserved history. Research information, not investment or medical advice.</p>`;
}
function matchFollow(c) {
  const s = state.studies.find((s) => s.id === c.study_id);
  return state.follows.some((f) =>
    f.kind === "company"
      ? String(c.company_id) === f.value
      : f.kind === "program"
        ? c.study_id === f.value
        : s &&
          (
            {
              drug: s.drug.split(";").map((v) => v.trim()),
              target: s.targets,
              indication: s.conditions,
            }[f.kind] || []
          ).some((v) => v.toLowerCase() === f.value.toLowerCase()),
  );
}
function coveragePage() {
  let rows = state.tab === "companies" ? state.companies : state.studies;
  if (state.q)
    rows = rows.filter((r) =>
      JSON.stringify(r).toLowerCase().includes(state.q.toLowerCase()),
    );
  return `${heading("Your research universe.", "Follow companies, clinical programs, targets, and indications.", `<button data-action="follow-form">${icon("plus")} Follow a theme</button>`)}<div class="section-head"><div class="segmented"><button data-tab="companies" class="${state.tab === "companies" ? "selected" : ""}">Companies · ${state.companies.length}</button><button data-tab="programs" class="${state.tab === "programs" ? "selected" : ""}">Clinical programs · ${state.studies.length}</button></div><span class="subtitle">${rows.length} shown</span></div>${
    state.follows.filter((f) => !["company", "program"].includes(f.kind)).length
      ? `<div class="pill-list">${state.follows
          .filter((f) => !["company", "program"].includes(f.kind))
          .map(
            (f) =>
              `<span class="pill">${esc(f.kind)}: ${esc(f.value)}<button data-unfollow="${f.id}" aria-label="Unfollow ${esc(f.value)}">×</button></span>`,
          )
          .join("")}</div><br>`
      : ""
  }${rows.length ? `<div class="table-wrap"><table><thead><tr>${state.tab === "companies" ? "<th>Company</th><th>Coverage</th><th>Last check</th><th>Watchlist</th>" : "<th>Clinical program</th><th>Status</th><th>Primary completion</th><th>Watchlist</th>"}</tr></thead><tbody>${rows.map((r) => (state.tab === "companies" ? `<tr><td><button class="name-button" data-company="${r.id}">${esc(r.name)}</button><small>${esc(r.ticker)} · Oncology</small></td><td>${r.study_count} trials<small>${r.last_error ? "Refresh needs attention" : "Exact lead-sponsor matching"}</small></td><td>${fmt(r.last_checked)}${r.last_error ? '<small class="error">Last good snapshot retained</small>' : ""}</td><td><button class="small ${r.followed ? "" : "primary"}" data-follow-company="${r.id}">${r.followed ? "Following ✓" : "+ Follow"}</button></td></tr>` : `<tr><td class="table-title"><button class="name-button" data-study="${r.id}">${esc(r.title)}</button><small>${esc(r.ticker)} · ${esc(r.id)} · ${esc(r.phase.replaceAll("PHASE", "Phase "))}</small></td><td><span class="badge">${esc(r.status.replaceAll("_", " ").toLowerCase())}</span></td><td>${esc(r.primary_completion || "Not reported")}<small>Registry milestone · not readout</small></td><td><button class="small" data-follow-study="${r.id}">${state.follows.some((f) => f.kind === "program" && f.value === r.id) ? "Following ✓" : "+ Follow"}</button></td></tr>`)).join("")}</tbody></table></div>` : empty("No matching records", "Refresh the sources to collect a baseline, or adjust your search.")}<p class="footer-note">Directory tickers identify companies; they are not a current listing-status or ownership feed. Coverage excludes unmatched sponsors and collaborator-only records.</p>`;
}
async function renderPage() {
  const main = $("#main");
  if (!main) return;
  let page = state.page;
  if (page === "log") main.innerHTML = logPage();
  else if (page === "coverage") main.innerHTML = coveragePage();
  else {
    main.innerHTML = '<div class="loading">Loading…</div>';
    try {
      if (page === "digest") {
        const d = await api("/digest");
        if (state.page !== page) return;
        main.innerHTML = digestPage(d);
      }
      if (page === "review") {
        const r = await api("/review");
        if (state.page !== page) return;
        main.innerHTML = reviewPage(r);
      }
      if (page === "inbox") {
        const items = await api("/inbox");
        if (state.page !== page) return;
        state.inbox = items;
        main.innerHTML = inboxPage(items);
      }
      if (page === "settings") {
        const jobs = await api("/jobs");
        if (state.page !== page) return;
        main.innerHTML = settingsPage(jobs);
      }
    } catch (e) {
      main.innerHTML = `<p class="error">${esc(e.message)}</p>`;
    }
  }
}
function dialog(title, body, subtitle = "") {
  const d = $("#detail");
  $("#detail-content").innerHTML =
    `<div class="dialog-head"><div><h2>${title}</h2>${subtitle ? `<p class="dialog-subtitle">${subtitle}</p>` : ""}</div><button class="quiet small" data-action="close" aria-label="Close dialog">${icon("close")}</button></div><div class="dialog-body">${body}</div>`;
  if (!d.open) d.showModal();
}
function evidenceHTML(e) {
  return `<div class="evidence"><div class="card-meta"><span class="badge">${esc(e.label || e.source_type)}</span><span>Published ${esc(e.published_at || e.source_updated || "date unavailable")}</span></div><h3>${esc(e.title)}</h3><blockquote>${esc(e.quote)}</blockquote>${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">Open original source ↗</a>` : "<small>Fictional excerpt for demonstration only.</small>"}${e.source_type !== "registry" ? `<p class="subtitle">${e.reason ? `Stated reason: ${esc(e.reason)}` : e.reason_checked ? "Source checked: no reason stated." : "Reason not checked."}</p>` : ""}<p><small>Preserved ${fmt(e.captured_at)} ${e.precision ? `· Timing precision: ${esc(e.precision)}` : ""}</small></p></div>`;
}
async function showChange(id) {
  if (id.startsWith("demo")) {
    const c = demo.find((c) => c.id === id);
    dialog(
      esc(c.title),
      `<div class="demo-banner">Fictional example. This is not research about a real company.</div>${card(c)}${evidenceHTML({ label: "Previous", title: "Illustrative earlier company presentation", published_at: "2026-03-05", quote: `We expect to report ${c.drug.split(" · ")[0]} results in ${c.before_value}.` })}${evidenceHTML({ label: "Current", title: "Illustrative updated company presentation", published_at: "2026-08-06", quote: `We expect to report ${c.drug.split(" · ")[0]} results in ${c.after_value}.` })}<div class="notice">Registry primary completion and expected results readout are separate concepts. This demo does not establish a conflict.</div>`,
      `${esc(c.company)} · ${esc(c.drug)}`,
    );
    return;
  }
  const c = await api("/changes/" + id);
  dialog(
    esc(c.title),
    `${card(c)}${c.evidence.map(evidenceHTML).join("")}<h3>Source reconciliation</h3><div class="notice">${esc(c.reconciliation.message)}<br>${esc(c.reconciliation.note || "")}</div>`,
    `${esc(c.company)} · ${esc(c.study_id || "Company guidance")}`,
  );
}
async function showStudy(id) {
  const d = await api("/studies/" + id),
    s = d.study,
    r = d.reconciliation;
  dialog(
    esc(s.id),
    `<h3>${esc(s.title)}</h3><p class="subtitle">${esc(s.drug)}</p><br><div class="metadata"><div><span>Recruitment</span><strong>${esc(s.status.replaceAll("_", " "))}</strong></div><div><span>Phase</span><strong>${esc(s.phase || "Not reported")}</strong></div><div><span>Primary completion</span><strong>${esc(r.primary_completion?.date || "Not reported")} · ${esc(r.primary_completion?.type?.toLowerCase() || "uncertain")}</strong></div><div><span>Results publication</span><strong>${esc(r.results_posted?.date || "Not reported")}</strong></div></div><div class="notice">${esc(r.message)}<br>${esc(r.note)}</div><a href="https://clinicaltrials.gov/study/${esc(s.id)}" target="_blank" rel="noopener noreferrer">Open ClinicalTrials.gov ↗</a><h3>Company guidance</h3>${d.guidance.length ? d.guidance.map(evidenceHTML).join("") : '<p class="subtitle">No reviewed company guidance linked yet. Readout timing is uncertain.</p>'}<h3>Preserved registry versions</h3>${d.snapshots.map((v, i) => `<div class="coverage-row"><div class="company-label"><b>${i === d.snapshots.length - 1 ? "Initial baseline" : "Snapshot"} · ${fmt(v.captured_at)}</b><small>Source updated ${esc(v.source_updated || "date unavailable")}</small></div><a class="text-button" href="/api/snapshots/${v.id}">JSON ↓</a></div>`).join("")}<h3>Curated target tags</h3><p class="subtitle">${esc(JSON.parse(s.targets).join(", ") || "No reviewed target tags assigned.")}</p>${state.me.is_admin ? `<form id="target-form" data-study="${esc(s.id)}"><div class="field"><label for="targets">Targets (comma separated)</label><input id="targets" name="targets" value="${esc(JSON.parse(s.targets).join(", "))}"><small>Assign only targets supported by the source. Tags support exact matching in your watchlist.</small></div><button>Save target tags</button></form>` : ""}`,
  );
}
function evidenceForm() {
  dialog(
    "Record a source statement",
    `<p class="subtitle">Preserve the exact excerpt and the claim it supports. A reviewer must approve it before it changes the timeline.</p><br><form id="evidence-form"><div class="form-grid"><div class="field"><label for="company">Company</label><select id="company" name="company_id" required>${state.companies.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select></div><div class="field"><label for="study">Trial (optional)</label><select id="study" name="study_id"><option value="">Company-level statement</option></select></div><div class="field"><label for="source-type">Source type</label><select id="source-type" name="source_type"><option value="presentation">Company presentation</option><option value="press_release">Press release</option><option value="filing">Regulatory filing</option><option value="publication">Publication</option></select></div><div class="field"><label for="publication">Source publication date</label><input id="publication" type="date" name="published_at" required max="${new Date().toISOString().slice(0, 10)}"></div><div class="field full"><label for="title">Source title and page reference</label><input id="title" name="title" required minlength="3" maxlength="500" placeholder="Q2 corporate presentation, page 14"></div><div class="field full"><label for="url">Original source URL</label><input id="url" type="url" name="url" required placeholder="https://investors.company.com/…"></div><div class="field full"><label for="quote">Exact supporting excerpt</label><textarea id="quote" name="quote" required minlength="10" maxlength="20000" placeholder="Paste the original wording, including timing qualifiers."></textarea></div><div class="field"><label for="category">Change category</label><select id="category" name="category">${["timeline", "development_status", "trial_design", "enrollment", "partnership", "milestone", "results"].map((c) => `<option value="${c}">${label(c)}</option>`).join("")}</select></div><div class="field"><label for="value">Supported value / timing window</label><input id="value" name="value" required maxlength="1000" placeholder="e.g. H2 2026"><small>Use H1/H2, Q1–Q4, YYYY-MM, or original uncertain wording.</small></div><div class="field full"><label for="reason">Reason stated by the source (if any)</label><input id="reason" name="reason" maxlength="2000"></div></div><label class="check-label"><input type="checkbox" name="reason_checked">I checked the source for an explanation of the change.</label><p class="error" id="form-error"></p><button class="primary" type="submit">Save to review queue</button></form>`,
  );
  const update = () => {
    $("#study").innerHTML =
      '<option value="">Company-level statement</option>' +
      state.studies
        .filter((s) => String(s.company_id) === $("#company").value)
        .map(
          (s) =>
            `<option value="${s.id}">${esc(s.id)} · ${esc(s.title.slice(0, 80))}</option>`,
        )
        .join("");
  };
  $("#company").onchange = update;
  update();
}
function followForm() {
  dialog(
    "Follow a research theme",
    `<form id="follow-form"><div class="field"><label for="follow-kind">Follow type</label><select id="follow-kind" name="kind"><option value="drug">Drug / intervention</option><option value="target">Target</option><option value="indication">Indication</option></select></div><div class="field"><label for="follow-value">Name</label><input id="follow-value" name="value" list="theme-options" required maxlength="200"><datalist id="theme-options"></datalist><small>Names match exact registry terms or curated target tags, ignoring case.</small></div><p class="error" id="form-error"></p><button class="primary">Add to watchlist</button></form>`,
  );
  const update = () => {
    const kind = $("#follow-kind").value;
    const values = [
      ...new Set(
        state.studies.flatMap((s) =>
          kind === "drug"
            ? s.drug.split(";").map((v) => v.trim())
            : kind === "target"
              ? s.targets
              : s.conditions,
        ),
      ),
    ]
      .filter(Boolean)
      .sort();
    $("#theme-options").innerHTML = values
      .map((v) => `<option value="${esc(v)}">`)
      .join("");
  };
  $("#follow-kind").onchange = update;
  update();
}
function digestPage(d) {
  return `${heading("Your daily brief.", "A quiet, reviewed account of what changed in your watchlist.", `<a class="text-button" href="/api/digest/export?day=${d.day}">Download digest ↓</a>`)}<div class="digest-paper"><div class="paper-head"><p class="eyebrow">TrialDelta · ${fmt(d.day + "T12:00:00")}</p><h2>${d.changes.length ? `${d.changes.length} changes worth a closer look.` : "No reviewed changes today."}</h2><p class="subtitle">${d.follow_count} follows · approved updates · evidence included</p></div>${d.changes.length ? d.changes.map(card).join("") : '<p class="subtitle">Your digest includes changes approved today for the companies, programs, or themes you follow. Initial baselines and unreviewed claims do not generate alerts.</p><br><button data-page="coverage">Manage your watchlist →</button>'}<div class="notice">${esc(d.note)}</div><div class="field"><label for="digest-day">Browse another day</label><input type="date" id="digest-day" value="${d.day}"></div></div>`;
}
function reviewPage(r) {
  return `${heading("A second look.", "Check the evidence before a change reaches the daily digest.", `<button class="primary" data-action="evidence">${icon("plus")} Add source evidence</button>`)}<div class="review-panel">${!r.changes.length && !r.evidence.length ? empty("The review queue is clear.", "New registry differences and manually recorded company statements will appear here. The first approved statement establishes a baseline. Link statements to the same trial to detect guidance changes; company-level statements are archived without automatic comparisons.") : ""}${r.evidence.map((e) => `<article class="review-card"><div class="card-meta"><span class="ticker">${esc(e.ticker)}</span><span class="badge amber">Company statement</span><span>${label(e.category)}</span></div><h3>${esc(e.title)}</h3><p>${esc(e.published_at)} · ${esc(e.value)} · ${esc(e.precision)}</p><blockquote>${esc(e.quote)}</blockquote><a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">Inspect source ↗</a><p>${e.reason ? `Stated reason: ${esc(e.reason)}` : e.reason_checked ? "Recorder checked: no reason stated." : "Reason has not been checked."}</p><div class="actions"><button class="primary" data-review="evidence:${e.id}:approve">Approve evidence</button><button data-review="evidence:${e.id}:reject">Reject</button></div></article>`).join("")}${r.changes.map((c) => `<article class="review-card"><div class="card-meta"><span class="ticker">${esc(c.ticker)}</span><span class="badge blue">Registry difference</span><span>${esc(c.study_id)}</span></div><h3>${esc(c.title)}</h3><div class="comparison"><div><label>Previous</label><strong>${esc(c.before_value)}</strong></div><span>→</span><div class="after"><label>Current</label><strong>${esc(c.after_value)}</strong></div></div><div class="actions"><button class="primary" data-review="change:${c.id}:approve">Approve change</button><button data-review="change:${c.id}:reject">Dismiss as noise</button><button class="text-button" data-change="${c.id}">Inspect snapshots ↗</button></div></article>`).join("")}</div>`;
}
function settingsPage(jobs) {
  return `${heading("Workspace settings.", "Collection, evidence standards, and your account.")}<div class="settings-grid"><section class="settings-card"><h2>Collection schedule</h2><p>The daily worker refreshes registry records every 24 hours while running. In-app digests update as reviews are approved. Email delivery covers the previous completed UTC day.</p><p>Coverage: oncology studies attributed to an exact normalized lead sponsor. Missing or failed responses never imply a trial was removed.</p><p class="mono">python -m trialdelta.worker</p>${state.me.is_admin ? '<button data-action="sync">Refresh sources now</button>' : ""}</section><section class="settings-card"><h2>Your digest</h2><p>Daily digests are available in this workspace. Email requires a configured mail server and the worker’s explicit delivery option.</p><label class="check-label"><input id="digest-pref" type="checkbox" ${state.me.digest_enabled ? "checked" : ""}>Include this account in email delivery when enabled</label><p class="subtitle">${esc(state.me.email)}</p>${state.me.mode === "hosted" ? '<br><button data-action="logout">Sign out</button>' : ""}</section><section class="settings-card"><h2>Evidence standards</h2><ul><li>Baseline collection creates no change alerts.</li><li>Registry dates retain their original precision.</li><li>Company claims require a quote and a primary source.</li><li>Every published change has a reviewer.</li><li>Fictional examples stay outside your live database.</li></ul></section><section class="settings-card"><h2>Recent collection runs</h2>${
    jobs.length
      ? jobs
          .slice(0, 5)
          .map(
            (j) =>
              `<div class="coverage-row"><div class="company-label"><b>${esc(j.status)}</b><small>${fmt(j.started_at)}${j.summary ? ` · ${esc(JSON.parse(j.summary).studies || 0)} studies` : ""}</small></div>${icon(j.status === "complete" ? "check" : "clock")}</div>`,
          )
          .join("")
      : "<p>No collections have run yet.</p>"
  }</section>${state.me.is_admin ? `<section class="settings-card"><h2>Invite a researcher</h2><p>Create a private reader account for hosted mode. Readers have their own watchlists and cannot publish evidence.</p><form id="user-form"><div class="field"><label for="email">Email</label><input id="email" name="email" type="email" required></div><div class="field"><label for="password">Initial password</label><input id="password" name="password" type="password" minlength="14" required autocomplete="new-password"></div><p class="error" id="form-error"></p><button>Create account</button></form></section>` : ""}</div>`;
}
async function toggleFollow(kind, value) {
  const existing = state.follows.find(
    (f) => f.kind === kind && f.value === String(value),
  );
  if (existing) await api("/follows/" + existing.id, { method: "DELETE" });
  else
    await api("/follows", {
      method: "POST",
      body: JSON.stringify({ kind, value: String(value) }),
    });
  await load();
  renderPage();
  toast(existing ? "Removed from watchlist" : "Added to watchlist");
}
async function refresh() {
  if (state.syncing) return;
  await api("/sync", { method: "POST" });
  state.syncing = true;
  renderPage();
  toast("Source refresh started. You can keep researching.");
  clearInterval(window.syncTimer);
  window.syncTimer = setInterval(async () => {
    try {
      const overview = await api("/overview");
      if (overview.last_job && overview.last_job.status !== "running") {
        clearInterval(window.syncTimer);
        state.syncing = false;
        await load();
        renderPage();
        toast(
          overview.last_job.status === "complete"
            ? "Registry refresh complete"
            : "Refresh finished; some sources need attention.",
        );
      }
    } catch (e) {
      clearInterval(window.syncTimer);
      state.syncing = false;
      toast(e.message);
    }
  }, 5000);
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.page) {
      state.page = b.dataset.page;
      state.q = "";
      state.demo = false;
      shell();
      return;
    }
    if (b.dataset.mode) {
      state.demo = b.dataset.mode === "demo";
      renderPage();
      return;
    }
    if ("filter" in b.dataset) {
      state.category = b.dataset.filter;
      renderPage();
      return;
    }
    if (b.dataset.tab) {
      state.tab = b.dataset.tab;
      renderPage();
      return;
    }
    if (b.dataset.change) {
      await showChange(b.dataset.change);
      return;
    }
    if (b.dataset.study) {
      await showStudy(b.dataset.study);
      return;
    }
    if (b.dataset.company) {
      state.page = "coverage";
      state.tab = "programs";
      state.q = state.companies.find(
        (c) => c.id === Number(b.dataset.company),
      ).ticker;
      shell();
      return;
    }
    if (b.dataset.followCompany) {
      await toggleFollow("company", b.dataset.followCompany);
      return;
    }
    if (b.dataset.followStudy) {
      await toggleFollow("program", b.dataset.followStudy);
      return;
    }
    if (b.dataset.unfollow) {
      await api("/follows/" + b.dataset.unfollow, { method: "DELETE" });
      await load();
      renderPage();
      return;
    }
    if (b.dataset.candidate) {
      const item = state.inbox.find(
        (x) => x.id === Number(b.dataset.candidate),
      );
      evidenceForm();
      $("#company").value = item.company_id;
      $("#company").dispatchEvent(new Event("change"));
      $("#title").value = item.title;
      $("#url").value = item.url;
      $("#publication").value = (item.published_at || "").slice(0, 10);
      $("#source-type").value = "press_release";
      return;
    }
    if (b.dataset.dismiss) {
      await api("/inbox/" + b.dataset.dismiss + "/dismiss", { method: "POST" });
      renderPage();
      return;
    }
    if (b.dataset.review) {
      b.disabled = true;
      const [kind, id, action] = b.dataset.review.split(":");
      const result = await api(`/review/${kind}/${id}`, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      await load();
      shell();
      toast(
        result.change_created
          ? "Approved. A guidance change was published."
          : result.status === "approved"
            ? "Approved. Evidence history updated."
            : "Item rejected.",
      );
      return;
    }
    switch (b.dataset.action) {
      case "import-news":
        b.disabled = true;
        const imported = await api("/inbox/import", { method: "POST" });
        toast(
          imported.status === "complete"
            ? `${imported.imported} new source candidates imported`
            : imported.detail,
        );
        renderPage();
        break;
      case "close":
        $("#detail").close();
        break;
      case "evidence":
        evidenceForm();
        break;
      case "follow-form":
        followForm();
        break;
      case "sync":
        await refresh();
        break;
      case "followed":
        state.followed = !state.followed;
        renderPage();
        break;
      case "programs":
        state.page = "coverage";
        state.tab = "programs";
        shell();
        break;
      case "logout":
        await api("/logout", { method: "POST" });
        location.reload();
        break;
    }
  } catch (err) {
    b.disabled = false;
    toast(err.message);
  }
});
document.addEventListener("change", async (e) => {
  try {
    if (e.target.id === "digest-day") {
      const d = await api("/digest?day=" + e.target.value);
      $("#main").innerHTML = digestPage(d);
    }
    if (e.target.id === "digest-pref") {
      const r = await api("/digest/preferences", {
        method: "PUT",
        body: JSON.stringify({ enabled: e.target.checked }),
      });
      state.me.digest_enabled = r.enabled;
      toast(
        r.smtp_configured
          ? "Preference saved"
          : "Preference saved. Email server is not configured.",
      );
    }
  } catch (err) {
    toast(err.message);
  }
});
document.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target,
    b = $('button[type="submit"],button', f);
  if (b) b.disabled = true;
  const data = Object.fromEntries(new FormData(f));
  try {
    switch (f.id) {
      case "evidence-form":
        data.company_id = Number(data.company_id);
        data.study_id = data.study_id || null;
        data.reason = data.reason || null;
        data.reason_checked = !!data.reason_checked;
        await api("/evidence", { method: "POST", body: JSON.stringify(data) });
        $("#detail").close();
        state.page = "review";
        await load();
        shell();
        toast("Evidence saved for review");
        break;
      case "follow-form":
        await api("/follows", { method: "POST", body: JSON.stringify(data) });
        $("#detail").close();
        await load();
        renderPage();
        toast("Theme added to watchlist");
        break;
      case "target-form":
        await api("/studies/" + f.dataset.study + "/targets", {
          method: "PUT",
          body: JSON.stringify({
            targets: data.targets
              .split(",")
              .map((v) => v.trim())
              .filter(Boolean),
          }),
        });
        await load();
        toast("Target tags saved");
        break;
      case "user-form":
        await api("/users", { method: "POST", body: JSON.stringify(data) });
        f.reset();
        toast("Researcher account created");
        break;
      case "login-form":
        await api("/login", { method: "POST", body: JSON.stringify(data) });
        await boot();
        break;
    }
  } catch (err) {
    const error = $("#form-error", f);
    if (error) error.textContent = err.message;
    else toast(err.message);
  } finally {
    if (b) b.disabled = false;
  }
});
$("#detail").addEventListener("click", (e) => {
  if (e.target === $("#detail")) $("#detail").close();
});
function loginPage() {
  $("#app").innerHTML =
    `<main class="login"><div class="brand"><img src="/static/favicon.svg" alt="">TrialDelta</div><h1>Welcome back.</h1><p class="subtitle">Sign in to your research workspace.</p><br><form id="login-form"><div class="field"><label for="email">Email</label><input type="email" id="email" name="email" required autocomplete="username"></div><div class="field"><label for="password">Password</label><input type="password" id="password" name="password" required autocomplete="current-password"></div><p class="error" id="form-error"></p><button class="primary">Sign in</button></form></main>`;
}
async function boot() {
  try {
    state.me = await api("/me");
    await load();
    shell();
    const change = new URLSearchParams(location.search).get("change");
    if (change && /^\d+$/.test(change)) {
      try {
        await showChange(change);
      } catch (e) {
        toast(e.message);
      }
    }
  } catch (e) {
    if (e.message === "Please sign in") loginPage();
    else
      $("#app").innerHTML =
        `<main class="login"><h1>Workspace unavailable</h1><p class="error">${esc(e.message)}</p><p>Check that the TrialDelta server and database are running, then reload.</p></main>`;
  }
}
boot();

function inboxPage(items) {
  return `${heading("Start with the source.", "Headlines to investigate. They are not verified claims or change alerts.", `<button class="primary" data-action="import-news">Import source candidates</button>`)}<div class="notice">This optional inbox reads your existing news pipeline. Open each source, locate the primary document, and preserve an exact supporting excerpt. Syndicated headlines alone do not establish a change.</div><div class="review-panel">${
    items.length
      ? items
          .filter(
            (i) =>
              !state.q ||
              JSON.stringify(i).toLowerCase().includes(state.q.toLowerCase()),
          )
          .map(
            (i) =>
              `<article class="review-card"><div class="card-meta"><span class="ticker">${esc(i.ticker)}</span><span>${esc(i.source)}</span><span>${fmt(i.published_at)}</span><span class="badge amber">Unverified discovery</span></div><h3>${esc(i.title)}</h3><br><div class="actions"><a href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">Open source ↗</a><button data-candidate="${i.id}">Record evidence</button><button class="quiet" data-dismiss="${i.id}">Dismiss</button></div></article>`,
          )
          .join("")
      : empty(
          "Your source inbox is clear.",
          "Import source candidates from the optional news database, or record a primary source directly.",
        )
  } </div>`;
}

// Optional proposed WebMCP integration. All actions use the same authenticated API as the UI.
const agentToolsLifecycle = new AbortController();
window.addEventListener("pagehide", () => agentToolsLifecycle.abort(), {
  once: true,
});
if (document.modelContext?.registerTool) {
  const tools = [
    {
      name: "list_trialdelta_changes",
      title: "List reviewed TrialDelta changes",
      description:
        "Read approved changes from the live workspace, excluding fictional examples.",
      inputSchema: {
        type: "object",
        properties: {},
        additionalProperties: false,
      },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      async execute(input) {
        if (!input || Object.keys(input).length)
          throw new Error("Expected an empty object");
        return (await api("/changes")).map((c) => ({
          id: c.id,
          company: c.company,
          title: c.title,
          previous: c.before_value,
          current: c.after_value,
        }));
      },
    },
    {
      name: "follow_trialdelta_company",
      title: "Follow a company",
      description:
        "Add a company to the current signed-in user’s watchlist and refresh the visible workspace. Idempotent.",
      inputSchema: {
        type: "object",
        properties: { company_id: { type: "integer", minimum: 1 } },
        required: ["company_id"],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      async execute(input) {
        if (
          !input ||
          !Number.isInteger(input.company_id) ||
          input.company_id < 1 ||
          Object.keys(input).some((k) => k !== "company_id")
        )
          throw new Error("A positive integer company_id is required");
        const result = await api("/follows", {
          method: "POST",
          body: JSON.stringify({
            kind: "company",
            value: String(input.company_id),
          }),
        });
        await load();
        await renderPage();
        return {
          follow_id: result.id,
          company_id: input.company_id,
          followed: true,
        };
      },
    },
  ];
  for (const tool of tools) {
    try {
      Promise.resolve(
        document.modelContext.registerTool(tool, {
          signal: agentToolsLifecycle.signal,
        }),
      ).catch(() => {});
    } catch {}
  }
}
