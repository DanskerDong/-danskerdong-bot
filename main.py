#!/usr/bin/env python3
"""
DanskerDong — live-bot (Football-Data.org)
------------------------------------------
Kører hver 5. min via cron-job.org → workflow_dispatch.
Henter dagens kampe i de 12 ligaer Football-Data.org dækker, finder mål,
assists og røde kort med dansk spiller, og poster til Bluesky.
 
Spillere udenfor de 12 ligaer (Belgien, Norge, Sverige, MLS, Tyrkiet osv.)
håndteres af daily_catchup.py én gang i døgnet.
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
    print("FEJL: atproto-pakken er ikke installeret. Kør 'pip install -r requirements.txt'.")
    sys.exit(1)
 
 
# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
 
ROOT = Path(__file__).parent
PLAYERS_FILE = ROOT / "danish_players.json"
STATE_FILE = ROOT / "state.json"
 
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "").strip()
BLUESKY_PASSWORD = os.environ.get("BLUESKY_PASSWORD", "").strip()
FD_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
 
FD_BASE = "https://api.football-data.org/v4"
 
# Football-Data.org gratis-tier dækker disse competitions.
# Liste: https://www.football-data.org/coverage
FD_COMPETITIONS = ["PL", "ELC", "PD", "BL1", "SA", "FL1", "DED", "PPL", "CL", "BSA"]
# PL=Premier League, ELC=Championship, PD=La Liga, BL1=Bundesliga,
# SA=Serie A, FL1=Ligue 1, DED=Eredivisie, PPL=Primeira Liga,
# CL=Champions League, BSA=Brasilian Serie A
 
MAX_STATE_EVENTS = 5000
POST_COOLDOWN_SECONDS = 2
FD_COOLDOWN_SECONDS = 6.5  # max 10 kald/min på free tier; 6.5s = 9.2 kald/min
 
 
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
 
def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)
 
 
_NORDIC_TRANSLATE = str.maketrans({
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "Ae",
    "ß": "ss",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th",
    "ł": "l", "Ł": "L",
})
 
 
def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    translated = name.translate(_NORDIC_TRANSLATE)
    nfkd = unicodedata.normalize("NFKD", translated)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_str.lower().strip()
 
 
# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------
 
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
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"processed_events": [], "bootstrapped": False}
    state.setdefault("processed_events", [])
    state.setdefault("bootstrapped", False)
    return state
 
 
def save_state(state: dict) -> None:
    state["processed_events"] = state["processed_events"][-MAX_STATE_EVENTS:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
 
 
# ---------------------------------------------------------------------------
# Football-Data.org
# ---------------------------------------------------------------------------
 
def fd_get(path: str, params: dict | None = None) -> dict | None:
    if not FD_API_KEY:
        log("FEJL: FOOTBALL_DATA_API_KEY mangler.")
        return None
    url = f"{FD_BASE}{path}"
    headers = {"X-Auth-Token": FD_API_KEY}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code == 429:
            log(f"Football-Data rate-limit ramt ({path}); springer over denne kørsel.")
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log(f"Football-Data fejl: {path} → {e}")
        return None
 
 
def fetch_recent_matches() -> list[dict]:
    """
    Returnerer kampe fra i dag og i går (for at fange sene afslutninger),
    kun status FINISHED eller IN_PLAY/PAUSED.
    """
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    params = {
        "dateFrom": yesterday.isoformat(),
        "dateTo": today.isoformat(),
    }
    data = fd_get("/matches", params=params)
    if not data:
        return []
    matches = data.get("matches", []) or []
    # Filtrér til kun de competitions vi vil dække
    relevant = []
    for m in matches:
        comp = (m.get("competition") or {}).get("code", "")
        if comp not in FD_COMPETITIONS:
            continue
        status = m.get("status", "")
        if status not in ("FINISHED", "IN_PLAY", "PAUSED", "LIVE"):
            continue
        relevant.append(m)
    return relevant
 
 
def fetch_match_details(match_id: int | str) -> dict | None:
    return fd_get(f"/matches/{match_id}")
 
 
# ---------------------------------------------------------------------------
# Event-udtrækning
# ---------------------------------------------------------------------------
 
def _score_str(match: dict) -> str:
    score = match.get("score", {}) or {}
    full = score.get("fullTime", {}) or {}
    home = full.get("home")
    away = full.get("away")
    if home is None or away is None:
        ht = score.get("halfTime", {}) or {}
        home = ht.get("home", 0) or 0
        away = ht.get("away", 0) or 0
    return f"{home}-{away}"
 
 
def _team_names(match: dict) -> tuple[str, str, int | None, int | None]:
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}
    return (
        home.get("shortName") or home.get("name") or "?",
        away.get("shortName") or away.get("name") or "?",
        home.get("id"),
        away.get("id"),
    )
 
 
def _league_name(match: dict) -> str:
    comp = match.get("competition") or {}
    return comp.get("name") or "Ukendt liga"
 
 
def extract_events(match: dict, player_lookup: dict[str, dict]) -> list[dict]:
    """Find mål, assists og røde kort med dansk spiller."""
    events_out: list[dict] = []
    if not match:
        return events_out
 
    match_id = match.get("id", "?")
    home_name, away_name, home_id, away_id = _team_names(match)
    league_name = _league_name(match)
 
    # Mål
    goals = match.get("goals", []) or []
    for goal in goals:
        minute = goal.get("minute", "")
        injury = goal.get("injuryTime")
        if injury:
            minute = f"{minute}+{injury}"
        gtype = (goal.get("type") or "").upper()  # REGULAR, OWN, PENALTY
        team_obj = goal.get("team") or {}
        team_id = team_obj.get("id")
        team = home_name if team_id == home_id else away_name
        opponent = away_name if team_id == home_id else home_name
        # Score lige efter målet
        sc = goal.get("score") or {}
        h = sc.get("home")
        a = sc.get("away")
        score = f"{h}-{a}" if h is not None and a is not None else _score_str(match)
 
        scorer = (goal.get("scorer") or {}).get("name") or ""
        scorer_norm = normalize_name(scorer)
        hit_scorer = player_lookup.get(scorer_norm)
 
        # Mål — kun hvis ikke selvmål (eller hvis selvmål skal regnes som assist for modstander)
        if hit_scorer and gtype != "OWN":
            events_out.append({
                "event_id": f"FD-{match_id}-goal-{scorer_norm}-{minute}",
                "type": "goal",
                "player": hit_scorer["name"],
                "minute": str(minute),
                "team": team,
                "opponent": opponent,
                "score": score,
                "league": league_name,
                "match_id": match_id,
                "source": "football-data",
            })
 
        # Assist (ikke ved selvmål)
        if gtype != "OWN":
            assist = (goal.get("assist") or {})
            assister = assist.get("name") if isinstance(assist, dict) else ""
            assister_norm = normalize_name(assister)
            hit_assister = player_lookup.get(assister_norm)
            if hit_assister:
                events_out.append({
                    "event_id": f"FD-{match_id}-assist-{assister_norm}-{minute}",
                    "type": "assist",
                    "player": hit_assister["name"],
                    "minute": str(minute),
                    "team": team,
                    "opponent": opponent,
                    "score": score,
                    "league": league_name,
                    "match_id": match_id,
                    "source": "football-data",
                })
 
    # Røde kort (bookings)
    bookings = match.get("bookings", []) or []
    for booking in bookings:
        card = (booking.get("card") or "").upper()
        if "RED" not in card:
            continue
        minute = booking.get("minute", "")
        player_name = (booking.get("player") or {}).get("name") or ""
        player_norm = normalize_name(player_name)
        hit = player_lookup.get(player_norm)
        if not hit:
            continue
        team_obj = booking.get("team") or {}
        team_id = team_obj.get("id")
        team = home_name if team_id == home_id else away_name
        opponent = away_name if team_id == home_id else home_name
        events_out.append({
            "event_id": f"FD-{match_id}-red-{player_norm}-{minute}",
            "type": "red_card",
            "player": hit["name"],
            "minute": str(minute),
            "team": team,
            "opponent": opponent,
            "score": _score_str(match),
            "league": league_name,
            "match_id": match_id,
            "source": "football-data",
        })
 
    return events_out
 
 
# ---------------------------------------------------------------------------
# Post-formatering
# ---------------------------------------------------------------------------
 
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
 
 
# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------
 
def bluesky_login() -> Client | None:
    try:
        client = Client()
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
        return client
    except Exception as e:
        log(f"Kunne ikke logge ind på Bluesky: {e}")
        return None
 
 
def post_to_bluesky(client: Client, text: str) -> bool:
    try:
        client.send_post(text=text)
        return True
    except Exception as e:
        log(f"Post fejlede: {e}")
        return False
 
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
 
def main() -> int:
    if not BLUESKY_HANDLE or not BLUESKY_PASSWORD:
        log("FEJL: BLUESKY_HANDLE og BLUESKY_PASSWORD skal være sat.")
        return 1
    if not FD_API_KEY:
        log("FEJL: FOOTBALL_DATA_API_KEY skal være sat.")
        return 1
 
    log("Starter DanskerDong live-bot (Football-Data.org).")
 
    try:
        players, player_lookup = load_players()
    except Exception as e:
        log(f"Kunne ikke læse {PLAYERS_FILE.name}: {e}")
        return 1
 
    state = load_state()
    processed: set[str] = set(state.get("processed_events", []))
    bootstrapped = state.get("bootstrapped", False)
    log(f"Aktive spillere: {sum(1 for p in players if p.get('active', True))} · "
        f"events i state: {len(processed)} · bootstrapped: {bootstrapped}")
 
    matches = fetch_recent_matches()
    log(f"Fandt {len(matches)} relevante kampe i Football-Data.")
    if not matches:
        save_state(state)
        return 0
 
    new_events: list[dict] = []
    for m in matches:
        # Kald detail-endpoint kun hvis matches-listen ikke har goals/bookings
        # (de fleste tilfælde har det allerede inkluderet)
        if not m.get("goals") and not m.get("bookings"):
            details = fetch_match_details(m["id"])
            if details:
                m = details
            time.sleep(FD_COOLDOWN_SECONDS)
        events = extract_events(m, player_lookup)
        for ev in events:
            if ev["event_id"] not in processed:
                new_events.append(ev)
 
    log(f"Nye events med danskere: {len(new_events)}")
 
    if not bootstrapped:
        log("Bootstrap-kørsel: markerer eksisterende events som processeret uden at poste.")
        for ev in new_events:
            processed.add(ev["event_id"])
        state["bootstrapped"] = True
        state["processed_events"] = list(processed)
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
        log("Uventet fejl — hele traceback følger:")
        traceback.print_exc()
        sys.exit(1)
