import pyaudiowpatch as pyaudio

def test_loopback_devices():
    p = pyaudio.PyAudio()
    try:
        print("Testing loopback devices capture...")
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            is_loopback = dev.get('isLoopbackDevice', False)
            is_input = dev['maxInputChannels'] > 0
            
            if is_loopback:
                print(f"\nDevice [{i}]: {dev['name']}")
                print(f"  Channels: {dev['maxInputChannels']}, Rate: {dev['defaultSampleRate']}")
                try:
                    stream = p.open(
                        format=pyaudio.paInt16,
                        channels=dev['maxInputChannels'],
                        rate=int(dev['defaultSampleRate']),
                        input=True,
                        input_device_index=i
                    )
                    stream.read(1024, exception_on_overflow=False)
                    stream.close()
                    print("  -> Capture SUCCESS!")
                except Exception as e:
                    print(f"  -> Capture FAILED: {e}")
    finally:
        p.terminate()

if __name__ == "__main__":
    test_loopback_devices()
