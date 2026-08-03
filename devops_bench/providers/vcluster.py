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

"""vCluster provider: standalone virtual Kubernetes clusters on a host cluster."""

from __future__ import annotations

import ipaddress
import os
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ruamel.yaml import YAML

from devops_bench.core import (
    ClusterInfo,
    ConfigError,
    get_bool,
    get_env,
    get_logger,
)
from devops_bench.core.subprocess import run
from devops_bench.providers.base import PROVIDERS, Provider, ResolveContext

__all__ = ["VClusterProvider"]

_log = get_logger("providers.vcluster")

_LOCAL_CONTEXT_PREFIXES = (
    "kind-",
    "minikube",
    "docker-desktop",
    "colima",
    "k3d-",
    "rancher-desktop",
    "localhost",
    "local",
)


def _is_local_server_url(server: str) -> bool:
    """Check whether a Kubernetes server URL targets a local or private address."""
    if not server:
        return False
    try:
        split_res = urlsplit(server)
        hostname = split_res.hostname
    except Exception:
        return False
    if not hostname:
        return False
    if hostname in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        if isinstance(ip, ipaddress.IPv4Address):
            private_blocks = (
                ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
            )
            return any(ip in block for block in private_blocks)
    except ValueError:
        pass
    return False


def _get_current_context(kubeconfig_path: str) -> str:
    """Read the active current-context from a kubeconfig file."""
    path_obj = Path(kubeconfig_path).expanduser().resolve()
    if not path_obj.exists():
        raise ConfigError(f"No active current-context found in kubeconfig {kubeconfig_path}")
    yaml = YAML(typ="safe")
    try:
        with open(path_obj, encoding="utf-8") as f:
            config = yaml.load(f) or {}
    except Exception as exc:
        raise ConfigError(
            f"No active current-context found in kubeconfig {kubeconfig_path}: {exc}"
        ) from exc

    current = config.get("current-context") if isinstance(config, dict) else None
    if not current:
        raise ConfigError(f"No active current-context found in kubeconfig {kubeconfig_path}")
    return str(current)


def _is_allowlisted_context(context_name: str, kubeconfig_path: str) -> bool:
    """Check whether a kubecontext is local according to the allowlist rules."""
    if any(context_name.startswith(p) for p in _LOCAL_CONTEXT_PREFIXES):
        return True

    path_obj = Path(kubeconfig_path).expanduser().resolve()
    if not path_obj.exists():
        return False
    yaml = YAML(typ="safe")
    try:
        with open(path_obj, encoding="utf-8") as f:
            config = yaml.load(f) or {}
    except Exception:
        return False

    if not isinstance(config, dict):
        return False

    cluster_name = None
    for ctx in config.get("contexts", []):
        if isinstance(ctx, dict) and ctx.get("name") == context_name:
            context_data = ctx.get("context", {})
            if isinstance(context_data, dict):
                cluster_name = context_data.get("cluster")
            break

    if not cluster_name:
        return False

    for item in config.get("clusters", []):
        if isinstance(item, dict) and item.get("name") == cluster_name:
            cluster_data = item.get("cluster", {})
            if isinstance(cluster_data, dict):
                server = cluster_data.get("server", "")
                return _is_local_server_url(str(server))
    return False


