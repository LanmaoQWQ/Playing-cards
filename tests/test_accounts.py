import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from server import AccountStore, GameError, RoomManager


def fake_report(account_id=None, name="Alice", guest=False, game_id="m_test1"):
    return {
        "game_id": game_id,
        "room_code": "AAAA",
        "started_at": "2026-08-31T00:00:00.000Z",
        "finished_at": "2026-08-31T00:05:00.000Z",
        "winner_account_id": None if guest else account_id,
        "winner_name": name,
        "ended_reason": "completed",
        "players": [
            {
                "player_id": "p_1",
                "account_id": None if guest else account_id,
                "name": name,
                "is_guest": guest,
                "is_winner": True,
                "stats": {
                    "bluff_attempts": 2,
                    "bluff_successes": 1,
                    "challenge_attempts": 3,
                    "challenge_successes": 2,
                },
            }
        ],
        "rounds": [],
    }


def prepare_room(room, hands):
    room.state = "playing"
    room.winner_id = None
    room.started_at = server.utc_now()
    room.finished_at = None
    room.round_reports = []
    room.game_report = None
    room.recorded = False
    room.empty_order = []
    room.round_seq = 0
    room.next_play_id = 1
    room.declared_rank = None
    room.current_round_plays = []
    room.round_has_returned = False
    room.round_passers = set()
    room.last_challenge_result = None
    room.last_round_ended_reason = None
    for player, hand in zip(room.players, hands):
        player.hand = list(hand)
    leader = room.players[0]
    room.current_round_leader_id = leader.id
    room.current_player_id = leader.id
    room.current_player_index = room.player_index(leader.id)
    room.turn_deadline = time.time() + 60


class AccountTests(unittest.TestCase):
    def test_register_login_logout_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "test.db")
            account = store.register("Alice", "secret123")
            self.assertEqual(account["username"], "Alice")
            self.assertEqual(account["stats"]["games_played"], 0)

            with self.assertRaises(GameError):
                store.register("alice", "another456")

            with self.assertRaises(GameError):
                store.login("Alice", "wrong-password")

            logged = store.login("ALICE", "secret123")
            self.assertEqual(logged["id"], account["id"])

            token = store.create_session(account["id"])
            self.assertEqual(store.account_id_for_token(token), account["id"])
            store.logout(token)
            with self.assertRaises(GameError):
                store.account_id_for_token(token)

            with closing(sqlite3.connect(Path(tmp) / "test.db")) as conn:
                row = conn.execute(
                    "SELECT password_hash FROM accounts WHERE username_norm = 'alice'"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn("secret123", row[0])

    def test_leaderboard_excludes_guests_and_orders_by_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "test.db")
            guest = fake_report(guest=True, name="游客", game_id="m_guest")
            store.record_match(guest)
            self.assertEqual(store.leaderboard(), [])

            alice = store.register("Alice", "secret123")
            bob = store.register("Bob", "secret456")
            store.record_match(
                fake_report(account_id=alice["id"], name="Alice", game_id="m_alice")
            )
            store.record_match(
                fake_report(account_id=bob["id"], name="Bob", game_id="m_bob")
            )
            store.record_match(
                fake_report(account_id=bob["id"], name="Bob", game_id="m_bob2")
            )

            board = store.leaderboard()
            self.assertEqual([row["username"] for row in board], ["Bob", "Alice"])
            self.assertEqual(board[0]["stats"]["games_played"], 2)
            self.assertEqual(board[0]["stats"]["wins"], 2)

    def test_recent_matches_returns_only_participating_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "test.db")
            alice = store.register("Alice", "secret123")
            bob = store.register("Bob", "secret456")
            store.record_match(fake_report(account_id=alice["id"], name="Alice", game_id="m_a"))
            store.record_match(fake_report(account_id=bob["id"], name="Bob", game_id="m_b"))

            recent = store.recent_matches(alice["id"])
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["match_id"], "m_a")

    def test_no_challenge_round_marks_bluff_success(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        _, guest = manager.join_room(room.code, "B")
        prepare_room(room, [["♠5#1", "♥6#1"], ["♣7#1", "♦8#1"]])

        room.play(host, [host.hand[0]], "3")
        follower = room.current_player()
        room.pass_turn(follower)
        room.pass_turn(room.current_player())

        report = room.round_reports[-1]
        self.assertEqual(report["reason"], "completed")
        self.assertTrue(report["plays"][0]["is_bluff"])
        self.assertTrue(report["plays"][0]["bluff_succeeded"])

    def test_successful_challenge_marks_bluff_failed(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        _, guest = manager.join_room(room.code, "B")
        prepare_room(room, [["♠5#1", "♥6#1"], ["♣7#1", "♦8#1"]])

        room.play(host, [host.hand[0]], "3")
        follower = room.current_player()
        target_id = room.current_round_plays[0]["play_id"]
        room.challenge(follower, target_id)

        report = room.round_reports[-1]
        self.assertEqual(report["reason"], "challenge")
        self.assertTrue(report["challenge"]["success"])
        self.assertTrue(report["plays"][0]["is_bluff"])
        self.assertFalse(report["plays"][0]["bluff_succeeded"])

    def test_failed_challenge_does_not_count_bluff(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        _, guest = manager.join_room(room.code, "B")
        prepare_room(room, [["♠5#1", "♥6#1"], ["♣7#1", "♦8#1"]])

        room.play(host, [host.hand[0]], "5")
        follower = room.current_player()
        target_id = room.current_round_plays[0]["play_id"]
        room.challenge(follower, target_id)

        report = room.round_reports[-1]
        self.assertFalse(report["challenge"]["success"])
        self.assertFalse(report["plays"][0]["is_bluff"])


if __name__ == "__main__":
    unittest.main()
