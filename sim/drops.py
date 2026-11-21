import sys, os, io, contextlib, collections
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sim as S, world as worldmod, bot as botmod
with contextlib.redirect_stderr(io.StringIO()):
    W = worldmod.World(os.path.join(S.KIT, 'arena'))
class C(worldmod.World):
    def __new__(cls, *a, **k): return W
    def __init__(self, *a, **k): pass
botmod.World = C
tot = collections.Counter()
for seed in range(int(sys.argv[1]), int(sys.argv[2])):
    sim = S.Sim(S.load_config(), W, seed, botmod.Bot)
    oa = sim.bot.act
    def act(o, oa=oa):
        if o['drops'] and any(d['lands_in'] == 0 for d in o['drops']): tot['alive_with_landed_drop'] += 1
        for e in o['events']:
            if e['type'] == 'drop': tot['captures'] += 1
        if '+drop' in sim.bot.mode: tot['drop_ticks'] += 1
        if '+hold' in sim.bot.mode: tot['hold_ticks'] += 1
        return oa(o)
    sim.bot.act = act
    sim.run()
    tot['matches'] += 1
print(dict(tot))
