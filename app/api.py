"""REST + WebSocket API."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app import db
from app.pinnacle_client import ArcadiaClient, ArcadiaError
from app.poller import poller
from app.settings_store import get_all_settings_masked, get_arcadia_api_key, set_setting
from app.ws import manager

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    arcadia_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


@router.get("/settings")
async def get_settings():
    return await get_all_settings_masked()


@router.post("/settings")
async def update_settings(body: SettingsUpdate):
    changed_arcadia = False
    for key, value in body.model_dump(exclude_none=True).items():
        await set_setting(key, value)
        if key == "arcadia_api_key":
            changed_arcadia = True
    if changed_arcadia:
        await poller.refresh_client()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Saved leagues (persisted so league IDs don't need to be re-found in the
# browser's network tab every time - Pinnacle's UI doesn't expose them).
# ---------------------------------------------------------------------------

class SavedLeague(BaseModel):
    league_id: int
    league_name: str


@router.get("/saved-leagues")
async def list_saved_leagues():
    rows = await db.fetch(
        "select league_id, league_name, added_at from saved_leagues order by league_name asc"
    )
    return [dict(r) for r in rows]


@router.post("/saved-leagues")
async def add_saved_league(body: SavedLeague):
    await db.execute(
        """
        insert into saved_leagues (league_id, league_name) values ($1, $2)
        on conflict (league_id) do update set league_name = excluded.league_name
        """,
        body.league_id,
        body.league_name,
    )
    return {"ok": True}


@router.delete("/saved-leagues/{league_id}")
async def delete_saved_league(league_id: int):
    await db.execute("delete from saved_leagues where league_id = $1", league_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@router.get("/leagues/{league_id}/matchups")
async def discover_league_matchups(league_id: int):
    client = ArcadiaClient(api_key=await get_arcadia_api_key())
    try:
        matchups = await client.get_league_matchups(league_id)
    except ArcadiaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.aclose()
    return matchups


@router.get("/debug/markets/{matchup_id}")
async def debug_raw_markets(matchup_id: int):
    """Raw passthrough of the markets endpoint, for diagnosing schema
    mismatches against a live response - not used by the frontend.
    """
    client = ArcadiaClient(api_key=await get_arcadia_api_key())
    try:
        return await client.get_matchup_markets(matchup_id)
    except ArcadiaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.aclose()


class MatchToTrack(BaseModel):
    pinnacle_matchup_id: int
    home_team: str
    away_team: str
    start_time: datetime
    league_id: int
    league_name: str | None = None


class TrackMatchesRequest(BaseModel):
    matches: list[MatchToTrack]
    mjp_round_label: str | None = None


@router.post("/monitored-matches")
async def add_monitored_matches(body: TrackMatchesRequest):
    added = []
    for m in body.matches:
        row = await db.fetchrow(
            """
            insert into matchups (
                pinnacle_matchup_id, league_id, league_name, home_team, away_team,
                start_time, is_monitored, mjp_round_label
            ) values ($1,$2,$3,$4,$5,$6,true,$7)
            on conflict (pinnacle_matchup_id) do update set
                is_monitored = true,
                mjp_round_label = excluded.mjp_round_label
            returning id
            """,
            m.pinnacle_matchup_id,
            m.league_id,
            m.league_name,
            m.home_team,
            m.away_team,
            m.start_time,
            body.mjp_round_label,
        )
        added.append(row["id"])
    return {"added": added}


@router.patch("/matches/{matchup_id}")
async def update_match_monitoring(matchup_id: int, is_monitored: bool):
    await db.execute("update matchups set is_monitored = $1 where id = $2", is_monitored, matchup_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Summary + detail
# ---------------------------------------------------------------------------

@router.get("/rounds")
async def list_rounds():
    """Every Megajackpot round ever tracked, most recent first, with
    whether it's still actively being polled (is_active) - lets the
    dashboard distinguish the live round from archived history rather
    than matches just disappearing once auto-unmonitored.
    """
    rows = await db.fetch(
        """
        select
            coalesce(mjp_round_label, '(unlabeled)') as round_label,
            count(*) as match_count,
            min(start_time) as earliest_kickoff,
            max(start_time) as latest_kickoff,
            bool_or(is_monitored) as is_active
        from matchups
        group by coalesce(mjp_round_label, '(unlabeled)')
        order by min(start_time) desc
        """
    )
    return [dict(r) for r in rows]


@router.get("/matches/summary")
async def matches_summary(round_label: str | None = None):
    """Without round_label: the actively-monitored (live) matches, same
    as before. With round_label: every match in that round regardless of
    is_monitored, so an archived/finished round stays fully viewable
    instead of disappearing once auto-unmonitored.
    """
    if round_label is None:
        condition, params = "m.is_monitored = true", []
    elif round_label == "(unlabeled)":
        condition, params = "m.mjp_round_label is null", []
    else:
        condition, params = "m.mjp_round_label = $1", [round_label]

    rows = await db.fetch(
        f"""
        select
            m.id, m.pinnacle_matchup_id, m.home_team, m.away_team, m.league_name,
            m.start_time, m.mjp_round_label, m.is_monitored,
            s.tier, s.sharp_side, s.contested, s.total_score, s.ah_score, s.x2_score,
            s.limit_bonus, s.convergence_bonus, s.computed_at as score_computed_at,
            ah.detail_json as ah_detail,
            x2.detail_json as x2_detail,
            lim.detail_json as limit_detail
        from matchups m
        left join lateral (
            select * from match_scores ms where ms.matchup_id = m.id
            order by computed_at desc limit 1
        ) s on true
        left join lateral (
            select detail_json from signals sg where sg.matchup_id = m.id
            and sg.signal_type = 'ah_line_shift' order by computed_at desc limit 1
        ) ah on true
        left join lateral (
            select detail_json from signals sg where sg.matchup_id = m.id
            and sg.signal_type = 'x2_displacement' order by computed_at desc limit 1
        ) x2 on true
        left join lateral (
            select detail_json from signals sg where sg.matchup_id = m.id
            and sg.signal_type = 'limit_movement' order by computed_at desc limit 1
        ) lim on true
        where {condition}
        order by m.start_time asc
        """,
        *params,
    )
    result = []
    for r in rows:
        d = dict(r)
        d["ah_detail"] = db.from_jsonb(d["ah_detail"])
        d["x2_detail"] = db.from_jsonb(d["x2_detail"])
        d["limit_detail"] = db.from_jsonb(d["limit_detail"])
        result.append(d)
    return result


@router.get("/matches/{matchup_id}/detail")
async def match_detail(matchup_id: int):
    matchup = await db.fetchrow("select * from matchups where id = $1", matchup_id)
    if not matchup:
        raise HTTPException(status_code=404, detail="Match not found")

    latest_score = await db.fetchrow(
        "select * from match_scores where matchup_id = $1 order by computed_at desc limit 1",
        matchup_id,
    )
    score_history = await db.fetch(
        "select computed_at, total_score, tier, sharp_side from match_scores "
        "where matchup_id = $1 order by computed_at asc",
        matchup_id,
    )
    latest_signals = await db.fetch(
        """
        select distinct on (signal_type) signal_type, direction, magnitude, detail_json, computed_at
        from signals where matchup_id = $1
        order by signal_type, computed_at desc
        """,
        matchup_id,
    )

    # Full history of every velocity_shape check ever run for this match -
    # a fresh row is inserted every poll cycle, so "was this ever steam
    # earlier" is already sitting in the data even after the label has
    # since moved on to something else. Compressed to only the moments the
    # label actually changed, not every single poll that repeated it.
    velocity_rows = await db.fetch(
        """
        select computed_at, direction as label from signals
        where matchup_id = $1 and signal_type = 'velocity_shape'
        order by computed_at asc
        """,
        matchup_id,
    )
    velocity_transitions = []
    last_label = None
    for r in velocity_rows:
        if r["label"] != last_label:
            velocity_transitions.append({"at": r["computed_at"], "label": r["label"]})
            last_label = r["label"]

    # Full raw time-series for independent analysis, grouped by market so the
    # frontend can render one chart per series regardless of what the signal
    # engine concluded. Main lines (period 0, non-alternate) drive the
    # primary charts; everything else (alternates, first-half) is still
    # returned for the raw table since it was collected too.
    all_snapshots = await db.fetch(
        """
        select market_key, market_type, period, is_alternate, version, status, cutoff_at, captured_at,
               home_price, draw_price, away_price, home_points, limit_amount,
               fair_home_prob, fair_draw_prob, fair_away_prob
        from market_snapshots
        where matchup_id = $1
        order by captured_at asc
        """,
        matchup_id,
    )

    series: dict[str, list[dict]] = {"moneyline": [], "spread": [], "total": [], "team_total": [], "other": []}
    raw_table = []
    for r in all_snapshots:
        d = dict(r)
        raw_table.append(d)
        if not d["is_alternate"] and d["period"] == 0:
            series.get(d["market_type"], series["other"]).append(d)

    signals_by_type = {}
    for r in latest_signals:
        d = dict(r)
        d["detail_json"] = db.from_jsonb(d["detail_json"])
        signals_by_type[r["signal_type"]] = d

    return {
        "matchup": dict(matchup),
        "latest_score": dict(latest_score) if latest_score else None,
        "score_history": [dict(r) for r in score_history],
        "latest_signals": signals_by_type,
        "velocity_transitions": velocity_transitions,
        "series": {
            "moneyline_main": series["moneyline"],
            "spread_main": series["spread"],
            "total_main": series["total"],
        },
        "raw_snapshots": raw_table,
    }


@router.post("/poll/force/{matchup_id}")
async def force_poll(matchup_id: int):
    poller.force_poll(matchup_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
