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
  home: "m3 10 9-7 9 7M5 9v12h14V9M9 21v-8h6v8",
  log: "M3 5h18M3 12h18M3 19h12",
  coverage: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  digest: "M4 4h16v16H4zM7 8h10M7 12h10M7 16h6",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2",
  search: "m21 21-5-5M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14",
  arrow: "M4 12h16m-6-6 6 6-6 6",
  link: "M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2",
  close: "m6 6 12 12M6 18 18 6",
  check: "m5 12 4 4L19 6",
  clock: "M12 8v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18",
  plus: "M12 4v16M4 12h16",
  account: "M20 21v-2a7 7 0 0 0-14 0v2M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8",
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
    : "Date unavailable";
const phaseLabel = (value) => value === "NA" ? "Not applicable" : (value || "Phase not reported").replaceAll("PHASE", "Phase ");
const trialCountLabel = (count = 0) => `${count} ${count === 1 ? "trial" : "trials"}`;
const HOME_PAGE_SIZE = 10;
const FILING_PAGE_SIZES = [10, 25, 50, 100];
const HOME_TABS = { trials: "Trials", news: "News", tads: "Target action dates", filings: "8-Ks", calendar: "Calendar" };
const homeFeedKey = () => state.homeTab === "calendar" ? "tads" : state.homeTab;
function currentCalendarMonth() {
  const today = new Date();
  return `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}`;
}
const CHANGE_PAGE_SIZE = 10;
const state = {
  page: "home",
  homeTab: "trials",
  calendarMonth: currentCalendarMonth(),
  homePages: { trials: 1, news: 1, tads: 1, filings: 1 },
  homeFeeds: { trials: null, news: null, tads: null, filings: null },
  homeLoading: { trials: false, news: false, tads: false, filings: false },
  homeErrors: { trials: "", news: "", tads: "", filings: "" },
  filingsPageSize: HOME_PAGE_SIZE,
  filingsRequest: 0,
  demo: false,
  category: "",
  followed: true,
  changePage: 1,
  changeLoading: false,
  changeError: "",
  changeRequest: 0,
  q: "",
  me: null,
  companies: [],
  studies: [],
  follows: [],
  changes: [],
  tab: "companies",
  authConfig: { auth_required: true, public_browsing_enabled: false, registration_enabled: false, email_delivery_available: false, password_min_length: 14 },
  authView: "login",
  authToken: "",
  authEpoch: 0,
  returnPage: "home",
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
    company: "Northstar Therapeutics",
    ticker: "NSTR",
    drug: "NS-104 · type 2 diabetes",
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
    drug: "LDB-30 · rheumatoid arthritis",
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
function isGuest() {
  return state.me?.is_guest === true;
}
function publicBrowsingEnabled() {
  return state.authConfig.auth_required && state.authConfig.public_browsing_enabled === true;
}
async function api(path, options = {}) {
  const epoch = state.authEpoch;
  const publicRequest = path.startsWith("/auth/") || path === "/login";
  if (isGuest() && !publicRequest && ((options.method || "GET").toUpperCase() !== "GET" || /^\/(?:account|follows|digest)(?:[/?]|$)/.test(path))) {
    const error = new Error("Sign in to use your personal research workspace.");
    error.status = 401;
    throw error;
  }
  const response = await fetch("/api" + path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-TrialDelta-Client": "web",
      ...options.headers,
    },
  });
  if (epoch !== state.authEpoch) {
    const error = new Error("This session has changed. Please try again.");
    error.stale = true;
    throw error;
  }
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      data = { detail: "The server could not complete this request." };
    }
    if (epoch !== state.authEpoch) {
      const error = new Error("This session has changed. Please try again.");
      error.stale = true;
      throw error;
    }
    const error = new Error(
      Array.isArray(data.detail)
        ? data.detail.map((x) => x.msg).join("; ")
        : data.detail || "Request failed",
    );
    error.status = response.status;
    if (response.status === 401 && !publicRequest && state.me && !isGuest()) {
      state.returnPage = state.page;
      setRoute(state.page, true);
      clearUserState();
      loginPage("login", "Your session has expired. Sign in to continue.");
    }
    throw error;
  }
  const data = await response.json();
  if (epoch !== state.authEpoch) {
    const error = new Error("This session has changed. Please try again.");
    error.stale = true;
    throw error;
  }
  return data;
}
function clearUserState() {
  state.authEpoch += 1;
  Object.assign(state, {
    me: null, companies: [], studies: [], follows: [], changes: [],
    homeFeeds: { trials: null, news: null, tads: null, filings: null }, homeLoading: { trials: false, news: false, tads: false, filings: false },
    homeErrors: { trials: "", news: "", tads: "", filings: "" }, homePages: { trials: 1, news: 1, tads: 1, filings: 1 },
    filingsPageSize: HOME_PAGE_SIZE,
    q: "", category: "", followed: true, changePage: 1, changeLoading: false, changeError: "", demo: false, homeTab: "trials", tab: "companies", calendarMonth: currentCalendarMonth(),
  });
  clearTimeout(window.searchTimer);
  clearTimeout(window.toastTimer);
  $("#toast").classList.remove("show");
  $("#toast").textContent = "";
  $("#detail").close();
  $("#detail-content").innerHTML = "";
}
const customerPages = ["home", "digest", "log", "coverage", "settings", "account"];
const publicPages = ["home", "log", "coverage"];
const authPages = ["login", "signup", "forgot-password", "reset-password", "verify-email"];
function visitorIdentity() {
  // Visitors have no account record, session, or shared watchlist.
  return { is_guest: true, is_admin: false };
}
function signInButtons() {
  return `<button class="primary" data-auth="login">Sign in</button>${state.authConfig.registration_enabled ? '<button data-auth="signup">Create account</button>' : ""}`;
}
function inviteSignIn(message = "Sign in to save companies and trials to your personal watchlist.") {
  state.returnPage = state.page;
  dialog("Make this research your own.", `<p class="subtitle">${esc(message)}</p><div class="session-actions">${signInButtons()}</div><button class="text-button" data-action="close">Continue browsing</button>`);
}
function visitorPage(page) {
  const copy = {
    account: ["Your account.", "Create an account to save your profile, preferences, and personal watchlist."],
    digest: ["Your daily brief.", "Sign in to see a daily brief for the companies, trials, and themes you follow."],
    settings: ["Workspace settings.", "Sign in to manage your research preferences and personal workspace."],
  }[page];
  return `${heading(copy[0], copy[1])}<section class="settings-card visitor-invite"><p class="eyebrow">Browsing as a visitor</p><h2>Keep your research connected.</h2><p>You can browse trials, companies, news, and the change log without an account. Sign in when you are ready to save your own research.</p><div class="session-actions">${signInButtons()}</div><button class="text-button" data-action="browse">Continue browsing &nbsp;→</button></section>`;
}
function setRoute(page, replace = false) {
  const url = new URL(location.href);
  url.hash = page;
  history[replace ? "replaceState" : "pushState"]({}, "", url);
}
const accountChannel = typeof BroadcastChannel === "function" ? new BroadcastChannel("trialdelta-account") : null;
function notifyAccountChange() {
  accountChannel?.postMessage("auth-changed");
}
if (accountChannel) accountChannel.onmessage = async (event) => {
  if (event.data !== "auth-changed") return;
  clearUserState();
  state.authToken = "";
  $("#app").innerHTML = '<div class="boot" role="status">Updating your session…</div>';
  await boot();
};
function accountInitials() {
  const name = state.me.display_name?.trim();
  return name ? name.split(/\s+/).map((part) => part[0]).slice(0, 2).join("").toUpperCase()
    : (state.me.email || "U").slice(0, 2).toUpperCase();
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
    state.companies,
    state.studies,
    state.follows,
    state.changes,
  ] = await Promise.all(
    [api("/companies"), api("/studies"), isGuest() ? Promise.resolve([]) : api("/follows"), api(`/changes?followed=${state.followed}`)],
  );
}
function empty(title, copy, button = "") {
  return `<div class="empty">${icon("log")}<h3>${title}</h3><p>${copy}</p>${button}</div>`;
}
function heading(title, subtitle, actions = "") {
  return `<div class="page-heading"><div><p class="eyebrow">Clinical research workspace</p><h1>${title}</h1><p class="subtitle">${subtitle}</p></div><div class="actions">${actions}</div></div>`;
}
function shell() {
  const pages = [
    ["home", "Home"],
    ["digest", "Daily digest"],
    ["log", "Change log"],
    ["coverage", "Coverage"],
    ["settings", "Settings"],
    ["account", "Account"],
  ];
  $("#app").innerHTML =
    `<div class="shell ${isGuest() ? "visitor-shell" : ""}"><aside class="sidebar"><button class="brand brand-home" data-page="home" aria-label="TrialDelta home"><img src="/static/favicon.svg" alt=""><span>Trial<span>Delta</span></span></button><div class="workspace"><span>Research workspace</span><small>01</small></div><div class="nav-label">Research</div><nav class="nav" aria-label="Main navigation">${pages.map(([p, t]) => `<button data-page="${p}" class="${state.page === p ? "active" : ""}" ${state.page === p ? 'aria-current="page"' : ""}>${icon(p)}${t}</button>`).join("")}</nav><div class="side-bottom"><strong>Evidence over noise.</strong><br>Every change, with its source.<div class="side-rule"></div>${isGuest() ? "Browsing as a visitor" : state.me.mode === "local" ? "Local research workspace" : "Private research workspace"}<br><span>Prospective history · v0.1</span></div></aside><div class="workspace-main"><header class="topbar"><div class="breadcrumb">Workspace &nbsp;/&nbsp; <b>${pages.find((x) => x[0] === state.page)?.[1] || "Home"}</b></div><div class="top-right"><div class="search">${icon("search")}<input id="search" aria-label="Search companies, programs, news, or changes" placeholder="Search companies, programs…" value="${esc(state.q)}"></div><button class="home-logo" data-page="home" aria-label="Go to Home" title="Home"><img src="/static/favicon.svg" alt=""></button>${isGuest() ? `<div class="visitor-actions"><span class="visitor-label">Visitor</span><button class="text-button" data-auth="login">Sign in</button>${state.authConfig.registration_enabled ? '<button class="small primary" data-auth="signup">Create account</button>' : ""}</div>` : `<button class="avatar" data-page="account" title="Your account" aria-label="Your account: ${esc(state.me.display_name || state.me.email)}">${esc(accountInitials())}</button>${state.authConfig.auth_required ? '<button class="text-button top-signout" data-action="logout">Sign out</button>' : ""}`}</div></header><main id="main" class="content" tabindex="-1"></main></div></div>`;
  document.title = `${pages.find((x) => x[0] === state.page)?.[1] || "Home"} · TrialDelta`;
  renderPage();
  if (state.page === "log" && !state.demo) refreshChanges();
  $("#search").addEventListener("input", (e) => {
    state.q = e.target.value;
    state.changePage = 1;
    state.homePages = { trials: 1, news: 1, tads: 1, filings: 1 };
    resetHomeFilings();
    clearTimeout(window.searchTimer);
    window.searchTimer = setTimeout(renderPage, 150);
  });
}
function card(c) {
  return `<article class="change-card"><div class="card-meta"><span class="ticker">${esc(c.ticker)}</span><span>${esc(c.company)}</span><span>·</span><span>${esc(c.drug || c.study_id || "Company guidance")}</span></div><div class="card-meta"><span class="badge ${c.category === "timeline" ? "amber" : c.category === "enrollment" ? "blue" : ""}">${label(c.category)}</span><time datetime="${esc(c.detected_at || "")}" title="Change detected">${fmt(c.detected_at)}</time></div><h3 class="change-title">${esc(c.title)}</h3>${c.copy ? `<p class="change-copy">${esc(c.copy)}</p>` : ""}<div class="comparison"><div><label>Previous</label><strong>${esc(c.before_value)}</strong></div><span>→</span><div class="after"><label>Current</label><strong>${esc(c.after_value)}</strong></div></div><div class="card-footer"><span class="source-meta">${icon("link")} ${c.source === "registry" ? "Registry update &nbsp;·&nbsp; 2 registry snapshots" : "Company update &nbsp;·&nbsp; 2 company sources"}</span><button class="text-button" data-change="${c.id}">View evidence &nbsp;↗</button></div></article>`;
}

