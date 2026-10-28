"""Shape aria snapshots for agents: changes only, interactive elements only, element boxes."""

import difflib
import re

INTERACTIVE = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox",
    "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider", "spinbutton",
    "treeitem", "option",
})

_LINE = re.compile(r'^(?P<indent>\s*)- (?P<role>[\w-]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_REF = re.compile(r"\[ref=([^\]]+)\]")
_BOX = re.compile(r" ?\[box=(-?\d+),(-?\d+),(\d+),(\d+)\]")
_NOISE = re.compile(r" \[(?:active|box=[^\]]*)\]")
_POINTER = " [cursor=pointer]"


def _parse(line):
    m = _LINE.match(line)
    if not m:
        return None
    ref = _REF.search(m["rest"])
    return {"indent": len(m["indent"]), "role": m["role"], "name": m["name"] or "",
            "ref": ref.group(1) if ref else None, "rest": m["rest"]}


def _actionable(p):
    return p and p["ref"] and (p["role"] in INTERACTIVE or "[cursor=pointer]" in p["rest"])


def interactive(snapshot):
    """Flat list of elements an agent can act on, with headings for orientation."""
    lines = snapshot.splitlines()
    out = []
    near = ""
    for i, line in enumerate(lines):
        p = _parse(line)
        if not p:
            continue
        if p["ref"] and p["role"] == "heading":
            out.append(_NOISE.sub("", line.strip()))
        elif _actionable(p):
            entry = _NOISE.sub("", line.strip()).rstrip(":")
            if p["role"] in INTERACTIVE:
                entry = entry.replace(_POINTER, "")
            if not p["name"] and "]: " not in p["rest"] and near:
                entry += f' near "{near}"'
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if p["role"] == "link" and nxt.startswith("- /url:"):
                entry += " -> " + nxt[len("- /url:"):].strip()
            out.append(entry)
        text = p["name"] or (p["rest"].rsplit("]: ", 1)[1] if "]: " in p["rest"] else "")
        if p["role"] == "text":
            text = line.strip()[len("- text:"):].strip()
        if text and p["role"] not in INTERACTIVE:
            near = text[:60]
    return "\n".join(out)


def diff(old, new):
    """Changed lines between two snapshots of the same page; '' when nothing changed."""
    a = [_NOISE.sub("", l) for l in old.splitlines()]
    b = [_NOISE.sub("", l) for l in new.splitlines()]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        parent = _parent(b, j1) if j1 < len(b) else None
        if parent is not None and (not out or out[-1] != "  " + _bare(b[parent])):
            out.append("  " + _bare(b[parent]))
        out.extend("- " + _bare(l) for l in a[i1:i2])
        out.extend("+ " + _bare(l) for l in b[j1:j2])
    return "\n".join(out)


def _bare(line):
    line = line.strip()
    return line[2:] if line.startswith("- ") else line


def _parent(lines, j):
    depth = len(lines[j]) - len(lines[j].lstrip())
    for k in range(j - 1, -1, -1):
        d = len(lines[k]) - len(lines[k].lstrip())
        if d < depth and _REF.search(lines[k]):
            return k
    return None


def boxes(snapshot):
    """Actionable elements with viewport boxes; iframe children are offset by their frame."""
    out, frames = [], []
    for line in snapshot.splitlines():
        p = _parse(line)
        if not p:
            continue
        while frames and frames[-1][0] >= p["indent"]:
            frames.pop()
        box = _BOX.search(p["rest"])
        if not box:
            continue
        x, y, w, h = (int(v) for v in box.groups())
        ox = sum(f[1] for f in frames)
        oy = sum(f[2] for f in frames)
        if p["role"] == "iframe":
            frames.append((p["indent"], ox + x, oy + y))
        elif _actionable(p) and w > 0 and h > 0:
            out.append({"ref": p["ref"], "role": p["role"], "name": p["name"],
                        "x": ox + x, "y": oy + y, "w": w, "h": h})
    return out
