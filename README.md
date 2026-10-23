# Cash flow heat map

Builds a "Sources and Uses" heat map of quarterly (or monthly) net cash flows per account from
an Excel file, sized to print on US Letter paper in landscape (11 × 8.5 in, 300 dpi PNG).

![Example heat map](example/sample_heatmap.png)

## Input

An `.xlsx` file whose first sheet has a header row with these columns:

| Column | Meaning |
|---|---|
| `Date` | Date of the flow. Dates may repeat. |
| `Amount` | Amount moved. Numbers or text such as `"1,234.56"`, `"$1,500"` or `"(250.00)"`. |
| `To` | Account receiving the money (+Amount). |
| `From` | Account sending the money (−Amount). |

Other columns, such as `To Holding` and `From Holding`, may be included and are ignored.
Columns can be in any order.

Rows with an unreadable date or amount are skipped, and the script lists them.

## Usage

```bash
pip install -r requirements.txt
python cashflow_heatmap.py flows.xlsx -o heatmap.png
```

Options:

- `--period quarterly|monthly` sums the flows per calendar quarter (the default) or month.
- `--sheet NAME` reads a sheet other than the first.
- `--no-labels` leaves the numbers out of the cells.

## What the chart shows

- One row per account, in the order **Estate, Illiquid, Semi-liquid, Liquid, Safe**
  (`ACCOUNT_ORDER`). Estate is hidden if it never appears in the file (`HIDE_IF_UNUSED`).
  Any other account name is shown at the bottom with a warning.
- One column per calendar quarter (or month with `--period monthly`), plus a
  **Total** column for the whole period.
- Blue is net money in, red is net money out, gray is zero. The numbers are in bold.
- If an account named **Net Outflow** appears in the `To` column, it is shown in a
  separate row along the bottom with its sign reversed.
- The Total column and the Net Outflow row each have their own color scale; the
  legend applies to the quarterly (or monthly) cells of the main grid.

`example/` contains a made-up input file and the heat map it produces.

## Cumulative net flow by holding

`holding_heatmap.py` shows how money moved between holdings. It uses the same colors, page
size and bold cell labels as `cashflow_heatmap.py`, and has two views:

- **Latest quarter (default):** a From × To matrix of everything moved between holdings in
  the most recent quarter in the file, with each holding's net flow beside it.
- **`--all-data`:** the running total of net flow into each holding, by quarter (or month),
  across the whole file.

![Example flow matrix for the latest quarter](example/sample_holding_matrix.png)

![Example holding heat map, made with --all-data](example/sample_holding_heatmap.png)

The input sheet needs these columns, in any order. Any other columns, such as `To` and
`From`, are ignored.

| Column | Meaning |
|---|---|
| `Date` | Date of the flow. Dates may repeat. |
| `Amount` | Amount moved, in the same formats as above. |
| `To Holding` | Holding receiving the money (+Amount). |
| `From Holding` | Holding sending the money (−Amount). |

```bash
python holding_heatmap.py flows.xlsx -o holding_heatmap.png                    # latest quarter
python holding_heatmap.py flows.xlsx --period monthly -o latest_by_month.png  # net by month
python holding_heatmap.py flows.xlsx --all-data -o all_quarters.png           # every quarter
```

`--sheet` and `--no-labels` work as above. Two more options help with many holdings:

- `--top N` shows the N holdings with the most money moving in and out (default 20) and
  combines the rest into one **Other holdings** row (and column). The footer says how many
  were combined. `--top 0` shows every holding; with more than about 30 the names and
  numbers get too small to read, but the colors still show the pattern.
- `--table PATH.xlsx` also writes the full numbers for every holding, with nothing
  combined, to an Excel file: the amount moved and number of transfers for each pair,
  and the cumulative net flow.

- **Latest quarter (default):** only rows dated in the most recent calendar quarter that
  appears in the file are used. The quarter is shown in the title and printed when the
  script runs.
  - Each matrix cell is the total moved from the row's holding to the column's holding
    that quarter, with the number of transfers underneath. Many transfers between the
    same two holdings add up into one cell. Empty gray cells had no transfers.
  - A blank `To Holding` or `From Holding` appears as **External**, so deposits and
    withdrawals are visible. A negative amount counts as a transfer the other way.
  - The **Net flow** column (three columns, one per month, with `--period monthly`) is
    each holding's in minus out since the start of the quarter.
  - Transfers between two holdings that were both combined into Other holdings aren't
    shown in the matrix (they don't change Other holdings' net flow).
  - When the numbers don't fit with the transfer counts, the counts are dropped; then the
    numbers are rounded (`870K`); then they're left out and the colors carry the pattern.
- **`--all-data`:** every row is used. Each cell is the holding's net flow (in minus out)
  added up from the first period in the file through the end of that period, and the
  title shows the range of quarters. Periods with no activity carry the total forward.
  A blank `To Holding` or `From Holding` only affects the other side.
- Holdings are listed alphabetically.
- If the numbers don't fit in the cells (e.g. many months), they're rounded to whole
  thousands or millions (`-366K`) instead of one decimal place.

`example/sample_holding_flows.xlsx` is a made-up input file for this script.
