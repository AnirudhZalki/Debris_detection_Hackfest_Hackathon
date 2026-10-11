"""CDM triage model: probability that an encounter ends up as a high-risk conjunction.

LightGBM + isotonic calibrator trained offline on the ESA Kelvins collision-avoidance CDM
dataset (162,634 CDMs). Inputs are the Kelvins CDM fields; the ones we can derive from our own
screening (geometry, covariance sigmas, orbit elements, space weather, our Pc) are filled in and
the OD-quality fields we do not have are left missing (LightGBM handles NaN).

Caveats the dashboard repeats: the training targets are a handful of sun-synchronous
satellites (inc ~98 deg, ~700 km) at solar minimum, and the two strongest inputs are the Pc and
its scale factor, so the score is only reported for encounters inside that regime.
"""
from __future__ import annotations

import json
import logging
import math
import pickle
from pathlib import Path

import numpy as np

from ..astro.propagate import ric_frame

log = logging.getLogger("sdrp.triage")
MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "models" / "triage"
DOMAIN = {"inc_deg": (94.0, 102.0), "alt_km": (500.0, 950.0)}


class TriageModel:
    def __init__(self):
        import lightgbm as lgb
        self.names = json.loads((MODEL_DIR / "feature_names.json").read_text())
        self.booster = lgb.Booster(model_file=str(MODEL_DIR / "triage_model_lgb.txt"))
        with open(MODEL_DIR / "triage_calibrator.pkl", "rb") as f:   # sklearn IsotonicRegression
            self.iso = pickle.load(f)
        self.benchmark = json.loads((MODEL_DIR / "ml_metrics.json").read_text())

    @staticmethod
    def in_domain(prim):
        alt = 0.5 * (prim.perigee_km + prim.apogee_km)
        return (DOMAIN["inc_deg"][0] <= prim.inc_deg <= DOMAIN["inc_deg"][1]
                and DOMAIN["alt_km"][0] <= alt <= DOMAIN["alt_km"][1])

    def features(self, e, oe, prim, sec, swd, pc_scale):
        """One Kelvins-style feature row (metres, m/s, days, degrees)."""
        dr, dv = np.asarray(e["r2"] - e["r1"], float), np.asarray(e["v2"] - e["v1"], float)
        if e["swap"]:
            dr, dv = -dr, -dv
        r_p, v_p = (e["r2"], e["v2"]) if e["swap"] else (e["r1"], e["v1"])
        M = ric_frame(r_p, v_p)
        rr, vv = M @ dr * 1000, M @ dv * 1000
        miss_m = oe["miss_km"] * 1000
        sp = np.asarray(oe["sigma_primary_ric_km"]) * 1000
        ss = np.asarray(oe["sigma_secondary_ric_km"]) * 1000
        C = np.asarray(e["C"])
        try:
            maha = math.sqrt(max(float(dr @ np.linalg.solve(C, dr)), 0.0))
        except np.linalg.LinAlgError:
            maha = np.nan
        f = dict.fromkeys(self.names, np.nan)
        f.update({
            "time_to_tca": oe["lead_time_h"] / 24, "miss_distance": miss_m,
            "relative_speed": oe["rel_speed_kms"] * 1000,
            "relative_position_r": rr[0], "relative_position_t": rr[1], "relative_position_n": rr[2],
            "relative_velocity_r": vv[0], "relative_velocity_t": vv[1], "relative_velocity_n": vv[2],
            "max_risk_estimate": math.log10(max(oe["pc"], 1e-30)), "max_risk_scaling": pc_scale,
            "t_sigma_r": sp[0], "t_sigma_t": sp[1], "t_sigma_n": sp[2],
            "c_sigma_r": ss[0], "c_sigma_t": ss[1], "c_sigma_n": ss[2],
            "t_j2k_sma": prim.sma_km, "t_j2k_ecc": prim.ecc, "t_j2k_inc": prim.inc_deg,
            "c_j2k_sma": sec.sma_km, "c_j2k_ecc": sec.ecc, "c_j2k_inc": sec.inc_deg,
            "t_h_apo": prim.apogee_km, "t_h_per": prim.perigee_km,
            "c_h_apo": sec.apogee_km, "c_h_per": sec.perigee_km,
            "t_rcs_estimate": prim.rcs_m2 or np.nan, "c_rcs_estimate": sec.rcs_m2 or np.nan,
            "geocentric_latitude": oe["tca_lat"], "mahalanobis_distance": maha,
            "F10": swd.get("f107", np.nan), "F3M": swd.get("f107a", np.nan), "AP": swd.get("ap", np.nan),
            "log_miss_distance": math.log10(max(miss_m, 1e-3)),
            "relative_kinetic_energy": 0.5 * (oe["rel_speed_kms"] * 1000) ** 2,
            "miss_to_sigma_ratio": miss_m / max(math.hypot(oe["sigma_major_km"], oe["sigma_minor_km"]) * 1000, 1e-6),
        })
        return [f[n] for n in self.names]

    def score(self, rows):
        raw = self.booster.predict(np.asarray(rows, float))
        return self.iso.predict(raw), raw


def load():
    try:
        return TriageModel()
    except Exception as ex:
        log.warning("triage model unavailable: %s", ex)
        return None
