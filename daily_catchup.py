#!/usr/bin/env python3
"""
DanskerDong — daglig catchup (API-Football)
-------------------------------------------
Kører kl. 06:00 dansk tid via .github/workflows/daily.yml.
Henter gårsdagens kampe for de hold hvor en aktiv dansker er ifølge
team_cache.json, finder events (mål/assist/rødt kort) og poster nye til
Bluesky.
 
Bruger ~10-50 API-Football kald/dag. Free tier: 100 kald/dag.
"""
 
from __future__ import annotations
 
import json
import os
import sys
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
 
import requests
 
try:
    from atproto import Client
except ImportError:
    print("FEJL: atproto-pakken er ikke installeret.")
    sys.exit(1)
 
 
ROOT = Path(__file__).parent
PLAYERS_FILE = ROOT / "danish_players.json"
STATE_FILE = ROOT / "state.json"
CACHE_FILE = ROOT / "team_cache.json"
 
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "").strip()
BLUESKY_PASSWORD = os.environ.get("BLUESKY_PASSWORD", "").strip()
AF_API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
 
AF_BASE = "https://v3.football.api-sports.io"
AF_HEADERS = {"x-apisports-key": AF_API_KEY}
 
# Football-Data.org dækker disse ligaer live; vi springer dem her over
# for ikke at duplikere posts.
FD_LEAGUE_IDS = {
    39,   # Premier League
    40,   # Championship
    140,  # La Liga
    78,   # Bundesliga
    135,  # Serie A
    61,   # Ligue 1
    88,   # Eredivisie
    94,   # Primeira Liga
    2,    # Champions League
    71,   # Brazilian Serie A
}
 
MAX_STATE_EVENTS = 5000
POST_COOLDOWN_SECONDS = 2
AF_COOLDOWN_SECONDS = 1.0
 
 
# ---------------------------------------------------------------------------
 
def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)
 
 
_NORDIC_TRANSLATE = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae",
    "ß": "ss", "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th",
    "ł": "l", "Ł": "L",
})
 
 
def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    translated = name.translate(_NORDIC_TRANSLATE)
    nfkd = unicodedata.normalize("NFKD", translated)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_str.lower().strip()
 
 
