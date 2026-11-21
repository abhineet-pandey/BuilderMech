"""Approximate local simulator for mechfight/1 (NOT the real server).

Runs our Bot in-process against a squad of 15 simple house bots that share vision and hunt us
(one hunter at first, another each time we kill one, all of them after `all_hunt_t`).
Physics, vision, weapons, zone, loot, cubes and offers follow protocol.md closely enough to catch
navigation, aiming and decision bugs. Scores are only indicative of the real thing.

    python sim/sim.py --matches 8 --seed 1
"""
import argparse
import gzip
import io
import json
import math
import os
import random
import sys
import time
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mechfight as mf  # noqa: E402
from world import World  # noqa: E402

KIT = os.path.join(ROOT, "kit", "mechfight-kit")
PASSIVE = os.environ.get("SIM_PASSIVE") == "1"
# calibration knobs for the house bots (defaults = original behaviour)
SIM_AIM = float(os.environ.get("SIM_AIM", "1"))  # aim error multiplier
SIM_REACT = float(os.environ.get("SIM_REACT", "0"))  # seconds of continuous sight before a house bot fires
SIM_ENGAGE = float(os.environ.get("SIM_ENGAGE", "120"))  # non-hunters engage us within this range when they see us
SIM_DMG = float(os.environ.get("SIM_DMG", "1"))
SIM_MYDMG = float(os.environ.get("SIM_MYDMG", "1"))  # what-if: our damage to mechs  # house bot damage multiplier
TAU = 2 * math.pi


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def load_config():
    with gzip.open(os.path.join(KIT, "check", "stream.jsonl.gz"), "rt") as f:
        return json.loads(f.readline())["config"]


class Mech:
    def __init__(self, mid, pos, cfg, level=None):
        m = cfg["mech"]
        self.id = mid
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.cmd_aim = [0.0, 0.0]
        self.hp = m["hp"]
        self.max_hp = m["hp"]
        self.energy = m["energy"]
        self.max_energy = m["energy"]
        self.grounded = True
        self.cubes = 0
        self.upgrades = []
        self.alive = True
        self.level = level  # None = our bot
        self.weapons = [self.new_weapon("gun", cfg), None]
        self.last_spend = -9
        self.last_combat = -9
        self.charge = 0.0
        self.charging = False
        self.short_until = -1
        self.action = {}
        self.seen = {}  # id -> last time seen (memory)
        self.events = []
        self.sounds = []
        self.beams = []
        self.offers = []
        self.offer_id = 0
        self.armor = 0.0
        self.death_t = None
        self.stats = dict(m)
        self.kills = 0
        self.provoked_t = -99

    def new_weapon(self, kind, cfg):
        s = dict(cfg["weapons"][kind])
        w = {"kind": kind, "cooldown": 0.0, "stats": s}
        if kind == "gun":
            w["ammo"] = s["mag"]
            w["reload"] = 0.0
        if kind == "laser":
            w["charge"] = 0.0
        return w

    def scale(self):
        return 1 + 0.05 * self.cubes

    def center(self):
        return (self.pos[0], self.pos[1], self.pos[2] + 4.5 * self.scale())


