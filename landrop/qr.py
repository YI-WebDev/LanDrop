"""ターミナルとPCの画面に表示するQRコード。"""

from __future__ import annotations

import qrcode
from qrcode.constants import ERROR_CORRECT_L

# 縦2マスを1文字で表す。キーは（上が暗い, 下が暗い）で、明るいマスを文字で塗る
_BLOCKS = {(False, False): "█", (False, True): "▀", (True, False): "▄", (True, True): " "}
# 端末の配色に左右されないよう、白い文字・黒い背景に固定する
_WHITE_ON_BLACK = "\x1b[97;40m"
_RESET = "\x1b[0m"


def _matrix(data: str) -> list[list[bool]]:
    """暗いマスを True とした行列（周りの余白2マスを含む）。"""
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_L, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def render_qr(data: str, color: bool) -> str:
    """QRコードを文字列として描画する。color=False のときは端末の文字色で塗る。"""
    matrix = _matrix(data)
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    lines = []
    for top, bottom in zip(matrix[0::2], matrix[1::2]):
        line = "".join(_BLOCKS[(t, b)] for t, b in zip(top, bottom))
        lines.append(f"{_WHITE_ON_BLACK}{line}{_RESET}" if color else line)
    return "\n".join(lines)


def render_qr_svg(data: str) -> str:
    """PCの画面に表示するQRコード。ダークモードでも読めるよう、白い背景を付ける。"""
    matrix = _matrix(data)
    size = len(matrix)
    dark = "".join(f"M{x},{y}h1v1h-1z" for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/><path d="{dark}" fill="#000"/></svg>'
    )
