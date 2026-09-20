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
  # Per-run path so concurrent runs do not share the report.
  report_path = var.report_path != "" ? var.report_path : "~/rightsizing-report-${var.cluster_name}.json"
}

# setup.sh labels and taints two of the three workers as the Spot pool.
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
  }
}

# Managed by Terraform so `tofu destroy` removes it.
resource "local_file" "rightsizing_report" {
  filename = pathexpand(local.report_path)
  content  = file("${path.module}/manifests/rightsizing-report.json")

  # The agent may run as a different uid than the provisioner, and the
  # verifiers encode this report's values, so it has to be readable.
  file_permission = "0644"
}

# Per-node taints need kubectl; the kind provider cannot express them.
resource "null_resource" "setup" {
  triggers = {
    cluster = kind_cluster.default.name
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/setup.sh"
    environment = {
      KUBECONFIG    = pathexpand(var.kubeconfig_path)
      MANIFESTS_DIR = "${path.module}/manifests"
      # setup.sh reads $HOME under set -u; a local-exec only inherits what
      # the caller had.
      HOME = pathexpand("~")
    }
  }
}
