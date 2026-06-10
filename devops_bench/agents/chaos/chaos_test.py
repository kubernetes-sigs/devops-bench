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

import os
import unittest
from unittest import mock

from devops_bench.agents.chaos.chaos import ChaosAgent


class TestChaosAgent(unittest.TestCase):

  @mock.patch("devops_bench.agents.chaos.chaos.genai.Client")
  @mock.patch("subprocess.run")
  @mock.patch.dict(
      os.environ, {"GEMINI_API_KEY": "fake-key"}
  )
  def test_inject_fault_generate_load_success(self, mock_run, mock_genai_client):
    # Setup mock run
    mock_run.return_value = mock.MagicMock(
        stdout="mock stdout", stderr="mock stderr", returncode=0
    )

    # Setup mock GenAI client
    mock_client = mock.MagicMock()
    mock_genai_client.return_value = mock_client

    mock_chat = mock.MagicMock()
    mock_client.chats.create.return_value = mock_chat

    mock_response = mock.MagicMock()
    mock_response.text = "Disruption complete"
    mock_chat.send_message.return_value = mock_response

    agent = ChaosAgent()

    action_spec = {
        "type": "generate_load",
        "target": {
            "service_url": "http://localhost:8082",
            "qps": 100,
            "duration": "10s",
            "concurrency": 2,
        },
    }

    # Execute
    agent.inject_fault(action_spec)

    # Verify GenAI Client initialization
    mock_genai_client.assert_called_once_with(api_key="fake-key")

    # Verify chat creation with correct parameters
    mock_client.chats.create.assert_called_once_with(
        model="gemini-3-flash-preview", config=mock.ANY
    )
    # Deep compare config
    config = mock_client.chats.create.call_args[1]["config"]
    self.assertEqual(config.temperature, 0.0)
    self.assertEqual(config.tools, [agent._run_command])
    self.assertIn(
        "You are a professional Site Reliability Engineer",
        config.system_instruction,
    )

    # Verify send_message was called with the goal
    mock_chat.send_message.assert_called_once()
    goal_sent = mock_chat.send_message.call_args[0][0]
    self.assertIn(
        "execute the following planned chaos engineering disruption action",
        goal_sent,
    )
    self.assertIn("generate_load", goal_sent)

  @mock.patch("devops_bench.agents.chaos.chaos.genai.Client")
  def test_inject_fault_unsupported_type(self, mock_genai_client):
    agent = ChaosAgent()
    action_spec = {"type": "invalid_type"}

    agent.inject_fault(action_spec)

    # Should return early and NOT call Gemini
    mock_genai_client.assert_not_called()

  @mock.patch("devops_bench.agents.chaos.chaos.genai.Client")
  def test_inject_fault_failure_propagates(self, mock_genai_client):
    # Setup mock GenAI client to raise exception
    mock_client = mock.MagicMock()
    mock_genai_client.return_value = mock_client
    mock_client.chats.create.side_effect = Exception("API Error")

    agent = ChaosAgent()
    action_spec = {"type": "generate_load"}

    with self.assertRaises(Exception) as context:
      agent.inject_fault(action_spec)

    self.assertIn("API Error", str(context.exception))

  @mock.patch("subprocess.run")
  def test_run_command_success(self, mock_run):
    mock_run.return_value = mock.MagicMock(
        stdout="stdout output", stderr="stderr output", returncode=0
    )

    agent = ChaosAgent()
    result = agent._run_command("echo hello")

    self.assertIn("Stdout:\nstdout output", result)
    self.assertIn("Stderr:\nstderr output", result)
    mock_run.assert_called_once_with(
        ["echo", "hello"], capture_output=True, text=True, timeout=40
    )

  @mock.patch("subprocess.run")
  def test_run_command_exception(self, mock_run):
    mock_run.side_effect = Exception("command failed")

    agent = ChaosAgent()
    result = agent._run_command("echo hello")

    self.assertEqual(result, "Error: command failed")

  @mock.patch("subprocess.run")
  def test_run_command_load_spike_signaling(self, mock_run):
    mock_run.return_value = mock.MagicMock(stdout="ok", stderr="", returncode=0)

    agent = ChaosAgent()
    mock_event = mock.MagicMock()
    agent._chaos_active_event = mock_event

    # Should signal when "fortio load" is in command
    agent._run_command("fortio load -qps 10")
    mock_event.set.assert_called_once()

    mock_event.reset_mock()

    # Should NOT signal when "fortio load" is NOT in command
    agent._run_command("echo not-a-load")
    mock_event.set.assert_not_called()

  def test_run_command_blocked(self):
    agent = ChaosAgent()
    # rm is not in the safelist
    result = agent._run_command("rm -rf /")
    self.assertIn("Command 'rm' is not allowed", result)


if __name__ == "__main__":
  unittest.main()
