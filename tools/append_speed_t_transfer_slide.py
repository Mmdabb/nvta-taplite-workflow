"""Append the tested speed/T transfer policy slide to the results deck."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


PAGE_WIDTH = 959.976
PAGE_HEIGHT = 540.0
NAVY = (20 / 255, 40 / 255, 66 / 255)
BLUE = (30 / 255, 110 / 255, 170 / 255)
TEAL = (31 / 255, 143 / 255, 131 / 255)
ORANGE = (226 / 255, 139 / 255, 47 / 255)
PALE_BLUE = (235 / 255, 244 / 255, 250 / 255)
PALE_ORANGE = (253 / 255, 244 / 255, 231 / 255)
GRAY = (92 / 255, 104 / 255, 116 / 255)
LIGHT_GRAY = (229 / 255, 233 / 255, 237 / 255)


def _set_color(pdf: canvas.Canvas, color: tuple[float, float, float]) -> None:
    pdf.setFillColorRGB(*color)


def _text(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    value: str,
    *,
    size: float = 12,
    color: tuple[float, float, float] = NAVY,
    font: str = "Helvetica",
) -> None:
    _set_color(pdf, color)
    pdf.setFont(font, size)
    pdf.drawString(x, y, value)


def _centered(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    value: str,
    *,
    size: float = 12,
    color: tuple[float, float, float] = NAVY,
    font: str = "Helvetica",
) -> None:
    _set_color(pdf, color)
    pdf.setFont(font, size)
    pdf.drawCentredString(x + width / 2, y, value)


def _rounded_box(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    fill: tuple[float, float, float],
    *,
    radius: float = 9,
) -> None:
    _set_color(pdf, fill)
    pdf.roundRect(x, y, width, height, radius, stroke=0, fill=1)


def _draw_slide(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.setTitle("NVTA 2050 speed and congestion-time transfer update")

    _text(
        pdf,
        54,
        486,
        "Speed and congestion-time transfer is deliberately narrower",
        size=25,
        font="Helvetica-Bold",
    )
    _text(
        pdf,
        54,
        461,
        "Only defensible base-link lineage carries speed anchors and T0/T2/T3 into 2050",
        size=12.5,
        color=GRAY,
    )

    stages = (
        ("Exact A-B", "49,234", TEAL),
        ("Replacement path", "4", TEAL),
        ("High-confidence geometry", "33", TEAL),
        ("Corridor", "3", ORANGE),
        ("Link-type fallback", "388", ORANGE),
    )
    x = 54.0
    y = 354.0
    gap = 11.0
    box_width = 158.0
    box_height = 70.0
    for index, (label, count, color) in enumerate(stages):
        fill = PALE_BLUE if index < 3 else PALE_ORANGE
        _rounded_box(pdf, x, y, box_width, box_height, fill)
        _text(pdf, x + 13, y + 44, label, size=11, font="Helvetica-Bold")
        _text(pdf, x + 13, y + 18, count, size=19, color=color, font="Helvetica-Bold")
        if index < len(stages) - 1:
            _set_color(pdf, LIGHT_GRAY)
            pdf.setLineWidth(2)
            pdf.line(x + box_width + 2, y + box_height / 2, x + box_width + gap - 2, y + box_height / 2)
        x += box_width + gap

    _set_color(pdf, ORANGE)
    pdf.setLineWidth(3)
    stop_x = 54 + 3 * (box_width + gap) - gap / 2
    pdf.line(stop_x, y - 6, stop_x, y + box_height + 6)
    _centered(
        pdf,
        stop_x - 63,
        y - 24,
        126,
        "lineage boundary",
        size=9.5,
        color=ORANGE,
        font="Helvetica-Bold",
    )

    _rounded_box(pdf, 54, 232, 507, 94, PALE_BLUE)
    _text(pdf, 72, 301, "TRANSFERRED", size=10, color=TEAL, font="Helvetica-Bold")
    _text(pdf, 72, 276, "49,271 future pairs receive speed anchors", size=17, font="Helvetica-Bold")
    _text(pdf, 72, 252, "T triplets transfer only when the base pair has a complete accepted triplet", size=11, color=GRAY)

    _rounded_box(pdf, 579, 232, 327, 94, PALE_ORANGE)
    _text(pdf, 597, 301, "LEFT BLANK BY POLICY", size=10, color=ORANGE, font="Helvetica-Bold")
    _text(pdf, 597, 276, "391 corridor/fallback pairs", size=17, font="Helvetica-Bold")
    _text(pdf, 597, 252, "No corridor or link-type inference for speed or T values", size=10.5, color=GRAY)

    _text(pdf, 54, 196, "2050 period-output validation", size=13, font="Helvetica-Bold")
    headers = ("AM", "MD", "PM")
    speed_values = ("49,264", "49,271", "49,264")
    triplet_values = ("42,627", "42,043", "43,977")
    table_x = (286, 392, 498)
    _text(pdf, 72, 167, "Converted links with both speed anchors", size=10.5, color=GRAY)
    _text(pdf, 72, 139, "Converted open links with complete T triplets", size=10.5, color=GRAY)
    for x_pos, header, speed, triplet in zip(table_x, headers, speed_values, triplet_values):
        _centered(pdf, x_pos, 194, 82, header, size=10, color=BLUE, font="Helvetica-Bold")
        _centered(pdf, x_pos, 165, 82, speed, size=13, font="Helvetica-Bold")
        _centered(pdf, x_pos, 137, 82, triplet, size=13, font="Helvetica-Bold")

    _set_color(pdf, LIGHT_GRAY)
    pdf.setLineWidth(1)
    pdf.line(54, 119, 906, 119)
    _text(pdf, 54, 94, "Separate, scenario-local artifacts", size=12, font="Helvetica-Bold")
    _text(pdf, 54, 72, "future_qvdf_node_pair_overrides.npy", size=10.5, color=BLUE, font="Helvetica-Bold")
    _text(pdf, 292, 72, "QVDF only", size=10.5, color=GRAY)
    _text(pdf, 410, 72, "future_speed_t_overrides.npy", size=10.5, color=TEAL, font="Helvetica-Bold")
    _text(pdf, 623, 72, "speed anchors + T values", size=10.5, color=GRAY)
    _text(pdf, 54, 49, "Packaged 2025 dictionaries remain read-only; SHA-256 checks passed after the 2050 run.", size=10, color=GRAY)

    _text(pdf, 54, 20, "NVTA 2025 to 2050 attribute transfer test", size=8.5, color=GRAY)
    _text(pdf, 892, 20, "12", size=8.5, color=GRAY)
    pdf.showPage()
    pdf.save()


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(60):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(0.25)


def append_slide(input_pdf: Path, output_pdf: Path) -> None:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_pdf.parent) as temporary_directory:
        temporary_root = Path(temporary_directory)
        slide_path = temporary_root / "speed_t_update.pdf"
        combined_path = temporary_root / "combined.pdf"
        _draw_slide(slide_path)

        source = PdfReader(BytesIO(input_pdf.read_bytes()))
        slide = PdfReader(BytesIO(slide_path.read_bytes()))
        writer = PdfWriter()
        for page in source.pages:
            writer.add_page(page)
        writer.add_page(slide.pages[0])
        writer.add_metadata(
            {
                "/Title": "NVTA 2050 Future-Network Attribute Transfer Results",
                "/Author": "NVTA TAPLite workflow",
                "/Subject": "Tested QVDF, speed-anchor, and T0/T2/T3 transfer policy",
            }
        )
        with combined_path.open("wb") as stream:
            writer.write(stream)
        _replace_with_retry(combined_path, output_pdf)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: append_speed_t_transfer_slide.py INPUT_PDF OUTPUT_PDF")
    append_slide(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
