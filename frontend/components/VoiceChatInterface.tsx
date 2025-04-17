/**
 * src/components/VoiceChatInterface.tsx
 *
 * Frontend component for interacting with the ADK Voice Agent via WebSocket.
 * Handles audio recording, sending audio chunks, receiving audio/text,
 * playing back audio, and displaying the conversation.
 */
"use client"; // Required for components using hooks and browser APIs

import React, { useState, useEffect, useRef, useCallback } from 'react';

// Generate a simple session ID for this client instance
const SESSION_ID = `session_${Math.random().toString(36).substring(7)}`;

// Define WebSocket URL. Use environment variable for flexibility, fallback for local dev.
// Ensure the backend FastAPI server runs on port 8000 if using the fallback.
const WEBSOCKET_URL = process.env.NEXT_PUBLIC_WEBSOCKET_URL || `ws://localhost:8000/ws/${SESSION_ID}`;

// --- Enums for State Management ---
enum ConnectionStatus {
  Disconnected = 'Disconnected',
  Connecting = 'Connecting',
  Connected = 'Connected',
  Error = 'Error',
}

enum AgentStatus {
  Idle = 'Idle',          // Waiting for user input
  Listening = 'Listening', // Currently recording user audio
  Processing = 'Processing', // User finished speaking, waiting for agent response
  Speaking = 'Speaking',   // Agent is sending back audio/text response
}

// --- Interface for Conversation Turns ---
interface ConversationTurn {
  speaker: 'user' | 'agent';
  text: string;
}

// --- Interface to handle potential browser differences for AudioContext ---
interface WindowWithAudioContext extends Window {
  AudioContext?: typeof AudioContext; // Standard API
  webkitAudioContext?: typeof AudioContext; // Prefixed version for older Safari
}

