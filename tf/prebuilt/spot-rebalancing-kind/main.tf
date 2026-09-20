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
  report_path = var.report_path != "" ? var.report_path : "~/rightsizing-report-${var.cluster_name}.json"
}

# Multi-node kind cluster: 1 control-plane + 3 workers. setup.sh designates one
# worker as the "on-demand" pool and taints/labels the other two as a reserved
# "spot" pool (control-plane is tainted by kind, so workloads land on workers).
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

# Deliver the rightsizing (VPA) report declaratively. Managed by TF, so it is
# removed automatically on `tofu destroy` — no teardown shell needed.
resource "local_file" "rightsizing_report" {
  filename = pathexpand(local.report_path)
  content  = file("${path.module}/manifests/rightsizing-report.json")
}

# Outside-the-cluster setup: label/taint the node pools and deploy the fleet. The
# node taints/labels need kubectl (the kind provider can't express per-node taints
# declaratively); the fleet apply + readiness wait round it out. Runs during
# `tofu apply`, before the agent starts.
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
    }
  }
}
