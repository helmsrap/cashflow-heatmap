#!/usr/bin/env python3
"""Build a heat map of the cumulative net flow into each holding, quarterly or monthly,
from an xlsx file.

The input sheet must have a header row with the columns: Date, Amount, To Holding,
From Holding. Each row moves `Amount` out of the `From Holding` and into the
`To Holding`. The net flow per period (in minus out) is added up period by period,
so each cell shows the running total for that holding up to the end of the period.
Any other columns, such as "To" and "From", are ignored.

By default only the most recent calendar quarter in the file is used. That quarter is
drawn as a From x To matrix of the amounts moved between holdings, with each holding's
net flow beside it (the running totals start at zero at the beginning of the quarter).
Use --all-data to use every row and show the running totals over time instead.

With many holdings, only the --top N (default 20) with the most money moving in and out
are shown; the rest are combined as "Other holdings". --table writes the full numbers
for every holding to an xlsx file.

Usage:
    python holding_heatmap.py flows.xlsx [-o holding_heatmap.png] [--all-data]
                              [--period quarterly|monthly] [--top N] [--table PATH.xlsx]
                              [--sheet NAME] [--no-labels]
"""

import argparse
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, ListedColormap, Normalize, TwoSlopeNorm
from matplotlib.ticker import FuncFormatter

# Shared parsing, colours and page size (also sets the Agg backend).
from cashflow_heatmap import (DIVERGING, DPI, INK, MUTED_INK, PAGE_SIZE, PERIODS, SURFACE,
                              compact, parse_amount, style_axes)

REQUIRED_COLUMNS = ["Date", "Amount", "To Holding", "From Holding"]
# Name used in the flow matrix for a blank To/From Holding (deposits and withdrawals).
EXTERNAL = "External"
# Amounts moved between holdings: one hue, light = little, dark = a lot. Green keeps it
# apart from the red/blue of the net-flow scale, which means out/in.
SEQUENTIAL = LinearSegmentedColormap.from_list("moved", ["#dff3ea", "#1baf7a", "#0a5c40"])
EMPTY_CELL = "#f0efec"  # a holding pair with no transfers
# Holdings beyond --top are combined under this name.
OTHER = "Other holdings"
DEFAULT_TOP = 20


def holding_order(names):
    """Sort holding names alphabetically, with OTHER and then EXTERNAL last."""
    names = set(names)
    order = sorted(names - {OTHER, EXTERNAL}, key=str.casefold)
    return order + [n for n in (OTHER, EXTERNAL) if n in names]


def gap_width(cell_pts):
    """Width (points) of the surface-coloured gap between cells: 2pt, thinner for
    small cells so the gaps never hide the colours."""
    return min(2.0, 0.08 * cell_pts)


def fold_holdings(df, top):
    """Keep the `top` holdings with the most money moving in and out; rename the rest
    to OTHER. Returns (rows, number of holdings combined). top=0 keeps them all."""
    legs = pd.concat([df["To Holding"], df["From Holding"]], ignore_index=True)
    amounts = pd.concat([df["Amount"].abs()] * 2, ignore_index=True)
    named = legs.notna() & (legs != "").fillna(False)
    activity = amounts[named].groupby(legs[named]).sum()
    if top <= 0 or len(activity) <= top:
        return df, 0
    keep = set(activity.nlargest(top).index)
    df = df.copy()
    for col in ("To Holding", "From Holding"):
        side = df[col]
        fold = (side.notna() & (side != "").fillna(False) & ~side.isin(keep)).astype(bool)
        df[col] = side.mask(fold, OTHER)
    return df, len(activity) - top


