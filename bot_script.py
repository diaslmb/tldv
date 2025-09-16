import os
import sys
import re
import asyncio
import subprocess
from playwright.async_api import async_playwright, TimeoutError
import json
import requests
from collections import defaultdict, Counter
from difflib import SequenceMatcher

# --- CONFIGURATION ---
MEETING_URL = sys.argv[1] if len(sys.argv) > 1 else ""
MAX_MEETING_DURATION_SECONDS = 10800
OUTPUT_FILENAME = "meeting_audio.wav"
CAPTIONS_FILENAME = "captions.json"
TRANSCRIPT_FILENAME = "transcript.txt"
MAPPED_TRANSCRIPT_FILENAME = "mapped_transcript.txt"
SPEAKER_MAPPING_FILENAME = "speaker_mapping.json"
WHISPERX_URL = "http://localhost:8000/v1/audio/transcriptions"

class SpeakerMapper:
    def __init__(self):
        self.speaker_segments = defaultdict(list)  # speaker_label -> [text_segments]
        self.real_name_segments = defaultdict(list)  # real_name -> [text_segments]
        self.mapping = {}  # speaker_label -> real_name
        
    def add_caption_segment(self, real_name, text, timestamp):
        """Add a caption segment with real speaker name"""
        self.real_name_segments[real_name].append({
            'text': text.strip(),
            'timestamp': timestamp,
            'length': len(text.strip())
        })
    
    def add_transcription_segment(self, speaker_label, text, start_time, end_time):
        """Add a transcription segment with generic speaker label"""
        self.speaker_segments[speaker_label].append({
            'text': text.strip(),
            'start_time': start_time,
            'end_time': end_time,
            'length': len(text.strip())
        })
    
    def similarity_score(self, text1, text2):
        """Calculate similarity between two text segments"""
        if not text1 or not text2:
            return 0.0
        return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()
    
    def find_best_matches(self):
        """Find best matches between speaker labels and real names"""
        matches = {}
        
        for speaker_label, speaker_segments in self.speaker_segments.items():
            best_match = None
            best_score = 0.0
            match_details = []
            
            for real_name, name_segments in self.real_name_segments.items():
                if real_name == "Unknown" or real_name == "NoteTaker Bot":
                    continue
                    
                total_score = 0.0
                match_count = 0
                
                # Compare all segments from this speaker with all segments from this real name
                for speaker_seg in speaker_segments:
                    for name_seg in name_segments:
                        similarity = self.similarity_score(speaker_seg['text'], name_seg['text'])
                        if similarity > 0.3:  # Minimum similarity threshold
                            total_score += similarity
                            match_count += 1
                
                if match_count > 0:
                    avg_score = total_score / match_count
                    match_details.append({
                        'real_name': real_name,
                        'avg_score': avg_score,
                        'match_count': match_count,
                        'total_segments': len(name_segments)
                    })
            
            # Sort by average score and match count
            match_details.sort(key=lambda x: (x['avg_score'], x['match_count']), reverse=True)
            
            if match_details and match_details[0]['avg_score'] > 0.4:  # Confidence threshold
                best_match = match_details[0]['real_name']
                best_score = match_details[0]['avg_score']
            
            matches[speaker_label] = {
                'mapped_name': best_match or f"Unknown_{speaker_label}",
                'confidence': best_score,
                'all_matches': match_details[:3]  # Top 3 matches
            }
        
        self.mapping = {k: v['mapped_name'] for k, v in matches.items()}
        return matches
    
    def get_mapped_name(self, speaker_label):
        """Get the mapped real name for a speaker label"""
        return self.mapping.get(speaker_label, f"Unknown_{speaker_label}")

def get_ffmpeg_command(platform, duration):
    if platform.startswith("linux"):
        return ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-t", str(duration), OUTPUT_FILENAME]
    elif platform == "darwin":
        return ["ffmpeg", "-y", "-f", "avfoundation", "-i", ":BlackHole 2ch", "-t", str(duration), OUTPUT_FILENAME]
    return None

def parse_transcription_segments(transcript_text):
    """Parse the transcript text and extract speaker segments with timestamps"""
    segments = []
    # Split by <br><br> to get individual speaker segments
    parts = transcript_text.split('<br><br>')
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
            
        # Extract speaker and timestamp info
        # Pattern: [SPEAKER_XX] [start_time - end_time]<br> text
        pattern = r'\[SPEAKER_(\d+)\]\s*\[([0-9.]+)\s*-\s*([0-9.]+)\]<br>\s*(.*)'
        match = re.match(pattern, part, re.DOTALL)
        
        if match:
            speaker_num = match.group(1)
            start_time = float(match.group(2))
            end_time = float(match.group(3))
            text = match.group(4).replace('<br>', ' ').strip()
            
            segments.append({
                'speaker_label': f"SPEAKER_{speaker_num}",
                'start_time': start_time,
                'end_time': end_time,
                'text': text
            })
    
    return segments

