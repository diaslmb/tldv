import os
import sys
import asyncio
import subprocess
from playwright.async_api import async_playwright

# --- CONFIGURATION ---
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else ""
MEETING_DURATION_SECONDS = 300 
OUTPUT_FILENAME = "meeting_audio.wav"

def get_ffmpeg_command(platform):
    """Returns the appropriate ffmpeg command based on the operating system."""
    if platform.startswith("linux"):
        return [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME
        ]
    elif platform == "darwin": # macOS
        return [
            "ffmpeg", "-y", "-f", "avfoundation", "-i", ":BlackHole 2ch",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME
        ]
    return None

async def join_and_record_meeting(url: str, duration: int):
    """Launches a browser, joins a meeting, and records the audio."""
    ffmpeg_command = get_ffmpeg_command(sys.platform)
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
            ]
        )
        context = await browser.new_context(permissions=["microphone", "camera"])
        page = await context.new_page()

        recorder = None
        try:
            print(f"Navigating to {url}...")
            await page.goto(url, timeout=60000)

            name_input_selector = 'input[placeholder="Your name"]'
            print("Waiting for the name input field...")
            await page.wait_for_selector(name_input_selector, timeout=15000)
            print("Entering a name...")
            await page.locator(name_input_selector).fill("NoteTaker Bot")

            # =========================================================
            # ===> THE ONLY CHANGE IS ON THIS LINE <===
            join_button_selector = 'button:has-text("Ask to join")'
            # =========================================================
            
            print(f"Waiting for the '{join_button_selector}' button...")
            
            print(f"Starting recording... Command: {' '.join(ffmpeg_command)}")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            print(f"Clicking '{join_button_selector}'...")
            await page.locator(join_button_selector).click(timeout=15000)

            print(f"✅ Successfully requested to join. Now waiting in the lobby. Recording for {duration} seconds.")
            await asyncio.sleep(duration)

        except Exception as e:
            print(f"An error occurred: {e}")
            await page.screenshot(path="debug_screenshot.png")
            print("📸 Screenshot saved to debug_screenshot.png. Please check this file.")
            
        finally:
            print("Cleaning up...")
            if recorder and recorder.poll() is None:
                recorder.terminate()
                stdout, stderr = recorder.communicate()
                print("Recording process terminated.")
                if os.path.exists(OUTPUT_FILENAME):
                    print(f"✅ Audio saved to {OUTPUT_FILENAME}")
                else:
                    print("❌ Recording failed. FFmpeg error output:")
                    print(stderr.decode())
            await browser.close()
            print("Browser closed.")

if __name__ == "__main__":
    if not MEETING_URL:
        print("Error: Please provide a meeting URL as a command-line argument.")
        sys.exit(1)
    
    asyncio.run(join_and_record_meeting(MEETING_URL, MEETING_DURATION_SECONDS))
