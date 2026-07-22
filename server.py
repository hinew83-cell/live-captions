import asyncio
import json
import logging
import threading
import time
import math
import numpy as np
import pyaudiowpatch as pyaudio
import speech_recognition as sr
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os
import uuid
import io
import wave
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

def translate_and_detect(text: str):
    """
    Translates text to Korean using Google Translate gtx web API.
    Returns (translated_text, detected_language_code).
    """
    if not text or not text.strip():
        return "", ""
    try:
        url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ko&dt=t&q=" + urllib.parse.quote(text)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3.0) as response:
            result = json.loads(response.read().decode('utf-8'))
            translated_parts = [part[0] for part in result[0] if part[0]]
            translated_text = "".join(translated_parts).strip()
            detected_lang = result[2]
            return translated_text, detected_lang
    except Exception as e:
        logger.error(f"Translation failed: {e}")
        return "", ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize PyAudio as a global singleton to prevent repeated initialization crashes
p_audio = pyaudio.PyAudio()

# Track active audio capture streams to safely re-initialize PortAudio
active_streams_count = 0

# Audio cache for re-listening to segments
audio_cache = {}

def pcm_to_wav(pcm_data, sample_rate, channels, sample_width):
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, 'wb') as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return wav_buf.getvalue()

class AudioTranscriber:
    def __init__(self, websocket: WebSocket, main_loop=None):
        self.websocket = websocket
        self.main_loop = main_loop
        self.p = p_audio
        self.stream = None
        self.is_running = False
        self.thread = None
        self.recognizer = sr.Recognizer()
        
        # Audio configuration
        self.channels = 2
        self.sample_rate = 44100
        self.chunk_size = 1024
        
        # Buffers for transcription
        self.audio_buffer = bytearray()
        self.silence_timer = 0
        self.speech_started = False
        
        self.language = "ko-KR"
        self.mode = "realtime"
        self.session_audio_data = bytearray()
        self.session_segments = []
        self.silence_threshold = 300  # Dynamic starting threshold
        self.silence_duration = 0.95  # Natural pause trigger (0.95s gap)
        self.max_speech_duration = 7.5  # Collect up to 7.5s of speech for rich context
        self.rms_history = []
        
    def get_devices(self):
        devices = []
        try:
            wasapi_info = self.p.get_host_api_info_by_type(pyaudio.paWASAPI)
            wasapi_idx = wasapi_info['index']
        except OSError:
            wasapi_idx = -1

        try:
            default_output = self.p.get_default_output_device_info()
            default_output_idx = default_output['index'] if default_output else -1
        except Exception:
            default_output_idx = -1

        for i in range(self.p.get_device_count()):
            try:
                dev = self.p.get_device_info_by_index(i)
                is_loopback = dev.get('isLoopbackDevice', False)
                
                # We only want PC sound loopback devices
                if not is_loopback:
                    continue
                
                # Clean name: remove trailing "[Loopback]" if it exists, since we label it
                clean_name = dev['name'].replace('[Loopback]', '').replace('(Loopback)', '').strip()
                display_name = f"💻 [PC 소리 수신] {clean_name}"
                
                devices.append({
                    "index": i,
                    "name": display_name,
                    "is_loopback": is_loopback,
                    "is_default": dev['index'] == default_output_idx,
                    "channels": dev['maxInputChannels'],
                    "type": "Loopback"
                })
            except Exception as e:
                logger.error(f"Error reading device {i}: {e}")
        return devices

    def start_listening(self, device_index: int, language: str = "ko-KR", mode: str = "batch"):
        if self.is_running:
            self.stop_listening()
            
        self.language = language
        self.mode = "batch"
        self.session_audio_data = bytearray()
        self.session_segments = []
        self.is_running = True
        self.audio_buffer = bytearray()
        self.speech_started = False
        
        # Find device details
        device_info = self.p.get_device_info_by_index(device_index)
        self.channels = device_info['maxInputChannels']
        self.sample_rate = int(device_info['defaultSampleRate'])
        
        logger.info(f"Starting WASAPI Loopback on device index {device_index} ({device_info['name']})")
        logger.info(f"Channels: {self.channels}, Sample Rate: {self.sample_rate}")
        
        formats_to_try = [pyaudio.paInt16, pyaudio.paFloat32]
        rates_to_try = [self.sample_rate]
        if 44100 not in rates_to_try:
            rates_to_try.append(44100)
        if 48000 not in rates_to_try:
            rates_to_try.append(48000)
            
        success = False
        for fmt in formats_to_try:
            for rate in rates_to_try:
                try:
                    logger.info(f"Trying to open stream: format={fmt}, rate={rate}, channels={self.channels}")
                    self.stream = self.p.open(
                        format=fmt,
                        channels=self.channels,
                        rate=int(rate),
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=self.chunk_size
                    )
                    self.audio_format = fmt
                    self.sample_rate = int(rate)
                    logger.info(f"Successfully opened audio stream! (Format: {fmt}, Rate: {rate})")
                    success = True
                    break
                except Exception as e:
                    logger.warning(f"Failed combination (Format: {fmt}, Rate: {rate}): {e}")
            if success:
                break
                
        if not success:
            logger.error("Failed to open audio stream after trying all format/sample rate combinations.")
            self.is_running = False
            return False
            
        global active_streams_count
        active_streams_count += 1
            
        self.thread = threading.Thread(target=self._audio_loop, daemon=True)
        self.thread.start()
        return True

    def stop_listening(self):
        if not self.is_running and self.stream is None:
            return
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=2.0)
            self.thread = None
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception as e:
                logger.error(f"Error closing stream: {e}")
            self.stream = None
            
        global active_streams_count
        active_streams_count = max(0, active_streams_count - 1)
        logger.info("Stopped listening.")
        
        # Transcribe final remaining audio buffer if not empty
        if self.audio_buffer and len(self.audio_buffer) > 0:
            duration = round(len(self.audio_buffer) / (self.sample_rate * self.channels * 2), 1)
            if duration >= 0.8:
                logger.info("Transcribing final remaining audio buffer...")
                try:
                    import numpy as np
                    audio_data = np.frombuffer(self.audio_buffer, dtype=np.int16)
                    if self.channels == 2:
                        raw_audio_mono = ((audio_data[0::2].astype(np.int32) + audio_data[1::2].astype(np.int32)) // 2).astype(np.int16).tobytes()
                    else:
                        raw_audio_mono = audio_data.tobytes()
                    audio_data_sr = sr.AudioData(raw_audio_mono, self.sample_rate, 2)
                    text = self.recognizer.recognize_google(audio_data_sr, language=self.language)
                    final_text = text
                    is_missed = False
                except Exception as e:
                    logger.info(f"Final block transcription note: {e}")
                    final_text = "[발음 불명확 / 미인식]"
                    is_missed = True
                
                self.session_segments.append({
                    "text": final_text,
                    "start_time": self.current_segment_start_time,
                    "duration": duration,
                    "isMissed": is_missed
                })
        
        # If in batch mode, compile and send the final list of segments
        if self.mode == "batch":
            session_id = str(uuid.uuid4())
            total_duration = 0.0
            
            if self.session_audio_data:
                total_duration = round(len(self.session_audio_data) / (self.sample_rate * 2), 1)
                wav_bytes = pcm_to_wav(bytes(self.session_audio_data), self.sample_rate, channels=1, sample_width=2)
                audio_cache[session_id] = wav_bytes
                if len(audio_cache) > 100:
                    audio_cache.pop(next(iter(audio_cache)))
                    
            if not self.session_segments:
                self.session_segments.append({
                    "text": "[일괄 변환 결과 없음 / 감지된 음성 없음]",
                    "start_time": 0.0,
                    "duration": 0.0,
                    "isMissed": False
                })
            
            logger.info(f"Sending batch result. Segments count: {len(self.session_segments)}, Session ID: {session_id}, Duration: {total_duration}s")
            if self.main_loop and self.websocket:
                asyncio.run_coroutine_threadsafe(
                    self.websocket.send_json({
                        "type": "batch_result",
                        "session_id": session_id,
                        "segments": self.session_segments,
                        "timestamp": time.time()
                    }),
                    self.main_loop
                )

    def _audio_loop(self):
        last_transcribe_time = time.time()
        silence_start_time = None
        
        # Audio stats for silence detection (we convert everything to int16 which is 2 bytes)
        sample_width = 2
        
        while self.is_running:
            try:
                # Read raw data
                if self.stream.get_read_available() > 0:
                    raw_data = self.stream.read(self.chunk_size, exception_on_overflow=False)
                else:
                    time.sleep(0.01)
                    continue
                
                # Convert format if opened in paFloat32
                if self.audio_format == pyaudio.paFloat32:
                    audio_data_float = np.frombuffer(raw_data, dtype=np.float32)
                    audio_data = (audio_data_float * 32767.0).clip(-32768, 32767).astype(np.int16)
                    data = audio_data.tobytes()
                else:
                    audio_data = np.frombuffer(raw_data, dtype=np.int16)
                    data = raw_data
                    
                if len(audio_data) == 0:
                    continue
                
                if self.channels == 2:
                    chunk_mono = ((audio_data[0::2].astype(np.int32) + audio_data[1::2].astype(np.int32)) // 2).astype(np.int16).tobytes()
                else:
                    chunk_mono = audio_data.tobytes()
                
                if self.mode == "batch":
                    self.session_audio_data.extend(chunk_mono)
                
                # Calculate RMS volume level
                rms = np.sqrt(np.mean(audio_data.astype(np.float64)**2))
                
                # Track noise floor to dynamically adjust threshold (handles BGM/noise)
                self.rms_history.append(rms)
                if len(self.rms_history) > 150:  # ~3.5 seconds history
                    self.rms_history.pop(0)
                
                noise_floor = np.percentile(self.rms_history, 20)
                self.silence_threshold = max(150, min(700, int(noise_floor + 100)))
                
                # Send volume level to client for visualization
                # Map volume to a 0-100 range
                norm_volume = min(100, int((rms / 32768.0) * 800))
                
                if self.main_loop and self.websocket:
                    asyncio.run_coroutine_threadsafe(
                        self.websocket.send_json({"type": "volume", "volume": norm_volume}), 
                        self.main_loop
                    )
                
                # Detect speech / silence
                is_speech = rms > self.silence_threshold
                
                if is_speech:
                    if not self.speech_started:
                        self.speech_started = True
                        self.current_segment_start_time = round(len(self.session_audio_data) / (self.sample_rate * 2), 1)
                        logger.info(f"Speech started at session offset: {self.current_segment_start_time}s")
                        if self.main_loop and self.websocket:
                            asyncio.run_coroutine_threadsafe(
                                self.websocket.send_json({"type": "speech_started"}),
                                self.main_loop
                            )
                    self.audio_buffer.extend(data)
                    silence_start_time = None
                else:
                    if self.speech_started:
                        self.audio_buffer.extend(data)
                        if silence_start_time is None:
                            silence_start_time = time.time()
                        elif time.time() - silence_start_time > self.silence_duration:
                            # Silence duration exceeded, trigger transcription
                            logger.info("Silence detected. Transcribing segment...")
                            self._trigger_transcribe(bytes(self.audio_buffer), sample_width, self.current_segment_start_time)
                            self.audio_buffer = bytearray()
                            self.speech_started = False
                            silence_start_time = None
                
                # Force transcription if audio buffer grows too long
                duration = len(self.audio_buffer) / (self.sample_rate * self.channels * sample_width)
                if duration > self.max_speech_duration:
                    logger.info("Max speech duration reached. Forcing transcription...")
                    self._trigger_transcribe(bytes(self.audio_buffer), sample_width, self.current_segment_start_time)
                    self.audio_buffer = bytearray()
                    self.speech_started = False
                    silence_start_time = None
                    
            except Exception as e:
                logger.error(f"Error in audio loop: {e}")
                time.sleep(0.1)

    def _trigger_transcribe(self, raw_audio, sample_width, start_time):
        # run transcription in a separate thread so it doesn't block audio capture
        t = threading.Thread(
            target=self._transcribe_worker, 
            args=(raw_audio, sample_width, self.sample_rate, self.channels, self.language, start_time),
            daemon=True
        )
        t.start()

    def _transcribe_worker(self, raw_audio, sample_width, sample_rate, channels, language, start_time):
        try:
            # Check if there's enough sound to transcribe
            audio_np = np.frombuffer(raw_audio, dtype=np.int16)
            if len(audio_np) == 0 or np.max(np.abs(audio_np)) < self.silence_threshold:
                return

            # SpeechRecognition requires mono audio for Google Web Speech
            # If channels > 1, we downmix to mono by averaging channels
            if channels > 1:
                audio_np = audio_np.reshape(-1, channels)
                audio_mono = audio_np.mean(axis=1).astype(np.int16)
                raw_audio_mono = audio_mono.tobytes()
                channels_mono = 1
            else:
                raw_audio_mono = raw_audio
                channels_mono = channels

            # Calculate duration in seconds
            duration = round(len(raw_audio_mono) / (sample_rate * sample_width), 1)
            
            # Filter out segments shorter than 0.8 seconds to minimize unrecognized noise segments
            if duration < 0.8:
                logger.info(f"Skipping too short segment: {duration}s")
                return

            audio_data = sr.AudioData(raw_audio_mono, sample_rate, sample_width)
            
            # Cache the audio clip in WAV format
            segment_id = str(uuid.uuid4())
            wav_bytes = pcm_to_wav(raw_audio_mono, sample_rate, channels=1, sample_width=sample_width)
            audio_cache[segment_id] = wav_bytes
            if len(audio_cache) > 100:
                audio_cache.pop(next(iter(audio_cache)))
            
            logger.info(f"Sending request to Google Web Speech API (Lang: {language})...")
            text = self.recognizer.recognize_google(audio_data, language=language)
            
            if not text or not text.strip():
                raise sr.UnknownValueError()
                
            # If the selected language is not Korean, translate it to Korean
            if language != "ko-KR":
                translation, _ = translate_and_detect(text)
                if translation:
                    final_text = f"{text} ({translation})"
                else:
                    final_text = text
            else:
                final_text = text
            
            logger.info(f"Transcription result: {final_text}")
            
            if self.mode == "batch":
                self.session_segments.append({
                    "text": final_text,
                    "start_time": start_time,
                    "duration": duration,
                    "isMissed": False
                })
            else:
                if self.main_loop and self.websocket:
                    asyncio.run_coroutine_threadsafe(
                        self.websocket.send_json({
                            "type": "caption",
                            "text": final_text,
                            "segment_id": segment_id,
                            "duration": duration,
                            "timestamp": time.time()
                        }),
                        self.main_loop
                    )
        except sr.UnknownValueError:
            logger.info("Speech Recognition could not understand audio")
            
            # Calculate duration for unrecognized segment to check if we should discard it
            duration = round(len(raw_audio_mono) / (sample_rate * sample_width), 1)
            if duration < 0.8:
                return
                
            segment_id = str(uuid.uuid4())
            wav_bytes = pcm_to_wav(raw_audio_mono, sample_rate, channels=1, sample_width=sample_width)
            audio_cache[segment_id] = wav_bytes
            if len(audio_cache) > 100:
                audio_cache.pop(next(iter(audio_cache)))
                
            if self.mode == "batch":
                self.session_segments.append({
                    "text": "[발음 불명확 / 미인식]",
                    "start_time": start_time,
                    "duration": duration,
                    "isMissed": True
                })
            else:
                if self.main_loop and self.websocket:
                    asyncio.run_coroutine_threadsafe(
                        self.websocket.send_json({
                            "type": "caption",
                            "text": "[발음 불명확 / 미인식]",
                            "isMissed": True,
                            "segment_id": segment_id,
                            "duration": duration,
                            "timestamp": time.time()
                        }),
                        self.main_loop
                    )
        except sr.RequestError as e:
            logger.error(f"Could not request results from Google Speech Recognition service; {e}")
            if self.main_loop and self.websocket:
                asyncio.run_coroutine_threadsafe(
                    self.websocket.send_json({
                        "type": "error",
                        "message": "구글 STT API 요청 실패 (인터넷 연결을 확인하세요)"
                    }),
                    self.main_loop
                )
        except Exception as e:
            logger.error(f"Error in transcription worker: {e}")

    def close(self):
        self.stop_listening()

@app.get("/api/devices")
def get_devices():
    global p_audio
    if active_streams_count == 0:
        try:
            logger.info("Refreshing PyAudio instance to detect newly connected devices...")
            p_audio.terminate()
        except Exception as e:
            logger.error(f"Error terminating PyAudio instance: {e}")
        p_audio = pyaudio.PyAudio()
        
    transcriber = AudioTranscriber(None)
    return {"devices": transcriber.get_devices()}

@app.get("/api/audio/{segment_id}")
def get_audio(segment_id: str):
    if segment_id in audio_cache:
        return Response(content=audio_cache[segment_id], media_type="audio/wav")
    return Response(status_code=404, content="Audio not found")

@app.on_event("shutdown")
def shutdown_event():
    logger.info("Terminating global PyAudio instance...")
    p_audio.terminate()

global_transcriber = None

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection established")
    
    global global_transcriber
    loop = asyncio.get_running_loop()
    
    # If there is already a running transcriber, attach the new websocket and event loop to it!
    if global_transcriber and global_transcriber.is_running:
        logger.info("Attaching new websocket to active background transcriber")
        global_transcriber.websocket = websocket
        global_transcriber.main_loop = loop
        # Immediately notify client that we are already recording
        await websocket.send_json({
            "type": "status",
            "status": "listening",
            "message": "기존 녹음 세션에 연결되었습니다."
        })
    else:
        # Create a new transcriber instance
        global_transcriber = AudioTranscriber(websocket, loop)
        
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            action = message.get("action")
            
            if action == "start":
                device_index = message.get("device_index", 0)
                language = message.get("language", "ko-KR")
                mode = message.get("mode", "batch")
                
                if not global_transcriber:
                    global_transcriber = AudioTranscriber(websocket, loop)
                else:
                    global_transcriber.websocket = websocket
                    global_transcriber.main_loop = loop
                    
                success = global_transcriber.start_listening(device_index, language, mode)
                await websocket.send_json({
                    "type": "status",
                    "status": "listening" if success else "error",
                    "message": "음성 감지를 시작했습니다." if success else "오디오 캡처 장치를 열 수 없습니다."
                })
            elif action == "stop":
                if global_transcriber:
                    global_transcriber.stop_listening()
                await websocket.send_json({
                    "type": "status",
                    "status": "stopped",
                    "message": "음성 감지를 중지했습니다."
                })
                
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # DO NOT call close() or stop_listening() here so that the background thread remains active!
        # Just detach the websocket references so we don't try to send data to a closed socket.
        if global_transcriber:
            global_transcriber.websocket = None
            global_transcriber.main_loop = None

# Mount frontend static files
# Make sure this directory exists or we create it
frontend_path = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(frontend_path):
    os.makedirs(frontend_path)

app.mount("/", StaticFiles(directory=frontend_path, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
