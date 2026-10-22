"""Shared millimetre cells for the legacy model-space title block."""

from datetime import date


def title_block_cells(width, *, title, project, design, check, sheet_no, ratio):
    """Keep the 180 x 56 block inside the inner frame, with one text per cell."""
    x, y = float(width) - 190.0, 10.0
    split, right = x + 120.0, x + 180.0
    rows = [(y + 36.0, y + 56.0), (y + 18.0, y + 36.0), (y, y + 18.0)]
    left_text = [title, f"工程:{project}" if project else "", f"设计:{design}  审核:{check}"]
    right_text = [f"图号 {sheet_no}", f"比例 {ratio}", f"日期 {date.today().isoformat()}"]
    cells = []
    for index, (bottom, top) in enumerate(rows):
        cells.append((left_text[index], [x + 2, bottom + 2, split - 2, top - 2], 5.0 if index == 0 else 3.5))
        cells.append((right_text[index], [split + 2, bottom + 2, right - 2, top - 2], 3.5))
    return [x, y, right, y + 56.0], split, [y + 18.0, y + 36.0], cells
