const PRODUCT_CONFIG = window.PRIMEQK_CLIENT_CONFIG;

if (!PRODUCT_CONFIG) {
  throw new Error("product-config.js must be loaded before the shared client");
}

const roomGroupOrder = PRODUCT_CONFIG.roomGroupOrder || Object.keys(PRODUCT_CONFIG.roomGroups || {});
if (roomGroupOrder.length < 1 || roomGroupOrder.length > 2) {
  throw new Error("the shared lobby requires one or two room groups");
}

const CONFIG = {
  ...PRODUCT_CONFIG,
  wsUrl: new URLSearchParams(location.search).get("ws") || PRODUCT_CONFIG.wsUrl,
  features: {
    assist: false,
    registration: false,
    hnpChallenge: false,
    campaign: false,
    globalChat: false,
    tournament: false,
    recruitment: false,
    practiceAuth: false,
    ...(PRODUCT_CONFIG.features || {}),
  },
  roomGroupOrder,
};

const SOUND_SETTING_KEY = "prime-daifugo-" + CONFIG.productKey + "-sound-enabled";
const TOURNAMENT_TOKEN_KEY = "prime-daifugo-" + CONFIG.productKey + "-tournament-tokens";
const PLAYER_PROFILE_KEY = "primeqk_player_profile_v1";
const LEGACY_ROOM_SESSION_TOKEN_KEY = "prime-daifugo-" + CONFIG.productKey + "-room-session-tokens";
const ROOM_SESSION_TOKEN_KEY = LEGACY_ROOM_SESSION_TOKEN_KEY + "-v2";
const GUEST_MODE_KEY = "prime-daifugo-" + CONFIG.productKey + "-guest-mode";
const GUEST_NAME_KEY = "prime-daifugo-" + CONFIG.productKey + "-guest-name";
const RECRUITMENT_OWNER_KEY = "prime-daifugo-" + CONFIG.productKey + "-recruitment-owner";
const RECRUITMENT_GUEST_OWNER_KEY = RECRUITMENT_OWNER_KEY + "-guest";
const PRACTICE_ACCESS_TOKEN_KEY = "prime-daifugo-" + CONFIG.productKey + "-practice-access-token";
const PLAYER_JOINED_SOUND_URL = CONFIG.playerJoinedSoundUrl || "./assets/sounds/player-joined.mp3";
let playerJoinedAudio = null;
let soundUnlockPromise = null;

const state = {
  ws: null,
  connected: false,
  playerId: null,
  playerName: "",
  guestMode: false,
  roomJoined: false,
  appMode: "setup",
  roomState: "waiting",
  selectedRoomGroupKey: CONFIG.defaultRoomGroupKey,
  selectedRoomKey: CONFIG.defaultRoomKey,
  selectedRoomsByGroup: Object.fromEntries(
    Object.entries(CONFIG.roomGroups).map(([groupKey, group]) => [groupKey, group.roomKeys[0]]),
  ),
  isWaiting: false,
  currentTurn: "",
  firstPlayerId: null,
  soundEnabled: readSoundPreference(),
  roomRosterInitialized: false,
  roomCounts: {},
  roomCountsLoaded: false,
  roomCountsTimer: null,
  reconnectTimer: null,
  recruitments: [],
  recruitmentMaxCount: 5,
  recruitmentSubmitPending: false,
  recruitmentOwnerTokens: { main: "", guest: "" },
  playingDisconnectGraceSeconds: 60,
  waitingDisconnectGraceSeconds: 180,
  roomRules: {},
  roomCpuProfiles: {},
  roomHnpChallengeEnabled: {},
  roomRegisteredNumberLimits: {},
  tournaments: {},
  tournament: null,
  tournamentParticipantId: null,
  tournamentCountdownTimer: null,
  cpuChooserOpen: false,
  selectedCpuKey: "",
  players: [],
  currentRoomHasCpu: false,
  hnpChallengeEnabled: false,
  chatMode: "room",
  globalChatSubscribed: false,
  globalChatJoining: false,
  globalUnreadCount: 0,
  registeredPrimeValues: new Set(),
  registeredCompositeValues: new Set(),
  sampleOptions: [],
  deckCount: "-",
  fieldNumber: "",
  revolution: false,
  fieldCards: [],
  handCounts: [],
  flowPreviewCards: [],
  flowPreviewNumber: "",
  flowPreviewTimer: null,
  hand: [],
  selectedCards: [],
  jokerAssignedRanks: [],
  compositeMode: false,
  compositeTokens: [],
  compositeJokerAssign: [],
  lastAssistCandidates: [],
  remainingFinishExists: false,
  assistFilters: {
    target_scope: "all",
    limit_mode: "ten",
    order: "recommended",
    count_scope: "field",
    card_count: "1",
    face_mode: "letters",
  },
  pendingFlow: null,
  sampleLoadedForFlow: false,
  cpuRequestedForFlow: false,
  startRequestedForFlow: false,
  assistTimer: null,
  assistRequestVersion: 0,
  practiceAuthorized: !CONFIG.features.practiceAuth,
};

const el = {};

document.addEventListener("DOMContentLoaded", () => {
  bindElements();
  configureProductUi();
  initializeIdentityForm();
  bindEvents();
  setRandomNameIfEmpty();
  initializeRecruitmentForm();
  initializePracticeAuth();
  connect();
  if (CONFIG.features.tournament) {
    state.tournamentCountdownTimer = window.setInterval(renderTournamentCallCountdown, 1000);
  }
  renderAll();
});

function configureProductUi() {
  document.body.dataset.product = CONFIG.productKey;
  document.querySelectorAll("[data-feature]").forEach((element) => {
    const enabled = !!CONFIG.features[element.dataset.feature];
    element.classList.toggle("product-disabled", !enabled);
  });
}

function initializePracticeAuth() {
  if (!CONFIG.features.practiceAuth) return;
  const form = document.getElementById("practiceAuthForm");
  const input = document.getElementById("practiceAccessToken");
  if (!form || !input) return;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const token = input.value.trim();
    if (!token) return;
    savePracticeAccessToken(token);
    authorizeCompositePractice(token);
    updatePracticeAuthUi("認証中…");
  });
  updatePracticeAuthUi("");
}

function readPracticeAccessToken() {
  try {
    return sessionStorage.getItem(PRACTICE_ACCESS_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function savePracticeAccessToken(token) {
  try {
    sessionStorage.setItem(PRACTICE_ACCESS_TOKEN_KEY, token);
  } catch {}
}

function clearPracticeAccessToken() {
  try {
    sessionStorage.removeItem(PRACTICE_ACCESS_TOKEN_KEY);
  } catch {}
}

function authorizeCompositePractice(token) {
  send({ type: "authorize_composite_practice", access_token: token });
}

function updatePracticeAuthUi(message) {
  const gate = document.getElementById("practiceAuthGate");
  const status = document.getElementById("practiceAuthStatus");
  if (gate) gate.classList.toggle("hidden", state.practiceAuthorized);
  document.body.classList.toggle("practice-locked", !state.practiceAuthorized);
  if (status) status.textContent = message;
}

function requestInitialLobbyState() {
  send({ type: "get_room_counts" });
  requestRecruitments();
  if (state.roomJoined) {
    send({ type: "set_name", name: state.playerName });
    send({
      type: "join_room",
      room_id: currentRoomId(),
      ...(roomResumeToken(currentRoomId()) ? { resume_token: roomResumeToken(currentRoomId()) } : {}),
    });
  }
}

function readPlayerProfile() {
  try {
    const profile = JSON.parse(localStorage.getItem(PLAYER_PROFILE_KEY) || "{}");
    return profile && typeof profile === "object" ? profile : {};
  } catch {
    return {};
  }
}

function savedMainName() {
  const name = readPlayerProfile().display_name;
  return typeof name === "string" ? name.trim().slice(0, 24) : "";
}

function saveMainName(name) {
  try {
    const profile = readPlayerProfile();
    profile.display_name = String(name || "").trim().slice(0, 24);
    localStorage.setItem(PLAYER_PROFILE_KEY, JSON.stringify(profile));
  } catch {
    log("error", "表示名をこのブラウザに保存できませんでした。");
  }
}

function guestName() {
  try {
    return (sessionStorage.getItem(GUEST_NAME_KEY) || "").trim().slice(0, 24);
  } catch {
    return "";
  }
}

function saveGuestName(name) {
  try {
    sessionStorage.setItem(GUEST_NAME_KEY, String(name || "").trim().slice(0, 24));
  } catch {}
}

function initializeIdentityForm() {
  try {
    state.guestMode = sessionStorage.getItem(GUEST_MODE_KEY) === "true";
    // v1は端末内の全タブで共有されていたため、安全のため引き継がない。
    localStorage.removeItem(LEGACY_ROOM_SESSION_TOKEN_KEY);
  } catch {
    state.guestMode = false;
  }
  el.guestModeToggle.checked = state.guestMode;
  el.nameInput.value = state.guestMode ? guestName() : savedMainName();
  state.playerName = el.nameInput.value;
  updateIdentityModeUi();
}

function persistCurrentName() {
  const name = el.nameInput.value.trim().slice(0, 24);
  el.nameInput.value = name;
  state.playerName = name;
  if (state.guestMode) saveGuestName(name);
  else saveMainName(name);
}

function toggleGuestMode() {
  if (state.roomJoined) {
    el.guestModeToggle.checked = state.guestMode;
    return;
  }
  persistCurrentName();
  state.guestMode = el.guestModeToggle.checked;
  try {
    sessionStorage.setItem(GUEST_MODE_KEY, String(state.guestMode));
  } catch {}
  el.nameInput.value = state.guestMode ? guestName() : savedMainName();
  state.playerName = el.nameInput.value;
  updateIdentityModeUi();
  setRandomNameIfEmpty();
  if (el.recruitmentName) el.recruitmentName.value = el.nameInput.value.trim();
  requestRecruitments();
  renderAll();
}

function updateIdentityModeUi() {
  if (!el.identityModeNote) return;
  el.identityModeNote.textContent = state.guestMode
    ? "このタブ専用の名前を使います。保存済みの名前・復帰情報・大会参加権は使用しません。"
    : "保存済みの名前を使います。対戦の復帰情報はタブごとに分かれます。";
}

function bindElements() {
  [
    "connectionDot",
    "connectionLabel",
    "serverLabel",
    "setupPanel",
    "roomPanel",
    "registerPanel",
    "nameInput",
    "randomNameBtn",
    "guestModeToggle",
    "identityModeNote",
    "roomGroupPrimaryBtn",
    "roomGroupSecondaryBtn",
    "roomPickerHint",
    "roomList",
    "practiceBtn",
    "recruitmentCount",
    "recruitmentList",
    "recruitmentForm",
    "recruitmentTime",
    "recruitmentName",
    "recruitmentRule",
    "recruitmentSubmitBtn",
    "recruitmentStatus",
    "soundToggleBtn",
    "leaveBtn",
    "roomBadge",
    "roomHeading",
    "nextHint",
    "playStatus",
    "playerList",
    "watcherList",
    "readyBtn",
    "addCpuBtn",
    "startBtn",
    "reconnectPolicyNote",
    "cpuChooser",
    "cpuProfileSelect",
    "cpuProfileDescription",
    "cpuChooserCloseBtn",
    "confirmCpuBtn",
    "sampleBtn",
    "sampleSelect",
    "primeText",
    "compositeText",
    "saveRegisterBtn",
    "registerStatus",
    "registerLimitNote",
    "fieldZone",
    "fieldNumber",
    "fieldCards",
    "deckCount",
    "myHandMetric",
    "myHandLabel",
    "myHandCount",
    "opponentMetric",
    "opponentLabel",
    "opponentCounts",
    "assistRecommendedBtn",
    "assistStrongBtn",
    "assistEasyBtn",
    "assistRestBtn",
    "assistManyBtn",
    "assistList",
    "selectedTitle",
    "clearSelectionBtn",
    "selectedCards",
    "jokerControls",
    "compositeModeBtn",
    "compositePanel",
    "compositeTitle",
    "compositeCards",
    "compositeJokerControls",
    "compositeMulBtn",
    "compositePowBtn",
    "compositeClearBtn",
    "playBtn",
    "drawBtn",
    "passBtn",
    "handCards",
    "remainingFinishNotice",
    "turnBadge",
    "roomChatTab",
    "globalChatTab",
    "globalUnreadBadge",
    "globalChatGate",
    "enableGlobalChatBtn",
    "globalQuickMessages",
    "chatComposer",
    "roomLogBox",
    "globalLogBox",
    "chatInput",
    "chatBtn",
    "campaignResultDialog",
    "campaignDialogCloseBtn",
    "campaignResultTitle",
    "campaignResultMessage",
    "campaignShareBtn",
    "campaignResultPageLink",
    "tournamentPanel",
    "tournamentTitle",
    "tournamentStatusBadge",
    "tournamentSchedule",
    "tournamentMessage",
    "tournamentRegisterBtn",
    "tournamentWithdrawBtn",
    "tournamentPairing",
    "tournamentLeagueTable",
    "tournamentStandings",
    "tournamentTimingNote",
  ].forEach((id) => {
    el[id] = document.getElementById(id);
  });
}

function bindEvents() {
  const [primaryGroupKey, secondaryGroupKey] = CONFIG.roomGroupOrder;
  el.randomNameBtn.addEventListener("click", setRandomName);
  el.nameInput.addEventListener("change", persistCurrentName);
  el.nameInput.addEventListener("blur", persistCurrentName);
  el.guestModeToggle.addEventListener("change", toggleGuestMode);
  el.roomGroupPrimaryBtn.addEventListener("click", () => selectRoomGroup(primaryGroupKey));
  if (secondaryGroupKey) {
    el.roomGroupSecondaryBtn.addEventListener("click", () => selectRoomGroup(secondaryGroupKey));
  }
  el.roomList.addEventListener("click", (event) => {
    const roomButton = event.target.closest("[data-room-key]");
    if (roomButton) selectRoom(roomButton.dataset.roomKey);
  });
  el.practiceBtn.addEventListener("click", () => startFlow("enter"));
  if (el.recruitmentForm) el.recruitmentForm.addEventListener("submit", createRecruitment);
  if (el.recruitmentList) el.recruitmentList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-delete-recruitment]");
    if (button) deleteRecruitment(button.dataset.deleteRecruitment);
  });
  el.soundToggleBtn.addEventListener("click", toggleSound);
  el.leaveBtn.addEventListener("click", leaveRoom);
  el.readyBtn.addEventListener("click", toggleReady);
  el.addCpuBtn.addEventListener("click", toggleCpu);
  el.cpuProfileSelect.addEventListener("change", selectCpuProfile);
  el.cpuChooserCloseBtn.addEventListener("click", closeCpuChooser);
  el.confirmCpuBtn.addEventListener("click", confirmCpuSelection);
  el.startBtn.addEventListener("click", startGame);
  el.sampleBtn.addEventListener("click", loadSample);
  el.saveRegisterBtn.addEventListener("click", saveRegisteredNumbers);
  el.clearSelectionBtn.addEventListener("click", clearSelection);
  el.compositeModeBtn.addEventListener("click", toggleCompositeMode);
  el.compositeMulBtn.addEventListener("click", () => addCompositeOp("×"));
  el.compositePowBtn.addEventListener("click", () => addCompositeOp("^"));
  el.compositeClearBtn.addEventListener("click", () => {
    clearCompositeMode();
    renderAll();
  });
  el.playBtn.addEventListener("click", playSelected);
  el.drawBtn.addEventListener("click", () => send({ type: "draw_card" }));
  el.passBtn.addEventListener("click", () => send({ type: "pass" }));
  el.roomChatTab.addEventListener("click", () => switchChatMode("room"));
  el.globalChatTab.addEventListener("click", () => switchChatMode("global"));
  el.enableGlobalChatBtn.addEventListener("click", enableGlobalChat);
  document.querySelectorAll("[data-global-template]").forEach((button) => {
    button.addEventListener("click", () => sendGlobalTemplate(button.dataset.globalTemplate));
  });
  el.chatBtn.addEventListener("click", sendChat);
  el.chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") sendChat();
  });
  el.assistRecommendedBtn.addEventListener("click", () => setAssistOrder("recommended"));
  el.assistStrongBtn.addEventListener("click", () => setAssistOrder("strong"));
  el.assistEasyBtn.addEventListener("click", () => setAssistOrder("weak"));
  el.assistRestBtn.addEventListener("click", toggleAssistRest);
  el.assistManyBtn.addEventListener("click", toggleAssistLimit);
  el.campaignDialogCloseBtn.addEventListener("click", closeCampaignResult);
  if (el.tournamentRegisterBtn) el.tournamentRegisterBtn.addEventListener("click", registerForTournament);
  if (el.tournamentWithdrawBtn) el.tournamentWithdrawBtn.addEventListener("click", withdrawFromTournament);
  const unlockSoundOnFirstRelevantClick = (event) => {
    if (event.target.closest(".room-group-choice-card, .room-slot-card")) return;
    document.removeEventListener("click", unlockSoundOnFirstRelevantClick, true);
    if (state.soundEnabled) unlockSoundPlayback();
  };
  document.addEventListener("click", unlockSoundOnFirstRelevantClick, true);
}

