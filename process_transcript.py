import re


def parse_transcript(raw_text: str):
    """
    Parse Whisper diarization output into structured segments.
    Input looks like:
      [SPEAKER_01] [0.03 - 22.88]<br> Text...
    """
    pattern = re.compile(
        r"\[(SPEAKER_\d+)\]\s*\[(\d+\.\d+)\s*-\s*(\d+\.\d+)\]\s*(.+?)(?=\n?\[SPEAKER_|\Z)",
        re.DOTALL,
    )

    transcript = []
    cleaned_text = raw_text.replace("<br>", "\n")

    for match in pattern.finditer(cleaned_text):
        spk_id, start, end, seg_text = match.groups()
        transcript.append(
            {
                "speaker_id": spk_id,
                "start": float(start),
                "end": float(end),
                "text": seg_text.strip(),
            }
        )

    return transcript


if __name__ == "__main__":
    # Quick test
    sample = """[SPEAKER_01] [0.03 - 22.88]<br> Hello world.<br><br>[SPEAKER_00] [22.90 - 24.60]<br> Hi back."""
    result = parse_transcript(sample)
    from pprint import pprint

    pprint(result)
