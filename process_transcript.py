import re

def parse_transcript(raw_text: str):
    """Parse Whisper diarization output into structured segments."""
    pattern = re.compile(
        r"\[(SPEAKER_\d+)\]\s*\[(\d+\.\d+)\s*-\s*(\d+\.\d+)\]\s*(.+?)(?=\n?\[SPEAKER_|\Z)",
        re.DOTALL
    )
    transcript = []
    for match in pattern.finditer(raw_text.replace("<br>", "\n")):
        spk_id, start, end, seg_text = match.groups()
        transcript.append({
            "speaker_id": spk_id,
            "start": float(start),
            "end": float(end),
            "text": seg_text.strip()
        })
    return transcript
