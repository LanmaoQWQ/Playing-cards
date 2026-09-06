import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from server import GameError, Room, RoomManager, is_spade_three, rank_from_card_id


def make_room(*names):
    manager = RoomManager()
    room, host = manager.create_room(names[0])
    players = [host]
    for name in names[1:]:
        _, player = manager.join_room(room.code, name)
        players.append(player)
    return manager, room, players


class BluffGameTests(unittest.TestCase):
    def prepare_room(self, room, hands):
        room.state = "playing"
        room.winner_id = None
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

    def test_two_deck_deal_108_with_jokers_and_spade_three_leader(self):
        _, room, players = make_room("A", "B", "C", "D")
        room.start(players[0])

        hands = [list(p.hand) for p in players]
        total = sum(len(h) for h in hands)
        self.assertEqual(total, 108)
        jokers = [c for h in hands for c in h if server.is_joker(c)]
        self.assertEqual(len(jokers), 4)
        self.assertEqual(sorted(jokers), ["★大王#1", "★大王#2", "☆小王#1", "☆小王#2"])
        leader = room.current_player()
        self.assertIsNotNone(leader)
        self.assertTrue(any(is_spade_three(card) for card in leader.hand))
        self.assertEqual(room.current_round_leader_id, leader.id)
        self.assertIsNone(room.declared_rank)

    def test_three_player_deal_108(self):
        _, room, players = make_room("A", "B", "C")
        room.start(players[0])

        counts = sorted(len(p.hand) for p in players)
        self.assertEqual(counts, [36, 36, 36])
        self.assertEqual(sum(counts), 108)

    def test_four_and_six_player_distribution(self):
        _, room4, players4 = make_room("A", "B", "C", "D")
        room4.start(players4[0])
        self.assertEqual([len(p.hand) for p in players4], [27, 27, 27, 27])

        _, room6, players6 = make_room("A", "B", "C", "D", "E", "F")
        room6.start(players6[0])
        self.assertEqual(sorted(len(p.hand) for p in players6), [18, 18, 18, 18, 18, 18])

    def test_first_play_sets_rank_and_advances(self):
        _, room, players = make_room("A", "B")
        room.start(players[0])
        leader = room.current_player()
        old_count = len(leader.hand)
        card = leader.hand[0]

        room.play(leader, [card], "5")

        self.assertEqual(room.declared_rank, "5")
        self.assertEqual(len(room.current_round_plays), 1)
        self.assertEqual(len(leader.hand), old_count - 1)
        self.assertNotEqual(room.current_player_id, leader.id)

    def test_follower_must_use_same_declared_rank(self):
        _, room, players = make_room("A", "B")
        room.start(players[0])
        leader = room.current_player()
        room.play(leader, [leader.hand[0]], "7")
        follower = room.current_player()

        with self.assertRaises(GameError):
            room.play(follower, [follower.hand[0]], "8")

    def test_max_eight_cards_and_nine_rejected(self):
        _, room, players = make_room("A", "B")
        hand = [f"?{i}" for i in range(1, 10)]
        self.prepare_room(room, [hand, ["?A"]])
        leader = players[0]

        room.play(leader, hand[:8], "3")
        self.assertEqual(len(leader.hand), 1)

        _, room2, players2 = make_room("A", "B")
        hand2 = [f"?{i}" for i in range(1, 10)]
        self.prepare_room(room2, [hand2, ["?A"]])
        with self.assertRaises(GameError):
            room2.play(players2[0], hand2, "3")

    def test_duplicate_physical_card_rejected(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?3#1", "?4"], ["?5"]])
        with self.assertRaises(GameError):
            room.play(players[0], ["?3#1", "?3#1"], "3")

    def test_one_player_cannot_start(self):
        manager = RoomManager()
        room, player = manager.create_room("A")
        with self.assertRaises(GameError):
            room.start(player)

    def test_six_player_room_is_full(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        for name in ["B", "C", "D", "E", "F"]:
            manager.join_room(room.code, name)
        self.assertEqual(len(room.players), 6)
        with self.assertRaises(GameError):
            manager.join_room(room.code, "G")

    def test_challenge_true_makes_challenger_take_pile(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        leader = players[0]
        room.play(leader, ["?3#1"], "3")

        challenger = room.current_player()
        challenger_count = len(challenger.hand)
        play_id = room.current_round_plays[0]["play_id"]
        room.challenge(challenger, play_id)

        self.assertTrue(room.last_challenge_result["truth"])
        self.assertEqual(room.last_challenge_result["winner_id"], leader.id)
        self.assertEqual(room.last_challenge_result["loser_id"], challenger.id)
        self.assertEqual(len(challenger.hand), challenger_count + 1)
        self.assertEqual(room.current_player_id, leader.id)

    def test_challenge_false_makes_target_take_pile(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1"], ["?4"]])
        leader = players[0]
        room.play(leader, ["?3#1"], "A")

        challenger = room.current_player()
        leader_count = len(leader.hand)
        play_id = room.current_round_plays[0]["play_id"]
        room.challenge(challenger, play_id)

        self.assertFalse(room.last_challenge_result["truth"])
        self.assertEqual(room.last_challenge_result["winner_id"], challenger.id)
        self.assertEqual(room.last_challenge_result["loser_id"], leader.id)
        self.assertEqual(len(leader.hand), leader_count + 1)
        self.assertEqual(room.current_player_id, challenger.id)

    def test_challenge_settles_entire_round_pile(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        leader = players[0]
        other = players[1]

        room.play(leader, ["?3#1"], "3")
        room.play(other, ["?5"], "3")
        target_id = room.current_round_plays[-1]["play_id"]
        room.challenge(leader, target_id)

        self.assertEqual(room.state, "playing")
        self.assertEqual(len(leader.hand), 1)
        self.assertEqual(len(other.hand), 2)
        self.assertEqual(room.last_challenge_result["pile_count"], 2)
        self.assertEqual(len(room.last_challenge_result["pile_cards"]), 2)
        self.assertEqual(room.current_player_id, leader.id)

    def test_passer_is_skipped_until_round_ends(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4", "?7"], ["?5"]])
        leader = players[0]
        follower = players[1]

        room.play(leader, ["?3#1"], "3")
        room.pass_turn(follower)  # B 过牌：本轮出局
        self.assertIn(follower.id, room.round_passers)
        self.assertEqual(room.current_player_id, leader.id)  # 跳过 B，回到 A

        room.play(leader, ["?4"], "3")  # A 仍可继续出
        self.assertEqual(room.current_player_id, leader.id)  # B 已被跳过

        room.pass_turn(leader)  # A 也过 → 全员过牌
        self.assertEqual(room.state, "playing")
        self.assertEqual(room.current_round_plays, [])
        self.assertEqual(room.current_round_leader_id, leader.id)
        self.assertEqual(room.round_passers, set())
        self.assertIsNone(room.declared_rank)

        # 新一轮开始：首家必须先出牌报点，不能过牌
        with self.assertRaises(GameError):
            room.pass_turn(leader)

    def test_full_pass_cycle_ends_round_and_keeps_leader(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        leader = players[0]
        follower = players[1]

        room.play(leader, ["?3#1"], "3")
        room.pass_turn(follower)
        self.assertEqual(room.current_player_id, leader.id)
        self.assertEqual(len(room.current_round_plays), 1)

        room.pass_turn(leader)

        self.assertEqual(room.current_player_id, leader.id)
        self.assertEqual(room.current_round_leader_id, leader.id)
        self.assertEqual(room.current_round_plays, [])
        self.assertEqual(room.round_passers, set())
        self.assertIsNone(room.declared_rank)

    def test_win_after_playing_last_cards_and_round_completes(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1"], ["?4"]])
        leader = players[0]
        follower = players[1]

        room.play(leader, ["?3#1"], "3")
        room.play(follower, ["?4"], "3")
        self.assertEqual(room.state, "playing")
        self.assertEqual(room.current_player_id, leader.id)

        room.pass_turn(leader)
        room.pass_turn(follower)

        self.assertEqual(room.state, "finished")
        self.assertEqual(room.winner_id, leader.id)

    def test_leader_can_challenge_follower_when_round_returns(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        leader = players[0]
        follower = players[1]

        room.play(leader, ["?3#1", "?4"], "3")
        self.assertEqual(len(leader.hand), 0)
        room.play(follower, ["?5"], "3")
        self.assertEqual(room.current_player_id, leader.id)

        target_id = next(
            play["play_id"]
            for play in room.current_round_plays
            if play["player_id"] == follower.id
        )
        room.challenge(leader, target_id)

        self.assertFalse(room.last_challenge_result["truth"])
        self.assertEqual(room.state, "finished")
        self.assertEqual(room.winner_id, leader.id)

    def test_last_card_false_challenge_keeps_game_going(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1"], ["?4"]])
        leader = players[0]
        room.play(leader, ["?3#1"], "A")

        challenger = room.current_player()
        play_id = room.current_round_plays[0]["play_id"]
        room.challenge(challenger, play_id)

        self.assertEqual(room.state, "playing")
        self.assertIn("?3#1", leader.hand)
        self.assertEqual(room.current_player_id, challenger.id)

    def test_wrong_turn_is_rejected(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1"], ["?4"]])
        other = players[1]
        with self.assertRaises(GameError):
            room.play(other, [other.hand[0]], "5")

    def test_timer_auto_challenges_last_play(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        leader = players[0]
        room.play(leader, ["?3#1"], "3")

        follower = room.current_player()
        follower_count = len(follower.hand)
        room.turn_deadline = time.time() - 1
        room.tick(time.time())

        self.assertEqual(room.state, "playing")
        self.assertIsNotNone(room.last_challenge_result)
        self.assertEqual(len(follower.hand), follower_count + 1)

    def test_chat_cleans_limits_and_keeps_history(self):
        _, room, players = make_room("A", "B")
        room.chat(players[0], "\u4f60\u597d\U0001F600\n\u6362\u884c")
        self.assertEqual(room.chat_messages[-1]["text"], "\u4f60\u597d\U0001F600\u6362\u884c")

        room.chat(players[1], "?" * 250)
        self.assertEqual(len(room.chat_messages[-1]["text"]), 200)

        snapshot = room.state_for(players[0])
        self.assertEqual(len(snapshot["chat_messages"]), 2)
        self.assertEqual(snapshot["chat_messages"][0]["player_name"], "A")


class RoomBrowserTests(unittest.TestCase):
    """Tests for the public room listing used by the join-room browser."""

    def test_list_reports_counts_hosts_and_sorts_fewest_first(self):
        manager = RoomManager()
        room_a, _alice = manager.create_room("Alice")
        manager.join_room(room_a.code, "Bob")
        room_b, _carol = manager.create_room("Carol")

        items = {item["room_code"]: item for item in manager.list_public_rooms()}
        self.assertEqual(items[room_a.code]["player_count"], 2)
        self.assertEqual(items[room_a.code]["host_player_name"], "Alice")
        self.assertEqual(items[room_a.code]["state"], "waiting")
        self.assertEqual(items[room_b.code]["player_count"], 1)
        self.assertEqual(items[room_b.code]["host_player_name"], "Carol")

        codes = [item["room_code"] for item in manager.list_public_rooms()]
        self.assertEqual(codes, [room_b.code, room_a.code])

    def test_list_includes_playing_rooms(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        manager.join_room(room.code, "B")
        room.start(host)

        entry = next(
            item for item in manager.list_public_rooms() if item["room_code"] == room.code
        )
        self.assertEqual(entry["state"], "playing")
        self.assertEqual(entry["player_count"], 2)

    def test_list_sorts_waiting_before_playing(self):
        manager = RoomManager()
        playing_room, phost = manager.create_room("P")
        manager.join_room(playing_room.code, "P2")
        playing_room.start(phost)
        waiting_room, _ = manager.create_room("W")

        codes = [item["room_code"] for item in manager.list_public_rooms()]
        self.assertEqual(codes, [waiting_room.code, playing_room.code])

    def test_empty_waiting_room_is_not_listed(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        room.leave(host)
        codes = [item["room_code"] for item in manager.list_public_rooms()]
        self.assertNotIn(room.code, codes)

    def test_cleanup_removes_empty_waiting_room(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        room.leave(host)
        self.assertEqual(room.players, [])

        manager.cleanup(time.time())
        with self.assertRaises(GameError):
            manager.get_room(room.code)

    def test_cleanup_removes_idle_rooms_and_keeps_recent_ones(self):
        manager = RoomManager()
        now = time.time()
        stale_waiting, stale_host = manager.create_room("A")
        stale_host.connected = False
        stale_waiting.last_activity = now - server.ROOM_IDLE_TIMEOUT_SECONDS - 1

        stale_playing, p_host = manager.create_room("B")
        manager.join_room(stale_playing.code, "B2")
        p_host.connected = False
        stale_playing.players[1].connected = False
        stale_playing.last_activity = now - server.ROOM_IDLE_TIMEOUT_SECONDS - 5

        fresh_room, fresh_host = manager.create_room("C")
        fresh_host.connected = False
        fresh_room.last_activity = now - 10

        manager.cleanup(now)

        with self.assertRaises(GameError):
            manager.get_room(stale_waiting.code)
        with self.assertRaises(GameError):
            manager.get_room(stale_playing.code)
        self.assertIsNotNone(manager.get_room(fresh_room.code))


class MidGameAbortTests(unittest.TestCase):
    """When every player leaves mid-game the room aborts instead of bot-playing."""

    def _started_two_player_room(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        _, other = manager.join_room(room.code, "B")
        room.start(host)
        return manager, room, host, other

    def test_all_leave_schedules_abort_and_freezes_auto_play(self):
        _manager, room, host, other = self._started_two_player_room()
        self.assertIsNone(room.abort_deadline)

        room.leave(host)
        self.assertIsNone(room.abort_deadline)  # 仍有 1 人在线

        room.leave(other)
        self.assertIsNotNone(room.abort_deadline)  # 全员离开 -> 启动作废倒计时

        plays_before = len(room.current_round_plays)
        room.turn_deadline = time.time() - 1
        room.tick(time.time())
        self.assertEqual(len(room.current_round_plays), plays_before)
        self.assertEqual(room.state, "playing")

    def test_rejoin_cancels_abort(self):
        _manager, room, host, other = self._started_two_player_room()
        room.leave(host)
        room.leave(other)
        self.assertIsNotNone(room.abort_deadline)

        _manager.rejoin_room(room.code, host.token)
        self.assertIsNone(room.abort_deadline)
        self.assertTrue(host.connected)

    def test_cleanup_aborts_room_after_grace(self):
        manager, room, host, other = self._started_two_player_room()
        room.leave(host)
        room.leave(other)
        now = time.time()

        manager.cleanup(now)  # 倒计时未结束：保留
        self.assertIsNotNone(manager.get_room(room.code))

        room.abort_deadline = now - 1
        manager.cleanup(now)  # 倒计时结束且仍全员离线：回收
        with self.assertRaises(GameError):
            manager.get_room(room.code)

    def test_abort_pending_room_hidden_from_public_list(self):
        manager, room, host, other = self._started_two_player_room()
        codes = [item["room_code"] for item in manager.list_public_rooms()]
        self.assertIn(room.code, codes)

        room.leave(host)
        room.leave(other)
        codes = [item["room_code"] for item in manager.list_public_rooms()]
        self.assertNotIn(room.code, codes)


class ReturnToLobbyTests(unittest.TestCase):
    """A finished game returns to the same room's lobby instead of restarting."""

    def prepare_room(self, room, hands):
        room.state = "playing"
        room.winner_id = None
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

    def test_finished_game_returns_to_lobby_and_can_start_again(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1"], ["?4"]])
        leader, follower = players
        room.play(leader, ["?3#1"], "3")
        room.play(follower, ["?4"], "3")
        room.pass_turn(leader)
        room.pass_turn(follower)
        self.assertEqual(room.state, "finished")
        self.assertEqual(room.winner_id, leader.id)

        room.return_to_lobby(leader)

        self.assertEqual(room.state, "waiting")
        self.assertIsNone(room.winner_id)
        self.assertIsNone(room.game_report)
        self.assertIsNone(room.turn_deadline)
        self.assertEqual(len(room.players), 2)
        self.assertEqual(players[0].hand, [])
        self.assertEqual(players[1].hand, [])

        # 房间不解散：房主可在等待房重新开局
        room.start(leader)
        self.assertEqual(room.state, "playing")
        self.assertEqual(len(room.players), 2)

    def test_return_to_lobby_rejected_while_playing(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        with self.assertRaises(GameError):
            room.return_to_lobby(players[0])

    def test_return_to_lobby_idempotent_in_waiting(self):
        manager = RoomManager()
        room, host = manager.create_room("A")
        room.return_to_lobby(host)
        self.assertEqual(room.state, "waiting")


class AdjacentChallengeAndPassOutTests(unittest.TestCase):
    """New bluff rules: only the latest play is challengeable; passing forfeits the round."""

    def prepare_room(self, room, hands):
        room.state = "playing"
        room.winner_id = None
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

    def test_third_player_acts_after_second_passes(self):
        _, room, players = make_room("A", "B", "C")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"], ["?6", "?7"]])
        a, b, c = players

        room.play(a, ["?3#1"], "3")
        room.pass_turn(b)  # B 过牌：本轮出局
        self.assertIn(b.id, room.round_passers)
        self.assertEqual(room.current_player_id, c.id)  # 轮到 C

        # B 已出局：不会被轮到（turn-based 拒绝），也收不到新回合
        room.play(c, ["?6"], "3")
        self.assertEqual(room.current_player_id, a.id)  # 跳过 B 回到 A
        self.assertNotEqual(room.current_player_id, b.id)

    def test_cannot_challenge_older_play(self):
        _, room, players = make_room("A", "B", "C")
        self.prepare_room(room, [["?3#1", "?4"], ["?8"], ["?9"]])
        a, b, c = players
        room.play(a, ["?3#1"], "3")
        room.play(b, ["?8"], "3")
        room.play(c, ["?9"], "3")  # 轮到 A

        b_play = room.current_round_plays[1]["play_id"]
        with self.assertRaises(GameError):
            room.challenge(a, b_play)  # 更早的一手不可质疑

        c_play = room.current_round_plays[2]["play_id"]
        room.challenge(a, c_play)  # 最近一手可以质疑
        self.assertIsNotNone(room.last_challenge_result)
        self.assertEqual(room.last_challenge_result["target_player_id"], c.id)

    def test_cannot_challenge_or_pass_before_leader_plays(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        a, _b = players

        with self.assertRaises(GameError):
            room.challenge(a, 1)  # 桌面无牌
        with self.assertRaises(GameError):
            room.pass_turn(a)  # 首家未报点不能过牌

    def test_cannot_challenge_own_latest_play(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        a, b = players
        room.play(a, ["?3#1"], "3")
        room.pass_turn(b)  # B 出局 → A 的回合但最近一手是自己的
        play_id = room.current_round_plays[-1]["play_id"]
        with self.assertRaises(GameError):
            room.challenge(a, play_id)

    def test_challenge_settles_round_and_winner_leads(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?3#1", "?4"], ["?5"]])
        a, b = players
        room.play(a, ["?3#1"], "3")  # 真话
        play_id = room.current_round_plays[-1]["play_id"]
        room.challenge(b, play_id)  # 质疑失败（对方说真话）→ 质疑者收牌、出牌人继续
        self.assertTrue(room.last_challenge_result["truth"])
        self.assertEqual(room.last_challenge_result["loser_id"], b.id)
        self.assertEqual(room.current_player_id, a.id)
        self.assertEqual(room.round_passers, set())


class JokerWildcardTests(unittest.TestCase):
    """Big/small jokers are wildcards matching any declared rank."""

    def prepare_room(self, room, hands):
        room.state = "playing"
        room.winner_id = None
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

    def test_four_twos_plus_small_joker_counts_as_five_twos(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(
            room,
            [["?2#1", "?2#2", "?2#3", "?2#4", "☆小王#1"], ["?3#1"]],
        )
        a, b = players
        room.play(a, ["?2#1", "?2#2", "?2#3", "?2#4", "☆小王#1"], "2")
        play_id = room.current_round_plays[-1]["play_id"]
        room.challenge(b, play_id)
        self.assertTrue(room.last_challenge_result["truth"])

    def test_big_joker_also_counts_as_declared_rank(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?2#1", "?2#2", "★大王#1"], ["?3#1"]])
        a, b = players
        room.play(a, ["?2#1", "?2#2", "★大王#1"], "2")
        play_id = room.current_round_plays[-1]["play_id"]
        room.challenge(b, play_id)
        self.assertTrue(room.last_challenge_result["truth"])

    def test_real_mismatch_with_joker_is_still_a_bluff(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["?2#1", "?3#1", "☆小王#1"], ["?4#1"]])
        a, b = players
        room.play(a, ["?2#1", "?3#1", "☆小王#1"], "2")
        play_id = room.current_round_plays[-1]["play_id"]
        room.challenge(b, play_id)
        self.assertFalse(room.last_challenge_result["truth"])
        self.assertEqual(room.last_challenge_result["target_player_id"], a.id)

    def test_joker_only_play_can_open_round(self):
        _, room, players = make_room("A", "B")
        self.prepare_room(room, [["★大王#1"], ["?4#1"]])
        a, b = players
        room.play(a, ["★大王#1"], "5")
        play_id = room.current_round_plays[-1]["play_id"]
        room.challenge(b, play_id)
        self.assertTrue(room.last_challenge_result["truth"])

    def test_jokers_sort_after_ace(self):
        hand = ["♠A#1", "☆小王#1", "★大王#1"]
        self.assertEqual(server.sort_hand(hand), ["♠A#1", "☆小王#1", "★大王#1"])


class AiOpponentRoomTests(unittest.TestCase):
    """Host-managed AI seats in the waiting lobby."""

    def _room_with_ai(self, strength="hard"):
        manager = RoomManager()
        room, host = manager.create_room("Human")
        ai = room.add_ai(host, strength)
        return manager, room, host, ai

    def test_host_adds_ai_and_start_with_two_players(self):
        _manager, room, host, ai = self._room_with_ai("medium")
        self.assertTrue(ai.is_ai)
        self.assertEqual(ai.ai_strength, "medium")
        self.assertTrue(ai.is_guest)
        self.assertFalse(ai.connected)
        self.assertEqual(len(room.players), 2)
        self.assertTrue(any(p["is_ai"] for p in room.state_for(host)["players"]))

        room.start(host)  # 真人 + AI 合计 2 即可开局
        self.assertEqual(room.state, "playing")
        self.assertGreater(len(ai.hand), 0)

    def test_only_host_can_manage_ai(self):
        manager, room, host, ai = self._room_with_ai()
        _, other = manager.join_room(room.code, "Human2")
        with self.assertRaises(GameError):
            room.add_ai(other, "easy")
        with self.assertRaises(GameError):
            room.remove_ai(other, ai.id)
        with self.assertRaises(GameError):
            room.set_ai_strength(other, ai.id, "hard")

        room.remove_ai(host, ai.id)
        self.assertEqual(len(room.players), 2)
        self.assertFalse(any(p.is_ai for p in room.players))

    def test_ai_cannot_be_managed_while_playing(self):
        manager, room, host, ai = self._room_with_ai()
        room.start(host)
        with self.assertRaises(GameError):
            room.add_ai(host, "easy")
        with self.assertRaises(GameError):
            room.remove_ai(host, ai.id)
        with self.assertRaises(GameError):
            room.set_ai_strength(host, ai.id, "hard")

    def test_room_is_capped_at_six_including_ai(self):
        manager = RoomManager()
        room, host = manager.create_room("Human")
        for _ in range(5):
            room.add_ai(host, "easy")
        self.assertEqual(len(room.players), 6)
        with self.assertRaises(GameError):
            room.add_ai(host, "easy")

    def test_set_ai_strength_validates(self):
        manager, room, host, ai = self._room_with_ai("easy")
        room.set_ai_strength(host, ai.id, "hard")
        self.assertEqual(ai.ai_strength, "hard")
        with self.assertRaises(GameError):
            room.set_ai_strength(host, ai.id, "insane")

    def test_ai_acts_on_its_turn_when_deadline_passes(self):
        _manager, room, host, ai = self._room_with_ai("medium")
        room.start(host)
        ai_index = room.player_index(ai.id)
        room.current_player_index = ai_index
        room.current_player_id = ai.id
        room.declared_rank = None
        room.current_round_plays = []
        room.round_passers = set()
        room.abort_deadline = None
        room.turn_deadline = time.time() - 1

        room.tick(time.time())

        self.assertEqual(room.state, "playing")
        self.assertIn(room.declared_rank, server.RANKS)
        self.assertEqual(len(room.current_round_plays), 1)
        self.assertEqual(room.current_round_plays[0]["player_id"], ai.id)

    def test_ai_seat_survives_return_to_lobby(self):
        manager, room, host, ai = self._room_with_ai("easy")
        # 快速打完整局：真人(首家)打光手牌且无人质疑 → 直接获胜。
        players = room.players
        players[0].hand = ["?3#1"]
        ai.hand = ["?4#1"]
        room.state = "playing"
        room.empty_order = []
        room.round_seq = 0
        room.next_play_id = 1
        room.declared_rank = None
        room.current_round_plays = []
        room.round_has_returned = False
        room.round_passers = set()
        room.current_round_leader_id = players[0].id
        room.current_player_id = players[0].id
        room.current_player_index = 0
        room.turn_deadline = time.time() + 60

        room.play(players[0], ["?3#1"], "3")
        room.pass_turn(ai)
        room.pass_turn(players[0])  # 全员过牌：打光手牌的首家获胜
        self.assertEqual(room.state, "finished")

        room.return_to_lobby(players[0])
        self.assertEqual(room.state, "waiting")
        self.assertEqual(len(room.players), 2)
        self.assertTrue(any(p.is_ai for p in room.players))


if __name__ == "__main__":
    unittest.main()
