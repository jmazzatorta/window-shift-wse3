#!/usr/bin/env python3
"""Compile the free-routing testbench for the Cerebras appliance.

Usage:
    python3 fr_compile.py [--width 16] [--height 16] [--radius 3]
                          [--msg 16] [--kz 1] [--label LABEL]

Output -> ./compiled/<label>/ (cs_<hash>.tar.gz, out/, config.json).
config.json records the parameters so fr_run.py never needs them again.
"""
import argparse, datetime, json, os, shutil, socket
from cerebras.sdk.client import SdkCompiler

WSE3_FABRIC_DIMS = os.environ.get("CSL_FABRIC_DIMS", "762,1172")


def default_label(a):
    return f"FR_{a.width}x{a.height}_r{a.radius}_m{a.msg}_k{a.kz}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=16)
    ap.add_argument("--height", type=int, default=16)
    ap.add_argument("--radius", type=int, default=3)
    ap.add_argument("--msg", type=int, default=16, help="payload words per message (>=2)")
    ap.add_argument("--kz", type=int, default=1, help="lanes per PE")
    ap.add_argument("--label", default=None)
    ap.add_argument("--source-dir", default=".")
    ap.add_argument("--compiled-dir", default="compiled")
    a = ap.parse_args()

    assert a.msg >= 2, "msg must be >= 2 (origin + lane header)"
    assert a.radius >= 1 and a.kz >= 1
    nblk = (2 * a.radius + 1) * a.kz
    assert nblk * a.msg < 32768, "row buffer offset exceeds i16"

    label = a.label or default_label(a)
    out_dir = os.path.join(a.compiled_dir, label)
    os.makedirs(out_dir, exist_ok=True)

    cslc_opts = (
        f"--arch=wse3 "
        f"--fabric-dims={WSE3_FABRIC_DIMS} "
        f"--fabric-offsets=4,1 "
        f"--params=width:{a.width} "
        f"--params=height:{a.height} "
        f"--params=RADIUS:{a.radius} "
        f"--params=MSG_WORDS:{a.msg} "
        f"--params=KZ:{a.kz} "
        f"--params=MEMCPY_H2D_ID:0 "
        f"--params=MEMCPY_D2H_ID:1 "
        f"--max-inlined-iterations 16 "
        f"-o out --memcpy --channels 1"
    )

    print("=" * 60)
    print("FREE-ROUTING TESTBENCH COMPILE")
    print("=" * 60)
    print(f"  Date   : {datetime.datetime.now().isoformat()}")
    print(f"  Host   : {socket.gethostname()}")
    print(f"  Label  : {label}")
    print(f"  Mesh   : {a.width}x{a.height}  R={a.radius}  MSG={a.msg}  KZ={a.kz}")
    print(f"  Opts   : {cslc_opts}")
    print()

    with SdkCompiler(disable_version_check=True,
                     resource_mem=256 << 30, resource_cpu=48000) as compiler:
        artifact_path = compiler.compile(a.source_dir, "fr_layout.csl",
                                         cslc_opts, a.source_dir)

    print(f"[fr_compile] artifact_path = {artifact_path}")
    base = os.path.basename(artifact_path)
    local = os.path.join(a.source_dir, base)
    if os.path.exists(local):
        dst = os.path.join(out_dir, base)
        if os.path.abspath(local) != os.path.abspath(dst):
            shutil.move(local, dst)
    src_out = os.path.join(a.source_dir, "out")
    dst_out = os.path.join(out_dir, "out")
    if os.path.exists(src_out) and os.path.abspath(src_out) != os.path.abspath(dst_out):
        if os.path.exists(dst_out):
            shutil.rmtree(dst_out)
        shutil.move(src_out, dst_out)
    with open(os.path.join(out_dir, "config.json"), "w") as f:
        json.dump({"artifact_path": artifact_path, "width": a.width,
                   "height": a.height, "radius": a.radius, "msg": a.msg,
                   "kz": a.kz}, f, indent=1)
    print(f"[fr_compile] done -> {out_dir}")


if __name__ == "__main__":
    main()
