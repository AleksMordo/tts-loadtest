variable "folder_id" {
  type        = string
  description = "YC folder id"
}

variable "zone" {
  type        = string
  default     = "ru-central1-a"
  description = "Зона размещения (у GPU-платформ ограниченный список зон — сверить с докой)"
}

variable "gpu_platform_id" {
  type        = string
  default     = "gpu-standard-v3"
  description = <<-EOT
    Платформа GPU-ВМ. Кандидаты: gpu-standard-v3 (A100 80GB),
    standard-v3-t4 (T4 16GB), gpu-standard-v2 (V100 32GB), новая V4-платформа.
    Актуальный список и цены: https://yandex.cloud/ru/docs/compute/pricing
  EOT
}

variable "gpu_count" {
  type    = number
  default = 1
}

variable "gpu_vm_cores" {
  type        = number
  default     = 28
  description = "vCPU GPU-ВМ (у GPU-платформ фиксированы на конфигурацию — сверить с докой)"
}

variable "gpu_vm_memory_gb" {
  type    = number
  default = 119
}

variable "gpu_disk_gb" {
  type        = number
  default     = 150
  description = "SSD: веса модели (~5 GB) + docker images; ужато под квоту 200 GB SSD"
}

# loadgen ужат под дефолтные квоты (32 vCPU / 128 GB RAM на облако):
# 28+4 vCPU, 119+8 GB RAM. Для полки 100 линий этого хватает (asyncio-клиент
# лёгкий), контроль CPU loadgen — в Prometheus (node_loadgen).
variable "loadgen_cores" {
  type    = number
  default = 4
}

variable "loadgen_memory_gb" {
  type    = number
  default = 8
}

variable "loadgen_disk_gb" {
  type    = number
  default = 30
}

variable "preemptible" {
  type        = bool
  default     = false
  description = "Прерываемые ВМ дешевле, но полку 100 линий гнать на обычной ВМ"
}

variable "allowed_ssh_cidr" {
  type        = string
  description = "CIDR, откуда разрешён SSH (ваш IP/32)"
}

variable "image_family" {
  type        = string
  default     = "ubuntu-2204-lts"
  description = "Семейство образа. Если не GPU-optimized — драйверы поставит роль nvidia"
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}

variable "ssh_user" {
  type    = string
  default = "ubuntu"
}

variable "subnet_cidr" {
  type    = string
  default = "10.128.0.0/24"
}
