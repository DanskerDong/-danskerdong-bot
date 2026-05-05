#!/usr/bin/env python3
"""
DanskerDong — ugentlig opdatering af team_cache.json (API-Football)
-------------------------------------------------------------------
Kører søndag kl. 03:00 dansk tid via .github/workflows/weekly.yml.
For hver aktiv spiller i danish_players.json: slå op via /players/profiles
(uden team/league-krav) for at finde spiller-ID, derefter /players?id=...
for at hente aktuelle hold + liga.

Spillere der ikke findes 3 uger i træk markeres som "inactive" og springes
over fremover. Inaktive genbesøges hver 8. uge.

API-budget: ~2 kald per aktiv spiller. 50 spillere → ~100 kald/uge.
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

import requests


ROOT = Path(__file__).parent
PLAYERS_FILE = ROOT / "danish_players.json"
CACHE_FILE = ROOT / "team_cache.json"

AF_API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
AF_BASE = "https://v3.football.api-sports.io"
AF_HEADERS = {"x-apisports-key": AF_API_KEY}

AF_COOLDOWN_SECONDS = 0.7
MAX_MISS_BEFORE_INACTIVE = 3
INACTIVE_RECHECK_WEEKS = 8


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


def af_get(path: str, params: dict | None = None) -> dict | None:
    if not AF_API_KEY:
        log("FEJL: API_FOOTBALL_KEY mangler.")
        return None
    url = f"{AF_BASE}{path}"
    try:
        r = requests.get(url, headers=AF_HEADERS, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        # API-Football returnerer ofte 'errors' som dict eller liste med daglige limits etc.
        errs = data.get("errors")
        if errs:
            if isinstance(errs, dict) and errs:
                log(f"  API-Football errors: {errs}")
            elif isinstance(errs, list) and errs:
                log(f"  API-Football errors: {errs}")
        return data
    except requests.RequestException as e:
        log(f"API-Football fejl: {path} → {e}")
        return None


def current_season() -> int:
    """API-Football's season parameter er året kalenderåret året begynder."""
    now = datetime.now(timezone.utc)
    if now.month >= 7:
        return now.year
    return now.year - 1


def find_player_id(name: str, last: str) -> int | None:
    """
    Trin 1: /players/profiles?search=LASTNAME (kræver IKKE team/league).
    Returnerer det bedste match som player-ID.
    Prioritering: dansk + fuldnavn-match > dansk + efternavn-match >
    dansk + en-eller-anden > fuldnavn-match.
    """
    if len(last) < 4:
        log(f"  efternavn for kort til /profiles ({last!r}) — springer over")
        return None

    data = af_get("/players/profiles", params={"search": last})
    if not data:
        return None
    profiles = data.get("response", []) or []
    if not profiles:
        return None

    target_norm = normalize_name(name)
    target_last_norm = normalize_name(last)

    best_id = None
    danish_lastname_match = None
    danish_any = None
    name_match = None

    for entry in profiles:
        player = entry.get("player", {}) or {}
        nationality = (player.get("nationality") or "").lower()
        firstname = player.get("firstname") or ""
        lastname = player.get("lastname") or ""
        full = f"{firstname} {lastname}".strip() or (player.get("name") or "")
        full_norm = normalize_name(full)
        last_norm = normalize_name(lastname)
        pid = player.get("id")
        is_danish = "denmark" in nationality

        if full_norm == target_norm and is_danish:
            best_id = pid
            break
        if is_danish and last_norm == target_last_norm and danish_lastname_match is None:
            danish_lastname_match = pid
        if is_danish and danish_any is None:
            danish_any = pid
        if full_norm == target_norm and name_match is None:
            name_match = pid

    return best_id or danish_lastname_match or danish_any or name_match


def fetch_player_team(player_id: int, season: int) -> dict | None:
    """
    Trin 2: /players?id=ID&season=YYYY → returnerer entry med statistics.
    """
    data = af_get("/players", params={"id": player_id, "season": season})
    if not data:
        return None
    response = data.get("response", []) or []
    if not response:
        # Prøv også sidste sæson (fx hvis aktuel sæson endnu ikke er startet)
        data = af_get("/players", params={"id": player_id, "season": season - 1})
        if not data:
            return None
        response = data.get("response", []) or []
        if not response:
            return None
    return response[0]


def search_player(name: str, season: int) -> tuple[dict | None, int]:
    """
    Returnerer (entry, calls_used).
    """
    last = name.split()[-1] if name else name
    pid = find_player_id(name, last)
    calls = 1
    if pid is None:
        return None, calls
    time.sleep(AF_COOLDOWN_SECONDS)
    entry = fetch_player_team(pid, season)
    calls += 1 if entry is None else 1
    return entry, calls


