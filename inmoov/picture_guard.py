"""What FRED will and will not paint: the audience is children.

He stands in school gyms and community halls, and the chest screen is at a
six-year-old's eye height. On 2026-09-23 "a romance novel hunk" came back as
a shirtless man — technically the picture that was asked for, and not one
for that room. This module is the two checks that stop it, and its rule is
simple: **if a picture would not be fine on a primary-school wall, he does
not paint it.**

Two checks, because each catches what the other cannot:

**Before painting, the words.** ``check_prompt`` reads the description for
nudity and revealing clothing, sexual content, blood and gore, horror and
frightening imagery, weapons, drugs and alcohol, and hate symbols, and
answers with the *kind* of thing it found, or "" when the prompt is fine. It
is a word list — whole words, plurals, a few obvious phrasings — so it is
fast, needs nothing installed, and is easy to read and extend. It cannot
catch "a handsome lifeguard", which is why there is a second check.

**After painting, the picture.** ``check_picture`` runs NudeNet (an ONNX
detector, ~30 ms a picture on these cores) over the finished image and
refuses it when it finds exposed skin where a shirt or trousers belong. It
is the only check that sees what the painter actually did with an innocent
prompt — the models these run on were trained on the whole internet, and a
"romantic novel cover" without the word "shirtless" still came back
half-undressed. NudeNet is optional (``venv/bin/pip install nudenet``; its
weights download once); without it the prompt check stands alone and the
log says so once.

Neither check is the first line. The tool's description tells the brain
(Claude, or the local model) who the audience is, so a request for a picture
that is not for this room gets a kind spoken no and an offer of something
else, and never reaches the easel. These checks are for what slips past.

``images.guard`` in config/settings.json: "family" (the default) runs both;
"off" runs neither — an admin's choice, not a spoken one.
"""
from __future__ import annotations

import re
import threading

# Not on the list, on purpose: "monster", "dragon", "skeleton", "sword",
# "battle", "haunted", "devil" (Tasmanian), "killer" (whale), "shooting"
# (star), "cocktail" (fruit), "wound" (up), "injured" (bird at the vet) —
# a children's request uses all of these, and the picture check is behind
# them. Each entry is a whole word or phrase, matched case-insensitively with
# plurals and common endings (-s, -es, -ed, -ing) allowed, so "gun" also
# catches "guns" and "stab" catches "stabbing". Hyphens in the prompt are
# read as spaces, so "bare-chested" is "bare chested". Keep the reason short
# and speakable: it is what FRED says he will not paint.
_RULES: dict[str, tuple[str, ...]] = {
    "nudity or revealing clothing": (
        "nude", "naked", "nudity", "nudist", "topless", "shirtless", "bare chested",
        "bare breasted", "bottomless", "undressed", "undressing", "unclothed",
        "bikini", "lingerie", "underwear", "panties", "thong", "bra", "g string",
        "cleavage", "breast", "boob", "nipple", "butt", "buttock", "booty", "genital",
        "penis", "vagina", "crotch", "groin",
    ),
    "sexual content": (
        "sexy", "sexual", "sex", "seductive", "seduce", "sensual", "erotic",
        "erotica", "porn", "porno", "pornographic", "xxx", "nsfw", "hentai",
        "stripper", "strip club", "pole dancer", "hooker", "prostitute",
        "orgy", "kinky", "fetish", "bondage", "bdsm", "smoldering",
        "romance novel", "romantic novel", "playboy", "pin up", "pinup", "lap dance",
        "make out", "making out", "french kiss", "horny", "aroused", "lust", "lustful",
    ),
    "blood or gore": (
        "blood", "bloody", "bleeding", "gore", "gory", "guts", "entrails", "corpse",
        "dead body", "dead bodies", "murder", "murdered", "kill", "killed", "killing",
        "slaughter", "massacre", "decapitated", "decapitation", "beheaded", "severed",
        "mutilated", "mutilation", "dismembered", "torture", "tortured", "stab",
        "stabbed", "suicide", "hanged", "hanging body", "execution", "executed",
        "wounded", "carnage", "war crime",
    ),
    "horror or frightening pictures": (
        "horror", "scary", "creepy", "terrifying", "nightmare", "nightmarish",
        "gruesome", "grotesque", "demon", "demonic", "satan", "satanic",
        "possessed", "exorcism", "zombie", "ghoul", "undead", "slasher", "serial killer",
        "jumpscare", "jump scare", "clown killer", "killer clown",
        "disturbing", "macabre", "evil spirit",
    ),
    "weapons": (
        "gun", "pistol", "rifle", "shotgun", "firearm", "handgun", "revolver",
        "machine gun", "assault rifle", "ak 47", "ak47", "ar 15", "ar15", "sniper",
        "grenade", "bomb", "explosive", "dynamite", "landmine", "knife", "knives",
        "machete", "dagger", "switchblade", "shootout", "gunfight",
        "bullet", "ammunition", "ammo",
    ),
    "drugs, smoking or alcohol": (
        "drug", "cocaine", "heroin", "meth", "methamphetamine", "marijuana",
        "cannabis", "weed smoking", "smoking weed", "bong", "joint smoking", "lsd",
        "ecstasy pill", "mdma", "opioid", "fentanyl", "syringe", "needle drug",
        "cigarette", "cigar", "vape", "vaping", "smoking", "beer", "vodka", "whiskey",
        "whisky", "tequila", "rum", "liquor", "booze", "drunk", "drunken", "hangover",
        "alcohol", "alcoholic",
    ),
    "hate symbols": (
        "nazi", "swastika", "hitler", "kkk", "ku klux klan", "klansman", "white power",
        "confederate flag", "lynching", "lynched", "isis flag", "hate symbol",
    ),
}

