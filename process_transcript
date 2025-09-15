import re
import json

# Example: load STT response (replace with your service call result)
stt_output = {
    "text": """[SPEAKER_01] [0.03 - 22.88]<br> И тут происходит щелчок...
<br><br>[SPEAKER_00] [22.90 - 24.60]<br> Обязательное обучение.
<br><br>[SPEAKER_01] [25.34 - 34.85]<br> И поверьте, если мы сейчас не займемся этим...
"""
}

# Example speaker log from Playwright bot (timestamps + names)
speaker_log = [
    {"time": 5, "speaker": "Alice"},
    {"time": 25, "speaker": "Bob"},
    {"time": 40, "speaker": "Alice"},
]

def map_speaker_id_to_name(seg_start, diarization_id, speaker_log):
    """
    Map diarization speaker (SPEAKER_00, etc.) to closest Google Meet speaker name.
    """
    if not speaker_log:
        return diarization_id

    closest = min(speaker_log, key=lambda s: abs(s["time"] - seg_start))
    return closest["speaker"]

def parse_stt_output(stt_output, speaker_log):
    text = stt_output["text"].replace("<br>", "\n")

    # Match diarization segments: [SPEAKER_01] [0.03 - 22.88] text...
    pattern = re.compile(r"\[(SPEAKER_\d+)\]\s*\[(\d+\.\d+)\s*-\s*(\d+\.\d+)\]\s*(.+)", re.DOTALL)

    transcript = []
    for match in pattern.finditer(text):
        spk_id, start, end, seg_text = match.groups()
        start, end = float(start), float(end)

        speaker_name = map_speaker_id_to_name(start, spk_id, speaker_log)

        transcript.append({
            "speaker": speaker_name,
            "start": start,
            "end": end,
            "text": seg_text.strip()
        })

    return transcript

final_transcript = parse_stt_output(stt_output, speaker_log)

# Save JSON
with open("meeting_transcript.json", "w", encoding="utf-8") as f:
    json.dump(final_transcript, f, ensure_ascii=False, indent=2)

print("✅ Processed transcript saved: meeting_transcript.json")