def compact_whole(value):
    """Like compact() but with no decimals: -366K instead of -365.7K."""
    a = abs(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= size:
            return f"{value / size:,.0f}{suffix}"
    return f"{value:,.0f}"


def label_cells(ax, values, limit, fontsize, fmt):
    """Write each non-zero value in bold inside its cell. NaN cells are left blank."""
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            v = values[i, j]
            if v == 0 or not np.isfinite(v):
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
    longest = max((len(fmt(v)) for v in values.flat if np.isfinite(v)), default=1)
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
    table = table.cumsum(axis=1)
    return table.reindex(holding_order(table.index))


def latest_quarter(df):
    """Keep only the rows in the most recent calendar quarter that has any data.

    Returns (filtered rows, that quarter as a pandas Period).
    """
    quarter = df["Date"].dt.to_period("Q")
    latest = quarter.max()
    return df[quarter == latest], latest


def flow_matrix(df):
    """Return (amounts, counts): one row per From Holding and one column per To Holding,
    with the total moved and the number of transfers (NaN where there were none).

    A blank holding becomes EXTERNAL. A negative amount is counted as a transfer the
    other way. Both axes list the same holdings, alphabetically, with
    OTHER and EXTERNAL last.
    """
    sides = {c: df[c].fillna("").replace("", EXTERNAL) for c in ("From Holding", "To Holding")}
    reverse = df["Amount"] < 0
    pairs = pd.DataFrame({
        "From": sides["From Holding"].where(~reverse, sides["To Holding"]),
        "To": sides["To Holding"].where(~reverse, sides["From Holding"]),
        "Amount": df["Amount"].abs(),
    })
    pairs = pairs[pairs["From"] != pairs["To"]]  # e.g. both sides blank

    grouped = pairs.groupby(["From", "To"])["Amount"]
    amounts, counts = grouped.sum().unstack(), grouped.size().unstack()
    order = holding_order(set(amounts.index) | set(amounts.columns))
    return (amounts.reindex(index=order, columns=order),
            counts.reindex(index=order, columns=order))


def label_matrix(ax, amounts, counts, norm):
    """Write the amount (bold) and, if there's room, the number of transfers in each cell."""
    fig = ax.figure
    bbox = ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted())
    cell_h = 72 * bbox.height / amounts.shape[0]
    fmt = compact
    fontsize = cell_fontsize(ax, amounts, fmt)
    # Two lines (amount, then a smaller count) need ~2.2 font heights.
    two_lines = min(fontsize, 0.9 * cell_h / 2.2)
    show_counts = two_lines >= 5
    if show_counts:
        fontsize = two_lines
    elif fontsize < 4.5:
        # Amounts only; if they still don't fit, try whole numbers (870K, not 870.4K).
        fmt = compact_whole
        fontsize = cell_fontsize(ax, amounts, fmt)
        if fontsize < 4.5:
            return
    # Offset of each line from the cell centre, in rows (1 row = 1 data unit).
    offset = 0.55 * fontsize / cell_h if show_counts else 0

    for i in range(amounts.shape[0]):
        for j in range(amounts.shape[1]):
            v = amounts[i, j]
            if not np.isfinite(v):
                continue
            color = "white" if norm(v) > 0.5 else INK
            ax.text(j + 0.5, i + 0.5 - offset, fmt(v), ha="center", va="center",
                    fontsize=fontsize, fontweight="bold", color=color)
            if show_counts:
                n = int(counts[i, j])
                ax.text(j + 0.5, i + 0.5 + offset, f"{n:,} transfer{'s' * (n != 1)}",
                        ha="center", va="center", fontsize=0.75 * fontsize,
                        color=color, alpha=0.85)


