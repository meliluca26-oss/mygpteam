#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MyGPTeam -> data.json  (aggiornamento cloud, gira su GitHub Actions)

Fa login su MyGPTeam con le credenziali passate via variabili d'ambiente
(MYGP_USER / MYGP_PASS, prese dai GitHub Secrets), scarica i feed XML del
gioco e aggiorna data.json con le parti che cambiano ogni giorno
(finanze, piloti, budget, staff, ingegneri). Laboratori, ricerca e
strutture NON sono esposti via XML: vengono mantenuti dal data.json esistente.

La password non e' mai stampata nei log.
"""

import os, re, sys, json, time, html
import xml.etree.ElementTree as ET
import requests

BASE = "https://www.mygpteam.com"
LOGIN_URL = BASE + "/includes/login_ajax.php"
HOME_URL = BASE + "/default.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}

USER = os.environ.get("MYGP_USER", "").strip()
PASS = os.environ.get("MYGP_PASS", "")

if not USER or not PASS:
    print("ERRORE: mancano le variabili MYGP_USER / MYGP_PASS (GitHub Secrets).")
    sys.exit(1)


def die(msg):
    print("ERRORE: " + msg)
    sys.exit(1)


def login(session):
    # prende i cookie iniziali dalla landing
    session.get(BASE + "/", timeout=30)
    payload = {"usr": USER, "pwd": PASS, "statuslg": "Log in"}
    r = session.post(
        LOGIN_URL, data=payload, timeout=30,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/",
            "Origin": BASE,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    # la RISPOSTA non contiene la password (quella e' nella richiesta): stampiamo
    # stato + un breve estratto solo per diagnostica.
    body = (r.text or "").strip().replace("\n", " ")
    print("Login HTTP %s | server=%s | risposta[:120]=%r" % (
        r.status_code, r.headers.get("Server", "?"), body[:120]))


def get_xml(session, path):
    r = session.get(BASE + path, timeout=30,
                    headers={"Accept": "application/xml, text/xml, */*",
                             "Referer": HOME_URL})
    if r.status_code != 200:
        die("feed %s -> HTTP %s | server=%s (login fallito o WAF?)" % (
            path, r.status_code, r.headers.get("Server", "?")))
    txt = r.content.decode("iso-8859-1", "replace")
    if "<F1Project" not in txt:
        die("feed %s non valido: la risposta non e' XML (probabile login fallito)." % path)
    return ET.fromstring(txt.encode("iso-8859-1", "replace"))


def t(node, tag, default=None):
    el = node.find(tag)
    return el.text if el is not None and el.text is not None else default


def i(node, tag, default=0):
    v = t(node, tag, None)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


# --- mappatura livello "lavoro di gruppo" (scala 1-20) per i livelli noti ---
SPIRIT_WORDS = {9: "eccezionale", 10: "fantastico", 11: "meraviglioso"}


def spirit_label(n):
    w = SPIRIT_WORDS.get(n)
    return (w + " [%d]" % n) if w else ("livello %d" % n)


def funding_tier(euro):
    tiers = [0, 1000, 5000, 15000, 25000, 50000]
    return tiers.index(euro) if euro in tiers else 0


def get_xml_url(session, url):
    r = session.get(url, timeout=30,
                    headers={"Accept": "application/xml, text/xml, */*", "Referer": HOME_URL})
    if r.status_code != 200:
        return None
    txt = r.content.decode("iso-8859-1", "replace")
    if "<F1Project" not in txt or "<Error>" in txt:
        return None
    return ET.fromstring(txt.encode("iso-8859-1", "replace"))


def fetch_last_race(session, team_name):
    """Legge l'ultima gara disputata (griglia = qualifica, classifica, vincitore, giro veloce)."""
    try:
        home = session.get(HOME_URL, timeout=30).content.decode("iso-8859-1", "replace")
    except Exception as ex:
        print("gara: home non leggibile (%s)" % ex)
        return None
    m = re.search(r"Ultima gara.{0,1200}?(?:viewRace\.php\?id=|ID:\s*)(\d+)", home, re.S)
    if not m:
        print("gara: id ultima gara non trovato nella home.")
        return None
    rid = m.group(1)
    root = get_xml_url(session, BASE + "/xml/gara.php?lingua=1&idgara=" + rid)
    race = root.find(".//Race") if root is not None else None
    if race is None:
        print("gara: feed gara %s non disponibile." % rid)
        return None

    grid = []
    grid_el = race.find("StartingGrid")
    if grid_el is not None:
        for idx, car in enumerate(grid_el.findall("Car")):
            grid.append({"pos": idx + 1, "driver": t(car, "Driver"), "team": t(car, "Team")})
    grid_pos = {(g["driver"] or ""): g["pos"] for g in grid}

    winner, best, classification = {}, {}, []
    fin = race.find("Finish")
    if fin is not None:
        w, b, pos = fin.find("Winner"), fin.find("BestLap"), fin.find("Positions")
        if w is not None:
            winner = {"driver": t(w, "Driver"), "time": t(w, "RaceTime")}
        if b is not None:
            best = {"driver": t(b, "Driver"), "time": t(b, "LapTime")}
        if pos is not None:
            for idx, car in enumerate(pos.findall("Car")):
                drv = t(car, "Driver")
                classification.append({
                    "pos": idx + 1, "driver": drv, "team": t(car, "Team"),
                    "grid": grid_pos.get(drv or "", None),
                    "gap": t(car, "GapTime"), "pit": i(car, "NumPitStop"),
                })

    finished = {c["driver"] for c in classification}
    mine = [dict(c) for c in classification if (c["team"] or "") == team_name]
    for g in grid:  # nostri piloti ritirati: in griglia ma non a punti
        if (g["team"] or "") == team_name and g["driver"] not in finished:
            mine.append({"pos": None, "driver": g["driver"], "team": g["team"],
                         "grid": g["pos"], "gap": None, "pit": None})

    return {
        "id": rid, "name": t(race, "RaceName"), "track": t(race, "TrackName"),
        "laps": i(race, "Laps"), "date": t(race, "RaceDate"),
        "winner": winner, "bestLap": best,
        "classification": classification, "mine": mine,
    }


