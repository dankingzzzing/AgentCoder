import json

def average(values):
    """Return the mean, or 0.0 for an empty sequence."""
    return sum(values) / len(values) if values else 0.0

def add_tag(tag, tags=None):
    """Independent calls should have independent lists."""
    if tags is None:
        tags = []
    tags.append(tag)
    return tags

def parse_score(raw):
    """Accept only numeric JSON values."""
    value = json.loads(raw)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a numeric JSON value")
    return value

def load_score(raw):
    """Invalid integer strings must raise ValueError."""
    return int(raw)