def plot_flow_matrix(amounts, counts, net, out_path, quarter, note, show_labels=True,
                     period="quarterly"):
    """Draw the From x To matrix for one quarter with the net-flow columns beside it.

    `net` has the same rows as `amounts` (NaN for EXTERNAL) and one column per period.
    `note` starts the footer line.
    """
    names = list(amounts.index)
    n, n_periods = len(names), net.shape[1]
    values = amounts.to_numpy(dtype=float)
    count_values = counts.to_numpy(dtype=float)
    net_values = net.to_numpy(dtype=float)

    moved_norm = Normalize(vmin=0.0, vmax=np.nanmax(values))
    net_limit = np.nanmax(np.abs(net_values)) if np.isfinite(net_values).any() else 1.0
    net_norm = TwoSlopeNorm(vmin=-(net_limit or 1.0), vcenter=0.0, vmax=net_limit or 1.0)

    # Rough cell size (points) used to size the holding names before layout.
    row_pts = 72 * (PAGE_SIZE[1] - 3.6) / n
    name_fontsize = max(4, min(9, 0.7 * row_pts))
    gap = gap_width(row_pts)

    # Widths in inches: the net columns get enough room for their labels and colour bar.
    net_width = max(1.3, 0.9 * n_periods)
    matrix_width = PAGE_SIZE[0] - 2.4 - net_width
    fig = plt.figure(figsize=PAGE_SIZE, facecolor=SURFACE, layout="constrained")
    fig.get_layout_engine().set(w_pad=0.3, h_pad=0.3, wspace=0.03, hspace=0.02)
    grid = fig.add_gridspec(2, 2, width_ratios=[matrix_width, net_width],
                            height_ratios=[1, 0.02])
    ax = fig.add_subplot(grid[0, 0])
    ax_net = fig.add_subplot(grid[0, 1], sharey=ax)
    cax = fig.add_subplot(grid[1, 0])
    cax_net = fig.add_subplot(grid[1, 1])

    # Light gray behind every pair, except the diagonal (a holding to itself),
    # then the pairs that had transfers on top.
    background = np.where(np.eye(n, dtype=bool), np.nan, 0.0)
    ax.pcolormesh(np.ma.masked_invalid(background), cmap=ListedColormap([EMPTY_CELL]),
                  edgecolors=SURFACE, linewidth=gap)
    mesh = ax.pcolormesh(np.ma.masked_invalid(values), cmap=SEQUENTIAL, norm=moved_norm,
                         edgecolors=SURFACE, linewidth=gap)
    net_mesh = ax_net.pcolormesh(np.ma.masked_invalid(net_values), cmap=DIVERGING,
                                 norm=net_norm, edgecolors=SURFACE, linewidth=gap)

    for a in (ax, ax_net):
        a.set_facecolor(SURFACE)
        style_axes(a)
        a.xaxis.tick_top()
        a.xaxis.set_label_position("top")
    ax.invert_yaxis()  # shared with ax_net

    ax.set_yticks(np.arange(n) + 0.5)
    ax.set_yticklabels([s if len(s) <= 30 else s[:29] + "…" for s in names],
                       fontsize=name_fontsize)
    ax.set_xticks(np.arange(n) + 0.5)
    ax.set_xticklabels([s if len(s) <= 22 else s[:21] + "…" for s in names],
                       rotation=45, ha="left", fontsize=name_fontsize)
    ax.set_ylabel("From", color=MUTED_INK, fontsize=10, fontweight="bold")
    ax.set_xlabel("To", color=MUTED_INK, fontsize=10, fontweight="bold")

    _, label_format, _ = PERIODS[period]
    ax_net.tick_params(labelleft=False)
    ax_net.set_xticks(np.arange(n_periods) + 0.5)
    ax_net.set_xticklabels([p.strftime(label_format) for p in net.columns],
                           rotation=45, ha="left", fontsize=name_fontsize)
    ax_net.set_xlabel("Net flow", color=MUTED_INK, fontsize=10, fontweight="bold")

    for bar_ax, bar_mesh, label, ticks in (
            (cax, mesh, f"Amount moved in {quarter.strftime(PERIODS['quarterly'][1])}", None),
            (cax_net, net_mesh, "Cumulative net  (− out / + in)",
             [net_norm.vmin, 0.0, net_norm.vmax])):
        cbar = fig.colorbar(bar_mesh, cax=bar_ax, orientation="horizontal", ticks=ticks)
        cbar.outline.set_visible(False)
        cbar.ax.tick_params(colors=MUTED_INK, labelsize=7, length=0)
        cbar.ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: compact(v)))
        cbar.set_label(label, color=MUTED_INK, fontsize=8)

    fig.suptitle(f"Flows Between Holdings, {quarter.strftime(PERIODS['quarterly'][1])}",
                 x=0.02, ha="left", color=INK, fontsize=13, fontweight="bold")
    note += ("\nRead across a row for where a holding's money went, "
            "down a column for where it came from.")
    if EXTERNAL in names:
        note += f" {EXTERNAL} = a blank To or From Holding."
    fig.supxlabel(note, x=0.02, ha="left", color=MUTED_INK, fontsize=8)

    fig.draw_without_rendering()  # run the layout so cell sizes are final

    if show_labels:
        label_matrix(ax, values, count_values, moved_norm)
        for fmt in (compact, compact_whole):
            fontsize = cell_fontsize(ax_net, net_values, fmt)
            if fontsize >= 4.5:
                label_cells(ax_net, net_values, net_limit, fontsize, fmt)
                break

    fig.savefig(out_path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def plot_heatmap(table, out_path, show_labels=True, period="quarterly", note=None):
    n_holdings, n_periods = table.shape
    values = table.to_numpy(dtype=float)

    limit = np.abs(values).max() or 1.0
    norm = TwoSlopeNorm(vmin=-limit, vcenter=0.0, vmax=limit)

    # Rough plot area (page minus margins, labels and colour bar) used to size
    # tick labels before layout; points per row / column.
    row_pts = 72 * (PAGE_SIZE[1] - 2.0) / n_holdings
    col_pts = 72 * (PAGE_SIZE[0] - 3.5) / n_periods
    y_fontsize = max(4, min(9, 0.7 * row_pts))
    gap = gap_width(min(row_pts, col_pts))
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
                         edgecolors=SURFACE, linewidth=gap)
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

    _, quarter_format, _ = PERIODS["quarterly"]
    first, last = table.columns[0].asfreq("Q"), table.columns[-1].asfreq("Q")
    span = first.strftime(quarter_format)
    if last != first:
        span += f" – {last.strftime(quarter_format)}"
    fig.suptitle(f"Cumulative Net Flow by Holding, {span}", x=0.02, ha="left",
                 color=INK, fontsize=13, fontweight="bold")
    if note:
        fig.supxlabel(note, x=0.02, ha="left", color=MUTED_INK, fontsize=8)

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


