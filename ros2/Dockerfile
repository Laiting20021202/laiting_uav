FROM ros@sha256:d9f14e84747bea639a65e00cf871d2c68ae55385d2acf6239614de0cadb41dcc
COPY ros2/config/apt.lock /tmp/apt.lock
RUN apt-get update -qq && xargs -a /tmp/apt.lock apt-get install -y --no-install-recommends \
    && dpkg-query -W > /opt/uav-packages.lock && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY uav/__init__.py uav/auth.py uav/config.py /app/uav/
COPY ros2/uav_ros /app/uav_ros
COPY ros2/web /app/web
COPY ros2/config /app/config
COPY ros2/entrypoint.sh /app/entrypoint.sh
RUN python3 -c "from pathlib import Path; import hashlib; root=Path('/app'); files=sorted(p for p in root.rglob('*') if p.is_file()); (root/'source.sha256').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(root))+'\\n' for p in files))"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ROS_LOG_DIR=/data/logs
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
