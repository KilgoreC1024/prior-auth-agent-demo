#!/bin/bash
# Prior-Auth Checklist Agent - launcher (macOS / Linux)
# If double-clicking doesn't work: open Terminal, type "bash ", drag this file
# onto the window, and press Enter.
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close..."
  exit 1
fi

if ! python3 -c "import flask, anthropic, openai" >/dev/null 2>&1; then
  echo "Installing required packages (one-time, ~30 seconds)..."
  python3 -m pip install --quiet flask anthropic openai
fi

echo "Starting the Prior-Auth Agent... your browser will open automatically."
echo "Keep this window open while you use the app. Press Ctrl+C to stop."
python3 app.py
