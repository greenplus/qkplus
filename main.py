from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import lru_cache
from typing import Dict, List, Optional, Tuple
from rules import PRESETS, RulePreset, DeckRule, PenaltyRule, PrimeRule, MovePolicy
from registered_primes import (
    parse_registered_composite_text,
    parse_registered_prime_text,
    registered_prime_template_index,
)
from hnp_challenge import build_hnp_tokens, choose_hnp_permutation
from cpu_player import (
    CpuPlayer,
    CpuProfile,
    available_cpu_profile_payloads,
    choose_gold_finish_candidate,
    choose_profile_cpu_action,
    fish_extra_prime_values,
    get_cpu_profile,
    is_cpu_player,
)
from assist_recommendation import (
    RECOMMENDATION_CACHE_VERSION,
    rank_recommended_assist_candidates,
)
from campaign_store import (
    LAUNCH_PERIOD_KEY,
    LEGACY_CAMPAIGN_KEY,
    CampaignSettings,
    CampaignStore,
    utc_now,
)
from composite_practice_stats_store import (
    ACTOR_CPU,
    ACTOR_OWNER,
    CompositePracticeStatsStore,
)
from tournament import TournamentRun, hash_resume_token, issue_resume_token, parse_datetime
from tournament_store import TournamentStore
from recruitment_store import (
    MAX_ACTIVE_RECRUITMENTS,
    RECRUITMENT_RULE_LABELS,
    RecruitmentError,
    RecruitmentNotification,
    RecruitmentStore,
)
import copy
import json
import random
from random import randrange
import secrets
import uuid
import asyncio
import os, httpx
from math import gcd
import time
import traceback

def int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_JOIN_NOTIFY_LIMIT = int_env("DISCORD_JOIN_NOTIFY_LIMIT", 5)
DISCORD_JOIN_NOTIFY_WINDOW_SECONDS = int_env("DISCORD_JOIN_NOTIFY_WINDOW_SECONDS", 3600, minimum=1)
RECRUITMENT_DISCORD_PAIR_LIMIT = int_env("RECRUITMENT_DISCORD_PAIR_LIMIT", 3)
RECRUITMENT_DISCORD_WINDOW_SECONDS = int_env(
    "RECRUITMENT_DISCORD_WINDOW_SECONDS",
    3600,
    minimum=1,
)
PLUS_CLIENT_URL = os.getenv("PLUS_CLIENT_URL", "https://greenplus.github.io/qkplus/")
NEO_CLIENT_URL = os.getenv("NEO_CLIENT_URL", "https://greenplus.github.io/qkneo/")
LEGACY_CLIENT_URL = os.getenv("LEGACY_CLIENT_URL", "https://greenplus.github.io/primeqk_online/")
SERVER_DIR = Path(__file__).resolve().parent
DATA_DIR = SERVER_DIR / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
SAMPLE_MEMORY_JSON = KNOWLEDGE_DIR / "sample_memory.json"
REGISTERED_TOURNAMENT_JSON = KNOWLEDGE_DIR / "registered_prime_daifugo_plus_ge4.json"
GOLD_PRIME_TABLE_JSON = KNOWLEDGE_DIR / "gold_prime_table_memory.json"
COMPOSITE_PRACTICE_COUNTERMEASURES_JSON = (
    KNOWLEDGE_DIR / "composite_practice_countermeasures_v1.json"
)
SILVER_PRIME_TABLE_JSON = KNOWLEDGE_DIR / "silver_prime_table_memory.json"
COMPOSITE_PRACTICE_GE3_TEXT = KNOWLEDGE_DIR / "composite_practice_composites_ge3.txt"
COMPOSITE_PRACTICE_PRIME_JSON = KNOWLEDGE_DIR / "composite_practice_primes_le3_upper.json"
CAMPAIGN_SETTINGS = CampaignSettings.from_env()
CAMPAIGN_STORE = CampaignStore()
TOURNAMENT_STORE = TournamentStore()
RECRUITMENT_STORE = RecruitmentStore(
    notifications_enabled=bool(WEBHOOK_URL),
    notification_pair_limit=RECRUITMENT_DISCORD_PAIR_LIMIT,
    notification_window_seconds=RECRUITMENT_DISCORD_WINDOW_SECONDS,
)
COMPOSITE_PRACTICE_STATS_STORE = CompositePracticeStatsStore()
TOURNAMENT_ADMIN_TOKEN = os.getenv("TOURNAMENT_ADMIN_TOKEN", "").strip()
COMPOSITE_PRACTICE_ACCESS_TOKEN = os.getenv("COMPOSITE_PRACTICE_ACCESS_TOKEN", "").strip()
COMPOSITE_PRACTICE_ROOM_ID = "composite_practice_1"
COMPOSITE_PRACTICE_ACCESS_SCOPE = "composite_practice_owner"
TOURNAMENT_ROOM_ID = "plus_tournament_1"
PLUS_TOURNAMENT_RULE_KEYS = frozenset(
    key
    for key, preset in PRESETS.items()
    if preset.prime_rule != PrimeRule.REGISTERED
    and not preset.assist_enabled
    and not preset.registration_enabled
    and not preset.hnp_challenge_enabled
    and preset.move_policy == MovePolicy.STANDARD
)
TOURNAMENT_RUNS_BY_ROOM: dict[str, TournamentRun] = {}
TOURNAMENT_SESSIONS: dict[str, "Player"] = {}
TOURNAMENT_MATCH_ROOMS: dict[str, "Room"] = {}
TOURNAMENT_MATCH_VIEWERS: dict[str, set["Player"]] = {}
ROOM_RESUME_SESSIONS: dict[str, "Player"] = {}
TOURNAMENT_LOCK = asyncio.Lock()
TOURNAMENT_SCHEDULER_TASK = None
RECRUITMENT_NOTIFICATION_TASK = None


class TournamentSessionConflict(ValueError):
    """A live tournament participant session requires an explicit takeover."""

    def __init__(self, run: TournamentRun, participant_id: str):
        super().__init__("同じ大会参加権が別のタブで使用中です。")
        self.run = run
        self.participant_id = participant_id


@asynccontextmanager
async def lifespan(_app):
    global TOURNAMENT_SCHEDULER_TASK, RECRUITMENT_NOTIFICATION_TASK
    if CAMPAIGN_SETTINGS.enabled:
        await CAMPAIGN_STORE.connect()
    await TOURNAMENT_STORE.connect()
    await RECRUITMENT_STORE.connect()
    await COMPOSITE_PRACTICE_STATS_STORE.connect()
    for run in await TOURNAMENT_STORE.load_active_runs():
        TOURNAMENT_RUNS_BY_ROOM[run.room_id] = run
    TOURNAMENT_SCHEDULER_TASK = asyncio.create_task(tournament_scheduler_loop())
    if WEBHOOK_URL:
        RECRUITMENT_NOTIFICATION_TASK = asyncio.create_task(
            recruitment_notification_loop()
        )
    yield
    if TOURNAMENT_SCHEDULER_TASK is not None:
        TOURNAMENT_SCHEDULER_TASK.cancel()
        try:
            await TOURNAMENT_SCHEDULER_TASK
        except asyncio.CancelledError:
            pass
    if RECRUITMENT_NOTIFICATION_TASK is not None:
        RECRUITMENT_NOTIFICATION_TASK.cancel()
        try:
            await RECRUITMENT_NOTIFICATION_TASK
        except asyncio.CancelledError:
            pass
    await CAMPAIGN_STORE.close()
    await TOURNAMENT_STORE.close()
    await RECRUITMENT_STORE.close()
    await COMPOSITE_PRACTICE_STATS_STORE.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CAMPAIGN_SETTINGS.allowed_origins),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

ASSIST_LIMITS = {
    "ten": 10,
    "fifty": 50,
    "many": 50,
}
ASSIST_SCAN_LIMITS = {
    "ten": 500,
    "fifty": 2000,
    "many": 2000,
}
ASSIST_REALIZATIONS_PER_NUMBER = 4
SPECIAL_ASSIST_EFFECTS = {
    57: "cut",
    1729: "revolution",
}
CHEF_CARD_RANK = 593
CHEF_CARD_SUIT = "🧑‍🍳"
JOKER_ASSIGNABLE_VALUES = {str(value) for value in range(14)}
MAX_PLAYER_NAME_LENGTH = 24
MAX_CHAT_MESSAGE_LENGTH = 120
GLOBAL_CHAT_COOLDOWN_SECONDS = 1.0
GLOBAL_CHAT_TEMPLATES = {
    "recruit": {
        "message": "対戦相手を募集しています！",
        "badge": "対戦募集",
    },
    "beginner_welcome": {
        "message": "初心者の方も歓迎です。一緒に遊びませんか？",
        "badge": "初心者歓迎",
    },
    "spectators_welcome": {
        "message": "観戦・見学も歓迎です！",
        "badge": "観戦歓迎",
    },
}


def invalid_joker_assignments(values: List[str]) -> List[str]:
    return [
        str(value)
        for value in values
        if str(value) != "inf" and str(value) not in JOKER_ASSIGNABLE_VALUES
    ]


def joker_assignment_error_message() -> str:
    return "ジョーカーは0〜13にのみ割り当てできます。"

################################################
# 素数判定
################################################

_SMALL_PRIMES = (2,3,5,7,11,13,17,19,23,29,31,37)

def is_prime(n: int, k: int = 16) -> bool:
    if n < 2:
        return False
    # 小素数チェック（高速化 & 明確化）
    for p in _SMALL_PRIMES:
        if n == p:
            return True
        if n % p == 0:
            return False

    # n-1 = d * 2^s
    m = n - 1
    lsb = m & -m
    s = lsb.bit_length() - 1
    d = m // lsb

    def check(a: int) -> bool:
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            return True
        for _ in range(s - 1):
            x = (x * x) % n
            if x == n - 1:
                return True
        return False  # 合成数確定

    # 2^64 未満は決定的な既知の底集合で完全判定
    if n < (1 << 64):
        for a in (2,3,5,7,11,13,17,19,23,29,31,37):
            if not check(a):
                return False
        return True

    # それ以上（=72桁含む）は確率的に k ラウンド
    for _ in range(k):
        a = randrange(2, n - 1)
        if not check(a):
            return False
    return True

def is_twin_quadruplet_prime(n: int) -> bool:
    """
    四つ子素数判定。
    n が四つ組 {a, a+2, a+6, a+8} のいずれかに属し、
    その4つがすべて素数なら True。
    例外として 5,7,11,13 も True。
    """
    if n in {5, 7, 11, 13}:
        return True

    if not is_prime(n):
        return False

    # n が四つ組のどの位置かで候補の開始点 a を調べる
    candidates = [n, n - 2, n - 6, n - 8]

    for a in candidates:
        if a < 2:
            continue
        quad = [a, a + 2, a + 6, a + 8]
        if n in quad and all(is_prime(x) for x in quad):
            return True

    return False

def find_prime_factor(n: int, time_limit: float = 2.0) -> int:
    """
    Pollard Rho + 最後の保険の試し割りで n の素因数を1つ返す。
    - できるだけ最後まで粘る
    - ただし安全のため time_limit 秒で打ち切る
    - n が素数なら n 自身を返す
    """
    start_time = time.perf_counter()

    if n % 2 == 0:
        return 2
    if n % 3 == 0:
        return 3
    if is_prime(n):
        return n

    def timed_out() -> bool:
        return (time.perf_counter() - start_time) >= time_limit

    m = int(n ** 0.125) + 1
    c = 1

    while not timed_out():
        f = lambda a, c=c: (pow(a, 2, n) + c) % n
        y = 2
        g = q = 1
        r = 1
        ys = 0

        while g == 1 and not timed_out():
            x = y
            k = 0
            q = 1

            while k < r and g == 1 and not timed_out():
                ys = y
                upper = min(m, r - k)
                for _ in range(upper):
                    y = f(y)
                    q = (q * abs(x - y)) % n
                g = gcd(q, n)
                k += upper

            r *= 2

        if timed_out():
            break

        if g == n:
            g = 1
            y = ys
            while g == 1 and not timed_out():
                y = f(y)
                g = gcd(abs(x - y), n)

        if timed_out():
            break

        if 1 < g < n:
            if is_prime(g):
                return g
            return find_prime_factor(g, time_limit=max(0.1, time_limit - (time.perf_counter() - start_time)))

        c += 1

    # ---- 保険: 時間が残っていれば試し割り ----
    d = 5
    while d * d <= n and not timed_out():
        if n % d == 0:
            return d
        if n % (d + 2) == 0:
            return d + 2
        d += 6

    # 見つからなければ n を返す
    return n

def is_semiprime(n: int) -> bool:
    """
    半素数判定。
    素数2個の積なら True（平方も可）。
    """
    if n < 4:
        return False

    if is_prime(n):
        return False

    p = find_prime_factor(n, time_limit=2.0)
    if p <= 1 or p == n:
        return False

    q, r = divmod(n, p)
    if r != 0:
        return False

    return is_prime(p) and is_prime(q)

def is_valid_prime_by_rule(n: int, rule: RulePreset) -> bool:
    if rule.prime_rule is PrimeRule.NORMAL:
        return is_prime(n)
    if rule.prime_rule is PrimeRule.TETRAD:
        return is_twin_quadruplet_prime(n)
    if rule.prime_rule is PrimeRule.SEMIPRIME:
        if n >= 10**24:
            return False
        return is_semiprime(n)
    return is_prime(n)

def is_valid_prime_for_player(n: int, player: "Player", rule: RulePreset) -> bool:
    if rule.prime_rule is PrimeRule.REGISTERED:
        return player.can_use_registered_prime(n)
    return is_valid_prime_by_rule(n, rule)


def normal_play_allowed_for_player(player: "Player", rule: RulePreset, played_cards: List[dict]) -> bool:
    if rule.move_policy is MovePolicy.STANDARD:
        return True
    if rule.move_policy is not MovePolicy.COMPOSITE_ONLY_WITH_SMALL_HAND_FINISH:
        return False
    if not 1 <= len(player.hand) <= rule.normal_finish_max_hand_size:
        return False
    return {
        card.get("card_id") for card in played_cards
    } == {
        card.get("card_id") for card in player.hand
    }

def rule_display_name(prime_rule: PrimeRule) -> str:
    if prime_rule is PrimeRule.TETRAD:
        return "四つ子素数"
    if prime_rule is PrimeRule.SEMIPRIME:
        return "半素数"
    if prime_rule is PrimeRule.REGISTERED:
        return "登録済み素数"
    return "素数"

################################################
# クラス定義
################################################

class Room:
    def __init__(self, room_id: str, rule: RulePreset, category: str = "Classic"):
        self.room_id = room_id
        self.rule: RulePreset = rule
        self.category = category
        self.players = []    # Playerオブジェクトのリスト
        self.state = "waiting"
        self.deck = []
        self.field = []      # 場に出ているカード
        self.reserve = [] # 山札予備軍
        self.last_number = None     # “場に出ている”最後の数値を保持
        self.current_turn_id = None
        self.first_player_id = None
        self.has_drawn = False
        self.reverse_order = False
        self.score_log = []
        self.game_id: Optional[str] = None
        self.game_started_at: Optional[datetime] = None
        self.campaign_player_id: Optional[str] = None
        self.campaign_cpu_key: Optional[str] = None
        self.campaign_period = None
        self.campaign_largest_prime: Optional[int] = None
        self.tournament_run_id: Optional[str] = None
        self.tournament_match_id: Optional[str] = None

    async def broadcast(self, message: dict):
        disconnected = []
        removed_immediately = []
        for p in list(self.players):
            if hasattr(p, "ws") and p.ws is None and not is_cpu_player(p):
                continue
            try:
                await p.send_json(message)
            except Exception as exc:
                print(f"broadcast failed in {self.room_id}: {exc}")
                disconnected.append(p)
        for p in disconnected:
            if getattr(p, "room_resume_token_hash", None):
                await mark_player_disconnected(p)
            else:
                removed_immediately.append(p)
                if p in self.players:
                    self.players.remove(p)
                if getattr(p, "room", None) is self:
                    p.room = None
                    p.status = "watching"
                    p.clear_hand()
        if disconnected:
            if self.state == "playing":
                for p in disconnected:
                    if p.status == "waiting":
                        record_score_play_line(self, p, "切断")
            if removed_immediately:
                await handle_room_after_player_removed(self)
            else:
                await self.update_room_status()

    async def update_room_status(self):
        message = {
            "type": "update_room_status",
            "room_id": public_room_id(self),
            "rule": self.rule.label,
            "category": self.category,
            "allow_composite": self.rule.allow_composite,
            "prime_rule": self.rule.prime_rule.name.lower(),
            "assist_enabled": self.rule.assist_enabled,
            "registration_enabled": self.rule.registration_enabled,
            "hnp_challenge_enabled": self.rule.hnp_challenge_enabled,
            "cpu_profiles": available_cpu_profile_payloads(self.rule),
            "count": len(self.players),
            "player_list": [
                {
                    "id": p.id,
                    "name": p.name,
                    "status": p.status,
                    "is_cpu": is_cpu_player(p),
                    "cpu_key": getattr(p, "cpu_key", None),
                    "connected": not hasattr(p, "ws") or p.ws is not None or is_cpu_player(p),
                    "tournament_participant_id": getattr(p, "tournament_participant_id", None),
                    "registered_prime_count": len(p.registered_primes),
                    "registered_composite_count": len(p.registered_composites),
                }
                for p in self.players
            ],
            "waiting_count": len([p for p in self.players if p.status == "waiting"])
        }
        if is_tournament_managed_room(self):
            run = tournament_run_for_room(self)
            for room_player in list(self.players):
                if room_player.ws is None:
                    continue
                try:
                    await room_player.send_json({
                        **message,
                        "tournament": tournament_public_payload(
                            run,
                            room_player.tournament_participant_id,
                        ),
                    })
                except Exception:
                    await mark_player_disconnected(room_player)
            if self.tournament_match_id:
                await broadcast_tournament_match_state(self)
            return
        await self.broadcast(message)

    async def log_chat(self, message: str, sender="system"):
        payload = {"type": "chat", "sender": sender, "message": message}
        if self.room_id == TOURNAMENT_ROOM_ID:
            run = tournament_run_for_room(self)
            if run is not None:
                await broadcast_tournament_lobby(run, payload)
                return
        if self.tournament_match_id:
            payload.update({
                "scope": "tournament_match",
                "match_id": self.tournament_match_id,
            })
        await self.broadcast(payload)

    # その他、ルームに関連するロジック（プレイヤー追加、削除、ゲーム開始、次のターンなど）をメソッドとして実装
    async def update_game_state(self):
        current_player = next((p for p in self.players if p.id == self.current_turn_id), None)
        current_name = current_player.name if current_player else None
        state_msg = {
            "type": "game_update",
            "room_id": public_room_id(self),
            "state": self.state,
            "category": self.category,
            "current_turn": current_name,
            "current_turn_id": self.current_turn_id,
            "first_player_id": self.first_player_id,
            "revolution": self.reverse_order,
            "field_number": str(self.last_number) if self.last_number is not None else None,
            "allow_composite": self.rule.allow_composite,
            "prime_rule": self.rule.prime_rule.name.lower(),
            "assist_enabled": self.rule.assist_enabled,
            "registration_enabled": self.rule.registration_enabled,
            "hnp_challenge_enabled": self.rule.hnp_challenge_enabled,
            "deck_count": len(self.deck),
            "field": self.field,
            "player_list": [
                {
                    "id": p.id,
                    "name": p.name,
                    "status": p.status,
                    "is_cpu": is_cpu_player(p),
                    "cpu_key": getattr(p, "cpu_key", None),
                    "connected": not hasattr(p, "ws") or p.ws is not None or is_cpu_player(p),
                    "tournament_participant_id": getattr(p, "tournament_participant_id", None),
                    "registered_prime_count": len(p.registered_primes),
                    "registered_composite_count": len(p.registered_composites),
                }
                for p in self.players
            ],
            "hand_counts": [
                {"id": p.id, "name": p.name, "count": len(p.hand)}
                for p in get_active_players(self)
            ]
        }
        if is_tournament_managed_room(self):
            run = tournament_run_for_room(self)
            for room_player in list(self.players):
                if room_player.ws is None:
                    continue
                try:
                    await room_player.send_json({
                        **state_msg,
                        "tournament": tournament_public_payload(
                            run,
                            room_player.tournament_participant_id,
                        ),
                    })
                except Exception:
                    await mark_player_disconnected(room_player)
            if self.tournament_match_id:
                await broadcast_tournament_match_state(self)
            return
        await self.broadcast(state_msg)

    async def try_end_game(self) -> bool:
        """勝者がいれば game_over を投げて True、なければ False を返す"""
        winner = check_win_condition(self)
        if winner is not None:
            winner_player = next(
                (p for p in get_active_players(self) if p.id == self.current_turn_id),
                None,
            )
            campaign_result = await maybe_record_campaign_win(self, winner_player)
            await record_tournament_game_result(self, winner_player)
            self.state = "waiting"
            await self.broadcast({"type": "game_over", "winner": winner, "state": self.state})
            await self.log_chat(f"{winner}が勝利しました")
            await maybe_log_talkative_fish_game_over(self)
            await publish_score_log(self, winner)
            if campaign_result is not None and winner_player is not None:
                await winner_player.send_json(campaign_result)
            return True
        return False


# アプリケーションの初期化時にRoomインスタンスを必要な数だけ作成しておく
NEO_BEGINNER_ROOM_IDS = ("room_16", "room_17", "room_18")
NEO_ADVANCED_ROOM_IDS = ("room_14", "room_19", "room_20")
CLASSIC_ROOM_IDS = ("room_1", "room_2", "room_3", "room_4", "room_5", "room_6")
PLUS_ROOM_IDS = ("room_7", "room_8", "room_9")

ROOM_CONFIG = [
    ("room_1", PRESETS["std-5-1"], "Classic"),
    ("room_2", PRESETS["half-7-1-c"], "Classic"),
    ("room_3", PRESETS["std-7-1"], "Classic"),
    ("room_4", PRESETS["std-11-f-c"], "Classic"),
    ("room_5", PRESETS["std-11-n-c"], "Classic"),
    ("room_6", PRESETS["std-11-n-no-c"], "Classic"),
    ("room_7", PRESETS["std-11-n-c-rev"], "Plus"),
    ("room_8", PRESETS["tetrad-11-n-c"], "Plus"),
    ("room_9", PRESETS["semiprime-11-n-c"], "Plus"),
    (TOURNAMENT_ROOM_ID, PRESETS["std-11-n-c"], "Plus"),
    ("room_13", PRESETS["registered-11-n"], "Neo"),
    ("room_14", PRESETS["registered-11-n-assist"], "Neo"),
    ("room_15", PRESETS["neo-assist-11-n-unlimited"], "Neo"),
    ("room_16", PRESETS["half-7-1-c-assist"], "Neo"),
    ("room_17", PRESETS["half-7-1-c-assist"], "Neo"),
    ("room_18", PRESETS["half-7-1-c-assist"], "Neo"),
    ("room_19", PRESETS["registered-11-n-assist"], "Neo"),
    ("room_20", PRESETS["registered-11-n-assist"], "Neo"),
    (COMPOSITE_PRACTICE_ROOM_ID, PRESETS["composite-practice-11-n"], "CompositePractice"),
    ("event_1", PRESETS["event-chef-11-1-c"], "Events"),
    ("event_2", PRESETS["event-chef-11-1-c"], "Events"),
    ("event_3", PRESETS["event-chef-11-1-c"], "Events"),
]
ROOM_CATEGORY_DESCRIPTIONS = {
    "Events": "イベント「素数大富豪百鬼夜行」不定期開催中。今までの記録はこちら。",
}
ROOM_DESCRIPTIONS = {
    "event_1": "偶数の半分がコックさんに。偶数カードを半減し、減った12枚分だけランク593・スート🧑‍🍳のカードを追加します。Xは593に割り当てできません。ペナルティは1枚です。",
    "event_2": "偶数の半分がコックさんに。偶数カードを半減し、減った12枚分だけランク593・スート🧑‍🍳のカードを追加します。Xは593に割り当てできません。ペナルティは1枚です。",
    "event_3": "偶数の半分がコックさんに。偶数カードを半減し、減った12枚分だけランク593・スート🧑‍🍳のカードを追加します。Xは593に割り当てできません。ペナルティは1枚です。",
    "room_15": (
        "登録した素数・合成数をもとにアシスト候補を表示する部屋です。"
        "登録リストによる使用制限はないため、登録していない素数も通常通り出せます。\n"
        "素数候補欄では、検索対象を手札全体・選択中・未選択から切り替えられます。"
        "候補数、強い順/弱い順/効率順、出せる数/全枚数/枚数指定も変更できます。\n"
        "候補ボタンを押すと、出す予定のカードが自動で並びます。"
        "合成数候補では、式に使う材料札もあわせてセットされます。"
        "ジョーカーを含む候補は X69|X=2 のような数譜方式で表示されます。"
    ),
    COMPOSITE_PRACTICE_ROOM_ID: (
        "合成数出しだけで進める非公開練習部屋です。手札が3枚以下のときに限り、"
        "全手札を使う通常の合法手で上がれます。57と1729は合成数出しでのみ発動します。"
    ),
}
rooms = {rid: Room(rid, rule, category) for rid, rule, category in ROOM_CONFIG}

