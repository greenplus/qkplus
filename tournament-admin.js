const params = new URLSearchParams(location.search);
const wsUrl = params.get("ws") || "wss://web-production-c8e68.up.railway.app/ws/plus";
const el = Object.fromEntries([
  "hyakkiCurrentRule", "hyakkiRuleKey", "hyakkiApplyBtn", "winsRequired", "cancelTournamentBtn", "reservationList",
  "connectionStatus", "scheduleForm", "scheduleBtn", "cancelConfirmation", "cancelConfirmationText", "cancelConfirmBtn", "cancelBackBtn", "adminToken", "title", "formatKey", "ruleKey",
  "registrationOpensAt", "startsAt", "maxParticipants", "refreshBtn", "historyBtn",
  "runSummary", "currentMatch", "matchList", "standings", "historyList", "messageLog",
  "pairingMode", "registrationClosesAt", "registrationClosesLabel", "flexiblePolicy",
].map((id) => [id, document.getElementById(id)]));
let ws;
let tournament = null;
let pendingCancellation = null;

el.hyakkiApplyBtn.addEventListener("click", () => send({
  type: "tournament_admin_hyakki_rule", admin_token: el.adminToken.value,
  rule_key: el.hyakkiRuleKey.value,
}));
setDefaultDates();
connect();
el.pairingMode.addEventListener("change", () => {
  const flexible = el.pairingMode.value === "flexible";
  el.registrationClosesLabel.hidden = el.flexiblePolicy.hidden = !flexible;
  el.registrationClosesAt.required = flexible;
});

el.scheduleForm.addEventListener("submit", event => event.preventDefault());
el.scheduleForm.addEventListener("keydown", event => {
  if (event.key === "Enter") event.preventDefault();
});
el.scheduleBtn.addEventListener("click", () => {
  if (!el.scheduleForm.reportValidity()) return;
  send({
    type: "tournament_admin_schedule",
    admin_token: el.adminToken.value,
    room_id: "plus_tournament_1",
    title: el.title.value.trim(),
    format_key: el.formatKey.value.trim(),
    rule_key: el.ruleKey.value,
    wins_required: Number(el.winsRequired.value),
    registration_opens_at: new Date(el.registrationOpensAt.value).toISOString(),
    starts_at: new Date(el.startsAt.value).toISOString(),
    max_participants: Number(el.maxParticipants.value),
    pairing_mode: el.pairingMode.value,
    ...(el.pairingMode.value === "flexible" ? {registration_closes_at: new Date(el.registrationClosesAt.value).toISOString()} : {}),
  });
});
el.refreshBtn.addEventListener("click", () => send({ type: "get_tournament_status" }));
el.cancelTournamentBtn.addEventListener("click", () => cancelTournament(tournament));
function cancelTournament(run) {
  if (!run || run.cancellation_requested_at) return;
  pendingCancellation = {run_id: run.run_id, title: run.title};
  el.cancelConfirmationText.textContent = `大会「${run.title}」を中止しますか？`;
  el.cancelConfirmation.showModal();
  el.cancelBackBtn.focus();
}
function closeCancellationConfirmation() {
  pendingCancellation = null; el.cancelConfirmation.close();
}
el.cancelBackBtn.addEventListener("click", closeCancellationConfirmation);
el.cancelConfirmation.addEventListener("cancel", event => { event.preventDefault(); closeCancellationConfirmation(); });
el.cancelConfirmation.addEventListener("keydown", event => {
  if (event.key === "Escape") closeCancellationConfirmation();
  if (event.key === "Enter") event.preventDefault();
});
el.cancelConfirmBtn.addEventListener("click", () => {
  if (!pendingCancellation) return;
  const runId = pendingCancellation.run_id;
  closeCancellationConfirmation();
  send({type: "tournament_admin_cancel", admin_token: el.adminToken.value, room_id: "plus_tournament_1", run_id: runId});
});
el.historyBtn.addEventListener("click", () => send({
  type: "tournament_admin_history",
  admin_token: el.adminToken.value,
}));
el.matchList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  send({
    type: button.dataset.action === "skip" ? "tournament_admin_skip" : "tournament_admin_resolve",
    admin_token: el.adminToken.value,
    room_id: "plus_tournament_1",
    run_id: tournament?.run_id,
    match_id: button.dataset.matchId,
    ...(button.dataset.winnerId ? { winner_id: button.dataset.winnerId } : {}),
  });
});

