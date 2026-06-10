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

import json
import os
import subprocess
import datetime
from google import genai
from google.genai import types

def log(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] {msg}", flush=True)

class ChaosAgent:
  """Injects chaos faults."""

  def __init__(self):
    self._chaos_active_event = None

  def _run_command(self, command: str) -> str:
    """Executes a shell command."""
    try:
        parts = command.strip().split()
        if not parts:
            return "Error: Empty command"
        
        binary = parts[0]
        binary_name = os.path.basename(binary)
        
        allowed_binaries = {"fortio", "echo", "sleep"}
        if binary_name not in allowed_binaries:
            log(f"[ChaosAgent/Tool] Blocked unsafe command: {command}")
            return f"Error: Command '{binary_name}' is not allowed. Allowed commands are: {', '.join(sorted(allowed_binaries))}"

        log(f"[ChaosAgent/Tool] Running command: {command}")
        
        if self._chaos_active_event and "fortio load" in command:
            log("[ChaosAgent/Tool] Load spike detected. Signaling main thread...")
            self._chaos_active_event.set()
            
        res = subprocess.run(parts, capture_output=True, text=True, timeout=40)
        return f"Stdout:\n{res.stdout}\nStderr:\n{res.stderr}"
    except Exception as e:
        return f"Error: {e}"

  def inject_fault(self, spec: dict):
    self._chaos_active_event = getattr(self, "chaos_active_event", self._chaos_active_event)
    
    action_type = spec.get("type")
    
    if action_type != "generate_load":
      log(f"Error: Unsupported chaos action type '{action_type}'")
      return
      
    log(f"[ChaosAgent] Activating LLM Planned mode for action type '{action_type}'...")
    
    goal = (
        f"Your goal is to execute the following planned chaos engineering disruption action:\n"
        f"```json\n{json.dumps(spec, indent=2)}\n```\n\n"
        f"Guidelines for execution:\n"
        f"1. Use the 'fortio' tool to inject traffic into the workload.\n"
        f"2. Note: The service target URLs (like *.svc.cluster.local) are port-forwarded to 'http://localhost:8080' on the host, so run fortio against http://localhost:8080 instead.\n"
        f"Use your _run_command tool to execute this disruption safely and effectively."
    )
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
       raise RuntimeError("GEMINI_API_KEY must be set to use ChaosAgent")
    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("JUDGE_MODEL", "gemini-3-flash-preview")
    system_instruction = (
        "You are a professional Site Reliability Engineer (SRE) and Chaos Engineering Expert.\n"
        "Your role is to disrupt the workloads to test system resilience, which can happen in two modes:\n"
        "1. Planned Mode: Execute a specific chaos disruption according to a provided JSON spec.\n"
        "2. Autonomous Mode: Autonomously explore the cluster state, identify critical targets (pods, nodes, services), "
        "and inject transient faults to test recovery.\n\n"
        "You are equipped with the `_run_command` tool, which runs shell commands locally on the host control machine "
        "(which is fully authenticated and has admin kubectl privileges).\n\n"
        "Strict Guidelines for Execution:\n"
        "- Single Execution Policy: You MUST execute exactly one tool call to run the planned 'fortio' load generation spike. "
        "Do NOT attempt to rerun, adjust, or tune the load generation if the target service saturates or returns timeouts. "
        "Once the single load command is executed, analyze the output, write your final performance summary, and exit immediately.\n"
        "- Safety First: Only inject transient, safe, and recoverable faults (e.g. killing pods, scaling deployments, "
        "generating traffic spikes). Do NOT permanently destroy clusters, namespaces, or nodes.\n"
        "- Traffic Generation: For load spikes, use the 'fortio' binary. Since the internal "
        "service URLs (*.svc.cluster.local) are port-forwarded to the host, you MUST target 'http://localhost:8080' instead.\n"
        "- Analysis & Clarity: Analyze command outputs carefully, report stdout/stderr accurately, and confirm in your "
        "final response when the disruption has been successfully completed."
    )
    
    log("[ChaosAgent] Spawning Chaos LLM chat session with direct python tools...")
    chat = client.chats.create(
        model=model_name,
        config=types.GenerateContentConfig(
            tools=[self._run_command],
            system_instruction=system_instruction,
            temperature=0.0
        )
    )
    
    try:
        response = chat.send_message(goal)
        log("[ChaosAgent] Planned chaos execution complete!")
        log(f"Agent Final Output:\n{response.text}")
    except Exception as e:
        log(f"[ChaosAgent] Error during LLM chaos execution: {e}")
        raise
