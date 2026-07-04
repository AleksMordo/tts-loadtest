terraform {
  required_version = ">= 1.5.0"
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = ">= 0.130"
    }
  }
}

# Аутентификация: переменная окружения YC_TOKEN (yc iam create-token)
# либо YC_SERVICE_ACCOUNT_KEY_FILE. folder_id/zone — в terraform.tfvars.
provider "yandex" {
  folder_id = var.folder_id
  zone      = var.zone
}
