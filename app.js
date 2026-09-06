/* ========== 常量与状态 ========== */
const RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"];
const RED_SUITS = new Set(["♥", "♦"]);
const JOKER_BIG_SUIT = "★";
const JOKER_SMALL_SUIT = "☆";
const MAX_CARDS_PER_PLAY = 8;
const SOUND_FILES = {
  play: "sounds/play.mp3",
  win: "sounds/win.mp3",
  lose: "sounds/lose.mp3",
};
const PLAYER_COLORS = ["#D8B46A", "#5BB0A6", "#C85B5B", "#7D8CC4", "#C08F4E", "#8F7FC4"];
const AI_TIERS = [
  { key: "easy", label: "简单" },
  { key: "medium", label: "中等" },
  { key: "hard", label: "困难" },
];
const QUICK_PHRASES = ["吹牛！", "不信！", "稳了！", "快点！", "🤨", "😏"];
const EMOJIS = ["😀", "😂", "🤣", "😏", "🤨", "😎", "🥳", "😭", "🔥", "👍", "👎", "🃏"];
const STORAGE_KEY = "bluff_identity";
const AUTH_STORAGE_KEY = "bluff_auth";
const MUSIC_STORAGE_KEY = "bluff_music_on";
const MUSIC_VOLUME_KEY = "bluff_music_volume";
const SFX_VOLUME_KEY = "bluff_sfx_volume";

let snapshot = null;
let selectedCardIds = new Set();
let selectedRank = null;
let eventSource = null;
let timerInterval = null;
let chatOpen = false;
let bgmAudio = null;
let musicOn = false;
let musicPlaying = false;
let musicFailed = false;
let musicVolume = 0.6;
let sfxVolume = 1;
let soundAudios = {};
let pendingPlayAnimationId = null;
let renderTimeout = null;
let shownGameOverRoomWinner = null;
let aiStrength = "medium";

const state = {
  roomCode: null,
  token: null,
  playerId: null,
  authToken: null,
  account: null,
};

let authMode = "login";

/* ========== DOM 工具 ========== */
const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(message) {
  const container = $("toast-container");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

function showView(id) {
  ["view-home", "view-lobby", "view-game", "view-rooms"].forEach((viewId) => {
    $(viewId).classList.toggle("active", viewId === id);
  });

  if (id === "view-lobby" || id === "view-game") {
    ensureMusicPlaying();
  } else {
    pauseMusic();
  }
}

function showError(id, message) {
  const el = $(id);
  if (el) el.textContent = message || "";
}

function setModal(title, bodyHtml, closeText = "知道了", wide = false) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = bodyHtml;
  $("modal-close").textContent = closeText;
  $("modal").classList.toggle("modal-wide", wide);
  $("modal-mask").hidden = false;
}

function closeModal() {
  $("modal-mask").hidden = true;
}

function cardParts(cardId) {
  const suit = cardId.charAt(0);
  const rank = cardId.slice(1).split("#", 1)[0];
  return { suit, rank, red: RED_SUITS.has(suit) || suit === JOKER_BIG_SUIT };
}

function isJokerCard(cardId) {
  const suit = cardId.charAt(0);
  return suit === JOKER_BIG_SUIT || suit === JOKER_SMALL_SUIT;
}

function cardColorClass(cardId) {
  if (isJokerCard(cardId)) return "joker-card";
  return cardParts(cardId).red ? "red" : "black";
}

function cardFaceInner(cardId) {
  const { suit, rank } = cardParts(cardId);
  if (isJokerCard(cardId)) {
    const jokerKind = suit === JOKER_BIG_SUIT ? "joker-big" : "joker-small";
    return `<span class="rank joker-rank ${jokerKind}">${escapeHtml(rank)}</span>`;
  }
  return `<span class="rank">${escapeHtml(rank)}</span><span class="suit">${escapeHtml(suit)}</span>`;
}

function cardFaceHtml(cardId, extraClass = "") {
  return `<div class="card ${cardColorClass(cardId)} ${extraClass}">${cardFaceInner(cardId)}</div>`;
}

function playerColorFor(playerId) {
  const code = String(playerId || "").split("").reduce((sum, ch) => sum + ch.charCodeAt(0), 0);
  return PLAYER_COLORS[code % PLAYER_COLORS.length];
}

function applyAvatarColor(element, playerId) {
  if (!element) return;
  element.style.setProperty("--avatar-bg", playerColorFor(playerId));
}

function renderQuickPhrases() {
  const container = $("quick-phrases");
  if (!container || container.dataset.ready === "1") return;
  container.dataset.ready = "1";
  QUICK_PHRASES.forEach((phrase) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quick-phrase";
    button.textContent = phrase;
    button.setAttribute("aria-label", `发送 ${phrase}`);
    button.addEventListener("click", () => sendChatMessage(phrase));
    container.appendChild(button);
  });
}

function clearConfetti() {
  const layer = $("confetti-layer");
  if (layer) layer.replaceChildren();
}

function showConfetti() {
  const layer = $("confetti-layer");
  if (!layer) return;
  clearConfetti();
  layer.hidden = false;
  const colors = [...PLAYER_COLORS, "#E7D8A8", "#C82F2F"];
  for (let i = 0; i < 70; i += 1) {
    const piece = document.createElement("i");
    piece.className = "confetti-piece";
    piece.style.left = `${Math.random() * 100}%`;
    piece.style.background = colors[Math.floor(Math.random() * colors.length)];
    piece.style.animationDelay = `${Math.random() * 0.8}s`;
    piece.style.animationDuration = `${2.2 + Math.random() * 1.4}s`;
    layer.appendChild(piece);
  }
  setTimeout(() => {
    clearConfetti();
    layer.hidden = true;
  }, 4200);
}

function triggerTableShake() {
  const area = $("table-area");
  if (!area) return;
  area.classList.remove("shake");
  void area.offsetWidth;
  area.classList.add("shake");
}

function animateRoundDiscard(callback) {
  const area = $("table-plays");
  if (!area) {
    if (callback) callback();
    return;
  }
  area.classList.add("discarding");
  if (renderTimeout) clearTimeout(renderTimeout);
  renderTimeout = setTimeout(() => {
    area.classList.remove("discarding");
    if (callback) callback();
  }, 320);
}

let tableNoticeTimer = null;

function hideTableNotice() {
  if (tableNoticeTimer) {
    clearTimeout(tableNoticeTimer);
    tableNoticeTimer = null;
  }
  const el = $("table-notice");
  if (el) el.hidden = true;
}

function showTableNotice(text) {
  const el = $("table-notice");
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
  el.style.animation = "none";
  void el.offsetWidth;
  el.style.animation = "";
  if (tableNoticeTimer) clearTimeout(tableNoticeTimer);
  tableNoticeTimer = setTimeout(() => {
    el.hidden = true;
    tableNoticeTimer = null;
  }, 2500);
}

/* ========== API ========== */
async function api(path, method = "GET", body = undefined) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new Error((payload && payload.error) || `请求失败 (${response.status})`);
  }
  return payload;
}

async function createRoom(name, authToken = null) {
  return api(
    "/api/rooms",
    "POST",
    authToken ? { auth_token: authToken } : { name }
  );
}

async function joinRoom(code, name, authToken = null) {
  return api(
    `/api/rooms/${encodeURIComponent(code)}/join`,
    "POST",
    authToken ? { auth_token: authToken } : { name }
  );
}