ROOM_CATEGORIES_BY_CLIENT_SURFACE = {
    "legacy": frozenset({"Classic", "Plus", "Events"}),
    "plus": frozenset({"Classic", "Plus"}),
    "neo": frozenset({"Neo"}),
    "plus_practice": frozenset({"CompositePractice"}),
}

WEBSOCKET_CLIENT_SURFACE_BY_PATH = {
    "/ws": "legacy",
    "/ws/plus": "plus",
    "/ws/neo": "neo",
    "/ws/plus-practice": "plus_practice",
}


def room_is_available_to_client(room: Room, client_surface: Optional[str]) -> bool:
    if client_surface is None:
        return True
    return room.category in ROOM_CATEGORIES_BY_CLIENT_SURFACE.get(client_surface, frozenset())


def rooms_for_client(
    client_surface: Optional[str] = None,
    practice_authorized: bool = False,
) -> dict[str, Room]:
    return {
        room_id: room
        for room_id, room in rooms.items()
        if room_is_available_to_client(room, client_surface)
        and (room_id != COMPOSITE_PRACTICE_ROOM_ID or practice_authorized)
    }


def room_counts_payload(
    client_surface: Optional[str] = None,
    practice_authorized: bool = False,
) -> dict:
    visible_rooms = rooms_for_client(client_surface, practice_authorized=practice_authorized)
    return {
        "type": "room_counts",
        "client_surface": client_surface,
        "counts": {room_id: len(room.players) for room_id, room in visible_rooms.items()},
        "rules": {rid: room.rule.label for rid, room in visible_rooms.items()},
        "room_categories": {rid: room.category for rid, room in visible_rooms.items()},
        "room_category_descriptions": ROOM_CATEGORY_DESCRIPTIONS,
        "allow_composite": {rid: room.rule.allow_composite for rid, room in visible_rooms.items()},
        "prime_rules": {rid: room.rule.prime_rule.name.lower() for rid, room in visible_rooms.items()},
        "assist_enabled": {rid: room.rule.assist_enabled for rid, room in visible_rooms.items()},
        "registration_enabled": {rid: room.rule.registration_enabled for rid, room in visible_rooms.items()},
        "hnp_challenge_enabled": {rid: room.rule.hnp_challenge_enabled for rid, room in visible_rooms.items()},
        "registered_number_limits": {
            rid: room.rule.registered_number_limit
            for rid, room in visible_rooms.items()
        },
        "room_descriptions": {rid: ROOM_DESCRIPTIONS.get(rid, "") for rid in visible_rooms},
        "registered_sample_options": registered_sample_options(
            include_composite_practice=practice_authorized,
        ),
        "cpu_profiles": {rid: available_cpu_profile_payloads(room.rule) for rid, room in visible_rooms.items()},
        "tournaments": {
            rid: tournament_public_payload(run)
            for rid, run in TOURNAMENT_RUNS_BY_ROOM.items()
            if rid in visible_rooms
        },
    }


async def recruitment_payload(
    board_key: str,
    owner_token: object,
    notice: Optional[str] = None,
) -> dict:
    payload = {
        "type": "recruitments",
        "board_key": board_key,
        "items": await RECRUITMENT_STORE.list_active(
            board_key=board_key,
            owner_token=owner_token,
        ),
        "max_count": MAX_ACTIVE_RECRUITMENTS,
        "server_now": utc_now().isoformat(),
    }
    if notice:
        payload["notice"] = notice
    return payload


def tournament_public_payload(
    run: Optional[TournamentRun],
    viewer_participant_id: Optional[str] = None,
) -> dict:
    if run is None:
        return {
            "status": "unavailable",
            "room_id": TOURNAMENT_ROOM_ID,
            "persistent": TOURNAMENT_STORE.persistent,
            "match_ready_seconds": tournament_match_ready_seconds(),
            "disconnect_grace_seconds": waiting_disconnect_grace_seconds(),
            "playing_disconnect_grace_seconds": playing_disconnect_grace_seconds(),
            "waiting_disconnect_grace_seconds": waiting_disconnect_grace_seconds(),
            "available_rules": tournament_rule_catalog(),
        }
    payload = {
        **run.public_payload(viewer_participant_id=viewer_participant_id),
        "rule": tournament_rule_payload(PRESETS[run.rule_key]),
        "available_rules": tournament_rule_catalog(),
        "persistent": TOURNAMENT_STORE.persistent,
        "match_ready_seconds": tournament_match_ready_seconds(),
        "disconnect_grace_seconds": waiting_disconnect_grace_seconds(),
        "playing_disconnect_grace_seconds": playing_disconnect_grace_seconds(),
        "waiting_disconnect_grace_seconds": waiting_disconnect_grace_seconds(),
    }
    payload["active_matches"] = [
        tournament_match_live_summary(run, match.match_id)
        for match in run.current_matches
    ]
    return payload


def tournament_rule_payload(preset: RulePreset) -> dict:
    deck_rules = {
        DeckRule.DEFAULT: ("default", "山札通常", True),
        DeckRule.EVEN_HALVED: ("even_halved", "山札の偶数カード半減", False),
        DeckRule.EVEN_HALVED_WITH_CHEFS: (
            "even_halved_with_chefs",
            "山札の偶数カード半減＋コックさん12枚",
            False,
        ),
    }
    penalty_rules = {
        PenaltyRule.NORMAL: ("normal", "ペナルティ通常", True),
        PenaltyRule.ALWAYS_1: ("always_1", "ペナルティ1枚", False),
        PenaltyRule.FIELD_COUNT: ("field_count", "ペナルティは場の枚数", False),
    }
    prime_rules = {
        PrimeRule.NORMAL: ("normal", "素数", True),
        PrimeRule.TETRAD: ("tetrad", "四つ子素数", False),
        PrimeRule.SEMIPRIME: ("semiprime", "半素数", False),
        PrimeRule.REGISTERED: ("registered", "登録制限", False),
    }
    deck_key, deck_label, deck_default = deck_rules[preset.deck_rule]
    penalty_key, penalty_label, penalty_default = penalty_rules[preset.penalty_rule]
    prime_key, prime_label, prime_default = prime_rules[preset.prime_rule]
    summary_parts = []
    if preset.start_revolution:
        summary_parts.append("初期革命")
    if not prime_default:
        summary_parts.append(prime_label)
    summary_parts.append(f"{preset.hand_size}枚")
    if not deck_default:
        summary_parts.append(deck_label.removeprefix("山札の"))
    if not penalty_default:
        summary_parts.append(penalty_label)
    if not preset.allow_composite:
        summary_parts.append("合成数なし")
    return {
        "key": preset.key,
        "label": preset.label,
        "summary": " / ".join(summary_parts),
        "hand_size": preset.hand_size,
        "deck_rule": {"key": deck_key, "label": deck_label, "default": deck_default},
        "penalty_rule": {
            "key": penalty_key,
            "label": penalty_label,
            "default": penalty_default,
        },
        "prime_rule": {"key": prime_key, "label": prime_label, "default": prime_default},
        "allow_composite": preset.allow_composite,
        "start_revolution": preset.start_revolution,
    }


def tournament_rule_catalog() -> list[dict]:
    return [
        tournament_rule_payload(preset)
        for key, preset in PRESETS.items()
        if key in PLUS_TOURNAMENT_RULE_KEYS
    ]


def tournament_match_ready_seconds() -> int:
    return int_env("TOURNAMENT_MATCH_READY_SECONDS", 60, minimum=10)


def tournament_disconnect_grace_seconds() -> int:
    return waiting_disconnect_grace_seconds()


def playing_disconnect_grace_seconds() -> int:
    return int_env("PLAYING_DISCONNECT_GRACE_SECONDS", 60, minimum=10)


def waiting_disconnect_grace_seconds() -> int:
    return int_env("WAITING_DISCONNECT_GRACE_SECONDS", 180, minimum=10)


def is_tournament_managed_room(room: Optional["Room"]) -> bool:
    return bool(room and (room.room_id == TOURNAMENT_ROOM_ID or room.tournament_run_id))


def public_room_id(room: "Room") -> str:
    return TOURNAMENT_ROOM_ID if is_tournament_managed_room(room) else room.room_id


def tournament_run_for_room(room: Optional["Room"]) -> Optional[TournamentRun]:
    if room is None:
        return None
    if room.room_id == TOURNAMENT_ROOM_ID:
        return TOURNAMENT_RUNS_BY_ROOM.get(TOURNAMENT_ROOM_ID)
    if not room.tournament_run_id:
        return None
    return next(
        (run for run in TOURNAMENT_RUNS_BY_ROOM.values() if run.run_id == room.tournament_run_id),
        None,
    )


def tournament_for_player(player: "Player") -> Optional[TournamentRun]:
    run = tournament_run_for_room(player.room)
    if run is not None:
        return run
    participant_id = player.tournament_participant_id
    if participant_id:
        return next(
            (item for item in TOURNAMENT_RUNS_BY_ROOM.values() if participant_id in item.participants),
            None,
        )
    return None


def tournament_match_live_summary(run: TournamentRun, match_id: str) -> dict:
    match = next((item for item in run.matches if item.match_id == match_id), None)
    if match is None:
        return {"match_id": match_id, "status": "unavailable"}
    room = TOURNAMENT_MATCH_ROOMS.get(match_id)
    participants = run.participants
    round_matches = [item for item in run.matches if item.round_no == match.round_no]
    table_no = round_matches.index(match) + 1
    current_player = (
        next((item for item in room.players if item.id == room.current_turn_id), None)
        if room is not None
        else None
    )
    hand_counts = []
    if room is not None:
        hand_counts = [
            {
                "participant_id": getattr(room_player, "tournament_participant_id", None),
                "display_name": room_player.name,
                "count": len(room_player.hand),
            }
            for room_player in get_active_players(room)
        ]
    return {
        "match_id": match.match_id,
        "round_no": match.round_no,
        "table_no": table_no,
        "player1_id": match.player1_id,
        "player2_id": match.player2_id,
        "player1_name": participants[match.player1_id].display_name,
        "player2_name": participants[match.player2_id].display_name,
        "winner_id": match.winner_id,
        "winner_name": participants[match.winner_id].display_name if match.winner_id else None,
        "status": match.status,
        "resolution": match.resolution,
        "ready_player_ids": list(match.ready_player_ids),
        "ready_deadline_at": match.ready_deadline_at.isoformat() if match.ready_deadline_at else None,
        "started_at": match.started_at.isoformat() if match.started_at else None,
        "completed_at": match.completed_at.isoformat() if match.completed_at else None,
        "room_state": room.state if room is not None else match.status,
        "current_turn": current_player.name if current_player is not None else None,
        "current_turn_participant_id": (
            getattr(current_player, "tournament_participant_id", None)
            if current_player is not None
            else None
        ),
        "revolution": bool(room.reverse_order) if room is not None else False,
        "field_number": str(room.last_number) if room is not None and room.last_number is not None else None,
        "field": [
            {
                "suit": card.get("suit"),
                "rank": card.get("rank"),
                "is_joker": bool(card.get("is_joker") or card.get("suit") == "X"),
            }
            for card in (room.field if room is not None else [])
        ],
        "deck_count": len(room.deck) if room is not None else None,
        "hand_counts": hand_counts,
        "connected_player_ids": [
            participant_id
            for participant_id in (match.player1_id, match.player2_id)
            if (
                TOURNAMENT_SESSIONS.get(participant_id) is not None
                and TOURNAMENT_SESSIONS[participant_id].ws is not None
            )
        ],
        "viewer_count": len(TOURNAMENT_MATCH_VIEWERS.get(match_id, set())),
    }


def tournament_lobby_recipients(run: TournamentRun) -> list["Player"]:
    recipients = []
    seen = set()
    lobby = rooms.get(run.room_id)
    candidates = list(lobby.players if lobby is not None else []) + [
        session
        for participant_id, session in TOURNAMENT_SESSIONS.items()
        if participant_id in run.participants
    ]
    for recipient in candidates:
        identity = id(recipient)
        if identity in seen or recipient.ws is None:
            continue
        seen.add(identity)
        recipients.append(recipient)
    return recipients


async def broadcast_tournament_lobby(run: TournamentRun, payload: dict) -> None:
    message = {**payload, "scope": "tournament_lobby", "run_id": run.run_id}
    for recipient in tournament_lobby_recipients(run):
        try:
            await recipient.send_json(message)
        except Exception:
            await mark_player_disconnected(recipient)


def clear_tournament_match_view(player: "Player") -> None:
    match_id = getattr(player, "tournament_view_match_id", None)
    if not match_id:
        return
    viewers = TOURNAMENT_MATCH_VIEWERS.get(match_id)
    if viewers is not None:
        viewers.discard(player)
        if not viewers:
            TOURNAMENT_MATCH_VIEWERS.pop(match_id, None)
    player.tournament_view_match_id = None


async def watch_tournament_match(player: "Player", match_id: str) -> None:
    run = tournament_for_player(player) or TOURNAMENT_RUNS_BY_ROOM.get(TOURNAMENT_ROOM_ID)
    if run is None or not is_tournament_managed_room(player.room):
        raise ValueError("大会ロビーへ入室してから観戦してください。")
    match = next((item for item in run.current_matches if item.match_id == match_id), None)
    if match is None or match.status not in {"called", "playing"}:
        raise ValueError("この対戦は現在観戦できません。")
    own_match = (
        run.current_match_for_participant(player.tournament_participant_id)
        if player.tournament_participant_id
        else None
    )
    if own_match is not None and own_match.status == "playing" and own_match.match_id != match_id:
        raise ValueError("自分の対戦中はほかの対戦を観戦できません。")
    clear_tournament_match_view(player)
    player.tournament_view_match_id = match_id
    TOURNAMENT_MATCH_VIEWERS.setdefault(match_id, set()).add(player)
    await player.send_json({
        "type": "tournament_match_view_update",
        "match": tournament_match_live_summary(run, match_id),
    })


async def broadcast_tournament_match_state(room: "Room") -> None:
    run = tournament_run_for_room(room)
    match_id = room.tournament_match_id
    if run is None or not match_id:
        return
    summary = tournament_match_live_summary(run, match_id)
    await broadcast_tournament_lobby(run, {
        "type": "tournament_match_summary",
        "match": summary,
    })
    for viewer in list(TOURNAMENT_MATCH_VIEWERS.get(match_id, set())):
        if viewer.ws is None:
            clear_tournament_match_view(viewer)
            continue
        try:
            await viewer.send_json({
                "type": "tournament_match_view_update",
                "match": summary,
            })
        except Exception:
            clear_tournament_match_view(viewer)


async def finish_tournament_match_view(
    run: TournamentRun,
    room: Optional["Room"],
    match_id: str,
    winner_name: Optional[str],
) -> None:
    summary = tournament_match_live_summary(run, match_id)
    summary["score_lines"] = [
        record.get("line", "")
        for record in (room.score_log if room is not None else [])
        if record.get("line")
    ]
    viewers = list(TOURNAMENT_MATCH_VIEWERS.pop(match_id, set()))
    for viewer in viewers:
        viewer.tournament_view_match_id = None
        if viewer.ws is None:
            continue
        try:
            await viewer.send_json({
                "type": "tournament_match_view_ended",
                "winner": winner_name,
                "match": summary,
            })
        except Exception:
            pass
    await broadcast_tournament_lobby(run, {
        "type": "tournament_match_summary",
        "match": summary,
    })


def room_disconnect_grace_seconds(room: "Room", player: "Player") -> int:
    if room.state == "playing" and player.status == "waiting":
        return playing_disconnect_grace_seconds()
    return waiting_disconnect_grace_seconds()


async def mark_player_disconnected(player: "Player", *, now: Optional[datetime] = None) -> None:
    if player.room is None:
        return
    first_notice = getattr(player, "disconnected_at", None) is None
    room = player.room
    player.ws = None
    if first_notice:
        player.disconnected_at = now or utc_now()
        grace_seconds = room_disconnect_grace_seconds(room, player)
        await room.log_chat(
            f"{player.name}の通信が一時的に切れました。{grace_seconds}秒間、復帰を待ちます。"
        )


def room_resume_session(token: object, requested_room_id: str) -> Optional["Player"]:
    if not isinstance(token, str) or not token:
        return None
    session = ROOM_RESUME_SESSIONS.get(hash_resume_token(token))
    if (
        session is None
        or session.room_session_room_id != requested_room_id
        or session.ws is not None
    ):
        return None
    return session


async def bind_room_resume_session(
    incoming: "Player",
    session: "Player",
) -> "Player":
    incoming.ws, session.ws = None, incoming.ws
    session.disconnected_at = None
    session.client_surface = incoming.client_surface
    session.composite_practice_authorized = incoming.composite_practice_authorized
    await session.send_json({"type": "your_id", "id": session.id, "name": session.name})
    await session.send_json({
        "type": "room_session",
        "room_id": session.room_session_room_id,
        "status": "resumed",
        "display_name": session.name,
    })
    if session.room is not None:
        await session.room.log_chat(f"{session.name}が通信切断から復帰しました。")
    return session


async def issue_room_resume_session(player: "Player", room_id: str) -> None:
    if player.room_resume_token_hash:
        ROOM_RESUME_SESSIONS.pop(player.room_resume_token_hash, None)
    token = issue_resume_token()
    digest = hash_resume_token(token)
    player.room_resume_token_hash = digest
    player.room_session_room_id = room_id
    ROOM_RESUME_SESSIONS[digest] = player
    await player.send_json({
        "type": "room_session",
        "room_id": room_id,
        "status": "issued",
        "resume_token": token,
        "playing_disconnect_grace_seconds": playing_disconnect_grace_seconds(),
        "waiting_disconnect_grace_seconds": waiting_disconnect_grace_seconds(),
    })


def forget_room_resume_session(player: "Player") -> None:
    if player.room_resume_token_hash:
        ROOM_RESUME_SESSIONS.pop(player.room_resume_token_hash, None)
    player.room_resume_token_hash = None
    player.room_session_room_id = None


def room_initialization_payload(room: "Room", player: "Player") -> dict:
    return {
        "type": "room_state_initialization",
        "room_id": public_room_id(room),
        "room_state": room.state,
        "category": room.category,
        "allow_composite": room.rule.allow_composite,
        "prime_rule": room.rule.prime_rule.name.lower(),
        "assist_enabled": room.rule.assist_enabled,
        "registration_enabled": room.rule.registration_enabled,
        "hnp_challenge_enabled": room.rule.hnp_challenge_enabled,
        "description": ROOM_DESCRIPTIONS.get(public_room_id(room), ""),
        "cpu_profiles": available_cpu_profile_payloads(room.rule),
        "playing_disconnect_grace_seconds": playing_disconnect_grace_seconds(),
        "waiting_disconnect_grace_seconds": waiting_disconnect_grace_seconds(),
        "tournament": tournament_public_payload(
            tournament_run_for_room(room),
            player.tournament_participant_id,
        ) if is_tournament_managed_room(room) else None,
    }


def tournament_admin_authorized(value) -> bool:
    return bool(
        TOURNAMENT_ADMIN_TOKEN
        and isinstance(value, str)
        and secrets.compare_digest(value, TOURNAMENT_ADMIN_TOKEN)
    )


def composite_practice_authorized(value) -> bool:
    return bool(
        COMPOSITE_PRACTICE_ACCESS_TOKEN
        and isinstance(value, str)
        and secrets.compare_digest(value, COMPOSITE_PRACTICE_ACCESS_TOKEN)
    )


def player_can_access_room(player: "Player", room: Room) -> bool:
    if room.room_id != COMPOSITE_PRACTICE_ROOM_ID:
        return True
    return bool(getattr(player, "composite_practice_authorized", False))


def room_discord_join_notifications_enabled(room: Room) -> bool:
    return room.room_id != COMPOSITE_PRACTICE_ROOM_ID


async def composite_practice_stats_payload() -> dict:
    snapshot = await COMPOSITE_PRACTICE_STATS_STORE.snapshot()
    return {"type": "composite_practice_stats", **snapshot}


async def record_composite_practice_play(player: "Player", room: Room, number: int) -> None:
    if room.room_id != COMPOSITE_PRACTICE_ROOM_ID:
        return
    actor_kind = ACTOR_CPU if is_cpu_player(player) else ACTOR_OWNER
    try:
        await COMPOSITE_PRACTICE_STATS_STORE.record_play(
            actor_kind=actor_kind,
            composite_number=number,
        )
    except Exception as exc:
        # 分析用記録の障害で対局を止めない。
        print(f"composite practice stats record failed: {exc}")


async def send_tournament_status(player: "Player") -> None:
    run = tournament_for_player(player) or TOURNAMENT_RUNS_BY_ROOM.get(TOURNAMENT_ROOM_ID)
    await player.send_json({
        "type": "tournament_update",
        "tournament": tournament_public_payload(run, player.tournament_participant_id),
    })


async def schedule_tournament(data: dict, actor: str) -> TournamentRun:
    rule_key = str(data.get("rule_key", "std-11-n-c"))
    if rule_key not in PLUS_TOURNAMENT_RULE_KEYS:
        raise ValueError("plus大会で使用できないルールです。")
    room_id = str(data.get("room_id", TOURNAMENT_ROOM_ID))
    if room_id != TOURNAMENT_ROOM_ID:
        raise ValueError("現在指定できる大会ルームはplus大会ルームのみです。")
    existing = TOURNAMENT_RUNS_BY_ROOM.get(room_id)
    if existing and existing.status in {"scheduled", "registration", "running"}:
        raise ValueError("この部屋には進行中または予定済みの大会があります。")

    run = TournamentRun.create(
        format_key=str(data.get("format_key", "plus-round-robin-v1")),
        title=str(data.get("title", "素数大富豪＋ 定期大会")),
        room_id=room_id,
        rule_key=rule_key,
        registration_opens_at=parse_datetime(data.get("registration_opens_at")),
        starts_at=parse_datetime(data.get("starts_at")),
        max_participants=int(data.get("max_participants", 10)),
    )
    TOURNAMENT_RUNS_BY_ROOM[room_id] = run
    room = rooms[room_id]
    room.rule = PRESETS[rule_key]
    room.tournament_run_id = run.run_id
    await TOURNAMENT_STORE.save_run(run)
    await TOURNAMENT_STORE.audit(run.run_id, actor=actor, action="schedule", details={
        "title": run.title,
        "format_key": run.format_key,
        "rule_key": run.rule_key,
        "registration_opens_at": run.registration_opens_at.isoformat(),
        "starts_at": run.starts_at.isoformat(),
    })
    await room.log_chat(f"大会「{run.title}」の日程が設定されました。")
    await notify_tournament_discord(run, "scheduled")
    if run.status == "registration":
        await notify_tournament_discord(run, "registration")
    await room.update_room_status()
    return run


async def bind_tournament_participant(
    player: "Player",
    run: TournamentRun,
    participant_id: str,
) -> "Player":
    participant = run.participants[participant_id]
    existing = TOURNAMENT_SESSIONS.get(participant_id)
    active_match = run.current_match_for_participant(participant_id)
    target_room = (
        TOURNAMENT_MATCH_ROOMS.get(active_match.match_id)
        if active_match is not None and active_match.status == "playing"
        else rooms[run.room_id]
    )
    target_room = target_room or rooms[run.room_id]
    if existing is not None and existing is not player:
        old_ws = existing.ws
        if old_ws is not None and old_ws is not player.ws:
            try:
                await old_ws.send_json({
                    "type": "session_replaced",
                    "room_id": public_room_id(existing.room) if existing.room else run.room_id,
                    "message": "大会参加権が別のタブへ引き継がれました。",
                })
            except Exception:
                pass
            try:
                await old_ws.close(code=4001)
            except Exception:
                pass
        incoming_token_hash = player.room_resume_token_hash
        incoming_session_room_id = player.room_session_room_id
        if existing.room_resume_token_hash:
            ROOM_RESUME_SESSIONS.pop(existing.room_resume_token_hash, None)
        if incoming_token_hash:
            ROOM_RESUME_SESSIONS[incoming_token_hash] = existing
        existing.room_resume_token_hash = incoming_token_hash
        existing.room_session_room_id = incoming_session_room_id
        player.room_resume_token_hash = None
        player.room_session_room_id = None
        incoming_room = player.room
        if incoming_room is not None and player in incoming_room.players:
            incoming_room.players.remove(player)
        old_room = existing.room
        if old_room is not None and old_room is not target_room and existing in old_room.players:
            old_room.players.remove(existing)
        if existing not in target_room.players:
            target_room.players.append(existing)
        existing.room = target_room
        existing.ws = player.ws
        existing.disconnected_at = None
        existing.name = participant.display_name
        player.ws = None
        player.room = None
        bound = existing
    else:
        old_room = player.room
        if old_room is not None and old_room is not target_room and player in old_room.players:
            old_room.players.remove(player)
        if player not in target_room.players:
            target_room.players.append(player)
        player.room = target_room
        player.tournament_participant_id = participant_id
        player.name = participant.display_name
        player.disconnected_at = None
        bound = player
    bound.tournament_participant_id = participant_id
    TOURNAMENT_SESSIONS[participant_id] = bound
    await bound.send_json({"type": "your_id", "id": bound.id, "name": bound.name})
    return bound


