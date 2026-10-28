#!/bin/sh
# Installs or updates send-to-kindle on an Ubuntu/Debian server. Run as root from a checkout:
#   sudo ./deploy/install.sh
# It creates the system user "kindle", puts the code in /opt/send-to-kindle with its own virtualenv,
# keeps state in /var/lib/send-to-kindle and installs the systemd service. Settings (README,
# "Settings") go in /etc/send-to-kindle/settings, readable by root and the service only.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)

apt-get install -y -qq pandoc poppler-utils python3-venv git >/dev/null

id kindle >/dev/null 2>&1 || useradd --system --home-dir /var/lib/send-to-kindle --shell /usr/sbin/nologin kindle
install -d -o kindle -g kindle -m 750 /var/lib/send-to-kindle
install -d -o root -g kindle -m 750 /etc/send-to-kindle
if [ ! -f /etc/send-to-kindle/settings ]; then
  # Names only; fill in the values (README, "Settings").
  printf '%s=\n' STK_KINDLE_EMAIL STK_SMTP_USER STK_SMTP_PASSWORD STK_SENDER STK_TOKEN > /etc/send-to-kindle/settings
  chown root:kindle /etc/send-to-kindle/settings
  chmod 640 /etc/send-to-kindle/settings
  echo "Fill in /etc/send-to-kindle/settings, then: systemctl restart send-to-kindle"
fi

install -d -m 755 /opt/send-to-kindle
cp -r "$here/send_to_kindle" "$here/requirements.txt" /opt/send-to-kindle/
[ -x /opt/send-to-kindle/.venv/bin/python ] || python3 -m venv /opt/send-to-kindle/.venv
/opt/send-to-kindle/.venv/bin/pip install -q --upgrade pip
/opt/send-to-kindle/.venv/bin/pip install -q --upgrade -r /opt/send-to-kindle/requirements.txt

install -m 644 "$here/deploy/send-to-kindle.service" /etc/systemd/system/send-to-kindle.service
systemctl daemon-reload
systemctl enable --now send-to-kindle >/dev/null 2>&1
systemctl restart send-to-kindle
sleep 2
systemctl --no-pager --lines=5 status send-to-kindle | head -12
