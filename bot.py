"""
Bot Telegram NHL : compos probables, analyse de match, joueurs en forme.
Lancé toutes les 15 min par GitHub Actions (voir nhl.yml). Sans serveur.

Sources :
- API officielle NHL (api-web.nhle.com) : calendrier, classement, stats, game logs
  et COMPOSITIONS OFFICIELLES (joueurs habillés, publiés environ 1 h avant le match)
Aucune notification automatique n'est envoyée avant la compo officielle.
"""
import json
import math
import os
import re
import datetime as dt
from zoneinfo import ZoneInfo

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API = "https://api-web.nhle.com/v1"
TG = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = "state.json"
ET = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nhl-telegram-bot)"}
WINDOW_BEFORE = 2.0   # on surveille les matchs qui commencent dans les 2 h
WINDOW_AFTER = 0.5    # et ceux commencés depuis moins de 30 min

# abréviation NHL -> slug DailyFaceoff
TEAMS = {
    "ANA": "anaheim-ducks", "BOS": "boston-bruins", "BUF": "buffalo-sabres",
    "CGY": "calgary-flames", "CAR": "carolina-hurricanes", "CHI": "chicago-blackhawks",
    "COL": "colorado-avalanche", "CBJ": "columbus-blue-jackets", "DAL": "dallas-stars",
    "DET": "detroit-red-wings", "EDM": "edmonton-oilers", "FLA": "florida-panthers",
    "LAK": "los-angeles-kings", "MIN": "minnesota-wild", "MTL": "montreal-canadiens",
    "NSH": "nashville-predators", "NJD": "new-jersey-devils", "NYI": "new-york-islanders",
    "NYR": "new-york-rangers", "OTT": "ottawa-senators", "PHI": "philadelphia-flyers",
    "PIT": "pittsburgh-penguins", "SEA": "seattle-kraken", "SJS": "san-jose-sharks",
    "STL": "st-louis-blues", "TBL": "tampa-bay-lightning", "TOR": "toronto-maple-leafs",
    "UTA": "utah-mammoth", "VAN": "vancouver-canucks", "VGK": "vegas-golden-knights",
    "WSH": "washington-capitals", "WPG": "winnipeg-jets",
}

_cache = {}


# ----------------------------------------------------------------- utilitaires
def get(url, as_text=False):
    if url in _cache:
        return _cache[url]
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        out = r.text if as_text else r.json()
    except Exception as e:  # réseau ou format : on continue sans planter
        print(f"[warn] {url} -> {e}")
        out = None
    _cache[url] = out
    return out


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text):
    for i in range(0, len(text), 3800):
        requests.post(
            f"{TG}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text[i:i + 3800],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("offset", 0)
    s.setdefault("teams", [])      # vide = toutes les équipes
    s.setdefault("official", [])   # ids de matchs dont la compo officielle a déjà été envoyée
    return s


def save_state(s):
    s["official"] = s["official"][-300:]
    s.pop("lines", None)
    s.pop("analysed", None)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1, sort_keys=True)


def resolve_team(arg):
    a = (arg or "").strip().lower()
    if not a:
        return None
    if a.upper() in TEAMS:
        return a.upper()
    for ab, slug in TEAMS.items():
        if a in slug.replace("-", " ") or a in slug:
            return ab
    return None


# --------------------------------------------------------------------- calendrier
def games_on(date):
    d = get(f"{API}/schedule/{date.isoformat()}") or {}
    out = []
    for day in d.get("gameWeek", []):
        if day.get("date") == date.isoformat():
            for g in day.get("games", []):
                if g.get("gameType") in (2, 3):  # saison régulière / séries
                    out.append(g)
    return out


def start_dt(g):
    return dt.datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00"))


def abbr(g, side):
    return g[side]["abbrev"]


def fmt_time(g):
    return start_dt(g).astimezone(PARIS).strftime("%a %d/%m %H:%M")


# ------------------------------------------------------------------ modèle de victoire
def standings():
    d = get(f"{API}/standings/now") or {}
    return {t["teamAbbrev"]["default"]: t for t in d.get("standings", [])}