async function rejoinRoom(code, token) {
  return api(`/api/rooms/${encodeURIComponent(code)}/rejoin`, "POST", { token });
}

async function registerAccount(username, password) {
  return api("/api/auth/register", "POST", { username, password });
}

async function loginAccount(username, password) {
  return api("/api/auth/login", "POST", { username, password });
}

async function logoutAccount(authToken) {
  return api("/api/auth/logout", "POST", { auth_token: authToken });
}

async function fetchMe(authToken) {
  return api(`/api/auth/me?auth_token=${encodeURIComponent(authToken)}`);
}

async function fetchLeaderboard() {
  return api("/api/leaderboard?limit=20");
}

async function fetchPlayerProfile(accountId) {
  return api(`/api/players/${encodeURIComponent(accountId)}`);
}

async function sendAction(action, payload = {}) {
  const response = await api(
    `/api/rooms/${encodeURIComponent(state.roomCode)}/action`,
    "POST",
    { token: state.token, action, payload }
  );
  if (response && response.snapshot) {
    snapshot = response.snapshot;
    render();
  }
  return response;
}

async function loadShareUrl() {
  const el = $("share-url");
  if (!el) return;

  const localHosts = new Set(["localhost", "127.0.0.1"]);
  if (!localHosts.has(location.hostname)) {
    el.textContent = location.origin;
    return;
  }

  try {
    const health = await api("/api/health");
    el.textContent = `http://${health.lan_ip}:${health.port}`;
  } catch {
    el.textContent = "未检测到局域网地址";
  }
}

/* ========== 背景音乐 ========== */
function readVolumePreference(key, fallback) {
  try {
    const value = parseFloat(localStorage.getItem(key));
    if (!Number.isFinite(value)) return fallback;
    return Math.min(1, Math.max(0, value));
  } catch {
    return fallback;
  }
}

function writeVolumePreference(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // Ignore storage failures; the in-memory value still applies.
  }
}

function applyMusicVolume() {
  if (bgmAudio) bgmAudio.volume = musicVolume;
}

function applySfxVolume() {
  Object.values(soundAudios).forEach((audio) => {
    if (audio) audio.volume = sfxVolume;
  });
}

function updateVolumeControls() {
  const music = $("music-volume");
  const sfx = $("sfx-volume");
  if (music) music.value = String(Math.round(musicVolume * 100));
  if (sfx) sfx.value = String(Math.round(sfxVolume * 100));
  const musicLabel = $("music-volume-value");
  const sfxLabel = $("sfx-volume-value");
  if (musicLabel) musicLabel.textContent = `${Math.round(musicVolume * 100)}%`;
  if (sfxLabel) sfxLabel.textContent = `${Math.round(sfxVolume * 100)}%`;
}

function readMusicPreference() {
  try {
    return localStorage.getItem(MUSIC_STORAGE_KEY) !== "0";
  } catch {
    return true;
  }
}

function writeMusicPreference(enabled) {
  try {
    localStorage.setItem(MUSIC_STORAGE_KEY, enabled ? "1" : "0");
  } catch {
    // Ignore: storage may be blocked, but music still works for this session.
  }
}

function updateMusicButtons() {
  document.querySelectorAll(".music-toggle").forEach((button) => {
    const label = musicFailed ? "未找到 bgm.mp3" : musicOn ? "关闭音乐" : "开启音乐";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.textContent = musicOn ? "♫" : "♪";
    button.classList.toggle("active", musicOn);
    button.disabled = musicFailed;
  });

}

function isRoomViewActive() {
  return ["view-lobby", "view-game"].some((id) => {
    const view = $(id);
    return view && view.classList.contains("active");
  });
}

async function ensureMusicPlaying() {
  if (!bgmAudio || !musicOn || musicFailed || !isRoomViewActive()) return;
  if (musicPlaying) return;
  try {
    await bgmAudio.play();
  } catch {
    // Mobile browsers may reject autoplay until the next user gesture.
  }
}

function pauseMusic() {
  if (!bgmAudio) return;
  try {
    bgmAudio.pause();
  } catch {
    // Ignore: the audio element may already be detached.
  }
  bgmAudio.currentTime = 0;
  musicPlaying = false;
  updateMusicButtons();
}

async function toggleMusic() {
  if (musicFailed) return;
  musicOn = !musicOn;
  writeMusicPreference(musicOn);
  if (musicOn) {
    await ensureMusicPlaying();
  } else {
    pauseMusic();
  }
  updateMusicButtons();
}

function unlockMusic() {
  if (musicOn && isRoomViewActive()) {
    ensureMusicPlaying();
  }
}

function initMusic() {
  bgmAudio = $("bgm");
  musicOn = readMusicPreference();
  musicVolume = readVolumePreference(MUSIC_VOLUME_KEY, 0.6);
  musicFailed = !bgmAudio;
  applyMusicVolume();
  updateVolumeControls();

  if (!bgmAudio) {
    updateMusicButtons();
    return;
  }

  bgmAudio.addEventListener("playing", () => {
    musicPlaying = true;
    musicFailed = false;
    updateMusicButtons();
  });
  bgmAudio.addEventListener("pause", () => {
    musicPlaying = false;
    updateMusicButtons();
  });
  bgmAudio.addEventListener("error", () => {
    musicPlaying = false;
    musicFailed = true;
    updateMusicButtons();
  });

  window.addEventListener("pointerdown", unlockMusic);
  updateMusicButtons();
}


/* ========== 身份存储 ========== */

/* ========== 音效 ========== */
function initSounds() {
  sfxVolume = readVolumePreference(SFX_VOLUME_KEY, 1);
  Object.keys(SOUND_FILES).forEach((name) => {
    const audio = document.getElementById(`sfx-${name}`);
    if (!audio) return;
    audio.volume = sfxVolume;
    soundAudios[name] = audio;
  });
  updateVolumeControls();
  window.addEventListener("pointerdown", unlockSound);
}

function unlockSound() {
  Object.values(soundAudios).forEach((audio) => {
    if (!audio) return;
    audio.volume = sfxVolume;
    if (audio.readyState === 0) audio.load();
  });
}

function playSound(name) {
  const audio = soundAudios[name];
  if (!audio) return;
  try {
    audio.volume = sfxVolume;
    audio.currentTime = 0;
    const pending = audio.play();
    if (pending && pending.catch) pending.catch(() => {});
  } catch {
    // Ignore autoplay or missing audio failures.
  }
}

function stopOutcomeSound() {
  ["win", "lose"].forEach((name) => {
    const audio = soundAudios[name];
    if (!audio) return;
    try {
      audio.pause();
      audio.currentTime = 0;
    } catch {
      // Ignore detached audio elements.
    }
  });
}

function playOutcomeSound(isWin) {
  stopOutcomeSound();
  playSound(isWin ? "win" : "lose");
}


function loadAuthToken() {
  try {
    return localStorage.getItem(AUTH_STORAGE_KEY);
  } catch {
    return null;
  }
}

function saveAuthToken(token) {
  try {
    if (token) {
      localStorage.setItem(AUTH_STORAGE_KEY, token);
    } else {
      localStorage.removeItem(AUTH_STORAGE_KEY);
    }
  } catch {
    // Ignore storage failures; the in-memory auth token still works.
  }
}

function loadIdentity() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function saveIdentity(identity) {
  state.roomCode = identity.room_code;
  state.token = identity.player_token;
  state.playerId = identity.player_id;
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      room_code: identity.room_code,
      player_token: identity.player_token,
      player_id: identity.player_id,
    })
  );
}

