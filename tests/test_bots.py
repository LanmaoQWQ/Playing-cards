import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bots

HAND_POOL = (
    ["♠2#1", "♠2#2", "♥2#1", "♦3#1", "♠3#1", "♠4#1", "♣5#1", "♥5#1", "♠6#1", "♠A#1", "☆小王#1", "★大王#1"]
)


def sample_hand(rng, size):
    return rng.sample(HAND_POOL, k=min(size, len(HAND_POOL)))


def base_ctx(own_id="p_me", hand=None, declared="5"):
    return {
        "hand": list(hand or []),
        "declared_rank": declared,
        "table": [],
        "players": [{"id": "p_me", "hand_count": 10, "is_ai": True}],
        "own_id": own_id,
        "revealed": [],
    }


def ctx_with_last_play(hand, declared, claim_count, author_id="p_them", play_id=7):
    ctx = base_ctx(hand=hand, declared=declared)
    ctx["table"] = [
        {
            "play_id": play_id,
            "player_id": author_id,
            "player_name": "对手",
            "declared_rank": declared,
            "count": claim_count,
        }
    ]
    ctx["players"] = [
        {"id": "p_me", "hand_count": len(hand), "is_ai": True},
        {"id": "p_them", "hand_count": 8, "is_ai": False},
    ]
    return ctx


class BotDecisionTests(unittest.TestCase):
    def test_opening_leader_must_play_and_declare_rank(self):
        rng = random.Random(1)
        for strength in bots.AI_STRENGTHS:
            hand = sample_hand(rng, 8)
            ctx = base_ctx(hand=hand, declared=None)
            action = bots.decide(ctx, strength, rng)
            self.assertEqual(action["kind"], "play", strength)
            rank = action.get("declared_rank")
            self.assertIn(rank, bots.RANKS, strength)
            cards = action["cards"]
            self.assertTrue(1 <= len(cards) <= bots.MAX_CARDS_PER_PLAY)
            self.assertEqual(len(set(cards)), len(cards))
            self.assertTrue(all(c in hand for c in cards))

    def test_impossible_claim_is_challenged_by_all_strengths(self):
        # The AI itself holds 2 twos; claiming 8 twos is physically impossible.
        rng = random.Random(2)
        hand = ["♠2#1", "♠2#2", "♠3#1", "♠4#1", "♠5#1", "♠6#1"]
        for strength in bots.AI_STRENGTHS:
            ctx = ctx_with_last_play(hand, "2", claim_count=8)
            action = bots.decide(ctx, strength, rng)
            self.assertEqual(action["kind"], "challenge", strength)
            self.assertEqual(action["target_play_id"], 7)

    def test_hard_challenges_risky_claim_but_easy_medium_do_not(self):
        # AI holds no fives; an opponent with a bluff history claims two fives.
        hand = ["♠2#1", "♠3#1", "♠4#1", "♠6#1"]
        ctx = ctx_with_last_play(hand, "5", claim_count=2)
        ctx["revealed"] = [
            {"player_id": "p_them", "declared_rank": "5", "actual_cards": ["♠3#1"], "was_bluff": True},
            {"player_id": "p_them", "declared_rank": "5", "actual_cards": ["♠7#1"], "was_bluff": True},
        ]
        self.assertEqual(
            bots.decide(ctx, "hard", random.Random(1))["kind"], "challenge"
        )
        self.assertNotEqual(
            bots.decide(ctx, "medium", random.Random(1))["kind"], "challenge"
        )
        self.assertNotEqual(
            bots.decide(ctx, "easy", random.Random(1))["kind"], "challenge"
        )

    def test_outputs_are_always_legal(self):
        for seed in range(1, 26):
            rng = random.Random(seed)
            for strength in bots.AI_STRENGTHS:
                hand = sample_hand(rng, rng.randint(1, 10))
                if not hand:
                    continue
                declared = rng.choice([None, "2", "3", "5", "K"])
                ctx = base_ctx(hand=hand, declared=declared)
                if declared is not None:
                    # Half the rounds include a previous hand by somebody else.
                    ctx["table"] = [
                        {
                            "play_id": 1,
                            "player_id": "p_them",
                            "player_name": "对手",
                            "declared_rank": declared,
                            "count": rng.randint(1, 4),
                        }
                    ]
                    ctx["players"].append(
                        {"id": "p_them", "hand_count": 7, "is_ai": False}
                    )
                action = bots.decide(ctx, strength, rng)
                kind = action["kind"]
                if kind == "play":
                    cards = action["cards"]
                    self.assertGreaterEqual(len(cards), 1, (seed, strength))
                    self.assertLessEqual(len(cards), bots.MAX_CARDS_PER_PLAY)
                    self.assertEqual(len(set(cards)), len(cards))
                    self.assertTrue(all(c in hand for c in cards))
                    if ctx["declared_rank"] is not None:
                        self.assertEqual(
                            action.get("declared_rank"), ctx["declared_rank"]
                        )
                    else:
                        self.assertIn(action.get("declared_rank"), bots.RANKS)
                elif kind == "challenge":
                    self.assertEqual(action["target_play_id"], 1)
                    table = ctx.get("table") or []
                    self.assertTrue(table)
                    self.assertNotEqual(table[-1]["player_id"], ctx["own_id"])
                else:
                    self.assertEqual(kind, "pass")
                    self.assertIsNotNone(ctx["declared_rank"])


if __name__ == "__main__":
    unittest.main()
