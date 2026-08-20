#!/usr/bin/env python3
"""Tune FRED's wake word from what he actually heard, not from what we guessed.

WAKE_WORDS in inmoov/listener.py is currently a hand-made list. It was made by
somebody reading logs/heard.jsonl and running an edit-distance script over it by
hand; every entry in it ("alfred" is "hey Fred" run into one token, "bread" is
what a general lexicon reaches for when a three-phoneme name competes with the
whole English vocabulary) came out of that session. This tool is that session,
written down, so the next person does not have to redo it from memory.

It does four things, in this order:

  1. **Vocabulary.** A word the model cannot emit is dead weight. "fread" was
     dropped for exactly this reason — it is not in the lexicon, so no decoder
     can ever produce it, and listing it only makes Vosk log a warning for every
     grammar it builds. Checked against the model, not assumed.
  2. **Candidate additions.** Tokens *observed in the log* that are phonetically
     near his name and are not in WAKE_WORDS yet. Ranked, with the evidence.
  3. **Candidate removals.** Words in WAKE_WORDS that are ordinary English and
     so will be said by a room that is not talking to him.
  4. **False-wake risk.** Utterances that reached the brain but do not read as
     addressed to him.

and then prints a suggested tuple, with a line of evidence per change. It never
edits listener.py: the decision is a person's, and the evidence is what this
tool owes them.

WHAT THIS LOG CAN AND CANNOT TELL YOU
-------------------------------------
Read this before believing any number below. logs/heard.jsonl is not a
transcript of the room; it is deliberately a transcript of what got *through*
(see inmoov/heardlog.py), and it records the command **after** _strip_wake() has
cut the name off. Three consequences, and an earlier analysis got the first one
backwards:

* **A row with no name in it proves nothing.** Listener._dispatch has two ways
  in. Inside an open window (``armed``, or ``now < _armed_until`` — the six
  seconds after a bare "Fred", the nine after a question of his, or a barge-in)
  the text is passed through **whole and unstripped**. Outside one, _strip_wake
  removes everything up to and including the first wake word. So a nameless row
  is either "the name was recognised and cut off" or "the window was open and
  no name was needed", and nothing in the file distinguishes them.
* **A row that still has a name in it was *not* gated by that name.** It went
  through the window path, where the text is never stripped (or it carried the
  name twice and you are looking at the second). Those rows are this file's
  most valuable content — they are the only place the name appears in its
  mangled form — but they exist *because the window was open*, not because the
  wake gate liked them.
* **A false wake that succeeded is invisible as such.** "my friend told me"
  wakes him today; it lands in this log as "told me", with no trace of the word
  that let it in. So this tool can count the false wakes whose *text* still
  looks unaddressed, and it can count nothing at all about the ones whose
  remainder happens to read like a command. It has no denominator either: the
  chatter the gate correctly ignored is never written down, by design.

The short version: this file is evidence for **adding** words (it shows what
the recogniser really emits when someone says his name) and much weaker
evidence for **removing** them (the cost of a bad wake word is mostly not
recorded here). Removals below lean on the word being ordinary English, which
is an argument from the language rather than from the log, and it is labelled
as such.

    python3 tools/wake_audit.py                     # the whole audit
    python3 tools/wake_audit.py --check bride       # one candidate, in detail
    python3 tools/wake_audit.py --top 20 --examples 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Imported rather than copied — including the private one. Two lists of his name
# that can drift apart is precisely the failure this tool exists to end, and
# _strip_wake is the actual gate, so asking it "would this have woken him?" is
# the only answer worth printing.
from inmoov.listener import WAKE_WORDS, _strip_wake      # noqa: E402

NAME = "fred"
DEFAULT_LOG = ROOT / "logs" / "heard.jsonl"
# The model the robot actually runs (voice.asr_model in inmoov/settings.py), not
# listener.py's fallback default — barge-in needs the dynamic graph, so this is
# the lexicon a candidate has to exist in to be worth anything.
DEFAULT_MODEL = ROOT / "models" / "vosk-model-en-us-0.22-lgraph"

# ---------------------------------------------------------------------------
# Phonetics
#
# Spelling distance alone is the wrong tool here and it is worth saying why:
# "right" is four edits from "fred" on paper and one sound away from it in a
# hall, and "right stop" appears in this very log where somebody plainly said
# "Fred, stop". So each token is first reduced to a coarse *sound* skeleton —
# consonants collapsed into the classes English confuses under noise (b/p, f/v,
# d/t, k/g), every vowel run flattened to a single V, silent graphemes handled
# by the digraph rules below — and the distance is measured on that.
#
# The rules are longest-match-first and their output is already class letters,
# so they are not re-processed. "igh" is here because "right"/"bright" are the
# two commonest mishearings in the log and neither survives a naive letter map.
_RULES = [("ough", "V"), ("augh", "V"), ("eigh", "V"), ("igh", "V"),
          ("tch", "T"), ("sch", "SK"), ("ph", "F"), ("th", "T"), ("ch", "K"),
          ("sh", "S"), ("ck", "K"), ("gh", ""), ("kn", "N"), ("wr", "R"),
          ("qu", "KW"), ("wh", ""), ("ng", "N")]
_CLASS = {"b": "P", "p": "P", "f": "F", "v": "F", "d": "T", "t": "T",
          "s": "S", "z": "S", "c": "K", "k": "K", "g": "K", "q": "K",
          "x": "KS", "m": "N", "n": "N", "l": "L", "r": "R", "j": "J",
          "a": "V", "e": "V", "i": "V", "o": "V", "u": "V", "y": "V",
          "h": "", "w": ""}


def phone_key(word: str) -> str:
    """A coarse sound skeleton. "fred", "fraud", "frayed" and "fried" all give
    FRVT; "bread" and "bright" give PRVT; "friend" gives FRVNT."""
    w = "".join(ch for ch in word.lower() if ch.isalpha())
    out, i = [], 0
    while i < len(w):
        for src, dst in _RULES:
            if w.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(_CLASS.get(w[i], ""))
            i += 1
    return re.sub(r"(.)\1+", r"\1", "".join(out))


def sub_distance(target: str, key: str) -> int:
    """Edit distance from ``target`` to the *best matching stretch* of ``key``.

    Free at both ends, so "alfred" (VLFRVT) scores 0 against FRVT rather than 2
    — which is the point: the recogniser really does return the name welded to
    the word in front of it. The cost of that freedom is that long words match
    cheaply too ("refrigerator" contains FRVK), so callers also bound the length
    difference; see ``KEY_SLACK``.
    """
    prev = [0] * (len(key) + 1)             # free start: any prefix is skippable
    for i, tc in enumerate(target, 1):
        cur = [i] + [0] * len(key)
        for j, kc in enumerate(key, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (tc != kc))
        prev = cur
    return min(prev)                        # free end: any suffix is skippable


def edit(a: str, b: str) -> int:
    """Plain Levenshtein, on the spelling. Only ever shown, never decided on."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


