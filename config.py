"""Shared settings. Override any value with an environment variable."""
import os

DB_DSN = os.getenv(
    "MYOSA_DB_DSN",
    "host=localhost port=5432 dbname=fermentation user=myosa password=myosa connect_timeout=5",
)

MQTT_HOST = os.getenv("MYOSA_MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MYOSA_MQTT_PORT", "1883"))

# One topic per sensor (matches "three separate Python subscribers" in the doc)
TOPICS = {
    "pressure": "myosa/ferment/pressure",       # BMP180
    "temperature": "myosa/ferment/temperature", # GY906
    "motion": "myosa/ferment/motion",           # MPU6050
}

# Validation limits
PRESSURE_RANGE_HPA = (300.0, 1100.0)   # BMP180 datasheet
OBJECT_TEMP_RANGE_C = (-70.0, 380.0)   # GY906 / MLX90614
AMBIENT_TEMP_RANGE_C = (-40.0, 125.0)
VIBRATION_G = 0.15     # |accel| deviation from 1 g that counts as vibration
LID_OPEN_G = 0.60      # bigger jolt / tilt => lid opened
