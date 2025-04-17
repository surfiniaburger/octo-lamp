# backend/main.py
import os
import json
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv
import io # Required for pydub
from pydub import AudioSegment # Import pydub

# ... other imports ...
from google.genai.types import (
    Part,
    Content,
    Blob,
    LiveConnectConfig,
    SpeechConfig,
    VoiceConfig,
    PrebuiltVoiceConfig,
    Modality,
    AudioTranscriptionConfig
)
from google.adk.runners import Runner
from google.adk.agents import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from google_search_agent.agent import root_agent, LIVE_API_MODEL # Import model name too

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

APP_NAME = "ADK Audio Streaming Example"
AGENT_VOICE_NAME = "Aoede"
AGENT_LANGUAGE_CODE = "en-US"
EXPECTED_INPUT_SAMPLE_RATE = 16000 # Gemini requires 16kHz input
EXPECTED_INPUT_CHANNELS = 1
OUTPUT_SAMPLE_RATE = 24000 # Gemini outputs 24kHz

session_service = InMemorySessionService()

def start_agent_session(session_id: str):
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
        # Use LiveConnectConfig directly if RunConfig doesn't expose enough
        # Or ensure RunConfig passes these through correctly
        live_config = LiveConnectConfig(
            response_modalities=[Modality.AUDIO, Modality.TEXT],
            speech_config=SpeechConfig(
                voice_config=VoiceConfig(
                    prebuilt_voice_config=PrebuiltVoiceConfig(
                        voice_name=AGENT_VOICE_NAME,
                    ),
                ),
            )
            # Optional transcriptions
            # input_audio_transcription=AudioTranscriptionConfig(),
            # output_audio_transcription=AudioTranscriptionConfig(),
         )

        live_request_queue = LiveRequestQueue()

        # Use the underlying genai client's connect method via ADK's abstraction
        # if run_live doesn't directly take LiveConnectConfig
        # This part is tricky and depends on ADK's exact API.
        # Let's stick to runner.run_live but ensure the model in root_agent is correct
        # and hope RunConfig properly translates. If 1007 persists, direct Live API config might be needed.

        run_config = RunConfig(
            response_modalities=[Modality.AUDIO, Modality.TEXT],
            speech_config=SpeechConfig(
                voice_config=VoiceConfig(
                    prebuilt_voice_config=PrebuiltVoiceConfig(
                        voice_name=AGENT_VOICE_NAME,
                    )
                ),
            ),
        )


        live_events = runner.run_live(
            session=session,
            live_request_queue=live_request_queue,
            run_config=run_config # Pass RunConfig here
        )
        logger.info(f"Agent session started successfully for ID: {session_id}")
        return live_events, live_request_queue
    except Exception as e:
        logger.error(f"Error starting agent session {session_id}: {e}", exc_info=True)
        raise

