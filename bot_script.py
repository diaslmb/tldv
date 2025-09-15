import os
import sys
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError

# --- CONFIGURATION ---
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else ""
# The fixed duration is now a fallback safety measure. 10800 seconds = 3 hours.
MAX_MEETING_DURATION_SECONDS = 10800
OUTPUT_FILENAME = "meeting_audio.wav"


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
    """Launches a browser, joins a meeting, records until it's the last one, and disables video."""
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
            
            # --- NEW: DISABLE CAMERA LOGIC ---
            try:
                # Wait for the camera button to be visible after joining
                camera_button = page.get_by_role("button", name="Turn off camera")
                await camera_button.wait_for(timeout=10000)
                await camera_button.click()
                print("📸 Camera turned off.")
            except TimeoutError:
                print("Could not find 'Turn off camera' button, or camera was already off.")
            
            # --- NEW: DYNAMIC RECORDING LOGIC ---
            print("Bot is now in the meeting. Monitoring participant count...")
            check_interval_seconds = 15
            while True:
                await asyncio.sleep(check_interval_seconds)
                try:
                    # This selector targets the button that shows the participant list and the count within it.
                    # It is the most likely part of the script to break if Google updates its UI.
                    participant_button = page.get_by_role("button", name=re.compile(r"Participants|Show everyone"))
                    participant_count_text = await participant_button.inner_text()
                    participant_count = int(re.search(r'\d+', participant_count_text).group())

                    print(f"[{participant_count}] participants in the meeting.")
                    
                    # If only the bot is left, end the meeting.
                    if participant_count <= 1:
                        print("Only 1 participant left. Ending the recording.")
                        break
                except (TimeoutError, AttributeError, ValueError):
                    # If the participant count element can't be found, the meeting may have ended abruptly.
                    print("Could not find participant count. Assuming meeting has ended.")
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
            if recorder:
                if recorder.poll() is None:
                    recorder.terminate() # Stop ffmpeg if it's still running
                stdout, stderr = recorder.communicate()
                if os.path.exists(OUTPUT_FILENAME) and os.path.getsize(OUTPUT_FILENAME) > 0:
                    print(f"✅ Audio recording successful. File saved to {OUTPUT_FILENAME}")
                else:
                    print("❌ Recording failed or was empty. The output file is missing or empty.")
                    print("--- FFmpeg Error Output ---")
                    print(stderr.decode('utf-8', 'ignore'))
                    print("-----------------------------")

            await browser.close()
            print("Browser closed.")


if __name__ == "__main__":
    if not MEETING_URL:
        print("Error: Please provide a meeting URL as a command-line argument.")
        sys.exit(1)

    asyncio.run(join_and_record_meeting(MEETING_URL, MAX_MEETING_DURATION_SECONDS))
