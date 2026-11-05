"""Deterministic screenplay parser.

No LLM is involved here. Every timestamp the tool ever outputs is copied from a
block parsed by this module, so the model cannot invent a cut point.

Expected layout (Kuku screenplay export):

    EPISODE NUMBER: Episode 1
    EPISODE LOGLINE: ...
    CONTINUITY NOTES: ...
    --- SCRIPT START ---
    SCENE 01
    Scene heading: INT. CATHEDRAL - DAY
    Scene start: 00:00.00
    Scene end: 02:18.00
    **CAMERA:**
    Start: (00:00.00) | End: (00:04.80)
    Shot description...
    **LADY JULIANA (Confused, Breathless):**
    Start: (00:13.50) | End: (00:14.30)
    "Why?"
    --- SCRIPT END ---
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

TIME_RE = re.compile(r"^Start:\s*\(\s*([\d:.]+)\s*\)\s*\|\s*End:\s*\(\s*([\d:.]+)\s*\)\s*$")
HEADER_RE = re.compile(r"^\*\*(.+?):?\*\*\s*$")
SCENE_RE = re.compile(r"^SCENE\s+(\d+)\s*$", re.I)
SCENE_FIELD_RE = re.compile(r"^Scene (heading|start|end):\s*(.*)$", re.I)
META_RE = re.compile(r"^(EPISODE NUMBER|EPISODE LOGLINE|CONTINUITY NOTES):\s*(.*)$", re.I)

NON_DIALOGUE = {
    "CAMERA": "camera",
    "ACTION": "action",
    "SOUND FX": "sfx",
    "SFX": "sfx",
    "MUSIC": "music",
    "VFX": "vfx",
    "TEXT": "text",
    "ON-SCREEN TEXT": "text",
    "TRANSITION": "transition",
}
DELIVERY_FLAGS = {"O.S.", "V.O.", "O.C.", "CONT'D", "CONT’D"}


def to_seconds(ts: str) -> float:
    """'01:23.45' -> 83.45 ; also accepts 'H:MM:SS.ss'."""
    total = 0.0
    for part in ts.strip().split(":"):
        total = total * 60 + float(part)
    return round(total, 3)


def fmt(sec: float) -> str:
    """83.45 -> '01:23.45' (same style as the screenplays)."""
    sec = max(0.0, sec)
    m, s = divmod(round(sec, 2), 60)
    return f"{int(m):02d}:{s:05.2f}"


@dataclass
class Block:
    id: str            # stable id, e.g. "E012-B034"
    ep: int
    idx: int           # position within the episode
    kind: str          # camera | action | sfx | dialogue | ...
    speaker: str | None
    tags: str          # emotion / delivery tags, e.g. "Cold, Commanding"
    text: str
    start: float
    end: float
    scene: int
    scene_heading: str

    @property
    def is_dialogue(self) -> bool:
        return self.kind == "dialogue"


@dataclass
class Episode:
    ep: int
    logline: str
    continuity: str
    duration: float
    source: str
    scenes: list[dict] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _split_label(label: str) -> tuple[str, str, str]:
    """'MATRIARCH (O.S.) (Firm)' -> ('MATRIARCH', 'dialogue', 'O.S.; Firm')."""
    label = label.strip().rstrip(":").strip()
    parens = re.findall(r"\(([^)]*)\)", label)
    name = re.sub(r"\s*\(.*$", "", label).strip()
    kind = NON_DIALOGUE.get(name.upper(), "dialogue")
    flags = [p.strip() for p in parens if p.strip().upper() in DELIVERY_FLAGS]
    emotions = [p.strip() for p in parens if p.strip().upper() not in DELIVERY_FLAGS]
    tags = "; ".join(flags + emotions)
    return name, kind, tags


def parse_episode(path: str | Path) -> Episode:
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()

    meta = {"EPISODE NUMBER": "", "EPISODE LOGLINE": "", "CONTINUITY NOTES": ""}
    scenes: list[dict] = []
    raw_blocks: list[dict] = []
    warnings: list[str] = []

    cur_scene = {"scene": 0, "heading": "", "start": None, "end": None}
    cur: dict | None = None  # block being filled

    def close_block():
        nonlocal cur
        if cur is None:
            return
        if cur.get("start") is None:
            warnings.append(f"line {cur['line']}: block '{cur['label']}' has no timing; skipped")
        else:
            cur["text"] = " ".join(t for t in cur["text"] if t).strip()
            raw_blocks.append(cur)
        cur = None

    for n, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        m = META_RE.match(line)
        if m and cur is None:
            meta[m.group(1).upper()] = m.group(2).strip()
            continue
        if line.startswith("--- SCRIPT"):
            close_block()
            continue
        m = SCENE_RE.match(line)
        if m:
            close_block()
            cur_scene = {"scene": int(m.group(1)), "heading": "", "start": None, "end": None}
            scenes.append(cur_scene)
            continue
        m = SCENE_FIELD_RE.match(line)
        if m and cur is None:
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "heading":
                cur_scene["heading"] = val
            else:
                try:
                    cur_scene[key] = to_seconds(val)
                except ValueError:
                    warnings.append(f"line {n}: bad scene {key} '{val}'")
            continue
        if line.lower().startswith("actions and dialogue"):
            continue
        m = HEADER_RE.match(line)
        if m:
            close_block()
            name, kind, tags = _split_label(m.group(1))
            cur = {
                "label": m.group(1), "speaker": None if kind != "dialogue" else name,
                "kind": kind, "tags": tags, "text": [], "start": None, "end": None,
                "scene": cur_scene["scene"], "scene_heading": cur_scene["heading"], "line": n,
            }
            continue
        m = TIME_RE.match(line)
        if m and cur is not None and cur["start"] is None:
            cur["start"], cur["end"] = to_seconds(m.group(1)), to_seconds(m.group(2))
            if cur["end"] < cur["start"]:
                warnings.append(f"line {n}: end before start; swapped")
                cur["start"], cur["end"] = cur["end"], cur["start"]
            continue
        if cur is not None:
            cur["text"].append(line)
        else:
            warnings.append(f"line {n}: stray text ignored: {line[:60]}")
    close_block()

    ep_num = _episode_number(meta["EPISODE NUMBER"], path.name)
    # Screenplay order is kept (it is the editor's reading order); ties broken by time.
    raw_blocks.sort(key=lambda b: (b["start"], b["line"]))
    blocks = [
        Block(
            id=f"E{ep_num:03d}-B{i:03d}", ep=ep_num, idx=i, kind=b["kind"], speaker=b["speaker"],
            tags=b["tags"], text=b["text"], start=b["start"], end=b["end"],
            scene=b["scene"], scene_heading=b["scene_heading"],
        )
        for i, b in enumerate(raw_blocks)
    ]
    ends = [b.end for b in blocks] + [s["end"] for s in scenes if s.get("end") is not None]
    duration = max(ends) if ends else 0.0
    if not blocks:
        warnings.append("no timed blocks found: is this a screenplay in the expected format?")
    return Episode(
        ep=ep_num, logline=meta["EPISODE LOGLINE"], continuity=meta["CONTINUITY NOTES"],
        duration=duration, source=path.name, scenes=scenes, blocks=blocks, warnings=warnings,
    )


def _episode_number(meta_value: str, filename: str) -> int:
    for text in (meta_value, filename):
        m = re.search(r"(\d+)", text or "")
        if m:
            return int(m.group(1))
    raise ValueError(f"cannot find an episode number in '{meta_value}' or '{filename}'")


def load_show(folder: str | Path) -> list[Episode]:
    folder = Path(folder)
    files = sorted(p for p in folder.glob("*.txt") if p.is_file())
    if not files:
        raise FileNotFoundError(f"no .txt screenplays in {folder}")
    episodes = [parse_episode(p) for p in files]
    episodes.sort(key=lambda e: e.ep)
    seen: dict[int, str] = {}
    for e in episodes:
        if e.ep in seen:
            raise ValueError(f"episode {e.ep} appears twice: {seen[e.ep]} and {e.source}")
        seen[e.ep] = e.source
    return episodes


def speaker_stats(episodes: list[Episode]) -> dict[str, dict]:
    """Every dialogue label with the episodes it appears in and a sample line."""
    stats: dict[str, dict] = {}
    for e in episodes:
        for b in e.blocks:
            if not b.is_dialogue:
                continue
            s = stats.setdefault(b.speaker, {"episodes": [], "lines": 0, "sample": b.text})
            if e.ep not in s["episodes"]:
                s["episodes"].append(e.ep)
            s["lines"] += 1
    return stats
