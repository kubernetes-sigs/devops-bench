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

"""Assert a property of a command's output run inside a live pod.

``resource_property`` answers "what does the cluster say the state is"; this
verifier answers "what is actually happening inside the running container",
which a Deployment's spec and even its own ``status`` conditions cannot show:
a rollout can report ``Available=True`` on the declared, correct image tag
while the real service behind it is a stale binary (an old, unmanaged pod
still receiving traffic through the same Service) or a config regression only
observable by asking the workload itself (a tailed log, a served response). A
single ``kubectl exec`` captures that live signal; everything else (matching
one of several candidate pods, applying an operator to the captured text) is
shared with :mod:`resource_property` on purpose so the two checks read the
same way in a task's ``verification_spec``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, model_validator

from devops_bench.k8s import exec_pod, get_resource
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)
from devops_bench.verification.verifiers.resource_property import (
    _apply_op,
    _compile_regex,
    _object_name,
)

__all__ = ["PodExecVerifier"]

_VALUE_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "matches")


def _phase(obj: Any) -> str | None:
    """Best-effort ``status.phase`` for a matched pod."""
    if isinstance(obj, dict):
        status = obj.get("status")
        if isinstance(status, dict):
            phase = status.get("phase")
            if isinstance(phase, str):
                return phase
    return None


@VERIFIERS.register("pod_exec")
class PodExecVerifier(BaseVerifier):
    """Run a command inside a pod and compare its (trimmed) stdout.

    Exactly one pod is targeted per evaluation. ``resource_name`` addresses it
    directly; ``selector`` resolves to the first ``Running`` pod (by name, for
    determinism) among the matches, which is the common case for a task fixture
    that only ever runs one replica of the thing being probed (a dedicated
    prober pod, a sidecar's own pod). A selector matching a multi-replica
    Deployment checks one representative pod, not all of them — tasks that need
    every replica probed should declare one entry per pod name instead.

    Attributes:
        type: Discriminator literal, always ``"pod_exec"``.
        resource_name: Exact pod name. Mutually exclusive with ``selector``.
        selector: Label selector resolving to one or more pods; the first
            ``Running`` one (by name) is used.
        namespace: Optional namespace; defaults to the active one.
        container: Optional container name, required when the pod runs more
            than one container.
        command: Argv executed inside the container, never a shell string
            (pass ``["sh", "-c", "..."]`` explicitly for shell features).
        op: Comparison applied to the command's stripped stdout.
        value: Right-hand side of the comparison.
    """

    type: Literal["pod_exec"] = "pod_exec"
    resource_name: str | None = None
    selector: str | None = None
    namespace: str | None = None
    container: str | None = None
    command: list[str] = Field(min_length=1)
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "matches"]
    value: Any = None

    @model_validator(mode="after")
    def _check_shape(self) -> PodExecVerifier:
        """Reject combinations that cannot mean anything at evaluation time."""
        if bool(self.resource_name) == bool(self.selector):
            msg = "pod_exec takes exactly one of 'resource_name' or 'selector'"
            raise ValueError(msg)
        if self.op in _VALUE_OPS and self.value is None:
            raise ValueError(f"op {self.op!r} requires 'value'")
        if self.op == "matches":
            # Caught here so a malformed pattern is a load-time config error.
            # Left to ``_apply_op`` it would return False on every poll and the
            # entry would report ``fail``, which claims the command's output was
            # observed and did not match — nothing was ever compared.
            try:
                _compile_regex(str(self.value))
            except re.error as exc:
                msg = f"op 'matches' has an invalid pattern {self.value!r}: {exc}"
                raise ValueError(msg) from exc
        return self

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll the exec'd command's output until it satisfies ``op`` or times out."""
        return self._poll_to_result(lambda: self._check(timeout_sec), timeout_sec)

    def _resolve_pod_name(self, timeout_sec: float) -> tuple[str | None, str | None]:
        """Return ``(pod_name, error)``; exactly one is non-``None``."""
        if self.resource_name:
            return self.resource_name, None
        try:
            payload = get_resource(
                "pods",
                selector=self.selector,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
                timeout=single_call_timeout(timeout_sec),
            )
        except Exception as exc:  # noqa: BLE001 - a kubectl failure is a check error
            return None, f"kubectl get pods failed: {exc}"
        items = payload.get("items") or []
        if not items:
            return None, f"no pod matched selector {self.selector!r}"
        # A selector outlives the pods that carried it: a completed Job's pod
        # and a Succeeded predecessor of a restarted Deployment both keep their
        # labels. Sorting the raw matches by name can hand back one of those,
        # and every ``exec`` into it fails until the budget runs out even though
        # a healthy pod was sitting in the same match set.
        running = [item for item in items if _phase(item) == "Running"]
        if not running:
            phases = sorted({_phase(item) or "unknown" for item in items})
            return None, (
                f"no running pod matched selector {self.selector!r} "
                f"({len(items)} matched, phases: {', '.join(phases)})"
            )
        names = sorted(_object_name(item) for item in running)
        return names[0], None

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One evaluation pass: resolve the pod, exec the command, apply the operator."""
        pod_name, resolve_error = self._resolve_pod_name(timeout_sec)
        if pod_name is None:
            return "error", resolve_error or "could not resolve a target pod", None

        try:
            completed = exec_pod(
                pod_name,
                self.command,
                container=self.container,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
                timeout=single_call_timeout(timeout_sec),
            )
        except Exception as exc:  # noqa: BLE001 - a kubectl exec failure is a check error
            return "error", f"kubectl exec {pod_name!r} failed: {exc}", None

        output = completed.stdout.strip()
        raw = {"pod": pod_name, "command": self.command, "output": output}
        ok, reason = _apply_op(self.op, output, self.value)
        return ("pass" if ok else "fail"), f"{pod_name}: {reason}", raw