def rating(t):
    """Note 0..1 : points%, forme sur 10 matchs, différentiel de buts. Ramenée vers 0.5 en début de saison."""
    if not t:
        return 0.5, {}
    gp = t.get("gamesPlayed", 0)
    if gp == 0:
        return 0.5, {}
    pp = t.get("pointPctg")
    if pp is None:
        pp = t.get("points", 0) / (2 * gp)
    l10 = t.get("l10GamesPlayed") or (
        t.get("l10Wins", 0) + t.get("l10Losses", 0) + t.get("l10OtLosses", 0))
    l10pp = (2 * t.get("l10Wins", 0) + t.get("l10OtLosses", 0)) / (2 * l10) if l10 else pp
    gd = t.get("goalDifferential", 0) / gp
    w = min(gp, 20) / 20  # fiabilité de l'échantillon
    r = (0.45 * (0.5 + (pp - 0.5) * w)
         + 0.30 * (0.5 + (l10pp - 0.5) * w)
         + 0.25 * (0.5 + max(-1, min(1, gd / 3)) * 0.5 * w))
    return r, {"pp": pp, "l10": l10pp, "gd": gd, "gp": gp,
               "rec": f"{t.get('wins', 0)}-{t.get('losses', 0)}-{t.get('otLosses', 0)}"}


def played_yesterday(team, date):
    d = get(f"{API}/club-schedule-season/{team}/now") or {}
    y = (date - dt.timedelta(days=1)).isoformat()
    return any(g.get("gameDate") == y for g in d.get("games", []))


def win_prob(g, st):
    h, a = abbr(g, "homeTeam"), abbr(g, "awayTeam")
    rh, ih = rating(st.get(h))
    ra, ia = rating(st.get(a))
    date = start_dt(g).astimezone(ET).date()
    adj = 0.03  # avantage de la glace
    b2b_h, b2b_a = played_yesterday(h, date), played_yesterday(a, date)
    if b2b_h:
        adj -= 0.02
    if b2b_a:
        adj += 0.02
    p_home = 1 / (1 + math.exp(-8 * (rh - ra + adj)))
    return p_home, ih, ia, b2b_h, b2b_a


# ------------------------------------------------------------------ joueurs en forme
def player_form(team, dressed=None):
    d = get(f"{API}/club-stats/{team}/now") or {}
    skaters = d.get("skaters", [])
    if dressed is not None:  # compo officielle connue : on ne garde que les joueurs qui jouent
        skaters = [p for p in skaters if p["playerId"] in dressed]
    skaters = sorted(skaters, key=lambda p: p.get("points", 0), reverse=True)[:8]
    out = []
    for p in skaters:
        log = (get(f"{API}/player/{p['playerId']}/game-log/now") or {}).get("gameLog", [])[:5]
        if not log:
            continue
        g5 = sum(x.get("goals", 0) for x in log)
        pts5 = sum(x.get("points", 0) for x in log)
        sh5 = sum(x.get("shots", 0) for x in log)
        gp = max(p.get("gamesPlayed", 1), 1)
        shots, goals = p.get("shots", 0), p.get("goals", 0)
        spg = 0.5 * (sh5 / len(log)) + 0.5 * (shots / gp)
        shp = (goals + 2) / (shots + 20)  # taux de tir régressé vers la moyenne
        out.append({
            "name": f"{p['firstName']['default'][0]}. {p['lastName']['default']}",
            "g5": g5, "pts5": pts5, "sh5": sh5, "n": len(log),
            "pg": 1 - math.exp(-spg * shp),
            "form": g5 * 2 + pts5 + sh5 * 0.25,
        })
    return out


def form_block(team, dressed=None):
    pl = player_form(team, dressed)
    if not pl:
        return f"<b>{team}</b> : pas assez de données récentes.\n"
    hot = sorted(pl, key=lambda x: x["form"], reverse=True)[:3]
    sc = sorted(pl, key=lambda x: x["pg"], reverse=True)[:2]
    s = f"<b>{team}</b>\n🔥 En forme (5 derniers matchs) :\n"
    for p in hot:
        s += f"• {esc(p['name'])} : {p['g5']} B, {p['pts5']} pts, {p['sh5']} tirs\n"
    s += "🎯 Buteurs probables :\n"
    for p in sc:
        s += f"• {esc(p['name'])} ≈ {round(p['pg'] * 100)} % de marquer\n"
    return s


