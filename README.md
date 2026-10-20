# Window-shift free routing on the Cerebras WSE-3

A small, standalone CSL testbench that demonstrates and measures **software
("free") routing** of messages across the PE mesh of a Cerebras WSE-3, using a
**window shift** pattern: every PE delivers its message(s) to every PE inside a
`(2R+1) × (2R+1)` window centred on itself.

There are no static multicast routes and no pass-through fabric switches. Every
hop is a single-hop transfer between neighbouring PEs, and forwarding is
store-and-forward in the PE core. The fabric configuration is therefore the
same for any radius, message size or mesh size, and is the same 8 colors
everywhere.

> **Target environment.** This testbench has been adapted for the **appliance
> (Wafer-Scale Cluster) version of the Cerebras SDK** as deployed on the
> **Argonne / ALCF Cerebras CS-3 cluster**. It compiles and runs through the
> Python appliance client (`cerebras.sdk.client`, `SdkCompiler` / `SdkRuntime`)
> rather than the containerised (Singularity) `cslc` / `cs_python --cmaddr`
> flow. See [Environment](#environment) for the exact SDK version and settings.

The project is meant to answer two questions:

1. **Correctness** — does every expected message arrive exactly once, uncorrupted,
   in hop order, without deadlock?
2. **Performance** — how does completion time scale with the radius, the message
   size, the queue depth and the mesh size?

> Status: **correctness PASS on device** (WSE-3, 16×16, R=3, MSG=16, KZ=1):
> 256/256 PEs finished, 10000/10000 deliveries, 0 header / payload / hop-order
> errors. The on-device cycle counter and NBUF > 1 are next (see
> [Roadmap](#roadmap)).

**Deliveries vs hops.** A delivery is a (receiver, message) pair. Each PE also
consumes its own message, which takes 0 hops. Every other delivery ends with
exactly one hop, because each PE a message passes through is also one of its
destinations. So `deliveries = msg-hops + width·height`. For 16×16 with R=3
that is 10000 = 9744 + 256: no hop is wasted.

---

## How it works

### Two phases

```
 Phase X (rows)                      Phase Y (columns)

   . . . . . . .                       . . . o . . .
   . . . . . . .                       . . . o . . .
   . . . . . . .                       . . . o . . .
   o o o P o o o   ──── then ────▶     o o o P o o o
   . . . . . . .                       . . . o . . .
   . . . . . . .                       . . . o . . .
   . . . . . . .                       . . . o . . .

 P's block travels R hops East and   Every block a PE holds after Phase X
 R hops West. Each PE collects its    (its own + 2R row neighbours) travels
 2R+1 row-window blocks into rowbuf.  R hops North and R hops South.
```

After both phases, PE `(x, y)` has seen the message of every PE
`(x+dx, y+dy)` with `|dx|, |dy| ≤ R` that lies inside the mesh, including its
own. Every message makes at most 2R hops.

### Transport rules (per direction)

- **Parity colors.** Each direction uses two colors, an even one and an odd
  one. A PE sends on the color of its own parity, and its neighbour, which has
  the opposite parity, receives on that same color. So the sender's out-color
  always equals the receiver's in-color. 4 directions × 2 = 8 colors.
- **One message in flight.** Each direction has one send and one receive slot,
  and sending alternates strictly with draining (`emit → txDone → arm_drain →
  rxDone → emit …`). Sending and draining never happen at the same time.
- **Hop count in the message.** The last word of each message (`SENT`) carries
  the hop count. A receiver forwards the message only while `hop < R`.
- **Local termination.** Every PE knows at compile time how many arrivals to
  expect from each direction (`n_from_w/e/n/s`, clipped at the mesh edges). It
  finishes a direction when the count is reached, so there is no global barrier.
- **Event-driven.** The WSE-3 scheduler is cooperative, so a busy-wait on a
  completion deadlocks. All progress is driven by task activations on
  tx/rx completion.
- **Lanes (KZ).** With `KZ > 1`, each PE originates KZ independent messages.
  Phase X runs once per lane, and Phase Y shifts every collected block.
- **Edge skipping.** In Phase Y a block whose source column lies off-mesh is
  skipped. This depends only on `pe_x`, so every PE in a column skips the same
  blocks and the column stays in step.

### Synthetic payload and verification

There is no application math. Each message is `MSG_WORDS` f32 words plus the
hop sentinel:

| word | content |
|---|---|
| `0` | origin id + 1 (`origin = oy*width + ox`) |
| `1` | lane + 1 |
| `2 … MSG_WORDS-1` | `fill(origin, lane, i)`, a known integer pattern |
| `SENT` | hop count (transport only) |

Each PE runs `verify_and_count` on every message it consumes:

- **Header check:** the origin and lane must match the position the transport
  says the message came from.
- **Payload check:** every body word must equal `fill()` of the received
  header. This catches corruption even when the header is intact.
- **Fold into `stats`:** count, Σ key, and Σ (lo² + hi²) of the key, where
  `key = origin*KZ + lane + 1`.

The host recomputes the expected values for every PE (`model.py`) and
compares.

> All on-PE integer arithmetic is 16-bit: the PE core has no 32-bit integer
> multiply (cslc fails with `Cannot select: i32 = mul`). This limits
> `width × height × KZ < 32768`, which is checked at compile time.

---

## Repository layout

| file | role |
|---|---|
| `layout.csl` | Top-level layout: mesh, colors, task ids, parameters, exports |
| `pe.csl` | Per-PE program: transport state machine, payload seeding, `verify_and_count` |
| `compile.py` | Compiles one configuration on the appliance → `compiled/<label>/` |
| `run.py` | Runs a compiled configuration, polls completion, verifies, prints PASS/FAIL |
| `model.py` | Host oracle: expected per-PE stats, fabric traffic (no SDK needed) |
| `sim.py` | Event-level Python simulation of `pe.csl` (no SDK needed) |

---

## Parameters

All parameters are compile-time CSL `param`s. The DSD extents, buffer sizes and
expected arrival counts are derived from them at compile time, so you need one
compile per configuration.

| flag | CSL param | meaning | default |
|---|---|---|---|
| `--width`, `--height` | `width`, `height` | mesh size (PEs) | 16 × 16 |
| `--radius` | `RADIUS` | window radius R (hops per direction) | 3 |
| `--msg` | `MSG_WORDS` | payload words per message (≥ 2) | 16 |
| `--kz` | `KZ` | lanes = messages originated per PE | 1 |

Compile-time limits (assertions):

- `(2R+1)·KZ·MSG_WORDS < 32768`, because row-buffer offsets are i16;
- `width·height·KZ < 32768`, because ids and keys must fit in u16.

---

## Environment

The compile and run scripts target the **appliance (Wafer-Scale Cluster) SDK**,
the mode used on the **Argonne Leadership Computing Facility (ALCF) Cerebras
CS-3 cluster**. They were adapted to that deployment and assume:

| item | value |
|---|---|
| SDK / ML Software | **Cerebras SDK 2.10** (`cerebras_sdk==2.10.0`, `cerebras_appliance==2.10.0`) |
| client module | `cerebras.sdk.client` (`SdkCompiler`, `SdkRuntime`) |
| Python env | an appliance venv, e.g. `cs_appliance_sdk` (Python 3.11); scripts run under `cs_python` |
| architecture | `--arch=wse3` |
| fabric dims | `--fabric-dims=762,1172 --fabric-offsets=4,1` (full CS-3 wafer; override with `CSL_FABRIC_DIMS`) |
| memcpy | `--memcpy --channels 1`, H2D color 0 / D2H color 1 |

Notes specific to the appliance / ALCF setup:

- **No `--cmaddr`.** Unlike the containerised SDK, the appliance client is given
  the compiled artifact directly (`SdkRuntime(artifact, simulator=False)`); the
  cluster resolves the CS-3 allocation, so no CM address is passed.
- **Version check disabled.** The appliance client (seen as 1.1.0) and the
  cluster server can report an `InconsistentVersion` warning; both scripts pass
  `disable_version_check=True` to proceed, as is standard on the ALCF system.
- **Single worker / single CS.** An SDK job on the WSC uses one worker node and
  one CS system, which is sufficient for this single-wafer testbench.
- **Simulator fallback.** The only difference between a CS-3 run and a simulator
  run is the fabric dims; `run.py --simulator` exercises the same artifact
  through the SDK fabric simulator without a hardware allocation.

> Porting to a different site or SDK line (e.g. the 1.x / ML Software 2.4–2.5
> appliance releases, or the containerised `cslc` flow) mainly means matching
> the `cerebras.sdk.client` API surface and the `--fabric-dims` of the local
> system; the CSL itself is unchanged.

---

## Running

Requirements: access to the Argonne / ALCF Cerebras CS-3 appliance (or another
WSC running SDK 2.10) and the `cerebras.sdk` Python client in an activated
environment (e.g. `cs_appliance_sdk`), plus NumPy. See
[Environment](#environment) for versions and settings.

### 1. (Optional) check the logic locally — no hardware needed

```bash
python3 model.py   # expected counts for 16×16, R=3
python3 sim.py     # event-level simulation, 8 configs × 25 random schedules
```

`sim.py` runs the same state machine as `pe.csl` with a rendezvous
fabric model, the most restrictive one: a send completes only when the
neighbour has armed its receive. Tasks fire in a random order. It should print
`ALL OK`.

### 2. Compile

```bash
python compile.py --label fr1                                  # defaults
python compile.py --radius 2 --msg 32 --kz 2 --label r2_m32_k2 # any config
```

The output goes to `compiled/<label>/` and holds the artifact, `out/` and
`config.json`. The runner reads the parameters back from `config.json`, so you
never pass them twice.

For long compiles over SSH, run inside `tmux` so a dropped connection does not
kill the job.

### 3. Run and verify

```bash
python run.py --label fr1
python run.py --label fr1 --timeout 60   # longer polling window
```

Expected output for a correct run:

```
[run] done = 256/256 ...
--- VERIFY ---
  finished PEs        : 256/256
  count mismatches    : 0
  key-sum mismatches  : 0
  header errors       : 0
  payload word errors : 0
  sentinel errors X/Y : 0/0
RESULT: PASS
```

If the run does not complete within the timeout, `run.py` prints a progress
table for the middle row (`dbg`): receive and send counters per direction, the
Phase X pass, the current Phase Y block, and done flags. This shows where the
routing stopped.

---

## Device-side exports

| symbol | type | content |
|---|---|---|
| `done` | `u32[1]` | 1 when the PE finished both phases |
| `stats` | `u32[8]` | consumed, Σkey, Σ(lo²+hi²), header err, payload err, X hop err, Y hop err, finished |
| `dbg` | `u32[16]` | progress counters for hang diagnosis (see `pe.csl`) |
| `runtime_init` | fn | arms the command stream; launch it first |
| `run_shift` | fn | seeds the payload and starts Phase X, then returns. The host polls `done` |
