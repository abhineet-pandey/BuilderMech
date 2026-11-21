"""Royale bot: loot early, fight one hunter at a time, break line of sight to regenerate, respect the zone."""
import math
import random
import sys
import time

import numpy as np

import mechfight as mf
from world import World

TAU = 2 * math.pi
import os as _os
P_RET = float(_os.environ.get("P_RET", "0"))
P_RET_OUT = float(_os.environ.get("P_RET_OUT", "0.7"))
P_AVOID = _os.environ.get("P_AVOID", "0") == "1"
P_OUTNUM = float(_os.environ.get("P_OUTNUM", "0"))
P_LASER_R = float(_os.environ.get("P_LASER_R", "80"))
P_GUN_R = float(_os.environ.get("P_GUN_R", "35"))
P_LIGHT = _os.environ.get("P_LIGHT", "1") == "1"
P_MAXLOS = int(_os.environ.get("P_MAXLOS", "99"))  # this many hostiles with a clear shot forces a retreat
P_SEEK = _os.environ.get("P_SEEK", "0") == "1"  # with nothing in view, go after remembered enemies / gunfire
P_FINISH = _os.environ.get("P_FINISH", "1") == "1"  # stick with and close in on a badly hurt target
P_CHAIN = _os.environ.get("P_CHAIN", "1") == "1"  # with lightning, prefer targets standing near other enemies
P_AIMPRED = _os.environ.get("P_AIMPRED", "1") == "1"  # gun: fire if the aim will be on target after one tick of slew
P_TOL = float(_os.environ.get("P_TOL", "0.9"))  # gun fire gate: allowed aim error, in target radii
P_PRED_T = float(_os.environ.get("P_PRED_T", "1"))  # how many sim ticks ahead the predicted aim looks
P_LPRED = _os.environ.get("P_LPRED", "0") == "1"  # laser: release on the predicted aim too
P_LOAD = _os.environ.get("P_LOAD", "def")  # loadout: def (lightning+laser) | gunlight (gun+lightning) | gunlaser
P_UPG = _os.environ.get("P_UPG", "def")  # upgrade order: def (armor first) | off (gun damage first)
P_FARM_HP = float(_os.environ.get("P_FARM_HP", "0.9"))  # only shoot crates/drones above this HP (lets regen run)
P_GRAB = _os.environ.get("P_GRAB", "1") == "1"  # pick up nearby cubes in the middle of a fight
P_GRAB_R = float(_os.environ.get("P_GRAB_R", "45"))  # how far (m) we detour for a cube mid-fight
P_CUBEW = float(_os.environ.get("P_CUBEW", "1"))  # weight of cubes / drones / crates when looting
P_CUBEW_C = float(_os.environ.get("P_CUBEW_C", "2"))  # extra weight on loose cubes only when looting
P_ENGAGE = float(_os.environ.get("P_ENGAGE", "90"))  # ignore enemies farther than this unless they shot us recently
P_NPCAVOID = _os.environ.get("P_NPCAVOID", "1") == "1"  # while healing, keep away from drones (they shoot within 45 m)
P_HOLD = _os.environ.get("P_HOLD", "0") == "1"  # nothing to do: stand still (silent) instead of walking laps
P_EXPLORE = _os.environ.get("P_EXPLORE", "0") == "1"  # nothing to do: visit unexplored parts of the circle (find loot)
P_DROP_R = float(_os.environ.get("P_DROP_R", "140"))  # how far (m) we go for a supply drop
P_CUBECAP = int(_os.environ.get("P_CUBECAP", "999"))  # stop chasing cubes at this many (past stat_cap they only make us bigger)
P_LBOOST = _os.environ.get("P_LBOOST", "1") == "1"
P_HEALHIDE = float(_os.environ.get("P_HEALHIDE", "0"))  # nothing on us and HP below this: hide from known enemies and regen
P_AIMHOST = _os.environ.get("P_AIMHOST", "0") == "1"
P_HURTSKIP = float(_os.environ.get("P_HURTSKIP", "0"))  # below this HP, don't start fights with mechs that aren't shooting us
P_HEALSTILL = _os.environ.get("P_HEALSTILL", "0") == "1"  # hurt with no known enemy around: stand still and quiet  # leave alone mechs that aren't aiming at / shooting / near us
P_LDODGE = _os.environ.get("P_LDODGE", "0") == "1"  # sidestep enemy lasers that are charging and aimed at us
P_FACE = float(_os.environ.get("P_FACE", "0"))  # target-choice penalty per active attacker we'd turn our back on
# --- free-for-all (qualifying / final) mode. Practice house bots never kill each other, so mechs dying that we
# didn't kill means a real free-for-all: there, survival pays and nobody is hunting us specifically.
P_FFA = _os.environ.get("P_FFA", "auto")  # auto (detect) | 0 (never) | 1 (always)
P_FFA_DEATHS = int(_os.environ.get("P_FFA_DEATHS", "3"))  # deaths not by us that switch the mode on
P_FFA_ENG = float(_os.environ.get("P_FFA_ENG", "40"))  # FFA: only take on non-hostile mechs this close
P_FFA_WEAK = float(_os.environ.get("P_FFA_WEAK", "0.3"))  # FFA: ...or ones this badly hurt (cheap kill, cubes)
P_FFA_RET = float(_os.environ.get("P_FFA_RET", "0"))  # FFA: retreat below this HP fraction
P_FFA_RET_OUT = float(_os.environ.get("P_FFA_RET_OUT", "0.8"))
P_FFA_AVOID = _os.environ.get("P_FFA_AVOID", "1") == "1"  # FFA: hold fire on mechs that aren't fighting us
P_FFA_UPG = _os.environ.get("P_FFA_UPG", "hp")  # FFA upgrade order: hp (plating first) | def  # 0: no boosting in a fight while holding lightning (save energy for arcs)

# How much we like holding each weapon (mortar is a liability: enemy lasers cook it off).
WEAPON_VALUE = {"lightning": 3.2, "laser": 3.0, "gun": 2.0, "mortar": 0.0}
if P_LOAD == "gunlight":
    WEAPON_VALUE = {"lightning": 3.2, "gun": 2.0, "laser": 1.0, "mortar": 0.0}
elif P_LOAD == "gunlaser":
    WEAPON_VALUE = {"laser": 3.0, "gun": 2.0, "lightning": 1.0, "mortar": 0.0}

UPGRADE_OFFENSE = [
    "gun_damage", "gun_rate", "lightning_efficiency", "laser_damage", "armor", "plating", "laser_charge",
    "lightning_range", "lightning_chain", "gun_velocity", "gyros", "gun_mag", "nanites", "capacitor",
    "reactor", "servos", "jets", "sensors", "mortar_reload", "mortar_splash", "mortar_velocity", "laser_pierce",
]

UPGRADE_HEAL = [
    "armor", "nanites", "plating", "laser_damage", "lightning_efficiency", "gun_damage",
    "gun_rate", "laser_charge", "lightning_range", "gyros", "lightning_chain", "gun_mag",
    "gun_velocity", "capacitor", "reactor", "servos", "jets", "sensors", "mortar_reload",
    "mortar_splash", "mortar_velocity", "laser_pierce",
]

UPGRADE_FFA = [
    "plating", "armor", "nanites", "laser_damage", "gun_damage", "gun_rate", "laser_charge", "gyros",
    "lightning_efficiency", "gun_mag", "gun_velocity", "lightning_range", "lightning_chain", "capacitor",
    "reactor", "servos", "jets", "sensors", "mortar_reload", "mortar_splash", "mortar_velocity", "laser_pierce",
]