function researchRail() {
  return `<aside class="rail"><div class="rail-card"><div class="rail-head"><h2>Companies to explore</h2>${icon("coverage")}</div>${state.companies.filter((c) => c.followed).concat(state.companies.filter((c) => !c.followed)).slice(0, 5).map((c) => `<div class="coverage-row"><span class="company-mark">${esc(c.ticker.slice(0, 2))}</span><div class="company-label"><b>${esc(c.name)}</b><small>${esc(c.ticker)} · ${c.followed ? "Following" : esc(trialCountLabel(c.study_count))}</small></div><button class="text-button" data-company="${c.id}" aria-label="View ${esc(c.name)} trials">↗</button></div>`).join("") || '<p class="rail-note">Company profiles will appear here when available.</p>'}<button class="text-button subtle-link" data-page="coverage">Explore companies &nbsp;→</button></div>
    <div class="rail-card digest-rail"><p class="eyebrow">Your daily research brief</p><h2>Only the changes<br>worth your attention.</h2><p class="rail-note">Updates for the companies and trials you follow, with source records attached.</p><button data-page="digest">Open daily digest &nbsp;→</button></div><div class="rail-card"><h2>Dates mean different things.</h2><p class="rail-note">A trial’s primary completion is not its expected readout. We preserve both, and say when the timing is uncertain.</p></div></aside>`;
}

function safeNewsUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password
      ? url.href
      : "";
  } catch {
    return "";
  }
}

function homeTrialCard(s) {
  const phase = phaseLabel(s.phase);
  const status = (s.status || "Status not reported").replaceAll("_", " ").toLowerCase();
  return `<article class="change-card home-trial-card"><div class="card-meta"><span class="ticker">${esc(s.ticker)}</span><span>${esc(s.company)}</span><span>·</span><span>${esc(s.id)}</span></div><h3 class="change-title"><button class="feed-title" data-study="${esc(s.id)}">${esc(s.title)}</button></h3><div class="card-meta"><span class="badge blue">${esc(phase)}</span><span class="badge">${esc(status)}</span></div>${s.drug ? `<p class="change-copy">${esc(s.drug)}</p>` : ""}<div class="card-footer"><span class="source-meta">${icon("clock")}<span>Added to workspace <time datetime="${esc(s.added_at)}">${fmt(s.added_at)}</time></span></span><button class="text-button" data-study="${esc(s.id)}">Open trial &nbsp;↗</button></div></article>`;
}