function clearIdentity() {
  state.roomCode = null;
  state.token = null;
  state.playerId = null;
  localStorage.removeItem(STORAGE_KEY);
}

function applyIdentity(response) {
  saveIdentity(response);
  snapshot = response.snapshot;
}

/* ========== 账号与排行榜 ========== */
function renderAuth() {
  const credentials = $("auth-credentials");
  const logged = $("account-logged");
  const guestArea = $("guest-area");
  const submit = $("btn-auth-submit");

  document.querySelectorAll(".auth-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.authMode === authMode);
  });

  const usernameHint = $("username-hint");
  if (usernameHint) {
    usernameHint.hidden = state.account ? true : authMode !== "register";
  }

  if (state.account) {
    $("account-username").textContent = state.account.username;
    credentials.hidden = true;
    logged.hidden = false;
    guestArea.hidden = true;
    $("auth-error").textContent = "";
    return;
  }

  logged.hidden = true;
  if (authMode === "guest") {
    credentials.hidden = true;
    guestArea.hidden = false;
    $("auth-error").textContent = "";
  } else {
    credentials.hidden = false;
    guestArea.hidden = true;
    submit.textContent = authMode === "register" ? "注册" : "登录";
    $("auth-username").autocomplete = "username";
    $("auth-password").autocomplete = authMode === "register" ? "new-password" : "current-password";
  }
}

function setAuthMode(mode) {
  authMode = mode;
  renderAuth();
}

async function restoreAuth() {
  const token = loadAuthToken();
  if (!token) {
    renderAuth();
    return;
  }
  try {
    const response = await fetchMe(token);
    state.authToken = token;
    state.account = response.account;
  } catch {
    clearAuthToken();
    state.authToken = null;
    state.account = null;
  }
  renderAuth();
}

async function handleAuthSubmit() {
  const username = $("auth-username").value.trim();
  const password = $("auth-password").value;
  if (!username || !password) {
    showError("auth-error", "请填写账号和密码");
    return;
  }
  const button = $("btn-auth-submit");
  button.disabled = true;
  try {
    const response = authMode === "register"
      ? await registerAccount(username, password)
      : await loginAccount(username, password);
    state.authToken = response.auth_token;
    state.account = response.account;
    saveAuthToken(state.authToken);
    showError("auth-error", "");
    renderAuth();
    toast(authMode === "register" ? "注册成功" : "登录成功");
  } catch (error) {
    showError("auth-error", error.message);
  } finally {
    button.disabled = false;
  }
}

async function handleLogout() {
  const token = state.authToken;
  state.authToken = null;
  state.account = null;
  clearAuthToken();
  renderAuth();
  toast("已退出登录");

  if (token) {
    try {
      await logoutAccount(token);
    } catch {
      // Server-side logout is best-effort; local auth is already cleared.
    }
  }
}

function formatRate(value) {
  return `${Math.round((Number(value) || 0) * 100)}%`;
}

function avatarInitial(name) {
  return escapeHtml((name || "?").charAt(0) || "?");
}

function podiumCardHtml(item, rank) {
  const username = item.username || "玩家";
  const stats = item.stats || {};
  const badge = rank === 1 ? "👑" : rank === 2 ? "🥈" : "🥉";
  return `<div class="podium-card rank-${rank}" data-account-id="${escapeHtml(item.account_id)}">
    <div class="podium-badge">${badge}</div>
    <div class="podium-avatar" style="--avatar-bg:${playerColorFor(item.account_id)}">${avatarInitial(username)}</div>
    <div class="podium-name">${escapeHtml(username)}</div>
    <div class="podium-metric">${Number(stats.wins || 0)} 胜 · ${Number(stats.games_played || 0)} 局</div>
  </div>`;
}

function leaderboardHtml(players) {
  if (!players || !players.length) {
    return `<p class="empty-text">暂无上榜玩家</p>`;
  }

  const top = players.slice(0, 3);
  const ordered = [top[1] || null, top[0] || null, top[2] || null];
  const podiumHtml = `<div class="podium">${ordered.map((item) => {
    return item ? podiumCardHtml(item, players.indexOf(item) + 1) : `<div class="podium-slot"></div>`;
  }).join("")}</div>`;

  const restHtml = players.slice(3).map((item, index) => {
    const rank = index + 4;
    const stats = item.stats || {};
    return `<button class="board-item" data-account-id="${escapeHtml(item.account_id)}" type="button">
      <span class="board-rank">${rank}</span>
      <span class="board-avatar" style="--avatar-bg:${playerColorFor(item.account_id)}">${avatarInitial(item.username)}</span>
      <span class="board-name">${escapeHtml(item.username)}</span>
      <span class="board-stat">${Number(stats.games_played || 0)} 局 · ${Number(stats.wins || 0)} 胜</span>
      <span class="board-rate">${formatRate(stats.win_rate)}</span>
    </button>`;
  }).join("");

  return `<div class="leaderboard-view">
    <div class="board-heading"><span>🏆 排行榜</span><small>胜场优先 · 胜率次之</small></div>
    ${podiumHtml}
    ${restHtml ? `<div class="board-list">${restHtml}</div>` : ""}
  </div>`;
}

function profileStatCard(label, value, sublabel) {
  return `<div class="stat-card">
    <span>${label}</span>
    <strong>${value}</strong>
    <small>${sublabel}</small>
  </div>`;
}

