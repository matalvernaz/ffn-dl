"""Text-to-speech audiobook generation with character voice mapping."""

import asyncio
import hashlib
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

from .exporters import html_to_text, strip_note_paragraphs
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


def _balance_quotes(text):
    """Strip unbalanced stray quotes that confuse dialogue pairing.

    A single typo like ``leave."`` with no opener (real example: FFN
    13985352 ch.2) shifts every subsequent dialogue/narration pair
    by one quote, so narration ends up tagged as speech and vice-versa
    for the rest of the chapter. This pre-pass classifies each quote
    as opener or closer from its neighbors and drops orphans before
    the dialogue regex runs.
    """
    chars = list(text)
    inside = False
    for i, c in enumerate(chars):
        if c == '\u201c':
            kind = 'open'
        elif c == '\u201d':
            kind = 'close'
        elif c == '"':
            prev = chars[i - 1] if i > 0 else ' '
            nxt = chars[i + 1] if i + 1 < len(chars) else ' '
            looks_open = (
                prev.isspace() and not nxt.isspace()
                and nxt not in ('"', '\u201c', '\u201d')
            )
            looks_close = (
                (not prev.isspace())
                and (nxt.isspace() or nxt in '.,;:!?)]}>' or i == len(chars) - 1)
            )
            if looks_open and not looks_close:
                kind = 'open'
            elif looks_close and not looks_open:
                kind = 'close'
            else:
                kind = 'close' if inside else 'open'
        else:
            continue
        if not inside:
            if kind == 'close':
                # Stray closer with no opener — drop it.
                chars[i] = ''
                continue
            inside = True
        else:
            if kind == 'open':
                # New opener while still inside a quote — previous open
                # never got closed. Drop this orphan; the existing open
                # state will pair with the next true closer.
                chars[i] = ''
                continue
            inside = False
    return ''.join(chars)

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
# Optional adverb between name and verb (or verb and name). Lowercase
# only so proper nouns ending in -ly ("Sally", "Riley", "Holly") aren't
# swallowed as manner adverbs and stripped from attribution.
_ADVERB_OPT = r"(?:[a-z]+ly\s+)?"
_AFTER_NAME_VERB = re.compile(rf"\s*{_NAME_PAT}\s+{_ADVERB_OPT}(?P<verb>\w+)")
_AFTER_VERB_NAME = re.compile(
    rf"\s*{_ADVERB_OPT}(?P<verb>\w+)\s+{_NAME_PAT}"
    r"(?:\s|[.,;!?])"  # require word boundary after name
)

