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

"""Concrete CLI-binary agent harnesses (``gemini``, ``oc``).

Each harness lives in its own subpackage (:mod:`.gemini_cli`, :mod:`.openclaw`)
that splits the run driver (``agent``) from the trajectory parsers (``parsing``)
and self-registers under its canonical key on import via ``@AGENTS.register``.
Subpackages are imported by the harness at call time, not by this package's
``__init__`` — keeping the import graph light.
"""