async def register_or_resume_tournament_player(
    player: "Player",
    data: dict,
) -> "Player":
    run = tournament_for_player(player)
    if run is None:
        raise ValueError("現在この部屋で受付中の大会はありません。")
    resume_token = data.get("resume_token")
    participant = (
        run.participant_for_token(resume_token)
        if isinstance(resume_token, str) and resume_token
        else None
    )
    issued_token = None
    resumed = participant is not None
    if participant is not None:
        existing = TOURNAMENT_SESSIONS.get(participant.participant_id)
        if (
            existing is not None
            and existing is not player
            and existing.ws is not None
            and data.get("takeover") is not True
        ):
            raise TournamentSessionConflict(run, participant.participant_id)
    if participant is None:
        participant, issued_token = run.register(player.name)
        await TOURNAMENT_STORE.audit(
            run.run_id,
            actor=participant.display_name,
            action="register",
            details={"participant_id": participant.participant_id},
        )
    bound = await bind_tournament_participant(player, run, participant.participant_id)
    await TOURNAMENT_STORE.save_run(run)
    payload = {
        "type": "tournament_registration",
        "status": "resumed" if resumed else "registered",
        "participant_id": participant.participant_id,
        "display_name": participant.display_name,
        "tournament": tournament_public_payload(run, participant.participant_id),
    }
    if issued_token is not None:
        payload["resume_token"] = issued_token
    await bound.send_json(payload)
    current_match = run.current_match_for_participant(participant.participant_id)
    if (
        current_match is not None
        and current_match.status == "called"
        and participant.participant_id in {current_match.player1_id, current_match.player2_id}
    ):
        await notify_tournament_match_call(run, current_match)
    if bound.room is not None:
        await bound.room.log_chat(
            f"{participant.display_name}が大会へ復帰しました。"
            if resumed
            else f"{participant.display_name}が大会に参加登録しました。"
        )
        await bound.room.update_room_status()
        await bound.send_hand_update()
        if bound.room.state == "playing":
            await bound.room.update_game_state()
    return bound


async def withdraw_tournament_player(player: "Player") -> None:
    run = tournament_for_player(player)
    participant_id = player.tournament_participant_id
    if run is None or participant_id is None:
        raise ValueError("大会参加登録が見つかりません。")
    participant = run.participants[participant_id]
    run.withdraw(participant_id)
    player.tournament_participant_id = None
    TOURNAMENT_SESSIONS.pop(participant_id, None)
    await TOURNAMENT_STORE.save_run(run)
    await TOURNAMENT_STORE.audit(
        run.run_id,
        actor=participant.display_name,
        action="withdraw",
        details={"participant_id": participant_id},
    )
    await player.room.log_chat(f"{participant.display_name}が大会参加を取り消しました。")
    await player.room.update_room_status()
    await player.send_json({
        "type": "tournament_withdrawn",
        "run_id": run.run_id,
        "tournament": tournament_public_payload(run),
    })


def tournament_session_online(participant_id: str, room: Optional[Room] = None) -> bool:
    session = TOURNAMENT_SESSIONS.get(participant_id)
    return bool(
        session
        and session.ws is not None
        and is_tournament_managed_room(session.room)
        and (room is None or session.room is room)
    )


async def announce_tournament_globally(message: str) -> None:
    await broadcast_global_chat({
        "type": "global_chat",
        "sender": "大会システム",
        "message": message,
        "template_key": None,
        "template_badge": "大会",
        **global_chat_room_meta(rooms[TOURNAMENT_ROOM_ID]),
    }, client_surface="plus")


def tournament_discord_notification_content(
    run: TournamentRun,
    event: str,
    *,
    winner_text: Optional[str] = None,
    reason: Optional[str] = None,
) -> str:
    event_headings = {
        "scheduled": "📅 **大会開催決定 / 素数大富豪＋**",
        "registration": "📣 **大会参加登録開始 / 素数大富豪＋**",
        "running": "🏁 **大会開始 / 素数大富豪＋**",
        "cancelled": "🚫 **大会中止 / 素数大富豪＋**",
        "finished": "🏆 **大会終了・結果発表 / 素数大富豪＋**",
    }
    heading = event_headings.get(event, "🎴 **大会情報 / 素数大富豪＋**")
    title = discord_safe_text(run.title)
    rule_summary = discord_safe_text(tournament_rule_payload(PRESETS[run.rule_key])["summary"])
    registration_timestamp = int(run.registration_opens_at.timestamp())
    start_timestamp = int(run.starts_at.timestamp())
    lines = [
        heading,
        f"**{title}**",
        f"🎴 ルール: **{rule_summary}**",
    ]
    if event == "scheduled":
        lines.extend([
            f"📝 参加登録: <t:{registration_timestamp}:F>（<t:{registration_timestamp}:R>）",
            f"🕐 大会開始: <t:{start_timestamp}:F>（<t:{start_timestamp}:R>）",
            f"👥 定員: {run.max_participants}人",
        ])
    elif event == "registration":
        lines.extend([
            f"🕐 大会開始: <t:{start_timestamp}:F>（<t:{start_timestamp}:R>）",
            f"👥 定員: {run.max_participants}人",
        ])
    elif event == "running":
        lines.append(f"👥 参加者: {len(run.active_participants)}人")
    elif event == "cancelled":
        lines.append(discord_safe_text(reason or "大会は中止になりました。"))
    elif event == "finished":
        lines.append(f"🥇 優勝: **{discord_safe_text(winner_text or '該当者なし')}**")
        standings = run.standings()[:10]
        if standings:
            result_lines = [
                f"{row['rank']}位 {discord_safe_text(row['display_name'])}: "
                f"{row['wins']}勝{row['losses']}敗 / {row['points']}点"
                for row in standings
            ]
            lines.append("📊 最終順位\n" + "\n".join(result_lines))
    lines.append(f"▶ {PLUS_CLIENT_URL}")
    return "\n".join(lines)


async def notify_tournament_discord(
    run: TournamentRun,
    event: str,
    *,
    winner_text: Optional[str] = None,
    reason: Optional[str] = None,
) -> bool:
    return await notify_discord(tournament_discord_notification_content(
        run,
        event,
        winner_text=winner_text,
        reason=reason,
    ))


def tournament_match_both_ready(match) -> bool:
    return {match.player1_id, match.player2_id}.issubset(set(match.ready_player_ids))


async def notify_tournament_match_call(run: TournamentRun, match) -> None:
    for participant_id in (match.player1_id, match.player2_id):
        session = TOURNAMENT_SESSIONS.get(participant_id)
        if not tournament_session_online(participant_id):
            continue
        await session.send_json({
            "type": "tournament_match_call",
            "message": "あなたの対戦です。「この対戦に参加」を押してください。",
            "match_id": match.match_id,
            "ready_deadline_at": match.ready_deadline_at.isoformat() if match.ready_deadline_at else None,
            "tournament": tournament_public_payload(run, participant_id),
        })


async def mark_tournament_match_ready(player: "Player", match_id: str) -> None:
    run = tournament_for_player(player)
    participant_id = player.tournament_participant_id
    if run is None or participant_id is None:
        raise ValueError("大会参加情報が見つかりません。")
    match = run.current_match_for_participant(participant_id)
    if match is None or match.match_id != match_id:
        raise ValueError("この対戦の参加確認は終了しています。")
    run.mark_match_ready(participant_id, match_id)
    await TOURNAMENT_STORE.save_run(run)
    await TOURNAMENT_STORE.audit(
        run.run_id,
        actor=run.participants[participant_id].display_name,
        action="match_ready",
        details={"match_id": match.match_id, "participant_id": participant_id},
    )
    await player.send_json({
        "type": "tournament_match_ready_ack",
        "match_id": match.match_id,
        "tournament": tournament_public_payload(run, participant_id),
    })
    await rooms[run.room_id].update_room_status()
    if tournament_match_both_ready(match):
        await prepare_tournament_match(run, match)


async def prepare_tournament_match(
    run: TournamentRun,
    match,
    *,
    now: Optional[datetime] = None,
) -> None:
    if match is None or match.status not in {"called", "playing"}:
        return
    if not (
        tournament_session_online(match.player1_id)
        and tournament_session_online(match.player2_id)
    ):
        return
    current = now or utc_now()
    if match.status == "called":
        deadline_elapsed = bool(
            match.ready_deadline_at is not None
            and current >= match.ready_deadline_at
        )
        if not tournament_match_both_ready(match) and not deadline_elapsed:
            return
    run.begin_match(match.match_id, now=current)
    await TOURNAMENT_STORE.save_run(run)
    player1 = TOURNAMENT_SESSIONS[match.player1_id]
    player2 = TOURNAMENT_SESSIONS[match.player2_id]
    room = TOURNAMENT_MATCH_ROOMS.get(match.match_id)
    if room is None:
        room = Room(
            f"{TOURNAMENT_ROOM_ID}/{run.run_id}/{match.match_id}",
            PRESETS[run.rule_key],
            "Plus",
        )
        room.tournament_run_id = run.run_id
        room.tournament_match_id = match.match_id
        TOURNAMENT_MATCH_ROOMS[match.match_id] = room
    for match_player in (player1, player2):
        clear_tournament_match_view(match_player)
        old_room = match_player.room
        if old_room is not None and old_room is not room and match_player in old_room.players:
            old_room.players.remove(match_player)
        if match_player not in room.players:
            room.players.append(match_player)
        match_player.room = room
        match_player.status = "waiting"
        match_player.disconnected_at = None
        match_player.clear_hand()
        await match_player.send_json({
            "type": "tournament_match_started",
            "match_id": match.match_id,
            "round_no": match.round_no,
            "tournament": tournament_public_payload(run, match_player.tournament_participant_id),
        })
    await rooms[run.room_id].log_chat(
        f"第{match.round_no}ラウンド: {player1.name} vs {player2.name} を開始します。"
    )
    await rooms[run.room_id].update_room_status()
    await room.update_room_status()
    await start_game(room)


async def start_or_prepare_next_tournament_match(run: TournamentRun) -> None:
    lobby = rooms[run.room_id]
    existing_matches = run.current_matches
    matches = existing_matches or run.start_next_round(
        ready_wait_seconds=tournament_match_ready_seconds(),
    )
    if not matches:
        await TOURNAMENT_STORE.save_run(run)
        return
    await TOURNAMENT_STORE.save_run(run)
    if not existing_matches:
        await lobby.log_chat(
            f"第{matches[0].round_no}ラウンドの{len(matches)}試合を呼び出しました。"
            f"対戦者は{tournament_match_ready_seconds()}秒以内に参加確認を押してください。"
        )
        for match in matches:
            await notify_tournament_match_call(run, match)
    await lobby.update_room_status()


def tournament_disconnect_resolution_message(
    run: TournamentRun,
    match,
    winner_id: Optional[str],
    resolution: str,
) -> Optional[str]:
    if resolution == "forfeit" and winner_id is not None:
        loser_id = match.player2_id if winner_id == match.player1_id else match.player1_id
        loser_name = run.participants[loser_id].display_name
        return f"{loser_name}は復帰猶予を超えたため、切断による不戦敗になりました。"
    if resolution == "auto_skip":
        return "両対戦者が復帰猶予を超えたため、切断扱いで対戦をスキップしました。"
    return None


async def resolve_tournament_match(
    run: TournamentRun,
    match_id: str,
    winner_id: Optional[str],
    *,
    resolution: str,
    actor: str,
    advance: bool = True,
) -> None:
    was_finished = run.status == "finished"
    match = run.resolve_match(match_id, winner_id, resolution=resolution)
    finished_now = not was_finished and run.status == "finished"
    await TOURNAMENT_STORE.save_run(run)
    await TOURNAMENT_STORE.audit(run.run_id, actor=actor, action="resolve_match", details={
        "match_id": match_id,
        "winner_id": winner_id,
        "resolution": resolution,
    })
    lobby = rooms[run.room_id]
    await close_tournament_match_room(run, match_id, winner_id=winner_id)
    disconnect_message = tournament_disconnect_resolution_message(
        run,
        match,
        winner_id,
        resolution,
    )
    if disconnect_message:
        await lobby.log_chat(disconnect_message)
    if winner_id is None:
        await lobby.log_chat("対戦はスキップされました。")
    else:
        winner_name = run.participants[winner_id].display_name
        await lobby.log_chat(f"大会結果を反映しました: {winner_name}の勝利")
    await lobby.update_room_status()
    if finished_now:
        await announce_tournament_finished(run)
    elif advance and run.status != "finished":
        await start_or_prepare_next_tournament_match(run)


async def close_tournament_match_room(
    run: TournamentRun,
    match_id: str,
    *,
    winner_id: Optional[str],
    broadcast_result: bool = True,
) -> None:
    room = TOURNAMENT_MATCH_ROOMS.get(match_id)
    winner_name = run.participants[winner_id].display_name if winner_id else None
    await finish_tournament_match_view(run, room, match_id, winner_name)
    if room is None:
        return
    TOURNAMENT_MATCH_ROOMS.pop(match_id, None)
    room.state = "waiting"
    room.current_turn_id = None
    if broadcast_result:
        await room.broadcast({"type": "game_over", "winner": winner_name, "state": room.state})
    lobby = rooms[run.room_id]
    for room_player in list(room.players):
        room.players.remove(room_player)
        room_player.clear_hand()
        room_player.status = "watching"
        room_player.room = lobby
        if room_player not in lobby.players:
            lobby.players.append(room_player)
        if room_player.ws is not None:
            await room_player.send_hand_update()
            await room_player.send_json({
                "type": "tournament_return_to_lobby",
                "tournament": tournament_public_payload(
                    run,
                    room_player.tournament_participant_id,
                ),
            })
    await lobby.update_room_status()


async def announce_tournament_finished(run: TournamentRun) -> None:
    leaders = [row for row in run.standings() if row["rank"] == 1]
    winner_text = "、".join(row["display_name"] for row in leaders) or "該当者なし"
    room = rooms[run.room_id]
    await room.log_chat(f"全試合終了。優勝: {winner_text}")
    await announce_tournament_globally(f"大会「{run.title}」が終了しました。優勝: {winner_text}")
    await notify_tournament_discord(run, "finished", winner_text=winner_text)
    await room.update_room_status()


async def record_tournament_game_result(room: Room, winner_player: Optional["Player"]) -> None:
    run = tournament_run_for_room(room)
    match = next(
        (item for item in run.current_matches if item.match_id == room.tournament_match_id),
        None,
    ) if run else None
    participant_id = getattr(winner_player, "tournament_participant_id", None)
    if run is None or match is None or participant_id is None:
        return
    if participant_id not in {match.player1_id, match.player2_id}:
        return
    was_finished = run.status == "finished"
    match = run.resolve_match(match.match_id, participant_id, resolution="game")
    finished_now = not was_finished and run.status == "finished"
    await TOURNAMENT_STORE.save_run(run)
    await TOURNAMENT_STORE.audit(run.run_id, actor="system", action="game_result", details={
        "match_id": match.match_id,
        "winner_id": participant_id,
        "game_id": room.game_id,
    })
    asyncio.create_task(delayed_tournament_match_completion(
        run,
        match.match_id,
        participant_id,
        1.5,
        announce_finished=finished_now,
    ))


async def delayed_tournament_match_completion(
    run: TournamentRun,
    match_id: str,
    winner_id: Optional[str],
    delay_seconds: float,
    *,
    announce_finished: bool = False,
) -> None:
    await asyncio.sleep(delay_seconds)
    await close_tournament_match_room(
        run,
        match_id,
        winner_id=winner_id,
        broadcast_result=False,
    )
    if announce_finished:
        await announce_tournament_finished(run)
    elif run.status != "finished":
        await start_or_prepare_next_tournament_match(run)


def tournament_participant_disconnect_expired(
    player: Optional["Player"],
    online: bool,
    now: datetime,
    grace_seconds: int,
    fallback_time: Optional[datetime],
) -> bool:
    if online:
        return False
    disconnected_at = player.disconnected_at if player is not None else None
    disconnected_at = disconnected_at or fallback_time or now
    if player is not None and player.disconnected_at is None:
        player.disconnected_at = disconnected_at
    return (now - disconnected_at).total_seconds() >= grace_seconds


async def expire_disconnected_room_sessions(now: Optional[datetime] = None) -> None:
    current = now or utc_now()
    sessions = list({id(player): player for player in ROOM_RESUME_SESSIONS.values()}.values())
    expired_by_room: dict[int, tuple[Room, list["Player"]]] = {}
    for player in sessions:
        room = player.room
        if player.ws is not None or room is None:
            continue
        if room.tournament_match_id:
            continue
        disconnected_at = player.disconnected_at or current
        if (current - disconnected_at).total_seconds() < room_disconnect_grace_seconds(room, player):
            continue
        entry = expired_by_room.setdefault(id(room), (room, []))
        entry[1].append(player)

    for room, expired_players in expired_by_room.values():
        departed_ids = []
        for player in expired_players:
            departed_ids.append(player.id)
            if player in room.players:
                room.players.remove(player)
            player.room = None
            player.status = "watching"
            player.clear_hand()
            forget_room_resume_session(player)
            await room.log_chat(
                f"{player.name}は復帰猶予を超えたため、切断扱いで退室しました。"
            )
        await handle_room_after_player_removed(
            room,
            departed_ids[0] if len(departed_ids) == 1 else None,
        )


async def tournament_scheduler_tick(now: Optional[datetime] = None) -> None:
    current = now or utc_now()
    async with TOURNAMENT_LOCK:
        for run in list(TOURNAMENT_RUNS_BY_ROOM.values()):
            old_status = run.status
            transition = run.advance_clock(now=current)
            room = rooms.get(run.room_id)
            if room is None:
                continue
            room.rule = PRESETS[run.rule_key]
            room.tournament_run_id = run.run_id
            if transition is not None:
                await TOURNAMENT_STORE.save_run(run)
                await TOURNAMENT_STORE.audit(run.run_id, actor="system", action="status_change", details={
                    "from": old_status,
                    "to": run.status,
                })
                if run.status == "registration":
                    await room.log_chat(f"大会「{run.title}」の参加登録を開始しました。")
                    await announce_tournament_globally(
                        f"大会「{run.title}」の参加登録を開始しました。plus大会ルームへお越しください。"
                    )
                    await notify_tournament_discord(run, "registration")
                elif run.status == "cancelled":
                    reason = "参加者が2人未満のため大会を中止しました。"
                    await room.log_chat(reason)
                    await notify_tournament_discord(run, "cancelled", reason=reason)
                elif run.status == "running":
                    await room.log_chat("参加登録を締め切り、対戦の割り振りを確定しました。")
                    await notify_tournament_discord(run, "running")
                await room.update_room_status()
            if run.status != "running":
                continue
            if not run.current_matches:
                await start_or_prepare_next_tournament_match(run)
                continue
            for match in list(run.current_matches):
                match_room = TOURNAMENT_MATCH_ROOMS.get(match.match_id)
                if match.status == "playing" and match_room is None:
                    # 再起動で手札状態が失われた試合だけを再呼び出しする。
                    match.status = "called"
                    match.started_at = None
                    match.called_at = current
                    match.ready_deadline_at = current + timedelta(
                        seconds=tournament_match_ready_seconds()
                    )
                    match.ready_player_ids = []
                    await TOURNAMENT_STORE.save_run(run)
                    await notify_tournament_match_call(run, match)

                expected_room = match_room if match.status == "playing" else None
                player1 = TOURNAMENT_SESSIONS.get(match.player1_id)
                player2 = TOURNAMENT_SESSIONS.get(match.player2_id)
                online1 = tournament_session_online(match.player1_id, expected_room)
                online2 = tournament_session_online(match.player2_id, expected_room)
                if online1 and online2:
                    if match.status == "called":
                        deadline_elapsed = bool(
                            match.ready_deadline_at is not None
                            and current >= match.ready_deadline_at
                        )
                        if tournament_match_both_ready(match) or deadline_elapsed:
                            await prepare_tournament_match(run, match, now=current)
                    continue

                grace_seconds = (
                    playing_disconnect_grace_seconds()
                    if match.status == "playing"
                    else waiting_disconnect_grace_seconds()
                )
                fallback_time = match.started_at if match.status == "playing" else match.called_at
                expired1 = tournament_participant_disconnect_expired(
                    player1, online1, current, grace_seconds, fallback_time
                )
                expired2 = tournament_participant_disconnect_expired(
                    player2, online2, current, grace_seconds, fallback_time
                )
                if expired1 and online2:
                    winner_id = match.player2_id
                elif expired2 and online1:
                    winner_id = match.player1_id
                elif expired1 and expired2:
                    winner_id = None
                else:
                    continue
                await resolve_tournament_match(
                    run,
                    match.match_id,
                    winner_id,
                    resolution="forfeit" if winner_id else "auto_skip",
                    actor="system",
                )

        await expire_disconnected_room_sessions(current)


async def tournament_scheduler_loop() -> None:
    while True:
        try:
            await tournament_scheduler_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(2)


def campaign_base_payload(now: datetime, period=None) -> dict:
    schedule_label = "毎週 月曜6:00〜日曜24:00（日本時間）"
    if period is not None and period.key == LAUNCH_PERIOD_KEY:
        schedule_label = "初週のみ8/12 6:00開始・8/17 0:00終了（日本時間）"
    return {
        "campaign_key": CAMPAIGN_SETTINGS.key,
        "period_key": period.key if period is not None else None,
        "period_label": period.label if period is not None else None,
        "goal": period.goal if period is not None else CAMPAIGN_SETTINGS.goal,
        "starts_at": period.starts_at.isoformat() if period is not None else None,
        "ends_at": period.ends_at.isoformat() if period is not None else None,
        "server_now": now.isoformat(),
        "campaign_url": CAMPAIGN_SETTINGS.page_url,
        "schedule": CAMPAIGN_SETTINGS.schedule,
        "schedule_label": schedule_label,
    }


@app.get("/api/campaigns/gold-cpu-100")
async def get_cpu_campaign_status() -> dict:
    now = utc_now()
    state, period = CAMPAIGN_SETTINGS.period_state(now)
    payload = campaign_base_payload(now, period)
    empty = {
        "total_wins": 0,
        "progress_percent": 0,
        "rankings": [],
        "prime_rankings": [],
        "history": [],
        "last_updated_at": None,
    }

    if not CAMPAIGN_SETTINGS.enabled:
        return {
            **payload,
            "status": "unavailable",
            "message": "週次チャレンジは現在無効です",
            **empty,
        }

    if period is None:
        return {
            **payload,
            "status": "unavailable",
            "message": CAMPAIGN_SETTINGS.end_error
            or CAMPAIGN_SETTINGS.start_error
            or "開催期間を取得できません",
            **empty,
        }

    if not CAMPAIGN_STORE.ready:
        return {
            **payload,
            "status": "unavailable",
            "message": "集計情報を取得できません",
            **empty,
        }

    try:
        if state == "scheduled":
            overview = {
                "total_wins": 0,
                "rankings": [],
                "prime_rankings": [],
                "last_updated_at": None,
            }
        else:
            overview = await CAMPAIGN_STORE.overview(
                CAMPAIGN_SETTINGS.key,
                period,
                limit=20,
            )
        history = await CAMPAIGN_STORE.history(
            (CAMPAIGN_SETTINGS.key, LEGACY_CAMPAIGN_KEY),
            limit=12,
        )
    except Exception as exc:
        print(f"campaign leaderboard failed: {exc}")
        return {
            **payload,
            "status": "unavailable",
            "message": "集計情報を取得できません",
            **empty,
        }

    total_wins = overview["total_wins"]
    message = ""
    if state == "scheduled":
        message = "月曜6:00から新しい週が始まります。歴代記録をご覧ください。"
    elif state == "finished":
        message = "この開催は終了しました。最終結果です。"
    return {
        **payload,
        "status": state,
        "message": message,
        "total_wins": total_wins,
        "progress_percent": min(100, round(total_wins / period.goal * 100, 1)),
        "rankings": overview["rankings"],
        "prime_rankings": overview["prime_rankings"],
        "history": history,
        "last_updated_at": overview["last_updated_at"],
    }


class Player:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = secrets.token_hex(16)
        suffix = int(self.id, 16) % 10000
        self.name = f"プレイヤー{suffix:04d}"
        self.room = None  # 所属ルーム（Roomオブジェクト）
        self.status = "watching"  # 初期状態は観戦中
        self.hand = []  # プレイヤーが持つカードリスト
        self.registered_primes: set[int] = set()
        self.registered_composites: set[int] = set()
        self.registered_composite_entries = ()
        self.global_chat_subscribed = False
        self.last_global_chat_at = 0.0
        self.tournament_participant_id: Optional[str] = None
        self.tournament_view_match_id: Optional[str] = None
        self.disconnected_at: Optional[datetime] = None
        self.room_resume_token_hash: Optional[str] = None
        self.room_session_room_id: Optional[str] = None
        self.client_surface = "legacy"
        self.composite_practice_authorized = False
        self.small_finish_index = registered_prime_template_index((), max_cards=3)

    async def send_json(self, message: dict):
        """WebSocketを通じてJSONメッセージを送信する"""
        if self.ws is None:
            raise RuntimeError("player is disconnected")
        await self.ws.send_json(message)

    async def send_hand_update(self):
        """手札の変更通知をクライアントに送信する"""
        message = {
            "type": "hand_update",
            "your_hand": self.hand
        }
        await self.send_json(message)

    def sort_hand(self):
        """手札をランク順（必要に応じてスートも考慮）に並び替える"""
        # ここでは単純にカードの"rank"で昇順にソート
        self.hand.sort(key=lambda card: card["rank"])

    def add_card(self, card: dict):
        """手札にカードを追加する"""
        self.hand.append(card)
        self.sort_hand()  # カード追加後に手札を並び替え

    def remove_card(self, card: dict) -> bool:
        """手札から指定のカードを削除する。存在すればTrue、なければFalseを返す"""
        if card in self.hand:
            self.hand.remove(card)
            return True
        return False

    def has_cards(self, cards: List[dict]) -> bool:
        """指定されたカード群が自分の手札に存在するかチェックする"""
        temp = self.hand[:]  # コピーを使ってチェック
        for card in cards:
            if card in temp:
                temp.remove(card)
            else:
                return False
        return True

    def remove_cards(self, cards: List[dict]) -> bool:
        """指定されたカード群を手札から削除する。すべて削除できた場合にTrueを返す"""
        if not self.has_cards(cards):
            return False
        for card in cards:
            self.remove_card(card)
        return True

    def clear_hand(self):
        """手札をクリアする"""
        self.hand = []

    def replace_registered_primes(self, values: set[int]) -> None:
        self.registered_primes = set(values)
        self.small_finish_index = registered_prime_template_index(
            tuple(sorted(self.registered_primes)),
            max_cards=3,
        )

    def can_use_registered_prime(self, n: int) -> bool:
        return n in self.registered_primes

    def replace_registered_composites(self, values: set[int], entries=()) -> None:
        self.registered_composites = set(values)
        self.registered_composite_entries = tuple(entries)

    def can_use_registered_composite(self, n: int) -> bool:
        return n in self.registered_composites