class Sim:
    def __init__(self, cfg, world, seed, bot_factory, all_hunt_t=300.0, verbose=False):
        self.cfg = cfg
        self.w = world
        self.rng = random.Random(seed)
        self.t = 0.0
        self.tick = 0
        self.verbose = verbose
        self.all_hunt_t = all_hunt_t
        self.mechs = []
        self.projectiles = []
        self.pid = 1
        self.pickups = {}
        self.crates = {}
        self.cubes = {}
        self.npcs = []
        self.deaths = []
        b = world.bounds
        zc = cfg["zone"]["start_center"]
        # spawns: inside 0.8 of the zone, open ground, separated
        spawns = []
        tries = 0
        while len(spawns) < 16 and tries < 20000:
            tries += 1
            a = self.rng.random() * TAU
            r = math.sqrt(self.rng.random()) * cfg["zone"]["start_radius"] * 0.8
            x, y = zc[0] + math.cos(a) * r, zc[1] + math.sin(a) * r
            if not world.in_bounds(x, y, 30):
                continue
            if world.height(x, y) - world.ground(x, y) > 0.5:
                continue
            if any(math.hypot(x - s[0], y - s[1]) < 110 for s in spawns):
                continue
            spawns.append((x, y, world.ground(x, y)))
        self.rng.shuffle(spawns)
        levels = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3
        me_slot = self.rng.randrange(16)
        li = 0
        for i in range(16):
            lvl = None if i == me_slot else levels[li]
            if lvl:
                li += 1
            self.mechs.append(Mech(i, spawns[i], cfg, lvl))
        self.me = self.mechs[me_slot]
        # loot
        nid = 8000
        for _ in range(cfg["loot"]["floor_count"]):
            p = self.rand_open()
            kind = self.rng.choice(["mortar", "laser", "lightning"])
            self.pickups[nid] = (kind, p)
            nid += 1
        self.next_pickup = nid
        for k in range(cfg["crates"]["count"]):
            self.crates[1000 + k] = [self.rand_open(), cfg["crates"]["hp"]]
        self.next_cube = 5000
        for k in range(cfg["npc"]["count"]):
            p = self.rand_open()
            self.npcs.append({"id": 100 + k, "pos": list(p), "home": p, "hp": cfg["npc"]["hp"], "cd": 0, "target": None, "vel": [0, 0, 0]})
        # zone schedule
        self.phases = []
        c = list(zc)
        r = cfg["zone"]["start_radius"]
        tt = 0.0
        for ph in cfg["zone"]["phases"]:
            nr = ph["radius"]
            a = self.rng.random() * TAU
            off = self.rng.random() * (r - nr) * 0.9
            nc = [c[0] + math.cos(a) * off, c[1] + math.sin(a) * off]
            self.phases.append((tt + ph["hold_s"], tt + ph["hold_s"] + ph["shrink_s"], c, r, nc, nr, ph["dps"]))
            tt += ph["hold_s"] + ph["shrink_s"]
            c, r = nc, nr
        self.hunters = set()
        self.bot = bot_factory({"type": "hello", "slot": self.me.id, "seed": seed, "map": {"dir": os.path.join(KIT, "arena")}, "config": cfg})
        self.bot_ms = []
        self.bot_errors = 0
        self.house_seen_me_t = -99
        self.my_kills = 0
        self.my_dmg_log = []
        self.my_dealt = {}
        self.my_shots = {}
        self.my_fight_ticks = 0
        self.drops = []
        for k, dt in enumerate(cfg["drops"]["times"]):
            self.drops.append({"id": 20000 + k, "call": dt, "pos": None, "holder": None, "progress": 0.0, "taken": False})

    def rand_open(self):
        zc = self.cfg["zone"]["start_center"]
        for _ in range(1000):
            a = self.rng.random() * TAU
            r = math.sqrt(self.rng.random()) * 420
            x, y = zc[0] + math.cos(a) * r, zc[1] + math.sin(a) * r
            if self.w.in_bounds(x, y, 20) and self.w.height(x, y) - self.w.ground(x, y) < 0.3:
                return (x, y, self.w.ground(x, y))
        return (0, 0, 0)

    # ------------------------------------------------------------------ zone
    def zone(self):
        t = self.t
        for (s0, s1, c, r, nc, nr, dps) in self.phases:
            if t < s1:
                if t <= s0:
                    return {"center": c, "radius": r, "next_center": nc, "next_radius": nr, "shrink_start": s0, "shrink_end": s1, "dps": dps}
                f = (t - s0) / (s1 - s0)
                cc = [c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f]
                return {"center": cc, "radius": r + (nr - r) * f, "next_center": nc, "next_radius": nr, "shrink_start": s0, "shrink_end": s1, "dps": dps}
        last = self.phases[-1]
        return {"center": last[4], "radius": 0.0, "next_center": last[4], "next_radius": 0.0, "shrink_start": last[0], "shrink_end": last[1], "dps": last[6]}

    # ------------------------------------------------------------------ vision
    def can_see(self, a, pt, near_extra=0):
        eye = (a.pos[0], a.pos[1], a.pos[2] + 8 * a.scale())
        d = math.hypot(pt[0] - eye[0], pt[1] - eye[1])
        near = self.cfg["vision"]["near_range"] + near_extra
        if d > self.cfg["vision"]["range"]:
            return False
        if d > near:
            b = math.atan2(pt[1] - eye[1], pt[0] - eye[0])
            if abs(wrap(b - a.aim[0])) > math.radians(self.cfg["vision"]["fov_deg"]) / 2:
                return False
        return self.w.los(eye, pt) or self.w.los(eye, (pt[0], pt[1], pt[2] + 3))

    def observe(self, m):
        t = self.t
        mechs = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.can_see(m, o.center()):
                m.seen[o.id] = t
            if t - m.seen.get(o.id, -99) <= self.cfg["vision"]["memory_s"]:
                mechs.append({"id": o.id, "kind": "mech", "pos": [round(v, 2) for v in o.pos], "vel": [round(v, 2) for v in o.vel],
                              "aim": list(o.aim), "hp": o.hp, "max_hp": o.max_hp, "cubes": o.cubes,
                              "weapons": [w["kind"] for w in o.weapons if w], "charging": o.charging})
        for n in self.npcs:
            if n["hp"] > 0 and self.can_see(m, (n["pos"][0], n["pos"][1], n["pos"][2] + 1.7)):
                mechs.append({"id": n["id"], "kind": "npc", "pos": list(n["pos"]), "vel": list(n["vel"]), "aim": [0, 0], "hp": n["hp"], "max_hp": self.cfg["npc"]["hp"]})
        projs = []
        for p in self.projectiles:
            if p["owner"] != m.id and self.can_see(m, p["pos"]):
                projs.append({"id": p["id"], "kind": p["kind"], "pos": list(p["pos"]), "vel": list(p["vel"])})
        crates = [{"id": k, "pos": list(v[0]), "hp": v[1]} for k, v in self.crates.items() if self.can_see(m, (v[0][0], v[0][1], v[0][2] + 1.2))]
        cubes = [{"id": k, "pos": list(v)} for k, v in self.cubes.items() if self.can_see(m, (v[0], v[1], v[2] + 0.5))]
        pickups = [{"id": k, "kind": v[0], "pos": list(v[1])} for k, v in self.pickups.items() if self.can_see(m, (v[1][0], v[1][1], v[1][2] + 0.5))]
        drops = []
        for d in self.drops:
            if d["pos"] is not None and not d["taken"]:
                drops.append({"id": d["id"], "pos": d["pos"], "radius": 8, "lands_in": max(0.0, d["call"] + 25 - t), "capture_s": 4, "progress": d["progress"], "holder": d["holder"]})
        offer = m.offers[0] if m.offers else None
        obs = {"type": "obs", "tick": self.tick, "t": round(t, 2), "alive": sum(1 for o in self.mechs if o.alive),
               "self": {"id": m.id, "pos": list(m.pos), "vel": list(m.vel), "aim": list(m.aim), "hp": m.hp, "max_hp": m.max_hp,
                        "energy": m.energy, "max_energy": m.max_energy, "grounded": m.grounded, "cubes": m.cubes,
                        "upgrades": list(m.upgrades), "in_zone": self.in_zone(m),
                        "weapons": [json.loads(json.dumps(w)) if w else None for w in m.weapons],
                        "stats": {**m.stats, "eye_height": 8 * m.scale(), "muzzle_height": 7 * m.scale()}},
               "mechs": mechs, "projectiles": projs, "crates": crates, "cubes": cubes, "pickups": pickups,
               "sounds": m.sounds, "beams": m.beams, "events": m.events, "drops": drops, "zone": self.zone(), "offer": offer}
        m.sounds, m.beams, m.events = [], [], []
        return obs

    def in_zone(self, m):
        z = self.zone()
        return math.hypot(m.pos[0] - z["center"][0], m.pos[1] - z["center"][1]) <= z["radius"]

    def emit_sound(self, src, kind):
        if src.level is None and kind in ("gun", "laser", "lightning", "mortar"):
            self.my_shots[kind] = self.my_shots.get(kind, 0) + 1
        rng_ = self.cfg["sound"]["range"].get(kind, 100)
        for o in self.mechs:
            if not o.alive or o is src:
                continue
            d = math.hypot(o.pos[0] - src.pos[0], o.pos[1] - src.pos[1])
            if d <= rng_:
                b = math.atan2(src.pos[1] - o.pos[1], src.pos[0] - o.pos[0]) + self.rng.gauss(0, math.radians(7))
                o.sounds.append({"kind": kind, "bearing": wrap(b), "dist": "near" if d < 60 else "mid" if d < 160 else "far", "age": 0.05})

    # ------------------------------------------------------------------ damage
    def damage(self, victim, amount, weapon, attacker, from_pt):
        if not victim.alive:
            return
        mult = 1.0
        rear = False
        if attacker is not None:
            mult *= 1 + 0.04 * min(attacker.cubes, 10)
            b = math.atan2(from_pt[1] - victim.pos[1], from_pt[0] - victim.pos[0])
            if abs(wrap(b - victim.aim[0])) > math.pi / 2:
                mult *= 1.5
                rear = True
            mult *= (1 - victim.armor)
        amt = amount * mult
        if attacker is not None and attacker.level is not None and victim.level is None:
            amt *= SIM_DMG
            self.my_dmg_log.append((self.t, attacker.id, attacker.level, amt, rear))
        if attacker is not None and attacker.level is None and victim.level is not None:
            amt *= SIM_MYDMG
            self.my_dealt[weapon] = self.my_dealt.get(weapon, 0) + amt
        victim.hp -= amt
        victim.last_combat = self.t
        if attacker is not None:
            attacker.last_combat = self.t
            victim.provoked_t = self.t
            attacker.events.append({"type": "hit", "target": victim.id, "amount": round(amt, 1), "weapon": weapon, "rear": rear})
            if attacker.level is not None and victim.level is None:
                self.house_seen_me_t = self.t
        b = math.atan2(from_pt[1] - victim.pos[1], from_pt[0] - victim.pos[0]) if from_pt else None
        victim.events.append({"type": "damaged", "amount": round(amt, 1), "weapon": weapon, "bearing": b, "by": attacker.id if attacker else None, "rear": rear})
        if victim.hp <= 0:
            victim.alive = False
            victim.death_t = self.tick
            self.deaths.append((self.tick, victim.id))
            if attacker is not None:
                attacker.events.append({"type": "kill", "target": victim.id})
                attacker.kills += 1
            n = victim.cubes + 2
            for k in range(n):
                a = k * TAU / n
                self.cubes[self.next_cube] = (victim.pos[0] + math.cos(a) * 2, victim.pos[1] + math.sin(a) * 2, victim.pos[2])
                self.next_cube += 1
            for w in victim.weapons:
                if w and w["kind"] != "gun":
                    self.pickups[self.next_pickup] = (w["kind"], tuple(victim.pos))
                    self.next_pickup += 1
            self.emit_sound(victim, "death")
            if victim.level is not None and attacker is self.me:
                self.my_kills += 1

    def damage_npc(self, n, amount, attacker):
        if n["hp"] <= 0:
            return
        amt = amount * (1 + 0.04 * min(attacker.cubes, 10))
        n["hp"] -= amt
        n["target"] = attacker.id
        attacker.last_combat = self.t  # dealing damage resets the regen timer
        attacker.events.append({"type": "hit", "target": n["id"], "amount": amt, "weapon": "x"})
        if n["hp"] <= 0:
            for k in range(2):
                self.cubes[self.next_cube] = (n["pos"][0] + k, n["pos"][1], n["pos"][2])
                self.next_cube += 1

    def damage_crate(self, cid, amount, attacker):
        c = self.crates.get(cid)
        if not c:
            return
        attacker.last_combat = self.t
        c[1] -= amount
        if c[1] <= 0:
            del self.crates[cid]
            self.cubes[self.next_cube] = tuple(c[0])
            self.next_cube += 1
            if self.rng.random() < 0.5:
                self.pickups[self.next_pickup] = (self.rng.choice(["mortar", "laser", "lightning"]), (c[0][0] + 1.5, c[0][1], c[0][2]))
                self.next_pickup += 1

    def add_cube(self, m):
        m.cubes += 1
        old = m.max_hp
        base = self.cfg["mech"]["hp"] * (1.15 ** m.upgrades.count("plating"))
        m.max_hp = base * (1 + 0.04 * min(m.cubes, 10))
        m.hp += m.max_hp - old
        m.events.append({"type": "pickup", "cubes": m.cubes})
        if m.cubes in self.cfg["cubes"]["upgrade_thresholds"]:
            self.make_offer(m)

    def make_offer(self, m):
        held = {w["kind"] for w in m.weapons if w}
        pool = [u for u in self.cfg["upgrades"] if m.upgrades.count(u["id"]) < u["max"] and (not u.get("requires") or u["requires"] in held)]
        self.rng.shuffle(pool)
        m.offer_id += 1
        m.offers.append({"id": m.offer_id, "expires": self.t + 5, "choices": [{"id": u["id"], "name": u["name"], "desc": u["desc"]} for u in pool[:3]]})

    def apply_upgrade(self, m, uid):
        u = next(x for x in self.cfg["upgrades"] if x["id"] == uid)
        m.upgrades.append(uid)
        m.events.append({"type": "upgrade", "id": uid})
        for e in u["effects"]:
            sec, key = e["path"].rsplit(".", 1)
            if sec == "mech":
                tgt = m.stats
            else:
                kind = sec.split(".")[1]
                tgt = None
                for w in m.weapons:
                    if w and w["kind"] == kind:
                        tgt = w["stats"]
                if tgt is None:
                    continue
            if "mul" in e:
                tgt[key] = tgt[key] * e["mul"]
            if "add" in e:
                tgt[key] = tgt[key] + e["add"]
        m.armor = m.stats.get("armor", 0)
        if uid == "plating":
            f = 1.15
            m.max_hp *= f
            m.hp *= f
        if uid == "capacitor":
            m.max_energy += 30

    # ------------------------------------------------------------------ actions
    def apply_action(self, m, a):
        cfg = self.cfg
        m.action = a
        if "aim" in a:
            m.cmd_aim = [float(a["aim"][0]), max(cfg["mech"]["min_pitch"], min(cfg["mech"]["max_pitch"], float(a["aim"][1])))]
        if a.get("pick") is not None and m.offers:
            off = m.offers.pop(0)
            i = int(a["pick"])
            if 0 <= i < len(off["choices"]):
                self.apply_upgrade(m, off["choices"][i]["id"])
        if a.get("reload"):
            for w in m.weapons:
                if w and w["kind"] == "gun" and w["reload"] == 0 and w["ammo"] < w["stats"]["mag"]:
                    w["reload"] = w["stats"]["reload"]
                    w["ammo"] = 0
        if a.get("take") is not None:
            pid = a["take"]
            p = self.pickups.get(pid)
            if not p:
                m.events.append({"type": "take_failed", "id": pid, "reason": "gone"})
            elif math.hypot(p[1][0] - m.pos[0], p[1][1] - m.pos[1]) > cfg["loot"]["pickup_radius"] or abs(p[1][2] - m.pos[2]) > 4:
                m.events.append({"type": "take_failed", "id": pid, "reason": "range"})
            elif any(w and w["kind"] == p[0] for w in m.weapons):
                m.events.append({"type": "take_failed", "id": pid, "reason": "have"})
            else:
                slot = None
                if a.get("drop") is not None:
                    slot = int(a["drop"])
                else:
                    for i, w in enumerate(m.weapons):
                        if w is None:
                            slot = i
                            break
                if slot is None:
                    m.events.append({"type": "take_failed", "id": pid, "reason": "full"})
                else:
                    old = m.weapons[slot]
                    del self.pickups[pid]
                    if old:
                        self.pickups[self.next_pickup] = (old["kind"], tuple(m.pos))
                        m.events.append({"type": "dropped", "id": self.next_pickup, "kind": old["kind"], "slot": slot})
                        self.next_pickup += 1
                    nw = m.new_weapon(p[0], cfg)
                    for uid in m.upgrades:
                        u = next(x for x in cfg["upgrades"] if x["id"] == uid)
                        for e in u["effects"]:
                            if e["path"].startswith("weapons." + p[0] + "."):
                                key = e["path"].rsplit(".", 1)[1]
                                if "mul" in e:
                                    nw["stats"][key] *= e["mul"]
                                if "add" in e:
                                    nw["stats"][key] += e["add"]
                    nw["cooldown"] = max(cfg["loot"]["equip_s"], nw["stats"].get("cooldown", 0))
                    m.weapons[slot] = nw
                    m.events.append({"type": "took", "id": pid, "kind": p[0], "slot": slot})

    def step_mech(self, m, dt):
        cfg = self.cfg
        mc = m.stats
        a = m.action
        mv = a.get("move", [0, 0])
        ml = math.hypot(mv[0], mv[1])
        if ml > 1:
            mv = [mv[0] / ml, mv[1] / ml]
        boost = bool(a.get("boost"))
        charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in m.weapons)
        spd = mc["boost_speed"] if (boost and m.grounded and m.energy > 0 and ml > 0.1) else mc["walk_speed"]
        if charging:
            spd *= cfg["mech"]["charge_move_mult"]
        tvx, tvy = mv[0] * spd, mv[1] * spd
        acc = mc["ground_accel"] if m.grounded else mc["air_accel"]
        for k, tv in ((0, tvx), (1, tvy)):
            dv = tv - m.vel[k]
            lim = acc * dt
            m.vel[k] += max(-lim, min(lim, dv))
        spent = False
        if boost and m.grounded and ml > 0.1 and m.energy > 0:
            m.energy -= mc["boost_drain"] * dt
            spent = True
        if a.get("jump") and m.grounded:
            m.vel[2] = mc["jump_speed"]
            m.grounded = False
        if not m.grounded:
            m.vel[2] -= mc["gravity"] * dt
            if boost and m.energy > 0:
                m.vel[2] = min(mc["max_rise_speed"], m.vel[2] + mc["jet_accel"] * dt)
                m.energy -= mc["jet_drain"] * dt
                spent = True
        m.energy = max(0.0, m.energy)
        if spent:
            m.last_spend = self.t
        elif self.t - m.last_spend > mc["energy_regen_delay"]:
            m.energy = min(m.max_energy, m.energy + mc["energy_regen"] * dt)
        # horizontal move with wall sliding
        for k in (0, 1):
            np_ = list(m.pos)
            np_[k] += m.vel[k] * dt
            h = self.w.height(np_[0] + (2.5 if m.vel[k] > 0 else -2.5) * (k == 0), np_[1] + (2.5 if m.vel[k] > 0 else -2.5) * (k == 1))
            if h > m.pos[2] + mc["step_up"] or not self.w.in_bounds(np_[0], np_[1], 2):
                m.vel[k] = 0.0
            else:
                m.pos[k] = np_[k]
        m.pos[2] += m.vel[2] * dt
        gh = self.w.height(m.pos[0], m.pos[1])
        if m.pos[2] <= gh:
            m.pos[2] = gh
            m.vel[2] = 0.0
            m.grounded = True
        elif m.grounded and m.pos[2] - gh < 1.3:
            m.pos[2] = gh
        else:
            m.grounded = False
        # aim slew
        dy = wrap(m.cmd_aim[0] - m.aim[0])
        lim = mc["turn_rate"] * dt
        m.aim[0] = wrap(m.aim[0] + max(-lim, min(lim, dy)))
        dp = m.cmd_aim[1] - m.aim[1]
        lim = mc["pitch_rate"] * dt
        m.aim[1] += max(-lim, min(lim, dp))
        # regen
        if self.t - m.last_combat > mc["regen_delay"] and self.in_zone(m):
            m.hp = min(m.max_hp, m.hp + mc["regen_per_s"] * dt)

    def fire_weapons(self, m, dt):
        cfg = self.cfg
        fire = m.action.get("fire", [False, False]) or [False, False]
        m.charging = False
        for i, w in enumerate(m.weapons):
            if not w:
                continue
            s = w["stats"]
            w["cooldown"] = max(0.0, w["cooldown"] - dt)
            if w["kind"] == "gun" and w["reload"] > 0:
                w["reload"] = max(0.0, w["reload"] - dt)
                if w["reload"] == 0:
                    w["ammo"] = s["mag"]
            f = i < len(fire) and bool(fire[i])
            k = w["kind"]
            if k == "laser":
                if f and w["cooldown"] == 0 and self.t >= m.short_until:
                    w["charge"] = w.get("charge", 0) + dt
                    m.charging = True
                    if w["charge"] >= s["charge"] + s["hold_s"]:
                        self.fire_laser(m, w)
                elif w.get("charge", 0) > 0:
                    if w["charge"] >= s["min_charge"]:
                        self.fire_laser(m, w)
                    else:
                        w["charge"] += dt
                        m.charging = True
                continue
            if not f or w["cooldown"] > 0:
                continue
            if k == "gun":
                if w["reload"] > 0:
                    continue
                if w["ammo"] <= 0:
                    w["reload"] = s["reload"]
                    continue
                w["ammo"] -= 1
                w["cooldown"] = s["cooldown"]
                if w["ammo"] == 0:
                    w["reload"] = s["reload"]
                yaw = m.aim[0] + math.radians(self.rng.gauss(0, s["spread_deg"]))
                pit = m.aim[1] + math.radians(self.rng.gauss(0, s["spread_deg"]))
                self.spawn_proj(m, "gun", yaw, pit, s["speed"], s["damage"], s["range"])
                self.emit_sound(m, "gun")
            elif k == "mortar":
                w["cooldown"] = s["cooldown"]
                self.spawn_proj(m, "mortar", m.aim[0], m.aim[1], s["speed"], s["damage"], 9999)
                self.emit_sound(m, "mortar")
            elif k == "lightning":
                if m.energy < s["energy"]:
                    continue
                w["cooldown"] = s["cooldown"]
                m.energy -= s["energy"]
                m.last_spend = self.t
                self.fire_lightning(m, w)
                self.emit_sound(m, "lightning")

    def muzzle(self, m):
        return (m.pos[0] + math.cos(m.aim[0]) * 3, m.pos[1] + math.sin(m.aim[0]) * 3, m.pos[2] + 7 * m.scale())

    def spawn_proj(self, m, kind, yaw, pit, speed, dmg, rng_):
        o = self.muzzle(m)
        v = (math.cos(pit) * math.cos(yaw) * speed, math.cos(pit) * math.sin(yaw) * speed, math.sin(pit) * speed)
        self.projectiles.append({"id": self.pid, "kind": kind, "pos": list(o), "vel": list(v), "owner": m.id, "dmg": dmg, "left": rng_, "ttl": 7})
        self.pid += 1

    def hit_cyl(self, p, q, c_xy, z0, z1, r):
        """segment p->q vs vertical cylinder; returns param or None"""
        steps = 6
        for k in range(steps + 1):
            f = k / steps
            x = p[0] + (q[0] - p[0]) * f
            y = p[1] + (q[1] - p[1]) * f
            z = p[2] + (q[2] - p[2]) * f
            if math.hypot(x - c_xy[0], y - c_xy[1]) <= r and z0 <= z <= z1:
                return f
        return None

    def step_projectiles(self, dt):
        g = self.cfg["weapons"]["mortar"]["gravity"]
        keep = []
        for p in self.projectiles:
            old = list(p["pos"])
            if p["kind"] == "mortar":
                p["vel"][2] -= g * dt
            new = [old[k] + p["vel"][k] * dt for k in range(3)]
            p["ttl"] -= dt
            owner = self.mechs[p["owner"]] if p["owner"] < 100 else None
            hit = None
            for o in self.mechs:
                if not o.alive or o.id == p["owner"]:
                    continue
                f = self.hit_cyl(old, new, o.pos, o.pos[2], o.pos[2] + 9 * o.scale(), 2.5 * o.scale())
                if f is not None:
                    hit = ("mech", o)
                    break
            if not hit:
                for n in self.npcs:
                    if n["hp"] > 0 and self.hit_cyl(old, new, n["pos"], n["pos"][2], n["pos"][2] + 3.5, 1.8) is not None:
                        hit = ("npc", n)
                        break
            if not hit:
                for cid, c in self.crates.items():
                    if self.hit_cyl(old, new, c[0], c[0][2], c[0][2] + 2.4, 1.4) is not None:
                        hit = ("crate", cid)
                        break
            wall = new[2] <= self.w.height(new[0], new[1])
            if p["kind"] == "gun":
                p["left"] -= math.dist(old, new)
            if hit:
                if p["kind"] == "mortar":
                    self.explode(p, new, owner, direct=hit)
                elif hit[0] == "mech":
                    self.damage(hit[1], p["dmg"], "gun" if p["kind"] == "gun" else "npc", owner, old)
                elif hit[0] == "npc" and owner:
                    self.damage_npc(hit[1], p["dmg"], owner)
                elif hit[0] == "crate" and owner:
                    self.damage_crate(hit[1], p["dmg"], owner)
                continue
            if wall or p["ttl"] <= 0 or p.get("left", 1) <= 0:
                if p["kind"] == "mortar" and wall:
                    self.explode(p, new, owner, direct=None)
                continue
            p["pos"] = new
            keep.append(p)
        self.projectiles = keep

    def explode(self, p, pt, owner, direct):
        s = self.cfg["weapons"]["mortar"]
        for o in self.mechs:
            if not o.alive:
                continue
            if direct and direct[0] == "mech" and direct[1] is o:
                self.damage(o, s["damage"], "mortar", owner, pt)
                continue
            d = math.dist(pt, o.center())
            if d < s["splash_radius"] + 2.5:
                f = 1 - (1 - s["splash_edge"]) * min(1, d / s["splash_radius"])
                dmg = s["splash"] * f * (0.5 if o is owner else 1)
                self.damage(o, dmg, "mortar", owner if o is not owner else None, pt)
                o.energy = max(0, o.energy - s["drain"] * f)
        if owner:
            self.emit_sound(owner, "explosion")

    def fire_laser(self, m, w):
        s = w["stats"]
        frac = s["min_frac"] + (1 - s["min_frac"]) * min(1.0, w["charge"] / s["charge"])
        w["charge"] = 0.0
        w["cooldown"] = s["cooldown"]
        dmg = s["damage"] * frac
        o = self.muzzle(m)
        d = (math.cos(m.aim[1]) * math.cos(m.aim[0]), math.cos(m.aim[1]) * math.sin(m.aim[0]), math.sin(m.aim[1]))
        # walk the beam
        best = None
        for o2 in self.mechs:
            if o2 is m or not o2.alive:
                continue
            c = o2.center()
            v = (c[0] - o[0], c[1] - o[1])
            along = v[0] * d[0] / max(1e-6, math.hypot(d[0], d[1])) + v[1] * d[1] / max(1e-6, math.hypot(d[0], d[1]))
            if along < 0 or along > s["range"]:
                continue
            hd = math.hypot(d[0], d[1])
            px, py = o[0] + d[0] / hd * along, o[1] + d[1] / hd * along
            pz = o[2] + d[2] / max(1e-6, hd) * along
            if math.hypot(px - c[0], py - c[1]) <= 2.5 * o2.scale() and o2.pos[2] <= pz <= o2.pos[2] + 9 * o2.scale():
                if self.w.los(o, (px, py, pz)) and (best is None or along < best[0]):
                    best = (along, o2)
        if best:
            o2 = best[1]
            cook = 1 + s["cookoff"] if any(x and x["kind"] == "mortar" for x in o2.weapons) else 1
            self.damage(o2, dmg * cook, "laser", m, o)
        for n in self.npcs:
            if n["hp"] > 0:
                c = (n["pos"][0], n["pos"][1], n["pos"][2] + 1.7)
                b = math.atan2(c[1] - o[1], c[0] - o[0])
                dd = math.hypot(c[0] - o[0], c[1] - o[1])
                if abs(wrap(b - m.aim[0])) * dd < 1.8 and dd < s["range"] and self.w.los(o, c):
                    self.damage_npc(n, dmg, m)
        for cid, c in list(self.crates.items()):
            b = math.atan2(c[0][1] - o[1], c[0][0] - o[0])
            dd = math.hypot(c[0][0] - o[0], c[0][1] - o[1])
            if abs(wrap(b - m.aim[0])) * dd < 1.4 and dd < s["range"] and self.w.los(o, (c[0][0], c[0][1], c[0][2] + 1.2)):
                self.damage_crate(cid, dmg, m)
        self.emit_sound(m, "laser")

    def fire_lightning(self, m, w):
        s = w["stats"]
        o = self.muzzle(m)
        cands = []
        for o2 in self.mechs:
            if o2 is not m and o2.alive:
                cands.append(("mech", o2, o2.center()))
        for n in self.npcs:
            if n["hp"] > 0:
                cands.append(("npc", n, (n["pos"][0], n["pos"][1], n["pos"][2] + 1.7)))
        for cid, c in self.crates.items():
            cands.append(("crate", cid, (c[0][0], c[0][1], c[0][2] + 1.2)))
        best = None
        for kind, obj, c in cands:
            d = math.dist(o, c)
            if d > s["range"]:
                continue
            b = math.atan2(c[1] - o[1], c[0] - o[0])
            if abs(wrap(b - m.aim[0])) > math.radians(s["cone_deg"]):
                continue
            if not self.w.los(o, c):
                continue
            if best is None or d < best[0]:
                best = (d, kind, obj, c)
        if not best:
            return
        dmg = s["damage"]
        hitset = set()
        cur = best
        for jump in range(int(s["chain"]) + 1):
            _, kind, obj, c = cur
            hitset.add(id(obj))
            if kind == "mech":
                self.damage(obj, dmg, "lightning", m, o)
                if any(x and x["kind"] == "laser" for x in obj.weapons):
                    obj.short_until = self.t + s["short_s"]
                    for x in obj.weapons:
                        if x and x["kind"] == "laser":
                            x["charge"] = 0
            elif kind == "npc":
                self.damage_npc(obj, dmg, m)
            else:
                self.damage_crate(obj, dmg, m)
            dmg *= s["chain_falloff"]
            nxt = None
            for k2, ob2, c2 in cands:
                if id(ob2) in hitset:
                    continue
                d2 = math.dist(c, c2)
                if d2 < s["chain_range"] and (nxt is None or d2 < nxt[0]):
                    nxt = (d2, k2, ob2, c2)
            if not nxt:
                break
            cur = nxt

    def step_npcs(self, dt):
        cfg = self.cfg["npc"]
        for n in self.npcs:
            if n["hp"] <= 0:
                continue
            n["cd"] = max(0, n["cd"] - dt)
            tgt = None
            if n["target"] is not None:
                o = self.mechs[n["target"]]
                if o.alive and math.dist(o.pos, n["pos"]) < cfg["deaggro_range"] and math.dist(n["pos"], n["home"]) < cfg["chase_leash"]:
                    tgt = o
                else:
                    n["target"] = None
            if tgt is None and math.dist(n["pos"], n["home"]) < cfg["leash"]:
                for o in self.mechs:
                    if o.alive and math.dist(o.pos, n["pos"]) < cfg["aggro_range"] and self.w.los((n["pos"][0], n["pos"][1], n["pos"][2] + 3), o.center()):
                        tgt = o
                        break
            if tgt and n["cd"] == 0:
                n["cd"] = cfg["cooldown"]
                c = tgt.center()
                src = (n["pos"][0], n["pos"][1], n["pos"][2] + 3)
                if self.w.los(src, c):
                    yaw = math.atan2(c[1] - src[1], c[0] - src[0]) + math.radians(self.rng.gauss(0, cfg["spread_deg"]))
                    pit = math.atan2(c[2] - src[2], math.hypot(c[0] - src[0], c[1] - src[1]))
                    sp = cfg["projectile_speed"]
                    self.projectiles.append({"id": self.pid, "kind": "npc", "pos": [src[0] + math.cos(yaw) * 2, src[1] + math.sin(yaw) * 2, src[2]],
                                             "vel": [math.cos(pit) * math.cos(yaw) * sp, math.cos(pit) * math.sin(yaw) * sp, math.sin(pit) * sp],
                                             "owner": 999, "dmg": cfg["damage"], "left": 200, "ttl": 3})
                    self.pid += 1

    # ------------------------------------------------------------------ house AI
    def house_act(self, m):
        """A simple squad bot. Accuracy and reaction depend on level."""
        cfg = self.cfg
        lvl = {"easy": 0, "medium": 1, "hard": 2, "expert": 3}[m.level]
        me = self.me
        a = {"move": [0, 0], "aim": m.cmd_aim, "fire": [False, False]}
        z = self.zone()
        hunting = m.id in self.hunters
        # shared vision of us
        sees_me = me.alive and (self.can_see(m, me.center()) or self.t - self.house_seen_me_t < 1.0)
        if sees_me and self.can_see(m, me.center()):
            self.house_seen_me_t = self.t
        knows_me = me.alive and (self.t - self.house_seen_me_t < 5 or hunting)
        goal = None
        provoked = self.t - m.provoked_t < 8
        engage = me.alive and (hunting and knows_me or ((not PASSIVE or provoked) and sees_me and math.dist(m.pos, me.pos) < SIM_ENGAGE))
        if engage:
            d = math.dist(m.pos, me.pos)
            goal = me.pos if d > 60 else None
            if goal is None:
                b = math.atan2(me.pos[1] - m.pos[1], me.pos[0] - m.pos[0]) + math.pi / 2
                sgn = 1 if int(self.t / 1.5) % 2 else -1
                a["move"] = [math.cos(b) * sgn, math.sin(b) * sgn]
            # aim with lead and level-dependent error
            src = self.muzzle(m)
            c = me.center()
            tt = math.dist(src, c) / 252
            lead = (0.3, 0.6, 0.85, 1.0)[lvl]
            err = math.radians((2.5, 1.5, 0.8, 0.4)[lvl]) * SIM_AIM
            px, py = c[0] + me.vel[0] * tt * lead, c[1] + me.vel[1] * tt * lead
            yaw = math.atan2(py - src[1], px - src[0]) + self.rng.gauss(0, err)
            pit = math.atan2(c[2] - src[2], math.hypot(px - src[0], py - src[1]))
            a["aim"] = [yaw, pit]
            vis = self.w.los(src, c)
            if vis:
                if getattr(m, "see_start", None) is None:
                    m.see_start = self.t
            else:
                m.see_start = None
            if vis and self.t - m.see_start < SIM_REACT:
                vis = False
            fire = [False, False]
            for i, w in enumerate(m.weapons):
                if not w:
                    continue
                if w["kind"] == "gun" and vis and d < 200:
                    fire[i] = True
                if w["kind"] == "laser" and vis and d < 250:
                    fire[i] = w.get("charge", 0) < w["stats"]["charge"] or abs(wrap(m.aim[0] - yaw)) > 0.02
                if w["kind"] == "lightning" and vis and d < 42:
                    fire[i] = True
                if w["kind"] == "mortar" and 30 < d < 200 and vis:
                    v = w["stats"]["speed"]
                    x = math.hypot(c[0] - src[0], c[1] - src[1])
                    disc = v ** 4 - 22 * (22 * x * x + 2 * (c[2] - src[2]) * v * v)
                    if disc > 0:
                        a["aim"] = [yaw, math.atan((v * v - math.sqrt(disc)) / (22 * x))]
                        fire[i] = True
            a["fire"] = fire
        else:
            # loot / hold zone
            best = None
            for pid, (k, p) in self.pickups.items():
                if any(w and w["kind"] == k for w in m.weapons) or k == "mortar" and lvl >= 2:
                    continue
                if not any(w is None for w in m.weapons):
                    continue
                d = math.dist(p, m.pos)
                if d < 150 and (best is None or d < best[0]):
                    best = (d, pid, p)
            if best:
                goal = best[2]
                if best[0] < 3.5:
                    a["take"] = best[1]
            else:
                zc = z["next_center"]
                if math.hypot(m.pos[0] - zc[0], m.pos[1] - zc[1]) > z["next_radius"] * 0.6:
                    goal = (zc[0], zc[1], 0)
            a["aim"] = [m.aim[0] + 0.3, 0.0]
        if goal is not None:
            dx, dy = goal[0] - m.pos[0], goal[1] - m.pos[1]
            dd = math.hypot(dx, dy) or 1
            ux, uy = dx / dd, dy / dd
            # crude wall handling: jump + jet when blocked
            ahead = self.w.height(m.pos[0] + ux * 4, m.pos[1] + uy * 4)
            a["move"] = [ux, uy]
            if ahead > m.pos[2] + 1.2:
                a["jump"] = True
                a["boost"] = True
            if not m.grounded and m.energy > 5 and ahead > m.pos[2] - 0.5:
                a["boost"] = True
        # stay in zone
        if math.hypot(m.pos[0] - z["center"][0], m.pos[1] - z["center"][1]) > z["radius"] - 5:
            dx, dy = z["center"][0] - m.pos[0], z["center"][1] - m.pos[1]
            dd = math.hypot(dx, dy) or 1
            a["move"] = [dx / dd, dy / dd]
        if m.offers:
            a["pick"] = 0
        return a

    # ------------------------------------------------------------------ loop
    def update_hunters(self):
        alive_house = [m for m in self.mechs if m.alive and m.level is not None]
        order = sorted(alive_house, key=lambda m: ["easy", "medium", "hard", "expert"].index(m.level))
        if self.t >= self.all_hunt_t:
            want = len(order)
        else:
            want = 1 + self.my_kills
        self.hunters = {m.id for m in order[:want]}

    def run(self, max_t=480.0):
        dt = 1.0 / self.cfg["match"]["tick_hz"]
        while self.t < max_t:
            alive = [m for m in self.mechs if m.alive]
            if len(alive) <= 1 or not self.me.alive:
                break
            if self.tick % 2 == 0:
                self.update_hunters()
                # offers time out
                for m in alive:
                    if m.offers and self.t > m.offers[0]["expires"]:
                        off = m.offers.pop(0)
                        if off["choices"]:
                            self.apply_upgrade(m, off["choices"][0]["id"])
                for m in alive:
                    if m.level is None:
                        obs = self.observe(m)
                        t0 = time.perf_counter()
                        try:
                            with contextlib.redirect_stdout(io.StringIO()):
                                a = self.bot.act(obs) or {}
                        except Exception:
                            import traceback
                            self.bot_errors += 1
                            if self.bot_errors <= 2:
                                traceback.print_exc()
                            a = {}
                        mf.draw.clear()
                        self.bot_ms.append((time.perf_counter() - t0) * 1000)
                        if str(getattr(self.bot, "mode", "")).startswith("fight"):
                            self.my_fight_ticks += 1
                        json.dumps(a)
                    else:
                        m.events, m.sounds, m.beams = [], [], []
                        a = self.house_act(m)
                    self.apply_action(m, a)
            for m in alive:
                self.step_mech(m, dt)
                self.fire_weapons(m, dt)
                # cubes
                for cid, c in list(self.cubes.items()):
                    if math.hypot(c[0] - m.pos[0], c[1] - m.pos[1]) < 3.5 and abs(c[2] - m.pos[2]) < 4:
                        del self.cubes[cid]
                        self.add_cube(m)
                # zone
                z = self.zone()
                if math.hypot(m.pos[0] - z["center"][0], m.pos[1] - z["center"][1]) > z["radius"]:
                    m.hp -= z["dps"] * dt
                    m.last_combat = self.t
                    if m.hp <= 0 and m.alive:
                        m.alive = False
                        m.death_t = self.tick
                        self.deaths.append((self.tick, m.id))
            # supply drops
            for d in self.drops:
                if d["taken"]:
                    continue
                if d["pos"] is None and self.t >= d["call"]:
                    z = self.zone()
                    a = self.rng.random() * TAU
                    r = self.rng.random() * z["next_radius"] * 0.7
                    x, y = z["next_center"][0] + math.cos(a) * r, z["next_center"][1] + math.sin(a) * r
                    d["pos"] = [x, y, self.w.ground(x, y)]
                if d["pos"] is not None and self.t >= d["call"] + 25:
                    inside = [m for m in self.mechs if m.alive and math.hypot(m.pos[0] - d["pos"][0], m.pos[1] - d["pos"][1]) < 8]
                    if len(inside) == 1:
                        if d["holder"] != inside[0].id:
                            d["holder"], d["progress"] = inside[0].id, 0.0
                        d["progress"] += dt
                        if d["progress"] >= 4:
                            d["taken"] = True
                            m = inside[0]
                            for _ in range(3):
                                self.add_cube(m)
                            m.hp, m.energy = m.max_hp, m.max_energy
                            m.events.append({"type": "drop", "id": d["id"], "cubes": m.cubes})
                    elif not inside:
                        d["holder"], d["progress"] = None, 0.0
            self.step_projectiles(dt)
            self.step_npcs(dt)
            self.t += dt
            self.tick += 1
        # placement
        if self.me.alive:
            alive = sorted([m for m in self.mechs if m.alive], key=lambda m: -m.hp)
            place = alive.index(self.me) + 1
        else:
            place = sum(1 for m in self.mechs if m.alive) + 1
            # those who died on the same tick share the place
            same = sum(1 for tk, mid in self.deaths if tk == self.me.death_t and mid != self.me.id)
            place += same
        return place


