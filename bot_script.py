import os
import sys
import re
import time
import json
import asyncio
import subprocess
import requests
from playwright.async_api import async_playwright, TimeoutError
from process_transcript import parse_transcript

# --- CONFIGURATION ---
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else ""
MAX_MEETING_DURATION_SECONDS = 10800
OUTPUT_FILENAME = "meeting_audio.wav"
WHISPER_API_URL = "http://localhost:8000/v1/audio/transcriptions"  # your Whisper STT endpoint

def get_ffmpeg_command(platform, duration):
    """Returns the appropriate ffmpeg command based on the operating system."""
    if platform.startswith("linux"):
        return [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-t", str(duration), OUTPUT_FILENAME,
        ]
    elif platform == "darwin":  # macOS
        return [
            "ffmpeg", "-y", "-f", "avfoundation", "-i", ":BlackHole 2ch",
            "-t", str(duration), OUTPUT_FILENAME,
        ]
    return None


async def join_and_record_meeting(url: str, max_duration: int):
    """Joins a Google Meet, records audio, and logs active speakers."""
    ffmpeg_command = get_ffmpeg_command(sys.platform, max_duration)
    if not ffmpeg_command:
        print(f"Unsupported OS: {sys.platform}. Could not determine ffmpeg command.")
        return

    print("Starting browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
            ],
        )
        context = await browser.new_context(permissions=["microphone", "camera"])
        page = await context.new_page()

        recorder = None
        speaker_log = []
        start_time = time.time()

        try:
            print(f"Navigating to {url}...")
            await page.goto(url, timeout=60000)

            print("Entering a name...")
            await page.locator('input[placeholder="Your name"]').fill("NoteTaker Bot")

            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join"))
            print("Waiting for the join button...")
            await join_button_locator.wait_for(timeout=15000)

            print(f"Starting recording for a maximum of {max_duration / 3600:.1f} hours...")
            recorder = subprocess.Popen(
                ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            print("Clicking the join button...")
            await join_button_locator.click(timeout=15000)
            print("Successfully joined or requested to join.")

            # Disable camera
            try:
                camera_button = page.get_by_role("button", name="Turn off camera")
                await camera_button.wait_for(timeout=10000)
                await camera_button.click()
            except TimeoutError:
                pass

            # Disable microphone
            try:
                mic_button = page.get_by_role("button", name="Turn off microphone")
                await mic_button.wait_for(timeout=10000)
                await mic_button.click()
            except TimeoutError:
                pass

            print("Bot is now in the meeting. Monitoring participants & speakers...")

            check_interval_seconds = 3
            while True:
                await asyncio.sleep(check_interval_seconds)

                # --- Capture participant count ---
                try:
                    participant_button = page.get_by_role("button", name=re.compile(r"Participants|Show everyone"))
                    participant_count_text = await participant_button.inner_text()
                    participant_count = int(re.search(r'\d+', participant_count_text).group())
                except Exception:
                    participant_count = 0

                # --- Capture active speaker ---
                try:
                    active_speaker_locator = page.locator('div[aria-label*="is speaking"]')
                    active_speakers = await active_speaker_locator.all_inner_texts()
                    if active_speakers:
                        ts = time.time() - start_time
                        speaker_log.append({"time": ts, "speaker": active_speakers[0]})
                        print(f"[{participant_count}] 🎤 {active_speakers[0]} is speaking...")
                except Exception:
                    pass

                # Stop when only bot remains
                if participant_count <= 1:
                    print("Only 1 participant left. Ending recording.")
                    break

        except Exception as e:
            print(f"Error during setup/join: {e}")
            await page.screenshot(path="debug_screenshot.png")

        finally:
            print("Cleaning up...")
            if recorder:
                if recorder.poll() is None:
                    recorder.terminate()
                stdout, stderr = recorder.communicate()
                if os.path.exists(OUTPUT_FILENAME) and os.path.getsize(OUTPUT_FILENAME) > 0:
                    print(f"✅ Audio recording saved: {OUTPUT_FILENAME}")
                else:
                    print("❌ Recording failed. Error log:")
                    print(stderr.decode('utf-8', 'ignore'))

            await browser.close()
            print("Browser closed.")

            return speaker_log


def align_transcript_with_speakers(transcript, speaker_log):
    """Replace diarization speaker IDs with closest real Google Meet speaker name."""
    result = []
    for seg in transcript:
        seg_start = seg["start"]
        closest = min(speaker_log, key=lambda s: abs(s["time"] - seg_start)) if speaker_log else {"speaker": seg["speaker_id"]}
        result.append({
            "speaker": closest["speaker"],
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"]
        })
    return result



def transcribe_audio(filename):
    print("Sending audio to Whisper service...")
    with open(filename, "rb") as f:
        resp = requests.post(WHISPER_API_URL, files={"file": f})
    resp.raise_for_status()
    raw_text = resp.json()["text"]
    return parse_transcript(raw_text)


if __name__ == "__main__":
    if not MEETING_URL:
        print("Error: Please provide a meeting URL as a command-line argument.")
        sys.exit(1)

    # Step 1: Join & record
    speaker_log = asyncio.run(join_and_record_meeting(MEETING_URL, MAX_MEETING_DURATION_SECONDS))

    # Step 2: Transcribe with Whisper
    if os.path.exists(OUTPUT_FILENAME):
        transcript = transcribe_audio(OUTPUT_FILENAME)

        # Step 3: Align speakers
        final_transcript = align_transcript_with_speakers(transcript, speaker_log)

        # Step 4: Save JSON
        with open("meeting_transcript.json", "w", encoding="utf-8") as f:
            json.dump(final_transcript, f, ensure_ascii=False, indent=2)

        print("✅ Transcript saved: meeting_transcript.json")
