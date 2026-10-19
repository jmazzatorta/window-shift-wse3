#!/usr/bin/env python3
"""Run + verify the free-routing testbench.

Reads the parameters from compiled/<label>/config.json, launches the window
shift, polls done[] until every PE finishes (or timeout), then checks per PE:
  - consumed count, key sum, key^2 sum   == fr_model.expected_stats
  - header errors, payload word errors   == 0
  - X / Y sentinel (hop order) errors    == 0

Usage:
    python3 fr_run.py --label LABEL [--timeout 30] [--simulator]
"""
import argparse, glob, json, os, time
import numpy as np
from cerebras.sdk.client import SdkRuntime
from cerebras.appliance.pb.sdk.sdk_common_pb2 import MemcpyDataType, MemcpyOrder

from fr_model import expected_stats, traffic

U32 = MemcpyDataType.MEMCPY_32BIT
ROW = MemcpyOrder.ROW_MAJOR


def resolve_artifact(compiled_dir, label):
    cands = glob.glob(os.path.join(compiled_dir, label, "cs_*.tar.gz"))
    if len(cands) != 1:
        raise RuntimeError(f"expected 1 artifact in {compiled_dir}/{label}/, got {cands}")
    return cands[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--compiled-dir", default="compiled")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--simulator", action="store_true")
    a = ap.parse_args()

    with open(os.path.join(a.compiled_dir, a.label, "config.json")) as f:
        cfg = json.load(f)
    W, H, R, MSG, KZ = cfg["width"], cfg["height"], cfg["radius"], cfg["msg"], cfg["kz"]
    artifact = resolve_artifact(a.compiled_dir, a.label)
    print(f"[fr_run] {a.label}: mesh {W}x{H} R={R} MSG={MSG} KZ={KZ}")
    print(f"[fr_run] artifact = {artifact}")

    def d2h(runner, sym, n):
        buf = np.zeros(W * H * n, dtype=np.uint32)
        runner.memcpy_d2h(buf, sym, 0, 0, W, H, n, streaming=False,
                          data_type=U32, nonblock=False, order=ROW)
        return buf.reshape(H, W, n)

    with SdkRuntime(artifact, simulator=a.simulator, disable_version_check=True) as runner:
        s_done = runner.get_id("done")
        s_stats = runner.get_id("stats")
        s_dbg = runner.get_id("dbg")

        runner.launch("runtime_init", nonblock=False)
        pre = d2h(runner, s_done, 1)
        print(f"[fr_run] pre-launch done sum = {int(pre.sum())} (expect 0)")

        print("[fr_run] launch run_shift...")
        t0 = time.time()
        runner.launch("run_shift", nonblock=False)
        ndone = 0
        while True:
            done = d2h(runner, s_done, 1)[:, :, 0]
            ndone = int((done == 1).sum())
            if ndone == W * H or time.time() - t0 > a.timeout:
                break
            time.sleep(0.5)
        print(f"[fr_run] done = {ndone}/{W*H} after {time.time()-t0:.2f}s (host wall, coarse)")

        stats = d2h(runner, s_stats, 8)
        dbg = d2h(runner, s_dbg, 16)

    # --- progress dump (useful on a hang) ---
    if ndone != W * H:
        y = H // 2
        print(f"\n--- progress, row y={y} ---")
        print("   x : rE rW rS rN  txE txW txN txS  xpass curBlk yStart done")
        for x in range(W):
            d = dbg[y, x]
            print(f"  {x:2d} : {d[0]:2d} {d[1]:2d} {d[5]:2d} {d[6]:2d}  "
                  f"{d[3]:3d} {d[2]:3d} {d[7]:3d} {d[8]:3d}  {d[12]:#06x} {d[13]:5d} "
                  f"{d[14]:5d} {done[y, x]}")
        print(f"  PEs in Phase Y: {int((dbg[:,:,14]==1).sum())}/{W*H}")

    # --- verify ---
    ecnt, es1, es2 = expected_stats(W, H, R, KZ)
    fin = stats[:, :, 7] == 1
    bad_cnt = fin & (stats[:, :, 0] != ecnt)
    bad_sum = fin & ((stats[:, :, 1] != es1) | (stats[:, :, 2] != es2))
    hdr = int(stats[:, :, 3].sum()); pay = int(stats[:, :, 4].sum())
    sx = int(stats[:, :, 5].sum()); sy = int(stats[:, :, 6].sum())

    print("\n--- VERIFY ---")
    print(f"  finished PEs        : {int(fin.sum())}/{W*H}")
    print(f"  count mismatches    : {int(bad_cnt.sum())}")
    print(f"  key-sum mismatches  : {int(bad_sum.sum())}")
    print(f"  header errors       : {hdr}")
    print(f"  payload word errors : {pay}")
    print(f"  sentinel errors X/Y : {sx}/{sy}")
    for (y, x) in list(zip(*np.nonzero(bad_cnt | bad_sum)))[:8]:
        print(f"  [BAD] PE({x},{y}) got cnt={stats[y,x,0]} sum={stats[y,x,1]} sq={stats[y,x,2]}"
              f" | exp cnt={ecnt[y,x]} sum={es1[y,x]} sq={es2[y,x]}")

    tr = traffic(W, H, R, KZ, MSG)
    print(f"\n  deliveries={tr['deliveries']} (device {int(stats[:,:,0].sum())})"
          f"  msg-hops={tr['msg_hops']}  word-hops={tr['word_hops']}")

    ok = (ndone == W * H and int(fin.sum()) == W * H and not bad_cnt.any()
          and not bad_sum.any() and hdr == 0 and pay == 0 and sx == 0 and sy == 0)
    print("\nRESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
