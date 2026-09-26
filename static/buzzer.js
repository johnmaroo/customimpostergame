const app = document.getElementById("app");

const state = {
  token: localStorage.getItem("buzzer.token") || "",
  snapshot: null,
  meta: null,
  error: "",
  offline: false,
  busy: false,
  mode: "join",
  name: localStorage.getItem("buzzer.name") || "",
  joinCode: localStorage.getItem("buzzer.room") || "",
  // What this device's own last tap did. The room says who got in; this says
  // what happened to you, including the taps that never counted.
  tap: null,
  howTo: false,
  showQr: false,
};

const DEFAULT_HOLD_SECONDS = 20;
const WATCH_BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
// A room that cannot be reached for a moment is not a room that has closed.
const ROOM_GONE_GRACE_MS = 25000;
const CODE_PATTERN = /\/buzzer\/([A-Za-z]{4})/;

let watching = 0;
let misses = 0;
let roomGoneSince = 0;
let clockId = null;

// -- small helpers -------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

function sleep(ms) {
  return new Promise((done) => setTimeout(done, ms));
}

function codeFromLocation() {
  const params = new URLSearchParams(location.search);
  const query = (params.get("room") || "").toUpperCase();
  const path = location.pathname.match(CODE_PATTERN);
  return (query || (path ? path[1] : "")).toUpperCase();
}

function holdSeconds() {
  const asked = Number(state.meta?.holdSeconds);
  return Number.isFinite(asked) && asked > 0 ? asked : DEFAULT_HOLD_SECONDS;
}

function seconds(ms) {
  return `${(Math.max(0, ms) / 1000).toFixed(2)}s`;
}

function points(value) {
  return value < 0 ? `−${Math.abs(value)}` : String(value);
}

function buzz_vibrate(pattern) {
  try {
    navigator.vibrate?.(pattern);
  } catch {
    /* a device that does not buzz back is still perfectly playable */
  }
}

// -- talking to the server -----------------------------------------------

class ApiError extends Error {
  constructor(message, { status = 0, code = "" } = {}) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(path, { method = "GET", body, timeoutMs = 15000 } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const stop = new AbortController();
  const bell = setTimeout(() => stop.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: stop.signal,
    });
  } catch {
    throw new ApiError("Cannot reach the room right now.", { code: "offline" });
  } finally {
    clearTimeout(bell);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new ApiError(data.error || "Something went wrong.", {
      status: res.status,
      code: data.code || "",
    });
  }
  return data;
}

function setToken(token) {
  state.token = token || "";
  if (token) localStorage.setItem("buzzer.token", token);
  else localStorage.removeItem("buzzer.token");
}

function rememberRoom(code) {
  const value = String(code || "").trim().toUpperCase();
  if (!value) return;
  state.joinCode = value;
  localStorage.setItem("buzzer.room", value);
}

function dropSession() {
  setToken("");
  state.snapshot = null;
  state.tap = null;
  state.offline = false;
  misses = 0;
  roomGoneSince = 0;
  watching += 1;
}

function applyView(view, { force = false, tap } = {}) {
  if (!view) return;
  const seen = state.snapshot;
  // A held request can land after a newer answer has already overtaken it.
  if (!force && seen && view.updatedAt < seen.updatedAt) return;
  // A fresh window means a fresh race: forget what this device's last tap did.
  if (!seen || view.openedAt !== seen.openedAt) state.tap = null;
  if (tap !== undefined) state.tap = tap;
  state.snapshot = view;
  rememberRoom(view.code);
  render();
}

function setError(err) {
  state.error = String(err?.message || err || "");
}

async function act(work) {
  if (state.busy) return;
  state.busy = true;
  state.error = "";
  render();
  try {
    const answer = await work();
    if (answer?.token) setToken(answer.token);
    const view = answer?.room || (answer?.code ? answer : null);
    if (view) applyView(view, { force: true });
    misses = 0;
    state.offline = false;
  } catch (err) {
    setError(err);
    if (err.status === 401) dropSession();
  } finally {
    state.busy = false;
    render();
    startWatching();
  }
}