def transcribe_and_map_speakers(audio_path, captions_data):
    """Transcribe audio and map speaker labels to real names"""
    if not os.path.exists(audio_path):
        print(f"❌ Audio file not found at {audio_path}")
        return
        
    print(f"🎤 Sending {audio_path} to whisperx for transcription...")
    
    try:
        # Get transcription from service
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f)}
            response = requests.post(WHISPERX_URL, files=files)
            
        if response.status_code != 200:
            print(f"❌ Transcription failed. Status code: {response.status_code}\n{response.text}")
            return
            
        transcript_data = response.json()
        raw_transcript = transcript_data.get('text', '')
        
        # Save raw transcript
        with open(TRANSCRIPT_FILENAME, 'w', encoding='utf-8') as f:
            f.write(raw_transcript.replace('<br>', '\n'))
        print(f"✅ Raw transcription saved to {TRANSCRIPT_FILENAME}")
        
        # Parse transcription segments
        transcript_segments = parse_transcription_segments(raw_transcript)
        print(f"📝 Parsed {len(transcript_segments)} transcript segments")
        
        # Initialize speaker mapper
        mapper = SpeakerMapper()
        
        # Add caption data to mapper
        for caption in captions_data:
            mapper.add_caption_segment(
                caption['speaker'], 
                caption['caption'], 
                caption.get('timestamp', 0)
            )
        
        # Add transcription segments to mapper
        for segment in transcript_segments:
            mapper.add_transcription_segment(
                segment['speaker_label'],
                segment['text'],
                segment['start_time'],
                segment['end_time']
            )
        
        # Find best matches
        print("🔍 Mapping speaker labels to real names...")
        mapping_results = mapper.find_best_matches()
        
        # Save mapping results
        with open(SPEAKER_MAPPING_FILENAME, 'w', encoding='utf-8') as f:
            json.dump(mapping_results, f, indent=4, ensure_ascii=False)
        print(f"✅ Speaker mapping saved to {SPEAKER_MAPPING_FILENAME}")
        
        # Generate mapped transcript
        mapped_transcript_lines = []
        for segment in transcript_segments:
            real_name = mapper.get_mapped_name(segment['speaker_label'])
            confidence = mapping_results.get(segment['speaker_label'], {}).get('confidence', 0.0)
            
            timestamp_str = f"[{segment['start_time']:.1f} - {segment['end_time']:.1f}]"
            confidence_str = f"(confidence: {confidence:.2f})" if confidence > 0 else ""
            
            line = f"{real_name} {timestamp_str} {confidence_str}\n{segment['text']}\n"
            mapped_transcript_lines.append(line)
        
        # Save mapped transcript
        with open(MAPPED_TRANSCRIPT_FILENAME, 'w', encoding='utf-8') as f:
            f.write('\n'.join(mapped_transcript_lines))
        print(f"✅ Mapped transcript saved to {MAPPED_TRANSCRIPT_FILENAME}")
        
        # Print summary
        print("\n📊 Speaker Mapping Summary:")
        for speaker_label, info in mapping_results.items():
            print(f"  {speaker_label} -> {info['mapped_name']} (confidence: {info['confidence']:.2f})")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Error connecting to whisperx service: {e}")
    except Exception as e:
        print(f"❌ An unexpected error occurred during transcription: {e}")
        import traceback
        traceback.print_exc()

async def join_and_record_meeting(url: str, max_duration: int):
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
                "--use-fake-device-for-media-stream"
            ]
        )
        context = await browser.new_context(permissions=["microphone", "camera"])
        page = await context.new_page()
        recorder = None
        captions_data = []

        try:
            print(f"Navigating to {url}...")
            await page.goto(url, timeout=60000)
            print("Entering a name...")
            await page.locator('input[placeholder="Your name"]').fill("NoteTaker Bot")

            # Turn off mic and camera BEFORE joining
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
            
            # Grace period for captions to appear
            print("Waiting 15 seconds for meeting to stabilize and captions to start...")
            await asyncio.sleep(15)

            # Dynamic recording and caption scraping logic
            print("Bot is now in the meeting. Monitoring participant count and scraping captions...")
            check_interval_seconds = 5
            seen_captions = set()
            
            while True:
                await asyncio.sleep(check_interval_seconds)
                try:
                    # Scrape captions
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
                                captions_data.append({
                                    "speaker": speaker_name, 
                                    "caption": caption_text, 
                                    "timestamp": timestamp
                                })
                                seen_captions.add(caption_key)
                                print(f"CAPTURED: [{speaker_name}] {caption_text}")
                        except Exception as e:
                            print(f"Could not process a caption block: {e}")

                    # Check participant count
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
                    # Enhanced transcription with speaker mapping
                    transcribe_and_map_speakers(OUTPUT_FILENAME, captions_data)
                else:
                    print(f"❌ Recording failed or was empty.\n--- FFmpeg Error Output ---\n{stderr.decode('utf-8', 'ignore')}\n-----------------------------")
            
            # Save captions data
            if captions_data:
                with open(CAPTIONS_FILENAME, 'w', encoding='utf-8') as f:
                    json.dump(captions_data, f, indent=4, ensure_ascii=False)
                print(f"✅ Captions saved to {CAPTIONS_FILENAME}")
            
            await browser.close()
            print("Browser closed.")

if __name__ == "__main__":
    if not MEETING_URL:
        print("Error: Please provide a meeting URL as a command-line argument.")
        sys.exit(1)
    asyncio.run(join_and_record_meeting(MEETING_URL, MAX_MEETING_DURATION_SECONDS))
