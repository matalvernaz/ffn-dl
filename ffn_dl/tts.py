"""Text-to-speech audiobook generation with character voice mapping."""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

# edge_tts is only required when actually synthesizing audio — importing
# this module (e.g. from the exporters' FFMETADATA escape helper or from
# a unit test) should work without the `audio` optional extra installed.
# The lazy loader below is used by the two call sites that need it.
try:
    import edge_tts as _edge_tts  # noqa: F401
except ImportError:
    _edge_tts = None


def _require_edge_tts():
    global _edge_tts
    if _edge_tts is None:
        try:
            import edge_tts as _m
        except ImportError as exc:
            raise RuntimeError(
                "edge-tts is required for audiobook generation. "
                "Install with: pip install 'ffn-dl[audio]'"
            ) from exc
        _edge_tts = _m
    return _edge_tts


def _find_tool(name):
    """Find ffmpeg/ffprobe — bundled with PyInstaller or on PATH."""
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / (name + (".exe" if os.name == "nt" else ""))
        if bundled.exists():
            return str(bundled)
    return shutil.which(name) or name

from .exporters import html_to_text
from .models import Story

logger = logging.getLogger(__name__)

# ── Voice pools ───────────────────────────────────────────────────

# Narrator voice — calm, clear storytelling voice
NARRATOR_VOICE = "en-US-AriaNeural"

# Character voice pools by detected gender
MALE_VOICES = [
    "en-US-GuyNeural",
    "en-GB-RyanNeural",
    "en-US-ChristopherNeural",
    "en-AU-WilliamMultilingualNeural",
    "en-US-EricNeural",
    "en-GB-ThomasNeural",
    "en-CA-LiamNeural",
    "en-US-RogerNeural",
    "en-IE-ConnorNeural",
    "en-US-SteffanNeural",
    "en-NZ-MitchellNeural",
    "en-US-BrianNeural",
]

FEMALE_VOICES = [
    "en-US-JennyNeural",
    "en-GB-SoniaNeural",
    "en-US-EmmaNeural",
    "en-US-MichelleNeural",
    "en-AU-NatashaNeural",
    "en-GB-LibbyNeural",
    "en-CA-ClaraNeural",
    "en-US-AvaNeural",
    "en-IE-EmilyNeural",
    "en-NZ-MollyNeural",
    "en-IN-NeerjaExpressiveNeural",
    "en-GB-MaisieNeural",
]

NEUTRAL_VOICES = MALE_VOICES + FEMALE_VOICES

# Dialogue attribution verbs → SSML style (for voices that support it)
# Dialogue attribution verbs → prosody adjustments (rate, volume, pitch)
EMOTION_MAP = {
    "whispered": "whisper",
    "murmured": "whisper",
    "muttered": "whisper",
    "hissed": "whisper",
    "shouted": "shout",
    "yelled": "shout",
    "screamed": "shout",
    "bellowed": "shout",
    "exclaimed": "excited",
    "laughed": "cheerful",
    "chuckled": "cheerful",
    "giggled": "cheerful",
    "joked": "cheerful",
    "sobbed": "sad",
    "cried": "sad",
    "wailed": "sad",
    "whimpered": "sad",
    "snapped": "angry",
    "snarled": "angry",
    "growled": "angry",
    "demanded": "angry",
}

# Emotion → edge-tts prosody parameters
EMOTION_PROSODY = {
    "whisper":  {"rate": "-15%", "volume": "-30%", "pitch": "-5Hz"},
    "shout":    {"rate": "+10%", "volume": "+20%", "pitch": "+10Hz"},
    "excited":  {"rate": "+15%", "volume": "+10%", "pitch": "+5Hz"},
    "cheerful": {"rate": "+10%", "volume": "+5%",  "pitch": "+8Hz"},
    "sad":      {"rate": "-20%", "volume": "-10%", "pitch": "-10Hz"},
    "angry":    {"rate": "+10%", "volume": "+15%", "pitch": "-5Hz"},
}


# ── Dialogue parsing ──────────────────────────────────────────────


# Match quoted speech — handles straight, curly, and mixed quote styles.
# Minimum 2 chars so short exclamations ("Hi!", "No!", "Box?") still
# register as dialogue.
_ANY_QUOTE = '[\"\u201c\u201d]'
_DIALOGUE_RE = re.compile(
    rf'{_ANY_QUOTE}(?P<speech>[^\"\u201c\u201d]{{2,}}){_ANY_QUOTE}'
)

# After a closing quote: "dialogue," Name verbed  OR  "dialogue," verbed Name
# Name matches: optional honorific/title ("Mrs.", "Professor", "Aunt", etc.)
# followed by 1–2 proper-noun tokens — OR a pronoun. This keeps titled
# speakers intact ("Mrs. Weasley", "Professor McGonagall") instead of
# splitting them into two fake characters.
_TITLE_PREFIX = (
    r"(?:"
    r"Mr\.?|Mrs\.?|Ms\.?|Miss|Mister|Mistress|"
    r"Dr\.?|Prof\.?|Professor|"
    r"Sir|Lord|Lady|Madam|Madame|Dame|"
    r"Aunt|Auntie|Uncle|Master|"
    r"Captain|Cap|Colonel|Commander|General|Major|Lieutenant|Lt\.?|"
    r"Sergeant|Sgt\.?|Officer|Agent|Detective|"
    r"Headmaster|Headmistress|Auror|Deputy|"
    r"King|Queen|Prince|Princess|Duke|Duchess|Count|Countess|"
    r"Brother|Sister|Father|Mother|Reverend|Cardinal|Bishop"
    r")\s+"
)
# Allow camelcase / mid-word caps / apostrophes so names like McGonagall,
# MacArthur, and O'Brien register as a single proper-noun token.
_PROPER_TOKENS = r"[A-Z][a-zA-Z']*[a-z](?:\s+[A-Z][a-zA-Z']*[a-z])?"
_NAME_PAT = (
    r"(?P<name>"
    rf"(?:{_TITLE_PREFIX})?{_PROPER_TOKENS}"
    r"|"
    r"(?:he|she|they|it|He|She|They|It)"
    r")"
)
_AFTER_NAME_VERB = re.compile(rf"\s*{_NAME_PAT}\s+(?P<verb>\w+)")
_AFTER_VERB_NAME = re.compile(
    rf"\s*(?P<verb>\w+)\s+{_NAME_PAT}"
    r"(?:\s|[.,;!?])"  # require word boundary after name
)

# Before an opening quote: Name verbed, "dialogue"
_BEFORE_ATTRIB = re.compile(
    r"(?P<name>"
    rf"(?:{_TITLE_PREFIX})?{_PROPER_TOKENS}"
    r")"
    r'\s+(?P<verb>\w+)\s*,\s*$'
)

# Common attribution verbs. Fanfic writers reach for non-speech verbs
# ("pressed", "nodded", "grinned") to tag dialogue more often than
# traditional fiction, so this list is deliberately broad.
_SPEECH_VERBS = {
    # Canonical speech
    "said", "asked", "replied", "answered", "whispered", "murmured",
    "muttered", "shouted", "yelled", "screamed", "exclaimed", "cried",
    "called", "told", "added", "continued", "began", "suggested",
    "demanded", "insisted", "agreed", "protested", "snapped", "snarled",
    "growled", "laughed", "chuckled", "giggled", "sobbed", "sighed",
    "groaned", "moaned", "hissed", "bellowed", "wailed", "whimpered",
    "stammered", "stuttered", "blurted", "joked", "remarked", "noted",
    "observed", "commented", "declared", "announced", "explained",
    "offered", "interrupted", "repeated", "admitted", "confessed",
    "acknowledged", "rasped", "breathed", "grunted", "stated",
    # Commands / emphasis
    "ordered", "commanded", "barked", "scolded", "warned", "chided",
    "teased", "retorted", "countered", "responded", "intoned",
    "pressed", "prodded", "pushed", "urged", "prompted",
    # Manner
    "drawled", "mumbled", "complained", "whined", "grumbled",
    "gasped", "snorted", "scoffed", "huffed", "sneered", "spat",
    "pleaded", "begged", "prayed", "greeted", "crooned", "cooed",
    "lisped", "spluttered", "babbled", "squeaked", "squealed",
    "piped", "chirped", "quipped", "boasted", "bragged", "promised",
    "vowed", "swore", "confided", "asserted", "argued",
    "cautioned", "reminded",
    "assured", "reassured", "soothed", "coaxed", "consoled",
    "reasoned", "clarified", "elaborated", "finished", "concluded",
    "corrected", "apologized", "apologised",
    # Fanfic-style verbs — non-verbal actions commonly paired with a
    # quote to attribute it. We accept these to avoid losing speakers
    # like "…" Lee pressed, "…" Harry nodded.
    "nodded", "shook",  # "he shook his head"
    "grinned", "smirked", "smiled", "beamed", "frowned", "grimaced",
    "scowled", "pouted", "blinked", "shrugged", "gestured",
    "nodded", "glared",
    "called", "yelled", "crowed", "cackled", "roared",
    "sang", "hummed",
    "wondered", "mused", "speculated", "thought", "pondered",
    "inquired", "queried", "quizzed", "questioned",
    "repeated", "reiterated", "echoed", "parroted",
    "conceded", "conceded", "concurred", "yielded",
    "spoke", "voiced", "uttered", "exhaled", "inhaled",
    "informed", "notified", "instructed", "directed",
    "suggested", "proposed", "recommended",
    "began", "started", "resumed", "ended", "stopped",
    "interjected", "cut", "butted",  # "butted in"
    # Between-dialogue pause verbs — speaker is the same character
    # doing an action between two lines of their own speech:
    # "Hi," Harry paused, "how are you?"
    "paused", "hesitated", "stopped",
    "drawled", "purred", "rumbled",
    "hollered", "whooped",
    "trailed", "faltered", "finished",
    "agreed", "disagreed", "confirmed", "denied",
    "supplied", "volunteered", "ventured",
    "commented", "opined", "noted",
    "acknowledged", "conceded",
    "huffed", "chuckled", "snickered", "tittered",
    "translated", "recited", "dictated", "read",
    "deadpanned", "drawled",
    "accused", "challenged", "defended",
    "soothed",
    # "-ed" narrations that often take dialogue in fanfic
    "breathed", "whispered", "hissed", "growled",
}


