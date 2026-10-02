"""Wipe analysis: pulls one boss pull's deaths and the events around them and
turns them into per-player feedback lines.

Entry points: report_code, get_report_fights, review_pull, discover.
Output is a list of Line(text, tag); the tag drives the colour in the UI
(header, kill, info, dim, warn, bad, normal).
"""

import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def fmt_duration(ms):
    m = int(ms // 60000)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else fmt_time(ms)


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
    fights(killType: Encounters) { id encounterID name kill difficulty startTime endTime fightPercentage bossPercentage lastPhase }
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
    report_start: int = 0

    def ability(self, gid):
        return self.abilities.get(int(gid or 0)) or f"spell {gid}"

    def name(self, actor_id):
        a = self.actors.get(int(actor_id))
        return a["name"] if a else f"#{actor_id}"


def get_pull_context(client, code, fight_id):
    q = """
query($code: String!, $fid: [Int]) { reportData { report(code: $code) {
  startTime
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
    return PullContext(code, fight, actors, abilities, pull_number, players, report.get("startTime") or 0)


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
    ready, ready_names = [], []
    for d in my_defs:
        if d["name"] in up:
            continue
        mine = [c for c in casts if ctx.ability(c["abilityGameID"]) == d["name"]]
        if not d.get("baseline") and not mine:
            continue
        in_pull = sorted(c["timestamp"] for c in mine if start <= c["timestamp"] <= t)
        if not in_pull:
            ready.append(f"{d['name']} (not used this pull)")
            ready_names.append(d["name"])
        elif (t - in_pull[-1]) / 1000 >= d["cd"]:
            ready.append(f"{d['name']} (last used {(t - in_pull[-1]) / 1000:.0f}s ago)")
            ready_names.append(d["name"])
    if raid_has_warlock and not one_shot:
        stone = defs.get("healthstone", "Healthstone")
        if not any(ctx.ability(c["abilityGameID"]) == stone and start <= c["timestamp"] <= t for c in casts):
            ready.append(stone)
            ready_names.append(stone)
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
    return lines, {"ready": ready_names, "one_shot": one_shot}


@dataclass
class PullResult:
    """One reviewed pull: the text review plus the numbers the evening summary needs."""
    fight: dict
    pull_number: int
    lines: list
    deaths: list = field(default_factory=list)       # {"player", "ability", "t"} in order, t = ms into the pull
    unpressed: dict = field(default_factory=dict)    # player -> [defensive names] (non-one-shot detailed deaths)
    avoidable: dict = field(default_factory=dict)    # player -> {"hits", "amount"}
    mech_fails: Counter = field(default_factory=Counter)  # player -> missed mechanic count
    started_at: int = 0                                   # epoch ms the pull started


def review_pull(client, code, fight_id, detail_deaths=8):
    return analyze_pull(client, code, fight_id, detail_deaths).lines


def analyze_pull(client, code, fight_id, detail_deaths=8):
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
                    f"{fmt_time(end - start)}{pct}{phase} | {len(player_ids)} players | {len(deaths)} death{"s" if len(deaths) != 1 else ""} ===",
                    "kill" if f.get("kill") else "header"))
    res = PullResult(f, ctx.pull_number, out, started_at=ctx.report_start + start,
                     deaths=[{"player": ctx.name(d["targetID"]), "ability": ctx.ability(d.get("killingAbilityGameID")),
                              "t": d["timestamp"] - start} for d in deaths])

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
            dl, info = death_detail(ctx, d, detail_ev, rules, raid_has_warlock, defs)
            out += dl
            if info["ready"] and not info["one_shot"]:
                res.unpressed[ctx.name(d["targetID"])] = info["ready"]
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
            a = res.avoidable.setdefault(ctx.name(e["targetID"]), {"hits": 0, "amount": 0})
            a["hits"] += 1
            a["amount"] += e.get("amount", 0)
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
                res.mech_fails[ctx.name(who)] += 1
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
    return res


# ---------------------------------------------------------------------------
# Whole report: every pull, then an evening summary
# ---------------------------------------------------------------------------
def analyze_report(client, code, include_kills=True, detail_deaths=8, on_progress=None, on_result=None,
                   should_stop=None, workers=4):
    """Reviews every boss pull in a report (a few at a time). on_result gets
    each PullResult in pull order as soon as it and all earlier ones are done.
    Returns (report, [PullResult])."""
    report = get_report_fights(client, code)
    fights = [f for f in report.get("fights") or [] if is_reviewable(f, include_kills)]
    results, ready, next_i = {}, {}, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyze_pull, client, code, f["id"], detail_deaths): i for i, f in enumerate(fights)}
        for done_count, fut in enumerate(as_completed(futures), 1):
            if should_stop and should_stop():
                for other in futures:
                    other.cancel()
                break
            i = futures[fut]
            try:
                ready[i] = fut.result()
            except Exception as e:  # one bad pull shouldn't sink the whole report
                f = fights[i]
                ready[i] = PullResult(f, 0, [Line(f"=== WIPE - {f['name']} (fight {f['id']}) | review failed ==="),
                                             Line(f"  {e}", "bad")])
            if on_progress:
                on_progress(done_count, len(fights), fights[i])
            while next_i in ready:
                results[next_i] = ready.pop(next_i)
                if on_result:
                    on_result(results[next_i])
                next_i += 1
    return report, [results[i] for i in sorted(results)]


def summarize_report(report, results, include_kills=True, detail_deaths=8):
    """The evening at a glance: bosses, what killed people, who died first,
    who left defensives unpressed, avoidable damage and missed mechanics."""
    from datetime import datetime  # local: only the summary needs it

    boss_fights = [f for f in report.get("fights") or [] if f.get("encounterID", 0) > 0 and f.get("difficulty", 99) <= 5]
    kills = sum(1 for f in boss_fights if f.get("kill"))
    total_deaths = sum(len(r.deaths) for r in results)
    combat_ms = sum(f["endTime"] - f["startTime"] for f in boss_fights)
    span_ms = (boss_fights[-1]["endTime"] - boss_fights[0]["startTime"]) if boss_fights else 0
    date = datetime.fromtimestamp(report["startTime"] / 1000).strftime("%a %d %b %Y") if report.get("startTime") else ""
    out = [Line(f"=== REPORT - {report.get('title') or 'Report'} | {date} | {fmt_duration(span_ms)} raid | "
                f"{fmt_duration(combat_ms)} in combat | {len(boss_fights)} pulls | {kills} kills | "
                f"{len(boss_fights) - kills} wipes | {total_deaths} deaths ===", "header")]
    if not boss_fights:
        out.append(Line("  No boss pulls in this report."))
        return out

    # --- bosses ---------------------------------------------------------------
    out += [Line(""), Line("  Bosses:", "info")]
    bosses = {}
    for f in boss_fights:
        bosses.setdefault((f["encounterID"], f["difficulty"]), []).append(f)
    for (_, diff), pulls in bosses.items():
        name = f"{pulls[0]['name']} {DIFFICULTY.get(diff, '')}"
        time_in = fmt_time(sum(p["endTime"] - p["startTime"] for p in pulls))
        kill_at = next((i for i, p in enumerate(pulls, 1) if p.get("kill")), None)
        wipes = [p for p in pulls if not p.get("kill")]
        if kill_at:
            verdict = f"killed on pull {kill_at}"
            tag = "normal"
        else:
            best = min((p.get("bossPercentage") if p.get("bossPercentage") is not None else 100) for p in wipes)
            verdict = f"not killed, best {best:.1f}%"
            tag = "warn"
        out.append(Line(f"    {name} - {len(pulls)} pull{'s' if len(pulls) != 1 else ''}, {verdict}, {time_in} in combat", tag))

    if not results:
        out.append(Line(""))
        out.append(Line("  No pulls were reviewed." + ("" if include_kills else " (Only wipes are reviewed - turn on Include kills to count kills too.)"), "dim"))
        return out

    # --- what killed people ------------------------------------------------------
    by_ability, started = Counter(), Counter()
    for r in results:
        for d in r.deaths:
            by_ability[d["ability"]] += 1
        if r.deaths and not r.fight.get("kill"):
            started[r.deaths[0]["ability"]] += 1
    out += [Line(""), Line("  What killed people:", "info")]
    for ability, n in by_ability.most_common(6):
        first = f", first death of {started[ability]} wipe{'s' if started[ability] != 1 else ''}" if started[ability] else ""
        out.append(Line(f"    {ability} - {n} death{'s' if n != 1 else ''}{first}"))

    # --- how each wipe started -------------------------------------------------
    wipe_results = [r for r in results if not r.fight.get("kill") and r.deaths]
    if wipe_results:
        out += [Line(""), Line("  How each wipe started:", "info")]
        for r in wipe_results:
            d0 = r.deaths[0]
            pct = r.fight.get("bossPercentage")
            pct = f" at {pct:.1f}%" if pct is not None else ""
            out.append(Line(f"    {r.fight['name']} #{r.pull_number}{pct} - {d0['player']} to {d0['ability']} at {fmt_time(d0['t'])}"))

    # --- players ----------------------------------------------------------------
    players = defaultdict(lambda: {"deaths": 0, "first": 0, "early": 0, "on_kill": 0, "unpressed": 0, "hits": 0, "amount": 0, "mech": 0})
    unpressed_names = Counter()
    for r in results:
        for i, d in enumerate(r.deaths):
            p = players[d["player"]]
            p["deaths"] += 1
            if i == 0:
                p["first"] += 1
            if i < 3:
                p["early"] += 1
            if r.fight.get("kill"):
                p["on_kill"] += 1
        for name, defs_ready in r.unpressed.items():
            players[name]["unpressed"] += 1
            unpressed_names.update(defs_ready)
        for name, a in r.avoidable.items():
            players[name]["hits"] += a["hits"]
            players[name]["amount"] += a["amount"]
        for name, n in r.mech_fails.items():
            players[name]["mech"] += n

    # Every wipe ends with everyone dead, so plain death counts don't rank anyone;
    # dying first or early, dying on a kill, and avoidable mistakes do.
    def score(p):
        return p["first"] * 3 + p["early"] * 2 + p["on_kill"] * 2 + p["unpressed"] * 2 + p["mech"] * 2 + p["hits"]

    ranked = sorted(((n, p) for n, p in players.items() if score(p)), key=lambda kv: -score(kv[1]))
    out += [Line(""), Line(f"  Players (most to fix first, {len(ranked)} of {len(players)} with something to look at):", "info")]
    for name, p in ranked[:12]:
        parts = [f"{p['deaths']} death{'s' if p['deaths'] != 1 else ''}"]
        if p["first"]:
            parts.append(f"died first {p['first']}x")
        if p["early"] > p["first"]:
            parts.append(f"in the first 3 deaths {p['early']}x")
        if p["on_kill"]:
            parts.append(f"died on a kill {p['on_kill']}x")
        if p["unpressed"]:
            parts.append(f"died with a defensive unpressed {p['unpressed']}x")
        if p["hits"]:
            parts.append(f"{p['hits']} avoidable hit{'s' if p['hits'] != 1 else ''} ({fmt_amount(p['amount'])})")
        if p["mech"]:
            parts.append(f"missed {p['mech']} mechanic{'s' if p['mech'] != 1 else ''}")
        tag = "warn" if p["first"] >= 2 or p["unpressed"] >= 2 or p["mech"] >= 2 else "normal"
        out.append(Line(f"    {name} - {', '.join(parts)}", tag))
    if len(ranked) > 12:
        out.append(Line(f"    ...and {len(ranked) - 12} more with smaller issues.", "dim"))

    if unpressed_names:
        out += [Line(""), Line("  Defensives most often left unpressed:", "info"),
                Line("    " + ", ".join(f"{n} x{c}" for n, c in unpressed_names.most_common(8)), "warn")]

    out.append(Line(""))
    notes = [f"Defensive checks cover the first {detail_deaths} deaths of each pull and skip one-shots."]
    if not include_kills:
        notes.append("Only wipes were reviewed - turn on Include kills to count deaths on kills too.")
    out.append(Line("  " + " ".join(notes), "dim"))
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
