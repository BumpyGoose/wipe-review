"""Wipe analysis: pulls one boss pull's deaths and the events around them and
turns them into per-player feedback lines.

Entry points: report_code, get_report_fights, review_pull, discover.
Output is a list of Line(text, tag); the tag drives the colour in the UI
(header, kill, info, dim, warn, bad, normal).
"""

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DIFFICULTY = {1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}


@dataclass
class Line:
    text: str
    tag: str = "normal"


def load_data(name):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def report_code(url_or_code):
    """'https://www.warcraftlogs.com/reports/AbC123#fight=4' or 'AbC123' -> 'AbC123'."""
    s = (url_or_code or "").strip()
    m = re.search(r"reports/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9]{8,}", s) else None


def fmt_time(ms):
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


def fmt_amount(n):
    n = float(n or 0)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


# ---------------------------------------------------------------------------
# Report polling
# ---------------------------------------------------------------------------
def get_report_fights(client, code):
    q = """
query($code: String!) {
  reportData { report(code: $code) {
    title startTime endTime
    fights(killType: Encounters) { id encounterID name kill difficulty startTime endTime fightPercentage lastPhase }
  } }
  rateLimitData { limitPerHour pointsSpentThisHour }
}"""
    data = client.query(q, {"code": code})
    report = data["reportData"]["report"]
    if report is None:
        raise ValueError(f"Report {code} not found (or it's private).")
    report["rateLimit"] = data["rateLimitData"]
    return report


def is_reviewable(fight, include_kills):
    return fight.get("encounterID", 0) > 0 and fight.get("difficulty", 99) <= 5 and (include_kills or not fight.get("kill"))


# ---------------------------------------------------------------------------
# One pull's review
# ---------------------------------------------------------------------------
@dataclass
class PullContext:
    code: str
    fight: dict
    actors: dict
    abilities: dict
    pull_number: int
    players: list = field(default_factory=list)

    def ability(self, gid):
        return self.abilities.get(int(gid or 0)) or f"spell {gid}"

    def name(self, actor_id):
        a = self.actors.get(int(actor_id))
        return a["name"] if a else f"#{actor_id}"


def get_pull_context(client, code, fight_id):
    q = """
query($code: String!, $fid: [Int]) { reportData { report(code: $code) {
  fights(fightIDs: $fid) { id encounterID name kill difficulty startTime endTime fightPercentage bossPercentage lastPhase friendlyPlayers }
  all: fights(killType: Encounters) { id encounterID difficulty }
  masterData { actors(type: "Player") { id name server subType icon } abilities { gameID name } }
} } }"""
    report = client.query(q, {"code": code, "fid": [fight_id]})["reportData"]["report"]
    if not report or not report["fights"]:
        raise ValueError(f"Fight {fight_id} not found in report {code}.")
    fight = report["fights"][0]
    actors = {a["id"]: a for a in report["masterData"]["actors"]}
    abilities = {a["gameID"]: a["name"] for a in report["masterData"]["abilities"]}
    pull_number = sum(1 for f in report["all"]
                      if f["encounterID"] == fight["encounterID"] and f["difficulty"] == fight["difficulty"] and f["id"] <= fight["id"])
    players = [actors[p] for p in fight.get("friendlyPlayers") or [] if p in actors]
    return PullContext(code, fight, actors, abilities, pull_number, players)


def spec_of(actor):
    icon = actor.get("icon") or ""
    return icon.split("-", 1)[1] if "-" in icon else ""


def player_label(actor):
    cls = re.sub(r"([a-z])([A-Z])", r"\1 \2", actor.get("subType") or "")
    spec = spec_of(actor)
    return f"{actor['name']} ({spec} {cls})" if spec else f"{actor['name']} ({cls})"


def defensive_list(defs, actor):
    spec = spec_of(actor)
    return [d for d in defs["classes"].get(actor.get("subType"), []) if not d.get("specs") or spec in d["specs"]]


