#!/bin/bash
# Cashed office screen - run ONCE on the Raspberry Pi (Raspberry Pi OS with desktop).
#   curl -sL https://cash-production-8165.up.railway.app/office/setup.sh | bash -s "https://cash-production-8165.up.railway.app/office?key=YOUR_KEY"
# After a restart the Pi opens the office screen full screen by itself, every time it's switched on.
set -e
URL="$1"
if [ -z "$URL" ]; then
  echo 'Usage: bash setup_office_pi.sh "https://cash-production-8165.up.railway.app/office?key=YOUR_KEY"'
  exit 1
fi
case "$URL" in *sound=*) ;; *) URL="$URL&sound=1" ;; esac   # payout chime on by default
case "$URL" in *pi=1*) ;; *) URL="$URL&pi=1" ;; esac         # the Pi speaks with its own voice (not the browser)

echo "== Installing the browser (this can take a few minutes)"
sudo apt-get update -y
if command -v chromium >/dev/null 2>&1; then BROWSER=chromium
elif command -v chromium-browser >/dev/null 2>&1; then BROWSER=chromium-browser
else
  sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser
  if command -v chromium >/dev/null 2>&1; then BROWSER=chromium; else BROWSER=chromium-browser; fi
fi
sudo apt-get install -y unclutter >/dev/null 2>&1 || true   # hides the mouse pointer (optional)
sudo apt-get install -y espeak-ng alsa-utils v4l-utils python3 curl   # backup voice, audio, TV power detection
sudo timedatectl set-timezone Europe/London || true

echo "== Installing the office voice (Piper)"
BASE="${URL%%/office*}"
KEY="$(printf '%s' "$URL" | sed -n 's/.*[?&]key=\([^&]*\).*/\1/p')"
printf 'BASE=%s\nKEY=%s\n' "$BASE" "$KEY" > "$HOME/.cashed-office"
chmod 600 "$HOME/.cashed-office"
case "$(uname -m)" in
  aarch64) PIPER_TAR=piper_linux_aarch64.tar.gz ;;
  armv7l)  PIPER_TAR=piper_linux_armv7l.tar.gz ;;
  *)       PIPER_TAR="" ;;
esac
if [ -n "$PIPER_TAR" ] && [ ! -x "$HOME/piper/piper" ]; then
  curl -fsSL "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/$PIPER_TAR" | tar xz -C "$HOME" \
    || echo "   (Piper download failed - the backup voice will be used)"
fi
if [ -x "$HOME/piper/piper" ] && [ ! -f "$HOME/piper/voice.onnx" ]; then
  V=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx
  curl -fsSL -o "$HOME/piper/voice.onnx" "$V" && curl -fsSL -o "$HOME/piper/voice.onnx.json" "$V.json" \
    || echo "   (Voice download failed - the backup voice will be used)"
fi

echo "== Installing the office speaker"
curl -fsSL "$BASE/office/announcer.py" -o "$HOME/office_announcer.py"
cat > "$HOME/cashed-voice.sh" <<VOICE
#!/bin/bash
exec 8>/tmp/cashed-voice.lock
flock -n 8 || exit 0
while true; do
  # auto-update: grab the latest version from the bot (keeps the old one if offline)
  curl -fsSL "$BASE/office/announcer.py" -o "$HOME/office_announcer.py.new" && mv "$HOME/office_announcer.py.new" "$HOME/office_announcer.py"
  python3 "$HOME/office_announcer.py" >> "$HOME/cashed-voice.log" 2>&1
  sleep 5
done
VOICE
chmod +x "$HOME/cashed-voice.sh"

echo "== Installing the Spotify speaker ('Office')"
if ! command -v librespot >/dev/null 2>&1; then
  curl -fsSL https://dtcooper.github.io/raspotify/install.sh | sh || echo "   (Spotify speaker install failed)"
fi
sudo systemctl disable --now raspotify >/dev/null 2>&1 || true   # we run it in the desktop session instead (shares the speaker)
cat > "$HOME/cashed-spotify.sh" <<SPOT
#!/bin/bash
exec 6>/tmp/cashed-spotify.lock
flock -n 6 || exit 0
sleep 10
mkdir -p "$HOME/.cache/librespot"
while true; do
  librespot --name "Office" --device-type speaker --bitrate 160 --backend alsa --device default \
    --initial-volume 60 --enable-volume-normalisation --cache "$HOME/.cache/librespot" --disable-audio-cache \
    >> "$HOME/cashed-spotify.log" 2>&1
  sleep 5
done
SPOT
chmod +x "$HOME/cashed-spotify.sh"

echo "== Installing 'Hey Ultra' voice control"
sudo apt-get install -y python3-venv unzip
[ -d "$HOME/cashed-venv" ] || python3 -m venv "$HOME/cashed-venv"
"$HOME/cashed-venv/bin/pip" install -q vosk || echo "   (Vosk install failed - voice control won't start)"
if [ ! -d "$HOME/vosk-model" ]; then
  curl -fsSL -o /tmp/vosk.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip \
    && unzip -q -o /tmp/vosk.zip -d "$HOME" && mv "$HOME/vosk-model-small-en-us-0.15" "$HOME/vosk-model" \
    || echo "   (Wake word model download failed)"
  rm -f /tmp/vosk.zip