GLOBAL_CHAT_SUBSCRIBERS: set[Player] = set()


def normalize_chat_message(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    message = value.strip()
    if not message or len(message) > MAX_CHAT_MESSAGE_LENGTH:
        return None
    return message


def global_chat_room_meta(room: Optional[Room]) -> dict:
    if room is None:
        return {"room_id": None, "room_badge": "ロビー", "room_tone": "neutral"}
    if room.room_id in NEO_BEGINNER_ROOM_IDS:
        number = NEO_BEGINNER_ROOM_IDS.index(room.room_id) + 1
        return {
            "room_id": room.room_id,
            "room_badge": f"初級・ルーム{number}",
            "room_tone": "beginner",
        }
    if room.room_id in NEO_ADVANCED_ROOM_IDS:
        number = NEO_ADVANCED_ROOM_IDS.index(room.room_id) + 1
        return {
            "room_id": room.room_id,
            "room_badge": f"上級・ルーム{number}",
            "room_tone": "advanced",
        }
    if room.room_id in CLASSIC_ROOM_IDS:
        number = CLASSIC_ROOM_IDS.index(room.room_id) + 1
        return {
            "room_id": room.room_id,
            "room_badge": f"Classic・ルーム{number}",
            "room_tone": "classic",
        }
    if room.room_id in PLUS_ROOM_IDS:
        number = PLUS_ROOM_IDS.index(room.room_id) + 1
        return {
            "room_id": room.room_id,
            "room_badge": f"Plus・ルーム{number}",
            "room_tone": "plus",
        }
    if room.room_id == TOURNAMENT_ROOM_ID:
        return {
            "room_id": room.room_id,
            "room_badge": "Plus・大会",
            "room_tone": "plus",
        }
    return {
        "room_id": room.room_id,
        "room_badge": room.room_id,
        "room_tone": "neutral",
    }


async def subscribe_global_chat(player: Player) -> bool:
    if player.room is None:
        await player.send_json({
            "type": "error",
            "code": "global_chat_requires_room",
            "message": "部屋へ入室してからグローバルチャットを表示してください。",
        })
        return False
    GLOBAL_CHAT_SUBSCRIBERS.add(player)
    player.global_chat_subscribed = True
    client_surface = getattr(player, "client_surface", "legacy")
    await player.send_json({
        "type": "global_chat_joined",
        "subscriber_count": len([
            subscriber
            for subscriber in GLOBAL_CHAT_SUBSCRIBERS
            if getattr(subscriber, "client_surface", "legacy") == client_surface
        ]),
        "notice": "ここからのメッセージだけが表示されます。個人情報や連絡先は書き込まないでください。",
    })
    return True


async def unsubscribe_global_chat(player: Player) -> None:
    GLOBAL_CHAT_SUBSCRIBERS.discard(player)
    player.global_chat_subscribed = False
    await player.send_json({"type": "global_chat_left"})


async def broadcast_global_chat(payload: dict, client_surface: str) -> None:
    disconnected = []
    for subscriber in list(GLOBAL_CHAT_SUBSCRIBERS):
        if getattr(subscriber, "client_surface", "legacy") != client_surface:
            continue
        try:
            await subscriber.send_json(payload)
        except Exception:
            disconnected.append(subscriber)
    for subscriber in disconnected:
        GLOBAL_CHAT_SUBSCRIBERS.discard(subscriber)
        subscriber.global_chat_subscribed = False


async def handle_global_chat_message(player: Player, data: dict) -> bool:
    if player not in GLOBAL_CHAT_SUBSCRIBERS or not player.global_chat_subscribed:
        await player.send_json({
            "type": "error",
            "code": "global_chat_not_joined",
            "message": "注意事項を確認してからグローバルチャットを表示してください。",
        })
        return False
    if player.room is None:
        await player.send_json({
            "type": "error",
            "code": "global_chat_requires_room",
            "message": "グローバルチャットへの送信には部屋への入室が必要です。",
        })
        return False

    template_key = data.get("template_key")
    template = GLOBAL_CHAT_TEMPLATES.get(template_key) if isinstance(template_key, str) else None
    if template_key is not None and template is None:
        await player.send_json({
            "type": "error",
            "code": "invalid_global_chat_template",
            "message": "選択した定型文は使用できません。",
        })
        return False
    message = template["message"] if template else normalize_chat_message(data.get("message"))
    if message is None:
        await player.send_json({
            "type": "error",
            "code": "invalid_chat_message",
            "message": f"メッセージは1〜{MAX_CHAT_MESSAGE_LENGTH}文字で入力してください。",
        })
        return False

    now = time.monotonic()
    if now - getattr(player, "last_global_chat_at", 0.0) < GLOBAL_CHAT_COOLDOWN_SECONDS:
        await player.send_json({
            "type": "error",
            "code": "global_chat_rate_limited",
            "message": "少し待ってから送信してください。",
        })
        return False
    player.last_global_chat_at = now

    await broadcast_global_chat({
        "type": "global_chat",
        "sender": player.name,
        "message": message,
        "template_key": template_key if template else None,
        "template_badge": template["badge"] if template else None,
        **global_chat_room_meta(player.room),
    }, client_surface=getattr(player, "client_surface", "legacy"))
    return True

################################################
# 勝敗判定ロジック
################################################

def check_win_condition(room):
    active_players = get_active_players(room)

    if len(active_players) == 0:
        return None

    # 現在プレイヤーの手札0枚による通常勝利
    current_turn_id = room.current_turn_id
    if current_turn_id is None:
        return None
    current_player = next((p for p in active_players if p.id == current_turn_id), None)
    if current_player is not None:
        if len(current_player.hand) == 0:
            # 勝利者のIDまたはPlayerオブジェクトそのものを返す（要件に応じて）
            return current_player.name
    return None

def get_active_players(room) -> List["Player"]:
    return [p for p in room.players if p.status == "waiting"]


def prepare_campaign_game(
    room: Room,
    active_players: List["Player"],
    started_at: Optional[datetime] = None,
) -> bool:
    now = started_at or utc_now()
    room.game_id = str(uuid.uuid4())
    room.game_started_at = now
    room.campaign_player_id = None
    room.campaign_cpu_key = None
    room.campaign_period = None
    room.campaign_largest_prime = None

    period = CAMPAIGN_SETTINGS.active_period(now)
    if not CAMPAIGN_SETTINGS.enabled or period is None:
        return False
    if room.room_id not in NEO_BEGINNER_ROOM_IDS or room.rule.key != "half-7-1-c-assist":
        return False
    if len(active_players) != 2:
        return False

    human_players = [player for player in active_players if not is_cpu_player(player)]
    cpu_players = [player for player in active_players if is_cpu_player(player)]
    if len(human_players) != 1 or len(cpu_players) != 1:
        return False

    cpu_key = getattr(cpu_players[0], "cpu_key", None)
    if cpu_key != "gold_planner":
        return False

    room.campaign_player_id = human_players[0].id
    room.campaign_cpu_key = cpu_key
    room.campaign_period = period
    return True


def remember_campaign_prime(room: Room, player: "Player", number: int) -> None:
    """Keep the human player's largest legal prime for this eligible game."""
    if room.campaign_player_id != player.id or room.campaign_period is None:
        return
    if not is_prime(number):
        return
    if room.campaign_largest_prime is None or number > room.campaign_largest_prime:
        room.campaign_largest_prime = number


async def maybe_record_campaign_win(
    room: Room,
    winner_player: Optional["Player"],
    won_at: Optional[datetime] = None,
) -> Optional[dict]:
    if not room.game_id or room.game_started_at is None or not room.campaign_cpu_key:
        return None

    now = won_at or utc_now()
    period = CAMPAIGN_SETTINGS.active_period(now)
    if period is None or room.campaign_period is None:
        return None
    if period.key != room.campaign_period.key:
        return None

    human_player = next(
        (player for player in room.players if player.id == room.campaign_player_id),
        None,
    )
    if (
        CAMPAIGN_STORE.ready
        and human_player is not None
        and room.campaign_largest_prime is not None
    ):
        try:
            await asyncio.wait_for(
                CAMPAIGN_STORE.record_prime(
                    campaign_key=CAMPAIGN_SETTINGS.key,
                    period=period,
                    game_id=room.game_id,
                    player_name=human_player.name,
                    prime_value=room.campaign_largest_prime,
                    achieved_at=now,
                ),
                timeout=3,
            )
        except Exception as exc:
            print(f"campaign prime recording failed: {exc}")

    if winner_player is None or is_cpu_player(winner_player):
        return None
    if room.campaign_player_id != winner_player.id:
        return None

    base_result = {
        "type": "campaign_result",
        "campaign_key": CAMPAIGN_SETTINGS.key,
        "period_key": period.key,
        "period_label": period.label,
        "player_name": winner_player.name,
        "goal": period.goal,
        "campaign_url": CAMPAIGN_SETTINGS.page_url,
    }
    if not CAMPAIGN_STORE.ready:
        return {
            **base_result,
            "status": "failed",
            "message": "勝利しましたが、キャンペーン記録を保存できませんでした。",
        }

    try:
        counts = await asyncio.wait_for(
            CAMPAIGN_STORE.record_win(
                campaign_key=CAMPAIGN_SETTINGS.key,
                period=period,
                game_id=room.game_id,
                player_name=winner_player.name,
                room_id=room.room_id,
                rule_key=room.rule.key,
                cpu_key=room.campaign_cpu_key,
                game_started_at=room.game_started_at,
                won_at=now,
            ),
            timeout=3,
        )
    except Exception as exc:
        print(f"campaign win recording failed: {exc}")
        return {
            **base_result,
            "status": "failed",
            "message": "勝利しましたが、キャンペーン記録を保存できませんでした。",
        }

    return {
        **base_result,
        "status": "recorded",
        "player_wins": counts["player_wins"],
        "total_wins": counts["total_wins"],
        "message": "今週のCPUチャレンジに1勝を記録しました。",
    }


def score_card_symbol(card: dict) -> str:
    if card.get("is_joker") or card.get("suit") == "X":
        return "X"
    return score_value_symbol(card.get("rank"))

def score_value_symbol(value) -> str:
    value = str(value)
    return {
        "1": "A",
        "10": "T",
        "11": "J",
        "12": "Q",
        "13": "K",
    }.get(value, value)

def score_sort_key(card: dict) -> int:
    if card.get("is_joker") or card.get("suit") == "X":
        return 10_000
    return int(card.get("rank", 0))

def score_cards_text(cards: List[dict], sort_cards: bool = False) -> str:
    ordered = sorted(cards, key=score_sort_key) if sort_cards else cards
    return "".join(score_card_symbol(c) for c in ordered)

def score_joker_suffix(cards: List[dict], assigned_values: List[str]) -> str:
    suffixes = []
    joker_index = 0
    for card in cards:
        if not (card.get("is_joker") or card.get("suit") == "X"):
            continue
        if joker_index >= len(assigned_values):
            break
        value = str(assigned_values[joker_index])
        joker_index += 1
        if value != "inf":
            suffixes.append(f"|X={score_value_symbol(value)}")
    return "".join(suffixes)

def score_state_prefix(room: Room) -> str:
    return "[R]" if room.reverse_order else ""

def score_win_suffix(player: "Player") -> str:
    return "#" if len(player.hand) == 0 else ""

def score_tokens_text(tokens: List[dict], cards_by_id: Dict[str, dict]) -> str:
    parts = []
    for token in tokens:
        if token.get("kind") == "card":
            card = cards_by_id.get(token.get("card_id"))
            parts.append(score_card_symbol(card) if card else "?")
        elif token.get("kind") == "op":
            parts.append("*" if token.get("op") == "×" else token.get("op", "?"))
    return "".join(parts)

def record_score_line(room: Room, line: str) -> None:
    room.score_log.append({
        "turn": len(room.score_log) + 1,
        "line": line,
    })

def record_score_play_line(room: Room, player: "Player", notation: str) -> None:
    prefix = f"{player.name}:"
    if room.score_log:
        last = room.score_log[-1]
        line = last.get("line", "")
        if line.startswith(prefix):
            tail = line[len(prefix):]
            if "D(" in tail and tail.endswith(")") and ",P(" not in tail:
                draw_prefix = tail.split("D(", 1)[0]
                suffix = notation
                if draw_prefix and suffix.startswith(draw_prefix):
                    suffix = suffix[len(draw_prefix):]
                last["line"] = line + suffix
                return
    record_score_line(room, f"{player.name}:{notation}")

def record_score_event(room: Room, player: "Player", notation: str, result: str) -> None:
    line = f"{player.name}:{notation}"
    room.score_log.append({
        "turn": len(room.score_log) + 1,
        "player": player.name,
        "notation": notation,
        "result": result,
        "line": line,
    })

async def publish_score_log(room: Room, winner: Optional[str]) -> None:
    if not room.score_log:
        return
    payload = {
        "type": "score_record",
        "sender": "system",
        "winner": winner,
        "records": room.score_log,
        "lines": [record.get("line", "") for record in room.score_log if record.get("line")],
    }
    if room.tournament_match_id:
        payload.update({
            "scope": "tournament_match",
            "match_id": room.tournament_match_id,
        })
    await room.broadcast(payload)
    if room.tournament_match_id:
        run = tournament_run_for_room(room)
        if run is not None:
            match = next(
                (item for item in run.matches if item.match_id == room.tournament_match_id),
                None,
            )
            await broadcast_tournament_lobby(run, {
                "type": "tournament_score_record",
                "match_id": room.tournament_match_id,
                "round_no": match.round_no if match is not None else None,
                "player1_name": run.participants[match.player1_id].display_name if match is not None else None,
                "player2_name": run.participants[match.player2_id].display_name if match is not None else None,
                "winner": winner,
                "lines": payload["lines"],
            })

################################################
# カード生成と配布のユーティリティ
################################################
def generate_deck() -> List[dict]:
    deck = []
    for suit in ["S","H","D","C"]:
        for rank in range(1,14):
            deck.append({
                "card_id": str(uuid.uuid4()),
                "suit": suit,
                "rank": rank,
                "is_joker": False
            })
    # ジョーカー２枚にも同様にIDを
    for _ in range(2):
        deck.append({
            "card_id": str(uuid.uuid4()),
            "suit": "X",
            "rank": 0,
            "is_joker": True
        })
    random.shuffle(deck)
    return deck

def build_deck(rule: RulePreset) -> List[dict]:
    deck = generate_deck()
    if rule.deck_rule in (DeckRule.EVEN_HALVED, DeckRule.EVEN_HALVED_WITH_CHEFS):
        kept = []
        removed_count = 0
        for card in deck:
            remove_even_card = (
                (not card["is_joker"])
                and (card["rank"] % 2 == 0)
                and (card["suit"] in ("D", "H"))
            )
            if remove_even_card:
                removed_count += 1
            else:
                kept.append(card)
        deck = kept
        if rule.deck_rule is DeckRule.EVEN_HALVED_WITH_CHEFS:
            deck.extend(
                {
                    "card_id": str(uuid.uuid4()),
                    "suit": CHEF_CARD_SUIT,
                    "rank": CHEF_CARD_RANK,
                    "is_joker": False,
                }
                for _ in range(removed_count)
            )
    random.shuffle(deck)
    return deck

def shuffle_and_deal(deck: List[dict], hand_n: int, num_players: int = 2
                     ) -> Tuple[List[List[dict]], List[dict]]:
    """
    deck をシャッフルして num_players 人へ hand_n 枚ずつ順番配り。
    返り値: hands[プレイヤーごとの手札], remaining_deck
    """
    deck = deck[:]            # 破壊的変更を避ける
    random.shuffle(deck)

    hands = [[] for _ in range(num_players)]
    total_needed = hand_n * num_players
    if len(deck) < total_needed:
        total_needed = len(deck) - (len(deck) % num_players)
        hand_n = total_needed // num_players  # 足りない場合は配れるだけ配る

    # ラウンドロビンで配る（将来のバグ予防：順番性が必要な場合に備える）
    for r in range(hand_n):
        for i in range(num_players):
            hands[i].append(deck.pop(0))
    return hands, deck

def push_to_reserve(room: Room, cards: List[dict]) -> None:
    """出した札を、出した順番のまま予備軍へ積む（重複登録は呼び出し側で避ける）"""
    if cards:
        room.reserve.extend(cards)

def flow_field(room: Room) -> None:
    """場が流れたときの共通処理：場を空にし、予備軍を山札の“下”に戻す（順序保持）"""
    room.field = []
    room.last_number = None
    if room.reserve:
        room.deck.extend(room.reserve)  # pop(0)で上から引く設計なので、extendは“下に戻す”
        room.reserve.clear()

def return_cards_to_deck_bottom(room, cards: List[dict]) -> None:
    """合成数の『消費カード』を即座に山札の底に戻す。場は流さない。"""
    if not cards:
        return
    room.deck.extend(cards)

def get_penalty_card_count(rule: PenaltyRule, field_card_count: int, normal_card_count: int) -> int:
    """
    ペナルティ枚数を返す。
      ALWAYS_1    -> 1
      FIELD_COUNT -> 場の枚数
      NORMAL      -> 通常ルールの枚数
    """
    if rule is PenaltyRule.ALWAYS_1:
        return 1
    if rule is PenaltyRule.FIELD_COUNT:
        return field_card_count
    if rule is PenaltyRule.NORMAL:
        return normal_card_count
    return normal_card_count

def missing_registered_prime_players(room: Room) -> List["Player"]:
    if (
        room.rule.prime_rule is not PrimeRule.REGISTERED
        or not room.rule.registration_enabled
    ):
        return []
    return [
        p for p in get_active_players(room)
        if not p.registered_primes and not p.registered_composites
    ]

def registered_numbers_update_payload(prime_result, composite_result) -> dict:
    return {
        "type": "registered_numbers_updated",
        "prime_values": sorted(set(prime_result.prime_values)),
        "composite_values": sorted(set(composite_result.composite_values)),
        "prime_count": len(prime_result.prime_values),
        "composite_count": len(composite_result.composite_values),
        "prime_duplicate_count": prime_result.duplicate_count,
        "composite_duplicate_count": composite_result.duplicate_count,
        "prime_errors": [asdict(error) for error in prime_result.errors],
        "composite_errors": [asdict(error) for error in composite_result.errors],
        "truncated": prime_result.truncated or composite_result.truncated,
    }

def registered_number_total(prime_values, composite_values) -> int:
    return len(set(prime_values)) + len(set(composite_values))

def registered_number_limit_error_payload(limit: int, total: int) -> dict:
    return {
        "type": "error",
        "code": "registered_number_limit",
        "message": f"この部屋では登録できる素数・合成数は合計{limit}個までです。現在の入力は{total}個です。",
    }

def replace_player_registered_numbers_from_text(
    player: "Player",
    prime_text: str,
    composite_text: str,
    limit: Optional[int] = None,
) -> dict:
    prime_result = parse_registered_prime_text(prime_text)
    composite_result = parse_registered_composite_text(composite_text)
    total = registered_number_total(prime_result.prime_values, composite_result.composite_values)
    if limit is not None and total > limit:
        return registered_number_limit_error_payload(limit, total)
    player.replace_registered_primes(set(prime_result.prime_values))
    player.replace_registered_composites(
        set(composite_result.composite_values),
        composite_result.entries,
    )
    return registered_numbers_update_payload(prime_result, composite_result)

REGISTERED_SAMPLE_DEFS = {
    "sashimi2024": {
        "label": "サンプル：さしみ2024",
        "prime_json": SAMPLE_MEMORY_JSON,
        "composite_text": None,
    },
    "tournament_order": {
        "label": "サンプル：大会出た順",
        "prime_json": REGISTERED_TOURNAMENT_JSON,
        "composite_text": None,
    },
    "gold_prime_table": {
        "label": "サンプル：ゴールド素数表",
        "prime_json": GOLD_PRIME_TABLE_JSON,
        "composite_text": None,
    },
    "silver_prime_table": {
        "label": "サンプル：シルバー素数表",
        "prime_json": SILVER_PRIME_TABLE_JSON,
        "composite_text": None,
    },
    "composite_practice_ge3": {
        "label": "合成数練習：3枚以下上位互換＋大会3回以上＋対策147式",
        "prime_json": COMPOSITE_PRACTICE_PRIME_JSON,
        "composite_text": COMPOSITE_PRACTICE_GE3_TEXT,
        "supplemental_composite_json": COMPOSITE_PRACTICE_COUNTERMEASURES_JSON,
        "access_scope": COMPOSITE_PRACTICE_ACCESS_SCOPE,
        "visible": True,
    },
    "composite_practice_cpu_ge3": {
        "label": "合成数練習CPU：3枚以下上位互換＋大会3回以上＋対策147式",
        "prime_json": COMPOSITE_PRACTICE_PRIME_JSON,
        "composite_text": COMPOSITE_PRACTICE_GE3_TEXT,
        "supplemental_composite_json": COMPOSITE_PRACTICE_COUNTERMEASURES_JSON,
        "access_scope": COMPOSITE_PRACTICE_ACCESS_SCOPE,
        "visible": False,
    },
}
DEFAULT_REGISTERED_SAMPLE_KEY = "sashimi2024"

def supplemental_composite_text_from_json(path: Optional[Path]) -> str:
    if path is None or not path.exists():
        return ""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    composite_text = str(data.get("compositeText", "")).strip()
    if composite_text:
        return composite_text
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError(f"invalid supplemental composite knowledge: {path}")
    equations = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"invalid supplemental composite entry: {path}")
        equation = str(entry.get("registeredEquation", "")).strip()
        if not equation:
            raise ValueError(f"missing registeredEquation: {path}")
        equations.append(equation)
    return "\n".join(equations)


def load_sample_memory_from_files(
    prime_json: Path,
    composite_text_path: Optional[Path] = None,
    supplemental_composite_json: Optional[Path] = None,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple, str, str]:
    if not prime_json.exists():
        return (), (), (), "", ""
    data = json.loads(prime_json.read_text(encoding="utf-8-sig"))
    prime_text = str(data.get("primeText", "")).strip()
    if composite_text_path is not None and composite_text_path.exists():
        composite_text = composite_text_path.read_text(encoding="utf-8-sig").strip()
    else:
        composite_text = "\n".join(
            part.strip()
            for part in (
                str(data.get("compositeText", "")).strip(),
                str(data.get("additionalCompositeText", "")).strip(),
            )
            if part.strip()
        )
    supplemental_composite_text = supplemental_composite_text_from_json(
        supplemental_composite_json
    )
    if supplemental_composite_text:
        composite_text = "\n".join(
            part for part in (composite_text, supplemental_composite_text) if part
        )
    prime_result = parse_registered_prime_text(prime_text)
    composite_result = parse_registered_composite_text(composite_text)
    return (
        prime_result.prime_values,
        composite_result.composite_values,
        composite_result.entries,
        prime_text,
        composite_text,
    )

def load_registered_samples() -> dict:
    samples = {}
    for key, definition in REGISTERED_SAMPLE_DEFS.items():
        samples[key] = {
            "key": key,
            "label": definition["label"],
            "access_scope": definition.get("access_scope"),
            "visible": definition.get("visible", True),
            "data": load_sample_memory_from_files(
                definition["prime_json"],
                definition.get("composite_text"),
                definition.get("supplemental_composite_json"),
            ),
        }
    return samples

REGISTERED_SAMPLES = load_registered_samples()

def registered_sample_options(include_composite_practice: bool = False) -> list[dict]:
    options = []
    for key, sample in REGISTERED_SAMPLES.items():
        if not sample.get("visible", True):
            continue
        if sample.get("access_scope") == COMPOSITE_PRACTICE_ACCESS_SCOPE and not include_composite_practice:
            continue
        prime_values, composite_values, _, _, _ = sample["data"]
        prime_count = len(set(prime_values))
        composite_count = len(set(composite_values))
        options.append({
            "key": key,
            "label": sample["label"],
            "prime_count": prime_count,
            "composite_count": composite_count,
            "total_count": prime_count + composite_count,
        })
    return options