# Before an opening quote: Name verbed, "dialogue"
_BEFORE_ATTRIB = re.compile(
    r"(?P<name>"
    rf"(?:{_TITLE_PREFIX})?{_PROPER_TOKENS}"
    r")"
    rf'\s+{_ADVERB_OPT}(?P<verb>\w+)\s*,\s*$'
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
    "cautioned", "reminded", "advised", "counseled", "counselled",
    "encouraged", "lectured", "reproached", "admonished",
    "assured", "reassured", "soothed", "coaxed", "consoled",
    "reasoned", "clarified", "elaborated", "finished", "concluded",
    "corrected", "apologized", "apologised",
    # Fanfic-style verbs — non-verbal actions commonly paired with a
    # quote to attribute it. We accept these to avoid losing speakers
    # like "…" Lee pressed, "…" Harry nodded.
    "nodded", "shook",  # "he shook his head"
    "grinned", "smirked", "smiled", "beamed", "frowned", "grimaced",
    "scowled", "pouted", "blinked", "shrugged", "gestured",
    "nodded", "glared", "motioned",
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

    def __init__(self, text, speaker=None, emotion=None, scene_break=False):
        self.text = "" if scene_break else text.strip()
        self.speaker = speaker  # None = narrator
        self.emotion = emotion  # SSML style name or None
        # Marker segments don't carry audio of their own — the chapter
        # stitcher substitutes a longer silence clip at their position.
        self.scene_break = scene_break


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
    # Month / day / holiday names
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
    "Halloween", "Christmas", "Easter", "Hanukkah", "Diwali",
    "Yule", "Samhain", "Beltane", "Imbolc",
    # Sentence-start prepositions / connectives capitalised mid-narration
    "Behind", "Beside", "Beneath", "Underneath", "Above", "Below",
    "Across", "Among", "Amongst", "Around", "Between", "Beyond",
    "Inside", "Outside", "Toward", "Towards", "Against", "Along",
    "Onto", "Upon", "Within", "Throughout", "Through",
    "Without", "Despite", "Although", "Though", "Whereas",
    # Common interjections / fanfic narration starters
    "Blimey", "Bloody", "Merlin", "Magic", "Magicals", "Mage", "Magical",
    "Earth", "Heaven", "Hell", "God", "Lord",  # vocatives, not characters
    "Box", "Trolley", "Trunk", "Wand", "Owl", "Cake", "Tea",
    "Breathe", "Breath", "Sigh", "Pause",
    "Thank", "Thanks", "Sorry", "Please",
    "Principle", "Aspect", "Charm", "Spell", "Hex", "Jinx", "Curse",
    # Suspect dangling possessives that often start lines
    "I'll", "I'd", "I've", "I'm", "Let", "Let's", "You'll", "You'd",
    "You've", "You're", "We'll", "We'd", "We've", "We're",
    "He'll", "He'd", "He's", "She'll", "She'd", "She's",
    "They'll", "They'd", "They've", "They're", "It'll", "It's",
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


def _collect_confirmed_speakers(text):
    """Pre-scan the text for names attributed via explicit speech verbs.

    Returns a set containing both full attribution strings and their
    individual capitalized tokens. The pre-action heuristic later
    filters its candidate names against this set so that capitalized
    common nouns ("Halloween", "Reluctantly", "Magicals", "Behind")
    are rejected when they happen to fall in the action beat before
    a quote.
    """
    confirmed = set()

    def _add(name):
        cleaned = _strip_possessive((name or '').strip().rstrip(",.;:!?"))
        if not cleaned:
            return
        if cleaned in _SENTENCE_STARTERS or cleaned in _NAME_SKIP_TITLES:
            return
        if cleaned.lower() in _PRONOUNS:
            return
        confirmed.add(cleaned)
        for tok in cleaned.split():
            tok_clean = _strip_possessive(tok.rstrip(",.;:!?'\u2019"))
            if (
                tok_clean
                and tok_clean[0].isupper()
                and tok_clean not in _SENTENCE_STARTERS
                and tok_clean not in _NAME_SKIP_TITLES
            ):
                confirmed.add(tok_clean)

    for m in _DIALOGUE_RE.finditer(text):
        after = text[m.end() : m.end() + 80]
        am = _AFTER_NAME_VERB.match(after)
        if am and am.group("verb").lower() in _SPEECH_VERBS:
            _add(am.group("name"))
        am = _AFTER_VERB_NAME.match(after)
        if am and am.group("verb").lower() in _SPEECH_VERBS:
            _add(am.group("name"))
        before = text[max(0, m.start() - 80) : m.start()]
        bm = _BEFORE_ATTRIB.search(before)
        if bm and bm.group("verb").lower() in _SPEECH_VERBS:
            _add(bm.group("name"))
    return confirmed


def parse_segments(text):
    """Split story text into narration and dialogue segments.

    Tracks the last identified speaker so that pronoun-only attribution
    ("she said") and unattributed dialogue in a back-and-forth exchange
    carry forward correctly.
    """
    text = _balance_quotes(text)
    confirmed_speakers = _collect_confirmed_speakers(text)

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

        # Soft post-attribution: "Name verbed [object]" with a non-speech
        # action verb. Accept only when Name is a confirmed speaker
        # elsewhere in the chapter — catches "Sirius ran his hands over
        # his face." and "Hermione motioned to the timid-looking boy."
        # without opening the door to every capitalized common noun.
        if not speaker:
            am = _AFTER_NAME_VERB.match(after_text)
            if am:
                raw_name = am.group("name")
                if raw_name and raw_name.lower() not in _PRONOUNS:
                    cleaned = _clean_speaker(raw_name)
                    if cleaned and cleaned in confirmed_speakers:
                        speaker = cleaned
                        attrib_end = match.end() + am.end()

        # Pre-action attribution — "Ron looked up. 'Trouble?'" — a
        # very common fanfic pattern where the speaker is named in
        # the immediately-preceding narration but without a speech
        # verb. If only one name appears in the action beat, use it.
        # If several do, fall back to the *last sentence's subject*:
        # "Harry grinned at Ron. 'Hi.'" resolves to Harry, not Ron,
        # because the subject of the sentence the quote follows is
        # almost always the speaker in fanfic prose.
        if not speaker:
            pre_text = text[pos : match.start()]
            stripped = pre_text.strip()
            # Skip pre-action when the orphan tail of a previous post-
            # attribution starts mid-sentence ("…cradling Harry."): the
            # only name there is almost always the OBJECT of the prior
            # speaker's action, not the next speaker.
            starts_lower = bool(stripped) and stripped[0].islower()
            if 0 < len(stripped) <= 200 and not starts_lower:
                def _clean_unique(hay):
                    out = []
                    for n in _PROPER_NAME_RE.findall(hay):
                        n = _strip_possessive(n)
                        if n in _SENTENCE_STARTERS or n in _NAME_SKIP_TITLES:
                            continue
                        # Adverbs sneak through the proper-noun regex when
                        # capitalised at sentence start ("Reluctantly,
                        # Sirius passed…"). Drop -ly tokens unless the
                        # name was confirmed elsewhere.
                        if n.endswith("ly") and n not in confirmed_speakers:
                            continue
                        if n not in out:
                            out.append(n)
                    # When at least one confirmed speaker is present,
                    # restrict to confirmed candidates so capitalised
                    # common nouns (Halloween, Magicals, Box, …) don't
                    # win the pre-action lottery.
                    if confirmed_speakers:
                        confirmed_only = [
                            n for n in out if n in confirmed_speakers
                        ]
                        if confirmed_only:
                            return confirmed_only
                    return out

                def _apply_title(hay, candidate):
                    idx = hay.rfind(candidate)
                    if idx < 0:
                        return candidate
                    preceding = hay[max(0, idx - 20):idx].rstrip()
                    for title in _NAME_SKIP_TITLES:
                        if preceding.endswith(title) or preceding.endswith(title + "."):
                            return f"{title} {candidate}"
                    return candidate

                clean_names = _clean_unique(stripped)
                if len(clean_names) == 1:
                    speaker = _apply_title(stripped, clean_names[0])
                elif len(clean_names) >= 2:
                    sentences = [
                        s.strip()
                        for s in re.split(r"(?<=[.!?])\s+", stripped)
                        if s.strip()
                    ]
                    if sentences:
                        last_sent = sentences[-1]
                        last_names = _clean_unique(last_sent)
                        if last_names:
                            # First name in the last sentence ≈ subject
                            speaker = _apply_title(last_sent, last_names[0])

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
                # Possessives count as "other names present" so an action
                # beat like "…on his cousin Andromeda's home." doesn't
                # silently keep Sirius as speaker when the next line is
                # actually Andromeda. The original strict-non-possessive
                # filter was too generous on long orphan-tail gaps.
                long_orphan_tail = (
                    non_ws > 60 and stripped[:1].islower()
                )
                other_names = [
                    n for n in _PROPER_NAME_RE.findall(stripped)
                    if _strip_possessive(n) not in _SENTENCE_STARTERS
                    and _strip_possessive(n) not in _NAME_SKIP_TITLES
                    and _strip_possessive(n) != last_first
                    and _strip_possessive(n) != last_tail
                    and (long_orphan_tail or not _is_possessive(n))
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
    """Assigns and persists character → voice mappings.

    The mapper picks each character a voice from the candidate pool —
    by default the legacy edge-only ``MALE_VOICES`` / ``FEMALE_VOICES``
    constants (so behavior matches pre-2.2.0 unless callers opt in),
    but ``set_voice_pool`` lets the audiobook generator install a
    richer per-character pool built from the user's enabled providers,
    accent map, and (when LLM analysis ran) character-profile metadata.

    Voice ids written into the map JSON are now namespaced as
    ``provider:short_name``. Pre-2.2.0 maps with bare short_names are
    auto-prefixed with ``edge:`` on read so existing per-story
    mappings keep resolving.
    """

    def __init__(self, map_path=None):
        self.map_path = Path(map_path) if map_path else None
        self.mapping = {}  # character name → voice id ("provider:short_name")
        self._male_idx = 0
        self._female_idx = 0
        self._neutral_idx = 0
        # Default candidate lists — fed by the legacy edge-only
        # constants. ``set_voice_pool`` overrides to provide the
        # per-character pools the multi-provider / accent-aware
        # audiobook pipeline computes.
        self._fallback_male = [_namespace_legacy(v) for v in MALE_VOICES]
        self._fallback_female = [_namespace_legacy(v) for v in FEMALE_VOICES]
        self._fallback_neutral = [_namespace_legacy(v) for v in NEUTRAL_VOICES]
        self._per_character_pool: dict[str, list[str]] = {}
        self._per_character_idx: dict[str, int] = {}
        if self.map_path and self.map_path.exists():
            try:
                raw = json.loads(self.map_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Voice map unreadable (%s); ignoring", exc)
                raw = {}
            self.mapping = {k: _namespace_legacy(v) for k, v in raw.items()}
            logger.info("Loaded voice map with %d characters", len(self.mapping))

    def save(self):
        if self.map_path:
            self.map_path.parent.mkdir(parents=True, exist_ok=True)
            self.map_path.write_text(
                json.dumps(self.mapping, indent=2), encoding="utf-8"
            )

    def set_voice_pool(self, per_character: dict[str, list[str]]):
        """Install a per-character voice pool — VoiceMapper consults
        these lists first before falling back to the gender-based
        defaults. Each list is the gender / accent / locale-filtered
        candidate set for that character; we round-robin across it so
        repeated characters in the same fic don't collide on voice.
        Voice ids inside the pool may be bare or namespaced; both are
        normalised to the namespaced form."""
        self._per_character_pool = {
            name: [_namespace_legacy(v) for v in voices]
            for name, voices in per_character.items()
            if voices
        }
        self._per_character_idx = {name: 0 for name in self._per_character_pool}

    def assign(self, name, gender="neutral"):
        existing = self.mapping.get(name)
        if existing:
            return existing

        pool = self._per_character_pool.get(name)
        if pool:
            idx = self._per_character_idx.get(name, 0)
            voice = pool[idx % len(pool)]
            self._per_character_idx[name] = idx + 1
        elif gender == "male":
            voice = self._fallback_male[self._male_idx % len(self._fallback_male)]
            self._male_idx += 1
        elif gender == "female":
            voice = self._fallback_female[
                self._female_idx % len(self._fallback_female)
            ]
            self._female_idx += 1
        else:
            voice = self._fallback_neutral[
                self._neutral_idx % len(self._fallback_neutral)
            ]
            self._neutral_idx += 1

        # Don't assign the narrator voice to a character.
        narrator_ns = _namespace_legacy(NARRATOR_VOICE)
        if voice == narrator_ns and (pool or self._fallback_male):
            return self.assign(name, gender)

        self.mapping[name] = voice
        return voice

    def get(self, name, default=None):
        narrator = _namespace_legacy(default or NARRATOR_VOICE)
        return self.mapping.get(name, narrator)


def _namespace_legacy(voice: str) -> str:
    """Add a ``edge:`` prefix to a bare short_name. Legacy voice maps
    written before 2.2.0 have bare names; the provider dispatcher only
    accepts namespaced ids. Already-namespaced ids pass through."""
    if not voice or ":" in voice:
        return voice
    return f"edge:{voice}"


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


def _tts_kwargs_for_segment(segment, voice, speech_rate=0):
    """Build the edge-tts Communicate kwargs for a segment.

    Shared between the real synthesis path and the failure-diagnostic
    logger so both see exactly the same parameters.
    """
    kwargs = {"voice": voice}
    emotion_prosody = {}
    if segment.emotion:
        emotion_prosody = EMOTION_PROSODY.get(segment.emotion, {})
    rate = _combine_rate(speech_rate, emotion_prosody.get("rate"))
    if rate:
        kwargs["rate"] = rate
    for key in ("volume", "pitch"):
        if key in emotion_prosody:
            kwargs[key] = emotion_prosody[key]
    return kwargs


async def _generate_segment_audio(segment, voice, output_path, speech_rate=0):
    """Generate audio for a single segment via the chosen TTS provider.

    speech_rate is an integer percent delta applied on top of any
    emotion-driven rate adjustment. The voice id may be either a bare
    edge short_name (legacy, pre-2.2.0 voice maps) or the namespaced
    ``provider:short_name`` form — ``tts_providers.synthesize`` handles
    both.
    """
    text = segment.text.strip()
    # Skip fragments too short to be meaningful speech
    if not text or len(text) < 3 or text.strip(".,;:!?-–—' \"") == "":
        return False

    kwargs = _tts_kwargs_for_segment(segment, voice, speech_rate=speech_rate)
    from . import tts_providers

    voice_arg = kwargs.pop("voice", voice)
    # The provider dispatcher is sync; running it inside an async
    # function keeps the call-site shape unchanged (the audiobook
    # generator awaits this coroutine in a gather) but pushes the
    # actual subprocess / network work onto a worker thread so we
    # don't block the asyncio loop the audio generator runs on.
    await asyncio.to_thread(
        tts_providers.synthesize,
        voice_arg, text, output_path,
        rate=kwargs.get("rate"),
        volume=kwargs.get("volume"),
        pitch=kwargs.get("pitch"),
    )
    return True


# Upper bound on the text length handed to edge-tts per call. Above
# roughly this size edge-tts silently returns empty audio ("No audio was
# received") instead of synthesising. 2000 chars sits comfortably below
# that ceiling while still letting the chapter stitcher produce
# naturally-paced narration spans — smaller caps splinter long prose
# into choppy fragment joins.
_MAX_SEGMENT_CHARS = 2000

# Sentence-terminator split: matches whitespace after .!? while keeping
# the terminator with the preceding sentence. Used to split oversized
# narrator segments at natural boundaries.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_oversized_text(text, max_len=_MAX_SEGMENT_CHARS):
    """Split a string at sentence boundaries so no piece exceeds
    ``max_len``. Falls back to a hard whitespace split for pathologically
    long sentences so edge-tts still gets a bounded payload."""
    text = text.strip()
    if len(text) <= max_len:
        return [text]

    parts = []
    buf = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not sentence:
            continue
        # Pathological sentence (> max_len on its own): hard-wrap on
        # word boundaries rather than letting it through oversized.
        if len(sentence) > max_len:
            if buf:
                parts.append(buf)
                buf = ""
            words = sentence.split(" ")
            chunk = ""
            for w in words:
                if chunk and len(chunk) + 1 + len(w) > max_len:
                    parts.append(chunk)
                    chunk = w
                else:
                    chunk = f"{chunk} {w}" if chunk else w
            if chunk:
                buf = chunk
            continue
        if buf and len(buf) + 1 + len(sentence) > max_len:
            parts.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}" if buf else sentence
    if buf:
        parts.append(buf)
    return parts


def _merge_small_segments(segments, min_len=30):
    """Merge short segments to reduce API calls and avoid errors.

    Pass 1: merge adjacent narrator segments (bounded by
    ``_MAX_SEGMENT_CHARS`` so giant prose blocks don't fuse into a
    single payload edge-tts will reject).
    Pass 2: absorb very short narrator fragments into the nearest
    narrator segment (also bounded).
    Pass 3: drop anything that's just punctuation.
    Pass 4: split any remaining oversized segment at sentence
    boundaries — covers segments that arrived already too big from
    upstream parsing.
    """
    if not segments:
        return []

    # Pass 1: merge adjacent narration (bounded)
    merged = []
    for seg in segments:
        if seg.scene_break:
            merged.append(seg)
            continue
        if not seg.text:
            continue
        can_merge = (
            merged
            and not merged[-1].scene_break
            and seg.speaker is None
            and merged[-1].speaker is None
            and len(merged[-1].text) + 1 + len(seg.text) <= _MAX_SEGMENT_CHARS
        )
        if can_merge:
            merged[-1].text += " " + seg.text
        else:
            merged.append(Segment(seg.text, seg.speaker, seg.emotion))

    # Pass 2: absorb tiny narrator fragments (< min_len) into neighbors
    cleaned = []
    for i, seg in enumerate(merged):
        if seg.scene_break:
            cleaned.append(seg)
            continue
        if seg.speaker is None and len(seg.text) < min_len:
            # Try to append to the previous narrator segment (but not
            # across a scene-break marker — that would glue two scenes
            # together and lose the pause). Respect the same size cap
            # as Pass 1.
            for prev in reversed(cleaned):
                if prev.scene_break:
                    break
                if prev.speaker is None:
                    if len(prev.text) + 1 + len(seg.text) <= _MAX_SEGMENT_CHARS:
                        prev.text += " " + seg.text
                        break
                    # Previous narrator is already at the cap — keep
                    # this fragment standalone rather than blowing past
                    # the limit.
                    cleaned.append(seg)
                    break
            else:
                # No previous narrator — keep it, it'll merge forward later
                cleaned.append(seg)
        else:
            cleaned.append(seg)

    # Pass 3: drop empty / punctuation-only segments, but preserve
    # scene-break markers.
    filtered = [
        s for s in cleaned
        if s.scene_break or s.text.strip(".,;:!?-–—' \"")
    ]

    # Pass 4: split any segment still over the cap at sentence
    # boundaries. Covers oversized inputs (rare) and belt-and-braces
    # against earlier passes.
    result = []
    for seg in filtered:
        if seg.scene_break or len(seg.text) <= _MAX_SEGMENT_CHARS:
            result.append(seg)
            continue
        for piece in _split_oversized_text(seg.text):
            result.append(Segment(piece, speaker=seg.speaker, emotion=seg.emotion))
    return result


# Max concurrent edge-tts API calls per chapter
_TTS_CONCURRENCY = 5

# Pause inserted between segments when the speaker changes. Makes
# multi-voice playback sound less like a rushed relay handoff.
_SPEAKER_CHANGE_PAUSE_MS = 400

# Longer pause substituted for <hr/> scene breaks so listeners get a
# recognisable beat between scenes instead of a synthesised "asterisk
# asterisk asterisk".
_SCENE_BREAK_PAUSE_MS = 1500

# Sentinel placed in chapter text where a <hr/> scene break appeared.
# Uses U+241E (SYMBOL FOR INFORMATION SEPARATOR TWO) — outside the range
# of anything a scraper would emit, so it survives parse_segments cleanly.
_SCENE_BREAK_MARKER = "\u241e"

# Characters that can appear in a decorative scene-break line. Fanfic
# authors invent every variation imaginable — ``---``, ``===``, ``* * *``,
# ``~~~``, ``###``, ``oOo``, ``xXx``, ``o0o``, ``•••``, em-dash runs —
# so the detector is permissive. Letter ornaments are limited to
# ``oOxX0`` since those are the only letter-shaped chars routinely used
# as dividers; broader alphabetic matches would trip on short real words.
_SCENE_BREAK_DECO_CHARS = set(
    "-=_~*#+.,;:!?/\\|"
    " \t"
    "oOxX0"
    "•·×"
    "★☆♦♠♥♣♢♤♡♧"
    "‡†§❦❧✦✧❖⟡"
    "⋆⸺⸻—–‒"
)

# A lone ellipsis paragraph is usually dramatic pause prose, not a
# divider; excluding it avoids inserting silence for "..." beats.
_ELLIPSIS_ONLY_RE = re.compile(r"^[\.…\s]+$")


def _is_scene_break_line(text):
    """Detect a line composed entirely of decorative/divider characters.

    Matches ``---``, ``===``, ``* * *``, ``~~~``, ``###``, ``oOo``,
    ``xXx``, ``o0o``, em-dash runs, and long runs like
    ``-x-x-x-x-x-...`` that some FFN authors stretch to 60-80
    characters as a visual barrier.

    Length handling splits by character class:

    * Symbol-containing lines (anything outside ``oOxX0`` + whitespace)
      are divider-by-construction no matter how long — real prose
      can't consist solely of punctuation.
    * Pure ornamental-letter lines (only ``oOxX0`` + whitespace) stay
      capped at 40 chars and require a mixed-case or zero-digit
      pattern, so short real words like "ox" or a rating label "OOO"
      don't trigger.
    """
    s = text.strip()
    if len(s) < 3:
        return False
    if _ELLIPSIS_ONLY_RE.match(s):
        return False
    if not all(c in _SCENE_BREAK_DECO_CHARS for c in s):
        return False
    # Line contains a non-letter symbol (``---``, ``* * *``, ``###``,
    # ``-x-x-x-``, etc.) — unambiguously a divider.
    if any(c not in "oOxX0 \t" for c in s):
        return True
    if len(s) > 40:
        return False
    # Pure ornamental-letter line: accept only distinctive patterns —
    # mixed case (``oOo``, ``xXx``, ``ooOoo``), containing a digit 0
    # (``o0o``), or a pure-uppercase X run (``XXX`` / ``XXXX`` /
    # ``X X X``). The last one is included because ``XXX`` is
    # overwhelmingly used as a scene break in fanfic; ``OOO`` is
    # deliberately still excluded since it's ambiguous with rating
    # labels, and lowercase ``ooo`` / ``xxx`` stay excluded as prose
    # affection/laugh markers.
    has_lower = any(c in "ox" for c in s)
    has_upper = any(c in "OX" for c in s)
    has_zero = "0" in s
    if (has_lower and has_upper) or has_zero:
        return True
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 3 and all(c == "X" for c in letters):
        return True
    return False


def _normalize_scene_break_lines(text):
    """Scan a block of text and replace any line that consists solely of
    decorative scene-break characters with the scene-break marker."""
    if not text:
        return text
    lines = text.split("\n")
    changed = False
    for i, line in enumerate(lines):
        if _is_scene_break_line(line):
            lines[i] = _SCENE_BREAK_MARKER
            changed = True
    return "\n".join(lines) if changed else text


def _llm_strip_an_paragraphs(text: str, llm_config: dict | None) -> str:
    """Backstop pass: when ``--strip-notes`` is on AND the LLM
    attribution backend is configured, send every top-level paragraph
    of the post-regex chapter text to the LLM for an A/N decision and
    drop the flagged ones.

    Runs after ``strip_note_paragraphs`` so the regex catches the easy
    80% (explicit ``A/N:`` labels, structural pre/post-divider blocks)
    without burning LLM tokens. The LLM is the second pass that picks
    up the disguised cases — outros that don't reach the structural
    keyword gate, shout-outs in the middle of a chapter, etc.
    """
    if not text or not llm_config:
        return text
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return text
    from . import attribution

    flagged = attribution.classify_authors_notes_via_llm(
        paragraphs, llm_config=llm_config,
    )
    if not flagged:
        return text
    kept = [p for i, p in enumerate(paragraphs) if i not in flagged]
    return "\n\n".join(kept)


def _html_to_audiobook_text(html, strip_notes=False, hr_as_stars=False):
    """HTML → plain text tuned for TTS, gated on the user-facing flags.

    When ``strip_notes`` is set, paragraph-level author's notes are
    removed before synthesis. When ``hr_as_stars`` is set, every scene
    divider — real ``<hr/>`` tags plus text-based dividers (``---``,
    ``* * *``, ``oOo``, etc.) — is replaced with a scene-break sentinel
    so the chapter stitcher can insert a silence pause instead of
    synthesising the divider as literal speech.

    With both flags off, this degrades to the legacy ``html_to_text``
    behaviour: A/Ns are read aloud and ``<hr/>`` becomes "* * *" (which
    edge-tts reads as "asterisk asterisk asterisk"). Listeners who
    actually want that can opt in by leaving both checkboxes clear.
    """
    from bs4 import BeautifulSoup, NavigableString, Tag

    cleaned = strip_note_paragraphs(html) if strip_notes else html
    soup = BeautifulSoup(cleaned, "html.parser")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    parts = []
    for child in soup.children:
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if not text:
                continue
            if hr_as_stars:
                text = _normalize_scene_break_lines(text)
            parts.append(text)
        elif isinstance(child, Tag):
            if child.name == "hr":
                parts.append(_SCENE_BREAK_MARKER if hr_as_stars else "* * *")
                continue
            text = child.get_text().strip()
            if not text:
                continue
            if hr_as_stars:
                text = _normalize_scene_break_lines(text)
            parts.append(text)

    return "\n\n".join(parts)


def _segment_chapter_text(text):
    """Run parse_segments over each scene of a chapter, separated by the
    scene-break marker, and splice a scene-break Segment between scenes
    so the audio stitcher can insert a pause."""
    if _SCENE_BREAK_MARKER not in text:
        return parse_segments(text)
    out = []
    scenes = text.split(_SCENE_BREAK_MARKER)
    for i, scene in enumerate(scenes):
        scene = scene.strip()
        if scene:
            out.extend(parse_segments(scene))
        if i < len(scenes) - 1:
            out.append(Segment("", scene_break=True))
    return out


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


def _build_voice_pool(
    *, characters: list[str],
    genders: dict[str, str],
    profiles: dict[str, dict],
    accents: dict[str, str],
    enabled_providers: list[str] | None,
    narrator_voice: str,
) -> dict[str, list[str]]:
    """Build the per-character candidate voice list the VoiceMapper
    rotates through.

    Layered prior, strongest first:

    1. Per-character accent map override (``en-GB``, ``fr-FR``, ...).
    2. LLM-derived profile accent (when present).
    3. No accent filter — use the whole pool of the right gender.

    Gender comes from the profile when available, falling back to the
    name-based heuristic. The narrator voice is excluded from every
    pool so a character can't accidentally collide with the narrator.

    The returned dict maps character name → ordered list of namespaced
    voice ids (``"edge:en-GB-RyanNeural"``, ``"piper:en_GB-alan-medium"``,
    ...). VoiceMapper.assign rotates through the list when picking.
    """
    from . import tts_providers

    catalog = tts_providers.all_voices(providers=enabled_providers)
    if not catalog:
        return {}

    by_id = {v.id: v for v in catalog}
    narrator_id = _namespace_legacy(narrator_voice)
    narrator_info = by_id.get(narrator_id)

    def _gender_filter(target_gender: str):
        def _ok(v):
            if narrator_info is not None and v.id == narrator_info.id:
                return False
            if target_gender == "male" and v.gender.lower() != "male":
                return False
            if target_gender == "female" and v.gender.lower() != "female":
                return False
            return True
        return _ok

    out: dict[str, list[str]] = {}
    for name in characters:
        profile = profiles.get(name) or {}
        gender = (
            profile.get("gender")
            or genders.get(name)
            or "neutral"
        ).lower()
        accent = accents.get(name) or profile.get("accent") or "any"
        accent_lc = accent.lower()
        accent_lang = accent_lc.split("-", 1)[0] if "-" in accent_lc else accent_lc

        gender_ok = _gender_filter(gender)

        # Three-tier preference: exact locale > language > any locale.
        # Take the best non-empty tier so a single en-GB pick beats a
        # noisy mix of en-US fallbacks polluting the rotation.
        if accent_lc in ("any", ""):
            candidates = [v for v in catalog if gender_ok(v)]
        else:
            tier_exact = [
                v for v in catalog
                if gender_ok(v) and v.locale.lower() == accent_lc
            ]
            tier_language = [
                v for v in catalog
                if gender_ok(v) and v.language.lower() == accent_lang
            ]
            if tier_exact:
                candidates = tier_exact
            elif tier_language:
                logger.info(
                    "Voice pool: no %s voices in exact locale %s for %s "
                    "— using language-level fallback (%s)",
                    gender, accent_lc, name, accent_lang,
                )
                candidates = tier_language
            else:
                logger.info(
                    "Voice pool: no %s voices match accent %s for %s "
                    "— falling back to any locale",
                    gender, accent_lc, name,
                )
                candidates = [v for v in catalog if gender_ok(v)]

        if candidates:
            out[name] = [v.id for v in candidates]
    return out


# Forward type alias only used in the helper above; avoids a TYPE_CHECKING
# block while keeping the annotation readable.
VoiceInfo_t = "tts_providers.VoiceInfo"


async def _generate_with_semaphore(
    sem, seg, voice, path, idx, ch_num, speech_rate=0, narrator_voice=None,
):
    """Generate one segment with a concurrency limiter.

    Three attempts with progressive fallback — edge-tts can reproducibly
    reject a specific text+voice+emotion combo ("No audio was received"),
    so plain retries with identical parameters just burn the budget.
    Each attempt strips one more suspected culprit so a segment that
    fails at full config still has a chance of going through:

      1. Full kwargs — assigned character voice + emotion prosody.
      2. Emotion prosody stripped — same voice, user speech_rate only.
         Covers the common case where a rate/pitch/volume combo pushes
         edge-tts past what that specific voice supports.
      3. Narrator voice — last-ditch different voice + plain kwargs.
         The listener hears the line in the wrong voice rather than a
         silent gap, which is the tradeoff users consistently prefer.
    """
    fallback_voice = narrator_voice or NARRATOR_VOICE
    seg_no_emotion = (
        Segment(seg.text, speaker=seg.speaker, emotion=None,
                scene_break=seg.scene_break)
        if seg.emotion else seg
    )
    attempts = (
        ("full", voice, seg),
        ("no-emotion", voice, seg_no_emotion),
        ("narrator-fallback", fallback_voice, seg_no_emotion),
    )
    async with sem:
        for attempt, (label, try_voice, try_seg) in enumerate(attempts, 1):
            try:
                ok = await _generate_segment_audio(
                    try_seg, try_voice, path, speech_rate=speech_rate,
                )
                if ok and path.exists() and path.stat().st_size > 0:
                    if attempt > 1:
                        logger.info(
                            "TTS segment %d (ch %d) recovered on attempt %d "
                            "(%s, voice=%s)",
                            idx, ch_num, attempt, label, try_voice,
                        )
                    return path
            except Exception as exc:
                kwargs = _tts_kwargs_for_segment(
                    try_seg, try_voice, speech_rate=speech_rate,
                )
                text_preview = seg.text[:200]
                if attempt < len(attempts):
                    logger.debug(
                        "TTS attempt %d/%d (%s) failed for segment %d (ch %d): %s | "
                        "speaker=%s emotion=%s kwargs=%s text=%r",
                        attempt, len(attempts), label, idx, ch_num, exc,
                        seg.speaker, seg.emotion, kwargs, text_preview,
                    )
                    await asyncio.sleep(2)
                else:
                    logger.warning(
                        "TTS failed after %d attempts for segment %d (ch %d): %s | "
                        "speaker=%s emotion=%s last_kwargs=%s text_len=%d text=%r",
                        len(attempts), idx, ch_num, exc,
                        seg.speaker, seg.emotion, kwargs, len(seg.text), text_preview,
                    )
    return None


async def generate_chapter_audio(
    segments, voice_mapper, output_path,
    chapter_num=0, narrator_voice=None, speech_rate=0,
):
    """Generate audio for a full chapter's worth of segments.

    Produces chapter *body* audio only — the per-chapter spoken heading
    ("Chapter N. Title") is synthesised separately by the caller and
    concatenated at assembly time. Keeping the heading out of the body
    keeps chapter-body caching content-addressed on prose alone, so a
    retitled chapter doesn't invalidate thousands of synthesised
    segments.
    """
    narrator = narrator_voice or NARRATOR_VOICE
    segments = _merge_small_segments(segments)

    tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-tts-"))
    sem = asyncio.Semaphore(_TTS_CONCURRENCY)

    # Pre-generate a silence clip inserted at speaker boundaries so the
    # multi-voice playback isn't a breathless relay.
    silence_clip = _make_silence_clip(tmp_dir, _SPEAKER_CHANGE_PAUSE_MS / 1000)
    # Longer clip substituted for <hr/> scene breaks so the listener
    # actually hears a beat between scenes.
    scene_break_clip = _make_silence_clip(tmp_dir, _SCENE_BREAK_PAUSE_MS / 1000)

    # Launch all segment TTS calls concurrently (bounded by semaphore),
    # preserving the speaker for each so we can detect voice changes
    # when stitching the chapter together. Scene-break segments are not
    # synthesised — they become a silence clip inserted in-place.
    tasks = []
    plan = []  # [(kind, speaker, task_index_or_None)], preserves order
    for i, seg in enumerate(segments):
        if seg.scene_break:
            plan.append(("scene_break", None, None))
            continue
        if not seg.text:
            continue
        voice = voice_mapper.get(seg.speaker, narrator) if seg.speaker else narrator
        seg_path = tmp_dir / f"seg_{i:06d}.mp3"
        task_idx = len(tasks)
        tasks.append((i, seg_path, seg.speaker, _generate_with_semaphore(
            sem, seg, voice, seg_path, i, chapter_num,
            speech_rate=speech_rate, narrator_voice=narrator,
        )))
        plan.append(("speech", seg.speaker, task_idx))

    results = await asyncio.gather(*(t[3] for t in tasks))

    # Build the ordered playback sequence, dropping failed TTS segments
    # but keeping scene-break pauses.
    ordered = []  # [(kind, speaker, path_or_None)]
    for kind, speaker, task_idx in plan:
        if kind == "scene_break":
            if scene_break_clip is not None:
                ordered.append(("scene_break", None, scene_break_clip))
            continue
        r = results[task_idx]
        if r is not None:
            ordered.append(("speech", speaker, r))

    # Drop leading/trailing scene-break pauses — a chapter that opens or
    # closes on silence sounds like a bug, not a beat.
    while ordered and ordered[0][0] == "scene_break":
        ordered.pop(0)
    while ordered and ordered[-1][0] == "scene_break":
        ordered.pop()

    if not any(kind == "speech" for kind, _, _ in ordered):
        return False

    # Merge segments into one chapter file using ffmpeg, inserting the
    # speaker-change silence clip between consecutive segments whose
    # speakers differ, and the scene-break clip at each marker.
    list_file = tmp_dir / "segments.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        prev_speaker = None
        first = True
        last_was_scene_break = False
        for kind, speaker, sf in ordered:
            if kind == "scene_break":
                f.write(_concat_entry(sf))
                last_was_scene_break = True
                # Don't reset prev_speaker — the scene-break pause
                # already covers the speaker-change gap, so the first
                # speech segment after it shouldn't get another pause
                # stacked on top.
                continue
            if (
                silence_clip is not None
                and not first
                and not last_was_scene_break
                and speaker != prev_speaker
            ):
                f.write(_concat_entry(silence_clip))
            f.write(_concat_entry(sf))
            prev_speaker = speaker
            first = False
            last_was_scene_break = False

    result = subprocess.run(
        [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    # Clean up temp dir
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if result.returncode != 0:
        tail = _decode_stderr(result.stderr).strip()[-400:]
        logger.warning("ffmpeg concat failed for ch %d: %s", chapter_num, tail)
        # Dump the concat list so the user can see what ffmpeg was asked
        # to stitch. Real failures here ("please specify the format
        # manually") often turn out to be a zero-byte segment or a bad
        # silence clip; without the list it's impossible to tell which
        # file tripped the concat demuxer.
        if logger.isEnabledFor(logging.DEBUG):
            try:
                logger.debug(
                    "ffmpeg concat list for ch %d:\n%s",
                    chapter_num, list_file.read_text(encoding="utf-8"),
                )
            except OSError:
                pass
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


# Map known fanfic-host suffixes to TTS-friendly display names for the
# audiobook intro line. Keys are matched as suffixes against the parsed
# netloc, so both bare and "www." hosts hit the same entry.
_SITE_DISPLAY_NAMES = {
    "archiveofourown.org": "Archive of Our Own",
    "fanfiction.net": "fanfiction dot net",
    "fictionpress.com": "fictionpress dot com",
    "royalroad.com": "Royal Road",
    "wattpad.com": "Wattpad",
    "literotica.com": "Literotica",
    "mediaminer.org": "Media Miner",
    "ficwad.com": "Fic Wad",
}


def _site_display_name(url):
    """TTS-friendly name for the source site behind ``url``.

    Falls back to the bare host (minus ``www.``) so an unknown site
    still produces a speakable phrase rather than being omitted.
    """
    from urllib.parse import urlparse

    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for suffix, name in _SITE_DISPLAY_NAMES.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return host or "the web"


# Structural labels that name their own place in the book. When the
# author's chapter title *is* one of these (optionally with a
# subtitle, e.g. "Prologue: Before the Fall"), we read it verbatim —
# "Chapter 1. Prologue" sounds wrong.
_STRUCTURAL_LABELS = (
    "prologue", "prelude", "preface", "foreword", "introduction",
    "epilogue", "afterword", "postscript", "coda",
    "interlude", "intermission", "intermezzo",
    "author's note", "authors note", "a/n",
)


def _format_chapter_heading(n, title):
    """Build the spoken heading for chapter ``n`` given its raw title.

    Handles the common fanfic cases: a missing/generic title collapses
    to ``Chapter N``; a title that already names itself a chapter or
    a structural label (Prologue, Epilogue, Interlude, …) is read
    verbatim; otherwise the number is prepended so the listener always
    hears a clear chapter marker.
    """
    title = (title or "").strip()
    if not title:
        return f"Chapter {n}"
    if re.match(r"^chapter\b", title, re.I):
        return title
    # Structural labels — verbatim, optionally with a ": subtitle".
    low = title.lower()
    for label in _STRUCTURAL_LABELS:
        if low == label or low.startswith(label + ":") or low.startswith(label + " -"):
            return title
    # Pure-number titles ("1", "1.", "1 -") — upgrade to "Chapter N".
    if re.match(r"^\d+\s*[.\-:)]*\s*$", title):
        return f"Chapter {n}"
    return f"Chapter {n}. {title}"


async def _synthesize_heading(text, voice, output_path, speech_rate=0):
    """Synthesize a single narrator line (intro / chapter heading).

    Uses a stripped-down Segment so emotion prosody and speech-rate
    handling stay consistent with the per-segment pipeline, but without
    the attribution/pronunciation machinery that applies to prose.
    """
    seg = Segment(text)
    return await _generate_segment_audio(
        seg, voice, output_path, speech_rate=speech_rate,
    )


def _decode_stderr(value) -> str:
    """Return ``value`` as a str, robust to subprocess returning bytes.

    ``subprocess.run(..., text=True)`` is *supposed* to decode stderr for
    us, but on some frozen Windows builds it hands back a ``bytes`` object
    instead — when that slips through, ``%s`` formats as ``b'...'`` and
    the real error message is masked behind a stringified bytes repr that
    truncates the actual tail. Decoding defensively here keeps the
    warning-path output readable no matter what subprocess returned.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _concat_entry(path) -> str:
    """Format a path for an ffmpeg concat-demuxer list file. Resolves to
    an absolute POSIX-style path: the concat demuxer parses single-quoted
    values with escape-sequence semantics, so a Windows `C:\\Users\\...\\Temp\\ffn-tts-x`
    gets `\\t`/`\\n`/etc. interpreted and the file lookup fails. Forward
    slashes are accepted by ffmpeg on both platforms.
    """
    s = str(Path(path).resolve()).replace("\\", "/")
    s = s.replace("'", "'\\''")
    return f"file '{s}'\n"


def _run_ffmpeg(cmd, *, step):
    """Run an ffmpeg/ffprobe invocation and surface stderr on failure.
    The default `subprocess.run(check=True, capture_output=True)` raises
    CalledProcessError with the ffmpeg message hidden in `.stderr`; we
    want that in the user's face so audiobook errors are debuggable.
    """
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = _decode_stderr(result.stderr).strip().splitlines()[-20:]
        message = "\n".join(tail) or "(no ffmpeg stderr)"
        raise RuntimeError(
            f"ffmpeg failed during {step} (exit {result.returncode}):\n{message}"
        )
    return result


def build_m4b(chapter_files, story, output_path, cover_path=None, intro_file=None):
    """Merge per-chapter MP3s into a single M4B with chapter markers.

    ``intro_file``, if given, is concatenated before the first chapter
    and gets its own "Introduction" chapter marker so listeners can
    skip back to the attribution without scrubbing.
    """
    if not chapter_files:
        return None

    tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-m4b-"))

    # Build ffmpeg concat list. Paths must be absolute: ffmpeg resolves
    # `file` entries relative to the list file's own directory, so a bare
    # "ch_0001.mp3" here would be looked up inside tmp_dir.
    list_file = tmp_dir / "chapters.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        if intro_file and Path(intro_file).exists():
            f.write(_concat_entry(intro_file))
        for cf in chapter_files:
            f.write(_concat_entry(cf))

    # First pass: merge all MP3s into one
    merged = tmp_dir / "merged.mp3"
    _run_ffmpeg(
        [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(merged),
        ],
        step="concat",
    )

    def _probe_ms(path):
        probe = subprocess.run(
            [
                FFPROBE, "-v", "quiet", "-show_entries",
                "format=duration", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            text=True,
        )
        return int(float(probe.stdout.strip() or "0") * 1000)

    # Get chapter durations for metadata
    chapters_meta = tmp_dir / "chapters_meta.txt"
    with open(chapters_meta, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={_escape_ffmeta(story.title)}\n")
        f.write(f"artist={_escape_ffmeta(story.author)}\n")
        f.write(f"album={_escape_ffmeta(story.title)}\n")
        f.write("genre=Audiobook\n\n")

        offset_ms = 0
        if intro_file and Path(intro_file).exists():
            intro_ms = _probe_ms(intro_file)
            if intro_ms > 0:
                f.write("[CHAPTER]\n")
                f.write("TIMEBASE=1/1000\n")
                f.write(f"START={offset_ms}\n")
                f.write(f"END={offset_ms + intro_ms}\n")
                f.write("title=Introduction\n\n")
                offset_ms += intro_ms

        for i, cf in enumerate(chapter_files):
            duration_ms = _probe_ms(cf)
            ch_title = story.chapters[i].title if i < len(story.chapters) else f"Chapter {i + 1}"

            f.write("[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={offset_ms}\n")
            f.write(f"END={offset_ms + duration_ms}\n")
            f.write(f"title={_escape_ffmeta(ch_title)}\n\n")
            offset_ms += duration_ms

    # Convert to M4B (AAC in M4A container) with chapter metadata.
    # All -i inputs must come before output options like -map_metadata;
    # otherwise ffmpeg rejects the cover -i as "input option on output file".
    cmd = [
        FFMPEG, "-y",
        "-i", str(merged),
        "-i", str(chapters_meta),
    ]
    if cover_path and Path(cover_path).exists():
        cmd.extend(["-i", str(cover_path)])
        # -c:v mjpeg forces JPEG-encoded cover art; without it the ipod
        # muxer defaults to libx264, which refuses odd-dimension images
        # (e.g. a 75x100 webp thumbnail) and aborts the whole mux.
        cmd.extend(["-map_metadata", "1", "-map", "0:a", "-map", "2:v",
                     "-c:v", "mjpeg",
                     "-disposition:v", "attached_pic"])
    else:
        cmd.extend(["-map_metadata", "1"])
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

def detect_voices(story, map_path=None, strip_notes=False, hr_as_stars=False):
    """Run the character + voice pipeline on a Story without synthesising.

    Returns a list of {"name", "gender", "voice"} dicts in frequency order
    (most-mentioned speakers first). Existing mappings in map_path are
    preserved; newly-seen characters are assigned a voice and written back
    on save. ``strip_notes`` / ``hr_as_stars`` match the audiobook flags
    so preview numbers line up with what the listener will actually hear.
    """
    mapper = VoiceMapper(map_path)

    full_text = ""
    all_segments = []
    for ch in story.chapters:
        text = _html_to_audiobook_text(ch.html, strip_notes=strip_notes, hr_as_stars=hr_as_stars)
        full_text += text + "\n"
        all_segments.append(_segment_chapter_text(text))

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


def synthesize_sample(voice, text, output_path):
    """Synthesize a short preview clip to output_path (MP3) via the
    voice's provider. Accepts both legacy bare voice names and the
    ``provider:short_name`` form."""
    from . import tts_providers

    tts_providers.synthesize(voice, text, Path(output_path))
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


# ── Attribution cache ─────────────────────────────────────────────
#
# BookNLP attribution takes minutes per book and is deterministic in the
# chapter text + backend + model-size. Caching per-chapter (hash of the
# chapter text as key) means:
#   * Re-runs after a TTS crash skip BookNLP entirely for every chapter
#     whose text hasn't changed.
#   * Fanfics that grow by one chapter only pay attribution cost for the
#     new chapter; existing chapters hit cache.
#   * A chapter edited by the author (typo fix, rewrite) is naturally
#     re-attributed because its hash no longer matches.
#
# The cache lives under ``<portable_root>/cache/attribution/`` — the
# same ``cache/`` folder that already holds HuggingFace downloads and
# chapter cache, so everything reusable across runs sits in one place.
# Two people who download the same story share attribution results
# across different output directories. One file per chapter
# (content-addressed by sha256) means concurrent renders never collide
# and a single torn write only loses one chapter.
_ATTR_CACHE_VERSION = 1


def _attr_cache_root():
    """Shared attribution-cache directory, independent of output dir."""
    from . import portable
    return portable.cache_dir() / "attribution" / f"v{_ATTR_CACHE_VERSION}"


def _attr_cache_entry_path(backend, model_size, text_hash):
    size_bucket = model_size if model_size else "_"
    return _attr_cache_root() / backend / size_bucket / f"{text_hash}.json"


def _hash_chapter_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_character_list(story):
    """Return the per-story character list as a list of strings.

    Each scraper merges the inner ``extra`` dict directly onto
    ``Story.metadata`` (see e.g. ``FFNScraper.download``,
    ``AO3Scraper.download``), so the cast lives at
    ``story.metadata["characters"]`` regardless of site. The string
    is comma-separated for FFN/FicWad/AO3; we also split on "/" so
    pairing-style entries ("Harry/Ginny") yield two names.
    """
    metadata = getattr(story, "metadata", None) or {}
    raw = metadata.get("characters") if isinstance(metadata, dict) else None
    if not raw or not isinstance(raw, str):
        return []
    parts: list[str] = []
    for chunk in raw.split(","):
        for sub in chunk.split("/"):
            name = sub.strip()
            if name:
                parts.append(name)
    return parts


def _segment_to_dict(seg):
    return {
        "text": seg.text,
        "speaker": seg.speaker,
        "emotion": seg.emotion,
        "scene_break": seg.scene_break,
    }


def _segment_from_dict(d):
    # Segment.__init__ zeroes text when scene_break=True, so pass the
    # original text through and let it do the right thing.
    return Segment(
        d.get("text", ""),
        speaker=d.get("speaker"),
        emotion=d.get("emotion"),
        scene_break=bool(d.get("scene_break", False)),
    )


def _load_attr_entry(backend, model_size, text_hash):
    """Return the cached segment dict-list for this chapter, or None."""
    path = _attr_cache_entry_path(backend, model_size, text_hash)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Attribution cache entry unreadable (%s); ignoring", exc)
        return None
    if not isinstance(data, list):
        return None
    return data


def _save_attr_entry(backend, model_size, text_hash, segment_dicts):
    """Atomically write one chapter's attribution result to the shared cache."""
    path = _attr_cache_entry_path(backend, model_size, text_hash)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(segment_dicts), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write attribution cache entry: %s", exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except (OSError, NameError):
            pass


# ── Chapter audio cache ───────────────────────────────────────────
#
# Per-chapter TTS is the single most expensive step in the pipeline —
# a 44-chapter book is thousands of Edge-TTS round trips, each subject
# to sporadic 503s, and a failed M4B mux at the end used to mean
# re-synthesising the whole book on the next run. We content-address
# the chapter *body* audio on (segments + voice assignments + narrator
# + rate) so re-runs after any downstream failure, and re-runs with a
# different cover or different chapter titles, reuse the expensive
# synthesis. Headings are deliberately excluded from the key: they're
# cheap to regenerate and were also the source of an earlier bug where
# cached chapters silently lacked the spoken title.
_CHAPTER_CACHE_VERSION = 1


def _chapter_cache_root(story_id):
    """Per-story cache dir for chapter body MP3s."""
    from . import portable

    safe_id = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(story_id or "anon"))[:64]
    return (
        portable.cache_dir()
        / "chapter_audio"
        / f"v{_CHAPTER_CACHE_VERSION}"
        / safe_id
    )


def _chapter_cache_key(segs, voice_mapper, narrator, speech_rate):
    """Content-address the chapter body audio.

    Must cover every input that affects the rendered WAV bytes: the
    ordered segment texts/speakers/emotions/scene-breaks, the voice
    each speaker resolves to in this story, the narrator fallback, and
    the speech rate. Pronunciation overrides don't need their own
    fingerprint — they're applied to ``seg.text`` before this runs.
    """
    speakers = sorted({s.speaker for s in segs if s.speaker})
    voice_fp = {sp: voice_mapper.get(sp, narrator) for sp in speakers}
    blob = json.dumps(
        {
            "v": _CHAPTER_CACHE_VERSION,
            "segs": [
                [s.text, s.speaker, s.emotion, bool(s.scene_break)]
                for s in segs
            ],
            "voices": voice_fp,
            "narrator": narrator,
            "rate": speech_rate,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _concat_mp3s(inputs, output):
    """Lossless concat of MP3 inputs into ``output`` using the concat demuxer."""
    inputs = [Path(p) for p in inputs if p and Path(p).exists()]
    if not inputs:
        return False
    if len(inputs) == 1:
        shutil.copyfile(inputs[0], output)
        return True
    tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-concat-"))
    try:
        list_file = tmp_dir / "list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for p in inputs:
                f.write(_concat_entry(p))
        result = subprocess.run(
            [
                FFMPEG, "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_file), "-c", "copy", str(output),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "ffmpeg concat failed (%s); stderr tail: %s",
                result.returncode, (result.stderr or "").strip()[-300:],
            )
            return False
        return True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def generate_audiobook(
    story, output_dir,
    progress_callback=None,
    narrator_voice=None,
    speech_rate=0,
    attribution_backend="builtin",
    attribution_model_size=None,
    attribution_llm_config=None,
    enabled_tts_providers=None,
    strip_notes=False,
    hr_as_stars=False,
):
    """Generate an M4B audiobook from a Story with character voice mapping.

    narrator_voice overrides the default NARRATOR_VOICE constant.
    speech_rate is an integer percent delta (-50..+100 sensible range)
    applied to every TTS synthesis call on top of any emotion prosody.
    attribution_backend selects the speaker-attribution refinement pass:
    "builtin" (regex only), "fastcoref", "booknlp", or "llm". Unknown
    or uninstalled backends silently fall back to builtin.
    attribution_model_size picks a size variant for backends that
    expose one (BookNLP: "small" or "big"; ignored otherwise).
    attribution_llm_config is required when attribution_backend=="llm":
    a dict with keys ``provider``, ``model``, ``api_key``, ``endpoint``.
    strip_notes, when True, drops paragraph-level author's notes before
    synthesis (listeners who want A/Ns read aloud can leave it off).
    hr_as_stars, when True, replaces every scene divider (<hr/> plus
    text-based patterns like ``---``, ``* * *``, ``oOo``) with a 1.5s
    silence clip so the chapter stitcher inserts a real beat instead of
    synthesising the divider as literal speech.
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

    # LLM pronunciation seeding — only fires on first render of a
    # story (existing user-edited maps are never overwritten). Uses
    # the metadata cast list + the first chunks of chapter text to
    # identify made-up names, fandom terms, foreign words.
    if (
        not pronunciation_map
        and attribution_backend == "llm"
        and attribution_llm_config
    ):
        from . import character_profile

        seed_text = "\n\n".join(
            ch.html for ch in story.chapters[:2]
        )[:40000]
        seeded_pron = character_profile.seed_pronunciations_via_llm(
            character_list=_extract_character_list(story),
            full_text=seed_text,
            llm_config=attribution_llm_config,
        )
        if seeded_pron:
            existing = {}
            try:
                existing = json.loads(pron_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            for key, val in seeded_pron.items():
                existing.setdefault(key, val)
            pron_path.write_text(
                json.dumps(existing, indent=2) + "\n", encoding="utf-8",
            )
            pronunciation_map = _load_pronunciation_map(pron_path)
            logger.info(
                "LLM seeded %d pronunciation overrides", len(seeded_pron),
            )

    if pronunciation_map:
        logger.info("Loaded %d pronunciation overrides", len(pronunciation_map))

    # Gather full text for gender detection — honours the caller's
    # strip-notes / hr-as-stars preferences so A/Ns and dividers are
    # handled consistently with what the listener will hear.
    use_llm_an = (
        strip_notes
        and attribution_backend == "llm"
        and bool(attribution_llm_config)
    )
    full_text = ""
    chapter_texts = []
    for ch in story.chapters:
        text = _html_to_audiobook_text(
            ch.html, strip_notes=strip_notes, hr_as_stars=hr_as_stars,
        )
        if use_llm_an:
            # Backstop: regex-based strip_note_paragraphs may have left
            # disguised A/Ns (mid-chapter outros, beta thanks dressed as
            # prose). Ask the LLM to flag any remaining ones.
            text = _llm_strip_an_paragraphs(text, attribution_llm_config)
        chapter_texts.append(text)
        full_text += text + "\n"

    # Parse all segments to find character names, honouring scene breaks.
    all_segments = []
    for text in chapter_texts:
        segs = _segment_chapter_text(text)
        all_segments.append(segs)

    # Pull the metadata-derived character list (AO3 character tags,
    # FFN's third bare segment, FicWad's story-characters span) and
    # use it as a closed-world prior for both the LLM prompt and
    # heuristic post-refinement.
    character_list = _extract_character_list(story)

    # Optional neural refinement pass — replaces / augments the regex
    # speaker attribution. Silently no-ops if the backend isn't
    # installed, so the render never fails on a missing dep.
    if attribution_backend and attribution_backend != "builtin":
        from . import attribution

        # The cache key encodes (backend, size_bucket, chapter_hash).
        # For the LLM backend the (provider, model) pair determines
        # the answer, so encode that into the size_bucket slot.
        if attribution_backend == "llm" and attribution_llm_config:
            cache_size_bucket = attribution.llm_cache_token(
                attribution_llm_config.get("provider", ""),
                attribution_llm_config.get("model", ""),
            )
        else:
            cache_size_bucket = attribution_model_size

        hits = 0
        misses = 0
        for idx, (text, segs) in enumerate(zip(chapter_texts, all_segments)):
            key = _hash_chapter_text(text)
            cached = _load_attr_entry(
                attribution_backend, cache_size_bucket, key,
            )
            if cached is not None:
                all_segments[idx] = [_segment_from_dict(d) for d in cached]
                hits += 1
                continue
            refined = attribution.refine_speakers(
                segs, text,
                backend=attribution_backend,
                model_size=attribution_model_size,
                character_list=character_list,
                llm_config=attribution_llm_config,
            )
            all_segments[idx] = refined
            # Only persist when the backend actually ran. On fallback
            # (backend missing or raised) refine_speakers returns the
            # unrefined builtin segments — saving those under the
            # requested backend's cache key would make the next render
            # see a "cache hit" and skip the real refinement forever.
            if attribution.has_failed(
                attribution_backend, attribution_model_size,
            ):
                misses += 1
                continue
            # Persist immediately so an interrupted run preserves
            # per-chapter progress — re-running won't repeat completed
            # attribution work.
            _save_attr_entry(
                attribution_backend, cache_size_bucket, key,
                [_segment_to_dict(s) for s in refined],
            )
            misses += 1
        if hits or misses:
            logger.info(
                "Attribution cache: %d hit%s, %d miss%s (%s)",
                hits, "" if hits == 1 else "es",
                misses, "" if misses == 1 else "es",
                _attr_cache_root(),
            )

    # Backend-agnostic post-attribution refinement — runs on both
    # builtin and neural output (including cache-loaded segments), so
    # rebuilding audio from an existing attribution cache still picks
    # up the latest self-intro / junk-speaker passes.
    from . import attribution as _post_attribution
    _post_attribution.post_refine(all_segments, character_list=character_list)

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

    # Accent + profile maps live next to the audiobook output. The
    # accent map is the user's hand-editable override; the profile
    # map is whatever the LLM most-recently classified. Both survive
    # re-renders so edits aren't clobbered. Only seed when empty.
    from . import accent_map as _accent_map
    accents_path = output_dir / f".ffn-accents-{story.id}.json"
    profile_path = output_dir / f".ffn-profile-{story.id}.json"
    accents = _accent_map.filter_user_entries(_accent_map.load_accents(accents_path))
    profiles = _accent_map.filter_user_entries(_accent_map.load_profiles(profile_path))

    if (
        attribution_backend == "llm"
        and attribution_llm_config
        and characters
        and (not profiles or not accents)
    ):
        from . import character_profile

        candidate_names = list(characters)
        if character_list:
            for n in character_list:
                if n not in candidate_names:
                    candidate_names.append(n)
        seeded = character_profile.seed_profiles_via_llm(
            character_list=candidate_names,
            full_text=full_text,
            llm_config=attribution_llm_config,
        )
        if seeded:
            # Only fill in keys the user hasn't already set — never
            # clobber a hand-edited entry.
            for name, profile in seeded.items():
                profiles.setdefault(name, profile)
            _accent_map.save_profiles(profile_path, profiles)
            for name, accent in character_profile.derive_accents_from_profiles(
                seeded
            ).items():
                accents.setdefault(name, accent)
            _accent_map.save_accents(accents_path, accents)
            logger.info(
                "LLM character-profile seeder filled %d profile entries",
                len(seeded),
            )

        # Narrator voice suggestion — only fires when the caller hasn't
        # forced ``narrator_voice``. The LLM picks gender + accent that
        # match the story's tone; we translate that into a real voice id
        # by filtering the live provider catalog.
        if not narrator_voice:
            narrator_profile = character_profile.suggest_narrator_via_llm(
                full_text=full_text,
                llm_config=attribution_llm_config,
            )
            if narrator_profile:
                picked = character_profile.pick_narrator_voice_for_profile(
                    profile=narrator_profile,
                    enabled_providers=enabled_tts_providers,
                    fallback=narrator,
                )
                if picked and picked != narrator:
                    logger.info(
                        "LLM narrator-voice suggestion: %s (%s — %s)",
                        picked,
                        narrator_profile.get("accent") or "any",
                        narrator_profile.get("rationale") or "",
                    )
                    narrator = picked

    if not accents_path.exists():
        _accent_map.save_accents(accents_path, accents)
    if not profile_path.exists():
        _accent_map.save_profiles(profile_path, profiles)

    pool = _build_voice_pool(
        characters=characters,
        genders=genders,
        profiles=profiles,
        accents=accents,
        enabled_providers=enabled_tts_providers,
        narrator_voice=narrator,
    )
    if pool:
        mapper.set_voice_pool(pool)

    logger.info("Detected %d speaking characters (merged from %d raw)",
                len(characters), len(raw_char_counts))
    for name in characters[:15]:
        profile = profiles.get(name) or {}
        gender = profile.get("gender") or genders.get(name) or "neutral"
        voice = mapper.assign(name, gender)
        accent = accents.get(name) or profile.get("accent") or "any"
        logger.info("  %s (%s, %s) → %s", name, gender, accent, voice)

    mapper.save()

    # Generate audio for each chapter.
    #
    # Body (prose) and heading ("Chapter N. Title") are produced and
    # cached separately:
    #   * Body is expensive (hundreds of TTS calls) and content-addressed
    #     in the persistent cache under portable.cache_dir(), so re-runs
    #     after a failed mux or cover download skip re-synthesis.
    #   * Heading is cheap (one TTS call) and regenerated per run —
    #     a title edit or a prior heading-synth failure can't poison
    #     future runs the way an inline-with-body cache would.
    cache_root = _chapter_cache_root(story.id)
    cache_root.mkdir(parents=True, exist_ok=True)
    build_tmp = Path(tempfile.mkdtemp(prefix="ffn-audiobook-", dir=output_dir))

    chapter_files = []
    total = len(story.chapters)
    cache_hits = 0
    cache_misses = 0

    for i, (ch, segs) in enumerate(zip(story.chapters, all_segments), 1):
        body_key = _chapter_cache_key(segs, mapper, narrator, speech_rate)
        body_path = cache_root / f"{body_key}.mp3"

        if body_path.exists() and body_path.stat().st_size > 0:
            cache_hits += 1
        else:
            # Keep the .mp3 extension last so ffmpeg can infer the output
            # format; a trailing ".tmp" confuses the muxer with "Invalid
            # argument". os.replace still handles the atomic swap.
            tmp_body = body_path.with_name(body_path.stem + ".tmp" + body_path.suffix)
            success = asyncio.run(
                generate_chapter_audio(
                    segs, mapper, tmp_body,
                    chapter_num=i, narrator_voice=narrator,
                    speech_rate=speech_rate,
                )
            )
            if success and tmp_body.exists() and tmp_body.stat().st_size > 0:
                os.replace(tmp_body, body_path)
                cache_misses += 1
            else:
                logger.warning("No audio generated for chapter %d", i)
                if tmp_body.exists():
                    tmp_body.unlink(missing_ok=True)
                if progress_callback:
                    progress_callback(i, total, ch.title)
                continue

        # Heading synth per run — if it fails we fall back to the body
        # alone so the chapter is still audible, just untitled.
        heading_text = _format_chapter_heading(i, ch.title)
        heading_path = build_tmp / f"heading_{i:04d}.mp3"
        try:
            heading_ok = asyncio.run(
                _synthesize_heading(
                    heading_text, narrator, heading_path,
                    speech_rate=speech_rate,
                )
            )
        except Exception as exc:
            logger.warning(
                "Chapter %d heading TTS failed (%s); chapter will play untitled",
                i, exc,
            )
            heading_ok = False

        assembled = build_tmp / f"ch_{i:04d}.mp3"
        if heading_ok and heading_path.exists() and heading_path.stat().st_size > 0:
            if not _concat_mp3s([heading_path, body_path], assembled):
                # Concat failed — fall back to body only.
                shutil.copyfile(body_path, assembled)
        else:
            shutil.copyfile(body_path, assembled)
        chapter_files.append(assembled)

        if progress_callback:
            progress_callback(i, total, ch.title)

    if not chapter_files:
        shutil.rmtree(build_tmp, ignore_errors=True)
        raise RuntimeError("No chapter audio was generated.")

    logger.info(
        "Chapter audio cache: %d hit%s, %d miss%s (%s)",
        cache_hits, "" if cache_hits == 1 else "s",
        cache_misses, "" if cache_misses == 1 else "es",
        cache_root,
    )

    # Download cover image for embedding
    cover_path = None
    cover_url = story.metadata.get("cover_url")
    if cover_url:
        from .exporters import _fetch_cover_image

        result = _fetch_cover_image(cover_url)
        if result:
            img_bytes, media_type = result
            ext = "jpg" if "jpeg" in media_type else media_type.split("/")[-1]
            cover_path = build_tmp / f"cover.{ext}"
            cover_path.write_bytes(img_bytes)

    # Synthesize a short attribution intro — "Title, by Author.
    # Downloaded from <site>." — so the opening of the audiobook names
    # what it is rather than dropping the listener straight into prose.
    intro_path = build_tmp / "intro.mp3"
    intro_text = (
        f"{story.title}, by {story.author}. "
        f"Downloaded from {_site_display_name(story.url)}."
    )
    try:
        intro_ok = asyncio.run(
            _synthesize_heading(intro_text, narrator, intro_path, speech_rate=speech_rate)
        )
        if not intro_ok or not intro_path.exists() or intro_path.stat().st_size == 0:
            intro_path = None
    except Exception as exc:
        logger.warning("Audiobook intro TTS failed (%s); continuing without it", exc)
        intro_path = None

    # Build final M4B
    from .exporters import _safe_filename

    filename = f"{_safe_filename(story.title)} - {_safe_filename(story.author)}.m4b"
    m4b_path = output_dir / filename

    logger.info("Building M4B with %d chapters...", len(chapter_files))
    try:
        build_m4b(chapter_files, story, m4b_path, cover_path, intro_file=intro_path)
    finally:
        # Per-run scratch (assembled chapter files, heading clips, intro,
        # cover) is disposable. The body-audio cache lives under
        # portable.cache_dir() and is deliberately left untouched so the
        # next run can reuse it.
        shutil.rmtree(build_tmp, ignore_errors=True)
        map_path.unlink(missing_ok=True)

    return m4b_path
