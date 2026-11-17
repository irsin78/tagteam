# tagteam

A small harness for one developer who uses Claude Code and Codex (and optionally
Gemini through `agy`) together: one orchestrator session splits the work, worker
sessions design and implement, another vendor reviews, and only the orchestrator
commits.

**Status: being rebuilt.** The previous version is in the git history. This
README will be rewritten when the rebuild lands.

Install into a project:

    python install.py <project dir>
    python install.py <project dir> --check

Optional benchmark data: with an Artificial Analysis API key in the environment
variable `TAGTEAM_AA_API_KEY`, the weekly model collector reads scores and prices
from the free Data API. Model benchmark data: [Artificial Analysis](https://artificialanalysis.ai/).

License: see LICENSE.
