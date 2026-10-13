"""Read partial top-level public tool strings; never parse provider reasoning."""

import json
import re


def public_strings(arguments):
    result = {}
    for key in ("solution", "tests", "explanation", "summary"):
        match = re.search(r'(?<!\\)"' + key + r'"\s*:\s*"', arguments)
        if not match:
            continue
        start, end, safe = match.end(), match.end(), match.end()
        while end < len(arguments):
            char = arguments[end]
            if char == '"':
                break
            if char == "\\":
                needed = 6 if arguments[end:end+2] == "\\u" else 2
                if end + needed > len(arguments):
                    break
                end += needed
            else:
                end += 1
            safe = end
        try:
            value = json.loads('"' + arguments[start:safe] + '"')
            result[key] = value.encode("utf-8", errors="replace").decode("utf-8")
        except (ValueError, UnicodeError):
            continue
    return result
