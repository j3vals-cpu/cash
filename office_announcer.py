#!/usr/bin/env python3
"""
Office speaker for the Raspberry Pi.

- When the office TV is turned on (06:00-21:00) it reads out the rundown: what needs doing in Discord and
  what people have been talking about, then your reminders from the day before, then weather, to-do list,
  US news, Nasdaq open and today's payouts.
  It spots the TV turning on through HDMI-CEC (the TV's own power signal), or the screen connecting.
- It also speaks anything sent with /office say or /office rundown in Discord.

Uses the Piper voice if it's installed (natural), otherwise espeak-ng (robotic).
Settings come from ~/.cashed-office (written by the setup script):
    BASE=https://cash-production-8165.up.railway.app
    KEY=...
"""
import json
import math
import os
import shutil
import struct
import subprocess
import time
import urllib.parse
import urllib.request
import wave
from datetime import datetime

HOME = os.path.expanduser("~")
CONF = os.path.join(HOME, ".cashed-office")
PIPER = os.path.join(HOME, "piper", "piper")
VOICE = os.path.join(HOME, "piper", "voice.onnx")
CHIME = os.path.join(HOME, ".cashed-chime.wav")
SPEAK_FROM, SPEAK_TO = 6, 21   # only read the rundown during these hours
COOLDOWN = 3 * 3600            # don't repeat the rundown if the TV is switched off and on again within 3 hours


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def conf():
    c = {}
    with open(CONF) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                c[k] = v
    return c["BASE"].rstrip("/"), c["KEY"]


def get(base, key, path, **params):
    params["key"] = key
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read().decode())


def make_chime():
    if os.path.exists(CHIME):
        return
    rate, notes = 22050, [(1318.5, 0.0), (1760, 0.09), (2637, 0.18), (2093, 0.30)]
    n = int(rate * 1.0)
    buf = [0.0] * n
    for f, start in notes:
        s0 = int(start * rate)
        for i in range(int(0.55 * rate)):
            if s0 + i >= n:
                break
            env = math.exp(-6 * i / rate)
            buf[s0 + i] += 0.25 * env * math.sin(2 * math.pi * f * i / rate)
    with wave.open(CHIME, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 32000)) for x in buf))


def chime():
    open(SPEAKING, "w").close()
    subprocess.run(["aplay", "-q", CHIME], check=False)


def voice_rate():
    try:
        with open(VOICE + ".json") as f:
            return json.load(f)["audio"]["sample_rate"]
    except Exception:
        return 22050


SPEAKING = "/tmp/cashed-speaking"  # tells the "Hey Ultra" listener to ignore the speaker's own voice


def duck(on: bool):
    """Turn the Spotify music down while Ultra listens/talks (and back up after)."""
    try:
        base, key = conf()
        req = urllib.request.Request(f"{base}/office/duck?{urllib.parse.urlencode({'key': key, 'on': '1' if on else '0'})}",
                                     data=b"", method="POST")
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        pass


def say(text):
    text = " ".join(text.split())
    if not text:
        return
    open(SPEAKING, "w").close()
    duck(True)
    try:
        _say(text)
    finally:
        duck(False)
        try:
            os.remove(SPEAKING)
        except OSError:
            pass


def _say(text):
    log("Saying:", text[:120])
    if os.path.exists(PIPER) and os.path.exists(VOICE):
        try:  # stream while it speaks, so long text starts straight away
            p1 = subprocess.Popen([PIPER, "--model", VOICE, "--output-raw", "--sentence_silence", "0.35"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            p2 = subprocess.Popen(["aplay", "-q", "-r", str(voice_rate()), "-f", "S16_LE", "-t", "raw", "-"],
                                  stdin=p1.stdout)
            p1.stdin.write(text.encode()); p1.stdin.close()
            p2.wait(); p1.wait()
            if p1.returncode == 0:
                return
        except Exception as e:
            log("Piper failed:", e)
    if shutil.which("espeak-ng"):
        subprocess.run(["espeak-ng", "-v", "en-gb", "-s", "160", text], check=False)


_cec_ready = False


def display_on():
    """True if the TV/monitor is on, False if off, None if we can't tell."""
    global _cec_ready
    if os.path.exists("/dev/cec0") and shutil.which("cec-ctl"):
        try:
            if not _cec_ready:
                subprocess.run(["cec-ctl", "-d", "/dev/cec0", "--playback"], capture_output=True, timeout=10)
                _cec_ready = True
            out = subprocess.run(["cec-ctl", "-d", "/dev/cec0", "--to", "0", "--give-device-power-status"],
                                 capture_output=True, text=True, timeout=10).stdout.lower()
            if "pwr-state: on" in out or "to-on" in out:
                return True
            if "standby" in out:
                return False
        except Exception:
            pass
    # fallback: many TVs/monitors disconnect HDMI when switched off
    import glob
    states = []
    for path in glob.glob("/sys/class/drm/card*-HDMI-A-*/status"):
        try:
            with open(path) as f:
                states.append(f.read().strip() == "connected")
        except Exception:
            pass
    return any(states) if states else None


def wait_for_network(base, key):
    for _ in range(60):
        try:
            get(base, key, "/office/voice", after=-1)
            return True
        except Exception:
            time.sleep(5)
    return False


def main():
    base, key = conf()
    make_chime()
    if not wait_for_network(base, key):
        log("No connection to the bot - will keep trying")
    last, was_on, last_rundown = -1, None, 0.0
    next_check = 0.0
    while True:
        # 1) TV turned on -> rundown
        if time.time() >= next_check:
            next_check = time.time() + 5
            on = display_on()
            try:
                quiet_now = get(base, key, "/office/voice", after=0).get("quiet") if on and was_on is not True else False
            except Exception:
                quiet_now = False
            if on and was_on is not True and quiet_now:
                log("Screen on, but quiet mode is on - skipping the rundown")
                was_on = True
            elif on and was_on is not True:  # just turned on (or already on when the Pi started)
                hour = datetime.now().hour
                if SPEAK_FROM <= hour < SPEAK_TO and time.time() - last_rundown > COOLDOWN:
                    log("Screen turned on - reading the rundown")
                    try:
                        time.sleep(6)  # give the TV a moment to wake its speakers
                        text = get(base, key, "/office/rundown")["text"]
                        chime(); say(text)
                        last_rundown = time.time()
                    except Exception as e:
                        log("Rundown failed:", e)
            if on is not None:
                was_on = on
        # 2) anything sent from Discord (/office say, /office rundown) or the screen's Rundown button
        try:
            d = get(base, key, "/office/voice", after=last)
            quiet = d.get("quiet")
            if last >= 0:
                for item in d["items"]:
                    if quiet and item["kind"] != "timer":
                        continue  # quiet mode: show on screen only
                    for _ in range(60):  # let Ultra finish its reply first (it's talking from the listener)
                        if not os.path.exists(SPEAKING):
                            break
                        time.sleep(0.5)
                    chime()
                    say(("Announcement from " + item["by"] + ". " if item["kind"] == "say" else "") + item["text"])
                    if item["kind"] == "rundown":
                        last_rundown = time.time()
            last = d["last"]
        except Exception as e:
            log("Poll failed:", e)
            time.sleep(10)
        time.sleep(3)


if __name__ == "__main__":
    main()
