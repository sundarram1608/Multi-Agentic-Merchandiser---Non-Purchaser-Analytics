# Manual Eval Prompts — Ground Truth vs Agentic Answer

A curated set of 15 prompts spanning three difficulty tiers. Use these
to manually evaluate whether the chat's answers actually match the
underlying data — not just whether the SQL pipeline ran without errors
(that's `test_golden_sql.py`'s job).

This file is the companion to `golden_sql_dataset.jsonl`:

| | Golden SQL (automated) | This file (manual) |
|---|---|---|
| Question answered | "Did the pipeline break?" | "Is the answer correct?" |
| Verifies | SQL shape, Planner path, row count bounds | Numeric accuracy, completeness, semantic attribution |
| Runs in | CI / pre-commit | Manual, with Excel pivot tables alongside |
| Cadence | Every prompt change | Weekly, or before/after big changes |

---

## How to use this file

For each prompt below:

1. **Compute the ground truth in Excel first.** Open the seed CSV
   (`data_sample/non_purchasers_feedback.csv`) or pull a fresh copy
   from MySQL. Build the pivot table(s) described in the "Ground
   truth" line. Record the exact expected numbers.

2. **Ask the chat the prompt verbatim.** Note the answer text, the
   SQL from "Show SQL", and the agent trace (especially the
   `coder · ok · SQL produced` detail with the reasoning block).

3. **Score the response on three axes** (use ✓ / ✗ / partial):

   - **Numeric accuracy** — do the numbers in the answer match
     Excel exactly? (allow ±0 for counts, ±1pp for percentages)
   - **Completeness** — did the agent include every group /
     dimension you'd expect, or did it truncate (LIMIT bug) or
     silently merge categories?
   - **Attribution correctness** — when the agent says "Necklace
     has 41 design issues at X1", is 41 *actually* the count for
     (Necklace + design + X1), and not some other slice that
     happens to equal 41?

4. **For Tier 3**, also score:

   - **Discovery correctness** — did the agent re-discover the
     "top" entities at the new scope, or did it copy-paste names
     from a previous turn? (Tests the Planner's hierarchical-
     analysis Case H rule.)

5. **Log the results** in a spreadsheet (or in
   `tests/eval_results_YYYY-MM-DD.csv` if you want a versioned
   record). After 15 prompts you'll have a 15×3 (or 15×4 for
   Tier 3) matrix telling you which tier the system is weakest at
   and which kind of error it makes most often.

---

## Tier 1 — Easy (single-level aggregation)

One `GROUP BY` (or a bare `COUNT(*)`). A single Excel pivot answers
each. The system should nail these on the first attempt with no
retries; if any of these go through a retry or the Supervisor, it's
a regression worth investigating.

### Prompt 1 — total feedback count

> What is the total number of non-purchase feedbacks across all stores?

- **Tests:** bare `COUNT(*)`, no GROUP BY
- **Ground truth:** count of rows in `non_purchasers_feedback`
  (should be ~1,200)
- **Expected SQL shape:** `SELECT COUNT(*) AS total FROM non_purchasers_feedback`

### Prompt 2 — top store by volume

> Which store has the highest number of non-purchase feedbacks, and what's the count?

- **Tests:** one GROUP BY on `store_code`, top-1
- **Ground truth:** pivot with `store_code` as Rows, Count of
  `customer_name` as Values, sort descending → top row
- **Expected SQL shape:** `SELECT store_code, COUNT(*) AS n FROM
  non_purchasers_feedback GROUP BY store_code ORDER BY n DESC
  LIMIT 1`

### Prompt 3 — feedback count per product

> What's the total feedback count for each product across all stores?

- **Tests:** one GROUP BY on `product_looking_for`
- **Ground truth:** pivot `product_looking_for` as Rows, Count as
  Values → 5 rows (Necklace / Anklets / Ear Rings / Finger Rings
  / Bangles)
- **Expected SQL shape:** `SELECT product_looking_for, COUNT(*) ...
  GROUP BY product_looking_for ORDER BY n DESC` (no LIMIT)

### Prompt 4 — non_purchase_type breakdown

> Show me the breakdown of feedbacks by non_purchase_type across the whole dataset.

- **Tests:** one GROUP BY on `non_purchase_type`. **CRITICAL**:
  this is the disambiguation test — the system MUST pick
  `non_purchase_type` (not `attribute_value`) because the user
  named the column directly.
- **Ground truth:** pivot `non_purchase_type` as Rows, Count as
  Values → 9 rows (design / size / color / weight / price /
  customization / service / stock / none)
- **Expected SQL shape:** `SELECT non_purchase_type, COUNT(*) ...
  GROUP BY non_purchase_type` (no `attribute_value IS NOT NULL`
  filter)

### Prompt 5 — user_category breakdown