// -- watching the room ---------------------------------------------------

function startWatching() {
  const mine = ++watching;
  if (!state.token) return;
  (async () => {
    while (mine === watching && state.token) {
      if (document.visibilityState === "hidden") {
        await sleep(400);
        continue;
      }
      const since = state.snapshot?.rev || "";
      const hold = since ? holdSeconds() : 0;
      try {
        const view = await api(
          `/api/buzz/room?since=${encodeURIComponent(since)}&wait=${hold}`,
          { timeoutMs: (hold + 12) * 1000 },
        );
        if (mine !== watching) return;
        misses = 0;
        roomGoneSince = 0;
        if (state.offline || state.error) {
          state.offline = false;
          state.error = "";
        }
        applyView(view);
      } catch (err) {
        if (mine !== watching) return;
        if (!keepWatching(err)) return;
        await sleep(WATCH_BACKOFF_MS[Math.min(misses, WATCH_BACKOFF_MS.length - 1)]);
        misses += 1;
      }
    }
  })();
}

function keepWatching(err) {
  if (err.status === 401) {
    dropSession();
    setError(err);
    render();
    return false;
  }
  if (err.code === "room_closed") {
    const now = Date.now();
    if (!roomGoneSince) roomGoneSince = now;
    if (now - roomGoneSince < ROOM_GONE_GRACE_MS) return true;
    dropSession();
    setError("That room has closed.");
    render();
    return false;
  }
  state.offline = true;
  render();
  return true;
}

function tickClock() {
  clearInterval(clockId);
  clockId = setInterval(() => {
    const snap = state.snapshot;
    if (!snap || snap.phase !== "open") return;
    if (snap.lockedUntil && Date.now() >= snap.lockedUntil) {
      snap.lockedUntil = 0;
      render();
      return;
    }
    if (!snap.openedAt) return;
    const face = app.querySelector(".elapsed");
    if (!face) return;
    face.textContent = seconds(sinceOpen(snap));
  }, 100);
}

function lockedFor(snap) {
  if (!snap.lockedUntil) return 0;
  return Math.max(0, snap.lockedUntil - Date.now());
}

function sinceOpen(snap) {
  // Measured against the server's own clock, so an iPad set to the wrong
  // time still shows the host the same number everyone else sees.
  if (!snap.openedAt) return 0;
  const drift = Date.now() - snap.receivedAt;
  return (snap.serverNow - snap.openedAt) * 1000 + drift;
}

// -- screens -------------------------------------------------------------

function toast() {
  if (state.offline) {
    return `<div class="toast notice">Reconnecting to the room…</div>`;
  }
  if (!state.error) return "";
  return `<div class="toast" role="alert">${esc(state.error)}</div>`;
}

function render() {
  const snap = state.snapshot;
  if (snap && !snap.receivedAt) {
    snap.receivedAt = Date.now();
    // A lockout is kept as a deadline on this device's own clock. The room
    // does not change when a penalty runs out, so nothing is coming from the
    // server to say the button is live again.
    const locked = snap.you?.lockedForMs || 0;
    snap.lockedUntil = locked > 0 ? snap.receivedAt + locked : 0;
  }
  app.classList.toggle("console", Boolean(snap?.you?.isHost));
  if (!snap) {
    if (state.token) renderReconnecting();
    else renderHome();
    return;
  }
  if (snap.you.isHost) renderConsole(snap);
  else renderPlayer(snap);
}

function renderReconnecting() {
  app.innerHTML = `<section class="screen stack-lg">
    ${toast()}
    <div class="waiting"><p>Finding your room…</p></div>
    <button class="linkish" id="start-over" type="button">Start over</button>
  </section>`;
  app.querySelector("#start-over").onclick = () => {
    dropSession();
    render();
  };
}