class Segment:
    """A piece of text to be spoken."""

    def __init__(self, text, speaker=None, emotion=None):
        self.text = text.strip()
        self.speaker = speaker  # None = narrator
        self.emotion = emotion  # SSML style name or None


_PRONOUNS = {"he", "she", "they", "it"}
# Proper-name regex: allows internal caps (McGonagall, MacKenzie, O'Brien)
# and apostrophes. Length ≥ 3 so 2-letter abbreviations (Mr, Dr) are skipped.
# Also matches possessive forms ("Harry's") — _strip_possessive below
# normalizes them before use.
_PROPER_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z']{1,}[a-z])\b")


def _is_possessive(name):
    return name.endswith("'s") or name.endswith("\u2019s")


def _strip_possessive(name):
    if name.endswith("'s") or name.endswith("\u2019s"):
        return name[:-2]
    return name


# Common capitalized English words that regularly appear at sentence
# starts and get falsely detected as character names. Includes vocatives
# inside dialogue and typical narrator interjections.
_SENTENCE_STARTERS = {
    # Articles / demonstratives / pronouns (capitalized sentence-start)
    "The", "This", "That", "These", "Those", "But", "And", "Or",
    "She", "His", "Her", "Him", "They", "Their", "Them", "You", "Your",
    "Our", "Ours", "Its", "It", "We", "Us", "My", "Mine",
    # Adverbs / conjunctions that often start a sentence
    "Then", "When", "Where", "What", "Which", "Who", "Whom", "Whose",
    "Why", "How", "Not", "Now", "Yes", "No",
    "Well", "So", "If", "While", "Since", "Once", "Twice",
    "Perhaps", "Maybe", "Somehow", "Sometimes", "Often", "Always",
    "Never", "Rarely", "Just", "Only", "Only", "Barely",
    "After", "Before", "During", "Until", "Unless",
    "Actually", "Apparently", "Obviously", "Clearly",
    "Most", "Mostly", "Some", "Many", "Few", "All", "Each", "Every",
    "Either", "Neither", "Both",
    "Are", "Is", "Was", "Were", "Be", "Being", "Been",
    "Do", "Does", "Did", "Has", "Have", "Had",
    "Can", "Could", "Will", "Would", "Shall", "Should", "May", "Might",
    "Good", "Bad", "Great", "Nice", "Fine", "Okay", "Right",
    "Hey", "Hi", "Hello", "Oh", "Oi", "Eh", "Ah", "Aha",
    # Common vocatives inside dialogue
    "Boys", "Girls", "Children", "Kids", "Gentlemen",
    "Ladies", "Everyone", "Anyone", "Someone", "Nobody",
    "Friends", "Folks", "Lads", "Lasses", "Guys", "Fellas",
    # Common narrator-side fragments / typos
    "Yeah", "Yep", "Yup", "Nope", "Nah",
    # Short action / verb-ish words often mistaken
    "Run", "Go", "Come", "Stop", "Wait", "Stay", "Look",
    "Dead", "Alive", "Lost", "Found",
    "Head", "Hand", "Back", "Side", "Front",
    "Hook", "Tooth", "Nail", "Book", "Page", "Line", "Chapter",
    # Adjectives/nationalities often capitalised mid-sentence that are
    # not characters by themselves in most fic.
    "French", "English", "Spanish", "Italian", "German", "Russian",
    "Chinese", "Japanese", "American", "British", "Irish", "Scottish",
    "Welsh", "Indian", "African", "European", "Asian",
    "Bulgarian", "Romanian", "Hungarian", "Polish", "Greek",
    # Common HP location nouns that aren't characters
    "Hogwarts", "Gryffindor", "Slytherin", "Ravenclaw", "Hufflepuff",
    "Diagon", "Hogsmeade", "Azkaban",
    # Month / day names
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
}

# Honorifics/titles that should NOT be treated as standalone character
# names when resolving a pronoun back to a speaker.
_NAME_SKIP_TITLES = {
    "Mr", "Mrs", "Ms", "Miss", "Mister", "Mistress",
    "Dr", "Prof", "Professor",
    "Sir", "Lord", "Lady", "Madam", "Madame", "Dame",
    "Aunt", "Auntie", "Uncle", "Master",
    "Captain", "Cap", "Colonel", "Commander", "General",
    "Major", "Lieutenant", "Sergeant", "Officer", "Agent", "Detective",
    "Headmaster", "Headmistress", "Auror", "Deputy",
    "King", "Queen", "Prince", "Princess",
    "Duke", "Duchess", "Count", "Countess",
    "Brother", "Sister", "Father", "Mother",
    "Reverend", "Cardinal", "Bishop",
}


