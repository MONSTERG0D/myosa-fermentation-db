"""
MQTT -> TimescaleDB subscriber.

Run one process per sensor (the "three subscribers" from the doc):
    python subscriber.py pressure
    python subscriber.py temperature
    python subscriber.py motion

Expected JSON payloads from the MYOSA board ("ts" optional, epoch seconds):
    pressure:    {"run_id": 1, "ts": 1731150000, "pressure_hpa": 1013.42, "bmp_temp_c": 27.1}
    temperature: {"run_id": 1, "object_c": 26.8, "ambient_c": 27.5}
    motion:      {"run_id": 1, "ax": 0.01, "ay": -0.02, "az": 0.99, "gx": 0.3, "gy": -0.1, "gz": 0.0}
"""
import json
import math
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import psycopg

import config as C


def parse_time(payload):
    ts = payload.get("ts")
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime.fromisoformat(str(ts))


def in_range(v, lo_hi):
    return v is not None and lo_hi[0] <= v <= lo_hi[1]


def alert(cur, run_id, source, level, msg):
    cur.execute(
        "INSERT INTO alerts (run_id, source, level, message) VALUES (%s,%s,%s,%s)",
        (run_id, source, level, msg),
    )
    print(f"[ALERT/{level}] {source}: {msg}")


# ---------------- handlers (validate + insert) ----------------

def handle_pressure(cur, p):
    run_id, t = int(p["run_id"]), parse_time(p)
    hpa = float(p["pressure_hpa"])
    if not in_range(hpa, C.PRESSURE_RANGE_HPA):
        alert(cur, run_id, "bmp180", "warning", f"pressure out of range: {hpa} hPa")
        return
    cur.execute(
        """INSERT INTO pressure_readings (time, run_id, pressure_hpa, bmp_temp_c)
           VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (t, run_id, hpa, p.get("bmp_temp_c")),
    )
    # First reading of a run becomes the atmospheric baseline P0
    cur.execute(
        "UPDATE runs SET baseline_hpa = %s WHERE run_id = %s AND baseline_hpa IS NULL",
        (hpa, run_id),
    )
    if hpa > C.PRESSURE_RANGE_HPA[1] - 20:
        alert(cur, run_id, "bmp180", "critical", f"near sensor cap ({hpa} hPa) - check micro-leak")


def handle_temperature(cur, p):
    run_id, t = int(p["run_id"]), parse_time(p)
    obj, amb = p.get("object_c"), p.get("ambient_c")
    if obj is not None and not in_range(obj, C.OBJECT_TEMP_RANGE_C):
        alert(cur, run_id, "gy906", "warning", f"object temp out of range: {obj}")
        return
    if amb is not None and not in_range(amb, C.AMBIENT_TEMP_RANGE_C):
        amb = None
    cur.execute(
        """INSERT INTO temperature_readings (time, run_id, object_c, ambient_c)
           VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (t, run_id, obj, amb),
    )


def handle_motion(cur, p):
    run_id, t = int(p["run_id"]), parse_time(p)
    ax, ay, az = (float(p.get(k, 0)) for k in ("ax", "ay", "az"))
    mag = math.sqrt(ax * ax + ay * ay + az * az)
    dev = abs(mag - 1.0)
    event = "lid_open" if dev >= C.LID_OPEN_G else "vibration" if dev >= C.VIBRATION_G else "none"
    cur.execute(
        """INSERT INTO motion_readings (time, run_id, ax, ay, az, gx, gy, gz, accel_mag, event)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (t, run_id, ax, ay, az, p.get("gx"), p.get("gy"), p.get("gz"), mag, event),
    )
    if event != "none":
        alert(cur, run_id, "mpu6050", "critical" if event == "lid_open" else "warning",
              f"{event} detected (|a|={mag:.2f} g)")


HANDLERS = {"pressure": handle_pressure, "temperature": handle_temperature, "motion": handle_motion}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in HANDLERS:
        sys.exit("usage: python subscriber.py [pressure|temperature|motion]")
    sensor = sys.argv[1]
    topic, handler = C.TOPICS[sensor], HANDLERS[sensor]
    conn = psycopg.connect(C.DB_DSN, autocommit=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"[{sensor}] connected to MQTT ({reason_code}); subscribing {topic}")
        client.subscribe(topic, qos=1)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            with conn.cursor() as cur:
                handler(cur, payload)
        except (ValueError, KeyError, TypeError) as e:
            print(f"[{sensor}] rejected bad message {msg.payload!r}: {e}")
        except psycopg.Error as e:
            print(f"[{sensor}] DB error: {e}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"myosa-{sensor}-sub")
    client.on_connect, client.on_message = on_connect, on_message
    client.connect(C.MQTT_HOST, C.MQTT_PORT, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
