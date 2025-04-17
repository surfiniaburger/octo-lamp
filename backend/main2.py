# backend/main.py
import os
import json
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

from google.genai.types import ( # Corrected import path likely needed
    Part,
    Content,
    Blob, # Needed for inline_data
    LiveConnectConfig, # Potentially needed for direct config if ADK doesn't expose all
    SpeechConfig,
    VoiceConfig,
    PrebuiltVoiceConfig,
    Modality,
    AudioTranscriptionConfig,
)
from google.adk.runners import Runner
from google.adk.agents import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles # Keep for potential future static serving
from fastapi.responses import FileResponse # Keep for potential future static serving

# Import your agent definition
from google_search_agent.agent import root_agent

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Environment Variables (.env file)
load_dotenv()

# --- Configuration ---
APP_NAME = "ADK Audio Streaming Example"
# Choose the desired voice from Gemini Live API docs (e.g., "Aoede", "Puck", etc.)
AGENT_VOICE_NAME = "Aoede"
# Set desired language code (e.g., "en-US")
AGENT_LANGUAGE_CODE = "en-US"
# Input audio format expected from client (as per Live API docs)
EXPECTED_INPUT_SAMPLE_RATE = 16000
EXPECTED_INPUT_CHANNELS = 1
# Output audio format from Live API (as per Live API docs)
OUTPUT_SAMPLE_RATE = 24000

# --- ADK Setup ---
session_service = InMemorySessionService()

def start_agent_session(session_id: str):
    """Starts an agent session configured for Audio I/O."""
    logger.info(f"Starting agent session for ID: {session_id}")
    try:
        session = session_service.create_session(
            app_name=APP_NAME,
            user_id=session_id,
            session_id=session_id,
        )

        runner = Runner(
            app_name=APP_NAME,
            agent=root_agent,
            session_service=session_service,
        )

        # --- Configure RunConfig for Audio ---
        # This part might need adjustment based on exact ADK capabilities/versions.
        # We aim to tell ADK/Gemini we want AUDIO output.
        run_config = RunConfig(
            response_modalities=[Modality.AUDIO, Modality.TEXT], # Request both audio and text
            speech_config=SpeechConfig( # Configure the voice
                voice_config=VoiceConfig(
                    prebuilt_voice_config=PrebuiltVoiceConfig(
                        voice_name=AGENT_VOICE_NAME,
                    )
                ),
            ),
            # Optional: Enable transcription if needed (adds cost/latency)
            # input_audio_transcription=AudioTranscriptionConfig(),
            # output_audio_transcription=AudioTranscriptionConfig(),
        )
        # ---------------------------------------

        live_request_queue = LiveRequestQueue()

        live_events = runner.run_live(
            session=session,
            live_request_queue=live_request_queue,
            run_config=run_config,
        )
        logger.info(f"Agent session started successfully for ID: {session_id}")
        return live_events, live_request_queue

    except Exception as e:
        logger.error(f"Error starting agent session {session_id}: {e}", exc_info=True)
        raise