function renderHome() {
  const linked = codeFromLocation();
  const code = linked || state.joinCode || "";
  const hosting = state.mode === "host";
  const least = state.meta?.minPasswordLength || 6;
  app.innerHTML = `<section class="screen stack-lg">
    ${toast()}
    <div class="hero">
      <div class="kicker">${linked ? "Room found" : "Quiz buzzer"}</div>
      <h1 class="title">BUZZER</h1>
      <p class="lede">The host opens the buzzers, every iPad lights up at once, and the first tap takes the clue.</p>
    </div>
    <div class="segmented" role="tablist">
      <button type="button" role="tab" id="tab-join" class="${hosting ? "" : "on"}" aria-selected="${!hosting}">Join a room</button>
      <button type="button" role="tab" id="tab-host" class="${hosting ? "on" : ""}" aria-selected="${hosting}">Host</button>
    </div>
    ${hosting ? hostForms(code, least) : joinForm(code)}
    <button class="linkish" id="how-toggle" type="button">${state.howTo ? "Hide how it works" : "How it works"}</button>
    ${state.howTo ? `<div class="panel"><ol class="how-list">
      <li>You host with a password. Anybody with the room code can join as a player; only the password opens the buzzers.</li>
      <li>Read the clue, then tap <b>Open buzzers</b>. Every iPad's button goes live at the same moment.</li>
      <li>The first tap to reach the server takes it, and the name appears on your console with how fast it was.</li>
      <li>Mark the answer right or wrong. A wrong answer loses the points and leaves that player out of the rest of the clue.</li>
      <li>Tapping before the buzzers open is a false start, and costs that player the first fraction of a second.</li>
    </ol></div>` : ""}
    <p class="hint"><a class="linkish" href="/">Looking for Imposter? It is still here.</a></p>
  </section>`;

  app.querySelector("#tab-join").onclick = () => switchTo("join");
  app.querySelector("#tab-host").onclick = () => switchTo("host");
  app.querySelector("#how-toggle").onclick = () => {
    keepTyping();
    state.howTo = !state.howTo;
    render();
  };
  if (hosting) bindHostForms();
  else bindJoinForm(code);
}

function switchTo(mode) {
  keepTyping();
  state.mode = mode;
  state.error = "";
  render();
}

function keepTyping() {
  const name = app.querySelector("#player-name");
  if (name) state.name = name.value;
  const code = app.querySelector("#room-code");
  if (code) state.joinCode = code.value.toUpperCase();
}

function joinForm(code) {
  return `<form id="join-form" class="stack panel">
    <label>Your name
      <input id="player-name" name="name" maxlength="24" autocomplete="nickname" value="${esc(state.name)}" placeholder="Maya" required />
    </label>
    <label>Room code
      <input id="room-code" class="code-input" maxlength="4" autocapitalize="characters" autocomplete="off" spellcheck="false" value="${esc(code)}" placeholder="KNTQ" required />
    </label>
    <button class="btn" type="submit"${state.busy ? " disabled" : ""}>Join room</button>
  </form>`;
}

function bindJoinForm(code) {
  app.querySelector("#join-form").onsubmit = (event) => {
    event.preventDefault();
    const name = app.querySelector("#player-name").value.trim();
    const room = (app.querySelector("#room-code").value || code).trim().toUpperCase();
    state.name = name;
    state.joinCode = room;
    localStorage.setItem("buzzer.name", name);
    act(() => api("/api/buzz/rooms/join", { method: "POST", body: { code: room, name } }));
  };
}

