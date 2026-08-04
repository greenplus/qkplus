const params = new URLSearchParams(location.search);
const wsUrl = params.get("ws") || "wss://web-production-c8e68.up.railway.app/ws/plus";
const el = Object.fromEntries([
  "connectionStatus", "scheduleForm", "adminToken", "title", "formatKey", "ruleKey",
  "registrationOpensAt", "startsAt", "maxParticipants", "refreshBtn", "historyBtn",
  "runSummary", "currentMatch", "matchList", "standings", "historyList", "messageLog",
].map((id) => [id, document.getElementById(id)]));
let ws;
let tournament = null;

setDefaultDates();
connect();

el.scheduleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  send({
    type: "tournament_admin_schedule",
    admin_token: el.adminToken.value,
    room_id: "plus_tournament_1",
    title: el.title.value.trim(),
    format_key: el.formatKey.value.trim(),
    rule_key: el.ruleKey.value,
    registration_opens_at: new Date(el.registrationOpensAt.value).toISOString(),
    starts_at: new Date(el.startsAt.value).toISOString(),
    max_participants: Number(el.maxParticipants.value),
  });
});
el.refreshBtn.addEventListener("click", () => send({ type: "get_tournament_status" }));
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
  if (msg.type === "tournament_update" || msg.type === "tournament_admin_result") {
    tournament = msg.tournament;
    renderTournament();
    if (msg.type === "tournament_admin_result") message("変更を保存しました。");
  } else if (msg.type === "tournament_admin_history") {
    renderHistory(msg.runs || []);
  } else if (msg.type === "error") {
    message(msg.message || "エラーが発生しました。", true);
  }
}

function renderTournament() {
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
  el.runSummary.textContent = `${tournament.title} / ${statusLabel(tournament.status)} / ${formatDate(tournament.starts_at)}開始 / ${tournament.participant_count}人`;
  const current = tournament.current_match;
  el.currentMatch.className = current ? "current-match active" : "current-match empty";
  el.currentMatch.textContent = current
    ? `現在: 第${current.round_no}ラウンド ${current.player1_name} vs ${current.player2_name}`
    : "進行中の対戦はありません。";

  el.matchList.replaceChildren(...(tournament.matches || []).map((match) => {
    const row = document.createElement("article");
    row.className = "match-row";
    const heading = document.createElement("header");
    const title = document.createElement("strong");
    title.textContent = `R${match.round_no} #${match.sequence_no} ${match.player1_name} vs ${match.player2_name}`;
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
    item.textContent = `${row.rank}位 ${row.display_name}: ${row.wins}勝${row.losses}敗 / ${row.points}点`;
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

function renderHistory(runs) {
  el.historyList.replaceChildren(...runs.map((run) => {
    const row = document.createElement("article");
    row.className = "history-row";
    const leaders = (run.standings || []).filter((item) => item.rank === 1).map((item) => item.display_name).join("、");
    row.textContent = `${formatDate(run.starts_at)} / ${run.title} / ${statusLabel(run.status)}${leaders ? ` / 1位 ${leaders}` : ""}`;
    return row;
  }));
}

function message(text, error = false) {
  el.messageLog.textContent = text;
  el.messageLog.classList.toggle("error", error);
}

function statusLabel(status) {
  return ({ scheduled: "開催予定", registration: "受付中", running: "進行中", finished: "終了", cancelled: "中止", pending: "待機", playing: "対戦中", completed: "完了", skipped: "スキップ" })[status] || status;
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
}

function localDateTimeValue(date) {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}
