const app = document.getElementById("app");

const state = {
  token: localStorage.getItem("imposter.token") || "",
  snapshot: null,
  meta: null,
  error: "",
  howTo: false,
  flipped: false,
  hidden: false,
  peek: null,
  name: localStorage.getItem("imposter.name") || "",
  joinCode: "",
  inviteToken: "",
  busy: false,
  lastInvite: null,
  drafts: { word: "", inviteName: "", invitePhone: "", focus: "", selStart: null, selEnd: null },
};

const PALETTE = ["#e8c37a", "#ff8fa0", "#8ed6f7", "#9be7b7", "#c4b5fd", "#fb923c", "#f9a8d4", "#67e8f9"];

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function colorFor(id) {
  let n = 0;
  for (const ch of String(id)) n = (n * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[n % PALETTE.length];
}

function initials(name) {
  const parts = String(name || "").trim().split(/\s+/);
  return ((parts[0] || "?").slice(0, 1) + (parts[1] || "").slice(0, 1)).toUpperCase();
}

function joinCodeFromLocation() {
  const params = new URLSearchParams(location.search);
  const q = (params.get("join") || "").toUpperCase();
  const path = location.pathname.match(/\/join\/([A-Za-z]{4})/i);
  return (q || (path ? path[1] : "")).toUpperCase();
}

function inviteTokenFromLocation() {
  return new URLSearchParams(location.search).get("invite") || "";
}

function joinUrlFor(s) {
  return s.joinUrl || `${location.origin}/join/${s.code}`;
}

function safeQr(svg) {
  const markup = String(svg || "").trim();
  if (!markup.startsWith("<svg")) return "";
  return markup;
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong.");
  return data;
}

function setToken(token) {
  state.token = token || "";
  if (token) localStorage.setItem("imposter.token", token);
  else localStorage.removeItem("imposter.token");
}

function setError(err) {
  state.error = err ? String(err.message || err) : "";
}

async function loadMeta() {
  try {
    state.meta = await api("/api/meta");
  } catch {
    state.meta = { packs: [], savedWordCount: 0, hasAiKey: false };
  }
}

function roomFingerprint(snap) {
  if (!snap) return "";
  return JSON.stringify({
    phase: snap.phase,
    roundNumber: snap.roundNumber,
    code: snap.code,
    canStart: snap.canStart,
    remainingWordCount: snap.remainingWordCount,
    usedWordCount: snap.usedWordCount,
    numImposters: snap.numImposters,
    discussSeconds: snap.discussSeconds,
    passAndPlay: snap.passAndPlay,
    wordsVisible: snap.wordsVisible,
    words: snap.words,
    usedWords: snap.usedWords,
    speakerIndex: snap.speakerIndex,
    discussEndsAt: snap.discussEndsAt,
    players: snap.players,
    invites: (snap.invites || []).map((inv) => [inv.token, inv.name, inv.claimed, inv.phoneMasked]),
    you: snap.you,
    result: snap.result,
    joinUrl: snap.joinUrl,
  });
}

function captureDrafts() {
  const word = app.querySelector("#add-word [name=word]");
  const inviteName = app.querySelector("#invite-form [name=name]");
  const invitePhone = app.querySelector("#invite-form [name=phone]");
  if (!word && !inviteName) return;
  const active = document.activeElement;
  const inApp = active && app.contains(active);
  state.drafts = {
    word: word ? word.value : state.drafts.word,
    inviteName: inviteName ? inviteName.value : state.drafts.inviteName,
    invitePhone: invitePhone ? invitePhone.value : state.drafts.invitePhone,
    focus: inApp ? (active.getAttribute("name") || active.id || "") : "",
    selStart: inApp && "selectionStart" in active ? active.selectionStart : null,
    selEnd: inApp && "selectionEnd" in active ? active.selectionEnd : null,
  };
}

function restoreDrafts() {
  const drafts = state.drafts;
  const word = app.querySelector("#add-word [name=word]");
  if (word) word.value = drafts.word || "";
  const inviteName = app.querySelector("#invite-form [name=name]");
  if (inviteName) inviteName.value = drafts.inviteName || "";
  const invitePhone = app.querySelector("#invite-form [name=phone]");
  if (invitePhone) invitePhone.value = drafts.invitePhone || "";
  if (!drafts.focus) return;
  const focused = app.querySelector(`[name="${drafts.focus}"]`) || (drafts.focus && app.querySelector(`#${drafts.focus}`));
  if (!focused) return;
  focused.focus();
  if (drafts.selStart == null || typeof focused.setSelectionRange !== "function") return;
  try {
    focused.setSelectionRange(drafts.selStart, drafts.selEnd ?? drafts.selStart);
  } catch {
    /* some inputs do not support a selection range */
  }
}

let refreshInFlight = false;

async function refresh() {
  if (!state.token || refreshInFlight) return;
  refreshInFlight = true;
  try {
    const snap = await api("/api/room");
    const phaseChanged = Boolean(state.snapshot && state.snapshot.phase !== snap.phase);
    const roundChanged = Boolean(state.snapshot && state.snapshot.roundNumber !== snap.roundNumber);
    const changed = roomFingerprint(state.snapshot) !== roomFingerprint(snap);
    const hadError = Boolean(state.error);
    state.snapshot = snap;
    if (phaseChanged || roundChanged) {
      state.flipped = false;
      state.hidden = false;
      state.peek = null;
    }
    setError("");
    if (changed || phaseChanged || hadError) render();
  } catch (err) {
    const message = String(err.message || "").toLowerCase();
    if (message.includes("session expired") || message.includes("sign in to this room")) {
      setToken("");
      state.snapshot = null;
    }
    setError(err);
    render();
  } finally {
    refreshInFlight = false;
  }
}

async function act(fn) {
  if (state.busy) return;
  state.busy = true;
  try {
    const result = await fn();
    if (result && result.token) setToken(result.token);
    if (result && result.room) state.snapshot = result.room;
    else if (result && result.phase) state.snapshot = result;
    else if (result && result.role) {
      state.peek = result.role;
      state.snapshot = result.room;
    }
    setError("");
  } catch (err) {
    setError(err);
  } finally {
    state.busy = false;
    render();
  }
}

function toast() {
  return state.error ? `<div class="toast" role="alert">${esc(state.error)}</div>` : "";
}

function avatar(player) {
  const bg = colorFor(player.id);
  return `<div class="avatar" style="background:${bg}">${esc(initials(player.name))}</div>`;
}

function playerRow(player, youId, extra = "", actions = "") {
  const me = player.id === youId ? " me" : "";
  return `<div class="player${me}">
    ${avatar(player)}
    <div class="grow">
      <div class="player-name">${esc(player.name)}${player.isHost ? " · host" : ""}${player.id === youId ? " · you" : ""}</div>
      <div class="player-meta">${player.score} pt${player.score === 1 ? "" : "s"}${extra}</div>
    </div>
    ${actions}
  </div>`;
}

function seatedInDealOrder(s) {
  const byId = Object.fromEntries((s.players || []).map((p) => [p.id, p]));
  if (s.speakingOrder?.length) {
    return s.speakingOrder.map((p) => byId[p.id]).filter(Boolean);
  }
  return (s.players || []).filter((p) => p.inRound);
}

function renderHome() {
  const code = state.joinCode || joinCodeFromLocation();
  const scanned = Boolean(code);
  app.innerHTML = `<section class="screen stack-lg">
    ${toast()}
    <div class="hero">
      <div class="kicker">${scanned ? "Scan received" : "Party game"}</div>
      ${scanned ? `<div class="room-code">${esc(code)}</div><p class="lede">Enter your name to sit down at this table.</p>` : `<h1 class="title">IMPOSTER</h1><p class="lede">Most of you share a secret word. Someone does not. Talk around it — then vote out the faker.</p>`}
    </div>
    <label>Your name
      <input id="player-name" name="name" maxlength="24" autocomplete="nickname" value="${esc(state.name)}" placeholder="Maya" required />
    </label>
    ${scanned ? `
    <form id="join-form" class="stack panel">
      <button class="btn" type="submit"${state.busy ? " disabled" : ""}>Join room ${esc(code)}</button>
    </form>
    <button class="linkish" id="not-this-room" type="button">Not this room</button>
    ` : `
    <form id="create-form" class="stack panel">
      <button class="btn" type="submit"${state.busy ? " disabled" : ""}>Create a room</button>
    </form>
    <form id="join-form" class="stack panel">
      <label>Room code
        <input id="join-code" class="code-input" maxlength="4" autocapitalize="characters" autocomplete="off" spellcheck="false" value="${esc(code)}" placeholder="KNTQ" required />
      </label>
      <button class="btn btn-ghost" type="submit"${state.busy ? " disabled" : ""}>Join room</button>
    </form>
    `}
    <button class="linkish" id="how-toggle" type="button">${state.howTo ? "Hide how to play" : "How to play"}</button>
    ${state.howTo ? `<div class="panel"><ol class="how-list">
      <li>One person creates a room. Everyone else scans the QR on that screen, or gets a text with a join link.</li>
      <li>You can also type the 4-letter code if you already have the site open.</li>
      <li>The host adds a starter pack or types custom words. Words save for next time.</li>
      <li>Each player taps a private card: faithfuls see the word, imposters see a category.</li>
      <li>Take turns talking about the word without saying it. Imposters blend in.</li>
      <li>Vote. If the table names an imposter, the faithfuls score. If not, the imposters do.</li>
    </ol></div>` : ""}
  </section>`;

  app.querySelector("#how-toggle").onclick = () => {
    state.name = app.querySelector("#player-name")?.value || state.name;
    state.joinCode = (app.querySelector("#join-code")?.value || state.joinCode || code).toUpperCase();
    state.howTo = !state.howTo;
    render();
  };
  const notThis = app.querySelector("#not-this-room");
  if (notThis) notThis.onclick = () => {
    state.joinCode = "";
    state.inviteToken = "";
    history.replaceState({}, "", "/");
    render();
  };
  const readName = () => app.querySelector("#player-name").value.trim();
  const createForm = app.querySelector("#create-form");
  if (createForm) createForm.onsubmit = (event) => {
    event.preventDefault();
    const name = readName();
    state.name = name;
    localStorage.setItem("imposter.name", name);
    act(async () => api("/api/rooms", { method: "POST", body: { name } }));
  };
  app.querySelector("#join-form").onsubmit = (event) => {
    event.preventDefault();
    const name = readName();
    const joinCode = (app.querySelector("#join-code")?.value || code).trim().toUpperCase();
    state.name = name;
    state.joinCode = joinCode;
    localStorage.setItem("imposter.name", name);
    act(async () => api("/api/rooms/join", {
      method: "POST",
      body: { name, code: joinCode, inviteToken: state.inviteToken || undefined },
    }));
  };
}

function settingsPanel(s) {
  if (!s.you.isHost) {
    return `<div class="panel hint">${s.numImposters} imposter${s.numImposters === 1 ? "" : "s"} · ${s.discussSeconds ? s.discussSeconds + "s discussion" : "host-run discussion"}</div>`;
  }
  return `<div class="panel stack">
    <div class="spread"><h3>Table rules</h3></div>
    <label>Imposters
      <select id="num-imposters">
        ${[1, 2, 3].map((n) => `<option value="${n}"${s.numImposters === n ? " selected" : ""}>${n}</option>`).join("")}
      </select>
    </label>
    <label>Discussion timer
      <select id="discuss-seconds">
        ${[
          [0, "Host ends it"],
          [45, "45 seconds"],
          [60, "1 minute"],
          [90, "1.5 minutes"],
          [120, "2 minutes"],
          [180, "3 minutes"],
        ].map(([v, label]) => `<option value="${v}"${s.discussSeconds === v ? " selected" : ""}>${label}</option>`).join("")}
      </select>
    </label>
    <label class="toggle">Pass one phone around
      <input id="pass-play" type="checkbox"${s.passAndPlay ? " checked" : ""} />
    </label>
    <label class="toggle">Show words on this phone
      <input id="words-visible" type="checkbox"${s.wordsVisible ? " checked" : ""} />
    </label>
  </div>`;
}

function wordPanel(s) {
  if (!s.you.isHost) {
    return `<div class="panel"><div class="stat"><b>${s.remainingWordCount}</b><span>words ready</span></div></div>`;
  }
  const packs = (state.meta?.packs || []).map((pack) =>
    `<button class="chip" type="button" data-pack="${esc(pack.id)}">${esc(pack.title)}</button>`
  ).join("");
  const words = (s.words || []).map((word) =>
    `<span class="chip word-chip">${esc(word)}<button type="button" data-remove="${esc(word)}" aria-label="Remove ${esc(word)}">×</button></span>`
  ).join("");
  return `<div class="panel stack">
    <div class="spread">
      <h3>Word bank</h3>
      <span class="muted">${s.remainingWordCount} left · ${s.usedWordCount} used</span>
    </div>
    <form id="add-word" class="row">
      <input name="word" maxlength="48" placeholder="Add a custom word" autocomplete="off" />
      <button class="btn btn-small" type="submit">Add</button>
    </form>
    <div class="chips">${packs}</div>
    <div class="btn-row">
      <button class="btn btn-ghost btn-small" id="load-saved" type="button">Load saved (${state.meta?.savedWordCount ?? 0})</button>
      <button class="btn btn-ghost btn-small" id="recycle" type="button">Reuse used words</button>
    </div>
    ${s.wordsVisible ? `<div class="chips">${words || `<span class="muted">Words stay hidden until you toggle them.</span>`}</div>` : `<p class="hint">Words are hidden on screen so nobody peeking learns the bank. They still save to this computer.</p>`}
  </div>`;
}

function invitePanel(s) {
  if (!s.you.isHost) return "";
  const pending = (s.invites || []).filter((inv) => !inv.claimed);
  const claimed = (s.invites || []).filter((inv) => inv.claimed);
  const last = state.lastInvite;
  return `<div class="panel stack">
    <h3>Text a join link</h3>
    <p class="hint">They tap the text, the game opens, and they sit down as that name.</p>
    <form id="invite-form" class="stack">
      <label>Name
        <input name="name" maxlength="24" placeholder="Jordan" autocomplete="name" />
      </label>
      <label>Phone number
        <input name="phone" type="tel" inputmode="tel" placeholder="5551234567" autocomplete="tel" />
      </label>
      <button class="btn" type="submit"${state.busy ? " disabled" : ""}>Send invite</button>
    </form>
    ${last ? `<p class="hint">${last.sent ? `Sent to ${esc(last.name)} via iMessage.` : `Invite saved for ${esc(last.name)}.`} ${last.smsUrl ? `<a class="linkish" href="${esc(last.smsUrl)}">Open Messages</a>` : ""}</p>` : ""}
    ${pending.length ? `<div class="player-list">${pending.map((inv) => `<div class="player">
      <div class="grow"><div class="player-name">${esc(inv.name)}</div><div class="player-meta">${esc(inv.phoneMasked)} · waiting</div></div>
      ${inv.smsUrl ? `<a class="btn btn-ghost btn-small" href="${esc(inv.smsUrl)}">Text</a>` : ""}
    </div>`).join("")}</div>` : ""}
    ${claimed.length ? `<p class="muted">${claimed.map((inv) => esc(inv.name)).join(", ")} joined from a text.</p>` : ""}
  </div>`;
}

function renderLobby() {
  const s = state.snapshot;
  const url = joinUrlFor(s);
  const qr = safeQr(s.joinQrSvg);
  app.innerHTML = `<section class="screen stack-lg">
    ${toast()}
    <div class="qr-wrap">
      <div class="kicker">Scan to join</div>
      <div class="qr-frame">${qr || `<p class="hint">Join link: ${esc(url)}</p>`}</div>
      <div class="room-code">${esc(s.code)}</div>
      <p class="hint">Same Wi-Fi. Camera app → tap the notification.</p>
    </div>
    <div class="btn-row">
      <button class="btn btn-ghost btn-small" id="copy-code" type="button">Copy code</button>
      <button class="btn btn-ghost btn-small" id="copy-link" type="button">Copy join link</button>
    </div>
    ${invitePanel(s)}
    <div class="panel stack">
      <div class="spread"><h3>Players</h3><span class="muted">${s.players.length}</span></div>
      <div class="player-list">
        ${s.players.map((p) => {
          const kick = s.you.isHost && !p.isHost
            ? `<button class="btn btn-ghost btn-small" data-kick="${p.id}" type="button">Remove</button>`
            : "";
          return playerRow(p, s.you.id, "", kick);
        }).join("")}
      </div>
    </div>
    ${settingsPanel(s)}
    ${wordPanel(s)}
    <div class="footer-actions">
      ${s.you.isHost
        ? `<button class="btn" id="start"${s.canStart && !state.busy ? "" : " disabled"}>Start round ${s.roundNumber + 1}</button>
           <p class="hint">${s.canStart ? "Everyone should have their own screen face-down." : "Need 3 players, at least one word, and fewer imposters than players."}</p>`
        : `<p class="waiting">Waiting for ${esc(s.players.find((p) => p.isHost)?.name || "the host")} to start.</p>`}
      <button class="btn btn-ghost" id="leave" type="button">Leave</button>
    </div>
  </section>`;
  bindLobby(s);
}

function bindLobby(s) {
  const url = joinUrlFor(s);
  const copy = async (text, btn) => {
    try {
      await navigator.clipboard.writeText(text);
      if (btn) btn.textContent = "Copied";
    } catch {
      setError(new Error("Copy failed — select it manually."));
      render();
    }
  };
  const copyCode = app.querySelector("#copy-code");
  if (copyCode) copyCode.onclick = () => copy(s.code, copyCode);
  const copyLink = app.querySelector("#copy-link");
  if (copyLink) copyLink.onclick = () => copy(url, copyLink);
  const inviteForm = app.querySelector("#invite-form");
  if (inviteForm) inviteForm.onsubmit = (event) => {
    event.preventDefault();
    const name = inviteForm.querySelector("[name=name]").value.trim();
    const phone = inviteForm.querySelector("[name=phone]").value.trim();
    act(async () => {
      const result = await api("/api/room/invite", { method: "POST", body: { name, phone } });
      state.lastInvite = {
        name,
        sent: Boolean(result.sent),
        smsUrl: result.smsUrl,
      };
      return result;
    });
  };
  const start = app.querySelector("#start");
  if (start) start.onclick = () => act(() => api("/api/room/start", { method: "POST" }));
  const leave = app.querySelector("#leave");
  if (leave) leave.onclick = () => act(async () => {
    await api("/api/room/leave", { method: "POST" });
    setToken("");
    state.snapshot = null;
    return {};
  });
  const saveSettings = () => {
    if (!s.you.isHost) return;
    act(() => api("/api/room/settings", {
      method: "POST",
      body: {
        numImposters: Number(app.querySelector("#num-imposters").value),
        discussSeconds: Number(app.querySelector("#discuss-seconds").value),
        passAndPlay: app.querySelector("#pass-play").checked,
        wordsVisible: app.querySelector("#words-visible").checked,
      },
    }));
  };
  ["#num-imposters", "#discuss-seconds", "#pass-play", "#words-visible"].forEach((sel) => {
    const node = app.querySelector(sel);
    if (node) node.onchange = saveSettings;
  });
  const form = app.querySelector("#add-word");
  if (form) form.onsubmit = (event) => {
    event.preventDefault();
    const input = form.querySelector("input");
    const word = input.value.trim();
    if (!word) return;
    input.value = "";
    act(() => api("/api/room/words", { method: "POST", body: { word } }));
  };
  app.querySelectorAll("[data-pack]").forEach((btn) => {
    btn.onclick = () => act(() => api("/api/room/words/pack", { method: "POST", body: { packId: btn.dataset.pack } }));
  });
  const loadSaved = app.querySelector("#load-saved");
  if (loadSaved) loadSaved.onclick = () => act(() => api("/api/room/words/saved", { method: "POST" }));
  const recycle = app.querySelector("#recycle");
  if (recycle) recycle.onclick = () => act(() => api("/api/room/words/recycle", { method: "POST" }));
  app.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.onclick = () => act(() => api("/api/room/words/remove", { method: "POST", body: { word: btn.dataset.remove } }));
  });
  app.querySelectorAll("[data-kick]").forEach((btn) => {
    btn.onclick = () => act(() => api("/api/room/kick", { method: "POST", body: { playerId: btn.dataset.kick } }));
  });
}