function hostForms(code, least) {
  return `<form id="open-form" class="stack panel">
    <label>Host password
      <input id="host-password" type="password" autocomplete="new-password" autocapitalize="none" autocorrect="off" spellcheck="false" minlength="${least}" placeholder="At least ${least} characters" required />
    </label>
    <button class="btn" type="submit"${state.busy ? " disabled" : ""}>Open a new room</button>
    <p class="hint">Keep this password. It is what lets you take the console back on another device.</p>
  </form>
  <form id="claim-form" class="stack panel">
    <div class="kicker">Already have a room</div>
    <label>Room code
      <input id="room-code" class="code-input" maxlength="4" autocapitalize="characters" autocomplete="off" spellcheck="false" value="${esc(code)}" placeholder="KNTQ" required />
    </label>
    <label>Host password
      <input id="claim-password" type="password" autocomplete="current-password" autocapitalize="none" autocorrect="off" spellcheck="false" required />
    </label>
    <button class="btn btn-ghost" type="submit"${state.busy ? " disabled" : ""}>Take the console</button>
  </form>`;
}

function bindHostForms() {
  app.querySelector("#open-form").onsubmit = (event) => {
    event.preventDefault();
    const password = app.querySelector("#host-password").value;
    act(() => api("/api/buzz/rooms", { method: "POST", body: { password } }));
  };
  app.querySelector("#claim-form").onsubmit = (event) => {
    event.preventDefault();
    const code = app.querySelector("#room-code").value.trim().toUpperCase();
    const password = app.querySelector("#claim-password").value;
    state.joinCode = code;
    act(() => api("/api/buzz/rooms/host", { method: "POST", body: { code, password } }));
  };
}

// -- the player's one button ---------------------------------------------

function buzzFace(snap) {
  const you = snap.you;
  const tap = state.tap;
  if (tap?.pending) {
    return { look: "sent", lead: "Sent", note: "Waiting for the room to answer." };
  }
  if (you.gotIn) {
    return {
      look: "won",
      lead: "You're in",
      note: "Answer out loud. The host is scoring it now.",
      reaction: you.reactionMs === null ? "" : seconds(you.reactionMs),
    };
  }
  if (tap && !tap.tookIt && tap.reason === "too_early") {
    return { look: "missed", lead: "Too early", note: `That is a false start. You lose the first ${seconds(snap.falseStartMs)} of the next window.` };
  }
  if (snap.phase === "answering" && snap.winner) {
    return { look: "missed", lead: "Missed it", note: `${snap.winner.name} got in at ${seconds(snap.winner.reactionMs ?? 0)}.` };
  }
  if (you.spent) {
    return { look: "blocked", lead: "You're out", note: "You had this one. Waiting for the next clue." };
  }
  if (snap.phase === "open" && lockedFor(snap) > 0) {
    return { look: "blocked", lead: "Locked out", note: "You buzzed before the clue. Your button opens in a moment." };
  }
  if (snap.phase === "open") {
    return { look: "live", lead: "Buzz", note: "" };
  }
  if (tap && !tap.tookIt && tap.message) {
    return { look: "missed", lead: "Not yet", note: tap.message };
  }
  return { look: "", lead: "Wait", note: "The host has not opened the buzzers." };
}

function buzzMarkup(snap) {
  const face = buzzFace(snap);
  return `<button class="buzz-btn ${face.look}" id="buzz" type="button" aria-live="polite">
    <span>${esc(face.lead)}</span>
    ${face.reaction ? `<span class="reaction">${esc(face.reaction)}</span>` : ""}
    ${face.note ? `<small>${esc(face.note)}</small>` : ""}
  </button>`;
}

function paintBuzz() {
  const button = app.querySelector("#buzz");
  if (!button || !state.snapshot) return;
  button.outerHTML = buzzMarkup(state.snapshot);
  bindBuzz();
}

function bindBuzz() {
  const button = app.querySelector("#buzz");
  if (!button) return;
  // pointerdown, not click: a click waits for the finger to come back up,
  // and that wait is longer than the gap this whole game is decided by.
  button.onpointerdown = sendBuzz;
  button.onclick = (event) => {
    event.preventDefault();
    // A click with no pointer behind it came from the keyboard.
    if (event.detail === 0) sendBuzz();
  };
  button.oncontextmenu = (event) => event.preventDefault();
}

