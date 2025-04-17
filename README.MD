# Create project directory
mkdir adk-audio-streaming && cd adk-audio-streaming

cd backend

# Create Python virtual environment
python -m venv .venv

# Activate (adjust for your OS)
# macOS/Linux:
source .venv/bin/activate
# Windows CMD:
# .venv\Scripts\activate.bat
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

# Install dependencies
pip install google-adk fastapi uvicorn python-dotenv websockets "uvicorn[standard]" google-cloud-aiplatform google-auth
# Note: google-cloud-aiplatform and google-auth might be needed for Vertex AI auth
# Note: websockets library is explicitly added for clarity, though FastAPI uses it.

Run `pip freeze > requirements.txt` after installing to capture exact versions.