function currentRoomOption() {
  return CONFIG.rooms[state.selectedRoomKey] || CONFIG.rooms[CONFIG.defaultRoomKey];
}

function currentRoomId() {
  return currentRoomOption().roomId;
}

function isTournamentRoom() {
  return Boolean(CONFIG.features.tournament && currentRoomOption().tournament);
}

function currentRoomGroupOption() {
  return CONFIG.roomGroups[state.selectedRoomGroupKey] || CONFIG.roomGroups[CONFIG.defaultRoomGroupKey];
}

function selectRoomGroup(roomGroupKey) {
  if (state.roomJoined || !CONFIG.roomGroups[roomGroupKey]) return;
  state.selectedRoomGroupKey = roomGroupKey;
  const rememberedRoomKey = state.selectedRoomsByGroup[roomGroupKey];
  const firstAvailableRoomKey = CONFIG.roomGroups[roomGroupKey].roomKeys.find(isRoomSelectable);
  const roomKey = isRoomSelectable(rememberedRoomKey)
    ? rememberedRoomKey
    : firstAvailableRoomKey || CONFIG.roomGroups[roomGroupKey].roomKeys[0];
  selectRoom(roomKey);
}

function selectRoom(roomKey) {
  if (state.roomJoined || !CONFIG.rooms[roomKey] || !isRoomSelectable(roomKey)) return;
  state.selectedRoomKey = roomKey;
  state.selectedRoomGroupKey = CONFIG.rooms[roomKey].roomGroupKey;
  state.selectedRoomsByGroup[state.selectedRoomGroupKey] = roomKey;
  state.hnpChallengeEnabled = CONFIG.features.hnpChallenge && !!state.roomHnpChallengeEnabled[currentRoomId()];
  state.allowComposite = state.roomAllowComposite?.[currentRoomId()] !== false;
  state.assistEnabled = CONFIG.features.assist && !!state.roomAssistEnabled?.[currentRoomId()];
  state.currentRoomHasCpu = false;
  state.cpuChooserOpen = false;
  state.selectedCpuKey = "";
  renderSampleOptions();
  setSampleSelectToRoomDefault();
  renderRoomChoice();
  renderAll();
}

function setSampleSelectToRoomDefault() {
  const key = currentRoomOption().defaultSampleKey || CONFIG.defaultSampleKey;
  if ([...el.sampleSelect.options].some((option) => option.value === key)) {
    el.sampleSelect.value = key;
  }
}

function isRoomAvailable(roomKey) {
  if (!CONFIG.rooms[roomKey]) return false;
  if (!state.roomCountsLoaded) return true;
  return Object.prototype.hasOwnProperty.call(state.roomCounts, CONFIG.rooms[roomKey].roomId);
}

function isRoomFull(roomKey) {
  if (!isRoomAvailable(roomKey)) return false;
  return (state.roomCounts[CONFIG.rooms[roomKey].roomId] || 0) >= CONFIG.maxRoomPlayers;
}

function isRoomSelectable(roomKey) {
  return isRoomAvailable(roomKey) && !isRoomFull(roomKey);
}

function connect() {
  if (state.ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.ws.readyState)) return;
  setConnection("connecting", "接続中", CONFIG.wsUrl);
  state.ws = new WebSocket(CONFIG.wsUrl);

  state.ws.addEventListener("open", () => {
    state.connected = true;
    setConnection("online", "接続済み", `${CONFIG.lobbyName} / ${Object.keys(CONFIG.rooms).length}部屋`);
    if (CONFIG.features.practiceAuth) {
      state.practiceAuthorized = false;
      const accessToken = readPracticeAccessToken();
      if (accessToken) authorizeCompositePractice(accessToken);
    } else {
      requestInitialLobbyState();
    }
    clearInterval(state.roomCountsTimer);
    state.roomCountsTimer = setInterval(() => {
      if (state.appMode === "setup" && state.practiceAuthorized) {
        send({ type: "get_room_counts" });
        requestRecruitments();
      }
    }, 5000);
    renderAll();
  });

  state.ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    handleMessage(message);
  });

  state.ws.addEventListener("close", () => {
    state.connected = false;
    state.recruitmentSubmitPending = false;
    state.globalChatSubscribed = false;
    state.globalChatJoining = false;
    if (CONFIG.features.practiceAuth) {
      state.practiceAuthorized = false;
      updatePracticeAuthUi("");
    }
    clearInterval(state.roomCountsTimer);
    state.roomCountsTimer = null;
    setConnection("error", "切断されました", "2秒後に自動再接続します");
    log("system", `サーバーとの接続が切れました。対戦中は${state.playingDisconnectGraceSeconds}秒、待機中は${state.waitingDisconnectGraceSeconds}秒まで自動復帰を試みます。`);
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = window.setTimeout(connect, 2000);
    renderAll();
  });

  state.ws.addEventListener("error", () => {
    state.connected = false;
    state.globalChatSubscribed = false;
    state.globalChatJoining = false;
    clearInterval(state.roomCountsTimer);
    state.roomCountsTimer = null;
    setConnection("error", "接続エラー", "サーバーに接続できませんでした");
    renderAll();
  });
}