def extract_team_info(entry: dict) -> dict | None:
    statistics = entry.get("statistics", []) or []
    if not statistics:
        return None
    for stat in statistics:
        team = stat.get("team", {}) or {}
        league = stat.get("league", {}) or {}
        team_id = team.get("id")
        if team_id:
            return {
                "team_id": team_id,
                "team_name": team.get("name", "?"),
                "league_id": league.get("id"),
                "league_name": league.get("name", "?"),
                "country": league.get("country", "?"),
                "season": league.get("season"),
            }
    return None


def load_players() -> list[dict]:
    with open(PLAYERS_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def weeks_between(iso_date: str, now: datetime) -> int:
    try:
        then = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return int((now - then).days / 7)
    except Exception:
        return 999


def main() -> int:
    if not AF_API_KEY:
        log("FEJL: API_FOOTBALL_KEY skal være sat.")
        return 1

    log("Starter weekly_update — opdaterer team_cache.json.")
    players = load_players()
    cache = load_cache()
    season = current_season()
    log(f"Bruger sæson: {season}")

    now = datetime.now(timezone.utc)
    today_iso = now.isoformat()

    attempted = 0
    found = 0
    not_found = 0
    skipped_inactive = 0
    total_calls = 0

    for p in players:
        if not p.get("active", True):
            continue
        name = p["name"]
        existing = cache.get(name, {})
        status = existing.get("status", "active")
        miss_count = existing.get("miss_count", 0)
        marked_inactive = existing.get("marked_inactive")

        if status == "inactive" and marked_inactive:
            if weeks_between(marked_inactive, now) < INACTIVE_RECHECK_WEEKS:
                skipped_inactive += 1
                continue
            log(f"Recheck af inaktiv spiller: {name}")

        attempted += 1
        log(f"Slår {name!r} op …")
        entry, calls = search_player(name, season)
        total_calls += calls
        time.sleep(AF_COOLDOWN_SECONDS)

        if entry is None:
            miss_count += 1
            log(f"  ikke fundet (miss_count={miss_count})")
            new_entry = dict(existing)
            new_entry.update({
                "team_id": None,
                "team_name": None,
                "league_id": None,
                "league_name": None,
                "miss_count": miss_count,
                "last_attempt": today_iso,
            })
            if miss_count >= MAX_MISS_BEFORE_INACTIVE:
                new_entry["status"] = "inactive"
                new_entry["marked_inactive"] = today_iso
                log(f"  → markeres som inaktiv ({miss_count} misser)")
            else:
                new_entry["status"] = "missing"
            cache[name] = new_entry
            not_found += 1
            continue

        team_info = extract_team_info(entry)
        if team_info is None:
            miss_count += 1
            cache[name] = {
                "team_id": None,
                "team_name": None,
                "league_id": None,
                "league_name": None,
                "status": "missing",
                "miss_count": miss_count,
                "last_attempt": today_iso,
            }
            not_found += 1
            log(f"  fundet, men ingen aktuelle statistikker (miss_count={miss_count})")
            continue

        cache[name] = {
            **team_info,
            "status": "active",
            "miss_count": 0,
            "last_found": today_iso,
            "last_attempt": today_iso,
        }
        log(f"  → {team_info['team_name']} ({team_info['league_name']}, "
            f"{team_info['country']})")
        found += 1

    save_cache(cache)
    log(f"Player lookup: {attempted} attempted, {found} found, {not_found} not found, "
        f"{skipped_inactive} skipped (inactive). Total API-kald: {total_calls}.")
    log("Cache written.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("Uventet fejl:")
        traceback.print_exc()
        sys.exit(1)
aktiv ({miss_count} misser)")
            else:
                new_entry["status"] = "missing"
            cache[name] = new_entry
            not_found += 1
            continue

        team_info = extract_team_info(entry)
        if team_info is None:
            miss_count += 1
            cache[name] = {
                "team_id": None,
                "team_name": None,
                "league_id": None,
                "league_name": None,
                "status": "missing",
                "miss_count": miss_count,
                "last_attempt": today_iso,
            }
            not_found += 1
            log(f"  fundet, men ingen aktuelle statistikker (miss_count={miss_count})")
            continue

        cache[name] = {
            **team_info,
            "status": "active",
            "miss_count": 0,
            "last_found": today_iso,
            "last_attempt": today_iso,
        }
        log(f"  → {team_info['team_name']} ({team_info['league_name']}, "
            f"{team_info['country']})")
        found += 1

    save_cache(cache)
    log(f"Player lookup: {attempted} attempted, {found} found, {not_found} not found, "
        f"{skipped_inactive} skipped (inactive). Total API-kald: {total_calls}.")
    log("Cache written.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("Uventet fejl:")
        traceback.print_exc()
        sys.exit(1)
