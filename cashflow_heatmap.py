#!/usr/bin/env python3
"""Build a heat map of monthly net cash flows per account from an xlsx file.

The input sheet must have a header row with the columns: Date, Amount, To, From.
Each row moves `Amount` out of the `From` account and into the `To` account, so
for every row the `To` account gets +Amount and the `From` account gets -Amount.

If an account named "Net Outflow" appears in the To column, its net flow is
shown with the sign reversed in a separate row along the bottom.

Usage:
    python cashflow_heatmap.py flows.xlsx [-o heatmap.png] [--sheet NAME] [--no-labels]
"""

import argparse
import math
import re
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.ticker import FuncFormatter

REQUIRED_COLUMNS = ["Date", "Amount", "To", "From"]
# If this account appears in the To column, its net flow is shown with the
# sign reversed in a separate row along the bottom of the heat map.
OUTFLOW_ACCOUNT = "Net Outflow"
# Accounts in the order they appear in the heat map, top to bottom.
ACCOUNT_ORDER = ["Estate", "Illiquid", "Semi-liquid", "Liquid", "Safe"]
# Accounts from ACCOUNT_ORDER that are left out when they have no entries in the file.
HIDE_IF_UNUSED = {"Estate"}

# Diverging scale: red = net outflow, neutral gray = zero, blue = net inflow.
DIVERGING = LinearSegmentedColormap.from_list(
    "net_flow",
    ["#a8282a", "#e34948", "#f0efec", "#2a78d6", "#14498a"],
)
SURFACE = "#fcfcfb"
INK = "#2b2b29"
MUTED_INK = "#6b6b66"

PAGE_SIZE = (11, 8.5)  # inches: US Letter, landscape
DPI = 300  # print quality


