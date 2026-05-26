#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import csv
import html
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import requests

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from fabinsights.card_images import card_color_from_id, select_goagain_image_url
from fabinsights.downloader import resolve_api_key


API_URL = "https://fab-insights.azurewebsites.net/api/v1/download_csv"
GOAGAIN_CARDS_URL = "https://api.goagain.dev/v1/cards"
FORMAT_CODE = 7
DEFAULT_MAX_GAP_MINUTES = 120
COLOR_COUNT_MIN_GAMES = 10
OMENS_HEROES = {
    "aurora_emissary_of_lightning",
    "oscilio_scion_of_the_third_age",
    "zyggy",
}
HERO_ALIASES = {
    "aurora": "aurora_emissary_of_lightning",
    "aurora_emissary_of_lightning": "aurora_emissary_of_lightning",
    "oscilio": "oscilio_scion_of_the_third_age",
    "oscilio_forked_continuum": "oscilio_scion_of_the_third_age",
    "oscilio_scion_of_the_third_age": "oscilio_scion_of_the_third_age",
    "zyggy": "zyggy",
    "zyggy_starlight": "zyggy",
}
HERO_DISPLAY_NAMES = {
    "aurora_emissary_of_lightning": "Aurora, Emissary of Lightning",
    "oscilio_scion_of_the_third_age": "Oscilio, Scion of the Third Age",
    "zyggy": "Zyggy",
}
MATCHUP_HERO_COLUMNS = (
    ("aurora_emissary_of_lightning", "vs Aurora"),
    ("oscilio_scion_of_the_third_age", "vs Oscilio"),
    ("zyggy", "vs Zyggy"),
)
OMENS_WEAPON_IDS = {
    "aphrodias",
    "scorpio_comet_tail",
    "volzar_meteor_storm",
}
OMENS_WEAPON_NAMES = {
    "Aphrodias",
    "Scorpio, Comet Tail",
    "Volzar, Meteor Storm",
}


@dataclass(frozen=True)
class SeatGame:
    player: str
    opponent: str
    game_id: str
    game_guid: str
    created_at: str
    created_dt: datetime
    hero: str
    opponent_hero: str
    win: bool
    turns: int
    conceded: bool
    first_player: int
    cards: tuple[dict[str, Any], ...]
    arena: tuple[dict[str, str], ...]
    damage_dealt: float | int | None
    damage_blocked: float | int | None
    damage_threatened: float | int | None
    average_value_per_turn: float | int | None

    @property
    def card_total(self) -> int:
        return sum(int(card.get("qty") or 0) for card in self.cards)


def parse_args() -> argparse.Namespace:
    today = date.today()
    p = argparse.ArgumentParser(description="Build an Omens draft report from Talishar format 7 API CSVs.")
    p.add_argument("--start-date", default="2026-05-01", help="YYYY-MM-DD inclusive")
    p.add_argument("--end-date", default=today.isoformat(), help="YYYY-MM-DD inclusive")
    p.add_argument("--out-dir", default="data/reports/ad_hoc/omen_draft_3_0")
    p.add_argument("--max-gap-minutes", type=int, default=DEFAULT_MAX_GAP_MINUTES)
    p.add_argument("--image-limit", type=int, default=260, help="Maximum unique cards to resolve through GoAgain.")
    p.add_argument("--skip-images", action="store_true", help="Do not query GoAgain card images.")
    p.add_argument(
        "--custom-cards-file",
        default=None,
        help="Optional draftsim/custom-cards text file with [CustomCards] image_uris to use for previews.",
    )
    p.add_argument(
        "--include-non-omen",
        action="store_true",
        help="Include every format 7 hero instead of limiting 3-0 runs to Omens heroes.",
    )
    p.add_argument("--api-key-file", default=None)
    return p.parse_args()


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def parse_deck_json(raw: object) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_created_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


