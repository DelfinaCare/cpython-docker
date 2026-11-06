# Build every supported CPython release for amd64 and arm64:
#
#   docker buildx bake                    # all versions, both platforms
#   docker buildx bake python-3_15        # one version
#   docker buildx bake test               # smoke tests, native platform
#
# C library versions are pinned in the Dockerfile and shared by every CPython
# version, so their stages are built once and reused across the matrix.

variable "IMAGE" {
  default = "python-from-source"
}

# One entry per supported minor version. Keep the 3.14 entry in sync with the
# PYTHON_VERSION / PYTHON_SHA256 defaults in the Dockerfile.
variable "PYTHON_RELEASES" {
  default = [
    { minor = "3.14", version = "3.14.10", sha256 = "de414a57119abef52784d8993e341d53aed635fc2b05c2607d9f98a2c5b72032" },
    { minor = "3.15", version = "3.15.2",  sha256 = "8b5536d280142ecf6cdfe15d12a21ba1c65e300bbf7b28a8d0cdb849575797de" },
  ]
}

group "default" {
  targets = ["python"]
}

target "python" {
  name      = "python-${replace(py.minor, ".", "_")}"
  matrix    = { py = PYTHON_RELEASES }
  context   = "."
  target    = "runtime"
  platforms = ["linux/amd64", "linux/arm64"]
  args = {
    PYTHON_VERSION = py.version
    PYTHON_SHA256  = py.sha256
  }
  tags = ["${IMAGE}:${py.version}", "${IMAGE}:${py.minor}"]
}

target "test" {
  name    = "test-${replace(py.minor, ".", "_")}"
  matrix  = { py = PYTHON_RELEASES }
  context = "."
  target  = "test"
  args = {
    PYTHON_VERSION = py.version
    PYTHON_SHA256  = py.sha256
  }
  output = ["type=cacheonly"]
}