> What's the feedback distribution across user categories?

- **Tests:** one GROUP BY on `user_category`
- **Ground truth:** pivot `user_category` as Rows, Count as Values →
  6 rows (Babies, Teen-age Girls, Office-going Women, Everyday
  Wear, Wedding, Birthday)
- **Expected SQL shape:** `SELECT user_category, COUNT(*) ... GROUP
  BY user_category`

---

## Tier 2 — Complex (two-dimension aggregation, top-N per group)

Compound GROUP BY plus per-group ranking. Excel needs a pivot with
two row dimensions, or a manual top-N filter. The system should
usually pass on the first Coder attempt; one Code Reviewer retry is
acceptable. The reasoning block in the agent trace should explicitly
name TWO dimensions.

### Prompt 6 — top non_purchase_type per store

> What is the top non_purchase_type at each store?

- **Tests:** GROUP BY `(store_code, non_purchase_type)` + top-1
  per store via `ROW_NUMBER() OVER (PARTITION BY store_code ORDER
  BY n DESC)`
- **Ground truth:** pivot with `store_code` as Rows,
  `non_purchase_type` as Columns, Count as Values → for each row,
  pick the largest cell. Expect 6 rows total.
- **Expected SQL shape:** CTE with ROW_NUMBER, outer SELECT
  WHERE rnk = 1

### Prompt 7 — top 3 non_purchase_types per product

> For each product, what are the top 3 non_purchase_types?

- **Tests:** GROUP BY `(product_looking_for, non_purchase_type)`
  + ROW_NUMBER per product, filter rnk ≤ 3
- **Ground truth:** pivot with `product_looking_for` as Rows,
  `non_purchase_type` as Columns, sort each row descending, take
  top 3 → 15 cells total (5 products × top 3)
- **Expected SQL shape:** CTE with COUNT, ROW_NUMBER OVER
  (PARTITION BY product_looking_for ORDER BY n DESC), outer
  SELECT WHERE rnk ≤ 3

### Prompt 8 — comparison between two stores

> Compare the most-requested products at stores X1 and X4.

- **Tests:** GROUP BY `(store_code, product_looking_for)` with
  filter `store_code IN ('X1','X4')` + top-N per store
- **Ground truth:** filter pivot to `store_code IN ('X1','X4')`,
  Rows = `product_looking_for`, Columns = `store_code`, Values =
  Count. Sort each column descending.
- **Expected SQL shape:** `WHERE store_code IN ('X1','X4') GROUP
  BY store_code, product_looking_for ORDER BY store_code, n
  DESC`

### Prompt 9 — top attribute_values for Necklace

> What are the top 5 attribute_values customers ask for in Necklaces across all stores?

- **Tests:** filter `product_looking_for='Necklace' AND
  attribute_value IS NOT NULL`, GROUP BY `attribute_value`, top 5
- **Ground truth:** filter pivot to `product_looking_for='Necklace'`
  and `attribute_value IS NOT NULL`, Rows = `attribute_value`,
  Values = Count, sort descending → top 5
- **Expected SQL shape:** `WHERE product_looking_for='Necklace'
  AND attribute_value IS NOT NULL GROUP BY attribute_value
  ORDER BY n DESC LIMIT 5`

### Prompt 10 — top 2 non_purchase_types per user_category

> For each user category, list the top 2 non_purchase_types.

- **Tests:** GROUP BY `(user_category, non_purchase_type)` +
  top-2 per user_category
- **Ground truth:** pivot Rows = `user_category`, Columns =
  `non_purchase_type`, take top 2 per row → 12 cells (6 user
  categories × top 2)
- **Expected SQL shape:** CTE + ROW_NUMBER OVER (PARTITION BY
  user_category ORDER BY n DESC), outer SELECT WHERE rnk ≤ 2

---

## Tier 3 — Very complex (multi-step, discover-then-drilldown)

At least two CTEs with sequential aggregations. The "discovered"
step depends on the result of a previous aggregation. Excel
verification requires multiple pivot tables and careful filtering /
manual computation. The system's chain-of-thought scaffolding,
hierarchical-analysis rule, and reasoning verification all get
exercised together at this tier.

### Prompt 11 — top stores → top products → top types (three-level discovery)

> For the top 2 stores by overall feedback volume, find the top 2 products at each of those stores, and then for each (store, product) combination, give me the top 3 non_purchase_types.

- **Tests:** 3-level nested CTE — discover top stores → discover
  top products per top store → top 3 types per (store, product).
  This is the canonical hierarchical-analysis case.
- **Result shape:** up to 12 rows (2 stores × 2 products × 3
  types)
