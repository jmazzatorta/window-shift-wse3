#!/usr/bin/env python3
"""Event-level simulation of the fr_pe.csl state machine (no SDK needed).

Mirrors fr_pe.csl line by line: emit/arm_drain/txDone/rxDone for E,W,N,S,
Phase X passes, Phase Y sub-shifts with slot skipping, verify_and_count.

Fabric model = RENDEZVOUS (the most restrictive): a send completes only when
the receiving neighbour has an armed drain on that link. Any real buffering in
the fabric only helps, so no deadlock here => no deadlock from the protocol.
Message contents are delivered as-is, so mixing of phases/blocks would show up
as header/payload errors, exactly as on device.

Task order is randomised (cooperative, run-to-completion): any ready
completion may fire next.

Usage: python3 fr_sim.py  (runs a grid of configs x seeds)
"""
import random
from fr_model import expected_stats

M32 = 1 << 32
# link direction -> (dx, dy) of the receiver, and the receiver's rx direction
SEND = {"E": (1, 0), "W": (-1, 0), "N": (0, -1), "S": (0, 1)}


def fill(o, l, i):
    v = (o & 0x7FF) * 13 + (i & 0x7FF) * 7 + (l & 0x3F) * 3 + 1
    assert v < 65536
    return v


class PE:
    def __init__(s, sim, x, y):
        W, H, R, KZ, MSG = sim.W, sim.H, sim.R, sim.KZ, sim.MSG
        s.sim, s.x, s.y = sim, x, y
        s.has = {"W": x > 0, "E": x < W - 1, "N": y > 0, "S": y < H - 1}
        s.n_from = {"w": min(x, R), "e": min(W - 1 - x, R),
                    "n": min(y, R), "s": min(H - 1 - y, R)}
        s.my_id = y * W + x
        s.NBLK = (2 * R + 1) * KZ
        s.rowbuf = [None] * s.NBLK
        s.carry = {d: None for d in "EWNS"}
        s.rxtmp = {d: None for d in "EWNS"}
        s.sp = {d: 0 for d in "EWNS"}
        s.recvd = {d: 0 for d in "wens"}
        s.rxcl = {d: False for d in "EWNS"}
        s.txcl = {d: False for d in "EWNS"}
        s.stats = [0] * 8
        s.done = 0

    # ---- payload / consumer ----
    def seed(s):
        for zb in range(s.sim.KZ):
            s.rowbuf[zb] = [s.my_id + 1, zb + 1] + [fill(s.my_id, zb, i) for i in range(2, s.sim.MSG)]

    def slot_dx(s, sl):
        R = s.sim.R
        return 0 if sl == 0 else (-sl if sl <= R else sl - R)

    def slot_valid(s, sl):
        R = s.sim.R
        if sl == 0:
            return True
        return sl <= s.n_from["w"] if sl <= R else (sl - R) <= s.n_from["e"]

    def verify(s, buf, dy):
        W, KZ = s.sim.W, s.sim.KZ
        ox, oy = s.x + s.cur_dx, s.y + dy
        exp_id = oy * W + ox
        o1, l1 = buf[0], buf[1]
        if o1 != exp_id + 1 or l1 != s.cur_lane + 1:
            s.stats[3] += 1
        o = o1 - 1 if o1 > 0 else 0
        l = l1 - 1 if l1 > 0 else 0
        s.stats[4] += sum(1 for i in range(2, s.sim.MSG) if buf[i] != fill(o, l, i))
        key = o * KZ + l + 1
        s.stats[0] += 1
        s.stats[1] = (s.stats[1] + key) % M32
        assert key < 65536
        s.stats[2] = (s.stats[2] + (key & 0xFF) ** 2 + (key >> 8) ** 2) % M32

    # ---- fabric primitives ----
    def emit(s, d, hop):
        s.carry[d] = s.carry[d][:s.sim.MSG] + [hop]
        s.sp[d] += 1
        s.sim.post_send(s, d, list(s.carry[d]))

    def arm(s, d):
        s.sim.post_drain(s, d)

    # ---- Phase X ----
    def x_dir_finished(s):
        s.x_dirs_done += 1
        if s.x_dirs_done == 2:
            s.x_zb += 1
            if s.x_zb < s.sim.KZ:
                s.start_x_pass()
            else:
                s.start_phase_y()

    def try_dir(s, d):
        if s.rxcl[d] and s.txcl[d] and s.sp[d] == 0:
            (s.x_dir_finished if d in "EW" else s.y_dir_finished)()

    # generic chain: send dir d, receive-counter key rk, store hook
    RK = {"E": "w", "W": "e", "N": "s", "S": "n"}

    def tx_done(s, d):
        s.sp[d] -= 1
        rk = s.RK[d]
        if not s.rxcl[d] and s.recvd[rk] < s.n_from[rk]:
            s.arm(d)
        s.try_dir(d)

    def rx_done(s, d, msg):
        R, KZ, MSG = s.sim.R, s.sim.KZ, s.sim.MSG
        s.rxtmp[d] = msg
        rk = s.RK[d]
        s.recvd[rk] += 1
        hop = int(msg[MSG])
        if hop != s.recvd[rk]:
            s.stats[5 if d in "EW" else 6] += 1
        if d == "E":
            s.rowbuf[s.recvd["w"] * KZ + s.x_zb] = msg[:MSG]
        elif d == "W":
            s.rowbuf[(R + s.recvd["e"]) * KZ + s.x_zb] = msg[:MSG]
        elif d == "N":
            s.verify(msg, hop)
        else:
            s.verify(msg, -hop)
        s.carry[d] = msg[:MSG] + [0]
        fwd = s.has[d] and hop < R
        more = s.recvd[rk] < s.n_from[rk]
        if not more:
            s.rxcl[d] = True
            s.txcl[d] = True
        if fwd:
            s.emit(d, hop + 1)
        elif more:
            s.arm(d)
        s.try_dir(d)

    def start_dir_pair(s, d1, d2, src):
        for d in (d1, d2):
            s.carry[d] = list(src) + [0]
            rk = s.RK[d]
            s.recvd[rk] = 0
            s.sp[d] = 0
            s.rxcl[d] = s.txcl[d] = (s.n_from[rk] == 0)
        for d in (d1, d2):
            rk = s.RK[d]
            if s.has[d]:
                s.emit(d, 1)
            elif s.n_from[rk] > 0:
                s.arm(d)

    def start_x_pass(s):
        s.x_dirs_done = 0
        s.start_dir_pair("E", "W", s.rowbuf[s.x_zb])
        if s.rxcl["E"] and s.txcl["E"] and s.sp["E"] == 0:
            s.x_dir_finished()
        if s.rxcl["W"] and s.txcl["W"] and s.sp["W"] == 0:
            s.x_dir_finished()

    # ---- Phase Y ----
    def y_dir_finished(s):
        s.y_dirs_done += 1
        if s.y_dirs_done == 2:
            s.next_blk()
            s.advance_or_finish()

    def next_blk(s):
        s.cur_blk += 1
        s.cur_lane += 1
        if s.cur_lane == s.sim.KZ:
            s.cur_lane = 0
            s.cur_slot += 1

    def start_sub_shift(s):
        s.cur_dx = s.slot_dx(s.cur_slot)
        src = s.rowbuf[s.cur_blk]
        s.y_dirs_done = 0
        # carries loaded + counters reset before consuming own block (as CSL)
        s.start_dir_pair_y_setup(src)
        s.verify(s.carry["N"], 0)
        for d in ("N", "S"):
            rk = s.RK[d]
            if s.has[d]:
                s.emit(d, 1)
            elif s.n_from[rk] > 0:
                s.arm(d)
        if s.rxcl["N"] and s.txcl["N"] and s.sp["N"] == 0:
            s.y_dir_finished()
        if s.rxcl["S"] and s.txcl["S"] and s.sp["S"] == 0:
            s.y_dir_finished()

    def start_dir_pair_y_setup(s, src):
        for d in ("N", "S"):
            s.carry[d] = list(src) + [0]
            rk = s.RK[d]
            s.recvd[rk] = 0
            s.sp[d] = 0
            s.rxcl[d] = s.txcl[d] = (s.n_from[rk] == 0)

    def advance_or_finish(s):
        while s.cur_blk < s.NBLK and not s.slot_valid(s.cur_slot):
            s.next_blk()
        if s.cur_blk < s.NBLK:
            s.start_sub_shift()
        else:
            s.stats[7] = 1
            s.done = 1

    def start_phase_y(s):
        s.cur_blk = s.cur_slot = s.cur_lane = 0
        s.advance_or_finish()

    def run_shift(s):
        s.seed()
        s.x_zb = 0
        s.start_x_pass()


