#!/usr/bin/env python3
"""
human_typer.py - types text into whatever window has focus, like a person would.

Features: variable speed (WPM-based), punctuation/paragraph pauses, random
"thinking" pauses, realistic typos fixed with backspace (immediately or a few
letters later), hesitation revisions (type a few words, delete, retype), and
optional AI revisions (types an alternate sentence opening, deletes it, then
types the real sentence).

Controls while running:
    F8  = pause / resume (2-second grace period after resuming to click back in)
    Esc = stop immediately

Install:
    pip install pynput
    pip install anthropic          # only needed for --ai
    export ANTHROPIC_API_KEY=...   # only needed for --ai

Examples:
    python human_typer.py --file essay.txt
    python human_typer.py --file notes.txt --wpm 70 --typo-rate 0.02
    python human_typer.py --file draft.txt --ai --ai-rate 0.25



    
Rollover is off by default. 
Turn it on by adding --rollover, for example python human_typer.py --file essay.txt --wpm 110 --rollover.

How it works:

Keys are released on a schedule. Each key is pressed, 
and its release is set for after its dwell time instead of happening right away.
The wait() function now does those releases on time. 
That’s what lets the next key go down while the previous one is still held, the way fast typists actually type.
Without --rollover, each key is fully released before the next press, just like the previous version. 
If you set --wpm above 90 without it, the script prints a tip.
With --rollover, the next key can be pressed before the last one is released, so you actually reach your --wpm. 
At most two keys are down when a new one is pressed.
Some keys never overlap. Capital letters, shifted symbols, Enter, Tab and Backspace always wait for earlier keys to come up. 
This keeps Shift from leaking onto neighbouring keys and keeps line breaks and deletions clean. The same key twice in a row (like the “ll” in “hello”) also waits, since a key can’t be pressed while it’s already down.





"""

import argparse
import json
import math
import random
import re
import sys
import threading
import time

from pynput import keyboard

# macOS fix: pynput's hotkey listener reads the keyboard layout from its own
# background thread, and newer macOS versions crash any app that does that
# (it must happen on the main thread). We read it once here, on the main
# thread at import time, and hand the listener the saved copy instead.
if sys.platform == "darwin":
    import contextlib
    try:
        import pynput._util.darwin as _pyn_util
        import pynput.keyboard._darwin as _pyn_kb

        with _pyn_util.keycode_context() as _ctx:
            _CACHED_KEYCODE_CONTEXT = _ctx

        @contextlib.contextmanager
        def _cached_keycode_context():
            yield _CACHED_KEYCODE_CONTEXT

        _pyn_kb.keycode_context = _cached_keycode_context
        _pyn_util.keycode_context = _cached_keycode_context
    except Exception as _e:  # pynput internals changed; hotkeys may crash
        print(f"[warning] macOS hotkey fix not applied: {_e}")

Key = keyboard.Key
kb = keyboard.Controller()

stop_event = threading.Event()
pause_event = threading.Event()  # set = paused


class Stopped(Exception):
    pass


# ------------------------------------------------------------ held keys ---
# Keys that are currently pressed down, each with the time it should be
# released: [release_time, key]. wait() releases them on schedule, which is
# what lets one key still be down while the next is pressed (rollover).

held_keys = []
MAX_HELD = 2  # with rollover, at most this many keys down before a new press


def release_due(force=False):
    """Release every held key whose time has come (or all of them if force)."""
    now = time.time()
    for item in held_keys[:]:
        if force or item[0] <= now:
            try:
                kb.release(item[1])
            except Exception:
                pass
            held_keys.remove(item)


def release_all():
    release_due(force=True)


def held_remaining():
    """Seconds until the last currently held key is due to be released."""
    if not held_keys:
        return 0.0
    return max(t for t, _ in held_keys) - time.time()


# ---------------------------------------------------------------- hotkeys ---

