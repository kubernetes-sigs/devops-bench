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

"""Per-run telemetry every CLI parser reports the same way."""

from __future__ import annotations

__all__ = ["note_model"]


def note_model(served_models: list[str], value: object) -> None:
    """Append ``value`` to ``served_models`` if it is a new model id.

    Every CLI transcript names the model that answered somewhere, and all of
    them feed the same ``AgentResult.served_models`` contract: distinct ids in
    first-seen order. Keeping the rule in one place means a change to it — id
    normalization, say — lands on every harness at once rather than on whichever
    two of three the author remembered.

    Args:
        served_models: List to append to, mutated in place.
        value: The transcript's model field, or anything else. Non-strings, the
            empty string, and ids already recorded are ignored.
    """
    if isinstance(value, str) and value and value not in served_models:
        served_models.append(value)