async def agent_to_client_messaging(websocket: WebSocket, live_events: asyncio.Queue, session_id: str):
    """Streams agent responses (text & audio) to the client."""
    logger.info(f"[{session_id}] Starting agent->client messaging task.")
    try:
        # The live_events might be an async generator, not a queue
        async for event in live_events: # Adjust if live_events is not an async iterator
            message_to_send = None
            message_type = "unknown"

            # Check event type (structure depends on ADK version)
            # Assuming event might have attributes like turn_complete, interrupted, content
            is_turn_complete = getattr(event, 'turn_complete', False)
            is_interrupted = getattr(event, 'interrupted', False)
            content = getattr(event, 'content', None)

            if is_turn_complete:
                message_to_send = json.dumps({"type": "status", "message": "turn_complete"})
                message_type = "turn_complete_status"
                logger.info(f"[{session_id}] Agent Turn Complete.")
                await websocket.send_text(message_to_send)
            elif is_interrupted:
                message_to_send = json.dumps({"type": "status", "message": "interrupted"})
                message_type = "interrupted_status"
                logger.info(f"[{session_id}] Agent Interrupted.")
                await websocket.send_text(message_to_send)
            elif content and content.parts:
                for part in content.parts:
                    if part.text:
                        message_to_send = json.dumps({"type": "text", "data": part.text})
                        message_type = "text"
                        logger.info(f"[{session_id}] AGENT -> CLIENT (Text): {part.text[:50]}...")
                        await websocket.send_text(message_to_send)

                    elif part.inline_data and part.inline_data.data:
                        audio_bytes = part.inline_data.data
                        mime_type = part.inline_data.mime_type or f"audio/l16; rate={OUTPUT_SAMPLE_RATE}" # Default if missing
                        # Send JSON marker *first*
                        await websocket.send_text(json.dumps({
                            "type": "audio_chunk_info",
                            "mime_type": mime_type,
                            "chunk_size": len(audio_bytes)
                        }))
                        # Then send the raw bytes
                        await websocket.send_bytes(audio_bytes)
                        message_type = "audio"
                        logger.info(f"[{session_id}] AGENT -> CLIENT (Audio): Sent {len(audio_bytes)} bytes ({mime_type}).")

            await asyncio.sleep(0.01) # Yield control briefly

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] WebSocket disconnected during agent->client messaging.")
    except asyncio.CancelledError:
        logger.info(f"[{session_id}] Agent->client messaging task cancelled.")
    except Exception as e:
        logger.error(f"[{session_id}] Error in agent->client messaging: {e}", exc_info=True)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": "Server error during agent communication."}))
        except:
            pass
    finally:
        logger.info(f"[{session_id}] Agent->client messaging task finished.")

async def client_to_agent_messaging(websocket: WebSocket, live_request_queue: LiveRequestQueue, session_id: str):
    """Receives messages (text & audio) from client and sends to agent."""
    logger.info(f"[{session_id}] Starting client->agent messaging task.")
    transcoding_tasks = set() # Keep track of background transcoding

    try:
        while True:
            message = await websocket.receive()

            if "text" in message:
                text_data = message["text"]
                logger.info(f"[{session_id}] CLIENT -> AGENT (Raw Text Received): {text_data[:50]}...")
                try:
                    data = json.loads(text_data)
                    if data.get("type") == "text_input":
                        user_text = data.get("data", "")
                        if user_text:
                            logger.info(f"[{session_id}] CLIENT -> AGENT (Parsed Text Input): {user_text}")
                            content = Content(role="user", parts=[Part.from_text(text=user_text)])
                            live_request_queue.send_content(content=content)
                    # Add other JSON command handling here if needed (e.g., interrupt)
                    else:
                        logger.warning(f"[{session_id}] Received unhandled JSON message: {data}")
                except json.JSONDecodeError:
                    logger.warning(f"[{session_id}] Received non-JSON text, ignoring: {text_data[:50]}...")
                    # Avoid sending plain text if only audio/structured input is expected

            elif "bytes" in message:
                webm_bytes = message["bytes"]
                logger.info(f"[{session_id}] CLIENT -> AGENT (WebM Audio): Received {len(webm_bytes)} bytes.")

                # --- Transcoding Logic ---
                async def transcode_and_send(audio_data):
                    try:
                        # Load WebM bytes into pydub AudioSegment
                        webm_audio = AudioSegment.from_file(io.BytesIO(audio_data), format="webm")

                        # Ensure mono and 16kHz sample rate
                        pcm_audio = webm_audio.set_channels(EXPECTED_INPUT_CHANNELS)
                        pcm_audio = pcm_audio.set_frame_rate(EXPECTED_INPUT_SAMPLE_RATE)

                        # Export as raw PCM (16-bit little-endian is default for 'raw' format)
                        pcm_bytes_io = io.BytesIO()
                        pcm_audio.export(pcm_bytes_io, format="raw")
                        pcm_bytes = pcm_bytes_io.getvalue()

                        logger.info(f"[{session_id}] Transcoded {len(audio_data)} webm bytes to {len(pcm_bytes)} L16/16kHz PCM bytes.")

                        # Create ADK Part with correct format
                        audio_part = Part.from_inline_data(
                            Blob(
                                mime_type=f"audio/l16; rate={EXPECTED_INPUT_SAMPLE_RATE}",
                                data=pcm_bytes
                            )
                        )
                        content = Content(role="user", parts=[audio_part])
                        live_request_queue.send_content(content=content)
                        logger.info(f"[{session_id}] Sent transcoded audio chunk to agent.")

                    except Exception as transcode_error:
                        logger.error(f"[{session_id}] Error transcoding audio chunk: {transcode_error}", exc_info=True)
                    finally:
                        transcoding_tasks.discard(asyncio.current_task()) # Remove self when done

                # Run transcoding in the background to avoid blocking the receive loop
                task = asyncio.create_task(transcode_and_send(webm_bytes))
                transcoding_tasks.add(task)
                # -------------------------

            await asyncio.sleep(0.01) # Yield control

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] WebSocket disconnected during client->agent messaging.")
    except asyncio.CancelledError:
        logger.info(f"[{session_id}] Client->agent messaging task cancelled.")
    except Exception as e:
        logger.error(f"[{session_id}] Error in client->agent messaging: {e}", exc_info=True)
    finally:
        logger.info(f"[{session_id}] Client->agent messaging task finished.")
         # Wait briefly for any ongoing transcoding tasks to finish
        if transcoding_tasks:
            logger.info(f"[{session_id}] Waiting for {len(transcoding_tasks)} transcoding tasks to complete...")
            await asyncio.wait(transcoding_tasks, timeout=5.0) # Wait max 5 seconds
            remaining_tasks = [t for t in transcoding_tasks if not t.done()]
            if remaining_tasks:
                logger.warning(f"[{session_id}] {len(remaining_tasks)} transcoding tasks did not finish in time.")
                for t in remaining_tasks:
                    t.cancel()