function sendBuzz(event) {
  if (event) event.preventDefault();
  const snap = state.snapshot;
  if (!snap || snap.you.isHost || state.tap?.pending) return;
  state.tap = { pending: true };
  paintBuzz();
  buzz_vibrate(12);
  api("/api/buzz/room/buzz", { method: "POST", timeoutMs: 10000 })
    .then((answer) => {
      buzz_vibrate(answer.tookIt ? 90 : [14, 50, 14]);
      applyView(answer.room, {
        force: true,
        tap: { tookIt: answer.tookIt, reason: answer.reason, message: answer.message },
      });
    })
    .catch((err) => {
      if (err.status === 401) {
        dropSession();
        setError(err);
      } else {
        state.tap = { tookIt: false, reason: err.code, message: err.message };
      }
      render();
    });
}

function renderPlayer(snap) {
  const you = snap.you;
  const verdict = snap.lastResult && snap.lastResult.seatId === you.id ? snap.lastResult : null;
  app.innerHTML = `<section class="screen stack">
    ${toast()}
    <div class="brand">
      <div>
        <div class="kicker">Room ${esc(snap.code)} · clue ${snap.clueNumber}</div>
        <h2>${esc(you.name)}</h2>
      </div>
      <div class="stat"><b>${points(you.score)}</b><span>points</span></div>
    </div>
    <div class="buzz-stage">${buzzMarkup(snap)}</div>
    ${verdict ? `<div class="panel ${verdict.correct ? "" : "warn"}"><p>${verdict.correct ? `Right — ${points(verdict.value)} points.` : `Wrong — that cost you ${verdict.value} points.`}</p></div>` : ""}
    <div class="panel stack">
      <div class="kicker">Scores</div>
      <div class="player-list">${snap.players.map((row) => scoreRow(row, snap, false)).join("")}</div>
    </div>
    <div class="footer-actions">
      <button class="btn btn-ghost" id="leave" type="button">Leave the room</button>
    </div>
  </section>`;
  bindBuzz();
  app.querySelector("#leave").onclick = leaveRoom;
}

// -- the host's console ---------------------------------------------------

function scoreRow(row, snap, hosting) {
  const inNow = snap.winner?.seatId === row.id;
  const classes = ["score-row"];
  if (inNow) classes.push("in");
  if (row.spent) classes.push("spent");
  if (!hosting && row.id === snap.you.id) classes.push("you");
  return `<div class="${classes.join(" ")}">
    <span class="dot ${row.away ? "away" : ""}" title="${row.away ? "Not heard from lately" : "Here"}"></span>
    <span class="grow player-name">${esc(row.name)}${row.spent ? ' <span class="player-meta">· out this clue</span>' : ""}</span>
    ${hosting ? `<button class="chip" data-add="${esc(row.id)}" type="button" title="Add the clue value">+</button>
    <button class="chip" data-sub="${esc(row.id)}" type="button" title="Take the clue value away">−</button>
    <button class="chip" data-kick="${esc(row.id)}" type="button" title="Remove from the room">×</button>` : ""}
    <span class="pts ${row.score < 0 ? "down" : ""}">${points(row.score)}</span>
  </div>`;
}

function stage(snap) {
  if (snap.phase === "answering" && snap.winner) {
    return `<div class="stage answering">
      <div class="kicker">In</div>
      <div class="who">${esc(snap.winner.name)}</div>
      <p class="hint">${snap.winner.reactionMs === null ? "" : `${esc(seconds(snap.winner.reactionMs))} after the buzzers opened`}</p>
    </div>`;
  }
  if (snap.phase === "open") {
    return `<div class="stage open">
      <div class="kicker">Buzzers are open</div>
      <div class="elapsed">${esc(seconds(sinceOpen(snap)))}</div>
      <p class="hint">Waiting for somebody to take it.</p>
    </div>`;
  }
  const last = snap.lastResult;
  if (last) {
    return `<div class="stage ${last.correct ? "right" : "wrong"}">
      <div class="kicker">${last.correct ? "Right" : "Wrong"}</div>
      <div class="who">${esc(last.name)}</div>
      <p class="hint">${last.correct ? `+${last.value}` : `−${last.value}`} points${last.correct ? "" : " · out for the rest of this clue"}</p>
    </div>`;
  }
  return `<div class="stage">
    <div class="kicker">Clue ${snap.clueNumber}</div>
    <div class="who">Buzzers shut</div>
    <p class="hint">Read the clue, then open them.</p>
  </div>`;
}

