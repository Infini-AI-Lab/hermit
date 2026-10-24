"""Score a Continual Harness (Pokemon Red) window by how far along the game the policy got.

The benchmark's milestones are an ordered path through the game (bedroom -> Pallet Town -> Oak's Lab -> ... ->
Champion). The score is the furthest milestone reached along that path, scaled to [0, 1]. GAME_RUNNING (always
true at boot) counts as 0 — it is not progress.

A short window often ends before the next milestone fires, so a pure milestone score would be flat and give the
Evolver no gradient. We add a small, bounded exploration term — distinct map tiles visited this window — that
lives strictly between the current milestone and the next. So "wandered out of the house and explored" scores
above "mashed A in one spot," and "reached a new milestone" always scores above any amount of exploration.
"""

# The ordered Red milestones (pokemon_red_env/red_emulator.py: RED_MILESTONES_ORDER). GAME_RUNNING=0, CHAMPION=last.
RED_MILESTONES = [
    "GAME_RUNNING", "PALLET_TOWN", "OAKS_LAB", "STARTER_OBTAINED", "ROUTE_1", "VIRIDIAN_CITY", "ROUTE_2",
    "VIRIDIAN_FOREST", "PEWTER_CITY", "PEWTER_GYM", "BOULDER_BADGE", "ROUTE_3", "MT_MOON", "CERULEAN_CITY",
    "CERULEAN_GYM", "CASCADE_BADGE", "ROUTE_5", "VERMILION_CITY", "SS_ANNE", "VERMILION_GYM", "THUNDER_BADGE",
    "ROUTE_9", "ROCK_TUNNEL", "LAVENDER_TOWN", "CELADON_CITY", "CELADON_GYM", "RAINBOW_BADGE", "ROCKET_HIDEOUT",
    "POKEMON_TOWER", "FUCHSIA_CITY", "FUCHSIA_GYM", "SOUL_BADGE", "SAFFRON_CITY", "SILPH_CO", "SAFFRON_GYM",
    "MARSH_BADGE", "CINNABAR_ISLAND", "CINNABAR_GYM", "VOLCANO_BADGE", "VIRIDIAN_GYM", "EARTH_BADGE", "ROUTE_23",
    "VICTORY_ROAD", "INDIGO_PLATEAU", "ELITE_FOUR_LORELEI", "ELITE_FOUR_BRUNO", "ELITE_FOUR_AGATHA",
    "ELITE_FOUR_LANCE", "CHAMPION",
]
_INDEX = {m: i for i, m in enumerate(RED_MILESTONES)}
_DENOM = len(RED_MILESTONES) - 1          # so CHAMPION -> 1.0
EXPLORE_CAP = 20                          # distinct tiles that count as (almost) full progress to the next milestone


def score(task, output):
    extra = getattr(output, "extra", None) or {}
    completed = extra.get("completed") or []
    tiles = int(extra.get("unique_tiles") or 0)
    moved = int(extra.get("moved_steps") or 0)
    menu = int(extra.get("menu_presses") or 0)

    reached = [m for m in completed if m in _INDEX]
    furthest = max((_INDEX[m] for m in reached), default=0)
    latest_name = max(reached, key=lambda m: _INDEX[m], default="none")
    # exploration sub-progress, bounded just below the next milestone (0 once the game is finished)
    sub = min(tiles / EXPLORE_CAP, 1.0) * 0.999 if furthest < _DENOM else 0.0
    value = (furthest + sub) / _DENOM

    note = ""
    if furthest == 0 and tiles <= 2:
        note = " The agent barely moved — likely stuck pressing into walls or trapped in the START menu."
    elif menu and furthest == 0:
        note = f" It pressed START/SELECT {menu}x and made no milestone progress — the menu does not advance the game."
    fb = (f"Furthest milestone: {latest_name} ({furthest}/{_DENOM}). Explored {tiles} distinct tiles; "
          f"moved on {moved} steps.{note}")
    return {"score": round(value, 4), "feedback": fb}