@PROVIDERS.register("vcluster")
class VClusterProvider(Provider):
    """Provider for virtual Kubernetes clusters hosted on a standing host cluster."""

    def ensure_account_credentials(self) -> None:
        """No-op: vCluster provider account credentials are not needed inline."""
        _log.debug("VCluster provider: account credentials no-op")

    def ensure_cluster_credentials(
        self,
        cluster_name: str,
        location: str,
        variables: dict[str, Any],
        outputs: dict[str, Any] | None = None,
    ) -> ClusterInfo:
        """Extract virtual cluster kubeconfig from outputs and write securely to disk.

        Args:
            cluster_name: Cluster name from the stack outputs.
            location: Location from the stack outputs.
            variables: OpenTofu input variables the cluster was provisioned with.
            outputs: OpenTofu output values containing ``kubeconfig``.

        Returns:
            The cluster's :class:`~devops_bench.core.ClusterInfo`.

        Raises:
            ConfigError: If ``kubeconfig`` output is missing or target path is invalid.
        """
        if not outputs or "kubeconfig" not in outputs or not outputs["kubeconfig"]:
            raise ConfigError(
                "OpenTofu outputs missing 'kubeconfig' required for VClusterProvider."
            )
        kubeconfig_yaml = str(outputs["kubeconfig"])

        target_path = variables.get("kubeconfig_path")
        if not target_path:
            if get_bool("BENCH_PARALLEL", False) and get_env("KUBECONFIG"):
                target_path = get_env("KUBECONFIG")
            else:
                target_path = str(
                    Path(tempfile.gettempdir()) / f"vcluster-{cluster_name}-kubeconfig.yaml"
                )

        raw_target = Path(target_path).expanduser()
        if raw_target.is_symlink():
            raise ConfigError(f"Refusing to write kubeconfig to symlink: {target_path}")
        default_kubeconfig = str(Path("~/.kube/config").expanduser().resolve())
        resolved_target = raw_target.resolve()
        if resolved_target == Path(default_kubeconfig):
            raise ConfigError(
                "Refusing to overwrite ~/.kube/config with virtual cluster kubeconfig."
            )

        node_port = variables.get("node_port")
        if location == "local" and node_port is not None:
            try:
                yaml = YAML()
                config = yaml.load(kubeconfig_yaml)
                if isinstance(config, dict) and "clusters" in config:
                    for item in config["clusters"]:
                        if isinstance(item, dict):
                            cluster_data = item.get("cluster")
                            if isinstance(cluster_data, dict) and "server" in cluster_data:
                                server = str(cluster_data["server"])
                                if "127.0.0.1" in server or "localhost" in server:
                                    cluster_data["server"] = f"https://127.0.0.1:{node_port}"
                    stream = StringIO()
                    yaml.dump(config, stream)
                    kubeconfig_yaml = stream.getvalue()
            except Exception as exc:
                _log.warning("Failed to rewrite 127.0.0.1 server URL in kubeconfig: %s", exc)

        resolved_target.parent.mkdir(parents=True, exist_ok=True)
        if raw_target.is_symlink() or resolved_target.is_symlink():
            raise ConfigError(f"Refusing to write kubeconfig to symlink: {target_path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(resolved_target, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(kubeconfig_yaml)
        resolved_target.chmod(0o600)
        _log.info("Wrote virtual cluster kubeconfig to %s (mode 0600)", resolved_target)

        return ClusterInfo.from_dict(
            {
                "name": cluster_name,
                "location": location,
                "project": "local-vcluster",
                "kubeconfig_path": str(resolved_target),
            }
        )

    @staticmethod
    def _is_safe_scratch_path(path: Path) -> bool:
        """Check if a file path is a temporary scratch file safe for cleanup."""
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            return False

        default_kubeconfig = Path("~/.kube/config").expanduser().resolve()
        if resolved == default_kubeconfig:
            return False

        if resolved.is_dir():
            return False

        if resolved.parent == Path(tempfile.gettempdir()).resolve():
            return True

        if (Path(tempfile.gettempdir()) / "devops-bench-runs").resolve() in resolved.parents:
            return True

        if (
            get_env("BENCH_RUN_STATE_ROOT")
            and Path(get_env("BENCH_RUN_STATE_ROOT")).resolve() in resolved.parents
        ):
            return True

        return bool(
            get_env("TF_DATA_DIR")
            and Path(get_env("TF_DATA_DIR")).resolve().parent == resolved.parent
        )

    def cleanup(
        self,
        cluster_info: ClusterInfo,
        variables: dict[str, Any] | None = None,
    ) -> None:
        """Clean up orphaned PVs in the host cluster and remove temporary kubeconfig.

        Args:
            cluster_info: The cluster info of the cluster that was destroyed.
            variables: Optional OpenTofu input variables used during provisioning.
        """
        vars_dict = variables or {}
        host_kubeconfig = vars_dict.get("host_kubeconfig_path") or os.environ.get(
            "HOST_KUBECONFIG", "~/.kube/config"
        )
        host_kubeconfig_path = str(Path(host_kubeconfig).expanduser().resolve())
        host_context = vars_dict.get("host_kubecontext") or os.environ.get("HOST_KUBECONTEXT")
        if not host_context:
            try:
                host_context = _get_current_context(host_kubeconfig_path)
            except ConfigError:
                host_context = None

        if not cluster_info.name or not cluster_info.name.strip():
            _log.warning("Skipping VCluster PV cleanup: cluster_info.name is empty.")
        else:
            label_selector = (
                f"devops-bench/run-scoped=true,devops-bench/cluster-name={cluster_info.name}"
            )
            cmd = ["kubectl", f"--kubeconfig={host_kubeconfig_path}"]
            if host_context:
                cmd.append(f"--context={host_context}")
            get_cmd = cmd + [
                "get",
                "pv",
                "-l",
                label_selector,
                "-o",
                "jsonpath={.items[*].metadata.name}",
            ]

            res = run(get_cmd, capture=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                pv_names = res.stdout.strip().split()
                if pv_names:
                    _log.info(
                        "Deleting %d orphaned PersistentVolume(s) for cluster %s",
                        len(pv_names),
                        cluster_info.name,
                    )
                    del_cmd = cmd + ["delete", "pv", *pv_names]
                    run(del_cmd, capture=False, check=False)

        if cluster_info.kubeconfig_path:
            kube_path = Path(cluster_info.kubeconfig_path).expanduser().resolve()
            if self._is_safe_scratch_path(kube_path):
                try:
                    if kube_path.exists():
                        kube_path.unlink()
                        _log.info(
                            "Deleted temporary virtual cluster kubeconfig: %s",
                            kube_path,
                        )
                except OSError as exc:
                    _log.warning(
                        "Failed to delete temporary kubeconfig %s: %s",
                        kube_path,
                        exc,
                    )
            else:
                _log.debug(
                    "Skipping deletion of non-scratch kubeconfig path: %s",
                    kube_path,
                )

    def resolve_variables(
        self, ctx: ResolveContext, custom_variables: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve OpenTofu variables for vCluster stack deployment.

        Args:
            ctx: Default resolution context.
            custom_variables: Task-specified custom variables.

        Returns:
            Resolved OpenTofu variables mapping.

        Raises:
            ConfigError: If target host kubecontext is remote without allowance.
        """
        variables = custom_variables.copy()
        variables.setdefault("infra_provider", "vcluster")
        variables.setdefault("project_id", ctx.project_id or "local-vcluster")
        variables.setdefault("cluster_name", ctx.cluster_name or "devops-bench-vcluster")
        variables.setdefault("location", ctx.location or "local")

        namespace = get_env("NAMESPACE") or f"vcluster-{variables['cluster_name']}"
        variables.setdefault("namespace", namespace)

        host_kubeconfig = variables.get("host_kubeconfig_path") or os.environ.get(
            "HOST_KUBECONFIG", "~/.kube/config"
        )
        host_kubeconfig_path = str(Path(host_kubeconfig).expanduser().resolve())
        variables["host_kubeconfig_path"] = host_kubeconfig_path

        host_context = variables.get("host_kubecontext") or os.environ.get("HOST_KUBECONTEXT")
        if not host_context:
            host_context = _get_current_context(host_kubeconfig_path)
        variables["host_kubecontext"] = host_context

        is_local = _is_allowlisted_context(host_context, host_kubeconfig_path)
        if not is_local:
            if not get_bool("ALLOW_REMOTE_HOST_KUBECONTEXT", False):
                raise ConfigError(
                    f"Host kubecontext {host_context!r} is classified as remote. "
                    "Set ALLOW_REMOTE_HOST_KUBECONTEXT=true to allow."
                )
            variables.setdefault("service_type", "LoadBalancer")
        else:
            variables.setdefault("service_type", "NodePort")

        kubeconfig_path = variables.get("kubeconfig_path")
        default_kubeconfig = str(Path("~/.kube/config").expanduser().resolve())
        if (
            not kubeconfig_path
            or str(Path(kubeconfig_path).expanduser().resolve()) == default_kubeconfig
        ):
            if get_bool("BENCH_PARALLEL", False) and get_env("KUBECONFIG"):
                kubeconfig_path = get_env("KUBECONFIG")
            else:
                kubeconfig_path = str(
                    Path(tempfile.gettempdir())
                    / f"vcluster-{variables['cluster_name']}-kubeconfig.yaml"
                )
            variables["kubeconfig_path"] = kubeconfig_path

        return variables