def write_table(df, path, freq, matrix=True):
    """Write the full numbers for every holding (nothing combined) to an xlsx file."""
    label_format = PERIODS["quarterly" if freq == "Q" else "monthly"][1]
    net = cumulative_net_flows(df, freq=freq)
    net.columns = [p.strftime(label_format) for p in net.columns]
    with pd.ExcelWriter(path) as writer:
        if matrix:
            amounts, counts = flow_matrix(df)
            amounts.rename_axis(index="From / To").to_excel(writer, sheet_name="Amount moved")
            counts.rename_axis(index="From / To").to_excel(writer, sheet_name="Transfers")
        net.rename_axis(index="Holding").to_excel(writer, sheet_name="Cumulative net flow")
    print(f"Saved full table to {path}")


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
    parser.add_argument("--all-data", action="store_true",
                        help="use every row and show running totals over time (default: "
                             "only the most recent quarter, as a From x To matrix)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, metavar="N",
                        help="show the N holdings with the most money moving in and out "
                             f"and combine the rest as \"{OTHER}\" (default: {DEFAULT_TOP}; "
                             "0 shows every holding)")
    parser.add_argument("--table", metavar="PATH.xlsx",
                        help="also write the full numbers for every holding to this xlsx "
                             "file (nothing combined)")
    parser.add_argument("--sheet", default=0,
                        help="sheet name or index to read (default: first sheet)")
    parser.add_argument("--no-labels", action="store_true",
                        help="don't print values inside the cells")
    args = parser.parse_args()

    sheet = int(args.sheet) if str(args.sheet).isdigit() else args.sheet
    df = load_flows(args.xlsx, sheet)
    if df.empty:
        sys.exit("No valid rows found.")
    if not args.all_data:
        df, quarter = latest_quarter(df)
        print(f"Using only {quarter.strftime(PERIODS['quarterly'][1])} "
              f"({len(df)} rows); pass --all-data to use every row.")

    freq = PERIODS[args.period][0]
    if args.table:
        write_table(df, args.table, freq, matrix=not args.all_data)
    shown, n_combined = fold_holdings(df, args.top)
    combined_note = ""
    if n_combined:
        combined_note = (f"Showing the {args.top} holdings with the most money moved; the "
                         f"other {n_combined} are combined as {OTHER}.")
        print(f"{n_combined} holdings combined as \"{OTHER}\"; use --top to change how "
              f"many are shown, or --table for every holding.")

    if not args.all_data:
        amounts, counts = flow_matrix(shown)
        if amounts.empty:
            sys.exit("No To Holding or From Holding values found.")
        net = cumulative_net_flows(shown, freq=freq).reindex(amounts.index)
        note = f"{len(df):,} transfers, {compact(df['Amount'].abs().sum())} moved in total."
        if n_combined:
            note += f" {combined_note} Transfers between two of those aren't shown."
        plot_flow_matrix(amounts, counts, net, args.output, quarter, note,
                         show_labels=not args.no_labels, period=args.period)
        print(f"Saved flow matrix for {len(amounts)} holdings to {args.output}")
        return

    table = cumulative_net_flows(shown, freq=freq)
    if table.empty:
        sys.exit("No To Holding or From Holding values found.")
    plot_heatmap(table, args.output, show_labels=not args.no_labels, period=args.period,
                 note=combined_note)
    unit = "quarters" if args.period == "quarterly" else "months"
    print(f"Saved heat map for {table.shape[0]} holdings x {table.shape[1]} {unit} "
          f"to {args.output}")


if __name__ == "__main__":
    main()
