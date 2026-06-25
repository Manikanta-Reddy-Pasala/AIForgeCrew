---
name: data-analysis-notebook
description: Do data analysis in notebooks/pandas cleanly and reproducibly
triggers: [jupyter, notebook, pandas, data analysis, dataframe, csv, plot, numpy]
source: builtin
---

- **Reproducible**: cells run top-to-bottom from a fresh kernel; no hidden out-of-order state. Restart-and-run-all before trusting results.
- **Inspect before trusting**: `.shape`, `.head()`, `.info()`, `.describe()`, null counts. Know your data's types, ranges, and missingness first.
- **Vectorize** with pandas/numpy; avoid `iterrows` loops on big frames.
- **Don't mutate silently**: be explicit about copies vs views (`SettingWithCopyWarning`); chain or `.assign` deliberately.
- **One question per analysis**; show the steps so the conclusion is auditable. Label plots (title/axes/units).
- **Handle missing/outliers explicitly** and say how — don't let them skew aggregates.
- Move proven logic OUT of the notebook into tested functions/modules; notebooks are for exploration, not production.