def on_press(key):
    if key == Key.esc:
        stop_event.set()
        print("\n[stopped]")
        return False
    if key == Key.f8:
        if pause_event.is_set():
            pause_event.clear()
            print("[resumed - click back into your text box]")
        else:
            pause_event.set()
            print("[paused - press F8 to resume]")


def wait(seconds):
    """Sleep that respects pause and stop, and releases held keys on time."""
    end = time.time() + seconds
    while True:
        release_due()
        if stop_event.is_set():
            release_all()
            raise Stopped
        if pause_event.is_set():
            release_all()  # never leave a key down while paused
            t0 = time.time()
            while pause_event.is_set() and not stop_event.is_set():
                time.sleep(0.05)
            if stop_event.is_set():
                raise Stopped
            time.sleep(2.0)  # grace period to refocus the window
            end += time.time() - t0
            continue
        now = time.time()
        remaining = end - now
        if remaining <= 0:
            return
        step = min(0.01, remaining)
        if held_keys:  # wake up in time for the next scheduled release
            next_release = min(t for t, _ in held_keys) - now
            step = min(step, max(0.001, next_release))
        time.sleep(step)


# ---------------------------------------------------------- keyboard data ---

NEIGHBORS = {
    "q": "wa", "w": "qeas", "e": "wrsd", "r": "etdf", "t": "ryfg",
    "y": "tugh", "u": "yihj", "i": "uojk", "o": "ipkl", "p": "ol",
    "a": "qwsz", "s": "awedxz", "d": "serfcx", "f": "drtgvc", "g": "ftyhbv",
    "h": "gyujnb", "j": "huikmn", "k": "jiolm", "l": "kop",
    "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn",
    "n": "bhjm", "m": "njk",
}

FAST_BIGRAMS = {
    "th", "he", "in", "er", "an", "re", "on", "at", "en", "nd", "ti", "es",
    "or", "te", "of", "ed", "is", "it", "al", "ar", "st", "to", "nt", "ng",
    "se", "ha", "as", "ou", "io", "le", "ve",
}

SHIFT_CHARS = set('~!@#$%^&*()_+{}|:"<>?')


# ------------------------------------------------- finger layout & timing ---
# QWERTY touch-typing finger assignment: each letter -> (hand, finger_id).
# finger_id is unique per finger across both hands, so two letters share a
# finger exactly when their finger_ids match.

_FINGER_ROWS = {
    0: "qaz",     # left pinky
    1: "wsx",     # left ring
    2: "edc",     # left middle
    3: "rfvtgb",  # left index
    4: "yhnujm",  # right index
    5: "ik",      # right middle
    6: "ol",      # right ring
    7: "p",       # right pinky
}
FINGER = {}
for _fid, _letters in _FINGER_ROWS.items():
    _hand = "L" if _fid <= 3 else "R"
    for _c in _letters:
        FINGER[_c] = (_hand, _fid)


def pair_class(a, b):
    """Classify an ordered letter pair by how fast it's typed."""
    a, b = a.lower(), b.lower()
    if a not in FINGER or b not in FINGER:
        return "other"
    (ha, fa), (hb, fb) = FINGER[a], FINGER[b]
    if ha != hb:
        return "alternate"    # hands alternate: fast
    if fa == fb and a != b:
        return "same_finger"  # one finger, two keys: slow
    return "same_hand"        # same hand, different finger: neutral


# Common English trigraphs, typed as one practiced burst (their interior gap
# gets an extra speed-up on top of the pair factor).
COMMON_TRIGRAMS = {
    "the", "and", "ing", "ion", "ent", "her", "for", "ter", "tha", "ate",
    "est", "ers", "ati", "hat", "all", "ver", "ith", "nce", "men", "con",
    "pro", "com", "tio", "igh", "ght", "our", "ure", "res", "ess", "ill",
    "ous", "ear", "ain", "ide", "ive", "oun", "sti", "ome", "rea",
}


