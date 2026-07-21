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

"""OpenTofu-backed deployer driving repo-local ``tf/`` stacks."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devops_bench.core import ClusterInfo, ConfigError, get_logger
from devops_bench.core.subprocess import run
from devops_bench.deployers.base import Deployer

if TYPE_CHECKING:
    from devops_bench.providers import Provider

__all__ = ["TFDeployer"]

# This module lives at ``<repo_root>/devops_bench/deployers/tofu.py``; the repo
# root is therefore three levels up, and Tofu stacks live under ``<repo_root>/tf``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TF_ROOT = _REPO_ROOT / "tf"

_log = get_logger("deployers.tofu")


def _format_var(value: Any) -> str:
    """Format a Python value as an OpenTofu ``-var`` literal.

    Args:
        value: Variable value to serialize.

    Returns:
        ``"true"``/``"false"`` for booleans, JSON for lists and dicts,
        ``"null"`` for ``None``, and ``str(value)`` otherwise.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if value is None:
        return "null"
    return str(value)


def _get_declared_variables(tf_dir: str) -> set[str]:
    """Scan the stack directory for declared variable names.

    Scans HCL ``.tf`` files with a fast line-based regex and ``.tf.json`` files
    by parsing their top-level ``variable`` object. The HCL scan is a heuristic
    and can be fooled by a ``variable`` block commented out inside a ``/* ... */``
    span; such a false positive only downgrades a clean drop to a tofu-side
    error, never the reverse.
    """
    declared: set[str] = set()
    for path in Path(tf_dir).glob("*.tf"):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    match = re.match(r'^\s*variable\s+"([^"]+)"', line)
                    if match:
                        declared.add(match.group(1))
        except OSError:
            continue
    for path in Path(tf_dir).glob("*.tf.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        variables = data.get("variable") if isinstance(data, dict) else None
        if isinstance(variables, dict):
            declared.update(variables)
    return declared


def _isolated_work_dir(stack_dir: str, tf_root: Path) -> str:
    """Return a per-run private copy of the stack dir, or ``stack_dir`` unchanged.

    Per-run isolation already keys ``TF_DATA_DIR`` and the state file per run, but
    both still ran ``tofu`` in the *shared* ``tf/prebuilt/<stack>`` directory, so
    two concurrent runs of the SAME stack contend on its ``.terraform.lock.hcl``
    (no lock file is committed, so every ``init`` rewrites it). To give each run a
    private working directory, copy the WHOLE ``tf/`` tree — stacks reference
    modules via relative ``../../`` paths, so a leaf-only copy would break — into
    the run's scratch dir (the parent of ``TF_DATA_DIR``, beside the per-run
    state file) and run tofu in the copied stack.

    Only applies to in-repo stacks under an isolated (parallel) run; external or
    absolute stacks and single (non-isolated) runs keep the original directory.
    Any copy failure falls back to the original directory so provisioning still
    proceeds (degrading to the shared-dir behavior, never failing).
    """
    tf_data_dir = os.environ.get("TF_DATA_DIR")
    if not tf_data_dir or not tf_data_dir.strip():
        return stack_dir
    try:
        rel = Path(stack_dir).resolve().relative_to(tf_root.resolve())
    except ValueError:
        return stack_dir  # external/absolute stack: cannot relocate safely
    try:
        run_dir = Path(tf_data_dir).resolve().parent
        dest_tf = run_dir / "tf"
        shutil.copytree(tf_root, dest_tf, dirs_exist_ok=True)
        return str(dest_tf / rel)
    except OSError as exc:
        _log.warning(
            "could not isolate tofu stack dir (%s); falling back to shared %s",
            exc,
            stack_dir,
        )
        return stack_dir


class TFDeployer(Deployer):
    """Deployer that provisions a cluster via an OpenTofu stack.

    A pure provisioning engine: it runs ``tofu`` and delegates all cloud-specific
    behavior (account credentials, cluster credentials, project resolution) to
    its :class:`~devops_bench.providers.Provider`. Honors the ``TF_DATA_DIR``
    environment variable so OpenTofu state can be redirected for idempotent runs.

    Path resolution: a relative ``tf_dir`` is resolved under ``<repo_root>/tf``;
    an absolute path (``~`` is expanded) is used as-is, so stacks may live outside
    the repository.

    Args:
        tf_dir: Stack directory; an absolute/``~`` path used as-is, or a name
            resolved under ``<repo_root>/tf``.
        provider: Cloud provider supplying credentials and cluster details.
        variables: OpenTofu input variables passed as ``-var`` flags.
        custom_keys: Subset of ``variables`` that came from task-level config;
            any of these not declared in the TF stack raises ``ConfigError``
            in :meth:`_var_flags`.

    Raises:
        ConfigError: If the stack directory does not exist.
    """

    def __init__(
        self,
        tf_dir: str,
        provider: Provider,
        variables: dict[str, Any] | None = None,
        custom_keys: set[str] | None = None,
    ) -> None:
        tf_path = Path(tf_dir).expanduser()
        if tf_path.is_absolute():
            if not tf_path.exists():
                raise ConfigError(f"Absolute TF directory not found: {tf_dir}")
            self.tf_dir = str(tf_path)
        else:
            repo_tf_path = _TF_ROOT / tf_path
            if not repo_tf_path.exists():
                raise ConfigError(f"TF stack not found in repo: {tf_dir} (checked {repo_tf_path})")
            self.tf_dir = str(repo_tf_path)

        # Per-run private working directory (a copy of the tf/ tree under the
        # run's scratch dir) when isolated; otherwise the shared stack dir.
        self.work_dir = _isolated_work_dir(self.tf_dir, _TF_ROOT)

        self.provider = provider
        self.variables = variables or {}
        self.custom_keys = custom_keys or set()

    def _var_flags(self) -> list[str]:
        # Scan the directory tofu actually runs in (the isolated per-run copy
        # under parallel runs; the shared stack dir otherwise), not self.tf_dir.
        declared = _get_declared_variables(self.work_dir)
        flags: list[str] = []
        for key, value in self.variables.items():
            if key in declared:
                flags.extend(["-var", f"{key}={_format_var(value)}"])
            elif key in self.custom_keys:
                raise ConfigError(
                    f"Variable {key!r} defined in task config is not declared in TF stack {self.tf_dir!r}"
                )
            else:
                _log.warning(
                    "dropping variable %r passed to tofu stack %r: not declared in tf files",
                    key,
                    self.tf_dir,
                )
        return flags

    @staticmethod
    def _state_flags() -> list[str]:
        """Return ``-state`` flags placing per-run state beside ``TF_DATA_DIR``.

        When ``TF_DATA_DIR`` is set (the parallel-isolation path keys it to a
        per-run ``<run>/tf-data`` dir), the local state file is written to
        ``<run>/terraform.tfstate`` — the *parent* of ``TF_DATA_DIR``, NOT
        inside it. ``<TF_DATA_DIR>/terraform.tfstate`` is OpenTofu's reserved
        backend-state path; writing the full resource state there makes a later
        ``tofu init``/``output`` fail with "does not support state version 4".
        Returns an empty list when ``TF_DATA_DIR`` is unset, so a single run
        keeps OpenTofu's default in-directory state.
        """
        tf_data_dir = os.environ.get("TF_DATA_DIR")
        if not tf_data_dir or not tf_data_dir.strip():
            return []
        return ["-state", str(Path(tf_data_dir).resolve().parent / "terraform.tfstate")]

    def up(self) -> None:
        tf_path = Path(self.work_dir)
        if not tf_path.exists():
            raise ConfigError(f"TF directory not found: {self.work_dir} (stack: {self.tf_dir})")

        self.provider.ensure_account_credentials()
        run(["tofu", "init", "-input=false"], cwd=self.work_dir, capture=False)

        cmd = [
            "tofu",
            "apply",
            "-auto-approve",
            "-input=false",
            *self._state_flags(),
            *self._var_flags(),
        ]
        run(cmd, cwd=self.work_dir, capture=False)

    def down(self) -> None:
        tf_path = Path(self.work_dir)
        if not tf_path.exists():
            _log.warning(
                "TF directory %s (stack: %s) not found. Skipping teardown.",
                self.work_dir,
                self.tf_dir,
            )
            return

        self.provider.ensure_account_credentials()
        run(["tofu", "init", "-input=false"], cwd=self.work_dir, capture=False)

        cmd = [
            "tofu",
            "destroy",
            "-auto-approve",
            "-input=false",
            *self._state_flags(),
            *self._var_flags(),
        ]
        run(cmd, cwd=self.work_dir, capture=False)

    def get_cluster_info(self) -> ClusterInfo:
        """Read cluster details from the stack outputs.

        Parses the stack outputs (no side effects) and delegates project
        resolution and kubeconfig setup to the provider.

        Returns:
            The provisioned cluster's :class:`~devops_bench.core.ClusterInfo`.

        Raises:
            ConfigError: If required outputs are missing or unparseable.
        """
        run(["tofu", "init", "-input=false"], cwd=self.work_dir, capture=False)

        result = run(
            ["tofu", "output", "-json", *self._state_flags()],
            cwd=self.work_dir,
            capture=True,
        )
        try:
            outputs = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ConfigError("failed to parse 'tofu output -json'") from exc

        cluster_name = outputs.get("cluster_name", {}).get("value")
        if not cluster_name:
            raise ConfigError("Failed to retrieve 'cluster_name' from TF outputs.")

        location = outputs.get("cluster_location", {}).get("value")
        if not location:
            raise ConfigError("Failed to retrieve 'cluster_location' from TF outputs.")

        return self.provider.ensure_cluster_credentials(cluster_name, location, self.variables)