def parse_segments(text):
    """Split story text into narration and dialogue segments.

    Tracks the last identified speaker so that pronoun-only attribution
    ("she said") and unattributed dialogue in a back-and-forth exchange
    carry forward correctly.
    """
    segments = []
    pos = 0
    last_speaker = None
    prev_speaker = None  # speaker before last_speaker, for 2-way alternation

    for match in _DIALOGUE_RE.finditer(text):
        # Narration before this dialogue
        pre = text[pos : match.start()].strip()
        if pre:
            segments.append(Segment(pre))

        speech = match.group("speech").strip()
        speaker = None
        emotion = None

        # Try attribution after the quote: "dialogue," Name verbed
        after_text = text[match.end() : match.end() + 80]

        def _resolve_pronoun(pronoun=None):
            """When attribution uses a pronoun, find the nearest name in
            the preceding narration text. Returns a full titled name
            when the match is preceded by an honorific ("Mrs. Weasley",
            "Professor McGonagall") so speakers are not split into a
            spurious "Weasley" character.

            When a pronoun ("he"/"she") is passed, prefer candidates
            whose gender matches — "Hermione called. 'X,' he said."
            should resolve `he` to a male character, not Hermione.
            """
            pronoun_gender = None
            if pronoun:
                p = pronoun.lower()
                if p in ("he", "him", "his", "himself"):
                    pronoun_gender = "male"
                elif p in ("she", "her", "hers", "herself"):
                    pronoun_gender = "female"

            window = text[max(0, match.start() - 200) : match.start()]
            matches = [(m.start(), m.group(1)) for m in _PROPER_NAME_RE.finditer(window)]
            candidates = [
                (pos, n) for pos, n in matches
                if n not in _SENTENCE_STARTERS
                and n not in _NAME_SKIP_TITLES
                and not _is_possessive(n)
            ]
            if not candidates:
                # Fall back to possessive-only context:
                # "Harry's eyes narrowed. 'Hi,' he said." should still
                # resolve the pronoun to Harry.
                candidates = [
                    (pos, n) for pos, n in matches
                    if n not in _SENTENCE_STARTERS
                    and n not in _NAME_SKIP_TITLES
                ]
            if not candidates:
                return last_speaker

            def _titled(pos, name):
                name = _strip_possessive(name)
                preceding = window[max(0, pos - 20):pos].rstrip()
                for title in _NAME_SKIP_TITLES:
                    if preceding.endswith(title) or preceding.endswith(title + "."):
                        return f"{title} {name}"
                return name

            # Gender-aware pick: walk candidates latest-first and return
            # the first whose detected gender matches the pronoun.
            if pronoun_gender:
                for pos, name in reversed(candidates):
                    full = _titled(pos, name)
                    g = _guess_gender_from_name(full)
                    if g == pronoun_gender:
                        return full
                # No gender match — fall through to nearest-name default

            pos, name = candidates[-1]
            return _titled(pos, name)

        def _clean_speaker(raw_name):
            """Normalize a captured speaker name: strip possessive 's,
            reject common sentence starters / bare titles, keep titled
            names intact."""
            if not raw_name:
                return None
            raw_name = _strip_possessive(raw_name.strip())
            # Reject single-word sentence starters / noise
            if raw_name in _SENTENCE_STARTERS:
                return None
            # Reject a bare honorific with no following name
            if raw_name in _NAME_SKIP_TITLES:
                return None
            # If the first word of a multi-word name is a sentence
            # starter ("Not Percy", "Now Harry"), drop the starter —
            # the rest is the real name.
            tokens = raw_name.split()
            while tokens and tokens[0] in _SENTENCE_STARTERS:
                tokens = tokens[1:]
            if not tokens:
                return None
            return " ".join(tokens)

        # Detect post-dialogue attribution. When found, we still need
        # the listener to hear "Harry said", so the attribution text is
        # emitted as its own narrator segment after the dialogue — and
        # `attrib_end` advances so the NEXT iteration's pre-text doesn't
        # re-include the same words (which would confuse the pre-action
        # heuristic below by counting the just-used name again).
        attrib_end = match.end()
        am = _AFTER_NAME_VERB.match(after_text)
        if am and am.group("verb").lower() in _SPEECH_VERBS:
            name = am.group("name")
            verb = am.group("verb").lower()
            if name.lower() not in _PRONOUNS:
                speaker = _clean_speaker(name)
            else:
                speaker = _resolve_pronoun(name)
            emotion = EMOTION_MAP.get(verb)
            attrib_end = match.end() + am.end()

        if not speaker:
            am = _AFTER_VERB_NAME.match(after_text)
            if am and am.group("verb").lower() in _SPEECH_VERBS:
                name = am.group("name")
                verb = am.group("verb").lower()
                if name.lower() not in _PRONOUNS:
                    speaker = _clean_speaker(name)
                else:
                    speaker = _resolve_pronoun(name)
                emotion = EMOTION_MAP.get(verb)
                attrib_end = match.end() + am.end()

        if not speaker:
            before_text = text[max(0, match.start() - 80) : match.start()]
            bm = _BEFORE_ATTRIB.search(before_text)
            if bm and bm.group("verb").lower() in _SPEECH_VERBS:
                speaker = _clean_speaker(bm.group("name"))
                emotion = EMOTION_MAP.get(bm.group("verb").lower())

        # Pre-action attribution — "Ron looked up. 'Trouble?'" — a
        # very common fanfic pattern where the speaker is named in
        # the immediately-preceding narration but without a speech
        # verb. Require a SINGLE distinct proper name in the gap so
        # ambiguous crowds don't get misattributed.
        if not speaker:
            pre_text = text[pos : match.start()]
            stripped = pre_text.strip()
            if 0 < len(stripped) <= 200:
                raw_names = _PROPER_NAME_RE.findall(stripped)
                clean_names = []
                for n in raw_names:
                    n = _strip_possessive(n)
                    if n in _SENTENCE_STARTERS or n in _NAME_SKIP_TITLES:
                        continue
                    if n not in clean_names:
                        clean_names.append(n)
                if len(clean_names) == 1:
                    candidate = clean_names[0]
                    # Attach a leading honorific if present
                    idx = stripped.rfind(candidate)
                    preceding = stripped[max(0, idx - 20):idx].rstrip()
                    for title in _NAME_SKIP_TITLES:
                        if preceding.endswith(title) or preceding.endswith(title + "."):
                            candidate = f"{title} {candidate}"
                            break
                    speaker = candidate

        # Consecutive-quote fallback: if this dialogue has no attribution
        # and the text between it and the previous quote is short OR
        # references the previous speaker by name, it is most likely the
        # same speaker continuing.
        #   "Hi," Hermione said. "Where have you been?"
        #              └── gap mentions "Hermione" → carry forward
        # For pure-whitespace gaps in a two-speaker exchange, alternate
        # between last_speaker and prev_speaker so quick back-and-forth
        # dialogue reads correctly instead of sticking to one voice.
        if not speaker and last_speaker:
            pre_text = text[pos : match.start()]
            stripped = pre_text.strip()
            non_ws = len(stripped)
            has_words = any(c.isalnum() for c in stripped)
            if not has_words and prev_speaker and prev_speaker != last_speaker:
                # No actual words between quotes (pure whitespace or
                # just stray punctuation left over from consumed
                # attribution) and two distinct speakers are in play —
                # alternate between them.
                speaker = prev_speaker
            elif non_ws <= 15:
                speaker = last_speaker
            elif non_ws <= 200:
                # Carry forward when the gap has no OTHER proper name in
                # play — absence of a new character = same speaker
                # continuing. "X said. Y walked in. 'hi'" would wrongly
                # keep X, so this is gated on no other names appearing.
                last_first = last_speaker.split()[0]
                last_tail = last_speaker.split()[-1]
                other_names = [
                    n for n in _PROPER_NAME_RE.findall(stripped)
                    if n not in _SENTENCE_STARTERS
                    and n not in _NAME_SKIP_TITLES
                    and n != last_first
                    and n != last_tail
                    and not _is_possessive(n)
                ]
                if not other_names:
                    speaker = last_speaker

        if speaker:
            if speaker != last_speaker:
                prev_speaker = last_speaker
            last_speaker = speaker

        # Truly unattributable dialogue — no speaker, no pronoun, no
        # preceding/trailing name. Render it as narrator speech but
        # keep the quote marks so TTS renders it with dialogue-like
        # intonation instead of sounding like plain exposition.
        seg_text = speech
        if speaker is None:
            seg_text = f'"{speech}"'
        segments.append(Segment(seg_text, speaker=speaker, emotion=emotion))
        # If we consumed after-attribution text ("Harry said"), emit it
        # as its own narrator segment so the listener hears it — while
        # keeping pos advanced past it for clean subsequent parsing.
        if attrib_end > match.end():
            attrib_text = text[match.end():attrib_end].strip()
            if attrib_text:
                segments.append(Segment(attrib_text))
        pos = attrib_end

    # Trailing narration
    trailing = text[pos:].strip()
    if trailing:
        segments.append(Segment(trailing))

    return segments


# ── Gender detection ──────────────────────────────────────────────


# Name-based gender detection: suffixes and common overrides.
# Pronoun analysis is unreliable in POV narratives where one gender
# dominates the prose, so we lean on names as the primary signal.
_FEMALE_SUFFIXES = (
    "ella", "anna", "ette", "ine", "elle", "issa", "ina",
    "lia", "ria", "dia", "sia", "nie", "ley", "lie",
)

# Titles and honorifics. Stripped from name parts; gendered variants also
# directly imply a gender (strongest hint — overrides name lookup).
_MALE_TITLES = {
    "mr", "mister", "sir", "lord", "master", "uncle",
    "king", "prince", "duke", "count", "baron", "earl",
    "brother", "father", "bro", "grandpa", "grandfather",
    "headmaster",
}
_FEMALE_TITLES = {
    "mrs", "ms", "miss", "madam", "madame", "lady",
    "mistress", "aunt", "auntie",
    "queen", "princess", "duchess", "countess", "baroness",
    "sister", "mother", "mum", "mom", "grandma", "grandmother",
    "headmistress", "dame",
}
_NEUTRAL_TITLES = {
    "professor", "prof", "doctor", "dr",
    "captain", "cap", "colonel", "commander", "general",
    "major", "lieutenant", "lt", "sergeant", "sgt",
    "officer", "agent", "detective",
    "elder", "senator", "councillor", "mayor",
    "minister", "director", "chief",
    "reverend", "rev", "cardinal", "bishop",
    "auror", "deputy",
}

