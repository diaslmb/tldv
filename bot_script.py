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

async def debug_page_elements(page):
    """Debug function to help identify caption and participant elements"""
    print("🔍 Debugging page elements...")
    try:
        # Look for any elements containing "caption" or "participant"
        all_elements = await page.query_selector_all("*")
        caption_elements = []
        participant_elements = []
        
        for element in all_elements[:100]:  # Limit to first 100 elements
            try:
                element_text = await element.inner_text()
                element_html = await element.evaluate("el => el.outerHTML")
                
                if element_text and len(element_text.strip()) > 0:
                    if any(word in element_text.lower() for word in ['caption', 'subtitle']):
                        caption_elements.append({
                            'text': element_text[:100],
                            'html': element_html[:200]
                        })
                    elif any(word in element_text.lower() for word in ['participant', 'people']):
                        participant_elements.append({
                            'text': element_text[:50],
                            'html': element_html[:200]
                        })
            except:
                continue
        
        print(f"Found {len(caption_elements)} potential caption elements:")
        for i, elem in enumerate(caption_elements[:3]):
            print(f"  {i+1}. Text: {elem['text']}")
            print(f"      HTML: {elem['html']}")
            
        print(f"Found {len(participant_elements)} potential participant elements:")
        for i, elem in enumerate(participant_elements[:3]):
            print(f"  {i+1}. Text: {elem['text']}")
            print(f"      HTML: {elem['html']}")
            
    except Exception as e:
        print(f"Debug failed: {e}")

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
            
            # Debug page elements
            await debug_page_elements(page)

            # Dynamic recording and caption scraping logic
            print("Bot is now in the meeting. Monitoring participant count and scraping captions...")
            check_interval_seconds = 5
            seen_captions = set()
            consecutive_no_participants = 0
            max_no_participants = 3  # Allow 3 consecutive failures before exiting
            
            while True:
                await asyncio.sleep(check_interval_seconds)
                try:
                    # Try multiple caption selectors (Google Meet changes frequently)
                    # More specific selectors for actual captions, not UI elements
                    caption_selectors = [
                        "div.iTTPOb.VbkSUe",  # Original selector
                        "[data-is-caption='true']",  # Data attribute for captions
                        "[aria-live='polite'] div",  # Live region for captions
                        "[role='log'] > div",  # Direct children of log role
                        "div[data-self-name] + div",  # Adjacent to participant info
                        ".TBMuR.bj4p3b",  # Caption container class (newer)
                        "div[jsname='dsyhDe']",  # Specific jsname for captions
                        "div[jsname='r4nke']",  # Alternative jsname
                    ]
                    
                    captions_found = False
                    for selector in caption_selectors:
                        try:
                            caption_containers = await page.query_selector_all(selector)
                            if caption_containers:
                                print(f"📝 Found {len(caption_containers)} caption elements using selector: {selector}")
                                captions_found = True
                                
                                for container in caption_containers:
                                    try:
                                        # Try multiple ways to extract speaker and text
                                        speaker_name = "Unknown"
                                        caption_text = ""
                                        
                                        # Method 1: Original selectors
                                        try:
                                            speaker_element = await container.query_selector("div.zs7s8d.jxF_2d")
                                            if speaker_element:
                                                speaker_name = await speaker_element.inner_text()
                                            caption_text_element = await container.query_selector("span[jsname='YSxPC']")
                                            if caption_text_element:
                                                caption_text = await caption_text_element.inner_text()
                                        except:
                                            pass
                                        
                                        # Method 2: Generic text extraction
                                        if not caption_text:
                                            try:
                                                full_text = await container.inner_text()
                                                if full_text and len(full_text.strip()) > 0:
                                                    lines = full_text.strip().split('\n')
                                                    if len(lines) >= 2:
                                                        speaker_name = lines[0].strip()
                                                        caption_text = ' '.join(lines[1:]).strip()
                                                    elif len(lines) == 1:
                                                        caption_text = lines[0].strip()
                                            except:
                                                pass
                                        
                                        # Method 3: Just get all text if nothing else works
                                        if not caption_text:
                                            try:
                                                caption_text = await container.inner_text()
                                                caption_text = caption_text.strip()
                                            except:
                                                pass
                                        
                                        if caption_text and len(caption_text) > 3:  # Minimum length filter
                                            # Filter out UI elements and noise
                                            ui_noise = [
                                                # Language options
                                                "afrikaans", "albanian", "amharic", "arabic", "armenian", "azerbaijani",
                                                "basque", "bengali", "bulgarian", "burmese", "catalan", "chinese",
                                                "czech", "dutch", "english", "estonian", "filipino", "finnish",
                                                "french", "galician", "georgian", "german", "greek", "gujarati",
                                                "hebrew", "hindi", "hungarian", "icelandic", "indonesian", "italian",
                                                "japanese", "javanese", "kannada", "kazakh", "khmer", "kinyarwanda",
                                                "korean", "lao", "latvian", "lithuanian", "macedonian", "malay",
                                                "malayalam", "marathi", "mongolian", "nepali", "northern sotho",
                                                "norwegian", "persian", "polish", "portuguese", "romanian", "russian",
                                                "serbian", "sesotho", "sinhala", "slovak", "slovenian", "spanish",
                                                "sundanese", "swahili", "swati", "swedish", "tamil", "telugu",
                                                "thai", "tshivenda", "tswana", "turkish", "ukrainian", "urdu",
                                                "uzbek", "vietnamese", "xhosa", "xitsonga", "zulu", "beta",
                                                # Font options
                                                "font size", "font color", "default", "tiny", "small", "medium",
                                                "large", "huge", "jumbo", "white", "black", "blue", "green",
                                                "yellow", "cyan", "magenta",
                                                # UI elements
                                                "turn on microphone", "turn off microphone", "turn on camera",
                                                "turn off camera", "share screen", "leave call", "meeting details",
                                                "chat with everyone", "meeting tools", "people", "backgrounds and effects",
                                                "show in a tile", "more options", "reactions aren't available",
                                                "raise hand", "turn on captions", "turn off captions", "open caption settings",
                                                "audio settings", "video settings", "gemini isn't taking notes",
                                                "developing an extension", "people have joined by phone",
                                                "people outside the host's organization", "rooms can also contain visitors",
                                                # Material Design icons
                                                "visual_effects", "more_vert", "format_size", "circle", "settings",
                                                "keyboard_arrow_up", "mic_off", "videocam_off", "computer_arrow_up",
                                                "mood", "closed_caption", "back_hand", "call_end", "info", "people",
                                                "chat", "pen_spark_io25", "domain_disabled"
                                            ]
                                            
                                            # Convert to lowercase for comparison
                                            caption_lower = caption_text.lower().strip()
                                            speaker_lower = speaker_name.lower().strip()
                                            
                                            # Check if this is UI noise
                                            is_ui_noise = False
                                            
                                            # Check against known UI text
                                            for noise in ui_noise:
                                                if noise in caption_lower:
                                                    is_ui_noise = True
                                                    break
                                            
                                            # Check for pure icon names (single words with underscores)
                                            if "_" in speaker_name and " " not in speaker_name:
                                                is_ui_noise = True
                                            
                                            # Check for time stamps
                                            if re.match(r'^\d{1,2}:\d{2}
                                                
                                    except Exception as e:
                                        print(f"Could not process a caption container: {e}")
                                break  # Found captions with this selector, no need to try others
                        except Exception as e:
                            continue  # Try next selector
                    
                    if not captions_found:
                        # Try to find live captions area more specifically
                        print("🔍 Looking for live captions area...")
                        try:
                            # Look for elements that might contain live captions
                            potential_caption_areas = await page.query_selector_all([
                                "[aria-live]",  # Live regions
                                "[role='log']",  # Log regions
                                "div[data-allocation-index]",  # Meeting content areas
                                ".U26fgb.mUbCce",  # Google Meet content containers
                            ])
                            
                            for area in potential_caption_areas:
                                area_text = await area.inner_text()
                                if area_text and len(area_text) > 20:  # Has substantial content
                                    # Check if this contains participant names we know
                                    if "Dias Ilyas" in area_text or "NoteTaker Bot" in area_text:
                                        print(f"📝 Found potential caption area with text: {area_text[:200]}...")
                                        
                                        # Try to extract captions from this area
                                        nested_elements = await area.query_selector_all("div, span, p")
                                        for element in nested_elements:
                                            element_text = await element.inner_text()
                                            if element_text and len(element_text.strip()) > 5:
                                                # Try to detect if this looks like a caption
                                                # (contains actual words, not just UI text)
                                                words = element_text.split()
                                                if len(words) >= 2 and not any(ui in element_text.lower() for ui in ["turn on", "turn off", "settings", "options"]):
                                                    print(f"🎯 Potential live caption: {element_text}")
                                                    
                        except Exception as e:
                            print(f"Error searching for caption areas: {e}")

                    # Check participant count with multiple selectors
                    participant_selectors = [
                        "button[aria-label*='participant']",
                        "button[aria-label*='Show everyone']",
                        "button[data-tooltip*='participant']",
                        "button[title*='participant']",
                        "[role='button']:has-text('participant')",
                        "div[data-participant-count]",
                    ]
                    
                    participant_found = False
                    for p_selector in participant_selectors:
                        try:
                            if "has-text" in p_selector:
                                # Handle Playwright text selector
                                participant_elements = await page.query_selector_all("button")
                                for btn in participant_elements:
                                    btn_text = await btn.inner_text()
                                    if "participant" in btn_text.lower():
                                        match = re.search(r'\d+', btn_text)
                                        if match:
                                            participant_count = int(match.group())
                                            print(f"[{participant_count}] participants in the meeting.")
                                            participant_found = True
                                            consecutive_no_participants = 0
                                            if participant_count <= 1:
                                                print("Only 1 participant left. Ending the recording.")
                                                return  # Exit the function to end recording
                                            break
                            else:
                                participant_button = await page.query_selector(p_selector)
                                if participant_button:
                                    participant_count_text = await participant_button.inner_text()
                                    match = re.search(r'\d+', participant_count_text)
                                    if match:
                                        participant_count = int(match.group())
                                        print(f"[{participant_count}] participants in the meeting.")
                                        participant_found = True
                                        consecutive_no_participants = 0
                                        if participant_count <= 1:
                                            print("Only 1 participant left. Ending the recording.")
                                            return  # Exit the function to end recording
                                        break
                        except:
                            continue
                    
                    if not participant_found:
                        consecutive_no_participants += 1
                        print(f"Could not find participant count ({consecutive_no_participants}/{max_no_participants}). Continuing...")
                        if consecutive_no_participants >= max_no_participants:
                            print("Multiple failures to find participant count. Assuming meeting has ended.")
                            break
                        
                except Exception as e:
                    print(f"An unexpected error occurred while monitoring meeting: {e}")
                    consecutive_no_participants += 1
                    if consecutive_no_participants >= max_no_participants:
                        print("Too many consecutive errors. Ending recording.")
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
    asyncio.run(join_and_record_meeting(MEETING_URL, MAX_MEETING_DURATION_SECONDS)), caption_text.strip()):
                                                is_ui_noise = True
                                            
                                            # Check for meeting IDs
                                            if re.match(r'^[a-z]{3}-[a-z]{4}-[a-z]{3}
                                                
                                    except Exception as e:
                                        print(f"Could not process a caption container: {e}")
                                break  # Found captions with this selector, no need to try others
                        except Exception as e:
                            continue  # Try next selector
                    
                    if not captions_found:
                        print("🔍 No captions found with any selector. Checking page content...")
                        # Debug: Print some page content to see what's available
                        try:
                            page_content = await page.content()
                            if "iTTPOb" in page_content:
                                print("📝 Found iTTPOb in page content - captions may be present")
                            if "caption" in page_content.lower():
                                print("📝 Found 'caption' text in page content")
                        except:
                            pass

                    # Check participant count with multiple selectors
                    participant_selectors = [
                        "button[aria-label*='participant']",
                        "button[aria-label*='Show everyone']",
                        "button[data-tooltip*='participant']",
                        "button[title*='participant']",
                        "[role='button']:has-text('participant')",
                        "div[data-participant-count]",
                    ]
                    
                    participant_found = False
                    for p_selector in participant_selectors:
                        try:
                            if "has-text" in p_selector:
                                # Handle Playwright text selector
                                participant_elements = await page.query_selector_all("button")
                                for btn in participant_elements:
                                    btn_text = await btn.inner_text()
                                    if "participant" in btn_text.lower():
                                        match = re.search(r'\d+', btn_text)
                                        if match:
                                            participant_count = int(match.group())
                                            print(f"[{participant_count}] participants in the meeting.")
                                            participant_found = True
                                            consecutive_no_participants = 0
                                            if participant_count <= 1:
                                                print("Only 1 participant left. Ending the recording.")
                                                return  # Exit the function to end recording
                                            break
                            else:
                                participant_button = await page.query_selector(p_selector)
                                if participant_button:
                                    participant_count_text = await participant_button.inner_text()
                                    match = re.search(r'\d+', participant_count_text)
                                    if match:
                                        participant_count = int(match.group())
                                        print(f"[{participant_count}] participants in the meeting.")
                                        participant_found = True
                                        consecutive_no_participants = 0
                                        if participant_count <= 1:
                                            print("Only 1 participant left. Ending the recording.")
                                            return  # Exit the function to end recording
                                        break
                        except:
                            continue
                    
                    if not participant_found:
                        consecutive_no_participants += 1
                        print(f"Could not find participant count ({consecutive_no_participants}/{max_no_participants}). Continuing...")
                        if consecutive_no_participants >= max_no_participants:
                            print("Multiple failures to find participant count. Assuming meeting has ended.")
                            break
                        
                except Exception as e:
                    print(f"An unexpected error occurred while monitoring meeting: {e}")
                    consecutive_no_participants += 1
                    if consecutive_no_participants >= max_no_participants:
                        print("Too many consecutive errors. Ending recording.")
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
    asyncio.run(join_and_record_meeting(MEETING_URL, MAX_MEETING_DURATION_SECONDS)), caption_text.strip()):
                                                is_ui_noise = True
                                            
                                            # Check for very short single words (likely UI elements)
                                            if len(caption_text.strip()) < 10 and " " not in caption_text.strip():
                                                is_ui_noise = True
                                            
                                            # Check for URLs
                                            if "http" in caption_lower or "developers.google.com" in caption_lower:
                                                is_ui_noise = True
                                            
                                            # Only process if not UI noise
                                            if not is_ui_noise:
                                                caption_key = f"{speaker_name}:{caption_text}"
                                                if caption_key not in seen_captions:
                                                    timestamp = asyncio.get_event_loop().time()
                                                    captions_data.append({
                                                        "speaker": speaker_name, 
                                                        "caption": caption_text, 
                                                        "timestamp": timestamp
                                                    })
                                                    seen_captions.add(caption_key)
                                                    print(f"CAPTURED: [{speaker_name}] {caption_text}")
                                            else:
                                                print(f"FILTERED UI: [{speaker_name}] {caption_text}")
                                                
                                    except Exception as e:
                                        print(f"Could not process a caption container: {e}")
                                break  # Found captions with this selector, no need to try others
                        except Exception as e:
                            continue  # Try next selector
                    
                    if not captions_found:
                        print("🔍 No captions found with any selector. Checking page content...")
                        # Debug: Print some page content to see what's available
                        try:
                            page_content = await page.content()
                            if "iTTPOb" in page_content:
                                print("📝 Found iTTPOb in page content - captions may be present")
                            if "caption" in page_content.lower():
                                print("📝 Found 'caption' text in page content")
                        except:
                            pass

                    # Check participant count with multiple selectors
                    participant_selectors = [
                        "button[aria-label*='participant']",
                        "button[aria-label*='Show everyone']",
                        "button[data-tooltip*='participant']",
                        "button[title*='participant']",
                        "[role='button']:has-text('participant')",
                        "div[data-participant-count]",
                    ]
                    
                    participant_found = False
                    for p_selector in participant_selectors:
                        try:
                            if "has-text" in p_selector:
                                # Handle Playwright text selector
                                participant_elements = await page.query_selector_all("button")
                                for btn in participant_elements:
                                    btn_text = await btn.inner_text()
                                    if "participant" in btn_text.lower():
                                        match = re.search(r'\d+', btn_text)
                                        if match:
                                            participant_count = int(match.group())
                                            print(f"[{participant_count}] participants in the meeting.")
                                            participant_found = True
                                            consecutive_no_participants = 0
                                            if participant_count <= 1:
                                                print("Only 1 participant left. Ending the recording.")
                                                return  # Exit the function to end recording
                                            break
                            else:
                                participant_button = await page.query_selector(p_selector)
                                if participant_button:
                                    participant_count_text = await participant_button.inner_text()
                                    match = re.search(r'\d+', participant_count_text)
                                    if match:
                                        participant_count = int(match.group())
                                        print(f"[{participant_count}] participants in the meeting.")
                                        participant_found = True
                                        consecutive_no_participants = 0
                                        if participant_count <= 1:
                                            print("Only 1 participant left. Ending the recording.")
                                            return  # Exit the function to end recording
                                        break
                        except:
                            continue
                    
                    if not participant_found:
                        consecutive_no_participants += 1
                        print(f"Could not find participant count ({consecutive_no_participants}/{max_no_participants}). Continuing...")
                        if consecutive_no_participants >= max_no_participants:
                            print("Multiple failures to find participant count. Assuming meeting has ended.")
                            break
                        
                except Exception as e:
                    print(f"An unexpected error occurred while monitoring meeting: {e}")
                    consecutive_no_participants += 1
                    if consecutive_no_participants >= max_no_participants:
                        print("Too many consecutive errors. Ending recording.")
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