async def agent_to_client_messaging(websocket: WebSocket, live_events, session_id: str):
    """Streams agent responses (text & audio) to the client."""
    logger.info(f"[{session_id}] Starting agent->client messaging task.")
    try:
        while True:
            async for event in live_events:
                message_to_send = None
                message_type = "unknown" # For logging

                if event.turn_complete:
                    message_to_send = json.dumps({"type": "status", "message": "turn_complete"})
                    message_type = "turn_complete_status"
                    logger.info(f"[{session_id}] Agent Turn Complete.")
                elif event.interrupted:
                    message_to_send = json.dumps({"type": "status", "message": "interrupted"})
                    message_type = "interrupted_status"
                    logger.info(f"[{session_id}] Agent Interrupted.")
                elif event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            # Send text part
                            message_to_send = json.dumps({"type": "text", "data": part.text})
                            message_type = "text"
                            logger.info(f"[{session_id}] AGENT -> CLIENT (Text): {part.text[:50]}...")
                            await websocket.send_text(message_to_send)
                            message_to_send = None # Reset after sending

                        elif part.inline_data and part.inline_data.data:
                            # Send audio part (raw bytes)
                            # Check mime type if necessary: part.inline_data.mime_type == 'audio/pcm' or similar
                            audio_bytes = part.inline_data.data
                            # The frontend needs to know this is audio data.
                            # We send a JSON marker *before* the binary data.
                            await websocket.send_text(json.dumps({
                                "type": "audio_chunk_info",
                                "mime_type": part.inline_data.mime_type, # e.g., 'audio/l16; rate=24000'
                                "chunk_size": len(audio_bytes)
                            }))
                            await websocket.send_bytes(audio_bytes)
                            message_type = "audio"
                            logger.info(f"[{session_id}] AGENT -> CLIENT (Audio): Sent {len(audio_bytes)} bytes.")
                            # Don't reset message_to_send here, handled above

                        # Handle other part types if needed (e.g., function calls)
                        # elif part.function_call: ...

                # Send any remaining status messages that weren't part of content
                if message_to_send:
                    logger.info(f"[{session_id}] AGENT -> CLIENT (Status): {message_type}")
                    await websocket.send_text(message_to_send)

                await asyncio.sleep(0.01) # Yield control briefly

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] WebSocket disconnected during agent->client messaging.")
    except asyncio.CancelledError:
        logger.info(f"[{session_id}] Agent->client messaging task cancelled.")
    except Exception as e:
        logger.error(f"[{session_id}] Error in agent->client messaging: {e}", exc_info=True)
        # Attempt to send an error message to the client if possible
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": "Server error during agent communication."}))
        except:
            pass # Ignore if send fails (connection likely closed)
    finally:
        logger.info(f"[{session_id}] Agent->client messaging task finished.")


async def client_to_agent_messaging(websocket: WebSocket, live_request_queue: LiveRequestQueue, session_id: str):
    """Receives messages (text & audio) from client and sends to agent."""
    logger.info(f"[{session_id}] Starting client->agent messaging task.")
    try:
        while True:
            message = await websocket.receive() # Handles both text and bytes

            if "text" in message:
                text_data = message["text"]
                logger.info(f"[{session_id}] CLIENT -> AGENT (Text): {text_data[:50]}...")
                try:
                     # Attempt to parse as JSON for potential structured messages
                     data = json.loads(text_data)
                     if data.get("type") == "control" and data.get("command") == "interrupt":
                         logger.info(f"[{session_id}] Received interrupt command from client.")
                         # ADK's interruption mechanism might be implicit via sending new audio/text,
                         # or might need an explicit API call if available. Check ADK docs/source.
                         # For now, sending empty content *might* signal something, or just rely on new audio.
                         # live_request_queue.send_content(Content(role="user", parts=[])) # Example - check if effective
                     elif data.get("type") == "text_input":
                         # Handle explicit text input
                         user_text = data.get("data", "")
                         if user_text:
                            content = Content(role="user", parts=[Part.from_text(text=user_text)])
                            live_request_queue.send_content(content=content)
                     else:
                         logger.warning(f"[{session_id}] Received unhandled JSON message: {data}")
                except json.JSONDecodeError:
                     # If not JSON, assume it's plain text input (legacy behavior)
                     content = Content(role="user", parts=[Part.from_text(text=text_data)])
                     live_request_queue.send_content(content=content)


            elif "bytes" in message:
                audio_bytes = message["bytes"]
                logger.info(f"[{session_id}] CLIENT -> AGENT (Audio): Received {len(audio_bytes)} bytes.")

                # **CRITICAL**: Ensure audio_bytes are in the format Gemini Live expects:
                # 16-bit PCM, 16kHz, mono, little-endian.
                # The frontend *must* send data in this format, or you need transcoding here (adds latency).

                # Create a Part with inline audio data.
                # The mime_type helps the API interpret the data.
                # Adjust mime_type if your input differs (e.g., different rate), but API requires 16kHz.
                audio_part = Part.from_inline_data(
                    Blob(
                        mime_type=f"audio/l16; rate={EXPECTED_INPUT_SAMPLE_RATE}", # Linear16 PCM @ 16kHz
                        data=audio_bytes
                    )
                )
                content = Content(role="user", parts=[audio_part])
                live_request_queue.send_content(content=content)

            await asyncio.sleep(0.01) # Yield control

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] WebSocket disconnected during client->agent messaging.")
    except asyncio.CancelledError:
        logger.info(f"[{session_id}] Client->agent messaging task cancelled.")
    except Exception as e:
        logger.error(f"[{session_id}] Error in client->agent messaging: {e}", exc_info=True)
    finally:
        logger.info(f"[{session_id}] Client->agent messaging task finished.")