class Sim:
    def __init__(s, W, H, R, KZ, MSG, seed):
        s.W, s.H, s.R, s.KZ, s.MSG = W, H, R, KZ, MSG
        s.rng = random.Random(seed)
        s.pes = {(x, y): PE(s, x, y) for y in range(H) for x in range(W)}
        s.pending_tx = {}   # (pe xy, dir) -> msg   (at most 1: sp<=1 by protocol)
        s.armed = set()     # (pe xy, dir) drains armed
        s.ready = []        # completions ready to run as tasks
        s.max_inflight = 0

    def post_send(s, pe, d, msg):
        k = ((pe.x, pe.y), d)
        assert k not in s.pending_tx, f"two sends in flight on {k}"
        s.pending_tx[k] = msg
        s.match()

    def post_drain(s, pe, d):
        k = ((pe.x, pe.y), d)
        assert k not in s.armed, f"double-armed drain {k}"
        s.armed.add(k)
        s.match()

    def match(s):
        # a send on dir d from P meets the drain on dir d at P+SEND[d]
        for (xy, d), msg in list(s.pending_tx.items()):
            dx, dy = SEND[d]
            rx = ((xy[0] + dx, xy[1] + dy), d)
            if rx in s.armed:
                del s.pending_tx[(xy, d)]
                s.armed.discard(rx)
                s.ready.append(("tx", xy, d, None))
                s.ready.append(("rx", rx[0], d, msg))

    def run(s):
        order = list(s.pes.values())       # launch order randomised too
        s.rng.shuffle(order)
        for pe in order:
            pe.run_shift()
        steps = 0
        while s.ready:
            i = s.rng.randrange(len(s.ready))
            kind, xy, d, msg = s.ready.pop(i)
            pe = s.pes[xy]
            (pe.tx_done(d) if kind == "tx" else pe.rx_done(d, msg))
            steps += 1
        return steps


