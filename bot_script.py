import os
import sys
import asyncio
import subprocess
from playwright.async_api import async_playwright

# --- CONFIGURATION ---
# The URL will be passed in when the script is called
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else "https://meet.google.com/ruc-cqzz-ekr"
# How long to record for (in seconds)
MEETING_DURATION_SECONDS = 300 
OUTPUT_FILENAME = "meeting_audio.wav"

# --- FFMPEG COMMANDS PER OS ---
def get_ffmpeg_command(platform):
    if platform.startswith("linux"):
        # Records from the default pulse audio output sink
        return [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME
        ]
    elif platform == "darwin": # macOS
        # Records from the "BlackHole 2ch" virtual audio device
        return [
            "ffmpeg", "-y", "-f", "avfoundation", "-i", ":BlackHole 2ch",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME
        ]
    else: # Windows
        # This is more complex. You need to find your "Stereo Mix" device name.
        # Open Sound settings, find the device name and replace "Stereo Mix".
        # You may need to enable it first.
        print("WARNING: Windows audio capture requires manual setup.")
        return [
            "ffmpeg", "-y", "-f", "dshow", "-i", "audio=Stereo Mix (Realtek(R) Audio)",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME
        ]

async def join_and_record_meeting(url: str, duration: int):
    ffmpeg_command = get_ffmpeg_command(sys.platform)
    if not ffmpeg_command:
        print(f"Unsupported OS: {sys.platform}")
        return

    print("Starting browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, # Must be False if not using a virtual display like Xvfb
            args=[
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
            ]
        )
        context = await browser.new_context(permissions=["microphone", "camera"])
        page = await context.new_page()

        try:
            print(f"Navigating to {url}...")
            await page.goto(url)

            # IMPORTANT: Before running, manually set your Mac's sound output to "BlackHole 2ch"
            # in System Settings -> Sound -> Output.
            if sys.platform == "darwin":
                print("\n!!! ACTION REQUIRED FOR MACOS !!!")
                input("Please set your system's sound output to 'BlackHole 2ch' and press Enter...")

            join_button_selector = 'button:has-text("Join now")'
            await page.wait_for_selector(join_button_selector, timeout=30000)
            
            print(f"Starting recording... Command: {' '.join(ffmpeg_command)}")
            recorder = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            print("Clicking 'Join now'...")
            await page.locator(join_button_selector).click()

            print(f"Successfully joined. Recording for {duration} seconds.")
            await asyncio.sleep(duration)

        except Exception as e:
            print(f"An error occurred: {e}")
        finally:
            print("Meeting duration ended. Stopping recording and closing browser.")
            if 'recorder' in locals() and recorder.poll() is None:
                recorder.terminate()
                stdout, stderr = recorder.communicate()
                print("Recording stopped.")
                if os.path.exists(OUTPUT_FILENAME):
                    print(f"✅ Audio saved to {OUTPUT_FILENAME}")
                    # NEXT STEP: Upload OUTPUT_FILENAME to your Phase 1 API here.
                else:
                    print("❌ Recording failed. FFmpeg output:")
                    print(stderr.decode())
            await browser.close()

if __name__ == "__main__":
    if "your-test-code" in MEETING_URL:
        print("Error: Please provide a valid meeting URL as a command-line argument.")
        sys.exit(1)
    
    asyncio.run(join_and_record_meeting(MEETING_URL, MEETING_DURATION_SECONDS))
