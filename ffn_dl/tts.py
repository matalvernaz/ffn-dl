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

import edge_tts


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


# Match quoted speech — handles straight, curly, and mixed quote styles
_ANY_QUOTE = '[\"\u201c\u201d]'
_DIALOGUE_RE = re.compile(
    rf'{_ANY_QUOTE}(?P<speech>[^\"\u201c\u201d]{{5,}}){_ANY_QUOTE}'
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
_PROPER_TOKENS = r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"
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

# Common attribution verbs
_SPEECH_VERBS = {
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
    "ordered", "commanded", "barked", "scolded", "warned", "chided",
    "teased", "retorted", "countered", "responded", "intoned",
    "drawled", "mumbled", "complained", "whined", "grumbled",
    "gasped", "snorted", "scoffed", "huffed", "sneered", "spat",
    "pleaded", "begged", "prayed", "greeted", "crooned", "cooed",
    "lisped", "spluttered", "babbled", "squeaked", "squealed",
    "piped", "chirped", "quipped", "boasted", "bragged", "promised",
    "vowed", "swore", "confided", "admitted", "asserted", "argued",
    "cautioned", "prompted", "urged", "insisted", "reminded",
    "assured", "reassured", "soothed", "coaxed", "consoled",
    "reasoned", "clarified", "elaborated", "finished", "concluded",
    "agreed", "corrected", "apologized", "apologised",
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
_PROPER_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z']{1,}[a-z])\b")

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

    for match in _DIALOGUE_RE.finditer(text):
        # Narration before this dialogue
        pre = text[pos : match.start()].strip()
        if pre:
            segments.append(Segment(pre))

        speech = match.group("speech").strip()
        speaker = None
        emotion = None
        attrib_end = match.end()  # will advance past attribution text

        # Try attribution after the quote: "dialogue," Name verbed
        after_text = text[match.end() : match.end() + 80]

        def _resolve_pronoun():
            """When attribution uses a pronoun, find the nearest name in
            the preceding narration text. Returns a full titled name
            when the match is preceded by an honorific ("Mrs. Weasley",
            "Professor McGonagall") so speakers are not split into a
            spurious "Weasley" character."""
            window = text[max(0, match.start() - 200) : match.start()]
            # Find all (start_offset, name) tuples
            matches = [(m.start(), m.group(1)) for m in _PROPER_NAME_RE.finditer(window)]
            skip = {"The", "This", "That", "But", "And", "She", "His",
                    "Her", "They", "Then", "When", "What", "How", "Not",
                    # Common vocatives inside dialogue that get
                    # mistaken for narrator-side proper nouns:
                    "Boys", "Girls", "Children", "Kids", "Gentlemen",
                    "Ladies", "Everyone", "Anyone", "Someone", "Nobody",
                    "Friends", "Folks", "Lads", "Lasses", "Guys",
                    "Yeah", "Well", "Oh", "Hey", "Hi", "Hello"}
            # Drop honorifics and common non-name words
            candidates = [
                (pos, n) for pos, n in matches
                if n not in skip and n not in _NAME_SKIP_TITLES
            ]
            if not candidates:
                return last_speaker
            pos, name = candidates[-1]
            # If a title word immediately precedes this name, include it.
            preceding = window[max(0, pos - 20):pos].rstrip()
            for title in _NAME_SKIP_TITLES:
                # Match "Mrs.", "Mr ", "Professor ", "Aunt ", etc.
                if preceding.endswith(title) or preceding.endswith(title + "."):
                    return f"{title} {name}"
            return name

        am = _AFTER_NAME_VERB.match(after_text)
        if am and am.group("verb").lower() in _SPEECH_VERBS:
            name = am.group("name")
            verb = am.group("verb").lower()
            if name.lower() not in _PRONOUNS:
                speaker = name
            else:
                speaker = _resolve_pronoun()
            emotion = EMOTION_MAP.get(verb)
            attrib_end = match.end() + am.end()

        if not speaker:
            am = _AFTER_VERB_NAME.match(after_text)
            if am and am.group("verb").lower() in _SPEECH_VERBS:
                name = am.group("name")
                verb = am.group("verb").lower()
                if name.lower() not in _PRONOUNS:
                    speaker = name
                else:
                    speaker = _resolve_pronoun()
                emotion = EMOTION_MAP.get(verb)
                attrib_end = match.end() + am.end()

        if not speaker:
            before_text = text[max(0, match.start() - 80) : match.start()]
            bm = _BEFORE_ATTRIB.search(before_text)
            if bm and bm.group("verb").lower() in _SPEECH_VERBS:
                speaker = bm.group("name")
                emotion = EMOTION_MAP.get(bm.group("verb").lower())

        if speaker:
            last_speaker = speaker

        segments.append(Segment(speech, speaker=speaker, emotion=emotion))
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


async def _generate_segment_audio(segment, voice, output_path):
    """Generate audio for a single segment using edge-tts."""
    text = segment.text.strip()
    # Skip fragments too short to be meaningful speech
    if not text or len(text) < 3 or text.strip(".,;:!?-–—' \"") == "":
        return False

    kwargs = {"voice": voice}

    # Apply prosody adjustments for emotional delivery
    if segment.emotion:
        prosody = EMOTION_PROSODY.get(segment.emotion, {})
        kwargs.update(prosody)

    comm = edge_tts.Communicate(text, **kwargs)
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


async def _generate_with_semaphore(sem, seg, voice, path, idx, ch_num):
    """Generate one segment with a concurrency limiter (retries up to 3 times)."""
    async with sem:
        for attempt in range(1, 4):
            try:
                ok = await _generate_segment_audio(seg, voice, path)
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


async def generate_chapter_audio(segments, voice_mapper, output_path, chapter_num=0, narrator_voice=None):
    """Generate audio for a full chapter's worth of segments."""
    narrator = narrator_voice or NARRATOR_VOICE
    segments = _merge_small_segments(segments)

    tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-tts-"))
    sem = asyncio.Semaphore(_TTS_CONCURRENCY)

    # Launch all segment TTS calls concurrently (bounded by semaphore)
    tasks = []
    for i, seg in enumerate(segments):
        if not seg.text:
            continue
        voice = voice_mapper.get(seg.speaker, narrator) if seg.speaker else narrator
        seg_path = tmp_dir / f"seg_{i:06d}.mp3"
        tasks.append((i, seg_path, _generate_with_semaphore(
            sem, seg, voice, seg_path, i, chapter_num
        )))

    results = await asyncio.gather(*(t[2] for t in tasks))

    # Collect successful files in order
    segment_files = [r for r in results if r is not None]

    if not segment_files:
        return False

    # Merge segments into one chapter file using ffmpeg
    list_file = tmp_dir / "segments.txt"
    with open(list_file, "w") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

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


def build_m4b(chapter_files, story, output_path, cover_path=None):
    """Merge per-chapter MP3s into a single M4B with chapter markers."""
    if not chapter_files:
        return None

    tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-m4b-"))

    # Build ffmpeg concat list
    list_file = tmp_dir / "chapters.txt"
    with open(list_file, "w") as f:
        for cf in chapter_files:
            f.write(f"file '{cf}'\n")

    # First pass: merge all MP3s into one
    merged = tmp_dir / "merged.mp3"
    subprocess.run(
        [
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(merged),
        ],
        capture_output=True,
        check=True,
    )

    # Get chapter durations for metadata
    chapters_meta = tmp_dir / "chapters_meta.txt"
    with open(chapters_meta, "w") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={story.title}\n")
        f.write(f"artist={story.author}\n")
        f.write(f"album={story.title}\n")
        f.write(f"genre=Audiobook\n\n")

        offset_ms = 0
        for i, cf in enumerate(chapter_files):
            # Get duration using ffprobe
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
            f.write(f"title={ch_title}\n\n")
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
        "-c:a", "aac", "-b:a", "64k",  # 64k is fine for speech
        "-movflags", "+faststart",
        str(output_path),
    ])

    subprocess.run(cmd, capture_output=True, check=True)

    # Clean up
    import shutil as _shutil
    _shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path


# ── Main entry point ──────────────────────────────────────────────


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


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


def generate_audiobook(story, output_dir, progress_callback=None, narrator_voice=None):
    """Generate an M4B audiobook from a Story with character voice mapping.

    narrator_voice overrides the default NARRATOR_VOICE constant.
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

    # Count character mentions across all chapters
    char_counts = Counter()
    for segs in all_segments:
        for seg in segs:
            if seg.speaker:
                char_counts[seg.speaker] += 1

    # Only assign voices to characters with 2+ dialogue instances
    characters = [name for name, count in char_counts.most_common() if count >= 2]
    genders = detect_character_genders(full_text, characters)

    logger.info("Detected %d speaking characters", len(characters))
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
            generate_chapter_audio(segs, mapper, ch_path, chapter_num=i, narrator_voice=narrator)
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
