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

"""Ingest-ready result rows bridging the harness output and the dashboard."""

from devops_bench.results.aggregate import (
    aggregate,
    build_manifests,
    dedupe_latest,
    discover_row_files,
    rebatch_rows,
)
from devops_bench.results.normalize import (
    build_rows,
    derive_augmentation,
    extract_score,
    normalize_tokens,
    setup_id,
    slugify,
)
from devops_bench.results.row import SCHEMA_VERSION, Manifest, ResultRow

__all__ = [
    "SCHEMA_VERSION",
    "Manifest",
    "ResultRow",
    "aggregate",
    "build_manifests",
    "build_rows",
    "dedupe_latest",
    "derive_augmentation",
    "discover_row_files",
    "extract_score",
    "normalize_tokens",
    "rebatch_rows",
    "setup_id",
    "slugify",
]