class TypistProfile:
    """A fixed set of timing factors for one run, so a given pair or trigraph
    is always typed at the same characteristic speed instead of being randomly
    re-jittered each time. Pass a seeded Random to reproduce a profile; with
    --seed, the same profile comes back every run."""

    CLASS_MEAN = {        # center of each class's speed factor
        "alternate": 0.74,
        "same_hand": 1.00,
        "same_finger": 1.55,
        "other": 1.00,
    }
    CLASS_DRIFT = {       # how far this typist's class mean may sit from center
        "alternate": 0.06,
        "same_hand": 0.06,
        "same_finger": 0.18,
        "other": 0.0,
    }
    _LETTERS = "abcdefghijklmnopqrstuvwxyz"

    def __init__(self, rng):
        # this typist's personal mean for each class, drawn once
        self._class_mean = {
            cls: mean + rng.uniform(-self.CLASS_DRIFT[cls], self.CLASS_DRIFT[cls])
            for cls, mean in self.CLASS_MEAN.items()
        }
        # a stable factor for every letter pair: class mean x the pair's own quirk
        self._pair = {}
        for a in self._LETTERS:
            for b in self._LETTERS:
                base = self._class_mean[pair_class(a, b)]
                self._pair[a + b] = max(0.45, base * rng.uniform(0.92, 1.08))
        # a stable burst factor for each common trigraph's interior gap
        self._tri = {tri: rng.uniform(0.80, 0.92) for tri in sorted(COMMON_TRIGRAMS)}

    def pair_factor(self, a, b):
        if not a or not b:
            return 1.0
        return self._pair.get(a.lower() + b.lower(), 1.0)

    def tri_factor(self, a, b, c):
        if not (a and b and c):
            return 1.0
        return self._tri.get((a + b + c).lower(), 1.0)


# ------------------------------------------------------------------ typer ---

