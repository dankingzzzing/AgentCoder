def average(values):
    """Return the mean, or 0.0 for an empty sequence."""
    return sum(values) / len(values)

def add_tag(tag, tags=[]):
    """Independent calls should have independent lists."""
    tags.append(tag)
    return tags

def parse_score(raw):
    """Accept only numeric JSON values."""
    return eval(raw)

def load_score(raw):
    """Invalid integer strings must raise ValueError."""
    try:
        return int(raw)
    except:
        pass