function controls(snap) {
  if (snap.phase === "answering") {
    return `<div class="btn-row">
      <button class="btn btn-green" id="right" type="button"${state.busy ? " disabled" : ""}>Right +${snap.clueValue}</button>
      <button class="btn btn-rose" id="wrong" type="button"${state.busy ? " disabled" : ""}>Wrong −${snap.clueValue}</button>
    </div>
    <button class="btn btn-ghost" id="reset" type="button"${state.busy ? " disabled" : ""}>Cancel this buzz</button>`;
  }
  if (snap.phase === "open") {
    return `<button class="btn btn-ghost" id="reset" type="button"${state.busy ? " disabled" : ""}>Shut the buzzers</button>`;
  }
  return `<button class="btn" id="open" type="button"${state.busy ? " disabled" : ""}>Open buzzers</button>
  ${snap.lastResult || snap.players.some((row) => row.spent)
    ? `<button class="btn btn-ghost" id="reset" type="button"${state.busy ? " disabled" : ""}>Next clue</button>`
    : ""}`;
}

function renderConsole(snap) {
  const waiting = snap.players.length === 0;
  app.innerHTML = `<section class="screen stack">
    ${toast()}
    ${sharingWarning()}
    <div class="brand">
      <div>
        <div class="kicker">Host console</div>
        <h2>Room ${esc(snap.code)}</h2>
      </div>
      <div class="stat"><b>${snap.players.length}</b><span>${snap.players.length === 1 ? "player" : "players"}</span></div>
    </div>
    ${stage(snap)}
    ${controls(snap)}
    ${waiting ? qrPanel(snap) : `<div class="panel stack">
      <div class="spread"><div class="kicker">Scores</div><button class="linkish" id="show-qr" type="button">Show the join code</button></div>
      <div class="player-list">${snap.players.map((row) => scoreRow(row, snap, true)).join("")}</div>
    </div>`}
    ${state.showQr && !waiting ? qrPanel(snap) : ""}
    <div class="panel stack">
      <div class="kicker">Settings</div>
      <label>Points a clue is worth
        <input id="clue-value" type="number" inputmode="numeric" min="0" max="100000" step="50" value="${snap.clueValue}" />
      </label>
      <label>Lockout for buzzing early
        <select id="false-start">
          ${[0, 250, 500, 1000].map((ms) => `<option value="${ms}"${ms === snap.falseStartMs ? " selected" : ""}>${ms === 0 ? "Off" : `${ms} ms`}</option>`).join("")}
        </select>
      </label>
      <button class="btn btn-ghost btn-small" id="reset-scores" type="button">Reset every score</button>
    </div>
    <p class="keys"><kbd>space</kbd> open buzzers · <kbd>Y</kbd> right · <kbd>N</kbd> wrong · <kbd>R</kbd> next clue</p>
    <div class="footer-actions">
      <button class="btn btn-ghost" id="leave" type="button">Put the console down</button>
    </div>
  </section>`;
  bindConsole(snap);
}

function qrPanel(snap) {
  return `<div class="qr-wrap">
    <div class="kicker">Join at</div>
    <div class="room-code">${esc(snap.code)}</div>
    <div class="qr-frame">${snap.joinQrSvg || ""}</div>
    <label class="join-url-label">Or open this
      <input class="join-url" id="join-url" value="${esc(snap.joinUrl || "")}" readonly />
    </label>
  </div>`;
}

function sharingWarning() {
  const store = state.meta?.roomStore;
  if (!store || store.shared) return "";
  return `<div class="panel warn stack">
    <h3>Rooms are not shared between copies of this app</h3>
    <p class="hint">${esc(store.detail)}</p>
  </div>`;
}