function handleMessage(msg) {
  switch (msg.type) {
    case "your_id":
      state.playerId = msg.id;
      break;
    case "composite_practice_authorization":
      state.practiceAuthorized = !!msg.authorized;
      updatePracticeAuthUi(msg.message || "");
      if (state.practiceAuthorized) {
        requestInitialLobbyState();
      } else {
        clearPracticeAccessToken();
      }
      break;
    case "room_counts":
      state.roomCounts = msg.counts || {};
      state.roomCountsLoaded = true;
      state.roomRules = msg.rules || {};
      state.roomCpuProfiles = msg.cpu_profiles || {};
      state.roomHnpChallengeEnabled = msg.hnp_challenge_enabled || {};
      state.roomRegisteredNumberLimits = msg.registered_number_limits || {};
      state.roomAllowComposite = msg.allow_composite || {};
      state.roomAssistEnabled = msg.assist_enabled || {};
      state.tournaments = msg.tournaments || {};
      if (isTournamentRoom()) setTournamentState(state.tournaments[currentRoomId()] || null);
      if (!isRoomSelectable(state.selectedRoomKey)) {
        const fallbackRoomKey = currentRoomGroupOption().roomKeys.find(isRoomSelectable)
          || Object.keys(CONFIG.rooms).find(isRoomSelectable);
        if (fallbackRoomKey) {
          state.selectedRoomKey = fallbackRoomKey;
          state.selectedRoomGroupKey = CONFIG.rooms[fallbackRoomKey].roomGroupKey;
          state.selectedRoomsByGroup[state.selectedRoomGroupKey] = fallbackRoomKey;
        }
      }
      state.hnpChallengeEnabled = CONFIG.features.hnpChallenge && !!state.roomHnpChallengeEnabled[currentRoomId()];
      state.allowComposite = state.roomAllowComposite[currentRoomId()] !== false;
      state.assistEnabled = CONFIG.features.assist && !!state.roomAssistEnabled[currentRoomId()];
      state.sampleOptions = msg.registered_sample_options || [];
      renderSampleOptions();
      break;
    case "recruitments":
      state.recruitments = Array.isArray(msg.items) ? msg.items : [];
      state.recruitmentMaxCount = Number(msg.max_count) || 5;
      state.recruitmentSubmitPending = false;
      if (msg.notice) setRecruitmentStatus(msg.notice, "success");
      break;
    case "recruitment_error":
      state.recruitmentSubmitPending = false;
      setRecruitmentStatus(msg.message || "募集を更新できませんでした。", "error");
      break;
    case "name_set":
      state.playerName = msg.name || state.playerName;
      el.nameInput.value = state.playerName;
      persistCurrentName();
      break;
    case "room_state_initialization":
      state.roomJoined = true;
      state.roomState = msg.room_state || "waiting";
      state.appMode = msg.room_state === "playing" ? "playing" : "room";
      state.playingDisconnectGraceSeconds = msg.playing_disconnect_grace_seconds ?? state.playingDisconnectGraceSeconds;
      state.waitingDisconnectGraceSeconds = msg.waiting_disconnect_grace_seconds ?? state.waitingDisconnectGraceSeconds;
      if (typeof msg.hnp_challenge_enabled === "boolean") {
        state.hnpChallengeEnabled = CONFIG.features.hnpChallenge && msg.hnp_challenge_enabled;
      }
      if (typeof msg.allow_composite === "boolean") state.allowComposite = msg.allow_composite;
      if (typeof msg.assist_enabled === "boolean") {
        state.assistEnabled = CONFIG.features.assist && msg.assist_enabled;
      }
      if (isTournamentRoom()) {
        setTournamentState(msg.tournament || state.tournaments[currentRoomId()] || null);
        const resumeToken = tournamentResumeToken(state.tournament?.run_id);
        if (resumeToken && state.tournament?.status !== "unavailable") {
          send({ type: "tournament_register", resume_token: resumeToken });
        }
      }
      continuePendingFlowAfterJoin();
      break;
    case "room_session":
      if (msg.resume_token && msg.room_id) saveRoomResumeToken(msg.room_id, msg.resume_token);
      if (msg.status === "resumed") log("system", "切断前の席と手札へ復帰しました。");
      if (msg.display_name) {
        state.playerName = msg.display_name;
        el.nameInput.value = state.playerName;
        persistCurrentName();
      }
      state.playingDisconnectGraceSeconds = msg.playing_disconnect_grace_seconds ?? state.playingDisconnectGraceSeconds;
      state.waitingDisconnectGraceSeconds = msg.waiting_disconnect_grace_seconds ?? state.waitingDisconnectGraceSeconds;
      break;
    case "room_left":
      if (msg.room_id) removeRoomResumeToken(msg.room_id);
      break;
    case "update_room_status":
      if (msg.room_id === currentRoomId()) {
        const nextPlayers = msg.player_list || [];
        state.roomCounts[currentRoomId()] = msg.count;
        state.currentRoomHasCpu = nextPlayers.some((player) => player.is_cpu);
        if (state.currentRoomHasCpu) state.cpuChooserOpen = false;
        state.roomCpuProfiles[currentRoomId()] = msg.cpu_profiles || state.roomCpuProfiles[currentRoomId()] || [];
        if (isTournamentRoom() && msg.tournament) setTournamentState(msg.tournament);
        if (typeof msg.hnp_challenge_enabled === "boolean") state.hnpChallengeEnabled = msg.hnp_challenge_enabled;
        detectPlayerJoined(nextPlayers);
        renderPlayers(nextPlayers, msg.waiting_count || 0);
        continuePendingFlowAfterCpuStatus();
      }
      break;
    case "tournament_update":
      setTournamentState(msg.tournament || null);
      break;
    case "tournament_registration":
      state.tournamentParticipantId = msg.participant_id || null;
      if (msg.display_name) {
        state.playerName = msg.display_name;
        el.nameInput.value = state.playerName;
        persistCurrentName();
      }
      if (!state.guestMode && msg.resume_token && msg.tournament?.run_id) {
        saveTournamentResumeToken(msg.tournament.run_id, msg.resume_token);
      }
      setTournamentState(msg.tournament || state.tournament);
      log("system", msg.status === "resumed" ? "大会参加情報を復元しました。" : "大会に参加登録しました。");
      break;
    case "tournament_session_conflict": {
      setTournamentState(msg.tournament || state.tournament);
      const takeOver = !state.guestMode && window.confirm(
        "同じ大会参加権が別のタブで使用中です。このタブへ参加権を引き継ぎますか？\n\nキャンセルすると、このタブでは観戦を続けます。",
      );
      if (takeOver) {
        send({
          type: "tournament_register",
          resume_token: tournamentResumeToken(msg.run_id || state.tournament?.run_id),
          takeover: true,
        });
      } else {
        log("system", "大会参加権は別タブに残し、このタブでは観戦を続けます。");
      }
      break;
    }
    case "session_replaced":
      state.roomJoined = false;
      state.appMode = "setup";
      state.roomState = "waiting";
      state.isWaiting = false;
      if (msg.room_id) removeRoomResumeToken(msg.room_id);
      log("system", msg.message || "この参加情報は別のタブへ引き継がれました。");
      break;
    case "tournament_match_call":
      setTournamentState(msg.tournament || state.tournament);
      log("system", msg.message || "あなたの対戦です。大会パネルを確認してください。");
      playPlayerJoinedSound();
      break;
    case "tournament_match_ready_ack":
      setTournamentState(msg.tournament || state.tournament);
      log("system", "対戦への参加を受け付けました。相手の確認を待っています。");
      break;
    case "tournament_match_started":
      setTournamentState(msg.tournament || state.tournament);
      log("system", `第${msg.round_no}ラウンドの対戦ルームへ移動しました。`);
      break;
    case "tournament_return_to_lobby":
      setTournamentState(msg.tournament || state.tournament);
      state.appMode = "room";
      state.roomState = "waiting";
      log("system", "大会ロビーへ戻りました。");
      break;
    case "tournament_withdrawn":
      removeTournamentResumeToken(msg.run_id);
      state.tournamentParticipantId = null;
      setTournamentState(msg.tournament || state.tournament);
      log("system", "大会参加を取り消しました。");
      break;
    case "registered_numbers_updated":
    case "registered_primes_updated":
      renderRegisteredStatus(msg);
      state.sampleLoadedForFlow = true;
      continuePendingFlowAfterRegistration();
      break;
    case "game_start":
      state.appMode = "playing";
      state.roomState = "playing";
      state.firstPlayerId = null;
      if (typeof msg.hnp_challenge_enabled === "boolean") state.hnpChallengeEnabled = msg.hnp_challenge_enabled;
      clearFlowPreview(false);
      clearSelection();
      break;
    case "deal":
      state.hand = msg.your_hand || [];
      clearSelection();
      scheduleAssist();
      break;
    case "hand_update":
      state.hand = msg.your_hand || [];
      renderHand();
      scheduleAssist();
      break;
    case "game_update":
      state.appMode = msg.state === "playing" ? "playing" : state.appMode;
      state.roomState = msg.state || state.roomState;
      state.currentTurn = msg.current_turn || "";
      state.firstPlayerId = msg.first_player_id || state.firstPlayerId;
      state.currentRoomHasCpu = (msg.player_list || []).some((player) => player.is_cpu);
      if (isTournamentRoom() && msg.tournament) setTournamentState(msg.tournament);
      if (typeof msg.hnp_challenge_enabled === "boolean") state.hnpChallengeEnabled = msg.hnp_challenge_enabled;
      renderField(msg);
      renderPlayers(msg.player_list || [], null);
      scheduleAssist();
      break;
    case "turn_update":
    case "next_turn":
      state.currentTurn = msg.current_turn || "";
      scheduleAssist();
      break;
    case "prime_assist_result":
      if (!state.assistEnabled) break;
      if (
        Number.isInteger(msg.assist_request_id)
        && msg.assist_request_id !== state.assistRequestVersion
      ) {
        break;
      }
      state.lastAssistCandidates = msg.candidates || [];
      state.remainingFinishExists = typeof msg.remaining_finish_exists === "boolean"
        ? msg.remaining_finish_exists
        : state.lastAssistCandidates.some(candidateFinishesRemaining);
      renderHand();
      renderAssist();
      break;
    case "action_result":
      if (msg.action === "field_flow") {
        showFlowPreview(msg.played_cards || [], msg.number);
      }
      break;
    case "penalty":
      break;
    case "game_over":
      state.roomState = msg.state || "waiting";
      state.appMode = "room";
      state.hand = [];
      state.firstPlayerId = null;
      clearFlowPreview(false);
      clearSelection();
      break;
    case "campaign_result":
      if (CONFIG.features.campaign) showCampaignResult(msg);
      break;
    case "score_record":
      logScoreRecord(msg.lines || []);
      break;
    case "chat":
      log(msg.sender || "chat", msg.message || "");
      break;
    case "global_chat_joined":
      state.globalChatSubscribed = true;
      state.globalChatJoining = false;
      state.globalUnreadCount = 0;
      logGlobalSystem(msg.notice || "グローバルチャットを表示しました。");
      break;
    case "global_chat_left":
      state.globalChatSubscribed = false;
      state.globalChatJoining = false;
      state.chatMode = "room";
      break;
    case "global_chat":
      logGlobalChat(msg);
      if (state.chatMode !== "global") state.globalUnreadCount += 1;
      break;
    case "error":
      if (msg.code === "registered_number_limit") {
        el.registerStatus.textContent = msg.message || "登録数が上限を超えています";
      }
      if (state.chatMode === "global") {
        state.globalChatJoining = false;
        logGlobalSystem(msg.message || "エラーが発生しました。");
      } else {
        log("error", msg.message || "エラーが発生しました。");
      }
      break;
  }
  renderAll();
}

function setTournamentState(tournament) {
  state.tournament = tournament;
  if (tournament?.viewer_participant_id) {
    state.tournamentParticipantId = tournament.viewer_participant_id;
  }
}