function roleFace(role) {
  if (!role) return "";
  if (role.kind === "imposter") {
    return `<div class="face face-back imposter">
      <div class="kicker rose">Keep this private</div>
      <h2>You are the Imposter</h2>
      ${role.clue
        ? `<p class="lede">Talk around this category:</p><div class="secret-word">${esc(role.clue)}</div>`
        : `<p class="lede">No category clue this round. Listen hard and blend in.</p>`}
    </div>`;
  }
  return `<div class="face face-back faithful">
    <div class="kicker gold">Keep this private</div>
    <p class="lede">The word is</p>
    <div class="secret-word">${esc(role.word)}</div>
  </div>`;
}

function renderReveal() {
  const s = state.snapshot;
  if (s.you.sittingOut) {
    app.innerHTML = `<section class="screen stack-lg">${toast()}<div class="waiting">This round already started. You are in for the next one.</div></section>`;
    return;
  }
  if (s.passAndPlay && !s.you.isHost) {
    app.innerHTML = `<section class="screen stack-lg">
      ${toast()}
      <div class="waiting">
        <div class="kicker">Pass-and-play</div>
        <h2>Look at the shared phone</h2>
        <p>The host is revealing cards one at a time. Look away until they say your name.</p>
      </div>
    </section>`;
    return;
  }
  if (s.passAndPlay && s.you.isHost) {
    const overlay = state.peek ? `<div class="card-stage">
      <div class="card flipped"><div class="card-inner">
        <div class="face face-front"></div>
        ${roleFace(state.peek)}
      </div></div>
      <button class="btn" id="hide-peek" type="button">Hide card</button>
    </div>` : "";
    app.innerHTML = `<section class="screen stack-lg">
      ${toast()}
      <div>
        <div class="kicker">Pass the phone</div>
        <h2>Reveal one player at a time</h2>
        <p class="lede">Hand them the phone, tap their name, then hide the card before the next person.</p>
      </div>
      ${overlay}
      <div class="vote-grid">
        ${seatedInDealOrder(s).map((p) =>
          `<button class="vote-btn" data-peek="${p.id}">${esc(p.name)}${p.ready ? " · seen" : ""}</button>`
        ).join("")}
      </div>
      <button class="btn btn-ghost" id="advance" type="button">Everyone has seen it — discuss</button>
    </section>`;
    app.querySelectorAll("[data-peek]").forEach((btn) => {
      btn.onclick = () => act(() => api("/api/room/peek", { method: "POST", body: { playerId: btn.dataset.peek } }));
    });
    const hide = app.querySelector("#hide-peek");
    if (hide) hide.onclick = () => { state.peek = null; render(); };
    app.querySelector("#advance").onclick = () => act(() => api("/api/room/advance", { method: "POST" }));
    return;
  }

  const role = s.you.role;
  const showCard = state.flipped && !state.hidden;
  app.innerHTML = `<section class="screen stack-lg">
    ${toast()}
    <div>
      <div class="kicker">Round ${s.roundNumber}</div>
      <h2>${state.hidden ? "Look up." : "Private card"}</h2>
      <p class="lede">${state.hidden ? "Keep a straight face." : "Tilt the phone toward you. Nobody else should see this."}</p>
    </div>
    ${state.hidden ? `<div class="waiting"><span class="dot"></span> Waiting for ${s.players.filter((p) => p.inRound && !p.ready).length} more…</div>` : `
    <button class="card${showCard ? " flipped" : ""}" id="role-card" type="button">
      <div class="card-inner">
        <div class="face face-front">
          <div class="seal">🎭</div>
          <h2>Tap to reveal</h2>
          <p class="muted">Your role stays on this phone.</p>
        </div>
        ${roleFace(role)}
      </div>
    </button>`}
    <div class="footer-actions">
      ${!state.hidden && showCard ? `<button class="btn" id="got-it" type="button">Got it — hide card</button>` : ""}
      ${s.you.isHost ? `<button class="btn btn-ghost" id="advance" type="button">Skip to discussion</button>` : ""}
    </div>
  </section>`;
  const card = app.querySelector("#role-card");
  if (card) card.onclick = () => {
    state.flipped = true;
    try { navigator.vibrate?.(12); } catch {}
    render();
  };
  const got = app.querySelector("#got-it");
  if (got) got.onclick = () => {
    state.hidden = true;
    act(() => api("/api/room/ready", { method: "POST" }));
  };
  const advance = app.querySelector("#advance");
  if (advance) advance.onclick = () => act(() => api("/api/room/advance", { method: "POST" }));
}

