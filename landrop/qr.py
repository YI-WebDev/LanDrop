from __future__ import annotations

import qrcode
from qrcode.constants import ERROR_CORRECT_L

_HALF_BLOCK_BY_LIGHT_TOP_AND_BOTTOM = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}
_WHITE_ON_BLACK = "\x1b[97;40m"
_RESET = "\x1b[0m"


def _matrix(data: str) -> list[list[bool]]:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_L, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def render_qr(data: str, color: bool) -> str:
    matrix = _matrix(data)
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    lines = []
    for dark_top_row, dark_bottom_row in zip(matrix[0::2], matrix[1::2]):
        line = "".join(
            _HALF_BLOCK_BY_LIGHT_TOP_AND_BOTTOM[(not dark_top, not dark_bottom)]
            for dark_top, dark_bottom in zip(dark_top_row, dark_bottom_row)
        )
        lines.append(f"{_WHITE_ON_BLACK}{line}{_RESET}" if color else line)
    return "\n".join(lines)


def render_qr_svg(data: str) -> str:
    matrix = _matrix(data)
    size = len(matrix)
    dark = "".join(f"M{x},{y}h1v1h-1z" for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell)
    white_background = f'<rect width="{size}" height="{size}" fill="#fff"/>'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
        f'{white_background}<path d="{dark}" fill="#000"/></svg>'
    )