def player_can_load_registered_sample(player: "Player", sample: Optional[dict]) -> bool:
    if sample is None:
        return False
    if sample.get("access_scope") != COMPOSITE_PRACTICE_ACCESS_SCOPE:
        return True
    return bool(
        getattr(player, "composite_practice_authorized", False)
        and player.room is not None
        and player.room.room_id == COMPOSITE_PRACTICE_ROOM_ID
    )

def registered_sample_for_key(sample_key: str):
    return REGISTERED_SAMPLES.get(sample_key) or REGISTERED_SAMPLES.get(DEFAULT_REGISTERED_SAMPLE_KEY)

(
    SAMPLE_REGISTERED_PRIMES,
    SAMPLE_REGISTERED_COMPOSITES,
    SAMPLE_REGISTERED_COMPOSITE_ENTRIES,
    SAMPLE_REGISTERED_PRIME_TEXT,
    SAMPLE_REGISTERED_COMPOSITE_TEXT,
) = registered_sample_for_key(DEFAULT_REGISTERED_SAMPLE_KEY)["data"]

def limit_registered_sample_data(
    primes,
    composites,
    composite_entries,
    prime_text: str,
    composite_text: str,
    limit: Optional[int] = None,
):
    if limit is None or registered_number_total(primes, composites) <= limit:
        return primes, composites, composite_entries, prime_text, composite_text, False

    limited_primes = tuple(dict.fromkeys(primes))[:limit]
    remaining = max(0, limit - len(limited_primes))
    limited_entries = []
    seen_composites = set()
    for entry in composite_entries:
        if remaining <= 0:
            break
        if entry.value in seen_composites:
            continue
        seen_composites.add(entry.value)
        limited_entries.append(entry)
        remaining -= 1

    limited_composites = tuple(entry.value for entry in limited_entries)
    limited_prime_text = "\n".join(str(value) for value in limited_primes)
    limited_composite_text = "\n".join(
        f"{entry.pattern}={entry.expression}"
        for entry in limited_entries
    )
    return (
        limited_primes,
        limited_composites,
        tuple(limited_entries),
        limited_prime_text,
        limited_composite_text,
        True,
    )

def load_sample_registered_prime_payload(
    player: "Player",
    sample_key: str = DEFAULT_REGISTERED_SAMPLE_KEY,
    limit: Optional[int] = None,
) -> dict:
    sample = registered_sample_for_key(sample_key)
    if sample is None:
        primes, composites, composite_entries, prime_text, composite_text = (), (), (), "", ""
        sample_key = DEFAULT_REGISTERED_SAMPLE_KEY
        sample_label = ""
    else:
        primes, composites, composite_entries, prime_text, composite_text = sample["data"]
        sample_key = sample["key"]
        sample_label = sample["label"]
    primes, composites, composite_entries, prime_text, composite_text, limited = limit_registered_sample_data(
        primes,
        composites,
        composite_entries,
        prime_text,
        composite_text,
        limit,
    )
    player.replace_registered_primes(set(primes))
    player.replace_registered_composites(
        set(composites),
        composite_entries,
    )
    return {
        "type": "registered_numbers_updated",
        "prime_values": sorted(player.registered_primes),
        "composite_values": sorted(player.registered_composites),
        "prime_count": len(player.registered_primes),
        "composite_count": len(player.registered_composites),
        "prime_duplicate_count": 0,
        "composite_duplicate_count": 0,
        "prime_errors": [],
        "composite_errors": [],
        "truncated": limited,
        "registered_number_limit": limit,
        "sample": True,
        "sample_key": sample_key,
        "sample_label": sample_label,
        "sample_prime_text": prime_text,
        "sample_composite_text": composite_text,
    }

def field_allows_number(room: Room, number: int, card_count: int) -> bool:
    if room.field and card_count != len(room.field):
        return False
    return field_allows_number_value(room, number)

def field_allows_number_value(room: Room, number: int) -> bool:
    if room.field:
        field_number = room.last_number if room.last_number is not None else -1
        if not room.reverse_order and number <= field_number:
            return False
        if room.reverse_order and number >= field_number:
            return False
    return True

def find_prime_realization(
    number: int,
    source_cards: List[dict],
    required_card_count: Optional[int] = None,
) -> Optional[dict]:
    realizations = find_prime_realizations(
        number,
        source_cards,
        required_card_count,
        limit=1,
    )
    return realizations[0] if realizations else None

def assist_card_text(cards: List[dict], assigned_numbers: List[str]) -> str:
    parts = []
    suffixes = []
    joker_index = 0
    for card in cards:
        if card.get("is_joker") or card.get("suit") == "X":
            assigned = assigned_numbers[joker_index] if joker_index < len(assigned_numbers) else "?"
            parts.append("X")
            if assigned != "inf":
                suffixes.append(f"|X={score_value_symbol(assigned)}")
            joker_index += 1
        else:
            parts.append(score_value_symbol(card.get("rank")))
    return "".join(parts) + "".join(suffixes)

def assist_joker_count(cards: List[dict]) -> int:
    return sum(1 for card in cards if card.get("is_joker") or card.get("suit") == "X")

def is_single_joker_realization(realization: dict) -> bool:
    cards = realization.get("cards") or []
    return (
        len(cards) == 1
        and assist_joker_count(cards) == 1
    )

def build_assist_number_text_filter(
    source_cards: List[dict],
    required_card_count: Optional[int],
):
    rank_counts = [0] * 14
    joker_count = 0
    for card in source_cards:
        if card.get("is_joker") or card.get("suit") == "X":
            joker_count += 1
            continue
        try:
            rank = int(card.get("rank"))
        except (TypeError, ValueError):
            continue
        if 0 <= rank <= 13:
            rank_counts[rank] += 1

    rank_options = tuple(
        (str(rank), rank)
        for rank, count in enumerate(rank_counts)
        if count > 0
    )
    joker_options = tuple(str(value) for value in range(14))
    total_cards = len(source_cards)

    @lru_cache(maxsize=None)
    def can_match(text: str, index: int, counts: tuple[int, ...], jokers_left: int, used_count: int) -> bool:
        if required_card_count is not None:
            if used_count > required_card_count:
                return False
            if used_count + (len(text) - index) < required_card_count:
                return False
            if used_count + (total_cards - used_count) < required_card_count:
                return False
        if index == len(text):
            return required_card_count is None or used_count == required_card_count

        for option, rank in rank_options:
            if counts[rank] <= 0 or not text.startswith(option, index):
                continue
            next_counts = list(counts)
            next_counts[rank] -= 1
            if can_match(text, index + len(option), tuple(next_counts), jokers_left, used_count + 1):
                return True

        if jokers_left > 0:
            for option in joker_options:
                if text.startswith(option, index) and can_match(
                    text,
                    index + len(option),
                    counts,
                    jokers_left - 1,
                    used_count + 1,
                ):
                    return True

        return False

    def can_realize(number: int) -> bool:
        text = str(number)
        return can_match(text, 0, tuple(rank_counts), joker_count, 0)

    return can_realize

def find_prime_realizations(
    number: int,
    source_cards: List[dict],
    required_card_count: Optional[int] = None,
    limit: int = ASSIST_REALIZATIONS_PER_NUMBER,
) -> List[dict]:
    text = str(number)
    rank_counts = [0] * 14
    cards_by_rank: list[list[dict]] = [[] for _ in range(14)]
    joker_cards: list[dict] = []
    available_jokers = 0
    for card in source_cards:
        if card.get("is_joker") or card.get("suit") == "X":
            available_jokers += 1
            joker_cards.append(card)
            continue
        try:
            rank = int(card.get("rank"))
        except (TypeError, ValueError):
            continue
        if 0 <= rank <= 13:
            rank_counts[rank] += 1
            cards_by_rank[rank].append(card)

    rank_options = tuple(
        (str(rank), rank)
        for rank, count in enumerate(rank_counts)
        if count > 0
    )
    joker_options = tuple(str(value) for value in range(14))
    impossible = len(source_cards) + 1

    @lru_cache(maxsize=None)
    def min_jokers_to_match(
        index: int,
        counts: tuple[int, ...],
        jokers_left: int,
        used_count: int,
    ) -> int:
        if required_card_count is not None:
            if used_count > required_card_count:
                return impossible
            if used_count + (len(text) - index) < required_card_count:
                return impossible
            if used_count + sum(counts) + jokers_left < required_card_count:
                return impossible
        if index == len(text):
            if required_card_count is None or used_count == required_card_count:
                return 0
            return impossible

        best = impossible
        for option, rank in rank_options:
            if counts[rank] <= 0 or not text.startswith(option, index):
                continue
            next_counts = list(counts)
            next_counts[rank] -= 1
            tail = min_jokers_to_match(
                index + len(option),
                tuple(next_counts),
                jokers_left,
                used_count + 1,
            )
            if tail == 0:
                return 0
            best = min(best, tail)

        if jokers_left > 0:
            for option in joker_options:
                if not text.startswith(option, index):
                    continue
                tail = min_jokers_to_match(
                    index + len(option),
                    counts,
                    jokers_left - 1,
                    used_count + 1,
                )
                if tail != impossible:
                    best = min(best, 1 + tail)

        return best

    minimum_joker_count = min_jokers_to_match(
        0,
        tuple(rank_counts),
        available_jokers,
        0,
    )
    if minimum_joker_count == impossible:
        return []

    # Search rank-count states rather than physical-card permutations. Cards
    # with the same rank are interchangeable for the visible realization; the
    # previous physical-card DFS revisited up to 4! equivalent paths before it
    # could discover the next distinct spelling on large hands.
    used_tokens: list[tuple[str, int | str]] = []
    results: list[dict] = []
    seen_patterns: set[tuple[str, ...]] = set()

    def card_pattern() -> tuple[str, ...]:
        return tuple(
            f"X={value}" if kind == "joker" else str(value)
            for kind, value in used_tokens
        )

    def collect_result() -> None:
        joker_count = sum(1 for kind, _ in used_tokens if kind == "joker")
        if joker_count != minimum_joker_count:
            return
        pattern = card_pattern()
        if pattern in seen_patterns:
            return
        seen_patterns.add(pattern)
        used_rank_counts = [0] * 14
        used_jokers = 0
        cards: list[dict] = []
        assigned_numbers: list[str] = []
        for kind, value in used_tokens:
            if kind == "joker":
                cards.append(joker_cards[used_jokers])
                used_jokers += 1
                assigned_numbers.append(str(value))
                continue
            rank = int(value)
            cards.append(cards_by_rank[rank][used_rank_counts[rank]])
            used_rank_counts[rank] += 1
        results.append({
            "number": number,
            "cards": cards,
            "assigned_numbers": assigned_numbers,
            "visible_text": assist_card_text(cards, assigned_numbers),
            "joker_count": joker_count,
        })

    def visit(
        index: int,
        counts: tuple[int, ...],
        jokers_left: int,
        used_joker_count: int = 0,
    ) -> None:
        if len(results) >= limit:
            return
        if used_joker_count > minimum_joker_count:
            return
        if required_card_count is not None and len(used_tokens) > required_card_count:
            return
        if index == len(text):
            if required_card_count is None or len(used_tokens) == required_card_count:
                collect_result()
            return

        for option, rank in rank_options:
            if counts[rank] <= 0 or not text.startswith(option, index):
                continue
            next_counts = list(counts)
            next_counts[rank] -= 1
            used_tokens.append(("rank", rank))
            visit(
                index + len(option),
                tuple(next_counts),
                jokers_left,
                used_joker_count,
            )
            used_tokens.pop()
            if len(results) >= limit:
                return

        if jokers_left > 0:
            for option in joker_options:
                if not text.startswith(option, index):
                    continue
                used_tokens.append(("joker", option))
                visit(
                    index + len(option),
                    counts,
                    jokers_left - 1,
                    used_joker_count + 1,
                )
                used_tokens.pop()
                if len(results) >= limit:
                    return

    visit(0, tuple(rank_counts), available_jokers)
    results.sort(key=lambda result: (
        len(result["cards"]),
        result.get("joker_count", 0),
        result["visible_text"],
    ))
    return results

def remove_cards_by_id(cards: List[dict], used_cards: List[dict]) -> List[dict]:
    used_ids = {card["card_id"] for card in used_cards}
    return [card for card in cards if card["card_id"] not in used_ids]

def find_rank_sequence_realization(
    ranks: tuple[int, ...],
    source_cards: List[dict],
) -> Optional[dict]:
    used: list[dict] = []
    assigned_by_card_id: dict[str, str] = {}

    def visit(index: int, remaining: list[dict]) -> bool:
        if index == len(ranks):
            return True
        rank = ranks[index]

        candidates = sorted(
            enumerate(remaining),
            key=lambda item: 1 if item[1].get("is_joker") or item[1].get("suit") == "X" else 0,
        )
        for i, card in candidates:
            is_joker = card.get("is_joker") or card.get("suit") == "X"
            if not is_joker and int(card.get("rank")) != rank:
                continue
            used.append(card)
            if is_joker:
                assigned_by_card_id[card["card_id"]] = str(rank)
            next_remaining = remaining[:i] + remaining[i + 1:]
            if visit(index + 1, next_remaining):
                return True
            if is_joker:
                assigned_by_card_id.pop(card["card_id"], None)
            used.pop()
        return False

    if not visit(0, source_cards[:]):
        return None

    return {
        "cards": used[:],
        "assigned_numbers": [
            assigned_by_card_id[card["card_id"]]
            for card in used
            if card.get("is_joker") or card.get("suit") == "X"
        ],
    }

def find_composite_expression_realization(entry, source_cards: List[dict]) -> Optional[dict]:
    remaining = source_cards[:]
    material_cards: list[dict] = []
    assigned_numbers: list[str] = []
    tokens: list[dict] = []

    for token in entry.expression_tokens:
        if token.kind == "op":
            tokens.append({
                "kind": "op",
                "op": "×" if token.op == "*" else token.op,
            })
            continue

        realization = find_rank_sequence_realization(token.ranks, remaining)
        if realization is None:
            return None
        for card in realization["cards"]:
            tokens.append({"kind": "card", "card_id": card["card_id"]})
        material_cards.extend(realization["cards"])
        assigned_numbers.extend(realization["assigned_numbers"])
        remaining = remove_cards_by_id(remaining, realization["cards"])

    return {
        "tokens": tokens,
        "cards": material_cards,
        "assigned_numbers": assigned_numbers,
    }

def assist_limit_from_filters(data: dict) -> int:
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    limit_mode = filters.get("limit_mode", "ten")
    if limit_mode in ASSIST_LIMITS:
        return ASSIST_LIMITS[limit_mode]
    limit = data.get("limit", 10)
    try:
        return max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        return 10

def assist_scan_limit_from_filters(data: dict) -> int:
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    limit_mode = filters.get("limit_mode", "ten")
    return ASSIST_SCAN_LIMITS.get(limit_mode, ASSIST_SCAN_LIMITS["ten"])

def assist_filter_value(data: dict, key: str, default: str) -> str:
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        return default
    value = filters.get(key)
    return value if isinstance(value, str) else default

def assist_card_count_from_filters(data: dict) -> Optional[int]:
    value = assist_filter_value(data, "card_count", "1")
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 1
    return count if 1 <= count <= 11 else 1

def assist_candidate_is_legal_for_filters(
    room: Room,
    candidate: dict,
    count_scope: str,
    specified_card_count: Optional[int],
) -> bool:
    card_count = len(candidate.get("cards") or [])
    if count_scope == "specified":
        return card_count == specified_card_count
    if count_scope != "field" or not room.field:
        return True
    if candidate.get("special_effect") == "infinity":
        return len(room.field) == 1
    return field_allows_number(room, int(candidate.get("number", 0)), card_count)

def assist_recommendation_cache_key(
    player: "Player",
    room: Room,
    source_cards: list[dict],
    selected_ids: list,
    composite_ids: list,
    count_scope: str,
    kind_scope: str,
    specified_card_count: Optional[int],
) -> tuple:
    def card_signature(card: dict) -> tuple:
        return (
            str(card.get("card_id")),
            card.get("rank"),
            card.get("suit"),
            bool(card.get("is_joker")),
        )

    composite_entries = tuple(
        sorted(
            (
                int(entry.value),
                str(entry.expression),
            )
            for entry in getattr(player, "registered_composite_entries", ())
        )
    )
    return (
        RECOMMENDATION_CACHE_VERSION,
        tuple(card_signature(card) for card in player.hand),
        tuple(card_signature(card) for card in source_cards),
        tuple(card_signature(card) for card in room.field),
        room.last_number,
        bool(room.reverse_order),
        getattr(room.rule, "key", ""),
        tuple(sorted(int(number) for number in player.registered_primes)),
        tuple(sorted(int(number) for number in player.registered_composites)),
        composite_entries,
        tuple(str(card_id) for card_id in selected_ids),
        tuple(str(card_id) for card_id in composite_ids),
        count_scope,
        kind_scope,
        specified_card_count,
    )

def assist_number_sort_key(room: Room, order: str):
    strong_first = order == "strong"
    if room.reverse_order:
        return (lambda item: item[1]) if strong_first else (lambda item: -item[1])
    return (lambda item: -item[1]) if strong_first else (lambda item: item[1])

def assist_efficiency_score(candidate: dict) -> float:
    if candidate.get("special_effect") == "infinity":
        return float("inf")
    card_count = max(1, len(candidate.get("cards") or []))
    return candidate["number"] / (10 ** (card_count - 1))

def assist_duplicate_card_set_key(candidate: dict) -> Optional[tuple]:
    cards = candidate.get("cards") or []
    if not cards:
        return None
    card_ids = tuple(sorted(str(card.get("card_id")) for card in cards))
    return (
        candidate.get("kind"),
        card_ids,
    )

def assist_joker_position_key(candidate: dict) -> tuple[int, ...]:
    return tuple(
        index
        for index, card in enumerate(candidate.get("cards") or [])
        if card.get("is_joker") or card.get("suit") == "X"
    )

def assist_duplicate_card_set_choice_key(candidate: dict, prefer_low_number: bool = False) -> tuple:
    number = candidate.get("number", 0)
    return (
        -number if prefer_low_number else number,
        tuple(-position for position in assist_joker_position_key(candidate)),
        -len(candidate.get("cards") or []),
        candidate.get("visible_text", ""),
    )

def deduplicate_duplicate_card_set_assist_candidates(
    candidates: list[dict],
    prefer_low_number: bool = False,
) -> list[dict]:
    indexed_candidates = list(enumerate(candidates))
    best_by_key: dict[tuple, tuple[int, dict]] = {}
    passthrough: list[tuple[int, dict]] = []
    for index, candidate in indexed_candidates:
        key = assist_duplicate_card_set_key(candidate)
        if key is None:
            passthrough.append((index, candidate))
            continue
        current = best_by_key.get(key)
        if current is None or assist_duplicate_card_set_choice_key(
            candidate,
            prefer_low_number,
        ) > assist_duplicate_card_set_choice_key(current[1], prefer_low_number):
            best_by_key[key] = (index, candidate)

    kept = passthrough + list(best_by_key.values())
    kept.sort(key=lambda item: item[0])
    return [candidate for _, candidate in kept]

def finalize_assist_candidates(
    candidates: list[dict],
    limit: int,
    source: str,
    truncated: bool,
    scan_limit: Optional[int] = None,
    order: str = "weak",
    prefer_low_number: bool = False,
    source_cards: Optional[list[dict]] = None,
    room: Optional[Room] = None,
) -> dict:
    candidates = deduplicate_duplicate_card_set_assist_candidates(
        candidates,
        prefer_low_number=prefer_low_number,
    )
    remaining_finish_exists = any(
        candidate.get("finishes_remaining")
        for candidate in candidates
    )
    if order == "recommended":
        candidates = rank_recommended_assist_candidates(
            candidates,
            source_cards or [],
            reverse_order=bool(room and room.reverse_order),
        )
    elif order == "efficient":
        candidates = sorted(
            candidates,
            key=lambda candidate: (
                -assist_efficiency_score(candidate),
                -candidate["number"],
                len(candidate.get("cards") or []),
                candidate.get("joker_count", 0),
                candidate.get("visible_text", ""),
            ),
        )

    if order != "recommended":
        # 57 and 1729 are always useful special plays, even when they are not
        # registered. Keep at least one realization of each available special
        # number when the ordinary candidate limit is applied.
        required_special_ids = []
        seen_special_effects = set()
        for candidate in candidates:
            special_effect = candidate.get("special_effect")
            if special_effect and special_effect not in seen_special_effects:
                seen_special_effects.add(special_effect)
                required_special_ids.append(id(candidate))
        required_special_id_set = set(required_special_ids)
        optional_limit = max(0, limit - len(required_special_ids))
        optional_ids = [
            id(candidate)
            for candidate in candidates
            if id(candidate) not in required_special_id_set
        ][:optional_limit]
        kept_ids = required_special_id_set | set(optional_ids)
        limited_candidates = [
            candidate
            for candidate in candidates
            if id(candidate) in kept_ids
        ]
        truncated = truncated or len(limited_candidates) < len(candidates)
        candidates = limited_candidates
    payload = {
        "candidates": candidates,
        "truncated": truncated,
        "source": source,
        "remaining_finish_exists": remaining_finish_exists,
    }
    if scan_limit is not None:
        payload["scan_limit"] = scan_limit
    return payload