// --- Main Component ---
export default function VoiceChatInterface() {
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>(ConnectionStatus.Disconnected);
  const [agentStatus, setAgentStatus] = useState<AgentStatus>(AgentStatus.Idle);
  const [conversation, setConversation] = useState<ConversationTurn[]>([]);
  const [isRecording, setIsRecording] = useState(false);

  // Refs for managing WebSocket, MediaRecorder, AudioContext, and state that shouldn't trigger re-renders
  const ws = useRef<WebSocket | null>(null);
  const mediaRecorder = useRef<MediaRecorder | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const audioQueue = useRef<ArrayBuffer[]>([]); // Queue for incoming audio bytes from the agent
  const isPlaying = useRef<boolean>(false); // Flag to prevent overlapping audio playback
  const audioChunks = useRef<Blob[]>([]); // Ref to hold recorded chunks locally if needed (currently unused for sending)


  // --- Audio Playback Logic ---
  const playNextAudioChunk = useCallback(async () => {
    // Ensure AudioContext is ready and not suspended (requires user interaction sometimes)
    if (audioContext.current?.state === 'suspended') {
      try {
        await audioContext.current.resume();
        console.log("AudioContext resumed");
      } catch (err) {
        console.error("Failed to resume AudioContext:", err);
        isPlaying.current = false; // Can't play if context is suspended
        return;
      }
    }

    // Exit if already playing, queue is empty, or context isn't ready
    if (isPlaying.current || audioQueue.current.length === 0 || !audioContext.current) {
      return;
    }

    isPlaying.current = true; // Set playing flag
    const audioData = audioQueue.current.shift(); // Get the next audio chunk from the queue

    if (audioData) {
      try {
        const sampleRate = OUTPUT_SAMPLE_RATE; // Get this from backend if possible, or hardcode
        const numberOfChannels = 1; // Assuming mono output from Gemini
                // Create an empty AudioBuffer
                // Length is number of samples: byteLength / bytesPerSample (2 for 16-bit)
                const frameCount = audioData.byteLength / 2;
                const audioBuffer = audioContext.current.createBuffer(
                    numberOfChannels,
                    frameCount,
                    sampleRate
                );

                // Get the channel data buffer (Float32Array)
                const channelData = audioBuffer.getChannelData(0);

                // Convert Int16 PCM data to Float32 required by Web Audio API
                // DataView allows reading multi-byte values respecting endianness
                const pcmView = new DataView(audioData);
                for (let i = 0; i < frameCount; i++) {
                    // Read Int16 value (assuming little-endian), divide by 32768 to normalize to [-1.0, 1.0]
                    channelData[i] = pcmView.getInt16(i * 2, true) / 32768.0;
                }
        console.log("Decoding audio chunk:", audioData.byteLength);
        // Decode the ArrayBuffer (raw audio data) into an AudioBuffer
        //const audioBuffer = await audioContext.current.decodeAudioData(audioData);

        // Create a source node to play the buffer
        const source = audioContext.current.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(audioContext.current.destination); // Connect to speakers
        source.start(); // Play the audio
        console.log(`Playing raw PCM audio chunk: ${audioData.byteLength} bytes`);
        
        // When this chunk finishes playing, reset the flag and try to play the next chunk
        source.onended = () => {
          console.log("Audio chunk finished playing.");
          isPlaying.current = false;
          playNextAudioChunk(); // Recursively call to process the rest of the queue
        };
      } catch (error) {
        console.error("Error decoding/playing audio data:", error);
        isPlaying.current = false; // Reset flag on error
        playNextAudioChunk(); // Attempt to play the next chunk even if this one failed
      }
    } else {
      // No data was in the queue, reset playing flag
      isPlaying.current = false;
       // Optional: If agent was speaking but queue is empty, maybe update status
       // if (agentStatus === AgentStatus.Speaking) {
       //  setAgentStatus(AgentStatus.Idle);
       // }
    }
    // This useCallback has no dependencies because it relies on refs and state updates
    // handled elsewhere (onmessage). AgentStatus check inside was removed as dependency
    // to prevent unnecessary function recreation and potential reconnect loops.
  }, []);

  // --- WebSocket Connection Logic ---
  const connectWebSocket = useCallback(() => {
    if (ws.current && ws.current.readyState === WebSocket.OPEN) {
      console.log("WebSocket already open.");
      return;
    }

    console.log(`Attempting to connect to ${WEBSOCKET_URL}`);
    setConnectionStatus(ConnectionStatus.Connecting);
    setAgentStatus(AgentStatus.Idle);
    setConversation([]); // Clear conversation on new connection
    audioQueue.current = []; // Clear audio queue
    isPlaying.current = false; // Reset playback flag

    ws.current = new WebSocket(WEBSOCKET_URL);

    ws.current.onopen = () => {
      console.log("WebSocket Connected");
      setConnectionStatus(ConnectionStatus.Connected);
      setAgentStatus(AgentStatus.Idle);
    };

    ws.current.onclose = (event) => {
      console.log("WebSocket Disconnected:", event.reason, event.code);
      setConnectionStatus(ConnectionStatus.Disconnected);
      setAgentStatus(AgentStatus.Idle);
      setIsRecording(false); // Ensure recording stops if connection drops
      if (mediaRecorder.current && mediaRecorder.current.state === 'recording') {
        mediaRecorder.current.stop();
      }
      // Optional: Implement automatic reconnection strategy here if desired
      // setTimeout(connectWebSocket, 5000);
    };

    ws.current.onerror = (error) => {
      console.error("WebSocket Error:", error);
      setConnectionStatus(ConnectionStatus.Error);
      setAgentStatus(AgentStatus.Idle);
    };

    // Handle incoming messages (Audio chunks, Text, Status)
    ws.current.onmessage = (event) => {
      // Use instanceof for type guarding binary data
      if (event.data instanceof Blob) {
        // If backend sends Blob, convert to ArrayBuffer
        console.log("Received audio chunk (Blob):", event.data.size);
        const reader = new FileReader();
        reader.onload = function() {
            if (reader.result instanceof ArrayBuffer) {
                audioQueue.current.push(reader.result);
                playNextAudioChunk(); // Attempt to play
            }
        };
        reader.readAsArrayBuffer(event.data);
        setAgentStatus(AgentStatus.Speaking);

      } else if (event.data instanceof ArrayBuffer) {
        // If backend sends ArrayBuffer directly
        console.log("Received audio chunk (ArrayBuffer):", event.data.byteLength);
        audioQueue.current.push(event.data);
        playNextAudioChunk(); // Attempt to play
        setAgentStatus(AgentStatus.Speaking);

      } else {
        // Assume JSON message for text, transcriptions, status
        try {
          const message = JSON.parse(event.data);
          console.log("Message from server:", message);

          switch (message.type) {
            case 'text_output': // Agent's main text response
              setConversation(prev => [...prev, { speaker: 'agent', text: message.text }]);
              // Agent might still be speaking audio even if text arrives
              break;

            case 'input_transcription': // Transcription of user's speech
               setConversation(prev => {
                   const last = prev[prev.length - 1];
                   // Update the placeholder if it exists
                   if (last?.speaker === 'user' && last.text.startsWith('🎙️...')) {
                       return [...prev.slice(0, -1), { speaker: 'user', text: `🎙️ ${message.text}` }];
                   } else { // Otherwise, add as new user turn (less likely with streaming audio)
                       return [...prev, { speaker: 'user', text: `🎙️ ${message.text}` }];
                   }
               });
               setAgentStatus(AgentStatus.Processing); // User potentially finished, agent working
              break;

            case 'output_transcription': // Live transcription of agent's speech
               setConversation(prev => {
                 const last = prev[prev.length - 1];
                 // Append to the last agent message if streaming text
                 if (last?.speaker === 'agent') {
                     return [...prev.slice(0, -1), { speaker: 'agent', text: last.text + message.text }];
                 } else { // Start a new agent message
                    return [...prev, { speaker: 'agent', text: message.text }];
                 }
               });
               setAgentStatus(AgentStatus.Speaking);
              break;

            case 'status': // Agent status updates
              if (message.status === 'turn_complete') {
                console.log("Agent turn complete.");
                setAgentStatus(AgentStatus.Idle);
                playNextAudioChunk(); // Ensure any remaining queued audio is played
              } else if (message.status === 'interrupted') {
                 console.log("Agent interrupted by user.");
                 setAgentStatus(AgentStatus.Idle); // Or Listening if user is now speaking
                 // Clear pending audio from the interrupted agent turn
                 audioQueue.current = [];
                 isPlaying.current = false;
                 // TODO: Need a way to stop current playback node if using AudioContext directly
                 // If using <audio> element, could just pause/reset it.
              }
              break;
            default:
              console.warn("Received unknown message type:", message.type);
          }
        } catch (e) {
          console.error("Failed to parse server message or invalid message format:", e, event.data);
        }
      }
    };
    // connectWebSocket depends on playNextAudioChunk because it's called in onmessage.
    // playNextAudioChunk is stable (empty dependency array).
  }, [playNextAudioChunk]);

  // Effect runs on mount: Initialize AudioContext and connect WebSocket
  useEffect(() => {
    // Safely initialize AudioContext considering browser differences
    const ExtendedWindow = window as WindowWithAudioContext; // Cast window to our extended interface
    // Check for standard AudioContext first, then the prefixed version
    const AudioContextClass = ExtendedWindow.AudioContext || ExtendedWindow.webkitAudioContext;

    if (!AudioContextClass) {
      console.error("Browser does not support AudioContext.");
      setConnectionStatus(ConnectionStatus.Error); // Indicate error state
      // Optionally: display an error message to the user in the UI
    } else {
      // Create the AudioContext instance
      audioContext.current = new AudioContextClass();
    }

    // Establish WebSocket connection
    connectWebSocket();

    // Cleanup function runs on component unmount
    return () => {
      console.log("Cleaning up VoiceChatInterface...");
      ws.current?.close(); // Close WebSocket connection
      if (mediaRecorder.current && mediaRecorder.current.state === 'recording') {
        mediaRecorder.current.stop(); // Stop recording if active
      }
      // Close the AudioContext
      audioContext.current?.close().catch(e => console.error("Error closing AudioContext", e));
    };
  }, [connectWebSocket]); // connectWebSocket is stable after its dependency fix

  // --- Audio Recording Logic ---
  const startRecording = async () => {
    // Pre-checks
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert("Your browser does not support audio recording.");
      return;
    }
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) {
        alert("WebSocket not connected. Please wait or try reconnecting.");
        return;
    }
    // Ensure AudioContext is active (might require user interaction)
     if (audioContext.current?.state === 'suspended') {
       try {
           await audioContext.current.resume();
       } catch (err) {
            console.error("AudioContext resume failed:", err);
            alert("Could not activate audio. Please interact with the page (e.g., click) and try again.");
            return;
       }
     }

    // Request microphone access
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

      // Use a common, widely supported format if possible, check browser compatibility if needed
      // Opus in WebM is generally good for quality/compression. Adjust if necessary.
      const options = { mimeType: 'audio/webm;codecs=opus' };
      if (!MediaRecorder.isTypeSupported(options.mimeType)) {
          console.warn(`${options.mimeType} not supported, trying default 'audio/webm'.`);
          // Fallback to default or other known supported types
          options.mimeType = 'audio/webm'; // A more common fallback
           if (!MediaRecorder.isTypeSupported(options.mimeType)) {
               alert("No supported audio recording format found!");
               return;
           }
      }
      console.log(`Using MediaRecorder MIME type: ${options.mimeType}`); // Log the actual used type

      mediaRecorder.current = new MediaRecorder(stream, options);

      // Event handler for when audio data is available
      mediaRecorder.current.ondataavailable = (event) => {
        if (event.data.size > 0 && ws.current?.readyState === WebSocket.OPEN) {
          // Convert the Blob chunk to ArrayBuffer and send it over WebSocket
           event.data.arrayBuffer().then(buffer => {
               if (ws.current?.readyState === WebSocket.OPEN) { // Double-check connection before sending
                   ws.current.send(buffer); // Send raw audio bytes
                   console.log(`Sent audio chunk: ${buffer.byteLength} bytes`);
               }
           }).catch(err => console.error("Error converting blob chunk to buffer:", err));
        }
      };

      // Event handler for recording start
      mediaRecorder.current.onstart = () => {
        if (audioChunks.current) { // Safety check, though should always exist
             audioChunks.current = []; // Clear previous chunks (more for local storage if used)
        }
        setIsRecording(true);
        setAgentStatus(AgentStatus.Listening);
        // Add a placeholder in the conversation log
        setConversation(prev => [...prev, { speaker: 'user', text: '🎙️...' }]);
        console.log("Recording started");
      };

      // Event handler for recording stop
      mediaRecorder.current.onstop = () => {
        setIsRecording(false);
        // Set status to Processing - backend might detect silence, but this gives visual feedback
        setAgentStatus(AgentStatus.Processing);
        console.log("Recording stopped");

        // Important: Stop the microphone stream tracks to release the hardware
        stream.getTracks().forEach(track => track.stop());

        // Optional: Send an explicit end-of-speech signal if your backend requires it
        // if (ws.current?.readyState === WebSocket.OPEN) {
        //     ws.current.send(JSON.stringify({ type: "activity_end" }));
        // }
      };

      // Start recording and generate data chunks frequently (e.g., every 500ms)
      mediaRecorder.current.start(500);

    } catch (err) {
      console.error("Error accessing microphone:", err);
      // Provide more specific error feedback if possible
      if (err instanceof Error && (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError')) {
          alert("Microphone access denied. Please allow microphone access in your browser settings.");
      } else {
          alert("Could not access microphone.");
      }
    }
  };

  // Function to stop recording
  const stopRecording = () => {
    if (mediaRecorder.current && mediaRecorder.current.state === 'recording') {
      mediaRecorder.current.stop();
    }
  };

  // Button handler to toggle recording state
  const handleToggleRecording = () => {
    if (isRecording) {
      stopRecording();
    } else {
      startRecording();
    }
  };

  // --- Component Rendering ---
  return (
    <div className="flex flex-col h-screen max-w-2xl mx-auto p-4 bg-gray-50 font-sans">
      <h1 className="text-2xl font-bold mb-4 text-center text-gray-800">ADK Voice Agent</h1>

      {/* Status Indicators */}
      <div className="mb-2 text-sm text-center text-gray-600 space-x-2">
        <span>
          WS: <span className={`font-semibold ${
            connectionStatus === ConnectionStatus.Connected ? 'text-green-600' :
            connectionStatus === ConnectionStatus.Connecting ? 'text-yellow-600 animate-pulse' : 'text-red-600'
          }`}>{connectionStatus}</span>
        </span>
        <span>|</span>
        <span>
          Agent: <span className="font-semibold capitalize">{agentStatus.toLowerCase()}</span>
        </span>
      </div>

      {/* Conversation Display Area */}
      <div className="flex-grow overflow-y-auto mb-4 p-4 bg-white border border-gray-200 rounded-lg shadow-inner space-y-4 min-h-[200px]">
        {conversation.length === 0 && (
          <p className="text-center text-gray-400 italic mt-4">Conversation will appear here...</p>
        )}
        {conversation.map((msg, index) => (
          <div key={index} className={`flex flex-col ${msg.speaker === 'user' ? 'items-end' : 'items-start'}`}>
            <div className={`max-w-[80%] p-3 rounded-lg shadow-sm text-sm ${
              msg.speaker === 'user'
                ? 'bg-blue-500 text-white rounded-br-none'
                : 'bg-gray-200 text-gray-800 rounded-bl-none'
            }`}>
              {/* Render newlines if present */}
              {msg.text.split('\n').map((line, i) => (
                  <React.Fragment key={i}>
                      {line}
                      {i < msg.text.split('\n').length - 1 && <br />}
                  </React.Fragment>
              ))}
            </div>
          </div>
        ))}
        {/* Placeholder for Agent Thinking/Speaking state */}
         {agentStatus === AgentStatus.Processing && (
            <div className="flex justify-start">
                <div className="max-w-[75%] p-3 rounded-lg bg-gray-200 text-gray-500 italic text-sm animate-pulse">
                    Thinking...
                </div>
            </div>
         )}
         {/* Optional: Could add a subtle indicator when agent is speaking audio */}
      </div>

      {/* Input Control Area */}
      <div className="flex justify-center items-center border-t border-gray-200 pt-4">
        <button
          onClick={handleToggleRecording}
          disabled={connectionStatus !== ConnectionStatus.Connected || agentStatus === AgentStatus.Processing || agentStatus === AgentStatus.Speaking}
          aria-label={isRecording ? 'Stop recording' : 'Start recording'}
          className={`px-5 py-3 rounded-full text-white font-semibold transition-all duration-200 ease-in-out disabled:opacity-60 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-offset-2 ${
            isRecording
              ? 'bg-red-600 hover:bg-red-700 focus:ring-red-500 animate-pulse'
              : 'bg-blue-600 hover:bg-blue-700 focus:ring-blue-500'
          } flex items-center justify-center gap-2 shadow-md`}
        >
          {/* Microphone Icon (Example using simple SVG) */}
          <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M7 4a3 3 0 016 0v4a3 3 0 11-6 0V4zm-1 3a4 4 0 00-4 4v1a1 1 0 001 1h10a1 1 0 001-1v-1a4 4 0 00-4-4V7zm10 4a1 1 0 00-1-1h-1a1 1 0 100 2h1a1 1 0 001-1zm-4-8a1 1 0 011 1v1h-.008a.992.992 0 01-.024.004A3.996 3.996 0 008 11v1a1 1 0 11-2 0v-1a3.996 3.996 0 00-.968-.216A.992.992 0 015 11.992V11a1 1 0 112 0v.008a.992.992 0 01.024.004A3.996 3.996 0 0012 7V6a1 1 0 011-1z" clipRule="evenodd" />
          </svg>
          {isRecording ? 'Stop Listening' : 'Start Listening'}
        </button>
      </div>
    </div>
  );
}



// Define constant for output sample rate used in playback
const OUTPUT_SAMPLE_RATE = 24000; // Match Gemini Live API spec