function remainingMs(s) {
  if (!s.discussEndsAt) return null;
  return Math.max(0, s.discussEndsAt * 1000 - Date.now());
}

function formatMs(ms) {
  const total = Math.ceil(ms / 1000);
  const m = Math.floor(total / 60);
  const sec = String(total % 60).padStart(2, "0");
  return m > 0 ? `${m}:${sec}` : `0:${sec}`;
}

function renderDiscuss() {
  const s = state.snapshot;
  const ms = remainingMs(s);
  const speaker = s.speakingOrder[s.speakerIndex] || s.speakingOrder[0];
  const total = (s.discussSeconds || 0) * 1000;
  const pct = ms == null || !total ? 100 : Math.max(0, Math.round((ms / total) * 100));
  app.innerHTML = `<section class="screen stack-lg">
    ${toast()}
    <div>
      <div class="kicker">Discussion</div>
      <h2>Talk around the word</h2>
      <p class="lede">Do not say it. Imposters should still sound like they belong.</p>
    </div>
    ${ms != null ? `<div class="timer${ms < 15000 ? " warn" : ""}">${formatMs(ms)}</div><div class="bar"><span style="--p:${pct}%"></span></div>` : `<p class="hint">No timer — host decides when to vote.</p>`}
    <div class="speaker">
      <div class="kicker">Speaking now</div>
      <strong>${esc(speaker?.name || "—")}</strong>
      <p class="muted">${s.speakingOrder.map((p, i) => i === s.speakerIndex ? `<span class="gold">${esc(p.name)}</span>` : esc(p.name)).join(" → ")}</p>
    </div>
    ${s.you.isHost ? `<div class="btn-row">
      <button class="btn btn-ghost" id="next-speaker" type="button">Next speaker</button>
      <button class="btn" id="advance" type="button">Start vote</button>
    </div>` : `<p class="hint">Listen for your name in the speaking order.</p>`}
  </section>`;
  const next = app.querySelector("#next-speaker");
  if (next) next.onclick = () => act(() => api("/api/room/next-speaker", { method: "POST" }));
  const advance = app.querySelector("#advance");
  if (advance) advance.onclick = () => act(() => api("/api/room/advance", { method: "POST" }));
}

