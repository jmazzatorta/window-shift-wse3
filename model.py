#!/usr/bin/env python3
"""Host-side oracle for the free-routing testbench (no SDK dependency).

For every PE it computes the stats the device must report after a window
shift of radius R with KZ lanes on a width x height mesh:
  count = number of window messages consumed (own included)
  sum   = sum of key        (mod 2^32)
  sumsq = sum of lo^2 + hi^2 of key (lo/hi = low/high byte of the u16 key)
with key = origin_id*KZ + lane + 1, origin_id = oy*width + ox.
The window of PE(x,y) is every (ox,oy) with |ox-x|<=R, |oy-y|<=R inside the mesh.
"""
import numpy as np

M32 = 1 << 32


def expected_stats(width, height, R, KZ):
    """Return (count, sum, sumsq) arrays of shape (height, width), uint64."""
    cnt = np.zeros((height, width), dtype=np.uint64)
    s1 = np.zeros((height, width), dtype=np.uint64)
    s2 = np.zeros((height, width), dtype=np.uint64)
    for y in range(height):
        for x in range(width):
            c = a = b = 0
            for oy in range(max(0, y - R), min(height, y + R + 1)):
                for ox in range(max(0, x - R), min(width, x + R + 1)):
                    oid = oy * width + ox
                    for lane in range(KZ):
                        key = oid * KZ + lane + 1
                        c += 1
                        a = (a + key) % M32
                        b = (b + (key & 0xFF) ** 2 + (key >> 8) ** 2) % M32
            cnt[y, x], s1[y, x], s2[y, x] = c, a, b
    return cnt, s1, s2


def traffic(width, height, R, KZ, msg_words):
    """Fabric work implied by one run (for throughput figures)."""
    # Phase X: each PE's block travels min(R, room) hops each way, per lane.
    hx = sum(min(R, x) + min(R, width - 1 - x) for x in range(width)) * height * KZ
    # Phase Y: every valid row-buffer block (one per in-row window source)
    # travels min(R, room) hops North and South.
    srcs = sum(1 + min(R, x) + min(R, width - 1 - x) for x in range(width))
    hy = srcs * sum(min(R, y) + min(R, height - 1 - y) for y in range(height)) * KZ
    hops = hx + hy
    cnt, _, _ = expected_stats(width, height, R, KZ)
    return {
        "msg_hops": int(hops),
        "word_hops": int(hops * (msg_words + 1)),
        "deliveries": int(cnt.sum()),
    }


if __name__ == "__main__":
    c, a, b = expected_stats(16, 16, 3, 1)
    print("count corner/edge/interior:", c[0, 0], c[0, 8], c[8, 8])
    print(traffic(16, 16, 3, 1, 16))