UPGRADE_PRIORITY = [
    "armor", "plating", "laser_damage", "lightning_efficiency", "nanites", "gun_damage",
    "gun_rate", "laser_charge", "lightning_range", "gyros", "lightning_chain", "gun_mag",
    "gun_velocity", "capacitor", "reactor", "servos", "jets", "sensors", "mortar_reload",
    "mortar_splash", "mortar_velocity", "laser_pierce",
]


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def norm2(x, y):
    d = math.hypot(x, y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Bot:
    name = "royale-py"

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        # P_SEED salts the RNG: same strategy, different (deterministic) dice on the fixed practice seeds
        self.rng = random.Random((hello.get("seed", 0) or 0) ^ (int(_os.environ.get("P_SEED", "0")) * 0x9E3779B1))
        self.world = World(hello["map"]["dir"])
        self.mech_cfg = cfg["mech"]
        self.grav = cfg["mech"]["gravity"]
        self.loot_r = cfg["loot"]["pickup_radius"]
        self.cube_r = cfg["cubes"]["pickup_radius"]
        self.size_per_cube = cfg["cubes"].get("size_per_cube", 0.05)
        # memory
        self.enemies = {}  # id -> dict(pos, vel, t, hp, max_hp, weapons, cubes)
        self.pickups = {}  # id -> (kind, pos, t_seen)
        self.cubes = {}  # id -> (pos, t_seen)
        self.crates = {}  # id -> (pos, hp, t_seen)
        self.threats = []  # (t, bearing, by)
        self.last_attacker = None
        self.last_attacker_t = -99
        # motion state
        self.strafe_sign = 1
        self.strafe_until = 0.0
        self.stuck_ticks = 0
        self.unstick_until = -1.0
        self.unstick_dir = (1.0, 0.0)
        self.avoid_side = 1
        self.avoid_until = -1.0
        self.jetting = False
        self.last_move = (0.0, 0.0)
        self.bad_take = {}  # pickup id -> t
        self.last_t = 0.0
        self.mode = "start"
        self.retreating = False
        self.hostile = {}  # enemy id -> last time it damaged us
        self._rays = None
        self.cur_target = None
        self.visited = set()  # 40 m cells we have walked through
        self._explore_key = None
        self._explore_cells = []
        self.n_mechs = cfg.get("match", {}).get("mechs", 16)
        self.kills = 0
        self.ffa = P_FFA == "1"

    # ------------------------------------------------------------------ helpers
    def zone_now(self, z, t):
        """Current zone circle at time t (interpolated during a shrink)."""
        if t <= z["shrink_start"] or z["shrink_end"] <= z["shrink_start"]:
            return z["center"], z["radius"]
        f = min(1.0, (t - z["shrink_start"]) / (z["shrink_end"] - z["shrink_start"]))
        # obs.zone.center/radius is already the live circle during a shrink; next is the target
        return z["center"], z["radius"]

    def safe_circle(self, obs):
        """The circle we should plan to be in: the next circle once its shrink is close."""
        z = obs["zone"]
        t = obs["t"]
        if t > z["shrink_start"] - 25:
            return z["next_center"], z["next_radius"]
        return z["center"], z["radius"]

    def in_circle(self, p, c, r, margin=0.0):
        return math.hypot(p[0] - c[0], p[1] - c[1]) < r - margin

    def center_h(self, m):
        scale = 1 + self.size_per_cube * (m.get("cubes") or 0)
        return m["pos"][2] + 4.5 * scale

    # ------------------------------------------------------------------ main
    def act(self, obs):
        try:
            return self._act(obs)
        except Exception as ex:  # never leave the mech coasting on a stale action
            self._errs = getattr(self, "_errs", 0) + 1
            if self._errs <= 3:
                import traceback
                log(f"act failed ({ex!r}), using fallback: " + traceback.format_exc())
            return self.fallback(obs)

    def fallback(self, obs):
        """Minimal safe behaviour: head for the zone, shoot the nearest visible mech with the gun."""
        act = {"tick": obs["tick"]}
        try:
            me = obs["self"]
            pos = me["pos"]
            z = obs["zone"]
            c = z.get("next_center") or z["center"]
            vx, vy = norm2(c[0] - pos[0], c[1] - pos[1])
            act["move"] = [vx, vy] if math.hypot(c[0] - pos[0], c[1] - pos[1]) > 10 else [0.0, 0.0]
            foes = [m for m in obs.get("mechs") or [] if m.get("kind") == "mech"]
            fire = [False, False]
            if foes:
                f = min(foes, key=lambda m: math.dist(pos, m["pos"]))
                dx, dy = f["pos"][0] - pos[0], f["pos"][1] - pos[1]
                dz = f["pos"][2] + 4.5 - (pos[2] + 7.0)
                act["aim"] = [math.atan2(dy, dx), max(-0.7, min(1.3, math.atan2(dz, math.hypot(dx, dy))))]
                for i, w in enumerate(me.get("weapons") or []):
                    if w and w.get("kind") == "gun" and i < 2:
                        fire[i] = math.hypot(dx, dy) < 150
            act["fire"] = fire
            if obs.get("offer"):
                act["pick"] = 0
        except Exception:
            pass
        return act

    def _act(self, obs):
        t0 = time.perf_counter()
        me = obs["self"]
        t = obs["t"]
        dt = max(0.05, t - self.last_t) if self.last_t else 0.1
        self.last_t = t
        pos = me["pos"]
        st = me["stats"]
        eye_h = st.get("eye_height", 8.0)
        muz_h = st.get("muzzle_height", 7.0)
        eye = (pos[0], pos[1], pos[2] + eye_h)
        muz = (pos[0], pos[1], pos[2] + muz_h)
        act = {"tick": obs["tick"]}
        self.update_memory(obs)
        self.visited.add((int(math.floor(pos[0] / 40.0)), int(math.floor(pos[1] / 40.0))))

        # --- upgrade offers
        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])

        weapons = me["weapons"]
        held = {w["kind"]: i for i, w in enumerate(weapons) if w}

        # --- classify visible actors
        enemies = []
        npcs = []
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                enemies.append(m)
            else:
                npcs.append(m)
        epts = [(m["pos"][0], m["pos"][1], self.center_h(m)) for m in enemies]
        etops = [(m["pos"][0], m["pos"][1], m["pos"][2] + 8.0) for m in enemies]
        los_c = self.world.los_many(muz, epts)
        los_t = self.world.los_many(eye, etops)
        for m, a, b in zip(enemies, los_c, los_t):
            m["_d"] = math.dist(pos, m["pos"])
            m["_los"] = bool(a)
            m["_seen"] = bool(a or b)

        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        self._pos = pos
        self._hpf = hp_frac
        target = self.pick_target(enemies, t, weapons)

        # --- weapon management: take good pickups in reach
        self.manage_pickups(obs, act, weapons, held)

        # --- decide movement mode
        want = (0.0, 0.0)
        boost = False
        aim_pt = None  # 3D point we want to shoot at
        aim_kind = None  # 'mech' | 'npc' | 'crate'
        aim_actor = None
        threats_near = [m for m in enemies if m["_d"] < 170]
        n_los = sum(1 for m in threats_near if m["_los"])
        sc, sr = self.safe_circle(obs)
        zc, zr = obs["zone"]["center"], obs["zone"]["radius"]

        # hostile = shot at us recently, or close and coming
        for m in enemies:
            m["_hostile"] = (t - self.hostile.get(m["id"], -99) < 12) or m["_d"] < 60
        hostiles = [m for m in threats_near if m["_hostile"]]
        n_host_los = sum(1 for m in hostiles if m["_los"])

        # retreat with hysteresis: pull out when hurt or outnumbered, come back only when healed
        tgt_frac = ((target.get("hp") or 1400) / max(1.0, target.get("max_hp") or 1400)) if target else 1.0
        p_ret, p_ret_out = (P_FFA_RET, P_FFA_RET_OUT) if self.ffa else (P_RET, P_RET_OUT)
        enter = (hp_frac < p_ret and not (tgt_frac < 0.2 and target and target["_d"] < 60)) \
            or (n_host_los >= 2 and hp_frac < P_OUTNUM) or n_host_los >= P_MAXLOS
        if threats_near and enter:
            self.retreating = True
        if self.retreating and (hp_frac > p_ret_out or not threats_near and hp_frac > 0.75):
            self.retreating = False
        hold_fire = False

        if target is not None:
            aim_actor = target
            aim_kind = "mech"
            if self.retreating:
                self.mode = "retreat"
                want, boost, hidden = self.retreat_move(obs, threats_near or [target], sc, sr)
                # cornered: someone is right on us with a clear shot, fight back
                cornered = any(m["_d"] < 40 and m["_los"] for m in threats_near)
                hold_fire = not cornered and (hidden or hp_frac < 0.5)
            elif (P_AVOID and not target["_hostile"] and (hp_frac < 0.8 or len(threats_near) > 1)) or                     (self.ffa and P_FFA_AVOID and t - self.hostile.get(target["id"], -99) > 8 and tgt_frac >= P_FFA_WEAK
                     and target["_d"] > 25):
                # don't start fights we don't need: keep looting but stay out of its way
                self.mode = "avoid"
                want, boost, aim_actor2, aim_kind2 = self.loot_move(obs, npcs, hp_frac, sc, sr)
                hold_fire = True
            else:
                self.mode = "fight"
                want, boost = self.fight_move(obs, target, weapons, sc, sr)
        else:
            self.mode = "loot"
            want, boost, aim_actor, aim_kind = self.loot_move(obs, npcs, hp_frac, sc, sr)
            if P_HEALHIDE and hp_frac < P_HEALHIDE and t - self.last_attacker_t > 2.0:
                # regen needs 5 s untouched; house bots only know where we are while one of them sees us
                known = []
                for eid, e in self.enemies.items():
                    if t - e["t"] < 15:
                        d_e = math.dist(pos, e["pos"])
                        if d_e < 220:
                            known.append({"pos": e["pos"], "_d": d_e})
                cube_near = any(math.hypot(c[0][0] - pos[0], c[0][1] - pos[1]) < 12 for c in self.cubes.values())
                if known and not cube_near:
                    want, boost, _hid = self.retreat_move(obs, known, sc, sr)
                    boost = False  # boosting delays energy regen and makes noise
                    aim_actor = aim_kind = None
                    self.mode = "healhide"
                elif P_HEALSTILL and not cube_near:
                    want, boost = (0.0, 0.0), False
                    aim_actor = aim_kind = None
                    self.mode = "healstill"
            if P_NPCAVOID and hp_frac < P_FARM_HP and aim_kind != "npc":
                # drones fire at any mech within 45 m; getting hit resets the 5 s regen timer
                rx = ry = 0.0
                for n in npcs:
                    dn = math.hypot(n["pos"][0] - pos[0], n["pos"][1] - pos[1])
                    if 0.1 < dn < 55:
                        k = (55 - dn) / 55 * 1.6
                        rx += (pos[0] - n["pos"][0]) / dn * k
                        ry += (pos[1] - n["pos"][1]) / dn * k
                if rx or ry:
                    want = norm2(want[0] + rx, want[1] + ry)
                    self.mode += "+avoidnpc"

        # --- supply drop: go take it, and once inside the ring stay there until it's ours
        dmove = self.drop_plan(obs, enemies, hp_frac, sc, sr)
        if dmove is not None:
            want, boost = dmove
            self.mode += "+drop"

        # --- zone pressure overrides everything (margins shrink with the circle, so a tiny
        # final circle doesn't make us boost back and forth across the center)
        dz = math.hypot(pos[0] - zc[0], pos[1] - zc[1])
        zmargin = min(4.0, zr * 0.3)
        smargin = min(6.0, sr * 0.3)
        dsc = math.hypot(pos[0] - sc[0], pos[1] - sc[1])
        if dz > zr - zmargin or not self.in_circle(pos, sc, sr, smargin) and t > obs["zone"]["shrink_start"] - 12:
            vx, vy = norm2(sc[0] - pos[0], sc[1] - pos[1])
            if dz > zr - zmargin:
                want = (vx, vy)
                boost = me["energy"] > 15 and dz > zr + 2
                self.mode += "+zone!"
            else:
                want = norm2(want[0] * 0.4 + vx, want[1] * 0.4 + vy)
                self.mode += "+zone"
        if sr < 8 and dsc < 1.5 and dz <= zr + 1.5:
            # final circle and we're at its center: stand still, save energy, make no noise
            want, boost = (0.0, 0.0), False
            self.mode += "+hold"

        # --- dodge mortar shells
        dodge = self.dodge_shells(obs)
        if dodge is not None:
            want = norm2(want[0] * 0.3 + dodge[0], want[1] * 0.3 + dodge[1])
            boost = me["energy"] > 25

        if P_LDODGE:
            ld = self.dodge_lasers(obs, enemies)
            if ld is not None:
                want = norm2(want[0] * 0.25 + ld[0], want[1] * 0.25 + ld[1])
                boost = boost or me["energy"] > 30
                self.mode += "+ldodge"

        # --- steering (walls, jets, stuck)
        move, jump, boost = self.steer(obs, want, boost)
        act["move"] = [round(move[0], 4), round(move[1], 4)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)

        # --- aiming and firing
        fire = [False, False]
        aim = None
        if aim_actor is not None:
            aim, fire = self.shoot(obs, aim_actor, aim_kind, weapons)
        if aim is None:
            aim = self.idle_aim(obs, move)
            fire = self.laser_release_idle(weapons)
        if hold_fire:
            fire = self.laser_release_idle(weapons)
        act["aim"] = [round(aim[0], 4), round(max(self.mech_cfg["min_pitch"], min(self.mech_cfg["max_pitch"], aim[1])), 4)]
        act["fire"] = fire

        # reload when it's cheap
        g = held.get("gun")
        if g is not None:
            gw = weapons[g]
            if gw.get("reload", 0) == 0 and gw.get("ammo", 1) < gw["stats"]["mag"]:
                ammo = gw.get("ammo", 1)
                mag = gw["stats"]["mag"]
                # empty, or a quiet moment: nothing in sight, or hiding behind cover
                if ammo == 0 or (target is None and ammo < mag * 0.6) or (hold_fire and ammo < mag * 0.75):
                    act["reload"] = True

        el = (time.perf_counter() - t0) * 1000
        mf.draw.label(f"{self.mode} hp{me['hp']:.0f} e{me['energy']:.0f} c{me['cubes']} {el:.1f}ms")
        return act

    # ------------------------------------------------------------------ memory
    def update_memory(self, obs):
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                self.enemies[m["id"]] = {"pos": m["pos"], "vel": m["vel"], "t": t, "hp": m.get("hp"),
                                         "weapons": m.get("weapons", []), "cubes": m.get("cubes", 0)}
        for e in obs["events"]:
            ty = e["type"]
            if ty == "damaged" and e.get("by") is not None:
                self.threats.append((t, e["bearing"], e["by"]))
                if e["by"] < 100:
                    self.hostile[e["by"]] = t
                self.last_attacker = e["by"]
                self.last_attacker_t = t
            elif ty == "kill":
                self.enemies.pop(e["target"], None)
                if e["target"] < 100:
                    self.kills += 1
            elif ty == "take_failed":
                self.bad_take[e["id"]] = t
                if e.get("reason") == "gone":
                    self.pickups.pop(e["id"], None)
            elif ty == "took":
                self.pickups.pop(e["id"], None)
        self.threats = [x for x in self.threats if t - x[0] < 6]
        if P_FFA == "auto" and not self.ffa and obs.get("alive") is not None:
            # drones and the odd stray shot kill a house bot now and then, so ask for several, early
            others = (self.n_mechs - obs["alive"]) - self.kills
            if (others >= P_FFA_DEATHS and t < 60) or (others >= P_FFA_DEATHS + 1 and t < 120):
                self.ffa = True
                log(f"FFA mode on at t={t:.1f}")
        # loot memory
        seen_p = set()
        for p in obs["pickups"]:
            self.pickups[p["id"]] = (p["kind"], p["pos"], t)
            seen_p.add(p["id"])
        seen_c = set()
        for c in obs["cubes"]:
            self.cubes[c["id"]] = (c["pos"], t)
            seen_c.add(c["id"])
        seen_k = set()
        for c in obs["crates"]:
            self.crates[c["id"]] = (c["pos"], c.get("hp", 300), t)
            seen_k.add(c["id"])
        # forget things we should be seeing but aren't (taken by someone, destroyed)
        eye = (pos[0], pos[1], pos[2] + 8)
        for store, seen, zoff in ((self.pickups, seen_p, 1.0), (self.cubes, seen_c, 0.5), (self.crates, seen_k, 1.2)):
            for k in list(store.keys()):
                if k in seen:
                    continue
                v = store[k]
                p = v[1] if store is self.pickups else v[0]
                ts = v[-1]
                d = math.hypot(p[0] - pos[0], p[1] - pos[1])
                if d < 40 and t - ts > 0.5:
                    del store[k]
                elif t - ts > (40 if store is self.cubes else 240):
                    del store[k]
        for k in list(self.enemies.keys()):
            if t - self.enemies[k]["t"] > 20:
                del self.enemies[k]

    # ------------------------------------------------------------------ targeting
    def pick_target(self, enemies, t, weapons=()):
        lightning = None
        for w in weapons:
            if w and w["kind"] == "lightning":
                lightning = w["stats"]
        best = None
        best_s = 1e9
        for m in enemies:
            d = m["_d"]
            if d > 260:
                continue
            frac = (m.get("hp") or 1400) / max(1.0, m.get("max_hp") or 1400)
            if P_HURTSKIP and self._hpf < P_HURTSKIP and t - self.hostile.get(m["id"], -99) > 6 and d > 35 and frac >= 0.35:
                continue  # hurt: let regen run instead of picking a new fight
            if P_AIMHOST and not self.ffa and t - self.hostile.get(m["id"], -99) > 12 and d > 45 and frac >= 0.35:
                # a house bot that isn't hunting us looks elsewhere; starting a fight with it adds an attacker
                to_me = math.atan2(self._pos[1] - m["pos"][1], self._pos[0] - m["pos"][0])
                if abs(wrap((m.get("aim") or [0, 0])[0] - to_me)) > 0.3 and m["id"] != self.cur_target:
                    continue
            if self.ffa:
                if t - self.hostile.get(m["id"], -99) > 8 and d > P_FFA_ENG and not (frac < P_FFA_WEAK and d < 100 and m["_los"]):
                    continue  # FFA: let the others fight; only the ones on us, close, or nearly dead
            elif d > P_ENGAGE and t - self.hostile.get(m["id"], -99) > 12:
                continue  # far away and not shooting at us: not our fight (yet)
            s = d / 60.0 + frac * 2.0
            if not m["_los"]:
                s += 2.5
            if m["id"] == self.last_attacker and t - self.last_attacker_t < 3:
                s -= 1.5
            if P_FINISH and m["id"] == self.cur_target:
                s -= 0.6  # don't flip between targets
                if frac < 0.35 and d < 200:
                    s -= 4.0  # nearly dead: finish it
            if P_CHAIN and lightning and d < lightning.get("range", 45) * 1.5:
                cr = lightning.get("chain_range", 14)
                near = sum(1 for o in enemies if o is not m and math.dist(o["pos"], m["pos"]) < cr)
                s -= 0.8 * min(near, int(lightning.get("chain", 2)))
            if P_FACE:
                # facing m puts anyone behind us on our rear armor (x1.5): count attackers more than 90 deg off
                bm = math.atan2(m["pos"][1] - self._pos[1], m["pos"][0] - self._pos[0])
                for o in enemies:
                    if o is m or t - self.hostile.get(o["id"], -99) > 4 or o["_d"] > 150:
                        continue
                    bo = math.atan2(o["pos"][1] - self._pos[1], o["pos"][0] - self._pos[0])
                    if abs(wrap(bo - bm)) > math.pi / 2:
                        s += P_FACE
            if s < best_s:
                best_s, best = s, m
        self.cur_target = best["id"] if best is not None else None
        return best

    def pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        for u in (UPGRADE_FFA if self.ffa and P_FFA_UPG == "hp" else UPGRADE_OFFENSE if P_UPG == "off" else UPGRADE_HEAL if P_UPG == "heal" else UPGRADE_PRIORITY):
            if u in ids:
                return ids.index(u)
        return 0

    # ------------------------------------------------------------------ loot
    def manage_pickups(self, obs, act, weapons, held):
        me = obs["self"]
        pos = me["pos"]
        t = obs["t"]
        best = None
        for p in obs["pickups"]:
            if math.hypot(p["pos"][0] - pos[0], p["pos"][1] - pos[1]) > self.loot_r - 0.3:
                continue
            if abs(p["pos"][2] - pos[2]) > 3.8:
                continue
            if t - self.bad_take.get(p["id"], -99) < 1.0:
                continue
            gain, slot = self.pickup_gain(p["kind"], weapons, held)
            if gain > 0 and (best is None or gain > best[0]):
                best = (gain, p["id"], slot)
        if best:
            act["take"] = best[1]
            if best[2] is not None:
                act["drop"] = best[2]

    def pickup_gain(self, kind, weapons, held):
        """(gain, slot_to_drop or None). gain <= 0 means don't want it."""
        if kind in held:
            return 0, None
        v = WEAPON_VALUE.get(kind, 0)
        for i, w in enumerate(weapons):
            if w is None:
                return v, None
        # both full: replace the worst one if clearly better (not a charging laser)
        worst_i, worst_v = None, 1e9
        for i, w in enumerate(weapons):
            if w["kind"] == "laser" and w.get("charge", 0) > 0:
                continue
            wv = WEAPON_VALUE.get(w["kind"], 0)
            if wv < worst_v:
                worst_i, worst_v = i, wv
        if worst_i is not None and v > worst_v + 0.5:
            return v - worst_v, worst_i
        return 0, None

    def loot_move(self, obs, npcs, hp_frac, sc, sr):
        """No enemy mech in sight: collect cubes, weapons, crates, NPCs, supply drops; else roam."""
        me = obs["self"]
        pos = me["pos"]
        t = obs["t"]
        weapons = me["weapons"]
        held = {w["kind"]: i for i, w in enumerate(weapons) if w}
        margin = 10.0
        cands = []  # (score, point, kind, actor)
        for cid, (p, ts) in (self.cubes.items() if me["cubes"] < P_CUBECAP else ()):
            if self.in_circle(p, sc, sr, margin):
                d = math.dist(p, pos)
                cands.append((12.0 * P_CUBEW * P_CUBEW_C / (d + 15), p, "cube", None))
        for pid, (kind, p, ts) in self.pickups.items():
            gain, _ = self.pickup_gain(kind, weapons, held)
            if gain <= 0 or t - self.bad_take.get(pid, -99) < 3:
                continue
            if self.in_circle(p, sc, sr, margin):
                d = math.dist(p, pos) + max(0.0, p[2] - pos[2]) * 1.5
                cands.append((gain * 3.5 / (d + 20), p, "pickup", None))
        has_weapon = any(w for w in weapons)
        if hp_frac > P_FARM_HP and has_weapon:
            for n in npcs:
                p = n["pos"]
                if self.in_circle(p, sc, sr, margin):
                    d = math.dist(p, pos)
                    cands.append((7.0 * P_CUBEW / (d + 30), p, "npc", n))
            for kid, (p, hp, ts) in self.crates.items():
                if self.in_circle(p, sc, sr, margin):
                    d = math.dist(p, pos)
                    cands.append((5.0 * P_CUBEW / (d + 25), p, "crate", {"pos": p, "id": kid, "hp": hp}))
        for dr in obs.get("drops") or []:
            p = dr["pos"]
            d = math.hypot(p[0] - pos[0], p[1] - pos[1])
            travel = d / 11.0
            if dr["lands_in"] < travel + 8 and d < 260 and hp_frac > 0.25 and self.in_circle(p, sc, sr, 0):
                cands.append((25.0 / (d + 30) * (1.6 - hp_frac), p, "drop", dr))
        if P_SEEK and hp_frac > 0.7 and has_weapon:
            # hunt: the nearest enemy we saw recently, else recent gunfire we heard
            best_e = None
            for eid, e in self.enemies.items():
                age = t - e["t"]
                if age < 15 and self.in_circle(e["pos"], sc, sr, margin):
                    d = math.dist(e["pos"], pos)
                    if best_e is None or d < best_e[0]:
                        best_e = (d, e["pos"])
            if best_e is not None:
                cands.append((6.0 / (best_e[0] + 40), best_e[1], "hunt", None))
            else:
                for snd in obs["sounds"]:
                    if snd["kind"] in ("gun", "laser", "lightning", "mortar") and snd["dist"] != "far":
                        r = 45.0 if snd["dist"] == "near" else 110.0
                        p = (pos[0] + math.cos(snd["bearing"]) * r, pos[1] + math.sin(snd["bearing"]) * r, pos[2])
                        if self.in_circle(p, sc, sr, margin):
                            cands.append((4.0 / (r + 40), p, "hunt", None))
                            break
        aim_actor = None
        aim_kind = None
        if cands:
            cands.sort(key=lambda c: -c[0])
            _, p, kind, actor = cands[0]
            d = math.hypot(p[0] - pos[0], p[1] - pos[1])
            if kind in ("npc", "crate"):
                aim_actor, aim_kind = actor, kind
                stand = 32.0 if kind == "npc" else 14.0
                if "lightning" in held:
                    stand = 22.0 if kind == "npc" else 10.0
                if d > stand:
                    want = norm2(p[0] - pos[0], p[1] - pos[1])
                else:
                    # circle at range
                    ux, uy = norm2(p[0] - pos[0], p[1] - pos[1])
                    self.update_strafe(t)
                    want = (-uy * self.strafe_sign * 0.8 + ux * (d - stand) / stand,
                            ux * self.strafe_sign * 0.8 + uy * (d - stand) / stand)
                    want = norm2(*want)
                self.mode = "loot-" + kind
                return want, False, aim_actor, aim_kind
            self.mode = "loot-" + kind
            want = norm2(p[0] - pos[0], p[1] - pos[1])
            if kind == "drop" and d < 3.0:
                want = (0.0, 0.0)
            if d < 1.0:
                want = (0.0, 0.0)
            boost = d > 40 and me["energy"] > 70 and kind in ("drop", "cube")
            return want, boost, None, None
        # roam: drift toward the safe circle center, wandering
        self.mode = "roam"
        target = sc
        d = math.hypot(target[0] - pos[0], target[1] - pos[1])
        if P_EXPLORE:
            goal = self.explore_goal(pos, sc, sr)
            if goal is not None:
                self.mode = "explore"
                return norm2(goal[0] - pos[0], goal[1] - pos[1]), False, None, None
        if P_HOLD and d < sr * 0.6:
            # safely inside the circle with nothing to do: stay put and quiet, let them come to us
            self.mode = "hold"
            return (0.0, 0.0), False, None, None
        if d < sr * 0.35:
            ang = t * 0.15 + (obs["self"]["id"] or 0)
            target = (sc[0] + math.cos(ang) * sr * 0.3, sc[1] + math.sin(ang) * sr * 0.3)
        want = norm2(target[0] - pos[0], target[1] - pos[1])
        return want, False, None, None

    def explore_goal(self, pos, sc, sr):
        """Center of the nearest unvisited 40 m cell inside the safe circle, on open ground."""
        w = self.world
        r = max(0.0, sr - 20.0)
        if r < 20:
            return None
        key = (round(sc[0]), round(sc[1]), round(r))
        if self._explore_key != key:  # the open cells only change when the circle does
            self._explore_key = key
            cells = []
            i0, i1 = int(math.floor((sc[0] - r) / 40.0)), int(math.floor((sc[0] + r) / 40.0))
            j0, j1 = int(math.floor((sc[1] - r) / 40.0)), int(math.floor((sc[1] + r) / 40.0))
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    cx, cy = i * 40.0 + 20.0, j * 40.0 + 20.0
                    if math.hypot(cx - sc[0], cy - sc[1]) > r or not w.in_bounds(cx, cy, 15):
                        continue
                    if w.height(cx, cy) - w.ground(cx, cy) > 2.0:
                        continue  # inside a building: skip
                    cells.append((i, j, cx, cy))
            self._explore_cells = cells
        best = None
        for i, j, cx, cy in self._explore_cells:
            if (i, j) in self.visited:
                continue
            d = (cx - pos[0]) ** 2 + (cy - pos[1]) ** 2
            if best is None or d < best[0]:
                best = (d, (cx, cy))
        return best[1] if best else None

    # ------------------------------------------------------------------ combat movement
    def desired_range(self, weapons, energy):
        kinds = {w["kind"] for w in weapons if w}
        if P_LIGHT and "lightning" in kinds and energy > 25:
            return 24.0
        if "laser" in kinds:
            return P_LASER_R
        if "gun" in kinds:
            return P_GUN_R
        if "mortar" in kinds:
            return 110.0
        return 120.0

    def update_strafe(self, t):
        if t >= self.strafe_until:
            self.strafe_sign = -self.strafe_sign if self.rng.random() < 0.75 else self.strafe_sign
            self.strafe_until = t + self.rng.uniform(0.45, 1.3)

    def fight_move(self, obs, target, weapons, sc, sr):
        me = obs["self"]
        pos = me["pos"]
        t = obs["t"]
        tp = target["pos"]
        d = math.hypot(tp[0] - pos[0], tp[1] - pos[1])
        ux, uy = norm2(tp[0] - pos[0], tp[1] - pos[1])
        rng = self.desired_range(weapons, me["energy"])
        tfrac = (target.get("hp") or 1400) / max(1.0, target.get("max_hp") or 1400)
        if P_FINISH and tfrac < 0.3:
            rng = min(rng, 20.0)  # close in for the kill: fewer misses, no time to heal
        if not target["_los"]:
            rng = min(rng, 45.0)  # close in to find it
        radial = max(-1.0, min(1.0, (d - rng) / max(15.0, rng * 0.5)))
        self.update_strafe(t)
        lat = 0.85
        wx = ux * radial + (-uy) * self.strafe_sign * lat
        wy = uy * radial + ux * self.strafe_sign * lat
        # stay inside the safe circle while fighting
        dc = math.hypot(pos[0] - sc[0], pos[1] - sc[1])
        if dc > sr * 0.85:
            cx, cy = norm2(sc[0] - pos[0], sc[1] - pos[1])
            k = min(1.5, (dc - sr * 0.85) / max(10.0, sr * 0.15))
            wx += cx * k
            wy += cy * k
        want = norm2(wx, wy)
        if P_GRAB and me["cubes"] < P_CUBECAP:
            # a cube within reach is worth a detour: +HP (heals the difference), +damage, upgrade progress
            best_c = None
            for cid, (cp, ts) in self.cubes.items():
                dc_ = math.hypot(cp[0] - pos[0], cp[1] - pos[1])
                if dc_ < P_GRAB_R and abs(cp[2] - pos[2]) < 4 and (best_c is None or dc_ < best_c[0]):
                    best_c = (dc_, cp)
            if best_c is not None:
                gx, gy = norm2(best_c[1][0] - pos[0], best_c[1][1] - pos[1])
                want = norm2(want[0] * 0.35 + gx, want[1] * 0.35 + gy)
        boost = d > rng + 40 and me["energy"] > 60 or (P_FINISH and tfrac < 0.3 and d > rng + 15 and me["energy"] > 30)
        if not P_LBOOST and any(w and w["kind"] == "lightning" for w in weapons) and d < 70:
            boost = False
        return want, boost

    def retreat_move(self, obs, threats, sc, sr):
        """Find the nearest spot that no threat can see and go there. Returns (want, boost, hidden_now)."""
        me = obs["self"]
        pos = me["pos"]
        w = self.world
        threats = sorted(threats, key=lambda m: m["_d"])[:4]
        # are we hidden right now?
        here = (pos[0], pos[1], pos[2] + 5.0)
        hidden_now = not any(w.los_many((m["pos"][0], m["pos"][1], m["pos"][2] + 8.0), [here])[0] for m in threats)
        # 16 rays out to 52 m, one vectorised sample of the height grid; a spot is reachable on foot
        # if nothing along its ray rises more than a step above our feet
        if self._rays is None:
            self._rays = (np.cos(np.arange(16) * TAU / 16), np.sin(np.arange(16) * TAU / 16), np.arange(1, 53, dtype=np.float64))
        cs, sn, rr = self._rays
        X = pos[0] + cs[:, None] * rr[None, :]
        Y = pos[1] + sn[:, None] * rr[None, :]
        Hs = w.sample(X, Y)
        cmx = np.maximum.accumulate(Hs, axis=1) - pos[2]
        zc_, zr_ = sc, sr
        cands = []
        for ri in (7, 15, 25, 37, 51):
            for k in range(16):
                if cmx[k, ri] > 1.2:
                    continue
                x, y = float(X[k, ri]), float(Y[k, ri])
                if not w.in_bounds(x, y, 6) or not self.in_circle((x, y), zc_, zr_, 4):
                    continue
                cands.append((x, y, float(Hs[k, ri])))
        best = None
        best_s = 1e9
        if cands:
            hidden = [True] * len(cands)
            pts = [(c[0], c[1], c[2] + 5.0) for c in cands]
            for m in threats:
                eye = (m["pos"][0], m["pos"][1], m["pos"][2] + 8.0)
                vis = w.los_many(eye, pts)
                hidden = [h and not v for h, v in zip(hidden, vis)]
            for c, hd in zip(cands, hidden):
                if not hd:
                    continue
                dme = math.hypot(c[0] - pos[0], c[1] - pos[1])
                near_threat = min(math.hypot(c[0] - m["pos"][0], c[1] - m["pos"][1]) for m in threats)
                s = dme - 0.4 * min(near_threat, 80.0)
                if s < best_s:
                    best_s, best = s, c
        if hidden_now:
            # stay put but drift away from the nearest threat a little
            m0 = threats[0]
            ax, ay = norm2(pos[0] - m0["pos"][0], pos[1] - m0["pos"][1])
            if m0["_d"] < 50 and self.path_clear(pos, ax, ay, 6) <= 1.2:
                return (ax * 0.6, ay * 0.6), False, True
            return (0.0, 0.0), False, True
        if best is not None:
            want = norm2(best[0] - pos[0], best[1] - pos[1])
        else:
            # run directly away from the threats' centroid, jinking
            cx = sum(m["pos"][0] for m in threats) / len(threats)
            cy = sum(m["pos"][1] for m in threats) / len(threats)
            want = norm2(pos[0] - cx, pos[1] - cy)
            self.update_strafe(obs["t"])
            want = norm2(want[0] - want[1] * 0.5 * self.strafe_sign, want[1] + want[0] * 0.5 * self.strafe_sign)
        return want, me["energy"] > 20, False

    def drop_plan(self, obs, enemies, hp_frac, sc, sr):
        """Movement for a supply drop worth taking now, or None.

        A capture needs us alone in the ring for capture_s; it pays 3 cubes plus full HP and energy,
        so once we're inside we stay (fighting from the ring) unless it's hopeless."""
        me = obs["self"]
        pos = me["pos"]
        t = obs["t"]
        my_id = me["id"]
        best = None
        for dr in obs.get("drops") or []:
            p = dr["pos"]
            r = dr.get("radius", 8.0)
            d = math.hypot(p[0] - pos[0], p[1] - pos[1])
            inside = d < r - 1.0
            zc, zr = obs["zone"]["center"], obs["zone"]["radius"]
            if not (self.in_circle(p, sc, sr, 2) or self.in_circle(p, zc, zr, 6)):
                continue
            if d > P_DROP_R or dr["lands_in"] > d / 11.0 + 4:
                continue
            # who else is in or near the ring?
            rivals = [m for m in enemies if math.hypot(m["pos"][0] - p[0], m["pos"][1] - p[1]) < r + 4]
            hostile_los = sum(1 for m in enemies if m["_los"] and m["_d"] < 120)
            holder = dr.get("holder")
            mine = holder == my_id
            if inside:
                # stay unless swarmed while hurt, or we're about to die before it completes
                left = dr.get("capture_s", 4.0) - (dr.get("progress", 0.0) if mine else 0.0)
                if hp_frac < 0.15 and left > 1.5:
                    continue
                if hostile_los >= 3 and hp_frac < 0.5 and not mine:
                    continue
            else:
                if self.retreating or hp_frac < 0.3:
                    continue
                if hostile_los >= 2:
                    continue
                if rivals and hp_frac < 0.6:
                    continue
                if holder is not None and not mine and dr.get("progress", 0) > 2.5 and d > 40:
                    continue  # someone nearly has it and we're far
            score = d - (60 if inside else 0) - (30 if mine else 0)
            if best is None or score < best[0]:
                best = (score, p, r, d, inside)
        if best is None:
            return None
        _, p, r, d, inside = best
        ux, uy = norm2(p[0] - pos[0], p[1] - pos[1])
        if not inside:
            return (ux, uy), d > 30 and me["energy"] > 45
        # inside: small strafe that keeps us well within the ring (harder to hit, never leaves)
        self.update_strafe(t)
        pull = max(0.0, (d - (r - 3.0)) / 2.0)
        wx = -uy * self.strafe_sign * 0.45 + ux * pull
        wy = ux * self.strafe_sign * 0.45 + uy * pull
        if d < 1.0:
            wx, wy = -uy * self.strafe_sign * 0.45, ux * self.strafe_sign * 0.45
        return (wx, wy), False

    def dodge_shells(self, obs):
        me = obs["self"]
        pos = me["pos"]
        g = self.cfg["weapons"]["mortar"]["gravity"]
        R = self.cfg["weapons"]["mortar"]["splash_radius"] + 4.0
        out = None
        for p in obs["projectiles"]:
            if p["kind"] != "mortar":
                continue
            x, y, z = p["pos"]
            vx, vy, vz = p["vel"]
            land = None
            for k in range(1, 80):
                tt = k * 0.05
                px, py, pz = x + vx * tt, y + vy * tt, z + vz * tt - 0.5 * g * tt * tt
                if pz <= self.world.height(px, py):
                    land = (px, py, tt)
                    break
            if land is None:
                continue
            fx = pos[0] + me["vel"][0] * land[2] * 0.5
            fy = pos[1] + me["vel"][1] * land[2] * 0.5
            d = math.hypot(fx - land[0], fy - land[1])
            if d < R:
                out = norm2(pos[0] - land[0], pos[1] - land[1])
                if out == (0.0, 0.0):
                    out = (1.0, 0.0)
        return out

    def dodge_lasers(self, obs, enemies):
        """A visible enemy charging a laser with its aim on us: step sideways off the line."""
        pos = obs["self"]["pos"]
        best = None
        for m in enemies:
            if not m.get("charging") or not m["_los"] or "laser" not in (m.get("weapons") or []):
                continue
            d = m["_d"]
            if d > 280:
                continue
            to_me = math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
            err = abs(wrap((m.get("aim") or [0, 0])[0] - to_me))
            if err * max(d, 1.0) > 6.0:  # its beam would miss us by more than ~2 radii
                continue
            if best is None or d < best[0]:
                best = (d, to_me)
        if best is None:
            return None
        ux, uy = math.cos(best[1]), math.sin(best[1])
        if self.obs_t_dodge_flip(obs["t"]):
            self.strafe_sign = -self.strafe_sign
        return (-uy * self.strafe_sign, ux * self.strafe_sign)

    def obs_t_dodge_flip(self, t):
        # keep one side for ~0.8 s so we actually get off the line, then maybe switch
        if t >= getattr(self, "_ldodge_until", -1):
            self._ldodge_until = t + 0.8
            return self.rng.random() < 0.5
        return False

    # ------------------------------------------------------------------ steering
    def path_clear(self, pos, dx, dy, look):
        n = max(3, int(look))
        hs = self.world.heights_along(pos[0] + dx * 1.2, pos[1] + dy * 1.2, pos[0] + dx * look, pos[1] + dy * look, n)
        return float(hs.max()) - pos[2]

    def steer(self, obs, want, boost):
        me = obs["self"]
        pos = me["pos"]
        t = obs["t"]
        vel = me["vel"]
        energy = me["energy"]
        grounded = me["grounded"]
        step_up = self.mech_cfg["step_up"]
        wl = math.hypot(*want)
        jump = False
        if wl < 0.05:
            self.stuck_ticks = 0
            self.jetting = False
            return (0.0, 0.0), False, False
        dx, dy = want[0] / wl, want[1] / wl
        speed = math.hypot(vel[0], vel[1])

        # stuck recovery in progress
        if t < self.unstick_until:
            ux, uy = self.unstick_dir
            return (ux, uy), grounded, energy > 10

        look = 3.5 + speed * 0.35
        rise = self.path_clear(pos, dx, dy, look)
        if rise > step_up:
            # can we jet over it? (jump gives ~2.7 m, then ~0.6 m per energy point)
            need = (rise - 2.5) / 0.6 + 12
            if rise < 50 and energy >= need:
                self.jetting = True
                return (dx, dy), grounded, True
            if self.jetting and not grounded and energy > 3:
                return (dx, dy), False, True
            # walk around: try offsets on the remembered side first
            if t > self.avoid_until:
                self.avoid_side = 1 if self.rng.random() < 0.5 else -1
            found = None
            for off in (0.4, 0.8, 1.2, 1.6, 2.0, 2.5):
                for side in (self.avoid_side, -self.avoid_side):
                    a = math.atan2(dy, dx) + off * side
                    ex, ey = math.cos(a), math.sin(a)
                    if self.path_clear(pos, ex, ey, look) <= step_up:
                        found = (ex, ey)
                        self.avoid_side = side
                        self.avoid_until = t + 1.5
                        break
                if found:
                    break
            if found:
                dx, dy = found
            else:
                jump = grounded
                boost = energy > 10
        else:
            # airborne and still under a ledge we were climbing: keep jetting
            if self.jetting and not grounded:
                ahead = self.path_clear(pos, dx, dy, 4.0)
                if ahead > -0.5 and energy > 3:
                    boost = True
            if grounded:
                self.jetting = False

        # stuck detection
        if grounded and wl > 0.5 and speed < 1.5:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = max(0, self.stuck_ticks - 1)
        if self.stuck_ticks >= 7:
            self.stuck_ticks = 0
            a = math.atan2(dy, dx) + self.rng.choice((-1, 1)) * self.rng.uniform(0.9, 2.2)
            self.unstick_dir = (math.cos(a), math.sin(a))
            self.unstick_until = t + 0.9
            return self.unstick_dir, True, energy > 10
        mag = min(1.0, wl)  # keep a deliberately slow move slow (e.g. holding a capture ring)
        return (dx * mag, dy * mag), jump, boost

    # ------------------------------------------------------------------ aiming / firing
    def lead_point(self, src, tpos, tvel, cz, speed, extra=0.05):
        """Intercept point for a projectile of `speed` from src at a target whose center is at height cz."""
        px, py, pz = tpos[0], tpos[1], cz
        vx, vy, vz = tvel
        if abs(vz) < 0.5:
            vz = 0.0
        tt = math.dist(src, (px, py, pz)) / speed + extra
        for _ in range(3):
            fz = pz + vz * tt - (0.5 * self.grav * tt * tt if vz != 0 else 0.0)
            fx, fy = px + vx * tt, py + vy * tt
            gz = self.world.height(fx, fy) + (cz - tpos[2])
            fz = max(fz, gz) if vz != 0 else fz
            tt = math.dist(src, (fx, fy, fz)) / speed + extra
        fz = pz + vz * tt - (0.5 * self.grav * tt * tt if vz != 0 else 0.0)
        fx, fy = px + vx * tt, py + vy * tt
        if vz != 0:
            fz = max(fz, self.world.height(fx, fy) + (cz - tpos[2]))
        return (fx, fy, fz)

    def aim_at(self, src, pt):
        dx, dy, dz = pt[0] - src[0], pt[1] - src[1], pt[2] - src[2]
        return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))

    def aimed(self, me_aim, want, dist, tol_m):
        ey = abs(wrap(me_aim[0] - want[0])) * max(dist, 1.0)
        ep = abs(me_aim[1] - want[1]) * max(dist, 1.0)
        return ey < tol_m and ep < tol_m * 1.6

    def mortar_pitch(self, src, pt, v, g, high):
        dx, dy = pt[0] - src[0], pt[1] - src[1]
        x = math.hypot(dx, dy)
        y = pt[2] - src[2]
        disc = v ** 4 - g * (g * x * x + 2 * y * v * v)
        if disc < 0 or x < 1:
            return None
        r = math.sqrt(disc)
        a = math.atan((v * v + r) / (g * x)) if high else math.atan((v * v - r) / (g * x))
        return a

    def shoot(self, obs, actor, kind, weapons):
        me = obs["self"]
        pos = me["pos"]
        st = me["stats"]
        muz = (pos[0], pos[1], pos[2] + st.get("muzzle_height", 7.0))
        tpos = actor["pos"]
        tvel = actor.get("vel", (0.0, 0.0, 0.0))
        if kind == "mech":
            cz = self.center_h(actor)
            radius = 2.5 * (1 + self.size_per_cube * (actor.get("cubes") or 0))
        elif kind == "npc":
            cz = tpos[2] + 1.75
            radius = 1.8
        else:
            cz = tpos[2] + 1.2
            radius = 1.4
        dist = math.dist(muz, (tpos[0], tpos[1], cz))
        los = self.world.los(muz, (tpos[0], tpos[1], cz)) or self.world.los(muz, (tpos[0], tpos[1], tpos[2] + 8.0 if kind == "mech" else cz + 1))
        fire = [False, False]
        my_aim = me["aim"]
        # choose the primary aim from the best ready weapon
        plan = None  # (aim, slot)
        aims = {}
        for i, w in enumerate(weapons):
            if not w:
                continue
            k = w["kind"]
            s = w["stats"]
            if k == "gun":
                p = self.lead_point(muz, tpos, tvel, cz, s["speed"], 0.06)
                aims[i] = self.aim_at(muz, p)
            elif k == "laser":
                p = (tpos[0] + tvel[0] * 0.08, tpos[1] + tvel[1] * 0.08, cz)
                aims[i] = self.aim_at(muz, p)
            elif k == "lightning":
                aims[i] = self.aim_at(muz, (tpos[0], tpos[1], cz))
            elif k == "mortar":
                v = s["speed"]
                tof = dist / (v * 0.7)
                p = (tpos[0] + tvel[0] * tof, tpos[1] + tvel[1] * tof, tpos[2] + 1.0)
                pitch = self.mortar_pitch(muz, p, v, s["gravity"], high=not los)
                if pitch is not None:
                    aims[i] = (math.atan2(p[1] - muz[1], p[0] - muz[0]), pitch)
        # priority: lightning in range > laser > gun > mortar
        order = []
        for i, w in enumerate(weapons):
            if not w or i not in aims:
                continue
            k = w["kind"]
            s = w["stats"]
            ready = w["cooldown"] <= 0.15
            if k == "lightning":
                ok = dist < s["range"] * 0.92 and me["energy"] >= s.get("energy", 3) + 2 and los
                order.append((0 if ok else 9, i))
            elif k == "laser":
                ok = los and dist < s["range"] * 0.95
                order.append((1 if ok and (ready or w.get("charge", 0) > 0) else 8, i))
            elif k == "gun":
                ok = los and dist < s["range"] * 0.97 and w.get("reload", 0) == 0 and w.get("ammo", 1) > 0
                order.append((2 if ok else 7, i))
            elif k == "mortar":
                ok = 28 < dist < 230
                order.append((3 if ok else 10, i))
        order.sort()
        if order:
            aim = aims[order[0][1]]
        else:
            aim = self.aim_at(muz, (tpos[0], tpos[1], cz))
        # where the torso will point after one sim tick of turning toward the commanded aim: the trigger
        # is held for 2 ticks, so a shot can still land if we're about to swing onto the target
        pred = my_aim
        if P_AIMPRED:
            ty = st.get("turn_rate", 4.0) * 0.05 * P_PRED_T
            tp = st.get("pitch_rate", 3.0) * 0.05 * P_PRED_T
            dy = wrap(aim[0] - my_aim[0])
            dp = aim[1] - my_aim[1]
            pred = (my_aim[0] + max(-ty, min(ty, dy)), my_aim[1] + max(-tp, min(tp, dp)))
        # fire each weapon whose own solution is close to our actual aim
        for i, w in enumerate(weapons):
            if not w or i not in aims:
                continue
            k = w["kind"]
            s = w["stats"]
            want_i = aims[i]
            if k == "gun":
                if los and dist < s["range"] and w.get("ammo", 0) > 0 and w.get("reload", 0) == 0:
                    if self.aimed(my_aim, want_i, dist, radius * P_TOL) or (P_AIMPRED and self.aimed(pred, want_i, dist, radius * (P_TOL - 0.1))):
                        fire[i] = True
            elif k == "lightning":
                cone = math.radians(s.get("cone_deg", 30))
                if los and dist < s["range"] * 0.95 and abs(wrap(my_aim[0] - want_i[0])) < cone * 0.6 and me["energy"] >= s.get("energy", 3) + 1:
                    fire[i] = True
            elif k == "laser":
                ch = w.get("charge", 0) or 0
                full = s.get("charge", 0.75)
                if ch > 0:
                    # charging: keep holding until full and on target, then let go
                    if ch >= full - 0.03 and (self.aimed(my_aim, want_i, dist, radius * 0.8) or (P_LPRED and self.aimed(pred, want_i, dist, radius * 0.7))) and los:
                        fire[i] = False
                    else:
                        fire[i] = True
                        if not los and ch >= full + s.get("hold_s", 2) - 0.25:
                            fire[i] = False
                else:
                    if los and dist < s["range"] * 0.95 and w["cooldown"] <= 0.05 and kind == "mech":
                        fire[i] = True
                    elif los and kind in ("npc", "crate") and w["cooldown"] <= 0.05 and dist < 120:
                        fire[i] = True
            elif k == "mortar":
                if 28 < dist < 230 and w["cooldown"] <= 0 and self.aimed(my_aim, want_i, 60.0, 2.0):
                    fire[i] = True
        return aim, fire

    def laser_release_idle(self, weapons):
        # no target: if a laser is charging, just keep holding (it auto-fires at the hold limit)
        fire = [False, False]
        for i, w in enumerate(weapons):
            if w and w["kind"] == "laser" and (w.get("charge", 0) or 0) > 0:
                fire[i] = True
        return fire

    def idle_aim(self, obs, move):
        me = obs["self"]
        t = obs["t"]
        # face whoever just shot us
        if self.threats:
            tt, b, by = self.threats[-1]
            if t - tt < 4:
                return (b, 0.0)
        # remembered enemies not in view: look toward the nearest
        pos = me["pos"]
        best = None
        for eid, e in self.enemies.items():
            if t - e["t"] < 12:
                d = math.dist(pos, e["pos"])
                if best is None or d < best[0]:
                    best = (d, e)
        if best and best[0] < 220:
            e = best[1]
            return (math.atan2(e["pos"][1] - pos[1], e["pos"][0] - pos[0]), 0.0)
        # sounds
        snd = [s for s in obs["sounds"] if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet")]
        if snd:
            s = min(snd, key=lambda s: {"near": 0, "mid": 1, "far": 2}[s["dist"]])
            if s["dist"] != "far":
                return (s["bearing"], 0.0)
        # sweep around the movement direction
        if abs(move[0]) + abs(move[1]) > 0.1:
            base = math.atan2(move[1], move[0])
        else:
            base = me["aim"][0]
        return (wrap(base + math.sin(t * 1.3) * 1.4), 0.0)


if __name__ == "__main__":
    mf.run(Bot)
