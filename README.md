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

## Flows between holdings

`holding_heatmap.py` builds a heat map of how money moved between holdings. It uses the
same colors, page size and bold cell labels as `cashflow_heatmap.py`. The two scripts are
independent: each one runs on its own, without the other file.

![Example heat map for the latest quarter](example/sample_holding_heatmap.png)

The input sheet needs these columns, in any order. Any other columns, such as `To` and
`From`, are ignored.

| Column | Meaning |
|---|---|
| `Date` | Date of the flow. Dates may repeat. |
| `Amount` | Amount moved, in the same formats as above. |
| `To Holding` | Holding receiving the money (+Amount). |
| `From Holding` | Holding sending the money (−Amount). |

```bash
python holding_heatmap.py flows.xlsx -o holding_heatmap.png                 # last quarter
python holding_heatmap.py flows.xlsx --all-data -o holding_heatmap_all.png  # all of the data
```

**What gets summed:** by default, only the rows in the most recent calendar quarter that
appears in the file. `--all-data` sums every row instead
([example](example/sample_holding_heatmap_all.png)). The title shows the quarter, or the
range of quarters, and the script prints how many rows it used.

`--sheet` and `--no-labels` work as above. Two more options help with many holdings:

- `--top N` shows the N holdings with the most money moving in and out (default 20) and
  combines the rest into one **Other holdings** row and column. The footer says how many
  were combined. `--top 0` shows every holding; with more than about 30 the names and
  numbers get too small to read, but the colors still show the pattern.
- `--table PATH.xlsx` also writes the full numbers for every holding, with nothing
  combined, to an Excel file: the amount moved and number of transfers for each pair,
  and each holding's net flow.

What the chart shows:

- Each cell is the total moved from the row's holding to the column's holding, with the
  number of transfers underneath. Many transfers between the same two holdings add up
  into one cell. Empty gray cells had no transfers.
- The **Net flow** column is each holding's money in minus money out: blue is net in,
  red is net out.
- A blank `To Holding` or `From Holding` appears as **External**, so deposits and
  withdrawals are visible. A negative amount counts as a transfer the other way.
- Holdings are listed alphabetically, with Other holdings and External last.
- Transfers between two holdings that were both combined into Other holdings aren't
  shown (they don't change any net flow).
- When the numbers don't fit with the transfer counts, the counts are dropped; then the
  numbers are rounded (`870K`); then they're left out and the colors carry the pattern.

`example/sample_holding_flows.xlsx` is a made-up input file for this script.

## MSCI Data Coverage table

`msci_data_coverage.py` extracts the "Closed-end fund count and capitalization by asset class"
table from the Data Coverage page of an MSCI Private Capital Benchmarks Summary PDF and
writes it to an Excel file. It's independent of the heat map scripts.

```bash
python msci_data_coverage.py report.pdf -o data_coverage.xlsx             # page 6 (default)
python msci_data_coverage.py report.pdf --page 7 -o data_coverage.xlsx    # if the page moves
```

- The sheet has one row per line of the table, with the columns Asset class, Parent,
  Level, Funds, FoFs, Total and Cap (USD M). Sub-classes (level 2) are named with their
  asset class, e.g. `Equity | Buyout` or `Credit | Generalist`, so every name is unique.
- A `-` in the PDF becomes an empty cell. Values are copied exactly as printed.
- The table is found by its header row (Asset class / Funds / FoFs / Total / Cap), not by
  fixed positions, so small layout changes between reports shouldn't break it. If the
  header isn't on the page, the script says so.
- The script checks that Funds + FoFs = Total on every row and that each group's rows add
  up to the group (allowing for rounding in Cap), and prints a note for any that don't. In
  the Q2 2026 report, for example, Credit's 48 FoFs aren't broken down by sub-class.

The extracted data comes from MSCI's report, so don't commit the output file to this
public repository.