# ----------------------------------------------------------------
# First-name overrides. These are canonical characters from widely
# written fandoms where the first name alone pins the gender. Kept
# lowercase; names with ambiguous real-world use (e.g. Lee, Morgan,
# Robin) are included ONLY when the fandom dominates usage in
# fanfiction corpora.
# ----------------------------------------------------------------
_FEMALE_NAMES = {
    # Harry Potter
    "hermione", "ginny", "ginevra", "luna", "fleur", "lily", "rose",
    "molly", "lucy", "roxanne", "dominique", "victoire", "audrey",
    "petunia", "marge", "bellatrix", "narcissa", "andromeda",
    "nymphadora", "dora", "tonks", "minerva", "pomona", "poppy",
    "sybill", "sybil", "rolanda", "aurora", "septima", "charity",
    "bathsheda", "dolores", "umbridge", "amelia", "astoria", "daphne",
    "pansy", "millicent", "tracey", "katie", "angelina", "alicia",
    "cho", "romilda", "lavender", "parvati", "padma", "marietta",
    "penelope", "queenie", "porpentina", "tina", "olympe", "perenelle",
    "hestia", "rita", "hannah", "susan", "megan", "ariana", "kendra",
    "gabrielle", "apolline", "ivy", "alicia", "vernity", "fluer",
    "hedwig", "mrs.norris", "nagini", "myrtle", "mrytle",
    "hooch", "sprout", "pomfrey", "sinistra", "vector", "burbage",
    "babbling", "skeeter", "mcgonagall", "trelawney",
    # Worm (Parahumans)
    "taylor", "lisa", "tattletale", "bitch", "rachel", "dinah",
    "skitter", "weaver", "amy", "panacea", "vicky", "victoria",
    "aisha", "imp", "riley", "bonesaw", "emma",
    "madison", "sophia", "shadowstalker", "missy", "vista",
    "theresa", "alexandria", "rebecca", "contessa", "fortuna",
    "ciara", "valkyrie", "cauldron", "bakuda", "purity",
    "noelle", "sundancer", "marissa", "narwhal",
    # Buffy / Angel
    "buffy", "willow", "dawn", "faith", "anya", "tara", "cordelia",
    "kendra", "darla", "drusilla", "harmony", "glory",
    # note: Buffy's "Fred" (Winifred Burkle) is F but collides with HP's
    # Fred Weasley (M) — HP dominance in fanfic corpora wins, so "fred"
    # is in _MALE_NAMES only. Buffy fic using Winifred's nickname will
    # need manual voice-map override.
    "winifred",
    "joyce", "jenny", "kate", "lilah", "eve",
    # Game of Thrones / ASOIAF
    "arya", "sansa", "cersei", "catelyn", "daenerys", "dany",
    "margaery", "shae", "ygritte", "brienne", "melisandre",
    "myrcella", "gilly", "lysa", "olenna", "ellaria", "missandei",
    "yara", "asha", "osha",
    # Lord of the Rings
    "eowyn", "arwen", "galadriel", "rosie", "lobelia",
    # Percy Jackson / Riordan
    "annabeth", "thalia", "hazel", "rachel", "silena", "bianca",
    "calypso", "piper",  # already above
    "clarisse", "zoe", "reyna", "drew", "artemis", "aphrodite",
    "athena", "hera", "persephone", "demeter", "hestia",
    # Marvel (MCU / 616)
    "natasha", "wanda", "pepper", "jane", "darcy", "peggy",
    "nebula", "gamora", "mantis", "okoye", "shuri", "nakia",
    "ramonda", "valkyrie",  # name collision with Worm ok
    "carol", "maria", "monica", "jessica", "kamala", "ava",
    "hope", "yelena", "melina", "morgan",  # morgan stark
    "may", "mj", "michelle", "liz", "betty",
    # DC
    "diana", "lois", "selina", "barbara", "kara", "harley",
    "ivy", "donna", "cassandra", "stephanie", "zatanna",
    "raven", "starfire", "koriand'r", "mera", "iris",
    # Naruto
    "sakura", "hinata", "ino", "tenten", "temari", "kushina",
    "tsunade", "anko", "kurenai", "konan", "mei", "mebuki",
    "karin", "shizune",
    # Bleach
    "rukia", "orihime", "yoruichi", "rangiku", "momo", "hinamori",
    "nemu", "soifon", "unohana", "nanao", "isane", "kiyone",
    "retsu", "neliel", "nel", "harribel", "apacci", "mila",
    # One Piece
    "nami", "robin", "boa", "hancock", "nico", "vivi", "shirahoshi",
    "carrot", "reiju", "tashigi", "hina", "perona", "tsuru",
    # Fullmetal Alchemist
    "winry", "riza", "hawkeye", "izumi", "lan", "lanfan", "mei",
    # Miscellaneous common / fantasy
    "alice", "claire", "eve", "grace", "iris", "jane", "joy",
    "kate", "mae", "may", "faith", "hope", "dawn", "willow",
    "joan", "ann", "beth", "ruth", "jean", "nell", "fern",
    "rachel", "lillian", "madison", "morgan", "misty",
    "sarah", "mary", "nancy", "helen", "karen", "wendy",
    "janet", "robin", "amber", "crystal", "heather", "brooke",
    "paige", "quinn", "phoebe", "sansa", "piper",
    "emma", "olivia", "sophia", "ava", "mia", "isabella",
    "charlotte", "amelia", "harper", "evelyn", "abigail",
    "emily", "elizabeth", "avery", "sofia", "ella", "madison",
    "scarlett", "victoria", "aria", "grace", "chloe", "camila",
    "penelope", "riley", "zoey", "nora", "lily", "eleanor",
    "hannah", "lillian", "addison", "aubrey", "ellie", "stella",
    "natalie", "zoe", "leah", "hazel", "violet", "aurora",
    "savannah", "audrey", "brooklyn", "bella", "claire", "skylar",
    "lucy", "paisley", "everly", "anna", "caroline", "nova",
    "genesis", "emilia", "kennedy", "samantha", "maya", "willow",
    "kinsley", "naomi", "aaliyah", "elena", "sarah", "ariana",
    "allison", "gabriella", "alice", "madelyn", "cora", "ruby",
    "eva", "serenity", "autumn", "adeline", "hailey", "gianna",
    "valentina", "isla", "eliana", "quinn", "nevaeh", "ivy",
    "sadie", "piper", "lydia", "alexa", "josephine", "emery",
    "julia", "delilah", "arianna", "vivian", "kaylee", "sophie",
    "brielle", "madeline", "peyton", "rylee", "clara", "hadley",
    "melanie", "mackenzie", "reagan", "adalynn", "liliana",
    "aubree", "jade", "katherine", "isabelle", "natalia", "raelynn",
    "maria", "athena", "ximena", "arya",  # already above
}