def _build_prime_assist_candidates(player: "Player", room: Room, data: dict) -> dict:
    if (
        not room.rule.registration_enabled
        or not room.rule.assist_enabled
    ):
        return {"candidates": [], "truncated": False, "source": "hand"}

    composite_practice = (
        room.rule.move_policy is MovePolicy.COMPOSITE_ONLY_WITH_SMALL_HAND_FINISH
    )
    selected_ids = data.get("selected_card_ids") or []
    if not isinstance(selected_ids, list):
        selected_ids = []
    composite_ids = data.get("composite_card_ids") or []
    if not isinstance(composite_ids, list):
        composite_ids = []

    hand_by_id = {card["card_id"]: card for card in player.hand}
    selected_and_composite_ids = []
    for cid in selected_ids + composite_ids:
        if cid in hand_by_id and cid not in selected_and_composite_ids:
            selected_and_composite_ids.append(cid)
    selected_id_set = set(selected_and_composite_ids)
    target_scope = assist_filter_value(data, "target_scope", "auto")
    if target_scope == "selected":
        source_cards = [hand_by_id[cid] for cid in selected_and_composite_ids]
        source = "selected"
    elif target_scope == "unselected":
        source_cards = [card for card in player.hand if card["card_id"] not in selected_id_set]
        source = "unselected"
    elif target_scope == "all":
        source_cards = player.hand[:]
        source = "all"
    else:
        source_cards = [hand_by_id[cid] for cid in selected_and_composite_ids]
        source = "selected" if source_cards else "unselected"
    if not source_cards and source != "selected":
        source_cards = player.hand[:]
        source = "unselected"

    count_scope = assist_filter_value(data, "count_scope", "field")
    kind_scope = assist_filter_value(data, "kind_scope", "all")
    order = assist_filter_value(data, "order", "weak")
    specified_card_count = assist_card_count_from_filters(data) if count_scope == "specified" else None
    required_card_count = None
    if room.field and count_scope == "field":
        required_card_count = len(room.field)
    elif count_scope == "specified":
        required_card_count = specified_card_count
    apply_field_value_filter = count_scope == "field"
    limit = assist_limit_from_filters(data)
    scan_limit = (
        ASSIST_SCAN_LIMITS["fifty"]
        if order == "recommended"
        else assist_scan_limit_from_filters(data)
    )
    recommendation_cache_key = None
    if order == "recommended":
        recommendation_cache_key = assist_recommendation_cache_key(
            player,
            room,
            source_cards,
            selected_ids,
            composite_ids,
            count_scope,
            kind_scope,
            specified_card_count,
        )
        cached = getattr(player, "_assist_recommendation_cache", None)
        if cached and cached.get("key") == recommendation_cache_key:
            return copy.deepcopy(cached["payload"])

    generation_required_card_count = (
        None
        if order == "recommended"
        else required_card_count
    )
    generation_apply_field_value_filter = (
        False
        if order == "recommended"
        else apply_field_value_filter
    )
    can_realize_number_text = build_assist_number_text_filter(
        source_cards,
        generation_required_card_count,
    )

    candidates = []
    scanned = 0
    registered_numbers = []
    if (
        kind_scope in ("all", "prime")
        and (
            not composite_practice
            or len(player.hand) <= room.rule.normal_finish_max_hand_size
        )
    ):
        registered_numbers.extend(
            ("prime", number, None)
            for number in player.registered_primes
            if number not in SPECIAL_ASSIST_EFFECTS
        )
    if room.rule.allow_composite and kind_scope in ("all", "composite"):
        registered_numbers.extend(
            ("composite", entry.value, entry)
            for entry in player.registered_composite_entries
            if entry.value in player.registered_composites
            and (composite_practice or entry.value not in SPECIAL_ASSIST_EFFECTS)
        )
    registered_numbers.sort(key=assist_number_sort_key(room, order))

    infinity_allowed_by_count = (
        generation_required_card_count is None
        or generation_required_card_count == 1
    ) and (not composite_practice or len(player.hand) == 1)
    if infinity_allowed_by_count:
        infinity_card = next(
            (
                card
                for card in source_cards
                if card.get("is_joker") or card.get("suit") == "X"
            ),
            None,
        )
        if infinity_card is not None:
            infinity_candidate = {
                "number": 0,
                "cards": [infinity_card],
                "assigned_numbers": ["inf"],
                "visible_text": "X",
                "joker_count": 1,
                "kind": "special",
                "special_effect": "infinity",
                "field_count_match": (
                    count_scope == "specified"
                    or not room.field
                    or len(room.field) == 1
                ),
            }
            infinity_candidate["finishes_hand"] = len(
                remove_cards_by_id(player.hand, infinity_candidate["cards"])
            ) == 0
            infinity_candidate["finishes_remaining"] = (
                bool(selected_id_set)
                and {
                    infinity_card["card_id"]
                } == {
                    card["card_id"]
                    for card in player.hand
                    if card["card_id"] not in selected_id_set
                }
            )
            if order == "recommended":
                infinity_candidate["_assist_legal"] = assist_candidate_is_legal_for_filters(
                    room,
                    infinity_candidate,
                    count_scope,
                    specified_card_count,
                )
            candidates.append(infinity_candidate)

    for number, special_effect in (() if composite_practice else SPECIAL_ASSIST_EFFECTS.items()):
        if generation_apply_field_value_filter and not field_allows_number_value(room, number):
            continue
        if not can_realize_number_text(number):
            continue
        realizations = find_prime_realizations(
            number,
            source_cards,
            generation_required_card_count,
            limit=ASSIST_REALIZATIONS_PER_NUMBER,
        )
        for realization in realizations:
            if is_single_joker_realization(realization):
                continue
            if (
                generation_apply_field_value_filter
                and not field_allows_number(room, number, len(realization["cards"]))
            ):
                continue
            realization["kind"] = "special"
            realization["special_effect"] = special_effect
            realization["field_count_match"] = (
                count_scope == "specified"
                or not room.field
                or len(realization["cards"]) == len(room.field)
            )
            realization["finishes_hand"] = len(
                remove_cards_by_id(player.hand, realization["cards"])
            ) == 0
            realization["finishes_remaining"] = (
                bool(selected_id_set)
                and {
                    card["card_id"]
                    for card in realization["cards"]
                } == {
                    card["card_id"]
                    for card in player.hand
                    if card["card_id"] not in selected_id_set
                }
            )
            if order == "recommended":
                realization["_assist_legal"] = assist_candidate_is_legal_for_filters(
                    room,
                    realization,
                    count_scope,
                    specified_card_count,
                )
            candidates.append(realization)

    scan_truncated = False
    for kind, number, entry in registered_numbers:
        if generation_apply_field_value_filter and not field_allows_number_value(room, number):
            continue
        if not can_realize_number_text(number):
            continue

        scanned += 1
        if scanned > scan_limit:
            scan_truncated = True
            break
        realizations = find_prime_realizations(
            number,
            source_cards,
            generation_required_card_count,
            limit=ASSIST_REALIZATIONS_PER_NUMBER,
        )
        for realization in realizations:
            # A joker played by itself is always infinity. It cannot represent
            # a registered prime or the visible value of a composite play.
            if is_single_joker_realization(realization):
                continue
            if (
                generation_apply_field_value_filter
                and not field_allows_number(room, number, len(realization["cards"]))
            ):
                continue
            realization["kind"] = kind
            if composite_practice and kind == "composite" and number in SPECIAL_ASSIST_EFFECTS:
                realization["special_effect"] = SPECIAL_ASSIST_EFFECTS[number]
            realization["field_count_match"] = (
                count_scope == "specified"
                or not room.field
                or len(realization["cards"]) == len(room.field)
            )
            if order == "recommended":
                realization["_assist_legal"] = assist_candidate_is_legal_for_filters(
                    room,
                    realization,
                    count_scope,
                    specified_card_count,
                )
            if kind != "composite":
                realization["finishes_hand"] = len(remove_cards_by_id(player.hand, realization["cards"])) == 0
                realization["finishes_remaining"] = (
                    bool(selected_id_set)
                    and {
                        card["card_id"]
                        for card in realization["cards"]
                    } == {
                        card["card_id"]
                        for card in player.hand
                        if card["card_id"] not in selected_id_set
                    }
                )
                candidates.append(realization)
                continue

            material_pool = source_cards if source in ("selected", "unselected", "all") else player.hand
            material_source = remove_cards_by_id(material_pool, realization["cards"])
            expression = find_composite_expression_realization(entry, material_source)
            if expression is None:
                continue
            realization["expression"] = entry.expression
            realization["composite"] = expression
            realization["material_text"] = assist_card_text(
                expression["cards"],
                expression["assigned_numbers"],
            )
            used_cards = realization["cards"] + expression["cards"]
            realization["finishes_hand"] = len(remove_cards_by_id(player.hand, used_cards)) == 0
            realization["finishes_remaining"] = (
                bool(selected_id_set)
                and {
                    card["card_id"]
                    for card in used_cards
                } == {
                    card["card_id"]
                    for card in player.hand
                    if card["card_id"] not in selected_id_set
                }
            )
            candidates.append(realization)

    if composite_practice:
        hand_ids = {card["card_id"] for card in player.hand}
        candidates = [
            candidate
            for candidate in candidates
            if candidate.get("kind") == "composite"
            or {
                card["card_id"]
                for card in candidate.get("cards", [])
            } == hand_ids
        ]

    result = finalize_assist_candidates(
        candidates,
        limit,
        source,
        truncated=scan_truncated,
        scan_limit=scan_limit if scan_truncated else None,
        order=order,
        prefer_low_number=room.reverse_order,
        source_cards=source_cards,
        room=room,
    )
    if recommendation_cache_key is not None:
        player._assist_recommendation_cache = {
            "key": recommendation_cache_key,
            "payload": copy.deepcopy(result),
        }
    return result

def build_prime_assist_candidates(player: "Player", room: Room, data: dict) -> dict:
    result = _build_prime_assist_candidates(player, room, data)
    selected_ids = data.get("selected_card_ids") or []
    composite_ids = data.get("composite_card_ids") or []
    if not isinstance(selected_ids, list):
        selected_ids = []
    if not isinstance(composite_ids, list):
        composite_ids = []
    selected_id_set = {
        card_id
        for card_id in selected_ids + composite_ids
        if isinstance(card_id, str)
    }
    if not selected_id_set or result.get("source") == "unselected":
        return result

    filters = data.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    if filters.get("order") == "recommended":
        return result
    probe_data = {
        **data,
        "filters": {
            **filters,
            "target_scope": "unselected",
        },
        "limit": 1,
    }
    remaining_result = _build_prime_assist_candidates(player, room, probe_data)
    result["remaining_finish_exists"] = bool(
        result.get("remaining_finish_exists")
        or remaining_result.get("remaining_finish_exists")
    )
    return result


def assist_snapshot_signature(player: "Player", room: Room) -> tuple:
    def card_signature(card: dict) -> tuple:
        return (
            str(card.get("card_id")),
            card.get("rank"),
            card.get("suit"),
            bool(card.get("is_joker")),
        )

    return (
        tuple(card_signature(card) for card in player.hand),
        tuple(card_signature(card) for card in room.field),
        room.last_number,
        bool(room.reverse_order),
        tuple(sorted(int(number) for number in player.registered_primes)),
        tuple(sorted(int(number) for number in player.registered_composites)),
        tuple(
            sorted(
                (int(entry.value), str(entry.expression))
                for entry in player.registered_composite_entries
            )
        ),
    )


async def build_prime_assist_candidates_nonblocking(
    player: "Player",
    room: Room,
    data: dict,
) -> dict:
    """Build assist data without monopolizing the WebSocket event loop."""
    initial_signature = assist_snapshot_signature(player, room)
    player_snapshot = copy.copy(player)
    player_snapshot.hand = copy.deepcopy(player.hand)
    player_snapshot.registered_primes = set(player.registered_primes)
    player_snapshot.registered_composites = set(player.registered_composites)
    player_snapshot.registered_composite_entries = tuple(
        player.registered_composite_entries
    )
    if hasattr(player, "_assist_recommendation_cache"):
        player_snapshot._assist_recommendation_cache = copy.deepcopy(
            player._assist_recommendation_cache
        )

    room_snapshot = copy.copy(room)
    room_snapshot.field = copy.deepcopy(room.field)
    data_snapshot = copy.deepcopy(data)
    result = await asyncio.to_thread(
        build_prime_assist_candidates,
        player_snapshot,
        room_snapshot,
        data_snapshot,
    )

    # Reuse the recommendation cache only when neither hand nor field changed
    # while the worker was running. A stale result can still be discarded by
    # the client's assist_request_id, but must never replace a fresh cache.
    if (
        getattr(player, "room", room) is room
        and assist_snapshot_signature(player, room) == initial_signature
        and hasattr(player_snapshot, "_assist_recommendation_cache")
    ):
        player._assist_recommendation_cache = copy.deepcopy(
            player_snapshot._assist_recommendation_cache
        )
    return result
################################################
# Webhook
################################################

_discord_join_notify_times = deque()
_discord_join_notify_suppressed = 0
_discord_join_notify_lock = asyncio.Lock()

async def notify_discord(content: str):
    if not WEBHOOK_URL:
        print("⚠️ Webhook URL が設定されていません")
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(WEBHOOK_URL, json={
                "content": content,
                "allowed_mentions": {"parse": []},
            })
            response.raise_for_status()
        return True
    except Exception as e:
        # エラーをハンドリング
        print("notify_discord failed:", e)
        return False


def reserve_discord_join_notification(now: float | None = None) -> tuple[bool, int]:
    global _discord_join_notify_suppressed
    if now is None:
        now = time.monotonic()

    cutoff = now - DISCORD_JOIN_NOTIFY_WINDOW_SECONDS
    while _discord_join_notify_times and _discord_join_notify_times[0] <= cutoff:
        _discord_join_notify_times.popleft()

    if DISCORD_JOIN_NOTIFY_LIMIT <= 0:
        return False, 0

    if len(_discord_join_notify_times) >= DISCORD_JOIN_NOTIFY_LIMIT:
        _discord_join_notify_suppressed += 1
        return False, 0

    _discord_join_notify_times.append(now)
    suppressed = _discord_join_notify_suppressed
    _discord_join_notify_suppressed = 0
    return True, suppressed


def discord_join_notification_content(player_name: str, room: Room) -> str:
    room_meta = global_chat_room_meta(room)
    room_label = room_meta["room_badge"]
    if room.category == "Neo":
        icon = "🟢"
        product_name = "素数大富豪NEO"
        client_url = NEO_CLIENT_URL
    elif room.category in {"Classic", "Plus"}:
        icon = "🔵"
        product_name = "素数大富豪＋"
        client_url = PLUS_CLIENT_URL
    else:
        icon = "🟠"
        product_name = "素数大富豪＋ 旧UI"
        client_url = LEGACY_CLIENT_URL
        if room.category == "Events" and room.room_id.startswith("event_"):
            room_label = f"Events・ルーム{room.room_id.removeprefix('event_')}"

    return (
        f"{icon} **{product_name}**\n"
        f"🎮 {player_name} が **{room_label}** に参加しました\n"
        f"ルール: {room.rule.label}\n"
        f"▶ {client_url}"
    )


async def notify_discord_join(player_name: str, room: Room):
    if not room_discord_join_notifications_enabled(room):
        return
    async with _discord_join_notify_lock:
        should_send, suppressed = reserve_discord_join_notification()

    if not should_send:
        return

    content = discord_join_notification_content(player_name, room)
    if suppressed:
        content = f"{content}\n（直近の入室通知 {suppressed} 件を省略しました）"
    await notify_discord(content)


def discord_safe_text(value: object) -> str:
    text_value = str(value or "")
    for character in ("\\", "*", "_", "~", "`", ">", "|"):
        text_value = text_value.replace(character, f"\\{character}")
    return text_value


def discord_recruitment_notification_content(
    event: RecruitmentNotification,
) -> str:
    product_name = "素数大富豪NEO" if event.board_key == "neo" else "素数大富豪＋"
    client_url = NEO_CLIENT_URL if event.board_key == "neo" else PLUS_CLIENT_URL
    player_name = discord_safe_text(event.name)
    rule_label = discord_safe_text(RECRUITMENT_RULE_LABELS.get(event.rule_key, event.rule_key))
    scheduled_timestamp = int(event.scheduled_at.timestamp())
    pair_id = event.recruitment_id.split("-", 1)[0]
    common = (
        f"👤 {player_name}\n"
        f"🎴 希望: **{rule_label}**\n"
        f"🕐 集合: <t:{scheduled_timestamp}:F>（<t:{scheduled_timestamp}:R>）\n"
        f"🔖 募集ID: `{pair_id}`"
    )
    if event.event_type == "created":
        return (
            f"📣 **対戦募集 / {product_name}**\n"
            f"{common}\n"
            f"▶ {client_url}"
        )
    resolution = (
        "投稿者が募集を削除しました。"
        if event.resolution_reason == "deleted"
        else "集合時間になりました。"
    )
    return (
        f"✅ **募集終了 / {product_name}**\n"
        f"{common}\n"
        f"{resolution}"
    )


async def recruitment_notification_loop():
    while True:
        try:
            await RECRUITMENT_STORE.expire_due()
            events = await RECRUITMENT_STORE.pending_notifications(limit=10)
            for event in events:
                delivered = await notify_discord(
                    discord_recruitment_notification_content(event)
                )
                if delivered:
                    await RECRUITMENT_STORE.mark_notification_delivered(event.event_id)
                    continue
                retry_seconds = min(3600, 15 * (2 ** min(event.attempt_count, 8)))
                await RECRUITMENT_STORE.mark_notification_failed(
                    event.event_id,
                    next_attempt_at=utc_now() + timedelta(seconds=retry_seconds),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"recruitment notification loop failed: {exc}")
        await asyncio.sleep(5)

################################################
# CPU処理
################################################

def current_turn_player(room: Room):
    return next((p for p in room.players if p.id == room.current_turn_id), None)


def human_players(room: Room):
    return [p for p in room.players if not is_cpu_player(p)]


def is_talkative_fish_cpu(player) -> bool:
    return is_cpu_player(player) and getattr(player, "cpu_key", None) == "talkative_fish"


def talkative_fish_cpus(room: Room):
    return [p for p in room.players if is_talkative_fish_cpu(p)]


TALKATIVE_FISH_GAME_OVER_MESSAGES = (
    "JやKなどの強いカードを無計画に使うと、後でウオう左往することになるウオ",
    "KKJとKKQTJの強さはこウオつ付け難いウオ",
    "好きな数字の並びを含む素数は大きさの割に覚えやすいウオ",
    "グロタンカットをした後はもう一度ドローできるウオ",
    "QQから始まる3枚出しは素数にならないウオ",
    "同じ枚数でも、絵札が多い素数ほど桁数が多くなるウオ",
    "これが素数なら勝てるのに、と思った組み合わせは知らなくても出してみる価値があるウオ",
    "好きな食べ物はサーロインステーキだウオ",
    "「ギョギョって言って」……？　そんな恐れ多い真似はできないウオ……",
    "ピヨ……？　何のことだウオ……？",
)


async def log_talkative_fish_message(room: Room, cpu, message: str) -> None:
    await room.log_chat(message, sender=getattr(cpu, "name", "饒舌な魚CPU"))


async def log_talkative_fish_join(room: Room, cpu) -> None:
    if is_talkative_fish_cpu(cpu):
        await log_talkative_fish_message(room, cpu, "よろしくお願いしますウオ")


async def log_talkative_fish_leave(room: Room, cpu) -> None:
    if is_talkative_fish_cpu(cpu):
        await log_talkative_fish_message(room, cpu, "ありがとうございましたウオ")


def talkative_fish_sashimi_text(*texts) -> Optional[str]:
    for text in texts:
        value = str(text or "")
        if "343" in value:
            return value.replace("343", "刺身") + "ウオ"
    return None


async def maybe_log_talkative_fish_sashimi(room: Room, *texts) -> None:
    message = talkative_fish_sashimi_text(*texts)
    if message is None:
        return
    for cpu in talkative_fish_cpus(room):
        await log_talkative_fish_message(room, cpu, message)


def talkative_fish_turn_start_text(cpu, room: Room) -> Optional[str]:
    if not is_talkative_fish_cpu(cpu):
        return None
    if choose_gold_finish_candidate(cpu, room, is_valid_prime_for_player) is not None:
        return "よしウオ"
    opponents = [
        player
        for player in get_active_players(room)
        if player.id != cpu.id
    ]
    if any(len(getattr(player, "hand", [])) <= 3 for player in opponents):
        return "少しきびしくなってきたウオ"
    if len(getattr(cpu, "hand", [])) >= 12:
        return "まだまだこれからウオ"
    return None


async def maybe_log_talkative_fish_turn_start(room: Room, cpu) -> None:
    message = talkative_fish_turn_start_text(cpu, room)
    if message is not None:
        await log_talkative_fish_message(room, cpu, message)


async def maybe_log_talkative_fish_game_over(room: Room) -> None:
    for cpu in talkative_fish_cpus(room):
        await log_talkative_fish_message(
            room,
            cpu,
            random.choice(TALKATIVE_FISH_GAME_OVER_MESSAGES),
        )


async def remove_cpus_if_no_humans(room: Room) -> bool:
    if human_players(room):
        return False
    cpus = [p for p in room.players if is_cpu_player(p)]
    if not cpus:
        return False
    for cpu in cpus:
        await log_talkative_fish_leave(room, cpu)
    for cpu in cpus:
        room.players.remove(cpu)
        cpu.room = None
        cpu.status = "watching"
        cpu.clear_hand()
    await room.log_chat("人間のプレイヤーがいなくなったためCPUが退室しました")
    await room.update_room_status()
    return True


async def handle_room_after_player_removed(room: Room, departed_player_id: str | None = None) -> None:
    if room.state == "playing":
        active_players = get_active_players(room)
        if len(active_players) == 1:
            winner_name = active_players[0].name
            room.state = "waiting"
            room.current_turn_id = None
            await room.broadcast({"type": "game_over", "winner": winner_name, "state": room.state})
            await room.log_chat(f"{winner_name}が勝利しました")
            await maybe_log_talkative_fish_game_over(room)
            await publish_score_log(room, winner_name)
        elif len(active_players) == 0:
            room.state = "waiting"
            room.current_turn_id = None
            await room.broadcast({"type": "game_over", "winner": None, "state": room.state})
            await room.log_chat("対戦者がいなくなったためゲームを終了しました")
            await maybe_log_talkative_fish_game_over(room)
            await publish_score_log(room, None)
        elif departed_player_id is not None and room.current_turn_id == departed_player_id:
            await next_turn(room)

    if await remove_cpus_if_no_humans(room):
        return
    await room.update_room_status()


async def add_cpu_to_room(room: Room, cpu_key: str = "basic", name: str | None = None) -> CpuPlayer:
    profile = get_cpu_profile(cpu_key)
    if profile is None:
        raise ValueError("unknown cpu profile")
    if not profile.supports_rule(room.rule):
        raise ValueError("cpu profile does not support this rule")
    cpu_count = sum(1 for p in room.players if is_cpu_player(p))
    base_name = name or profile.label
    cpu = CpuPlayer(
        name=f"{base_name}{cpu_count + 1}" if cpu_count else base_name,
        cpu_key=profile.key,
    )
    cpu.room = room
    cpu.status = "waiting"
    apply_cpu_knowledge(cpu, room, profile)
    room.players.append(cpu)
    await room.log_chat(f"{cpu.name}が入室しました")
    await log_talkative_fish_join(room, cpu)
    await room.update_room_status()
    return cpu


def apply_cpu_knowledge(cpu: CpuPlayer, room: Room, profile: CpuProfile) -> None:
    knowledge = profile.knowledge
    if knowledge.load_timing == "never":
        return
    if knowledge.load_timing == "registration" and not room.rule.registration_enabled:
        return

    if knowledge.source == "sample":
        if SAMPLE_REGISTERED_PRIMES or SAMPLE_REGISTERED_COMPOSITES:
            load_sample_registered_prime_payload(cpu)
        return

    if knowledge.source == "gold":
        load_sample_registered_prime_payload(cpu, sample_key="gold_prime_table")
        return

    if knowledge.source == "sample_key":
        load_sample_registered_prime_payload(cpu, sample_key=knowledge.sample_key)
        return

    if knowledge.source == "fish_silver":
        load_sample_registered_prime_payload(cpu, sample_key=knowledge.sample_key or "silver_prime_table")
        cpu.replace_registered_primes(set(cpu.registered_primes) | set(fish_extra_prime_values()))
        return

    if knowledge.source == "inline":
        replace_player_registered_numbers_from_text(
            cpu,
            knowledge.prime_text,
            knowledge.composite_text,
        )


async def remove_cpu_from_room(room: Room) -> bool:
    cpu = next((p for p in room.players if is_cpu_player(p)), None)
    if cpu is None:
        return False
    room.players.remove(cpu)
    await log_talkative_fish_leave(room, cpu)
    cpu.room = None
    cpu.status = "watching"
    cpu.clear_hand()
    await room.log_chat(f"{cpu.name}が退室しました")
    await room.update_room_status()
    return True


async def maybe_schedule_cpu_turn(room: Room) -> None:
    if room.state != "playing":
        return
    current = current_turn_player(room)
    if not is_cpu_player(current):
        return
    if getattr(room, "cpu_turn_running", False):
        return
    room.cpu_turn_running = True
    asyncio.create_task(run_cpu_turn(room, current))


async def run_cpu_turn(room: Room, cpu: CpuPlayer) -> None:
    try:
        await asyncio.sleep(0.8)
        if room.state != "playing" or room.current_turn_id != cpu.id or cpu not in room.players:
            return

        await maybe_log_talkative_fish_turn_start(room, cpu)
        action = choose_profile_cpu_action(cpu, room, validator=is_valid_prime_for_player)
        await execute_cpu_action(room, cpu, action)
        if action.kind == "draw":
            followup = choose_profile_cpu_action(cpu, room, validator=is_valid_prime_for_player)
            if followup.kind in ("play_prime", "play_composite") and room.current_turn_id == cpu.id:
                await asyncio.sleep(0.4)
                await execute_cpu_action(room, cpu, followup)
            elif room.current_turn_id == cpu.id:
                await asyncio.sleep(0.4)
                await pass_turn_for_player(cpu, room)
    finally:
        room.cpu_turn_running = False
        if room.state == "playing" and room.current_turn_id == cpu.id:
            await maybe_schedule_cpu_turn(room)


async def execute_cpu_action(room: Room, cpu: CpuPlayer, action) -> None:
    if action.kind == "play_prime":
        await handle_prime_play(cpu, room, action.payload)
        return
    if action.kind == "play_composite":
        if not room.rule.allow_composite:
            await pass_turn_for_player(cpu, room)
            return
        payload = {"mode": "composite", **action.payload}
        await handle_composite_play(cpu, room, payload)
        return
    if action.kind == "draw":
        await draw_card_for_player(cpu, room)
        return
    await pass_turn_for_player(cpu, room)


async def draw_card_for_player(player, room: Room) -> bool:
    if room.has_drawn:
        await player.send_json({"type": "error", "message": "このターンはすでにドロー済みです。"})
        return False
    if not room.deck:
        return False

    drawn = room.deck.pop(0)
    player.add_card(drawn)
    record_score_line(room, f"{player.name}:{score_state_prefix(room)}D({score_card_symbol(drawn)})")
    await player.send_hand_update()
    await room.update_game_state()
    room.has_drawn = True
    return True


async def pass_turn_for_player(player, room: Room) -> None:
    await player.send_hand_update()
    flow_field(room)
    await room.update_game_state()
    await room.broadcast({
        "type": "action_result",
        "action": "pass",
        "player_id": player.id
    })
    await room.log_chat(f"{player.name}がパスしました")
    record_score_play_line(room, player, f"{score_state_prefix(room)}%")
    await next_turn(room)

################################################
# WebSocket処理
################################################