class Typer:
    def __init__(self, args):
        self.args = args
        self.base = 60.0 / (args.wpm * 5)  # seconds per character
        self.prev = ""
        # independent RNG so the profile is stable regardless of what else
        # (typos, --ai, pauses) draws from the global random stream
        self.profile = TypistProfile(random.Random(args.seed))
        self.drift = 0.0     # slow wander in overall speed
        self.cautious = 0    # keystrokes left to type a bit slower after an error

    # low-level keys
    def dwell(self, key):
        """How long to hold a key down (seconds)."""
        lo, hi = self.args.dwell_min / 1000, self.args.dwell_max / 1000
        d = random.triangular(lo, hi, lo + (hi - lo) * 0.4)  # skewed toward the short end
        if key == Key.space:
            d *= 1.1  # thumbs tend to linger on the spacebar
        elif isinstance(key, str) and (key.isupper() or key in SHIFT_CHARS):
            d *= 1.15  # shifted keys are held a bit longer
        return d

    def tap(self, key, overlap=False):
        """Press a key and schedule its release after the dwell time.
        Returns the dwell used. If overlap is allowed (and --rollover or
        --rollover-rate is on), the previous key may still be down when this
        one is pressed."""
        d = self.dwell(key)
        can_overlap = (
            overlap
            and (self.args.rollover or self.args.rollover_rate > 0)
            and not any(k == key for _, k in held_keys)  # can't press a key that's already down
            and len(held_keys) < MAX_HELD
        )
        if not can_overlap:
            r = held_remaining()
            if r > 0:
                wait(r)  # let earlier keys finish their hold first
        kb.press(key)
        held_keys.append([time.time() + d, key])
        return d

    def type_char(self, ch):
        if ch == "\n":
            return self.tap(Key.enter)
        if ch == "\t":
            return self.tap(Key.tab)
        if ch == " ":
            return self.tap(Key.space, overlap=True)
        # shifted characters never overlap, so Shift can't leak onto other keys
        overlap = not (ch.isupper() or ch in SHIFT_CHARS)
        return self.tap(ch, overlap=overlap)

    def backspace(self, n):
        for _ in range(n):
            d = self.tap(Key.backspace)
            wait(d + random.uniform(0.035, 0.09))

    # timing
    def key_delay(self, ch, nxt=None):
        """Press-to-press time from ch to the next key.
        This gap is the latency of the pair (ch, nxt): the digraph factor and,
        when ch is the middle of a known trigraph, a trigraph burst apply here."""
        d = random.lognormvariate(math.log(self.base), 0.4)
        if nxt is not None and ch.isalpha() and nxt.isalpha():
            d *= self.profile.pair_factor(ch, nxt)             # this pair's own speed
            d *= self.profile.tri_factor(self.prev, ch, nxt)   # burst inside a trigraph
        if ch.isupper() or ch in SHIFT_CHARS:
            d *= 1.3

        # slow wander: consecutive gaps are correlated, so there are stretches
        # of faster and slower typing lasting roughly 10 seconds each
        self.drift = 0.98 * self.drift + random.gauss(0, 0.03)
        self.drift = max(-0.35, min(0.35, self.drift))
        d *= math.exp(self.drift)

        # typing is a bit careful for a few keys after making a mistake
        if self.cautious > 0:
            d *= 1.25
            self.cautious -= 1

        return max(0.015, min(d, self.base * 5))

    def pause_after(self, ch):
        s = self.args.pause_scale
        if ch in ".!?":
            return random.uniform(0.4, 1.3) * s
        if ch in ",;:":
            return random.uniform(0.1, 0.45) * s
        if ch == "\n":
            return random.uniform(0.8, 2.5) * s
        if ch == " " and random.random() < self.args.think_rate:
            return random.uniform(0.8, 3.0) * s
        return 0.0

    def rolls_into(self, ch, nxt):
        """Should the next key go down before this one comes up, as a
        habit on this familiar letter pair (independent of speed)?"""
        return (
            self.args.rollover_rate > 0
            and nxt is not None
            and ch.isalpha() and nxt.isalpha()
            and ch.islower() and nxt.islower()  # no Shift involved
            and ch != nxt                       # can't press a key that's already down
            and pair_class(ch, nxt) != "same_finger"  # one finger can't roll
            and (ch + nxt) in FAST_BIGRAMS
            and random.random() < self.args.rollover_rate
        )

    def emit(self, ch, with_pauses=True, nxt=None):
        held = self.type_char(ch)
        interval = self.key_delay(ch, nxt)
        if self.rolls_into(ch, nxt):
            # press the next key 10-60% of the way before this one is released,
            # giving a negative release-to-press gap at any WPM
            interval = max(0.015, held * random.uniform(0.4, 0.9))
        elif not self.args.rollover:
            # no overlap: the next press can't come until this key is released
            interval = max(interval, held + 0.01)
        if with_pauses:
            interval += self.pause_after(ch)
        self.prev = ch
        wait(interval)

    def type_plain(self, s):
        for j, ch in enumerate(s):
            nxt = s[j + 1] if j + 1 < len(s) else None
            self.emit(ch, with_pauses=False, nxt=nxt)

    # behaviours
    def type_and_retract(self, s, think=(0.6, 2.0)):
        """Type s, pause as if reconsidering, delete it."""
        self.type_plain(s)
        wait(random.uniform(*think))
        self.backspace(len(s))
        wait(random.uniform(0.2, 0.8))

    def typo_weight(self, ch):
        """Awkward letter pairs cause more errors than easy ones."""
        if not self.prev or not self.prev.isalpha():
            return 1.0
        return {"alternate": 0.75, "same_hand": 1.0,
                "same_finger": 1.8, "other": 1.0}[pair_class(self.prev, ch)]

    def _lookahead(self, text, start, force=False):
        """Sometimes keep typing a few letters before noticing the mistake.
        Returns how many letters were typed."""
        if not force and random.random() >= 0.4:
            return 0
        limit = random.randint(1, 3)
        j = start
        while j - start < limit and j < len(text) and text[j].isalpha():
            j += 1
        self.type_plain(text[start:j])
        return j - start

    def typo(self, text, i):
        """Make one realistic mistake at text[i], notice it, and fix it.
        Returns how many characters starting at i are already correctly
        typed (the caller skips them); usually 0."""
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        plain = ch.islower()
        next_plain = nxt.isalpha() and nxt.islower() and nxt != ch

        kinds, weights = ["sub"], [0.50]            # wrong neighbouring key
        if plain and next_plain:
            kinds.append("swap"); weights.append(0.20)    # "teh"
        if plain:
            kinds.append("double"); weights.append(0.15)  # "heello"
        if plain and nxt.isalpha():
            kinds.append("skip"); weights.append(0.15)    # "helo"
        kind = random.choices(kinds, weights)[0]

        keep = 0
        if kind == "sub":
            wrong = random.choice(NEIGHBORS[ch.lower()])
            if ch.isupper():
                wrong = wrong.upper()
            self.emit(wrong, with_pauses=False)
            typed = 1 + self._lookahead(text, i + 1)
        elif kind == "swap":
            self.emit(nxt, with_pauses=False, nxt=ch)
            self.emit(ch, with_pauses=False)
            typed = 2 + self._lookahead(text, i + 2)
        elif kind == "double":
            self.emit(ch, with_pauses=False, nxt=ch)
            self.emit(ch, with_pauses=False)
            typed = 1 + self._lookahead(text, i + 1)   # duplicate + lookahead
            keep = 1                                   # the first ch stays
        else:  # skip: you only notice after typing the following letters
            typed = self._lookahead(text, i + 1, force=True)

        wait(random.uniform(0.15, 0.6))  # "oops" reaction
        self.backspace(typed)
        wait(random.uniform(0.05, 0.25))
        self.cautious = random.randint(3, 6)
        self.prev = text[i + keep - 1] if i + keep > 0 else ""
        return keep

    def run(self, text, alternates, on_progress=None):
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            word_start = ch not in " \n\t" and (i == 0 or text[i - 1] in " \n\t")

            if i in alternates:
                self.type_and_retract(alternates.pop(i), think=(0.8, 2.5))
            elif word_start and random.random() < self.args.revise_rate:
                k = random.randint(0, 2)
                m = re.match(r"\S+(?: \S+){0,%d}" % k, text[i:])
                if m:
                    self.type_and_retract(m.group(0))

            if (ch.isalpha() and ch.lower() in NEIGHBORS
                    and random.random() < self.args.typo_rate * self.typo_weight(ch)):
                skip = self.typo(text, i)
                if skip:  # the typo already left this character correctly typed
                    i += skip
                    if on_progress:
                        on_progress(i, n)
                    continue

            self.emit(ch, nxt=text[i + 1] if i + 1 < n else None)
            i += 1
            if on_progress:
                on_progress(i, n)

        r = held_remaining()  # let the last key finish its hold
        if r > 0:
            wait(r)


