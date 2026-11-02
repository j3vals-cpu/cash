#!/usr/bin/env python3
"""
"Hey Ultra" voice control for the Raspberry Pi.

1. Listens offline (Vosk, free) for the wake phrase "hey ultra" - nothing is sent anywhere until then.
2. Beeps, records what you say until you stop talking (max 12 seconds).
3. Sends the recording to the bot, which turns it into text (Whisper) and does it (Claude).
4. Speaks the reply.

Works with any USB mic (an old webcam is fine). Settings: ~/.cashed-office (BASE, KEY, optional MIC=alsa device).
"""
import array
import io
import json
import math
import os
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import office_announcer as speaker  # reuse the voice + chime  # noqa: E402

HOME = os.path.expanduser("~")
MODEL_DIR = os.path.join(HOME, "vosk-model")
BEEP = os.path.join(HOME, ".cashed-beep.wav")
SPEAKING = "/tmp/cashed-speaking"       # the speaker sets this while talking, so we don't hear ourselves
RATE, CHUNK = 16000, 4000               # 16 kHz mono, 0.125 s chunks
WAKE = ["hey ultra", "okay ultra", "ok ultra", "ultra"]


def log(*a):
    speaker.log("[listen]", *a)


def conf():
    c = {}
    with open(os.path.join(HOME, ".cashed-office")) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                c[k] = v
    return c["BASE"].rstrip("/"), c["KEY"], c.get("MIC", "default")


def rms(data: bytes) -> float:
    a = array.array("h", data[: len(data) // 2 * 2])
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def make_beep():
    if os.path.exists(BEEP):
        return
    n = int(0.18 * 22050)
    frames = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 1046 * i / 22050) * min(1, (n - i) / 800)))
                      for i in range(n))
    with wave.open(BEEP, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(frames)


def beep():
    subprocess.run(["aplay", "-q", BEEP], check=False)


def mic_stream(device):
    return subprocess.Popen(["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def record_command(proc, ambient=300.0, max_s=12.0, silence_s=1.3):
    """Record until the speaker stops talking. `ambient` = room background level measured while waiting
    for the wake word (so talking straight after the beep still counts as speech). Returns WAV bytes."""
    frames, noise, spoke, quiet, heard_s = [], max(ambient, 150.0), False, 0.0, 0.0
    while heard_s < max_s:
        data = proc.stdout.read(CHUNK * 2)
        if not data:
            break
        frames.append(data)
        heard_s += len(data) / 2 / RATE
        level = rms(data)
        loud = level > noise * 2.2
        if loud:
            spoke, quiet = True, 0.0
        else:
            quiet += CHUNK / RATE
            noise = max(150.0, noise * 0.95 + level * 0.05)
        if spoke and quiet >= silence_s:
            break
        if not spoke and heard_s > 5:  # said nothing after the wake word
            return None
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(b"".join(frames))
    return buf.getvalue()


def local_transcript(model, wav: bytes) -> str:
    """Free offline speech-to-text with the same Vosk model (used when the bot has no OpenAI key)."""
    from vosk import KaldiRecognizer
    with wave.open(io.BytesIO(wav)) as w:
        frames = w.readframes(w.getnframes())
    rec = KaldiRecognizer(model, RATE)
    for i in range(0, len(frames), CHUNK * 2):
        rec.AcceptWaveform(frames[i:i + CHUNK * 2])
    return json.loads(rec.FinalResult()).get("text", "")


def send(base, key, wav, text=""):
    q = urllib.parse.urlencode({"key": key, "text": text})
    req = urllib.request.Request(f"{base}/office/listen?{q}", data=wav, method="POST",
                                 headers={"Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def state(base, key, st, text="", heard=""):
    """Tell the screen what Ultra is doing (shows the listening / thinking graphic)."""
    try:
        q = urllib.parse.urlencode({"key": key, "state": st, "text": text[:500], "heard": heard[:300]})
        urllib.request.urlopen(urllib.request.Request(f"{base}/office/state?{q}", data=b"", method="POST"), timeout=4).read()
    except Exception:
        pass


def main():
    import urllib.parse  # noqa: F401  (used in send)
    from vosk import KaldiRecognizer, Model, SetLogLevel
    SetLogLevel(-1)
    base, key, device = conf()
    make_beep()
    model = Model(MODEL_DIR)
    grammar = json.dumps(WAKE + ["[unk]"])
    rec = KaldiRecognizer(model, RATE, grammar)
    proc = mic_stream(device)
    log("Listening for 'Hey Ultra' on", device)
    ambient = 300.0
    while True:
        data = proc.stdout.read(CHUNK * 2)
        if not data:
            log("Mic stopped - restarting"); time.sleep(2); proc = mic_stream(device); continue
        if os.path.exists(SPEAKING):
            rec.Reset(); continue  # ignore our own voice
        lvl = rms(data)
        if lvl < ambient * 2:  # quiet-ish chunk: update the background level
            ambient = max(80.0, ambient * 0.97 + lvl * 0.03)
        heard = ""
        if rec.AcceptWaveform(data):
            heard = json.loads(rec.Result()).get("text", "")
        else:
            heard = json.loads(rec.PartialResult()).get("partial", "")
        if any(w in heard for w in WAKE):
            rec.Reset()
            log("Wake word heard")
            state(base, key, "listening")
            speaker.duck(True)  # music down so it can hear you
            beep()
            wav = record_command(proc, ambient)
            if not wav:
                speaker.duck(False)
                state(base, key, "idle")
                continue
            try:
                text = local_transcript(model, wav)
                log("Heard (local):", text)
                state(base, key, "thinking", heard=text)
                d = send(base, key, wav, text)
                log("Heard:", d.get("heard"), "| Reply:", d.get("reply"))
                reply = d.get("reply") or "Sorry, I didn't catch that."
                state(base, key, "speaking", reply, d.get("heard") or text)
                speaker.say(reply)
            except Exception as e:
                log("Send failed:", e)
                state(base, key, "speaking", "Sorry, I couldn't reach the server.")
                speaker.say("Sorry, I couldn't reach the server.")
            state(base, key, "idle")
            # drop audio captured while we were talking
            proc.terminate(); proc = mic_stream(device); rec.Reset()


if __name__ == "__main__":
    main()