function profileHtml(account, recentMatches, rank) {
  const stats = account.stats || {};
  const hero = `<div class="profile-hero">
    <div class="profile-avatar" style="--avatar-bg:${playerColorFor(account.id)}">${avatarInitial(account.username)}</div>
    <div class="profile-id">
      <h3>${escapeHtml(account.username)}</h3>
      <p>${rank ? `当前排名 #${rank}` : "暂无排名"}</p>
    </div>
  </div>`;

  const cards = `<div class="profile-stats">
    ${profileStatCard("总局数", Number(stats.games_played || 0), "已完成对局")}
    ${profileStatCard("胜场", Number(stats.wins || 0), `胜率 ${formatRate(stats.win_rate)}`)}
    ${profileStatCard("胜率", formatRate(stats.win_rate), `${Number(stats.wins || 0)} 胜`)}
    ${profileStatCard("吹牛成功", `${Number(stats.bluff_successes || 0)}/${Number(stats.bluff_attempts || 0)}`, `成功率 ${formatRate(stats.bluff_success_rate)}`)}
    ${profileStatCard("吹牛成功率", formatRate(stats.bluff_success_rate), `${Number(stats.bluff_attempts || 0)} 次吹牛`)}
    ${profileStatCard("质疑准确率", formatRate(stats.challenge_accuracy), `${Number(stats.challenge_successes || 0)}/${Number(stats.challenge_attempts || 0)} 成功`)}
  </div>`;

  const recentHtml = recentMatches.length
    ? recentMatches.map((match) => `<li class="recent-match">
        <span class="recent-winner">🏆 ${escapeHtml(match.winner_name)}</span>
        <span class="recent-meta">房间 ${escapeHtml(match.room_code)} · ${escapeHtml((match.finished_at || "").slice(0, 10))}</span>
      </li>`).join("")
    : `<li class="recent-empty">暂无对局记录</li>`;

  return `<div class="profile-view">
    ${hero}
    ${cards}
    <h4 class="section-title">最近对局</h4>
    <ul class="recent-list">${recentHtml}</ul>
  </div>`;
}
async function openLeaderboard() {
  try {
    const response = await fetchLeaderboard();
    setModal("排行榜", leaderboardHtml(response.players || []), "关闭", true);
    document.querySelectorAll("[data-account-id]").forEach((element) => {
      element.addEventListener("click", () => showPlayerProfile(element.dataset.accountId));
    });
  } catch (error) {
    toast(error.message);
  }
}

async function showPlayerProfile(accountId) {
  try {
    const [response, boardResponse] = await Promise.all([
      fetchPlayerProfile(accountId),
      fetchLeaderboard(),
    ]);
    const account = response.account;
    const recent = response.recent_matches || [];
    const rankIndex = (boardResponse.players || []).findIndex((player) => player.account_id === accountId);
    const rank = rankIndex >= 0 ? rankIndex + 1 : null;
    setModal(
      `${account.username} 的战绩`,
      profileHtml(account, recent, rank),
      "关闭",
      true
    );
  } catch (error) {
    toast(error.message);
  }
}

function playOutcomeText(play, challenge) {
  if (!play.is_bluff) return "如实";
  if (challenge && challenge.target_play_id === play.play_id && challenge.success) {
    return "吹牛失败";
  }
  return "吹牛成功";
}

function gameReportHtml(report) {
  if (!report) return "";

  const totals = {
    challenge_attempts: 0,
    challenge_successes: 0,
    bluff_attempts: 0,
    bluff_successes: 0,
  };

  (report.players || []).forEach((player) => {
    const stats = player.stats || {};
    totals.challenge_attempts += Number(stats.challenge_attempts || 0);
    totals.challenge_successes += Number(stats.challenge_successes || 0);
    totals.bluff_attempts += Number(stats.bluff_attempts || 0);
    totals.bluff_successes += Number(stats.bluff_successes || 0);
  });

  return `<div class="report-summary">
    <div><span>质疑次数</span><strong>${totals.challenge_attempts}</strong></div>
    <div><span>质疑成功</span><strong>${totals.challenge_successes}</strong></div>
    <div><span>吹牛次数</span><strong>${totals.bluff_attempts}</strong></div>
    <div><span>吹牛成功</span><strong>${totals.bluff_successes}</strong></div>
  </div>`;
}

/* ========== 结果弹窗 ========== */

/* ========== SSE 与牌桌渲染 ========== */
function connectStream() {
  if (!state.roomCode || !state.token) return;
  if (eventSource) eventSource.close();
  eventSource = new EventSource(
    `/api/rooms/${encodeURIComponent(state.roomCode)}/stream?token=${encodeURIComponent(state.token)}`
  );
  eventSource.onmessage = handleSSEEvent;
  eventSource.onerror = () => {
    if (eventSource && eventSource.readyState === EventSource.CLOSED) {
      toast("连接已断开，请重新加入房间");
      clearIdentity();
      snapshot = null;
      showView("view-home");
    }
  };
}

function handleSSEEvent(rawEvent) {
  let event = null;
  try {
    event = JSON.parse(rawEvent.data);
  } catch {
    return;
  }

  const type = event.type;
  const data = event.data || {};
  const incoming = event.snapshot || null;
  const previous = snapshot;

  if (type === "snapshot") {
    snapshot = incoming || snapshot;
    render();
    showGameOverIfFinished();
    return;
  }

  switch (type) {
    case "played": {
      const play = data.play || {};
      pendingPlayAnimationId = play.play_id ?? null;
      snapshot = incoming || snapshot;
      render();
      pendingPlayAnimationId = null;
      playSound("play");
      break;
    }
    case "passed": {
      const oldPile = previous && previous.game && previous.game.table ? previous.game.table.length : 0;
      const newPile = incoming && incoming.game && incoming.game.table ? incoming.game.table.length : 0;
      if (oldPile > 0 && newPile === 0) {
        // 全员过牌：无质疑结束，本轮牌面清空。
        snapshot = incoming;
        animateRoundDiscard(() => {
          render();
          showTableNotice("无人质疑，本轮牌面清空");
        });
      } else {
        snapshot = incoming || snapshot;
        render();
      }
      break;
    }
    case "round_ended": {
      const oldPile = previous && previous.game && previous.game.table ? previous.game.table.length : 0;
      const newPile = incoming && incoming.game && incoming.game.table ? incoming.game.table.length : 0;
      if (data.reason === "completed" && oldPile > 0 && newPile === 0) {
        snapshot = incoming;
        animateRoundDiscard(() => {
          render();
          showTableNotice("无人质疑，本轮牌面清空");
        });
      } else {
        snapshot = incoming || snapshot;
        render();
      }
      break;
    }
    case "challenged": {
      snapshot = incoming || snapshot;
      render();
      showChallengeResult(data.result);
      if (data.result && data.result.truth === false) triggerTableShake();
      break;
    }
    case "game_over": {
      snapshot = incoming || snapshot;
      render();
      showGameOverIfFinished();
      break;
    }
    case "chat_message": {
      snapshot = incoming || snapshot;
      renderChat();
      const message = data.message;
      if (message && message.player_id !== state.playerId) {
        showChatPopup(message);
      }
      break;
    }
    case "game_started": {
      shownGameOverRoomWinner = null;
      selectedCardIds.clear();
      selectedRank = null;
      hideTableNotice();
      snapshot = incoming || snapshot;
      render();
      break;
    }
    case "player_joined":
    case "player_left": {
      snapshot = incoming || snapshot;
      render();
      break;
    }
    case "error": {
      toast(data.error || "操作失败");
      if (incoming) {
        snapshot = incoming;
        render();
      }
      break;
    }
    default:
      if (incoming) {
        snapshot = incoming;
        render();
      }
  }
}

function showGameOverIfFinished() {
  if (
    !snapshot ||
    snapshot.state !== "finished" ||
    !snapshot.game ||
    !snapshot.game.winner_id
  ) {
    return;
  }
  const key = `${snapshot.room_code}:${snapshot.game.winner_id}`;
  if (shownGameOverRoomWinner === key) return;
  shownGameOverRoomWinner = key;
  showGameOver(snapshot.game.winner_id);
}

function isMyTurn() {
  return Boolean(
    snapshot &&
      snapshot.state === "playing" &&
      snapshot.you &&
      snapshot.game &&
      snapshot.game.current_player_id === snapshot.you.player_id
  );
}

function render() {
  if (!snapshot) {
    showView("view-home");
    return;
  }

  if (snapshot.state === "waiting") {
    renderLobby();
    showView("view-lobby");
  } else {
    renderGame();
    showView("view-game");
  }

  renderChat();
  renderQuickPhrases();
  renderEmojiBar();

  if (snapshot.state === "playing") {
    startTimer();
  } else {
    updateTimer();
  }

}

function lobbyPlayerHtml(player, canManageAi) {
  const meta = [];
  const isAi = Boolean(player.is_ai);
  if (player.is_host) meta.push("房主");
  if (isAi) {
    meta.push(`🤖 AI · ${aiStrengthLabel(player.ai_strength)}`);
  } else {
    meta.push(player.connected ? "在线" : "离线");
  }
  let controls = "";
  if (isAi && canManageAi) {
    const chips = AI_TIERS.map(
      (tier) =>
        `<button type="button" class="strength-chip ${tier.key === player.ai_strength ? "selected" : ""}" data-ai-set="${tier.key}" data-ai-player="${escapeHtml(player.id)}">${tier.label}</button>`
    ).join("");
    controls = `<div class="ai-tile-controls">${chips}<button type="button" class="ai-remove-btn" data-ai-remove="${escapeHtml(player.id)}">移除</button></div>`;
  }
  return `<div class="player-tile ${isAi ? "ai-tile" : ""}">
    <div class="player-avatar" style="--avatar-bg:${playerColorFor(player.id)}">${isAi ? "🤖" : avatarInitial(player.name)}</div>
    <div style="min-width:0">
      <div class="player-name">${escapeHtml(player.name)}</div>
      <div class="player-meta">${meta.join(" · ")}</div>
      ${controls}
    </div>
  </div>`;
}

function bindAiTileActions(container) {
  container.querySelectorAll("[data-ai-remove]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await sendAction("remove_ai", { player_id: button.dataset.aiRemove });
      } catch (error) {
        toast(error.message);
        button.disabled = false;
      }
    });
  });
  container.querySelectorAll("[data-ai-set]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await sendAction("ai_strength", {
          player_id: button.dataset.aiPlayer,
          strength: button.dataset.aiSet,
        });
      } catch (error) {
        toast(error.message);
        button.disabled = false;
      }
    });
  });
}

function initStrengthPickerOnce() {
  const picker = $("ai-strength-picker");
  if (!picker || picker.dataset.ready === "1") return;
  picker.dataset.ready = "1";
  AI_TIERS.forEach((tier) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "strength-chip";
    chip.dataset.aiChoose = tier.key;
    chip.textContent = tier.label;
    chip.addEventListener("click", () => {
      aiStrength = tier.key;
      syncStrengthPicker();
    });
    picker.appendChild(chip);
  });
}

function syncStrengthPicker() {
  const picker = $("ai-strength-picker");
  if (!picker) return;
  picker.querySelectorAll("[data-ai-choose]").forEach((chip) => {
    chip.classList.toggle("selected", chip.dataset.aiChoose === aiStrength);
  });
}

function renderLobby() {
  const players = snapshot.players || [];
  const you = snapshot.you || {};
  const canManageAi = Boolean(you.is_host);
  $("room-code").textContent = snapshot.room_code;
  $("lobby-status").textContent = `${players.length} / 6 人 · 至少 2 人，最多 6 人`;
  const list = $("player-list");
  list.innerHTML = players.map((p) => lobbyPlayerHtml(p, canManageAi && p.is_ai)).join("");
  bindAiTileActions(list);
  $("btn-start").disabled = !(canManageAi && players.length >= 2);

  const controls = $("ai-controls");
  if (controls) controls.hidden = !canManageAi;
  initStrengthPickerOnce();
  syncStrengthPicker();
}

function opponentHtml(player) {
  const active = snapshot.game.current_player_id === player.id;
  const passed = (snapshot.game.round_passers || []).includes(player.id);
  const isAi = Boolean(player.is_ai);
  let stateText;
  if (isAi) {
    stateText = `🤖 ${aiStrengthLabel(player.ai_strength)}`;
  } else {
    stateText = passed ? "已过牌" : player.connected ? "在线" : "离线";
  }
  return `<div class="opponent ${active ? "active turn-pulse" : ""} ${passed ? "passed" : ""}">
    <div class="avatar" style="--avatar-bg:${playerColorFor(player.id)}">${isAi ? "🤖" : avatarInitial(player.name)}</div>
    <div class="opp-name">${escapeHtml(player.name)}</div>
    <div class="opp-count">${Number(player.hand_count || 0)} 张 · ${stateText}</div>
  </div>`;
}

function aiStrengthLabel(strength) {
  const tier = AI_TIERS.find((t) => t.key === strength);
  return tier ? tier.label : strength || "AI";
}

function tablePlayHtml(play, isLatest) {
  const entering = pendingPlayAnimationId === play.play_id;
  const backs = Array.from(
    { length: Number(play.count || 0) },
    () => `<span class="card-back">♠</span>`
  ).join("");
  return `<div class="table-play ${isLatest ? "table-play--latest" : ""} ${entering ? "table-play--entering" : ""}">
    <span class="play-owner">${escapeHtml(play.player_name)}</span>
    <span class="table-play-cards">${backs}</span>
    <span class="play-declared">报 ${escapeHtml(play.declared_rank)} · ${Number(play.count || 0)} 张</span>
  </div>`;
}

function handCardHtml(cardId) {
  const selected = selectedCardIds.has(cardId);
  const selectable = isMyTurn();
  return `<button type="button" class="card ${cardColorClass(cardId)} ${selectable ? "selectable" : ""} ${selected ? "selected" : ""}" data-card-id="${escapeHtml(cardId)}" aria-pressed="${selected}">
    ${cardFaceInner(cardId)}
  </button>`;
}

function renderGame() {
  const game = snapshot.game || {};
  const players = snapshot.players || [];
  const canAct = isMyTurn();
  if (!canAct) {
    selectedCardIds.clear();
    selectedRank = null;
  }

  $("game-room-code").textContent = snapshot.room_code;
  $("game-round").textContent = String((Number(game.round_seq) || 0) + 1);
  $("game-declared").textContent = game.declared_rank || "待首家报点";

  const currentPlayer = players.find((player) => player.id === game.current_player_id);
  $("game-turn").textContent = currentPlayer
    ? currentPlayer.is_ai
      ? `${currentPlayer.name} 思考中…`
      : currentPlayer.name
    : snapshot.state === "finished"
      ? "游戏结束"
      : "--";

  const opponents = players.filter((player) => player.id !== snapshot.you.player_id);
  $("opponents").innerHTML = opponents.map(opponentHtml).join("");

  const plays = game.table || [];
  const latest = plays.length ? plays[plays.length - 1] : null;
  $("table-plays").innerHTML = plays.length
    ? plays.map((play) => tablePlayHtml(play, play === latest)).join("")
    : `<div class="empty-table">桌面暂无牌</div>`;

  const hand = snapshot.you.hand || [];
  Array.from(selectedCardIds).forEach((cardId) => {
    if (!hand.includes(cardId)) selectedCardIds.delete(cardId);
  });
  $("hand-count").textContent = `${hand.length} 张`;
  $("hand-area").innerHTML = hand.length
    ? hand.map(handCardHtml).join("")
    : `<div class="empty-table">没有手牌</div>`;

  const declaredRank = game.declared_rank || null;
  const hasDeclared = Boolean(declaredRank);
  const canPickRank = canAct && !hasDeclared;
  $("rank-picker").innerHTML = RANKS.map(
    (rank) =>
      `<button type="button" class="rank-btn ${selectedRank === rank ? "selected" : ""}" data-rank="${rank}" ${canPickRank ? "" : "disabled"}>${rank}</button>`
  ).join("");

  $("selected-count").textContent = `已选 ${selectedCardIds.size} 张`;
  $("btn-play").disabled = !(
    canAct &&
    selectedCardIds.size >= 1 &&
    selectedCardIds.size <= MAX_CARDS_PER_PLAY &&
    (declaredRank || selectedRank)
  );

  // 质疑对象固定为“上一手出牌的人”（最近一手）。
  const latestIsMine = Boolean(latest && latest.player_id === snapshot.you.player_id);
  const canChallenge = Boolean(canAct && hasDeclared && latest && !latestIsMine);
  const passButton = $("btn-pass-round");
  // 过牌 = 本轮出局；首家开局（尚未报点）不能过。
  passButton.disabled = !(canAct && hasDeclared);
  passButton.title = "过牌后本轮不再出牌/质疑，直到本轮结束";
  $("btn-challenge").disabled = !canChallenge;

  const label = $("challenge-target-label");
  if (canChallenge) {
    label.textContent = `质疑上一手：${latest.player_name} 的 ${latest.count} 张牌`;
  } else if (latestIsMine) {
    label.textContent = "上一手是你出的，只能继续出牌或过牌";
  } else if (!hasDeclared) {
    label.textContent = "首家请先出牌并报点";
  } else {
    label.textContent = canAct
      ? "当前不可质疑"
      : `等待 ${(currentPlayer && currentPlayer.name) || "--"} 行动`;
  }

  document.querySelectorAll("#hand-area [data-card-id]").forEach((button) => {
    button.addEventListener("click", () => toggleCard(button.dataset.cardId));
  });
  document.querySelectorAll("#rank-picker [data-rank]").forEach((button) => {
    button.addEventListener("click", () => selectRank(button.dataset.rank));
  });
}

function toggleCard(cardId) {
  if (!isMyTurn()) return;
  if (selectedCardIds.has(cardId)) {
    selectedCardIds.delete(cardId);
  } else {
    if (selectedCardIds.size >= MAX_CARDS_PER_PLAY) {
      toast(`一次最多出 ${MAX_CARDS_PER_PLAY} 张`);
      return;
    }
    selectedCardIds.add(cardId);
  }
  renderGame();
}

function selectRank(rank) {
  if (!isMyTurn() || (snapshot.game && snapshot.game.declared_rank)) return;
  selectedRank = rank;
  renderGame();
}

function startTimer() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(updateTimer, 500);
  updateTimer();
}

function updateTimer() {
  const el = $("game-timer");
  if (!el) return;
  if (
    !snapshot ||
    snapshot.state !== "playing" ||
    !snapshot.game ||
    !snapshot.game.turn_deadline
  ) {
    el.textContent = "--";
    return;
  }
  const seconds = Math.max(
    0,
    Math.ceil(snapshot.game.turn_deadline - Date.now() / 1000)
  );
  el.textContent = `${seconds}s`;
}

function renderChat() {
  const container = $("chat-messages");
  if (!container) return;
  const messages = (snapshot && snapshot.chat_messages) || [];
  container.innerHTML = messages
    .map(
      (message) =>
        `<div class="chat-message ${message.player_id === state.playerId ? "mine" : ""}">
          <div class="chat-author">${escapeHtml(message.player_name)}</div>
          <div class="chat-text">${escapeHtml(message.text)}</div>
        </div>`
    )
    .join("");
  container.scrollTop = container.scrollHeight;
}

function renderEmojiBar() {
  const container = $("emoji-bar");
  if (!container || container.dataset.ready === "1") return;
  container.dataset.ready = "1";
  EMOJIS.forEach((emoji) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "emoji-btn";
    button.textContent = emoji;
    button.setAttribute("aria-label", `插入 ${emoji}`);
    button.addEventListener("click", () => {
      const input = $("chat-input");
      if (!input) return;
      input.value += emoji;
      input.focus();
    });
    container.appendChild(button);
  });
}

function toggleChat(force) {
  chatOpen = force === undefined ? !chatOpen : force;
  const drawer = $("chat-drawer");
  if (drawer) drawer.hidden = !chatOpen;
  document.querySelectorAll(".chat-toggle").forEach((button) => {
    button.classList.toggle("active", chatOpen);
  });
  if (chatOpen && $("chat-input")) $("chat-input").focus();
}

async function sendChatMessage(text) {
  const message = String(text || "").trim();
  if (!message) return;
  const input = $("chat-input");
  if (input) input.value = "";
  try {
    await sendAction("chat", { text: message });
  } catch (error) {
    toast(error.message);
  }
}

function showChatPopup(message) {
  const container = $("chat-popups");
  if (!container) return;
  const popup = document.createElement("button");
  popup.type = "button";
  popup.className = "chat-popup";
  popup.innerHTML = `<strong>${escapeHtml(message.player_name)}</strong><span>${escapeHtml(message.text)}</span>`;
  popup.addEventListener("click", () => toggleChat(true));
  container.appendChild(popup);
  while (container.children.length > 3) container.firstElementChild.remove();
  setTimeout(() => popup.remove(), 5000);
}

function clearChatPopups() {
  const container = $("chat-popups");
  if (container) container.replaceChildren();
}

function showChallengeResult(result) {
  if (!result) return;
  const pileCards = (result.pile_cards || result.actual_cards || []);
  const cards = pileCards
    .map((cardId) => cardFaceHtml(cardId))
    .join("");

  const outcome = result.truth
    ? `说的是真话，质疑者 <strong>${escapeHtml(result.challenger_name)}</strong> 拿回全桌牌。`
    : `是假的，被质疑者 <strong>${escapeHtml(result.target_player_name)}</strong> 拿回全桌牌。`;
  const loserName = result.truth
    ? result.challenger_name
    : result.target_player_name;
  const pileCount = result.pile_count ?? pileCards.length;

  setModal(
    "质疑结果",
    `
      <p><strong>${escapeHtml(result.target_player_name)}</strong> 报了 ${escapeHtml(result.declared_rank)}。本轮桌面共 <strong>${pileCount}</strong> 张牌，全部归 <strong>${escapeHtml(loserName)}</strong>。</p>
      <div class="reveal-cards">${cards}</div>
      <p>${outcome}</p>
    `,
    "继续"
  );
}

function showGameOver(winnerId) {
  const winner = snapshot.players.find((p) => p.id === winnerId);
  const winnerName = winner ? winner.name : "某位玩家";
  const isMe = winnerId === state.playerId;
  if (isMe) {
    playOutcomeSound(true);
    showConfetti();
  } else {
    playOutcomeSound(false);
  }
  const title = isMe ? "你赢了 🎉" : "游戏结束";
  const report = snapshot.game && snapshot.game.report;
  const body = report
    ? gameReportHtml(report)
    : `<p>获胜者：<strong>${escapeHtml(winnerName)}</strong></p>`;
  // 不再由房主直接开下一局：所有人先返回本房间的等待房，房主在那里点「开始游戏」。
  const returnButton = `<button class="btn btn-primary btn-block" id="btn-return-lobby" type="button">返回等待房间</button>`;
  setModal(title, `${body}${returnButton}`, "关闭");

  const returnBtn = $("btn-return-lobby");
  if (returnBtn) {
    returnBtn.addEventListener("click", handleReturnToLobby);
  }
}

async function handleReturnToLobby() {
  if (!snapshot) return;
  const button = $("btn-return-lobby");
  if (button) button.disabled = true;
  try {
    await sendAction("return_lobby");
    stopOutcomeSound();
    clearConfetti();
    ensureMusicPlaying();
    closeModal();
  } catch (error) {
    toast(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

/* ========== 房间列表（加入房间浏览器） ========== */
const ROOM_LIST_REFRESH_MS = 3000;
let roomListTimer = null;
let lastRoomListJson = null;

function stopRoomsAutoRefresh() {
  if (roomListTimer) {
    clearInterval(roomListTimer);
    roomListTimer = null;
  }
}

function guestNameForJoin() {
  if (state.authToken) return "";
  const nick = $("rooms-nick");
  const home = $("input-name");
  const value = ((nick && nick.value.trim()) || (home && home.value.trim())) || "";
  if (nick) nick.value = value;
  if (home) home.value = value;
  return value;
}

async function completeRoomJoin(response) {
  stopRoomsAutoRefresh();
  applyIdentity(response);
  connectStream();
  render();
}

async function handleCodeJoin() {
  const code = $("input-code").value.trim();
  if (!code) {
    showError("rooms-error", "请填写房间码");
    return;
  }
  const name = guestNameForJoin();
  if (!state.authToken && !name) {
    showError("rooms-error", "请先在上方填写游客昵称");
    return;
  }
  $("btn-join").disabled = true;
  try {
    const response = await joinRoom(code, name, state.authToken);
    showError("rooms-error", "");
    toast("已加入房间");
    await completeRoomJoin(response);
  } catch (error) {
    showError("rooms-error", error.message);
    refreshRoomList();
  } finally {
    $("btn-join").disabled = false;
  }
}

function openRoomBrowser() {
  stopRoomsAutoRefresh();
  const nickWrap = $("rooms-nick-wrap");
  if (nickWrap) nickWrap.hidden = Boolean(state.account);
  const nick = $("rooms-nick");
  const home = $("input-name");
  if (nick && home && !state.account) nick.value = home.value;
  showError("rooms-error", "");
  $("input-code").value = "";
  lastRoomListJson = null;
  const container = $("room-list");
  if (container) {
    container.innerHTML = `<p class="empty-text rooms-state-text">加载中…</p>`;
  }
  const status = $("rooms-status");
  if (status) status.textContent = "";
  showView("view-rooms");
  refreshRoomList();
  roomListTimer = setInterval(refreshRoomList, ROOM_LIST_REFRESH_MS);
}

function closeRoomBrowser() {
  stopRoomsAutoRefresh();
  showView("view-home");
}

function renderRoomListError(message) {
  const container = $("room-list");
  if (!container) return;
  const status = $("rooms-status");
  if (status) status.textContent = "";
  container.innerHTML = `
    <div class="rooms-state-box">
      <p class="empty-text">${escapeHtml(message)}</p>
      <button class="btn btn-secondary" id="rooms-retry" type="button">重试</button>
    </div>`;
  const retry = $("rooms-retry");
  if (retry) {
    retry.addEventListener("click", async () => {
      lastRoomListJson = null;
      const area = $("room-list");
      if (area) area.innerHTML = `<p class="empty-text rooms-state-text">加载中…</p>`;
      await refreshRoomList();
    });
  }
}

async function refreshRoomList() {
  const status = $("rooms-status");
  try {
    const response = await api("/api/rooms");
    const json = JSON.stringify(response.rooms || []);
    if (json !== lastRoomListJson) {
      lastRoomListJson = json;
      renderRoomList(response.rooms || [], Number(response.max_players) || 6);
    }
    if (status) status.textContent = "自动刷新中…";
  } catch (error) {
    const container = $("room-list");
    const hasRows = container && container.querySelector(".room-row");
    if (hasRows) {
      if (status) status.textContent = "刷新失败，请点上方「刷新」重试";
    } else {
      renderRoomListError(error && error.message ? error.message : "网络异常，请稍后重试");
    }
  }
}

function roomRowHtml(room, maxPlayers) {
  const code = escapeHtml(room.room_code || "----");
  const host = escapeHtml(room.host_player_name || "未知房主");
  const count = Math.max(0, Math.min(maxPlayers, Number(room.player_count) || 0));
  const waiting = room.state === "waiting";
  const full = count >= maxPlayers;
  const dots = Array.from(
    { length: maxPlayers },
    (_, index) => `<i class="occ-dot${index < count ? " filled" : ""}"></i>`
  ).join("");
  let action = "";
  if (waiting && !full) {
    action = `<button type="button" class="btn btn-primary room-join-btn" data-room-code="${escapeHtml(room.room_code)}">加入</button>`;
  } else {
    action = `<span class="room-locked">${waiting ? "已满" : "对局中"}</span>`;
  }
  const badge = waiting
    ? `<span class="room-state-badge waiting">等待中</span>`
    : `<span class="room-state-badge playing">对局中</span>`;
  return `<div class="room-row">
    <span class="room-row-code">${code}</span>
    <div class="room-row-meta">
      <span class="room-host">${host} ${badge}</span>
      <span class="room-occupancy">
        <span class="occ-dots">${dots}</span>
        <span class="occ-text">${count}/${maxPlayers}</span>
      </span>
    </div>
    ${action}
  </div>`;
}

function roomGroupHtml(title, rooms, maxPlayers) {
  return `<div class="rooms-group">
    <h4 class="rooms-group-title">${escapeHtml(title)} · ${rooms.length}</h4>
    ${rooms.map((room) => roomRowHtml(room, maxPlayers)).join("")}
  </div>`;
}

function renderRoomList(rooms, maxPlayers) {
  const container = $("room-list");
  if (!container) return;
  const safeMax = Number(maxPlayers) > 0 ? Number(maxPlayers) : 6;
  if (!rooms || !rooms.length) {
    container.innerHTML = `<p class="empty-text rooms-state-text">当前没有房间，去首页创建一间吧</p>`;
    return;
  }
  const waiting = rooms.filter((room) => room.state === "waiting");
  const playing = rooms.filter((room) => room.state !== "waiting");
  const groups = [];
  if (waiting.length) groups.push(roomGroupHtml("等待加入", waiting, safeMax));
  if (playing.length) groups.push(roomGroupHtml("对局中", playing, safeMax));
  container.innerHTML = groups.join("");

  container.querySelectorAll(".room-join-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const code = String(button.dataset.roomCode || "");
      if (!code) return;
      const name = guestNameForJoin();
      if (!state.authToken && !name) {
        showError("rooms-error", "请先在上方填写游客昵称");
        return;
      }
      button.disabled = true;
      try {
        const response = await joinRoom(code, name, state.authToken);
        showError("rooms-error", "");
        toast("已加入房间");
        await completeRoomJoin(response);
      } catch (error) {
        toast(error.message);
        refreshRoomList();
      }
    });
  });
}

/* ========== 交互绑定 ========== */
function bindEvents() {
  document.querySelectorAll(".music-toggle").forEach((button) => {
    button.addEventListener("click", toggleMusic);
  });

  const musicVolumeInput = $("music-volume");
  if (musicVolumeInput) {
    musicVolumeInput.addEventListener("input", () => {
      musicVolume = Number(musicVolumeInput.value) / 100;
      writeVolumePreference(MUSIC_VOLUME_KEY, musicVolume);
      applyMusicVolume();
      updateVolumeControls();
    });
  }

  const sfxVolumeInput = $("sfx-volume");
  if (sfxVolumeInput) {
    sfxVolumeInput.addEventListener("input", () => {
      sfxVolume = Number(sfxVolumeInput.value) / 100;
      writeVolumePreference(SFX_VOLUME_KEY, sfxVolume);
      applySfxVolume();
      updateVolumeControls();
    });
  }

  document.querySelectorAll(".chat-toggle").forEach((button) => {
    button.addEventListener("click", () => toggleChat());
  });
  $("btn-close-chat").addEventListener("click", () => toggleChat(false));

  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("chat-input");
    if (!input) return;
    sendChatMessage(input.value);
  });

  document.querySelectorAll(".auth-tab").forEach((button) => {
    button.addEventListener("click", () => setAuthMode(button.dataset.authMode));
  });
  const passwordToggle = $("btn-toggle-password");
  if (passwordToggle) {
    passwordToggle.addEventListener("click", () => {
      const input = $("auth-password");
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      passwordToggle.textContent = showing ? "👁" : "🙈";
      passwordToggle.setAttribute("aria-label", showing ? "显示密码" : "隐藏密码");
      passwordToggle.title = showing ? "显示密码" : "隐藏密码";
    });
  }
  const authSubmitButton = $("btn-auth-submit");
  if (authSubmitButton) {
    authSubmitButton.addEventListener("click", handleAuthSubmit);
  }
  const logoutButton = $("btn-logout");
  if (logoutButton) {
    logoutButton.addEventListener("click", handleLogout);
  }
  const profileButton = $("btn-profile");
  if (profileButton) {
    profileButton.addEventListener("click", () => {
      if (state.account) showPlayerProfile(state.account.id);
    });
  }
  const leaderboardButton = $("btn-leaderboard");
  if (leaderboardButton) {
    leaderboardButton.addEventListener("click", openLeaderboard);
  }

  $("btn-create").addEventListener("click", async () => {
    const name = $("input-name").value.trim();
    if (!state.authToken && !name) {
      showError("home-error", "请先登录或填写游客昵称");
      return;
    }
    try {
      $("btn-create").disabled = true;
      const response = await createRoom(name, state.authToken);
      applyIdentity(response);
      connectStream();
      render();
      toast("房间已创建");
    } catch (error) {
      showError("home-error", error.message);
    } finally {
      $("btn-create").disabled = false;
    }
  });

  $("btn-join").addEventListener("click", handleCodeJoin);

  const browseRoomsButton = $("btn-browse-rooms");
  if (browseRoomsButton) {
    browseRoomsButton.addEventListener("click", openRoomBrowser);
  }
  const roomsBackButton = $("btn-rooms-back");
  if (roomsBackButton) {
    roomsBackButton.addEventListener("click", closeRoomBrowser);
  }
  const roomsRefreshButton = $("btn-rooms-refresh");
  if (roomsRefreshButton) {
    roomsRefreshButton.addEventListener("click", async () => {
      roomsRefreshButton.disabled = true;
      const label = roomsRefreshButton.textContent;
      roomsRefreshButton.textContent = "刷新中…";
      lastRoomListJson = null;
      const area = $("room-list");
      if (area) area.innerHTML = `<p class="empty-text rooms-state-text">加载中…</p>`;
      try {
        await refreshRoomList();
      } finally {
        roomsRefreshButton.disabled = false;
        roomsRefreshButton.textContent = label;
      }
    });
  }
  const roomsNickInput = $("rooms-nick");
  if (roomsNickInput) {
    roomsNickInput.addEventListener("input", () => {
      const home = $("input-name");
      if (home) home.value = roomsNickInput.value;
    });
  }

  $("copy-room-code").addEventListener("click", async () => {
    if (!snapshot) return;
    try {
      await navigator.clipboard.writeText(snapshot.room_code);
      toast("房间码已复制");
    } catch {
      toast(`房间码：${snapshot.room_code}`);
    }
  });

  const addAiButton = $("btn-add-ai");
  if (addAiButton) {
    addAiButton.addEventListener("click", async () => {
      addAiButton.disabled = true;
      try {
        await sendAction("add_ai", { strength: aiStrength });
      } catch (error) {
        toast(error.message);
      } finally {
        addAiButton.disabled = false;
      }
    });
  }

  $("btn-start").addEventListener("click", async () => {
    try {
      await sendAction("start");
    } catch (error) {
      toast(error.message);
    }
  });

  $("btn-play").addEventListener("click", async () => {
    if (!snapshot) return;
    const declaredRank = snapshot.game.declared_rank || selectedRank;
    if (!declaredRank || selectedCardIds.size === 0) return;
    try {
      await sendAction("play", {
        card_ids: Array.from(selectedCardIds),
        declared_rank: declaredRank,
      });
    } catch (error) {
      toast(error.message);
    }
  });

  $("btn-challenge").addEventListener("click", async () => {
    const plays = (snapshot && snapshot.game && snapshot.game.table) || [];
    const target = plays.length ? plays[plays.length - 1] : null;
    if (!target || target.player_id === state.playerId) return;
    try {
      await sendAction("challenge", { target_play_id: target.play_id });
    } catch (error) {
      toast(error.message);
    }
  });

  $("btn-pass-round").addEventListener("click", async () => {
    try {
      await sendAction("pass");
    } catch (error) {
      toast(error.message);
    }
  });

  $("modal-close").addEventListener("click", () => {
    if (snapshot && snapshot.state === "finished") {
      handleReturnToLobby();
    } else {
      closeModal();
    }
  });
  $("modal-mask").addEventListener("click", (event) => {
    if (event.target !== $("modal-mask")) return;
    if (snapshot && snapshot.state === "finished") {
      handleReturnToLobby();
    } else {
      closeModal();
    }
  });

  $("btn-leave-lobby").addEventListener("click", leaveRoom);
  $("btn-leave-game").addEventListener("click", leaveRoom);
}

async function leaveRoom() {
  stopRoomsAutoRefresh();
  if (state.roomCode && state.token) {
    try {
      await sendAction("leave");
    } catch {
      // Ignore: leaving locally is more important than server error.
    }
  }
  if (eventSource) eventSource.close();
  clearInterval(timerInterval);
  if (renderTimeout) clearTimeout(renderTimeout);
  pendingPlayAnimationId = null;
  clearConfetti();
  clearChatPopups();
  hideTableNotice();
  clearIdentity();
  snapshot = null;
  selectedCardIds.clear();
  selectedRank = null;
  showView("view-home");
  toggleChat(false);
  stopOutcomeSound();
  $("input-code").value = "";
  showError("home-error", "");
  pauseMusic();
  renderAuth();
  loadShareUrl();
}

/* ========== 打开页面的账号公告 ========== */
function showAnnouncement() {
  const body = `
    <p class="announce-intro">🎉 欢迎来到「吹牛B」扑克心理战！</p>
    <p>你可以<strong>直接以游客身份开桌</strong>，但游客对局<strong>不计战绩、也不上排行榜</strong>。</p>
    <p>注册或登录账号完全免费、无需手机号。登录后系统会自动为你记录：</p>
    <ul class="announce-list">
      <li>总局数 / 胜场 / 胜率</li>
      <li>吹牛成功率、质疑准确率</li>
      <li>每局完整战报与最近对局</li>
    </ul>
    <p>战绩保存在服务器，<strong>换手机、换电脑登录同一账号也能看到</strong>，并参与「排行榜」和好友一较高下。</p>
    <button class="btn btn-primary btn-block" id="btn-cta-login" type="button">去注册 / 登录</button>`;
  setModal("公告 · 账号与战绩", body, "先随便玩玩");
  const cta = $("btn-cta-login");
  if (cta) {
    cta.addEventListener("click", () => {
      closeModal();
      setAuthMode("register");
      renderAuth();
      const usernameInput = $("auth-username");
      if (usernameInput) usernameInput.focus();
    });
  }
}

/* ========== 启动 ========== */
async function boot() {
  bindEvents();
  initMusic();
  initSounds();
  loadShareUrl();
  await restoreAuth();
  const identity = loadIdentity();

  if (identity && identity.room_code && identity.player_token) {
    try {
      const response = await rejoinRoom(identity.room_code, identity.player_token);
      applyIdentity(response);
      connectStream();
      render();
      return;
    } catch {
      clearIdentity();
    }
  }

  showView("view-home");
  $("input-name").focus();
  if (!state.account) {
    showAnnouncement();
  }
}

boot();