# ------------------------------------------------------- AI alternates ---

def get_ai_alternates(text, rate, model):
    """Ask Claude for plausible 'false start' openings for some sentences.
    Returns {char_index: alternate_opening}."""
    try:
        import anthropic
    except ImportError:
        sys.exit("--ai needs the anthropic package: pip install anthropic")

    starts = {0} | {m.end() for m in re.finditer(r"[.!?][\"')\]]*\s+", text)}
    starts = sorted(s for s in starts if s < len(text))
    chosen = [s for s in starts if random.random() < rate]
    if not chosen:
        return {}

    sentences = []
    for s in chosen:
        m = re.search(r"[.!?]", text[s:])
        end = s + (m.end() if m else len(text) - s)
        sentences.append(text[s:end].strip())

    prompt = (
        "For each sentence below, write a plausible alternative OPENING "
        "(3 to 8 words) that a writer might start typing before changing "
        "their mind and writing the real sentence instead. It must differ "
        "from the real sentence's opening, fit the same context and tone, "
        "and be a sentence fragment (no ending punctuation).\n\n"
        "Return ONLY a JSON array of strings, one per sentence, same order, "
        "no other text.\n\n"
        + json.dumps(sentences, ensure_ascii=False)
    )

    print(f"Asking Claude for {len(sentences)} alternate openings...")
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        alts = json.loads(raw)
    except json.JSONDecodeError:
        print("Couldn't parse AI response; continuing without AI revisions.")
        return {}

    out = {}
    for s, a in zip(chosen, alts):
        if isinstance(a, str):
            a = " ".join(a.split())  # no newlines
            if a:
                out[s] = a
    return out