function readTournamentTokens() {
  try {
    return JSON.parse(localStorage.getItem(TOURNAMENT_TOKEN_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

function tournamentResumeToken(runId) {
  if (state.guestMode || !runId) return "";
  return readTournamentTokens()[runId] || "";
}

function saveTournamentResumeToken(runId, token) {
  try {
    const tokens = readTournamentTokens();
    tokens[runId] = token;
    localStorage.setItem(TOURNAMENT_TOKEN_KEY, JSON.stringify(tokens));
  } catch {
    log("error", "復帰トークンをこのブラウザに保存できませんでした。");
  }
}

function removeTournamentResumeToken(runId) {
  if (!runId) return;
  try {
    const tokens = readTournamentTokens();
    delete tokens[runId];
    localStorage.setItem(TOURNAMENT_TOKEN_KEY, JSON.stringify(tokens));
  } catch {}
}

function readRoomResumeTokens() {
  try {
    return JSON.parse(sessionStorage.getItem(ROOM_SESSION_TOKEN_KEY) || "{}") || {};
  } catch {
    return {};
  }
}

function roomResumeToken(roomId) {
  return roomId ? readRoomResumeTokens()[roomId] || "" : "";
}

function saveRoomResumeToken(roomId, token) {
  try {
    const tokens = readRoomResumeTokens();
    tokens[roomId] = token;
    sessionStorage.setItem(ROOM_SESSION_TOKEN_KEY, JSON.stringify(tokens));
  } catch {
    log("error", "対戦復帰トークンをこのブラウザに保存できませんでした。");
  }
}

function removeRoomResumeToken(roomId) {
  if (!roomId) return;
  try {
    const tokens = readRoomResumeTokens();
    delete tokens[roomId];
    sessionStorage.setItem(ROOM_SESSION_TOKEN_KEY, JSON.stringify(tokens));
  } catch {}
}

function registerForTournament() {
  if (!state.roomJoined || !isTournamentRoom()) return;
  if (state.guestMode) {
    log("system", "ゲストモードでは大会を観戦できますが、参加登録はできません。通常モードへ戻してから入室し直してください。");
    renderAll();
    return;
  }
  ensureName();
  const resumeToken = tournamentResumeToken(state.tournament?.run_id);
  send({
    type: "tournament_register",
    ...(resumeToken ? { resume_token: resumeToken } : {}),
  });
}

function withdrawFromTournament() {
  if (!state.tournamentParticipantId) return;
  send({ type: "tournament_withdraw" });
}

function readyForTournamentMatch(matchId) {
  if (!matchId) return;
  send({ type: "tournament_match_ready", match_id: matchId });
}

function startFlow(flow) {
  if (!state.connected || !state.ws || state.ws.readyState !== WebSocket.OPEN) {
    log("error", "サーバーへ接続してから入室してください。");
    renderAll();
    return;
  }
  if (!isRoomSelectable(state.selectedRoomKey)) {
    const message = isRoomFull(state.selectedRoomKey)
      ? "選んだ部屋は満員です。別の部屋を選んでください。"
      : `${currentRoomId()} は接続先サーバーにまだありません。サーバー反映後に選べます。`;
    log("error", message);
    renderAll();
    return;
  }
  ensureName();
  state.pendingFlow = flow;
  state.sampleLoadedForFlow = false;
  state.cpuRequestedForFlow = false;
  state.startRequestedForFlow = false;
  state.players = [];
  state.roomRosterInitialized = false;
  state.appMode = "room";
  send({ type: "set_name", name: state.playerName });
  const resumeToken = roomResumeToken(currentRoomId());
  send({
    type: "join_room",
    room_id: currentRoomId(),
    ...(resumeToken ? { resume_token: resumeToken } : {}),
  });
  renderAll();
}

function continuePendingFlowAfterJoin() {
  if (!state.pendingFlow) return;
  if (state.pendingFlow === "enter") {
    if (CONFIG.features.registration) loadSample();
    state.pendingFlow = null;
    return;
  }
  if (state.pendingFlow !== "watch") {
    loadSample();
    if (!state.isWaiting) {
      state.isWaiting = true;
      send({ type: "change_status", status: "waiting" });
    }
  }
}

function continuePendingFlowAfterRegistration() {
  if (state.pendingFlow === "practice") {
    if (!state.currentRoomHasCpu && !state.cpuRequestedForFlow) {
      state.cpuRequestedForFlow = true;
      addCpu();
      return;
    }
    if (!state.startRequestedForFlow) {
      state.startRequestedForFlow = true;
      setTimeout(startGame, 350);
    }
  }
}

function continuePendingFlowAfterCpuStatus() {
  if (state.pendingFlow === "practice" && state.sampleLoadedForFlow && state.currentRoomHasCpu && !state.startRequestedForFlow) {
    state.startRequestedForFlow = true;
    setTimeout(startGame, 350);
  }
}

function ensureName() {
  const typed = el.nameInput.value.trim().slice(0, 24);
  state.playerName = typed || randomName();
  el.nameInput.value = state.playerName;
  persistCurrentName();
}

function showCampaignResult(msg) {
  if (!CONFIG.features.campaign) return;
  const recorded = msg.status === "recorded";
  el.campaignResultTitle.textContent = recorded
    ? "ゴールドCPUへの勝利を記録しました！"
    : "勝利記録を保存できませんでした";
  el.campaignResultMessage.textContent = recorded
    ? `「${msg.player_name}」で個人通算${msg.player_wins}勝、みんなで${msg.total_wins}/${msg.goal}勝です。`
    : msg.message || "時間をおいて、キャンペーンページをご確認ください。";

  const campaignUrl = msg.campaign_url || "./campaign.html";
  el.campaignResultPageLink.href = campaignUrl;
  el.campaignShareBtn.classList.toggle("hidden", !recorded);

  if (recorded) {
    const shareText = `「${msg.player_name}」でゴールドCPUに勝利！ 個人通算${msg.player_wins}勝、みんなで${msg.total_wins}/${msg.goal}勝。 #素数大富豪NEO`;
    const params = new URLSearchParams({
      text: shareText,
      url: CONFIG.shareUrl,
    });
    el.campaignShareBtn.href = `https://twitter.com/intent/tweet?${params.toString()}`;
  } else {
    el.campaignShareBtn.removeAttribute("href");
  }

  if (typeof el.campaignResultDialog.showModal === "function") {
    if (!el.campaignResultDialog.open) el.campaignResultDialog.showModal();
  } else {
    el.campaignResultDialog.setAttribute("open", "");
  }
}

function closeCampaignResult() {
  if (typeof el.campaignResultDialog.close === "function") {
    el.campaignResultDialog.close();
  } else {
    el.campaignResultDialog.removeAttribute("open");
  }
}

function setRandomNameIfEmpty() {
  if (!el.nameInput.value.trim()) el.nameInput.placeholder = `例: ${randomSushiName()}`;
}

function setRandomName() {
  el.nameInput.value = randomName();
  persistCurrentName();
  if (el.recruitmentName) el.recruitmentName.value = state.playerName;
}

function initializeRecruitmentForm() {
  if (!CONFIG.features.recruitment || !el.recruitmentForm) return;
  el.recruitmentName.value = el.nameInput.value.trim();
  setDefaultRecruitmentTime();
  updateRecruitmentTimeBounds();
}

function recruitmentOwnerToken() {
  const modeKey = state.guestMode ? "guest" : "main";
  const storageKey = state.guestMode ? RECRUITMENT_GUEST_OWNER_KEY : RECRUITMENT_OWNER_KEY;
  const storage = state.guestMode ? sessionStorage : localStorage;
  try {
    const saved = storage.getItem(storageKey);
    if (saved && saved.length >= 32) return saved;
  } catch {}
  if (!state.recruitmentOwnerTokens[modeKey]) {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    state.recruitmentOwnerTokens[modeKey] = Array.from(
      bytes,
      (value) => value.toString(16).padStart(2, "0"),
    ).join("");
  }
  try {
    storage.setItem(storageKey, state.recruitmentOwnerTokens[modeKey]);
  } catch {}
  return state.recruitmentOwnerTokens[modeKey];
}

function requestRecruitments() {
  if (!CONFIG.features.recruitment || !state.connected) return;
  send({ type: "get_recruitments", owner_token: recruitmentOwnerToken() });
}

function createRecruitment(event) {
  event.preventDefault();
  if (!CONFIG.features.recruitment || state.recruitmentSubmitPending) return;
  const name = el.recruitmentName.value.trim().slice(0, 24);
  const scheduledAt = new Date(el.recruitmentTime.value);
  const now = new Date();
  if (!name) {
    setRecruitmentStatus("名前を入力してください。", "error");
    return;
  }
  if (Number.isNaN(scheduledAt.getTime()) || scheduledAt <= now) {
    setRecruitmentStatus("現在より後の集合時間を選んでください。", "error");
    return;
  }
  if (scheduledAt.getTime() > now.getTime() + 24 * 60 * 60 * 1000) {
    setRecruitmentStatus("集合時間は24時間以内にしてください。", "error");
    return;
  }
  state.recruitmentSubmitPending = true;
  setRecruitmentStatus("投稿しています…", "");
  send({
    type: "create_recruitment",
    owner_token: recruitmentOwnerToken(),
    name,
    rule_key: el.recruitmentRule.value,
    scheduled_at: scheduledAt.toISOString(),
  });
  renderRecruitments();
}

function deleteRecruitment(recruitmentId) {
  if (!recruitmentId || !state.connected) return;
  send({
    type: "delete_recruitment",
    owner_token: recruitmentOwnerToken(),
    recruitment_id: recruitmentId,
  });
}

function setDefaultRecruitmentTime() {
  if (!el.recruitmentTime) return;
  const date = new Date(Date.now() + 60 * 60 * 1000);
  date.setSeconds(0, 0);
  date.setMinutes(Math.ceil(date.getMinutes() / 30) * 30);
  el.recruitmentTime.value = localDatetimeValue(date);
}

function updateRecruitmentTimeBounds() {
  if (!el.recruitmentTime) return;
  const now = new Date();
  el.recruitmentTime.min = localDatetimeValue(new Date(now.getTime() + 60 * 1000));
  el.recruitmentTime.max = localDatetimeValue(new Date(now.getTime() + (24 * 60 - 1) * 60 * 1000));
}

function localDatetimeValue(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function setRecruitmentStatus(message, tone = "") {
  if (!el.recruitmentStatus) return;
  el.recruitmentStatus.textContent = message;
  el.recruitmentStatus.classList.toggle("success", tone === "success");
  el.recruitmentStatus.classList.toggle("error", tone === "error");
}

function randomName() {
  return `プレイヤー${Math.floor(1000 + Math.random() * 9000)}`;
}

function randomSushiName() {
  const sushiNames = [
    "マグロ",
    "サーモン",
    "イカ",
    "エビ",
    "タマゴ",
    "アナゴ",
    "イクラ",
    "ホタテ",
    "ハマチ",
    "ネギトロ",
    "カンパチ",
    "ブリ",
  ];
  return sushiNames[Math.floor(Math.random() * sushiNames.length)];
}

function leaveRoom() {
  const pairing = state.tournament?.current_match;
  const assignedTournamentMatch = Boolean(
    isTournamentRoom()
    && pairing
    && [pairing.player1_id, pairing.player2_id].includes(state.tournamentParticipantId)
    && ["called", "playing"].includes(pairing.status)
  );
  const activePlayerLeaving = state.roomState === "playing" && state.isWaiting;
  if (activePlayerLeaving || assignedTournamentMatch) {
    const message = activePlayerLeaving
      ? "対戦中に退室すると負けになります。それでも退室しますか？"
      : "対戦の呼び出し中に退室すると不戦敗になります。それでも退室しますか？";
    if (!window.confirm(message)) return;
  }
  send({ type: "leave_room" });
  state.roomJoined = false;
  state.appMode = "setup";
  state.isWaiting = false;
  state.pendingFlow = null;
  state.cpuChooserOpen = false;
  state.selectedCpuKey = "";
  state.players = [];
  state.roomRosterInitialized = false;
  state.hand = [];
  state.currentTurn = "";
  state.fieldCards = [];
  state.handCounts = [];
  state.firstPlayerId = null;
  clearFlowPreview(false);
  clearSelection();
  renderAll();
}

function toggleReady() {
  if (state.roomState === "playing") return;
  state.isWaiting = !state.isWaiting;
  send({ type: "change_status", status: state.isWaiting ? "waiting" : "watching" });
  if (!state.isWaiting) {
    state.hand = [];
    clearSelection();
  }
  renderAll();
}

function addCpu(cpuKey = "") {
  const profiles = state.roomCpuProfiles[currentRoomId()] || [];
  const selected = profiles.find((profile) => profile.key === cpuKey) || profiles[0];
  if (!selected) {
    log("error", "この部屋で選べるCPUがありません。");
    return;
  }
  send({ type: "add_cpu", cpu_key: selected.key });
}

function removeCpu() {
  send({ type: "remove_cpu" });
}

function toggleCpu() {
  if (state.roomState === "playing") return;
  if (state.currentRoomHasCpu) {
    state.cpuChooserOpen = false;
    removeCpu();
  } else {
    state.cpuChooserOpen = !state.cpuChooserOpen;
    renderCpuChooser();
  }
}

function selectCpuProfile() {
  state.selectedCpuKey = el.cpuProfileSelect.value;
  renderCpuChooser();
}

function closeCpuChooser() {
  state.cpuChooserOpen = false;
  renderCpuChooser();
}

function confirmCpuSelection() {
  const cpuKey = el.cpuProfileSelect.value || state.selectedCpuKey;
  state.selectedCpuKey = cpuKey;
  state.cpuChooserOpen = false;
  addCpu(cpuKey);
  renderCpuChooser();
}

function startGame() {
  send({ type: "start_game" });
}

function loadSample() {
  const selected = el.sampleSelect.value || currentRoomOption().defaultSampleKey || CONFIG.defaultSampleKey;
  send({ type: "load_sample_registered_primes", sample_key: selected });
  el.registerStatus.textContent = "サンプル読み込み中...";
}

function saveRegisteredNumbers() {
  send({
    type: "set_registered_numbers",
    prime_text: el.primeText.value,
    composite_text: el.compositeText.value,
  });
  el.registerStatus.textContent = "登録中...";
}

function renderRegisteredStatus(msg) {
  if (msg.sample_key) el.sampleSelect.value = msg.sample_key;
  if (msg.sample_prime_text) el.primeText.value = msg.sample_prime_text;
  if (msg.sample_composite_text) el.compositeText.value = msg.sample_composite_text;
  if (Array.isArray(msg.prime_values) || Array.isArray(msg.composite_values)) {
    state.registeredPrimeValues = new Set((msg.prime_values || []).map((value) => String(value)));
    state.registeredCompositeValues = new Set((msg.composite_values || []).map((value) => String(value)));
  } else {
    rebuildRegisteredValueSets();
  }
  const primeCount = msg.prime_count ?? msg.count ?? 0;
  const compositeCount = msg.composite_count ?? 0;
  const errorCount = (msg.prime_errors || msg.errors || []).length + (msg.composite_errors || []).length;
  el.registerStatus.textContent = errorCount
    ? `素数 ${primeCount} / 合成数 ${compositeCount} / エラー ${errorCount}`
    : `素数 ${primeCount} / 合成数 ${compositeCount}`;
  renderSelection();
  scheduleAssist();
}

function renderSampleOptions() {
  const current = el.sampleSelect.value || currentRoomOption().defaultSampleKey || CONFIG.defaultSampleKey;
  const limit = state.roomRegisteredNumberLimits[currentRoomId()];
  const availableOptions = state.sampleOptions.filter((option) => (
    !Number.isFinite(limit)
    || !Number.isFinite(option.total_count)
    || option.total_count <= limit
  ));
  el.sampleSelect.innerHTML = "";
  availableOptions.forEach((option) => {
    const opt = document.createElement("option");
    opt.value = option.key;
    opt.textContent = readableSampleLabel(option);
    el.sampleSelect.appendChild(opt);
  });
  if ([...el.sampleSelect.options].some((option) => option.value === current)) {
    el.sampleSelect.value = current;
  }
}

function readableSampleLabel(option) {
  const labels = {
    sashimi2024: "おすすめセット",
    tournament_order: "大会風セット",
    gold_prime_table: "ゴールド素数表",
    silver_prime_table: "シルバー素数表",
  };
  const label = labels[option.key] || option.label || option.key;
  return Number.isFinite(option.total_count)
    ? `${label}（${option.total_count}件）`
    : label;
}

function renderPlayers(players, waitingCount) {
  state.players = players;
  const self = players.find((player) => player.id === state.playerId);
  if (self) state.isWaiting = self.status === "waiting";
  const participants = players
    .filter((player) => player.status === "waiting")
    .map(playerLabel);
  const watchers = players
    .filter((player) => player.status !== "waiting")
    .map(playerLabel);
  el.playerList.textContent = participants.length ? participants.join("、") : "なし";
  el.watcherList.textContent = watchers.length ? watchers.join("、") : "なし";
  el.playerList.title = participants.join("、");
  el.watcherList.title = watchers.join("、");
  if (waitingCount !== null) {
    const canStart = state.isWaiting && (waitingCount === 1 || waitingCount === 2);
    el.startBtn.disabled = !canStart;
  }
}

function detectPlayerJoined(nextPlayers) {
  const previousIds = new Set(state.players.map((player) => player.id));
  const otherHumanJoined = state.roomJoined
    && state.roomRosterInitialized
    && nextPlayers.some((player) => (
      player.id
      && player.id !== state.playerId
      && !player.is_cpu
      && !previousIds.has(player.id)
    ));

  state.roomRosterInitialized = true;
  if (otherHumanJoined) playPlayerJoinedSound();
}

function playerLabel(player) {
  const name = player.name || (player.is_cpu ? "CPU" : "プレイヤー");
  if (player.id === state.playerId) return `${name}（自分）`;
  if (player.is_cpu) return player.name || "CPU";
  return name;
}

function renderField(msg) {
  state.deckCount = String(msg.deck_count ?? "-");
  state.fieldNumber = msg.field_number == null ? "" : String(msg.field_number);
  state.revolution = Boolean(msg.revolution);
  state.fieldCards = msg.field || [];
  state.handCounts = msg.hand_counts || [];
  if (state.fieldCards.length) clearFlowPreview(false);
  el.deckCount.textContent = state.deckCount;
  renderFieldCards();
  renderHandMetrics();
}

function renderFieldCards() {
  const field = state.fieldCards.length ? state.fieldCards : state.flowPreviewCards;
  const isPreview = !state.fieldCards.length && state.flowPreviewCards.length;
  el.fieldCards.innerHTML = "";
  el.fieldNumber.textContent = isPreview
    ? displayFieldNumber(state.flowPreviewNumber)
    : displayFieldNumber(state.fieldNumber);
  el.fieldZone.classList.toggle("revolution", state.revolution);
  if (!field.length) {
    setCardRowColumns(el.fieldCards, 1);
    el.fieldCards.textContent = "まだ何も出ていません";
    el.fieldCards.classList.add("empty");
    el.fieldCards.classList.remove("flow-preview");
  } else {
    setCardRowColumns(el.fieldCards, field.length);
    el.fieldCards.classList.remove("empty");
    el.fieldCards.classList.toggle("flow-preview", isPreview);
    field.forEach((card) => el.fieldCards.appendChild(cardButton(card, { staticOnly: true, field: true })));
  }
}

function displayFieldNumber(number) {
  if (number === "X") return "∞";
  return number === "" || number == null ? "なし" : String(number);
}

function showFlowPreview(cards, number = "") {
  if (!cards.length) return;
  state.flowPreviewCards = cards;
  state.flowPreviewNumber = number == null ? "" : String(number);
  if (state.flowPreviewTimer) clearTimeout(state.flowPreviewTimer);
  renderFieldCards();
  state.flowPreviewTimer = setTimeout(() => clearFlowPreview(true), 1100);
}

function clearFlowPreview(shouldRender = true) {
  if (state.flowPreviewTimer) clearTimeout(state.flowPreviewTimer);
  state.flowPreviewTimer = null;
  state.flowPreviewCards = [];
  state.flowPreviewNumber = "";
  if (shouldRender) renderFieldCards();
}

function renderHandMetrics() {
  if (state.roomState === "playing" && !state.isWaiting) {
    const spectatorCounts = spectatorHandCounts();
    renderSpectatorHandMetric(el.myHandMetric, el.myHandLabel, el.myHandCount, "先手", spectatorCounts[0]);
    renderSpectatorHandMetric(el.opponentMetric, el.opponentLabel, el.opponentCounts, "後手", spectatorCounts[1]);
    return;
  }

  el.myHandLabel.textContent = "手札";
  el.myHandCount.textContent = String(state.hand.length);
  el.myHandMetric.title = `自分の手札: ${state.hand.length}枚`;
  el.myHandMetric.setAttribute("aria-label", `自分の手札 ${state.hand.length}枚`);

  el.opponentLabel.textContent = "相手";
  renderOpponentCounts(state.handCounts);
}

function spectatorHandCounts() {
  if (!state.firstPlayerId) return state.handCounts;
  const firstIndex = state.handCounts.findIndex((item) => item.id === state.firstPlayerId);
  if (firstIndex < 0) return state.handCounts;
  const first = state.handCounts[firstIndex];
  return [first, ...state.handCounts.filter((_, index) => index !== firstIndex)];
}

function renderSpectatorHandMetric(metric, label, count, side, player) {
  const playerName = player?.name || "";
  label.textContent = playerName ? `${side} ${playerName}` : side;
  count.textContent = player ? String(player.count) : "-";
  const description = player
    ? `${side} ${playerName}の手札: ${player.count}枚`
    : `${side}: プレイヤーなし`;
  metric.title = description;
  metric.setAttribute("aria-label", description);
}

function renderOpponentCounts(handCounts) {
  const opponents = handCounts.filter((item) => (
    state.playerId && item.id
      ? item.id !== state.playerId
      : item.name !== state.playerName
  ));
  if (!opponents.length) {
    el.opponentCounts.textContent = "-";
    el.opponentCounts.title = "";
    el.opponentMetric.title = "相手の手札: 未確定";
    el.opponentMetric.setAttribute("aria-label", "相手の手札 未確定");
    return;
  }
  el.opponentCounts.textContent = opponents.map((item) => item.count).join("/");
  el.opponentCounts.title = opponents.map((item) => `${item.name}: ${item.count}`).join(" / ");
  const description = opponents.map((item) => `${item.name}の手札 ${item.count}枚`).join("、");
  el.opponentMetric.title = description;
  el.opponentMetric.setAttribute("aria-label", description);
}

function renderAll() {
  document.body.dataset.mode = state.appMode;
  el.setupPanel.classList.toggle("hidden", state.appMode !== "setup");
  el.roomPanel.classList.toggle("hidden", state.appMode === "setup");
  el.guestModeToggle.disabled = state.roomJoined;
  updateIdentityModeUi();
  renderSoundToggle();
  renderRoomChoice();
  renderRecruitments();
  renderChat();
  el.playStatus.textContent = state.isWaiting
    ? state.roomState === "playing"
      ? "対戦中"
      : "対戦待ち"
    : "観戦中";
  const tournamentRoom = isTournamentRoom();
  el.readyBtn.textContent = state.isWaiting ? "待機をやめる" : "対戦に参加";
  el.readyBtn.classList.toggle("hidden", tournamentRoom);
  el.addCpuBtn.classList.toggle("hidden", tournamentRoom);
  el.startBtn.classList.toggle("hidden", tournamentRoom);
  el.readyBtn.disabled = tournamentRoom || state.roomState === "playing";
  el.addCpuBtn.textContent = state.currentRoomHasCpu ? "CPU退出" : "CPU追加";
  el.addCpuBtn.setAttribute("aria-expanded", String(state.cpuChooserOpen && !state.currentRoomHasCpu));
  el.addCpuBtn.disabled = state.roomState === "playing" || (
    !state.currentRoomHasCpu
    && !(state.roomCpuProfiles[currentRoomId()] || []).length
  );
  el.startBtn.disabled = tournamentRoom || state.roomState === "playing" || !state.isWaiting;
  if (el.reconnectPolicyNote) {
    el.reconnectPolicyNote.textContent = `通信切断時は対戦中${formatDuration(state.playingDisconnectGraceSeconds)}、待機中${formatDuration(state.waitingDisconnectGraceSeconds)}まで同じブラウザから復帰できます。「退室」は復帰待ちになりません。`;
  }
  renderCpuChooser();
  el.playBtn.disabled = !isMyTurn() || !state.selectedCards.length || (state.compositeMode && !state.compositeTokens.length);
  el.playBtn.textContent = isHnpChallengeSelection() ? "HNPチャレンジ" : "出す";
  el.compositeModeBtn.disabled = !state.allowComposite || state.roomState !== "playing" || !state.hand.length;
  el.compositeModeBtn.textContent = "合成数出し";
  el.compositeModeBtn.classList.toggle("hidden", state.compositeMode || !state.allowComposite);
  el.drawBtn.disabled = !isMyTurn();
  el.passBtn.disabled = !isMyTurn();
  const watchingTurn = state.roomState === "playing" && !state.isWaiting;
  el.turnBadge.textContent = isMyTurn()
    ? "あなたの番"
    : watchingTurn
      ? state.currentTurn
        ? `${state.currentTurn}の番`
        : "手番待ち"
      : state.roomState === "playing"
        ? "相手の番"
        : "待機中";
  el.turnBadge.classList.toggle("ready", isMyTurn());
  el.turnBadge.classList.toggle("alert", state.roomState === "playing" && state.isWaiting && !isMyTurn());
  el.turnBadge.classList.toggle("spectating", watchingTurn);
  renderHandMetrics();
  renderNextHint();
  renderHand();
  renderSelection();
  renderCompositeZone();
  renderAssist();
  renderTournament();
}

function renderRecruitments() {
  if (!CONFIG.features.recruitment || !el.recruitmentList) return;
  updateRecruitmentTimeBounds();
  const posts = state.recruitments.filter((post) => {
    const scheduledAt = new Date(post.scheduled_at);
    return !Number.isNaN(scheduledAt.getTime()) && scheduledAt > new Date();
  });
  el.recruitmentCount.textContent = `${posts.length} / ${state.recruitmentMaxCount}件`;
  el.recruitmentList.replaceChildren();

  if (!posts.length) {
    const empty = document.createElement("p");
    empty.className = "recruitment-empty";
    empty.textContent = state.connected
      ? "現在の募集はありません。最初の募集を投稿できます。"
      : "サーバーへ接続すると募集を表示します。";
    el.recruitmentList.append(empty);
  }

  posts.forEach((post) => {
    const scheduledAt = new Date(post.scheduled_at);
    const card = document.createElement("article");
    card.className = "recruitment-card";
    card.classList.toggle("mine", !!post.can_delete);

    const time = document.createElement("div");
    time.className = "recruitment-time";
    const timeLabel = document.createElement("strong");
    timeLabel.textContent = new Intl.DateTimeFormat("ja-JP", {
      hour: "2-digit",
      minute: "2-digit",
    }).format(scheduledAt);
    const dateLabel = document.createElement("small");
    dateLabel.textContent = new Intl.DateTimeFormat("ja-JP", {
      month: "numeric",
      day: "numeric",
      weekday: "short",
    }).format(scheduledAt);
    time.append(timeLabel, dateLabel);

    const person = document.createElement("div");
    person.className = "recruitment-person";
    const name = document.createElement("strong");
    name.textContent = post.name || "プレイヤー";
    const rule = document.createElement("span");
    rule.className = "recruitment-rule";
    rule.textContent = post.rule_label || "希望ルール未設定";
    person.append(name, rule);
    if (post.can_delete) {
      const ownerBadge = document.createElement("span");
      ownerBadge.className = "recruitment-owner-badge";
      ownerBadge.textContent = "あなたの募集";
      person.append(ownerBadge);
    }

    card.append(time, person);
    if (post.can_delete) {
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "recruitment-delete";
      deleteButton.dataset.deleteRecruitment = post.id;
      deleteButton.textContent = "削除";
      deleteButton.setAttribute("aria-label", `${post.name || "自分"}の募集を削除`);
      card.append(deleteButton);
    }
    el.recruitmentList.append(card);
  });

  const ownsPost = posts.some((post) => post.can_delete);
  const boardFull = posts.length >= state.recruitmentMaxCount;
  el.recruitmentSubmitBtn.disabled = (
    !state.connected
    || state.recruitmentSubmitPending
    || ownsPost
    || boardFull
  );
  el.recruitmentSubmitBtn.textContent = state.recruitmentSubmitPending
    ? "投稿中…"
    : ownsPost
      ? "1件投稿済み"
      : boardFull
        ? "現在5件あります"
        : "募集を投稿";
}

function renderTournament() {
  if (!CONFIG.features.tournament || !el.tournamentPanel) return;
  const visible = state.roomJoined && isTournamentRoom();
  el.tournamentPanel.classList.toggle("hidden", !visible);
  if (!visible) return;

  const tournament = state.tournament;
  const statusLabels = {
    scheduled: "開催予定",
    registration: "参加受付中",
    running: "進行中",
    finished: "終了",
    cancelled: "中止",
    unavailable: "未開催",
  };
  el.tournamentTitle.textContent = tournament?.title || "定期大会";
  el.tournamentStatusBadge.textContent = statusLabels[tournament?.status] || "未開催";
  el.tournamentStatusBadge.dataset.status = tournament?.status || "unavailable";

  if (!tournament || tournament.status === "unavailable") {
    el.tournamentSchedule.textContent = "次回日程は未設定です。";
    el.tournamentMessage.textContent = "管理者が日程とルールを設定すると、ここで参加登録できます。";
  } else {
    el.tournamentSchedule.textContent = `受付 ${formatTournamentDate(tournament.registration_opens_at)} / 開始 ${formatTournamentDate(tournament.starts_at)}`;
    el.tournamentMessage.textContent = tournament.status === "registration"
      ? `${tournament.participant_count}/${tournament.max_participants}人が登録済みです。`
      : tournament.status === "scheduled"
        ? "受付開始時刻になると参加登録ボタンが有効になります。"
        : tournament.status === "running"
          ? "対戦はシステムが順番に割り振り、自動で開始します。"
          : tournament.status === "finished"
            ? "全試合の最終結果です。"
            : "この開催回は中止になりました。";
  }

  const registered = Boolean(state.tournamentParticipantId || tournament?.registered);
  el.tournamentRegisterBtn.disabled = state.guestMode || !tournament || tournament.status !== "registration" || registered;
  el.tournamentRegisterBtn.textContent = state.guestMode
    ? "ゲストは観戦のみ"
    : registered
      ? "参加登録済み"
      : "大会に参加登録";
  el.tournamentWithdrawBtn.classList.toggle("hidden", !registered || tournament?.status !== "registration");

  const pairing = tournament?.current_match;
  el.tournamentPairing.classList.toggle("hidden", !pairing);
  if (pairing) {
    const isViewerMatch = [pairing.player1_id, pairing.player2_id].includes(state.tournamentParticipantId);
    const viewerReady = (pairing.ready_player_ids || []).includes(state.tournamentParticipantId);
    const player1Ready = (pairing.ready_player_ids || []).includes(pairing.player1_id);
    const player2Ready = (pairing.ready_player_ids || []).includes(pairing.player2_id);
    el.tournamentPairing.replaceChildren();
    const label = document.createElement("strong");
    label.textContent = `第${pairing.round_no}ラウンド`;
    const names = document.createElement("span");
    names.textContent = pairing.status === "called"
      ? `${pairing.player1_name}${player1Ready ? " ✓" : ""} vs ${pairing.player2_name}${player2Ready ? " ✓" : ""}`
      : `${pairing.player1_name} vs ${pairing.player2_name}`;
    const note = document.createElement("small");
    note.dataset.tournamentCountdown = "true";
    note.textContent = tournamentPairingNote(pairing, isViewerMatch, viewerReady);
    el.tournamentPairing.append(label, names, note);
    if (pairing.status === "called" && isViewerMatch && !viewerReady) {
      const readyButton = document.createElement("button");
      readyButton.type = "button";
      readyButton.className = "primary-button tournament-match-ready-button";
      readyButton.textContent = "この対戦に参加";
      readyButton.addEventListener("click", () => readyForTournamentMatch(pairing.match_id));
      el.tournamentPairing.appendChild(readyButton);
    }
  }

  renderTournamentLeagueTable(tournament?.league_table);
  el.tournamentStandings.replaceChildren();
  (tournament?.standings || []).forEach((row) => {
    const item = document.createElement("li");
    item.textContent = `${row.rank}位 ${row.display_name} — ${row.wins}勝${row.losses}敗 / ${row.points}点`;
    if (row.participant_id === state.tournamentParticipantId) item.classList.add("self");
    el.tournamentStandings.appendChild(item);
  });
  if (!el.tournamentStandings.children.length) {
    const item = document.createElement("li");
    item.textContent = "結果はまだありません";
    item.classList.add("empty");
    el.tournamentStandings.appendChild(item);
  }
  if (el.tournamentTimingNote) {
    const readySeconds = tournament?.match_ready_seconds ?? 60;
    const playingSeconds = tournament?.playing_disconnect_grace_seconds ?? 60;
    const waitingSeconds = tournament?.waiting_disconnect_grace_seconds ?? 180;
    el.tournamentTimingNote.textContent = `対戦呼び出しは両者確認で即開始、未確認でも両者接続中なら${readySeconds}秒後に自動開始します。切断復帰猶予は対戦中${formatDuration(playingSeconds)}、ロビー待機中${formatDuration(waitingSeconds)}です。復帰トークンはこのブラウザだけに保存します。`;
  }
}

function tournamentPairingNote(pairing, isViewerMatch, viewerReady) {
  if (pairing.status === "playing") return isViewerMatch ? "対戦中です" : "現在の対戦";
  if (pairing.status !== "called") return isViewerMatch ? "あなたの対戦です" : "現在の対戦";
  const remaining = tournamentReadySecondsRemaining(pairing);
  if (isViewerMatch && viewerReady) return `参加確認済み・相手を待っています（自動開始まで${remaining}秒）`;
  if (isViewerMatch) return `あなたの対戦です（自動開始まで${remaining}秒）`;
  return `対戦者を呼び出し中（自動開始まで${remaining}秒）`;
}

function tournamentReadySecondsRemaining(pairing) {
  if (!pairing?.ready_deadline_at) return state.tournament?.match_ready_seconds ?? 60;
  return Math.max(0, Math.ceil((Date.parse(pairing.ready_deadline_at) - Date.now()) / 1000));
}

function renderTournamentCallCountdown() {
  if (!state.roomJoined || !isTournamentRoom()) return;
  const pairing = state.tournament?.current_match;
  const note = el.tournamentPairing?.querySelector("[data-tournament-countdown]");
  if (!pairing || !note) return;
  const isViewerMatch = [pairing.player1_id, pairing.player2_id].includes(state.tournamentParticipantId);
  const viewerReady = (pairing.ready_player_ids || []).includes(state.tournamentParticipantId);
  note.textContent = tournamentPairingNote(pairing, isViewerMatch, viewerReady);
}

function renderTournamentLeagueTable(leagueTable) {
  if (!el.tournamentLeagueTable) return;
  el.tournamentLeagueTable.replaceChildren();
  const players = leagueTable?.players || [];
  if (!players.length) {
    el.tournamentLeagueTable.textContent = "組合せ確定後に表示します。";
    el.tournamentLeagueTable.classList.add("empty");
    return;
  }
  el.tournamentLeagueTable.classList.remove("empty");
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  const corner = document.createElement("th");
  corner.textContent = "選手";
  headRow.appendChild(corner);
  players.forEach((player) => {
    const cell = document.createElement("th");
    cell.textContent = player.display_name;
    cell.title = player.display_name;
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement("tbody");
  (leagueTable.rows || []).forEach((row) => {
    const tr = document.createElement("tr");
    const name = document.createElement("th");
    name.textContent = row.display_name;
    tr.appendChild(name);
    players.forEach((opponent) => {
      const result = row.cells?.[opponent.participant_id] || { result: "pending", label: "・" };
      const cell = document.createElement("td");
      cell.textContent = result.label;
      cell.className = `league-result league-result-${result.result}`;
      cell.title = `${row.display_name} vs ${opponent.display_name}: ${result.label}`;
      tr.appendChild(cell);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  el.tournamentLeagueTable.appendChild(table);
}

function formatDuration(seconds) {
  const total = Number(seconds) || 0;
  if (total % 60 === 0) return `${total / 60}分`;
  return `${total}秒`;
}

function formatTournamentDate(value) {
  if (!value) return "未設定";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function readSoundPreference() {
  try {
    return localStorage.getItem(SOUND_SETTING_KEY) === "true";
  } catch {
    return false;
  }
}

function saveSoundPreference() {
  try {
    localStorage.setItem(SOUND_SETTING_KEY, String(state.soundEnabled));
  } catch {
    // 保存できない環境でも、このページを開いている間は切り替えを有効にする。
  }
}

function getPlayerJoinedAudio() {
  if (!playerJoinedAudio) {
    playerJoinedAudio = new Audio(PLAYER_JOINED_SOUND_URL);
    playerJoinedAudio.preload = "auto";
  }
  return playerJoinedAudio;
}

function toggleSound() {
  state.soundEnabled = !state.soundEnabled;
  saveSoundPreference();
  renderSoundToggle();
  if (state.soundEnabled) unlockSoundPlayback();
}

function renderSoundToggle() {
  el.soundToggleBtn.classList.toggle("is-on", state.soundEnabled);
  el.soundToggleBtn.setAttribute("aria-pressed", String(state.soundEnabled));
  el.soundToggleBtn.setAttribute(
    "aria-label",
    state.soundEnabled ? "効果音をオフにする" : "効果音をオンにする",
  );
  el.soundToggleBtn.title = `効果音：${state.soundEnabled ? "オン" : "オフ"}`;
}

function unlockSoundPlayback() {
  if (!state.soundEnabled || soundUnlockPromise) return soundUnlockPromise;
  const audio = getPlayerJoinedAudio();
  const previousVolume = audio.volume;
  audio.muted = true;
  audio.volume = 0;
  soundUnlockPromise = audio.play()
    .then(() => {
      audio.pause();
      audio.currentTime = 0;
    })
    .catch(() => {})
    .finally(() => {
      audio.pause();
      audio.currentTime = 0;
      audio.volume = previousVolume;
      audio.muted = false;
      soundUnlockPromise = null;
    });
  return soundUnlockPromise;
}

function playPlayerJoinedSound() {
  if (!state.soundEnabled) return;
  if (soundUnlockPromise) {
    soundUnlockPromise.then(() => playPlayerJoinedSound());
    return;
  }
  const audio = getPlayerJoinedAudio();
  audio.pause();
  audio.currentTime = 0;
  audio.muted = false;
  audio.play().catch(() => {});
}

function renderCpuChooser() {
  const profiles = state.roomCpuProfiles[currentRoomId()] || [];
  const shouldShow = state.cpuChooserOpen
    && !state.currentRoomHasCpu
    && state.roomState !== "playing"
    && profiles.length > 0;
  el.addCpuBtn.setAttribute("aria-expanded", String(shouldShow));
  el.cpuChooser.classList.toggle("hidden", !shouldShow);
  if (!shouldShow) return;

  const selectedExists = profiles.some((profile) => profile.key === state.selectedCpuKey);
  if (!selectedExists) {
    const defaultCpuKey = currentRoomOption().defaultCpuKey;
    state.selectedCpuKey = profiles.some((profile) => profile.key === defaultCpuKey)
      ? defaultCpuKey
      : profiles[0].key;
  }

  el.cpuProfileSelect.replaceChildren(...profiles.map((profile) => {
    const option = document.createElement("option");
    option.value = profile.key;
    option.textContent = profile.label;
    return option;
  }));
  el.cpuProfileSelect.value = state.selectedCpuKey;

  const selected = profiles.find((profile) => profile.key === state.selectedCpuKey);
  const description = selected?.description || "このCPUの説明はありません。";
  const campaignPrefix = CONFIG.features.campaign
    && state.selectedRoomGroupKey === CONFIG.roomGroupOrder[0]
    && selected?.key === "gold_planner"
    ? "【イベント開催中】 "
    : "";
  el.cpuProfileDescription.textContent = `${campaignPrefix}${description}`;
  el.confirmCpuBtn.textContent = `${selected?.label || "CPU"}を追加`;
}

function renderRoomChoice() {
  const room = currentRoomOption();
  const roomId = currentRoomId();
  const [primaryGroupKey, secondaryGroupKey] = CONFIG.roomGroupOrder;

  el.roomGroupPrimaryBtn.parentElement.classList.toggle("hidden", !secondaryGroupKey);

  el.roomGroupPrimaryBtn.classList.toggle("active", state.selectedRoomGroupKey === primaryGroupKey);
  el.roomGroupSecondaryBtn.classList.toggle("active", !!secondaryGroupKey && state.selectedRoomGroupKey === secondaryGroupKey);
  el.roomGroupPrimaryBtn.disabled = state.roomJoined;
  el.roomGroupSecondaryBtn.disabled = state.roomJoined || !secondaryGroupKey;
  el.roomGroupPrimaryBtn.setAttribute("aria-pressed", String(state.selectedRoomGroupKey === primaryGroupKey));
  el.roomGroupSecondaryBtn.setAttribute("aria-pressed", String(state.selectedRoomGroupKey === secondaryGroupKey));
  renderRoomList();

  el.practiceBtn.textContent = `${room.label} ルーム${room.roomNumber}に入室する`;
  el.practiceBtn.disabled = !state.connected || !isRoomSelectable(state.selectedRoomKey);
  el.roomBadge.textContent = room.badge;
  el.roomHeading.textContent = `${room.label}ルーム ${room.roomNumber}`;
  const registeredNumberLimit = state.roomRegisteredNumberLimits[roomId];
  el.registerLimitNote.classList.toggle("hidden", !Number.isFinite(registeredNumberLimit));
  if (Number.isFinite(registeredNumberLimit)) {
    el.registerLimitNote.textContent = `${room.label}は素数・合成数あわせて${registeredNumberLimit}件まで登録できます。`;
  }
  if (state.connected && state.appMode === "setup") {
    renderServerLobbyStatus();
  }
}

function renderRoomList() {
  const roomGroup = currentRoomGroupOption();
  el.roomPickerHint.textContent = `${roomGroup.label}の入室人数`;
  el.roomList.setAttribute("aria-label", `${roomGroup.label}の部屋選択`);
  el.roomList.replaceChildren();

  roomGroup.roomKeys.forEach((roomKey) => {
    const room = CONFIG.rooms[roomKey];
    const available = isRoomAvailable(roomKey);
    const count = available ? state.roomCounts[room.roomId] ?? 0 : null;
    const full = available && count >= CONFIG.maxRoomPlayers;
    const active = state.selectedRoomKey === roomKey;
    const tournament = room.tournament ? state.tournaments[room.roomId] : null;
    const status = !available
      ? "準備中"
      : full
        ? "満員"
        : tournament?.status === "registration"
          ? `参加受付中 ${tournament.participant_count}/${tournament.max_participants}人`
          : tournament?.status === "running"
            ? "大会進行中"
            : tournament?.status === "scheduled"
              ? "開催予定"
              : count > 0 ? "参加者あり" : "空いています";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "room-slot-card";
    button.dataset.roomKey = roomKey;
    button.disabled = state.roomJoined || !available || full;
    button.classList.toggle("active", active);
    button.classList.toggle("unavailable", !available);
    button.classList.toggle("full", full);
    button.classList.toggle("has-players", available && count > 0 && !full);
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(active));
    button.setAttribute(
      "aria-label",
      `ルーム${room.roomNumber}、${available ? `${count}人入室中` : "準備中"}、${status}`,
    );

    const heading = document.createElement("span");
    heading.className = "room-slot-heading";
    const dot = document.createElement("i");
    dot.className = "room-status-dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("strong");
    name.textContent = `ルーム ${room.roomNumber}`;
    heading.append(dot, name);

    const ruleTitle = document.createElement("span");
    ruleTitle.className = "room-slot-rule";
    ruleTitle.textContent = room.title || room.badge || "";

    const ruleSummary = document.createElement("small");
    ruleSummary.className = "room-slot-summary";
    ruleSummary.textContent = room.summary || "";

    const population = document.createElement("span");
    population.className = "room-slot-population";
    const populationValue = document.createElement("b");
    populationValue.textContent = available ? String(count) : "−";
    const populationUnit = document.createElement("small");
    populationUnit.textContent = available ? ` / ${CONFIG.maxRoomPlayers}人` : " 人";
    population.append(populationValue, populationUnit);

    const statusLabel = document.createElement("small");
    statusLabel.className = "room-slot-status";
    statusLabel.textContent = status;
    button.append(heading, ruleTitle, ruleSummary, population, statusLabel);
    el.roomList.append(button);
  });
}

function renderServerLobbyStatus() {
  const roomGroup = currentRoomGroupOption();
  el.serverLabel.textContent = `${CONFIG.lobbyName} / ${roomGroup.label}の${roomGroup.roomKeys.length}部屋を表示中`;
}

function renderNextHint() {
  if (!state.roomJoined) {
    el.nextHint.textContent = "まず入室します。入室後に対戦参加、CPU追加、開始を選べます。";
    return;
  }
  if (state.roomState !== "playing") {
    if (isTournamentRoom()) {
      el.nextHint.textContent = state.tournament?.status === "registration"
        ? state.tournamentParticipantId
          ? "参加登録済みです。開始時刻になるとシステムが組合せと対戦を進行します。"
          : "大会パネルの「大会に参加登録」を押してください。"
        : state.tournament?.status === "running"
          ? "大会進行中です。対戦者に選ばれると自動でゲームが始まります。"
          : "次回大会の日程と受付開始をお待ちください。";
      return;
    }
    if (!state.isWaiting) {
      el.nextHint.textContent = "観戦中です。遊ぶ場合は「対戦に参加」を押してください。";
    } else if (!state.currentRoomHasCpu) {
      el.nextHint.textContent = "「開始」で一人プレイ、CPUを追加して「開始」でCPU対戦ができます。友だちを待つ場合はこのまま待てます。";
    } else {
      el.nextHint.textContent = "準備OKです。「開始」を押すと練習対戦が始まります。";
    }
    return;
  }
  if (isMyTurn()) {
    el.nextHint.textContent = state.lastAssistCandidates.length
      ? "おすすめ候補を押すと、出すカードが自動で選ばれます。"
      : "候補がない場合はドローかパスを試してください。";
  } else {
    el.nextHint.textContent = state.isWaiting
      ? "相手の番です。場と手札を見ながら次の候補を待ちましょう。"
      : `${state.currentTurn || "プレイヤー"}の番です。`;
  }
}

function renderHand() {
  el.remainingFinishNotice.classList.toggle(
    "hidden",
    !state.selectedCards.length || !state.remainingFinishExists,
  );
  el.handCards.innerHTML = "";
  if (!state.hand.length) {
    setCardRowColumns(el.handCards, 1);
    el.handCards.textContent = state.roomState === "playing" ? "手札を待っています" : "ゲーム開始後に表示されます";
    el.handCards.classList.add("empty-row");
    return;
  }
  const selectedIds = new Set(state.selectedCards.map((card) => card.card_id));
  const compositeIds = new Set(state.compositeTokens.filter((token) => token.kind === "card").map((token) => token.card_id));
  const visibleHand = state.hand.filter((card) => !selectedIds.has(card.card_id) && !compositeIds.has(card.card_id));
  if (!visibleHand.length) {
    setCardRowColumns(el.handCards, 1);
    el.handCards.textContent = "すべて選択中または材料中";
    el.handCards.classList.add("empty-row");
    return;
  }
  setCardRowColumns(el.handCards, visibleHand.length);
  el.handCards.classList.remove("empty-row");
  visibleHand.forEach((card) => {
    const btn = cardButton(card);
    btn.addEventListener("click", () => toggleCard(card, "hand"));
    el.handCards.appendChild(btn);
  });
}

function renderSelection() {
  el.selectedCards.innerHTML = "";
  if (!state.selectedCards.length) {
    if (state.assistFilters.target_scope === "unselected") state.assistFilters.target_scope = "all";
    setCardRowColumns(el.selectedCards, 1);
    el.selectedCards.textContent = "未選択";
    el.selectedCards.classList.add("empty-row");
    el.selectedTitle.textContent = "選択中: なし";
  } else {
    setCardRowColumns(el.selectedCards, state.selectedCards.length);
    el.selectedCards.classList.remove("empty-row");
    state.selectedCards.forEach((card) => {
      const btn = cardButton(card);
      btn.classList.add("selected");
      btn.addEventListener("click", () => toggleCard(card, "selected"));
      el.selectedCards.appendChild(btn);
    });
    const number = selectedNumberText();
    el.selectedTitle.textContent = number
      ? `選択中: ${number}${selectedNumberRegistrationLabel()}${isHnpChallengeSelection() ? " (HNP)" : ""}`
      : "選択中: X=?";
  }
  renderJokerControls();
}

function setCardRowColumns(container, count) {
  const cardCount = Math.max(1, count || 1);
  const columns = Math.min(18, cardCount);
  const styles = getComputedStyle(container);
  const cardWidth = parseFloat(styles.getPropertyValue("--card-width")) || 52;
  const configuredGap = parseFloat(styles.getPropertyValue("--card-gap")) || 0;
  const availableWidth = container.clientWidth || 0;
  const fullWidth = cardCount * cardWidth + Math.max(0, cardCount - 1) * configuredGap;

  if (availableWidth > 0 && fullWidth <= availableWidth) {
    container.style.gridTemplateColumns = `repeat(${cardCount}, var(--card-width))`;
    container.style.columnGap = "var(--card-gap)";
    return;
  }

  const overlapStep = columns > 1
    ? Math.max(1, (availableWidth - cardWidth) / (columns - 1))
    : cardWidth;
  container.style.gridTemplateColumns = `repeat(${columns}, ${overlapStep}px)`;
  container.style.columnGap = "0px";
}

function renderJokerControls() {
  el.jokerControls.innerHTML = "";
  const jokers = state.selectedCards.filter(isJoker);
  jokers.forEach((_, index) => {
    const label = document.createElement("label");
    label.textContent = `ジョーカー${index + 1}`;
    const select = document.createElement("select");
    ["inf", ...Array.from({ length: 14 }, (_, i) => String(i))].forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value === "inf" ? "∞" : value;
      select.appendChild(option);
    });
    select.value = state.jokerAssignedRanks[index] ?? "inf";
    select.addEventListener("change", () => {
      state.jokerAssignedRanks[index] = select.value;
      renderSelection();
      scheduleAssist();
    });
    label.appendChild(select);
    el.jokerControls.appendChild(label);
  });
}

function renderCompositeZone() {
  el.compositePanel.classList.toggle("hidden", !state.compositeMode);
  el.compositeCards.innerHTML = "";
  el.compositeJokerControls.innerHTML = "";
  if (!state.compositeMode) return;

  normalizeCompositeJokerRanks();
  const expression = tokensToText(state.compositeTokens, state.compositeJokerAssign);
  el.compositeTitle.textContent = `${selectedNumberText() || "?"} = ${expression || "材料未選択"}`;
  if (!state.compositeTokens.length) {
    setCardRowColumns(el.compositeCards, 1);
    el.compositeCards.textContent = "手札から材料札を選んでください";
    el.compositeCards.classList.add("empty-row");
  } else {
    setCardRowColumns(el.compositeCards, state.compositeTokens.length);
    el.compositeCards.classList.remove("empty-row");
    state.compositeTokens.forEach((token, index) => {
      if (token.kind === "op") {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "operator-token";
        btn.textContent = token.op;
        btn.title = "クリックで削除";
        btn.addEventListener("click", () => removeCompositeToken(index));
        el.compositeCards.appendChild(btn);
        return;
      }

      const card = cardForToken(token);
      const btn = cardButton(card);
      btn.classList.add("selected");
      btn.title = "クリックで材料から外す";
      btn.addEventListener("click", () => removeCompositeToken(index));
      el.compositeCards.appendChild(btn);
    });
  }

  renderCompositeJokerControls();
}

function renderCompositeJokerControls() {
  const jokers = state.compositeTokens
    .filter((token) => token.kind === "card")
    .map(cardForToken)
    .filter(isJoker);
  normalizeCompositeJokerRanks();
  jokers.forEach((_, index) => {
    const label = document.createElement("label");
    label.textContent = `式ジョーカー${index + 1}`;
    const select = document.createElement("select");
    ["inf", ...Array.from({ length: 14 }, (_, i) => String(i))].forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value === "inf" ? "∞" : value;
      select.appendChild(option);
    });
    select.value = state.compositeJokerAssign[index] ?? "inf";
    select.addEventListener("change", () => {
      state.compositeJokerAssign[index] = select.value;
      renderCompositeZone();
    });
    label.appendChild(select);
    el.compositeJokerControls.appendChild(label);
  });
}

function renderAssist() {
  if (!state.assistEnabled) return;
  const recommendationMode = state.assistFilters.order === "recommended";
  el.assistRecommendedBtn.classList.toggle("active", recommendationMode);
  el.assistStrongBtn.classList.toggle("active", state.assistFilters.order === "strong");
  el.assistEasyBtn.classList.toggle("active", state.assistFilters.order === "weak");
  el.assistRestBtn.classList.toggle("active", state.assistFilters.target_scope === "unselected");
  el.assistRestBtn.disabled = !state.selectedCards.length;
  el.assistManyBtn.classList.toggle("hidden", recommendationMode);
  el.assistManyBtn.classList.toggle("active", state.assistFilters.limit_mode === "fifty");
  el.assistManyBtn.textContent = state.assistFilters.limit_mode === "fifty" ? "減らす" : "増やす";
  el.assistManyBtn.title = state.assistFilters.limit_mode === "fifty" ? "候補: 多め" : "候補: 少なめ";
  el.assistList.innerHTML = "";
  if (state.roomState !== "playing" || !state.hand.length) {
    el.assistList.textContent = "ゲーム開始後に候補が出ます";
    el.assistList.classList.add("empty");
    return;
  }
  if (!state.lastAssistCandidates.length) {
    el.assistList.textContent = "候補なし";
    el.assistList.classList.add("empty");
    return;
  }
  el.assistList.classList.remove("empty");
  state.lastAssistCandidates.forEach((candidate, index) => {
    const btn = document.createElement("button");
    btn.className = "assist-card";
    btn.type = "button";

    const number = document.createElement("span");
    number.className = "assist-number";
    number.textContent = candidate.visible_text || candidate.number || assistCardsText(candidate.cards, candidate.assigned_numbers);
    btn.appendChild(number);

    const tags = assistTags(candidate);
    if (tags.length) {
      const tagRow = document.createElement("span");
      tagRow.className = "assist-tags";
      tags.forEach((label) => {
        const tag = document.createElement("span");
        tag.className = "assist-tag";
        tag.textContent = label;
        tagRow.appendChild(tag);
      });
      btn.appendChild(tagRow);
    }

    btn.addEventListener("click", () => applyAssistCandidate(candidate));
    el.assistList.appendChild(btn);
  });
}

function assistTags(candidate) {
  const tags = [];
  if (candidate.kind === "composite") tags.push("合成");
  if (candidate.special_effect === "infinity") tags.push("∞");
  if (candidate.special_effect === "cut") tags.push("カット");
  if (candidate.special_effect === "revolution") tags.push("革命");
  if (candidate.field_count_match === false) tags.push("枚数注意");
  if (candidate.next_finish) tags.push("次で上がり");
  if (candidateFinishesRemaining(candidate)) tags.push("残り上がり");
  if (candidate.finishes_hand) tags.push("上がり");
  return tags;
}

function candidateFinishesRemaining(candidate) {
  if (candidate.finishes_remaining) return true;
  const selectedIds = new Set([
    ...state.selectedCards.map((card) => card.card_id),
    ...state.compositeTokens
      .filter((token) => token.kind === "card")
      .map((token) => token.card_id),
  ]);
  if (!selectedIds.size) return false;

  const remainingIds = new Set(
    state.hand
      .filter((card) => !selectedIds.has(card.card_id))
      .map((card) => card.card_id),
  );
  const usedIds = new Set([
    ...(candidate.cards || []).map((card) => card.card_id),
    ...(((candidate.composite || {}).cards) || []).map((card) => card.card_id),
  ]);
  return remainingIds.size > 0
    && remainingIds.size === usedIds.size
    && [...remainingIds].every((cardId) => usedIds.has(cardId));
}

function toggleCard(card, source = "hand") {
  if (state.compositeMode && source === "hand") {
    addCompositeCard(card);
    return;
  }
  const index = state.selectedCards.findIndex((item) => item.card_id === card.card_id);
  if (index >= 0) {
    state.selectedCards.splice(index, 1);
  } else {
    state.selectedCards.push(card);
  }
  normalizeJokerRanks();
  renderAll();
  scheduleAssist();
}

function toggleCompositeMode() {
  if (state.compositeMode) {
    clearCompositeMode();
  } else {
    state.compositeMode = true;
    state.compositeTokens = [];
    state.compositeJokerAssign = [];
  }
  renderAll();
}

function addCompositeCard(card) {
  removeCompositeCard(card.card_id);
  state.selectedCards = state.selectedCards.filter((item) => item.card_id !== card.card_id);
  state.compositeTokens.push({ kind: "card", card_id: card.card_id });
  normalizeJokerRanks();
  normalizeCompositeJokerRanks();
  renderAll();
  scheduleAssist();
}

function addCompositeOp(op) {
  if (!state.compositeMode) return;
  const last = state.compositeTokens[state.compositeTokens.length - 1];
  if (!last || last.kind !== "card") return;
  state.compositeTokens.push({ kind: "op", op });
  renderAll();
}

function removeCompositeToken(index) {
  state.compositeTokens.splice(index, 1);
  normalizeCompositeJokerRanks();
  renderAll();
}

function removeCompositeCard(cardId) {
  const index = state.compositeTokens.findIndex((token) => token.kind === "card" && token.card_id === cardId);
  if (index >= 0) state.compositeTokens.splice(index, 1);
}

function applyAssistCandidate(candidate) {
  const handById = new Map(state.hand.map((card) => [card.card_id, card]));
  state.selectedCards = (candidate.cards || []).map((card) => handById.get(card.card_id)).filter(Boolean);
  state.jokerAssignedRanks = (candidate.assigned_numbers || []).map(String);
  if (candidate.kind === "composite") {
    state.compositeMode = true;
    state.compositeTokens = ((candidate.composite && candidate.composite.tokens) || []).map((token) => ({ ...token }));
    state.compositeJokerAssign = ((candidate.composite && candidate.composite.assigned_numbers) || []).map(String);
  } else {
    clearCompositeMode();
  }
  normalizeJokerRanks();
  normalizeCompositeJokerRanks();
  scheduleAssist(0);
  renderAll();
}

function clearSelection() {
  state.selectedCards = [];
  state.jokerAssignedRanks = [];
  clearCompositeMode();
  renderAll();
  scheduleAssist();
}

function clearCompositeMode() {
  state.compositeMode = false;
  state.compositeTokens = [];
  state.compositeJokerAssign = [];
}

function normalizeJokerRanks() {
  const count = state.selectedCards.filter(isJoker).length;
  state.jokerAssignedRanks = state.jokerAssignedRanks.slice(0, count);
  while (state.jokerAssignedRanks.length < count) state.jokerAssignedRanks.push("inf");
}

function normalizeCompositeJokerRanks() {
  const count = state.compositeTokens
    .filter((token) => token.kind === "card")
    .map(cardForToken)
    .filter(isJoker).length;
  state.compositeJokerAssign = state.compositeJokerAssign.slice(0, count);
  while (state.compositeJokerAssign.length < count) state.compositeJokerAssign.push("inf");
}

function cardForToken(token) {
  return state.hand.find((card) => card.card_id === token.card_id) || token;
}

function selectedNumberText() {
  if (!state.selectedCards.length) return "";
  const parts = [];
  let jokerIndex = 0;
  for (const card of state.selectedCards) {
    if (isJoker(card)) {
      const value = state.jokerAssignedRanks[jokerIndex++] ?? "inf";
      if (value === "inf") return "";
      parts.push(value);
    } else {
      parts.push(String(card.rank));
    }
  }
  const text = parts.join("");
  return text.startsWith("0") ? "" : text;
}

function registeredPatternToValue(pattern) {
  const faceValues = { t: "10", j: "11", q: "12", k: "13" };
  const text = String(pattern || "").trim().toLowerCase();
  if (!/^[0-9tjqk]+$/.test(text)) return null;
  const value = [...text].map((char) => faceValues[char] || char).join("");
  return !value || value.startsWith("0") ? null : value;
}

function rebuildRegisteredValueSets() {
  state.registeredPrimeValues = new Set();
  state.registeredCompositeValues = new Set();
  String(el.primeText.value || "").split(/[\s,、，]+/).forEach((token) => {
    const value = registeredPatternToValue(token);
    if (value !== null) state.registeredPrimeValues.add(value);
  });
  String(el.compositeText.value || "").split(/\r?\n/).forEach((line) => {
    const left = line.split("=")[0].split("|")[0].trim();
    const token = left.split(/\s+/)[0];
    const value = registeredPatternToValue(token);
    if (value !== null) state.registeredCompositeValues.add(value);
  });
}

function selectedNumberRegistrationLabel() {
  const number = selectedNumberText();
  if (!number) return "";
  const registeredValues = state.compositeMode
    ? state.registeredCompositeValues
    : state.registeredPrimeValues;
  return registeredValues.has(number) ? "（登録済み）" : "";
}

function isHnpChallengeSelection() {
  if (!state.hnpChallengeEnabled || state.compositeMode || state.selectedCards.length < 2) return false;
  const number = selectedNumberText();
  if (!number || number === "57" || number === "1729") return false;
  return !state.registeredPrimeValues.has(number);
}

function playSelected() {
  if (!state.selectedCards.length) return;
  if (state.compositeMode) {
    const lastToken = state.compositeTokens[state.compositeTokens.length - 1];
    if (!lastToken || lastToken.kind !== "card") {
      log("error", "合成数出しゾーンに材料札で終わる式を作ってください。");
      return;
    }
    if (!selectedNumberText() || state.jokerAssignedRanks.some((value) => String(value) === "inf")) {
      log("error", "合成数出しでは、選択中のジョーカー値を数字にしてください。");
      return;
    }
    if (state.compositeJokerAssign.some((value) => String(value) === "inf")) {
      log("error", "合成数出しゾーンのジョーカー値を数字にしてください。");
      return;
    }
    send({
      type: "play_card",
      mode: "composite",
      selected: {
        cards: state.selectedCards,
        assigned_numbers: state.jokerAssignedRanks,
      },
      consume: {
        cards: compositeConsumeCards(),
      },
      composite: {
        tokens: state.compositeTokens,
        assigned_numbers: state.compositeJokerAssign,
      },
    });
  } else {
    send({
      type: "play_card",
      cards: state.selectedCards,
      assigned_numbers: state.jokerAssignedRanks,
    });
  }
  clearSelection();
}

function compositeConsumeCards() {
  const handById = new Map(state.hand.map((card) => [card.card_id, card]));
  return state.compositeTokens
    .filter((token) => token.kind === "card")
    .map((token) => handById.get(token.card_id))
    .filter(Boolean);
}

function scheduleAssist(delay = 220) {
  if (!state.assistEnabled) return;
  if (state.assistTimer) clearTimeout(state.assistTimer);
  state.assistRequestVersion += 1;
  const requestVersion = state.assistRequestVersion;
  state.remainingFinishExists = false;
  if (el.remainingFinishNotice) el.remainingFinishNotice.classList.add("hidden");
  state.assistTimer = setTimeout(() => requestAssist(requestVersion), delay);
}

function requestAssist(requestVersion = state.assistRequestVersion) {
  state.assistTimer = null;
  if (!state.assistEnabled) return;
  if (requestVersion !== state.assistRequestVersion) return;
  if (state.roomState !== "playing" || !state.hand.length || !state.roomJoined) return;
  send({
    type: "get_prime_assist",
    assist_request_id: requestVersion,
    selected_card_ids: state.selectedCards.map((card) => card.card_id),
    composite_card_ids: state.compositeTokens
      .filter((token) => token.kind === "card")
      .map((token) => token.card_id),
    filters: state.assistFilters,
    limit: state.assistFilters.limit_mode === "fifty" ? 50 : 10,
  });
}

function setAssistOrder(order) {
  state.assistFilters.order = order;
  scheduleAssist();
  renderAssist();
}

function toggleAssistLimit() {
  state.assistFilters.limit_mode = state.assistFilters.limit_mode === "fifty" ? "ten" : "fifty";
  scheduleAssist();
  renderAssist();
}

function toggleAssistRest() {
  state.assistFilters.target_scope = state.assistFilters.target_scope === "unselected" ? "all" : "unselected";
  scheduleAssist();
  renderAssist();
}

function cardButton(card, options = {}) {
  const btn = document.createElement(options.staticOnly ? "div" : "button");
  btn.className = `playing-card ${isRedSuit(card.suit) ? "red" : ""}`;
  if (options.field) btn.classList.add("field-card");
  if (!options.staticOnly) btn.type = "button";
  const suit = document.createElement("span");
  suit.className = "suit";
  suit.textContent = suitLabel(card);
  const rank = document.createElement("span");
  rank.className = "rank";
  rank.textContent = rankLabel(card);
  btn.append(suit, rank);
  return btn;
}

function suitLabel(card) {
  if (isJoker(card)) return "☆";
  return { H: "♥", D: "♦", S: "♠", C: "♣" }[card.suit] || card.suit || "";
}

function rankLabel(card) {
  if (isJoker(card)) return "X";
  return { 1: "A", 10: "T", 11: "J", 12: "Q", 13: "K" }[Number(card.rank)] || String(card.rank);
}

function isJoker(card) {
  return card?.is_joker || card?.suit === "X";
}

function isRedSuit(suit) {
  return suit === "H" || suit === "D";
}

function assistCardsText(cards = [], assigned = []) {
  let jokerIndex = 0;
  return cards
    .map((card) => {
      if (!isJoker(card)) return rankLabel(card);
      const value = assigned[jokerIndex++] ?? "?";
      return value === "inf" ? "X" : `X=${value}`;
    })
    .join("");
}

function tokensToText(tokens = [], assigned = []) {
  const handById = new Map(state.hand.map((card) => [card.card_id, card]));
  let jokerIndex = 0;
  return tokens
    .map((token) => {
      if (token.kind === "op") return token.op === "*" ? "×" : token.op;
      const card = handById.get(token.card_id) || token;
      if (!isJoker(card)) return rankLabel(card);
      const value = assigned[jokerIndex++] ?? "?";
      return `X=${value}`;
    })
    .join(" ");
}

function isMyTurn() {
  return Boolean(state.currentTurn && state.playerName && state.currentTurn === state.playerName && state.roomState === "playing");
}

function switchChatMode(mode) {
  if (!['room', 'global'].includes(mode)) return;
  state.chatMode = mode;
  if (mode === "global" && state.globalChatSubscribed) state.globalUnreadCount = 0;
  renderChat();
}

function enableGlobalChat() {
  if (!state.connected || !state.roomJoined || state.globalChatJoining || state.globalChatSubscribed) return;
  state.globalChatJoining = true;
  send({ type: "join_global_chat" });
  renderChat();
}

function sendGlobalTemplate(templateKey) {
  if (!state.globalChatSubscribed || !templateKey) return;
  send({ type: "global_chat", template_key: templateKey });
}

function renderChat() {
  const isGlobal = state.chatMode === "global";
  el.roomChatTab.classList.toggle("active", !isGlobal);
  el.globalChatTab.classList.toggle("active", isGlobal);
  el.roomChatTab.setAttribute("aria-selected", String(!isGlobal));
  el.globalChatTab.setAttribute("aria-selected", String(isGlobal));
  el.globalUnreadBadge.classList.toggle("hidden", state.globalUnreadCount === 0 || isGlobal);
  el.globalUnreadBadge.textContent = state.globalUnreadCount > 9 ? "9+" : String(state.globalUnreadCount);

  const showGate = isGlobal && !state.globalChatSubscribed;
  el.globalChatGate.classList.toggle("hidden", !showGate);
  el.globalQuickMessages.classList.toggle("hidden", !isGlobal || !state.globalChatSubscribed);
  el.chatComposer.classList.toggle("hidden", showGate);
  el.roomLogBox.classList.toggle("hidden", isGlobal);
  el.globalLogBox.classList.toggle("hidden", !isGlobal || !state.globalChatSubscribed);

  el.enableGlobalChatBtn.disabled = !state.connected || !state.roomJoined || state.globalChatJoining;
  el.enableGlobalChatBtn.textContent = state.globalChatJoining
    ? "接続しています…"
    : "注意事項を確認して表示する";
  el.chatInput.placeholder = isGlobal ? `${CONFIG.productName}全体へのメッセージ` : "部屋へのメッセージ";
  el.chatBtn.disabled = isGlobal && !state.globalChatSubscribed;
}

function sendChat() {
  const message = el.chatInput.value.trim();
  if (!message) return;
  send({ type: state.chatMode === "global" ? "global_chat" : "chat", message });
  el.chatInput.value = "";
}

function send(payload) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    log("error", "まだサーバーに接続できていません。");
    return;
  }
  state.ws.send(JSON.stringify(payload));
}

function setConnection(kind, label, detail) {
  el.connectionDot.className = "dot";
  if (kind === "online") el.connectionDot.classList.add("online");
  if (kind === "error") el.connectionDot.classList.add("error");
  el.connectionLabel.textContent = label;
  el.serverLabel.textContent = detail;
}

function log(sender, message) {
  const line = document.createElement("div");
  line.className = "log-line";
  const strong = document.createElement("strong");
  strong.textContent = sender;
  line.append(strong, document.createTextNode(`: ${message}`));
  el.roomLogBox.prepend(line);
}

function logGlobalSystem(message) {
  const line = document.createElement("div");
  line.className = "log-line global-chat-system";
  line.textContent = message;
  el.globalLogBox.prepend(line);
}

function logGlobalChat(message) {
  const line = document.createElement("div");
  line.className = "log-line global-chat-line";
  const meta = document.createElement("div");
  meta.className = "global-chat-meta";

  if (message.room_badge) {
    const roomBadge = document.createElement("span");
    roomBadge.className = "global-room-badge";
    if (["advanced", "classic", "plus", "neutral"].includes(message.room_tone)) {
      roomBadge.classList.add(message.room_tone);
    }
    roomBadge.textContent = message.room_badge;
    meta.append(roomBadge);
  }
  if (message.template_badge) {
    const templateBadge = document.createElement("span");
    templateBadge.className = "global-template-badge";
    templateBadge.textContent = message.template_badge;
    meta.append(templateBadge);
  }

  const sender = document.createElement("strong");
  sender.textContent = message.sender || "プレイヤー";
  const body = document.createElement("div");
  body.className = "global-chat-message";
  body.textContent = message.message || "";
  meta.append(sender);
  line.append(meta, body);
  el.globalLogBox.prepend(line);
}

function logScoreRecord(lines) {
  if (!lines.length) return;
  const entry = document.createElement("details");
  entry.className = "log-line score-record";
  entry.open = true;

  const summary = document.createElement("summary");
  summary.textContent = "数譜";
  entry.appendChild(summary);

  const pre = document.createElement("pre");
  pre.textContent = lines.join("\n");
  entry.appendChild(pre);

  el.roomLogBox.prepend(entry);
}
