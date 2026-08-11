from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo


DEFAULT_CAMPAIGN_KEY = "gold-cpu-weekly"
DEFAULT_CAMPAIGN_GOAL = 300
DEFAULT_CAMPAIGN_PAGE_URL = "https://greenplus.github.io/qkneo/campaign.html"
WEEKLY_TIMEZONE = ZoneInfo("Asia/Tokyo")
LEGACY_CAMPAIGN_KEY = "gold-cpu-100"
LEGACY_PERIOD_KEY = "2026-07-28-special"
LAUNCH_PERIOD_KEY = "2026-08-12-launch"
LAUNCH_WEEK_MONDAY = date(2026, 8, 10)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


def parse_campaign_datetime(
    value: Optional[str],
    variable_name: str,
) -> tuple[Optional[datetime], Optional[str]]:
    if not value or not value.strip():
        return None, f"{variable_name} が未設定です"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, f"{variable_name} はタイムゾーン付きISO日時で設定してください"
    if parsed.tzinfo is None:
        return None, f"{variable_name} にはタイムゾーンが必要です"
    return parsed.astimezone(timezone.utc), None


def parse_start_at(value: Optional[str]) -> tuple[Optional[datetime], Optional[str]]:
    return parse_campaign_datetime(value, "CPU_CAMPAIGN_START_AT")


def parse_end_at(value: Optional[str]) -> tuple[Optional[datetime], Optional[str]]:
    return parse_campaign_datetime(value, "CPU_CAMPAIGN_END_AT")


@dataclass(frozen=True)
class CampaignPeriod:
    key: str
    starts_at: datetime
    ends_at: datetime
    goal: int
    label: str


@dataclass(frozen=True)
class CampaignSettings:
    enabled: bool
    key: str
    goal: int
    start_at: Optional[datetime]
    start_error: Optional[str]
    end_at: Optional[datetime]
    end_error: Optional[str]
    page_url: str
    allowed_origins: tuple[str, ...]
    schedule: str = "one-off"

    @classmethod
    def from_env(cls) -> "CampaignSettings":
        start_at, start_error = parse_start_at(os.getenv("CPU_CAMPAIGN_START_AT"))
        end_at, end_error = parse_end_at(os.getenv("CPU_CAMPAIGN_END_AT"))
        if start_at is not None and end_at is not None and end_at <= start_at:
            end_error = "CPU_CAMPAIGN_END_AT は開始日時より後に設定してください"
        schedule = os.getenv("CPU_CAMPAIGN_SCHEDULE", "weekly").strip().lower()
        if schedule not in {"weekly", "one-off"}:
            schedule = "weekly"
        origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "CPU_CAMPAIGN_ALLOWED_ORIGINS",
                "https://greenplus.github.io,http://127.0.0.1:5174,http://localhost:5174",
            ).split(",")
            if origin.strip()
        )
        return cls(
            enabled=bool_env("CPU_CAMPAIGN_ENABLED"),
            key=os.getenv("CPU_CAMPAIGN_KEY", DEFAULT_CAMPAIGN_KEY).strip()
            or DEFAULT_CAMPAIGN_KEY,
            goal=positive_int_env("CPU_CAMPAIGN_GOAL", DEFAULT_CAMPAIGN_GOAL),
            start_at=start_at,
            start_error=start_error,
            end_at=end_at,
            end_error=end_error,
            page_url=os.getenv(
                "CPU_CAMPAIGN_PAGE_URL",
                DEFAULT_CAMPAIGN_PAGE_URL,
            ).strip()
            or DEFAULT_CAMPAIGN_PAGE_URL,
            allowed_origins=origins,
            schedule=schedule,
        )

    def period_state(
        self,
        now: Optional[datetime] = None,
    ) -> tuple[str, Optional[CampaignPeriod]]:
        current = (now or utc_now()).astimezone(timezone.utc)
        if self.schedule == "weekly":
            local = current.astimezone(WEEKLY_TIMEZONE)
            monday = local.date() - timedelta(days=local.weekday())
            start_local = datetime.combine(monday, time(6), WEEKLY_TIMEZONE)
            end_local = datetime.combine(monday + timedelta(days=7), time(0), WEEKLY_TIMEZONE)
            period_key = start_local.date().isoformat()
            label = f"{start_local:%Y/%m/%d}週"
            if monday == LAUNCH_WEEK_MONDAY:
                start_local = datetime(2026, 8, 12, 6, 0, tzinfo=WEEKLY_TIMEZONE)
                period_key = LAUNCH_PERIOD_KEY
                label = "初週 2026/08/12 6:00開始"
            status = "active"
            if local < start_local:
                status = "scheduled"
            period = CampaignPeriod(
                key=period_key,
                starts_at=start_local.astimezone(timezone.utc),
                ends_at=end_local.astimezone(timezone.utc),
                goal=self.goal,
                label=label,
            )
            return status, period

        if self.start_at is None or self.end_at is None or self.end_error is not None:
            return "unavailable", None
        period = CampaignPeriod(
            key=self.start_at.astimezone(WEEKLY_TIMEZONE).date().isoformat(),
            starts_at=self.start_at,
            ends_at=self.end_at,
            goal=self.goal,
            label="期間限定キャンペーン",
        )
        if current < period.starts_at:
            return "scheduled", period
        if current >= period.ends_at:
            return "finished", period
        return "active", period

    def active_period(self, now: Optional[datetime] = None) -> Optional[CampaignPeriod]:
        status, period = self.period_state(now)
        return period if status == "active" else None

    def is_active(self, now: Optional[datetime] = None) -> bool:
        return self.enabled and self.active_period(now) is not None