def int_value(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def number_value(value: object) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        parsed = float(str(value))
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def normalize_hero_id(value: object) -> str:
    hero = str(value or "").strip()
    return HERO_ALIASES.get(hero, hero)


def display_hero_name(value: object) -> str:
    hero = normalize_hero_id(value)
    return HERO_DISPLAY_NAMES.get(hero, hero.replace("_", " "))


def arena_exclusion_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def is_equipment_arena_card(card: dict[str, Any]) -> bool:
    card_id = str(card.get("id") or "").strip().lower()
    name = str(card.get("name") or "").strip()
    excluded_ids = set(HERO_ALIASES) | OMENS_HEROES | OMENS_WEAPON_IDS
    excluded_names = {
        *(arena_exclusion_name(name) for name in HERO_DISPLAY_NAMES.values()),
        *(arena_exclusion_name(name) for name in HERO_ALIASES),
        *(arena_exclusion_name(name) for name in OMENS_WEAPON_NAMES),
    }
    if card_id in excluded_ids:
        return False
    if arena_exclusion_name(name) in excluded_names:
        return False
    return True


def card_results(deck: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cards: list[dict[str, Any]] = []
    for row in deck.get("cardResults") or []:
        if not isinstance(row, dict):
            continue
        qty = int_value(row.get("numCopies"))
        card_id = str(row.get("cardId") or "").strip()
        name = str(row.get("cardName") or "").strip()
        if not qty or not card_id:
            continue
        cards.append(
            {
                "id": card_id,
                "name": name,
                "qty": qty,
                "pitch": int_value(row.get("pitchValue")),
                "played": int_value(row.get("played")),
                "hits": int_value(row.get("hits")),
                "blocked": int_value(row.get("blocked")),
                "pitched": int_value(row.get("pitched")),
                "discarded": int_value(row.get("discarded")),
                "color": card_color_from_id(card_id),
            }
        )
    cards.sort(key=lambda item: (str(item["name"]).lower(), str(item["id"])))
    return tuple(cards)


def arena_results(deck: dict[str, Any]) -> tuple[dict[str, str], ...]:
    cards: list[dict[str, str]] = []
    for row in deck.get("arenaCardResults") or []:
        if not isinstance(row, dict):
            continue
        card_id = str(row.get("cardId") or "").strip()
        name = str(row.get("cardName") or "").strip()
        if card_id:
            cards.append({"id": card_id, "name": name})
    return tuple(cards)


def fetch_day(api_key: str, day: date) -> tuple[dict[str, Any], list[dict[str, str]]]:
    last_error = ""
    for attempt in range(1, 4):
        try:
            response = requests.get(
                API_URL,
                params={"format": str(FORMAT_CODE), "date": day.isoformat()},
                headers={"x-functions-key": api_key},
                timeout=35,
            )
            if response.status_code != 200:
                return {"date": day.isoformat(), "status_code": response.status_code}, []
            meta = response.json()
            csv_response = requests.get(meta["download_url"], timeout=90)
            text = csv_response.text
            rows = list(csv.DictReader(io.StringIO(text))) if text.strip() else []
            return meta, rows
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < 3:
                time.sleep(attempt * 2)
    return {"date": day.isoformat(), "error": last_error}, []


def seat_games_from_rows(day: date, rows: list[dict[str, str]]) -> list[SeatGame]:
    games: list[SeatGame] = []
    for row in rows:
        for deck_key, player_key, opponent_key in (
            ("deck1_json", "player1_name", "player2_name"),
            ("deck2_json", "player2_name", "player1_name"),
        ):
            player = str(row.get(player_key) or "").strip()
            if not player:
                continue
            created_at = str(row.get("created_at") or "").strip()
            created_dt = parse_created_at(created_at)
            if created_dt is None:
                continue
            deck = parse_deck_json(row.get(deck_key))
            games.append(
                SeatGame(
                    player=player,
                    opponent=str(row.get(opponent_key) or "").strip(),
                    game_id=str(row.get("game_id") or "").strip(),
                    game_guid=str(row.get("game_guid") or "").strip(),
                    created_at=created_at,
                    created_dt=created_dt,
                    hero=normalize_hero_id(deck.get("playerHero")),
                    opponent_hero=normalize_hero_id(deck.get("opposingHero")),
                    win=int_value(deck.get("result")) == 1,
                    turns=int_value(deck.get("turns")),
                    conceded=str(row.get("conceded") or "").strip().lower() == "true",
                    first_player=int_value(deck.get("firstPlayer"), default=-1),
                    cards=card_results(deck),
                    arena=arena_results(deck),
                    damage_dealt=number_value(deck.get("totalDamageDealt")),
                    damage_blocked=number_value(deck.get("totalDamageBlocked")),
                    damage_threatened=number_value(deck.get("totalDamageThreatened")),
                    average_value_per_turn=number_value(deck.get("averageValuePerTurn")),
                )
            )
    return games


def dedupe_player_games(games: list[SeatGame]) -> list[SeatGame]:
    best: dict[tuple[str, str], SeatGame] = {}
    for game in games:
        game_key = game.game_id or game.game_guid or f"{game.created_at}:{game.opponent}"
        key = (game.player, game_key)
        current = best.get(key)
        if current is None or game.created_dt > current.created_dt:
            best[key] = game
    return list(best.values())


def split_sessions(games: list[SeatGame], max_gap_minutes: int) -> list[dict[str, Any]]:
    by_player: dict[str, list[SeatGame]] = defaultdict(list)
    for game in games:
        by_player[game.player].append(game)
    for player_games in by_player.values():
        player_games.sort(key=lambda game: game.created_dt)

    sessions: list[dict[str, Any]] = []
    for player, player_games in by_player.items():
        current: list[SeatGame] = []
        for game in player_games:
            if not current:
                current = [game]
                continue
            gap_minutes = (game.created_dt - current[-1].created_dt).total_seconds() / 60
            if gap_minutes > max_gap_minutes or game.hero != current[-1].hero:
                sessions.append({"player": player, "games": current})
                current = [game]
            else:
                current.append(game)
        if current:
            sessions.append({"player": player, "games": current})
    return sessions


def aggregate_cards(cards: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for card in cards:
        card_id = str(card["id"])
        row = by_id.setdefault(
            card_id,
            {
                "id": card_id,
                "name": card.get("name") or card_id,
                "qty": 0,
                "pitch": card.get("pitch"),
                "color": card.get("color") or "",
                "played": 0,
                "hits": 0,
                "blocked": 0,
                "pitched": 0,
                "discarded": 0,
            },
        )
        row["qty"] += int(card.get("qty") or 0)
        for key in ("played", "hits", "blocked", "pitched", "discarded"):
            row[key] += int(card.get(key) or 0)
    return sorted(by_id.values(), key=lambda item: (str(item["name"]).lower(), str(item["id"])))


def card_counter(cards: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for card in cards:
        counter[str(card["name"])] += int(card["qty"])
    return counter


def classify_candidate(window: list[SeatGame], best_deck_game: SeatGame, max_gap_minutes: int) -> str:
    duration = (window[-1].created_dt - window[0].created_dt).total_seconds() / 60
    unique_opponents = len({game.opponent for game in window if game.opponent})
    if (
        best_deck_game.card_total >= 30
        and unique_opponents >= 3
        and duration <= max_gap_minutes
        and all(game.turns > 0 and not game.conceded for game in window)
    ):
        return "strict"
    if best_deck_game.card_total >= 30 and unique_opponents >= 3 and duration <= max_gap_minutes:
        return "high"
    if unique_opponents >= 2 and duration <= max_gap_minutes:
        return "medium"
    return "low"


def candidate_from_window(
    player: str,
    session_games: list[SeatGame],
    index: int,
    window: list[SeatGame],
    max_gap_minutes: int,
) -> dict[str, Any]:
    best_deck_game = max(window, key=lambda game: game.card_total)
    cards = aggregate_cards(best_deck_game.cards)
    top_cards = [{"name": name, "qty": qty} for name, qty in card_counter(cards).most_common(12)]
    duration = (window[-1].created_dt - window[0].created_dt).total_seconds() / 60
    return {
        "player": player,
        "player_short": player[:8],
        "hero": window[0].hero,
        "session_games": len(session_games),
        "window_index": index,
        "start": window[0].created_at,
        "end": window[-1].created_at,
        "duration_min": round(duration, 1),
        "classification": classify_candidate(window, best_deck_game, max_gap_minutes),
        "unique_opponents": len({game.opponent for game in window if game.opponent}),
        "deck_cards_total": best_deck_game.card_total,
        "arena": list(best_deck_game.arena),
        "games": [
            {
                "at": game.created_at,
                "game_id": game.game_id,
                "opponent": game.opponent,
                "opponent_short": game.opponent[:8],
                "opponent_hero": game.opponent_hero,
                "turns": game.turns,
                "conceded": game.conceded,
                "card_total": game.card_total,
                "damage_dealt": game.damage_dealt,
                "damage_blocked": game.damage_blocked,
                "damage_threatened": game.damage_threatened,
                "average_value_per_turn": game.average_value_per_turn,
            }
            for game in window
        ],
        "top_cards": top_cards,
        "decklist": cards,
    }


def find_3_0_candidates(sessions: list[dict[str, Any]], max_gap_minutes: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for session in sessions:
        player = str(session["player"])
        games = list(session["games"])
        for index in range(0, max(0, len(games) - 2)):
            window = games[index : index + 3]
            if all(game.win for game in window):
                candidates.append(candidate_from_window(player, games, index, window, max_gap_minutes))
                break
    order = {"strict": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(candidates, key=lambda row: (order[row["classification"]], row["start"], row["player"]))


def dataset_profile(
    raw_rows_by_day: dict[str, int],
    raw_games: list[SeatGame],
    deduped_games: list[SeatGame],
    analysis_games: list[SeatGame],
    sessions: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    heroes = Counter(game.hero for game in deduped_games if game.hero)
    analysis_heroes = Counter(game.hero for game in analysis_games if game.hero)
    classifications = Counter(candidate["classification"] for candidate in candidates)
    strict_heroes = Counter(candidate["hero"] for candidate in candidates if candidate["classification"] == "strict")
    all_heroes = Counter(candidate["hero"] for candidate in candidates)
    card_rows = sum(len(game.cards) for game in deduped_games)
    arena_rows = sum(len(game.arena) for game in deduped_games)
    turns = [game.turns for game in deduped_games]
    return {
        "raw_rows_by_day": raw_rows_by_day,
        "raw_seats": len(raw_games),
        "deduped_player_games": len(deduped_games),
        "analyzed_player_games": len(analysis_games),
        "players": len({game.player for game in deduped_games}),
        "analyzed_players": len({game.player for game in analysis_games}),
        "sessions": len(sessions),
        "candidate_3_0_windows": len(candidates),
        "classification_counts": dict(classifications),
        "candidate_heroes": all_heroes.most_common(),
        "strict_heroes": strict_heroes.most_common(),
        "format": FORMAT_CODE,
        "card_result_rows": card_rows,
        "arena_result_rows": arena_rows,
        "turns": {
            "min": min(turns) if turns else None,
            "max": max(turns) if turns else None,
            "avg": round(sum(turns) / len(turns), 2) if turns else None,
        },
        "deduped_hero_seats": heroes.most_common(),
        "analyzed_hero_seats": analysis_heroes.most_common(),
    }


def aggregate_report_cards(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    strict = [candidate for candidate in candidates if candidate["classification"] == "strict"]
    card_counts: Counter[tuple[str, str]] = Counter()
    arena_counts: Counter[tuple[str, str]] = Counter()
    usage: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for candidate in strict:
        for card in candidate["decklist"]:
            key = (str(card["id"]), str(card["name"]))
            qty = int(card.get("qty") or 0)
            card_counts[key] += qty
            for stat in ("played", "hits", "blocked", "pitched", "discarded"):
                usage[key][stat] += int(card.get(stat) or 0)
        for card in candidate["arena"]:
            arena_counts[(str(card["id"]), str(card["name"]))] += 1
    top_cards = [
        {
            "id": card_id,
            "name": name,
            "copies": copies,
            **dict(usage[(card_id, name)]),
        }
        for (card_id, name), copies in card_counts.most_common(80)
    ]
    top_arena = [
        {"id": card_id, "name": name, "appearances": count}
        for (card_id, name), count in arena_counts.most_common(40)
    ]
    return {"top_cards": top_cards, "top_arena": top_arena}


def pct(wins: int, games: int) -> float:
    return round((wins / games) * 100, 1) if games else 0.0


def rounded(value: float) -> float:
    return round(value, 2)


def card_stats_row(card_id: str, row: dict[str, Any], *, games: int | None = None) -> dict[str, Any]:
    game_count = int(games if games is not None else row.get("games", 0))
    wins = int(row.get("wins", 0))
    copies = int(row.get("copies", 0))
    smoothed = ((wins + 2) / (game_count + 4)) * 100 if game_count else 0
    avg_copies = copies / game_count if game_count else 0
    played_pg = int(row.get("played", 0)) / game_count if game_count else 0
    blocked_pg = int(row.get("blocked", 0)) / game_count if game_count else 0
    pitched_pg = int(row.get("pitched", 0)) / game_count if game_count else 0
    hits_pg = int(row.get("hits", 0)) / game_count if game_count else 0
    score = (
        smoothed * 0.35
        + min(game_count, 50) * 2
        + min(avg_copies, 3) * 3
        + min(played_pg, 3) * 1.5
    )
    return {
        "id": card_id,
        "name": row.get("name") or card_id,
        "color": row.get("color") or "",
        "games": game_count,
        "wins": wins,
        "wr": pct(wins, game_count),
        "smoothed_wr": rounded(smoothed),
        "score": rounded(score),
        "copies": copies,
        "avg_copies": rounded(avg_copies),
        "played_pg": rounded(played_pg),
        "blocked_pg": rounded(blocked_pg),
        "pitched_pg": rounded(pitched_pg),
        "hits_pg": rounded(hits_pg),
    }


def empty_turn_order_stats() -> dict[str, int | float]:
    return {"games": 0, "wins": 0, "wr": 0.0}


def equipment_count_bucket(count: int) -> int:
    return min(4, max(0, count))


def equipment_count_label(count: int) -> str:
    return "4+" if count >= 4 else str(count)


def equipment_stats_row(card_id: str, row: dict[str, Any]) -> dict[str, Any]:
    game_count = int(row.get("games") or 0)
    wins = int(row.get("wins") or 0)
    smoothed = ((wins + 1.5) / (game_count + 3)) * 100 if game_count else 0
    score = smoothed + min(game_count, 80) * 0.18
    return {
        "id": card_id,
        "name": row.get("name") or card_id,
        "games": game_count,
        "wins": wins,
        "wr": pct(wins, game_count),
        "smoothed_wr": rounded(smoothed),
        "score": rounded(score),
    }


def build_performance_analytics(games: list[SeatGame]) -> dict[str, Any]:
    clean_games = [game for game in games if game.hero and game.turns > 0 and not game.conceded]
    by_hero_card: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    by_hero_color: dict[str, dict[str, dict[str, dict[str, Any]]]] = defaultdict(lambda: defaultdict(dict))
    by_hero_vs_card: dict[str, dict[str, dict[str, dict[str, Any]]]] = defaultdict(lambda: defaultdict(dict))
    by_hero_color_count: dict[str, dict[str, dict[int, Counter[str]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(Counter)))
    by_hero_color_mix: dict[str, dict[tuple[int, int, int], Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    by_hero_equipment_count: dict[str, dict[int, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    by_hero_equipment_card: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    by_hero_equipment_combo: dict[str, dict[tuple[str, ...], Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    matchup_stats: Counter[tuple[str, str, str]] = Counter()
    matchup_turn_order_stats: Counter[tuple[str, str, str, str]] = Counter()
    turn_order: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))

    for game in clean_games:
        matchup_stats[(game.hero, game.opponent_hero, "games")] += 1
        if game.win:
            matchup_stats[(game.hero, game.opponent_hero, "wins")] += 1
        bucket = "first" if game.first_player == 1 else "second" if game.first_player == 0 else "unknown"
        for matchup_bucket in ("total", bucket):
            matchup_turn_order_stats[(game.hero, game.opponent_hero, matchup_bucket, "games")] += 1
            if game.win:
                matchup_turn_order_stats[(game.hero, game.opponent_hero, matchup_bucket, "wins")] += 1
        turn_order[game.hero][bucket]["games"] += 1
        if game.win:
            turn_order[game.hero][bucket]["wins"] += 1

        color_counts: Counter[str] = Counter()
        for card in game.cards:
            color = str(card.get("color") or "").strip() or card_color_from_id(card.get("id"))
            if color in {"red", "yellow", "blue"}:
                color_counts[color] += int(card.get("qty") or 0)
        if game.cards:
            for color in ("red", "yellow", "blue"):
                count_bucket = by_hero_color_count[game.hero][color][int(color_counts[color])]
                count_bucket["games"] += 1
                if game.win:
                    count_bucket["wins"] += 1
            mix = (int(color_counts["red"]), int(color_counts["yellow"]), int(color_counts["blue"]))
            by_hero_color_mix[game.hero][mix]["games"] += 1
            if game.win:
                by_hero_color_mix[game.hero][mix]["wins"] += 1

        seen_equipment: dict[str, dict[str, str]] = {}
        for card in game.arena:
            if not is_equipment_arena_card(card):
                continue
            card_id = str(card.get("id") or "").strip()
            if not card_id or card_id in seen_equipment:
                continue
            seen_equipment[card_id] = {
                "id": card_id,
                "name": str(card.get("name") or card_id).strip() or card_id,
            }
        equipment_count = equipment_count_bucket(len(seen_equipment))
        by_hero_equipment_count[game.hero][equipment_count]["games"] += 1
        if game.win:
            by_hero_equipment_count[game.hero][equipment_count]["wins"] += 1
        combo = tuple(sorted(seen_equipment))
        by_hero_equipment_combo[game.hero][combo]["games"] += 1
        by_hero_equipment_combo[game.hero][combo]["equipment"] = [seen_equipment[card_id]["name"] for card_id in combo]
        if game.win:
            by_hero_equipment_combo[game.hero][combo]["wins"] += 1
        for card_id, card in seen_equipment.items():
            row = by_hero_equipment_card[game.hero].setdefault(
                card_id,
                {"name": card["name"], "games": 0, "wins": 0},
            )
            row["games"] += 1
            row["wins"] += 1 if game.win else 0

        seen_cards: set[str] = set()
        for card in game.cards:
            card_id = str(card.get("id") or "").strip()
            if not card_id or card_id in seen_cards:
                continue
            seen_cards.add(card_id)
            color = str(card.get("color") or "").strip() or card_color_from_id(card_id)
            for target in (
                by_hero_card[game.hero],
                by_hero_color[game.hero][color],
                by_hero_vs_card[game.hero][game.opponent_hero],
            ):
                row = target.setdefault(
                    card_id,
                    {
                        "name": card.get("name") or card_id,
                        "color": color,
                        "games": 0,
                        "wins": 0,
                        "copies": 0,
                        "played": 0,
                        "blocked": 0,
                        "pitched": 0,
                        "hits": 0,
                    },
                )
                row["games"] += 1
                row["wins"] += 1 if game.win else 0
                row["copies"] += int(card.get("qty") or 0)
                for stat in ("played", "blocked", "pitched", "hits"):
                    row[stat] += int(card.get(stat) or 0)

    hero_card_rankings: dict[str, list[dict[str, Any]]] = {}
    hero_color_played: dict[str, dict[str, list[dict[str, Any]]]] = {}
    hero_color_wr: dict[str, dict[str, list[dict[str, Any]]]] = {}
    vs_hero_cards: dict[str, dict[str, list[dict[str, Any]]]] = {}
    hero_color_count_wr: dict[str, dict[str, list[dict[str, Any]]]] = {}
    hero_color_mix_wr: dict[str, list[dict[str, Any]]] = {}
    hero_equipment_count_wr: dict[str, list[dict[str, Any]]] = {}
    hero_equipment_cards: dict[str, list[dict[str, Any]]] = {}
    hero_equipment_combos: dict[str, list[dict[str, Any]]] = {}

    for hero, card_rows in by_hero_card.items():
        rows = []
        for card_id, row in card_rows.items():
            out = card_stats_row(card_id, row)
            out["matchup_wr"] = {}
            for opponent, _label in MATCHUP_HERO_COLUMNS:
                vs_row = by_hero_vs_card.get(hero, {}).get(opponent, {}).get(card_id)
                game_count = int((vs_row or {}).get("games") or 0)
                wins = int((vs_row or {}).get("wins") or 0)
                out["matchup_wr"][opponent] = {"games": game_count, "wins": wins, "wr": pct(wins, game_count)}
            rows.append(out)
        rows.sort(key=lambda row: (-row["score"], -row["games"], str(row["name"]).lower()))
        hero_card_rankings[hero] = rows

    for hero, color_rows in by_hero_color.items():
        hero_color_played[hero] = {}
        hero_color_wr[hero] = {}
        for color in ("red", "yellow", "blue", ""):
            rows = [card_stats_row(card_id, row) for card_id, row in color_rows.get(color, {}).items()]
            if not rows:
                continue
            hero_color_played[hero][color or "unknown"] = sorted(
                rows,
                key=lambda row: (-row["copies"], -row["games"], -row["wr"], str(row["name"]).lower()),
            )[:20]
            hero_color_wr[hero][color or "unknown"] = sorted(
                rows,
                key=lambda row: (-row["smoothed_wr"], -row["games"], -row["wr"], str(row["name"]).lower()),
            )[:20]

    for hero, opponents in by_hero_vs_card.items():
        vs_hero_cards[hero] = {}
        for opponent, card_rows in opponents.items():
            rows = [card_stats_row(card_id, row) for card_id, row in card_rows.items()]
            rows.sort(key=lambda row: (-row["score"], -row["games"], str(row["name"]).lower()))
            vs_hero_cards[hero][opponent] = rows[:25]

    for hero, color_rows in by_hero_color_count.items():
        hero_color_count_wr[hero] = {}
        for color in ("red", "yellow", "blue"):
            rows = []
            for count, stats in color_rows.get(color, {}).items():
                game_count = int(stats.get("games", 0))
                if game_count < COLOR_COUNT_MIN_GAMES:
                    continue
                wins = int(stats.get("wins", 0))
                rows.append({"color": color, "count": count, "games": game_count, "wins": wins, "wr": pct(wins, game_count)})
            rows.sort(key=lambda row: int(row["count"]))
            hero_color_count_wr[hero][color] = rows

    for hero, mix_rows in by_hero_color_mix.items():
        rows = []
        for (red, yellow, blue), stats in mix_rows.items():
            game_count = int(stats.get("games", 0))
            if game_count < COLOR_COUNT_MIN_GAMES:
                continue
            wins = int(stats.get("wins", 0))
            rows.append({
                "red": red,
                "yellow": yellow,
                "blue": blue,
                "total": red + yellow + blue,
                "games": game_count,
                "wins": wins,
                "wr": pct(wins, game_count),
            })
        rows.sort(key=lambda row: (-int(row["games"]), -float(row["wr"]), int(row["red"]), int(row["yellow"]), int(row["blue"])))
        hero_color_mix_wr[hero] = rows[:40]

    for hero, count_rows in by_hero_equipment_count.items():
        rows = []
        for count in range(0, 5):
            stats = count_rows.get(count, Counter())
            game_count = int(stats.get("games", 0))
            wins = int(stats.get("wins", 0))
            rows.append({
                "count": count,
                "label": equipment_count_label(count),
                "games": game_count,
                "wins": wins,
                "wr": pct(wins, game_count),
            })
        hero_equipment_count_wr[hero] = rows

    for hero, equipment_rows in by_hero_equipment_card.items():
        rows = [equipment_stats_row(card_id, row) for card_id, row in equipment_rows.items()]
        rows.sort(key=lambda row: (-row["games"], -row["score"], -row["wr"], str(row["name"]).lower()))
        hero_equipment_cards[hero] = rows

    for hero, combo_rows in by_hero_equipment_combo.items():
        rows = []
        for card_ids, stats in combo_rows.items():
            game_count = int(stats.get("games", 0))
            wins = int(stats.get("wins", 0))
            equipment = list(stats.get("equipment") or [])
            rows.append({
                "equipment": equipment,
                "equipment_key": " + ".join(equipment) if equipment else "No equipment",
                "count": equipment_count_bucket(len(card_ids)),
                "label": equipment_count_label(len(card_ids)),
                "games": game_count,
                "wins": wins,
                "wr": pct(wins, game_count),
            })
        rows.sort(key=lambda row: (-int(row["games"]), -float(row["wr"]), str(row["equipment_key"]).lower()))
        hero_equipment_combos[hero] = rows[:40]

    matchups: list[dict[str, Any]] = []
    matchup_matrix: dict[str, dict[str, dict[str, dict[str, int | float]]]] = defaultdict(dict)
    matchup_pairs = sorted({(hero, opp) for hero, opp, _ in matchup_stats if hero and opp})
    for hero, opponent in matchup_pairs:
        game_count = matchup_stats[(hero, opponent, "games")]
        wins = matchup_stats[(hero, opponent, "wins")]
        matchups.append({"hero": hero, "opponent_hero": opponent, "games": game_count, "wins": wins, "wr": pct(wins, game_count)})
        matchup_matrix[hero][opponent] = {}
        for bucket in ("total", "first", "second"):
            bucket_games = matchup_turn_order_stats[(hero, opponent, bucket, "games")]
            bucket_wins = matchup_turn_order_stats[(hero, opponent, bucket, "wins")]
            matchup_matrix[hero][opponent][bucket] = {
                "games": bucket_games,
                "wins": bucket_wins,
                "wr": pct(bucket_wins, bucket_games),
            }
    matchups.sort(key=lambda row: (str(row["hero"]), -int(row["games"]), str(row["opponent_hero"])))

    first_second: dict[str, dict[str, dict[str, int | float]]] = {}
    for hero, buckets in turn_order.items():
        first_second[hero] = {}
        for bucket in ("first", "second", "unknown"):
            stats = buckets.get(bucket, Counter())
            game_count = int(stats.get("games", 0))
            wins = int(stats.get("wins", 0))
            first_second[hero][bucket] = {"games": game_count, "wins": wins, "wr": pct(wins, game_count)}

    return {
        "summary": {
            "games": len(clean_games),
            "heroes": len({game.hero for game in clean_games if game.hero}),
            "card_games": sum(1 for game in clean_games if game.cards),
            "equipment_games": sum(
                1 for game in clean_games if any(is_equipment_arena_card(card) for card in game.arena)
            ),
            "method": (
                "Card priority is inferred from cards present in played decks, not real draft picks. "
                "Score combines smoothed winrate, sample size, average copies, and played-per-game."
            ),
            "color_count_min_games": COLOR_COUNT_MIN_GAMES,
        },
        "hero_card_rankings": hero_card_rankings,
        "hero_color_played": hero_color_played,
        "hero_color_wr": hero_color_wr,
        "hero_color_count_wr": hero_color_count_wr,
        "hero_color_mix_wr": hero_color_mix_wr,
        "hero_equipment_count_wr": hero_equipment_count_wr,
        "hero_equipment_cards": hero_equipment_cards,
        "hero_equipment_combos": hero_equipment_combos,
        "matchups": matchups,
        "matchup_matrix": matchup_matrix,
        "first_second": first_second,
        "vs_hero_cards": vs_hero_cards,
    }


def build_strict_3_0_card_rankings(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    strict = [
        candidate
        for candidate in candidates
        if candidate.get("classification") == "strict" and int(candidate.get("deck_cards_total") or 0) >= 30
    ]
    lists_by_hero: Counter[str] = Counter()
    by_hero_card: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for candidate in strict:
        hero = str(candidate.get("hero") or "").strip()
        if not hero:
            continue
        lists_by_hero[hero] += 1
        for card in candidate.get("decklist") or []:
            card_id = str(card.get("id") or "").strip()
            if not card_id:
                continue
            row = by_hero_card[hero].setdefault(
                card_id,
                {
                    "name": card.get("name") or card_id,
                    "color": card.get("color") or card_color_from_id(card_id),
                    "games": 0,
                    "wins": 0,
                    "copies": 0,
                    "played": 0,
                    "blocked": 0,
                    "pitched": 0,
                    "hits": 0,
                },
            )
            row["games"] += 1
            row["wins"] += 1
            row["copies"] += int(card.get("qty") or 0)
            for stat in ("played", "blocked", "pitched", "hits"):
                row[stat] += int(card.get(stat) or 0)

    rankings: dict[str, list[dict[str, Any]]] = {}
    for hero, card_rows in by_hero_card.items():
        rows = []
        total_lists = lists_by_hero[hero]
        for card_id, row in card_rows.items():
            out = card_stats_row(card_id, row)
            out["deck_share"] = pct(int(row.get("games") or 0), total_lists)
            out["strict_3_0_lists"] = total_lists
            rows.append(out)
        rows.sort(key=lambda row: (-row["score"], -row["games"], str(row["name"]).lower()))
        rankings[hero] = rows

    return {
        "summary": {
            "strict_3_0_lists": sum(lists_by_hero.values()),
            "strict_3_0_lists_by_hero": lists_by_hero.most_common(),
            "method": (
                "Strict 3-0 card ranking uses only clean 3-0 decklists. Games means strict 3-0 lists containing "
                "the card; WR is therefore not a general winrate and should be read as success-list presence."
            ),
        },
        "hero_card_rankings": rankings,
    }


def add_strict_3_0_presence_to_analytics(analytics: dict[str, Any], strict_rankings: dict[str, Any]) -> None:
    strict_by_hero = strict_rankings.get("hero_card_rankings") or {}
    strict_index: dict[tuple[str, str], int] = {}
    for hero, rows in strict_by_hero.items():
        for row in rows or []:
            strict_index[(str(hero), str(row.get("id") or ""))] = int(row.get("games") or 0)

    for hero, rows in (analytics.get("hero_card_rankings") or {}).items():
        for row in rows or []:
            game_count = int(row.get("games") or 0)
            strict_count = strict_index.get((str(hero), str(row.get("id") or "")), 0)
            row["strict_3_0_count"] = strict_count
            row["strict_3_0_pct"] = pct(strict_count, game_count)


def image_cache_path(out_dir: Path) -> Path:
    return out_dir / "card_image_urls.json"


def load_image_cache(out_dir: Path) -> dict[str, dict[str, Any]]:
    path = image_cache_path(out_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_image_cache(out_dir: Path, cache: dict[str, dict[str, Any]]) -> None:
    image_cache_path(out_dir).write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def image_cache_key(card_id: object, name: object) -> str:
    return f"{str(card_id or '').strip()}|{str(name or '').strip()}"


def normalized_card_name(value: object) -> str:
    text = re.sub(r"\s+\((red|yellow|blue)\)\s*$", "", str(value or "").strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def custom_card_name_and_color(value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    match = re.search(r"\s+\((red|yellow|blue)\)\s*$", text, flags=re.IGNORECASE)
    color = match.group(1).lower() if match else ""
    name = re.sub(r"\s+\((red|yellow|blue)\)\s*$", "", text, flags=re.IGNORECASE).strip()
    return name, color


def extract_custom_cards_json(text: str) -> list[dict[str, Any]]:
    marker = "[CustomCards]"
    marker_index = text.find(marker)
    if marker_index < 0:
        return []
    start = text.find("[", marker_index + len(marker))
    if start < 0:
        return []
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                parsed = json.loads(text[start : index + 1])
                return parsed if isinstance(parsed, list) else []
    return []


def load_custom_card_images(path: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    try:
        cards = extract_custom_cards_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    images: dict[tuple[str, str], dict[str, Any]] = {}
    for card in cards:
        if not isinstance(card, dict):
            continue
        name, color = custom_card_name_and_color(card.get("name"))
        image_url = ((card.get("image_uris") or {}).get("en") if isinstance(card.get("image_uris"), dict) else "") or ""
        if not name or not image_url:
            continue
        images[(normalized_card_name(name), color)] = {
            "ok": True,
            "image_url": image_url,
            "source": str(path),
            "collector_number": card.get("collector_number") or "",
        }
    return images


def apply_custom_card_images(
    cache: dict[str, dict[str, Any]],
    card_refs: list[tuple[str, str]],
    custom_images: dict[tuple[str, str], dict[str, Any]],
) -> None:
    if not custom_images:
        return
    for card_id, name in card_refs:
        color = card_color_from_id(card_id)
        image = custom_images.get((normalized_card_name(name), color)) or custom_images.get((normalized_card_name(name), ""))
        if not image:
            continue
        key = image_cache_key(card_id, name)
        if not cache.get(key, {}).get("image_url"):
            cache[key] = dict(image)


def resolve_card_image(card_id: str, name: str) -> dict[str, Any]:
    if not name:
        return {"ok": False, "image_url": "", "error": "missing name"}
    url = f"{GOAGAIN_CARDS_URL}?{urlencode({'name': name, 'limit': 10})}"
    try:
        req = Request(url, headers={"User-Agent": "FABinsights/1.0"})
        with urlopen(req, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        image_url = select_goagain_image_url(payload, name=name, card_id=card_id, color=card_color_from_id(card_id))
        return {"ok": bool(image_url), "image_url": image_url, "source": url}
    except Exception as exc:
        return {"ok": False, "image_url": "", "error": str(exc), "source": url}


def iter_card_refs(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from iter_card_refs(item)
    elif isinstance(value, dict):
        if "id" in value and "name" in value:
            yield value
        for item in value.values():
            yield from iter_card_refs(item)


def collect_unique_cards(
    candidates: list[dict[str, Any]],
    aggregates: dict[str, list[dict[str, Any]]],
    analytics: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    unique: dict[str, tuple[str, str]] = {}
    for row in iter_card_refs(aggregates):
        unique[image_cache_key(row.get("id"), row.get("name"))] = (str(row.get("id") or ""), str(row.get("name") or ""))
    if analytics is not None:
        for row in iter_card_refs(analytics):
            unique[image_cache_key(row.get("id"), row.get("name"))] = (str(row.get("id") or ""), str(row.get("name") or ""))
    for candidate in candidates:
        for card in candidate["decklist"] + candidate["arena"]:
            unique[image_cache_key(card.get("id"), card.get("name"))] = (str(card.get("id") or ""), str(card.get("name") or ""))
    return list(unique.values())


def resolve_images(
    out_dir: Path,
    candidates: list[dict[str, Any]],
    aggregates: dict[str, list[dict[str, Any]]],
    analytics: dict[str, Any] | None = None,
    *,
    limit: int,
    skip: bool,
    custom_cards_file: Path | None = None,
) -> dict[str, dict[str, Any]]:
    cache = load_image_cache(out_dir)
    card_refs = collect_unique_cards(candidates, aggregates, analytics)
    apply_custom_card_images(cache, card_refs, load_custom_card_images(custom_cards_file))
    save_image_cache(out_dir, cache)
    if skip:
        return cache
    for card_id, name in card_refs[:limit]:
        key = image_cache_key(card_id, name)
        if key in cache:
            continue
        cache[key] = resolve_card_image(card_id, name)
    save_image_cache(out_dir, cache)
    return cache


def attach_images_to_cards(value: Any, images: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [attach_images_to_cards(item, images) for item in value]
    if isinstance(value, dict):
        out = {key: attach_images_to_cards(item, images) for key, item in value.items()}
        if "id" in out and "name" in out:
            image = images.get(image_cache_key(out.get("id"), out.get("name")), {})
            if image.get("image_url"):
                out["image_url"] = image["image_url"]
        return out
    return value


def sanitize_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove full player hashes from public artifacts; short labels remain for grouping."""
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        candidate.pop("player", None)
        for game in candidate.get("games") or []:
            if isinstance(game, dict):
                game.pop("opponent", None)
    return payload


def json_script(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    return text.replace("</", "<\\/")


def render_html(payload: dict[str, Any]) -> str:
    data_json = json_script(payload)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Omens Draft 3-0 Report</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #18202a;
      --muted: #65717f;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-2: #9f5f16;
      --danger: #b42318;
      --good: #12723a;
      --shadow: 0 12px 30px rgba(22, 28, 36, 0.10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
      letter-spacing: 0;
    }}
    header {{
      background: #17212d;
      color: #fff;
      padding: 28px 24px 22px;
      border-bottom: 4px solid var(--accent);
    }}
    .wrap {{ max-width: 1440px; margin: 0 auto; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; line-height: 1.15; font-weight: 760; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; }}
    h3 {{ margin: 0 0 10px; font-size: 15px; }}
    p {{ margin: 0; color: var(--muted); line-height: 1.45; }}
    header p {{ color: #c8d1dc; max-width: 980px; }}
    main {{ padding: 22px 24px 40px; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin: 18px 0;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .metric strong {{ display: block; font-size: 24px; line-height: 1; margin-bottom: 6px; }}
    .metric span {{ color: var(--muted); font-size: 12px; }}
    .band {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      margin: 14px 0;
    }}
    .grid-2 {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 0.45fr); gap: 14px; align-items: start; }}
    .controls {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) repeat(3, minmax(150px, 190px));
      gap: 10px;
      align-items: end;
      margin-top: 10px;
    }}
    label {{ display: grid; gap: 5px; font-size: 12px; color: var(--muted); }}
    input, select {{
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
    }}
    .segments {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 0; }}
    button.segment {{
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 8px 11px;
      font: inherit;
      cursor: pointer;
    }}
    button.segment.active {{ border-color: var(--accent); background: #e7f4f1; color: #064e48; }}
    .table-wrap {{ overflow: auto; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 720px; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #f0f3f7; color: #3d4855; font-size: 12px; position: sticky; top: 0; z-index: 1; }}
    .sort-button {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      width: 100%;
      border: 0;
      background: transparent;
      color: inherit;
      cursor: pointer;
      font: inherit;
      font-weight: 760;
      padding: 0;
      text-align: left;
    }}
    .sort-arrow {{ color: #758293; font-size: 10px; margin-left: auto; }}
    th[data-sort-active="asc"] .sort-arrow::after {{ content: " asc"; }}
    th[data-sort-active="desc"] .sort-arrow::after {{ content: " desc"; }}
    .runs {{ display: grid; gap: 12px; }}
    .run {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .run-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }}
    .run-title {{ font-weight: 760; }}
    .run-meta {{ color: var(--muted); font-size: 12px; margin-top: 3px; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      font-size: 12px;
      border: 1px solid var(--line);
      background: #fff;
      white-space: nowrap;
    }}
    .badge.strict {{ border-color: #98d5ad; background: #eaf8ef; color: var(--good); }}
    .badge.high {{ border-color: #b8d4f2; background: #edf6ff; color: #165a9f; }}
    .badge.medium {{ border-color: #e7c891; background: #fff7e8; color: var(--accent-2); }}
    .badge.low {{ border-color: #efaaa4; background: #fff0ee; color: var(--danger); }}
    .run-body {{ display: grid; grid-template-columns: minmax(300px, 0.85fr) minmax(0, 1.4fr); gap: 14px; padding: 14px 16px; }}
    .mini-list {{ display: grid; gap: 7px; }}
    .game-row {{ display: grid; grid-template-columns: 96px 1fr auto; gap: 8px; padding: 8px; background: #f7f9fb; border-radius: 6px; font-size: 12px; }}
    .decklist {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 7px; }}
    .card-line {{
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 8px;
      background: #fff;
      font-size: 13px;
    }}
    .qty {{ font-weight: 760; color: #344053; }}
    .card-name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .pitch {{ color: var(--muted); font-size: 12px; }}
    .arena {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .arena .card-chip {{
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 999px;
      padding: 5px 8px;
      font-size: 12px;
    }}
    .muted {{ color: var(--muted); }}
    .result-count {{ margin-top: 10px; font-size: 13px; color: var(--muted); }}
    .empty {{ padding: 18px; color: var(--muted); text-align: center; }}
    .stack {{ display: grid; gap: 14px; }}
    .subgrid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 14px; }}
    .section-title {{ display: flex; justify-content: space-between; gap: 10px; align-items: baseline; margin-bottom: 12px; }}
    .section-title .muted {{ font-size: 12px; }}
    .nowrap {{ white-space: nowrap; }}
    .matrix-table {{ min-width: 980px; table-layout: fixed; }}
    .matrix-table th, .matrix-table td {{ text-align: center; vertical-align: middle; }}
    .matrix-table th:first-child, .matrix-table td:first-child {{ text-align: left; width: 210px; }}
    .matrix-cell {{
      display: grid;
      gap: 2px;
      justify-items: center;
      border-radius: 6px;
      padding: 8px 6px;
      font-weight: 760;
    }}
    .matrix-cell .muted {{ font-weight: 500; font-size: 11px; }}
    .bar-table {{ display: grid; gap: 7px; }}
    .bar-row {{
      display: grid;
      grid-template-columns: 68px minmax(150px, 1fr) 76px 68px;
      gap: 8px;
      align-items: center;
      font-size: 12px;
    }}
    .bar-track {{
      height: 18px;
      border-radius: 999px;
      background: #eef1f5;
      border: 1px solid var(--line);
      overflow: hidden;
    }}
    .bar-fill {{ display: block; height: 100%; border-radius: inherit; }}
    .bar-label {{ font-weight: 760; }}
    .popover {{
      position: fixed;
      width: 240px;
      min-height: 336px;
      border-radius: 8px;
      border: 1px solid rgba(0,0,0,0.18);
      background: #fff;
      box-shadow: var(--shadow);
      padding: 8px;
      z-index: 50;
      display: none;
      pointer-events: none;
    }}
    .popover img {{ width: 100%; border-radius: 6px; display: block; }}
    .popover .no-img {{ padding: 18px; color: var(--muted); font-size: 13px; }}
    .card-preview-modal {{
      position: fixed;
      inset: 0;
      z-index: 80;
      display: grid;
      align-items: end;
      justify-items: center;
      padding: 18px;
      background: rgba(12, 18, 26, 0.64);
    }}
    .card-preview-modal[aria-hidden="true"] {{ display: none; }}
    .preview-sheet {{
      width: min(360px, 100%);
      max-height: calc(100vh - 36px);
      display: grid;
      gap: 10px;
      border-radius: 10px;
      background: #fff;
      box-shadow: var(--shadow);
      padding: 10px;
    }}
    .preview-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 34px;
      gap: 8px;
      align-items: center;
    }}
    .preview-title {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 14px;
      font-weight: 760;
    }}
    .preview-close {{
      width: 34px;
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-weight: 760;
      cursor: pointer;
    }}
    .preview-body {{ display: grid; justify-items: center; }}
    .preview-body img {{
      width: min(100%, 330px);
      max-height: calc(100vh - 118px);
      object-fit: contain;
      border-radius: 8px;
      display: block;
    }}
    .preview-body .no-img {{ padding: 24px; color: var(--muted); text-align: center; }}
    details summary {{ cursor: pointer; color: #344053; font-weight: 700; }}
    code {{ background: #eef1f5; padding: 1px 4px; border-radius: 4px; }}
    @media (max-width: 900px) {{
      .grid-2, .run-body, .controls, .run-head {{ grid-template-columns: 1fr; }}
      main {{ padding: 16px; }}
      header {{ padding: 22px 16px; }}
      .game-row {{ grid-template-columns: 1fr; }}
      .popover {{ display: none !important; }}
      .card-chip, .card-line {{ cursor: pointer; }}
    }}
    @media (max-width: 640px) {{
      h1 {{ font-size: 24px; }}
      h2 {{ font-size: 16px; }}
      main {{ padding: 12px; }}
      .band {{ padding: 12px; margin: 10px 0; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
      .metric {{ padding: 10px; }}
      .metric strong {{ font-size: 20px; }}
      .segments {{
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 4px;
      }}
      button.segment {{ flex: 0 0 auto; }}
      .section-title {{ display: grid; gap: 4px; }}
      .subgrid {{ grid-template-columns: 1fr; }}
      .table-wrap {{ margin-inline: -12px; border-left: 0; border-right: 0; border-radius: 0; }}
      table {{ min-width: 650px; }}
      .matrix-table {{ min-width: 920px; }}
      .decklist {{ grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }}
      .card-line {{ grid-template-columns: 24px minmax(0, 1fr) auto; }}
      .bar-row {{ grid-template-columns: 48px minmax(95px, 1fr) 52px 50px; gap: 6px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <h1>Omens Draft 3-0 Report</h1>
      <p>Talishar format 7 draft data grouped by consecutive player runs. Card previews use GoAgain image URLs when the card is available there.</p>
    </div>
  </header>
  <main>
    <div class="wrap">
      <section class="metrics" id="metrics"></section>

      <section class="band">
        <h2>Scope And Method</h2>
        <p id="method"></p>
        <div class="segments" id="viewSegments">
          <button class="segment active" data-view="runs" type="button">3-0 Runs</button>
          <button class="segment" data-view="rankings" type="button">Hero Card Rankings</button>
          <button class="segment" data-view="colors" type="button">Colors</button>
          <button class="segment" data-view="colorCounts" type="button">Color Count WR</button>
          <button class="segment" data-view="equipment" type="button">Equipment & Heroes</button>
          <button class="segment" data-view="matchups" type="button">Matchups</button>
          <button class="segment" data-view="vs" type="button">Vs Hero Cards</button>
          <button class="segment" data-view="cards" type="button">Card Totals</button>
          <button class="segment" data-view="fields" type="button">Available Fields</button>
        </div>
        <div class="controls">
          <label>Search
            <input id="search" type="search" placeholder="Hero, card, player, opponent">
          </label>
          <label>Hero
            <select id="heroFilter"></select>
          </label>
          <label>Classification
            <select id="classFilter">
              <option value="">All</option>
              <option value="strict" selected>Strict clean</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
          </label>
          <label>Decklist
            <select id="deckFilter">
              <option value="">All</option>
              <option value="with" selected>30 cards present</option>
              <option value="missing">Missing card list</option>
            </select>
          </label>
        </div>
        <div class="result-count" id="resultCount"></div>
      </section>

      <section id="runsView" class="runs"></section>

      <section id="rankingsView" class="stack" style="display:none"></section>

      <section id="colorsView" class="stack" style="display:none"></section>

      <section id="colorCountsView" class="stack" style="display:none"></section>

      <section id="equipmentView" class="stack" style="display:none"></section>

      <section id="matchupsView" class="stack" style="display:none"></section>

      <section id="vsView" class="stack" style="display:none"></section>

      <section id="cardsView" class="grid-2" style="display:none">
        <div class="band">
          <h2>Top Cards In Strict 3-0 Decks</h2>
          <div class="table-wrap"><table id="topCardsTable"></table></div>
        </div>
        <div class="band">
          <h2>Top Arena Cards</h2>
          <div class="table-wrap"><table id="arenaTable"></table></div>
        </div>
      </section>

      <section id="fieldsView" class="band" style="display:none">
        <h2>Available Data</h2>
        <div id="fields"></div>
      </section>
    </div>
  </main>
  <div class="popover" id="popover"></div>
  <div class="card-preview-modal" id="cardPreviewModal" aria-hidden="true">
    <div class="preview-sheet" role="dialog" aria-modal="true" aria-labelledby="cardPreviewTitle">
      <div class="preview-head">
        <div class="preview-title" id="cardPreviewTitle"></div>
        <button class="preview-close" id="cardPreviewClose" type="button" aria-label="Close card preview">X</button>
      </div>
      <div class="preview-body" id="cardPreviewBody"></div>
    </div>
  </div>
  <script id="report-data" type="application/json">{data_json}</script>
  <script>
    const report = JSON.parse(document.getElementById("report-data").textContent);
    const state = {{ view: "runs" }};
    const qs = (sel) => document.querySelector(sel);
    const qsa = (sel) => Array.from(document.querySelectorAll(sel));
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (ch) => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[ch]));
    const heroDisplayNames = report.hero_display_names || {{}};
    const shortHero = (hero) => heroDisplayNames[hero] || String(hero || "").replaceAll("_", " ");
    const matchupHeroes = [
      {{id: "aurora_emissary_of_lightning", label: "vs Aurora"}},
      {{id: "oscilio_scion_of_the_third_age", label: "vs Oscilio"}},
      {{id: "zyggy", label: "vs Zyggy"}}
    ];

    function metric(label, value) {{
      return `<div class="metric"><strong>${{esc(value)}}</strong><span>${{esc(label)}}</span></div>`;
    }}

    function renderMetrics() {{
      const p = report.profile;
      qs("#metrics").innerHTML = [
        metric("candidate 3-0 windows", p.candidate_3_0_windows),
        metric("strict clean 3-0", p.classification_counts.strict || 0),
        metric("analyzed player-games", p.analyzed_player_games),
        metric("analyzed players", p.analyzed_players),
        metric("sessions", p.sessions),
        metric("card result rows", p.card_result_rows),
        metric("equipment games", report.analytics.summary.equipment_games || 0)
      ].join("");
      qs("#method").innerHTML = esc(report.method);
    }}

    function initFilters() {{
      const heroes = Array.from(new Set([
        ...report.candidates.map((r) => r.hero).filter(Boolean),
        ...Object.keys(report.analytics.hero_card_rankings || {{}})
      ])).sort();
      qs("#heroFilter").innerHTML = `<option value="">All</option>` + heroes.map((h) => `<option value="${{esc(h)}}">${{esc(shortHero(h))}}</option>`).join("");
      for (const id of ["search", "heroFilter", "classFilter", "deckFilter"]) {{
        qs("#" + id).addEventListener("input", renderCurrent);
        qs("#" + id).addEventListener("change", renderCurrent);
      }}
      qsa("[data-view]").forEach((button) => button.addEventListener("click", () => {{
        state.view = button.dataset.view;
        qsa("[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
        renderCurrent();
      }}));
    }}

    function runHaystack(run) {{
      return [
        run.player_short, run.hero, run.classification,
        ...run.games.flatMap((g) => [g.opponent_short, g.opponent_hero, g.game_id]),
        ...run.decklist.flatMap((c) => [c.name, c.id]),
        ...run.arena.flatMap((c) => [c.name, c.id])
      ].join(" ").toLowerCase();
    }}

    function filteredRuns() {{
      const search = qs("#search").value.trim().toLowerCase();
      const hero = qs("#heroFilter").value;
      const klass = qs("#classFilter").value;
      const deck = qs("#deckFilter").value;
      return report.candidates.filter((run) => {{
        if (hero && run.hero !== hero) return false;
        if (klass && run.classification !== klass) return false;
        if (deck === "with" && Number(run.deck_cards_total || 0) < 30) return false;
        if (deck === "missing" && Number(run.deck_cards_total || 0) >= 30) return false;
        if (search && !runHaystack(run).includes(search)) return false;
        return true;
      }});
    }}

    function cardAttrs(card) {{
      const url = card.image_url || "";
      return `data-image="${{esc(url)}}" data-card="${{esc(card.name || card.id)}}"`
    }}

    function renderCardLine(card) {{
      return `<div class="card-line" ${{cardAttrs(card)}}>
        <span class="qty">${{esc(card.qty || 1)}}</span>
        <span class="card-name">${{esc(card.name || card.id)}}</span>
        <span class="pitch">${{esc(card.color || card.pitch || "")}}</span>
      </div>`;
    }}

    function renderRun(run) {{
      const games = run.games.map((g) => `<div class="game-row">
        <span>${{esc(String(g.at).slice(5, 16))}}</span>
        <span>vs ${{esc(shortHero(g.opponent_hero))}} <span class="muted">(${{esc(g.opponent_short)}})</span></span>
        <span>${{esc(g.turns)}} turns${{g.conceded ? " conceded" : ""}}</span>
      </div>`).join("");
      const arena = run.arena.length ? `<div class="arena">${{run.arena.map((c) => `<span class="card-chip" ${{cardAttrs(c)}}>${{esc(c.name || c.id)}}</span>`).join("")}}</div>` : `<p>No arena cards recorded.</p>`;
      const deck = run.decklist.length ? `<div class="decklist">${{run.decklist.map(renderCardLine).join("")}}</div>` : `<p>No cardResults decklist recorded for this run.</p>`;
      return `<article class="run">
        <div class="run-head">
          <div>
            <div class="run-title">${{esc(shortHero(run.hero))}} · ${{esc(run.player_short)}} · 3-0</div>
            <div class="run-meta">${{esc(run.start)}} to ${{esc(run.end)}} · ${{esc(run.duration_min)}} min · ${{esc(run.unique_opponents)}} opponents · session games ${{esc(run.session_games)}}</div>
          </div>
          <span class="badge ${{esc(run.classification)}}">${{esc(run.classification)}}</span>
        </div>
        <div class="run-body">
          <div>
            <h3>Games</h3>
            <div class="mini-list">${{games}}</div>
            <h3 style="margin-top:12px">Arena</h3>
            ${{arena}}
          </div>
          <div>
            <h3>Decklist ${{run.deck_cards_total ? `(${{run.deck_cards_total}} cards)` : ""}}</h3>
            ${{deck}}
          </div>
        </div>
      </article>`;
    }}

    function renderRuns() {{
      const runs = filteredRuns();
      qs("#resultCount").textContent = `Showing ${{runs.length}} of ${{report.candidates.length}} candidate 3-0 windows.`;
      qs("#runsView").innerHTML = runs.length ? runs.map(renderRun).join("") : `<div class="band empty">No runs match the filters.</div>`;
    }}

    function sortValue(column, row) {{
      if (column.sortValue) return column.sortValue(row);
      if (column.sortKey) return row[column.sortKey];
      if (column.key) return row[column.key];
      return "";
    }}

    function tableHead(columns) {{
      return `<thead><tr>${{columns.map((c, index) => `<th><button class="sort-button" type="button" data-sort-col="${{index}}">${{esc(c.label)}}<span class="sort-arrow">^v</span></button></th>`).join("")}}</tr></thead>`;
    }}

    function tableBody(columns, rows) {{
      return `<tbody>${{rows.map((row) => `<tr>${{columns.map((c) => {{
        const rendered = c.render ? c.render(row) : esc(row[c.key]);
        return `<td data-sort="${{esc(sortValue(c, row))}}">${{rendered}}</td>`;
      }}).join("")}}</tr>`).join("")}}</tbody>`;
    }}

    function renderTable(selector, columns, rows) {{
      const table = qs(selector);
      table.classList.add("sortable-table");
      table.innerHTML = tableHead(columns) + tableBody(columns, rows);
    }}

    function tableMarkup(columns, rows) {{
      if (!rows.length) return `<div class="empty">No rows for this filter.</div>`;
      return `<div class="table-wrap"><table class="sortable-table">${{tableHead(columns) + tableBody(columns, rows)}}</table></div>`;
    }}

    function normalizedSortValue(row, colIndex) {{
      const raw = (row.children[colIndex]?.dataset.sort || "").trim();
      if (/^-?\\d+(\\.\\d+)?$/.test(raw)) return {{number: Number(raw), text: ""}};
      return {{number: null, text: raw.toLowerCase()}};
    }}

    function sortTable(table, colIndex) {{
      const tbody = table.tBodies[0];
      if (!tbody) return;
      const currentCol = Number(table.dataset.sortCol ?? -1);
      const currentDir = table.dataset.sortDir || "asc";
      const dir = currentCol === colIndex && currentDir === "asc" ? "desc" : "asc";
      const rows = Array.from(tbody.rows);
      rows.sort((a, b) => {{
        const av = normalizedSortValue(a, colIndex);
        const bv = normalizedSortValue(b, colIndex);
        let result = 0;
        if (av.number !== null && bv.number !== null) result = av.number - bv.number;
        else result = av.text.localeCompare(bv.text, undefined, {{numeric: true, sensitivity: "base"}});
        return dir === "asc" ? result : -result;
      }});
      tbody.append(...rows);
      table.dataset.sortCol = String(colIndex);
      table.dataset.sortDir = dir;
      Array.from(table.tHead?.querySelectorAll("th") || []).forEach((th, index) => {{
        if (index === colIndex) th.dataset.sortActive = dir;
        else delete th.dataset.sortActive;
      }});
    }}

    function selectedAnalyticsHeroes(source) {{
      const hero = qs("#heroFilter").value;
      const heroes = Object.keys(source || {{}}).sort();
      return hero ? heroes.filter((h) => h === hero) : heroes;
    }}

    function searchRows(rows) {{
      const search = qs("#search").value.trim().toLowerCase();
      if (!search) return rows;
      return rows.filter((row) => Object.values(row).join(" ").toLowerCase().includes(search));
    }}

    function pctCell(row) {{
      return `<span class="nowrap">${{esc(row.wr)}}%</span><div class="muted">${{esc(row.wins)}}/${{esc(row.games)}}</div>`;
    }}

    function wrColor(wr) {{
      const value = Math.max(0, Math.min(100, Number(wr || 0)));
      const hue = Math.round(value * 1.2);
      return `hsl(${{hue}} 58% 42%)`;
    }}

    function matchupCell(row, heroId) {{
      const value = (row.matchup_wr || {{}})[heroId] || {{games: 0, wins: 0, wr: 0}};
      return `<span class="nowrap">${{esc(value.wr)}}%</span><div class="muted">${{esc(value.wins)}}/${{esc(value.games)}}</div>`;
    }}

    function cardCell(row) {{
      return `<span class="card-chip" ${{cardAttrs(row)}}>${{esc(row.name || row.id)}}</span><div class="muted">${{esc(row.id || "")}}</div>`;
    }}

    function equipmentComboCell(row) {{
      const items = row.equipment || [];
      if (!items.length) return `<span class="muted">No equipment</span>`;
      return `<div class="mini-list">${{items.map((name) => `<span>${{esc(name)}}</span>`).join("")}}</div>`;
    }}

    const cardRankColumns = [
      {{label: "#", sortKey: "rank", render: (r) => esc(r.rank)}},
      {{label: "Card", sortKey: "name", render: cardCell}},
      {{label: "Color", key: "color"}},
      {{label: "Games", key: "games"}},
      {{label: "Wins", key: "wins"}},
      {{label: "WR", sortKey: "wr", render: pctCell}},
      {{label: "3-0 Lists", key: "strict_3_0_count"}},
      {{label: "3-0 %", sortKey: "strict_3_0_pct", render: (r) => `${{esc(r.strict_3_0_pct || 0)}}%`}},
      {{label: "Score", key: "score"}},
      {{label: "Avg copies", key: "avg_copies"}},
      {{label: "Played/G", key: "played_pg"}},
      {{label: "Blocked/G", key: "blocked_pg"}},
      {{label: "Pitched/G", key: "pitched_pg"}},
      {{label: "Hits/G", key: "hits_pg"}},
      ...matchupHeroes.map((h) => ({{label: h.label, sortValue: (r) => (((r.matchup_wr || {{}})[h.id] || {{}}).wr || 0), render: (r) => matchupCell(r, h.id)}}))
    ];

    const compactCardRankColumns = [
      {{label: "#", sortKey: "rank", render: (r) => esc(r.rank)}},
      {{label: "Card", sortKey: "name", render: cardCell}},
      {{label: "Color", key: "color"}},
      {{label: "Games", key: "games"}},
      {{label: "WR", sortKey: "wr", render: pctCell}},
      {{label: "Score", key: "score"}},
      {{label: "Avg copies", key: "avg_copies"}},
      {{label: "Played/G", key: "played_pg"}},
      {{label: "Blocked/G", key: "blocked_pg"}},
      {{label: "Pitched/G", key: "pitched_pg"}}
    ];

    function rankedRows(rows, limit = null) {{
      const filtered = searchRows(rows);
      const sliced = Number.isFinite(limit) ? filtered.slice(0, limit) : filtered;
      return sliced.map((row, idx) => ({{...row, rank: idx + 1}}));
    }}

    function renderRankings() {{
      const analytics = report.analytics || {{}};
      const source = analytics.hero_card_rankings || {{}};
      const heroes = selectedAnalyticsHeroes(source);
      const shownRows = heroes.reduce((total, hero) => total + searchRows(source[hero] || []).length, 0);
      qs("#resultCount").textContent = `${{shownRows}} ranked cards shown from ${{analytics.summary.games}} clean player-games. Includes 3-0 presence and matchup winrates.`;
      qs("#rankingsView").innerHTML = heroes.length ? heroes.map((hero) => {{
        const rows = rankedRows(source[hero] || []);
        return `<div class="band">
          <div class="section-title"><h2>${{esc(shortHero(hero))}} Full Card Ranking</h2><span class="muted">score, 3-0 share, and WR by matchup</span></div>
          ${{tableMarkup(cardRankColumns, rows)}}
        </div>`;
      }}).join("") : `<div class="band empty">No hero card ranking data.</div>`;
    }}

    function renderColors() {{
      const analytics = report.analytics || {{}};
      const played = analytics.hero_color_played || {{}};
      const wr = analytics.hero_color_wr || {{}};
      const heroes = selectedAnalyticsHeroes(played);
      qs("#resultCount").textContent = `${{analytics.summary.card_games}} player-games have cardResults for color analysis.`;
      const cols = [
        {{label: "Card", sortKey: "name", render: cardCell}},
        {{label: "Games", key: "games"}},
        {{label: "WR", sortKey: "wr", render: pctCell}},
        {{label: "Copies", key: "copies"}},
        {{label: "Avg copies", key: "avg_copies"}}
      ];
      qs("#colorsView").innerHTML = heroes.length ? heroes.map((hero) => {{
        const colorBlocks = ["red", "yellow", "blue"].map((color) => {{
          const playedRows = rankedRows(((played[hero] || {{}})[color] || []), 12);
          const wrRows = rankedRows(((wr[hero] || {{}})[color] || []), 12);
          return `<div class="band">
            <div class="section-title"><h2>${{esc(shortHero(hero))}} · ${{esc(color)}}</h2><span class="muted">most played and best WR</span></div>
            <div class="subgrid">
              <div><h3>Most played</h3>${{tableMarkup(cols, playedRows)}}</div>
              <div><h3>Highest winrate</h3>${{tableMarkup(cols, wrRows)}}</div>
            </div>
          </div>`;
        }}).join("");
        return colorBlocks;
      }}).join("") : `<div class="band empty">No color data.</div>`;
    }}

    function renderColorCounts() {{
      const analytics = report.analytics || {{}};
      const counts = analytics.hero_color_count_wr || {{}};
      const mixes = analytics.hero_color_mix_wr || {{}};
      const heroes = selectedAnalyticsHeroes(counts);
      const mixCols = [
        {{label: "Red", key: "red"}},
        {{label: "Yellow", key: "yellow"}},
        {{label: "Blue", key: "blue"}},
        {{label: "Total", key: "total"}},
        {{label: "Games", key: "games"}},
        {{label: "WR", sortKey: "wr", render: pctCell}}
      ];
      function colorCountBars(rows, color) {{
        const visibleRows = rows.filter((row) => Number(row.games || 0) > 0);
        if (!visibleRows.length) return `<div class="empty">No rows for this filter.</div>`;
        return `<div class="bar-table">${{visibleRows.map((row) => {{
          const wr = Number(row.wr || 0);
          const width = wr > 0 ? Math.max(4, Math.round(wr)) : 0;
          const colorStyle = wrColor(wr);
          return `<div class="bar-row">
            <span class="bar-label">${{esc(row.count)}} ${{esc(color[0].toUpperCase())}}</span>
            <span class="bar-track" title="${{esc(row.wr)}}% WR with ${{esc(row.count)}} ${{esc(color)}} cards"><span class="bar-fill" style="width:${{width}}%; background:${{colorStyle}}"></span></span>
            <span class="nowrap">${{esc(row.wr)}}%</span>
            <span class="muted">${{esc(row.wins)}}/${{esc(row.games)}}</span>
          </div>`;
        }}).join("")}}</div>`;
      }}
      const minGames = analytics.summary.color_count_min_games || 10;
      qs("#resultCount").textContent = `Winrate by number of red, yellow, and blue cards in the recorded decklist. Buckets need at least ${{minGames}} games.`;
      qs("#colorCountsView").innerHTML = heroes.length ? heroes.map((hero) => {{
        const colorBlocks = ["red", "yellow", "blue"].map((color) => {{
          const rows = ((counts[hero] || {{}})[color] || []).filter((row) => Number(row.games || 0) > 0);
          return `<div>
            <h3>${{esc(color)}} count <span class="muted">(min ${{esc(minGames)}} games; bar = WR 0-100%)</span></h3>
            ${{colorCountBars(rows, color)}}
          </div>`;
        }}).join("");
        return `<div class="band">
          <div class="section-title"><h2>${{esc(shortHero(hero))}} Color Count Winrates</h2><span class="muted">counts are card copies in cardResults decklists</span></div>
          <div class="subgrid">${{colorBlocks}}</div>
          <h3 style="margin-top:14px">Most common exact mixes</h3>
          ${{tableMarkup(mixCols, (mixes[hero] || []))}}
        </div>`;
      }}).join("") : `<div class="band empty">No color-count data.</div>`;
    }}

    function renderEquipment() {{
      const analytics = report.analytics || {{}};
      const counts = analytics.hero_equipment_count_wr || {{}};
      const cards = analytics.hero_equipment_cards || {{}};
      const combos = analytics.hero_equipment_combos || {{}};
      const heroes = selectedAnalyticsHeroes(counts);
      const equipmentCols = [
        {{label: "Equipment", sortKey: "name", render: cardCell}},
        {{label: "Games", key: "games"}},
        {{label: "Wins", key: "wins"}},
        {{label: "WR", sortKey: "wr", render: pctCell}},
        {{label: "Score", key: "score"}}
      ];
      const comboCols = [
        {{label: "Equipment set", sortKey: "equipment_key", render: equipmentComboCell}},
        {{label: "Count", sortKey: "count", render: (r) => esc(r.label)}},
        {{label: "Games", key: "games"}},
        {{label: "WR", sortKey: "wr", render: pctCell}}
      ];
      function countBars(rows) {{
        const visibleRows = (rows || []).filter((row) => Number(row.games || 0) > 0);
        if (!visibleRows.length) return `<div class="empty">No equipment count data.</div>`;
        return `<div class="bar-table">${{visibleRows.map((row) => {{
          const wr = Number(row.wr || 0);
          const width = wr > 0 ? Math.max(4, Math.round(wr)) : 0;
          return `<div class="bar-row">
            <span class="bar-label">${{esc(row.label)}}</span>
            <span class="bar-track" title="${{esc(row.wr)}}% WR with ${{esc(row.label)}} equipment"><span class="bar-fill" style="width:${{width}}%; background:${{wrColor(wr)}}"></span></span>
            <span class="nowrap">${{esc(row.wr)}}%</span>
            <span class="muted">${{esc(row.wins)}}/${{esc(row.games)}}</span>
          </div>`;
        }}).join("")}}</div>`;
      }}
      qs("#resultCount").textContent = `${{analytics.summary.equipment_games || 0}} clean player-games have arenaCardResults equipment data. Counts are bucketed as 0, 1, 2, 3, and 4+.`;
      qs("#equipmentView").innerHTML = heroes.length ? heroes.map((hero) => {{
        const countRows = counts[hero] || [];
        const equipmentRows = rankedRows(cards[hero] || [], 40);
        const comboRows = rankedRows(combos[hero] || [], 40);
        return `<div class="band">
          <div class="section-title"><h2>${{esc(shortHero(hero))}} Equipment Count WR</h2><span class="muted">arenaCardResults; bar = WR 0-100%</span></div>
          ${{countBars(countRows)}}
          <div class="subgrid" style="margin-top:14px">
            <div>
              <h3>Equipment by winrate</h3>
              ${{tableMarkup(equipmentCols, equipmentRows)}}
            </div>
            <div>
              <h3>Most common equipment sets</h3>
              ${{tableMarkup(comboCols, comboRows)}}
            </div>
          </div>
        </div>`;
      }}).join("") : `<div class="band empty">No equipment data.</div>`;
    }}

    function renderMatchups() {{
      const analytics = report.analytics || {{}};
      const hero = qs("#heroFilter").value;
      const search = qs("#search").value.trim().toLowerCase();
      const allMatchups = analytics.matchups || [];
      let matchups = allMatchups;
      if (hero) matchups = matchups.filter((row) => row.hero === hero);
      if (search) matchups = matchups.filter((row) => [row.hero, row.opponent_hero].join(" ").toLowerCase().includes(search));
      const matrix = analytics.matchup_matrix || {{}};
      function matrixStatCell(stat) {{
        const row = stat || {{games: 0, wins: 0, wr: 0}};
        const bg = row.games ? wrColor(row.wr) : "#eef1f5";
        const fg = row.games && Number(row.wr || 0) < 42 ? "#fff" : "#18202a";
        return `<td><div class="matrix-cell" style="background:${{bg}}; color:${{fg}}">
          <span>${{row.games ? `${{esc(row.wr)}}%` : "-"}}</span>
          <span class="muted" style="color:${{fg}}">${{row.games ? `${{esc(row.wins)}}/${{esc(row.games)}}` : "0 games"}}</span>
        </div></td>`;
      }}
      const matrixMarkup = `<div class="table-wrap"><table class="matrix-table">
        <thead>
          <tr><th rowspan="2">Hero \\ Opponent</th>${{matchupHeroes.map((h) => `<th colspan="3">${{esc(shortHero(h.id))}}</th>`).join("")}}</tr>
          <tr>${{matchupHeroes.map(() => `<th>Total</th><th>First</th><th>Second</th>`).join("")}}</tr>
        </thead>
        <tbody>${{matchupHeroes.map((rowHero) => `<tr>
          <th>${{esc(shortHero(rowHero.id))}}</th>
          ${{matchupHeroes.map((colHero) => {{
            const group = ((matrix[rowHero.id] || {{}})[colHero.id] || {{}});
            return matrixStatCell(group.total) + matrixStatCell(group.first) + matrixStatCell(group.second);
          }}).join("")}}
        </tr>`).join("")}}</tbody>
      </table></div>`;
      const matchupCols = [
        {{label: "Hero", sortKey: "hero", render: (r) => esc(shortHero(r.hero))}},
        {{label: "Opponent", sortKey: "opponent_hero", render: (r) => esc(shortHero(r.opponent_hero))}},
        {{label: "Games", key: "games"}},
        {{label: "WR", sortKey: "wr", render: pctCell}}
      ];
      const fsRows = [];
      for (const [h, buckets] of Object.entries(analytics.first_second || {{}})) {{
        if (hero && h !== hero) continue;
        for (const bucket of ["first", "second", "unknown"]) {{
          const row = buckets[bucket];
          if (!row || !row.games) continue;
          fsRows.push({{hero: h, bucket, ...row}});
        }}
      }}
      const fsCols = [
        {{label: "Hero", sortKey: "hero", render: (r) => esc(shortHero(r.hero))}},
        {{label: "Seat", key: "bucket"}},
        {{label: "Games", key: "games"}},
        {{label: "WR", sortKey: "wr", render: pctCell}}
      ];
      qs("#resultCount").textContent = `${{matchups.length}} hero matchups shown. First/second uses Talishar firstPlayer.`;
      qs("#matchupsView").innerHTML = `
        <div class="band"><div class="section-title"><h2>Hero Matchup Matrix</h2><span class="muted">Total, first, and second from row hero perspective</span></div>${{matrixMarkup}}</div>
        <div class="band"><h2>Hero Matchups</h2>${{tableMarkup(matchupCols, matchups)}}</div>
        <div class="band"><h2>First Or Second</h2>${{tableMarkup(fsCols, fsRows)}}</div>
      `;
    }}

    function renderVsHero() {{
      const analytics = report.analytics || {{}};
      const source = analytics.vs_hero_cards || {{}};
      const heroes = selectedAnalyticsHeroes(source);
      qs("#resultCount").textContent = "Cards are grouped by your hero and opposing hero; ranking is inferred from those matchup games.";
      qs("#vsView").innerHTML = heroes.length ? heroes.map((hero) => {{
        const opponents = Object.keys(source[hero] || {{}}).sort();
        const blocks = opponents.map((opponent) => {{
          const rows = rankedRows(source[hero][opponent] || [], 15);
          if (!rows.length) return "";
          return `<div class="band">
            <div class="section-title"><h2>${{esc(shortHero(hero))}} vs ${{esc(shortHero(opponent))}}</h2><span class="muted">top cards in this matchup</span></div>
            ${{tableMarkup(compactCardRankColumns, rows)}}
          </div>`;
        }}).join("");
        return blocks;
      }}).join("") : `<div class="band empty">No matchup card data.</div>`;
    }}

    function renderCards() {{
      renderTable("#topCardsTable", [
        {{label: "Card", sortKey: "name", render: (r) => `<span class="card-chip" ${{cardAttrs(r)}}>${{esc(r.name)}}</span><div class="muted">${{esc(r.id)}}</div>`}},
        {{label: "Copies", key: "copies"}},
        {{label: "Played", key: "played"}},
        {{label: "Blocked", key: "blocked"}},
        {{label: "Pitched", key: "pitched"}}
      ], report.aggregates.top_cards);
      renderTable("#arenaTable", [
        {{label: "Card", sortKey: "name", render: (r) => `<span class="card-chip" ${{cardAttrs(r)}}>${{esc(r.name)}}</span><div class="muted">${{esc(r.id)}}</div>`}},
        {{label: "Appearances", key: "appearances"}}
      ], report.aggregates.top_arena);
    }}

    function renderFields() {{
      const p = report.profile;
      qs("#fields").innerHTML = `
        <details open><summary>CSV columns</summary><p><code>${{esc(report.available_fields.csv_columns.join(", "))}}</code></p></details>
        <details open><summary>Deck JSON fields</summary><p><code>${{esc(report.available_fields.deck_json_fields.join(", "))}}</code></p></details>
        <details open><summary>Card result fields</summary><p><code>${{esc(report.available_fields.card_result_fields.join(", "))}}</code></p></details>
        <details><summary>Raw rows by day</summary><pre>${{esc(JSON.stringify(p.raw_rows_by_day, null, 2))}}</pre></details>
        <details><summary>Hero seats</summary><pre>${{esc(JSON.stringify(p.deduped_hero_seats, null, 2))}}</pre></details>
      `;
    }}

    function renderCurrent() {{
      qs("#runsView").style.display = state.view === "runs" ? "grid" : "none";
      qs("#rankingsView").style.display = state.view === "rankings" ? "grid" : "none";
      qs("#colorsView").style.display = state.view === "colors" ? "grid" : "none";
      qs("#colorCountsView").style.display = state.view === "colorCounts" ? "grid" : "none";
      qs("#equipmentView").style.display = state.view === "equipment" ? "grid" : "none";
      qs("#matchupsView").style.display = state.view === "matchups" ? "grid" : "none";
      qs("#vsView").style.display = state.view === "vs" ? "grid" : "none";
      qs("#cardsView").style.display = state.view === "cards" ? "grid" : "none";
      qs("#fieldsView").style.display = state.view === "fields" ? "block" : "none";
      if (state.view === "runs") renderRuns();
      if (state.view === "rankings") renderRankings();
      if (state.view === "colors") renderColors();
      if (state.view === "colorCounts") renderColorCounts();
      if (state.view === "equipment") renderEquipment();
      if (state.view === "matchups") renderMatchups();
      if (state.view === "vs") renderVsHero();
      if (state.view === "cards") renderCards();
      if (state.view === "fields") renderFields();
    }}

    function installPopover() {{
      const pop = qs("#popover");
      document.body.addEventListener("mousemove", (event) => {{
        if (isTouchPreview()) return;
        const target = event.target.closest("[data-card]");
        if (!target) {{ pop.style.display = "none"; return; }}
        const image = target.dataset.image;
        pop.innerHTML = image ? `<img src="${{esc(image)}}" alt="${{esc(target.dataset.card)}}">` : `<div class="no-img">No preview available for<br>${{esc(target.dataset.card)}}</div>`;
        pop.style.display = "block";
        const left = Math.min(event.clientX + 18, window.innerWidth - 260);
        const top = Math.min(event.clientY + 18, window.innerHeight - 380);
        pop.style.left = Math.max(8, left) + "px";
        pop.style.top = Math.max(8, top) + "px";
      }});
      document.body.addEventListener("mouseleave", () => {{ pop.style.display = "none"; }});
    }}

    function isTouchPreview() {{
      return window.matchMedia("(hover: none)").matches || window.matchMedia("(pointer: coarse)").matches;
    }}

    function openCardPreview(target) {{
      const modal = qs("#cardPreviewModal");
      const title = qs("#cardPreviewTitle");
      const body = qs("#cardPreviewBody");
      const image = target.dataset.image || "";
      const card = target.dataset.card || "Card preview";
      title.textContent = card;
      body.innerHTML = image ? `<img src="${{esc(image)}}" alt="${{esc(card)}}">` : `<div class="no-img">No preview available for<br>${{esc(card)}}</div>`;
      modal.setAttribute("aria-hidden", "false");
    }}

    function closeCardPreview() {{
      qs("#cardPreviewModal").setAttribute("aria-hidden", "true");
    }}

    function installCardPreviewModal() {{
      const modal = qs("#cardPreviewModal");
      document.body.addEventListener("click", (event) => {{
        const target = event.target.closest("[data-card]");
        if (!target || !isTouchPreview()) return;
        event.preventDefault();
        openCardPreview(target);
      }});
      qs("#cardPreviewClose").addEventListener("click", closeCardPreview);
      modal.addEventListener("click", (event) => {{
        if (event.target === modal) closeCardPreview();
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") closeCardPreview();
      }});
    }}

    function installTableSorting() {{
      document.body.addEventListener("click", (event) => {{
        const button = event.target.closest("[data-sort-col]");
        if (!button) return;
        const table = button.closest("table");
        if (!table) return;
        sortTable(table, Number(button.dataset.sortCol));
      }});
    }}

    renderMetrics();
    initFilters();
    renderCurrent();
    installTableSorting();
    installCardPreviewModal();
    installPopover();
  </script>
</body>
</html>
"""


def printable_hero_name(hero: object) -> str:
    return display_hero_name(hero)


def render_full_card_rankings_html(
    payload: dict[str, Any],
    ranking_payload: dict[str, Any] | None = None,
    *,
    title: str = "Omens Draft Full Hero Card Rankings",
    subtitle: str = (
        "Same score model as Hero Card Rankings: smoothed winrate, sample size, average copies, and "
        "played-per-game. This is inferred from played decklists, not real draft pick order."
    ),
    count_label: str = "clean player-games",
    count_key: str = "games",
    first_count_label: str = "Games",
    include_matchup_columns: bool = True,
    include_strict_3_0_columns: bool = True,
) -> str:
    analytics = ranking_payload or (payload.get("analytics") or {})
    rankings = analytics.get("hero_card_rankings") or {}
    source = payload.get("source") or {}
    profile = payload.get("profile") or {}
    summary = analytics.get("summary") or {}
    rows: list[str] = []
    for hero in sorted(rankings):
        hero_rows = list(rankings.get(hero) or [])
        table_rows = []
        for index, row in enumerate(hero_rows, start=1):
            strict_cells = ""
            if include_strict_3_0_columns:
                strict_cells = (
                    f"<td>{html.escape(str(row.get('strict_3_0_count') or 0))}</td>"
                    f"<td>{html.escape(str(row.get('strict_3_0_pct') or 0))}%</td>"
                )
            matchup_cells = ""
            if include_matchup_columns:
                matchup_cells = "".join(
                    f"<td>{html.escape(str((row.get('matchup_wr') or {}).get(opponent, {}).get('wr') or 0))}%"
                    f"<div class=\"muted\">{html.escape(str((row.get('matchup_wr') or {}).get(opponent, {}).get('wins') or 0))}/"
                    f"{html.escape(str((row.get('matchup_wr') or {}).get(opponent, {}).get('games') or 0))}</div></td>"
                    for opponent, _label in MATCHUP_HERO_COLUMNS
                )
            table_rows.append(
                "<tr>"
                f"<td>{index}</td>"
                f"<td>{html.escape(str(row.get('name') or row.get('id') or ''))}<div class=\"muted\">{html.escape(str(row.get('id') or ''))}</div></td>"
                f"<td>{html.escape(str(row.get('color') or ''))}</td>"
                f"<td>{html.escape(str(row.get('games') or 0))}</td>"
                f"<td>{html.escape(str(row.get('wins') or 0))}</td>"
                f"<td>{html.escape(str(row.get('wr') or 0))}%</td>"
                f"{strict_cells}"
                f"<td>{html.escape(str(row.get('score') or 0))}</td>"
                f"<td>{html.escape(str(row.get('avg_copies') or 0))}</td>"
                f"<td>{html.escape(str(row.get('played_pg') or 0))}</td>"
                f"<td>{html.escape(str(row.get('blocked_pg') or 0))}</td>"
                f"<td>{html.escape(str(row.get('pitched_pg') or 0))}</td>"
                f"<td>{html.escape(str(row.get('hits_pg') or 0))}</td>"
                f"{matchup_cells}"
                "</tr>"
            )
        matchup_headers = ""
        if include_matchup_columns:
            matchup_headers = "".join(f"<th>{html.escape(label)}</th>" for _opponent, label in MATCHUP_HERO_COLUMNS)
        strict_headers = "<th>3-0 Lists</th><th>3-0 %</th>" if include_strict_3_0_columns else ""
        rows.append(
            "<section class=\"hero-section\">"
            f"<h2>{html.escape(printable_hero_name(hero))}</h2>"
            f"<p class=\"muted\">{len(hero_rows)} cards ranked by score.</p>"
            "<table>"
            "<thead><tr>"
            f"<th>#</th><th>Card</th><th>Color</th><th>{html.escape(first_count_label)}</th><th>Wins</th><th>WR</th>"
            f"{strict_headers}"
            "<th>Score</th><th>Avg copies</th><th>Played/G</th><th>Blocked/G</th><th>Pitched/G</th><th>Hits/G</th>"
            f"{matchup_headers}"
            "</tr></thead>"
            f"<tbody>{''.join(table_rows)}</tbody>"
            "</table>"
            "</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    @page {{ size: A4 landscape; margin: 12mm; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #18202a;
      background: #fff;
      letter-spacing: 0;
    }}
    header {{
      border-bottom: 3px solid #0f766e;
      padding-bottom: 10px;
      margin-bottom: 14px;
    }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    h2 {{ margin: 0 0 4px; font-size: 18px; }}
    p {{ margin: 0 0 6px; line-height: 1.35; }}
    .muted {{ color: #65717f; font-size: 11px; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(5, auto);
      gap: 10px;
      justify-content: start;
      font-size: 12px;
      margin-top: 8px;
    }}
    .summary span {{ border: 1px solid #d9dee7; border-radius: 5px; padding: 5px 7px; }}
    .hero-section {{ page-break-before: always; }}
    .hero-section:first-of-type {{ page-break-before: auto; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 9px; }}
    th, td {{ border-bottom: 1px solid #d9dee7; padding: 3px 4px; text-align: left; vertical-align: top; }}
    th {{ background: #f0f3f7; color: #3d4855; font-size: 9px; }}
    td:first-child, th:first-child {{ width: 28px; text-align: right; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p class="muted">{html.escape(subtitle)}</p>
    <div class="summary">
      <span>Format {html.escape(str(source.get("format") or ""))}</span>
      <span>{html.escape(str(source.get("start_date") or ""))} to {html.escape(str(source.get("end_date") or ""))}</span>
      <span>{html.escape(str(summary.get(count_key) or 0))} {html.escape(count_label)}</span>
      <span>{html.escape(str(profile.get("analyzed_players") or 0))} players</span>
      <span>Generated {html.escape(str(payload.get("generated_at") or ""))}</span>
    </div>
  </header>
  {''.join(rows)}
</body>
</html>
"""


def write_outputs(out_dir: Path, payload: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "omen_draft_3_0_report.json"
    html_path = out_dir / "omen_draft_3_0_report.html"
    full_rankings_html_path = out_dir / "omen_draft_full_hero_card_rankings.html"
    strict_rankings_html_path = out_dir / "omen_draft_strict_3_0_hero_card_rankings.html"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    full_rankings_html_path.write_text(render_full_card_rankings_html(payload), encoding="utf-8")
    strict_rankings_html_path.write_text(
        render_full_card_rankings_html(
            payload,
            payload.get("strict_3_0_card_rankings") or {},
            title="Omens Draft Strict 3-0 Hero Card Rankings",
            subtitle=(
                "Only clean strict 3-0 decklists are included. The first count column is the number of strict "
                "3-0 lists containing the card; WR is not a general winrate in this filtered report."
            ),
            count_label="strict 3-0 lists",
            count_key="strict_3_0_lists",
            first_count_label="3-0 Lists",
            include_matchup_columns=False,
            include_strict_3_0_columns=False,
        ),
        encoding="utf-8",
    )
    return html_path, json_path, full_rankings_html_path, strict_rankings_html_path


def main() -> None:
    args = parse_args()
    start = parse_date(args.start_date)
    end = parse_date(args.end_date)
    if end < start:
        raise SystemExit("--end-date cannot be earlier than --start-date")

    root = Path(__file__).resolve().parents[1]
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    custom_cards_file = Path(args.custom_cards_file).expanduser() if args.custom_cards_file else None

    api_key = resolve_api_key(
        api_key_file=args.api_key_file,
        default_api_key_file=root / ".local" / "fab_insights_api_key.txt",
        env_file=root / ".env",
    )

    raw_rows_by_day: dict[str, int] = {}
    raw_games: list[SeatGame] = []
    for day in daterange(start, end):
        meta, rows = fetch_day(api_key, day)
        raw_rows_by_day[day.isoformat()] = len(rows)
        raw_games.extend(seat_games_from_rows(day, rows))
        print(f"{day.isoformat()} rows={len(rows)} blob={meta.get('blob_name', '-')}")

    deduped_games = dedupe_player_games(raw_games)
    analysis_games = (
        deduped_games
        if args.include_non_omen
        else [game for game in deduped_games if game.hero in OMENS_HEROES and game.opponent_hero in OMENS_HEROES]
    )
    sessions = split_sessions(analysis_games, args.max_gap_minutes)
    candidates = find_3_0_candidates(sessions, args.max_gap_minutes)
    aggregates = aggregate_report_cards(candidates)
    analytics = build_performance_analytics(analysis_games)
    strict_3_0_card_rankings = build_strict_3_0_card_rankings(candidates)
    add_strict_3_0_presence_to_analytics(analytics, strict_3_0_card_rankings)
    profile = dataset_profile(raw_rows_by_day, raw_games, deduped_games, analysis_games, sessions, candidates)

    out_dir.mkdir(parents=True, exist_ok=True)
    images = resolve_images(
        out_dir,
        candidates,
        aggregates,
        analytics,
        limit=args.image_limit,
        skip=args.skip_images,
        custom_cards_file=custom_cards_file,
    )

    payload = {
        "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": {
            "api_url": API_URL,
            "format": FORMAT_CODE,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        "method": (
            f"Group format 7 player-games by player hash, sort by created_at, split sessions on hero change "
            f"or gaps over {args.max_gap_minutes} minutes, dedupe repeated player+game_id snapshots, then keep "
            "the first consecutive W-W-W window in each session. Strict runs additionally require a 30-card "
            "decklist, three unique opponents, no concessions, and all games with turns > 0. "
            + (
                "This run includes all format 7 heroes."
                if args.include_non_omen
                else "This run limits 3-0 analysis to Omens heroes: Aurora, Oscilio, and Zyggy."
            )
        ),
        "available_fields": {
            "csv_columns": [
                "game_id",
                "game_name",
                "game_guid",
                "format",
                "deck1_id",
                "deck2_id",
                "deck1_json",
                "deck2_json",
                "created_at",
                "player1_name",
                "player2_name",
                "count_winner_deck",
                "count_loser_deck",
                "is_public",
                "conceded",
            ],
            "deck_json_fields": [
                "playerHero",
                "opposingHero",
                "result",
                "winner",
                "firstPlayer",
                "turns",
                "totalTime",
                "yourTime",
                "totalDamageDealt",
                "totalDamageBlocked",
                "totalDamagePrevented",
                "totalDamageThreatened",
                "averageValuePerTurn",
                "averageCombatValuePerTurn",
                "averageDamageDealtPerTurn",
                "averageDamageThreatenedPerTurn",
                "averageCardsLeftOverPerTurn",
                "averageResourcesUsedPerTurn",
                "cardResults",
                "arenaCardResults",
                "tokenResults",
                "turnResults",
            ],
            "card_result_fields": [
                "cardId",
                "cardName",
                "numCopies",
                "pitchValue",
                "played",
                "hits",
                "blocked",
                "pitched",
                "discarded",
                "charged",
            ],
        },
        "hero_display_names": HERO_DISPLAY_NAMES,
        "profile": profile,
        "aggregates": attach_images_to_cards(aggregates, images),
        "analytics": attach_images_to_cards(analytics, images),
        "strict_3_0_card_rankings": strict_3_0_card_rankings,
        "candidates": attach_images_to_cards(candidates, images),
        "image_summary": {
            "resolved": sum(1 for row in images.values() if row.get("image_url")),
            "attempted_or_cached": len(images),
            "cache_file": str(image_cache_path(out_dir)),
            "provider": GOAGAIN_CARDS_URL,
            "custom_cards_file": str(custom_cards_file) if custom_cards_file else "",
        },
    }
    payload = sanitize_public_payload(payload)

    html_path, json_path, full_rankings_html_path, strict_rankings_html_path = write_outputs(out_dir, payload)
    print(f"html={html_path}")
    print(f"json={json_path}")
    print(f"full_rankings_html={full_rankings_html_path}")
    print(f"strict_3_0_rankings_html={strict_rankings_html_path}")
    print(f"candidates={len(candidates)} strict={profile['classification_counts'].get('strict', 0)}")


if __name__ == "__main__":
    main()
