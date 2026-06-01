"""YAML demo script loader + validation.

Schema (see demos/wikipedia.demo.yaml for an example):

    title: ...
    output: out/demo.mp4            # path relative to the yaml file
    viewport: {width: 1280, height: 720}
    tts:
      engine: say                   # say | openai | elevenlabs
      voice: Alex                   # engine-specific
      rate: 185
    steps:
      - say: "Welcome..."
        do: intro
      - say: "Open the app"
        do: goto
        url: https://example.com
      - say: "Point at the title"
        do: move
        to: h1
      - say: "Scroll down"
        do: scroll
        y: 800
      - say: "Hover on first visible heading below the fold"
        do: move_first_visible_h2
      - say: "Pause"
        do: wait
        ms: 500
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .tts import Voice

VALID_ACTIONS = {"intro", "goto", "move", "reveal", "scroll", "wait",
                 "move_first_visible_h2"}


@dataclass
class Step:
    say: str
    do: str
    url: Optional[str] = None
    to: Optional[str] = None
    y: Optional[int] = None
    ms: Optional[int] = None


@dataclass
class DemoScript:
    path: Path                        # source yaml path
    title: str
    output: Path                      # absolute path for the final mp4
    viewport: Tuple[int, int]
    voice: Voice
    steps: List[Step] = field(default_factory=list)

    @property
    def out_dir(self) -> Path:
        return self.output.parent


def load(path: Path) -> DemoScript:
    path = path.resolve()
    with path.open() as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    title = str(raw.get("title", path.stem))

    out_raw = raw.get("output", "out/demo.mp4")
    output = (path.parent / out_raw).resolve()

    vp = raw.get("viewport") or {}
    viewport = (int(vp.get("width", 1280)), int(vp.get("height", 720)))

    tts_raw = raw.get("tts") or {}
    voice = Voice(
        engine=str(tts_raw.get("engine", "say")),
        name=tts_raw.get("voice"),
        rate=int(tts_raw.get("rate", 185)),
        instructions=tts_raw.get("instructions"),
    )

    steps_raw = raw.get("steps") or []
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError(f"{path}: 'steps' must be a non-empty list")

    steps: List[Step] = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict):
            raise ValueError(f"{path}: step {i} must be a mapping")
        if "say" not in s or "do" not in s:
            raise ValueError(f"{path}: step {i} must have 'say' and 'do'")
        do = s["do"]
        if do not in VALID_ACTIONS:
            raise ValueError(
                f"{path}: step {i} has unknown action '{do}'. "
                f"Valid: {sorted(VALID_ACTIONS)}"
            )
        if do == "goto" and not s.get("url"):
            raise ValueError(f"{path}: step {i} ('goto') needs a 'url'")
        if do == "move" and not s.get("to"):
            raise ValueError(f"{path}: step {i} ('move') needs a 'to' selector")
        if do == "reveal" and not s.get("to"):
            raise ValueError(f"{path}: step {i} ('reveal') needs a 'to' selector")
        if do == "scroll" and "y" not in s:
            raise ValueError(f"{path}: step {i} ('scroll') needs 'y' (pixels)")
        steps.append(Step(
            say=str(s["say"]),
            do=do,
            url=s.get("url"),
            to=s.get("to"),
            y=int(s["y"]) if "y" in s else None,
            ms=int(s["ms"]) if "ms" in s else None,
        ))

    return DemoScript(
        path=path, title=title, output=output,
        viewport=viewport, voice=voice, steps=steps,
    )


STARTER_YAML = """\
title: {title}
output: out/{slug}.mp4
viewport: {{width: 1280, height: 720}}
tts:
  engine: say
  voice: Alex
  rate: 185

steps:
  - say: >-
      Welcome to {title}. This is a short walkthrough.
    do: intro

  - say: First we open the page.
    do: goto
    url: https://example.com

  - say: Point at the heading.
    do: move
    to: h1

  - say: That concludes the demo.
    do: wait
    ms: 500
"""


def scaffold(path: Path, title: str) -> None:
    slug = path.stem.replace(".demo", "")
    path.write_text(STARTER_YAML.format(title=title, slug=slug))
