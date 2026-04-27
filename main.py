#!/usr/bin/env python3
"""
Danskerdong bot
---------------
Poster mål, assists og røde kort fra danskere i udlandet til Bluesky.

Kører som cron-job via GitHub Actions (se .github/workflows/bot.yml).

Pipeline pr. kørsel:
  1. Hent dagens kampe fra Fotmob.
  2. Filtrer danske ligaer fra.
  3. For hver live/afsluttet kamp: hent kamp-detaljer.
  4. Find events (mål / assist / rødt kort) med en spiller fra
     danish_players.json.
  5. Post nye events (ikke allerede i state.json) til Bluesky.
  6. Gem opdateret state.

Data-kilde: Fotmob (uofficielt API). Hvis den pludselig stopper med at
virke, er der beskrevet fallback i README.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

# Fotmob liga-IDs vi IKKE vil poste fra (danske ligaer).
# 46 = Superligaen, 121 = NordicBet Liga (1. division), 122 = 2. division.
DANISH_LEAGUE_IDS = {46, 121, 122}

FOTMOB_BASE = "https://www.fotmob.com/api"
FOTMOB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,da;q=0.8",
    "Referer": "https://www.fotmob.com/",
}

# Maks antal event-IDs vi gemmer i state (forhindrer ubegrænset fil-vækst).
MAX_STATE_EVENTS = 5000

# Lille pause mellem posts for at være hyggelige ved Bluesky.
POST_COOLDOWN_SECONDS = 2

# Pause mellem Fotmob-kald.
FOTMOB_COOLDOWN_SECONDS = 0.4


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
    """Lowercase + fjern accenter/diakritiske tegn + trim whitespace.

    Håndterer nordiske bogstaver som ikke dekomponeres via NFKD:
    ø → o, æ → ae, ß → ss osv. (å dekomponeres naturligt)
    """
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
# Fotmob
# ---------------------------------------------------------------------------

def fotmob_get(path: str, params: dict | None = None) -> Any | None:
    url = f"{FOTMOB_BASE}{path}"
    try:
        r = requests.get(url, headers=FOTMOB_HEADERS, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log(f"Fotmob-kald fejlede: {path} → {e}")
        return None
    except json.JSONDecodeError as e:
        log(f"Fotmob JSON-fejl: {path} → {e}")
        return None


def fetch_todays_matches() -> list[dict]:
    """Returnerer liste af live/afsluttede kampe i dag uden for danske ligaer."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    data = fotmob_get("/data/matches", params={
        "date": today,
        "timezone": "Europe/Copenhagen",
        "includeNextDayLateNight": "true",
    })
    if not data:
        return []

    result = []
    for league in data.get("leagues", []):
        league_id = league.get("primaryId") or league.get("id")
        if league_id in DANISH_LEAGUE_IDS:
            continue
        league_name = league.get("name", "Ukendt liga")
        ccode = league.get("ccode", "")
        for match in league.get("matches", []):
            status = match.get("status", {}) or {}
            started = bool(status.get("started"))
            finished = bool(status.get("finished"))
            if not (started or finished):
                continue
            result.append({
                "id": match.get("id"),
                "league_id": league_id,
                "league_name": league_name,
                "country_code": ccode,
                "finished": finished,
            })
    return result


def fetch_match_details(match_id: str | int) -> dict | None:
    return fotmob_get("/data/matchDetails", params={"matchId": match_id})


# ---------------------------------------------------------------------------
# Event-udtrækning
# ---------------------------------------------------------------------------

def _find_events_list(details: dict) -> list[dict]:
    """Fotmob's struktur skifter nogle gange; prøv flere stier."""
    candidates = [
        ("content", "matchFacts", "events", "events"),
        ("content", "events", "events"),
        ("events", "events"),
    ]
    for path in candidates:
        node = details
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, list):
            return node
    return []


def _team_names(details: dict) -> tuple[str, str]:
    header = details.get("header") or {}
    teams = header.get("teams") or []
    if len(teams) >= 2:
        return (teams[0].get("name", "?"), teams[1].get("name", "?"))
    general = details.get("general") or {}
    return (general.get("homeTeam", {}).get("name", "?"),
            general.get("awayTeam", {}).get("name", "?"))


