"""Render rows as a terminal table, CSV (Excel-friendly) or JSON."""
import csv
import io
import json

MAX_CELL = 60


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4g}" if abs(value) < 1 else f"{value:,.2f}".replace(",", " ")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render(rows: list[dict], columns: list[str], fmt: str = "table") -> str:
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=1)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: _cell(r.get(c)) for c in columns})
        return buf.getvalue()
    if not rows:
        return "(nothing found)"
    cells = [[_cell(r.get(c)) for c in columns] for r in rows]
    cells = [[v if len(v) <= MAX_CELL else v[: MAX_CELL - 1] + "…" for v in row] for row in cells]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([line(columns), line(["-" * w for w in widths]), *(line(row) for row in cells)])
