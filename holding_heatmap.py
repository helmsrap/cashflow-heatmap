#!/usr/bin/env python3
"""Build a heat map of the money moved between holdings from an xlsx file.

The input sheet must have a header row with the columns: Date, Amount, To Holding,
From Holding. Each row moves `Amount` out of the `From Holding` and into the
`To Holding`. Any other columns, such as "To" and "From", are ignored.

The heat map is a From x To matrix: each cell is the total moved from one holding to
another, with the number of transfers. Beside it, each holding's net flow (in minus out).
By default only the most recent calendar quarter in the file is summed; --all-data sums
every row.

With many holdings, only the --top N (default 20) with the most money moving in and out
are shown; the rest are combined as "Other holdings". --table writes the full numbers
for every holding to an xlsx file.

Usage:
    python holding_heatmap.py flows.xlsx [-o holding_heatmap.png] [--all-data]
                              [--top N] [--table PATH.xlsx] [--sheet NAME] [--no-labels]
"""

import argparse
import re
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, ListedColormap, Normalize, TwoSlopeNorm
from matplotlib.ticker import FuncFormatter

REQUIRED_COLUMNS = ["Date", "Amount", "To Holding", "From Holding"]
QUARTER_FORMAT = "Q%q %Y"  # e.g. "Q3 2026"

# Diverging scale for net flow: red = net outflow, neutral gray = zero, blue = net inflow.
DIVERGING = LinearSegmentedColormap.from_list(
    "net_flow",
    ["#a8282a", "#e34948", "#f0efec", "#2a78d6", "#14498a"],
)
SURFACE = "#fcfcfb"
INK = "#2b2b29"
MUTED_INK = "#6b6b66"

PAGE_SIZE = (11, 8.5)  # inches: US Letter, landscape
DPI = 300  # print quality
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


def parse_amount(value):
    """Convert an amount such as "1,234.56", "$ 1,234", "(500.00)" or "-1,000" to a float.

    Returns NaN if the value can't be read as a number.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value is None or pd.isna(value):
        return np.nan

    text = str(value).strip().replace("−", "-")  # unicode minus sign
    negative = False
    if text.startswith("(") and text.endswith(")"):  # accounting-style negative
        negative, text = True, text[1:-1].strip()
    if text.endswith("-"):  # trailing minus, e.g. "1,000-"
        negative, text = True, text[:-1].strip()

    # Drop thousands separators, currency symbols/codes and spaces.
    text = re.sub(r"[,\s$€£¥]|USD|EUR|GBP", "", text, flags=re.IGNORECASE)
    if not re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", text):
        return np.nan

    number = float(text)
    return -number if negative else number


def compact(value):
    """Short label: 1.2M, -365.7K, 950."""
    a = abs(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= size:
            return f"{value / size:,.1f}{suffix}"
    return f"{value:,.0f}"


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


def style_axes(ax):
    ax.tick_params(colors=INK, length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)


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


def latest_quarter(df):
    """Keep only the rows in the most recent calendar quarter that has any data.

    Returns (filtered rows, that quarter as a pandas Period).
    """
    quarter = df["Date"].dt.to_period("Q")
    latest = quarter.max()
    return df[quarter == latest], latest


def quarter_span(df):
    """The calendar quarters the rows cover, e.g. "Q3 2026" or "Q1 2025 – Q4 2026"."""
    quarters = df["Date"].dt.to_period("Q")
    first, last = quarters.min().strftime(QUARTER_FORMAT), quarters.max().strftime(QUARTER_FORMAT)
    return first if first == last else f"{first} – {last}"


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


def net_flow(amounts):
    """Each holding's net flow (money in minus money out) from the flow matrix.
    NaN for EXTERNAL, which isn't a holding."""
    net = amounts.sum(axis=0) - amounts.sum(axis=1)  # column = in, row = out
    return net.mask(net.index == EXTERNAL)


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


