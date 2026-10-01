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

"""Flag-only detection of agents accessing sensitive benchmark material."""

from devops_bench.cheat_detection.detector import (
    DETECTOR_VERSION,
    REPORT_SCHEMA_VERSION,
    annotate_records,
    scan_record,
)
from devops_bench.cheat_detection.inventory import (
    DEFAULT_BASELINE,
    ENVIRONMENT_DOTFILES,
    baseline_from_granted_paths,
    build_inventory_rules,
    build_mount_rules,
    filter_rules_for_prompt,
)
from devops_bench.cheat_detection.rules import (
    DEFAULT_RULES,
    SensitiveAccessRule,
    load_ruleset,
)

__all__ = [
    "DEFAULT_BASELINE",
    "DEFAULT_RULES",
    "DETECTOR_VERSION",
    "ENVIRONMENT_DOTFILES",
    "REPORT_SCHEMA_VERSION",
    "SensitiveAccessRule",
    "annotate_records",
    "baseline_from_granted_paths",
    "build_inventory_rules",
    "build_mount_rules",
    "filter_rules_for_prompt",
    "load_ruleset",
    "scan_record",
]