NAME_KEY = phone_key(NAME)                  # "FRVT"
KEY_SLACK = 3        # how much longer than FRVT a candidate's skeleton may be.
                     # "alfred" is +2 and must survive; "refrigerator" is +7 and
                     # must not.
# How far from FRVT a token may be and still be worth a human's attention.
#
# One, and that is not caution — two is measured to be useless. FRVT is four
# classes long, so a distance of two is half the word, and running this log at
# that threshold surfaced "out" (VT), "not" (NVT), "care" (KVRV), "really"
# (RVLV), "robot" (RVPVT) and "russia" (RVSV), every one of them scoring 2 and
# none of them anything to do with his name. At one, everything that survives is
# something a person could plausibly have said while meaning "Fred": right,
# bride, bright, fried, front, fresh, bread.
MAX_DIST = 1
MIN_KEY = len(NAME_KEY) - 1     # and it has to have most of the sounds, so a
                                # two-class token like "out" cannot creep in

# ---------------------------------------------------------------------------
# Is a word ordinary English?
#
# This is the removal argument, and there is no word-frequency list on this
# machine to make it with (no /usr/share/dict, no hunspell, no nltk). What there
# *is* is the model's own 368k-word lexicon, so the proxy is morphological: how
# many derived forms of the word does English have? Ordinary nouns, verbs and
# adjectives are productive — friend gives friendly, friendless, friending;
# bread gives breaded, bready, breadless — and names mostly are not: alfred
# gives nothing, frayed gives nothing.
#
# Plurals and possessives are excluded on purpose. Every name takes them
# ("fred's", "ryans"), so counting them would call every name ordinary.
#
# Known limits, measured on the six live wake words rather than assumed:
#   * It measures *productivity*, not *frequency*, and frequency is what
#     actually causes false wakes. "fraud" is plainly an ordinary word and
#     scores 1 (only "fraudful"); "frank" is plainly a name and scores 6.
#   * So the number is printed, not obeyed. Use --corpus to hand it a real
#     frequency list if you have one, in which case that wins.
_INFLECTIONS = ("s", "es", "'s")            # deliberately not counted; see above
_DERIVATIONS = ("ed", "d", "ing", "ly", "er", "est", "ness", "ful", "less",
                "y", "ish", "able", "ation")