# ------------------------------------------------------------------ compos officielles (API NHL)
POS_ORDER = [("C", "Centres"), ("L", "Ailiers G"), ("R", "Ailiers D"), ("D", "Défenseurs")]


def official_lineups(g):
    """Compos officielles (joueurs habillés) d'après l'API NHL.
    Retourne {abréviation: [joueurs]} pour les 2 équipes, ou None tant que ce n'est pas publié."""
    d = get(f"{API}/gamecenter/{g['id']}/play-by-play") or {}
    by_team = {}
    for s in d.get("rosterSpots") or []:
        by_team.setdefault(s.get("teamId"), []).append(s)
    out = {}
    for side in ("homeTeam", "awayTeam"):
        roster = by_team.get(g[side].get("id"), [])
        skaters = [s for s in roster if s.get("positionCode") != "G"]
        goalies = [s for s in roster if s.get("positionCode") == "G"]
        # une vraie compo : ~18 patineurs + 2 gardiens (au-delà de 23 = effectif complet, pas une compo)
        if len(skaters) < 16 or not goalies or len(roster) > 23:
            return None
        out[abbr(g, side)] = roster
    return out


def _pname(s):
    first = (s.get("firstName") or {}).get("default", "?")
    last = (s.get("lastName") or {}).get("default", "?")
    return f"#{s.get('sweaterNumber', '')} {first[0]}. {last}"


def lineup_message(g, lu):
    h, a = abbr(g, "homeTeam"), abbr(g, "awayTeam")
    s = f"✅ <b>Compos officielles : {a} @ {h}</b>\n{fmt_time(g)} (heure de Paris)\n"
    for team in (a, h):
        s += f"\n<b>{team}</b>\n"
        roster = lu[team]
        for code, label in POS_ORDER:
            ps = sorted((x for x in roster if x.get("positionCode") == code),
                        key=lambda x: x.get("sweaterNumber") or 0)
            if ps:
                s += f"{label} : " + ", ".join(esc(_pname(x)) for x in ps) + "\n"
        gs = sorted((x for x in roster if x.get("positionCode") == "G"),
                    key=lambda x: x.get("sweaterNumber") or 0)
        s += "🥅 Gardiens habillés : " + ", ".join(esc(_pname(x)) for x in gs) + "\n"
    return s


# ------------------------------------------------------------------ analyse d'un match
def analysis(g, st, dressed=None):
    h, a = abbr(g, "homeTeam"), abbr(g, "awayTeam")
    p_home, ih, ia, b2b_h, b2b_a = win_prob(g, st)
    fav = h if p_home >= 0.5 else a
    s = f"🏒 <b>{a} @ {h}</b> — {fmt_time(g)} (heure de Paris)\n\n"
    s += f"🔮 <b>Victoire probable : {h} {round(p_home * 100)} % / {a} {round((1 - p_home) * 100)} %</b>\n"
    s += f"Favori : {fav}\n"
    if ih and ia:
        s += (f"Bilan : {h} {ih['rec']} | {a} {ia['rec']}\n"
              f"Forme 10 derniers (% pts) : {h} {round(ih['l10'] * 100)} % | {a} {round(ia['l10'] * 100)} %\n"
              f"Diff. de buts/match : {h} {ih['gd']:+.2f} | {a} {ia['gd']:+.2f}\n")
    if b2b_h:
        s += f"⚠️ {h} joue un match consécutif (fatigue).\n"
    if b2b_a:
        s += f"⚠️ {a} joue un match consécutif (fatigue).\n"
    if (ih.get("gp", 0) if ih else 0) < 10 or (ia.get("gp", 0) if ia else 0) < 10:
        s += "ℹ️ Début de saison : échantillon faible, fiabilité réduite.\n"
    s += "\n" + form_block(a, (dressed or {}).get(a)) + "\n" + form_block(h, (dressed or {}).get(h))
    s += "\n<i>Estimation statistique, pas une certitude.</i>"
    return s