function renderVote() {
  const s = state.snapshot;
  const others = seatedInDealOrder(s).filter((p) => p.id !== s.you.id);
  app.innerHTML = `<section class="screen stack-lg">
    ${toast()}
    <div>
      <div class="kicker">Vote</div>
      <h2>Who is the imposter?</h2>
      <p class="lede">${s.you.hasVoted ? "Locked in. Waiting on the table." : "You cannot vote for yourself. Skip if you truly have no read."}</p>
    </div>
    <div class="vote-grid">
      ${others.map((p) => `<button class="vote-btn${s.you.votedFor === p.id ? " picked" : ""}" data-vote="${p.id}" ${s.you.hasVoted ? "disabled" : ""}>${esc(p.name)}</button>`).join("")}
      <button class="vote-btn${s.you.hasVoted && s.you.votedFor == null ? " picked" : ""}" data-vote="" ${s.you.hasVoted ? "disabled" : ""}>Skip — not sure</button>
    </div>
    <p class="hint">${s.players.filter((p) => p.inRound && p.hasVoted).length}/${s.players.filter((p) => p.inRound).length} voted</p>
    ${s.you.isHost ? `<button class="btn btn-ghost" id="advance" type="button">Close votes</button>` : ""}
  </section>`;
  app.querySelectorAll("[data-vote]").forEach((btn) => {
    btn.onclick = () => act(() => api("/api/room/vote", {
      method: "POST",
      body: { targetId: btn.dataset.vote || null },
    }));
  });
  const advance = app.querySelector("#advance");
  if (advance) advance.onclick = () => act(() => api("/api/room/advance", { method: "POST" }));
}