ORDINARY_AT = 3      # derived forms at or above which we call a word ordinary


class Vocab:
    """The model's lexicon, and whether a word is in it.

    Prefers ``graph/words.txt``, which the -lgraph models ship and which costs
    one file read. The small model has no such file, so the fallback is to build
    a grammar out of the words and read Vosk's own complaint — the same code
    path _new_barge_rec uses, and the same warning that condemned "fread":

        WARNING (VoskAPI:UpdateGrammarFst()) Ignoring word missing in
        vocabulary: 'fread'

    That warning is printed by the C library to fd 2, so it is captured at the
    file-descriptor level rather than with contextlib.redirect_stderr.
    """

    def __init__(self, model_dir: Path):
        self.dir = Path(model_dir)
        self.words: set[str] | None = None
        self.source = "not checked"
        listing = self.dir / "graph" / "words.txt"
        if listing.is_file():
            self.words = {line.split(" ", 1)[0] for line in
                          listing.read_text(errors="replace").splitlines() if line}
            self.source = f"{listing.relative_to(ROOT)} ({len(self.words):,} words)"

    def available(self) -> bool:
        return self.words is not None or self.dir.is_dir()

    def has(self, word: str) -> bool | None:
        """True/False, or None when we could not tell."""
        if self.words is not None:
            return word.lower() in self.words
        return self.probe([word]).get(word.lower())

    def probe(self, words: list[str]) -> dict[str, bool | None]:
        """Ask Vosk itself, by building a grammar and reading its warnings.

        Loads the model (seconds, and a GB for the big one), so this only runs
        when there is no words.txt to read.
        """
        wanted = [w.lower() for w in words]
        if not self.dir.is_dir():
            return {w: None for w in wanted}
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except Exception as exc:                          # noqa: BLE001
            print(f"  (vosk is not importable here: {exc})")
            return {w: None for w in wanted}
        saved = os.dup(2)
        with tempfile.TemporaryFile(mode="w+") as sink:
            try:
                os.dup2(sink.fileno(), 2)
                SetLogLevel(0)                            # we want the warnings
                model = Model(str(self.dir))
                KaldiRecognizer(model, 16000, json.dumps(wanted + ["[unk]"]))
            except Exception as exc:                      # noqa: BLE001
                os.dup2(saved, 2)
                print(f"  (could not build a grammar from this model: {exc})")
                return {w: None for w in wanted}
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            sink.seek(0)
            noise = sink.read()
        missing = set(re.findall(
            r"Ignoring word missing in vocabulary: '([^']*)'", noise))
        return {w: w not in missing for w in wanted}

    def family(self, word: str) -> list[str]:
        """Derived forms of ``word`` that exist in the lexicon. See ORDINARY_AT."""
        if self.words is None:
            return []
        w = word.lower()
        return [w + s for s in _DERIVATIONS if w + s in self.words]


# ---------------------------------------------------------------------------
# Does an utterance read as addressed to him?
#
# Crude on purpose. The job is not to be right about every sentence, it is to
# sort sixty-odd rows into "obviously a command", "obviously the room" and "you
# had better read this one yourself", so that a candidate word can be scored by
# the company it keeps. Every borderline row lands in the middle bucket and gets
# printed rather than counted.
_QUESTION = {"what", "what's", "whats", "who", "who's", "where", "when", "why",
             "how", "how's", "which", "whose"}
