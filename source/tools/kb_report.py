"""Print knowledge-base statistics and a deduplicated reference list as Markdown.

    python tools/kb_report.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True

from stacksense.kb_loader import load_kb  # noqa: E402


def _sources(kb):
    seen = {}
    def add(items):
        for s in items or []:
            if isinstance(s, dict) and s.get("url"):
                seen.setdefault(s["url"], s.get("title") or s["url"])
    add(kb.references)
    add(kb.providers_list and [s for p in kb.providers_list for s in p.get("sources", [])])
    for entries in kb.layers.values():
        for e in entries:
            add(e.get("sources"))
            for o in e.get("offerings", []):
                add(o.get("sources"))
    for row in kb.compliance.values():
        add(row.get("sources"))
    add([s for d in kb.deprecated for s in d.get("sources", [])])
    add([s for d in kb.existing for s in d.get("sources", [])])
    return seen


def main() -> int:
    kb = load_kb()
    print("# Knowledge base report\n")
    print("| Layer | Entries | Offerings |")
    print("|---|---:|---:|")
    total_e = total_o = 0
    for layer in sorted(kb.layers):
        entries = kb.layers[layer]
        offs = sum(len(e.get("offerings", [])) for e in entries)
        total_e += len(entries)
        total_o += offs
        print(f"| {layer} | {len(entries)} | {offs} |")
    print(f"| **total** | **{total_e}** | **{total_o}** |\n")
    print(f"Providers: {len(kb.providers_list)}. Compliance rows: {len(kb.compliance)}. "
          f"Deprecated patterns: {len(kb.deprecated)}. Existing-system patterns: {len(kb.existing)}.\n")
    refs = _sources(kb)
    print(f"## References ({len(refs)})\n")
    for url, title in sorted(refs.items(), key=lambda kv: (kv[1].lower(), kv[0])):
        print(f"- {title}. <{url}>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