def check(W, H, R, KZ, MSG, seed):
    sim = Sim(W, H, R, KZ, MSG, seed)
    sim.run()
    ec, e1, e2 = expected_stats(W, H, R, KZ)
    ndone = sum(p.done for p in sim.pes.values())
    errs = 0
    for (x, y), p in sim.pes.items():
        st = p.stats
        if (st[0], st[1], st[2]) != (ec[y, x], e1[y, x], e2[y, x]) or any(st[3:7]):
            errs += 1
    stuck = len(sim.pending_tx) + len(sim.armed)
    return ndone, errs, stuck


if __name__ == "__main__":
    cfgs = [(16, 16, 3, 1, 16), (16, 16, 1, 1, 2), (16, 16, 3, 4, 8),
            (8, 12, 2, 3, 5), (5, 5, 7, 2, 4), (1, 9, 2, 1, 3), (9, 1, 2, 2, 3),
            (4, 4, 3, 1, 2)]
    allok = True
    for c in cfgs:
        res = [check(*c, seed) for seed in range(25)]
        bad = [(sd, r) for sd, r in enumerate(res)
               if r[0] != c[0] * c[1] or r[1] or r[2]]
        allok &= not bad
        print(f"W={c[0]:2d} H={c[1]:2d} R={c[2]} KZ={c[3]} MSG={c[4]:2d}: "
              f"{'OK' if not bad else 'FAIL ' + str(bad[:3])}  (25 seeds)")
    print("ALL OK" if allok else "FAILURES")