_IMPERATIVE = {"stop", "look", "turn", "tell", "say", "count", "move", "go",
               "wave", "open", "close", "play", "show", "come", "wake", "sleep",
               "drive", "point", "smile", "nod", "shake", "raise", "lower",
               "give", "find", "read", "start", "reset", "listen", "repeat",
               "sing", "dance", "follow", "forget", "remember", "explain"}
# Sentence heads that mean somebody is talking *to* something.
_HEADS = _QUESTION | _IMPERATIVE | {"can", "could", "would", "will", "do",
                                    "does", "are", "is", "please", "hey", "ok",
                                    "okay", "hi", "hello", "good", "morning",
                                    "afternoon", "evening", "thanks", "thank"}
# Dropped from the front before looking for a head. His name lands in the same
# slot as these, and the log is full of "the fred stop" and "the bread are you
# on drugs" — the recogniser puts a spurious "the" in front of the name often
# enough that not skipping it mislabels the clearest evidence in the file as
# room noise. "hey"/"ok"/"hi" are NOT here: those are address markers and count.
_FILLER = {"the", "a", "an", "uh", "um", "er", "oh", "ah", "yeah", "well",
           "so", "and", "but", "like", "just"}
_SECOND = {"you", "you're", "your", "yours", "yourself", "youre"}
# Pronouns that point at somebody who is not in the conversation. "i"/"me"/"my"
# are absent deliberately — "tell me a fact" is addressed to him.
_THIRD = {"he", "she", "they", "him", "her", "them", "his", "their", "theirs",
          "nobody", "somebody", "everyone", "anybody"}
LONG_UTTERANCE = 9    # tokens; past this, with no head and no "you", it is the
                      # room talking. Median command in this log is four.
# At or below this, an utterance is called unclear rather than unaddressed, no
# matter how little it looks like a command. "today", "one person", "no no" all
# appear in this log and all three are almost certainly somebody *answering* a
# question FRED had just asked, inside the nine-second follow-up window — which
# is the system working, not a false wake. A three-word answer and a three-word
# fragment of a television are the same string; only the window tells them
# apart, and the window is not in this file.
SHORT_UTTERANCE = 3


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9']+", (text or "").lower()) if t]


def shape(row: dict) -> tuple[str, str]:
    """('addressed' | 'unclear' | 'not addressed', why)."""
    toks = tokens(row.get("heard", ""))
    if not toks:
        return "unclear", "empty"
    if row.get("route") == "matched":
        # The command matcher recognised a whole pattern. Room noise does not do
        # that by accident, so this is the one certain 'yes' available.
        return "addressed", f"the matcher took it as {row.get('action') or 'a command'}"
    # The head is looked for in the first two *content* tokens rather than the
    # first token, because his name (or a mishearing of it) is what usually
    # occupies position one — "fred stop" and "the bride stop" are commands.
    lead = [t for t in toks[:4] if t not in _FILLER and t not in WAKE_WORDS][:2]
    head = bool(set(lead) & _HEADS)
    second = bool(_SECOND & set(toks))
    third = bool(_THIRD & set(toks))
    if head and not third:
        return "addressed", "starts like a command or a question"
    if second and len(toks) <= LONG_UTTERANCE and not third:
        return "addressed", "short, and says 'you'"
    if len(toks) <= SHORT_UTTERANCE and not third:
        return "unclear", "too short to tell — reads as an answer inside an open window"
    if len(toks) >= LONG_UTTERANCE and not head and not second:
        return "not addressed", f"{len(toks)} words, no command head, never says 'you'"
    if third and not second:
        return "not addressed", "talks about someone who isn't in the room"
    if not head and not second:
        return "not addressed", "no command head, never says 'you'"
    return "unclear", "reads either way"


