"""AI opponents for the bluff card game.

Pure decision helpers — no sockets, no game state mutation. The server builds a
context object from the current room state and asks ``decide`` what the AI wants
to do. Decisions only rely on the AI's own hand plus public information (what is
on the table, how many cards everyone holds, cards already revealed by past
challenges), so the AI never "cheats" by peeking at other hands.

Card ids follow the same convention as server.py: suit/rank glyph + "#copy",
e.g. "♠A#1", "★大王#1" (big joker), "☆小王#1" (small joker).
"""

from __future__ import annotations

import random
from typing import Any

RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
JOKER_SUITS = ("★", "☆")
TOTAL_COPIES_PER_RANK = 8  # two decks x four suits
MAX_CARDS_PER_PLAY = 8

AI_STRENGTHS = ("easy", "medium", "hard")
AI_STRENGTH_LABELS = {"easy": "简单", "medium": "中等", "hard": "困难"}


def rank_of(card: str) -> str:
    return card[1:].split("#", 1)[0]


def is_joker(card: str) -> bool:
    return card[:1] in JOKER_SUITS


def _is_in_hand(hand: list[str], cards: list[str]) -> bool:
    return len(cards) <= len(set(cards)) and all(card in hand for card in cards)


def _build_hand_stats(hand: list[str]) -> dict[str, Any]:
    """Counts per rank excluding jokers; jokers stored separately."""
    stats: dict[str, Any] = {"counts": {}, "jokers": 0}
    for card in hand:
        if is_joker(card):
            stats["jokers"] += 1
            continue
        rank = rank_of(card)
        stats["counts"][rank] = stats["counts"].get(rank, 0) + 1
    return stats


def _choose_leader_open(hand: list[str], rng: random.Random) -> dict[str, Any]:
    """First hand of a round: announce a rank and play (mostly honestly)."""
    stats = _build_hand_stats(hand)
    # Prefer the most numerous rank we actually hold.
    best_rank: str | None = None
    best_count = 0
    for rank, count in stats["counts"].items():
        if count > best_count:
            best_rank, best_count = rank, count
    if best_rank is None:
        # Hand full of jokers: announce a random rank and play one joker.
        best_rank = rng.choice(RANKS)
        chosen = [rng.choice([c for c in hand if is_joker(c)])]
    else:
        chosen = [c for c in hand if rank_of(c) == best_rank]
        # Open with a few copies only; keep jokers as flexible wildcards.
        chosen = chosen[: min(3, len(chosen))]
        jokers = [c for c in hand if is_joker(c)]
        chosen.extend(jokers[:1])
    return {"kind": "play", "cards": chosen, "declared_rank": best_rank}


def _pass_probability(strength: str) -> float:
    return {"easy": 0.42, "medium": 0.2, "hard": 0.06}[strength]


def _play_amount_scheme(
    strength: str, honest_count: int, jokers: int
) -> int:
    """How many cards to lay when continuing with the current declared rank."""
    if strength == "easy":
        return max(1, min(honest_count, 1))
    if strength == "medium":
        if honest_count >= 3:
            return min(honest_count, 3) + min(jokers, 1)
        if honest_count >= 1:
            return honest_count + min(jokers, 1)
        return 1
    # hard: unload copies quickly; jokers stay as insurance unless finishing.
    if honest_count >= 1:
        return min(honest_count, 8) + (min(jokers, 1) if honest_count < 4 else 0)
    return 1


def _estimate_lie_probability(
    ctx: dict[str, Any],
    strength: str,
    declared_rank: str,
    claim_count: int,
    author_id: str,
    stats: dict[str, Any],
) -> float:
    """Frequency-style estimate (0..1) that the previous hand is a bluff."""
    hand_stats = _build_hand_stats(ctx["hand"])
    own_of_rank = hand_stats["counts"].get(declared_rank, 0)

    # Physically impossible to be honest (the AI itself holds too many copies).
    if claim_count > TOTAL_COPIES_PER_RANK - own_of_rank:
        return 1.0

    score = 0.18  # baseline suspicion for any hand
    # Bigger claims are likelier to be lies.
    score += 0.12 * max(0, claim_count - 1)
    # Every copy of this rank the AI holds shrinks what the author could hold.
    score += 0.08 * min(own_of_rank, 4)

    revealed = ctx.get("revealed", [])
    author_entries = [e for e in revealed if e.get("player_id") == author_id]
    if author_entries:
        bluff_share = sum(1 for e in author_entries if e.get("was_bluff")) / len(author_entries)
        score += 0.25 * bluff_share - 0.08 * (1 - bluff_share)

    # Jokers in the author's revealed hands can back an honest claim.
    author_jokers = sum(
        1
        for e in author_entries
        for c in e.get("actual_cards", [])
        if is_joker(c)
    )
    score -= 0.05 * min(author_jokers, 2)

    # Difficulty scaling: hard stays sharp, easy stays gullible.
    scale = {"easy": 0.5, "medium": 0.8, "hard": 1.0}[strength]
    return min(1.0, max(0.0, score * scale))


