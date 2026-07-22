import pyaudiowpatch as pyaudio

def list_devices():
    p = pyaudio.PyAudio()
    try:
        # Get default WASAPI info if available
        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            print(f"WASAPI Host API found: Index {wasapi_info['index']}")
        except OSError:
            wasapi_info = None
            print("WASAPI Host API NOT found.")

        print("\n--- Audio Devices ---")
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            is_input = dev['maxInputChannels'] > 0
            is_output = dev['maxOutputChannels'] > 0
            is_loopback = dev.get('isLoopbackDevice', False)
            
            type_str = []
            if is_input: type_str.append("Input")
            if is_output: type_str.append("Output")
            if is_loopback: type_str.append("Loopback")
            
            print(f"[{i}] {dev['name']} - Channels: In {dev['maxInputChannels']}, Out {dev['maxOutputChannels']} - {', '.join(type_str)}")

        # Find loopback device associated with default output device
        if wasapi_info:
            try:
                default_speakers = p.get_default_output_device_info()
                print(f"\nDefault Speaker Info: index={default_speakers['index']}, name={default_speakers['name']}")
                
                # Look for loopback device matching default speaker
                loopback_found = None
                for i in range(p.get_device_count()):
                    dev = p.get_device_info_by_index(i)
                    if dev['hostApi'] == wasapi_info['index'] and dev.get('isLoopbackDevice', False):
                        if default_speakers['name'] in dev['name']:
                            loopback_found = dev
                            break
                
                if loopback_found:
                    print(f"Found Loopback Device for default speakers: Index {loopback_found['index']} - {loopback_found['name']}")
                else:
                    print("Could not find loopback device for default speakers. Listing all loopbacks:")
                    for i in range(p.get_device_count()):
                        dev = p.get_device_info_by_index(i)
                        if dev.get('isLoopbackDevice', False):
                            print(f"  Index {dev['index']} - {dev['name']}")
            except Exception as e:
                print("Error finding loopback device:", e)
                
    finally:
        p.terminate()

if __name__ == "__main__":
    list_devices()
