import sys, os, io, contextlib, time, collections, functools
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sim as S, world as worldmod, bot as botmod
with contextlib.redirect_stderr(io.StringIO()):
    W = worldmod.World(os.path.join(S.KIT, 'arena'))
class C(worldmod.World):
    def __new__(cls, *a, **k): return W
    def __init__(self, *a, **k): pass
botmod.World = C
cur = collections.Counter()
def wrapm(name):
    f = getattr(botmod.Bot, name)
    @functools.wraps(f)
    def g(self, *a, **k):
        t0 = time.perf_counter(); r = f(self, *a, **k); cur[name] += (time.perf_counter() - t0) * 1000; return r
    setattr(botmod.Bot, name, g)
for n in ["update_memory", "pick_target", "manage_pickups", "loot_move", "fight_move", "retreat_move", "dodge_shells", "steer", "shoot", "idle_aim"]:
    wrapm(n)
for seed in [int(x) for x in sys.argv[1:]]:
    sim = S.Sim(S.load_config(), W, seed, botmod.Bot)
    worst = []
    oa = sim.bot.act
    def act(o, oa=oa):
        cur.clear(); t0 = time.perf_counter(); r = oa(o); el = (time.perf_counter() - t0) * 1000
        worst.append((el, o['t'], sim.bot.mode, dict(cur), len(o['projectiles']), len(o['mechs'])))
        return r
    sim.bot.act = act
    sim.run()
    worst.sort(key=lambda x: -x[0])
    for w in worst[:4]:
        print(seed, f"{w[0]:.1f}ms t={w[1]} {w[2]} proj={w[4]} mechs={w[5]}", {k: round(v, 1) for k, v in w[3].items() if v > 0.5})