class CampaignStore:
    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.pool = None
        self.last_error: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.pool is not None

    async def connect(self) -> None:
        if not self.database_url:
            self.last_error = "DATABASE_URL が未設定です"
            return

        pool = None
        try:
            import asyncpg

            pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=5,
                command_timeout=5,
            )
            self.pool = pool
            await self._ensure_schema()
            self.last_error = None
        except Exception as exc:
            if pool is not None:
                await pool.close()
            self.pool = None
            self.last_error = str(exc)
            print(f"campaign database initialization failed: {exc}")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def _ensure_schema(self) -> None:
        if self.pool is None:
            raise RuntimeError("campaign database is unavailable")
        await self.pool.execute(
            f"""
            CREATE TABLE IF NOT EXISTS campaign_periods (
                campaign_key TEXT NOT NULL,
                period_key TEXT NOT NULL,
                starts_at TIMESTAMPTZ NOT NULL,
                ends_at TIMESTAMPTZ NOT NULL,
                goal INTEGER NOT NULL,
                label TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (campaign_key, period_key)
            );

            CREATE TABLE IF NOT EXISTS campaign_wins (
                id BIGSERIAL PRIMARY KEY,
                campaign_key TEXT NOT NULL,
                game_id UUID NOT NULL,
                player_name VARCHAR(24) NOT NULL,
                room_id TEXT NOT NULL,
                rule_key TEXT NOT NULL,
                cpu_key TEXT NOT NULL,
                game_started_at TIMESTAMPTZ NOT NULL,
                won_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (campaign_key, game_id)
            );
            ALTER TABLE campaign_wins ADD COLUMN IF NOT EXISTS period_key TEXT;
            UPDATE campaign_wins
            SET period_key = '{LEGACY_PERIOD_KEY}'
            WHERE campaign_key = '{LEGACY_CAMPAIGN_KEY}' AND period_key IS NULL;

            CREATE TABLE IF NOT EXISTS campaign_prime_records (
                id BIGSERIAL PRIMARY KEY,
                campaign_key TEXT NOT NULL,
                period_key TEXT NOT NULL,
                game_id UUID NOT NULL,
                player_name VARCHAR(24) NOT NULL,
                prime_value NUMERIC NOT NULL,
                digit_count INTEGER NOT NULL,
                achieved_at TIMESTAMPTZ NOT NULL,
                UNIQUE (campaign_key, period_key, game_id, player_name)
            );

            CREATE INDEX IF NOT EXISTS campaign_wins_period_player_idx
                ON campaign_wins (campaign_key, period_key, player_name);
            CREATE INDEX IF NOT EXISTS campaign_wins_period_won_at_idx
                ON campaign_wins (campaign_key, period_key, won_at);
            CREATE INDEX IF NOT EXISTS campaign_prime_period_value_idx
                ON campaign_prime_records (campaign_key, period_key, prime_value DESC);

            INSERT INTO campaign_periods (
                campaign_key, period_key, starts_at, ends_at, goal, label
            ) VALUES (
                '{LEGACY_CAMPAIGN_KEY}',
                '{LEGACY_PERIOD_KEY}',
                '2026-07-28T11:00:00+00:00',
                '2026-08-02T11:00:00+00:00',
                300,
                '2026/07/28〜08/02 特別開催'
            ) ON CONFLICT (campaign_key, period_key) DO NOTHING;
            """
        )

    @staticmethod
    async def _upsert_period(connection, campaign_key: str, period: CampaignPeriod) -> None:
        await connection.execute(
            """
            INSERT INTO campaign_periods (
                campaign_key, period_key, starts_at, ends_at, goal, label
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (campaign_key, period_key) DO UPDATE SET
                starts_at = EXCLUDED.starts_at,
                ends_at = EXCLUDED.ends_at,
                goal = EXCLUDED.goal,
                label = EXCLUDED.label
            """,
            campaign_key,
            period.key,
            period.starts_at,
            period.ends_at,
            period.goal,
            period.label,
        )

    async def ensure_period(self, campaign_key: str, period: CampaignPeriod) -> None:
        if self.pool is None:
            raise RuntimeError(self.last_error or "campaign database is unavailable")
        async with self.pool.acquire() as connection:
            await self._upsert_period(connection, campaign_key, period)

    async def record_win(
        self,
        *,
        campaign_key: str,
        period: CampaignPeriod,
        game_id: str,
        player_name: str,
        room_id: str,
        rule_key: str,
        cpu_key: str,
        game_started_at: datetime,
        won_at: datetime,
    ) -> dict[str, Any]:
        if self.pool is None:
            raise RuntimeError(self.last_error or "campaign database is unavailable")

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await self._upsert_period(connection, campaign_key, period)
                await connection.execute(
                    """
                    INSERT INTO campaign_wins (
                        campaign_key, period_key, game_id, player_name, room_id,
                        rule_key, cpu_key, game_started_at, won_at
                    ) VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (campaign_key, game_id) DO NOTHING
                    """,
                    campaign_key,
                    period.key,
                    game_id,
                    player_name,
                    room_id,
                    rule_key,
                    cpu_key,
                    game_started_at,
                    won_at,
                )
                counts = await connection.fetchrow(
                    """
                    SELECT
                        COUNT(*)::int AS total_wins,
                        COUNT(*) FILTER (WHERE player_name = $3)::int AS player_wins
                    FROM campaign_wins
                    WHERE campaign_key = $1 AND period_key = $2
                    """,
                    campaign_key,
                    period.key,
                    player_name,
                )

        return {
            "total_wins": int(counts["total_wins"]),
            "player_wins": int(counts["player_wins"]),
        }

    async def record_prime(
        self,
        *,
        campaign_key: str,
        period: CampaignPeriod,
        game_id: str,
        player_name: str,
        prime_value: int,
        achieved_at: datetime,
    ) -> None:
        if self.pool is None:
            raise RuntimeError(self.last_error or "campaign database is unavailable")
        value_text = str(prime_value)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await self._upsert_period(connection, campaign_key, period)
                await connection.execute(
                    """
                    INSERT INTO campaign_prime_records (
                        campaign_key, period_key, game_id, player_name,
                        prime_value, digit_count, achieved_at
                    ) VALUES ($1, $2, $3::uuid, $4, $5::numeric, $6, $7)
                    ON CONFLICT (campaign_key, period_key, game_id, player_name)
                    DO UPDATE SET
                        prime_value = GREATEST(
                            campaign_prime_records.prime_value,
                            EXCLUDED.prime_value
                        ),
                        digit_count = GREATEST(
                            campaign_prime_records.digit_count,
                            EXCLUDED.digit_count
                        ),
                        achieved_at = CASE
                            WHEN EXCLUDED.prime_value > campaign_prime_records.prime_value
                            THEN EXCLUDED.achieved_at
                            ELSE campaign_prime_records.achieved_at
                        END
                    """,
                    campaign_key,
                    period.key,
                    game_id,
                    player_name,
                    value_text,
                    len(value_text),
                    achieved_at,
                )

    async def overview(
        self,
        campaign_key: str,
        period: CampaignPeriod,
        limit: int = 20,
    ) -> dict[str, Any]:
        if self.pool is None:
            raise RuntimeError(self.last_error or "campaign database is unavailable")

        async with self.pool.acquire() as connection:
            await self._upsert_period(connection, campaign_key, period)
            total_wins = await connection.fetchval(
                """
                SELECT COUNT(*)::int FROM campaign_wins
                WHERE campaign_key = $1 AND period_key = $2
                """,
                campaign_key,
                period.key,
            )
            rows = await connection.fetch(
                """
                WITH player_totals AS (
                    SELECT player_name, COUNT(*)::int AS wins, MAX(won_at) AS reached_at
                    FROM campaign_wins
                    WHERE campaign_key = $1 AND period_key = $2
                    GROUP BY player_name
                )
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY wins DESC, reached_at ASC, player_name ASC
                    )::int AS rank,
                    player_name,
                    wins
                FROM player_totals
                ORDER BY wins DESC, reached_at ASC, player_name ASC
                LIMIT $3
                """,
                campaign_key,
                period.key,
                limit,
            )
            prime_rows = await connection.fetch(
                """
                WITH player_bests AS (
                    SELECT DISTINCT ON (player_name)
                        player_name, prime_value, achieved_at
                    FROM campaign_prime_records
                    WHERE campaign_key = $1 AND period_key = $2
                    ORDER BY player_name, prime_value DESC, achieved_at ASC
                )
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY prime_value DESC, achieved_at ASC, player_name ASC
                    )::int AS rank,
                    player_name,
                    prime_value::text AS prime_value,
                    LENGTH(prime_value::text)::int AS digit_count
                FROM player_bests
                ORDER BY prime_value DESC, achieved_at ASC, player_name ASC
                LIMIT $3
                """,
                campaign_key,
                period.key,
                limit,
            )
            last_updated_at = await connection.fetchval(
                """
                SELECT MAX(updated_at) FROM (
                    SELECT MAX(won_at) AS updated_at FROM campaign_wins
                    WHERE campaign_key = $1 AND period_key = $2
                    UNION ALL
                    SELECT MAX(achieved_at) AS updated_at FROM campaign_prime_records
                    WHERE campaign_key = $1 AND period_key = $2
                ) updates
                """,
                campaign_key,
                period.key,
            )

        return {
            "total_wins": int(total_wins or 0),
            "rankings": [
                {
                    "rank": int(row["rank"]),
                    "player_name": row["player_name"],
                    "wins": int(row["wins"]),
                }
                for row in rows
            ],
            "prime_rankings": [
                {
                    "rank": int(row["rank"]),
                    "player_name": row["player_name"],
                    "prime_value": row["prime_value"],
                    "digit_count": int(row["digit_count"]),
                }
                for row in prime_rows
            ],
            "last_updated_at": (
                last_updated_at.astimezone(timezone.utc).isoformat()
                if last_updated_at is not None
                else None
            ),
        }

    async def history(self, campaign_keys: tuple[str, ...], limit: int = 12) -> list[dict[str, Any]]:
        if self.pool is None:
            raise RuntimeError(self.last_error or "campaign database is unavailable")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    periods.campaign_key,
                    periods.period_key,
                    periods.starts_at,
                    periods.ends_at,
                    periods.goal,
                    periods.label,
                    COUNT(wins.id)::int AS total_wins,
                    COUNT(DISTINCT wins.player_name)::int AS participant_count,
                    (
                        SELECT player_name FROM campaign_wins AS winner_rows
                        WHERE winner_rows.campaign_key = periods.campaign_key
                          AND winner_rows.period_key = periods.period_key
                        GROUP BY player_name
                        ORDER BY COUNT(*) DESC, MAX(won_at) ASC, player_name ASC
                        LIMIT 1
                    ) AS winner_name,
                    (
                        SELECT COUNT(*)::int FROM campaign_wins AS winner_count_rows
                        WHERE winner_count_rows.campaign_key = periods.campaign_key
                          AND winner_count_rows.period_key = periods.period_key
                        GROUP BY player_name
                        ORDER BY COUNT(*) DESC, MAX(won_at) ASC, player_name ASC
                        LIMIT 1
                    ) AS winner_wins,
                    (
                        SELECT MAX(prime_value)::text FROM campaign_prime_records AS prime_rows
                        WHERE prime_rows.campaign_key = periods.campaign_key
                          AND prime_rows.period_key = periods.period_key
                    ) AS largest_prime
                FROM campaign_periods AS periods
                LEFT JOIN campaign_wins AS wins
                  ON wins.campaign_key = periods.campaign_key
                 AND wins.period_key = periods.period_key
                WHERE periods.campaign_key = ANY($1::text[])
                GROUP BY
                    periods.campaign_key, periods.period_key, periods.starts_at,
                    periods.ends_at, periods.goal, periods.label
                ORDER BY periods.starts_at DESC
                LIMIT $2
                """,
                list(campaign_keys),
                limit,
            )
        return [
            {
                "campaign_key": row["campaign_key"],
                "period_key": row["period_key"],
                "starts_at": row["starts_at"].astimezone(timezone.utc).isoformat(),
                "ends_at": row["ends_at"].astimezone(timezone.utc).isoformat(),
                "goal": int(row["goal"]),
                "label": row["label"],
                "total_wins": int(row["total_wins"]),
                "participant_count": int(row["participant_count"]),
                "winner_name": row["winner_name"],
                "winner_wins": int(row["winner_wins"] or 0),
                "largest_prime": row["largest_prime"],
            }
            for row in rows
        ]
