import os
import sys
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError
import json
import time
import requests

# --- CONFIGURATION ---
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else ""
MAX_MEETING_DURATION_SECONDS = 10800  # 3 hours
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
        last_caption_time = time.time()

        async def on_caption(speaker, text):
            nonlocal last_caption_time
            # Filter out common UI text that gets mistakenly captured
            if any(phrase in text.lower() for phrase in ["no one else is here", "backgrounds and effects", "visual effects"]):
                return
            
            # Prevent rapid duplicates
            if captions_data and captions_data[-1]["caption"] == text and captions_data[-1]["speaker"] == speaker:
                return

            timestamp = time.time()
            last_caption_time = timestamp
            print(f"CAPTURED: [{speaker}] {text}")
            captions_data.append({"speaker": speaker, "caption": text, "timestamp": timestamp})

        await context.expose_function("on_caption", on_caption)

        try:
            print(f"Navigating to {url}...")
            await page.goto(url, timeout=90000)
            print("Entering a name...")
            await page.locator('input[placeholder="Your name"]').fill("NoteTaker Bot")

            await page.get_by_role("button", name=re.compile("Turn off microphone", re.IGNORECASE)).click(timeout=10000)
            print("🎤 Microphone turned off before joining.")
            await page.get_by_role("button", name=re.compile("Turn off camera", re.IGNORECASE)).click(timeout=10000)
            print("📸 Camera turned off before joining.")

            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join", re.IGNORECASE))
            print("Waiting for the join button...")
            await join_button_locator.wait_for(timeout=30000)
            
            print(f"Starting recording for a maximum of {max_duration / 3600:.1f} hours...")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            print("Clicking the join button...")
            await join_button_locator.click(timeout=15000)
            print("Successfully joined or requested to join.")
            
            await page.wait_for_selector('button[aria-label*="Leave call"]', timeout=60000)
            print("✅ In the meeting.")

            got_it_button = page.get_by_role("button", name="Got it")
            if await got_it_button.is_visible(timeout=5000):
                await got_it_button.click()
                print("✅ Closed the 'Got it' pop-up window.")

            print("Trying to turn on captions...")
            captions_button = page.get_by_role("button", name=re.compile("Turn on captions", re.IGNORECASE))
            await captions_button.click(timeout=15000)
            print("✅ Clicked the main 'Turn on captions' (CC) button.")

            print("Waiting for captions to appear...")
            await page.wait_for_selector('[role="region"][aria-label*="Captions"]', timeout=20000)
            print("✅ Captions region is visible.")
            
            # --- IMPROVED JAVASCRIPT FOR SPEAKER/CAPTION SCRAPING ---
            js_code = """
            () => {
                const targetNode = document.body;
                const config = { childList: true, subtree: true, characterData: true };
                let lastSpeaker = 'Unknown Speaker';
                // This selector is crucial for identifying the speaker's name badge.
                const speakerBadgeSelector = '[data-self-name="You"] > div, .xt3V2d';

                const handleNode = (node) => {
                    if (typeof node.querySelector !== 'function') return;
                    
                    const speakerElement = node.querySelector(speakerBadgeSelector);
                    if (speakerElement && speakerElement.textContent) {
                         lastSpeaker = speakerElement.textContent.trim();
                    }
                    
                    const clone = node.cloneNode(true);
                    const speakerLabelInClone = clone.querySelector(speakerBadgeSelector);
                    if (speakerLabelInClone) speakerLabelInClone.remove();

                    // This selector targets the actual caption text spans.
                    const captionSpans = clone.querySelectorAll('span[jsname="YSxPC"]');
                    if (captionSpans.length > 0) {
                        for (const span of captionSpans) {
                            const captionText = span.textContent?.trim() || "";
                             if (captionText && captionText.toLowerCase() !== lastSpeaker.toLowerCase()) {
                                window.on_caption(lastSpeaker, captionText);
                            }
                        }
                    } else { // Fallback for other text content
                        const fallbackText = clone.textContent?.trim() || "";
                         if (fallbackText && fallbackText.toLowerCase() !== lastSpeaker.toLowerCase()) {
                            window.on_caption(lastSpeaker, fallbackText);
                        }
                    }
                };

                const observer = new MutationObserver((mutationsList) => {
                    for (const mutation of mutationsList) {
                        if (mutation.type === 'childList') {
                            mutation.addedNodes.forEach(node => {
                                if (node.nodeType === 1) { handleNode(node); }
                            });
                        } else if (mutation.type === 'characterData' && mutation.target.parentElement) {
                            handleNode(mutation.target.parentElement);
                        }
                    }
                });
                observer.observe(targetNode, config);
                console.log('✅ Advanced Caption Observer injected and running.');
            }
            """
            await page.evaluate(js_code)

            print("Bot is in the meeting. Monitoring for exit conditions...")
            start_time = time.time()
            IDLE_TIMEOUT_SECONDS = 300 # 5 minutes
            
            while time.time() - start_time < max_duration:
                # --- NEW: EXIT CONDITION CHECKS ---
                # Check 1: Did everyone leave?
                if await page.locator('text="No one else is here"').is_visible():
                    print("👋 Everyone left the meeting. Exiting.")
                    break
                # Check 2: Did we get kicked out or leave manually?
                if await page.locator('text=/You left the meeting|You\'ve been removed/').is_visible():
                    print("👋 Bot has left the meeting. Exiting.")
                    break
                # Check 3: Has it been too long since the last caption?
                if time.time() - last_caption_time > IDLE_TIMEOUT_SECONDS:
                    print(f"🕒 No new captions for {IDLE_TIMEOUT_SECONDS} seconds. Exiting.")
                    break
                
                await asyncio.sleep(10) # Check every 10 seconds

        except Exception as e:
            print(f"An error occurred: {e}")
            await page.screenshot(path="debug_screenshot.png")
            print("📸 Screenshot saved to debug_screenshot.png.")
        finally:
            print("Cleaning up...")
            if recorder and recorder.poll() is None:
                print("Terminating audio recording...")
                recorder.terminate()
                await asyncio.sleep(2)
                
            if os.path.exists(OUTPUT_FILENAME) and os.path.getsize(OUTPUT_FILENAME) > 0:
                print(f"✅ Audio recording successful. File saved to {OUTPUT_FILENAME}")
                transcribe_audio(OUTPUT_FILENAME)
            else:
                print("❌ Recording failed or was empty.")
            
            if captions_data:
                with open(CAPTIONS_FILENAME, 'w', encoding='utf-8') as f:
                    json.dump(captions_data, f, indent=4, ensure_ascii=False)
                print(f"✅ Captions with speaker names saved to {CAPTIONS_FILENAME}")
            
            await browser.close()
            print("Browser closed.")

if __name__ == "__main__":
    if not MEETING_URL:
        print("Error: Please provide a meeting URL as a command-line argument.")
        sys.exit(1)
    asyncio.run(join_and_record_meeting(MEETING_URL, MAX_MEETING_DURATION_SECONDS))