# ------------------------------------------------------------------ commandes Telegram
HELP = (
    "🏒 <b>Bot NHL</b>\n"
    "Alertes automatiques : uniquement quand la compo officielle est publiée (~1 h avant le match).\n"
    "/matchs : matchs du jour\n"
    "/analyse TOR : analyse du prochain match d'une équipe\n"
    "/abonner TOR : ne suivre que certaines équipes (par défaut : toutes)\n"
    "/desabonner TOR : retirer une équipe\n"
    "/equipes : voir tes équipes suivies\n"
    "Les réponses arrivent au prochain passage du bot (jusqu'à ~15 min)."
)


def next_game_for(team):
    today = dt.datetime.now(ET).date()
    for i in range(4):
        for g in games_on(today + dt.timedelta(days=i)):
            if team in (abbr(g, "homeTeam"), abbr(g, "awayTeam")):
                return g
    return None


def handle_commands(state):
    r = requests.get(f"{TG}/getUpdates", params={"offset": state["offset"], "timeout": 0}, timeout=20)
    for u in r.json().get("result", []):
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
            continue  # on ignore tout sauf toi
        parts = (msg.get("text") or "").split(maxsplit=1)
        if not parts:
            continue
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("/start", "/aide", "/help"):
            tg_send(HELP)
        elif cmd == "/matchs":
            today = dt.datetime.now(ET).date()
            gs = games_on(today)
            if not gs:
                tg_send("Aucun match NHL aujourd'hui.")
            else:
                tg_send("🏒 <b>Matchs du jour</b>\n" + "\n".join(
                    f"{fmt_time(g)} : {abbr(g, 'awayTeam')} @ {abbr(g, 'homeTeam')}" for g in gs))
        elif cmd == "/analyse":
            t = resolve_team(arg)
            if not t:
                tg_send("Équipe inconnue. Exemple : /analyse TOR")
                continue
            g = next_game_for(t)
            tg_send(analysis(g, standings()) if g else f"Pas de match de {t} dans les 3 prochains jours.")
        elif cmd == "/abonner":
            t = resolve_team(arg)
            if not t:
                tg_send("Équipe inconnue. Exemple : /abonner TOR")
                continue
            if t not in state["teams"]:
                state["teams"].append(t)
            tg_send(f"✅ Équipes suivies : {', '.join(sorted(state['teams']))}")
        elif cmd == "/desabonner":
            t = resolve_team(arg)
            if t in state["teams"]:
                state["teams"].remove(t)
            tg_send("Équipes suivies : " + (", ".join(sorted(state["teams"])) or "toutes"))
        elif cmd == "/equipes":
            tg_send("Équipes suivies : " + (", ".join(sorted(state["teams"])) or "toutes"))


# ------------------------------------------------------------------ boucle principale
def main():
    state = load_state()
    try:
        handle_commands(state)
    except Exception as e:
        print(f"[warn] commandes : {e}")

    now = dt.datetime.now(dt.timezone.utc)
    today = dt.datetime.now(ET).date()
    games = games_on(today) + games_on(today + dt.timedelta(days=1))
    followed = set(state["teams"]) or set(TEAMS)

    for g in games:
        try:
            h, a = abbr(g, "homeTeam"), abbr(g, "awayTeam")
            if h not in followed and a not in followed:
                continue
            gid = str(g["id"])
            if gid in state["official"]:
                continue
            delta = (start_dt(g) - now).total_seconds() / 3600
            if not (-WINDOW_AFTER < delta <= WINDOW_BEFORE):
                continue

            lu = official_lineups(g)
            if not lu:
                continue  # compo pas encore officielle : on ne dit rien

            dressed = {t: {x["playerId"] for x in r} for t, r in lu.items()}
            tg_send(lineup_message(g, lu))
            tg_send(analysis(g, standings(), dressed))
            state["official"].append(gid)
        except Exception as e:
            print(f"[warn] match {g.get('id')} : {e}")

    save_state(state)


if __name__ == "__main__":
    main()
