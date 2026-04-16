"""Text-to-speech audiobook generation with character voice mapping."""

import asyncio
import json
import logging
import re
import tempfile
from collections import Counter
from pathlib import Path

import edge_tts

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
# Name matches proper nouns OR pronouns (he/she/they/it)
_NAME_PAT = r"(?P<name>(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)|(?:he|she|they|it|He|She|They|It))"
_AFTER_NAME_VERB = re.compile(rf"\s*{_NAME_PAT}\s+(?P<verb>\w+)")
_AFTER_VERB_NAME = re.compile(
    rf"\s*(?P<verb>\w+)\s+{_NAME_PAT}"
    r"(?:\s|[.,;!?])"  # require word boundary after name
)

# Before an opening quote: Name verbed, "dialogue"
_BEFORE_ATTRIB = re.compile(
    r"(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
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
}


class Segment:
    """A piece of text to be spoken."""

    def __init__(self, text, speaker=None, emotion=None):
        self.text = text.strip()
        self.speaker = speaker  # None = narrator
        self.emotion = emotion  # SSML style name or None


_PRONOUNS = {"he", "she", "they", "it"}
_PROPER_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,})\b")


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
            the preceding narration text (more accurate than last_speaker)."""
            window = text[max(0, match.start() - 200) : match.start()]
            names = _PROPER_NAME_RE.findall(window)
            # Filter out common non-name words
            skip = {"The", "This", "That", "But", "And", "She", "His",
                    "Her", "They", "Then", "When", "What", "How", "Not"}
            names = [n for n in names if n not in skip]
            return names[-1] if names else last_speaker

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
_FEMALE_NAMES = {
    "hermione", "ginny", "luna", "fleur", "lily", "rose", "taylor",
    "alice", "claire", "eve", "grace", "iris", "ivy", "jane", "joy",
    "kate", "mae", "may", "faith", "hope", "dawn", "willow", "buffy",
    "joan", "ann", "beth", "ruth", "jean", "nell", "fern", "rachel",
    "lillian", "myrtle", "mrytle", "madison", "morgan", "arya", "sansa",
    "cersei", "daenerys", "misty", "susan", "sarah", "mary", "nancy",
    "helen", "karen", "wendy", "carol", "janet", "robin", "amber",
    "crystal", "heather", "brooke", "paige", "quinn", "skitter",
    "piper", "phoebe", "cordelia", "tara", "anya", "glory", "drusilla",
}
_MALE_NAMES = {
    "harry", "ron", "draco", "james", "albus", "sirius", "remus",
    "jack", "john", "max", "sam", "ben", "tom", "dan", "bob", "jim",
    "brian", "kevin", "mark", "paul", "peter", "sean", "adam", "carl",
    "dean", "eric", "greg", "hugh", "ian", "karl", "leon", "neil",
    "owen", "alan", "chad", "luke", "finn", "ross", "kurt", "seth",
    "michael", "micheal", "danny", "dumbledore", "snape", "neville",
    "fred", "george", "arthur", "bill", "charlie", "percy", "hagrid",
    "voldemort", "robert", "william", "richard", "edward", "henry",
    "charles", "david", "joseph", "george", "frank", "ray", "cole",
    "angel", "spike", "xander", "giles", "wesley", "gunn", "connor",
}


def _guess_gender_from_name(name):
    """Heuristic gender from first name patterns."""
    first = name.split()[0].lower()

    if first in _FEMALE_NAMES:
        return "female"
    if first in _MALE_NAMES:
        return "male"

    # Suffix heuristics
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

    def get(self, name):
        return self.mapping.get(name, NARRATOR_VOICE)


# ── Audio generation ──────────────────────────────────────────────


async def _generate_segment_audio(segment, voice, output_path):
    """Generate audio for a single segment using edge-tts."""
    text = segment.text
    if not text or len(text.strip()) < 2:
        return False

    kwargs = {"voice": voice}

    # Apply prosody adjustments for emotional delivery
    if segment.emotion:
        prosody = EMOTION_PROSODY.get(segment.emotion, {})
        kwargs.update(prosody)

    comm = edge_tts.Communicate(text, **kwargs)
    await comm.save(str(output_path))
    return True


async def generate_chapter_audio(segments, voice_mapper, output_path, chapter_num=0):
    """Generate audio for a full chapter's worth of segments."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-tts-"))
    segment_files = []

    for i, seg in enumerate(segments):
        if not seg.text:
            continue

        voice = voice_mapper.get(seg.speaker) if seg.speaker else NARRATOR_VOICE
        seg_path = tmp_dir / f"seg_{i:06d}.mp3"

        try:
            await _generate_segment_audio(seg, voice, seg_path)
            if seg_path.exists() and seg_path.stat().st_size > 0:
                segment_files.append(seg_path)
        except Exception as exc:
            logger.warning(
                "TTS failed for segment %d (ch %d): %s", i, chapter_num, exc
            )

    if not segment_files:
        return False

    # Merge segments into one chapter file using ffmpeg
    list_file = tmp_dir / "segments.txt"
    with open(list_file, "w") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

    import subprocess

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(output_path),
        ],
        capture_output=True,
    )

    # Clean up temp dir
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if result.returncode != 0:
        logger.warning("ffmpeg concat failed for ch %d: %s", chapter_num, result.stderr[:200])
        return False

    return True


def build_m4b(chapter_files, story, output_path, cover_path=None):
    """Merge per-chapter MP3s into a single M4B with chapter markers."""
    import subprocess

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
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
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
                    "ffprobe", "-v", "quiet", "-show_entries",
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
        "ffmpeg", "-y",
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
    merged.unlink(missing_ok=True)
    list_file.unlink(missing_ok=True)
    chapters_meta.unlink(missing_ok=True)
    tmp_dir.rmdir()

    return output_path


# ── Main entry point ──────────────────────────────────────────────


def generate_audiobook(story, output_dir, progress_callback=None):
    """Generate an M4B audiobook from a Story with character voice mapping.

    progress_callback(current_chapter, total_chapters, title) is called
    after each chapter is synthesized.
    """
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
            generate_chapter_audio(segs, mapper, ch_path, chapter_num=i)
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
