# Grondstoffenmonitor

Dagelijks bijgewerkte prijzen van grondstoffen voor ijsproductie: zuivel (melk, boter), suiker, glucose, cacao, koffie, verpakking (karton, plastic), de euro-dollarkoers en containervracht Shanghai → Rotterdam.

- **Website:** https://grondstoffenmonitor.github.io
- **Bijwerken:** elke ochtend via GitHub Actions (`.github/workflows/update.yml`). Handmatig: tabblad *Actions* → *Cijfers bijwerken* → *Run workflow*.
- **Script:** `scripts/fetch.py` haalt de cijfers op en schrijft `data/data.json`.
- **Historie:** `data/boter.csv`, `data/melk.csv` en `data/wci.csv` groeien elke week/maand aan.

Bronnen: ECB, Eurostat, IMF/FRED, ZuivelNL, EDF-ZuivelNL Internationale Melkprijsvergelijking en Drewry World Container Index.