_MALE_NAMES = {
    # Harry Potter — main cast
    "harry", "ron", "ronald", "draco", "james", "albus", "sirius",
    "remus", "severus", "neville", "dean", "seamus", "oliver",
    "cedric", "viktor", "lucius", "regulus", "kingsley", "rufus",
    "cornelius", "horace", "alastor", "filius", "gilderoy",
    "percy", "fred", "george", "arthur", "bill", "charlie",
    "hagrid", "rubeus", "voldemort", "tom", "riddle",
    "colin", "dennis", "peter", "pettigrew", "wormtail",
    "padfoot", "prongs", "moony", "hadrian",
    "igor", "karkaroff", "barty", "bartemius", "crouch",
    "dudley", "vernon", "quirrell", "quirinus",
    "aberforth", "gellert", "grindelwald", "argus", "filch",
    "bane", "firenze", "grawp", "dobby", "kreacher",
    "rodolphus", "rabastan", "evan", "rosier", "antonin",
    "dolohov", "walden", "macnair", "corban", "yaxley",
    "amycus", "augustus", "rookwood", "thorfinn", "rowle",
    "scrimgeour", "scabior", "fenrir", "greyback", "travers",
    "dedalus", "diggle", "elphias", "mundungus", "fletcher",
    "lee", "jordan",  # Lee Jordan — Fred & George's friend
    "blaise", "zabini", "theodore", "nott", "gregory", "goyle",
    "vincent", "crabbe", "marcus", "flint", "terry", "boot",
    "michael", "corner", "anthony", "goldstein", "ernie",
    "macmillan", "justin", "finch-fletchley", "zacharias",
    "smith", "wayne", "moon", "roger", "davies", "adrian",
    "pucey", "miles", "bletchley", "cormac", "mclaggen",
    "kevin", "entwhistle", "rolf", "newt", "newton", "theseus",
    "scamander", "graves", "jacob", "kowalski", "credence",
    "ollivander", "xenophilius", "ludo", "bagman", "ludovic",
    "augustus", "broderick", "bode", "sturgis", "podmore",
    "michael", "gibbon", "jugson", "selwyn", "nicolas", "flamel",
    "aberforth", "ignotus", "cadmus", "antioch", "peverell",
    "salazar", "godric", "wulfric", "percival", "brian",
    "teddy", "ted", "fabian", "gideon", "prewett", "marius",
    # Marauders / Weasley / misc shortenings
    "moony", "wormy", "padfoot", "prongs",
    # Worm / Parahumans
    "brian", "grue", "alec", "regent",
    "jeff", "clockblocker", "dean", "gallant", "carlos",
    "aegis", "chris", "armsmaster", "colin",
    "legend", "keith", "scion", "eidolon", "hero",
    "myrddin", "accord", "lung", "kaiser", "hookwolf",
    "stormtiger", "crusader", "krieg",
    "oni_lee", "uber", "leet", "skidmark", "mush",
    "aster", "theo", "coil", "calvert",
    # Buffy / Angel
    "xander", "giles", "rupert", "angel", "angelus", "spike",
    "william", "oz", "riley", "wesley", "gunn", "connor",
    "lorne", "doyle", "graham", "forrest",
    # Game of Thrones / ASOIAF
    "eddard", "ned", "robb", "jon", "bran", "rickon", "theon",
    "tyrion", "jaime", "tywin", "joffrey", "tommen", "stannis",
    "renly", "robert", "rhaegar", "viserys", "aemon", "aegon",
    "samwell", "sam", "gendry", "jorah", "tormund", "ramsay",
    "roose", "walder", "littlefinger", "petyr", "baelish",
    "varys", "bronn", "sandor", "gregor", "podrick", "edmure",
    "robin", "davos", "beric", "thoros", "mance", "jeor",
    # Lord of the Rings
    "frodo", "sam", "samwise", "merry", "meriadoc", "pippin",
    "peregrin", "gandalf", "mithrandir", "aragorn", "elessar",
    "legolas", "gimli", "boromir", "faramir", "denethor",
    "theoden", "eomer", "elrond", "celeborn", "thranduil",
    "saruman", "sauron", "bilbo", "gollum", "smeagol",
    "beorn", "radagast", "balin", "thorin", "dwalin", "oin",
    "gloin", "fili", "kili", "bofur", "bombur", "bifur",
    # Percy Jackson
    "percy", "grover", "luke", "chiron", "tyson", "nico",
    "jason", "leo", "frank", "malcolm", "connor", "travis",
    "will", "apollo", "ares", "zeus", "poseidon", "hades",
    "hermes", "hephaestus", "dionysus",
    # Marvel (MCU / 616)
    "tony", "steve", "bucky", "thor", "loki", "clint", "bruce",
    "stephen", "vision", "sam", "rhodey", "rhodes", "peter",
    "miles", "matt", "wade", "logan", "scott", "hank",
    "charles", "erik", "kurt", "bobby", "warren", "remy",
    "victor", "tchalla", "killmonger", "thanos", "nick", "fury",
    "phil", "coulson", "happy", "ned", "flash", "eugene",
    "johnny", "ben", "reed", "doc", "norman", "harry",  # already
    "eddie", "kraven", "vulture", "electro", "sandman",
    "mysterio", "quentin", "beck",
    # DC
    "bruce", "clark", "diana_m",  # Diana = F
    "arthur", "wally", "dick", "jason", "tim", "damian",
    "barry", "hal", "john_stewart", "kyle", "oliver",
    "lex", "joker", "riddler", "penguin", "oswald",
    # Naruto / Bleach / One Piece
    "naruto", "sasuke", "kakashi", "itachi", "obito", "madara",
    "minato", "jiraiya", "iruka", "shikamaru", "choji", "neji",
    "lee",  # Rock Lee — already covered
    "gaara", "kankuro", "kiba", "shino", "sai", "yamato",
    "orochimaru", "hashirama", "tobirama", "hiruzen", "asuma",
    "ichigo", "renji", "byakuya", "uryu", "chad", "sado", "aizen",
    "luffy", "zoro", "sanji", "usopp", "ace", "sabo",
    # Generic / modern / classic
    "jack", "john", "max", "ben", "tom", "dan", "bob", "jim",
    "brian", "kevin", "mark", "paul", "sean", "adam", "carl",
    "eric", "greg", "hugh", "ian", "karl", "leon", "neil",
    "owen", "alan", "chad", "luke", "finn", "ross", "kurt",
    "seth", "michael", "micheal", "danny", "robert", "william",
    "richard", "edward", "henry", "charles", "david", "joseph",
    "frank", "ray", "cole", "ryan", "nathan", "nathaniel",
    "zachary", "christopher", "christian", "christophe",
    "andrew", "joshua", "matthew", "daniel", "anthony",
    "thomas", "joseph", "steven", "stephen", "kenneth",
    "edward", "timothy", "jason", "jeffrey", "scott",
    "benjamin", "samuel", "raymond", "patrick", "alexander",
    "jack", "dennis", "jerry", "tyler", "aaron", "jose",
    "henry", "adam", "douglas", "nathan", "zachary", "walter",
    "kyle", "harold", "carl", "arthur", "roger", "lawrence",
    "terry", "albert", "jesse", "dylan", "bryan", "joe",
    "jordan", "billy", "bruce", "russell", "ronald",
    "philip", "craig", "alan", "shawn", "gary", "gerald",
    "bobby", "johnny", "ricky", "tony", "tommy", "louis",
    "wayne", "roy",
    # Pet/shortened fanfic-common
    "noah", "liam", "ethan", "mason", "caleb", "colton",
    "hunter", "owen", "wyatt", "grayson", "levi", "ezra",
    "jaxon", "asher", "carter", "landon", "blake",
}


# Single-gender canonical SURNAMES. Used when the speaker string has no
# first name (e.g. the text tags them as just "Snape" or "McGonagall").
# Only list surnames where ALL canonical characters with that surname
# share one gender — ambiguous family names (Weasley, Potter, Malfoy,
# Stark, Black) are deliberately omitted.
_MALE_SURNAMES = {
    "snape", "dumbledore", "hagrid", "voldemort", "riddle",
    "filch", "slughorn", "lockhart", "moody", "flitwick",
    "kingsley", "shacklebolt", "scrimgeour", "fudge",
    "diggory", "krum", "ollivander", "xenophilius",
    "grindelwald", "flamel", "dolohov", "yaxley", "greyback",
    "pettigrew", "wormtail", "scabior", "bagman",
    "quirrell", "karkaroff",
    "gandalf", "aragorn", "legolas", "gimli", "elrond",
    "saruman", "sauron", "bilbo", "frodo", "samwise",
    "skywalker",  # ambiguous across Star Wars — but fic usage = Luke dominant
    "kakashi", "itachi", "jiraiya", "orochimaru",
    "naruto", "sasuke",
    "grue", "regent", "armsmaster", "coil",
}
_FEMALE_SURNAMES = {
    "mcgonagall", "umbridge", "pomfrey", "sprout", "hooch",
    "trelawney", "sinistra", "vector", "burbage", "skeeter",
    "bones", "delacour", "granger", "greengrass",
    "parkinson", "bulstrode", "johnson", "spinnet", "bell",
    "chang", "vane", "brown", "patil", "norris",  # Mrs. Norris
    "tonks", "maxime", "pince", "padma",
    "galadriel", "arwen", "eowyn",
    "panacea",  # always Amy Dallon in Worm
    "skitter",  # always Taylor Hebert in Worm
    "tattletale",  # always Lisa Wilbourn in Worm
    "targaryen",  # ambiguous but Daenerys dominant in fic — skip
}
_FEMALE_SURNAMES.discard("targaryen")  # explicit: surname is ambiguous


def _strip_titles(parts):
    """Strip leading honorifics/titles from a name's words.

    Returns (remaining_parts, gender_hint_or_None). Gendered titles
    (Mr., Mrs., Aunt, Sir, Lady, …) set the hint; neutral titles
    (Professor, Doctor, Captain, …) are stripped without a hint.
    """
    hint = None
    cleaned = list(parts)
    while cleaned:
        token = cleaned[0].lower().rstrip(".,:;!?'\u2019")
        if token in _MALE_TITLES:
            hint = hint or "male"
            cleaned = cleaned[1:]
        elif token in _FEMALE_TITLES:
            hint = hint or "female"
            cleaned = cleaned[1:]
        elif token in _NEUTRAL_TITLES:
            cleaned = cleaned[1:]
        else:
            break
    return cleaned, hint


def _guess_gender_from_name(name):
    """Heuristic gender from a full speaker name string.

    Priority: gendered title > first-name lookup > canonical surname
    lookup > suffix heuristics. Returns None when ambiguous.
    """
    parts = name.split()
    if not parts:
        return None

    parts, title_hint = _strip_titles(parts)
    if title_hint:
        return title_hint

    if not parts:
        return None

    first = parts[0].lower().rstrip(".,:;!?'\u2019")
    last = parts[-1].lower().rstrip(".,:;!?'\u2019") if len(parts) > 1 else None

    if first in _FEMALE_NAMES:
        return "female"
    if first in _MALE_NAMES:
        return "male"

    # Canonical single-gender surname — only used when first name is
    # unknown (avoid overriding a known first name with a weaker signal).
    if last and last in _FEMALE_SURNAMES:
        return "female"
    if last and last in _MALE_SURNAMES:
        return "male"
    # If the speaker is tagged with JUST a surname (single token), check it.
    if first in _FEMALE_SURNAMES:
        return "female"
    if first in _MALE_SURNAMES:
        return "male"

    # Suffix heuristics on the first name
    if first.endswith(_FEMALE_SUFFIXES) or first.endswith("a"):
        return "female"

    # Names ending in hard consonants tend male
    if first.endswith(("ck", "rd", "ld", "rt", "rn", "us", "or", "er", "on")):
        return "male"

    return None  # ambiguous