def looks_like_a_name_slot(toks: list[str], i: int) -> bool:
    """Is token ``i`` sitting where his name sits — with a command after it?

    This is the strongest single signal in the file. "the bride stop", "right
    stop", "mainly fried can you hear me", "the bread are you on drugs": in
    every one, a token one sound from his name is immediately followed by
    something plainly aimed at him. A word that turns up in that slot is a
    mishearing of the name. A word that turns up in "the bright day" is not.
    """
    tail = toks[i + 1:]
    return bool(tail) and tail[0] in _HEADS


# ---------------------------------------------------------------------------
def load(path: Path, include_text: bool) -> tuple[list[dict], int, int]:
    rows, voice, text = [], 0, 0
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError as exc:
        print(f"cannot read {path}: {exc}")
        return [], 0, 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("source") == "voice":
            voice += 1
        else:
            text += 1
            if not include_text:
                continue
        rows.append(row)
    return rows, voice, text


def uniq(pairs: list[tuple[dict, int]]) -> list[tuple[dict, int]]:
    """One entry per utterance. "the bread bread oh about the" is one piece of
    evidence, not two, and quoting it twice would make it look like two."""
    seen, out = set(), []
    for row, i in pairs:
        mark = (row.get("t"), row.get("heard"))
        if mark in seen:
            continue
        seen.add(mark)
        out.append((row, i))
    return out


