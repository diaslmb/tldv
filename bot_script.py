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
CAPTIONS_FILENAME = "captions.json"
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
        captions_data = []

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

            try:
                await page.get_by_role("button", name="Turn on captions").click(timeout=10000)
                print("📝 Captions turned on.")
            except TimeoutError:
                print("Could not find 'Turn on captions' button, or captions were already on.")
            
            # --- NEW: GRACE PERIOD FOR CAPTIONS TO APPEAR ---
            print("Waiting 15 seconds for meeting to stabilize and captions to start...")
            await asyncio.sleep(15)

            # --- DYNAMIC RECORDING AND CAPTION SCRAPING LOGIC ---
            print("Bot is now in the meeting. Monitoring participant count and scraping captions...")
            check_interval_seconds = 5
            seen_captions = set()
            while True:
                await asyncio.sleep(check_interval_seconds)
                try:
                    caption_containers = await page.query_selector_all("div.iTTPOb.VbkSUe")
                    for container in caption_containers:
                        try:
                            speaker_element = await container.query_selector("div.zs7s8d.jxF_2d")
                            speaker_name = await speaker_element.inner_text() if speaker_element else "Unknown"
                            caption_text_element = await container.query_selector("span[jsname='YSxPC']")
                            caption_text = await caption_text_element.inner_text() if caption_text_element else ""
                            caption_key = f"{speaker_name}:{caption_text}"
                            if caption_text and caption_key not in seen_captions:
                                timestamp = asyncio.get_event_loop().time()
                                captions_data.append({"speaker": speaker_name, "caption": caption_text, "timestamp": timestamp})
                                seen_captions.add(caption_key)
                                print(f"CAPTURED: [{speaker_name}] {caption_text}")
                        except Exception as e:
                            print(f"Could not process a caption block: {e}")

                    participant_button = page.get_by_role("button", name=re.compile(r"Participants|Show everyone"))
                    participant_count_text = await participant_button.inner_text()
                    match = re.search(r'\d+', participant_count_text)
                    if match:
                        participant_count = int(match.group())
                        print(f"[{participant_count}] participants in the meeting.")
                        if participant_count <= 1:
                            print("Only 1 participant left. Ending the recording.")
                            break
                    else:
                        print("Could not determine participant count from text. Assuming meeting has ended.")
                        break
                except (TimeoutError, AttributeError):
                    print("Could not find participant count button. Assuming meeting has ended.")
                    break
                except Exception as e:
                    print(f"An unexpected error occurred while checking participants or scraping captions: {e}")
                    break
        except Exception as e:
            print(f"An error occurred during setup or joining: {e}")
            await page.screenshot(path="debug_screenshot.png")
            print("📸 Screenshot saved to debug_screenshot.png.")
        finally:
            print("Cleaning up...")
            if recorder and recorder.poll() is None:
                recorder.terminate()
                stdout, stderr = recorder.communicate()
                if os.path.exists(OUTPUT_FILENAME) and os.path.getsize(OUTPUT_FILENAME) > 0:
                    print(f"✅ Audio recording successful. File saved to {OUTPUT_FILENAME}")
                    transcribe_audio(OUTPUT_FILENAME)
                else:
                    print(f"❌ Recording failed or was empty.\n--- FFmpeg Error Output ---\n{stderr.decode('utf-8', 'ignore')}\n-----------------------------")
            
            if captions_data:
                with open(CAPTIONS_FILENAME, 'w') as f:
                    json.dump(captions_data, f, indent=4)
                print(f"✅ Captions saved to {CAPTIONS_FILENAME}")
            
            await browser.close()
            print("Browser closed.")

if __name__ == "__main__":
    if not MEETING_URL:
        print("Error: Please provide a meeting URL as a command-line argument.")
        sys.exit(1)
    asyncio.run(join_and_record_meeting(MEETING_URL, MAX_MEETING_DURATION_SECONDS))