function voteLabel(s, playerId) {
  if (!playerId) return "skipped";
  const named = s.players.find((p) => p.id === playerId);
  return named ? named.name : "someone";
}

function renderResults() {
  const s = state.snapshot;
  const result = s.result || {};
  const townWon = result.winner === "faithfuls";
  const imposters = s.players.filter((p) => result.imposterIds?.includes(p.id));
  const eliminated = s.players.filter((p) => result.eliminatedIds?.includes(p.id));
  app.innerHTML = `<section class="screen stack-lg">
    ${toast()}
    <div class="banner ${townWon ? "win" : "lose"}">
      <div class="kicker">${townWon ? "Faithfuls" : "Imposters"}</div>
      <h2>${townWon ? "Caught them." : "They got away."}</h2>
      <p>The word was <strong>${esc(result.word)}</strong></p>
    </div>
    <div class="panel stack">
      <h3>Imposter${imposters.length === 1 ? "" : "s"}</h3>
      ${imposters.map((p) => playerRow(p, s.you.id, result.clue ? ` · ${esc(result.clue)}` : "")).join("")}
      <p class="muted">${eliminated.length === 1 ? `The table named ${esc(eliminated[0].name)}.` : eliminated.length > 1 ? "The vote tied — imposters slip through." : "Nobody was named."}</p>
    </div>
    <div class="panel stack">
      <h3>Scoreboard</h3>
      <div class="player-list">
        ${s.players.map((p) => {
          const delta = p.scoreDelta ? ` · +${p.scoreDelta}` : "";
          const vote = p.inRound ? ` · voted ${esc(voteLabel(s, p.votedFor))}` : " · sat out";
          return playerRow(p, s.you.id, `${delta}${vote}`);
        }).join("")}
      </div>
    </div>
    <div class="footer-actions">
      ${s.you.isHost
        ? `<button class="btn" id="start"${s.canStart && !state.busy ? "" : " disabled"}>${s.remainingWordCount ? "Next round" : "Need more words"}</button>
           ${s.remainingWordCount ? "" : `<button class="btn btn-ghost" id="recycle" type="button">Put used words back</button>`}`
        : `<p class="waiting">Waiting on the host.</p>`}
    </div>
  </section>`;
  const start = app.querySelector("#start");
  if (start) start.onclick = () => act(() => api("/api/room/start", { method: "POST" }));
  const recycle = app.querySelector("#recycle");
  if (recycle) recycle.onclick = () => act(() => api("/api/room/words/recycle", { method: "POST" }));
}

