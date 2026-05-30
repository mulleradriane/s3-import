variable "tags" {
  type = map(any)
}

variable "custom_tags" {
  type    = map(any)
  default = {}
}

variable "legacy_name" {
  type    = string
  default = null
}

variable "require_data_tags" {
  type    = bool
  default = false
}
