"""Collision-avoidance maneuver recommendation (minimum delta-v).

For each candidate burn time (k half-revolutions before TCA, never in the past) and
direction (+/- along-track, +/- radial, +/- cross-track) we use Clohessy-Wiltshire
relative-motion dynamics to map an impulse into a displacement of the primary at TCA, then
bisect on the impulse magnitude until Pc drops below the target. The cheapest option wins.
Along-track burns ~half an orbit or more before TCA are usually most efficient (secular
along-track drift grows as 3*dv*t), which the search discovers automatically.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from ..astro.pc import pc_foster
from ..astro.propagate import MU, ric_frame


def cw_displacement(n, t, dv_ric):
    """Position change in RIC (km) at time t (s) after an impulse dv_ric (km/s)."""
    vx, vy, vz = dv_ric
    nt = n * t
    x = math.sin(nt) / n * vx + 2.0 / n * (1 - math.cos(nt)) * vy
    y = 2.0 / n * (math.cos(nt) - 1) * vx + (4 * math.sin(nt) - 3 * nt) / n * vy
    z = math.sin(nt) / n * vz
    return np.array([x, y, z])


def recommend(ev, C, hbr_km, now: datetime, target_pc=1e-5, max_dv_ms=5.0):
    r1, v1, r2, v2 = (np.asarray(ev[k]) for k in ("r1", "v1", "r2", "v2"))
    dr0 = r2 - r1
    dv_rel = v2 - v1
    a = 1.0 / (2 / np.linalg.norm(r1) - np.dot(v1, v1) / MU)
    n = math.sqrt(MU / a**3)
    period = 2 * math.pi / n
    M = ric_frame(r1, v1)                  # rows R, I, C (at TCA)
    tca = ev["tca"]
    pc0 = pc_foster(dr0, dv_rel, C, hbr_km)[0]
    time_left = (tca - now).total_seconds()
    options = []
    dirs = {"+along-track": (0, 1, 0), "-along-track": (0, -1, 0), "+radial": (1, 0, 0),
            "-radial": (-1, 0, 0), "+cross-track": (0, 0, 1), "-cross-track": (0, 0, -1)}
    for half_revs in (0.5, 1, 1.5, 2, 3, 4, 6, 8, 12):
        t = half_revs * period / 2
        if t > time_left - 600:            # need 10 min to upload commands
            continue
        for name, d in dirs.items():
            d = np.array(d, float)

            def pc_for(dv_kms):
                dpos_ric = cw_displacement(n, t, d * dv_kms)
                new_dr = dr0 - M.T @ dpos_ric          # primary moves -> relative vector changes
                return pc_foster(new_dr, dv_rel, C, hbr_km)[0], new_dr

            hi = max_dv_ms / 1000.0
            pc_hi, _ = pc_for(hi)
            if pc_hi > target_pc:
                continue
            lo = 0.0
            for _ in range(30):
                mid = 0.5 * (lo + hi)
                if pc_for(mid)[0] > target_pc:
                    lo = mid
                else:
                    hi = mid
            pc_new, new_dr = pc_for(hi)
            options.append({"burn_time": (tca - timedelta(seconds=t)).isoformat(),
                            "lead_time_h": t / 3600.0, "revs_before_tca": half_revs / 2,
                            "direction": name, "dv_ms": hi * 1000.0, "pc_after": pc_new,
                            "miss_after_km": float(np.linalg.norm(new_dr))})
    options.sort(key=lambda o: o["dv_ms"])
    best = options[0] if options else None
    # naive reference: radial burn half a rev before TCA (typical late "reactive" maneuver)
    naive = next((o for o in options if o["direction"].endswith("radial") and o["revs_before_tca"] == 0.25), None)
    late = [o for o in options if o["revs_before_tca"] == 0.25]
    naive = naive or (min(late, key=lambda o: o["dv_ms"]) if late else None)
    eff = None
    if best and naive and naive["dv_ms"] > 0:
        eff = 1 - best["dv_ms"] / naive["dv_ms"]
    return {"pc_before": pc0, "target_pc": target_pc, "best": best, "naive_reference": naive,
            "dv_saving_vs_naive": eff, "options": options[:12], "feasible": best is not None}