app = FastAPI(title=APP_NAME)

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    logger.info(f"Client #{session_id} connected via WebSocket.")
    agent_to_client_task = None
    client_to_agent_task = None
    live_request_queue = None
    live_events = None # Define here for finally block

    try:
        live_events_instance, live_request_queue_instance = start_agent_session(session_id)
        live_request_queue = live_request_queue_instance
        live_events = live_events_instance # Assign for cleanup if needed

        agent_to_client_task = asyncio.create_task(
            agent_to_client_messaging(websocket, live_events, session_id),
            name=f"agent_to_client_{session_id}" # Add names for easier debugging
        )
        client_to_agent_task = asyncio.create_task(
            client_to_agent_messaging(websocket, live_request_queue, session_id),
            name=f"client_to_agent_{session_id}"
        )

        done, pending = await asyncio.wait(
            {agent_to_client_task, client_to_agent_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task_name = task.get_name() if hasattr(task, 'get_name') else 'unknown pending task'
            logger.info(f"[{session_id}] Cancelling pending task: {task_name}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                 logger.error(f"[{session_id}] Error awaiting cancelled task {task_name}: {e}", exc_info=True)

        # Check which task finished and why (optional detailed logging)
        for task in done:
             task_name = task.get_name() if hasattr(task, 'get_name') else 'unknown completed task'
             try:
                 result = task.result() # This will raise exception if task failed
                 logger.info(f"[{session_id}] Task {task_name} completed normally.")
             except asyncio.CancelledError:
                 logger.info(f"[{session_id}] Task {task_name} was cancelled.")
             except Exception as e:
                 logger.error(f"[{session_id}] Task {task_name} failed: {e}", exc_info=True)


    except WebSocketDisconnect:
        logger.info(f"Client #{session_id} WebSocket connection closed cleanly.")
    except Exception as e:
        logger.error(f"Error in WebSocket endpoint for session {session_id}: {e}", exc_info=True)
        try: await websocket.close(code=1011, reason="Internal Server Error")
        except: pass
    finally:
        logger.info(f"Client #{session_id} disconnected.")
        # Ensure tasks are cancelled even if setup failed partially
        if agent_to_client_task and not agent_to_client_task.done():
            agent_to_client_task.cancel()
        if client_to_agent_task and not client_to_agent_task.done():
            client_to_agent_task.cancel()

        # ADK cleanup (check ADK docs for specific session cleanup if needed)
        # if session_id:
        #    session_service.delete_session(session_id) # If session service manages cleanup

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting FastAPI server with Uvicorn...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")