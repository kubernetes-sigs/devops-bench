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

terraform {
  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
  }
}

locals {
  # One containerd patch per mirrored host. A map iterates in lexical key order,
  # so the rendered list is stable across plans and adding a second host never
  # shows up as a diff on the first. Host and endpoint both go through
  # jsonencode: TOML and Go-template quoting are not the same, and an endpoint
  # carrying a quote or a backslash must not be able to break out of the string.
  #
  # registry.mirrors is the form containerd 1.x reads, which is what the pinned
  # node image ships. containerd 2.x deprecates it for a hosts.toml config_path;
  # that migration belongs with the node-image bump, not here.
  registry_mirror_patches = [
    for host, endpoints in var.registry_mirrors :
    <<-EOT
    [plugins."io.containerd.grpc.v1.cri".registry.mirrors.${jsonencode(host)}]
      endpoint = [${join(", ", [for endpoint in endpoints : jsonencode(endpoint)])}]
    EOT
  ]
}

resource "kind_cluster" "default" {
  name            = var.cluster_name
  node_image      = var.node_image
  kubeconfig_path = pathexpand(var.kubeconfig_path)
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    containerd_config_patches = local.registry_mirror_patches

    node {
      role = "control-plane"
    }

    dynamic "node" {
      for_each = range(max(0, var.node_count - 1))
      content {
        role = "worker"
      }
    }
  }
}

# Duplicates the KinD context to match a GKE-like name pattern.
# This is required for third-party gke-mcp tools to resolve the context when
# running tasks against local KinD clusters, as the MCP client expects the
# context to conform to the "gke_{project}_{location}_{cluster}" format.
resource "null_resource" "duplicate_context" {
  depends_on = [kind_cluster.default]

  triggers = {
    kubeconfig   = pathexpand(var.kubeconfig_path)
    kind_cluster = "kind-${var.cluster_name}"
    kind_user    = "kind-${var.cluster_name}"
    gke_context  = "gke_${var.project_id}_${var.location}_${var.cluster_name}"
  }

  provisioner "local-exec" {
    command = "kubectl --kubeconfig='${self.triggers.kubeconfig}' config set-context '${self.triggers.gke_context}' --cluster='${self.triggers.kind_cluster}' --user='${self.triggers.kind_user}'"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "kubectl --kubeconfig='${self.triggers.kubeconfig}' config delete-context '${self.triggers.gke_context}' || true"
  }
}