def consolidate_speakers(speaker_counts):
    """Merge short and long name variants referring to the same
    character within a single story.

    Input:  dict / Counter of {speaker_name: count}
    Output: (canonical_name_map, merged_counts)
      - canonical_name_map: {original_name: canonical_name}
      - merged_counts: {canonical_name: total_count} (after merging)

    Rules:
    - Strip possessive "'s" (already done upstream, but done again
      defensively).
    - For each single-word speaker (e.g. "Ron"), if there is EXACTLY
      one multi-word speaker whose first OR last word matches it,
      merge the short form into the long form (e.g. "Ron" +
      "Ron Weasley" → "Ron Weasley"). The long form wins because it
      disambiguates.
    - Last-name merging is skipped when the surname is ambiguous
      (Weasley, Potter, Black, etc. — families with multiple members).
    """
    # Surnames where multiple canonical characters share them — never
    # merge on this basis alone.
    AMBIGUOUS_SURNAMES = {
        "weasley", "potter", "malfoy", "black", "longbottom",
        "granger", "dursley", "stark", "targaryen", "lannister",
        "baratheon", "tully", "tyrell", "greyjoy", "bolton",
        "parkinson", "greengrass", "scamander",
    }

    # Pre-pass: collapse punctuation/title-spelling variants of the same
    # name ("Mr. Dumbledore" ↔ "Mr Dumbledore") so they don't survive as
    # two distinct speakers with two different voices.
    def _norm_key(name):
        tokens = name.split()
        stripped, _ = _strip_titles(tokens)
        return tuple(t.lower().rstrip(".,:;!?'\u2019") for t in stripped)

    variant_groups = {}  # norm_key → [(name, count), ...]
    for name, cnt in speaker_counts.items():
        clean = _strip_possessive(name).strip()
        key = _norm_key(clean)
        if not key:
            continue
        variant_groups.setdefault(key, []).append((clean, cnt))

    # Canonical spelling per group: highest-count variant, preferring
    # the one with a period in its title ("Mr. Dumbledore" over "Mr
    # Dumbledore") on ties.
    variant_canon = {}  # any_variant → canonical_variant
    for key, variants in variant_groups.items():
        if len(variants) == 1:
            variant_canon[variants[0][0]] = variants[0][0]
            continue
        variants.sort(key=lambda x: (-x[1], 0 if "." in x[0] else 1))
        winner = variants[0][0]
        for v, _c in variants:
            variant_canon[v] = winner

    canonical = {}
    # Build candidate map: short-name → list of (long_name, count)
    by_first = {}
    by_last = {}
    multi_word = []
    for name, cnt in speaker_counts.items():
        clean = _strip_possessive(name).strip()
        clean = variant_canon.get(clean, clean)
        tokens = clean.split()
        if len(tokens) == 1:
            continue
        # Strip leading titles when indexing so "Mrs. Weasley" indexes
        # as both "Mrs" (title) and "Weasley".
        stripped_tokens, _ = _strip_titles(tokens)
        if not stripped_tokens:
            continue
        first = stripped_tokens[0]
        last = stripped_tokens[-1] if len(stripped_tokens) > 1 else None
        multi_word.append((clean, cnt, first, last))
        by_first.setdefault(first, []).append((clean, cnt))
        if last and last != first:
            by_last.setdefault(last, []).append((clean, cnt))

    for name, cnt in speaker_counts.items():
        clean = _strip_possessive(name).strip()
        clean = variant_canon.get(clean, clean)
        tokens = clean.split()
        if len(tokens) > 1:
            canonical[name] = clean
            continue
        # Single-word speaker. Try to merge into a multi-word variant.
        token = tokens[0]
        token_low = token.lower()
        first_matches = by_first.get(token, [])
        last_matches = by_last.get(token, [])
        # If surname is ambiguous and token is that surname, don't merge
        if token_low in AMBIGUOUS_SURNAMES and not first_matches:
            canonical[name] = clean
            continue
        # Prefer first-name matches (more specific) — merge if exactly 1
        if len(first_matches) == 1:
            canonical[name] = first_matches[0][0]
        elif len(last_matches) == 1 and token_low not in AMBIGUOUS_SURNAMES:
            canonical[name] = last_matches[0][0]
        else:
            canonical[name] = clean

    merged = Counter()
    for name, cnt in speaker_counts.items():
        merged[canonical[name]] += cnt

    return canonical, merged


def detect_character_genders(full_text, characters):
    """Detect gender using name heuristics first, pronouns as fallback."""
    genders = {}
    lower = full_text.lower()
    either_re = re.compile(r"\b(?:he|him|his|himself|she|her|hers|herself)\b")

    for name in characters:
        # Try name-based detection first (most reliable)
        name_gender = _guess_gender_from_name(name)
        if name_gender:
            genders[name] = name_gender
            continue

        # Fallback: first pronoun after each name mention
        male_score = 0
        female_score = 0
        for m in re.finditer(re.escape(name), full_text):
            after = lower[m.end() : m.end() + 60]
            pm = either_re.search(after)
            if pm:
                word = pm.group()
                if word in ("he", "him", "his", "himself"):
                    male_score += 1
                else:
                    female_score += 1

        if male_score > female_score:
            genders[name] = "male"
        elif female_score > male_score:
            genders[name] = "female"
        else:
            genders[name] = "neutral"

    return genders


# ── Voice mapping ─────────────────────────────────────────────────


class VoiceMapper:
    """Assigns and persists character → voice mappings."""

    def __init__(self, map_path=None):
        self.map_path = Path(map_path) if map_path else None
        self.mapping = {}  # character name → voice ID
        self._male_idx = 0
        self._female_idx = 0
        self._neutral_idx = 0
        if self.map_path and self.map_path.exists():
            self.mapping = json.loads(self.map_path.read_text(encoding="utf-8"))
            logger.info("Loaded voice map with %d characters", len(self.mapping))

    def save(self):
        if self.map_path:
            self.map_path.parent.mkdir(parents=True, exist_ok=True)
            self.map_path.write_text(
                json.dumps(self.mapping, indent=2), encoding="utf-8"
            )

    def assign(self, name, gender="neutral"):
        if name in self.mapping:
            return self.mapping[name]

        if gender == "male":
            voice = MALE_VOICES[self._male_idx % len(MALE_VOICES)]
            self._male_idx += 1
        elif gender == "female":
            voice = FEMALE_VOICES[self._female_idx % len(FEMALE_VOICES)]
            self._female_idx += 1
        else:
            voice = NEUTRAL_VOICES[self._neutral_idx % len(NEUTRAL_VOICES)]
            self._neutral_idx += 1

        # Don't assign the narrator voice to a character
        if voice == NARRATOR_VOICE:
            return self.assign(name, gender)

        self.mapping[name] = voice
        return voice

    def get(self, name, default=None):
        return self.mapping.get(name, default or NARRATOR_VOICE)


# ── Audio generation ──────────────────────────────────────────────


def _rate_str(pct):
    """Format a percent delta (int) as edge-tts rate string: '+20%' / '-15%'.
    None or 0 returns None (no rate override — edge-tts default).
    """
    if pct is None or pct == 0:
        return None
    return f"{pct:+d}%"


def _combine_rate(base_pct, emotion_rate):
    """Combine a user rate override with an emotion's own rate shift.

    emotion_rate comes from EMOTION_PROSODY as a string like '+10%' or
    '-20%'. If either is absent the other wins; if both are present the
    deltas sum.
    """
    if not emotion_rate:
        return _rate_str(base_pct)
    try:
        emo_pct = int(emotion_rate.rstrip("%"))
    except ValueError:
        return _rate_str(base_pct) or emotion_rate
    total = (base_pct or 0) + emo_pct
    return _rate_str(total) if total else None


async def _generate_segment_audio(segment, voice, output_path, speech_rate=0):
    """Generate audio for a single segment using edge-tts.

    speech_rate is an integer percent delta applied on top of any
    emotion-driven rate adjustment.
    """
    text = segment.text.strip()
    # Skip fragments too short to be meaningful speech
    if not text or len(text) < 3 or text.strip(".,;:!?-–—' \"") == "":
        return False

    kwargs = {"voice": voice}

    # Apply prosody adjustments for emotional delivery
    emotion_prosody = {}
    if segment.emotion:
        emotion_prosody = EMOTION_PROSODY.get(segment.emotion, {})

    # rate is the one that combines additively with user preference;
    # volume and pitch stay emotion-only.
    rate = _combine_rate(speech_rate, emotion_prosody.get("rate"))
    if rate:
        kwargs["rate"] = rate
    for key in ("volume", "pitch"):
        if key in emotion_prosody:
            kwargs[key] = emotion_prosody[key]

    comm = _require_edge_tts().Communicate(text, **kwargs)
    await comm.save(str(output_path))
    return True


