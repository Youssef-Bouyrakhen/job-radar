"""Starts Job Radar and opens the dashboard in your browser."""
import socket
import sys
import threading
import webbrowser

import uvicorn

HOST, PORT = "127.0.0.1", 8000
URL = f"http://{HOST}:{PORT}"


def already_running() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    if already_running():
        print(f"\n  Job Radar (or another program) is already running on {URL}.")
        print("  Close the other Job Radar window first, then start it again.\n")
        sys.exit(1)
    print(f"\n  Job Radar is starting. Dashboard: {URL}\n  Press Ctrl+C in this window to stop it.\n")
    threading.Timer(2.0, webbrowser.open, args=[URL]).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
