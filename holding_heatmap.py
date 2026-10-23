#!/usr/bin/env python3
"""Build a heat map of the cumulative net flow into each holding, quarterly or monthly,
from an xlsx file.

The input sheet must have a header row with the columns: Date, Amount, To Holding,
From Holding. Each row moves `Amount` out of the `From Holding` and into the
`To Holding`. The net flow per period (in minus out) is added up period by period,
so each cell shows the running total for that holding up to the end of the period.
Any other columns, such as "To" and "From", are ignored.

Usage:
    python holding_heatmap.py flows.xlsx [-o holding_heatmap.png]
                              [--period quarterly|monthly] [--sheet NAME] [--no-labels]
"""

import argparse
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import TwoSlopeNorm
from matplotlib.ticker import FuncFormatter

# Shared parsing, colours and page size (also sets the Agg backend).
from cashflow_heatmap import (DIVERGING, DPI, INK, MUTED_INK, PAGE_SIZE, PERIODS, SURFACE,
                              compact, parse_amount, style_axes)

REQUIRED_COLUMNS = ["Date", "Amount", "To Holding", "From Holding"]


def compact_whole(value):
    """Like compact() but with no decimals: -366K instead of -365.7K."""
    a = abs(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= size:
            return f"{value / size:,.0f}{suffix}"
    return f"{value:,.0f}"


def label_cells(ax, values, limit, fontsize, fmt):
    """Write each non-zero value in bold inside its cell."""
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            v = values[i, j]
            if v == 0:
                continue
            dark_cell = abs(v) > 0.4 * limit
            ax.text(j + 0.5, i + 0.5, fmt(v), ha="center", va="center",
                    fontsize=fontsize, fontweight="bold",
                    color="white" if dark_cell else INK)


def cell_fontsize(ax, values, fmt):
    """Largest font (max 9pt) at which every value, written with fmt, fits inside its cell."""
    fig = ax.figure
    bbox = ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted())
    n_rows, n_cols = values.shape
    cell_w = 72 * bbox.width / n_cols
    cell_h = 72 * bbox.height / n_rows
    longest = max(len(fmt(v)) for v in values.flat)
    # Bold digits are a little wider than regular ones.
    return min(9, 0.6 * cell_h, cell_w / (0.68 * longest + 1))


def load_flows(path, sheet=0):
    df = pd.read_excel(path, sheet_name=sheet, dtype={"Amount": object})
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        sys.exit(f"Missing required column(s): {', '.join(missing)}. "
                 f"Found: {', '.join(df.columns)}")

    # Keep only the columns we use; any others are ignored.
    df = df[REQUIRED_COLUMNS].copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    raw_amounts = df["Amount"].copy()
    df["Amount"] = raw_amounts.map(parse_amount)
    df["To Holding"] = df["To Holding"].astype("string").str.strip()
    df["From Holding"] = df["From Holding"].astype("string").str.strip()

    bad = df["Date"].isna() | df["Amount"].isna()
    if bad.any():
        print(f"Warning: skipping {bad.sum()} row(s) with an invalid date or amount:",
              file=sys.stderr)
        # +2 converts the 0-based index to the Excel row number (header is row 1).
        for idx in df.index[bad][:10]:
            print(f"  row {idx + 2}: Date={df.at[idx, 'Date']!s}, "
                  f"Amount={raw_amounts.at[idx]!r}", file=sys.stderr)
        if bad.sum() > 10:
            print(f"  ... and {bad.sum() - 10} more", file=sys.stderr)
        df = df[~bad]
    return df


def cumulative_net_flows(df, freq="Q"):
    """Return a DataFrame indexed by holding, one column per calendar quarter
    (freq="Q") or month (freq="M"), holding the running total of net flow."""
    period = df["Date"].dt.to_period(freq)
    inflows = pd.DataFrame({"Holding": df["To Holding"], "Period": period,
                            "Net": df["Amount"]})
    outflows = pd.DataFrame({"Holding": df["From Holding"], "Period": period,
                             "Net": -df["Amount"]})
    # A blank holding (e.g. an external deposit or withdrawal) only affects the other side.
    legs = pd.concat([inflows, outflows]).dropna(subset=["Holding"])
    legs = legs[legs["Holding"] != ""]

    table = legs.pivot_table(index="Holding", columns="Period", values="Net",
                             aggfunc="sum", fill_value=0.0)
    # Include every period in the range, so quiet periods carry the running total forward.
    all_periods = pd.period_range(period.min(), period.max(), freq=freq)
    table = table.reindex(columns=all_periods, fill_value=0.0)
    return table.cumsum(axis=1).sort_index(key=lambda names: names.str.casefold())


