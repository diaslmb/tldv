import os
import sys
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright

# --- CONFIGURATION ---
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else ""
MEETING_DURATION_SECONDS = 30
OUTPUT_FILENAME = "meeting_audio.wav"


def get_ffmpeg_command(platform):
    if platform.startswith("linux"):
        return [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME,
        ]
    elif platform == "darwin":
        return [
            "ffmpeg", "-y", "-f", "avfoundation", "-i", ":BlackHole 2ch",
            "-t", str(MEETING_DURATION_SECONDS), OUTPUT_FILENAME,
        ]
    return None


async def join_and_record_meeting(url: str, duration: int):
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
            ],
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

            join_button_locator = page.get_by_role("button", name=re.compile("Join now|Ask to join"))

            print("Waiting for the join button...")
            await join_button_locator.wait_for(timeout=15000)

            print(f"Starting recording... Command: {' '.join(ffmpeg_command)}")
            recorder = subprocess.Popen(
                ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            print("Clicking the join button...")
            await join_button_locator.click(timeout=15000)

            print(
                f"✅ Successfully joined or requested to join. Recording for {duration} seconds."
            )
            await asyncio.sleep(duration)

        except Exception as e:
            print(f"An error occurred: {e}")
            await page.screenshot(path="debug_screenshot.png")
            print("📸 Screenshot saved to debug_screenshot.png.")

        finally:
            print("Cleaning up...")
            # === NEW, MORE ROBUST ERROR HANDLING ===
            if recorder:
                # Wait for the ffmpeg process to complete and capture its output
                stdout, stderr = recorder.communicate()

                # Check if the output file was created and is not empty
                if os.path.exists(OUTPUT_FILENAME) and os.path.getsize(OUTPUT_FILENAME) > 0:
                    print(f"✅ Audio recording successful. File saved to {OUTPUT_FILENAME}")
                else:
                    print("❌ Recording failed. The output file is missing or empty.")
                    print("--- FFmpeg Error Output ---")
                    # Decode stderr to print the error message from ffmpeg
                    print(stderr.decode('utf-8', 'ignore'))
                    print("-----------------------------")

            await browser.close()
            print("Browser closed.")


if __name__ == "__main__":
    if not MEETING_URL:
        print("Error: Please provide a meeting URL as a command-line argument.")
        sys.exit(1)

    asyncio.run(join_and_record_meeting(MEETING_URL, MEETING_DURATION_SECONDS))
