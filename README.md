# Danskerdong bot

Bot der poster til Bluesky hver gang en dansker scorer, assisterer eller får rødt kort i en udenlandsk kamp.

- **Hvor kører den:** GitHub Actions (cron hvert 3. minut)
- **Data-kilde:** Fotmob
- **Poster til:** [@danskerdong.bsky.social](https://bsky.app/profile/danskerdong.bsky.social)

## Filer

- `main.py` — bot-scriptet
- `danish_players.json` — liste over danskerne botten følger
- `state.json` — husker hvilke events der allerede er postet
- `requirements.txt` — Python-afhængigheder
- `.github/workflows/bot.yml` — cron-jobbet

## Secrets der skal være sat

I Settings → Secrets and variables → Actions:

- `BLUESKY_HANDLE` — fx `danskerdong.bsky.social`
- `BLUESKY_PASSWORD` — app-password fra Bluesky (ikke dit rigtige password)

## Første kørsel

Den første kørsel er en bootstrap: botten markerer alle nuværende events som set, uden at poste. Fra anden kørsel og frem poster den live.

## Tilføj spillere

Rediger `danish_players.json` direkte på GitHub. Husk alias'er til navne-matchning.