fi
curl -fsSL "$BASE/office/listener.py" -o "$HOME/office_listener.py"
cat > "$HOME/cashed-listen.sh" <<LISTEN
#!/bin/bash
exec 7>/tmp/cashed-listen.lock
flock -n 7 || exit 0
sleep 15
while true; do
  wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 1.0 2>/dev/null || amixer -q sset Capture 100% 2>/dev/null   # mic up full
  curl -fsSL "$BASE/office/listener.py" -o "$HOME/office_listener.py.new" && mv "$HOME/office_listener.py.new" "$HOME/office_listener.py"
  curl -fsSL "$BASE/office/announcer.py" -o "$HOME/office_announcer.py.new" && mv "$HOME/office_announcer.py.new" "$HOME/office_announcer.py"
  "$HOME/cashed-venv/bin/python" "$HOME/office_listener.py" >> "$HOME/cashed-listen.log" 2>&1
  sleep 5
done
LISTEN
chmod +x "$HOME/cashed-listen.sh"

echo "== Creating the kiosk launcher"
cat > "$HOME/cashed-office.sh" <<KIOSK
#!/bin/bash
exec 9>/tmp/cashed-office.lock
flock -n 9 || exit 0                       # only ever one copy running
xset s off 2>/dev/null; xset -dpms 2>/dev/null; xset s noblank 2>/dev/null
command -v unclutter >/dev/null && (unclutter -idle 3 &)
sleep 5                                    # let Wi-Fi connect
while true; do
  $BROWSER --kiosk --incognito --noerrdialogs --disable-infobars --disable-session-crashed-bubble \\
    --autoplay-policy=no-user-gesture-required --check-for-update-interval=31536000 \\
    --overscroll-history-navigation=0 --disable-pinch --touch-events=enabled \\
    --disable-features=Translate,TouchpadOverscrollHistoryNavigation \\
    "$URL"
  sleep 3                                  # if the browser ever closes or crashes, reopen it
done
KIOSK
chmod +x "$HOME/cashed-office.sh"

echo "== Starting it automatically on boot"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/cashed-spotify.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Office Spotify Speaker
Exec=$HOME/cashed-spotify.sh
X-GNOME-Autostart-enabled=true
DESK
cat > "$HOME/.config/autostart/cashed-listen.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Ultra Voice Control
Exec=$HOME/cashed-listen.sh
X-GNOME-Autostart-enabled=true
DESK
cat > "$HOME/.config/autostart/cashed-voice.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Ultra Office Speaker
Exec=$HOME/cashed-voice.sh
X-GNOME-Autostart-enabled=true
DESK
cat > "$HOME/.config/autostart/cashed-office.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Cashed Office Screen
Exec=$HOME/cashed-office.sh
X-GNOME-Autostart-enabled=true
DESK
mkdir -p "$HOME/.config/labwc"
grep -q cashed-office "$HOME/.config/labwc/autostart" 2>/dev/null || echo "$HOME/cashed-office.sh &" >> "$HOME/.config/labwc/autostart"
grep -q cashed-voice "$HOME/.config/labwc/autostart" 2>/dev/null || echo "$HOME/cashed-voice.sh &" >> "$HOME/.config/labwc/autostart"
grep -q cashed-listen "$HOME/.config/labwc/autostart" 2>/dev/null || echo "$HOME/cashed-listen.sh &" >> "$HOME/.config/labwc/autostart"
grep -q cashed-spotify "$HOME/.config/labwc/autostart" 2>/dev/null || echo "$HOME/cashed-spotify.sh &" >> "$HOME/.config/labwc/autostart"
mkdir -p "$HOME/.config/lxsession/LXDE-pi"
grep -q cashed-office "$HOME/.config/lxsession/LXDE-pi/autostart" 2>/dev/null || echo "@$HOME/cashed-office.sh" >> "$HOME/.config/lxsession/LXDE-pi/autostart"
grep -q cashed-voice "$HOME/.config/lxsession/LXDE-pi/autostart" 2>/dev/null || echo "@$HOME/cashed-voice.sh" >> "$HOME/.config/lxsession/LXDE-pi/autostart"
grep -q cashed-listen "$HOME/.config/lxsession/LXDE-pi/autostart" 2>/dev/null || echo "@$HOME/cashed-listen.sh" >> "$HOME/.config/lxsession/LXDE-pi/autostart"
grep -q cashed-spotify "$HOME/.config/lxsession/LXDE-pi/autostart" 2>/dev/null || echo "@$HOME/cashed-spotify.sh" >> "$HOME/.config/lxsession/LXDE-pi/autostart"

echo "== Desktop auto-login, no screen blanking, fresh restart at 5am daily"
sudo raspi-config nonint do_boot_behaviour B4 || true
sudo raspi-config nonint do_blanking 1 || true
( sudo crontab -l 2>/dev/null | grep -v 'cashed-reboot'; echo "0 5 * * * /sbin/shutdown -r now # cashed-reboot" ) | sudo crontab -

echo
echo "Done. Restarting in 10 seconds - the office screen will open by itself."
echo "Turn the TV off and on again to hear the rundown (06:00-21:00)."
echo "Plug in the webcam / USB mic, then say: Hey Ultra, what have we paid out today?"
echo "Spotify: open Spotify on your phone (same Wi-Fi), tap the speaker icon and pick 'Office' once."
echo "To leave the screen: plug in a keyboard and press Alt+F4."
sleep 10
sudo reboot