def death_detail(ctx, death, ev, rules, raid_has_warlock, defs):
    """What killed them, how fast, what was up, what wasn't pressed."""
    pid = death["targetID"]
    actor = ctx.actors[pid]
    t = death["timestamp"]
    start = ctx.fight["startTime"]
    hits = sorted((e for e in ev.get(f"dmg{pid}", []) if e.get("type") == "damage" and e.get("targetID") == pid),
                  key=lambda e: e["timestamp"])
    casts = [e for e in ev.get(f"cd{pid}", []) if e.get("type") == "cast"]
    debuffs = sorted(ev.get(f"db{pid}", []), key=lambda e: e["timestamp"])
    lines = []
    killer = ctx.ability(death.get("killingAbilityGameID"))
    last = hits[-1] if hits else None

    # How fast did they go down? Find the last moment they were at >= 90% hp
    # (pre-hit hp = hp after the hit + the hit itself).
    top_pct = top_at = None
    for h in hits:
        if not h.get("maxHitPoints"):
            continue
        pre = (h.get("hitPoints", 0) + h.get("amount", 0)) / h["maxHitPoints"] * 100
        if pre >= 90:
            top_pct, top_at = pre, h["timestamp"]
    if top_at is None:
        first = next((h for h in hits if h.get("maxHitPoints")), None)
        if first:
            top_at = first["timestamp"]
            top_pct = (first.get("hitPoints", 0) + first.get("amount", 0)) / first["maxHitPoints"] * 100
    window = (t - top_at) / 1000 if top_at is not None else None
    one_shot = window is not None and window <= 1.5

    if window is None:
        speed = "no damage events in the last 10s"
    elif one_shot:
        speed = f"from {top_pct:.0f}% in {window:.1f}s (one-shot)"
    else:
        speed = f"from {top_pct:.0f}% over {window:.1f}s"
    overkill = f", overkill {fmt_amount(last['overkill'])}" if last and last.get("overkill") else ""
    lines.append(Line(f"  {fmt_time(t - start)}  {player_label(actor)} - killed by {killer}, {speed}{overkill}", "normal"))

    # Damage breakdown since they were last healthy.
    since = top_at if top_at is not None else t - 5000
    recent = [h for h in hits if h["timestamp"] >= since]
    if recent:
        totals, counts = Counter(), Counter()
        for h in recent:
            totals[h["abilityGameID"]] += h.get("amount", 0)
            counts[h["abilityGameID"]] += 1
        parts = [f"{ctx.ability(gid)} {fmt_amount(total)}" + (f" ({counts[gid]}x)" if counts[gid] > 1 else "")
                 for gid, total in totals.most_common(4)]
        lines.append(Line("        took: " + ", ".join(parts), "dim"))

    # Boss debuffs on them at death.
    active = {}
    for e in debuffs:
        if e.get("targetID") != pid or e["timestamp"] > t:
            continue
        if e["type"].startswith("apply"):
            active[e["abilityGameID"]] = e["timestamp"]
        elif e["type"] == "removedebuff":
            active.pop(e["abilityGameID"], None)
    if active:
        names = [f"{ctx.ability(gid)} ({(t - at) / 1000:.1f}s)" for gid, at in sorted(active.items(), key=lambda kv: kv[1])]
        lines.append(Line("        debuffs at death: " + ", ".join(names), "dim"))

    # Defensives/externals active at the killing blow (auras listed on the damage event).
    my_defs = defensive_list(defs, actor)
    def_names = {d["name"] for d in my_defs} | set(defs.get("externals", []))
    auras = [ctx.ability(a) for a in (last.get("buffs") or "").split(".") if a] if last else []
    up = list(dict.fromkeys(a for a in auras if a in def_names))
    if up:
        lines.append(Line("        died with active: " + ", ".join(up), "dim"))

    # Defensives ready but not pressed. Talented ones only count once the
    # player has been seen casting them somewhere in the report.
    ready = []
    for d in my_defs:
        if d["name"] in up:
            continue
        mine = [c for c in casts if ctx.ability(c["abilityGameID"]) == d["name"]]
        if not d.get("baseline") and not mine:
            continue
        in_pull = sorted(c["timestamp"] for c in mine if start <= c["timestamp"] <= t)
        if not in_pull:
            ready.append(f"{d['name']} (not used this pull)")
        elif (t - in_pull[-1]) / 1000 >= d["cd"]:
            ready.append(f"{d['name']} (last used {(t - in_pull[-1]) / 1000:.0f}s ago)")
    if raid_has_warlock and not one_shot:
        stone = defs.get("healthstone", "Healthstone")
        if not any(ctx.ability(c["abilityGameID"]) == stone and start <= c["timestamp"] <= t for c in casts):
            ready.append(stone)
    if ready:
        verb = "had ready (would need pre-using - it was a one-shot)" if one_shot else "didn't press"
        lines.append(Line(f"        ! {verb}: " + ", ".join(ready), "warn"))

    # Avoidable damage in the run-up.
    avoid_ids = {a["id"] for a in rules.get("avoidable", [])}
    avoid_totals = Counter()
    for h in recent:
        if h["abilityGameID"] in avoid_ids:
            avoid_totals[h["abilityGameID"]] += h.get("amount", 0)
    if avoid_totals:
        lines.append(Line("        ! avoidable damage before death: " +
                          ", ".join(f"{ctx.ability(g)} ({fmt_amount(v)})" for g, v in avoid_totals.items()), "bad"))
    return lines


