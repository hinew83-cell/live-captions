let currentAudio = null;
let currentPlayButton = null;

// Define playAudio globally so it can be called from onclick attributes
window.playAudio = function(segmentId, buttonEl) {
    if (!segmentId || !buttonEl) return;
    
    // If clicked on the currently playing button, pause it
    if (currentAudio && currentPlayButton === buttonEl) {
        currentAudio.pause();
        return;
    }
    
    // Stop any currently playing audio first
    if (currentAudio) {
        currentAudio.pause();
    }
    
    currentAudio = new Audio(`/api/audio/${segmentId}`);
    currentPlayButton = buttonEl;
    
    // Change button icon to pause (||)
    const icon = buttonEl.querySelector("i");
    if (icon) {
        icon.className = "fa-solid fa-pause";
    }
    
    // Disable and fade other play buttons
    const allButtons = document.querySelectorAll(".btn-play-audio");
    allButtons.forEach(btn => {
        if (btn !== buttonEl) {
            btn.disabled = true;
            btn.style.opacity = "0.25";
            btn.style.pointerEvents = "none";
        }
    });
    
    function resetPlaybackState() {
        if (icon) {
            icon.className = "fa-solid fa-play";
        }
        allButtons.forEach(btn => {
            btn.disabled = false;
            btn.style.opacity = "";
            btn.style.pointerEvents = "";
        });
        currentAudio = null;
        currentPlayButton = null;
    }
    
    currentAudio.onended = resetPlaybackState;
    currentAudio.onpause = resetPlaybackState;
    
    currentAudio.play().catch(err => {
        console.error("오디오 재생 실패:", err);
        resetPlaybackState();
    });
};