_W = None


def _run_one(args):
    seed, all_hunt, botfile = args
    global _W
    import importlib
    import world as worldmod
    if _W is None:
        import contextlib as _c
        with _c.redirect_stderr(io.StringIO()):
            _W = worldmod.World(os.path.join(KIT, "arena"))
    cfg = load_config()
    botmod = importlib.import_module(botfile)

    class Cached(worldmod.World):
        def __new__(cls, *a, **k):
            return _W

        def __init__(self, *a, **k):
            pass

    botmod.World = Cached
    sim = Sim(cfg, _W, seed, botmod.Bot, all_hunt_t=all_hunt)
    place = sim.run()
    ms = sorted(sim.bot_ms)
    me = sim.me
    recent = [x for x in sim.my_dmg_log if sim.t - x[0] < 10]
    att = sorted({(x[1], x[2]) for x in recent})
    diag = dict(dealt={k: round(v) for k, v in sim.my_dealt.items()}, shots=dict(sim.my_shots), fight_s=round(sim.my_fight_ticks * 0.1, 1), alive=me.alive, hunters=len(sim.hunters), attackers=[a[1][0] for a in att],
                dmg10=round(sum(x[3] for x in recent)), mode=getattr(sim.bot, "mode", "?"),
                maxhp=round(me.max_hp), rear_frac=round(sum(x[3] for x in sim.my_dmg_log if x[4]) / max(1, sum(x[3] for x in sim.my_dmg_log)), 2), zone_r=round(sim.zone()["radius"]))
    return dict(diag=diag, seed=seed, place=place, t=sim.t, kills=sim.my_kills, cubes=me.cubes,
                weapons=[w and w["kind"] for w in me.weapons], upg=me.upgrades,
                p99=ms[int(len(ms) * 0.99)] if ms else 0, mx=ms[-1] if ms else 0, err=sim.bot_errors)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--all-hunt", type=float, default=300.0)
    ap.add_argument("--bot", default="bot")
    ap.add_argument("--passive", action="store_true", help="house bots that aren't hunting only fight back")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()
    import multiprocessing as mp
    if args.passive:
        os.environ["SIM_PASSIVE"] = "1"
    jobs = [(args.seed * 1000 + k, args.all_hunt, args.bot) for k in range(args.matches)]
    with mp.Pool(args.jobs) as pool:
        res = pool.map(_run_one, jobs)
    pts = []
    for r in res:
        pts.append(16 - r["place"])
        print(f"seed {r['seed']}: place {r['place']:2d} t {r['t']:5.1f} kills {r['kills']} cubes {r['cubes']} "
              f"weapons {r['weapons']} upg {r['upg']} p99 {r['p99']:.1f}ms max {r['mx']:.1f}ms  DIAG {r['diag']}", flush=True)
    n = len(pts)
    mean = sum(pts) / n
    sd = (sum((p - mean) ** 2 for p in pts) / max(1, n - 1)) ** 0.5
    errs = sum(r["err"] for r in res)
    if errs:
        print(f"!!! {errs} bot exceptions")
    print(f"mean points {mean:.2f} +- {sd / n ** 0.5:.2f} over {n}  (mean t {sum(r['t'] for r in res) / n:.0f}s, kills {sum(r['kills'] for r in res) / n:.2f})")


if __name__ == "__main__":
    main()