def review_pull(client, code, fight_id, detail_deaths=8):
    ctx = get_pull_context(client, code, fight_id)
    f = ctx.fight
    start, end = f["startTime"], f["endTime"]
    rules = load_data("bosses.json").get(str(f["encounterID"])) or {}  # re-read each pull so mid-raid edits apply
    defs = load_data("defensives.json")
    player_ids = {p["id"] for p in ctx.players}
    out = []

    # --- pass 1: deaths + boss rule events --------------------------------
    base = {"start": start, "end": end, "fightIDs": [fight_id]}
    items = [{**base, "alias": "deaths", "dataType": "Deaths"}]
    avoid_ids = [a["id"] for a in rules.get("avoidable", [])]
    debuff_rules = rules.get("debuffs", [])
    kick_rules = rules.get("interrupts", [])
    if avoid_ids:
        items.append({**base, "alias": "avoid", "dataType": "DamageTaken", "filter": f"ability.id in ({','.join(map(str, avoid_ids))})"})
    if debuff_rules:
        items.append({**base, "alias": "mechDebuffs", "dataType": "Debuffs",
                      "filter": f"ability.id in ({','.join(str(r['id']) for r in debuff_rules)})"})
        press_ids = sorted({p for r in debuff_rules for p in r.get("press", [])})
        if press_ids:
            items.append({**base, "alias": "mechPress", "dataType": "Casts", "filter": f"ability.id in ({','.join(map(str, press_ids))})"})
    if kick_rules:
        items.append({**base, "alias": "kicks", "dataType": "Casts", "hostility": "Enemies",
                      "filter": f"ability.id in ({','.join(str(r['id']) for r in kick_rules)})"})
    ev = client.events(code, items)
    deaths = sorted((d for d in ev["deaths"] if d.get("targetID") in player_ids), key=lambda d: d["timestamp"])

    # --- header ------------------------------------------------------------
    result = "KILL" if f.get("kill") else "WIPE"
    pct = "" if f.get("kill") else f" | boss {f.get('bossPercentage') or 0:.1f}%"
    phase = f" | phase {f['lastPhase']}" if f.get("lastPhase") else ""
    out.append(Line(""))
    out.append(Line(f"=== {result} - {f['name']} {DIFFICULTY.get(f['difficulty'], '')} pull #{ctx.pull_number} | "
                    f"{fmt_time(end - start)}{pct}{phase} | {len(player_ids)} players ===", "kill" if f.get("kill") else "header"))

    if not deaths:
        out.append(Line("  No player deaths."))
    else:
        first = deaths[0]
        out.append(Line(f"  First death {fmt_time(first['timestamp'] - start)}: {ctx.name(first['targetID'])} to "
                        f"{ctx.ability(first.get('killingAbilityGameID'))}. {len(deaths)} deaths in total, "
                        f"last at {fmt_time(deaths[-1]['timestamp'] - start)}."))
        by_killer = defaultdict(list)
        for d in deaths:
            by_killer[d.get("killingAbilityGameID")].append(d["timestamp"] - start)
        top = sorted(by_killer.items(), key=lambda kv: -len(kv[1]))[:3]
        parts = []
        for gid, ts in top:
            span = f"{fmt_time(ts[0])}-{fmt_time(ts[-1])}" if len(ts) > 1 else fmt_time(ts[0])
            parts.append(f"{ctx.ability(gid)} x{len(ts)} ({span})")
        out.append(Line("  Killed by: " + ", ".join(parts)))

        # --- pass 2: full detail for the first N deaths (first death per player)
        detailed, seen = [], set()
        for d in deaths[:detail_deaths]:
            if d["targetID"] not in seen:
                seen.add(d["targetID"])
                detailed.append(d)
        items = []
        for d in detailed:
            pid, t = d["targetID"], d["timestamp"]
            names = [x["name"] for x in defensive_list(defs, ctx.actors[pid])] + [defs.get("healthstone", "Healthstone")]
            name_filter = "ability.name in (" + ", ".join('"' + n.replace('"', '\\"') + '"' for n in names) + ")"
            window = {"start": t - 10000, "end": t + 50, "fightIDs": [fight_id], "sourceID": pid}
            items += [
                {**window, "alias": f"dmg{pid}", "dataType": "DamageTaken", "resources": True},
                {**window, "alias": f"db{pid}", "dataType": "Debuffs"},
                # whole report so far: proves which talented defensives they have
                {"alias": f"cd{pid}", "dataType": "Casts", "sourceID": pid, "start": 0, "end": t, "filter": name_filter},
            ]
        detail_ev = client.events(code, items)
        raid_has_warlock = any(p.get("subType") == "Warlock" for p in ctx.players)

        out.append(Line(""))
        out.append(Line("  Deaths:", "info"))
        for d in detailed:
            out += death_detail(ctx, d, detail_ev, rules, raid_has_warlock, defs)
        rest = deaths[detail_deaths:]
        if rest:
            out.append(Line(f"  ...then {len(rest)} more: " + ", ".join(
                f"{ctx.name(d['targetID'])} {fmt_time(d['timestamp'] - start)} ({ctx.ability(d.get('killingAbilityGameID'))})"
                for d in rest), "dim"))

    # --- avoidable damage, whole raid --------------------------------------
    avoid = [e for e in ev.get("avoid", []) if e.get("type") == "damage" and e.get("targetID") in player_ids]
    if avoid:
        out.append(Line(""))
        out.append(Line("  Avoidable damage:", "info"))
        per_player = defaultdict(lambda: defaultdict(list))
        for e in avoid:
            per_player[e["targetID"]][e["abilityGameID"]].append(e.get("amount", 0))
        for pid, abil in sorted(per_player.items(), key=lambda kv: -sum(sum(v) for v in kv[1].values())):
            parts = [f"{ctx.ability(g)} x{len(v)} ({fmt_amount(sum(v))})" for g, v in abil.items()]
            out.append(Line(f"    {ctx.name(pid):<14} {', '.join(parts)}", "bad"))

    # --- mechanic rules ----------------------------------------------------
    mech = []
    for r in debuff_rules:
        note = f" ({r['note']})" if r.get("note") else ""
        events = [e for e in ev.get("mechDebuffs", []) if e["abilityGameID"] == r["id"]]
        applies = sorted((e for e in events if e["type"] == "applydebuff" and e.get("targetID") in player_ids), key=lambda e: e["timestamp"])
        fails = []
        for a in applies:
            who, at = a["targetID"], a["timestamp"]
            failed = None
            if r.get("press"):
                win = r.get("within", 5) * 1000
                if not any(c.get("sourceID") == who and c["abilityGameID"] in r["press"] and at <= c["timestamp"] <= at + win
                           for c in ev.get("mechPress", [])):
                    failed = f"didn't press {r.get('pressLabel', 'the button')} within {r.get('within', 5)}s"
            if r.get("removedWithin"):
                win = r["removedWithin"] * 1000
                if not any(e["type"] == "removedebuff" and e.get("targetID") == who and at <= e["timestamp"] <= at + win for e in events):
                    failed = f"wasn't cleared within {r['removedWithin']}s"
            if failed:
                died = next((d for d in deaths if d["targetID"] == who and at <= d["timestamp"] <= at + 15000), None)
                fails.append({"at": at, "name": ctx.name(who), "failed": failed,
                              "died_after": (died["timestamp"] - at) / 1000 if died else None})
        # Failures within 2s of each other are one cast of the mechanic; 5+
        # players failing it together is a raid-wide problem, so say it once.
        i = 0
        while i < len(fails):
            j = i
            while j + 1 < len(fails) and fails[j + 1]["at"] - fails[i]["at"] <= 2000:
                j += 1
            group = fails[i:j + 1]
            if len(group) >= 5:
                dead = [x["name"] for x in group if x["died_after"] is not None]
                tail = f" - {len(dead)} died after: {', '.join(dead)}" if dead else ""
                mech.append(Line(f"    {fmt_time(group[0]['at'] - start)}  {r['name']} on {len(group)} players - {group[0]['failed']}{tail}{note}", "warn"))
            else:
                for x in group:
                    tail = f" - died {x['died_after']:.1f}s later" if x["died_after"] is not None else ""
                    mech.append(Line(f"    {fmt_time(x['at'] - start)}  {x['name']} got {r['name']} and {x['failed']}{tail}{note}", "warn"))
            i = j + 1
    for r in kick_rules:
        through = [e for e in ev.get("kicks", []) if e["abilityGameID"] == r["id"] and e["type"] == "cast"]
        if through:
            mech.append(Line(f"    {r['name']} went through uninterrupted {len(through)}x "
                             f"({', '.join(fmt_time(e['timestamp'] - start) for e in through)})", "warn"))
    if mech:
        out.append(Line(""))
        out.append(Line("  Missed mechanics:", "info"))
        out += mech
    if not (avoid_ids or debuff_rules or kick_rules):
        out.append(Line(""))
        out.append(Line(f"  (No rules for encounter {f['encounterID']} yet - run discover on a pull to list its abilities, "
                        "then add them to data/bosses.json.)", "dim"))
    return out


