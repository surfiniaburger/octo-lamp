# backend/google_search_agent/agent.py
from google.adk.agents import Agent
from google.adk.tools import google_search

# Use the Vertex AI Live API model
# Double-check the latest available model supporting Live API in Vertex AI docs
LIVE_API_MODEL = "gemini-2.0-flash-live-preview-04-09"

root_agent = Agent(
    name="live_audio_search_agent",
    # Use the specific Live API model for Vertex AI
    model=LIVE_API_MODEL,
    description="Agent to answer questions using Google Search, interacting via voice.",
    instruction=(
        "You are a helpful voice assistant. You answer questions concisely "
        "using Google Search when necessary. Respond naturally as if in conversation."
        # Add language instruction if needed, based on Live API docs:
        # "RESPOND IN English. YOU MUST RESPOND UNMISTAKABLY IN English."
        ),
    tools=[google_search]
)