def parse_amount(value):
    """Convert an amount such as "1,234.56", "$ 1,234", "(500.00)" or "-1,000" to a float.

    Returns NaN if the value can't be read as a number.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value is None or pd.isna(value):
        return float("nan")

    text = str(value).strip().replace("−", "-")  # unicode minus sign
    negative = False
    if text.startswith("(") and text.endswith(")"):  # accounting-style negative
        negative, text = True, text[1:-1].strip()
    if text.endswith("-"):  # trailing minus, e.g. "1,000-"
        negative, text = True, text[:-1].strip()

    # Drop thousands separators, currency symbols/codes and spaces.
    text = re.sub(r"[,\s$€£¥]|USD|EUR|GBP", "", text, flags=re.IGNORECASE)
    if not re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", text):
        return float("nan")

    number = float(text)
    return -number if negative else number


def load_flows(path, sheet=0):
    df = pd.read_excel(path, sheet_name=sheet, dtype={"Amount": object})
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        sys.exit(f"Missing required column(s): {', '.join(missing)}. "
                 f"Found: {', '.join(df.columns)}")

    df = df[REQUIRED_COLUMNS].copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    raw_amounts = df["Amount"].copy()
    df["Amount"] = raw_amounts.map(parse_amount)
    df["To"] = df["To"].astype("string").str.strip()
    df["From"] = df["From"].astype("string").str.strip()

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


def monthly_net_flows(df):
    """Return a DataFrame indexed by account, one column per calendar month."""
    month = df["Date"].dt.to_period("M")
    inflows = pd.DataFrame({"Account": df["To"], "Month": month, "Net": df["Amount"]})
    outflows = pd.DataFrame({"Account": df["From"], "Month": month, "Net": -df["Amount"]})
    # A blank To/From (e.g. an external deposit or withdrawal) only affects the other side.
    legs = pd.concat([inflows, outflows]).dropna(subset=["Account"])
    legs = legs[legs["Account"] != ""]

    table = legs.pivot_table(index="Account", columns="Month", values="Net",
                             aggfunc="sum", fill_value=0.0)
    # Include every month in the range, even ones with no activity.
    all_months = pd.period_range(month.min(), month.max(), freq="M")
    table = table.reindex(columns=all_months, fill_value=0.0)
    return table.sort_index()


def split_outflow(table, df):
    """Pull the "Net Outflow" account out of the table if it appears in the To column.

    Returns (table without that account, its row with the sign reversed or None).
    The name is matched ignoring case and surrounding spaces.
    """
    is_outflow = df["To"].str.casefold() == OUTFLOW_ACCOUNT.casefold()
    if not is_outflow.any():
        return table, None
    names = set(df.loc[is_outflow, "To"])
    outflow = -table.loc[table.index.isin(names)]
    if len(outflow) > 1:  # differently-cased spellings: merge them
        outflow = outflow.sum().to_frame(OUTFLOW_ACCOUNT).T
    return table.loc[~table.index.isin(names)], outflow


def order_accounts(table):
    """Put the accounts in ACCOUNT_ORDER, matching names ignoring case and spaces.

    Every account in ACCOUNT_ORDER gets a row (zeros if it had no activity),
    except those in HIDE_IF_UNUSED, which are dropped if they have no entries.
    Any other account is kept, with a warning, below the expected ones.
    """
    canonical = {name.casefold(): name for name in ACCOUNT_ORDER}
    table = table.rename(index=lambda n: canonical.get(n.strip().casefold(), n))
    table = table.groupby(level=0).sum()  # merge differently-cased spellings

    unexpected = sorted(set(table.index) - set(ACCOUNT_ORDER))
    if unexpected:
        print(f"Warning: unexpected account(s) shown at the bottom: "
              f"{', '.join(unexpected)}", file=sys.stderr)
    # The table only has rows for accounts that appear in To or From.
    shown = [a for a in ACCOUNT_ORDER if a not in HIDE_IF_UNUSED or a in table.index]
    return table.reindex(shown + unexpected, fill_value=0.0)


def compact(value):
    a = abs(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= size:
            return f"{value / size:,.1f}{suffix}"
    return f"{value:,.0f}"


def to_grid(frame):
    """DataFrame -> list of rows of floats, which is what pcolormesh draws."""
    return [[float(v) for v in row] for row in frame.itertuples(index=False)]


def abs_max(grid):
    """Largest absolute value in a list of rows (1.0 if all are zero)."""
    return max(abs(v) for row in grid for v in row) or 1.0


def label_cells(ax, values, limit, fontsize):
    """Write each non-zero value in bold inside its cell."""
    for i, row in enumerate(values):
        for j, v in enumerate(row):
            if v == 0:
                continue
            dark_cell = abs(v) > 0.4 * limit
            ax.text(j + 0.5, i + 0.5, compact(v), ha="center", va="center",
                    fontsize=fontsize, fontweight="bold",
                    color="white" if dark_cell else INK)


def cell_fontsize(ax, values):
    """Largest font (max 9pt) at which every value fits inside its cell."""
    fig = ax.figure
    bbox = ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted())
    cell_w = 72 * bbox.width / len(values[0])
    cell_h = 72 * bbox.height / len(values)
    longest = max(len(compact(v)) for row in values for v in row)
    # Bold digits are a little wider than regular ones.
    return min(9, 0.6 * cell_h, cell_w / (0.68 * longest + 1))


def style_axes(ax):
    ax.tick_params(colors=INK, length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)


def plot_heatmap(table, out_path, show_labels=True, outflow=None):
    """Draw the heat map. `outflow`, if given, is a one-row DataFrame (same
    month columns as `table`) drawn as a separate row along the bottom."""
    n_accounts, n_months = table.shape
    values = to_grid(table)
    totals = [[sum(row)] for row in values]

    limit = abs_max(values)
    norm = TwoSlopeNorm(vmin=-limit, vcenter=0.0, vmax=limit)
    # The total column gets its own scale; on the monthly scale it would saturate.
    total_limit = abs_max(totals)
    total_norm = TwoSlopeNorm(vmin=-total_limit, vcenter=0.0, vmax=total_limit)

    n_rows = n_accounts + (outflow is not None)
    # Rough plot area (page minus margins, labels, total column and colour bar)
    # used to size tick labels before layout; points per row / column.
    row_pts = 72 * (PAGE_SIZE[1] - 2.0) / n_rows
    col_pts = 72 * (PAGE_SIZE[0] - 4.5) / n_months
    y_fontsize = max(4, min(9, 0.7 * row_pts))
    x_fontsize = max(4, min(9, 0.9 * col_pts))
    # With many months, label every n-th one so the dates don't overlap.
    x_step = max(1, math.ceil(x_fontsize * 1.6 / col_pts))

    # Total column is ~0.75in wide whatever the number of months.
    month_width = PAGE_SIZE[0] - 4.5
    total_width = 0.75
    fig = plt.figure(figsize=PAGE_SIZE, facecolor=SURFACE, layout="constrained")
    # ~0.3in margin all round for printing.
    fig.get_layout_engine().set(w_pad=0.3, h_pad=0.3, wspace=0.01, hspace=0.02)
    if outflow is None:
        grid = fig.add_gridspec(1, 3, width_ratios=[month_width, total_width, 0.18])
    else:
        # The outflow row is one account-row tall, set apart below the main grid.
        grid = fig.add_gridspec(2, 3, width_ratios=[month_width, total_width, 0.18],
                                height_ratios=[n_accounts, 1])
    ax = fig.add_subplot(grid[0, 0])
    ax_total = fig.add_subplot(grid[0, 1])
    cax = fig.add_subplot(grid[0, 2])

    # edgecolor = surface gives a thin gap between cells.
    mesh = ax.pcolormesh(values, cmap=DIVERGING, norm=norm,
                         edgecolors=SURFACE, linewidth=2)
    ax_total.pcolormesh(totals, cmap=DIVERGING, norm=total_norm,
                        edgecolors=SURFACE, linewidth=2)
    month_axes, total_axes = [ax], [ax_total]
    rows = [(ax, ax_total, values, totals, limit, total_limit)]

    if outflow is not None:
        out_values = to_grid(outflow)
        out_totals = [[sum(row)] for row in out_values]
        # The outflow row also gets its own scale. Its total uses the total
        # column's scale (clipped at the darkest shade if it is larger).
        out_limit = abs_max(out_values)
        out_norm = TwoSlopeNorm(vmin=-out_limit, vcenter=0.0, vmax=out_limit)
        ax_out = fig.add_subplot(grid[1, 0])
        ax_out_total = fig.add_subplot(grid[1, 1])
        ax_out.pcolormesh(out_values, cmap=DIVERGING, norm=out_norm,
                          edgecolors=SURFACE, linewidth=2)
        ax_out_total.pcolormesh(out_totals, cmap=DIVERGING, norm=total_norm,
                                edgecolors=SURFACE, linewidth=2)
        ax_out.set_yticks([0.5])
        ax_out.set_yticklabels(outflow.index, fontsize=y_fontsize, fontweight="bold")
        month_axes.append(ax_out)
        total_axes.append(ax_out_total)
        rows.append((ax_out, ax_out_total, out_values, out_totals,
                     out_limit, total_limit))

    for a in month_axes + total_axes:
        a.set_facecolor(SURFACE)
        a.invert_yaxis()
        a.set_xticks([])
        style_axes(a)
    for a in total_axes:
        a.set_yticks([])

    ax.set_yticks([i + 0.5 for i in range(n_accounts)])
    # Truncate very long account names so they don't squeeze the plot.
    ax.set_yticklabels([n if len(n) <= 40 else n[:39] + "…" for n in table.index],
                       fontsize=y_fontsize)

    # Month and "Total" labels go under the bottom-most row.
    month_axes[-1].set_xticks([j + 0.5 for j in range(0, n_months, x_step)])
    month_axes[-1].set_xticklabels(
        [p.strftime("%b %Y") for p in table.columns[::x_step]],
        rotation=45, ha="right", fontsize=x_fontsize)
    total_axes[-1].set_xticks([0.5])
    total_axes[-1].set_xticklabels(["Total"], fontsize=x_fontsize, fontweight="bold",
                                   rotation=45, ha="right")

    cbar = fig.colorbar(mesh, cax=cax)
    cbar.outline.set_visible(False)
    cbar.ax.tick_params(colors=MUTED_INK, labelsize=8, length=0)
    cbar.ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: compact(v)))
    cbar.set_label("Monthly net flow  (− out  /  + in)", color=MUTED_INK, fontsize=9)

    fig.suptitle("Sources and Uses", x=0.02, ha="left",
                 color=INK, fontsize=13, fontweight="bold")

    fig.draw_without_rendering()  # run the layout so cell sizes are final

    # Cell values, sized to the actual cell size; skipped when cells are too small.
    if show_labels:
        for month_ax, total_ax, vals, tots, lim, tot_lim in rows:
            fontsize = cell_fontsize(month_ax, vals)
            if fontsize >= 4.5:
                label_cells(month_ax, vals, lim, fontsize)
            fontsize = cell_fontsize(total_ax, tots)
            if fontsize >= 4.5:
                label_cells(total_ax, tots, tot_lim, fontsize)

    fig.savefig(out_path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("xlsx", help="input .xlsx file with Date, Amount, To, From columns")
    parser.add_argument("-o", "--output", default="net_flows_heatmap.png",
                        help="output PNG path (default: net_flows_heatmap.png)")
    parser.add_argument("--sheet", default=0,
                        help="sheet name or index to read (default: first sheet)")
    parser.add_argument("--no-labels", action="store_true",
                        help="don't print values inside the cells")
    args = parser.parse_args()

    sheet = int(args.sheet) if str(args.sheet).isdigit() else args.sheet
    df = load_flows(args.xlsx, sheet)
    if df.empty:
        sys.exit("No valid rows found.")

    table = monthly_net_flows(df)
    table, outflow = split_outflow(table, df)
    table = order_accounts(table)
    plot_heatmap(table, args.output, show_labels=not args.no_labels, outflow=outflow)
    print(f"Saved heat map for {table.shape[0]} accounts x {table.shape[1]} months "
          f"to {args.output}")


if __name__ == "__main__":
    main()