function bindConsole(snap) {
  const press = (id, work) => {
    const button = app.querySelector(`#${id}`);
    if (button) button.onclick = () => act(work);
  };
  press("open", () => api("/api/buzz/room/open", { method: "POST" }));
  press("reset", () => api("/api/buzz/room/next", { method: "POST" }));
  press("right", () => api("/api/buzz/room/judge", { method: "POST", body: { correct: true } }));
  press("wrong", () => api("/api/buzz/room/judge", { method: "POST", body: { correct: false } }));
  press("reset-scores", () => api("/api/buzz/room/scores/reset", { method: "POST" }));

  app.querySelectorAll("[data-add]").forEach((button) => {
    button.onclick = () => act(() => api("/api/buzz/room/score", {
      method: "POST",
      body: { seatId: button.dataset.add, delta: snap.clueValue },
    }));
  });
  app.querySelectorAll("[data-sub]").forEach((button) => {
    button.onclick = () => act(() => api("/api/buzz/room/score", {
      method: "POST",
      body: { seatId: button.dataset.sub, delta: -snap.clueValue },
    }));
  });
  app.querySelectorAll("[data-kick]").forEach((button) => {
    button.onclick = () => act(() => api("/api/buzz/room/kick", {
      method: "POST",
      body: { seatId: button.dataset.kick },
    }));
  });

  const showQr = app.querySelector("#show-qr");
  if (showQr) showQr.onclick = () => {
    state.showQr = !state.showQr;
    render();
  };
  const joinUrl = app.querySelector("#join-url");
  if (joinUrl) joinUrl.onfocus = () => joinUrl.select();

  const value = app.querySelector("#clue-value");
  if (value) value.onchange = () => act(() => api("/api/buzz/room/settings", {
    method: "POST",
    body: { clueValue: Math.max(0, Number(value.value) || 0) },
  }));
  const early = app.querySelector("#false-start");
  if (early) early.onchange = () => act(() => api("/api/buzz/room/settings", {
    method: "POST",
    body: { falseStartMs: Number(early.value) || 0 },
  }));

  app.querySelector("#leave").onclick = leaveRoom;
}

function leaveRoom() {
  const goodbye = state.token
    ? api("/api/buzz/room/leave", { method: "POST" }).catch(() => {})
    : Promise.resolve();
  goodbye.then(() => {
    dropSession();
    state.mode = "join";
    render();
  });
}

// -- keyboard, for a host running this from a laptop ---------------------

function onKeyDown(event) {
  const snap = state.snapshot;
  if (!snap?.you?.isHost || state.busy || event.metaKey || event.ctrlKey || event.altKey) return;
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
  if (typing) return;
  const key = event.key.toLowerCase();
  const send = (path, body) => {
    event.preventDefault();
    act(() => api(path, { method: "POST", body }));
  };
  if (key === " " && snap.phase === "idle") send("/api/buzz/room/open");
  else if (key === "y" && snap.phase === "answering") send("/api/buzz/room/judge", { correct: true });
  else if (key === "n" && snap.phase === "answering") send("/api/buzz/room/judge", { correct: false });
  else if (key === "r") send("/api/buzz/room/next");
}

// -- start ----------------------------------------------------------------

async function loadMeta() {
  try {
    state.meta = await api("/api/buzz/meta", { timeoutMs: 8000 });
  } catch {
    state.meta = null;
  }
}

async function boot() {
  const linked = codeFromLocation();
  if (linked) state.joinCode = linked;
  else state.joinCode = state.joinCode || "";
  await loadMeta();
  render();
  if (state.token) startWatching();
  tickClock();
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.token) startWatching();
});
window.addEventListener("online", () => {
  if (state.token) startWatching();
});
window.addEventListener("pageshow", () => {
  if (state.token) startWatching();
});
document.addEventListener("keydown", onKeyDown);

boot();
