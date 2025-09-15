import asyncio
import time
import requests
from playwright.sync_api import sync_playwright
from process_transcript import parse_transcript

# CONFIG
MEETING_URL = None  # Passed as CLI argument
OUTPUT_AUDIO = "meeting_audio.wav"
WHISPER_API_URL = "http://localhost:8000/v1/audio/transcriptions"  # your Whisper STT endpoint


def join_meeting(meet_url: str):
    """Join Google Meet, stay, and record audio."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--use-fake-ui-for-media-stream",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context()
        page = context.new_page()

        print("Starting browser...")
        page.goto(meet_url)

        print("Navigating to", meet_url)

        # Handle name entry
        print("Entering a name...")
        try:
            name_input = page.locator('input[placeholder="Your name"]')
            if name_input.is_visible(timeout=5000):
                name_input.fill("SHAI VoiceAI")
                print("Name entered: SHAI VoiceAI")
            else:
                print("No name input field found (probably logged in). Skipping...")
        except Exception:
            print("Name input not shown. Continuing without entering name.")

        # Wait for join button
        print("Waiting for the join button...")
        try:
            join_btn = page.locator('button:has-text("Join now")')
            join_btn.wait_for(timeout=10000)
            join_btn.click()
            print("Clicking the join button...")
        except Exception:
            print("Join button not found. Trying alternative selector...")
            try:
                alt_btn = page.locator('button:has-text("Ask to join")')
                alt_btn.click()
                print("Clicked 'Ask to join'")
            except Exception:
                print("Failed to click join. Exiting.")
                context.close()
                browser.close()
                return

        print("Successfully joined or requested to join.")
        print("Bot is now in the meeting. Monitoring participants & speakers...")

        # Simulate recording (replace with your ffmpeg or pyAudio capture)
        print("Starting recording for a maximum of 3.0 hours...")
        duration = 10  # seconds for demo
        time.sleep(duration)
        print("Recording finished.")

        # Close browser
        context.close()
        browser.close()
        print("Browser closed.")


def transcribe_audio(filename: str):
    """Send audio to Whisper service and parse transcript."""
    print("Sending audio to Whisper service...")
    with open(filename, "rb") as f:
        resp = requests.post(WHISPER_API_URL, files={"file": f})
    resp.raise_for_status()
    raw_text = resp.json()["text"]
    transcript = parse_transcript(raw_text)
    return transcript


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python bot_script.py <MEET_URL>")
        sys.exit(1)

    MEETING_URL = sys.argv[1]

    # 1. Join meeting and record
    join_meeting(MEETING_URL)

    # 2. Transcribe
    try:
        final_transcript = transcribe_audio(OUTPUT_AUDIO)
        print("✅ Final transcript:")
        for seg in final_transcript:
            print(f"[{seg['speaker_id']} {seg['start']}-{seg['end']}] {seg['text']}")
    except Exception as e:
        print("Transcription failed:", e)