function connect() {
  ws = new WebSocket(wsUrl);
  ws.addEventListener("open", () => {
    el.connectionStatus.textContent = "接続済み";
    el.connectionStatus.classList.add("online");
    send({ type: "get_tournament_status" });
    send({ type: "get_hyakki_room_rules" });
  });
  ws.addEventListener("close", () => {
    el.connectionStatus.textContent = "切断";
    el.connectionStatus.classList.remove("online");
    message("サーバーとの接続が切れました。ページを再読み込みしてください。", true);
  });
  ws.addEventListener("message", ({ data }) => handleMessage(JSON.parse(data)));
}

function send(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return message("サーバーへ接続していません。", true);
  ws.send(JSON.stringify(payload));
}

function handleMessage(msg) {
  if (msg.type === "hyakki_room_rules") {
    const rules = msg.available_rules || [];
    el.hyakkiRuleKey.replaceChildren(...rules.map(rule => {
      const option = document.createElement("option");
      option.value = rule.key; option.textContent = rule.label; return option;
    }));
    el.hyakkiRuleKey.value = Object.values(msg.room_rules || {})[0] || "";
    const keys = [...new Set(Object.values(msg.room_rules || {}))];
    el.hyakkiCurrentRule.textContent = "現在のルール: " + keys.map(key =>
      rules.find(rule => rule.key === key)?.label || key).join(" / ")
      + (msg.persistent ? "（保存済み）" : "（再起動までの一時設定）");
    return;
  }
  if (msg.type === "tournament_update" || msg.type === "tournament_admin_result") {
    tournament = msg.tournament;
    renderRuleOptions(tournament?.available_rules || []);
    renderTournament();
    if (msg.type === "tournament_admin_result") message("変更を保存しました。");
  } else if (msg.type === "tournament_admin_history") {
    renderHistory(msg.runs || []);
  } else if (msg.type === "error") {
    message(msg.message || "エラーが発生しました。", true);
  }
}

function renderTournament() {
  el.cancelTournamentBtn.hidden = !["scheduled", "registration", "running"].includes(tournament?.status) || !!tournament?.cancellation_requested_at;
  el.reservationList.replaceChildren(...(tournament?.reservations || []).map(run => {
    const row = document.createElement("article"); row.className = "history-row";
    const text = document.createElement("p");
    text.textContent = `${run.title} / ${run.rule?.summary || ""} / ${run.wins_required || 1}本先取 / ${formatDate(run.registration_opens_at)}受付 / ${formatDate(run.starts_at)}開始予定`;
    const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "予約を中止";
    cancel.addEventListener("click", () => cancelTournament(run)); row.append(text, cancel); return row;
  }));
  if (!el.reservationList.children.length) el.reservationList.textContent = "次の予約はありません。";
  if (!tournament || tournament.status === "unavailable") {
    el.runSummary.textContent = "大会は設定されていません。";
    el.runSummary.className = "run-summary empty";
    el.currentMatch.textContent = "進行中の対戦はありません。";
    el.currentMatch.className = "current-match empty";
    el.matchList.replaceChildren();
    el.standings.replaceChildren();
    return;
  }
  el.runSummary.className = "run-summary";
  el.runSummary.textContent = `${tournament.title} / ${tournament.rule?.summary || tournament.rule_key} / ${statusLabel(tournament.status)} / ${formatDate(tournament.starts_at)}開始 / ${tournament.participant_count}人`;
  el.runSummary.textContent += ` / ${tournament.wins_required || 1}本先取`;
  if (tournament.cancellation_requested_at) el.runSummary.textContent += " / 中止受付済み・進行中の対局終了待ち";
  if (tournament.schedule_delay_seconds > 0) el.runSummary.textContent += " / 前の大会の終了待ちにより時刻を繰り下げ済み";
  if (tournament.pairing_mode === "flexible") el.runSummary.textContent += ` / 随時対戦 / 途中参加は${formatDate(tournament.registration_closes_at)}まで`;
  const activeMatches = tournament.active_matches || (tournament.current_match ? [tournament.current_match] : []);
  el.currentMatch.className = activeMatches.length ? "current-match active" : "current-match empty";
  el.currentMatch.textContent = activeMatches.length
    ? `進行中 ${activeMatches.length}試合: ${activeMatches.map((match) => `${matchLabel(match)} ${match.player1_name} vs ${match.player2_name}（${statusLabel(match.status)}）`).join(" / ")}`
    : "進行中の対戦はありません。";

  el.matchList.replaceChildren(...(tournament.matches || []).map((match) => {
    const row = document.createElement("article");
    row.className = "match-row";
    const heading = document.createElement("header");
    const title = document.createElement("strong");
    title.textContent = `${matchLabel(match)} ${match.player1_name} vs ${match.player2_name}`;
    if (tournament.wins_required > 1) title.textContent += `（${match.game_wins?.[match.player1_id] || 0}–${match.game_wins?.[match.player2_id] || 0}）`;
    const status = document.createElement("span");
    status.textContent = match.winner_name ? `${match.winner_name} 勝利` : statusLabel(match.status);
    heading.append(title, status);
    const actions = document.createElement("div");
    actions.className = "match-actions";
    actions.append(
      actionButton(`${match.player1_name}勝利`, "resolve", match, match.player1_id),
      actionButton(`${match.player2_name}勝利`, "resolve", match, match.player2_id),
      actionButton("スキップ", "skip", match),
    );
    row.append(heading, actions);
    return row;
  }));

  el.standings.replaceChildren(...(tournament.standings || []).map((row) => {
    const item = document.createElement("li");
    const retired = tournament.participants?.find(p => p.participant_id === row.participant_id)?.retired_at;
    item.textContent = `${row.rank}位 ${row.display_name}${retired ? "（棄権）" : ""}: ${row.wins}勝${row.losses}敗`;
    if (tournament.wins_required > 1) item.textContent += ` / ${row.games_won}–${row.games_lost}本（差${row.game_difference > 0 ? "+" : ""}${row.game_difference}）`;
    return item;
  }));
}

