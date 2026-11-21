"""Controlled checks for drop capture and final-circle holding (approximate sim)."""
import sys, os, io, contextlib, math
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sim as S, world as worldmod, bot as botmod
with contextlib.redirect_stderr(io.StringIO()):
    W = worldmod.World(os.path.join(S.KIT, 'arena'))
class C(worldmod.World):
    def __new__(cls, *a, **k): return W
    def __init__(self, *a, **k): pass
botmod.World = C
cfg = S.load_config()

def quiet_sim(seed):
    sim = S.Sim(cfg, W, seed, botmod.Bot)
    sim.update_hunters = lambda: None
    sim.hunters = set()
    for m in sim.mechs:            # park the house bots far away, out of play
        if m is not sim.me:
            m.alive = False
    sim.mechs[[m.id for m in sim.mechs if m is not sim.me][0]].alive = True  # keep one so the match runs
    other = next(m for m in sim.mechs if m.alive and m is not sim.me)
    other.pos = [sim.me.pos[0] + 400, sim.me.pos[1], sim.me.pos[2]]
    other.level = 'easy'
    sim.house_act = lambda m: {"move": [0, 0]}
    sim.npcs = []
    return sim

def run_until(sim, t_end):
    dt = 1 / 20
    while sim.t < t_end and sim.me.alive:
        sim.run(max_t=sim.t + dt * 2) if False else None
        break
    sim.run(max_t=t_end)

# --- 1. drop capture: drop lands 15 m away; bot at 60% HP
ok = 0
for seed in range(20):
    sim = quiet_sim(100 + seed)
    me = sim.me
    me.hp = me.max_hp * 0.6
    d = sim.drops[0]
    d["call"] = -24.0  # lands at t=1
    a = seed * 0.7
    x, y = me.pos[0] + math.cos(a) * 15, me.pos[1] + math.sin(a) * 15
    d["pos"] = [x, y, W.ground(x, y)]
    sim.drops = [d]
    # zone around us so the drop is inside the safe circle
    sim.phases = [(9999, 10000, [me.pos[0], me.pos[1]], 300, [me.pos[0], me.pos[1]], 250, 1)]
    sim.run(max_t=14.0)
    got = d["taken"] and d["holder"] == me.id
    ok += got
    if not got:
        print(f"  seed {seed}: not captured, holder={d['holder']} progress={d['progress']:.1f} dist={math.hypot(me.pos[0]-x, me.pos[1]-y):.1f} mode={sim.bot.mode}")
print(f"drop capture: {ok}/20 captured; hp after last = {me.hp:.0f}/{me.max_hp:.0f}")

# --- 2. final circle: radius 5 around a point 3 m away, shrinking to 0; bot should settle and stop boosting
res = []
for seed in range(10):
    sim = quiet_sim(200 + seed)
    me = sim.me
    c = [me.pos[0] + 3, me.pos[1]]
    sim.phases = [(0, 30, c, 5.0, c, 0.0, 260)]
    me.hp = me.max_hp = 50000  # survive the damage so we can watch the movement
    boosts = [0]
    oa = sim.bot.act
    def act(o, oa=oa, boosts=boosts):
        r = oa(o)
        if r.get("boost"): boosts[0] += 1
        return r
    sim.bot.act = act
    sim.run(max_t=20.0)
    res.append((math.hypot(me.pos[0] - c[0], me.pos[1] - c[1]), boosts[0], me.energy))
print("final circle: dist-to-center / boost ticks / energy:", [(round(a, 1), b, round(e)) for a, b, e in res])

# --- 3. capture under fire: a hunter shoots from ~90 m while the drop lands 10 m away
ok = 0
for seed in range(20):
    sim = quiet_sim(300 + seed)
    me = sim.me
    other = next(m for m in sim.mechs if m.alive and m is not me)
    a = seed * 0.9
    ox, oy = me.pos[0] + math.cos(a) * 90, me.pos[1] + math.sin(a) * 90
    other.pos = [ox, oy, W.height(ox, oy)]
    other.level = 'easy'
    sim.house_act = S.Sim.house_act.__get__(sim)
    sim.hunters = {other.id}
    d = sim.drops[0]
    d["call"] = -24.0
    x, y = me.pos[0] - math.cos(a) * 10, me.pos[1] - math.sin(a) * 10
    d["pos"] = [x, y, W.ground(x, y)]
    sim.drops = [d]
    sim.phases = [(9999, 10000, [me.pos[0], me.pos[1]], 300, [me.pos[0], me.pos[1]], 250, 1)]
    sim.run(max_t=14.0)
    ok += d["taken"] and d["holder"] == me.id
print(f"capture under fire: {ok}/20 captured")