def load_players() -> tuple[list[dict], dict[str, dict]]:
    with open(PLAYERS_FILE, encoding="utf-8") as f:
        players = json.load(f)
    lookup: dict[str, dict] = {}
    for p in players:
        if not p.get("active", True):
            continue
        lookup[normalize_name(p["name"])] = p
        for alias in p.get("aliases", []):
            lookup[normalize_name(alias)] = p
    return players, lookup
 
 
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_events": [], "bootstrapped": False}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"processed_events": [], "bootstrapped": False}
 
 
def save_state(state: dict) -> None:
    state["processed_events"] = state["processed_events"][-MAX_STATE_EVENTS:]
    state["last_run_daily"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
 
 
def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
 
 
# ---------------------------------------------------------------------------
# API-Football
# ---------------------------------------------------------------------------
 
def af_get(path: str, params: dict | None = None) -> dict | None:
    if not AF_API_KEY:
        log("FEJL: API_FOOTBALL_KEY mangler.")
        return None
    url = f"{AF_BASE}{path}"
    try:
        r = requests.get(url, headers=AF_HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            log(f"API-Football errors: {data.get('errors')}")
        return data
    except requests.RequestException as e:
        log(f"API-Football fejl: {path} → {e}")
        return None
 
 
def fetch_yesterdays_fixtures_for_team(team_id: int, date_str: str) -> list[dict]:
    data = af_get("/fixtures", params={"team": team_id, "date": date_str})
    if not data:
        return []
    return data.get("response", []) or []
 
 
def fetch_fixture_events(fixture_id: int) -> list[dict]:
    data = af_get("/fixtures/events", params={"fixture": fixture_id})
    if not data:
        return []
    return data.get("response", []) or []
 
 
# ---------------------------------------------------------------------------
# Event-udtrækning
# ---------------------------------------------------------------------------
 
def extract_events_from_fixture(fixture: dict, events: list[dict],
                                 player_lookup: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    teams = fixture.get("teams", {}) or {}
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    home_id = home.get("id")
    home_name = home.get("name", "?")
    away_name = away.get("name", "?")
 
    goals_obj = fixture.get("goals", {}) or {}
    score = f"{goals_obj.get('home', 0)}-{goals_obj.get('away', 0)}"
 
    league = (fixture.get("league") or {}).get("name", "Ukendt liga")
    fixture_id = (fixture.get("fixture") or {}).get("id", "?")
 
    for ev in events:
        ev_type = (ev.get("type") or "").lower()
        detail = (ev.get("detail") or "").lower()
        minute = ((ev.get("time") or {}).get("elapsed", "") or "")
        extra = ((ev.get("time") or {}).get("extra"))
        if extra:
            minute = f"{minute}+{extra}"
        team_obj = ev.get("team") or {}
        team_id = team_obj.get("id")
        team = home_name if team_id == home_id else away_name
        opponent = away_name if team_id == home_id else home_name
 
        player_obj = ev.get("player") or {}
        player_name = player_obj.get("name") or ""
        player_norm = normalize_name(player_name)
        assist_obj = ev.get("assist") or {}
        assist_name = assist_obj.get("name") or ""
        assist_norm = normalize_name(assist_name)
 
        if ev_type == "goal" and "own goal" not in detail:
            hit = player_lookup.get(player_norm)
            if hit:
                out.append({
                    "event_id": f"AF-{fixture_id}-goal-{player_norm}-{minute}",
                    "type": "goal",
                    "player": hit["name"],
                    "minute": str(minute),
                    "team": team,
                    "opponent": opponent,
                    "score": score,
                    "league": league,
                    "match_id": fixture_id,
                    "source": "api-football",
                })
            hit_a = player_lookup.get(assist_norm)
            if hit_a:
                out.append({
                    "event_id": f"AF-{fixture_id}-assist-{assist_norm}-{minute}",
                    "type": "assist",
                    "player": hit_a["name"],
                    "minute": str(minute),
                    "team": team,
                    "opponent": opponent,
                    "score": score,
                    "league": league,
                    "match_id": fixture_id,
                    "source": "api-football",
                })
        elif ev_type == "card" and "red" in detail:
            hit = player_lookup.get(player_norm)
            if hit:
                out.append({
                    "event_id": f"AF-{fixture_id}-red-{player_norm}-{minute}",
                    "type": "red_card",
                    "player": hit["name"],
                    "minute": str(minute),
                    "team": team,
                    "opponent": opponent,
                    "score": score,
                    "league": league,
                    "match_id": fixture_id,
                    "source": "api-football",
                })
    return out
 
 
def format_post(event: dict) -> str | None:
    et = event["type"]
    player = event["player"]
    minute = event["minute"]
    team = event["team"] or "?"
    opponent = event["opponent"] or "?"
    score = event["score"]
    league = event["league"]
    minute_prefix = f"{minute}' · " if minute else ""
    if et == "goal":
        headline = f"{minute_prefix}⚽️ MÅL! {player} scorer for {team} mod {opponent}"
    elif et == "assist":
        headline = f"{minute_prefix}🎯 ASSIST af {player} — {team} mod {opponent}"
    elif et == "red_card":
        headline = f"{minute_prefix}🟥 RØDT KORT til {player} — {team} mod {opponent}"
    else:
        return None
    lines = [headline]
    if score:
        lines.append(f"Stilling: {score}")
    lines.append(f"🏆 {league}")
    text = "\n".join(lines)
    if len(text) > 300:
        text = text[:297] + "…"
    return text
 
 
def bluesky_login() -> Client | None:
    try:
        client = Client()
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
        return client
    except Exception as e:
        log(f"Bluesky-login fejl: {e}")
        return None
 
 
def post_to_bluesky(client: Client, text: str) -> bool:
    try:
        client.send_post(text=text)
        return True
    except Exception as e:
        log(f"Post fejlede: {e}")
        return False
 
 
# ---------------------------------------------------------------------------
 
def main() -> int:
    if not BLUESKY_HANDLE or not BLUESKY_PASSWORD:
        log("FEJL: BLUESKY_HANDLE og BLUESKY_PASSWORD skal være sat.")
        return 1
    if not AF_API_KEY:
        log("FEJL: API_FOOTBALL_KEY skal være sat.")
        return 1
 
    log("Starter DanskerDong daglig catchup (API-Football).")
 
    players, player_lookup = load_players()
    state = load_state()
    cache = load_cache()
    processed: set[str] = set(state.get("processed_events", []))
    bootstrapped = state.get("bootstrapped", False)
 
    # Find unique team_ids fra cachen, ekskluderer FD-dækkede ligaer
    team_to_players: dict[int, list[str]] = {}
    for player_name, info in cache.items():
        if info.get("status") != "active":
            continue
        league_id = info.get("league_id")
        if league_id in FD_LEAGUE_IDS:
            continue
        team_id = info.get("team_id")
        if not team_id:
            continue
        team_to_players.setdefault(team_id, []).append(player_name)
 
    log(f"Hold at tjekke (uden for FD-dækning): {len(team_to_players)}")
 
    if not team_to_players:
        log("Ingen hold at tjekke. Husk at køre weekly_update.py først.")
        save_state(state)
        return 0
 
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    new_events: list[dict] = []
 
    for team_id in team_to_players:
        fixtures = fetch_yesterdays_fixtures_for_team(team_id, yesterday)
        time.sleep(AF_COOLDOWN_SECONDS)
        for fix in fixtures:
            status = ((fix.get("fixture") or {}).get("status") or {}).get("short", "")
            if status not in ("FT", "AET", "PEN"):
                continue
            fixture_id = (fix.get("fixture") or {}).get("id")
            if not fixture_id:
                continue
            evs = fetch_fixture_events(fixture_id)
            time.sleep(AF_COOLDOWN_SECONDS)
            for e in extract_events_from_fixture(fix, evs, player_lookup):
                if e["event_id"] not in processed:
                    new_events.append(e)
 
    log(f"Nye events: {len(new_events)}")
 
    if not bootstrapped:
        for ev in new_events:
            processed.add(ev["event_id"])
        state["bootstrapped"] = True
        state["processed_events"] = list(processed)
        log("Bootstrap-kørsel: markerer events processeret uden at poste.")
        save_state(state)
        return 0
 
    if not new_events:
        save_state(state)
        return 0
 
    client = bluesky_login()
    if client is None:
        return 1
 
    posted = 0
    for ev in new_events:
        text = format_post(ev)
        if not text:
            continue
        log(f"Posting: {text!r}")
        if post_to_bluesky(client, text):
            processed.add(ev["event_id"])
            posted += 1
            time.sleep(POST_COOLDOWN_SECONDS)
 
    log(f"Postede {posted}/{len(new_events)} events.")
    state["processed_events"] = list(processed)
    save_state(state)
    log("Færdig.")
    return 0
 
 
if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("Uventet fejl:")
        traceback.print_exc()
        sys.exit(1)