def _choose_challenge(
    ctx: dict[str, Any],
    strength: str,
    last: dict[str, Any],
    stats: dict[str, Any],
    rng: random.Random,
) -> bool:
    declared = ctx.get("declared_rank") or last.get("declared_rank")
    claim = int(last.get("count") or 0)
    author = last.get("player_id")
    p_lie = _estimate_lie_probability(ctx, strength, declared, claim, author, stats)

    pile_size = sum(int(p.get("count") or 0) for p in ctx.get("table", []))
    my_size = len(ctx["hand"])
    # Wrong challenges cost the pile; right ones hurt the bluffer. The bigger my
    # hand and the smaller the pile, the cheaper a gamble is.
    economic_boost = 0.0
    if my_size >= 5 and pile_size <= 3:
        economic_boost = 0.05
    elif my_size <= 2 and pile_size >= 8:
        economic_boost = -0.1

    threshold = {
        "easy": 0.75,
        "medium": 0.5,
        "hard": 0.35,
    }[strength]
    if p_lie >= 1.0:
        return True
    if strength == "hard" and p_lie + economic_boost >= 0.5 and my_size <= 3:
        # Endgame-ish: when running low, catching a bluff is worth more.
        return True
    return (p_lie + economic_boost) >= threshold and rng.random() < (0.55 if strength == "easy" else 0.9)


def decide(ctx: dict[str, Any], strength: str, rng: random.Random | None = None) -> dict[str, Any]:
    """Return a legal action dict for the AI on its turn.

    ``ctx`` keys: hand, declared_rank, table, players, own_id, revealed.
    Legal outputs:
      {"kind": "play", "cards": [...], "declared_rank": rank}   (rank only when opening)
      {"kind": "pass"}
      {"kind": "challenge", "target_play_id": id}
    """
    rng = rng or random.Random()
    hand = list(ctx.get("hand", []))
    if not hand:
        return {"kind": "pass"}
    declared = ctx.get("declared_rank")
    stats = _build_hand_stats(hand)

    if not declared:
        return _choose_leader_open(hand, rng)

    table = ctx.get("table", [])
    last = table[-1] if table else None

    # 1) Challenge the latest hand when it is not our own.
    if last and last.get("player_id") != ctx.get("own_id"):
        if _choose_challenge(ctx, strength, last, stats, rng):
            return {"kind": "challenge", "target_play_id": last["play_id"]}

    # 2) Continue by playing cards of the current declared rank.
    honest = stats["counts"].get(declared, 0)
    jokers = stats["jokers"]
    amount = _play_amount_scheme(strength, honest, jokers)
    use_honest = min(amount, honest)
    cards = [c for c in hand if rank_of(c) == declared][:use_honest]
    fill = max(0, amount - use_honest)
    if fill > 0:
        # Bluff filler: mismatched non-joker cards; jokers are never a lie.
        others = [c for c in hand if rank_of(c) != declared and not is_joker(c)]
        cards.extend(others[:fill])
    # Jokers ride along as honest wildcards when they help the claim.
    if jokers and amount > 0 and len(cards) < MAX_CARDS_PER_PLAY and rng.random() < 0.4:
        cards.extend([c for c in hand if is_joker(c)][:1])
    cards = cards[:MAX_CARDS_PER_PLAY]
    if cards and _is_in_hand(hand, cards):
        return {"kind": "play", "cards": cards, "declared_rank": declared}

    # 3) Nothing useful to lay: pass (forfeits the rest of the round) or bluff.
    if rng.random() < _pass_probability(strength):
        return {"kind": "pass"}
    fillers = [c for c in hand if not is_joker(c)][:1]
    if fillers:
        return {"kind": "play", "cards": fillers, "declared_rank": declared}
    return {"kind": "pass"}
