import os
import sys
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError
import json
import requests
import time

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
        seen_captions = set()

        # This function will be called from the browser's JS context
        async def on_caption(speaker, text):
            caption_key = f"{speaker}:{text}"
            if text and caption_key not in seen_captions:
                timestamp = time.time()
                print(f"CAPTURED: [{speaker}] {text}")
                captions_data.append({"speaker": speaker, "caption": text, "timestamp": timestamp})
                seen_captions.add(caption_key)

        await context.expose_function("on_caption", on_caption)

        try:
            print(f"Navigating to {url}...")
            await page.goto(url, timeout=60000)
            print("Entering a name...")
            await page.locator('input[placeholder="Your name"]').fill("NoteTaker Bot")

            await page.get_by_role("button", name="Turn off microphone").click(timeout=10000)
            print("🎤 Microphone turned off before joining.")
            await page.get_by_role("button", name="Turn off camera").click(timeout=10000)
            print("📸 Camera turned off before joining.")

            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join", re.IGNORECASE))
            print("Waiting for the join button...")
            await join_button_locator.wait_for(timeout=30000)
            
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

            # --- UPDATED CAPTION ACTIVATION LOGIC ---
            print("Trying to turn on captions...")
            captions_on = False
            # Try keyboard shortcut first
            for i in range(5):
                await page.keyboard.press("c")
                await asyncio.sleep(0.5)
                if await page.locator('[aria-label*="captions are available"]').is_visible():
                     print("✅ Captions enabled via keyboard shortcut.")
                     captions_on = True
                     break
            
            # Fallback to clicking the button if shortcut fails
            if not captions_on:
                try:
                    await page.get_by_role("button", name="Turn on captions").click(timeout=10000)
                    print("✅ Captions enabled via button click.")
                except TimeoutError:
                    print("❌ Could not find 'Turn on captions' button. Captions may fail.")

            await asyncio.sleep(5) # Grace period for captions to initialize

            # --- INJECT JAVASCRIPT TO OBSERVE CAPTIONS ---
            js_code = """
            () => {
                const targetNode = document.body;
                const config = { childList: true, subtree: true, characterData: true };

                let lastSpeaker = 'Unknown';
                const seenCaptions = new Set();

                const callback = (mutationsList, observer) => {
                    const captionBoxes = document.querySelectorAll('div.iTTPOb.VbkSUe');
                    if (captionBoxes.length === 0) return;

                    for (const container of captionBoxes) {
                        try {
                            const speakerElement = container.querySelector('div.zs7s8d.jxF_2d');
                            const speakerName = speakerElement ? speakerElement.innerText.trim() : lastSpeaker;
                            lastSpeaker = speakerName;

                            const captionElement = container.querySelector('span[jsname="YSxPC"]');
                            if (!captionElement) continue;
                            
                            const captionText = captionElement.innerText.trim();
                            const captionKey = `${speakerName}:${captionText}`;

                            if (captionText && !seenCaptions.has(captionKey)) {
                                seenCaptions.add(captionKey);
                                window.on_caption(speakerName, captionText);
                            }
                        } catch (e) {
                            // console.error('Error processing caption block:', e);
                        }
                    }
                };

                const observer = new MutationObserver(callback);
                observer.observe(targetNode, config);
                console.log('✅ Caption observer injected and running.');
            }
            """
            await page.evaluate(js_code)

            print("Bot is now in the meeting. Monitoring participant count...")
            check_interval_seconds = 10
            while True:
                await asyncio.sleep(check_interval_seconds)
                try:
                    participant_button = page.get_by_role("button", name=re.compile(r"Participants|Show everyone", re.IGNORECASE))
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
                    print(f"An unexpected error occurred while checking participants: {e}")
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
