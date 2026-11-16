import os
import sys
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Add bot directory to sys.path
sys.path.insert(0, str(Path(__file__).parent / "bot"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - SAUX Bot is running live")

    def log_message(self, format, *args):
        pass


def start_health_check_server():
    port_str = os.environ.get("PORT")
    if port_str:
        try:
            port = int(port_str)
            server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logging.info(f"Healthcheck HTTP server listening on 0.0.0.0:{port}")
        except Exception as e:
            logging.warning(f"Could not start healthcheck server on port {port_str}: {e}")


from bot import run_bot

if __name__ == "__main__":
    start_health_check_server()
    run_bot()
