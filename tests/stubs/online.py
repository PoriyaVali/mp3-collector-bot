"""CI stand-in for the bot: logs like the real one, answers /health, runs until stopped."""
import http.server
import logging

from app import __version__


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mp3bot")
log.info("download server listening on :8080")
log.info("bot @ci_stub_%s is online", __version__)
http.server.HTTPServer(("0.0.0.0", 8080), Health).serve_forever()