@app.websocket("/ws/neo")
@app.websocket("/ws/plus")
@app.websocket("/ws/plus-practice")
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_surface = WEBSOCKET_CLIENT_SURFACE_BY_PATH.get(websocket.scope.get("path"), "legacy")
    await websocket.accept()
    player = Player(websocket)  # 辞書ではなくPlayerクラスのインスタンスを生成
    player.client_surface = client_surface

    try:
        # 自分のIDを通知
        await websocket.send_json({"type": "your_id", "id": player.id, "name": player.name})

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            room_id = player.room.room_id if player.room else None

            if (
                client_surface == "plus_practice"
                and msg_type != "authorize_composite_practice"
                and not player.composite_practice_authorized
            ):
                await player.send_json({
                    "type": "error",
                    "code": "practice_authorization_required",
                    "message": "先に練習部屋の認証を行ってください。",
                })
                continue

            if msg_type == "authorize_composite_practice":
                authorized = (
                    client_surface == "plus_practice"
                    and composite_practice_authorized(data.get("access_token"))
                )
                player.composite_practice_authorized = authorized
                await player.send_json({
                    "type": "composite_practice_authorization",
                    "authorized": authorized,
                    "message": "認証しました。" if authorized else "アクセストークンが正しくありません。",
                })
                if authorized:
                    await player.send_json(room_counts_payload(
                        client_surface,
                        practice_authorized=True,
                    ))
                continue
            elif msg_type == "get_composite_practice_stats":
                if client_surface != "plus_practice" or not player.composite_practice_authorized:
                    await player.send_json({
                        "type": "error",
                        "code": "practice_authorization_required",
                        "message": "合成数カウントの閲覧には練習部屋の認証が必要です。",
                    })
                    continue
                await player.send_json(await composite_practice_stats_payload())
                continue
            elif msg_type == "set_name":
                requested_name = data.get("name", "")
                if not isinstance(requested_name, str):
                    await player.send_json({
                        "type": "error",
                        "code": "invalid_player_name",
                        "message": "表示名を文字列で入力してください。",
                    })
                    continue
                requested_name = requested_name.strip()
                if len(requested_name) > MAX_PLAYER_NAME_LENGTH:
                    await player.send_json({
                        "type": "error",
                        "code": "invalid_player_name",
                        "message": f"表示名は{MAX_PLAYER_NAME_LENGTH}文字以内にしてください。",
                    })
                    continue
                player.name = requested_name or player.name
                # 必要なら acknowledgment を返す
                await player.send_json({"type": "name_set", "id": player.id, "name": player.name})
                if player.room:
                    await player.room.update_room_status()
                continue
            elif msg_type == "get_tournament_status":
                run = tournament_for_player(player) or TOURNAMENT_RUNS_BY_ROOM.get(TOURNAMENT_ROOM_ID)
                await player.send_json({
                    "type": "tournament_update",
                    "tournament": tournament_public_payload(run, player.tournament_participant_id),
                })
                continue
            elif msg_type == "tournament_register":
                if not is_tournament_managed_room(player.room):
                    await player.send_json({"type": "error", "message": "plus大会ルームへ入室してから登録してください。"})
                    continue
                try:
                    player = await register_or_resume_tournament_player(player, data)
                except TournamentSessionConflict as exc:
                    await player.send_json({
                        "type": "tournament_session_conflict",
                        "message": str(exc),
                        "run_id": exc.run.run_id,
                        "participant_id": exc.participant_id,
                        "tournament": tournament_public_payload(exc.run),
                    })
                except ValueError as exc:
                    await player.send_json({"type": "error", "code": "tournament_registration", "message": str(exc)})
                continue
            elif msg_type == "tournament_withdraw":
                try:
                    await withdraw_tournament_player(player)
                except ValueError as exc:
                    await player.send_json({"type": "error", "code": "tournament_withdraw", "message": str(exc)})
                continue
            elif msg_type == "tournament_match_ready":
                try:
                    await mark_tournament_match_ready(player, str(data.get("match_id", "")))
                except ValueError as exc:
                    await player.send_json({"type": "error", "code": "tournament_match_ready", "message": str(exc)})
                continue
            elif msg_type == "tournament_watch_match":
                try:
                    await watch_tournament_match(player, str(data.get("match_id", "")))
                except ValueError as exc:
                    await player.send_json({"type": "error", "code": "tournament_watch_match", "message": str(exc)})
                continue
            elif msg_type == "tournament_lobby_chat":
                run = tournament_for_player(player) or TOURNAMENT_RUNS_BY_ROOM.get(TOURNAMENT_ROOM_ID)
                if run is None or not is_tournament_managed_room(player.room):
                    await player.send_json({"type": "error", "code": "tournament_lobby_chat", "message": "大会ロビーへ入室してください。"})
                    continue
                message = normalize_chat_message(data.get("message"))
                if message is None:
                    await player.send_json({
                        "type": "error",
                        "code": "invalid_chat_message",
                        "message": f"メッセージは1〜{MAX_CHAT_MESSAGE_LENGTH}文字で入力してください。",
                    })
                    continue
                await broadcast_tournament_lobby(run, {
                    "type": "chat",
                    "sender": player.name,
                    "message": message,
                })
                continue
            elif msg_type == "tournament_admin_schedule":
                if not tournament_admin_authorized(data.get("admin_token")):
                    await player.send_json({"type": "error", "code": "tournament_admin", "message": "管理トークンが正しくありません。"})
                    continue
                try:
                    run = await schedule_tournament(data, actor="admin")
                except (TypeError, ValueError) as exc:
                    await player.send_json({"type": "error", "code": "tournament_admin", "message": str(exc)})
                    continue
                await player.send_json({
                    "type": "tournament_admin_result",
                    "action": "schedule",
                    "tournament": tournament_public_payload(run),
                })
                continue
            elif msg_type in {"tournament_admin_resolve", "tournament_admin_skip"}:
                if not tournament_admin_authorized(data.get("admin_token")):
                    await player.send_json({"type": "error", "code": "tournament_admin", "message": "管理トークンが正しくありません。"})
                    continue
                run = TOURNAMENT_RUNS_BY_ROOM.get(str(data.get("room_id", TOURNAMENT_ROOM_ID)))
                if run is None:
                    await player.send_json({"type": "error", "code": "tournament_admin", "message": "大会が見つかりません。"})
                    continue
                match_id = str(data.get("match_id") or (run.current_match_id or ""))
                winner_id = None if msg_type == "tournament_admin_skip" else data.get("winner_id")
                try:
                    await resolve_tournament_match(
                        run,
                        match_id,
                        str(winner_id) if winner_id else None,
                        resolution="admin_override" if winner_id else "admin_skip",
                        actor="admin",
                    )
                except ValueError as exc:
                    await player.send_json({"type": "error", "code": "tournament_admin", "message": str(exc)})
                    continue
                await player.send_json({
                    "type": "tournament_admin_result",
                    "action": "resolve" if winner_id else "skip",
                    "tournament": tournament_public_payload(run),
                })
                continue
            elif msg_type == "tournament_admin_history":
                if not tournament_admin_authorized(data.get("admin_token")):
                    await player.send_json({"type": "error", "code": "tournament_admin", "message": "管理トークンが正しくありません。"})
                    continue
                recent_runs = await TOURNAMENT_STORE.load_recent_runs(room_id=TOURNAMENT_ROOM_ID, limit=20)
                await player.send_json({
                    "type": "tournament_admin_history",
                    "runs": [tournament_public_payload(run) for run in recent_runs],
                })
                continue
            elif msg_type in ("set_registered_numbers", "set_registered_primes"):
                if player.room and player.room.state == "playing":
                    await player.send_json({
                        "type": "error",
                        "message": "対戦中は登録内容を変更できません。",
                    })
                    continue

                prime_text = data.get("prime_text", data.get("text", ""))
                composite_text = data.get("composite_text", "")
                if not isinstance(prime_text, str) or not isinstance(composite_text, str):
                    await player.send_json({
                        "type": "error",
                        "message": "登録内容の入力形式が不正です。",
                    })
                    continue

                await player.send_json(replace_player_registered_numbers_from_text(
                    player,
                    prime_text,
                    composite_text,
                    limit=player.room.rule.registered_number_limit if player.room else None,
                ))

                if player.room:
                    await player.room.update_room_status()
                continue
            elif msg_type == "load_sample_registered_primes":
                if player.room and player.room.state == "playing":
                    await player.send_json({
                        "type": "error",
                        "message": "対戦中は登録内容を変更できません。",
                    })
                    continue
                sample_key = data.get("sample_key", DEFAULT_REGISTERED_SAMPLE_KEY)
                if not isinstance(sample_key, str):
                    sample_key = DEFAULT_REGISTERED_SAMPLE_KEY
                sample = registered_sample_for_key(sample_key)
                if not player_can_load_registered_sample(player, sample):
                    await player.send_json({
                        "type": "error",
                        "code": "registered_sample_forbidden",
                        "message": "このサンプルを読み込む権限がありません。",
                    })
                    continue
                sample_data = sample["data"] if sample else ((), (), (), "", "")
                if not sample_data[0] and not sample_data[1]:
                    await player.send_json({
                        "type": "error",
                        "message": "サンプル登録メモリがサーバーに見つかりません。",
                    })
                    continue

                await player.send_json(load_sample_registered_prime_payload(
                    player,
                    sample_key,
                    limit=player.room.rule.registered_number_limit if player.room else None,
                ))

                if player.room:
                    await player.room.update_room_status()
                continue
            elif msg_type == "get_room_counts":
                await websocket.send_json(room_counts_payload(
                    client_surface,
                    practice_authorized=player.composite_practice_authorized,
                ))

            elif msg_type == "get_recruitments":
                if client_surface not in {"neo", "plus"}:
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": "unavailable",
                        "message": "このクライアントでは募集掲示板を利用できません。",
                    })
                    continue
                try:
                    await player.send_json(await recruitment_payload(
                        client_surface,
                        data.get("owner_token"),
                    ))
                except RecruitmentError as exc:
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": exc.code,
                        "message": exc.message,
                    })
                except Exception as exc:
                    print(f"recruitment list failed: {exc}")
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": "temporarily_unavailable",
                        "message": "募集を読み込めませんでした。時間をおいて再度お試しください。",
                    })
                continue

            elif msg_type == "create_recruitment":
                if client_surface not in {"neo", "plus"}:
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": "unavailable",
                        "message": "このクライアントでは募集掲示板を利用できません。",
                    })
                    continue
                try:
                    created_recruitment = await RECRUITMENT_STORE.create(
                        name=data.get("name"),
                        rule_key=data.get("rule_key"),
                        scheduled_at=data.get("scheduled_at"),
                        owner_token=data.get("owner_token"),
                        board_key=client_surface,
                    )
                    notice = "募集を投稿しました。集合時間になると自動で消えます。"
                    if WEBHOOK_URL and not created_recruitment.notification_reserved:
                        notice += " Discord通知は現在の上限に達したため省略しました。"
                    await player.send_json(await recruitment_payload(
                        client_surface,
                        data.get("owner_token"),
                        notice=notice,
                    ))
                except RecruitmentError as exc:
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": exc.code,
                        "message": exc.message,
                    })
                except Exception as exc:
                    print(f"recruitment creation failed: {exc}")
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": "temporarily_unavailable",
                        "message": "募集を投稿できませんでした。時間をおいて再度お試しください。",
                    })
                continue

            elif msg_type == "delete_recruitment":
                if client_surface not in {"neo", "plus"}:
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": "unavailable",
                        "message": "このクライアントでは募集掲示板を利用できません。",
                    })
                    continue
                try:
                    deleted = await RECRUITMENT_STORE.delete(
                        recruitment_id=data.get("recruitment_id"),
                        owner_token=data.get("owner_token"),
                        board_key=client_surface,
                    )
                    if not deleted:
                        raise RecruitmentError(
                            "not_owner",
                            "募集が見つからないか、削除できる投稿者ではありません。",
                        )
                    await player.send_json(await recruitment_payload(
                        client_surface,
                        data.get("owner_token"),
                        notice="募集を削除しました。",
                    ))
                except RecruitmentError as exc:
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": exc.code,
                        "message": exc.message,
                    })
                except Exception as exc:
                    print(f"recruitment deletion failed: {exc}")
                    await player.send_json({
                        "type": "recruitment_error",
                        "code": "temporarily_unavailable",
                        "message": "募集を削除できませんでした。時間をおいて再度お試しください。",
                    })
                continue

            elif msg_type == "join_room":
                rid = data["room_id"]
                room = rooms.get(rid)
                if room is None:
                    await websocket.send_json({"type": "error", "message": "room not found"})
                    continue
                if not room_is_available_to_client(room, client_surface):
                    await websocket.send_json({
                        "type": "error",
                        "code": "room_not_available_for_client",
                        "message": "このクライアントからは選択した部屋に入室できません。",
                    })
                    continue
                if not player_can_access_room(player, room):
                    await websocket.send_json({
                        "type": "error",
                        "code": "practice_authorization_required",
                        "message": "この練習部屋へ入るには認証が必要です。",
                    })
                    continue

                resumed_session = room_resume_session(data.get("resume_token"), rid)
                if resumed_session is not None and resumed_session is not player:
                    player = await bind_room_resume_session(player, resumed_session)
                    active_room = player.room or room
                    if player.room is None:
                        active_room.players.append(player)
                        player.room = active_room
                    await active_room.update_room_status()
                    await player.send_json(room_initialization_payload(active_room, player))
                    await player.send_hand_update()
                    if active_room.state == "playing":
                        await active_room.update_game_state()
                    continue

                if player.room is room:
                    await room.update_room_status()
                    await player.send_json(room_initialization_payload(room, player))
                    continue

                if player.room:
                    await leave_room(player, notify_client=False)

                if len(room.players) >= 10:
                    await websocket.send_json({"type": "error", "message": "部屋が満員です。"})
                    continue

                await room.log_chat(f"{player.name}が入室しました")
                # 同期処理の後で、バックグラウンドに通知タスクを投げる
                if room_discord_join_notifications_enabled(room):
                    asyncio.create_task(
                        notify_discord_join(player.name, room)
                    )


                room.players.append(player)
                player.room = room
                player.status = "watching"  # 仮に入室したらwatchingに

                if client_surface != "legacy":
                    await issue_room_resume_session(player, rid)
                await room.update_room_status()
                await player.send_json(room_initialization_payload(room, player))

            elif msg_type == "leave_room":
                await leave_room(player)

            elif msg_type == "change_status":
                if not player.room:  # 部屋にいなければ無視
                    continue
                room = player.room
                if is_tournament_managed_room(room):
                    await player.send_json({
                        "type": "error",
                        "message": "大会ルームの対戦参加状態はシステムが管理します。大会パネルから参加登録してください。",
                    })
                    continue
                if room.state == "playing":
                    await player.send_json({
                        "type": "error",
                        "message": "対戦中は観戦状態に変更できません。退室する場合は退室してください。",
                    })
                    continue
                new_status = data["status"]
                player.status = new_status
                if new_status != "waiting":
                    player.clear_hand()
                    await player.send_hand_update()
                await room.update_room_status()
                if room.state == "playing" and await room.try_end_game():
                    await room.update_room_status()

            elif msg_type == "add_cpu":
                if not player.room:
                    continue
                room = player.room
                if is_tournament_managed_room(room):
                    await player.send_json({"type": "error", "message": "大会ルームにはCPUを追加できません。"})
                    continue
                if room.state == "playing":
                    await player.send_json({"type": "error", "message": "対戦中はCPUを追加できません。"})
                    continue
                if any(is_cpu_player(p) for p in room.players):
                    await player.send_json({"type": "error", "message": "この部屋にはすでにCPUがいます。"})
                    continue
                if len(room.players) >= 10:
                    await player.send_json({"type": "error", "message": "部屋が満員です。"})
                    continue
                cpu_key = data.get("cpu_key", "basic")
                try:
                    await add_cpu_to_room(room, cpu_key=cpu_key)
                except ValueError:
                    await player.send_json({"type": "error", "message": "この部屋では選択したCPUを使用できません。"})
                    continue

            elif msg_type == "remove_cpu":
                if not player.room:
                    continue
                room = player.room
                if room.state == "playing":
                    await player.send_json({"type": "error", "message": "対戦中はCPUを退出させられません。"})
                    continue
                if not await remove_cpu_from_room(room):
                    await player.send_json({"type": "error", "message": "この部屋にCPUはいません。"})
                    continue

            elif msg_type == "start_game":
                if not player.room:
                    continue
                room = player.room
                if is_tournament_managed_room(room):
                    await player.send_json({"type": "error", "message": "大会の対戦はシステムが自動で開始します。"})
                    continue

                # 対戦待ちプレイヤー確認
                waiting_players = get_active_players(room)
                if len(waiting_players) not in (1, 2):
                    await websocket.send_json({"type": "error", "message": "対戦待ちは1人または2人必要です。"})
                    continue
                disconnected_waiting = [
                    item
                    for item in waiting_players
                    if not is_cpu_player(item) and getattr(item, "ws", None) is None
                ]
                if disconnected_waiting:
                    await websocket.send_json({
                        "type": "error",
                        "message": "切断中の対戦待ちプレイヤーがいます。復帰するか待機猶予が終わるまでお待ちください。",
                    })
                    continue
                missing_registered = missing_registered_prime_players(room)
                if missing_registered:
                    names = ", ".join(p.name for p in missing_registered)
                    await websocket.send_json({
                        "type": "error",
                        "message": f"登録素数が未設定のプレイヤーがいます: {names}",
                    })
                    continue

                await start_game(room)
                await maybe_schedule_cpu_turn(room)

            elif msg_type == "get_prime_assist":
                if not player.room:
                    continue
                room = player.room
                assist_payload = {
                    "type": "prime_assist_result",
                    **await build_prime_assist_candidates_nonblocking(
                        player,
                        room,
                        data,
                    ),
                }
                if "assist_request_id" in data:
                    assist_payload["assist_request_id"] = data["assist_request_id"]
                await player.send_json(assist_payload)
                continue

            elif msg_type == "play_card":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                # モードごとに対応する関数を実行
                mode = (data.get("mode") or "prime").lower()
                try:
                    if mode == "composite":
                        if not room.rule.allow_composite:
                            await websocket.send_json({"type": "error", "message": "この部屋では合成数出しは使えません。"})
                            continue
                        await handle_composite_play(player, room, data)
                    else:
                        await handle_prime_play(player, room, data)
                except CompositeError as e:
                    await websocket.send_json({"type":"error","message":e.msg})



            elif msg_type == "draw_card":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                await draw_card_for_player(player, room)

            elif msg_type == "pass":
                if not player.room:
                    continue
                room = player.room
                if player.id != room.current_turn_id:
                    await websocket.send_json({"type": "error", "message": "あなたのターンではありません。"})
                    continue

                await pass_turn_for_player(player, room)

            elif msg_type == "join_global_chat":
                if client_surface == "plus_practice":
                    await player.send_json({"type": "error", "message": "練習ページではグローバルチャットを利用できません。"})
                    continue
                await subscribe_global_chat(player)

            elif msg_type == "leave_global_chat":
                await unsubscribe_global_chat(player)

            elif msg_type == "global_chat":
                if client_surface == "plus_practice":
                    await player.send_json({"type": "error", "message": "練習ページではグローバルチャットを利用できません。"})
                    continue
                await handle_global_chat_message(player, data)

            elif msg_type == "chat":
                if not player.room:
                    continue
                message = normalize_chat_message(data.get("message"))
                if message is None:
                    await player.send_json({
                        "type": "error",
                        "code": "invalid_chat_message",
                        "message": f"メッセージは1〜{MAX_CHAT_MESSAGE_LENGTH}文字で入力してください。",
                    })
                    continue
                display_sender = f"{player.name}"
                if room.room_id == TOURNAMENT_ROOM_ID:
                    run = tournament_run_for_room(room)
                    if run is not None:
                        await broadcast_tournament_lobby(run, {
                            "type": "chat",
                            "sender": display_sender,
                            "message": message,
                        })
                        continue
                payload = {
                    "type": "chat",
                    "sender": display_sender,
                    "message": message,
                }
                if room.tournament_match_id:
                    payload.update({
                        "scope": "tournament_match",
                        "match_id": room.tournament_match_id,
                    })
                await room.broadcast(payload)

    except WebSocketDisconnect:
        if player.ws is websocket and player.room is not None:
            room = player.room
            if player.room_resume_token_hash:
                await mark_player_disconnected(player)
                if room.state == "playing" and player.status == "waiting":
                    record_score_play_line(room, player, "切断")
                await room.update_room_status()
            else:
                await leave_room(player, notify_client=False)
    except Exception:
        traceback.print_exc()
        if player.ws is websocket and player.room is not None:
            room = player.room
            if player.room_resume_token_hash:
                await mark_player_disconnected(player)
                if room.state == "playing" and player.status == "waiting":
                    record_score_play_line(room, player, "切断")
                await room.update_room_status()
            else:
                await leave_room(player, notify_client=False)
    finally:
        if player.ws is websocket or player.ws is None:
            clear_tournament_match_view(player)
            GLOBAL_CHAT_SUBSCRIBERS.discard(player)
            player.global_chat_subscribed = False

################################################
# カードプレイ時の判定
################################################
async def handle_prime_play(player: Player, room: Room, data: dict) -> None:
    # 既存の "cards" + "assigned_numbers" で連結 → 特別数(57,1729) → 素数チェック
    played_cards = data.get("cards", [])
    score_prefix = score_state_prefix(room)
    if not played_cards:
        await player.ws.send_json({"type": "error", "message": "出すカードを選んでください。"})
        return
    # 手札にあるか検証
    if not player.has_cards(played_cards):
        await player.ws.send_json({"type": "error", "message": "そのカードは手札にありません。"})
        return
    if not normal_play_allowed_for_player(player, room.rule, played_cards):
        await player.ws.send_json({
            "type": "error",
            "code": "composite_play_required",
            "message": "この部屋では合成数出しだけが使えます。手札3枚以下では、全手札を使う通常の合法手でのみ上がれます。",
        })
        return

    # ジョーカー絡みの処理
    assigned_numbers = data.get("assigned_numbers", [])  # [ "inf" か 0〜13, ... ]
    # ―――――――――――――――――――
    # １）ジョーカーだけを単独で出す (グロタンカット相当)
    jokers = [c for c in played_cards if c["suit"] == "X"]
    if len(jokers) == 1 and len(played_cards) == 1:
        if room.field and len(room.field) != 1:
            await player.ws.send_json({"type": "error", "message": "ジョーカー1枚出しは、場が空か1枚のときだけ出せます。"})
            return
        push_to_reserve(room, played_cards)
        # ジョーカー1枚だけ → 場を流す
        player.remove_card(jokers[0])
        # 場を流して予備軍を山へ戻す
        flow_field(room)
        room.has_drawn = False
        await player.send_hand_update()
        await room.log_chat(f"{player.name}がジョーカーを出しました、インフィニティ！")
        record_score_play_line(room, player, f"{score_prefix}X[IN]{score_win_suffix(player)}")
        await room.update_game_state()
        await room.broadcast({
            "type": "action_result",
            "action": "field_flow",
            "player_id": player.id,
            "played_cards": played_cards,
            "number": "X",
            "flow_reason": "infinity",
        })
        if await room.try_end_game():
            await room.update_room_status()
        else:
            await broadcast_turn_update(room, player.name)
        return  # ターン継続
    # ２）ジョーカーを含む複数枚プレイ時は、置換して number を作成
    if jokers:
        if len(assigned_numbers) != len(jokers):
            await player.ws.send_json({
                "type": "error",
                "message": "ジョーカーの数字指定が不足しています。"
            })
            return
        if any(v == "inf" for v in assigned_numbers):
            await player.ws.send_json({
                "type": "error",
                "message": "複数枚出し時に「∞」指定はできません。"
            })
            return
        if invalid_joker_assignments(assigned_numbers):
            await player.ws.send_json({
                "type": "error",
                "message": joker_assignment_error_message()
            })
            return
        ranks = []
        joker_i = 0
        for c in played_cards:
            if c["suit"] == "X":
                val = assigned_numbers[joker_i]
                joker_i += 1
                ranks.append(str(val))
            else:
                ranks.append(str(c["rank"]))
        ranks_str = "".join(ranks)

        # 先頭が 0 の数字は許可しない
        if ranks_str.startswith("0"):
            await player.ws.send_json({
                "type": "error",
                "message": "最上位桁が0の数字は出せません。"
            })
            return

        try:
            number = int(ranks_str)
        except ValueError:
            number = -1
    else:
        # 通常カードのみ
        ranks_str = "".join(str(c["rank"]) for c in played_cards)
        try:
            number = int(ranks_str)
        except ValueError:
            number = -1

    # もしフィールドに既にカードが出ているなら、枚数と数の検証を行う
    if room.field:
        # ① 枚数チェック
        if len(played_cards) != len(room.field):
            await player.ws.send_json({"type": "error", "message": "枚数が違います。"})
            return

        # ② 数値チェック：フィールドのカードと比較
        field_number = room.last_number if room.last_number is not None else -1

        # 通常は「>」が必要、反転中は「<」を要求
        if not room.reverse_order:
            if number <= field_number:
                await player.ws.send_json({"type": "error", "message": "場より大きい数字を出してください。"})
                return
        else:
            if number >= field_number:
                await player.ws.send_json({"type": "error", "message": "場より小さい数字を出してください。(ラマヌジャン革命中)"})
                return

    # グロタンカット
    if number == 57 and not room.rule.special_numbers_composite_only:
        # 出した順そのまま予備軍に
        push_to_reserve(room, played_cards)
        for c in played_cards:
            player.remove_card(c)
        # 場を流して予備軍を山へ戻す
        flow_field(room)
        # 自分の手番を継続するため next_turn は呼ばない
        room.has_drawn = False
        # クライアントの表示を更新
        await player.send_hand_update()
        await room.log_chat(f"{player.name}が57を出しました、グロタンカット！")
        play_text = score_cards_text(played_cards) + score_joker_suffix(played_cards, assigned_numbers)
        record_score_play_line(room, player, f"{score_prefix}{play_text}[GC]{score_win_suffix(player)}")
        await room.update_game_state()
        await room.broadcast({
            "type": "action_result",
            "action": "field_flow",
            "player_id": player.id,
            "played_cards": played_cards,
            "number": 57,
            "flow_reason": "grotan_cut",
        })
        if await room.try_end_game():
            await room.update_room_status()
            return
        await broadcast_turn_update(room, player.name)
        return  # 次の処理（素数判定～next_turn）をすべてスキップ
    if number == 1729 and not room.rule.special_numbers_composite_only:
        # フラグをトグル
        room.reverse_order = not room.reverse_order
        # カードを場に出す
        push_to_reserve(room, played_cards)
        for c in played_cards:
            player.remove_card(c)
        room.field = played_cards
        room.last_number = number

        # 手札更新 & ゲーム状態通知
        await player.send_hand_update()
        await room.update_game_state()
        # ログ
        await room.log_chat(f"{player.name}が1729を出しました、ラマヌジャン革命！")
        play_text = score_cards_text(played_cards) + score_joker_suffix(played_cards, assigned_numbers)
        record_score_play_line(room, player, f"{score_prefix}{play_text}[RR]{score_win_suffix(player)}")

        # 通常の素数出しと同じく次のターンへ
        await next_turn(room)
        return

    submitted_number = number
    hnp_challenge = False
    if (
        room.rule.hnp_challenge_enabled
        and room.rule.prime_rule is PrimeRule.REGISTERED
        and len(played_cards) >= 2
        and not player.can_use_registered_prime(number)
    ):
        hnp_tokens = build_hnp_tokens(played_cards, assigned_numbers)
        hnp_result = choose_hnp_permutation(
            hnp_tokens,
            field_number=room.last_number if room.field else None,
            reverse_order=room.reverse_order,
        )
        if hnp_result is None:
            await player.ws.send_json({
                "type": "error",
                "message": "場に合法的に出せるHNPの並びがありません。",
            })
            return
        hnp_challenge = True
        played_cards = hnp_result.cards
        assigned_numbers = hnp_result.assigned_numbers
        number = hnp_result.number

    # 素数判定
    is_valid_play = is_prime(number) if hnp_challenge else is_valid_prime_for_player(number, player, room.rule)
    if not is_valid_play:
        # ペナルティ
        # 出そうとしたカードを引き直すことはしない(そもそも出されていないため)
        penalty_cards = get_penalty_card_count(
            room.rule.penalty_rule,
            field_card_count=len(played_cards),
            normal_card_count=len(played_cards),
        )
        drawn_penalties = []
        for _ in range(penalty_cards):
            if room.deck:
                drawn = room.deck.pop(0)
                player.add_card(drawn)
                drawn_penalties.append(drawn)

        # フィールドをリセット（場のカードを消す）2人対戦想定であることに注意
        flow_field(room)

        await player.send_hand_update()
        await room.update_game_state()
        penalty_payload = {
            "type": "penalty",
            "player_id": player.id,
            "played_cards": played_cards,
            "number": number,
        }
        if hnp_challenge:
            penalty_payload.update({
                "hnp_challenge": True,
                "submitted_number": submitted_number,
                "assigned_numbers": assigned_numbers,
            })
        await room.broadcast(penalty_payload)

        # チャットにペナルティのログを流す
        if hnp_challenge:
            await room.log_chat(f"{player.name}が{number}を出そうとしましたが、{number}は素数ではありません")
            await room.log_chat("HNP失敗！")
        else:
            rule_name = rule_display_name(room.rule.prime_rule)
            await room.log_chat(f"{player.name}が{number}を出そうとしましたが、{number}は{rule_name}ではありません")
        play_text = score_cards_text(played_cards) + score_joker_suffix(played_cards, assigned_numbers)
        record_score_play_line(
            room,
            player,
            f"{score_prefix}{play_text},P({score_cards_text(drawn_penalties, sort_cards=True)})"
        )

        await next_turn(room)
        return

    # 素数なら場に出す。常設チャレンジ対象局では人間の最大素数も記録する。
    remember_campaign_prime(room, player, number)
    push_to_reserve(room, played_cards)
    for c in played_cards:
        player.remove_card(c)
    room.field = played_cards
    room.last_number = number

    await player.send_hand_update()

    await room.update_game_state()
    action_payload = {
        "type": "action_result",
        "action": "play_card",
        "player_id": player.id,
        "played_cards": played_cards,
        "number": number,
    }
    if hnp_challenge:
        action_payload.update({
            "hnp_challenge": True,
            "submitted_number": submitted_number,
            "assigned_numbers": assigned_numbers,
        })
    await room.broadcast(action_payload)

    # チャットに「素数を出した」ログを流す
    await room.log_chat(f"{player.name}が{number}を出しました")
    if hnp_challenge:
        await room.log_chat("HNP！")
    await maybe_log_talkative_fish_sashimi(room, number)
    play_text = score_cards_text(played_cards) + score_joker_suffix(played_cards, assigned_numbers)
    record_score_play_line(room, player, f"{score_prefix}{play_text}{score_win_suffix(player)}")
    await next_turn(room)