def _score_str(details: dict) -> str:
    header = details.get("header") or {}
    status = header.get("status") or {}
    score = status.get("scoreStr") or ""
    return score.replace(" ", "")


def _league_name(details: dict) -> str:
    general = details.get("general") or {}
    return general.get("leagueName") or "Ukendt liga"


def extract_events(details: dict, player_lookup: dict[str, dict]) -> list[dict]:
    """Find mål/assist/rødt kort med dansk spiller i kampen."""
    events_out: list[dict] = []
    if not details:
        return events_out

    match_id = (
        (details.get("general") or {}).get("matchId")
        or details.get("matchId")
        or "?"
    )
    home, away = _team_names(details)
    league_name = _league_name(details)
    score = _score_str(details)

    for ev in _find_events_list(details):
        ev_type_raw = (ev.get("type") or "").lower()
        minute = ev.get("timeStr") or ev.get("time") or ""
        minute = str(minute).replace("'", "").strip()
        is_home = bool(ev.get("isHome"))
        team = home if is_home else away
        opponent = away if is_home else home

        if "goal" in ev_type_raw and "own" not in ev_type_raw:
            scorer = (ev.get("player") or {}).get("name") or ev.get("nameStr") or ""
            hit = player_lookup.get(normalize_name(scorer))
            if hit:
                events_out.append({
                    "event_id": f"{match_id}-goal-{normalize_name(scorer)}-{minute}",
                    "type": "goal",
                    "player": hit["name"],
                    "minute": minute,
                    "team": team,
                    "opponent": opponent,
                    "score": score,
                    "league": league_name,
                    "match_id": match_id,
                })

            assister_obj = ev.get("assistPlayer") or ev.get("assist") or {}
            assister = ""
            if isinstance(assister_obj, dict):
                assister = assister_obj.get("name") or ""
            elif isinstance(assister_obj, str):
                assister = assister_obj
            if assister:
                hit_a = player_lookup.get(normalize_name(assister))
                if hit_a:
                    events_out.append({
                        "event_id": f"{match_id}-assist-{normalize_name(assister)}-{minute}",
                        "type": "assist",
                        "player": hit_a["name"],
                        "minute": minute,
                        "team": team,
                        "opponent": opponent,
                        "score": score,
                        "league": league_name,
                        "match_id": match_id,
                    })

        is_red = (
            "redcard" in ev_type_raw.replace(" ", "").replace("_", "")
            or (ev_type_raw == "card" and (ev.get("card") or "").lower() == "red")
        )
        if is_red:
            player = (ev.get("player") or {}).get("name") or ev.get("nameStr") or ""
            hit = player_lookup.get(normalize_name(player))
            if hit:
                events_out.append({
                    "event_id": f"{match_id}-red-{normalize_name(player)}-{minute}",
                    "type": "red_card",
                    "player": hit["name"],
                    "minute": minute,
                    "team": team,
                    "opponent": opponent,
                    "score": score,
                    "league": league_name,
                    "match_id": match_id,
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
        log("FEJL: BLUESKY_HANDLE og BLUESKY_PASSWORD skal være sat som env-variabler.")
        return 1

    log("Starter danskerdong-bot-kørsel.")

    try:
        players, player_lookup = load_players()
    except Exception as e:
        log(f"Kunne ikke læse {PLAYERS_FILE.name}: {e}")
        return 1

    state = load_state()
    processed: set[str] = set(state.get("processed_events", []))
    bootstrapped = state.get("bootstrapped", False)

    log(f"Spillere i database: {len(players)} · "
        f"events i state: {len(processed)} · bootstrapped: {bootstrapped}")

    matches = fetch_todays_matches()
    log(f"Fandt {len(matches)} live/afsluttede kampe i dag uden for DK.")
    if not matches:
        save_state(state)
        return 0

    new_events: list[dict] = []

    for m in matches:
        details = fetch_match_details(m["id"])
        if not details:
            continue
        events = extract_events(details, player_lookup)
        for ev in events:
            if ev["event_id"] not in processed:
                new_events.append(ev)
        time.sleep(FOTMOB_COOLDOWN_SECONDS)

    log(f"Nye events med danskere: {len(new_events)}")

    if not bootstrapped:
        log("Bootstrap-kørsel: markerer eksisterende events som processeret "
            "uden at poste. Fremtidige kørsler vil poste nye events.")
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
