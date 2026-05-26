from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "omen_draft_report.py"
SPEC = importlib.util.spec_from_file_location("omen_draft_report", SCRIPT)
assert SPEC is not None
omen_draft_report = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = omen_draft_report
SPEC.loader.exec_module(omen_draft_report)


class OmenDraftReportTests(unittest.TestCase):
    def seat(
        self,
        *,
        hero: str,
        opp: str,
        win: bool,
        first: int = 1,
        arena: list[dict[str, str]] | None = None,
    ):
        return omen_draft_report.SeatGame(
            player=f"p-{hero}-{opp}-{first}-{win}",
            opponent=f"o-{opp}",
            game_id=f"g-{hero}-{opp}-{first}-{win}",
            game_guid="",
            created_at="2026-05-25 12:00:00",
            created_dt=datetime(2026, 5, 25, 12, 0, 0),
            hero=hero,
            opponent_hero=opp,
            win=win,
            turns=5,
            conceded=False,
            first_player=first,
            cards=(),
            arena=tuple(arena or []),
            damage_dealt=None,
            damage_blocked=None,
            damage_threatened=None,
            average_value_per_turn=None,
        )

    def test_equipment_analytics_group_by_hero_count_card_and_combo(self) -> None:
        games = [
            self.seat(
                hero="zyggy",
                opp="aurora_emissary_of_lightning",
                win=True,
                arena=[
                    {"id": "cosmo_scroll", "name": "Cosmo, Scroll of Ancestral Tapestry"},
                    {"id": "threadbare_tunic", "name": "Threadbare Tunic"},
                ],
            ),
            self.seat(
                hero="zyggy",
                opp="aurora_emissary_of_lightning",
                win=False,
                arena=[{"id": "cosmo_scroll", "name": "Cosmo, Scroll of Ancestral Tapestry"}],
            ),
            self.seat(
                hero="aurora_emissary_of_lightning",
                opp="zyggy",
                win=True,
                arena=[
                    {"id": "arcane_seeds", "name": "Arcane Seeds"},
                    {"id": "cosmo_scroll", "name": "Cosmo, Scroll of Ancestral Tapestry"},
                    {"id": "threadbare_tunic", "name": "Threadbare Tunic"},
                    {"id": "rune_gate", "name": "Rune Gate"},
                    {"id": "extra_slot", "name": "Extra Slot"},
                ],
            ),
        ]

        analytics = omen_draft_report.build_performance_analytics(games)

        zyggy_counts = analytics["hero_equipment_count_wr"]["zyggy"]
        self.assertEqual(zyggy_counts[1], {"count": 1, "label": "1", "games": 1, "wins": 0, "wr": 0.0})
        self.assertEqual(zyggy_counts[2], {"count": 2, "label": "2", "games": 1, "wins": 1, "wr": 100.0})
        self.assertEqual(analytics["hero_equipment_count_wr"]["aurora_emissary_of_lightning"][4]["label"], "4+")
        self.assertEqual(
            analytics["hero_equipment_cards"]["zyggy"][0]["name"],
            "Cosmo, Scroll of Ancestral Tapestry",
        )
        self.assertEqual(analytics["hero_equipment_cards"]["zyggy"][0]["games"], 2)
        self.assertEqual(analytics["hero_equipment_cards"]["zyggy"][0]["wr"], 50.0)
        self.assertEqual(analytics["hero_equipment_combos"]["zyggy"][0]["games"], 1)
        self.assertIn("Threadbare Tunic", analytics["hero_equipment_combos"]["zyggy"][0]["equipment"])

    def test_report_html_has_equipment_tab(self) -> None:
        html = omen_draft_report.render_html(
            {
                "profile": {
                    "candidate_3_0_windows": 0,
                    "classification_counts": {},
                    "analyzed_player_games": 0,
                    "analyzed_players": 0,
                    "sessions": 0,
                    "card_result_rows": 0,
                    "raw_rows_by_day": {},
                    "deduped_hero_seats": {},
                },
                "method": "test",
                "candidates": [],
                "analytics": {
                    "summary": {"games": 0, "card_games": 0, "equipment_games": 0},
                    "hero_card_rankings": {},
                    "hero_equipment_count_wr": {},
                    "hero_equipment_cards": {},
                    "hero_equipment_combos": {},
                },
                "aggregates": {"top_cards": [], "top_arena": []},
                "available_fields": {"csv_columns": [], "deck_json_fields": [], "card_result_fields": []},
                "hero_display_names": {},
            }
        )

        self.assertIn('data-view="equipment"', html)
        self.assertIn("function renderEquipment", html)
        self.assertIn("Equipment Count WR", html)


if __name__ == "__main__":
    unittest.main()
