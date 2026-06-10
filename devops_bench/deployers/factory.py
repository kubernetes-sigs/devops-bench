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
from typing import Any

from devops_bench.deployers.base import Deployer


def get_deployer(
    infra_config: dict[str, Any],
    global_project_id: str,
    global_cluster_name: str,
    global_location: str = None,
) -> Deployer:
    """
    Factory to instantiate the appropriate infrastructure deployer.

    Enforces GCP_LOCATION as the standard environment variable for location.

    Note: concrete deployer implementations are imported lazily so this module
    only depends on the Deployer interface. The concrete deployers (gcp, tf,
    kind) are migrated in later steps; this factory resolves them on demand.
    """
    # Imported lazily to keep this module decoupled from the concrete
    # implementations, which may not be present yet during migration.
    from devops_bench.deployers.gcp.gcp_deployer import GCPDeployer
    from devops_bench.deployers.gcp.variables import resolve_variables as resolve_gcp_vars
    from devops_bench.deployers.kind.variables import resolve_variables as resolve_kind_vars
    from devops_bench.deployers.tf.tf_deployer import TFDeployer

    provider_resolvers = {
        "gcp": resolve_gcp_vars,
        "kind": resolve_kind_vars,
    }

    # Respect task-level deployer first, fallback to cloud_provider if it is a valid deployer, otherwise default to terraform
    cloud_provider = os.environ.get("CLOUD_PROVIDER", "").lower()
    deployer_type = infra_config.get("deployer")
    if not deployer_type:
        deployer_type = cloud_provider if cloud_provider in ["tofu", "gcp"] else "tofu"

    # Resolve Location with strict precedence: argument then GCP_LOCATION env var
    location = global_location or os.environ.get("GCP_LOCATION", "us-central1-a")

    if deployer_type == "tofu":
        stack = infra_config.get("stack") or "prebuilt/kind"
        variables = infra_config.get("variables", {})

        # Deduce provider from stack name or CLOUD_PROVIDER env var
        provider = cloud_provider or ("kind" if "kind" in stack else "gcp")

        # Dynamically resolve variables based on the cloud provider
        resolver = provider_resolvers.get(provider)
        if resolver:
            variables = resolver(stack, variables, global_project_id, global_cluster_name, location)

        return TFDeployer(tf_dir=stack, variables=variables)

    # Fallback to legacy GCPDeployer (kubetest2)
    return GCPDeployer(
        project=global_project_id, location=location, cluster_name=global_cluster_name
    )