function render() {
  if (state.snapshot && state.snapshot.phase === "lobby") captureDrafts();
  const s = state.snapshot;
  if (!s) {
    renderHome();
    return;
  }
  const phase = s.phase;
  if (phase === "lobby") {
    renderLobby();
    restoreDrafts();
  } else if (phase === "reveal") renderReveal();
  else if (phase === "discuss") renderDiscuss();
  else if (phase === "vote") renderVote();
  else if (phase === "results") renderResults();
  else renderLobby();
}

let pollId = 0;
let timerId = 0;

function tickTimers() {
  clearInterval(timerId);
  timerId = setInterval(() => {
    const snap = state.snapshot;
    if (snap?.phase !== "discuss" || !snap.discussEndsAt) return;
    const ms = remainingMs(snap);
    if (ms === 0) {
      refresh();
      return;
    }
    const timer = app.querySelector(".timer");
    const bar = app.querySelector(".bar > span");
    if (!timer) return;
    timer.textContent = formatMs(ms);
    timer.classList.toggle("warn", ms < 15000);
    if (bar) {
      const total = (snap.discussSeconds || 0) * 1000;
      const pct = !total ? 100 : Math.max(0, Math.round((ms / total) * 100));
      bar.style.setProperty("--p", `${pct}%`);
    }
  }, 250);
}

function startPolling() {
  clearInterval(pollId);
  pollId = setInterval(() => {
    if (state.token && !state.busy && !refreshInFlight) refresh();
  }, 2000);
}

async function boot() {
  state.joinCode = joinCodeFromLocation();
  state.inviteToken = inviteTokenFromLocation();
  await loadMeta();
  if (state.inviteToken && !state.token) {
    try {
      const info = await api(`/api/invites/${encodeURIComponent(state.inviteToken)}`);
      state.name = info.name;
      state.joinCode = info.code;
      localStorage.setItem("imposter.name", info.name);
      const result = await api("/api/rooms/join", {
        method: "POST",
        body: { name: info.name, code: info.code, inviteToken: state.inviteToken },
      });
      setToken(result.token);
      state.snapshot = result.room;
      history.replaceState({}, "", `/join/${info.code}`);
    } catch (err) {
      setError(err);
    }
  }
  if (state.token) await refresh();
  else render();
  startPolling();
  tickTimers();
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

boot();
