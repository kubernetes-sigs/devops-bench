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

"""Per-run isolation so multiple benchmark processes can run concurrently.

A single host (a bastion VM or a laptop) can host several benchmark processes at
once, each provisioning and driving its own cluster. They collide on three
process-global resources unless each run is given its own: the kubeconfig file
(``gcloud get-credentials`` overwrites the active context), the active gcloud
config, and the OpenTofu data dir / lock. :class:`RunEnv` derives a unique run
id and, when isolation is on, points ``KUBECONFIG`` / ``CLOUDSDK_CONFIG`` /
``TF_DATA_DIR`` at per-run paths under a scratch dir. Because every downstream
``gcloud`` / ``kubectl`` / ``tofu`` / agent invocation inherits the process
environment, setting these three variables once is sufficient to isolate them.

Isolation is opt-in (``--parallel`` / ``BENCH_PARALLEL``) so single-run and
``--no-infra`` local workflows that rely on the operator's ambient kubeconfig
keep their existing behavior.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from devops_bench.core.config import get_env
from devops_bench.core.logging import get_logger

__all__ = ["RunEnv"]

_log = get_logger("core.run_env")

# GKE cluster names are capped at 40 chars and must match
# ``[a-z]([-a-z0-9]*[a-z0-9])?``.
_MAX_CLUSTER_NAME = 40

# The run discriminator is placed at the FRONT of derived cluster names so it
# survives the service-account ``account_id`` truncation in the tf/ stacks
# (``substr(cluster_name, 0, 10)`` in the tightest stack). 8 chars + a leading
# letter keeps the SA name unique while staying inside that window.
_CLUSTER_TOKEN_LEN = 8


def _default_run_id() -> str:
    """Build a run id unique among concurrent processes on one host.

    The PID guarantees uniqueness across processes running at the same instant;
    the timestamp prefix keeps run dirs human-sortable.

    Returns:
        A run id of the form ``"<YYYYmmdd-HHMMSS>-<pid>"``.
    """
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


@dataclass(frozen=True)
class RunEnv:
    """Isolation context for a single benchmark process.

    Attributes:
        run_id: Stable identifier for this run, reused for naming derived
            artifacts (cluster, run dir, OpenClaw session profile).
        isolated: Whether per-run isolation is active. When False the object is
            an inert pass-through (no env mutation, cluster name unchanged).
        run_dir: Scratch directory holding the run's kubeconfig / gcloud config
            / tofu data. Created only when ``isolated``.
        kubeconfig_path: Per-run ``KUBECONFIG`` target, or None when not isolated.
        gcloud_config_path: Per-run ``CLOUDSDK_CONFIG`` target, or None.
        tf_data_dir: Per-run ``TF_DATA_DIR`` target, or None.
    """

    run_id: str
    isolated: bool
    run_dir: Path
    kubeconfig_path: str | None
    gcloud_config_path: str | None
    tf_data_dir: str | None

    @classmethod
    def create(
        cls,
        *,
        parallel: bool,
        run_id: str | None = None,
        state_root: str | os.PathLike[str] | None = None,
    ) -> RunEnv:
        """Build a run environment, resolving the run id and scratch paths.

        Args:
            parallel: When True, derive per-run isolated paths; when False, an
                inert pass-through is returned.
            run_id: Explicit run id; defaults to ``RUN_ID`` env, then a
                PID/timestamp-derived id.
            state_root: Root for the per-run scratch dir; defaults to
                ``BENCH_RUN_STATE_ROOT`` env, then ``<tmp>/devops-bench-runs``.

        Returns:
            The constructed :class:`RunEnv`. No filesystem or environment side
            effects occur until :meth:`apply` is called.
        """
        resolved_id = run_id or get_env("RUN_ID") or _default_run_id()

        if not parallel:
            return cls(
                run_id=resolved_id,
                isolated=False,
                run_dir=Path(os.getcwd()),
                kubeconfig_path=None,
                gcloud_config_path=None,
                tf_data_dir=None,
            )

        root = Path(
            state_root
            or get_env("BENCH_RUN_STATE_ROOT")
            or (Path(tempfile.gettempdir()) / "devops-bench-runs")
        )
        run_dir = root / resolved_id
        return cls(
            run_id=resolved_id,
            isolated=True,
            run_dir=run_dir,
            kubeconfig_path=str(run_dir / "kubeconfig"),
            gcloud_config_path=str(run_dir / "gcloud"),
            tf_data_dir=str(run_dir / "tf-data"),
        )

    def apply(self) -> None:
        """Create the scratch dir and point process env at the per-run paths.

        A no-op when not isolated. Mutates ``os.environ`` so every subprocess
        spawned afterward (``gcloud`` / ``kubectl`` / ``tofu`` / agents)
        inherits the isolated kubeconfig, gcloud config, and tofu data dir.
        """
        if not self.isolated:
            return
        self.run_dir.mkdir(parents=True, exist_ok=True)
        Path(self.gcloud_config_path).mkdir(parents=True, exist_ok=True)  # type: ignore[arg-type]
        Path(self.tf_data_dir).mkdir(parents=True, exist_ok=True)  # type: ignore[arg-type]
        os.environ["KUBECONFIG"] = self.kubeconfig_path  # type: ignore[assignment]
        os.environ["CLOUDSDK_CONFIG"] = self.gcloud_config_path  # type: ignore[assignment]
        os.environ["TF_DATA_DIR"] = self.tf_data_dir  # type: ignore[assignment]
        # Publish the run id + scratch dir so run-aware components that spawn
        # their own subprocesses (e.g. the OpenClaw agent isolating its state
        # dir) can locate per-run paths without re-deriving them.
        os.environ["RUN_ID"] = self.run_id
        os.environ["BENCH_RUN_DIR"] = str(self.run_dir)
        _log.info(
            "run isolation active (run_id=%s): KUBECONFIG=%s CLOUDSDK_CONFIG=%s TF_DATA_DIR=%s",
            self.run_id,
            self.kubeconfig_path,
            self.gcloud_config_path,
            self.tf_data_dir,
        )

    @property
    def cluster_token(self) -> str:
        """Short, prefix-safe discriminator derived from the run id.

        Deterministic in the run id (so an operator-supplied ``RUN_ID`` still
        yields a stable token), lowercase, and starting with a letter so it is a
        valid leading segment of a GKE cluster / service-account name.
        """
        digest = hashlib.blake2s(self.run_id.encode(), digest_size=8).hexdigest()
        return ("c" + digest)[:_CLUSTER_TOKEN_LEN]

    def cluster_name(self, base: str) -> str:
        """Derive a collision-free cluster name from ``base`` for this run.

        Returns ``base`` unchanged when not isolated. Otherwise prepends the
        run token so two concurrent runs of the same task target distinct
        clusters, clamped to the GKE 40-char limit with no trailing dash.

        Args:
            base: The configured (shared) cluster name.

        Returns:
            The per-run cluster name.
        """
        if not self.isolated:
            return base
        return f"{self.cluster_token}-{base}"[:_MAX_CLUSTER_NAME].rstrip("-")