def plot_heatmap(table, out_path, show_labels=True, period="quarterly"):
    n_holdings, n_periods = table.shape
    values = table.to_numpy(dtype=float)

    limit = np.abs(values).max() or 1.0
    norm = TwoSlopeNorm(vmin=-limit, vcenter=0.0, vmax=limit)

    # Rough plot area (page minus margins, labels and colour bar) used to size
    # tick labels before layout; points per row / column.
    row_pts = 72 * (PAGE_SIZE[1] - 2.0) / n_holdings
    col_pts = 72 * (PAGE_SIZE[0] - 3.5) / n_periods
    y_fontsize = max(4, min(9, 0.7 * row_pts))
    x_fontsize = max(4, min(9, 0.9 * col_pts))
    # With many months, label every n-th one so the dates don't overlap.
    x_step = max(1, int(np.ceil(x_fontsize * 1.6 / col_pts)))

    fig = plt.figure(figsize=PAGE_SIZE, facecolor=SURFACE, layout="constrained")
    # ~0.3in margin all round for printing.
    fig.get_layout_engine().set(w_pad=0.3, h_pad=0.3, wspace=0.01)
    grid = fig.add_gridspec(1, 2, width_ratios=[PAGE_SIZE[0] - 3.5, 0.18])
    ax = fig.add_subplot(grid[0, 0])
    cax = fig.add_subplot(grid[0, 1])

    # edgecolor = surface gives a thin gap between cells.
    mesh = ax.pcolormesh(values, cmap=DIVERGING, norm=norm,
                         edgecolors=SURFACE, linewidth=2)
    ax.set_facecolor(SURFACE)
    ax.invert_yaxis()
    style_axes(ax)

    ax.set_yticks(np.arange(n_holdings) + 0.5)
    # Truncate very long holding names so they don't squeeze the plot.
    ax.set_yticklabels([n if len(n) <= 40 else n[:39] + "…" for n in table.index],
                       fontsize=y_fontsize)

    _, label_format, period_name = PERIODS[period]
    ax.set_xticks(np.arange(0, n_periods, x_step) + 0.5)
    ax.set_xticklabels([p.strftime(label_format) for p in table.columns[::x_step]],
                       rotation=45, ha="right", fontsize=x_fontsize)

    cbar = fig.colorbar(mesh, cax=cax)
    cbar.outline.set_visible(False)
    cbar.ax.tick_params(colors=MUTED_INK, labelsize=8, length=0)
    cbar.ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: compact(v)))
    cbar.set_label(f"Cumulative net flow, {period_name.lower()}  (− out  /  + in)",
                   color=MUTED_INK, fontsize=9)

    fig.suptitle("Cumulative Net Flow by Holding", x=0.02, ha="left",
                 color=INK, fontsize=13, fontweight="bold")

    fig.draw_without_rendering()  # run the layout so cell sizes are final

    # Cell values, sized to the actual cell size; skipped when cells are too small.
    # If the one-decimal labels don't fit (e.g. many months), try whole numbers.
    if show_labels:
        for fmt in (compact, compact_whole):
            fontsize = cell_fontsize(ax, values, fmt)
            if fontsize >= 4.5:
                label_cells(ax, values, limit, fontsize, fmt)
                break

    fig.savefig(out_path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("xlsx", help="input .xlsx file with Date, Amount, To Holding, "
                                     "From Holding columns")
    parser.add_argument("-o", "--output", default="holding_heatmap.png",
                        help="output PNG path (default: holding_heatmap.png)")
    parser.add_argument("--period", choices=PERIODS, default="quarterly",
                        help="show the running total per calendar quarter or month "
                             "(default: quarterly)")
    parser.add_argument("--sheet", default=0,
                        help="sheet name or index to read (default: first sheet)")
    parser.add_argument("--no-labels", action="store_true",
                        help="don't print values inside the cells")
    args = parser.parse_args()

    sheet = int(args.sheet) if str(args.sheet).isdigit() else args.sheet
    df = load_flows(args.xlsx, sheet)
    if df.empty:
        sys.exit("No valid rows found.")

    table = cumulative_net_flows(df, freq=PERIODS[args.period][0])
    if table.empty:
        sys.exit("No To Holding or From Holding values found.")
    plot_heatmap(table, args.output, show_labels=not args.no_labels, period=args.period)
    unit = "quarters" if args.period == "quarterly" else "months"
    print(f"Saved heat map for {table.shape[0]} holdings x {table.shape[1]} {unit} "
          f"to {args.output}")


if __name__ == "__main__":
    main()
