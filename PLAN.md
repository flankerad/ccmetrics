# ccmetrics owns the status line alone

- [ ] apply replaces, never wraps — ccmetrics/plan.py apply_setup: new command is always the plain 'ccmetrics statusline'; drop the --passthrough wrap; displaced command still named in old/message; .bak-ccmetrics copy unchanged
- [ ] apply unwraps an older build's wrap — ccmetrics/plan.py apply_setup already-wired branch: if _extract_passthrough is not None, back up, rewrite to plain, changed=True, name the dropped command; ours-and-plain keeps today's no-op
- [ ] revert restores from the backup — ccmetrics/plan.py revert_setup: (a) --passthrough present, restore it; (b) else read .bak-ccmetrics sibling BEFORE the _backup overwrite and restore its statusLine.command when _is_ours_command rejects it; (c) else delete statusLine; malformed backup never raises
- [ ] tests — tests/test_setup.py, tests/test_first_run_setup.py: wrap assertions become replace assertions; add backup-holds-displaced, revert-restores-from-backup, revert-without-backup-removes, apply-unwraps-old-build
- [ ] docs — README.md privacy promise changes from 'keeps any status line you already had' to takes-the-slot-and-backs-up; fix stale '76-test suite green'; DECISIONS.md entry superseding D13
