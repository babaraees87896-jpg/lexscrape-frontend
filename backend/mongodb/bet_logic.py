"""Betting + P/L logic — scraped 1ex99.in local JS se.

Source: output/assets/MatchDetail-DcLvOyoM.js
  Fr=async()     — bet submit routing (3 APIs)
  Io component   — bookmaker/toss position preview
  Kt=async()     — fancy P/L settle
  To component   — session/khado/meter/oddeven clicks
  Pe component   — match/tie/complete odds clicks

Markets (scraped → local API):
  bookmaker     oddBetPlaced       betFor=odds, oddsType=bookmaker
  toss          oddBetPlaced       betFor=toss, oddsType=toss
  matchOdds     oddBetPlaced       betFor=matchOdds + betfairMarketId
  tiedMatch     oddBetPlaced       betFor=tiedMatch (exchange P/L)
  completedMatch oddBetPlaced      betFor=completedMatch
  fancy/session sessionBetPlaced   betFor=fancy, gtype=fancy
  khado/meter/  meterKhadoOddEvenCricketCassinoBetPlace
  oddeven/cricketcasino
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

# betType: L=Back/Lagai, K=Lay/Khai, Y=Yes(session), N=No(session)

METER_KHADO_GTYPES = frozenset({"oddeven", "khado", "meter", "cricketcasino"})
EXCHANGE_BET_FOR = frozenset({"matchOdds", "tiedMatch", "completedMatch"})
BOOKMAKER_BET_FOR = frozenset({"odds", "bookmaker"})


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fancy_rate(odds: float) -> float:
    """Session rates arrive as either 0.90/1.10 or 90/110."""
    return round(odds / 100, 4) if odds > 3 else odds


def parse_team_selections(match: Optional[dict]) -> list[dict]:
    if not match:
        return []
    raw = match.get("teamData")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return list(raw.values())
    return []


def normalize_team_entry(team: dict) -> dict:
    item = dict(team)
    if not item.get("runnerName") and item.get("runner_name"):
        item["runnerName"] = item["runner_name"]
    if item.get("selectionId") is None and item.get("selection_id") is not None:
        item["selectionId"] = item["selection_id"]
    if item.get("selectionid") is None:
        item["selectionid"] = (
            item.get("selection_id")
            or item.get("selectionId")
            or item.get("bookmakerSelectionId")
        )
    if not item.get("marketId") and item.get("market_id"):
        item["marketId"] = item["market_id"]
    return item


def normalize_match_for_api(match: dict) -> dict:
    """Frontend expects teamData as array with runnerName (not JSON string)."""
    row = copy.deepcopy(match)
    row["teamData"] = [normalize_team_entry(t) for t in parse_team_selections(row)]
    return row


def _team_sid(team: dict) -> Any:
    return team.get("selection_id") or team.get("selectionId") or team.get("selectionid")


def _bookmaker_sid(team: dict) -> Any:
    """Cache team_data selectionid — bookmakerSelectionId (1/2), betfair id nahi."""
    bm = team.get("bookmakerSelectionId")
    if bm is not None:
        return bm
    sid = team.get("selection_id") or team.get("selectionId") or team.get("selectionid")
    if sid is not None:
        return sid
    return team.get("betfairSelectionId")


def _find_team_by_selection(teams: list[dict], selection_id: Any) -> Optional[dict]:
    sel_str = str(selection_id)
    for team in teams:
        for key in (
            "bookmakerSelectionId",
            "betfairSelectionId",
            "selectionid",
            "selectionId",
            "selection_id",
        ):
            val = team.get(key)
            if val is not None and str(val) == sel_str:
                return team
    return None


_TOSS_UI_ID_BY_BM = {1: "10000", 2: "20000"}


def toss_canonical_selection_id(match: Optional[dict], selection_id: Any) -> str:
    """Client toss UI uses 10000/20000 — declare/staff often sends 1/2."""
    sel = str(selection_id or "").strip()
    if sel in _TOSS_UI_ID_BY_BM.values():
        return sel
    teams = parse_team_selections(match)
    team = _find_team_by_selection(teams, selection_id)
    if team:
        bm = team.get("bookmakerSelectionId")
        try:
            bm_i = int(bm)
        except (TypeError, ValueError):
            bm_i = 0
        if bm_i in _TOSS_UI_ID_BY_BM:
            return _TOSS_UI_ID_BY_BM[bm_i]
    if sel in ("1", "2"):
        return _TOSS_UI_ID_BY_BM.get(int(sel), sel)
    return sel


def toss_selections_match(match: Optional[dict], selection_id: Any, won_selection_id: Any) -> bool:
    return toss_canonical_selection_id(match, selection_id) == toss_canonical_selection_id(match, won_selection_id)


_TOSS_UI_TO_BM = {"10000": 1, "20000": 2}


def toss_bookmaker_position_id(match: Optional[dict], selection_id: Any) -> Any:
    """Toss P/L keys are bookmaker 1/2 — map client UI ids (10000/20000) before delta calc."""
    canon = toss_canonical_selection_id(match, selection_id)
    if canon in _TOSS_UI_TO_BM:
        return _TOSS_UI_TO_BM[canon]
    resolved = resolve_client_selection_id(match, selection_id)
    if str(resolved) in ("1", "2"):
        return int(resolved)
    return resolved


def resolve_client_selection_id(match: Optional[dict], selection_id: Any) -> Any:
    """Bet payload selectionId → cache team_data selectionid key."""
    teams = parse_team_selections(match)
    team = _find_team_by_selection(teams, selection_id)
    if team:
        return _bookmaker_sid(team)
    return selection_id


_JUNK_TEAM_NAMES = frozenset({
    "", "odds", "bookmaker", "toss", "fancy", "matchodds", "session", "normal",
})


def team_label_from_row(team: dict) -> str:
    for key in ("team_name", "runner_name", "runnerName", "selectionName", "name"):
        val = str(team.get(key) or "").strip()
        if val.lower() not in _JUNK_TEAM_NAMES:
            return val
    return ""


def resolve_team_name(
    match: Optional[dict],
    selection_id: Any,
    *,
    payload_name: str = "",
    cache_teams: Optional[list[dict]] = None,
) -> str:
    """Bookmaker bet list — WEST INDIES jaisa team name (scraped site jaisa)."""
    name = str(payload_name or "").strip()
    if name.lower() not in _JUNK_TEAM_NAMES:
        return name
    for source in (parse_team_selections(match), cache_teams or []):
        if not source:
            continue
        team = _find_team_by_selection(source, selection_id)
        if team:
            label = team_label_from_row(team)
            if label:
                return label
    return ""


def admin_display_id_map(match: Optional[dict]) -> dict[str, str]:
    """Admin match-position UI bookmaker keys (1/2) — saari IDs yahan map."""
    teams = parse_team_selections(match)
    id_map: dict[str, str] = {}
    for team in teams:
        display = team.get("bookmakerSelectionId") or team.get("selection_id") or team.get("selectionId")
        if display is None:
            continue
        disp = str(display)
        for key in ("betfairSelectionId", "selectionid", "selectionId", "selection_id", "bookmakerSelectionId"):
            val = team.get(key)
            if val is not None:
                id_map[str(val)] = disp
    return id_map


def remap_to_admin_display_ids(match: Optional[dict], runners: dict) -> dict[str, float]:
    id_map = admin_display_id_map(match)
    if not id_map:
        return {str(k): _num(v) for k, v in (runners or {}).items()}
    remapped: dict[str, float] = {}
    for key, val in (runners or {}).items():
        disp = id_map.get(str(key), str(key))
        remapped[disp] = round(remapped.get(disp, 0) + _num(val), 2)
    return remapped


def remap_bookmaker_runners(match: Optional[dict], runners: dict) -> dict[str, float]:
    """Old positions (keys 1/2) ko live selectionid par map karo."""
    if not match or not runners:
        return {str(k): _num(v) for k, v in (runners or {}).items()}
    teams = parse_team_selections(match)
    if not teams:
        return {str(k): _num(v) for k, v in (runners or {}).items()}

    id_map: dict[str, str] = {}
    for team in teams:
        display = _bookmaker_sid(team)
        if display is None:
            continue
        display_key = str(display)
        for key in (
            "bookmakerSelectionId",
            "betfairSelectionId",
            "selectionid",
            "selectionId",
            "selection_id",
        ):
            val = team.get(key)
            if val is not None:
                id_map[str(val)] = display_key

    if not id_map:
        return {str(k): _num(v) for k, v in (runners or {}).items()}

    remapped: dict[str, float] = {}
    for key, val in runners.items():
        new_key = id_map.get(str(key), str(key))
        remapped[new_key] = round(remapped.get(new_key, 0) + _num(val), 2)
    return remapped


def bookmaker_selection_ids(match: Optional[dict], selection_id: Any) -> list[Any]:
    """
    Bookmaker/toss position sirf 2 runners par — JS arrayData.length < 3.
    IPL jaisi matches mein teamData mein 10 teams ho sakti hain; sirf active pair use karo.
    """
    teams = parse_team_selections(match)
    if not teams:
        sel = str(selection_id) if selection_id is not None else ""
        if sel in ("1", "2"):
            return [1, 2]
        if sel in ("10000", "20000"):
            return [10000, 20000]
        return [selection_id] if selection_id is not None else []

    sid_fn = _bookmaker_sid

    if len(teams) == 2:
        return [sid_fn(t) for t in teams if sid_fn(t) is not None]

    bm_pair = [t for t in teams if t.get("bookmakerSelectionId") in (1, 2)]
    if len(bm_pair) >= 2:
        bm_pair.sort(key=lambda t: t.get("bookmakerSelectionId", 0))
        return [sid_fn(t) for t in bm_pair[:2]]

    selected = _find_team_by_selection(teams, selection_id)
    if selected and selected.get("bookmakerSelectionId") in (1, 2):
        opp_id = 2 if selected["bookmakerSelectionId"] == 1 else 1
        opponent = next((t for t in teams if t.get("bookmakerSelectionId") == opp_id), None)
        if opponent:
            return [sid_fn(selected), sid_fn(opponent)]

    sids = [sid_fn(t) for t in teams if sid_fn(t) is not None]
    if selection_id is not None and len(sids) > 2:
        sel_str = str(selection_id)
        others = [s for s in sids if str(s) != sel_str]
        return [selection_id, others[0]] if others else [selection_id]
    return sids[:2] if len(sids) >= 2 else sids


def match_odds_selection_ids(match: Optional[dict], selection_id: Any) -> list[Any]:
    """Match odds — betfair selectionIds (2 runners)."""
    teams = parse_team_selections(match)
    if len(teams) == 2:
        return [_team_sid(t) for t in teams if _team_sid(t) is not None]
    sids = [_team_sid(t) for t in teams if _team_sid(t) is not None]
    if len(sids) > 2 and selection_id is not None:
        sel_str = str(selection_id)
        others = [s for s in sids if str(s) != sel_str]
        return [selection_id, others[0]] if others else [selection_id]
    return sids[:2] if len(sids) >= 2 else sids


def selection_ids_from_match(
    match: Optional[dict],
    fallback_selection_id: Any,
    odds_type: str = "",
    bet_for: str = "",
) -> list[Any]:
    ot = normalize_odds_type(bet_for, odds_type or "")
    if ot in ("bookmaker", "toss"):
        return bookmaker_selection_ids(match, fallback_selection_id)
    if ot == "match":
        return match_odds_selection_ids(match, fallback_selection_id)
    teams = parse_team_selections(match)
    ids = [_team_sid(t) for t in teams if _team_sid(t) is not None]
    if not ids and fallback_selection_id is not None:
        ids = [fallback_selection_id]
    return ids


def normalize_odds_type(bet_for: str, odds_type: str) -> str:
    """MatchDetail Fr=async() — betFor + oddsType se internal type."""
    bf = (bet_for or "").strip()
    ot = (odds_type or bf or "match").strip()
    if bf in EXCHANGE_BET_FOR or ot in EXCHANGE_BET_FOR or ot.lower() in ("match", "matchodds"):
        return "match"
    if bf == "toss" or ot == "toss":
        return "toss"
    if bf == "fancy" or ot in ("fancy", "session"):
        return "fancy"
    if bf in BOOKMAKER_BET_FOR or ot in ("bookmaker", "odds"):
        return "bookmaker"
    return ot.lower()


def is_fancy_market(bet_for: str, odds_type: str, gtype: str = "") -> bool:
    return normalize_odds_type(bet_for, odds_type) == "fancy"


def is_meter_khado_gtype(gtype: str) -> bool:
    return (gtype or "").lower() in METER_KHADO_GTYPES


def is_exchange_market(bet_for: str, odds_type: str) -> bool:
    return normalize_odds_type(bet_for, odds_type) == "match"


def is_bookmaker_market(bet_for: str, odds_type: str) -> bool:
    ot = normalize_odds_type(bet_for, odds_type)
    return ot in ("bookmaker", "toss")


def calc_liability(amount: float, odds: float, bet_type: str, odds_type: str, gtype: str = "", bet_for: str = "") -> float:
    """
    Balance lock — MatchDetail JS.
    Fancy gtype=fancy: odds decimal (0.95). Liability N = amount*odds.
    Bookmaker/toss: L=stake, K=stake*rate (decimal or /100 if >3).
    Exchange match/tie/complete: L=stake, K=stake*(odds-1).
    """
    bt = (bet_type or "L").upper()
    ot = normalize_odds_type(bet_for, odds_type)
    gt = (gtype or "").lower()

    if ot == "fancy":
        if bt == "N":
            return round(amount * _fancy_rate(odds), 2)
        return round(amount, 2)

    if ot in ("bookmaker", "toss"):
        if bt == "K":
            return round(amount * odds / 100, 2) if odds > 3 else round(amount * odds, 2)
        return round(amount, 2)

    if ot == "match":
        if bt in ("L", "B"):
            return round(amount, 2)
        if bt == "K":
            o = odds if odds > 1 else odds + 1
            return round(amount * max(o - 1, 0), 2)
        return round(amount, 2)

    if bt in ("L", "B", "Y"):
        return round(amount, 2)
    if bt == "K":
        o = odds if odds > 1 else odds + 1
        return round(amount * max(o - 1, 0), 2)
    if bt == "N":
        return round(amount * odds, 2) if odds <= 3 else round(amount * odds / 100, 2)
    return round(amount, 2)


def calc_bookmaker_position_delta(
    amount: float,
    odds: float,
    bet_type: str,
    selection_id: Any,
    all_selection_ids: list[Any],
) -> dict[str, float]:
    """
    PlaceBetModal / Io component JS:
    L selected → +amount*odds, others → -amount
    K selected → -amount*odds, others → +amount
    """
    bt = (bet_type or "L").upper()
    delta: dict[str, float] = {}
    for sid in all_selection_ids:
        key = str(sid)
        if str(sid) == str(selection_id):
            delta[key] = round(amount * odds if bt == "L" else -amount * odds, 2)
        else:
            delta[key] = round(-amount if bt == "L" else amount, 2)
    return delta


def calc_match_odds_position_delta(
    amount: float,
    odds: float,
    bet_type: str,
    selection_id: Any,
    all_selection_ids: list[Any],
) -> dict[str, float]:
    bt = (bet_type or "L").upper()
    o = odds if odds > 1 else odds + 1
    delta: dict[str, float] = {}
    for sid in all_selection_ids:
        key = str(sid)
        if str(sid) == str(selection_id):
            if bt in ("L", "B"):
                delta[key] = round(amount * (o - 1), 2)
            else:
                delta[key] = round(-amount * (o - 1), 2)
        else:
            delta[key] = round(-amount if bt in ("L", "B") else amount, 2)
    return delta


def calc_position_info(
    amount: float,
    odds: float,
    bet_type: str,
    odds_type: str,
    selection_id: Any,
    match: Optional[dict] = None,
    bet_for: str = "",
) -> dict[str, float]:
    ot = normalize_odds_type(bet_for, odds_type)

    if ot in ("bookmaker", "toss"):
        if ot == "toss":
            resolved_sel = toss_bookmaker_position_id(match, selection_id)
        else:
            resolved_sel = resolve_client_selection_id(match, selection_id)
        sids = bookmaker_selection_ids(match, resolved_sel)
        return calc_bookmaker_position_delta(amount, odds, bet_type, resolved_sel, sids)

    if ot == "match":
        sids = match_odds_selection_ids(match, selection_id)
        if len(sids) >= 2:
            return calc_match_odds_position_delta(amount, odds, bet_type, selection_id, sids)

    return {}


def position_info_for_client_bet(bet: dict, match: Optional[dict] = None) -> dict[str, float]:
    """Client P/L — cache selectionid (1/2) keys; stored bet se recompute."""
    bet_for = str(bet.get("betFor") or "")
    odds_type = str(bet.get("oddsType") or bet_for or "")
    if is_fancy_market(bet_for, odds_type, str(bet.get("gtype") or "")):
        return {str(k): _num(v) for k, v in (bet.get("positionInfo") or {}).items()}

    ot = normalize_odds_type(bet_for, odds_type)
    if ot in ("bookmaker", "toss"):
        pi = calc_position_info(
            _num(bet.get("stake")),
            _num(bet.get("odds")),
            str(bet.get("betType") or "L"),
            odds_type,
            bet.get("selectionId"),
            match,
            bet_for=bet_for,
        )
        if pi:
            return remap_bookmaker_runners(match, pi)
        stored = bet.get("positionInfo") or {}
        return remap_bookmaker_runners(match, stored) if stored else {}

    stored = bet.get("positionInfo") or {}
    return {str(k): _num(v) for k, v in stored.items()}


def merge_positions(existing: dict, delta: dict) -> dict[str, float]:
    merged = {str(k): _num(v) for k, v in (existing or {}).items()}
    for k, v in delta.items():
        merged[str(k)] = round(merged.get(str(k), 0) + _num(v), 2)
    return merged


def total_exposure_from_positions(positions: dict[str, float]) -> float:
    """Worst-case loss per market = abs(min position)."""
    if not positions:
        return 0.0
    worst = min(_num(v) for v in positions.values())
    return round(abs(worst), 2) if worst < 0 else 0.0


def _is_threshold_fancy(bet: dict) -> bool:
    gt = str(bet.get("fancyType") or bet.get("gtype") or "").lower()
    return gt in ("", "fancy", "normal", "session")


def _fancy_group_key(bet: dict) -> tuple[str, str, str, str, str]:
    return (
        str(bet.get("eventId") or ""),
        str(bet.get("marketId") or ""),
        str(bet.get("selectionId") or ""),
        str(bet.get("runnerName") or ""),
        str(bet.get("fancyType") or bet.get("gtype") or "fancy").lower(),
    )


def _threshold_fancy_pl_at_run(bet: dict, decision_run: int) -> float:
    amount = _num(bet.get("stake") or bet.get("amount"))
    odds = _fancy_rate(_num(bet.get("odds")))
    run = int(_num(bet.get("run")))
    bt = (bet.get("betType") or bet.get("type") or "Y").upper()

    rate = _fancy_rate(odds)

    if decision_run >= run and bt == "Y":
        return round(amount * rate, 2)
    if decision_run >= run and bt == "N":
        return round(-amount * rate, 2)
    if decision_run < run and bt == "Y":
        return round(-amount, 2)
    if decision_run < run and bt == "N":
        return round(amount, 2)
    return 0.0


def _threshold_fancy_exposure(bets: list[dict]) -> float:
    runs = sorted({int(_num(b.get("run"))) for b in bets})
    if not runs:
        return 0.0
    decision_points = [runs[0] - 1] + runs
    worst_pl = min(
        round(sum(_threshold_fancy_pl_at_run(b, run) for b in bets), 2)
        for run in decision_points
    )
    return round(abs(worst_pl), 2) if worst_pl < 0 else 0.0


def total_fancy_exposure(open_fancy_bets: list[dict]) -> float:
    grouped: dict[tuple[str, str, str, str, str], list[dict]] = {}
    fallback_exposure = 0.0

    for bet in open_fancy_bets:
        if _is_threshold_fancy(bet):
            grouped.setdefault(_fancy_group_key(bet), []).append(bet)
        else:
            fallback_exposure += _num(bet.get("liability") or bet.get("stake"))

    hedged_exposure = sum(_threshold_fancy_exposure(bets) for bets in grouped.values())
    return round(fallback_exposure + hedged_exposure, 2)


def fancy_bet_exposure(open_fancy_bets: list[dict], pending_bet: Optional[dict] = None) -> float:
    bets = list(open_fancy_bets)
    if pending_bet:
        bets.append(pending_bet)
    return total_fancy_exposure(bets)


def calc_net_exposure(
    position_rows: list[dict],
    open_fancy_bets: list[dict],
) -> float:
    odds_exp = sum(total_exposure_from_positions(row.get("runners") or {}) for row in position_rows)
    return round(odds_exp + total_fancy_exposure(open_fancy_bets), 2)


def settle_fancy_bet(bet: dict, decision_run: int) -> float:
    """MatchDetail Kt() — fancy P/L jab result declare hota hai."""
    amount = _num(bet.get("stake") or bet.get("amount"))
    odds = _num(bet.get("odds"))
    run = int(_num(bet.get("run")))
    bt = (bet.get("betType") or bet.get("type") or "Y").upper()
    gtype = (bet.get("fancyType") or bet.get("gtype") or "").lower()

    if gtype == "cricketcasino":
        digit = abs(decision_run) % 10
        return round(amount * odds, 2) if run == digit else round(-amount, 2)

    if gtype == "oddeven":
        is_odd = decision_run % 2 != 0
        is_even = decision_run % 2 == 0
        if bt == "N":
            return round(amount * (odds - 1), 2) if is_odd else round(-amount, 2)
        return round(amount * (odds - 1), 2) if is_even else round(-amount, 2)

    rate = _fancy_rate(odds)

    if decision_run >= run and bt == "Y":
        return round(amount * rate, 2)
    if decision_run >= run and bt == "N":
        return round(-amount * rate, 2)
    if decision_run < run and bt == "Y":
        return round(-amount, 2)
    if decision_run < run and bt == "N":
        return round(amount, 2)
    return 0.0


def _selections_match(match: Optional[dict], selection_id: Any, won_selection_id: Any) -> bool:
    """Bookmaker/match odds — betfair vs bookmakerSelectionId (1/2) dono match karo."""
    sel = str(selection_id)
    won = str(won_selection_id)
    if sel == won:
        return True
    id_map = admin_display_id_map(match)
    if not id_map:
        return False
    return id_map.get(sel, sel) == id_map.get(won, won)


def settle_odds_bet(bet: dict, won_selection_id: Any, match: Optional[dict] = None) -> float:
    amount = _num(bet.get("stake"))
    odds = _num(bet.get("odds"))
    bt = (bet.get("betType") or "L").upper()
    ot = normalize_odds_type(str(bet.get("betFor") or ""), str(bet.get("oddsType") or ""))
    if ot == "toss":
        won = toss_selections_match(match, bet.get("selectionId"), won_selection_id)
    else:
        won = _selections_match(match, bet.get("selectionId"), won_selection_id)

    if ot in ("bookmaker", "toss"):
        if bt == "L":
            if won:
                return round(amount * odds / 100, 2) if odds > 3 else round(amount * odds, 2)
            return round(-amount, 2)
        if won:
            return round(-amount * odds / 100, 2) if odds > 3 else round(-amount * odds, 2)
        return round(amount, 2)

    o = odds if odds > 1 else odds + 1
    if bt in ("L", "B"):
        return round(amount * (o - 1), 2) if won else round(-amount, 2)
    return round(-amount * (o - 1), 2) if won else round(amount, 2)


# Diamond casino position groups — scrape2/all_games.json + UI _FALLBACK_RESULT_JSON.
def _casino_register_pairs(groups: dict[str, list[str]], pairs: list[tuple[str, str]]) -> None:
    for a, b in pairs:
        row = [a, b]
        groups[a] = row
        groups[b] = row


def _casino_register_multi(groups: dict[str, list[str]], sids: list[str]) -> None:
    row = [str(s) for s in sids]
    for sid in row:
        groups[sid] = row


def _casino_register_singles(groups: dict[str, list[str]], sids: list[str]) -> None:
    for sid in sids:
        groups[str(sid)] = [str(sid)]


def _build_casino_position_groups() -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}

    teen20: dict[str, list[str]] = {}
    _casino_register_multi(teen20, ["1", "3"])  # UI Player A/B (result codes 1 & 3)
    _casino_register_pairs(teen20, [("2", "4"), ("5", "6"), ("7", "8"), ("9", "10"), ("11", "12")])
    out["teen20"] = teen20

    teen: dict[str, list[str]] = {}
    _casino_register_pairs(teen, [("1", "2")])
    out["Teen"] = teen
    out["teen"] = teen

    dt20: dict[str, list[str]] = {}
    _casino_register_multi(dt20, ["1", "2", "3"])
    _casino_register_singles(dt20, ["4"])
    _casino_register_pairs(dt20, [("5", "6"), ("7", "8"), ("22", "23"), ("24", "25")])
    _casino_register_singles(dt20, [str(i) for i in range(9, 22)] + [str(i) for i in range(26, 39)])
    for alias in ("dt20", "dt202", "dt6", "dtl20"):
        out[alias] = dt20

    abj: dict[str, list[str]] = {}
    _casino_register_pairs(abj, [("1", "4"), ("2", "5"), ("3", "6")])
    _casino_register_singles(abj, [str(i) for i in range(7, 26)])
    out["abj"] = abj

    lucky7eu: dict[str, list[str]] = {}
    _casino_register_multi(lucky7eu, ["1", "2", "3", "4"])
    _casino_register_pairs(lucky7eu, [("5", "6")])
    _casino_register_singles(lucky7eu, [str(i) for i in range(7, 24)])
    out["lucky7eu"] = lucky7eu

    lucky7: dict[str, list[str]] = {}
    _casino_register_pairs(lucky7, [("1", "2")])
    out["lucky7"] = lucky7

    card32: dict[str, list[str]] = {}
    _casino_register_multi(card32, ["1", "2", "3", "4"])
    out["card32"] = card32
    out["card32eu"] = card32

    aaa: dict[str, list[str]] = {}
    _casino_register_multi(aaa, ["1", "2", "3"])  # Amar/Akbar/Anthony — scrape dt20-style 3-way
    _casino_register_pairs(aaa, [("4", "5"), ("6", "7")])
    _casino_register_singles(aaa, [str(i) for i in range(8, 23)])
    out["aaa"] = aaa
    out["aaa2"] = aaa
    out["vaaa"] = aaa

    teen89: dict[str, list[str]] = {}
    _casino_register_pairs(teen89, [("1", "2")])
    out["teen9"] = teen89
    out["teen8"] = teen89

    return out


CASINO_POSITION_GROUPS: dict[str, dict[str, list[str]]] = _build_casino_position_groups()


_AAA_TYPES = frozenset({"aaa", "aaa2", "vaaa"})
_AAA_MAIN_SIDS = frozenset({"1", "2", "3"})


def _aaa_main_uses_decimal_odds(casino_type: str, selection_sid: Any = None) -> bool:
    """AAA Amar/Akbar/Anthony — scrape l1/b1 decimal odds (4.45 lay → liability 345 on 100)."""
    ctype = str(casino_type or "").lower()
    sel = str(selection_sid).strip() if selection_sid is not None else ""
    return ctype in _AAA_TYPES and sel in _AAA_MAIN_SIDS


def casino_profit_mult(rate: float, casino_type: str = "", selection_sid: Any = None) -> float:
    """
    Scraped diamond casino rate formats (PlaceBetModal / Io JS):
    - AAA main b1/l1: decimal odds 4.45 → mult 3.45 (NOT /100)
    - Teen onday b1/100: stored 98 → profit mult 0.98
    - teen20 decimal odds: 1.95 → profit mult 0.95
    - Direct decimal: 0.86 → 0.86
    """
    r = float(rate or 0)
    if _aaa_main_uses_decimal_odds(casino_type, selection_sid):
        return max(r - 1, 0) if r > 1 else r
    if r > 3:
        return r / 100
    if r > 1:
        return r - 1
    return r


def casino_runner_sids(casino_type: str, selection_sid: Any = None, extra: Optional[list[Any]] = None) -> list[str]:
    """Scraped site: posArray keys = sids in same market group only."""
    ctype = str(casino_type or "").strip()
    sel = str(selection_sid).strip() if selection_sid is not None else ""

    if isinstance(extra, list) and extra:
        return [str(s) for s in extra if s is not None and str(s).strip()]

    groups = CASINO_POSITION_GROUPS.get(ctype) or CASINO_POSITION_GROUPS.get(ctype.lower())
    if groups and sel and sel in groups:
        return list(groups[sel])

    if sel:
        return [sel]
    return []


def _casino_is_back(bet_type: str) -> bool:
    bt = (bet_type or "Yes").upper()
    return bt in ("L", "B", "Y", "YES")


def calc_casino_position_delta(
    amount: float,
    rate: float,
    bet_type: str,
    selection_sid: Any,
    all_sids: list[Any],
    casino_type: str = "",
) -> dict[str, float]:
    """
    Casino LAGAI/KHAI — MatchDetail match-odds style.
    Yes/L on sid: selected +stake*(rate-1), others -stake.
    No/K on sid: selected -stake*(rate-1), others +stake.
    """
    profit = round(amount * casino_profit_mult(rate, casino_type, selection_sid), 2)
    is_back = _casino_is_back(bet_type)
    delta: dict[str, float] = {}
    for sid in all_sids:
        key = str(sid)
        if str(sid) == str(selection_sid):
            delta[key] = profit if is_back else -profit
        else:
            delta[key] = round(-amount, 2) if is_back else round(amount, 2)
    return delta


def calc_casino_liability(
    amount: float,
    rate: float,
    bet_type: str,
    casino_type: str = "",
    selection_sid: Any = None,
) -> float:
    """Open bet lock — Yes=stake, No=stake*(rate-1)."""
    if _casino_is_back(bet_type):
        return round(amount, 2)
    return round(amount * casino_profit_mult(rate, casino_type, selection_sid), 2)


def settle_casino_bet(bet: dict, won: bool, payout_rate: Optional[float] = None) -> float:
    amount = _num(bet.get("stake"))
    rate = _num(payout_rate or bet.get("rate") or bet.get("odds") or 1)
    profit = round(
        amount * casino_profit_mult(rate, str(bet.get("casinoType") or ""), bet.get("sid")),
        2,
    )
    if _casino_is_back(bet.get("betType")):
        return profit if won else round(-amount, 2)
    return round(-profit, 2) if won else round(amount, 2)
