import os
import sys
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError
import json
import requests

# --- CONFIGURATION ---
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else ""
MAX_MEETING_DURATION_SECONDS = 10800
OUTPUT_FILENAME = "meeting_audio.wav"
TRANSCRIPT_FILENAME = "transcript.txt"
WHISPERX_URL = "http://localhost:8000/v1/audio/transcriptions"

def get_ffmpeg_command(platform, duration):
    if platform.startswith("linux"):
        return ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-t", str(duration), OUTPUT_FILENAME]
    elif platform == "darwin":
        return ["ffmpeg", "-y", "-f", "avfoundation", "-i", ":BlackHole 2ch", "-t", str(duration), OUTPUT_FILENAME]
    return None

def transcribe_audio(audio_path):
    if not os.path.exists(audio_path):
        print(f"❌ Audio file not found at {audio_path}")
        return
    print(f"🎤 Sending {audio_path} to whisperx for transcription...")
    try:
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f)}
            response = requests.post(WHISPERX_URL, files=files)
        if response.status_code == 200:
            transcript_data = response.json()
            clean_transcript = transcript_data.get('text', '').replace('<br>', '\n')
            with open(TRANSCRIPT_FILENAME, 'w') as f:
                f.write(clean_transcript)
            print(f"✅ Transcription successful. Saved to {TRANSCRIPT_FILENAME}")
        else:
            print(f"❌ Transcription failed. Status code: {response.status_code}\n{response.text}")
    except requests.exceptions.RequestException as e:
        print(f"❌ Error connecting to whisperx service: {e}")
    except Exception as e:
        print(f"An unexpected error occurred during transcription: {e}")

async def join_and_record_meeting(url: str, max_duration: int):
    ffmpeg_command = get_ffmpeg_command(sys.platform, max_duration)
    if not ffmpeg_command:
        print(f"Unsupported OS: {sys.platform}. Could not determine ffmpeg command.")
        return

    print("Starting browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        context = await browser.new_context(permissions=["microphone", "camera"])
        page = await context.new_page()
        recorder = None

        try:
            print(f"Navigating to {url}...")
            await page.goto(url, timeout=60000)
            print("Entering a name...")
            await page.locator('input[placeholder="Your name"]').fill("NoteTaker Bot")

            # --- Turn off mic and camera BEFORE joining ---
            try:
                await page.get_by_role("button", name="Turn off microphone").click(timeout=10000)
                print("🎤 Microphone turned off before joining.")
            except Exception:
                print("Could not turn off microphone before joining.")
            try:
                await page.get_by_role("button", name="Turn off camera").click(timeout=10000)
                print("📸 Camera turned off before joining.")
            except Exception:
                print("Could not turn off camera before joining.")

            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join"))
            print("Waiting for the join button...")
            await join_button_locator.wait_for(timeout=15000)

            print(f"Starting recording for a maximum of {max_duration / 3600:.1f} hours...")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            print("Clicking the join button...")
            await join_button_locator.click(timeout=15000)
            print("Successfully joined or requested to join.")

            try:
                await page.get_by_role("button", name="Got it").click(timeout=15000)
                print("✅ Closed the initial pop-up window.")
            except TimeoutError:
                print("Initial pop-up not found, continuing...")

            # --- NEW: Added a delay to allow the main meeting UI to stabilize ---
            print("Waiting for 10 seconds for the meeting UI to stabilize...")
            await asyncio.sleep(10)

            print("Bot is now in the meeting. Monitoring participant count...")
            check_interval_seconds = 10
            while True:
                await asyncio.sleep(check_interval_seconds)
                try:
                    participant_button_locator = page.locator('button[aria-label*="Show everyone"], button[aria-label*="Participants"]')
                    
                    await participant_button_locator.wait_for(state="visible", timeout=5000)
                    
                    participant_count_text = await participant_button_locator.get_attribute("aria-label")
                    print(f"DEBUG: Raw aria-label: '{participant_count_text}'")

                    match = re.search(r'\d+', participant_count_text)
                    if match:
                        participant_count = int(match.group())
                        print(f"✅ Successfully parsed participant count: [{participant_count}]")
                        if participant_count <= 1:
                            print("Only 1 participant left. Ending the recording.")
                            break
                    else:
                        print(f"❌ Could not parse participant count from text: '{participant_count_text}'")
                        break

                except TimeoutError:
                    print("Could not find participant count button. Assuming meeting has ended.")
                    await page.screenshot(path="debug_participant_timeout.png")
                    print("📸 Screenshot saved to debug_participant_timeout.png.")
                    break
                except Exception as e:
                    print(f"An unexpected error occurred while checking participants: {e}")
                    await page.screenshot(path="debug_participant_unexpected_error.png")
                    print("📸 Screenshot saved to debug_participant_unexpected_error.png.")
                    break
        except Exception as e:
            print(f"An error occurred during setup or joining: {e}")
            await page.screenshot(path="debug_setup_error.png")
            print("📸 Screenshot saved to debug_setup_error.png.")
        finally:
            print("Cleaning up...")
            if recorder and recorder.poll() is None:
                print("Stopping the recording...")
                recorder.terminate()
                stdout, stderr = recorder.communicate()
                if os.path.exists(OUTPUT_FILENAME) and os.path.getsize(OUTPUT_FILENAME) > 0:
                    print(f"✅ Audio recording successful. File saved to {OUTPUT_FILENAME}")
                    transcribe_audio(OUTPUT_FILENAME)
                else:
                    print(f"❌ Recording failed or was empty.\n--- FFmpeg Error Output ---\n{stderr.decode('utf-8', 'ignore')}\n-----------------------------")
            
            try:
                print("Attempting to hang up...")
                hang_up_button = page.get_by_role("button", name="Leave call")
                await hang_up_button.click(timeout=5000)
                print("✅ Clicked the 'Leave call' button.")
                await asyncio.sleep(3)
            except Exception as e:
                print(f"Could not click hang up button, may have already left: {e}")

            await browser.close()
            print("Browser closed.")

if __name__ == "__main__":
    if not MEETING_URL:
        print("Error: Please provide a meeting URL as a command-line argument.")
        sys.exit(1)
    asyncio.run(join_and_record_meeting(MEETING_URL, MAX_MEETING_DURATION_SECONDS))
