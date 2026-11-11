"""
Manage fermentation runs.

    python runs.py new "Kilju test 1" --sugar 70 --water 250 --headspace 100 --leak "needle 2 turns"
    python runs.py list
    python runs.py status
    python runs.py end 1
    python runs.py export 1          # -> run_1_pressure.csv
"""
import argparse
import csv

import psycopg

import config as C


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("name")
    n.add_argument("--sugar", type=float, default=70)
    n.add_argument("--water", type=float, default=250)
    n.add_argument("--headspace", type=float, default=100)
    n.add_argument("--leak", default=None)
    n.add_argument("--notes", default=None)
    sub.add_parser("list")
    sub.add_parser("status")
    e = sub.add_parser("end"); e.add_argument("run_id", type=int)
    x = sub.add_parser("export"); x.add_argument("run_id", type=int)
    a = ap.parse_args()

    with psycopg.connect(C.DB_DSN, autocommit=True) as conn, conn.cursor() as cur:
        if a.cmd == "new":
            cur.execute(
                """INSERT INTO runs (name, sugar_mass_g, water_volume_ml, headspace_volume_ml,
                                     leak_setting, notes)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING run_id""",
                (a.name, a.sugar, a.water, a.headspace, a.leak, a.notes),
            )
            print(f"created run_id = {cur.fetchone()[0]}  (send this run_id from the board)")
        elif a.cmd == "list":
            cur.execute("SELECT run_id, name, started_at, ended_at FROM runs ORDER BY run_id")
            for r in cur.fetchall():
                print(*r, sep=" | ")
        elif a.cmd == "status":
            cur.execute("SELECT * FROM run_status ORDER BY run_id")
            cols = [d.name for d in cur.description]
            for r in cur.fetchall():
                print("\n".join(f"{c:>18}: {v}" for c, v in zip(cols, r)), "\n")
        elif a.cmd == "end":
            cur.execute("UPDATE runs SET ended_at = now() WHERE run_id = %s", (a.run_id,))
            print(f"run {a.run_id} ended")
        elif a.cmd == "export":
            cur.execute(
                """SELECT time, pressure_hpa, bmp_temp_c FROM pressure_readings
                   WHERE run_id = %s ORDER BY time""", (a.run_id,))
            out = f"run_{a.run_id}_pressure.csv"
            with open(out, "w", newline="") as f:
                w = csv.writer(f); w.writerow(["time", "pressure_hpa", "bmp_temp_c"])
                w.writerows(cur.fetchall())
            print(f"saved {out}")


if __name__ == "__main__":
    main()