function homeNewsCard(n) {
  const url = safeNewsUrl(n.url);
  return `<article class="change-card home-news-card"><div class="card-meta"><span class="ticker">${esc(n.ticker)}</span>${n.company && n.company !== n.ticker ? `<span>${esc(n.company)}</span>` : ""}<span class="badge">Biotechnology</span></div><h3 class="change-title">${url ? `<a class="feed-title" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : esc(n.title)}</h3><div class="card-footer"><span class="source-meta"><span>${esc(n.source || "News")} · <time datetime="${esc(n.published_at || "")}">${fmt(n.published_at)}</time></span></span>${url ? `<a class="text-button" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Read article &nbsp;↗</a>` : ""}</div></article>`;
}

// Calendar components are not timestamps. Formatting in UTC keeps the reported
// day stable in every reader's time zone and preserves a month-only result.
function formatTad(value, precision) {
  const pattern = precision === "month" ? /^(\d{4})-(\d{2})$/ : precision === "day" ? /^(\d{4})-(\d{2})-(\d{2})$/ : null;
  const parts = pattern?.exec(value || "");
  if (!parts) return "Date unavailable";
  const year = Number(parts[1]), month = Number(parts[2]), day = Number(parts[3] || 1);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return "Date unavailable";
  return date.toLocaleDateString(undefined, {
    month: precision === "month" ? "long" : "short", year: "numeric",
    ...(precision === "day" ? { day: "numeric" } : {}), timeZone: "UTC",
  });
}

function homeTadCard(row) {
  const edited = row.is_edited === true || row.evidence_method === "edited";
  const manual = (row.evidence_method || row.entry_method) === "manual";
  const entryDetails = edited || manual;
  const identified = row.status === "identified";
  const title = identified ? formatTad(row.tad, row.precision) : row.status === "ambiguous" ? "Ambiguous date" : "Date unidentifiable";
  const status = identified ? row.precision === "month" ? "Month reported" : "Day reported" : "Needs review";
  const sources = Number.isInteger(row.source_count) && row.source_count > 0 ? row.source_count : 1;
  const manualCount = Number.isInteger(row.manual_count) && row.manual_count > 0 ? row.manual_count : 1;
  const ticker = row.ticker || row.symbol || "";
  const filingUrl = safeNewsUrl(row.filing_link);
  // The saved filing timestamp supplies source metadata only. Preserve its
  // calendar date rather than shifting it into the reader's local time zone.
  const filedDay = /^(\d{4}-\d{2}-\d{2})(?:[T ]|$)/.exec(row.filed_at || "")?.[1];
  const filedLabel = filedDay ? formatTad(filedDay, "day") : row.filed_at;
  const sourceMetadata = manual ? '<p class="change-copy">Entered manually.</p>' : `${row.accession_no ? `<p class="change-copy"><strong>Accession:</strong> ${esc(row.accession_no)}</p>` : ""}${row.filed_at ? `<p class="change-copy"><strong>Filed:</strong> <time datetime="${esc(row.filed_at)}" title="${esc(row.filed_at)}">${esc(filedLabel)}</time></p>` : ""}`;
  const sourceLabel = row.entry_method === "mixed"
    ? `${sources} source ${sources === 1 ? "filing" : "filings"} · ${manualCount} manual ${manualCount === 1 ? "entry" : "entries"}`
    : row.entry_method === "manual" || manual ? "Manual entry" : `SEC filing${row.form_type ? ` · ${esc(row.form_type)}` : ""} · ${sources} source ${sources === 1 ? "filing" : "filings"}`;
  const evidence = row.context ? `${entryDetails ? '<p class="change-copy"><strong>Notes:</strong></p>' : ""}<blockquote>${esc(row.context)}</blockquote>` : `<p class="change-copy">${entryDetails ? "No notes were saved for this entry." : "No excerpt was saved for this result."}</p>`;
  const originalUrl = edited ? safeNewsUrl(row.original_filing_link) : "";
  const originalDate = row.original_tad ? formatTad(row.original_tad, /^\d{4}-\d{2}$/.test(row.original_tad) ? "month" : "day") : "Not identified";
  const originalEvidence = edited ? `<div class="tad-original-evidence"><p class="change-copy"><strong>Original saved date:</strong> ${esc(originalDate)}</p><p class="change-copy"><strong>Original saved evidence:</strong></p>${row.original_context ? `<blockquote>${esc(row.original_context)}</blockquote>` : '<p class="change-copy">No original evidence was saved.</p>'}${row.entry_method === "manual" ? '<p class="change-copy">Entered manually.</p>' : sourceMetadata}${originalUrl ? `<a class="text-button" href="${esc(originalUrl)}" target="_blank" rel="noopener noreferrer" title="Open original saved source">Open original source &nbsp;↗</a>` : ""}</div>` : "";
  return `<article class="change-card home-tad-card"><div class="card-meta"><span class="ticker">${esc(ticker)}</span>${row.company && row.company !== ticker ? `<span>${esc(row.company)}</span>` : ""}<span class="badge ${identified ? "blue" : "amber"}">${status}</span>${edited ? '<span class="badge">Edited entry</span>' : ""}</div><h3 class="change-title">${identified ? `<time datetime="${esc(row.tad)}">${esc(title)}</time>` : title}</h3>${row.reason ? `<p class="change-copy">${esc(row.reason)}</p>` : ""}<details class="tad-evidence"><summary>${entryDetails ? "View entry details" : "View filing excerpt"}</summary>${edited ? '<p class="change-copy">Administrator correction.</p>' : ""}${evidence}${row.matched_text && !entryDetails ? `<p class="change-copy"><strong>Date expression:</strong> ${esc(row.matched_text)}</p>` : ""}${edited ? originalEvidence : sourceMetadata}</details><div class="card-footer"><span class="source-meta">${sourceLabel}</span>${filingUrl ? `<a class="text-button" href="${esc(filingUrl)}" target="_blank" rel="noopener noreferrer" title="${entryDetails ? "Open entry source" : "Open original SEC filing"}">${entryDetails ? "Open source" : "Open original filing"} &nbsp;↗</a>` : ""}</div></article>`;
}

function homeFilingCard(row) {
  const url = safeNewsUrl(row.filing_link);
  const filedDay = /^(\d{4}-\d{2}-\d{2})(?:[T ]|$)/.exec(row.filed_at || "")?.[1];
  const filedLabel = filedDay ? formatTad(filedDay, "day") : "Date unavailable";
  return `<article class="change-card home-filing-card"><div class="card-meta"><span class="ticker">${esc(row.ticker || "Unknown ticker")}</span>${row.company && row.company !== row.ticker ? `<span>${esc(row.company)}</span>` : ""}<span class="badge blue">${esc(row.form_type)}</span></div><h3 class="change-title">${esc(row.ticker || row.company || "Company")} · Form 8-K</h3><p class="change-copy"><strong>Accession:</strong> ${esc(row.accession_no || "Not supplied")}</p><div class="card-footer"><span class="source-meta">${icon("clock")}<span>Filed ${row.filed_at ? `<time datetime="${esc(row.filed_at)}" title="${esc(row.filed_at)}">${esc(filedLabel)}</time>` : "date unavailable"}</span></span>${url ? `<a class="text-button" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open filing &nbsp;↗</a>` : '<span class="source-meta">Filing link unavailable</span>'}</div></article>`;
}

function homePagination(total, page, pageCount, pageSize = HOME_PAGE_SIZE) {
  if (pageCount <= 1 && state.homeTab !== "filings") return "";
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);
  const links = [];
  let previous = 0;
  const pages = [...new Set([1, page - 1, page, page + 1, pageCount])]
    .filter(number => number >= 1 && number <= pageCount).sort((a, b) => a - b);
  for (const number of pages) {
    if (previous && number - previous > 1)
      links.push('<span class="pagination-gap" aria-hidden="true">…</span>');
    links.push(`<button data-home-page="${number}" aria-label="Go to page ${number}" ${number === page ? 'aria-current="page"' : ""}>${number}</button>`);
    previous = number;
  }
  return `<div class="feed-pagination"><p class="pagination-summary" role="status">Showing ${first}–${last} of ${total} ${state.homeTab === "trials" ? "trials" : state.homeTab === "news" ? "articles" : state.homeTab === "filings" ? "filings" : "results"}</p><nav aria-label="${HOME_TABS[state.homeTab]} pagination"><button data-home-page="${page - 1}" ${page === 1 ? "disabled" : ""}>Previous</button>${links.join("")}<button data-home-page="${page + 1}" ${page === pageCount ? "disabled" : ""}>Next</button></nav></div>`;
}

function resetHomeFilings(resetPage = true) {
  // An older page/search response must never replace the latest selection.
  state.filingsRequest += 1;
  if (resetPage) state.homePages.filings = 1;
  state.homeFeeds.filings = null;
  state.homeErrors.filings = "";
  state.homeLoading.filings = false;
}

function filingsFeedContent() {
  if (state.demo) return empty("8-Ks are not included in the demo.", "Open the live workspace to view saved 8-K filings.");
  const controls = `<div class="home-feed-tools filing-feed-tools"><label for="home-filings-page-size">Filings per page</label><select id="home-filings-page-size">${FILING_PAGE_SIZES.map(size => `<option value="${size}"${size === state.filingsPageSize ? " selected" : ""}>${size}</option>`).join("")}</select></div>`;
  if (state.homeErrors.filings)
    return controls + empty("Unable to load 8-Ks", esc(state.homeErrors.filings), '<button data-action="retry-home">Try again</button>');
  const feed = state.homeFeeds.filings;
  if (feed === null) return controls + '<div class="loading" role="status">Loading 8-K filings…</div>';
  if (!feed.items.length) return controls + empty(state.q.trim() ? "No matching 8-Ks" : "No 8-K filings yet.", state.q.trim() ? "Try another ticker, company, or accession number." : "Saved 8-K filings will appear here when available.");
  return `${controls}<div class="feed-date"><span>Latest 8-K filings</span><span>Newest filed first</span></div>${feed.items.map(homeFilingCard).join("")}${homePagination(feed.total, feed.page, feed.total_pages, feed.page_size)}`;
}

async function loadHomeFilings() {
  if (state.demo || state.homeFeeds.filings !== null || state.homeLoading.filings || state.homeErrors.filings) return;
  const epoch = state.authEpoch;
  const request = ++state.filingsRequest;
  const params = new URLSearchParams({ page: state.homePages.filings, page_size: state.filingsPageSize, q: state.q.trim() });
  state.homeLoading.filings = true;
  try {
    const feed = await api(`/home/filings?${params}`);
    if (epoch !== state.authEpoch || request !== state.filingsRequest) return;
    state.homeFeeds.filings = feed;
    state.homePages.filings = feed.page;
  } catch (e) {
    if (epoch !== state.authEpoch || request !== state.filingsRequest) return;
    state.homeErrors.filings = e.message;
  } finally {
    if (epoch !== state.authEpoch || request !== state.filingsRequest) return;
    state.homeLoading.filings = false;
    if (state.page === "home" && state.homeTab === "filings") {
      const focusSize = document.activeElement?.id === "home-filings-page-size";
      const panel = $("#home-feed-panel");
      if (panel) panel.innerHTML = filingsFeedContent();
      if (focusSize) $("#home-filings-page-size")?.focus({ preventScroll: true });
    }
  }
}

function matchingHomeRows(rows) {
  const query = state.q.trim().toLowerCase();
  return rows.filter((row) => !query || [row.title, row.company, row.ticker, row.symbol, row.id,
    row.drug, row.source, row.tad, row.context, row.matched_text, row.reason, row.status,
    row.accession_no, row.original_tad, row.original_context, ...(row.conditions || [])]
      .some((value) => String(value || "").toLowerCase().includes(query)));
}

function calendarEvent(row) {
  const ticker = row.ticker || row.symbol || row.company || "Filing";
  return `<button class="calendar-event" data-calendar-tad="${esc(row.id)}" aria-label="${esc(ticker)} · ${esc(formatTad(row.tad, row.precision))}" title="${esc(row.company || ticker)} · ${esc(formatTad(row.tad, row.precision))}">${esc(ticker)}</button>`;
}

function calendarContent() {
  if (state.demo) return empty("Calendar is not included in the demo.", "Open the live workspace to view saved target action dates.");
  const controls = `<div class="home-feed-tools"><button class="small" data-action="refresh-tads" ${state.homeLoading.tads || (state.homeFeeds.tads === null && !state.homeErrors.tads) ? "disabled" : ""}>Refresh dates</button></div>`;
  if (state.homeErrors.tads) return controls + empty("Unable to load calendar", esc(state.homeErrors.tads), '<button data-action="retry-home">Try again</button>');
  if (state.homeFeeds.tads === null) return controls + '<div class="loading" role="status">Loading the latest updates…</div>';
  const month = state.calendarMonth;
  const [year, monthNumber] = month.split("-").map(Number);
  const first = new Date(0);
  first.setUTCFullYear(year, monthNumber - 1, 1);
  const last = new Date(0);
  last.setUTCFullYear(year, monthNumber, 0);
  const monthLabel = formatTad(month, "month");
  const rows = matchingHomeRows(state.homeFeeds.tads);
  const identified = rows.filter(row => row.status === "identified");
  const days = identified.filter(row => row.precision === "day" && /^\d{4}-\d{2}-\d{2}$/.test(row.tad || "") && row.tad.startsWith(month + "-"));
  const monthOnly = identified.filter(row => row.precision === "month" && row.tad === month);
  const unresolved = rows.filter(row => row.status !== "identified");
  const cells = [];
  const cellCount = Math.ceil((first.getUTCDay() + last.getUTCDate()) / 7) * 7;
  const today = new Date();
  for (let index = 0; index < cellCount; index++) {
    const day = index - first.getUTCDay() + 1;
    if (day < 1 || day > last.getUTCDate()) {
      cells.push('<td class="calendar-outside" aria-hidden="true"></td>');
      continue;
    }
    const key = `${month}-${String(day).padStart(2, "0")}`;
    const events = days.filter(row => row.tad === key);
    const isToday = month === currentCalendarMonth() && day === today.getDate();
    cells.push(`<td data-calendar-day="${key}"${isToday ? ' class="calendar-today"' : ""}><time class="calendar-day-number" datetime="${key}"${isToday ? ' aria-current="date"' : ""}>${day}</time><div class="calendar-events">${events.map(calendarEvent).join("")}</div></td>`);
  }
  const weeks = [];
  for (let index = 0; index < cells.length; index += 7) weeks.push(`<tr>${cells.slice(index, index + 7).join("")}</tr>`);
  return `${controls}<div class="calendar-toolbar"><h3 id="calendar-month-label">${esc(monthLabel)}</h3><div class="calendar-navigation"><button class="small" data-calendar-month="-1" aria-label="Previous month" ${month === "0001-01" ? "disabled" : ""}>←</button><button class="small" data-calendar-month="0">This month</button><button class="small" data-calendar-month="1" aria-label="Next month" ${month === "9999-12" ? "disabled" : ""}>→</button></div></div><table class="tad-calendar" aria-labelledby="calendar-month-label"><thead><tr>${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map(day => `<th scope="col">${day}</th>`).join("")}</tr></thead><tbody>${weeks.join("")}</tbody></table><p class="calendar-count subtitle" role="status">${days.length} exact ${days.length === 1 ? "date" : "dates"} · ${monthOnly.length} month-only ${monthOnly.length === 1 ? "date" : "dates"} in ${esc(monthLabel)}${state.q.trim() ? " matching your search" : ""}.</p>${!days.length && !monthOnly.length ? '<p class="subtitle">No reported target action dates for this month.</p>' : ""}<section class="calendar-month-only" aria-label="Month-only target action dates"><h3>Month reported · day not specified</h3>${monthOnly.length ? `<div class="calendar-month-events">${monthOnly.map(calendarEvent).join("")}</div>` : '<p class="subtitle">No month-only dates reported for this month.</p>'}</section>${unresolved.length ? `<details class="calendar-undated"><summary>Unresolved dates (${unresolved.length})</summary><p class="subtitle">These mentions cannot be placed on a calendar day.</p>${unresolved.map(homeTadCard).join("")}</details>` : ""}`;
}

function homeFeedContent() {
  if (state.homeTab === "calendar") return calendarContent();
  if (state.homeTab === "filings") return filingsFeedContent();
  const tab = state.homeTab;
  const name = HOME_TABS[tab].toLowerCase();
  if (state.demo && tab === "tads")
    return empty("Target action dates are not included in the demo.", "Open the live workspace to view saved target action dates.");
  const controls = tab === "tads" ? `<div class="home-feed-tools"><button class="small" data-action="refresh-tads" ${state.homeLoading.tads || (state.homeFeeds.tads === null && !state.homeErrors.tads) ? "disabled" : ""}>Refresh dates</button></div>` : "";
  if (state.homeErrors[tab])
    return controls + empty(`Unable to load ${name}`, esc(state.homeErrors[tab]), '<button data-action="retry-home">Try again</button>');
  if (state.homeFeeds[tab] === null)
    return controls + '<div class="loading" role="status">Loading the latest updates…</div>';
  const query = state.q.trim().toLowerCase();
  const rows = matchingHomeRows(state.homeFeeds[tab]);
  if (!rows.length)
    return controls + empty(
      query ? `No matching ${name}` : tab === "trials" ? "New trials will appear here." : tab === "news" ? "No biotech news yet." : "No target action dates yet.",
      query ? "Try another company, program, or keyword." : tab === "trials"
        ? "Explore the companies in your workspace while you wait for new clinical programs."
        : tab === "news" ? "The latest available headlines for biotechnology companies will appear here."
        : "Saved target action dates, manual entries, and unresolved date mentions from SEC filings will appear here.",
      query ? "" : '<button data-page="coverage">Explore companies &nbsp;→</button>',
    );
  const pageCount = Math.ceil(rows.length / HOME_PAGE_SIZE);
  const page = Math.min(Math.max(state.homePages[tab], 1), pageCount);
  state.homePages[tab] = page;
  const first = (page - 1) * HOME_PAGE_SIZE;
  const pageRows = rows.slice(first, first + HOME_PAGE_SIZE);
  const renderer = { trials: homeTrialCard, news: homeNewsCard, tads: homeTadCard }[tab];
  return `${controls}<div class="feed-date"><span>${tab === "trials" ? "Recently added trials" : tab === "news" ? "Latest biotech headlines" : "Reported target action dates"}</span><span>${tab === "tads" ? "Latest dates first · unresolved last" : "Newest first"}</span></div>${pageRows.map(renderer).join("")}${homePagination(rows.length, page, pageCount)}`;
}

async function loadHomeFeed() {
  const epoch = state.authEpoch;
  const tab = homeFeedKey();
  if (tab === "filings") return loadHomeFilings();
  if ((state.demo && tab === "tads") || state.homeFeeds[tab] !== null || state.homeLoading[tab] || state.homeErrors[tab]) return;
  state.homeLoading[tab] = true;
  try {
    state.homeFeeds[tab] = await api(`/home/${tab}`);
  } catch (e) {
    if (epoch !== state.authEpoch) return;
    state.homeErrors[tab] = e.message;
  } finally {
    if (epoch !== state.authEpoch) return;
    state.homeLoading[tab] = false;
    if (state.page === "home" && homeFeedKey() === tab) {
      const panel = $("#home-feed-panel");
      if (panel) panel.innerHTML = homeFeedContent();
    }
  }
}

function homePage() {
  const notes = {
    trials: "Clinical programs, ordered by when they were first added to your workspace.",
    news: "The latest published headlines for biotechnology companies.",
    tads: "Dates reported in SEC filings and entered manually, including month-only dates and unresolved mentions. A reported date may have changed in a later filing.",
    filings: "Saved 8-K filings, ordered by filing date with the newest first. Search by ticker, company, or accession number.",
    calendar: "Reported target action dates by month. Month-only dates are listed separately; a reported date may have changed in a later filing.",
  };
  return `${heading("Home.", "The latest trials, biotechnology news, SEC filings, and reported target action dates, in one place.")}<div class="columns"><section class="home-feed"><div class="section-head"><h2>Latest updates</h2></div><div class="filters home-tabs" role="tablist" aria-label="Home feeds">${Object.entries(HOME_TABS).map(([tab, title]) => `<button id="home-tab-${tab}" role="tab" data-home-tab="${tab}" aria-controls="home-feed-panel" aria-selected="${state.homeTab === tab}" tabindex="${state.homeTab === tab ? 0 : -1}" class="${state.homeTab === tab ? "selected" : ""}">${title}</button>`).join("")}</div><p class="subtitle home-feed-note">${notes[state.homeTab]}</p><div id="home-feed-panel" role="tabpanel" aria-labelledby="home-tab-${state.homeTab}" tabindex="0">${homeFeedContent()}</div></section>${researchRail()}</div>`;
}


async function refreshChanges() {
  const request = ++state.changeRequest;
  const epoch = state.authEpoch;
  state.changeLoading = true;
  state.changeError = "";
  if (state.page === "log" && !state.demo) renderPage();
  try {
    const changes = await api(`/changes?followed=${state.followed}`);
    if (request !== state.changeRequest || epoch !== state.authEpoch) return;
    state.changes = changes;
  } catch (error) {
    if (request !== state.changeRequest || epoch !== state.authEpoch || error.stale) return;
    state.changeError = error.message;
  } finally {
    if (request === state.changeRequest && epoch === state.authEpoch) {
      state.changeLoading = false;
      if (state.page === "log" && !state.demo && state.me) renderPage();
    }
  }
}

function changePagination(total, page, pageCount) {
  if (pageCount <= 1) return "";
  const links = [];
  let previous = 0;
  for (let number = 1; number <= pageCount; number++) {
    if (number !== 1 && number !== pageCount && Math.abs(number - page) > 1) continue;
    if (previous && number - previous > 1)
      links.push('<span class="pagination-gap" aria-hidden="true">…</span>');
    links.push(`<button data-change-page="${number}" aria-label="Go to page ${number}" ${number === page ? 'aria-current="page"' : ""}>${number}</button>`);
    previous = number;
  }
  return `<div class="feed-pagination"><p class="pagination-summary" role="status">Showing ${(page - 1) * CHANGE_PAGE_SIZE + 1}–${Math.min(page * CHANGE_PAGE_SIZE, total)} of ${total} changes</p><nav aria-label="Change log pagination"><button data-change-page="${page - 1}" ${page === 1 ? "disabled" : ""}>Previous</button>${links.join("")}<button data-change-page="${page + 1}" ${page === pageCount ? "disabled" : ""}>Next</button></nav></div>`;
}

function changeFeedContent() {
  if (!state.demo && state.changeLoading)
    return '<div class="loading" role="status">Loading changes…</div>';
  if (!state.demo && state.changeError)
    return empty("Unable to load changes", esc(state.changeError), '<button data-action="retry-changes">Try again</button>');
  const query = state.q.trim().toLowerCase();
  const changes = (state.demo ? demo : state.changes)
    .filter((c) => !state.category || c.category === state.category)
    .filter((c) => !query || [c.company, c.ticker, c.study_id, c.study_title, c.drug, c.title, c.before_value, c.after_value, label(c.category)]
      .some((value) => String(value || "").toLowerCase().includes(query)))
    .slice().sort((a, b) => Date.parse(b.detected_at) - Date.parse(a.detected_at) || Number(b.id) - Number(a.id));
  if (!changes.length) {
    state.changePage = 1;
    const noFollows = !state.demo && state.followed && !state.follows.length;
    const filtered = query || state.category;
    const title = noFollows ? "Follow companies or trials to start." : filtered ? "No matching changes" : state.followed && !state.demo ? "No changes in your watchlist yet." : "No changes yet.";
    const copy = noFollows ? "Build your watchlist to see its updates here, or turn off Following only to explore all tracked changes."
      : filtered ? "Try another category or search, or turn off Following only to broaden the feed."
      : "New registry changes will appear here automatically as they are collected. Each update includes the previous and current source records.";
    return empty(title, copy, '<button data-page="coverage">Explore companies and trials &nbsp;→</button>');
  }
  const pageCount = Math.ceil(changes.length / CHANGE_PAGE_SIZE);
  const page = Math.min(Math.max(state.changePage, 1), pageCount);
  state.changePage = page;
  const rows = changes.slice((page - 1) * CHANGE_PAGE_SIZE, page * CHANGE_PAGE_SIZE);
  return `<div class="feed-date"><span>${state.demo ? "Sample research brief" : "Newest first"}</span><span>${state.demo ? "Fictional examples" : state.followed ? "Your watchlist" : "All tracked changes"}</span></div>${rows.map(card).join("")}${changePagination(changes.length, page, pageCount)}`;
}

function logPage() {
  return `${heading("The change log.", isGuest() ? "Explore changes across tracked companies and clinical trials." : "What changed for the companies and trials you follow.", `<button class="primary" data-action="evidence">${icon("plus")} Add evidence</button>`)}
    <div class="research-intro"><div class="research-intro-icon">${icon("coverage")}</div><div><h2>${isGuest() ? "Explore the evidence." : "Built around your research."}</h2><p>${isGuest() ? "Browse registry changes and source records. Sign in to follow the companies, trials, and themes that matter to you." : "Follow the companies, trials, and themes that matter to you. Their changes appear here automatically, with the source behind every update."}</p></div><button data-page="coverage">${isGuest() ? "Explore companies" : "Explore your watchlist"} &nbsp;→</button></div>
    <div class="columns"><section><div class="section-head"><h2>Meaningful changes</h2><div class="segmented"><button data-mode="live" class="${!state.demo ? "selected" : ""}">Live workspace</button><button data-mode="demo" class="${state.demo ? "selected" : ""}">Explore demo</button></div></div>${state.demo ? '<div class="demo-banner"><b>Illustrative demo.</b> These companies, changes, and evidence excerpts are fictional. Live data and your digest remain separate.</div>' : ""}
    <div class="filters"><button data-filter="" aria-pressed="${!state.category}" class="${!state.category ? "selected" : ""}">All changes</button>${["timeline", "development_status", "enrollment", "trial_design", "registry_date"].map((c) => `<button data-filter="${c}" aria-pressed="${state.category === c}" class="${state.category === c ? "selected" : ""}">${label(c)}</button>`).join("")}<span class="separator"></span><button data-action="followed" aria-pressed="${state.followed}" class="${state.followed ? "selected" : ""}" ${state.demo ? 'disabled title="Following applies to the live workspace"' : ""}>Following only</button></div>
    <div id="change-feed-panel" tabindex="-1" aria-label="Change log feed">${changeFeedContent()}</div>
    </section>${researchRail()}</div><p class="footer-note">Primary sources. Preserved history. Research information, not investment or medical advice.</p>`;
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

function coverageBadge(company) {
  if (!company || !["delisted", "out_of_sector"].includes(company.coverage_status)) return "";
  const text = company.coverage_status === "delisted" ? "Delisted" : "Outside Healthcare";
  return `<span class="badge coverage-inactive">${text}</span>`;
}

function coverageNotice(company) {
  const badge = coverageBadge(company);
  return badge ? `<div class="notice">${esc(company.name || company.company || company.ticker)} ${badge}<br>Automatic collection is inactive. Saved trials and source history remain available.</div>` : "";
}

function coveragePage() {
  let rows = state.tab === "companies" ? state.companies : state.studies;
  if (state.q)
    rows = rows.filter((r) =>
      (state.tab === "companies" ? `${r.name} ${r.ticker}` : JSON.stringify(r)).toLowerCase().includes(state.q.toLowerCase()),
    );
  return `${heading(isGuest() ? "Explore the research universe." : "Your research universe.", isGuest() ? "Browse companies and clinical programs. Sign in to build your personal watchlist." : "Follow companies, clinical programs, targets, and indications.", `<button data-action="follow-form">${icon("plus")} Follow a theme</button>`)}<div class="section-head"><div class="segmented"><button data-tab="companies" class="${state.tab === "companies" ? "selected" : ""}">Companies</button><button data-tab="programs" class="${state.tab === "programs" ? "selected" : ""}">Clinical programs</button></div></div>${
    state.follows.filter((f) => !["company", "program"].includes(f.kind)).length
      ? `<div class="pill-list">${state.follows.filter((f) => !["company", "program"].includes(f.kind)).map((f) => `<span class="pill">${esc(f.kind)}: ${esc(f.value)}<button data-unfollow="${f.id}" aria-label="Unfollow ${esc(f.value)}">×</button></span>`).join("")}</div><br>`
      : ""
  }${state.tab === "programs" ? coverageNotice(state.companies.find((c) => c.ticker.toLowerCase() === state.q.trim().toLowerCase())) : ""}${rows.length ? `<div class="table-wrap"><table><thead><tr>${state.tab === "companies" ? "<th>Company</th><th>Trials captured</th><th>Programs</th><th>Watchlist</th>" : "<th>Clinical program</th><th>Status</th><th>Primary completion</th><th>Watchlist</th>"}</tr></thead><tbody>${rows.map((r) => state.tab === "companies" ? `<tr><td><button class="name-button" data-company="${r.id}">${esc(r.name)}</button><small>${esc(r.ticker)} ${coverageBadge(r)}</small></td><td>${esc(r.study_count ?? 0)}</td><td><button class="text-button" data-company="${r.id}">Explore programs ↗</button></td><td><button class="small ${r.followed ? "" : "primary"}" data-follow-company="${r.id}">${r.followed ? "Following ✓" : "+ Follow"}</button></td></tr>` : `<tr><td class="table-title"><button class="name-button" data-study="${r.id}">${esc(r.title)}</button><small>${esc(r.ticker)} · ${esc(r.id)} · ${esc(phaseLabel(r.phase))} ${coverageBadge(r)}</small></td><td><span class="badge">${esc(r.status.replaceAll("_", " ").toLowerCase())}</span></td><td>${esc(r.primary_completion || "Not reported")}<small>Registry milestone · not readout</small></td><td><button class="small" data-follow-study="${r.id}">${state.follows.some((f) => f.kind === "program" && f.value === r.id) ? "Following ✓" : "+ Follow"}</button></td></tr>`).join("")}</tbody></table></div>` : empty(state.q ? "No matching companies or programs" : "More research to explore soon.", state.q ? "Try a different company, drug, or clinical program in your search." : "Company profiles and clinical programs will appear here when available. You can still follow a research theme.")}<p class="footer-note">Coverage tags reflect the latest market coverage refresh. Delisted and out-of-sector companies retain their saved trial history. Clinical programs are associated with their lead sponsor.</p>`;
}

async function renderPage() {
  const main = $("#main");
  if (!main || !state.me || !main.closest(".shell")) return;
  let page = state.page;
  if (isGuest() && !publicPages.includes(page)) {
    main.innerHTML = visitorPage(page);
    return;
  }
  if (page === "home") {
    main.innerHTML = homePage();
    await loadHomeFeed();
  } else if (page === "log") main.innerHTML = logPage();
  else if (page === "coverage") main.innerHTML = coveragePage();
  else {
    main.innerHTML = '<div class="loading">Loading…</div>';
    try {
      if (page === "digest") {
        const d = await api("/digest");
        if (state.page !== page || !state.me || !main.isConnected) return;
        main.innerHTML = digestPage(d);
      }

      if (page === "settings") {
        main.innerHTML = settingsPage();
      }
      if (page === "account") {
        const account = await api("/account");
        if (state.page !== page || !state.me || !main.isConnected) return;
        state.me = account;
        main.innerHTML = accountPage();
      }
    } catch (e) {
      if (!main.isConnected || e.stale) return;
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
    `${card(c)}${c.evidence.map(evidenceHTML).join("")}<h3>Source reconciliation</h3><div class="notice">${esc(sourceContext(c.reconciliation))}<br>${esc(c.reconciliation.note || "")}</div>`,
    `${esc(c.company)} · ${esc(c.study_id || "Company guidance")}`,
  );
}
async function showStudy(id) {
  const d = await api("/studies/" + id),
    s = d.study,
    r = d.reconciliation;
  dialog(
    esc(s.id),
    `${coverageNotice(d.company)}<h3>${esc(s.title)}</h3><p class="subtitle">${esc(s.drug)}</p><br><div class="metadata"><div><span>Recruitment</span><strong>${esc(s.status.replaceAll("_", " "))}</strong></div><div><span>Phase</span><strong>${esc(phaseLabel(s.phase))}</strong></div><div><span>Primary completion</span><strong>${esc(r.primary_completion?.date || "Not reported")} · ${esc(r.primary_completion?.type?.toLowerCase() || "uncertain")}</strong></div><div><span>Results publication</span><strong>${esc(r.results_posted?.date || "Not reported")}</strong></div></div><div class="notice">${esc(sourceContext(r))}<br>${esc(r.note)}</div><a href="https://clinicaltrials.gov/study/${esc(s.id)}" target="_blank" rel="noopener noreferrer">Open ClinicalTrials.gov ↗</a><h3>Company guidance</h3>${d.guidance.length ? d.guidance.map(evidenceHTML).join("") : '<p class="subtitle">No reviewed company guidance linked yet. Readout timing is uncertain.</p>'}<h3>Registry source history</h3>${d.snapshots.map((v, i) => `<div class="coverage-row"><div class="company-label"><b>${i === 0 ? "Latest saved version" : "Earlier version"} · ${fmt(v.captured_at)}</b><small>Source updated ${esc(v.source_updated || "date unavailable")}</small></div><a class="text-button" href="/api/snapshots/${v.id}">JSON ↓</a></div>`).join("")}<h3>Curated target tags</h3><p class="subtitle">${esc(JSON.parse(s.targets).join(", ") || "No reviewed target tags assigned.")}</p>${state.me.is_admin ? `<form id="target-form" data-study="${esc(s.id)}"><div class="field"><label for="targets">Targets (comma separated)</label><input id="targets" name="targets" value="${esc(JSON.parse(s.targets).join(", "))}"><small>Assign only targets supported by the source. Tags support exact matching in your watchlist.</small></div><button>Save target tags</button></form>` : ""}`,
  );
}
function evidenceForm() {
  dialog(
    "Record a source statement",
    `<p class="subtitle">Preserve the exact excerpt and the claim it supports. A reviewer must approve it before it changes the timeline.</p><br><form id="evidence-form"><div class="form-grid"><div class="field"><label for="company">Company</label><select id="company" name="company_id" required>${state.companies.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select></div><div class="field"><label for="study">Trial (optional)</label><select id="study" name="study_id"><option value="">Company-level statement</option></select></div><div class="field"><label for="source-type">Source type</label><select id="source-type" name="source_type"><option value="presentation">Company presentation</option><option value="press_release">Press release</option><option value="filing">Regulatory filing</option><option value="publication">Publication</option></select></div><div class="field"><label for="publication">Source publication date</label><input id="publication" type="date" name="published_at" required max="${new Date().toISOString().slice(0, 10)}"></div><div class="field full"><label for="title">Source title and page reference</label><input id="title" name="title" required minlength="3" maxlength="500" placeholder="Q2 corporate presentation, page 14"></div><div class="field full"><label for="url">Original source URL</label><input id="url" type="url" name="url" required placeholder="https://investors.company.com/…"></div><div class="field full"><label for="quote">Exact supporting excerpt</label><textarea id="quote" name="quote" required minlength="10" maxlength="20000" placeholder="Paste the original wording, including timing qualifiers."></textarea></div><div class="field"><label for="category">Change category</label><select id="category" name="category">${["timeline", "development_status", "trial_design", "enrollment", "partnership", "milestone", "results"].map((c) => `<option value="${c}">${label(c)}</option>`).join("")}</select></div><div class="field"><label for="value">Supported value / timing window</label><input id="value" name="value" required maxlength="1000" placeholder="e.g. H2 2026"><small>Use H1/H2, Q1–Q4, YYYY-MM, or original uncertain wording.</small></div><div class="field full"><label for="reason">Reason stated by the source (if any)</label><input id="reason" name="reason" maxlength="2000"></div></div><label class="check-label"><input type="checkbox" name="reason_checked">I checked the source for an explanation of the change.</label><p class="error" id="form-error"></p><button class="primary" type="submit">Submit for review</button></form>`,
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
  return `${heading("Your daily brief.", "A daily account of what changed in your watchlist.", `<a class="text-button" href="/api/digest/export?day=${d.day}">Download digest ↓</a>`)}<div class="digest-paper"><div class="paper-head"><p class="eyebrow">TrialDelta · ${fmt(d.day + "T12:00:00")}</p><h2>${d.changes.length ? `${d.changes.length} changes worth a closer look.` : "No changes today."}</h2><p class="subtitle">${d.follow_count} follows · source records included</p></div>${d.changes.length ? d.changes.map(card).join("") : '<p class="subtitle">Your digest includes registry changes detected today and company-source updates for the companies, trials, or themes you follow. Browse another day or explore more companies and programs to follow.</p><br><button data-page="coverage">Manage your watchlist →</button>'}<div class="notice">${esc(d.note)}</div><div class="field"><label for="digest-day">Browse another day</label><input type="date" id="digest-day" value="${d.day}"></div></div>`;
}

function settingsPage() {
  return `${heading("Workspace settings.", "Make this workspace part of your research routine.")}<div class="settings-grid"><section class="settings-card"><h2>Your research</h2><p>Follow the companies, programs, and themes that matter to you. Your watchlist is personal to your account.</p><button data-page="coverage">Manage your watchlist</button></section><section class="settings-card"><h2>Account & preferences</h2><p>Manage your profile, email preferences, and account security in one place.</p><button data-page="account">Open your account</button></section><section class="settings-card"><h2>Reading the evidence</h2><ul><li>Registry changes appear automatically with their previous and current source records.</li><li>Source excerpts and earlier statements stay linked to the change.</li><li>Registry dates retain their original precision.</li><li>A trial’s primary completion and a company’s expected readout are different milestones.</li></ul><button data-action="evidence">Add evidence for review</button></section></div>`;
}
function formFeedback() {
  return '<p class="error form-error" role="alert" tabindex="-1"></p><p class="form-status" role="status" aria-live="polite"></p>';
}
function accountPage() {
  const me = state.me;
  const authenticated = state.authConfig.auth_required;
  const delivery = !!me.email_delivery_available;
  return `${heading("Your account.", "Your profile, preferences, and sign-in security.")}
    ${!authenticated ? '<div class="notice account-notice">This is a local workspace profile. Sign-in and password management are disabled for this workspace.</div>' : ""}
    <div class="settings-grid account-grid">
      <section class="settings-card"><div class="account-card-heading"><h2>Profile</h2><span class="badge ${me.email_verified ? "" : "amber"}">${authenticated ? me.email_verified ? "Email verified" : "Email unverified" : "Local profile"}</span></div>
        <form id="profile-form"><div class="field"><label for="account-name">Name</label><input id="account-name" name="display_name" maxlength="100" autocomplete="name" value="${esc(me.display_name || "")}"></div><div class="field"><label for="account-email">Email</label><input id="account-email" type="email" value="${esc(me.email)}" readonly aria-describedby="account-email-help"><small id="account-email-help">${authenticated ? "This email is used to sign in. Email changes are not available yet." : "Email associated with this local profile."}</small></div>${me.created_at ? `<p class="account-member">Member since ${esc(fmt(me.created_at))}</p>` : ""}${formFeedback()}<button type="submit" class="primary">Save profile</button></form>
        ${authenticated && !me.email_verified ? `<div class="verification-block"><p>${delivery ? "Verify your email to confirm this address belongs to you." : "Email delivery is not set up for this workspace. Verification emails cannot be sent yet."}</p><button type="button" data-action="send-verification" ${delivery ? "" : "disabled"}>Send verification email</button><p id="verification-status" class="form-status" role="status" aria-live="polite"></p></div>` : ""}
      </section>
      <section class="settings-card"><h2>Email preferences</h2><p>Keep up with changes for the companies, trials, and themes you follow.</p><form id="preferences-form"><label class="check-label"><input id="digest-enabled" name="digest_enabled" type="checkbox" ${me.digest_enabled ? "checked" : ""}>Receive the daily research brief by email when available</label><p class="account-help">${delivery ? authenticated && !me.email_verified ? "Verify your email to receive your daily brief." : "Your preference applies to the email on your profile." : "Your preference will be saved. Email delivery is not set up for this workspace yet."}</p>${formFeedback()}<button type="submit">Save preferences</button></form><button class="text-button account-link" data-page="digest">Open your daily brief &nbsp;→</button></section>
      ${authenticated ? `<section class="settings-card"><h2>Password</h2><p>Choose a unique password with at least ${state.authConfig.password_min_length || 14} characters.</p><form id="password-form"><div class="field"><label for="current-password">Current password</label><input id="current-password" name="current_password" type="password" required maxlength="256" autocomplete="current-password"></div><div class="field"><label for="new-password">New password</label><input id="new-password" name="new_password" type="password" required minlength="${state.authConfig.password_min_length || 14}" maxlength="256" autocomplete="new-password"></div><div class="field"><label for="confirm-new-password">Confirm new password</label><input id="confirm-new-password" name="confirm_password" type="password" required autocomplete="new-password"></div><p class="account-help">Changing your password signs out your other sessions.</p>${formFeedback()}<button type="submit">Change password</button></form></section><section class="settings-card"><h2>Sessions</h2><p>Sign out of this browser, or end all sessions if you use TrialDelta on a shared device.</p><div class="session-actions"><button data-action="logout">Sign out</button><button data-action="logout-all">Sign out all devices</button></div><p class="account-help">Signing out all devices includes this browser. You can sign in again with your current password.</p></section>` : ""}
    </div>`;
}
function sourceContext(reconciliation) {
  if (reconciliation.status === "unlinked")
    return "This company statement is not linked to a clinical program.";
  return ["stale", "error"].includes(reconciliation.status)
    ? "Registry information may be out of date. Check the original source for the latest details."
    : "Read the registry record alongside the company’s source statements.";
}

async function toggleFollow(kind, value) {
  if (isGuest()) {
    inviteSignIn();
    return;
  }
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

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b || b.disabled) return;
  try {
    if (b.dataset.auth) {
      if ($("#app .shell")) state.returnPage = state.page;
      $("#detail").close();
      state.authToken = "";
      setRoute(b.dataset.auth);
      loginPage(b.dataset.auth);
      return;
    }
    if (b.dataset.page) {
      if (!state.me || !customerPages.includes(b.dataset.page)) return;
      state.page = b.dataset.page;
      state.returnPage = state.page;
      setRoute(state.page);
      if (state.page === "home") state.homeTab = "trials";
      state.q = "";
      state.changePage = 1;
      state.homePages = { trials: 1, news: 1, tads: 1, filings: 1 };
      resetHomeFilings();
      state.demo = false;
      shell();
      $("#main").focus({ preventScroll: true });
      return;
    }
    if (b.dataset.changePage) {
      const page = Number(b.dataset.changePage);
      if (!Number.isInteger(page) || page < 1 || b.disabled) return;
      state.changePage = page;
      const panel = $("#change-feed-panel");
      panel.innerHTML = changeFeedContent();
      panel.focus({ preventScroll: true });
      panel.scrollIntoView({ block: "start" });
      return;
    }
    if (b.dataset.homePage) {
      const page = Number(b.dataset.homePage);
      if (!Number.isInteger(page) || page < 1 || b.disabled) return;
      state.homePages[state.homeTab] = page;
      if (state.homeTab === "filings") resetHomeFilings(false);
      const panel = $("#home-feed-panel");
      panel.innerHTML = homeFeedContent();
      panel.focus({ preventScroll: true });
      panel.scrollIntoView({ block: "start" });
      if (state.homeTab === "filings") await loadHomeFeed();
      return;
    }
    if (b.dataset.calendarMonth !== undefined) {
      if (state.homeTab !== "calendar" || b.disabled) return;
      const offset = Number(b.dataset.calendarMonth);
      if (![-1, 0, 1].includes(offset)) return;
      if (offset === 0) state.calendarMonth = currentCalendarMonth();
      else {
        const [year, month] = state.calendarMonth.split("-").map(Number);
        const target = new Date(0);
        target.setUTCFullYear(year, month - 1 + offset, 1);
        if (target.getUTCFullYear() < 1 || target.getUTCFullYear() > 9999) return;
        state.calendarMonth = `${String(target.getUTCFullYear()).padStart(4, "0")}-${String(target.getUTCMonth() + 1).padStart(2, "0")}`;
      }
      $("#home-feed-panel").innerHTML = calendarContent();
      $(`[data-calendar-month="${offset}"]`).focus({ preventScroll: true });
      return;
    }
    if (b.dataset.calendarTad) {
      const row = (state.homeFeeds.tads || []).find(row => String(row.id) === b.dataset.calendarTad);
      if (row) dialog("Reported target action date", homeTadCard(row));
      return;
    }
    if (b.dataset.homeTab) {
      state.homeTab = b.dataset.homeTab;
      renderPage();
      $(`#home-tab-${state.homeTab}`).focus();
      return;
    }
    if (b.dataset.mode) {
      state.demo = b.dataset.mode === "demo";
      state.changePage = 1;
      renderPage();
      if (!state.demo) await refreshChanges();
      return;
    }
    if ("filter" in b.dataset) {
      state.category = b.dataset.filter;
      state.changePage = 1;
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
      setRoute("coverage");
      state.tab = "programs";
      state.q = state.companies.find(
        (c) => c.id === Number(b.dataset.company),
      ).ticker;
      resetHomeFilings();
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
      if (isGuest()) { inviteSignIn(); return; }
      await api("/follows/" + b.dataset.unfollow, { method: "DELETE" });
      await load();
      renderPage();
      return;
    }

    if (isGuest() && ["evidence", "follow-form", "followed", "send-verification", "logout-all"].includes(b.dataset.action)) {
      inviteSignIn(b.dataset.action === "evidence" ? "Sign in to submit source evidence for review." : undefined);
      return;
    }
    switch (b.dataset.action) {
      case "browse":
        if (!publicBrowsingEnabled()) return;
        state.authToken = "";
        $("#detail").close();
        setRoute(publicPages.includes(state.returnPage) ? state.returnPage : "home", true);
        await boot();
        break;
      case "refresh-tads":
        if (!["tads", "calendar"].includes(state.homeTab) || state.homeLoading.tads || state.demo) return;
        state.homeFeeds.tads = null;
        state.homeErrors.tads = "";
        state.homePages.tads = 1;
        renderPage();
        break;
      case "retry-home":
        state.homeErrors[homeFeedKey()] = "";
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

      case "followed":
        state.followed = !state.followed;
        state.changePage = 1;
        await refreshChanges();
        break;
      case "retry-changes":
        await refreshChanges();
        break;
      case "programs":
        state.page = "coverage";
        setRoute("coverage");
        state.tab = "programs";
        shell();
        break;
      case "retry-boot":
        b.disabled = true;
        await boot();
        break;
      case "send-verification": {
        b.disabled = true;
        const status = $("#verification-status");
        status.textContent = "Sending verification email…";
        try {
          const result = await api("/account/verification", { method: "POST" });
          if (status.isConnected) status.textContent = result.message || "Verification email sent. Check your inbox.";
        } catch (err) {
          if (status.isConnected) {
            status.textContent = err.message;
            status.classList.add("error");
          }
        } finally {
          b.disabled = false;
        }
        break;
      }
      case "logout":
      case "logout-all":
        b.disabled = true;
        await api(b.dataset.action === "logout-all" ? "/logout-all" : "/logout", { method: "POST" });
        clearUserState();
        notifyAccountChange();
        state.authToken = "";
        state.page = state.returnPage = "home";
        const signedOutMessage = b.dataset.action === "logout-all" ? "You are signed out on all devices." : "You are signed out.";
        if (publicBrowsingEnabled()) {
          history.replaceState({}, "", location.pathname + "#home");
          await boot();
          toast(signedOutMessage);
        } else {
          history.replaceState({}, "", location.pathname + "#login");
          loginPage("login", signedOutMessage);
        }
        break;
    }
  } catch (err) {
    b.disabled = false;
    if (!err.stale && !(err.status === 401 && !state.me)) toast(err.message);
  }
});
document.addEventListener("keydown", (e) => {
  if (!e.target.matches("[data-home-tab]") || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  e.preventDefault();
  const tabs = Object.keys(HOME_TABS);
  const index = tabs.indexOf(e.target.dataset.homeTab);
  const next = e.key === "Home" ? tabs[0] : e.key === "End" ? tabs.at(-1) : tabs[(index + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
  $(`[data-home-tab="${next}"]`).click();
});
document.addEventListener("change", async (e) => {
  try {
    if (e.target.id === "home-filings-page-size") {
      const size = Number(e.target.value);
      if (!FILING_PAGE_SIZES.includes(size)) return;
      state.filingsPageSize = size;
      resetHomeFilings();
      $("#home-feed-panel").innerHTML = filingsFeedContent();
      $("#home-filings-page-size").focus({ preventScroll: true });
      await loadHomeFeed();
      return;
    }
    if (e.target.id === "digest-day") {
      const d = await api("/digest?day=" + e.target.value);
      $("#main").innerHTML = digestPage(d);
    }

  } catch (err) {
    toast(err.message);
  }
});
document.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const b = $('button[type="submit"],button', f);
  const originalLabel = b?.textContent;
  const data = Object.fromEntries(new FormData(f));
  const error = $(".form-error, #form-error", f);
  const status = $(".form-status", f);
  if (error) error.textContent = "";
  if (status) status.textContent = "";
  const busy = { "login-form": "Signing in…", "register-form": "Creating account…", "forgot-form": "Sending…", "reset-form": "Resetting password…", "profile-form": "Saving…", "preferences-form": "Saving…", "password-form": "Changing password…" };
  if (b) {
    b.disabled = true;
    if (busy[f.id]) b.textContent = busy[f.id];
  }
  f.setAttribute("aria-busy", "true");
  try {
    switch (f.id) {
      case "evidence-form":
        data.company_id = Number(data.company_id);
        data.study_id = data.study_id || null;
        data.reason = data.reason || null;
        data.reason_checked = !!data.reason_checked;
        await api("/evidence", { method: "POST", body: JSON.stringify(data) });
        $("#detail").close();
        toast("Evidence submitted for review");
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
          method: "PUT", body: JSON.stringify({ targets: data.targets.split(",").map((v) => v.trim()).filter(Boolean) }),
        });
        await load();
        toast("Target tags saved");
        break;
      case "login-form":
        await api("/login", { method: "POST", body: JSON.stringify({ email: data.email, password: data.password }) });
        clearUserState();
        notifyAccountChange();
        await boot();
        break;
      case "register-form": {
        if (data.password !== data.confirm_password) throw new Error("Passwords do not match.");
        const result = await api("/auth/register", { method: "POST", body: JSON.stringify({ email: data.email, password: data.password, display_name: data.display_name }) });
        clearUserState();
        notifyAccountChange();
        if (!publicBrowsingEnabled()) state.returnPage = "account";
        setRoute(customerPages.includes(state.returnPage) ? state.returnPage : "account", true);
        await boot();
        if (state.me) toast(result.verification_sent ? "Account created. Check your inbox to verify your email." : result.email_delivery_available ? "Account created. Request a verification email from your account." : "Account created. Email delivery is not available yet.");
        break;
      }
      case "forgot-form": {
        const result = await api("/auth/forgot-password", { method: "POST", body: JSON.stringify({ email: data.email }) });
        status.textContent = result.message || "If an account matches this email, a password reset link will be sent.";
        break;
      }
      case "reset-form":
        if (data.new_password !== data.confirm_password) throw new Error("Passwords do not match.");
        await api("/auth/reset-password", { method: "POST", body: JSON.stringify({ token: state.authToken, new_password: data.new_password }) });
        state.authToken = "";
        clearUserState();
        notifyAccountChange();
        state.returnPage = "home";
        setRoute("login", true);
        loginPage("login", "Your password has been reset. Sign in with your new password.");
        break;
      case "profile-form": {
        const account = await api("/account", { method: "PATCH", body: JSON.stringify({ display_name: data.display_name }) });
        state.me = account;
        const avatar = $(".avatar");
        if (avatar) {
          avatar.textContent = accountInitials();
          avatar.setAttribute("aria-label", "Your account: " + (account.display_name || account.email));
        }
        status.textContent = "Profile saved.";
        toast("Profile saved");
        break;
      }
      case "preferences-form":
        state.me = await api("/account", { method: "PATCH", body: JSON.stringify({ digest_enabled: !!data.digest_enabled }) });
        status.textContent = "Email preferences saved.";
        toast("Email preferences saved");
        break;
      case "password-form":
        if (data.new_password !== data.confirm_password) throw new Error("Passwords do not match.");
        await api("/account/password", { method: "POST", body: JSON.stringify({ current_password: data.current_password, new_password: data.new_password }) });
        notifyAccountChange();
        f.reset();
        status.textContent = "Password changed. Other sessions have been signed out.";
        toast("Password changed");
        break;
    }
  } catch (err) {
    if (err.stale || (err.status === 401 && !f.isConnected)) return;
    if (error && f.isConnected) {
      error.textContent = err.message;
      error.focus();
    } else toast(err.message);
  } finally {
    f.removeAttribute("aria-busy");
    if (b) {
      b.disabled = false;
      b.textContent = originalLabel;
    }
  }
});
$("#detail").addEventListener("click", (e) => {
  if (e.target === $("#detail")) $("#detail").close();
});
function loginPage(view = "login", message = "") {
  if (isGuest()) clearUserState();
  if (view === "signup" && !state.authConfig.registration_enabled) view = "login";
  state.authView = view;
  const minimum = state.authConfig.password_min_length || 14;
  const titles = { login: "Welcome back.", signup: "Create your account.", "forgot-password": "Reset your password.", "reset-password": "Choose a new password.", "verify-email": "Email verification." };
  const title = titles[view] || titles.login;
  let content = "";
  if (view === "login") {
    content = `<form id="login-form"><div class="field"><label for="login-email">Email</label><input id="login-email" name="email" type="email" required maxlength="254" autocomplete="username" autocapitalize="none"></div><div class="field"><label for="login-password">Password</label><input id="login-password" name="password" type="password" required maxlength="256" autocomplete="current-password"></div>${formFeedback()}<button class="primary" type="submit">Sign in</button></form><div class="auth-links"><button class="text-button" data-auth="forgot-password">Forgot your password?</button>${state.authConfig.registration_enabled ? '<span>New to TrialDelta? <button class="text-button" data-auth="signup">Create account</button></span>' : ""}</div>`;
  } else if (view === "signup") {
    content = `<form id="register-form"><div class="field"><label for="register-name">Name (optional)</label><input id="register-name" name="display_name" maxlength="100" autocomplete="name"></div><div class="field"><label for="register-email">Email</label><input id="register-email" name="email" type="email" required maxlength="254" autocomplete="username" autocapitalize="none"></div><div class="field"><label for="register-password">Password</label><input id="register-password" name="password" type="password" required minlength="${minimum}" maxlength="256" autocomplete="new-password" aria-describedby="register-password-help"><small id="register-password-help">At least ${minimum} characters. Use a password unique to this account.</small></div><div class="field"><label for="register-confirm">Confirm password</label><input id="register-confirm" name="confirm_password" type="password" required autocomplete="new-password"></div>${formFeedback()}<button class="primary" type="submit">Create account</button></form><div class="auth-links"><span>Already have an account? <button class="text-button" data-auth="login">Sign in</button></span></div>`;
  } else if (view === "forgot-password") {
    content = state.authConfig.email_delivery_available
      ? `<form id="forgot-form"><div class="field"><label for="forgot-email">Email</label><input id="forgot-email" name="email" type="email" required maxlength="254" autocomplete="username" autocapitalize="none"></div>${formFeedback()}<button class="primary" type="submit">Send reset link</button></form>`
      : '<div class="notice">Password recovery by email is not available yet because email delivery has not been set up. Contact the workspace administrator for help with access.</div>';
    content += '<div class="auth-links"><button class="text-button" data-auth="login">Back to sign in</button></div>';
  } else if (view === "reset-password") {
    content = state.authToken
      ? `<form id="reset-form"><div class="field"><label for="reset-password">New password</label><input id="reset-password" name="new_password" type="password" required minlength="${minimum}" maxlength="256" autocomplete="new-password" aria-describedby="reset-password-help"><small id="reset-password-help">At least ${minimum} characters.</small></div><div class="field"><label for="reset-confirm">Confirm new password</label><input id="reset-confirm" name="confirm_password" type="password" required autocomplete="new-password"></div>${formFeedback()}<button class="primary" type="submit">Reset password</button></form>`
      : '<div class="notice">Open the password reset link from your email, or request a new one.</div>';
    content += '<div class="auth-links"><button class="text-button" data-auth="forgot-password">Request a new reset link</button><button class="text-button" data-auth="login">Back to sign in</button></div>';
  } else {
    content = '<div class="auth-links"><button class="text-button" data-auth="login">Continue to sign in</button></div>';
  }
  document.title = `${title.replace(/\.$/, "")} · TrialDelta`;
  $("#app").innerHTML = `<main id="main" class="login auth-card" tabindex="-1"><div class="brand"><img src="/static/favicon.svg" alt=""><span>Trial<span>Delta</span></span></div><p class="eyebrow">Your research, connected</p><h1>${title}</h1><p class="subtitle auth-intro">${view === "signup" ? "Build your watchlist and keep your research preferences in one place." : view === "login" ? "Sign in to your personal research workspace." : view === "forgot-password" ? "Recover access to your TrialDelta account." : view === "reset-password" ? "A new password will sign out your existing sessions." : "Confirm the email address on your account."}</p>${message ? `<p class="notice auth-message" role="status">${esc(message)}</p>` : ""}${content}${publicBrowsingEnabled() ? '<div class="auth-browse"><button class="text-button" data-action="browse">Continue browsing without an account &nbsp;→</button></div>' : ""}<p class="auth-footer">TrialDelta · Evidence over noise.</p></main>`;
  $("#main").focus({ preventScroll: true });
}
async function handleAuthLink() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const reset = fragment.get("reset-password");
  const verify = fragment.get("verify-email");
  if (!reset && !verify) return false;
  // Remove one-time tokens from browser history as soon as they are consumed.
  setRoute(reset ? "reset-password" : "verify-email", true);
  clearUserState();
  if (reset) {
    state.authToken = reset;
    loginPage("reset-password");
    return true;
  }
  state.authToken = "";
  loginPage("verify-email", "Verifying your email…");
  try {
    await api("/auth/verify-email", { method: "POST", body: JSON.stringify({ token: verify }) });
    try {
      state.me = await api("/me");
    } catch (err) {
      if (err.status !== 401) throw err;
    }
    if (state.me) {
      state.page = "account";
      setRoute("account", true);
      await load();
      shell();
      toast("Email verified.");
    } else {
      setRoute("login", true);
      loginPage("login", "Your email is verified. Sign in to continue.");
    }
  } catch (err) {
    loginPage("verify-email", err.message + " Sign in to your account to request a new verification email.");
  }
  return true;
}
async function boot() {
  try {
    state.authConfig = await api("/auth/config");
    if (await handleAuthLink()) return;
    const route = location.hash.slice(1);
    if (customerPages.includes(route)) state.returnPage = route;
    try {
      state.me = await api("/me");
    } catch (err) {
      if (err.status !== 401 || !publicBrowsingEnabled()) throw err;
      state.me = visitorIdentity();
      state.followed = false;
    }
    if (isGuest() && authPages.includes(route)) {
      loginPage(route);
      return;
    }
    await load();
    state.page = customerPages.includes(state.returnPage) ? state.returnPage : "home";
    setRoute(state.page, true);
    shell();
    const change = new URLSearchParams(location.search).get("change");
    if (change && /^\d+$/.test(change)) {
      try { await showChange(change); } catch (err) { if (!err.stale) toast(err.message); }
    }
  } catch (err) {
    if (err.stale) return;
    if (err.status === 401) {
      const route = location.hash.slice(1);
      loginPage(["signup", "forgot-password", "reset-password"].includes(route) ? route : "login");
    } else {
      $("#app").innerHTML = `<main id="main" class="login auth-card" tabindex="-1"><h1>Workspace unavailable</h1><p class="error" role="alert">${esc(err.message)}</p><p>Please try again in a moment.</p><button data-action="retry-boot">Try again</button></main>`;
    }
  }
}
window.addEventListener("hashchange", async () => {
  if (await handleAuthLink()) return;
  const page = location.hash.slice(1);
  if (state.me && (!isGuest() || !authPages.includes(page))) {
    if (isGuest()) {
      await boot();
      return;
    }
    state.page = customerPages.includes(page) ? page : "home";
    state.returnPage = state.page;
    shell();
  } else if (!state.me && publicBrowsingEnabled() && !authPages.includes(page)) {
    await boot();
  } else {
    if (customerPages.includes(page)) state.returnPage = page;
    loginPage(authPages.includes(page) ? page : "login");
  }
});
boot();


// Optional proposed WebMCP integration. The same API permissions apply as in the UI.
const agentToolsLifecycle = new AbortController();
window.addEventListener("pagehide", () => agentToolsLifecycle.abort(), {
  once: true,
});
if (document.modelContext?.registerTool) {
  const tools = [
    {
      name: "list_trialdelta_changes",
      title: "List TrialDelta changes",
      description:
        "Read watchlist changes for signed-in users or all tracked changes for visitors, excluding fictional examples.",
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
        if (isGuest()) {
          inviteSignIn();
          throw new Error("Sign in before following a company.");
        }
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
