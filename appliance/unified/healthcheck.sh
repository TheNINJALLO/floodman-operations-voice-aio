#!/bin/sh
set -eu
curl -fsS --max-time 5 http://127.0.0.1:8002/ready >/dev/null
curl -fsS --max-time 5 http://127.0.0.1:9000/health/live >/dev/null
curl -fsS --max-time 5 http://127.0.0.1:9004/health/ready >/dev/null