- **Ground truth:**
  - Pivot 1: store_code Rows, Count Values → top 2 stores
  - Pivot 2: filter to those stores, `product_looking_for` Rows,
    Count Values per store → top 2 products per store
  - Pivot 3: filter to each (store, product) pair,
    `non_purchase_type` Rows, Count Values → top 3 types per
    pair
- **Expected SQL shape:** three CTEs (top_stores, top_products,
  type_counts) with ROW_NUMBER windows partitioned appropriately

### Prompt 12 — top product per store → top attribute_value within

> At each store, what's the most-requested attribute_value within that store's top product?

- **Tests:** 2-level CTE — top product per store → top
  attribute_value within (store, top_product). Discovery at
  level 1 feeds the filter at level 2.
- **Result shape:** 6 rows (one per store)
- **Ground truth:**
  - Pivot 1: store_code Rows, product_looking_for Columns, Count
    Values → pick top product per store row
  - Pivot 2: for each (store, top_product) pair, filter and find
    the largest attribute_value
- **Expected SQL shape:** CTE for top product per store, JOIN
  back to the table filtered to (store, top_product), group by
  attribute_value, take top 1 per store

### Prompt 13 — diversity ranking

> Rank stores by the diversity of their non_purchase_type mix — measured by how many distinct non_purchase_types each store has at least 5 feedbacks for.

- **Tests:** nested aggregation — count per (store,
  non_purchase_type), filter ≥ 5, then COUNT DISTINCT types per
  store, sort
- **Result shape:** 6 rows (one per store)
- **Ground truth:**
  - Pivot 1: store_code Rows, non_purchase_type Columns, Count
    Values
  - For each row (store), count how many cells are ≥ 5
  - Sort stores by that count descending
- **Expected SQL shape:** CTE with COUNT GROUP BY (store, type),
  outer SELECT with COUNT(DISTINCT type) per store WHERE n ≥ 5,
  ORDER BY diversity DESC

### Prompt 14 — products dominated by size complaints

> Find every product where size-related complaints (non_purchase_type='size') account for more than 30% of total feedbacks for that product, and rank them by that percentage.

- **Tests:** ratio aggregation — total per product + size-filtered
  per product → compute % → filter > 30 → sort
- **Result shape:** up to 5 rows (whichever products clear the
  threshold)
- **Ground truth:**
  - Pivot 1: product Rows, Count Values → total per product
  - Pivot 2: filter to non_purchase_type='size', product Rows,
    Count Values → size count per product
  - Manual column in spreadsheet: size_count / total_count * 100
  - Filter > 30, sort descending
- **Expected SQL shape:** CTE with total per product, CTE with
  size count per product, JOIN with computed ratio, WHERE ratio
  > 30, ORDER BY ratio DESC

### Prompt 15 — Wedding-dominated (store, product) combinations

> Across all six stores, find the (store, product) combinations where the user category dominating non-purchase requests is 'Wedding' rather than the overall most-asked user category for that product. Show the count gap.

- **Tests:** comparison across two different aggregations —
  overall top `user_category` per product vs Wedding-specific
  count per (store, product); find where they diverge
- **Result shape:** variable (likely 5–15 rows)
- **Ground truth:**
  - Pivot 1: product Rows, user_category Columns, Count Values
    → identify the overall top user_category per product
  - Pivot 2: filter to user_category='Wedding', (store, product)
    Rows, Count Values
  - Pivot 3: at each (store, product), what's the actual top
    user_category? (one more pivot per row, or sort within
    pivot)
  - Flag rows where pivot-3 top differs from pivot-1 top, AND
    the pivot-3 top is 'Wedding'
  - For each flagged row, compute gap = Wedding_count -
    second_user_category_count_within_that_store_product
- **Expected SQL shape:** multiple CTEs — overall_top per
  product, per_combo top, Wedding-specific counts, JOIN with
  comparison conditions

---

## Scoring matrix template

Track your results in a spreadsheet like this:

| # | Tier | Prompt | Numeric ✓/✗ | Complete ✓/✗ | Attribution ✓/✗ | Discovery ✓/✗ | Notes |
|---|------|--------|-------------|--------------|------------------|----------------|-------|
| 1 | T1   | ...    |             |              |                  | N/A            |       |
| ... | ... | ... | ... | ... | ... | ... | ... |

The "Discovery" column applies only to Tier 3.

After running all 15:

- **Aggregate per-tier accuracy** — what percent of Tier 1 / 2 / 3 passed all axes?
- **Identify the dominant failure mode** — is the system mostly losing on numeric accuracy, attribution, or completeness?
- **Compare across runs** — if you re-run after a prompt change or model upgrade, has the failure profile shifted?

If a prompt fails repeatedly, consider promoting a structural version
of it into `golden_sql_dataset.jsonl` so future regressions get caught
automatically. Tier 1 prompts are the easiest to promote (predictable
SQL shape); Tier 2 with care; Tier 3 stays manual.
