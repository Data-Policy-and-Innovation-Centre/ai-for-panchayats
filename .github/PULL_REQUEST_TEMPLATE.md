## Summary

-

## What Changed

-

## Validation

- [ ] `uv run ruff check .`
- [ ] `uv run pytest`
- [ ] Notebooks have been stripped with `uv run nbstripout --install` or equivalent.
- [ ] No proprietary data files are committed directly to Git.
- [ ] DVC pointers / `dvc.lock` are updated when approved data or pipeline outputs changed.

## Review Expectations

- Keep this PR small enough to be reviewed by a human in a short sitting.
- Split unrelated changes into separate PRs before requesting review.
- Two-person review is required.
- Yashaswi is a mandatory reviewer.
- Add one additional reviewer who can assess the code, analysis, or domain impact.

## Data And Delivery Notes

- [ ] This PR does not modify data.
- [ ] This PR ingests or versions data through DVC.
- [ ] This PR updates generated exhibits.
- [ ] This PR requires `make deliver` after merge.

Notes:

-
