# Start the API with auto-reload on http://localhost:8000 (docs at /docs).
Set-Location $PSScriptRoot
& .\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000 @args
