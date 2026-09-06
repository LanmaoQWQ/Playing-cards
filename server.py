#!/usr/bin/env python3
"""Bluff card game server.

Serves the static frontend and a small JSON/SSE API. Uses only the Python
standard library so the host can double-click start.bat without installing
anything.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import queue
import random
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import bots

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8080
MIN_PLAYERS = 2
MAX_PLAYERS = 6
TURN_TIMEOUT_SECONDS = 60
MAX_CARDS_PER_PLAY = 8
DECK_COUNT = 2
ROOM_IDLE_TIMEOUT_SECONDS = 300  # 全员离线超过此时长后回收房间
ROOM_ABORT_GRACE_SECONDS = 60    # 对局中全员离开后的作废倒计时（期间可重连）
CLEANUP_INTERVAL_SECONDS = 5.0

AI_STRENGTHS = ("easy", "medium", "hard")
AI_STRENGTH_LABELS = {"easy": "简单", "medium": "中等", "hard": "困难"}
AI_DELAY_RANGES = {"easy": (4.0, 5.0), "medium": (3.5, 4.5), "hard": (3.0, 4.0)}
AI_DEFAULT_NAMES = ["小明", "小红", "老王", "小刚", "阿强", "翠花"]

RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUITS = ["♠", "♥", "♣", "♦"]
JOKER_BIG_SUIT = "★"
JOKER_SMALL_SUIT = "☆"
JOKER_RANK_ORDER = {"大王": 14, "小王": 13}
RANK_VALUES = {rank: index for index, rank in enumerate(RANKS)}
SUIT_ORDER = {suit: index for index, suit in enumerate(SUITS)}
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SPADE_THREE = "♠3"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bluff.db"
PBKDF2_ITERATIONS = 120_000
AUTH_TOKEN_BYTES = 32
LEADERBOARD_DEFAULT_LIMIT = 20

# ---- 排位模式（段位积分） ----
# 段位门槛：积分达到 threshold 即晋升对应段位。
TIERS: list[tuple[str, str, int]] = [
    ("bronze", "青铜", 0),
    ("silver", "白银", 100),
    ("gold", "黄金", 200),
    ("platinum", "铂金", 350),
    ("diamond", "钻石", 550),
    ("master", "宗师", 800),
    ("god", "赌神", 1100),
]
PLACEMENT_GAMES = 3   # 新账号前 3 局排位为定级赛
PLACEMENT_WIN = 100   # 定级赛赢一局固定加分
PLACEMENT_LOSS = -30  # 定级赛输一局固定扣分
RANK_BASE_LOSS = 15   # 每个输家基础扣分；赢家基础分 = RANK_BASE_LOSS × 输家人数（奖池制）
RANK_FLOOR = 0        # 积分下限，不出现负分


class GameError(Exception):
    """Expected game-rule error that can be returned to the client."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class AccountStore:
    """SQLite-backed accounts, sessions, stats and match history."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path or DB_PATH)
        self.lock = threading.RLock()
        self.sessions: dict[str, str] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_norm TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_stats (
                    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
                    games_played INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    bluff_attempts INTEGER NOT NULL DEFAULT 0,
                    bluff_successes INTEGER NOT NULL DEFAULT 0,
                    challenge_attempts INTEGER NOT NULL DEFAULT 0,
                    challenge_successes INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS matches (
                    id TEXT PRIMARY KEY,
                    room_code TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    winner_account_id TEXT,
                    winner_name TEXT NOT NULL,
                    ended_reason TEXT NOT NULL,
                    report_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS match_participants (
                    match_id TEXT NOT NULL REFERENCES matches(id),
                    account_id TEXT,
                    player_name TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_participants_account
                    ON match_participants(account_id);
                """
            )

    @staticmethod
    def _clean_username(username: str) -> str:
        username = str(username or "").strip()
        if not 3 <= len(username) <= 16:
            raise GameError("账号用户名需为 3–16 个字符")
        if any(ch.isspace() or ord(ch) < 32 for ch in username):
            raise GameError("账号用户名不能包含空格或控制字符")
        return username

    @staticmethod
    def _clean_password(password: str) -> str:
        password = str(password or "")
        if not 4 <= len(password) <= 64:
            raise GameError("密码需为 4–64 个字符")
        if any(ord(ch) < 32 for ch in password):
            raise GameError("密码不能包含控制字符")
        return password

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            _algo, iterations, salt_hex, hash_hex = encoded.split("$")
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, int(iterations)
            )
            return hmac.compare_digest(digest, expected)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(12)}"

    @staticmethod
    def _stats_dict(
        games_played: int = 0,
        wins: int = 0,
        bluff_attempts: int = 0,
        bluff_successes: int = 0,
        challenge_attempts: int = 0,
        challenge_successes: int = 0,
    ) -> dict[str, Any]:
        return {
            "games_played": games_played,
            "wins": wins,
            "win_rate": (wins / games_played) if games_played else 0,
            "bluff_attempts": bluff_attempts,
            "bluff_successes": bluff_successes,
            "bluff_success_rate": (bluff_successes / bluff_attempts) if bluff_attempts else 0,
            "challenge_attempts": challenge_attempts,
            "challenge_successes": challenge_successes,
            "challenge_accuracy": (challenge_successes / challenge_attempts) if challenge_attempts else 0,
        }

    def _account_row(self, account_id: str) -> dict[str, Any]:
        with self.lock, closing(self._connect()) as conn, conn:
            account = conn.execute(
                "SELECT id, username, created_at FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise GameError("账号不存在", 404)
            stats = conn.execute(
                """
                SELECT games_played, wins, bluff_attempts, bluff_successes,
                       challenge_attempts, challenge_successes
                FROM account_stats WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            values = tuple(stats) if stats else (0, 0, 0, 0, 0, 0)
            return {
                "id": account[0],
                "username": account[1],
                "created_at": account[2],
                "stats": self._stats_dict(*values),
            }

    def register(self, username: str, password: str) -> dict[str, Any]:
        username = self._clean_username(username)
        password = self._clean_password(password)
        username_norm = username.casefold()
        account_id = self._new_id("acc")
        password_hash = self._hash_password(password)
        created_at = utc_now()
        with self.lock, closing(self._connect()) as conn, conn:
            exists = conn.execute(
                "SELECT 1 FROM accounts WHERE username_norm = ?",
                (username_norm,),
            ).fetchone()
            if exists:
                raise GameError("该用户名已被注册")
            conn.execute(
                """
                INSERT INTO accounts (id, username, username_norm, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, username, username_norm, password_hash, created_at),
            )
            conn.execute(
                """
                INSERT INTO account_stats (account_id, updated_at)
                VALUES (?, ?)
                """,
                (account_id, utc_now()),
            )
        return self._account_row(account_id)

    def login(self, username: str, password: str) -> dict[str, Any]:
        username = str(username or "").strip()
        password = str(password or "")
        with self.lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                """
                SELECT id, password_hash FROM accounts
                WHERE username_norm = ?
                """,
                (username.casefold(),),
            ).fetchone()
        if row is None or not self._verify_password(password, row[1]):
            raise GameError("用户名或密码错误", 401)
        return self._account_row(row[0])

    def create_session(self, account_id: str) -> str:
        token = secrets.token_urlsafe(AUTH_TOKEN_BYTES)
        with self.lock:
            self.sessions[token] = account_id
        return token

    def account_id_for_token(self, token: str | None) -> str | None:
        if not token:
            return None
        with self.lock:
            account_id = self.sessions.get(token)
        if account_id is None:
            raise GameError("登录已失效，请重新登录", 401)
        return account_id

    def logout(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    def public_account(self, account_id: str) -> dict[str, Any]:
        return self._account_row(account_id)

    def leaderboard(self, limit: int = LEADERBOARD_DEFAULT_LIMIT) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT a.id, a.username, s.games_played, s.wins,
                       s.bluff_attempts, s.bluff_successes,
                       s.challenge_attempts, s.challenge_successes
                FROM account_stats s
                JOIN accounts a ON a.id = s.account_id
                WHERE s.games_played > 0
                ORDER BY s.wins DESC,
                         CASE WHEN s.games_played > 0 THEN s.wins * 1.0 / s.games_played ELSE 0 END DESC,
                         s.games_played DESC,
                         a.username ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "account_id": row[0],
                "username": row[1],
                "stats": self._stats_dict(*row[2:]),
            }
            for row in rows
        ]

    def recent_matches(self, account_id: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self.lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT m.id, m.room_code, m.started_at, m.finished_at,
                       m.winner_account_id, m.winner_name, m.ended_reason,
                       m.report_json
                FROM matches m
                JOIN match_participants mp ON mp.match_id = m.id
                WHERE mp.account_id = ?
                ORDER BY m.finished_at DESC
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        matches = []
        for row in rows:
            try:
                report = json.loads(row[7])
            except (TypeError, json.JSONDecodeError):
                report = {}
            matches.append(
                {
                    "match_id": row[0],
                    "room_code": row[1],
                    "started_at": row[2],
                    "finished_at": row[3],
                    "winner_account_id": row[4],
                    "winner_name": row[5],
                    "ended_reason": row[6],
                    "players": [p.get("name") for p in report.get("players", [])],
                }
            )
        return matches

    def record_match(self, report: dict[str, Any]) -> None:
        match_id = report["game_id"]
        with self.lock, closing(self._connect()) as conn, conn:
            exists = conn.execute("SELECT 1 FROM matches WHERE id = ?", (match_id,)).fetchone()
            if exists:
                return
            conn.execute(
                """
                INSERT INTO matches
                    (id, room_code, started_at, finished_at, winner_account_id,
                     winner_name, ended_reason, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    report.get("room_code", ""),
                    report.get("started_at", ""),
                    report.get("finished_at", ""),
                    report.get("winner_account_id"),
                    report.get("winner_name", ""),
                    report.get("ended_reason", ""),
                    json.dumps(report, ensure_ascii=False),
                ),
            )
            for player in report.get("players", []):
                account_id = player.get("account_id") if not player.get("is_guest") else None
                conn.execute(
                    """
                    INSERT INTO match_participants (match_id, account_id, player_name)
                    VALUES (?, ?, ?)
                    """,
                    (match_id, account_id, player.get("name", "")),
                )
                if account_id is None:
                    continue
                stats = player.get("stats", {})
                conn.execute(
                    """
                    INSERT INTO account_stats
                        (account_id, games_played, wins, bluff_attempts,
                         bluff_successes, challenge_attempts, challenge_successes, updated_at)
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        games_played = games_played + 1,
                        wins = wins + excluded.wins,
                        bluff_attempts = bluff_attempts + excluded.bluff_attempts,
                        bluff_successes = bluff_successes + excluded.bluff_successes,
                        challenge_attempts = challenge_attempts + excluded.challenge_attempts,
                        challenge_successes = challenge_successes + excluded.challenge_successes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        account_id,
                        1 if player.get("is_winner") else 0,
                        stats.get("bluff_attempts", 0),
                        stats.get("bluff_successes", 0),
                        stats.get("challenge_attempts", 0),
                        stats.get("challenge_successes", 0),
                        utc_now(),
                    ),
                )


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def get_lan_ip() -> str:
    """Best-effort detection of the host LAN / VPN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def make_deck() -> list[str]:
    deck: list[str] = []
    for copy_index in range(1, DECK_COUNT + 1):
        for suit in SUITS:
            for rank in RANKS:
                deck.append(f"{suit}{rank}#{copy_index}")
        deck.append(f"{JOKER_BIG_SUIT}大王#{copy_index}")
        deck.append(f"{JOKER_SMALL_SUIT}小王#{copy_index}")
    return deck


def rank_from_card_id(card_id: str) -> str:
    # Card IDs are suit-symbol + rank + optional "#copy", e.g. "S10#1".
    return card_id[1:].split("#", 1)[0]


def suit_from_card_id(card_id: str) -> str:
    return card_id[:1]


def is_spade_three(card_id: str) -> bool:
    return suit_from_card_id(card_id) == "\u2660" and rank_from_card_id(card_id) == "3"


def is_joker(card_id: str) -> bool:
    """Big/small jokers act as wildcards matching any declared rank."""
    return card_id[:1] in (JOKER_BIG_SUIT, JOKER_SMALL_SUIT)


def matches_declared(card_id: str, declared_rank: str) -> bool:
    return is_joker(card_id) or rank_from_card_id(card_id) == declared_rank


def _rank_order(card_id: str) -> int:
    rank = rank_from_card_id(card_id)
    if rank in JOKER_RANK_ORDER:
        return JOKER_RANK_ORDER[rank]
    return RANK_VALUES.get(rank, -1)


def sort_hand(hand: list[str]) -> list[str]:
    return sorted(
        hand,
        key=lambda card: (
            _rank_order(card),
            SUIT_ORDER.get(suit_from_card_id(card), -1),
            card,
        ),
    )


class Player:
    def __init__(
        self,
        player_id: str,
        name: str,
        token: str,
        is_host: bool,
        account_id: str | None = None,
        is_guest: bool | None = None,
        is_ai: bool = False,
        ai_strength: str | None = None,
    ) -> None:
        self.id = player_id
        self.name = name
        self.token = token
        self.is_host = is_host
        self.account_id = account_id
        self.is_guest = account_id is None if is_guest is None else is_guest
        self.is_ai = is_ai
        self.ai_strength = ai_strength
        self.connected = not is_ai
        self.hand: list[str] = []
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "is_host": self.is_host,
            "connected": self.connected,
            "hand_count": len(self.hand),
            "account_id": self.account_id,
            "is_guest": self.is_guest,
            "is_ai": self.is_ai,
            "ai_strength": self.ai_strength,
        }


class Room:
    def __init__(self, code: str, host: Player) -> None:
        self.code = code
        self.host_player_id = host.id
        self.players: list[Player] = []
        self.lock = threading.RLock()
        self.state = "waiting"
        self.winner_id: str | None = None
        self.declared_rank: str | None = None
        self.current_round_leader_id: str | None = None
        self.current_player_id: str | None = None
        self.current_player_index = 0
        self.round_seq = 0
        self.next_play_id = 1
        self.current_round_plays: list[dict[str, Any]] = []
        self.round_has_returned = False
        self.round_passers: set[str] = set()
        self.empty_order: list[str] = []
        self.turn_deadline: float | None = None
        self.last_challenge_result: dict[str, Any] | None = None
        self.last_round_ended_reason: str | None = None
        self.chat_messages: list[dict[str, Any]] = []
        self.next_chat_id = 1
        self.event_seq = 0
        self.last_activity = time.time()
        self.abort_deadline: float | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.round_reports: list[dict[str, Any]] = []
        self.game_report: dict[str, Any] | None = None
        self.recorded = False
        self.ai_reveals: list[dict[str, Any]] = []
        self.on_game_finished: Callable[[Room], None] | None = None
        self.players.append(host)

    def find_player_by_token(self, token: str | None) -> Player | None:
        if not token:
            return None
        with self.lock:
            for player in self.players:
                if player.token == token:
                    return player
        return None

    def find_player_by_id(self, player_id: str) -> Player | None:
        with self.lock:
            for player in self.players:
                if player.id == player_id:
                    return player
        return None

    def player_index(self, player_id: str) -> int:
        with self.lock:
            for index, player in enumerate(self.players):
                if player.id == player_id:
                    return index
        raise GameError("找不到玩家")

    def _start_timer_locked(self) -> None:
        delay = TURN_TIMEOUT_SECONDS
        current = self.find_player_by_id(self.current_player_id)
        if current is not None and current.is_ai:
            low, high = AI_DELAY_RANGES.get(
                current.ai_strength or "easy", AI_DELAY_RANGES["easy"]
            )
            delay = random.uniform(low, high)
        self.turn_deadline = time.time() + delay

    def _public_play(self, play: dict[str, Any]) -> dict[str, Any]:
        return {
            "play_id": play["play_id"],
            "player_id": play["player_id"],
            "player_name": play["player_name"],
            "declared_rank": play["declared_rank"],
            "count": len(play["card_ids"]),
            "round_seq": play["round_seq"],
        }

    def state_for(self, player: Player) -> dict[str, Any]:
        with self.lock:
            if self.state == "playing":
                hand = sort_hand(list(player.hand))
            else:
                hand = []

            game: dict[str, Any] = {
                "current_player_id": self.current_player_id,
                "declared_rank": self.declared_rank,
                "round_leader_id": self.current_round_leader_id,
                "round_seq": self.round_seq,
                "turn_deadline": self.turn_deadline,
                "table": [self._public_play(play) for play in self.current_round_plays],
                "round_has_returned": self.round_has_returned,
                "round_passers": sorted(self.round_passers),
                "winner_id": self.winner_id,
                "last_challenge_result": self.last_challenge_result,
                "report": self.game_report,
            }

            return {
                "room_code": self.code,
                "state": self.state,
                "host_player_id": self.host_player_id,
                "you": {
                    "player_id": player.id,
                    "name": player.name,
                    "is_host": player.is_host,
                    "account_id": player.account_id,
                    "is_guest": player.is_guest,
                    "hand": hand,
                },
                "players": [p.public_dict() for p in self.players],
                "chat_messages": list(self.chat_messages[-100:]),
                "game": game,
            }

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.event_seq += 1
            self.last_activity = time.time()
            seq = self.event_seq
            players = list(self.players)
            for player in players:
                event = {
                    "type": event_type,
                    "seq": seq,
                    "room_code": self.code,
                    "data": data or {},
                    "snapshot": self.state_for(player),
                    "sent_at": time.time(),
                }
                try:
                    player.queue.put_nowait(event)
                except queue.Full:
                    pass

    def reset_queue(self, player: Player) -> None:
        with self.lock:
            player.queue = queue.Queue(maxsize=256)

    def add_ai(
        self,
        host: Player,
        strength: str,
        name: str | None = None,
    ) -> Player:
        """Waiting-room only: the host adds an AI opponent seat."""
        with self.lock:
            if host.id != self.host_player_id:
                raise GameError("只有房主可以添加 AI")
            if self.state != "waiting":
                raise GameError("只有等待房间可以添加 AI")
            if strength not in AI_STRENGTHS:
                raise GameError("AI 强度无效")
            if len(self.players) >= MAX_PLAYERS:
                raise GameError("房间已满，无法添加 AI")
            display_name = (name or "").strip()[:16]
            if not display_name:
                ai_count = sum(1 for p in self.players if p.is_ai)
                label = AI_STRENGTH_LABELS[strength]
                display_name = f"AI·{label}{ai_count + 1}"
            player = Player(
                f"p_{secrets.token_hex(12)}",
                display_name,
                f"t_{secrets.token_hex(12)}",
                False,
                None,
                True,
                is_ai=True,
                ai_strength=strength,
            )
            self.players.append(player)
            player_dict = player.public_dict()
        self.publish("player_joined", {"player": player_dict})
        return player

    def remove_ai(self, host: Player, player_id: str) -> None:
        with self.lock:
            if host.id != self.host_player_id:
                raise GameError("只有房主可以移除 AI")
            if self.state != "waiting":
                raise GameError("只有等待房间可以移除 AI")
            target = self.find_player_by_id(player_id)
            if target is None or not target.is_ai:
                raise GameError("找不到要移除的 AI")
            self.players.remove(target)
        self.publish("player_left", {"player_id": player_id})

    def set_ai_strength(self, host: Player, player_id: str, strength: str) -> None:
        with self.lock:
            if host.id != self.host_player_id:
                raise GameError("只有房主可以修改 AI")
            if self.state != "waiting":
                raise GameError("只有等待房间可以修改 AI 强度")
            if strength not in AI_STRENGTHS:
                raise GameError("AI 强度无效")
            target = self.find_player_by_id(player_id)
            if target is None or not target.is_ai:
                raise GameError("找不到要修改的 AI")
            target.ai_strength = strength
        self.publish("lobby_updated", {})

    def start(self, player: Player) -> None:
        with self.lock:
            if player.id != self.host_player_id:
                raise GameError("只有房主可以开始游戏")
            if self.state == "playing":
                raise GameError("游戏已经开始")
            if len(self.players) < MIN_PLAYERS:
                raise GameError(f"至少需要 {MIN_PLAYERS} 名玩家")

            deck = make_deck()
            random.shuffle(deck)
            count = len(self.players)
            base = len(deck) // count
            remainder = len(deck) % count
            counts = [base] * count
            for index in random.sample(range(count), remainder):
                counts[index] += 1

            cursor = 0
            for player, hand_size in zip(self.players, counts):
                player.hand = sort_hand(deck[cursor : cursor + hand_size])
                cursor += hand_size

            spade_three_player = next(
                (p for p in self.players if any(is_spade_three(card) for card in p.hand)),
                self.players[0],
            )

            self.state = "playing"
            self.winner_id = None
            self.started_at = utc_now()
            self.finished_at = None
            self.round_reports = []
            self.game_report = None
            self.recorded = False
            self.ai_reveals = []
            self.empty_order = []
            self.round_seq = 0
            self.next_play_id = 1
            self.declared_rank = None
            self.current_round_plays = []
            self.round_has_returned = False
            self.round_passers = set()
            self.last_challenge_result = None
            self.last_round_ended_reason = None
            self.abort_deadline = None
            self.current_round_leader_id = spade_three_player.id
            self.current_player_id = spade_three_player.id
            self.current_player_index = self.player_index(spade_three_player.id)
            self._start_timer_locked()

        self.publish(
            "game_started",
            {
                "leader_player_id": spade_three_player.id,
                "leader_name": spade_three_player.name,
            },
        )

    def play(
        self,
        player: Player,
        card_ids: list[str],
        declared_rank: str | None,
        automatic: bool = False,
    ) -> None:
        with self.lock:
            self._validate_turn_locked(player)
            if not card_ids:
                raise GameError("请选择要出的牌")
            if not 1 <= len(card_ids) <= MAX_CARDS_PER_PLAY:
                raise GameError(f"一次只能出 1–{MAX_CARDS_PER_PLAY} 张牌")
            if len(set(card_ids)) != len(card_ids):
                raise GameError("不能重复选择同一张牌")
            for card_id in card_ids:
                if card_id not in player.hand:
                    raise GameError("所选牌不在你的手牌中")

            if self.declared_rank is None:
                if declared_rank not in RANKS:
                    raise GameError("首家必须报一个有效点数")
                self.declared_rank = declared_rank
            elif declared_rank != self.declared_rank:
                raise GameError(f"本轮必须报 {self.declared_rank}")

            play = {
                "play_id": self.next_play_id,
                "player_id": player.id,
                "player_name": player.name,
                "account_id": player.account_id,
                "is_guest": player.is_guest,
                "declared_rank": self.declared_rank,
                "card_ids": list(card_ids),
                "round_seq": self.round_seq,
                "created_at": time.time(),
            }
            self.next_play_id += 1
            self.current_round_plays.append(play)

            for card_id in card_ids:
                player.hand.remove(card_id)
            player.hand = sort_hand(player.hand)

            if not player.hand and player.id not in self.empty_order:
                self.empty_order.append(player.id)

            next_index = self._next_active_index_locked(self.current_player_index)
            if next_index is not None:
                self._set_current_locked(next_index)
            round_ended_reason = None

        self.publish(
            "played",
            {
                "play": self._public_play(play),
                "automatic": automatic,
                "player_name": player.name,
            },
        )
        if round_ended_reason:
            self.publish("round_ended", {"reason": round_ended_reason})
        if self.state == "finished":
            self.publish("game_over", {"winner_id": self.winner_id})

    def challenge(
        self,
        player: Player,
        target_play_id: int | None,
        automatic: bool = False,
    ) -> None:
        with self.lock:
            self._validate_turn_locked(player)
            if not self.current_round_plays:
                raise GameError("桌面上还没有牌可质疑")
            target = self.current_round_plays[-1]
            if target_play_id != target["play_id"]:
                raise GameError("只能质疑上一个出牌的人")
            if target["player_id"] == player.id:
                raise GameError("不能质疑自己刚出的牌")

            truth = all(
                matches_declared(card_id, target["declared_rank"])
                for card_id in target["card_ids"]
            )
            challenger_id = player.id
            target_player_id = target["player_id"]
            loser_id = challenger_id if truth else target_player_id
            winner_id = target_player_id if truth else challenger_id

            pile_cards: list[str] = []
            for play in self.current_round_plays:
                pile_cards.extend(play["card_ids"])

            loser = self.find_player_by_id(loser_id)
            if loser is None:
                raise GameError("找不到结算玩家")
            loser.hand.extend(pile_cards)
            loser.hand = sort_hand(loser.hand)
            self.empty_order = [
                empty_id for empty_id in self.empty_order if empty_id != loser_id
            ]

            result = {
                "target_play_id": target["play_id"],
                "target_player_id": target_player_id,
                "target_player_name": target["player_name"],
                "challenger_id": challenger_id,
                "challenger_name": player.name,
                "declared_rank": target["declared_rank"],
                "actual_cards": sort_hand(target["card_ids"]),
                "pile_cards": sort_hand(pile_cards),
                "truth": truth,
                "winner_id": winner_id,
                "loser_id": loser_id,
                "pile_count": len(pile_cards),
            }
            self.last_challenge_result = result
            self.ai_reveals.append(
                {
                    "player_id": target_player_id,
                    "declared_rank": target["declared_rank"],
                    "actual_cards": sort_hand(target["card_ids"]),
                    "was_bluff": not truth,
                }
            )
            self.ai_reveals = self.ai_reveals[-200:]
            self.round_reports.append(
                self._make_round_report_locked(
                    "challenge",
                    {
                        "challenger_id": challenger_id,
                        "challenger_name": player.name,
                        "target_play_id": target["play_id"],
                        "target_player_id": target_player_id,
                        "target_player_name": target["player_name"],
                        "declared_rank": target["declared_rank"],
                        "actual_cards": sort_hand(target["card_ids"]),
                        "success": not truth,
                    },
                )
            )
            self.current_round_plays = []
            self.round_seq += 1
            self.current_round_leader_id = winner_id
            self.current_player_id = winner_id
            self.current_player_index = self.player_index(winner_id)
            self.declared_rank = None
            self.last_round_ended_reason = "challenge"
            self.round_has_returned = False
            self.round_passers = set()

            if self.empty_order:
                self._finish_game_locked(self.empty_order[0])
            else:
                self._start_timer_locked()

        self.publish("challenged", {"result": result, "automatic": automatic})
        self.publish("round_ended", {"reason": "challenge"})
        if self.state == "finished":
            self.publish("game_over", {"winner_id": self.winner_id})
            self._record_finished()

    def pass_turn(self, player: Player, automatic: bool = False) -> None:
        with self.lock:
            self._validate_turn_locked(player)
            if self.declared_rank is None:
                raise GameError("首家必须先出牌报点，不能过牌")
            if player.id in self.round_passers:
                raise GameError("本轮已经过牌，不能再行动")
            # 过牌 = 本轮出局：直到本轮结束不再参与。
            self.round_passers.add(player.id)
            next_index = self._next_active_index_locked(self.current_player_index)
            if next_index is None:
                # 全员都已过牌 → 本轮无质疑结束。
                reason = self._end_round_no_challenge_locked()
            else:
                self._set_current_locked(next_index)
                reason = None

        self.publish("passed", {"player_name": player.name, "automatic": automatic})
        if reason:
            self.publish("round_ended", {"reason": reason})
        if self.state == "finished":
            self.publish("game_over", {"winner_id": self.winner_id})
            self._record_finished()

    def chat(self, player: Player, text: str) -> None:
        message_text = str(text or "").strip()
        message_text = "".join(ch for ch in message_text if ord(ch) >= 32)
        if not message_text:
            raise GameError("消息不能为空")
        if len(message_text) > 200:
            message_text = message_text[:200]

        with self.lock:
            message = {
                "chat_id": self.next_chat_id,
                "player_id": player.id,
                "player_name": player.name,
                "text": message_text,
                "created_at": time.time(),
            }
            self.next_chat_id += 1
            self.chat_messages.append(message)
            self.chat_messages = self.chat_messages[-100:]

        self.publish("chat_message", {"message": message})


    def _schedule_abort_if_empty_locked(self) -> None:
        """Freeze and schedule room teardown once every player left mid-game."""
        if self.state != "playing":
            return
        if self.abort_deadline is not None:
            return
        if self.players and all(not p.connected for p in self.players):
            self.abort_deadline = time.time() + ROOM_ABORT_GRACE_SECONDS

    def _cancel_abort_locked(self) -> None:
        self.abort_deadline = None

    def tick(self, now: float) -> None:
        with self.lock:
            if self.state != "playing":
                return
            if self.abort_deadline is not None:
                # 全员离线等待作废倒计时；暂停自动代打，等待重连或回收。
                return
            if self.turn_deadline is None or now < self.turn_deadline:
                return

            current = self.current_player()
            if current is None:
                return
            token = current.token

            if current.is_ai:
                try:
                    self._run_ai_turn_locked(current)
                except GameError:
                    self._ai_fallback_auto_locked(current)
                return

            if self.current_round_plays:
                target = self.current_round_plays[-1]
                if target["player_id"] != current.id:
                    self.challenge(current, target["play_id"], automatic=True)
                    return

            if not current.hand:
                if self.declared_rank is None:
                    # 防御性兜底：开局轮次不可能无手牌又未报点。
                    self._end_round_no_challenge_locked()
                else:
                    self.pass_turn(current, automatic=True)
                return
            card_id = current.hand[0]
            rank = rank_from_card_id(card_id)
            if rank not in RANKS:
                # 手牌只有王时自动出王并按默认报点 2（王匹配任意报点，判定为真）。
                rank = "2"
            self.play(
                current,
                [card_id],
                rank,
                automatic=True,
            )

    def _run_ai_turn_locked(self, player: Player) -> None:
        ctx = {
            "hand": list(player.hand),
            "declared_rank": self.declared_rank,
            "table": [self._public_play(p) for p in self.current_round_plays],
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "hand_count": len(p.hand),
                    "is_ai": p.is_ai,
                }
                for p in self.players
            ],
            "own_id": player.id,
            "revealed": list(self.ai_reveals),
        }
        action = bots.decide(ctx, player.ai_strength or "easy")
        kind = action.get("kind")
        if kind == "play":
            cards = action.get("cards") or []
            rank = action.get("declared_rank") or self.declared_rank
            self.play(player, list(cards), rank, automatic=True)
        elif kind == "challenge":
            self.challenge(player, action.get("target_play_id"), automatic=True)
        else:
            self.pass_turn(player, automatic=True)

    def _ai_fallback_auto_locked(self, player: Player) -> None:
        if self.current_round_plays:
            target = self.current_round_plays[-1]
            if target["player_id"] != player.id:
                self.challenge(player, target["play_id"], automatic=True)
                return
        if not player.hand:
            if self.declared_rank is not None:
                self.pass_turn(player, automatic=True)
            return
        card_id = player.hand[0]
        rank = rank_from_card_id(card_id)
        if rank not in RANKS:
            rank = "2"
        self.play(player, [card_id], rank, automatic=True)

    def current_player(self) -> Player | None:
        with self.lock:
            if not self.current_player_id:
                return None
            return self.find_player_by_id(self.current_player_id)

    def leave(self, player: Player) -> None:
        with self.lock:
            player.connected = False
            if self.state == "waiting":
                self.players.remove(player)
                if not self.players:
                    return
                if player.id == self.host_player_id:
                    new_host = next(
                        (p for p in self.players if not p.is_ai),
                        None,
                    )
                    if new_host is not None:
                        self.host_player_id = new_host.id
                        new_host.is_host = True
            self._schedule_abort_if_empty_locked()
        self.publish("player_left", {"player_id": player.id})

    def _validate_turn_locked(self, player: Player) -> None:
        if self.state != "playing":
            raise GameError("游戏尚未开始")
        if player.id != self.current_player_id:
            raise GameError("还没轮到你")

    def _next_active_index_locked(self, after_index: int) -> int | None:
        """Next seat (clockwise) that has not passed this round.

        After a play the acting player is still active, so a full lap without
        any other active player lands back on them; returns None only when
        every player has passed.
        """
        n = len(self.players)
        for step in range(1, n + 1):
            index = (after_index + step) % n
            if self.players[index].id not in self.round_passers:
                return index
        return None

    def _set_current_locked(self, index: int) -> None:
        self.current_player_index = index
        self.current_player_id = self.players[index].id
        if self.current_player_id == self.current_round_leader_id:
            self.round_has_returned = True
        self._start_timer_locked()

    def _end_round_no_challenge_locked(self) -> str:
        self.round_reports.append(self._make_round_report_locked("completed", None))
        self.current_round_plays = []
        self.round_seq += 1
        self.declared_rank = None
        self.last_round_ended_reason = "completed"
        self.round_has_returned = False
        self.round_passers = set()
        self.current_player_id = self.current_round_leader_id
        self.current_player_index = self.player_index(self.current_player_id or "")

        if self.empty_order:
            self._finish_game_locked(self.empty_order[0])
        else:
            self._start_timer_locked()
        return "completed"

    def _is_bluff(self, play: dict[str, Any]) -> bool:
        return not all(
            matches_declared(card_id, play["declared_rank"])
            for card_id in play["card_ids"]
        )

    def _make_round_report_locked(
        self,
        reason: str,
        challenge: dict[str, Any] | None,
    ) -> dict[str, Any]:
        plays: list[dict[str, Any]] = []
        for play in self.current_round_plays:
            is_bluff = self._is_bluff(play)
            bluff_succeeded = is_bluff and (
                challenge is None or play["play_id"] != challenge["target_play_id"]
            )
            plays.append(
                {
                    "play_id": play["play_id"],
                    "player_id": play["player_id"],
                    "account_id": play.get("account_id"),
                    "is_guest": play.get("is_guest", True),
                    "player_name": play["player_name"],
                    "declared_rank": play["declared_rank"],
                    "count": len(play["card_ids"]),
                    "is_bluff": is_bluff,
                    "bluff_succeeded": bluff_succeeded,
                }
            )
        return {
            "round_seq": self.round_seq,
            "reason": reason,
            "plays": plays,
            "challenge": challenge,
        }

    def _player_totals(self, player_id: str) -> dict[str, int]:
        totals = {
            "bluff_attempts": 0,
            "bluff_successes": 0,
            "challenge_attempts": 0,
            "challenge_successes": 0,
        }
        for round_report in self.round_reports:
            for play in round_report["plays"]:
                if play["player_id"] == player_id and play["is_bluff"]:
                    totals["bluff_attempts"] += 1
                    if play["bluff_succeeded"]:
                        totals["bluff_successes"] += 1
            challenge = round_report.get("challenge")
            if challenge and challenge["challenger_id"] == player_id:
                totals["challenge_attempts"] += 1
                if challenge["success"]:
                    totals["challenge_successes"] += 1
        return totals

    def _build_game_report(self, winner_id: str) -> dict[str, Any]:
        players: list[dict[str, Any]] = []
        winner_name = ""
        winner_account_id = None
        for player in self.players:
            is_winner = player.id == winner_id
            if is_winner:
                winner_name = player.name
                winner_account_id = player.account_id if not player.is_guest else None
            players.append(
                {
                    "player_id": player.id,
                    "account_id": player.account_id if not player.is_guest else None,
                    "name": player.name,
                    "is_guest": player.is_guest,
                    "is_winner": is_winner,
                    "stats": self._player_totals(player.id),
                }
            )
        return {
            "game_id": self._new_game_id(),
            "room_code": self.code,
            "started_at": self.started_at or utc_now(),
            "finished_at": self.finished_at or utc_now(),
            "winner_id": winner_id,
            "winner_name": winner_name,
            "winner_account_id": winner_account_id,
            "ended_reason": self.last_round_ended_reason or "completed",
            "players": players,
            "rounds": self.round_reports,
        }

    def _new_game_id(self) -> str:
        return f"m_{secrets.token_hex(12)}"

    def _record_finished(self) -> None:
        with self.lock:
            if self.recorded or self.state != "finished":
                return
            self.recorded = True
            callback = self.on_game_finished
            report = self.game_report
        if callback is not None and report is not None:
            callback(self)

    def return_to_lobby(self, player: Player) -> None:
        """Move a finished game back to the waiting lobby of the same room.

        The room and its members are kept, so the host can start a fresh game
        from the lobby instead of auto-restarting.
        """
        with self.lock:
            player.connected = True
            if self.state == "finished":
                self.state = "waiting"
                self.winner_id = None
                self.declared_rank = None
                self.current_round_leader_id = None
                self.current_player_id = None
                self.current_player_index = 0
                self.round_seq = 0
                self.next_play_id = 1
                self.current_round_plays = []
                self.round_has_returned = False
                self.round_passers = set()
                self.empty_order = []
                self.turn_deadline = None
                self.last_challenge_result = None
                self.last_round_ended_reason = None
                self.abort_deadline = None
                self.game_report = None
                for member in self.players:
                    member.hand = []
            elif self.state != "waiting":
                raise GameError("对局进行中，暂不能返回等待房间")
            else:
                return
        self.publish("back_to_lobby", {})

    def _finish_game_locked(self, winner_id: str) -> None:
        if self.state == "finished":
            return
        self.state = "finished"
        self.winner_id = winner_id
        self.finished_at = utc_now()
        self.current_player_id = None
        self.current_round_plays = []
        self.declared_rank = None
        self.turn_deadline = None
        self.game_report = self._build_game_report(winner_id)


class RoomManager:
    def __init__(self, account_store: AccountStore | None = None) -> None:
        self.rooms: dict[str, Room] = {}
        self.lock = threading.RLock()
        self.account_store = account_store

    def _record_finished_game(self, room: Room) -> None:
        if self.account_store is None or room.game_report is None:
            return
        self.account_store.record_match(room.game_report)

    def create_room(self, name: str, account_id: str | None = None) -> tuple[Room, Player]:
        name = self._clean_name(name)
        player_id = self._new_id("p")
        token = self._new_id("t")
        is_guest = account_id is None
        with self.lock:
            code = self._new_room_code_locked()
            host = Player(player_id, name, token, True, account_id, is_guest)
            room = Room(code, host)
            room.on_game_finished = self._record_finished_game
            self.rooms[code] = room
        return room, host

    def get_room(self, code: str) -> Room:
        code = self._normalize_code(code)
        with self.lock:
            room = self.rooms.get(code)
        if room is None:
            raise GameError("房间不存在", 404)
        return room

    def join_room(self, code: str, name: str, account_id: str | None = None) -> tuple[Room, Player]:
        room = self.get_room(code)
        name = self._clean_name(name)
        is_guest = account_id is None
        with room.lock:
            if room.state not in ("waiting", "playing"):
                raise GameError("该房间已结束")
            if account_id is not None and any(
                p.account_id == account_id and p.connected for p in room.players
            ):
                raise GameError("该账号已在房间中")
            existing = next(
                (
                    p
                    for p in room.players
                    if (
                        (account_id is not None and p.account_id == account_id and not p.connected)
                        or (account_id is None and p.is_guest and p.name == name and not p.connected)
                    )
                ),
                None,
            )
            if existing is not None:
                existing.token = self._new_id("t")
                existing.connected = True
                room._cancel_abort_locked()
                return room, existing

            if room.state != "waiting":
                raise GameError("游戏已开始，不能加入新玩家")
            if len(room.players) >= MAX_PLAYERS:
                raise GameError("房间已满")

            player = Player(self._new_id("p"), name, self._new_id("t"), False, account_id, is_guest)
            room.players.append(player)
        room.publish("player_joined", {"player": player.public_dict()})
        return room, player

    def rejoin_room(self, code: str, token: str) -> tuple[Room, Player]:
        room = self.get_room(code)
        player = room.find_player_by_token(token)
        if player is None:
            raise GameError("登录信息已失效，请重新加入房间", 401)
        with room.lock:
            player.connected = True
            room._cancel_abort_locked()
        room.publish("player_joined", {"player": player.public_dict()})
        return room, player

    def perform_action(
        self,
        code: str,
        token: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        room = self.get_room(code)
        player = room.find_player_by_token(token)
        if player is None:
            raise GameError("登录信息已失效，请重新加入房间", 401)

        if action == "start":
            room.start(player)
        elif action == "play":
            room.play(
                player,
                list(payload.get("card_ids", [])),
                payload.get("declared_rank"),
            )
        elif action == "challenge":
            room.challenge(player, payload.get("target_play_id"))
        elif action == "pass":
            room.pass_turn(player)
        elif action == "return_lobby":
            room.return_to_lobby(player)
        elif action == "add_ai":
            room.add_ai(
                player,
                payload.get("strength", "medium"),
                payload.get("name"),
            )
        elif action == "remove_ai":
            room.remove_ai(player, payload.get("player_id", ""))
        elif action == "ai_strength":
            room.set_ai_strength(
                player,
                payload.get("player_id", ""),
                payload.get("strength", "medium"),
            )
        elif action == "chat":
            room.chat(player, payload.get("text", ""))
        elif action == "leave":
            room.leave(player)
        else:
            raise GameError("未知操作")

        return room.state_for(player)

    def tick_all(self, now: float) -> None:
        with self.lock:
            rooms = list(self.rooms.values())
        for room in rooms:
            try:
                room.tick(now)
            except Exception:
                # A timer action should not bring down the server. In normal
                # operation this only fails when the room has already finished.
                continue

    def list_public_rooms(self) -> list[dict[str, Any]]:
        """Public room snapshot for the join-room browser (waiting + playing)."""
        with self.lock:
            rooms = list(self.rooms.values())
        items: list[dict[str, Any]] = []
        for room in rooms:
            with room.lock:
                if room.state not in ("waiting", "playing"):
                    continue
                if room.abort_deadline is not None:
                    # 全员已离开、等待作废倒计时，不再对外展示。
                    continue
                players = list(room.players)
                if not players:
                    continue
                host = next(
                    (p for p in players if p.id == room.host_player_id),
                    players[0],
                )
                items.append(
                    {
                        "room_code": room.code,
                        "state": room.state,
                        "player_count": len(players),
                        "host_player_name": host.name,
                    }
                )
        items.sort(
            key=lambda item: (
                0 if item["state"] == "waiting" else 1,
                item["player_count"],
            )
        )
        return items

    def cleanup(self, now: float) -> None:
        """Reap empty waiting rooms and rooms whose players have all gone idle.

        Keeps the room browser free of dead entries and recycles room codes.
        """
        with self.lock:
            for code in list(self.rooms):
                room = self.rooms[code]
                with room.lock:
                    if room.state == "waiting" and not room.players:
                        del self.rooms[code]
                        continue
                    if (
                        room.state == "playing"
                        and room.abort_deadline is not None
                        and now >= room.abort_deadline
                        and all(not p.connected for p in room.players)
                    ):
                        # 对局中全员离开且作废倒计时结束：本局作废，房间回收。
                        del self.rooms[code]
                        continue
                    if all(not p.connected for p in room.players) and (
                        now - room.last_activity > ROOM_IDLE_TIMEOUT_SECONDS
                    ):
                        del self.rooms[code]

    @staticmethod
    def _clean_name(name: str) -> str:
        name = str(name or "").strip()
        if not name:
            raise GameError("昵称不能为空")
        if len(name) > 16:
            name = name[:16]
        return name

    @staticmethod
    def _normalize_code(code: str) -> str:
        return str(code or "").strip().upper()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(12)}"

    def _new_room_code_locked(self) -> str:
        for _ in range(100):
            code = "".join(random.choices(ROOM_CODE_ALPHABET, k=4))
            if code not in self.rooms:
                return code
        raise GameError("暂时无法创建房间，请重试")


class AppContext:
    def __init__(self) -> None:
        self.account_store = AccountStore()
        self.rooms = RoomManager(self.account_store)
        self._stop_event = threading.Event()
        self._janitor_thread: threading.Thread | None = None

    def start_janitor(self) -> None:
        if self._janitor_thread and self._janitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._janitor_thread = threading.Thread(
            target=self._janitor_loop,
            name="bluff-janitor",
            daemon=True,
        )
        self._janitor_thread.start()

    def stop_janitor(self) -> None:
        self._stop_event.set()

    def _janitor_loop(self) -> None:
        last_cleanup = 0.0
        while not self._stop_event.is_set():
            now = time.time()
            self.rooms.tick_all(now)
            if now - last_cleanup >= CLEANUP_INTERVAL_SECONDS:
                try:
                    self.rooms.cleanup(now)
                except Exception:
                    # Cleanup must never take down the janitor thread.
                    pass
                last_cleanup = now
            self._stop_event.wait(0.5)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BluffCards/1.0"

    @property
    def app(self) -> AppContext:
        server: Any = self.server
        return server.app

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if self._route_api("GET", parsed):
                return
        except GameError as exc:
            self._send_error_json(exc.status, exc.message)
            return
        if parsed.path in ("/", ""):
            self._send_static("index.html")
            return
        self._send_static(parsed.path.lstrip("/"))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if self._route_api("POST", parsed):
                return
        except GameError as exc:
            self._send_error_json(exc.status, exc.message)
            return
        self._send_error_json(404, "Not Found")

    def _route_api(self, method: str, parsed: urllib.parse.ParseResult) -> bool:
        parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
        if not parts or parts[0] != "api":
            return False

        if method == "GET" and len(parts) == 2 and parts[1] == "health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "lan_ip": get_lan_ip(),
                    "port": self.server.server_port,
                    "rooms": len(self.app.rooms.rooms),
                },
            )
            return True

        if len(parts) == 3 and parts[1] == "auth":
            sub = parts[2]
            if method == "POST" and sub in ("register", "login"):
                body = self._read_json()
                username = body.get("username", "")
                password = body.get("password", "")
                if sub == "register":
                    account = self.app.account_store.register(username, password)
                else:
                    account = self.app.account_store.login(username, password)
                auth_token = self.app.account_store.create_session(account["id"])
                self._send_json(200, {"ok": True, "auth_token": auth_token, "account": account})
                return True
            if method == "POST" and sub == "logout":
                body = self._read_json()
                self.app.account_store.logout(body.get("auth_token", ""))
                self._send_json(200, {"ok": True})
                return True
            if method == "GET" and sub == "me":
                query = urllib.parse.parse_qs(parsed.query)
                token = (query.get("auth_token") or [""])[0]
                account_id = self.app.account_store.account_id_for_token(token)
                account = self.app.account_store.public_account(account_id)
                self._send_json(200, {"ok": True, "account": account})
                return True

        if method == "GET" and len(parts) == 2 and parts[1] == "leaderboard":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or [str(LEADERBOARD_DEFAULT_LIMIT)])[0])
            except ValueError:
                limit = LEADERBOARD_DEFAULT_LIMIT
            players = self.app.account_store.leaderboard(limit)
            self._send_json(200, {"ok": True, "players": players})
            return True

        if method == "GET" and len(parts) == 2 and parts[1] == "rooms":
            self._send_json(
                200,
                {
                    "ok": True,
                    "rooms": self.app.rooms.list_public_rooms(),
                    "max_players": MAX_PLAYERS,
                },
            )
            return True

        if method == "GET" and len(parts) == 3 and parts[1] == "players":
            account_id = parts[2]
            account = self.app.account_store.public_account(account_id)
            recent_matches = self.app.account_store.recent_matches(account_id)
            self._send_json(
                200,
                {"ok": True, "account": account, "recent_matches": recent_matches},
            )
            return True

        if method == "POST" and len(parts) == 2 and parts[1] == "rooms":
            body = self._read_json()
            token = body.get("auth_token", "")
            account_id = self.app.account_store.account_id_for_token(token)
            if account_id is not None:
                account = self.app.account_store.public_account(account_id)
                name = account["username"]
            else:
                name = body.get("name", "")
            room, player = self.app.rooms.create_room(name, account_id)
            self._send_identity(room, player, 201)
            return True

        if len(parts) == 4 and parts[1] == "rooms":
            code = parts[2]
            sub = parts[3]
            if method == "POST" and sub == "join":
                body = self._read_json()
                token = body.get("auth_token", "")
                account_id = self.app.account_store.account_id_for_token(token)
                if account_id is not None:
                    account = self.app.account_store.public_account(account_id)
                    name = account["username"]
                else:
                    name = body.get("name", "")
                room, player = self.app.rooms.join_room(code, name, account_id)
                self._send_identity(room, player, 200)
                return True
            if method == "POST" and sub == "rejoin":
                body = self._read_json()
                room, player = self.app.rooms.rejoin_room(
                    code, body.get("token", "")
                )
                self._send_identity(room, player, 200)
                return True
            if method == "POST" and sub == "action":
                body = self._read_json()
                snapshot = self.app.rooms.perform_action(
                    code,
                    body.get("token", ""),
                    body.get("action", ""),
                    body.get("payload", {}),
                )
                self._send_json(200, {"ok": True, "snapshot": snapshot})
                return True
            if method == "GET" and sub == "stream":
                query = urllib.parse.parse_qs(parsed.query)
                token = (query.get("token") or [""])[0]
                self._handle_stream(code, token)
                return True

        return False

    def _send_identity(self, room: Room, player: Player, status: int) -> None:
        self._send_json(
            status,
            {
                "room_code": room.code,
                "player_token": player.token,
                "player_id": player.id,
                "snapshot": room.state_for(player),
            },
        )

    def _handle_stream(self, code: str, token: str) -> None:
        try:
            room = self.app.rooms.get_room(code)
            player = room.find_player_by_token(token)
            if player is None:
                raise GameError("登录信息已失效，请重新加入房间", 401)
        except GameError as exc:
            self._send_error_json(exc.status, exc.message)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        room.reset_queue(player)
        with room.lock:
            player.connected = True
        try:
            event = {
                "type": "snapshot",
                "seq": room.event_seq,
                "room_code": room.code,
                "data": {},
                "snapshot": room.state_for(player),
                "sent_at": time.time(),
            }
            self._write_sse(event)
            while True:
                try:
                    event = player.queue.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._write_sse(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with room.lock:
                player.connected = False
                room._schedule_abort_if_empty_locked()

    def _write_sse(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GameError("请求格式错误")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    def _send_static(self, relative_path: str) -> None:
        if relative_path in ("", "/"):
            relative_path = "index.html"
        base = BASE_DIR.resolve()
        candidate = (base / relative_path).resolve()
        if candidate != base and base not in candidate.parents:
            self._send_error_json(404, "Not Found")
            return
        relative = candidate.relative_to(base)
        if relative.parts and relative.parts[0] in ("data", "tests", "__pycache__"):
            self._send_error_json(404, "Not Found")
            return
        if candidate.suffix in (".db", ".py", ".log"):
            self._send_error_json(404, "Not Found")
            return
        if not candidate.is_file():
            self._send_error_json(404, "Not Found")
            return

        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {format % args}")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: AppContext) -> None:
        super().__init__(address, Handler)
        self.app = app


def main() -> None:
    parser = argparse.ArgumentParser(description="吹牛卡牌游戏服务器")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    app = AppContext()
    server = AppServer((args.host, args.port), app)
    app.start_janitor()

    lan_ip = get_lan_ip()
    print("吹牛卡牌游戏已启动")
    print(f"本机访问:   http://localhost:{args.port}")
    print(f"局域网访问: http://{lan_ip}:{args.port}")
    print("按 Ctrl+C 停止服务")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        app.stop_janitor()
        server.server_close()


if __name__ == "__main__":
    main()