_ENDINGS = r"(?:s|es|ed|ing)?"


def _compile() -> list[tuple[str, re.Pattern]]:
    out = []
    for reason, words in _RULES.items():
        alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        out.append((reason, re.compile(rf"\b(?:{alts}){_ENDINGS}\b")))
    return out


_PATTERNS = _compile()


def _tidy(text: str) -> str:
    text = str(text or "").lower().replace("-", " ").replace("_", " ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


def check_prompt(prompt: str) -> str:
    """The kind of thing a prompt asks for that he will not paint, or ""."""
    text = _tidy(prompt)
    if not text:
        return ""
    for reason, pattern in _PATTERNS:
        if pattern.search(text):
            return reason
    return ""


# ---- the picture -------------------------------------------------------------
# NudeNet labels, and the confidence at which each one refuses the picture.
# Exposed genitals, breasts, buttocks: low bar, these are never fine here. A
# bare belly is a shirtless torso in most pictures, so it counts too, at a
# higher bar so a cartoon bear's tummy does not trip it. Armpits, feet, faces
# and everything "_COVERED" are not on the list: a tank top is clothing.
_PICTURE_LIMITS: dict[str, float] = {
    "FEMALE_GENITALIA_EXPOSED": 0.2, "MALE_GENITALIA_EXPOSED": 0.2,
    "ANUS_EXPOSED": 0.2, "BUTTOCKS_EXPOSED": 0.25,
    "FEMALE_BREAST_EXPOSED": 0.25, "MALE_BREAST_EXPOSED": 0.25,
    "BELLY_EXPOSED": 0.35,
}
PICTURE_REASON = "undressed people"

_detector = None
_detector_lock = threading.Lock()
_detector_error = ""


def detector_available() -> bool:
    """Whether NudeNet can be loaded (loads it the first time asked)."""
    return _detector_or_none() is not None


def _detector_or_none():
    global _detector, _detector_error
    if _detector is not None or _detector_error:
        return _detector
    with _detector_lock:
        if _detector is None and not _detector_error:
            try:
                from nudenet import NudeDetector                      # noqa: PLC0415
                _detector = NudeDetector()
            except Exception as exc:                                  # noqa: BLE001
                _detector_error = f"{type(exc).__name__}: {exc}"
    return _detector


def detector_error() -> str:
    _detector_or_none()
    return _detector_error


def check_picture(picture) -> tuple[str, list[str]]:
    """Look at a finished picture (PNG bytes, or a path). Returns (reason, seen).

    ``reason`` is "" when the picture passes, or when NudeNet is not
    installed — a missing detector is an admin's problem, reported through
    ``detector_error()``, not a reason to refuse every picture. ``what_was_seen``
    lists the labels over their limit, for the log.
    """
    det = _detector_or_none()
    if det is None:
        return "", []
    try:
        found = det.detect(picture if isinstance(picture, bytes) else str(picture))
    except Exception as exc:                                          # noqa: BLE001
        return "", [f"detector failed: {exc}"]
    seen = []
    for hit in found or []:
        label = str(hit.get("class", ""))
        score = float(hit.get("score", 0.0))
        limit = _PICTURE_LIMITS.get(label)
        if limit is not None and score >= limit:
            seen.append(f"{label}:{score:.2f}")
    return (PICTURE_REASON if seen else ""), seen