def plot_flow_matrix(amounts, counts, out_path, span, note, show_labels=True):
    """Draw the From x To matrix with each holding's net flow in a column beside it.

    `span` names the quarters summed (for the title); `note` starts the footer line.
    """
    names = list(amounts.index)
    n = len(names)
    values = amounts.to_numpy(dtype=float)
    count_values = counts.to_numpy(dtype=float)
    net_values = net_flow(amounts).to_numpy(dtype=float).reshape(-1, 1)

    moved_norm = Normalize(vmin=0.0, vmax=np.nanmax(values))
    net_limit = np.nanmax(np.abs(net_values)) if np.isfinite(net_values).any() else 0.0
    net_limit = net_limit or 1.0
    net_norm = TwoSlopeNorm(vmin=-net_limit, vcenter=0.0, vmax=net_limit)

    # Rough cell size (points) used to size the holding names before layout.
    row_pts = 72 * (PAGE_SIZE[1] - 3.6) / n
    name_fontsize = max(4, min(9, 0.7 * row_pts))
    gap = gap_width(row_pts)

    # Widths in inches: the net column gets enough room for its labels and colour bar.
    net_width = 1.3
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

    ax_net.tick_params(labelleft=False)
    ax_net.set_xticks([])
    ax_net.set_xlabel("Net flow", color=MUTED_INK, fontsize=10, fontweight="bold")

    for bar_ax, bar_mesh, label, ticks in (
            (cax, mesh, f"Amount moved, {span}", None),
            (cax_net, net_mesh, "Net flow  (− out / + in)", [-net_limit, 0.0, net_limit])):
        cbar = fig.colorbar(bar_mesh, cax=bar_ax, orientation="horizontal", ticks=ticks)
        cbar.outline.set_visible(False)
        cbar.ax.tick_params(colors=MUTED_INK, labelsize=7, length=0)
        cbar.ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: compact(v)))
        cbar.set_label(label, color=MUTED_INK, fontsize=8)

    fig.suptitle(f"Flows Between Holdings, {span}",
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


def write_table(df, path):
    """Write the full numbers for every holding (nothing combined) to an xlsx file."""
    amounts, counts = flow_matrix(df)
    with pd.ExcelWriter(path) as writer:
        amounts.rename_axis(index="From / To").to_excel(writer, sheet_name="Amount moved")
        counts.rename_axis(index="From / To").to_excel(writer, sheet_name="Transfers")
        (net_flow(amounts).drop(EXTERNAL, errors="ignore").rename("Net flow")
         .rename_axis("Holding").to_excel(writer, sheet_name="Net flow"))
    print(f"Saved full table to {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("xlsx", help="input .xlsx file with Date, Amount, To Holding, "
                                     "From Holding columns")
    parser.add_argument("-o", "--output", default="holding_heatmap.png",
                        help="output PNG path (default: holding_heatmap.png)")
    parser.add_argument("--all-data", action="store_true",
                        help="sum every row in the file (default: only the most recent "
                             "quarter in the file)")
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
    if args.all_data:
        print(f"Summing all {len(df):,} rows ({quarter_span(df)}).")
    else:
        df, quarter = latest_quarter(df)
        print(f"Summing only {quarter.strftime(QUARTER_FORMAT)} ({len(df):,} rows); "
              f"pass --all-data to sum every row.")

    if args.table:
        write_table(df, args.table)
    shown, n_combined = fold_holdings(df, args.top)
    if n_combined:
        print(f"{n_combined} holdings combined as \"{OTHER}\"; use --top to change how "
              f"many are shown, or --table for every holding.")

    amounts, counts = flow_matrix(shown)
    if amounts.empty:
        sys.exit("No To Holding or From Holding values found.")
    note = f"{len(df):,} transfers, {compact(df['Amount'].abs().sum())} moved in total."
    if n_combined:
        note += (f" Showing the {args.top} holdings with the most money moved; the other "
                 f"{n_combined} are combined as {OTHER}. Transfers between two of those "
                 f"aren't shown.")
    plot_flow_matrix(amounts, counts, args.output, quarter_span(df), note,
                     show_labels=not args.no_labels)
    print(f"Saved heat map for {len(amounts)} holdings to {args.output}")


if __name__ == "__main__":
    main()
