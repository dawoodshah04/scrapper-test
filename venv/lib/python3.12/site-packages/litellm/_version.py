import importlib_metadata

try:
    version = importlib_metadata.version("unclecode-litellm")
except Exception:
    try:
        version = importlib_metadata.version("litellm")
    except Exception:
        version = "unknown"
