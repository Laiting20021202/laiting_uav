#!/bin/bash
set -eo pipefail
source /opt/ros/humble/setup.bash
set -u
exec python3 -m uav_ros.main
