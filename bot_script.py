import os
import sys
import asyncio
import subprocess
from playwright.async_api import async_playwright

# --- CONFIGURATION ---
# The script reads the meeting URL from the command line argument.
# Example: python bot_script.py "https://meet.google.com/ruc-cqzz-ekr"
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else ""

# How long to record for (in seconds). 300 seconds = 5 minutes.
MEETING_DURATION_SECONDS = 300 
OUTPUT_FILENAME = "meeting_audio.wav"

def get_ffmpeg_command(platform):
    """Returns the appropriate ffmpeg command based on the operating system."""
    if platform.startswith("linux"):
        # Records from the default pulse audio output in a Linux environment.
        return [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME
        ]
    elif platform == "darwin": # macOS
        # Records from the "BlackHole 2ch" virtual audio device on macOS.
        return [
            "ffmpeg", "-y", "-f", "avfoundation", "-i", ":BlackHole 2ch",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME
        ]
    # Add other OS configurations here if needed (e.g., Windows).
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
            headless=False, # Must be False to work inside a virtual display like Xvfb.
            args=[
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",   # Auto-grant camera/mic permissions.
                "--use-fake-device-for-media-stream",
            ]
        )
        context = await browser.new_context(permissions=["microphone", "camera"])
        page = await context.new_page()

        recorder = None # Initialize recorder to None
        try:
            print(f"Navigating to {url}...")
            # Use a longer timeout for page navigation to handle slower connections.
            await page.goto(url, timeout=60000) 

            # This is the selector that failed before. We keep it to see why it failed.
            # The screenshot will tell us if the text should be "Ask to join" or something else.
            join_button_selector = 'button:has-text("Join now")'
            
            print(f"Waiting for selector: '{join_button_selector}'")
            await page.wait_for_selector(join_button_selector, timeout=30000)
            
            print(f"Starting recording... Command: {' '.join(ffmpeg_command)}")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            print("Clicking 'Join now'...")
            await page.locator(join_button_selector).click()

            print(f"Successfully joined. Recording for {duration} seconds.")
            await asyncio.sleep(duration)

        except Exception as e:
            print(f"An error occurred: {e}")
            # Take a screenshot to help debug what the browser sees.
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
        print('Example: python bot_script.py "https://meet.google.com/abc-defg-hij"')
        sys.exit(1)
    
    asyncio.run(join_and_record_meeting(MEETING_URL, MEETING_DURATION_SECONDS))
