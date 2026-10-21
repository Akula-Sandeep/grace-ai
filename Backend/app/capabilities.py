import importlib.util

# pgvector is required by migrations; it is reported so a broken image is visible at startup.
OPTIONAL_RUNTIME_PACKAGES = ("pgvector", "sentence_transformers")


def runtime_capabilities() -> dict[str, bool]:
    return {package: importlib.util.find_spec(package) is not None for package in OPTIONAL_RUNTIME_PACKAGES}