# ------------------------------------------------------------------- main ---

def main():
    p = argparse.ArgumentParser(description="Type text like a human into the focused window.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="text file to type")
    src.add_argument("--text", help="text to type (inline)")
    p.add_argument("--wpm", type=float, default=55, help="average words per minute (default 55)")
    p.add_argument("--typo-rate", type=float, default=0.03, help="chance per letter of a typo (default 0.03)")
    p.add_argument("--revise-rate", type=float, default=0.015, help="chance per word of a retype hesitation (default 0.015)")
    p.add_argument("--think-rate", type=float, default=0.03, help="chance per space of a long pause (default 0.03)")
    p.add_argument("--pause-scale", type=float, default=1.0, help="multiply all pauses (default 1.0)")
    p.add_argument("--dwell-min", type=float, default=70, help="min key hold time in ms (default 70)")
    p.add_argument("--dwell-max", type=float, default=150, help="max key hold time in ms (default 150)")
    p.add_argument("--rollover", action="store_true",
                   help="allow the next key to be pressed before the previous one is released "
                        "(needed to actually reach speeds above ~100 WPM)")
    p.add_argument("--rollover-rate", type=float, default=0.0,
                   help="chance that a common letter pair (th, er, in...) overlaps at any speed, "
                        "0 to 1 (default 0 = off; try 0.3)")
    p.add_argument("--countdown", type=int, default=5, help="seconds before typing starts (default 5)")
    p.add_argument("--ai", action="store_true", help="use Claude to generate realistic false-start revisions")
    p.add_argument("--ai-rate", type=float, default=0.25, help="fraction of sentences that get an AI false start (default 0.25)")
    p.add_argument("--model", default="claude-sonnet-5-5", help="Claude model for --ai")
    p.add_argument("--seed", type=int, help="random seed for repeatable runs")
    args = p.parse_args()

    if args.dwell_min > args.dwell_max:
        p.error("--dwell-min can't be larger than --dwell-max")

    if not 0 <= args.rollover_rate <= 1:
        p.error("--rollover-rate must be between 0 and 1")

    if args.seed is not None:
        random.seed(args.seed)

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = args.text
    text = text.replace("\r\n", "\n").rstrip("\n")

    alternates = get_ai_alternates(text, args.ai_rate, args.model) if args.ai else {}

    est = len(text) / (args.wpm * 5) * 1.3 * args.pause_scale
    print(f"{len(text)} characters, roughly {est:.1f} min at {args.wpm:g} WPM.")
    if args.wpm > 90 and not args.rollover:
        print("[tip] Above ~90 WPM the key holds eat into the gap between keys, "
              "so you'll type slower than --wpm. Add --rollover to fix that.")
    print("F8 = pause/resume, Esc = stop.")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    try:
        for s in range(args.countdown, 0, -1):
            print(f"Click into your text box... starting in {s}", end="\r", flush=True)
            wait(1)
        print(" " * 50, end="\r")
        Typer(args).run(text, alternates)
        print("Done.")
    except Stopped:
        pass
    finally:
        release_all()  # make sure no key is ever left stuck down
        listener.stop()


if __name__ == "__main__":
    main()