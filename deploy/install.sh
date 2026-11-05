#!/bin/sh
# Installs or updates send-to-kindle on an Ubuntu/Debian server. Run as root from a checkout:
#   sudo ./deploy/install.sh
# It creates the system user "kindle", puts the code in /opt/send-to-kindle with its own virtualenv,
# keeps state in /var/lib/send-to-kindle and installs the systemd units: the engine, its firewall table,
# and the converter (a fresh process with no network for every file). Settings (README, "Settings") go in
# /etc/send-to-kindle/settings, readable by root and the service only.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)

apt-get install -y -qq pandoc poppler-utils python3-venv git nftables tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-kat >/dev/null

id kindle >/dev/null 2>&1 || useradd --system --home-dir /var/lib/send-to-kindle --shell /usr/sbin/nologin kindle
install -d -o kindle -g kindle -m 750 /var/lib/send-to-kindle
install -d -o root -g kindle -m 750 /etc/send-to-kindle
if [ ! -f /etc/send-to-kindle/settings ]; then
  # Names only; fill in the values (README, "Settings").
  printf '%s=\n' STK_KINDLE_EMAIL STK_SMTP_USER STK_SMTP_PASSWORD STK_SENDER STK_TOKEN STK_PUBLIC_URL > /etc/send-to-kindle/settings
  printf 'STK_CONVERTER=/run/send-to-kindle/convert.sock\n' >> /etc/send-to-kindle/settings
  chown root:kindle /etc/send-to-kindle/settings
  chmod 640 /etc/send-to-kindle/settings
  echo "Fill in /etc/send-to-kindle/settings, then: systemctl restart send-to-kindle"
fi

install -d -m 755 /opt/send-to-kindle
# Requirements changed: a fresh virtualenv, so packages no longer needed don't linger.
cmp -s "$here/requirements.txt" /opt/send-to-kindle/requirements.txt || rm -rf /opt/send-to-kindle/.venv
rm -rf /opt/send-to-kindle/send_to_kindle
cp -r "$here/send_to_kindle" "$here/requirements.txt" /opt/send-to-kindle/
find /opt/send-to-kindle/send_to_kindle -name __pycache__ -prune -exec rm -rf {} +
[ -x /opt/send-to-kindle/.venv/bin/python ] || python3 -m venv /opt/send-to-kindle/.venv
/opt/send-to-kindle/.venv/bin/pip install -q --upgrade pip
/opt/send-to-kindle/.venv/bin/pip install -q --upgrade -r /opt/send-to-kindle/requirements.txt

install -m 644 "$here/deploy/egress.nft" /etc/send-to-kindle/egress.nft
for unit in send-to-kindle.service send-to-kindle-firewall.service send-to-kindle-convert.socket send-to-kindle-convert@.service \
            send-to-kindle-addresses.service send-to-kindle-addresses.timer; do
  install -m 644 "$here/deploy/$unit" "/etc/systemd/system/$unit"
done
# A failed unit leaves an alert for the next agent session, if the server has that set up (agent-alert@).
if [ -f /etc/systemd/system/agent-alert@.service ]; then
  for unit in send-to-kindle send-to-kindle-firewall send-to-kindle-addresses; do
    install -d -m 755 "/etc/systemd/system/$unit.service.d"
    printf '%s\n' '# Installed as a drop-in: when this unit fails, it leaves an alert for the next agent session.' \
      '[Unit]' 'OnFailure=agent-alert@%n.service' > "/etc/systemd/system/$unit.service.d/agent-alert.conf"
  done
fi
systemctl daemon-reload
# Where the engine may connect, before its firewall loads (the table includes the list).
systemctl start send-to-kindle-addresses
systemctl enable --now send-to-kindle-addresses.timer >/dev/null 2>&1
systemctl enable --now send-to-kindle-firewall >/dev/null 2>&1
systemctl reload send-to-kindle-firewall
systemctl enable --now send-to-kindle-convert.socket >/dev/null 2>&1
systemctl enable send-to-kindle >/dev/null 2>&1
systemctl restart send-to-kindle
sleep 2
systemctl --no-pager --lines=5 status send-to-kindle | head -12