# --- FastAPI App ---
app = FastAPI(title=APP_NAME)

# Serve static files (if you have a combined build) - Optional for Next.js dev
# STATIC_DIR = Path("../frontend/out") # Example if Next.js exports static site
# if STATIC_DIR.exists():
#     app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
#     @app.get("/")
#     async def serve_index():
#         return FileResponse(STATIC_DIR / "index.html")

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """Handles client WebSocket connections."""
    await websocket.accept()
    logger.info(f"Client #{session_id} connected via WebSocket.")

    agent_to_client_task = None
    client_to_agent_task = None
    live_request_queue = None # Define here for finally block

    try:
        # Start the agent session
        live_events, live_request_queue_instance = start_agent_session(session_id)
        live_request_queue = live_request_queue_instance # Assign for cleanup

        # Create concurrent tasks for bidirectional communication
        agent_to_client_task = asyncio.create_task(
            agent_to_client_messaging(websocket, live_events, session_id)
        )
        client_to_agent_task = asyncio.create_task(
            client_to_agent_messaging(websocket, live_request_queue, session_id)
        )

        # Keep the connection alive by waiting for tasks to complete
        # (they run until disconnect or error)
        done, pending = await asyncio.wait(
            {agent_to_client_task, client_to_agent_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # If one task finishes (e.g., due to error or disconnect), cancel the other
        for task in pending:
            logger.info(f"[{session_id}] Cancelling pending task: {task.get_name()}")
            task.cancel()
            try:
                await task # Wait for cancellation to complete
            except asyncio.CancelledError:
                pass # Expected
            except Exception as e:
                logger.error(f"[{session_id}] Error during task cancellation: {e}", exc_info=True)


    except WebSocketDisconnect:
        logger.info(f"Client #{session_id} WebSocket connection closed cleanly.")
    except Exception as e:
        logger.error(f"Error in WebSocket endpoint for session {session_id}: {e}", exc_info=True)
        # Try to close WebSocket gracefully with an error code if possible
        try:
            await websocket.close(code=1011, reason="Internal Server Error")
        except:
            pass # Ignore if already closed
    finally:
        logger.info(f"Client #{session_id} disconnected.")
        # Ensure tasks are cancelled if they are still running (e.g., if initial setup failed)
        if agent_to_client_task and not agent_to_client_task.done():
            agent_to_client_task.cancel()
        if client_to_agent_task and not client_to_agent_task.done():
            client_to_agent_task.cancel()
        # Clean up ADK resources if necessary - check ADK docs for explicit cleanup needs
        if live_request_queue:
             # live_request_queue.close() # Check if a close method exists/is needed
             pass
        # session_service.delete_session(session_id) # If needed

# Add a simple health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "ok"}

# --- Run the server (for local development) ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting FastAPI server with Uvicorn...")
    # Use reload=True for development, set host/port as needed
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)