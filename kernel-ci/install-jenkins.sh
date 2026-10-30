#!/bin/sh
# Run from an interactive terminal as florian. sudo is only used for Docker.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
state=/home/florian/.local/state/kernel-ci
test "$(id -un)" = florian
umask 077
mkdir -p "$state/jenkins-home" "$state/agent"
if [ ! -s "$state/admin-password" ]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$state/admin-password"
fi
sudo docker build -t home-lab-jenkins "$repo/kernel-ci"
# Deliberately fail on an existing container rather than replace its state.
sudo docker run -d --name home-lab-jenkins --restart unless-stopped \
    -p 127.0.0.1:8080:8080 \
    -e JAVA_OPTS=-Djenkins.install.runSetupWizard=false \
    -v "$state/jenkins-home:/var/jenkins_home" \
    -v "$state/admin-password:/run/lab-admin-password:ro" \
    -v "$repo/kernel-ci/Jenkinsfile:/run/lab-Jenkinsfile:ro" \
    home-lab-jenkins
# Copy the image's Java runtime for the host agent; no host package changes.
sudo docker cp home-lab-jenkins:/opt/java/openjdk "$state/jdk"
sudo chown -R "$(id -u):$(id -g)" "$state/jdk"
python3 - "$state" <<'PY'
import pathlib, sys, time, urllib.request
state = pathlib.Path(sys.argv[1])
for attempt in range(180):
    try:
        urllib.request.urlretrieve('http://127.0.0.1:8080/jnlpJars/agent.jar', state / 'agent.jar')
        if (state / 'jenkins-home/secrets/lab-agent-secret').exists():
            break
    except OSError:
        pass
    time.sleep(2)
else:
    raise SystemExit('Jenkins startup timed out; inspect sudo docker logs home-lab-jenkins')
PY
mkdir -p /home/florian/.config/systemd/user
cat > /home/florian/.config/systemd/user/kernel-ci-agent.service <<EOF
[Unit]
Description=Home lab Jenkins agent
After=network-online.target

[Service]
ExecStart=$state/jdk/bin/java -jar $state/agent.jar -url http://127.0.0.1:8080/ -name home-lab -secret @$state/jenkins-home/secrets/lab-agent-secret -webSocket -workDir $state/agent
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now kernel-ci-agent.service
printf 'Jenkins: http://127.0.0.1:8080/ (user admin)\nPassword file: %s/admin-password\n' "$state"
printf 'linux-master is queued; its first run registers the polling schedule.\n'