def skill_optval(stored):
    """Skill nostra (0-based) -> valore per il tool ufficiale Setup your car (0-200)."""
    try:
        return int(round(int(stored) * 200.0 / 19.0))
    except (TypeError, ValueError):
        return 0


def meteo_code(alt):
    a = (alt or "").lower()
    if "pioggia" in a and ("lieve" in a or "debole" in a):
        return 200, "pioggia lieve"
    if "pioggia" in a:
        return 400, "pioggia"
    return 0, "sole"


def official_setup(session, curve, disl, meteo_val, d):
    """Interroga il calcolatore UFFICIALE del gioco: ritorna i 4 settaggi lineari."""
    body = {
        "dislivelli": str(disl), "curve": str(curve), "meteo": str(meteo_val),
        "frenare": str(skill_optval(d.get("brake", 0))),
        "cambio": str(skill_optval(d.get("gear", 0))),
        "accelerare": str(skill_optval(d.get("accel", 0))),
        "traiettorie": str(skill_optval(d.get("traj", 0))),
        "_Next": "1",
    }
    try:
        r = session.post(BASE + "/setupYourCar.php", data=body, timeout=30,
                         headers={"Referer": BASE + "/setupYourCar.php"})
        raw = r.content.decode("iso-8859-1", "replace")
        raw = re.sub(r"<[^>]+>", " ", raw)            # via i tag: come il textContent del browser
        txt = re.sub(r"[ \t\r\n]+", " ", raw)
    except Exception as ex:
        print("setup: POST fallita (%s)" % ex)
        return None

    def g(term):
        m = re.search(term + r"\s*[:=]?\s*(\d{1,4})", txt)
        return int(m.group(1)) if m else None

    return {"coppia": g("Coppia"), "cambio": g("Rapporti"),
            "aero": g("Aerodinamica"), "pressione": g("Pressione")}


