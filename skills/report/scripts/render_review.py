#!/usr/bin/env python3
"""render_review.py — turn a report data model (JSON) into ONE self-contained,
offline, emailable .html for review (report-v2).

The single-file inliner PATTERN is the same token-substitution one proven by
divorcio/common/deck_src/build.py: read the template, splice the CSS / JS /
Alpine / data blob into named placeholders, write one file. We keep our own
copy (theirs is coupled to that deck's variants/locale/fallback machinery) but
carry over its hard-won lesson: a single-file HTML fails SILENTLY if its markup
or its data is malformed, so we GATE the build — validate the JSON, assert every
placeholder was filled, and assert the seven page anchors are present.

Separation of concerns (req 2): data (json) / structure (template) / style (css)
/ behaviour (js) are four files; this script only assembles them.

Usage:
  render_review.py --data model.json [-o out.html] [--title T] [--linked]

  --linked   dev mode: keep <link>/<script src> references to the asset files
             instead of inlining, for fast edit-reload. Default = inline (the
             portable, single-file deliverable).
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, "..", "assets"))

TEMPLATE = os.path.join(ASSETS, "review.template.html")
CSS = os.path.join(ASSETS, "review.css")
JS = os.path.join(ASSETS, "review.js")
ALPINE = os.path.join(ASSETS, "alpine.min.js")

REQUIRED_PAGES = ["progress", "todos", "resources", "validation", "facts", "timeline", "data"]
PLACEHOLDERS = ["__TITLE__", "__REVIEW_CSS__", "__REPORT_DATA__", "__ALPINE_JS__", "__REVIEW_JS__"]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _escape_for_script(json_text):
    # Prevent a literal "</script>" (or an HTML comment opener) inside the JSON
    # from prematurely closing the <script id=report-data> block.
    return json_text.replace("</", "<\\/").replace("<!--", "<\\!--")


def build(model, title=None, linked=False):
    template = _read(TEMPLATE)
    css = _read(CSS)
    js = _read(JS)
    alpine = _read(ALPINE)

    title = title or model.get("meta", {}).get("title") or model.get("meta", {}).get("project") or "Report"
    data_json = json.dumps(model, ensure_ascii=False, separators=(",", ":"))

    if linked:
        # dev mode: reference the asset files (must sit next to the output).
        out = template.replace("<style>__REVIEW_CSS__</style>",
                               '<link rel="stylesheet" href="review.css">')
        out = out.replace("<script>__ALPINE_JS__</script>",
                          '<script src="alpine.min.js" defer></script>')
        out = out.replace("<script>__REVIEW_JS__</script>",
                          '<script src="review.js"></script>')
        out = out.replace("__TITLE__", title)
        out = out.replace("__REPORT_DATA__", _escape_for_script(data_json))
    else:
        out = template.replace("__REVIEW_CSS__", css)
        # Alpine must be defined before review.js runs and before alpine:init;
        # inline it verbatim in the __ALPINE_JS__ slot (template loads it first).
        out = out.replace("__ALPINE_JS__", alpine)
        out = out.replace("__REVIEW_JS__", js)
        out = out.replace("__TITLE__", title)
        out = out.replace("__REPORT_DATA__", _escape_for_script(data_json))

    _gate(out, linked)
    return out


def _gate(html, linked):
    """Fail the build loudly rather than ship a page that renders blank."""
    # 1. every placeholder consumed
    leftover = [p for p in PLACEHOLDERS if p in html]
    if leftover:
        raise SystemExit(f"render ABORTED — unfilled placeholders: {leftover}")
    # 2. all seven page anchors present (sidebar <-> page contract)
    missing = [p for p in REQUIRED_PAGES if f"route==='{p}'" not in html]
    if missing:
        raise SystemExit(f"render ABORTED — missing page anchors: {missing}")
    # 3. in inline mode Alpine must actually be embedded (offline guarantee)
    if not linked and "x-data" in html and "Alpine" not in html:
        raise SystemExit("render ABORTED — Alpine not inlined; output would need the network")


def validate_model(model):
    """Light schema sanity — enough to catch author mistakes, not a straitjacket."""
    if not isinstance(model, dict):
        raise SystemExit("data model must be a JSON object")
    meta = model.get("meta")
    if not isinstance(meta, dict) or "project" not in meta:
        raise SystemExit("data model.meta.project is required")
    meta.setdefault("title", meta["project"] + " — status report")
    meta.setdefault("generated_at", "")
    for t in model.get("tasks", []):
        if "status" in t and t["status"] not in STATUS_OK:
            raise SystemExit(f"task {t.get('id')} has invalid status {t['status']!r} "
                             f"(allowed: {sorted(STATUS_OK)})")
    return model


STATUS_OK = {"done", "in-progress", "todo", "blocked"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a report data model into one self-contained review .html")
    ap.add_argument("--data", required=True, help="path to the report data model (JSON)")
    ap.add_argument("-o", "--output", help="output .html path (default: <slug>.html next to --data)")
    ap.add_argument("--title", help="override <title>/topbar heading")
    ap.add_argument("--linked", action="store_true", help="dev mode: link assets instead of inlining")
    args = ap.parse_args(argv)

    with open(args.data, encoding="utf-8") as f:
        model = json.load(f)
    validate_model(model)

    out_path = args.output
    if not out_path:
        slug = model.get("meta", {}).get("slug") or "report"
        out_path = os.path.join(os.path.dirname(os.path.abspath(args.data)), slug + ".html")

    html = build(model, title=args.title, linked=args.linked)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {out_path} ({len(html):,} bytes, {'linked' if args.linked else 'self-contained'})")
    return out_path


if __name__ == "__main__":
    main()