# 現行ルールでは指数が122を超える合法手が存在しないため、
# 計算量を抑える実用上の上限として122を採用する。
MAX_EXP = 122

# エラーメッセージ & 分類
class CompositeError(Exception):
    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)
# 文法エラー（やり直し）
class CompositeSyntaxError(CompositeError):
    pass

# 計算誤り（ペナルティ）
class CompositeMathError(CompositeError):
    pass

def map_joker_values_in_cards(cards: List[dict], assigned: List[str], allow_inf_singleton: bool) -> List[int]:
    """
    cards の並びを整数列(ランク)にする。Jokerは assigned で置換。
    allow_inf_singleton が True のときのみ、Joker1枚・単独・"inf" を許す（場流し扱いへ）。
    """
    jokers = [c for c in cards if c["suit"] == "X"]
    if len(jokers) != len(assigned):
        raise CompositeError("ジョーカーの数字指定が不足しています。")

    # 単独 Joker ∧ allow_inf_singleton のみ "inf" を許す
    if any(v == "inf" for v in assigned):
        if not (allow_inf_singleton and len(cards) == 1 and len(jokers) == 1):
            raise CompositeError("この状況で∞は使用できません。")
    if invalid_joker_assignments(assigned):
        raise CompositeError(joker_assignment_error_message())

    out = []
    ji = 0
    for c in cards:
        if c["suit"] == "X":
            v = assigned[ji]
            ji += 1
            if v == "inf":
                out.append("inf")  # 単独流しだけこのまま返す
            else:
                out.append(int(v))
        else:
            out.append(c["rank"])
    return out

def build_int_from_cards(seq: List[int]) -> int:
    s = "".join(str(x) for x in seq)
    if s.startswith("0"):
        raise CompositeError("最上位桁が0の数は作れません。")
    return int(s)

def parse_and_eval_composite(
    tokens: List[dict],
    token_card_ranks: Dict[str, int],
    rule: RulePreset,
) -> Tuple[int, List[str]]:
    """
    tokens: [{kind:'card', card_id:...} | {kind:'op', op:'×'|'^'}]
    token_card_ranks: card_id -> ランク（Jokerは割当後）
    joker_values: （未使用、説明簡略化）
    return: (value, used_card_ids)

    許可する構文:
      card+ ( (×|^) card+ )*
    つまり
      - カードは連続して整数を作ってよい
      - 演算子は連続不可
      - 先頭末尾はカード
    """
    if not tokens:
        raise CompositeSyntaxError("合成数の式が空です。")

    if tokens[0]["kind"] != "card" or tokens[-1]["kind"] != "card":
        raise CompositeSyntaxError("式の先頭と末尾はカードである必要があります。")

    # 1) 演算子の基本構文チェック
    prev_kind = None
    for i, t in enumerate(tokens):
        kind = t.get("kind")

        if kind not in ("card", "op"):
            raise CompositeSyntaxError("不正なトークン種別があります。")

        if kind == "op":
            op = t.get("op")
            if op not in ("×", "^"):
                raise CompositeSyntaxError(f"不正な演算子 {op} です。")

            # 演算子が先頭末尾に来るのは不可
            if i == 0 or i == len(tokens) - 1:
                raise CompositeSyntaxError("演算子を式の先頭・末尾には置けません。")

            # 演算子の連続は禁止
            if prev_kind == "op":
                raise CompositeSyntaxError("演算子を連続して置くことはできません。")

        prev_kind = kind

    # 2) “×” で分割
    chunks: List[List[dict]] = []
    cur: List[dict] = []
    for t in tokens:
        if t["kind"] == "op" and t["op"] == "×":
            if not cur:
                raise CompositeSyntaxError("× の前後が不正です。")
            chunks.append(cur)
            cur = []
        else:
            cur.append(t)
    if not cur:
        raise CompositeSyntaxError("× の後に数字が必要です。")
    chunks.append(cur)

    used_card_ids: List[str] = []
    total_value = 1

    # 3) 各 chunk を「card+ (^ card+)*」として解釈
    for ch in chunks:
        seqs: List[List[int]] = []
        temp_cards: List[str] = []

        cur_cards: List[int] = []
        cur_ids: List[str] = []

        for t in ch:
            if t["kind"] == "card":
                cid = t["card_id"]
                if cid not in token_card_ranks:
                    raise CompositeSyntaxError("未知のカードが指定されました。")
                cur_cards.append(token_card_ranks[cid])
                cur_ids.append(cid)

            else:
                # chunk 内に残ってよい演算子は ^ のみ
                if t["op"] != "^":
                    raise CompositeSyntaxError("× は分割済みのはずです。")
                if not cur_cards:
                    raise CompositeSyntaxError("^ の前後に数字が必要です。")

                seqs.append(cur_cards)
                temp_cards.extend(cur_ids)
                cur_cards, cur_ids = [], []

        # 末尾の整数を追加
        if not cur_cards:
            raise CompositeSyntaxError("式の末尾が不正です。")
        seqs.append(cur_cards)
        temp_cards.extend(cur_ids)

        # 4) 各 card 列を整数化
        ints = [build_int_from_cards(s) for s in seqs]

        # 5) 底の条件
        base = ints[0]
        if base < 2:
            raise CompositeSyntaxError("底が0または1は不可です。")
        if not is_valid_prime_by_rule(base, rule):
            kind = rule_display_name(rule.prime_rule)
            raise CompositeMathError(f"底 {base} が{kind}ではありません。")

        # 6) 指数連鎖を右結合で評価
        if len(ints) == 1:
            exp = 1
        else:
            exp = ints[-1]
            if exp > MAX_EXP:
                raise CompositeMathError(f"指数 {exp} が上限 {MAX_EXP} を超えています。")

            for e in reversed(ints[1:-1]):
                if e > MAX_EXP:
                    raise CompositeMathError(f"指数 {e} が上限 {MAX_EXP} を超えています。")
                exp = pow(e, exp)
                if exp > MAX_EXP:
                    raise CompositeMathError(f"合成された指数 {exp} が上限 {MAX_EXP} を超えています。")

        value = pow(base, exp)
        total_value *= value
        used_card_ids.extend(temp_cards)

    return total_value, used_card_ids

async def handle_composite_play(player: Player, room: Room, data: dict) -> None:
    # 0) 手番 & 手札 所有チェック（共通）
    selected = data.get("selected", {}) or {}
    consume  = data.get("consume", {}) or {}
    comp     = data.get("composite", {}) or {}
    sel_cards: List[dict] = selected.get("cards", [])
    con_cards: List[dict] = consume.get("cards", [])
    comp_tokens: List[dict] = comp.get("tokens", [])
    sel_assigned: List[str] = selected.get("assigned_numbers", [])
    comp_assigned: List[str] = comp.get("assigned_numbers", [])
    score_prefix = score_state_prefix(room)
    if not sel_cards:
        await player.ws.send_json({"type": "error", "message": "見せ札を選んでください。"})
        return
    if not comp_tokens:
        await player.ws.send_json({"type": "error", "message": "材料札で合成数の式を作ってください。"})
        return

    # composite.tokens から材料札を再構成（見せ札と材料札は常に別）
    token_card_ids = [t.get("card_id") for t in comp_tokens if t.get("kind") == "card"]
    token_card_ids = [cid for cid in token_card_ids if cid is not None]
    if token_card_ids:
        hand_by_id = {c["card_id"]: c for c in player.hand}
        con_cards = [hand_by_id[cid] for cid in token_card_ids if cid in hand_by_id]
    score_cards_by_id = {c["card_id"]: c for c in (sel_cards + con_cards)}
    score_composite_text = (
        f"{score_cards_text(sel_cards)}={score_tokens_text(comp_tokens, score_cards_by_id)}"
        f"{score_joker_suffix(sel_cards + con_cards, sel_assigned + comp_assigned)}"
    )
    composite_chat_text = (
        f"{score_cards_text(sel_cards)}={score_tokens_text(comp_tokens, score_cards_by_id).replace('*', '×')}"
        f"{score_joker_suffix(con_cards, comp_assigned)}"
    )

    # 手札に全部あるか
    all_consume = list({c["card_id"]:c for c in (sel_cards + con_cards)}.values())
    if not player.has_cards(all_consume):
        await player.ws.send_json({"type": "error", "message": "そのカードは手札にありません。"})
        return

    # 1) Joker 検証（選択側）: 合成数モードでは∞は常に禁止（単独流しも不可）
    try:
        # 値の割当チェックのみ行い、∞は許可しない
        map_joker_values_in_cards(sel_cards, sel_assigned, allow_inf_singleton=False)
    except CompositeError as e:
        await player.ws.send_json({"type":"error","message":e.msg});
        return

    # 2) 合成数場 Joker 割当
    #   comp_tokens 上に Joker が m 枚出現していることを数え、その m と comp_assigned の長さが一致、かつ inf を含まないことを要求
    comp_joker_count = 0
    card_by_id = { c["card_id"]: c for c in player.hand }
    for t in comp_tokens:
        if t.get("kind") == "card":
            c = card_by_id.get(t["card_id"])
            if c and c.get("is_joker"): comp_joker_count += 1
    if comp_joker_count != len(comp_assigned) or any(v=="inf" for v in comp_assigned):
        await player.ws.send_json({"type":"error","message":"合成数内のジョーカー指定が不正です。"})
        return
    if invalid_joker_assignments(comp_assigned):
        await player.ws.send_json({"type":"error","message":joker_assignment_error_message()})
        return

    # 3) token_card_ranks を作る（合成数トークンの “card_id → ランク”）
    #    Joker は comp_assigned を登場順に置換
    token_card_ranks: Dict[str,int] = {}
    jidx = 0
    for t in comp_tokens:
        if t.get("kind") == "card":
            cid = t["card_id"]
            c   = card_by_id.get(cid)
            if not c:
                await player.ws.send_json({"type":"error","message":"未知のカードが式に含まれています。"}); return
            if c.get("is_joker"):
                token_card_ranks[cid] = int(comp_assigned[jidx]); jidx += 1
            else:
                token_card_ranks[cid] = int(c["rank"])

    # 4) 早期チェック：枚数・大小は selected のみで判定（合成数のパース前）
    # 4-1) 枚数（場があるときは selected の枚数と一致必須）
    if room.field:
        if len(sel_cards) != len(room.field):
            await player.ws.send_json({"type":"error","message":"枚数が違います。"})
            return

    # 4-2) 大小（selected を連結して得た sel_number で比較）
    #      ※ 合成数モードでは ∞ 不可／先頭0不可
    try:
        sel_ranks = map_joker_values_in_cards(sel_cards, sel_assigned, allow_inf_singleton=False)
    except CompositeError as e:
        await player.ws.send_json({"type":"error","message":e.msg})
        return

    sel_str = "".join(str(x) for x in sel_ranks)
    if sel_str.startswith("0"):
        await player.ws.send_json({"type":"error","message":"最上位桁が0の数字は出せません。"})
        return
    sel_number = int(sel_str) if sel_str else -1

    if room.field:
        field_number = room.last_number if room.last_number is not None else -1
        if (not room.reverse_order and sel_number <= field_number) or (room.reverse_order and sel_number >= field_number):
            await player.ws.send_json({
                "type":"error",
                "message": ("場より大きい数字を出してください。" if not room.reverse_order else "場より小さい数字を出してください。(ラマヌジャン革命中)")
            })
            return

    # 5) 合成数の構文・評価（con 側）。構文はエラー返し、計算はペナルティ。
    try:
        number, used_ids = parse_and_eval_composite(comp_tokens, token_card_ranks, room.rule)
        # con を全て掛け合わせた number と sel_number は一致必須（不一致は MathError → ペナルティ）
        if number != sel_number:
            raise CompositeMathError("選択カードの数と合成数の値が一致しません。")
        if (
            room.rule.prime_rule is PrimeRule.REGISTERED
            and not player.can_use_registered_composite(sel_number)
        ):
            raise CompositeMathError(f"{sel_number}は本人の登録済み合成数に含まれていません。")
    except CompositeSyntaxError as e:
        await player.ws.send_json({"type":"error","message":e.msg})
        return
    except CompositeMathError as e:
        penalty_cards = get_penalty_card_count(
            room.rule.penalty_rule,
            field_card_count=len(sel_cards),
            normal_card_count=len(all_consume),
        )
        drawn_penalties = []
        for _ in range(penalty_cards):
            if room.deck:
                drawn = room.deck.pop(0)
                player.add_card(drawn)
                drawn_penalties.append(drawn)
        flow_field(room)
        await player.send_hand_update()
        await room.update_game_state()
        await room.broadcast({
            "type": "penalty",
            "player_id": player.id,
            "played_cards": sel_cards,
            "number": sel_number
        })
        await room.log_chat(f"{player.name}の合成数 {composite_chat_text} は不正でした（{e.msg}）。ペナルティ。")
        record_score_play_line(
            room,
            player,
            f"{score_prefix}{score_composite_text},P({score_cards_text(drawn_penalties, sort_cards=True)})"
        )
        await next_turn(room)
        return

    await record_composite_practice_play(player, room, sel_number)

    if room.rule.special_numbers_composite_only and sel_number in {57, 1729}:
        push_to_reserve(room, sel_cards)
        sel_ids = {c["card_id"] for c in sel_cards}
        con_only = [c for c in con_cards if c["card_id"] not in sel_ids]
        return_cards_to_deck_bottom(room, con_only)
        for card in all_consume:
            player.remove_card(card)

        await player.send_hand_update()
        if sel_number == 57:
            flow_field(room)
            room.has_drawn = False
            await room.update_game_state()
            await room.broadcast({
                "type": "action_result",
                "action": "field_flow",
                "player_id": player.id,
                "played_cards": sel_cards,
                "number": 57,
                "mode": "composite",
                "flow_reason": "grotan_cut",
            })
            await room.log_chat(f"{player.name}が合成数 {composite_chat_text} を出しました、グロタンカット！")
            record_score_play_line(room, player, f"{score_prefix}{score_composite_text}[GC]{score_win_suffix(player)}")
            if await room.try_end_game():
                await room.update_room_status()
                return
            await broadcast_turn_update(room, player.name)
            return

        room.reverse_order = not room.reverse_order
        room.field = sel_cards
        room.last_number = sel_number
        await room.update_game_state()
        await room.broadcast({
            "type": "action_result",
            "action": "play_card",
            "player_id": player.id,
            "played_cards": sel_cards,
            "number": 1729,
            "mode": "composite",
            "special_effect": "revolution",
        })
        await room.log_chat(f"{player.name}が合成数 {composite_chat_text} を出しました、ラマヌジャン革命！")
        record_score_play_line(room, player, f"{score_prefix}{score_composite_text}[RR]{score_win_suffix(player)}")
        await next_turn(room)
        return

    # 7) すべてOK → 札を「出した順」でreserveに積む → 手札から除去
    #    出した順は UI から渡す順序（selected→consume）で良ければそのまま。必要なら tokens から順序を決める。
    push_to_reserve(room, sel_cards)

    # selected と重複するカードは deck に戻さない
    sel_ids = {c["card_id"] for c in sel_cards}
    con_only = [c for c in con_cards if c["card_id"] not in sel_ids]
    return_cards_to_deck_bottom(room, con_only)


    # 手札からは selected/consume 全部を除去（all_consume はユニーク化済み想定）
    for c in all_consume:
        player.remove_card(c)

    # field には sel 側が残る仕様。大小・一致は sel_number 基準。
    room.field = sel_cards # 合成数は流すのでカウントされない
    room.last_number = sel_number

    await player.send_hand_update()
    await room.update_game_state()
    await room.broadcast({
        "type":"action_result",
        "action":"play_card",
        "player_id": player.id,
        "played_cards": room.field,
        "number": sel_number,
        "mode": "composite"
    })
    await room.log_chat(f"{player.name}が{composite_chat_text}を出しました")
    await maybe_log_talkative_fish_sashimi(room, composite_chat_text, sel_number)
    record_score_play_line(room, player, f"{score_prefix}{score_composite_text}{score_win_suffix(player)}")
    await next_turn(room)


################################################
# 部屋からの退出
################################################
async def leave_room(player, notify_client: bool = True):
    clear_tournament_match_view(player)
    if player.room is None:
        if notify_client:
            await player.send_json(room_counts_payload(
                player.client_surface,
                practice_authorized=player.composite_practice_authorized,
            ))
        return

    room = player.room
    public_id = public_room_id(room)
    run = tournament_for_player(player)
    tournament_match = (
        run.current_match_for_participant(player.tournament_participant_id)
        if run is not None and player.tournament_participant_id
        else None
    )
    if tournament_match is not None and tournament_match.status in {"called", "playing"}:
        opponent_id = (
            tournament_match.player2_id
            if tournament_match.player1_id == player.tournament_participant_id
            else tournament_match.player1_id
        )
        await resolve_tournament_match(
            run,
            tournament_match.match_id,
            opponent_id,
            resolution="explicit_leave_forfeit",
            actor=run.participants[player.tournament_participant_id].display_name,
            advance=False,
        )
        room = player.room or rooms[run.room_id]

    if player in room.players:
        departed_player_id = player.id
        room.players.remove(player)
        player.room = None

        # 退出通知
        await room.log_chat(f"{player.name}が退室しました")
        if room.state == "playing" and player.status == "waiting":
            record_score_play_line(room, player, "退出")
        player.clear_hand()

        await handle_room_after_player_removed(room, departed_player_id)
    else:
        player.room = None
        player.status = "watching"
        player.clear_hand()
    forget_room_resume_session(player)
    player.disconnected_at = None
    if notify_client:
        await player.send_json({"type": "room_left", "room_id": public_id})
        await player.send_json(room_counts_payload(
            player.client_surface,
            practice_authorized=player.composite_practice_authorized,
        ))
    if run is not None and tournament_match is not None and run.status == "running":
        await start_or_prepare_next_tournament_match(run)


################################################
# ゲーム開始処理
################################################
async def start_game(room):
    room.reverse_order = room.rule.start_revolution     # 革命はルールごとの開始時コンディションに戻す
    room.has_drawn = False         # ドロー済みフラグもクリア

    # 1) 待機中のプレイヤーを確定（1人練習または2人対戦）
    waiting_players = get_active_players(room)
    if len(waiting_players) not in (1, 2):
        return
    prepare_campaign_game(room, waiting_players)
    for p in room.players:
        if p not in waiting_players:
            p.clear_hand()
            await p.send_hand_update()

    # 2) デッキ生成→配布（プリセット準拠）
    deck = build_deck(room.rule)
    hands, remaining = shuffle_and_deal(deck, room.rule.hand_size, num_players=len(waiting_players))
    for player, hand in zip(waiting_players, hands):
        player.hand = hand
    room.deck = remaining

    room.reserve = []
    room.field = []  # 場のカードは空
    room.last_number = None
    room.score_log = []
    for player in waiting_players:
        player.sort_hand()
        record_score_line(room, f"{player.name}:({score_cards_text(player.hand, sort_cards=True)})")
    room.state = "playing"

    # ランダムに先攻プレイヤー決定
    room.current_turn_id = random.choice([p.id for p in waiting_players])
    room.first_player_id = room.current_turn_id

    # プレイヤーそれぞれに手札情報を送信
    for player in waiting_players:
        await player.send_json({"type": "deal", "your_hand": player.hand})

    # 全体にゲーム開始 & 現在のターン情報
    await room.broadcast({
        "type": "game_start",
        "category": room.category,
        "allow_composite": room.rule.allow_composite,
        "prime_rule": room.rule.prime_rule.name.lower(),
        "assist_enabled": room.rule.assist_enabled,
        "registration_enabled": room.rule.registration_enabled,
        "hnp_challenge_enabled": room.rule.hnp_challenge_enabled,
    })
    await room.update_game_state()
    # チャットにログを流す
    await room.log_chat("ゲーム開始！")
    await maybe_schedule_cpu_turn(room)


################################################
# 次のターンに移る
################################################
async def broadcast_turn_update(room, current_turn_name: str | None, reset_timer: bool = True) -> None:
    await room.broadcast({
        "type": "turn_update",
        "current_turn": current_turn_name,
        "current_turn_id": room.current_turn_id,
        "reset_timer": reset_timer,
    })
    if room.tournament_match_id:
        await broadcast_tournament_match_state(room)

async def next_turn(room):
    # ターンが変わるので、ドロー済みフラグをリセットする
    room.has_drawn = False

    # 対戦に参加している（statusが"waiting"の）プレイヤーだけを対象とする
    active_players = get_active_players(room)
    if len(active_players) < 1:
        return

    if await room.try_end_game():
        await room.update_room_status()
        return

    current_turn_id = room.current_turn_id
    # 現在の手番プレイヤーが active_players の中にいるかを確認
    idx = [i for i, p in enumerate(active_players) if p.id == current_turn_id]
    if not idx:
        # もし現在の手番プレイヤーが active でなければ、先頭のプレイヤーに設定
        room.current_turn_id = active_players[0].id
    else:
        # 元の順番を無視しているようだが2人対戦の間は大丈夫か？
        current_idx = idx[0]
        next_idx = (current_idx + 1) % len(active_players)
        room.current_turn_id = active_players[next_idx].id

    # await room.update_game_state() それぞれのアクションで既に呼び出されているので省略
    # 次のプレイヤー名を取得して送信
    next_player = next((p for p in room.players if p.id == room.current_turn_id), None)
    await broadcast_turn_update(room, next_player.name if next_player else None)
    await maybe_schedule_cpu_turn(room)