document.addEventListener("DOMContentLoaded", () => {
    // DOM Elements
    const deviceSelect = document.getElementById("device-select");
    const languageSelect = document.getElementById("language-select");
    const startBtn = document.getElementById("start-btn");
    const stopBtn = document.getElementById("stop-btn");
    const statusDot = document.getElementById("status-dot");
    const statusText = document.getElementById("status-text");
    const audioAmplitude = document.getElementById("audio-amplitude");
    
    const captionBody = document.getElementById("caption-body");
    const clearCaptionBtn = document.getElementById("clear-caption-btn");
    const fontIncreaseBtn = document.getElementById("font-increase-btn");
    const fontDecreaseBtn = document.getElementById("font-decrease-btn");
    
    const copyHistoryBtn = document.getElementById("copy-history-btn");
    const downloadHistoryBtn = document.getElementById("download-history-btn");
    const historyList = document.getElementById("history-list");
    
    const canvas = document.getElementById("waveform-canvas");
    const ctx = canvas.getContext("2d");


    // State Variables
    let ws = null;
    let isListening = false;
    let targetVolume = 0;
    let currentVolume = 0; // for interpolation
    let historyTranscripts = []; // Keep in memory for Copy/Download
    let fontSize = 1.8; // rem
    
    // Waveform visualization animation vars
    let animationId = null;
    let phase = 0;

    // Global closure for text synchronization when edited by user
    window.syncTextChange = function(el) {
        const idx = parseInt(el.getAttribute("data-index"));
        let newText = el.innerText.trim();
        
        if (newText.startsWith("- ")) {
            newText = newText.substring(2);
        }
        
        if (!isNaN(idx) && historyTranscripts[idx]) {
            historyTranscripts[idx].text = newText;
        }
        
        // Sync the edited text between caption feed and raw history logs
        const syncedEls = document.querySelectorAll(`[data-index="${idx}"]`);
        syncedEls.forEach(syncEl => {
            if (syncEl !== el) {
                syncEl.innerText = `- ${newText}`;
            }
        });
    };

    // Initialize Canvas Size
    function resizeCanvas() {
        canvas.width = canvas.parentElement.clientWidth;
        canvas.height = canvas.parentElement.clientHeight;
    }
    resizeCanvas();
    window.addEventListener("resize", resizeCanvas);

    // Fetch Audio Devices
    async function loadDevices() {
        try {
            const response = await fetch("/api/devices");
            const data = await response.json();
            deviceSelect.innerHTML = "";
            
            if (data.devices.length === 0) {
                deviceSelect.innerHTML = `<option value="">장치를 찾을 수 없습니다</option>`;
                return;
            }

            let defaultIdx = 0;
            data.devices.forEach((dev) => {
                const opt = document.createElement("option");
                opt.value = dev.index;
                opt.textContent = dev.name;
                deviceSelect.appendChild(opt);
                
                // Auto-select loopback
                if (dev.is_loopback) {
                    defaultIdx = dev.index;
                }
            });
            
            deviceSelect.value = defaultIdx;
        } catch (err) {
            console.error("장치 목록을 불러오는 중 오류 발생:", err);
            deviceSelect.innerHTML = `<option value="">장치 로드 실패</option>`;
        }
    }
    loadDevices();

    // Setup WebSocket connection
    function connectWebSocket() {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/ws`;
        
        ws = new WebSocket(wsUrl);
        
        ws.onopen = () => {
            console.log("WebSocket 연결 완료");
            updateStatus("active", "연결 완료 (대기)");
        };
        
        ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            
            if (msg.type === "volume") {
                targetVolume = msg.volume;
                audioAmplitude.textContent = `오디오 감지: ${msg.volume}%`;
            } else if (msg.type === "speech_started") {
                showTypingIndicator();
            } else if (msg.type === "caption") {
                removeTypingIndicator();
                addCaption(msg.text, msg.segment_id, msg.isMissed, msg.duration);
            } else if (msg.type === "batch_result") {
                removeTypingIndicator();
                
                // Clear any batch placeholders
                const placeholder = captionBody.querySelector(".batch-recording-placeholder");
                if (placeholder) {
                    placeholder.remove();
                }
                
                if (msg.segments && msg.segments.length > 0) {
                    msg.segments.forEach(seg => {
                        addCaption(seg.text, seg.segment_id, seg.isMissed || false, seg.duration);
                    });
                }
            } else if (msg.type === "status") {
                if (msg.status === "listening") {
                    updateStatus("listening", "녹음/분석 중");
                    addSystemTag("(녹음시작)");
                } else if (msg.status === "stopped") {
                    updateStatus("active", "대기 중");
                    addSystemTag("(녹음중지)");
                } else if (msg.status === "error") {
                    updateStatus("error", msg.message || "오류 발생");
                }
            } else if (msg.type === "error") {
                updateStatus("error", msg.message);
                removeTypingIndicator();
                addCaption(`[오류] ${msg.message}`, null, false);
            }
        };
        
        ws.onclose = () => {
            console.log("WebSocket 연결 닫힘");
            updateStatus("default", "서버 연결 해제됨");
            if (isListening) {
                handleStop();
            }
            // Auto reconnect after 3 seconds
            setTimeout(connectWebSocket, 3000);
        };

        ws.onerror = (err) => {
            console.error("WebSocket 에러:", err);
            updateStatus("error", "연결 에러");
        };
    }
    connectWebSocket();

    // Update Status Indicator
    function updateStatus(state, text) {
        statusDot.className = "status-dot";
        statusDot.classList.add(state);
        statusText.textContent = text;
    }

    // Helper to split Korean sentences at logical endings
    function splitKoreanSentences(text) {
        let marked = text.replace(/(습니다|봅니다|합니다|한다|했다|였다|이다|않는다|그렇다|된다|요|죠|니다|디다)([\s.,!?]+|$)/g, "$1.$2[SPLIT]");
        let parts = marked.split("[SPLIT]");
        return parts.map(p => p.trim()).filter(p => p.length > 0);
    }

    // Show bouncing dot indicator "..."
    function showTypingIndicator() {
        if (captionBody.querySelector(".typing-indicator")) return;
        
        const placeholder = captionBody.querySelector(".caption-placeholder");
        if (placeholder) {
            captionBody.innerHTML = "";
        }

        const indicator = document.createElement("div");
        indicator.className = "caption-line live typing-indicator";
        indicator.style.fontSize = `${fontSize}rem`;
        indicator.innerHTML = `- <span class="typing-dots">...</span>`;
        
        captionBody.appendChild(indicator);
        captionBody.scrollTop = captionBody.scrollHeight;
    }

    // Remove typing indicator
    function removeTypingIndicator() {
        const indicator = captionBody.querySelector(".typing-indicator");
        if (indicator) {
            indicator.remove();
        }
    }

    // Add Caption to screen (Unified Feed)
    function addCaption(text, segmentId, isMissed = false, duration = null) {
        const placeholder = captionBody.querySelector(".caption-placeholder");
        if (placeholder) {
            captionBody.innerHTML = "";
        }
        
        const histPlaceholder = historyList.querySelector(".history-placeholder");
        if (histPlaceholder) {
            historyList.innerHTML = "";
        }

        const currentIndex = historyTranscripts.length;

        if (isMissed) {
            const lines = captionBody.querySelectorAll(".caption-line");
            lines.forEach(l => {
                l.classList.remove("live");
                l.classList.add("history-segment");
            });

            const newLine = document.createElement("div");
            newLine.className = "caption-line live missed-caption";
            newLine.style.fontSize = `${fontSize}rem`;
            
            newLine.innerHTML = `
                <div class="line-content">
                    <button class="btn-play-audio" onclick="playAudio('${segmentId}', this)" title="다시 듣기">
                        <i class="fa-solid fa-play"></i>
                    </button>
                    <span class="missed-badge" title="발음 불명확 (클릭하여 다시 듣기)">
                        <i class="fa-solid fa-exclamation"></i>
                    </span>
                    ${duration ? `<span class="audio-duration-tag">${duration}s</span>` : ''}
                    <span class="bullet-text" contenteditable="true" data-index="${currentIndex}" onblur="syncTextChange(this)" style="color: var(--danger-color); margin-left: 0.5rem; outline: none; border-bottom: 1px dashed rgba(244,63,94,0.3);">- [미인식 구간]</span>
                </div>
            `;
            captionBody.appendChild(newLine);
            
            // Exclude [미인식 구간] from historyList right-panel display per user request
            
            historyTranscripts.push({ text: "[미인식 구간]", isSystemTag: false, isMissed: true });
            
            floatingText.innerHTML = `<span style="color: var(--danger-color); font-weight: 600;"><i class="fa-solid fa-exclamation-triangle"></i> 미인식</span>`;
        } else {
            const sentences = splitKoreanSentences(text);
            
            sentences.forEach((sentence, idx) => {
                const lines = captionBody.querySelectorAll(".caption-line");
                lines.forEach(l => {
                    l.classList.remove("live");
                    l.classList.add("history-segment");
                });
                
                const lineIndex = currentIndex + idx;

                const newLine = document.createElement("div");
                newLine.className = "caption-line live";
                newLine.style.fontSize = `${fontSize}rem`;
                
                newLine.innerHTML = `
                    <div class="line-content">
                        <button class="btn-play-audio" onclick="playAudio('${segmentId}', this)" title="다시 듣기">
                            <i class="fa-solid fa-play"></i>
                        </button>
                        ${duration ? `<span class="audio-duration-tag">${duration}s</span>` : ''}
                        <span class="bullet-text" contenteditable="true" data-index="${lineIndex}" onblur="syncTextChange(this)" style="outline: none;">- ${sentence}</span>
                    </div>
                `;
                
                captionBody.appendChild(newLine);
                
                const histLine = document.createElement("div");
                histLine.className = "history-item";
                histLine.innerHTML = `<p class="history-text" contenteditable="true" data-index="${lineIndex}" onblur="syncTextChange(this)">${sentence}</p>`;
                historyList.appendChild(histLine);
                
                historyTranscripts.push({ text: sentence, isSystemTag: false, isMissed: false });
            });

            historyList.scrollTop = historyList.scrollHeight;

            if (sentences.length > 0) {
                floatingText.textContent = sentences[sentences.length - 1];
            }
        }
        
        captionBody.scrollTop = captionBody.scrollHeight;
    }

    // Add System Tags directly into Feed
    function addSystemTag(text) {
        const placeholder = captionBody.querySelector(".caption-placeholder");
        if (placeholder) {
            captionBody.innerHTML = "";
        }

        const histPlaceholder = historyList.querySelector(".history-placeholder");
        if (histPlaceholder) {
            historyList.innerHTML = "";
        }

        const lines = captionBody.querySelectorAll(".caption-line");
        lines.forEach(l => {
            l.classList.remove("live");
            l.classList.add("history-segment");
        });

        const systemLine = document.createElement("div");
        systemLine.className = "caption-line live system-tag-line";
        systemLine.style.fontSize = `${fontSize}rem`;
        
        const histLine = document.createElement("div");
        histLine.className = "history-item system-tag-item";
        
        if (text === "(녹음시작)") {
            systemLine.innerHTML = `<span class="system-tag start-tag">- (녹음시작)</span>`;
            histLine.innerHTML = `<p class="history-text system-tag">(녹음시작)</p>`;
            historyTranscripts.push({ text: "(녹음시작)", isSystemTag: true });
        } else {
            systemLine.innerHTML = `<span class="system-tag stop-tag">(녹음중지)</span>`;
            histLine.innerHTML = `<p class="history-text system-tag">(녹음중지)</p>`;
            historyTranscripts.push({ text: "(녹음중지)", isSystemTag: true });
        }

        captionBody.appendChild(systemLine);
        historyList.appendChild(histLine);
        
        captionBody.scrollTop = captionBody.scrollHeight;
        historyList.scrollTop = historyList.scrollHeight;
    }

    // Trigger Start/Stop actions
    startBtn.addEventListener("click", () => {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            alert("서버와 연결이 끊겼습니다. 재연결을 기다리는 중입니다.");
            return;
        }
        
        const deviceIndex = parseInt(deviceSelect.value);
        if (isNaN(deviceIndex)) {
            alert("오디오 장치를 선택해 주세요.");
            return;
        }
        
        ws.send(JSON.stringify({
            action: "start",
            device_index: deviceIndex,
            language: languageSelect.value,
            mode: "batch"
        }));
        
        // Display a clear warning/helper message in the caption box
        const placeholder = captionBody.querySelector(".caption-placeholder");
        if (placeholder) {
            captionBody.innerHTML = "";
        }
        const batchMsg = document.createElement("div");
        batchMsg.className = "caption-line live batch-recording-placeholder";
        batchMsg.style.fontSize = `${fontSize}rem`;
        batchMsg.style.color = "var(--text-muted)";
        batchMsg.style.fontStyle = "italic";
        batchMsg.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i> 일괄 변환 녹음이 진행 중입니다. 소리가 감지되고 있으며, 중지 버튼을 누르면 누적된 전체 내용이 한 번에 타이핑됩니다...`;
        captionBody.appendChild(batchMsg);
        
        isListening = true;
        startBtn.disabled = true;
        stopBtn.disabled = false;
        deviceSelect.disabled = true;
        languageSelect.disabled = true;
    });

    stopBtn.addEventListener("click", handleStop);

    function handleStop() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ action: "stop" }));
        }
        
        isListening = false;
        startBtn.disabled = false;
        stopBtn.disabled = true;
        deviceSelect.disabled = false;
        languageSelect.disabled = false;
        targetVolume = 0;
        audioAmplitude.textContent = "오디오 신호 없음";
        updateStatus("active", "대기 중");
    }

    // Clear Caption Box
    clearCaptionBtn.addEventListener("click", () => {
        captionBody.innerHTML = `
            <div class="caption-placeholder">
                <i class="fa-solid fa-microphone-lines-slash"></i>
                <p>자막 기록이 비워졌습니다. 실시간 감지 중인 자막이 아래로 올라옵니다.</p>
            </div>
        `;
        historyList.innerHTML = `
            <div class="history-placeholder" style="color: var(--text-muted); text-align: center; padding: 2rem;">
                추출된 텍스트가 순차적으로 기록되어 마우스로 복사하기 쉽도록 여기에 쌓입니다.
            </div>
        `;
        historyTranscripts = [];
        floatingText.textContent = "자막 대기 중...";
    });

    // Font Controls
    fontIncreaseBtn.addEventListener("click", () => {
        if (fontSize < 3.0) {
            fontSize += 0.2;
            applyFontSize();
        }
    });

    fontDecreaseBtn.addEventListener("click", () => {
        if (fontSize > 1.0) {
            fontSize -= 0.2;
            applyFontSize();
        }
    });

    function applyFontSize() {
        const lines = captionBody.querySelectorAll(".caption-line");
        lines.forEach(l => {
            l.style.fontSize = `${fontSize}rem`;
        });
    }

    // Copy / Download handlers
    copyHistoryBtn.addEventListener("click", () => {
        if (historyTranscripts.length === 0) {
            alert("복사할 기록이 없습니다.");
            return;
        }
        
        const fullText = historyTranscripts
            .filter(item => !item.isMissed)
            .map(item => item.text)
            .join("\n");
        
        navigator.clipboard.writeText(fullText).then(() => {
            const originalText = copyHistoryBtn.innerHTML;
            copyHistoryBtn.innerHTML = `<i class="fa-solid fa-check"></i> 복사됨`;
            setTimeout(() => {
                copyHistoryBtn.innerHTML = originalText;
            }, 2000);
        }).catch(err => {
            console.error("클립보드 복사 실패:", err);
        });
    });

    downloadHistoryBtn.addEventListener("click", () => {
        if (historyTranscripts.length === 0) {
            alert("다운로드할 기록이 없습니다.");
            return;
        }
        
        const fullText = historyTranscripts
            .filter(item => !item.isMissed)
            .map(item => item.text)
            .join("\n");
        
        const blob = new Blob([fullText], { type: "text/plain;charset=utf-8" });
        const timestamp = new Date().toISOString().slice(0, 19).replace(/[-T:]/g, "");
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `live_transcript_${timestamp}.txt`;
        link.click();
        URL.revokeObjectURL(link.href);
    });

    // Waveform canvas rendering loop (Apple style Siri/Soundwave)
    function drawWaveform() {
        animationId = requestAnimationFrame(drawWaveform);
        
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        
        // Smooth out the volume changes
        currentVolume += (targetVolume - currentVolume) * 0.15;
        
        // If not listening, show a minimal flat wave
        const amplitude = isListening ? (currentVolume * 0.6) + 3 : 2;
        
        ctx.strokeStyle = "rgba(76, 201, 240, 0.45)";
        ctx.lineWidth = 2.5;
        
        // Wave 1
        ctx.beginPath();
        for (let x = 0; x < canvas.width; x++) {
            const y = (canvas.height / 2) + Math.sin(x * 0.015 + phase) * amplitude * Math.sin(x * Math.PI / canvas.width);
            if (x === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.stroke();
        
        // Wave 2 (opposite phase, primary color)
        ctx.strokeStyle = "rgba(114, 9, 183, 0.35)";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        for (let x = 0; x < canvas.width; x++) {
            const y = (canvas.height / 2) + Math.sin(x * 0.012 - phase + Math.PI) * (amplitude * 0.7) * Math.sin(x * Math.PI / canvas.width);
            if (x === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Wave 3 (faster phase, bright highlight color)
        ctx.strokeStyle = "rgba(76, 201, 240, 0.6)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let x = 0; x < canvas.width; x++) {
            const y = (canvas.height / 2) + Math.sin(x * 0.02 + phase * 1.5) * (amplitude * 0.4) * Math.sin(x * Math.PI / canvas.width);
            if (x === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.stroke();
        
        phase += 0.08;
    }
    drawWaveform();

    // Floating Mode Overlay Toggle
    floatModeBtn.addEventListener("click", () => {
        floatingOverlay.style.display = "block";
    });

    closeFloatBtn.addEventListener("click", () => {
        floatingOverlay.style.display = "none";
    });

    // Make floating mode draggable
    let isDragging = false;
    let currentX;
    let currentY;
    let initialX;
    let initialY;
    let xOffset = 0;
    let yOffset = 0;

    const dragHandle = floatingOverlay.querySelector(".floating-handle");

    dragHandle.addEventListener("mousedown", dragStart);
    document.addEventListener("mousemove", drag);
    document.addEventListener("mouseup", dragEnd);

    function dragStart(e) {
        initialX = e.clientX - xOffset;
        initialY = e.clientY - yOffset;
        
        if (e.target === dragHandle || dragHandle.contains(e.target)) {
            isDragging = true;
        }
    }

    function drag(e) {
        if (isDragging) {
            e.preventDefault();
            currentX = e.clientX - initialX;
            currentY = e.clientY - initialY;
            
            xOffset = currentX;
            yOffset = currentY;
            
            floatingOverlay.style.transform = `translate(calc(-50% + ${currentX}px), ${currentY}px)`;
        }
    }

    function dragEnd(e) {
        initialX = currentX;
        initialY = currentY;
        isDragging = false;
    }
});
