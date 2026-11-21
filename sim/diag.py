import sys, os, collections, io, contextlib
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sim as S, world as worldmod, importlib
botname = sys.argv[1] if len(sys.argv) > 1 else "bot"
seeds = [int(x) for x in sys.argv[2:]] or [1000, 1001, 1002]
botmod = importlib.import_module(botname)
with contextlib.redirect_stderr(io.StringIO()):
    W = worldmod.World(os.path.join(S.KIT, 'arena'))
class C(worldmod.World):
    def __new__(cls, *a, **k): return W
    def __init__(self, *a, **k): pass
botmod.World = C
cfg = S.load_config()
for seed in seeds:
    sim = S.Sim(cfg, W, seed, botmod.Bot)
    taken = collections.Counter(); dealt = collections.Counter(); shots = collections.Counter(); modes = collections.Counter()
    timeline = []
    od = sim.damage
    def damage(v, amt, wp, att, frm, od=od):
        if v is sim.me and att is not None: taken[(att.id, att.level, wp)] += amt
        if att is sim.me: dealt[wp] += amt
        return od(v, amt, wp, att, frm)
    sim.damage = damage
    osp = sim.spawn_proj
    def sp(m, kind, *a, osp=osp):
        if m is sim.me: shots[kind] += 1
        return osp(m, kind, *a)
    sim.spawn_proj = sp
    ofl = sim.fire_laser
    def fl(m, w, ofl=ofl):
        if m is sim.me: shots['laser'] += 1
        return ofl(m, w)
    sim.fire_laser = fl
    oa = sim.bot.act
    def act(o, oa=oa):
        r = oa(o); md = sim.bot.mode; modes[md.split('+')[0]] += 1
        if o['tick'] % 40 == 0:
            vis = [(m['id'], round(m['_d'])) for m in o['mechs'] if m['kind'] == 'mech']
            timeline.append(f"t{o['t']:.0f} {md} hp{o['self']['hp']:.0f} vis{vis}")
        return r
    sim.bot.act = act
    p = sim.run()
    print(f'seed {seed} place {p} t {sim.t:.1f} kills {sim.my_kills}')
    print('  taken', dict(taken.most_common(8)))
    print('  dealt', dict(dealt), 'shots', dict(shots))
    print('  modes', dict(modes))
    for l in timeline[-12:]: print('   ', l)
