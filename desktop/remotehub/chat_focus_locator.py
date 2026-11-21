"""Learned, version-bound AX structure matching. No text, indices or coordinates."""
import hashlib
import json
import re
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def valid(locator):
    return (isinstance(locator, dict) and set(locator) == {"version", "build", "signature"}
            and type(locator["version"]) is int and locator["version"] == 1
            and all(isinstance(locator[k], str) and re.fullmatch(r"[0-9a-f]{64}", locator[k])
                    for k in ("build", "signature")))


def signature(backend, target, window, cancelled=lambda: False):
    def metadata(node):
        role = backend.read(node, "AXRole")
        if not role or role in {"AXSheet", "AXDialog", "AXMenu", "AXMenuItem"}:
            raise ValueError("Incomplete or modal ancestry")
        subrole = backend.read(node, "AXSubrole")
        if subrole in {"AXSecureTextField", "AXDialog", "AXSystemDialog"}:
            raise ValueError("Protected ancestry")
        return [role, subrole, backend.read(node, "AXIdentifier")]

    chain, surroundings = [], []
    node = target
    for depth in range(64):
        if node is None or cancelled(): return None
        chain.append(metadata(node))
        if node == window:
            return digest([chain, surroundings])
        if 1 <= depth <= 3:
            children = backend.children(node)
            if len(children) > 64: return None
            surroundings.append([metadata(child) for child in children])
        node = backend.read(node, "AXParent")
    return None


def resolve(backend, window, locator, build, cancelled=lambda: False):
    if not valid(locator) or not build or locator["build"] != build or backend.modal(window):
        return None
    pending, matches = [(window, 0)], []
    deadline, count = time.monotonic() + 1.5, 0
    try:
        while pending and count < 3000 and time.monotonic() < deadline and not cancelled():
            node, depth = pending.pop()
            count += 1
            role = backend.read(node, "AXRole")
            if role is None or role in {"AXSheet", "AXDialog", "AXMenu", "AXMenuItem"}:
                return None
            if role == "AXTextArea":
                if (backend.read(node, "AXEnabled") is True and backend.settable(node)
                        and backend.visible(node, window)
                        and signature(backend, node, window, cancelled) == locator["signature"]):
                    matches.append(node)
                    if len(matches) > 1: return None
                continue
            if role in {"AXStaticText", "AXImage", "AXMenuBar"}: continue
            children = backend.children(node)
            if children and (depth >= 64 or count + len(pending) + len(children) > 3000):
                return None
            pending.extend((child, depth + 1) for child in children)
    except (ValueError, RuntimeError, TypeError):
        return None
    return matches[0] if not pending and not cancelled() and time.monotonic() < deadline and len(matches) == 1 else None


def describe(backend, window, target, build):
    if not build: return None
    try:
        value = signature(backend, target, window)
        locator = {"version": 1, "build": build, "signature": value}
        # A user-selected object must also be uniquely recoverable now.
        return locator if resolve(backend, window, locator, build) == target else None
    except (ValueError, RuntimeError, TypeError):
        return None