function actionButton(label, action, match, winnerId = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.dataset.action = action;
  button.dataset.matchId = match.match_id;
  if (winnerId) button.dataset.winnerId = winnerId;
  return button;
}

function matchLabel(match) {
  return tournament?.pairing_mode === "flexible" ? `対戦 #${match.sequence_no}` : `R${match.round_no} #${match.sequence_no}`;
}

function renderHistory(runs) {
  el.historyList.replaceChildren(...runs.map((run) => {
    const row = document.createElement("article");
    row.className = "history-row";
    const leaders = (run.standings || []).filter((item) => item.rank === 1).map((item) => item.display_name).join("、");
    row.textContent = `${formatDate(run.starts_at)} / ${run.title} / ${run.rule?.summary || run.rule_key} / ${statusLabel(run.status)}${leaders ? ` / 1位 ${leaders}` : ""}`;
    return row;
  }));
}

function renderRuleOptions(rules) {
  if (!Array.isArray(rules) || !rules.length) return;
  const selected = el.ruleKey.value;
  el.ruleKey.replaceChildren(...rules.map((rule) => {
    const option = document.createElement("option");
    option.value = rule.key;
    option.textContent = rule.summary || rule.label || rule.key;
    return option;
  }));
  if ([...el.ruleKey.options].some((option) => option.value === selected)) {
    el.ruleKey.value = selected;
  } else if ([...el.ruleKey.options].some((option) => option.value === "std-11-n-c")) {
    el.ruleKey.value = "std-11-n-c";
  }
}

function message(text, error = false) {
  el.messageLog.textContent = text;
  el.messageLog.classList.toggle("error", error);
}

function statusLabel(status) {
  return ({ scheduled: "開催予定", registration: "受付中", running: "進行中", finished: "終了", cancelled: "中止", pending: "待機", called: "呼出中", playing: "対戦中", completed: "完了", skipped: "スキップ" })[status] || status;
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString("ja-JP") : "未設定";
}

function setDefaultDates() {
  const now = new Date();
  const opens = new Date(now.getTime() + 5 * 60 * 1000);
  const starts = new Date(now.getTime() + 35 * 60 * 1000);
  el.registrationOpensAt.value = localDateTimeValue(opens);
  el.startsAt.value = localDateTimeValue(starts);
  el.registrationClosesAt.value = localDateTimeValue(new Date(starts.getTime() + 60 * 60 * 1000));
}

function localDateTimeValue(date) {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}
