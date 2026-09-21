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
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.0.0"
    }
  }
}

provider "kind" {}

locals {
  # Host-side artifact on the shared bastion. cluster_name is run-token-prefixed,
  # making it per-run unique so concurrent runs never collide. The task prompt
  # references the same path via the {{CLUSTER_NAME}} placeholder. An explicit
  # override wins.
  report_path = var.report_path != "" ? var.report_path : "~/carbon-report-${var.cluster_name}.json"
}

# Multi-node kind cluster: 1 control-plane + 4 workers. The workloads run on the
# four workers (control-plane is tainted by kind); the agent consolidates them
# onto fewer workers by cordoning + draining the underutilized ones.
resource "kind_cluster" "default" {
  name            = var.cluster_name
  node_image      = var.node_image
  kubeconfig_path = pathexpand(var.kubeconfig_path)
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
    }
    node {
      role = "worker"
    }
    node {
      role = "worker"
    }
    node {
      role = "worker"
    }
    node {
      role = "worker"
    }
  }
}

# Deliver the carbon-aware capacity report declaratively. Managed by TF, so it is
# removed automatically on `tofu destroy` — no teardown shell needed.
resource "local_file" "carbon_report" {
  filename = pathexpand(local.report_path)
  content  = file("${path.module}/manifests/carbon-report.json")
}

# Outside-the-cluster setup: deploy the fleet and wait for it to be Available. The
# kubectl apply isn't expressible as plan-time-safe declarative TF (kind has no
# cluster at plan time), so a thin script remains. Runs during `tofu apply`,
# before the agent starts.
resource "null_resource" "setup" {
  # Every input the setup depends on, not just the cluster name.
  #
  # Keyed on the name alone, an edit to scripts/setup.sh or to any manifest it
  # applies leaves this resource unchanged, so a re-apply skips the labelling,
  # the fleet, and the assertions entirely — the fixture change lands in the repo
  # but never reaches the cluster, and the task quietly runs its previous shape.
  # That is a silent wrong-fixture failure, which is worse than a loud one.
  #
  # The CA certificate is the replacement sentinel: a cluster torn down and
  # rebuilt under the same name gets a fresh CA, whereas `name` (and `id`, which
  # the provider derives from it) would not move.
  triggers = {
    cluster          = kind_cluster.default.name
    cluster_instance = sha256(kind_cluster.default.cluster_ca_certificate)
    setup_script     = filesha256("${path.module}/scripts/setup.sh")
    manifests = sha256(join("", [
      for f in sort(tolist(fileset("${path.module}/manifests", "**"))) :
      "${f}:${filesha256("${path.module}/manifests/${f}")}"
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/setup.sh"
    environment = {
      KUBECONFIG    = pathexpand(var.kubeconfig_path)
      MANIFESTS_DIR = "${path.module}/manifests"
    }
  }
}