def _merge_small_segments(segments, min_len=30):
    """Merge short segments to reduce API calls and avoid errors.

    Pass 1: merge adjacent narrator segments.
    Pass 2: absorb very short narrator fragments into the nearest
    narrator segment (even if non-adjacent).
    Pass 3: drop anything that's just punctuation.
    """
    if not segments:
        return []

    # Pass 1: merge adjacent narration
    merged = []
    for seg in segments:
        if not seg.text:
            continue
        if (
            merged
            and seg.speaker is None
            and merged[-1].speaker is None
        ):
            merged[-1].text += " " + seg.text
        else:
            merged.append(Segment(seg.text, seg.speaker, seg.emotion))

    # Pass 2: absorb tiny narrator fragments (< min_len) into neighbors
    cleaned = []
    for i, seg in enumerate(merged):
        if seg.speaker is None and len(seg.text) < min_len:
            # Try to append to the previous narrator segment
            for prev in reversed(cleaned):
                if prev.speaker is None:
                    prev.text += " " + seg.text
                    break
            else:
                # No previous narrator — keep it, it'll merge forward later
                cleaned.append(seg)
        else:
            cleaned.append(seg)

    # Pass 3: drop empty / punctuation-only segments
    return [s for s in cleaned if s.text.strip(".,;:!?-–—' \"")]


# Max concurrent edge-tts API calls per chapter
_TTS_CONCURRENCY = 5

# Pause inserted between segments when the speaker changes. Makes
# multi-voice playback sound less like a rushed relay handoff.
_SPEAKER_CHANGE_PAUSE_MS = 400


def _make_silence_clip(tmp_dir, duration_s):
    """Generate a short silent MP3 clip matching edge-tts output format
    (24 kHz mono MP3) so it can be concat-demuxed with -c copy."""
    path = tmp_dir / f"silence_{int(duration_s * 1000)}ms.mp3"
    result = subprocess.run(
        [
            FFMPEG, "-y",
            "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", f"{duration_s}",
            "-ac", "1", "-ar", "24000",
            "-codec:a", "libmp3lame", "-b:a", "48k",
            str(path),
        ],
        capture_output=True,
    )
    if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        return None
    return path


def _apply_pronunciation_map(text, pron_map):
    """Apply literal string replacements from a user pronunciation map.

    Ordering: longest keys first, so "Hermione Granger" matches before
    "Hermione". Case-sensitive by design — fanfic OC names often collide
    with common English words when lowercased.
    """
    if not pron_map or not text:
        return text
    for key in sorted(pron_map.keys(), key=len, reverse=True):
        if key:
            text = text.replace(key, pron_map[key])
    return text


def _load_pronunciation_map(path):
    """Load a pronunciation override map from JSON. Keys starting with
    '_' are treated as comments and filtered out. Returns empty dict on
    any parse error so a broken map doesn't break audiobook generation.
    """
    try:
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    str(k): str(v)
                    for k, v in data.items()
                    if k and not str(k).startswith("_")
                }
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Pronunciation map at %s unreadable: %s", path, exc)
    return {}


async def _generate_with_semaphore(sem, seg, voice, path, idx, ch_num, speech_rate=0):
    """Generate one segment with a concurrency limiter (retries up to 3 times)."""
    async with sem:
        for attempt in range(1, 4):
            try:
                ok = await _generate_segment_audio(seg, voice, path, speech_rate=speech_rate)
                if ok and path.exists() and path.stat().st_size > 0:
                    return path
            except Exception as exc:
                if attempt < 3:
                    logger.debug(
                        "TTS attempt %d/3 failed for segment %d (ch %d): %s",
                        attempt, idx, ch_num, exc,
                    )
                    await asyncio.sleep(2)
                else:
                    logger.warning(
                        "TTS failed after 3 attempts for segment %d (ch %d): %s",
                        idx, ch_num, exc,
                    )
    return None


async def generate_chapter_audio(
    segments, voice_mapper, output_path,
    chapter_num=0, narrator_voice=None, speech_rate=0,
):
    """Generate audio for a full chapter's worth of segments."""
    narrator = narrator_voice or NARRATOR_VOICE
    segments = _merge_small_segments(segments)

    tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-tts-"))
    sem = asyncio.Semaphore(_TTS_CONCURRENCY)

    # Pre-generate a silence clip inserted at speaker boundaries so the
    # multi-voice playback isn't a breathless relay.
    silence_clip = _make_silence_clip(tmp_dir, _SPEAKER_CHANGE_PAUSE_MS / 1000)

    # Launch all segment TTS calls concurrently (bounded by semaphore),
    # preserving the speaker for each so we can detect voice changes
    # when stitching the chapter together.
    tasks = []
    for i, seg in enumerate(segments):
        if not seg.text:
            continue
        voice = voice_mapper.get(seg.speaker, narrator) if seg.speaker else narrator
        seg_path = tmp_dir / f"seg_{i:06d}.mp3"
        tasks.append((i, seg_path, seg.speaker, _generate_with_semaphore(
            sem, seg, voice, seg_path, i, chapter_num, speech_rate=speech_rate,
        )))

    results = await asyncio.gather(*(t[3] for t in tasks))

    # Collect (speaker, path) pairs for successful generations, in order.
    ordered = [
        (tasks[idx][2], r)
        for idx, r in enumerate(results)
        if r is not None
    ]

    if not ordered:
        return False

    # Merge segments into one chapter file using ffmpeg, inserting the
    # silence clip between consecutive segments whose speakers differ.
    list_file = tmp_dir / "segments.txt"
    with open(list_file, "w") as f:
        prev_speaker = None
        first = True
        for speaker, sf in ordered:
            if (
                silence_clip is not None
                and not first
                and speaker != prev_speaker
            ):
                f.write(f"file '{silence_clip}'\n")
            f.write(f"file '{sf}'\n")
            prev_speaker = speaker
            first = False

    result = subprocess.run(
        [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(output_path),
        ],
        capture_output=True,
    )

    # Clean up temp dir
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if result.returncode != 0:
        logger.warning("ffmpeg concat failed for ch %d: %s", chapter_num, result.stderr[:200])
        return False

    return True


def _escape_ffmeta(value) -> str:
    """Escape special characters for FFMETADATA1 format. The spec requires
    backslash-escaping '=', ';', '#', '\\', and any newline in both keys
    and values. Fanfic titles routinely carry `=`, `;`, or newlines from
    HTML-stripping edge cases, and ffmpeg silently fails to parse the
    whole file when any one value trips the grammar.
    """
    s = "" if value is None else str(value)
    return (
        s
        .replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\r\n", "\n")
        .replace("\n", "\\\n")
    )


def _run_ffmpeg(cmd, *, step):
    """Run an ffmpeg/ffprobe invocation and surface stderr on failure.
    The default `subprocess.run(check=True, capture_output=True)` raises
    CalledProcessError with the ffmpeg message hidden in `.stderr`; we
    want that in the user's face so audiobook errors are debuggable.
    """
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-20:]
        message = "\n".join(tail) or "(no ffmpeg stderr)"
        raise RuntimeError(
            f"ffmpeg failed during {step} (exit {result.returncode}):\n{message}"
        )
    return result