def fetch_next_setup(session, drivers):
    """Setup ufficiale (Coppia/Cambio/Aero/Pressione) per la prossima gara, per ogni pilota."""
    if not drivers:
        return None
    try:
        home = session.get(HOME_URL, timeout=30).content.decode("iso-8859-1", "replace")
    except Exception:
        return None
    m = re.search(r"Prossima gara.{0,1500}?viewRace\.php\?id=(\d+)", home, re.S)
    if not m:
        print("nextSetup: prossima gara non trovata nella home.")
        return None
    root = get_xml_url(session, BASE + "/xml/gara.php?lingua=1&idgara=" + m.group(1))
    race = root.find(".//Race") if root is not None else None
    if race is None:
        print("nextSetup: feed gara futura non disponibile.")
        return None
    trackid = i(race, "IdTrack")
    try:
        th = session.get(BASE + "/viewTrack.php?id=" + str(trackid), timeout=30).content.decode("iso-8859-1", "replace")
    except Exception:
        return None
    cm = re.search(r"curve[^\d]{0,80}(\d+)\s*%", th, re.S)
    dm = re.search(r"dislivelli[^\d]{0,80}(\d+)\s*%", th, re.S)
    if not cm or not dm:
        print("nextSetup: curve/dislivelli non letti dal circuito.")
        return None
    curve, disl = int(cm.group(1)), int(dm.group(1))
    qm = re.search(r"Meteo qualifica.{0,200}?alt=\"([^\"]+)\"", th, re.S)
    gm = re.search(r"Meteo gara.{0,200}?alt=\"([^\"]+)\"", th, re.S)
    codeQ, labQ = meteo_code(qm.group(1) if qm else "")
    codeG, labG = meteo_code(gm.group(1) if gm else "")

    out = []
    for d in drivers:
        out.append({
            "name": d.get("name"),
            "inputs": {
                "frenata": skill_optval(d.get("brake", 0)),
                "cambio": skill_optval(d.get("gear", 0)),
                "accelerazione": skill_optval(d.get("accel", 0)),
                "traiettorie": skill_optval(d.get("traj", 0)),
            },
        })
    return {
        "race": {"name": t(race, "RaceName"), "track": t(race, "TrackName"),
                 "laps": i(race, "Laps"), "date": t(race, "RaceDate"),
                 "curve": curve, "disl": disl,
                 "meteoQuali": labQ, "meteoGara": labG,
                 "meteoQualiVal": codeQ, "meteoGaraVal": codeG},
        "drivers": out,
    }


def fetch_facilities(session, prev_fac):
    """Legge i livelli delle strutture da facilities.php (server-rendered)."""
    try:
        html = session.get(BASE + "/facilities.php", timeout=30).content.decode("iso-8859-1", "replace")
    except Exception:
        return None
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    mapping = [
        ("Strutture tifosi", "Tifosi"), ("Progettazione/CAD", "Progettazione"),
        ("Simulatori avanzati", "Simulatori avanzati"), ("Banche dati", "Banche dati"),
        ("Banco prova motori", "Banco prova motori"), ("Carboceramiche (freni)", "Carboceramiche"),
        ("Titanio (trasmissione)", "Titanio"), ("Banco prova sospensioni", "Banco prova sospensioni"),
        ("Galleria del vento (aero)", "Galleria del vento"), ("Nanotecnologie (elettronica)", "Nanotecnologie"),
        ("Tracciato test (gomme)", "Tracciato test"),
    ]
    prevmap = {}
    if isinstance(prev_fac, list):
        for f in prev_fac:
            prevmap[f.get("name")] = f.get("level")
    out, found = [], False
    for app_name, game_name in mapping:
        m = re.search(re.escape(game_name) + r"\s*\(\s*(\d+)\s*\)", txt)
        if m:
            found = True
            out.append({"name": app_name, "level": int(m.group(1))})
        else:
            out.append({"name": app_name, "level": prevmap.get(app_name)})
    return out if found else None


