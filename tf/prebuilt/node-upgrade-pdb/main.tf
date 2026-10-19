terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 7.45.0"
    }
    kind = {
      source  = "tehcyx/kind"
      version = "= 0.11.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "= 3.3.1"
    }
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" && var.location != "local" ? var.location : null
}

provider "kind" {}

# node-upgrade-pdb: The upgrade that can't drain the node it needs to.
# 1 cp + 2 workers. The setup script seeds workloads, PDBs, a DaemonSet,
# a health-poller, the change-record ConfigMap, and node labels.
module "cluster" {
  source          = "../../modules/cluster"
  infra_provider  = var.infra_provider
  project_id      = var.project_id
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  kubeconfig_path = var.kubeconfig_path
}

resource "null_resource" "setup" {
  depends_on = [module.cluster]

  triggers = {
    cluster = module.cluster.cluster_name
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/setup.sh"
    environment = {
      INFRA_PROVIDER = var.infra_provider
      PROJECT_ID     = var.project_id
      CLUSTER_NAME   = module.cluster.cluster_name
      LOCATION       = var.location
      KUBECONFIG     = pathexpand(var.kubeconfig_path)
      MANIFESTS_DIR  = "${path.module}/manifests"
      WAIT_TIMEOUT   = var.wait_timeout
    }
  }
}