def wrap(text: str, width: int = 66) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# ---------------------------------------------------------------------------
def check_one(word: str, vocab: Vocab) -> int:
    """--check: everything known about one candidate, on its own."""
    w = word.lower().strip()
    key = phone_key(w)
    dist = sub_distance(NAME_KEY, key)
    print(f"\n{w!r}\n")
    print(f"  {'sound skeleton':24} {key}   ({NAME!r} is {NAME_KEY})")
    print(f"  {'distance to the name':24} {dist}   "
          f"(spelling distance {edit(w, NAME)})")
    slack = len(key) - len(NAME_KEY)
    if slack > KEY_SLACK:
        print(f"  {' ':24} ...but {slack} sound classes longer than the name, so"
              f" a match\n  {' ':24} this cheap is coincidence, not a mishearing")
    print(f"  {'already in WAKE_WORDS':24} {'yes' if w in WAKE_WORDS else 'no'}")

    present = vocab.has(w)
    if present is None:
        print(f"  {'in the vocabulary':24} UNKNOWN — no words.txt, and vosk "
              f"could not answer")
    elif present:
        print(f"  {'in the vocabulary':24} yes ({vocab.dir.name})")
    else:
        print(f"  {'in the vocabulary':24} NO ({vocab.dir.name})")
        print("      Dead weight. The decoder can never emit it, so it can never")
        print("      match — and every grammar built from WAKE_WORDS will log a")
        print("      warning about it. This is why 'fread' was removed.")

    fam = vocab.family(w)
    if vocab.words is None:
        print(f"  {'ordinary English?':24} cannot say — that test counts derived "
              f"forms in\n  {' ':24} graph/words.txt, which this model does not "
              f"ship")
        return 0
    verdict = "ORDINARY" if len(fam) >= ORDINARY_AT else "not obviously ordinary"
    print(f"  {'ordinary English?':24} {verdict} "
          f"({len(fam)} derived form{'' if len(fam) == 1 else 's'})")
    if fam:
        print(f"  {' ':24} {', '.join(fam)}")
    if len(fam) >= ORDINARY_AT:
        print(f"  {' ':24} A room will say this word without meaning him.")
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--model", default=str(DEFAULT_MODEL),
                    help="model directory, for the vocabulary check")
    ap.add_argument("--check", metavar="WORD",
                    help="report on one candidate word and stop")
    ap.add_argument("--top", type=int, default=12, help="candidates to list")
    ap.add_argument("--examples", type=int, default=3,
                    help="utterances quoted per candidate")
    ap.add_argument("--include-text", action="store_true",
                    help="also mine what was typed at the panel. Off by default: "
                         "typed text says nothing about the microphone")
    ap.add_argument("--corpus", metavar="FILE",
                    help="a word list of ordinary English, one word per line. "
                         "Overrides the morphology proxy, which is a proxy")
    args = ap.parse_args()

    vocab = Vocab(Path(args.model))
    if args.check:
        return check_one(args.check, vocab)

    common: set[str] | None = None
    if args.corpus:
        try:
            words = Path(args.corpus).read_text(errors="replace").split()
            common = {w.strip().lower() for w in words if w.strip()}
        except OSError as exc:
            print(f"cannot read {args.corpus}: {exc}")
            return 1

    def is_ordinary(word: str) -> tuple[bool, str]:
        if common is not None:
            hit = word.lower() in common
            return hit, f"{'in' if hit else 'not in'} {Path(args.corpus).name}"
        fam = vocab.family(word)
        plural = "" if len(fam) == 1 else "s"
        return len(fam) >= ORDINARY_AT, f"{len(fam)} derived form{plural}" + (
            f": {', '.join(fam[:4])}" if fam else "")

    rows, n_voice, n_text = load(Path(args.log), args.include_text)
    if not rows:
        print("no usable rows — nothing to audit")
        return 1

    print("FRED wake-word audit")
    print(f"  log         {args.log}  ({n_voice} voice, {n_text} typed"
          f"{'; typed included' if args.include_text else '; typed excluded'})")
    named = vocab.source if vocab.words is not None else Path(args.model).name
    print(f"  model       {named}")
    print(f"  WAKE_WORDS  {' '.join(WAKE_WORDS)}")
    if n_voice < 100:
        print(f"\n  Only {n_voice} voice utterances have ever been logged. That is "
              f"enough to\n  notice a pattern and nowhere near enough to trust a "
              f"rate. Read the\n  quoted evidence below; do not read the counts as "
              f"statistics.")

    # -- 0. the asymmetry, restated with this file's own numbers --------------
    residual, stripped_or_windowed = [], 0
    for row in rows:
        # The gate itself, not a re-implementation of it: if _strip_wake finds a
        # name in the text that was *already logged*, that text cannot have been
        # through _strip_wake on its way here.
        if _strip_wake(row.get("heard", "")) is not None:
            residual.append(row)
        else:
            stripped_or_windowed += 1
    rule("0. What these rows are")
    print(f"  {len(residual)} rows still contain a wake word. Those did NOT come "
          f"through the\n    wake gate: _dispatch passes text unstripped only "
          f"inside an open window\n    (a bare \"Fred\", a question of his, a "
          f"barge-in). They are the log's best\n    evidence about the name — and "
          f"they exist because the window was open.")
    print(f"  {stripped_or_windowed} rows contain no wake word. That is not "
          f"evidence of anything: the\n    name may have been recognised and cut "
          f"off by _strip_wake, or the window\n    may have been open and no name "
          f"said at all. The file cannot tell you\n    which, so nothing below "
          f"counts a nameless row as a miss.")
    print("  0 rows exist for speech the gate correctly ignored — heardlog"
          "\n    only writes what got through. There is no denominator here, so no "
          "false-wake\n    *rate* can be computed from this file, only false "
          "wakes that happened.")

    # -- 1. vocabulary --------------------------------------------------------
    rule("1. Can the model even emit the current wake words?")
    if vocab.words is None and not vocab.dir.is_dir():
        print(f"  {args.model} is not here — skipped.")
        vocab_ok = {w: None for w in WAKE_WORDS}
    else:
        vocab_ok = (vocab.probe(list(WAKE_WORDS)) if vocab.words is None
                    else {w: vocab.has(w) for w in WAKE_WORDS})
        for w in WAKE_WORDS:
            state = vocab_ok[w]
            mark = {True: "in the lexicon", False: "MISSING — dead weight",
                    None: "unknown"}[state]
            print(f"  {w:10} {mark}")
        if all(vocab_ok.get(w) for w in WAKE_WORDS):
            print("  All six can be produced by this model, so none of them is "
                  "costing\n  a grammar warning. (This is the check that removed "
                  "'fread'.)")

    # -- 2. candidate additions ----------------------------------------------
    counts: Counter[str] = Counter()
    where: dict[str, list[tuple[dict, int]]] = {}
    for row in rows:
        for i, tok in enumerate(tokens(row.get("heard", ""))):
            counts[tok] += 1
            where.setdefault(tok, []).append((row, i))

    cands = []
    for tok, n in counts.items():
        if tok in WAKE_WORDS or len(tok) < 3:
            continue
        key = phone_key(tok)
        dist = sub_distance(NAME_KEY, key)
        if (dist > MAX_DIST or len(key) < MIN_KEY
                or len(key) - len(NAME_KEY) > KEY_SLACK):
            continue
        slots = sum(1 for row, i in where[tok]
                    if looks_like_a_name_slot(tokens(row.get("heard", "")), i))
        ordinary, why_ordinary = is_ordinary(tok)
        cands.append({"word": tok, "n": n, "dist": dist, "key": key,
                      "slots": slots, "ordinary": ordinary, "why": why_ordinary,
                      "in_vocab": vocab.has(tok)})
    # A word in the name's slot beats a word that is merely close: the first is
    # somebody addressing him, the second is a coincidence of the lexicon.
    cands.sort(key=lambda c: (-c["slots"], c["dist"], -c["n"], c["word"]))

    rule("2. Candidate additions — tokens near his name, seen in this log")
    if not cands:
        print("  nothing within two sounds of the name that isn't already in the list")
    else:
        print(f"  {'word':12} {'seen':>4} {'d':>2} {'name-slot':>9}  ordinary?")
        for c in cands[:args.top]:
            flag = "ORDINARY" if c["ordinary"] else "no"
            print(f"  {c['word']:12} {c['n']:4} {c['dist']:2} {c['slots']:9}  "
                  f"{flag} ({c['why']})")
        print("\n  name-slot = times the token was immediately followed by "
              "something aimed\n  at him (\"stop\", \"can you...\", \"are "
              "you...\"). That is what a mangled name\n  looks like; a high count "
              "here is the reason to add a word, and an\n  ORDINARY tag is the "
              "reason not to, however well it scores.")
        for c in cands[:args.top]:
            if not c["slots"]:
                continue
            print(f"\n  {c['word']}  ({c['key']}, {c['dist']} from {NAME_KEY})")
            shown = 0
            for row, i in uniq(where[c["word"]]):
                toks = tokens(row.get("heard", ""))
                if not looks_like_a_name_slot(toks, i):
                    continue
                print(f"      {row.get('t', '?')[-8:]}  {wrap(row.get('heard', ''))!r}")
                shown += 1
                if shown >= args.examples:
                    break

    # -- 3. candidate removals ------------------------------------------------
    rule("3. Current wake words — which of these will a crowd say?")
    removals = []
    for w in WAKE_WORDS:
        ordinary, why = is_ordinary(w)
        seen = counts.get(w, 0)
        slots = sum(1 for row, i in where.get(w, [])
                    if looks_like_a_name_slot(tokens(row.get("heard", "")), i))
        chatter = sum(1 for row, i in uniq(where.get(w, []))
                      if shape(row)[0] == "not addressed")
        print(f"\n  {w}")
        print(f"      ordinary English?  {'YES' if ordinary else 'no'}  ({why})")
        print(f"      seen in this log   {seen}"
              + (f"  ({slots} in the name's slot, {chatter} in unaddressed speech)"
                 if seen else "  — and see section 0: absence means nothing here,"
                              "\n                         because a wake word that"
                              " works gets stripped out"))
        for row, i in uniq(where.get(w, []))[:args.examples]:
            label, why_shape = shape(row)
            print(f"        {row.get('t', '?')[-8:]}  {wrap(row.get('heard', ''))!r}"
                  f"\n            -> {label} ({why_shape})")
        if ordinary and not slots:
            removals.append((w, "ordinary English, and never once observed standing "
                                "in for his name"))
        elif ordinary:
            print(f"      ! evidence both ways: it is an ordinary word AND it stood "
                  f"in for\n        his name {slots}x here. Keeping it trades false "
                  f"wakes for missed ones.")

    # -- 4. false-wake risk ---------------------------------------------------
    rule("4. What reached him that does not read as addressed to him")
    unaddressed = [r for r in rows if shape(r)[0] == "not addressed"]
    unclear = [r for r in rows if shape(r)[0] == "unclear"]
    total = len(rows)
    print(f"  {len(unaddressed)} of {total} rows read as not addressed to him, "
          f"{len(unclear)} as unclear.")
    print("  Each of these cost a reply, a spoken answer and a mic-deaf pause. "
          "Which\n  gate let each one in — a wake word, or a window still open "
          "from the\n  exchange before it — is NOT recoverable from this file. "
          "Read them as a\n  cost that is real and a cause that is not yet "
          "identified.\n")
    for row in unaddressed[:args.top]:
        toks = tokens(row.get("heard", ""))
        via = [t for t in toks if t in WAKE_WORDS]
        tag = f"  [carries {', '.join(sorted(set(via)))} -> window path]" if via else ""
        print(f"  {row.get('t', '?')}  {wrap(row.get('heard', ''), 58)!r}{tag}")
    if len(unaddressed) > args.top:
        print(f"  ... and {len(unaddressed) - args.top} more")

    # -- 5. the suggestion ----------------------------------------------------
    rule("5. Suggested WAKE_WORDS")
    keep, changes = [], []
    for w in WAKE_WORDS:
        if vocab_ok.get(w) is False:
            changes.append(f"- {w:10} drop: not in this model's vocabulary, so it "
                           f"can never fire")
            continue
        gone = next((why for word, why in removals if word == w), None)
        if gone:
            changes.append(f"- {w:10} drop: {gone}")
            continue
        keep.append(w)
    added, rejected = [], []
    for c in cands:
        if not c["slots"]:
            continue
        near = ("an exact sound match for the name" if not c["dist"]
                else "one sound from the name")
        if c["in_vocab"] is False:
            rejected.append((c, "not in this model's vocabulary — it can never be "
                                "emitted"))
        elif c["ordinary"]:
            rejected.append((c, f"ordinary English ({c['why']}) — a crowd will say "
                                f"it without meaning him"))
        else:
            added.append(c["word"])
            changes.append(f"+ {c['word']:10} add: {near}, not ordinary English, and "
                           f"heard {c['slots']}x\n               immediately before "
                           f"something aimed at him")
    suggested = tuple(list(keep) + sorted(added))

    print(f"\n  WAKE_WORDS = {suggested!r}\n")
    if changes:
        for line in changes:
            print(f"  {line}")
    else:
        print("  no change — nothing in this log argues for one")
    if rejected:
        # The most important lines in the report, arguably. These are words the
        # log positively shows standing in for his name — the evidence for them
        # is the same evidence that argues for the additions above — and adding
        # them anyway would be the mistake. "right" is the case in point: it
        # tops the name-slot table and it is a word people say every minute.
        print("\n  Seen standing in for his name, and still not worth adding:")
        for c, why in rejected:
            head, _, tail = why.partition(" — ")
            print(f"  ~ {c['word']:10} {head}")
            if tail:
                print(f"  {' ':12} {tail}")
            print(f"  {' ':12} name-slot {c['slots']}x, e.g. "
                  f"{wrap(uniq(where[c['word']])[0][0].get('heard', ''), 40)!r}")
        print("    Every one of these is a real mishearing of his name and would "
              "genuinely\n    catch a command this list misses. It is not worth "
              "what a hall full of\n    people saying \"right\" and \"bright\" "
              "would cost him.")
    watch = [w for w in keep if is_ordinary(w)[0]]
    if watch:
        print(f"\n  Kept, but the argument is unresolved: {', '.join(watch)}")
        print("    Ordinary English by the test in section 3, *and* observed "
              "standing in\n    for his name in this log. Section 0 says why this "
              "file cannot settle it:\n    the wakes these words cause are "
              "recorded with the word cut off.")
    print(f"\n  Against the current {WAKE_WORDS!r}.")
    print("  This is a suggestion for a person, not a patch. Before taking a "
          "removal,\n  weigh it against section 0: this log under-reports the cost "
          "of an ordinary\n  word (a false wake gets stripped and looks like a "
          "normal command) and\n  over-reports its benefit only when it appears in "
          "the name's slot.")
    if n_voice < 100:
        print(f"\n  And with {n_voice} voice utterances behind it, the honest "
              f"summary is: the\n  additions are worth acting on because each one "
              f"is a quoted sentence you\n  can read; the removals are worth "
              f"arguing about, not merging.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