def build(session, prev):
    piloti = get_xml(session, "/xml/piloti.php")
    eco = get_xml(session, "/xml/economia.php")
    staff = get_xml(session, "/xml/staff.php")
    ing = get_xml(session, "/xml/ingegneri.php")

    data = dict(prev) if isinstance(prev, dict) else {}

    # ---- piloti ----
    drivers = []
    for d in piloti.findall(".//Driver"):
        name = ((t(d, "DriverName", "") or "") + " " + (t(d, "DriverSurname", "") or "")).strip()
        drivers.append({
            "name": name,
            "age": i(d, "DriverAge"),
            "exp": i(d, "DriverExperience"),
            "cour": max(0, i(d, "DriverCourage") - 1),
            "judg": max(0, i(d, "DriverJudiciousness") - 1),
            "cool": max(0, i(d, "DriverTemperament") - 1),
            "con": i(d, "DriverContract"),
            "sal": i(d, "DriverWage"),
            "brake": max(0, i(d, "DriverBraking") - 1),
            "gear": max(0, i(d, "DriverGearChange") - 1),
            "accel": max(0, i(d, "DriverAccelerating") - 1),
            "traj": max(0, i(d, "DriverFollowTrajectories") - 1),
        })
    if drivers:
        data["drivers"] = drivers

    # ---- economia / team / budget ----
    b = eco.find(".//Budget")
    if b is not None:
        team = dict(data.get("team", {}))
        team.setdefault("name", "Bronte Racing Team")
        team.setdefault("formula", "F4")
        team["cash"] = i(b, "BudgetCash")
        team["bank"] = i(b, "BudgetBank")
        avail = parse_availability(session)
        if avail is not None:
            team["avail"] = avail
        data["team"] = team

        data["budget"] = {
            "iSpon": i(b, "BudgetThisInSponsor"),
            "iTv": i(b, "BudgetThisInTV"),
            "iPub": i(b, "BudgetThisInAttendance"),
            "iPrize": i(b, "BudgetThisInBonuses") + i(b, "BudgetThisInTransfers"),
            "iMerch": i(b, "BudgetThisInMerchandising"),
            "eDrv": i(b, "BudgetThisOutWages"),
            "eEng": i(b, "BudgetThisOutStaff"),
            "eRes": i(b, "BudgetThisOutInvComponents") + i(b, "BudgetThisOutInvDrivers"),
            "eOth": i(b, "BudgetThisOutFacilities") + i(b, "BudgetThisOutInterests") + i(b, "BudgetThisOutMerchandising"),
        }

    # ---- staff ----
    s = staff.find(".//Staff")
    if s is not None:
        data["staff"] = {
            "lavoroGruppo": spirit_label(i(s, "StaffTeamSpirit")),
            "meccanici": i(s, "StaffMechanicians"),
            "tecnici": i(s, "StaffTechnics"),
            "pubblicitari": i(s, "StaffPR"),
            "osservatori": i(s, "StaffObservers"),
            "preparatori": i(s, "StaffFitnessCoaches"),
            "manutentori": i(s, "StaffRepairman"),
        }

    # ---- ingegneri ----
    engs = []
    for e in ing.findall(".//Engineer"):
        name = ((t(e, "EngineerName", "") or "") + " " + (t(e, "EngineerSurname", "") or "")).strip()
        spec = (t(e, "EngineerSpecialization", "") or "").strip()
        engs.append({
            "name": name,
            "age": i(e, "EngineerAge"),
            "comp": spec.capitalize(),
            "brav": i(e, "EngineerCapableness"),
            "mecc": i(e, "EngineerMechanics"),
            "innov": i(e, "EngineerInnovation"),
        })
    if engs:
        data["engineers"] = engs

    # ---- talent scout (aggiorna solo il tier nel research esistente) ----
    ts = piloti.find(".//TalentScout")
    if ts is not None:
        research = dict(data.get("research", {}))
        research["scout"] = funding_tier(i(ts, "TalentScoutFunding"))
        data["research"] = research

    # ---- ultima gara disputata (griglia/qualifica + classifica) ----
    team_name = data.get("team", {}).get("name", "Bronte Racing Team")
    race = fetch_last_race(session, team_name)
    if race:
        data["lastRace"] = race

    # ---- setup UFFICIALE (dal tool del gioco) per la prossima gara ----
    ns = fetch_next_setup(session, data.get("drivers", []))
    if ns:
        data["nextSetup"] = ns

    # ---- strutture (livelli) letti dal gioco ----
    fac = fetch_facilities(session, data.get("facilities"))
    if fac:
        data["facilities"] = fac

    # lo stamp NON va qui: viene aggiunto in main() solo se i dati sono cambiati,
    # cosi' un semplice ricontrollo orario non genera un commit inutile.
    data.pop("stamp", None)
    data.pop("pushedAt", None)
    return data


def parse_availability(session):
    """Legge 'Disponibilita prevista' dalla home (best-effort)."""
    try:
        r = session.get(HOME_URL, timeout=30)
        txt = r.content.decode("iso-8859-1", "replace")
        txt = html.unescape(txt)
        # es. "Disponibilita prevista -59.547 &euro;" -> cattura -59.547 fino al simbolo euro
        m = re.search(r"[Dd]isponibilit[^\d\-€]{0,40}?(-?[\d\.]+)\s*€", txt)
        if m:
            num = m.group(1).replace(".", "").strip()
            return int(num)
    except Exception as ex:
        print("avail: impossibile leggerla (%s), mantengo la precedente." % ex)
    return None


def main():
    prev = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                prev = json.load(f)
        except Exception as ex:
            print("data.json esistente non leggibile (%s), riparto pulito." % ex)

    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    login(s)
    data = build(s, prev)

    # confronto solo sui dati "veri" (escluso lo stamp): se identici, non riscrivo
    prev_core = {k: v for k, v in prev.items() if k not in ("stamp", "pushedAt")}
    if data == prev_core:
        print("Nessuna variazione nei dati: data.json invariato, nessun commit.")
        return

    stamp = int(time.time() * 1000)
    out = {"stamp": stamp, "pushedAt": stamp}
    out.update(data)
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("Dati CAMBIATI: data.json aggiornato. stamp=%s cash=%s bank=%s avail=%s piloti=%d" % (
        stamp, out.get("team", {}).get("cash"),
        out.get("team", {}).get("bank"), out.get("team", {}).get("avail"),
        len(out.get("drivers", [])),
    ))


if __name__ == "__main__":
    main()
