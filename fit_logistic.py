"""
Fit the logistic model to pressure-vs-time for a run and store the result.

    ΔP(t) = A / (1 + e^{-k (t - t0)})      ΔP = P - P0 (gauge pressure)

Writes completion %, phase (lag / rapid / saturation) and ETA to fit_results.

    python fit_logistic.py --run 1              # fit once
    python fit_logistic.py --run 1 --watch 300  # refit every 5 min
    python fit_logistic.py --run 1 --plot       # also save run_1_fit.png
"""
import argparse
import math
import time
from datetime import timedelta

import numpy as np
import psycopg
from scipy.optimize import curve_fit

import config as C

ETA_FRACTION = 0.99  # "complete" = 99 % of saturation pressure


def logistic(t, a, k, t0):
    return a / (1.0 + np.exp(-k * (t - t0)))


def load(cur, run_id):
    cur.execute("SELECT started_at, baseline_hpa FROM runs WHERE run_id = %s", (run_id,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"run {run_id} not found")
    started_at, baseline = row
    # 1-minute averages straight from the hypertable (no need to wait for the cagg)
    cur.execute(
        """SELECT time_bucket(INTERVAL '1 minute', time) AS b, avg(pressure_hpa)
           FROM pressure_readings WHERE run_id = %s GROUP BY b ORDER BY b""",
        (run_id,),
    )
    rows = cur.fetchall()
    return started_at, baseline, rows


def fit(t_h, dp):
    a0 = max(dp.max() * 1.2, 1.0)
    half = np.argmax(dp >= dp.max() / 2) if dp.max() > 0 else len(dp) // 2
    p0 = [a0, 0.3, t_h[half]]
    popt, _ = curve_fit(
        logistic, t_h, dp, p0=p0,
        bounds=([0.1, 1e-4, -50.0], [2000.0, 20.0, 1000.0]), maxfev=20000,
    )
    resid = dp - logistic(t_h, *popt)
    ss_tot = np.sum((dp - dp.mean()) ** 2) or 1.0
    return popt, 1.0 - np.sum(resid ** 2) / ss_tot


def phase_of(t_now, k, t0):
    if t_now < t0 - 2.0 / k:
        return "lag"
    if t_now < t0 + 2.0 / k:
        return "rapid"
    return "saturation"


def run_fit(conn, run_id, plot=False):
    with conn.cursor() as cur:
        started_at, baseline, rows = load(cur, run_id)
        if len(rows) < 10:
            print(f"run {run_id}: only {len(rows)} minute-points, need >= 10")
            return
        times = [r[0] for r in rows]
        p = np.array([float(r[1]) for r in rows])
        p0_hpa = float(baseline) if baseline is not None else float(p[:5].mean())
        dp = p - p0_hpa
        # time origin = first reading (robust even if data was back-dated)
        started_at = times[0]
        t_h = np.array([(t - started_at).total_seconds() / 3600 for t in times])

        try:
            (a, k, t0), r2 = fit(t_h, dp)
        except RuntimeError as e:
            print(f"run {run_id}: fit did not converge ({e})")
            cur.execute(
                """INSERT INTO fit_results (run_id, n_points, baseline_hpa, current_dp_hpa, phase)
                   VALUES (%s,%s,%s,%s,'unknown')""",
                (run_id, len(rows), p0_hpa, float(dp[-1])),
            )
            return

        t_now = t_h[-1]
        current = float(logistic(t_now, a, k, t0))  # smoothed current ΔP
        completion = max(0.0, min(100.0, 100.0 * current / a))
        t_eta = t0 + math.log(ETA_FRACTION / (1 - ETA_FRACTION)) / k
        eta = started_at + timedelta(hours=float(t_eta))
        ph = phase_of(t_now, k, t0)

        cur.execute(
            """INSERT INTO fit_results (run_id, n_points, baseline_hpa, a_hpa, k_per_hour,
                   t0_hours, r_squared, current_dp_hpa, completion_pct, phase, eta)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, len(rows), p0_hpa, a, k, t0, r2, float(dp[-1]), completion, ph, eta),
        )
        print(f"run {run_id}: A={a:.1f} hPa  k={k:.3f}/h  t0={t0:.1f} h  R²={r2:.3f}  "
              f"→ {completion:.1f}% ({ph}), ETA {eta:%Y-%m-%d %H:%M}")

        if plot:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            tt = np.linspace(0, max(t_eta, t_now) * 1.05, 400)
            plt.figure(figsize=(8, 4.5))
            plt.plot(t_h, dp, ".", ms=3, label="measured ΔP (1-min avg)")
            plt.plot(tt, logistic(tt, a, k, t0), "-", label="logistic fit")
            plt.axvline(t_eta, ls="--", c="gray", label=f"99% ETA ({t_eta:.1f} h)")
            plt.xlabel("time since start (h)"); plt.ylabel("gauge pressure ΔP (hPa)")
            plt.title(f"Run {run_id} — {completion:.1f}% complete ({ph})")
            plt.legend(); plt.grid(alpha=0.3); plt.tight_layout()
            out = f"run_{run_id}_fit.png"
            plt.savefig(out, dpi=130)
            print(f"saved {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--watch", type=int, default=0, help="refit every N seconds")
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args()
    with psycopg.connect(C.DB_DSN, autocommit=True) as conn:
        while True:
            run_fit(conn, args.run, args.plot)
            if not args.watch:
                break
            time.sleep(args.watch)


if __name__ == "__main__":
    main()