def build_m4b(chapter_files, story, output_path, cover_path=None):
    """Merge per-chapter MP3s into a single M4B with chapter markers."""
    if not chapter_files:
        return None

    tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-m4b-"))

    # Build ffmpeg concat list. Paths must be absolute: ffmpeg resolves
    # `file` entries relative to the list file's own directory, so a bare
    # "ch_0001.mp3" here would be looked up inside tmp_dir.
    list_file = tmp_dir / "chapters.txt"
    with open(list_file, "w") as f:
        for cf in chapter_files:
            f.write(f"file '{Path(cf).resolve()}'\n")

    # First pass: merge all MP3s into one
    merged = tmp_dir / "merged.mp3"
    _run_ffmpeg(
        [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(merged),
        ],
        step="concat",
    )

    # Get chapter durations for metadata
    chapters_meta = tmp_dir / "chapters_meta.txt"
    with open(chapters_meta, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={_escape_ffmeta(story.title)}\n")
        f.write(f"artist={_escape_ffmeta(story.author)}\n")
        f.write(f"album={_escape_ffmeta(story.title)}\n")
        f.write("genre=Audiobook\n\n")

        offset_ms = 0
        for i, cf in enumerate(chapter_files):
            probe = subprocess.run(
                [
                    FFPROBE, "-v", "quiet", "-show_entries",
                    "format=duration", "-of", "csv=p=0", str(cf),
                ],
                capture_output=True,
                text=True,
            )
            duration_s = float(probe.stdout.strip() or "0")
            duration_ms = int(duration_s * 1000)
            ch_title = story.chapters[i].title if i < len(story.chapters) else f"Chapter {i + 1}"

            f.write("[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={offset_ms}\n")
            f.write(f"END={offset_ms + duration_ms}\n")
            f.write(f"title={_escape_ffmeta(ch_title)}\n\n")
            offset_ms += duration_ms

    # Convert to M4B (AAC in M4A container) with chapter metadata
    cmd = [
        FFMPEG, "-y",
        "-i", str(merged),
        "-i", str(chapters_meta),
        "-map_metadata", "1",
    ]
    if cover_path and Path(cover_path).exists():
        cmd.extend(["-i", str(cover_path), "-map", "0:a", "-map", "2:v",
                     "-disposition:v", "attached_pic"])
    cmd.extend([
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        str(output_path),
    ])

    _run_ffmpeg(cmd, step="m4b mux")

    import shutil as _shutil
    _shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path


# ── Main entry point ──────────────────────────────────────────────


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")
FFPLAY = _find_tool("ffplay")


# ── Voice preview ─────────────────────────────────────────────────

def detect_voices(story, map_path=None):
    """Run the character + voice pipeline on a Story without synthesising.

    Returns a list of {"name", "gender", "voice"} dicts in frequency order
    (most-mentioned speakers first). Existing mappings in map_path are
    preserved; newly-seen characters are assigned a voice and written back
    on save.
    """
    mapper = VoiceMapper(map_path)

    full_text = ""
    all_segments = []
    for ch in story.chapters:
        text = html_to_text(ch.html)
        full_text += text + "\n"
        all_segments.append(parse_segments(text))

    raw_char_counts = Counter()
    for segs in all_segments:
        for seg in segs:
            if seg.speaker:
                raw_char_counts[seg.speaker] += 1

    canonical_map, char_counts = consolidate_speakers(raw_char_counts)
    characters = [name for name, count in char_counts.most_common() if count >= 2]
    genders = detect_character_genders(full_text, characters)

    results = []
    for name in characters:
        gender = genders.get(name, "neutral")
        voice = mapper.assign(name, gender)
        results.append({
            "name": name,
            "gender": gender,
            "voice": voice,
            "count": char_counts[name],
        })

    mapper.save()
    return results, mapper


async def _synth_sample_async(text, voice, output_path):
    comm = _require_edge_tts().Communicate(text, voice=voice)
    await comm.save(str(output_path))


def synthesize_sample(voice, text, output_path):
    """Synthesize a short preview clip to output_path (MP3)."""
    asyncio.run(_synth_sample_async(text, voice, str(output_path)))
    return Path(output_path)


def play_audio_file(path):
    """Play an audio file in the background. Returns the Popen handle so
    the caller can terminate it if the user starts another preview.
    """
    try:
        return subprocess.Popen(
            [FFPLAY, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ffplay is required to play voice samples but was not found. "
            "Install ffmpeg (which bundles ffplay)."
        )


def _check_ffmpeg():
    """Verify ffmpeg is available, raise a helpful error if not."""
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg is required for audiobook generation but was not found.\n"
            "Install it from https://ffmpeg.org/download.html\n"
            "On Windows: winget install ffmpeg\n"
            "On macOS: brew install ffmpeg\n"
            "On Linux: sudo apt install ffmpeg"
        )


def generate_audiobook(
    story, output_dir,
    progress_callback=None,
    narrator_voice=None,
    speech_rate=0,
    attribution_backend="builtin",
    attribution_model_size=None,
):
    """Generate an M4B audiobook from a Story with character voice mapping.

    narrator_voice overrides the default NARRATOR_VOICE constant.
    speech_rate is an integer percent delta (-50..+100 sensible range)
    applied to every TTS synthesis call on top of any emotion prosody.
    attribution_backend selects the speaker-attribution refinement pass:
    "builtin" (regex only), "fastcoref", or "booknlp". Unknown or
    uninstalled backends silently fall back to builtin.
    attribution_model_size picks a size variant for backends that
    expose one (BookNLP: "small" or "big"; ignored otherwise).
    progress_callback(current_chapter, total_chapters, title) is called
    after each chapter is synthesized.
    """
    _check_ffmpeg()
    narrator = narrator_voice or NARRATOR_VOICE
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Voice map persists per story
    map_path = output_dir / f".ffn-voices-{story.id}.json"
    mapper = VoiceMapper(map_path)

    # Pronunciation overrides — optional user-editable JSON map:
    # {"Tom Riddle": "Tom Rid-ull", "Nym-a-dora": "Nim-fa-dora", ...}
    # Case-sensitive literal substitution applied before TTS. Per-story
    # so edits survive re-renders of the same audiobook.
    pron_path = output_dir / f".ffn-pronunciations-{story.id}.json"
    pronunciation_map = _load_pronunciation_map(pron_path)
    if not pron_path.exists():
        skeleton = {
            "_comment": (
                "Pronunciation overrides for TTS. Keys are replaced "
                "verbatim in every segment before synthesis (case-"
                "sensitive). Keys starting with '_' are ignored. "
                "Example: \"Hermione\": \"Her-my-oh-nee\""
            )
        }
        pron_path.write_text(json.dumps(skeleton, indent=2) + "\n", encoding="utf-8")
    if pronunciation_map:
        logger.info("Loaded %d pronunciation overrides", len(pronunciation_map))

    # Gather full text for gender detection
    full_text = ""
    chapter_texts = []
    for ch in story.chapters:
        text = html_to_text(ch.html)
        chapter_texts.append(text)
        full_text += text + "\n"

    # Parse all segments to find character names
    all_segments = []
    for text in chapter_texts:
        segs = parse_segments(text)
        all_segments.append(segs)

    # Optional neural refinement pass — replaces / augments the regex
    # speaker attribution. Silently no-ops if the backend isn't
    # installed, so the render never fails on a missing dep.
    if attribution_backend and attribution_backend != "builtin":
        from . import attribution

        for idx, (text, segs) in enumerate(zip(chapter_texts, all_segments)):
            all_segments[idx] = attribution.refine_speakers(
                segs, text,
                backend=attribution_backend,
                model_size=attribution_model_size,
            )

    # Apply pronunciation overrides to every segment's text before TTS.
    if pronunciation_map:
        for segs in all_segments:
            for seg in segs:
                seg.text = _apply_pronunciation_map(seg.text, pronunciation_map)

    # Count character mentions across all chapters
    raw_char_counts = Counter()
    for segs in all_segments:
        for seg in segs:
            if seg.speaker:
                raw_char_counts[seg.speaker] += 1

    # Merge short/long name variants so Ron, Ron Weasley, Weasley all
    # map to the same voice within this story.
    canonical_map, char_counts = consolidate_speakers(raw_char_counts)
    if canonical_map:
        # Rewrite each segment's speaker to the canonical form.
        for segs in all_segments:
            for seg in segs:
                if seg.speaker and seg.speaker in canonical_map:
                    seg.speaker = canonical_map[seg.speaker]

    # Only assign voices to characters with 2+ dialogue instances
    characters = [name for name, count in char_counts.most_common() if count >= 2]
    genders = detect_character_genders(full_text, characters)

    logger.info("Detected %d speaking characters (merged from %d raw)",
                len(characters), len(raw_char_counts))
    for name in characters[:15]:
        gender = genders.get(name, "neutral")
        voice = mapper.assign(name, gender)
        logger.info("  %s (%s) → %s", name, gender, voice)

    mapper.save()

    # Generate audio for each chapter
    chapter_files = []
    total = len(story.chapters)

    for i, (ch, segs) in enumerate(zip(story.chapters, all_segments), 1):
        ch_path = output_dir / f"ch_{i:04d}.mp3"

        if ch_path.exists() and ch_path.stat().st_size > 0:
            chapter_files.append(ch_path)
            if progress_callback:
                progress_callback(i, total, ch.title)
            continue

        success = asyncio.run(
            generate_chapter_audio(
                segs, mapper, ch_path,
                chapter_num=i, narrator_voice=narrator,
                speech_rate=speech_rate,
            )
        )
        if success:
            chapter_files.append(ch_path)
        else:
            logger.warning("No audio generated for chapter %d", i)

        if progress_callback:
            progress_callback(i, total, ch.title)

    if not chapter_files:
        raise RuntimeError("No chapter audio was generated.")

    # Download cover image for embedding
    cover_path = None
    cover_url = story.metadata.get("cover_url")
    if cover_url:
        from .exporters import _fetch_cover_image

        result = _fetch_cover_image(cover_url)
        if result:
            img_bytes, media_type = result
            ext = "jpg" if "jpeg" in media_type else media_type.split("/")[-1]
            cover_path = output_dir / f"cover.{ext}"
            cover_path.write_bytes(img_bytes)

    # Build final M4B
    from .exporters import _safe_filename

    filename = f"{_safe_filename(story.title)} - {_safe_filename(story.author)}.m4b"
    m4b_path = output_dir / filename

    logger.info("Building M4B with %d chapters...", len(chapter_files))
    build_m4b(chapter_files, story, m4b_path, cover_path)

    # Clean up chapter MP3s and cover
    for cf in chapter_files:
        cf.unlink(missing_ok=True)
    if cover_path and cover_path.exists():
        cover_path.unlink()
    map_path.unlink(missing_ok=True)

    return m4b_path
