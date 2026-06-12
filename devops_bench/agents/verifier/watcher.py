# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from typing import Callable, Optional


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress HTTP server output logs
        pass

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        try:
            raw_event = json.loads(post_data.decode('utf-8'))
            
            eventmeta = raw_event.get("eventmeta", {})
            kind = eventmeta.get("kind")
            raw_name = eventmeta.get("name", "")
            if "/" in raw_name:
                ns, name = raw_name.split("/", 1)
            else:
                ns = eventmeta.get("namespace", "")
                name = raw_name
                
            event = {
                "kind": kind,
                "name": name,
                "namespace": ns,
                "reason": eventmeta.get("reason")
            }
            self.server.callback(event)
        except Exception as e:
            pass
        self.send_response(200)
        self.end_headers()

class WebhookServer(http.server.HTTPServer):
    def __init__(self, port: int, callback: Callable[[dict], None]):
        super().__init__(('localhost', port), WebhookHandler)
        self.callback = callback

class KubeWatchService:
    """Manages the lifecycle of a local, isolated kubewatch daemon instance.

    It routes Kubernetes events to a local HTTP webhook receiver in a non-blocking way.
    """

    def __init__(
        self,
        callback: Callable[[dict], None],
        binary_path: Optional[str] = None,
    ):
        self.callback = callback
        self.binary_path = binary_path or os.environ.get("KUBEWATCH_BIN", "kubewatch")
        self.temp_dir = None
        self.port = None
        self.http_server = None
        self.http_thread = None
        self.process = None

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('localhost', 0))
            return s.getsockname()[1]

    def start(self):
        self.temp_dir = tempfile.mkdtemp(prefix="kubewatch-")
        self.port = self._find_free_port()

        # Start webhook receiver HTTP server
        self.http_server = WebhookServer(self.port, self.callback)
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever, daemon=True
        )
        self.http_thread.start()

        env = os.environ.copy()
        env["KW_CONFIG"] = self.temp_dir

        # Configure webhook handler in local config
        webhook_cmd = [
            self.binary_path,
            "config",
            "add",
            "webhook",
            "-u",
            f"http://localhost:{self.port}/webhook",
        ]
        subprocess.run(
            webhook_cmd,
            check=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Configure watched resources (pods and deployments)
        resource_cmd = [
            self.binary_path,
            "resource",
            "add",
            "--po",
            "--deploy",
        ]
        subprocess.run(
            resource_cmd,
            check=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Start the local daemon
        self.process = subprocess.Popen(
            [self.binary_path],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None

        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None

        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None
