"""Name correction after transcription (Jarvis #769).

Whisper took the proper-nouns list as an initial_prompt. Parakeet takes no
prompt, so names are fixed after transcription instead, the way Handy and
VoiceInk do it. Two steps, both deliberately conservative:

1. **Joins.** A run of 1 to 3 words whose letters, run together, spell a name
   that has internal capitals, digits or accents is replaced by that name:
   "hash directors" -> "HashDirectors", "Sum Sub" -> "SumSub". A plain name
   like "Dewald" or a word like "Telegram" is left alone, because an ordinary
   word ("matrix", "whisper") would otherwise be capitalised.
2. **Replacements.** ~/.voice-clip/replacements.txt maps something Parakeet
   keeps hearing to what was meant, one "heard => meant" per line, matched as
   whole words, ignoring case. This is how a recurring mishearing such as
   "to Wald" for "Dewald" gets fixed.

There is no fuzzy matching: a guess that rewrites a correct word is worse
than a name left as heard.
"""

import os
import re

from utils import STATE_DIR, _log

REPLACEMENTS_PATH = os.path.join(STATE_DIR, "replacements.txt")

DEFAULT_REPLACEMENTS = """# VoiceClip replacements: fix words the speech engine keeps mishearing.
# One per line: heard => meant
# Matched as whole words, ignoring case. Lines starting with # are ignored.
# Example:
# to Wald => Dewald
"""

_WORD = re.compile(r"[\w'’-]+", re.UNICODE)


def _key(s):
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE).lower()


def _is_distinctive(name):
    """True for names no ordinary word spells: CamelCase, acronyms, digits,
    accents. Only these are safe to restore from a split or lower-case run."""
    body = name[1:]
    return (
        any(c.isupper() for c in body)
        or any(c.isdigit() for c in name)
        or any(ord(c) > 127 for c in name)
    )


def fix_joins(text, names):
    """Replace 1-3 word runs that spell a distinctive name, run together."""
    targets = {}
    for n in names:
        n = n.strip()
        if n and _is_distinctive(n):
            targets[_key(n)] = n
    if not targets or not text:
        return text
    words = list(_WORD.finditer(text))
    out, last, i = [], 0, 0
    while i < len(words):
        hit = None
        for span in (3, 2, 1):
            if i + span > len(words):
                continue
            run = words[i:i + span]
            # only words separated by spaces or hyphens, never across a full stop
            gap = text[run[0].start():run[-1].end()]
            if span > 1 and re.search(r"[^\w\s'’-]", gap):
                continue
            name = targets.get(_key(gap))
            if name and gap != name:
                hit = (run[0].start(), run[-1].end(), name, span)
                break
            if name:  # already right
                break
        if hit:
            start, end, name, span = hit
            out.append(text[last:start])
            out.append(name)
            last = end
            i += span
        else:
            i += 1
    out.append(text[last:])
    return "".join(out)


def load_replacements(path=REPLACEMENTS_PATH):
    """Read "heard => meant" pairs; create the file with instructions if missing."""
    try:
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(DEFAULT_REPLACEMENTS)
        pairs = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=>" not in line:
                    continue
                heard, meant = (p.strip() for p in line.split("=>", 1))
                if heard:
                    pairs.append((heard, meant))
        return pairs
    except Exception as e:
        _log(f"replacements: could not read {path} ({e})", tag="names")
        return []


def apply_replacements(text, pairs):
    for heard, meant in pairs:
        pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, heard.split())) + r"(?!\w)"
        text = re.sub(pattern, lambda _m, m=meant: m, text, flags=re.IGNORECASE)
    return text


def correct_names(text, names, pairs):
    return apply_replacements(fix_joins(text, names), pairs)
