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
UA = "Mozilla/5.0 (compatible; MyGPTeam-updater/1.0)"

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
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"},
    )
    # non stampiamo il corpo (potrebbe contenere dati sensibili); solo lo stato
    print("Login HTTP %s (%d byte di risposta)" % (r.status_code, len(r.text or "")))


def get_xml(session, path):
    r = session.get(BASE + path, timeout=30)
    if r.status_code != 200:
        die("feed %s -> HTTP %s (login fallito o sessione scaduta?)" % (path, r.status_code))
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

    # ---- timestamp ----
    stamp = int(time.time() * 1000)
    data["stamp"] = stamp
    data["pushedAt"] = stamp
    return data


def parse_availability(session):
    """Legge 'Disponibilita prevista' dalla home (best-effort)."""
    try:
        r = session.get(HOME_URL, timeout=30)
        txt = r.content.decode("iso-8859-1", "replace")
        txt = html.unescape(txt)
        m = re.search(r"[Dd]isponibilit[\w\W]{0,40}?(-?\d[\d\.\s]*)\s*", txt)
        if m:
            num = m.group(1).replace(".", "").replace(" ", "").strip()
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
    s.headers.update({"User-Agent": UA})
    login(s)
    data = build(s, prev)

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    d0 = data.get("drivers", [{}])[0] if data.get("drivers") else {}
    print("OK: data.json aggiornato. stamp=%s cash=%s bank=%s avail=%s piloti=%d" % (
        data.get("stamp"), data.get("team", {}).get("cash"),
        data.get("team", {}).get("bank"), data.get("team", {}).get("avail"),
        len(data.get("drivers", [])),
    ))


if __name__ == "__main__":
    main()