# ---------------------------------------------------------------------------
# Discovery: list a boss's abilities to help write data/bosses.json
# ---------------------------------------------------------------------------
def discover(client, code, fight_ids):
    q = """
query($code: String!, $fid: [Int]) { reportData { report(code: $code) {
  fights(fightIDs: $fid) { id encounterID name }
  dt: table(dataType: DamageTaken, fightIDs: $fid, viewBy: Ability, hostilityType: Friendlies)
  db: table(dataType: Debuffs, fightIDs: $fid, hostilityType: Friendlies)
  ec: table(dataType: Casts, fightIDs: $fid, hostilityType: Enemies)
  deaths: table(dataType: Deaths, fightIDs: $fid)
} } }"""
    rep = client.query(q, {"code": code, "fid": list(fight_ids)})["reportData"]["report"]
    if not rep or not rep["fights"]:
        raise ValueError("No such fights in that report.")
    f = rep["fights"][0]
    out = [Line(f"=== {f['name']} (encounter {f['encounterID']}) - {len(rep['fights'])} pull(s) ===", "header")]

    killers = Counter(d["killingBlow"]["guid"] for d in rep["deaths"]["data"].get("entries", []) if d.get("killingBlow"))

    out += [Line(""), Line("  Enemy damage taken by players (id, name, total, hits, deaths caused):", "info")]
    enemy = [e for e in rep["dt"]["data"].get("entries", [])
             if e.get("actorType") in ("Boss", "NPC") or any(s.get("type") in ("Boss", "NPC") for s in e.get("sources") or [])]
    for e in sorted(enemy, key=lambda e: -e.get("total", 0)):
        hits = sum(h.get("count", 0) for h in e.get("hitdetails") or [])
        killed = f"  killed {killers[e['guid']]}" if killers.get(e["guid"]) else ""
        out.append(Line(f"    {e['guid']:<9} {e['name']:<32} {fmt_amount(e.get('total')):>8} {hits:>6} hits{killed}"))

    out += [Line(""), Line("  Debuffs on players (id, name, applications):", "info")]
    for a in sorted(rep["db"]["data"].get("auras", []), key=lambda a: -a.get("totalUses", 0)):
        out.append(Line(f"    {a['guid']:<9} {a['name']:<32} {a.get('totalUses', 0):>5}"))

    out += [Line(""), Line("  Enemy casts (id, name, count, caster) - candidates for interrupts:", "info")]
    for c in rep["ec"]["data"].get("entries", []):
        for ab in c.get("abilities") or []:
            out.append(Line(f"    {ab['guid']:<9} {ab['name']:<32} {ab.get('total', 0):>5}  {c['name']}"))
    return out
