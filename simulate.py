"""
Publish fake sensor data over MQTT so you can test the whole pipeline
without the MYOSA board. Time is compressed: 1 real second = 1 sim minute by default.

    python simulate.py --run 1                 # 48 h run, ~48 min real time
    python simulate.py --run 1 --speed 0       # dump everything instantly
"""
import argparse
import json
import math
import random
import time

import paho.mqtt.client as mqtt

import config as C


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--speed", type=float, default=1.0, help="real seconds per simulated minute")
    ap.add_argument("--a", type=float, default=65, help="saturation ΔP in hPa")
    ap.add_argument("--k", type=float, default=0.25, help="growth rate per hour")
    ap.add_argument("--t0", type=float, default=18, help="inflection point, hours")
    args = ap.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="myosa-sim")
    client.connect(C.MQTT_HOST, C.MQTT_PORT)
    client.loop_start()

    p0 = 1005.0
    start = time.time() - args.hours * 3600  # backdate so timestamps span the run
    for minute in range(int(args.hours * 60)):
        t_h = minute / 60
        ts = start + minute * 60
        dp = args.a / (1 + math.exp(-args.k * (t_h - args.t0)))
        msgs = {
            "pressure": {"run_id": args.run, "ts": ts,
                         "pressure_hpa": round(p0 + dp + random.gauss(0, 0.3), 2),
                         "bmp_temp_c": round(27 + random.gauss(0, 0.2), 2)},
            "temperature": {"run_id": args.run, "ts": ts,
                            "object_c": round(26.5 + random.gauss(0, 0.2), 2),
                            "ambient_c": round(27.5 + random.gauss(0, 0.2), 2)},
            "motion": {"run_id": args.run, "ts": ts,
                       "ax": random.gauss(0, 0.01), "ay": random.gauss(0, 0.01),
                       "az": 1 + random.gauss(0, 0.01), "gx": 0, "gy": 0, "gz": 0},
        }
        for sensor, payload in msgs.items():
            info = client.publish(C.TOPICS[sensor], json.dumps(payload), qos=1)
        if not args.speed and minute % 50 == 0:
            info.wait_for_publish()  # don't flood the queue
        if args.speed:
            time.sleep(args.speed)
    info.wait_for_publish()  # make sure every message is actually sent
    time.sleep(2)
    client.loop_stop()
    print("simulation done")


if __name__ == "__main__":
    main()
