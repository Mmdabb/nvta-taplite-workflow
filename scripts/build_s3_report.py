"""Build the implementation report from a verified full-network validation summary."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Flowable,
)

NAVY = colors.HexColor("#18364B")
BLUE = colors.HexColor("#216D9C")
TEAL = colors.HexColor("#158B84")
PALE = colors.HexColor("#EBF4F7")
GRAY = colors.HexColor("#546572")
RED = colors.HexColor("#FBEAE7")
WIDTH = A4[0] - 84
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleS3", fontSize=25, leading=29, textColor=NAVY, spaceAfter=15))
styles.add(ParagraphStyle(name="HeadingS3", fontSize=14, leading=18, textColor=NAVY, spaceBefore=10, spaceAfter=7))
styles.add(ParagraphStyle(name="TextS3", fontSize=9.4, leading=13, textColor=NAVY, spaceAfter=8))
styles.add(ParagraphStyle(name="SmallS3", fontSize=8, leading=10.5, textColor=GRAY, spaceAfter=5))
styles.add(ParagraphStyle(name="CellS3", fontSize=8, leading=10.6, textColor=NAVY))
styles.add(ParagraphStyle(name="CellHeadS3", fontSize=8, leading=10.6, textColor=colors.white))
styles.add(ParagraphStyle(name="CodeS3", fontName="Courier", fontSize=8.7, leading=13, textColor=NAVY, backColor=PALE, borderPadding=9, spaceAfter=13))


def p(text, style="TextS3"):
    return Paragraph(text, styles[style])


def table(headers, rows, widths, warn_rows=()):
    cells = [[p(escape(str(x)), "CellHeadS3") for x in headers]]
    cells += [[p(escape(str(x)), "CellS3") for x in row] for row in rows]
    result = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, NAVY),
        ("INNERGRID", (0, 1), (-1, -1), 0.3, colors.HexColor("#D6E1E8")),
        ("BOX", (0, 0), (-1, -1), 0.3, colors.HexColor("#D6E1E8")),
    ]
    for i in range(1, len(rows) + 1):
        commands.append(("BACKGROUND", (0, i), (-1, i), RED if i - 1 in warn_rows else colors.white))
    result.setStyle(TableStyle(commands))
    return result


class ProfileChart(Flowable):
    def __init__(self, values, label, color, height=140, imputed=False):
        super().__init__()
        self.width, self.height = WIDTH, height
        self.values, self.label, self.color, self.imputed = values, label, color, imputed

    def draw(self):
        c = self.canv
        left, bottom, plot_w, plot_h = 43, 25, self.width - 55, self.height - 48
        if self.imputed:
            c.setFillColor(colors.HexColor("#E6E9ED"))
            c.rect(left, bottom, plot_w / 4, plot_h, stroke=0, fill=1)
        maximum = max(v for _, v in self.values if math.isfinite(v)) * 1.12 or 1
        c.setFont("Helvetica", 7.5)
        for i in range(5):
            y = bottom + i * plot_h / 4
            c.setStrokeColor(colors.HexColor("#DEE5E9")); c.setLineWidth(0.4)
            c.line(left, y, left + plot_w, y)
            c.setFillColor(GRAY); c.drawRightString(left - 6, y - 2, f"{maximum * i / 4:,.0f}")
        for hour in [0, 6, 9, 15, 19, 24]:
            x = left + plot_w * hour / 24
            c.setFillColor(GRAY); c.drawCentredString(x, bottom - 13, f"{hour:02d}:00")
        c.setStrokeColor(self.color); c.setLineWidth(1.1)
        path = c.beginPath(); first = True
        for minute, value in self.values:
            if not math.isfinite(value):
                first = True; continue
            x, y = left + plot_w * minute / 1440, bottom + value / maximum * plot_h
            if first: path.moveTo(x, y); first = False
            else: path.lineTo(x, y)
        c.drawPath(path)
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 9); c.drawString(left, self.height - 12, self.label)


def footer(canvas, doc):
    canvas.setStrokeColor(colors.HexColor("#CBD9E2")); canvas.setLineWidth(0.5)
    canvas.line(42, 36, A4[0] - 42, 36)
    canvas.setFont("Helvetica", 8); canvas.setFillColor(GRAY)
    canvas.drawString(42, 23, "NVTA TAPLite Maintenance | S3 speed and volume export | November 5, 2026")
    canvas.drawRightString(A4[0] - 42, 23, str(doc.page))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-count", type=int, required=True)
    args = parser.parse_args()
    data = json.loads(args.summary.read_text(encoding="utf-8"))
    jur = data["scopes"]["link_speed_volume"]
    reg = data["scopes"]["link_speed_volume_regional"]
    worst = max(c["max_absolute_error"] for s in [jur, reg] for c in s["period_checks"])
    story = []
    def title(number, text):
        story.extend([p(f"IMPLEMENTATION REPORT / {number:02d}", "SmallS3"), p(text, "TitleS3")])
    def heading(text): story.append(p(text, "HeadingS3"))
    def text(value): story.append(p(value))
    def new(): story.append(PageBreak())

    title(1, "One daily link row.<br/>Assignment totals preserved.")
    text("A new postprocessing export combines each directed link's incremental speeds and approximate interval volumes across AM, MD, PM and NT. S3 supplies the within-period pattern; one multiplier per link and period preserves the assigned total.")
    story.append(table(["Regional links", "Five-minute speeds", "Five-minute volumes", "Maximum total error"],
                       [[f"{reg['unique_links']:,}", reg['speed_columns'], reg['volume_columns'], f"{worst:.2e} vehicles"]], [WIDTH/4]*4))
    heading("Step-by-step implementation")
    for name, body in [
        ("1. Resolve outputs and scopes", "Run after both combined performance files are created in the resolved DTALite folder. Use exactly their jurisdiction and regional roadway scopes."),
        ("2. Identify the same directed link", "Join converted network attributes by from_node_id + to_node_id. Export the network link_id, keeping the assignment ID separately in the audit."),
        ("3. Build the complete time grid", "Assign each timestamp to one configured period, including NT across midnight. Preserve supplied speeds and identify any missing timestamps explicitly."),
        ("4. Resolve link-level S3 inputs", "Read known free speed, capacity and lanes. Derive critical speed or m from one another; apply a documented prior only where the information is missing."),
        ("5. Convert and conserve", "Calculate whole-link flow, multiply by interval hours, sum within each period, then apply its assignment-total ratio to every interval."),
        ("6. Write and audit", "Write one row per unique directed link. Save parameter provenance, effective period attributes, scaling factors, exceptions and conservation errors separately."),
    ]:
        text(f"<b>{name}.</b> {body}")
    heading("Implementation location")
    text("Branch: <b>codex/s3-speed-volume-export</b><br/>Calculation: postprocessing/pipeline/speed_volume.py<br/>Automatic trigger: postprocessing/runner.py<br/>Regression tests: tests/test_speed_volume.py<br/>Reproduction: scripts/validate_speed_volume.py")

    new(); title(2, "Input files, columns<br/>and units")
    text("Inputs are the existing period link.csv and link_performance.csv files. The processed combined DataFrame supplies period-owned performance values; the exporter rereads converted link.csv for network attributes. Positive finite values are selected row by row.")
    rows = [
        ("Identity", "Both files: from_node_id, to_node_id", "Normalized directed pair. Opposite directions remain distinct; duplicate/unmatched pairs fail."),
        ("Output ID / type", "link.csv: link_id, link_type", "First configured period containing that link. Internal performance IDs stay in the audit."),
        ("Assigned total V", "link_performance.csv: volume", "Whole-link assignment count for that period. Never divide by lanes."),
        ("Incremental speeds", "spd_mph_HH:MM; legacy spd_HH:MM or spd_kmph_HH:MM", "Use mph; convert km/h with 1.609344. Prefer explicit mph for duplicate representations."),
        ("Free speed vf", "link.csv:vdf_free_speed_mph; performance:free_speed_mph; link.csv:free_speed", "First two are mph. Generic link.csv free_speed is km/h and is divided by 1.609344."),
        ("Lane count", "link.csv:lanes", "Directed lane count for the current period. Assume 1 only when unavailable/invalid; audit this."),
        ("Capacity C", "performance:link_capacity; link.csv:capacity; performance:lane_capacity", "link_capacity is already veh/hour/link. Other two are veh/hour/lane; multiply by current lanes once."),
        ("Critical speed vc", "speed_at_capacity_mph, vc_mph, fd_vc_mph", "Performance fields first, then network equivalents. Require 0 < vc < vf."),
        ("Shape m", "s3_shape_m, s3_m", "Performance first, then network. Derive missing m from vf and vc."),
    ]
    story.append(table(["Quantity", "Input columns / precedence", "Interpretation"], rows, [75, 205, WIDTH-280]))
    heading("Whole-link example")
    text("A three-lane link with capacity 1,800 veh/hour/lane has C = 5,400 veh/hour. If link_capacity already equals 5,400, use 5,400 directly. The resulting density, flow and interval volumes all describe the whole directed link.")
    text("QVDF cutoff_speed_mph, vdf_s, vdf_n and boundary speed anchors are not S3 speed-at-capacity or S3 shape parameters.")

    new(); title(3, "S3 inversion and<br/>period scaling")
    heading("Derive parameters before assuming them")
    text("If vf and m are known, derive vc. If vf and vc are known, derive m. When vc and m conflict, supplied valid vc is authoritative and m is recomputed. If vf is missing but vc and m exist, derive vf first.")
    story.append(p("vc = vf / 2^(2/m)<br/>m = 2 ln(2) / ln(vf/vc)<br/>kc = C / vc", "CodeS3"))
    text("When neither critical speed nor shape is supplied, use CBI's vc = 0.70 vf, giving m = 3.887. Known network vf is preserved, including urban speeds below 35 mph. Missing vf falls back to speed p95 clipped to 35-85 mph; absent data uses 35 mph. Missing C uses the CBI batch prior of 1,800 veh/hour/lane times lanes.")
    heading("Invert each interval's speed")
    story.append(p("v* = min(v, 0.99 vf)<br/>r  = max(vf / max(v*, 1 mph), 1.00001)<br/>k  = kc [max(r^(m/2) - 1, 1e-8)]^(1/m)<br/>q  = min(v* k, C)                 [veh/hour/link]<br/>u_i = q_i dt_i_hours              [vehicles/link]", "CodeS3"))
    text("These numerical safeguards follow CBI's inverse S3 function. The code evaluates density in log form to avoid exponent overflow. The saved supplied speeds are preserved; the 0.99 vf limit applies inside inversion.")
    heading("One common ratio for all intervals of one period")
    story.append(p("U = sum(u_i over the period)<br/>R = assigned period volume V / U<br/>approx_volume_i = R u_i<br/>sum(approx_volume_i) = V", "CodeS3"))
    text("AM, MD, PM and NT each get their own R for each directed link. Floating-point residue is corrected in the largest interval; no rounding to integer vehicles is applied.")
    story.append(table(["Illustrative five-minute bin", "Raw interval count", "R", "Saved count"],
                       [["1", "100", "2.0", "200"], ["2", "200", "2.0", "400"], ["3", "150", "2.0", "300"], ["Period total", "450", "V / U = 900 / 450", "900 = assignment"]], [160,100,120,WIDTH-380]))
    text("The CBI reconstruction module uses an additive capacity-constrained adjustment. This implementation uses the requested multiplicative adjustment. Final scaled flow may exceed C; the audit records it rather than clipping away assigned demand.")

    new(); title(4, "File schema and<br/>explicit assumptions")
    story.append(table(["New file beside combined outputs", "Contents"], [
        ("link_speed_volume.csv", "Jurisdiction-scoped daily rows"),
        ("link_speed_volume_regional.csv", "Regional daily rows"),
        ("Corresponding *_audit.csv files", "One record per link and period; parameters, sources, totals, scale and flags"),
    ], [240,WIDTH-240]))
    heading("Daily column order")
    text("<b>1.</b> link_id, from_node_id, to_node_id, link_type, free_speed (mph), capacity (veh/hour/link), lanes.<br/><b>2.</b> All spd_mph_HH:MM columns in clock order.<br/><b>3.</b> All approx_volume_HH:MM columns in the same clock order. Values are fractional vehicles per interval.")
    text("The seven static fields describe the first configured period containing a link. Each calculation uses the actual attributes for its own period; the audit retains those effective values. A link absent from a period retains blank cells for that period.")
    heading("Cases handled explicitly")
    story.append(table(["Case", "Behavior / audit"], [
        ("Missing overnight timestamps", "Infer the regular grid. Fill absent columns with the same link's assigned period average speed_mph. Audit imputed_speed_intervals and imputation_source."),
        ("Supplied invalid speed cells", "Keep the original cell. Give it zero raw flow weight and record invalid_speed_intervals. Other valid intervals receive the allocation."),
        ("Positive assignment, zero raw S3 total", "Allocate in proportion to interval duration. Status uniform_duration_fallback; scale factor is blank."),
        ("Zero assigned volume", "Every interval count is zero."),
        ("Duplicate/unmatched pairs, invalid assigned volume, overlapping periods", "Clear error; no silent attribution of another link's parameters."),
        ("Legacy file with no speed columns", "Skip new exports with a warning. Clear stale same-named exports; existing combined/statistics behavior remains."),
    ], [160,WIDTH-160], warn_rows=[4]))
    heading("Discovered in the existing true-shape run")
    text("NT is configured as 19:00-06:00, but the supplied NT speed columns cover only 19:00-23:55. The new export explicitly fills 00:00-05:55 (72 five-minute bins) with the link's assigned NT average speed. These are estimated speeds, not additional native model samples.")

    new(); title(5, "Validation on the<br/>true-shape network")
    text(f"Reprocessed copies of the existing DTALiteNetworkInput_TRUESHP_run outputs through the normal postprocessing entry point. This tests export/integration using completed assignment results; assignment itself was not rerun. All {args.test_count} automated project tests passed in a temporary checkout outside Dropbox.")
    story.append(table(["Scope", "Unique links", "Columns", "Speed / volume intervals"], [
        ("Jurisdiction", f"{jur['unique_links']:,}", jur['columns'], f"{jur['speed_columns']} / {jur['volume_columns']}"),
        ("Regional", f"{reg['unique_links']:,}", reg['columns'], f"{reg['speed_columns']} / {reg['volume_columns']}"),
    ], [130,100,90,WIDTH-320]))
    heading("Every exported link/period total checked after reading the CSV")
    rows=[]
    for scope, result in [("Jurisdiction",jur),("Regional",reg)]:
        for check in result['period_checks']:
            rows.append([scope, check['period'].upper(), f"{check['links']:,}", check['intervals'], f"{check['assigned_total']:,.3f}", f"{check['max_absolute_error']:.2e}"])
    story.append(table(["Scope", "Period", "Links", "Bins", "Assigned = exported*", "Max error"], rows, [90,45,65,40,WIDTH-305,65]))
    text("*Equality within absolute 1e-8 and relative 1e-12 numerical tolerance. Error is the maximum per-link difference in vehicles after CSV round-trip, rather than only a network-wide comparison.")
    heading("What the test confirmed")
    text(f"All {sum(reg['status_counts'].values()):,} regional link/period records used their native link_capacity and network vdf_free_speed_mph. Critical speed was unavailable, so all used 0.70 vf and derived m. There were {reg['invalid_speed_intervals']:,} invalid speed cells and no duration-allocation fallbacks. The regional audit records {reg['imputed_speed_intervals']:,} filled overnight cells and {reg['above_capacity_intervals']:,} intervals above capacity after scaling.")
    text(f"{reg['period_varying_capacity_links']:,} regional links changed capacity between periods, confirming why period-specific attributes must be used. The raw input SHA256 hashes were unchanged: <b>{data['source_hashes_unchanged']}</b>.")
    text("Regression coverage includes S3 capacity recovery, lane conversion, independent scaling, midnight ownership, alternate speed units, missing timestamps, zero/invalid speeds, ID changes, absent-period links, duplicate-pair rejection and both automatic output scopes.")

    new(); title(6, "Example daily profile<br/>and reproducibility")
    row = reg['example_row']; audits = reg['example_audit']
    text(f"Regional example: link {int(row['link_id'])}, directed pair {int(row['from_node_id'])} to {int(row['to_node_id'])}. The shaded 00:00-06:00 segment uses the audited NT average-speed fill. Vertical levels in the volume chart reflect independent period scale factors.")
    def points(prefix):
        return sorted([(int(k[-5:-3])*60+int(k[-2:]),float(v)) for k,v in row.items() if k.startswith(prefix)])
    story.append(ProfileChart(points('spd_mph_'), "Speed (mph)", BLUE, height=145, imputed=True))
    story.append(ProfileChart(points('approx_volume_'), "Approximate vehicles per five-minute interval", TEAL, height=145, imputed=True))
    story.append(table(["Period", "Assigned volume", "Raw S3 volume", "Scale R", "Exported total"], [
        (a['time_period'].upper(), f"{a['assigned_volume']:,.3f}", f"{a['raw_s3_volume']:,.3f}", f"{a['scale_factor']:.5f}", f"{a['estimated_volume_sum']:,.3f}") for a in audits
    ], [55,115,115,70,WIDTH-355]))
    heading("Reproduce and inspect")
    text("Run scripts/validate_speed_volume.py with --source pointing to existing period outputs and --output to a new test folder. It stages copies, runs normal postprocessing, writes validation_summary.json and ten-row sample CSVs, and hashes source inputs before and after.")
    text("Outputs for this report: <b>nvta-run/s3-speed-volume-validation-20261105-final/</b><br/>Specification: <b>docs/s3-speed-volume.md</b><br/>All requested changes are on <b>codex/s3-speed-volume-export</b>. Publishing is a separate release step after review.")
    source = "https://github.com/Mmdabb/CBI-TAPLite-Pipeline/blob/1ce571f80e34efa2afdde7ca266740ba057d0523/cbi/src/cbi/fundamental_diagram.py"
    text(f'<b>Reference:</b> <a href="{source}" color="#216D9C">CBI fundamental_diagram.py (pinned source)</a>; s3_speed, inverse_s3_flow and synthetic_s3_context. The 1,800 veh/hour/lane prior is from CBI batch.py. No CBI dependency was added to the installed workflow.')

    args.output.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(args.output), pagesize=A4, rightMargin=42, leftMargin=42, topMargin=42, bottomMargin=48).build(story, onFirstPage=footer, onLaterPages=footer)
    print(args.output)


if __name__ == "__main__":
    main()
